"""
Core Runtime Engine for MCPFactory.

Dynamically compiles a declarative `MCPBlueprintConfig` into:
1. An official `mcp.server.Server` instance (supporting `stdio` and `SSE` transports).
2. A stateless MCP JSON-RPC 2.0 protocol dispatcher (supporting Cloud Run Streamable HTTP `POST /mcp`).
3. Direct programmatic execution helpers for tools, resources, and prompts.
"""

from __future__ import annotations

import inspect
import logging
import re
from pathlib import Path
from typing import Any, Optional

from mcp_factory.connectors import (
    BaseConnector,
    GraphQLConnector,
    MockConnector,
    PythonPluginConnector,
    RestConnector,
    load_python_handler,
)
from mcp_factory.schema import (
    MCPBlueprintConfig,
    PromptConfig,
    ResourceConfig,
    ToolConfig,
    load_blueprint,
)
from mcp_factory.transformer import apply_response_transform, format_for_mcp

logger = logging.getLogger("mcp_factory.engine")


def _uri_template_to_regex(uri_template: str) -> re.Pattern:
    """
    Converts an RFC 6570 simple URI template like `orders://users/{user_id}/items/{item_id}`
    into a named-group regular expression.
    """
    escaped = re.escape(uri_template)
    # Replace escaped \{param\} with named capture group (?P<param>[^/]+)
    pattern = re.sub(r"\\\{([a-zA-Z0-9_]+)\\\}", r"(?P<\1>[^/]+)", escaped)
    return re.compile(f"^{pattern}$")


class MCPFactoryEngine:
    """
    Takes a validated `MCPBlueprintConfig` and dynamically registers all
    backends, tools, resources, resource templates, and prompts.
    """

    def __init__(self, config: MCPBlueprintConfig) -> None:
        self.config = config
        self.connectors: dict[str, BaseConnector] = {}
        self.tools_by_name: dict[str, ToolConfig] = {t.name: t for t in config.tools}
        self.prompts_by_name: dict[str, PromptConfig] = {p.name: p for p in config.prompts}
        self.static_resources: dict[str, ResourceConfig] = {}
        self.template_resources: list[tuple[re.Pattern, ResourceConfig]] = []

        self._python_connector = PythonPluginConnector()
        self._mock_connector = MockConnector()

        self._init_backends()
        self._init_resources()
        self.mcp_server = self._build_official_mcp_server()

    @classmethod
    def from_file(cls, path: str | Path) -> "MCPFactoryEngine":
        """Loads a JSON or YAML blueprint file and returns an initialized `MCPFactoryEngine`."""
        blueprint = load_blueprint(path)
        return cls(blueprint)

    def _init_backends(self) -> None:
        """Instantiates backend connectors based on `config.backends`."""
        for name, backend_cfg in self.config.backends.items():
            if backend_cfg.type == "rest":
                self.connectors[name] = RestConnector(backend_cfg)
            elif backend_cfg.type == "graphql":
                self.connectors[name] = GraphQLConnector(backend_cfg)
            elif backend_cfg.type == "python":
                self.connectors[name] = PythonPluginConnector(backend_cfg)
            elif backend_cfg.type == "mock":
                self.connectors[name] = MockConnector(backend_cfg)
            else:
                raise ValueError(f"Unsupported backend type '{backend_cfg.type}' for backend '{name}'.")

    def _init_resources(self) -> None:
        """Separates static resources from RFC 6570 URI template resources."""
        for res in self.config.resources:
            if res.is_template:
                self.template_resources.append((_uri_template_to_regex(res.uri), res))
            else:
                self.static_resources[res.uri] = res

    def _get_connector_for_tool(self, tool: ToolConfig) -> BaseConnector:
        """Resolves the right connector instance for a given tool."""
        if tool.connector == "python":
            return self._python_connector
        if tool.connector == "mock":
            return self._mock_connector

        if tool.backend and tool.backend in self.connectors:
            conn = self.connectors[tool.backend]
            # Allow a tool on a REST backend to override connector="graphql" or vice versa
            if tool.connector == "graphql" and not isinstance(conn, GraphQLConnector):
                return GraphQLConnector(self.config.backends[tool.backend])
            if tool.connector == "rest" and not isinstance(conn, RestConnector):
                return RestConnector(self.config.backends[tool.backend])
            return conn

        raise ValueError(f"Cannot resolve connector for tool '{tool.name}'.")

    # =========================================================================
    # High-Level Execution Methods (Tools, Resources, Prompts)
    # =========================================================================

    async def execute_tool(self, name: str, arguments: Optional[dict[str, Any]] = None) -> Any:
        """Executes a configured MCP tool by name and returns the transformed response data."""
        tool = self.tools_by_name.get(name)
        if not tool:
            raise KeyError(f"Unknown tool '{name}'. Available tools: {list(self.tools_by_name.keys())}")

        connector = self._get_connector_for_tool(tool)
        args = arguments or {}
        logger.info("Executing tool '%s' via connector '%s' with args=%s", name, tool.connector, args)
        return await connector.execute(tool, args)

    async def read_resource_by_uri(self, uri: str) -> tuple[str, str]:
        """
        Reads a static or templated resource by URI.
        Returns `(content_text, mime_type)`.
        """
        target_res: Optional[ResourceConfig] = self.static_resources.get(uri)
        extracted_params: dict[str, Any] = {}

        if not target_res:
            for pattern, res_cfg in self.template_resources:
                match = pattern.match(uri)
                if match:
                    target_res = res_cfg
                    extracted_params = match.groupdict()
                    break

        if not target_res:
            raise KeyError(f"Resource URI not found: '{uri}'")

        # 1. Static content
        if target_res.static_content is not None:
            transformed = apply_response_transform(target_res.static_content, target_res.response)
            return format_for_mcp(transformed), target_res.mime_type

        # 2. Custom Python handler
        if target_res.handler:
            func = load_python_handler(target_res.handler)
            raw = await func(**extracted_params) if inspect.iscoroutinefunction(func) else func(**extracted_params)
            transformed = apply_response_transform(raw, target_res.response)
            return format_for_mcp(transformed), target_res.mime_type

        # 3. Backend REST endpoint
        if target_res.backend and target_res.endpoint:
            synthetic_tool = ToolConfig(
                name=f"_resource_{target_res.name}",
                description=target_res.description,
                backend=target_res.backend,
                connector="rest",
                method="GET",
                endpoint=target_res.endpoint,
                response=target_res.response,
            )
            connector = self.connectors[target_res.backend]
            result = await connector.execute(synthetic_tool, extracted_params)
            return format_for_mcp(result), target_res.mime_type

        return format_for_mcp({"uri": uri, "name": target_res.name}), target_res.mime_type

    def render_prompt(self, name: str, arguments: Optional[dict[str, str]] = None) -> dict[str, Any]:
        """Renders a configured MCP prompt template with the given arguments."""
        prompt_cfg = self.prompts_by_name.get(name)
        if not prompt_cfg:
            raise KeyError(f"Unknown prompt '{name}'. Available prompts: {list(self.prompts_by_name.keys())}")

        args = arguments or {}
        for arg_def in prompt_cfg.arguments:
            if arg_def.required and arg_def.name not in args:
                raise ValueError(f"Missing required argument '{arg_def.name}' for prompt '{name}'.")

        rendered_text = prompt_cfg.template
        for k, v in args.items():
            rendered_text = rendered_text.replace(f"{{{k}}}", str(v))

        return {
            "description": prompt_cfg.description,
            "messages": [
                {
                    "role": prompt_cfg.role,
                    "content": {
                        "type": "text",
                        "text": rendered_text,
                    },
                }
            ],
        }

    def get_catalog_summary(self) -> dict[str, Any]:
        """Returns a human- and machine-readable catalog of the server's capabilities."""
        return {
            "server": self.config.server.model_dump(),
            "backends": {
                k: {
                    "type": v.type,
                    "base_url": v.base_url,
                    "auth_type": v.auth.type.value,
                }
                for k, v in self.config.backends.items()
            },
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "connector": t.connector,
                    "backend": t.backend,
                    "method": t.method if t.connector == "rest" else None,
                    "endpoint": t.endpoint if t.connector in ("rest", "graphql") else None,
                    "inputSchema": t.build_json_schema(),
                }
                for t in self.config.tools
            ],
            "resources": [
                {
                    "uri": r.uri,
                    "name": r.name,
                    "description": r.description,
                    "mimeType": r.mime_type,
                    "isTemplate": r.is_template,
                }
                for r in self.config.resources
            ],
            "prompts": [p.model_dump() for p in self.config.prompts],
        }

    # =========================================================================
    # Stateless MCP JSON-RPC 2.0 Protocol Dispatcher (For Cloud Run POST /mcp)
    # =========================================================================

    async def handle_jsonrpc(self, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        """
        Handles standard MCP JSON-RPC 2.0 messages directly over stateless HTTP POST.
        Enables horizontal auto-scaling on Google Cloud Run without sticky-session state issues.
        """
        jsonrpc_version = payload.get("jsonrpc", "2.0")
        req_id = payload.get("id")
        method = payload.get("method", "")
        params = payload.get("params") or {}

        # Notifications (no id) do not expect a response body
        if req_id is None and method.startswith("notifications/"):
            return None

        try:
            if method == "initialize":
                result: dict[str, Any] = {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                        "prompts": {"listChanged": False},
                    },
                    "serverInfo": {
                        "name": self.config.server.name,
                        "version": self.config.server.version,
                    },
                }
                if self.config.server.instructions:
                    result["instructions"] = self.config.server.instructions

            elif method == "ping":
                result = {}

            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "inputSchema": t.build_json_schema(),
                        }
                        for t in self.config.tools
                    ]
                }

            elif method == "tools/call":
                tool_name = params.get("name", "")
                tool_args = params.get("arguments") or {}
                output = await self.execute_tool(tool_name, tool_args)
                is_error = isinstance(output, dict) and output.get("error") is True
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": format_for_mcp(output),
                        }
                    ],
                    "isError": is_error,
                }

            elif method == "resources/list":
                result = {
                    "resources": [
                        {
                            "uri": r.uri,
                            "name": r.name,
                            "description": r.description,
                            "mimeType": r.mime_type,
                        }
                        for r in self.config.resources
                        if not r.is_template
                    ]
                }

            elif method == "resources/templates/list":
                result = {
                    "resourceTemplates": [
                        {
                            "uriTemplate": r.uri,
                            "name": r.name,
                            "description": r.description,
                            "mimeType": r.mime_type,
                        }
                        for r in self.config.resources
                        if r.is_template
                    ]
                }

            elif method == "resources/read":
                uri = str(params.get("uri", ""))
                text_content, mime_type = await self.read_resource_by_uri(uri)
                result = {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": mime_type,
                            "text": text_content,
                        }
                    ]
                }

            elif method == "prompts/list":
                result = {
                    "prompts": [
                        {
                            "name": p.name,
                            "description": p.description,
                            "arguments": [
                                {
                                    "name": a.name,
                                    "description": a.description,
                                    "required": a.required,
                                }
                                for a in p.arguments
                            ],
                        }
                        for p in self.config.prompts
                    ]
                }

            elif method == "prompts/get":
                prompt_name = params.get("name", "")
                prompt_args = params.get("arguments") or {}
                result = self.render_prompt(prompt_name, prompt_args)

            else:
                return {
                    "jsonrpc": jsonrpc_version,
                    "id": req_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method}",
                    },
                }

            return {
                "jsonrpc": jsonrpc_version,
                "id": req_id,
                "result": result,
            }

        except KeyError as exc:
            return {
                "jsonrpc": jsonrpc_version,
                "id": req_id,
                "error": {
                    "code": -32602,
                    "message": str(exc),
                },
            }
        except Exception as exc:
            logger.exception("Error handling JSON-RPC method '%s'", method)
            return {
                "jsonrpc": jsonrpc_version,
                "id": req_id,
                "error": {
                    "code": -32603,
                    "message": f"Internal error: {exc}",
                },
            }

    # =========================================================================
    # Official Python `mcp` SDK Server Builder (For stdio & SSE Transports)
    # =========================================================================

    def _build_official_mcp_server(self) -> Any:
        """
        Builds and wires an official `mcp.server.Server` instance if the `mcp` package is installed.
        Supports both MCP Python SDK v2.x (constructor callbacks) and v1.x (decorator registration).
        """
        try:
            import mcp.types as types
            from mcp.server import Server
        except ImportError:
            logger.warning("Official 'mcp' package not installed; using built-in JSON-RPC HTTP engine only.")
            return None

        init_params = inspect.signature(Server.__init__).parameters

        # MCP Python SDK v2.x style (`on_list_tools`, `on_call_tool`, etc.)
        if "on_list_tools" in init_params:
            async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
                return types.ListToolsResult(
                    tools=[
                        types.Tool(
                            name=t.name,
                            description=t.description,
                            inputSchema=t.build_json_schema(),
                        )
                        for t in self.config.tools
                    ]
                )

            async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
                output = await self.execute_tool(params.name, params.arguments or {})
                is_error = isinstance(output, dict) and output.get("error") is True
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text",
                            text=format_for_mcp(output),
                        )
                    ],
                    isError=is_error,
                )

            async def on_list_resources(ctx: Any, params: Any) -> types.ListResourcesResult:
                return types.ListResourcesResult(
                    resources=[
                        types.Resource(
                            uri=r.uri,
                            name=r.name,
                            description=r.description,
                            mimeType=r.mime_type,
                        )
                        for r in self.config.resources
                        if not r.is_template
                    ]
                )

            async def on_list_resource_templates(ctx: Any, params: Any) -> types.ListResourceTemplatesResult:
                return types.ListResourceTemplatesResult(
                    resourceTemplates=[
                        types.ResourceTemplate(
                            uriTemplate=r.uri,
                            name=r.name,
                            description=r.description,
                            mimeType=r.mime_type,
                        )
                        for r in self.config.resources
                        if r.is_template
                    ]
                )

            async def on_read_resource(ctx: Any, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
                uri_str = str(params.uri)
                text_content, mime_type = await self.read_resource_by_uri(uri_str)
                return types.ReadResourceResult(
                    contents=[
                        types.TextResourceContents(
                            uri=uri_str,
                            mimeType=mime_type,
                            text=text_content,
                        )
                    ]
                )

            async def on_list_prompts(ctx: Any, params: Any) -> types.ListPromptsResult:
                return types.ListPromptsResult(
                    prompts=[
                        types.Prompt(
                            name=p.name,
                            description=p.description,
                            arguments=[
                                types.PromptArgument(
                                    name=a.name,
                                    description=a.description,
                                    required=a.required,
                                )
                                for a in p.arguments
                            ],
                        )
                        for p in self.config.prompts
                    ]
                )

            async def on_get_prompt(ctx: Any, params: types.GetPromptRequestParams) -> types.GetPromptResult:
                rendered = self.render_prompt(params.name, params.arguments)
                msg = rendered["messages"][0]
                return types.GetPromptResult(
                    description=rendered["description"],
                    messages=[
                        types.PromptMessage(
                            role=msg["role"],
                            content=types.TextContent(
                                type="text",
                                text=msg["content"]["text"],
                            ),
                        )
                    ],
                )

            return Server(
                self.config.server.name,
                version=self.config.server.version,
                instructions=self.config.server.instructions,
                on_list_tools=on_list_tools,
                on_call_tool=on_call_tool,
                on_list_resources=on_list_resources,
                on_list_resource_templates=on_list_resource_templates,
                on_read_resource=on_read_resource,
                on_list_prompts=on_list_prompts,
                on_get_prompt=on_get_prompt,
            )

        # MCP Python SDK v1.x fallback (`@server.list_tools()` decorator style)
        server = Server(self.config.server.name)

        @server.list_tools()
        async def handle_list_tools() -> list[types.Tool]:
            return [
                types.Tool(
                    name=t.name,
                    description=t.description,
                    inputSchema=t.build_json_schema(),
                )
                for t in self.config.tools
            ]

        @server.call_tool()
        async def handle_call_tool(
            name: str,
            arguments: dict[str, Any] | None,
        ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
            output = await self.execute_tool(name, arguments or {})
            return [
                types.TextContent(
                    type="text",
                    text=format_for_mcp(output),
                )
            ]

        @server.list_resources()
        async def handle_list_resources() -> list[types.Resource]:
            return [
                types.Resource(
                    uri=types.AnyUrl(r.uri),
                    name=r.name,
                    description=r.description,
                    mimeType=r.mime_type,
                )
                for r in self.config.resources
                if not r.is_template
            ]

        @server.list_resource_templates()
        async def handle_list_resource_templates() -> list[types.ResourceTemplate]:
            return [
                types.ResourceTemplate(
                    uriTemplate=r.uri,
                    name=r.name,
                    description=r.description,
                    mimeType=r.mime_type,
                )
                for r in self.config.resources
                if r.is_template
            ]

        @server.read_resource()
        async def handle_read_resource(uri: types.AnyUrl) -> str | bytes:
            text_content, _ = await self.read_resource_by_uri(str(uri))
            return text_content

        @server.list_prompts()
        async def handle_list_prompts() -> list[types.Prompt]:
            return [
                types.Prompt(
                    name=p.name,
                    description=p.description,
                    arguments=[
                        types.PromptArgument(
                            name=a.name,
                            description=a.description,
                            required=a.required,
                        )
                        for a in p.arguments
                    ],
                )
                for p in self.config.prompts
            ]

        @server.get_prompt()
        async def handle_get_prompt(
            name: str,
            arguments: dict[str, str] | None,
        ) -> types.GetPromptResult:
            rendered = self.render_prompt(name, arguments)
            msg = rendered["messages"][0]
            return types.GetPromptResult(
                description=rendered["description"],
                messages=[
                    types.PromptMessage(
                        role=msg["role"],
                        content=types.TextContent(
                            type="text",
                            text=msg["content"]["text"],
                        ),
                    )
                ],
            )

        return server
