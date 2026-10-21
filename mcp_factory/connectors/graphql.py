"""
GraphQL Connector for MCPFactory.

Executes GraphQL queries and mutations against a configured GraphQL endpoint,
automatically mapping MCP tool arguments into GraphQL `variables`.
"""

from __future__ import annotations

from typing import Any

import httpx

from mcp_factory.auth import BackendAuthenticator
from mcp_factory.connectors.base import BaseConnector
from mcp_factory.schema import BackendConfig, ToolConfig
from mcp_factory.transformer import apply_response_transform


class GraphQLConnector(BaseConnector):
    """Executes GraphQL queries and mutations declared in an MCP blueprint."""

    def __init__(self, backend_config: BackendConfig) -> None:
        super().__init__(backend_config)
        self.backend_config = backend_config
        self.authenticator = BackendAuthenticator(
            backend_config.auth,
            base_url=backend_config.base_url,
        )
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Returns a persistent, connection-pooled AsyncClient to reuse TCP/TLS connections."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.backend_config.timeout_seconds,
                verify=self.backend_config.verify_ssl,
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=25,
                    keepalive_expiry=60.0,
                ),
            )
        return self._client

    async def execute(self, tool: ToolConfig, arguments: dict[str, Any]) -> Any:
        if not tool.graphql_query:
            raise ValueError(f"GraphQL tool '{tool.name}' is missing 'graphql_query'.")

        url = tool.endpoint
        if not url.startswith(("http://", "https://")):
            base = (self.backend_config.base_url or "").rstrip("/")
            path = "" if url in ("/", "") else (url if url.startswith("/") else f"/{url}")
            url = f"{base}{path}"

        # Populate defaults for any omitted arguments
        variables = dict(arguments)
        for param_name, param_cfg in tool.parameters.items():
            if param_name not in variables and param_cfg.default is not None:
                variables[param_name] = param_cfg.default

        payload: dict[str, Any] = {
            "query": tool.graphql_query,
            "variables": variables,
        }
        if tool.operation_name:
            payload["operationName"] = tool.operation_name

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "MCPFactory/1.0",
            **self.backend_config.headers,
            **tool.headers,
        }
        query_params: dict[str, str] = {}
        headers, query_params = await self.authenticator.apply_auth(headers, query_params)

        client = self._get_client()
        try:
            response = await client.post(
                url,
                headers=headers,
                params=query_params,
                json=payload,
            )
            response.raise_for_status()
            raw_data = response.json()
        except httpx.HTTPStatusError as exc:
            return {
                "error": True,
                "status_code": exc.response.status_code,
                "message": f"GraphQL HTTP {exc.response.status_code} from {url}",
                "details": exc.response.text[:1000],
            }
        except httpx.RequestError as exc:
            return {
                "error": True,
                "message": f"GraphQL request error to {url}: {exc}",
            }

        if isinstance(raw_data, dict) and "errors" in raw_data and not raw_data.get("data"):
            return {
                "error": True,
                "graphql_errors": raw_data["errors"],
            }

        return apply_response_transform(raw_data, tool.response)
