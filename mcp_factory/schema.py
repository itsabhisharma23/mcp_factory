"""
Declarative Schema & Configuration Loader for MCPFactory.

Supports JSON and YAML blueprints with automatic environment variable interpolation
(including default fallbacks like `${ENV_VAR:-default_value}`).
"""

from __future__ import annotations

import json
import os
import re
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

# Regex matching ${VAR_NAME} or ${VAR_NAME:-default_value}
ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def interpolate_env_vars(value: Any) -> Any:
    """
    Recursively interpolates environment variables in strings, lists, and dicts.
    Syntax supported:
      - `${VAR_NAME}` -> replaced with os.environ.get("VAR_NAME", "")
      - `${VAR_NAME:-fallback}` -> replaced with os.environ.get("VAR_NAME") or "fallback"
    """
    if isinstance(value, str):
        def _replace(match: re.Match) -> str:
            var_name = match.group(1)
            default_val = match.group(2)
            env_val = os.environ.get(var_name)
            if env_val is not None and env_val != "":
                return env_val
            if default_val is not None:
                return default_val
            return ""

        return ENV_VAR_PATTERN.sub(_replace, value)
    elif isinstance(value, dict):
        return {k: interpolate_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [interpolate_env_vars(item) for item in value]
    return value


class AuthType(str, Enum):
    NONE = "none"
    API_KEY = "api_key"
    BEARER = "bearer"
    BASIC = "basic"
    CUSTOM_HEADERS = "custom_headers"
    OAUTH2_CLIENT_CREDENTIALS = "oauth2_client_credentials"
    OAUTH2_REFRESH_TOKEN = "oauth2_refresh_token"
    GCP_ID_TOKEN = "gcp_id_token"
    GCP_ACCESS_TOKEN = "gcp_access_token"
    HMAC_SHA256 = "hmac_sha256"
    PASSTHROUGH = "passthrough"


class BackendAuthConfig(BaseModel):
    """Authentication configuration for an outbound backend service."""

    type: AuthType = Field(default=AuthType.NONE, description="Outbound authentication strategy")
    # For API_KEY
    header_name: str = Field(default="X-API-Key", description="Header name for API key or HMAC signature")
    query_param: Optional[str] = Field(default=None, description="Query param name if API key is passed in URL")
    # For API_KEY or BEARER
    token: Optional[str] = Field(default=None, description="Static token/key or ${ENV_VAR}")
    # For BASIC
    username: Optional[str] = Field(default=None, description="Basic auth username")
    password: Optional[str] = Field(default=None, description="Basic auth password")
    # For CUSTOM_HEADERS (e.g. Cloudflare X-Auth-Email + X-Auth-Key, Datadog DD-API-KEY + DD-APPLICATION-KEY)
    custom_headers: dict[str, str] = Field(
        default_factory=dict,
        description="Map of multiple custom authentication headers to inject",
    )
    # For OAUTH2_CLIENT_CREDENTIALS & OAUTH2_REFRESH_TOKEN
    token_url: Optional[str] = Field(default=None, description="OAuth2 token endpoint URL")
    client_id: Optional[str] = Field(default=None, description="OAuth2 client ID")
    client_secret: Optional[str] = Field(default=None, description="OAuth2 client secret")
    refresh_token: Optional[str] = Field(default=None, description="OAuth2 refresh token for oauth2_refresh_token flow")
    scopes: list[str] = Field(default_factory=list, description="OAuth2 requested scopes")
    client_auth_method: Literal["client_secret_post", "client_secret_basic"] = Field(
        default="client_secret_post",
        description="How OAuth2 client credentials are sent to token_url (form body vs Basic Auth header)",
    )
    extra_token_params: dict[str, str] = Field(
        default_factory=dict,
        description="Optional extra form parameters for OAuth2 token requests (e.g. 'audience' or 'resource')",
    )
    # For GCP_ID_TOKEN (Cloud Run to Cloud Run service-to-service auth)
    audience: Optional[str] = Field(
        default=None,
        description="Target audience URL for GCP OIDC ID token (defaults to backend base_url)",
    )
    # For HMAC_SHA256 request signing
    hmac_secret: Optional[str] = Field(default=None, description="Shared secret for HMAC-SHA256 signature")
    timestamp_header: str = Field(default="X-Timestamp", description="Header carrying Unix timestamp for HMAC")
    # For PASSTHROUGH (Zero-Trust User Token Delegation)
    source_header: str = Field(
        default="Authorization",
        description="Inbound client header to extract and forward to the backend in passthrough mode",
    )
    target_header: str = Field(
        default="Authorization",
        description="Outbound backend header name to receive the passthrough token",
    )


class BackendConfig(BaseModel):
    """Defines a reusable backend service connection."""

    type: Literal["rest", "graphql", "python", "mock"] = Field(
        default="rest",
        description="Default connector type for tools using this backend",
    )
    base_url: Optional[str] = Field(
        default=None,
        description="Base URL for REST or GraphQL endpoint (e.g., https://api.example.com/v1)",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Default HTTP headers sent with every request to this backend",
    )
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    max_retries: int = Field(default=2, ge=0, le=10)
    verify_ssl: bool = Field(default=True)
    follow_redirects: bool = Field(default=False, description="Disable unexpected redirects by default for SSRF safety")
    client_cert_path: Optional[str] = Field(default=None, description="Optional path to mTLS client certificate PEM")
    client_key_path: Optional[str] = Field(default=None, description="Optional path to mTLS client private key PEM")
    auth: BackendAuthConfig = Field(default_factory=BackendAuthConfig)


class ToolParameterConfig(BaseModel):
    """Simplified parameter definition that compiles into JSON Schema and controls HTTP routing."""

    type: Literal["string", "integer", "number", "boolean", "array", "object"] = "string"
    description: str = Field(default="", description="Clear explanation for the LLM")
    required: bool = Field(default=True, description="Whether the parameter is mandatory")
    default: Optional[Any] = Field(default=None, description="Default value if omitted")
    enum: Optional[list[Any]] = Field(default=None, description="Allowed values")
    items: Optional[dict[str, Any]] = Field(
        default=None,
        description="JSON Schema for array items when type='array' (defaults to {'type': 'string'})",
    )
    properties: Optional[dict[str, Any]] = Field(
        default=None,
        description="JSON Schema properties when type='object'",
    )
    location: Literal["auto", "path", "query", "body", "header"] = Field(
        default="auto",
        description=(
            "Where to place this parameter in REST requests. "
            "'auto' places {param} matches in URL path, GET/DELETE params in query, and POST/PUT/PATCH in body."
        ),
    )
    field_name: Optional[str] = Field(
        default=None,
        description="Override parameter name when sent to the backend (e.g. map 'user_id' -> 'userId')",
    )


class ResponseTransformConfig(BaseModel):
    """Controls how backend responses are filtered, shaped, and formatted before returning to the LLM."""

    extract_path: Optional[str] = Field(
        default=None,
        description="Dot-separated path to extract from JSON response (e.g. 'data.orders' or 'items.0')",
    )
    pick_fields: Optional[list[str]] = Field(
        default=None,
        description="Whitelist of fields to keep in dictionary or list-of-dictionary responses",
    )
    omit_fields: Optional[list[str]] = Field(
        default=None,
        description="Blacklist of fields to remove (e.g. ['password_hash', 'internal_metadata'])",
    )
    max_items: Optional[int] = Field(
        default=None,
        description="Maximum number of items to return if the extracted payload is a list",
    )
    template: Optional[str] = Field(
        default=None,
        description="Optional Python format/template string to render the response as text",
    )


class ToolConfig(BaseModel):
    """Declarative definition of an MCP Tool mapped to a backend action."""

    name: str = Field(..., pattern=r"^[a-zA-Z0-9_-]+$", description="Unique MCP tool name")
    description: str = Field(..., description="Detailed tool description shown to the LLM")
    backend: Optional[str] = Field(
        default=None,
        description="Name of the backend defined in `backends` (optional for python/mock connectors)",
    )
    connector: Optional[Literal["rest", "graphql", "python", "mock"]] = Field(
        default=None,
        description="Connector type override; defaults to the referenced backend's type",
    )

    # REST Connector settings
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = Field(default="GET")
    endpoint: str = Field(
        default="/",
        description="Relative path (e.g., '/orders/{order_id}') or full URL",
    )
    headers: dict[str, str] = Field(default_factory=dict, description="Tool-specific HTTP headers")
    static_query: dict[str, Any] = Field(
        default_factory=dict,
        description="Static query parameters always sent with the request",
    )
    static_body: dict[str, Any] = Field(
        default_factory=dict,
        description="Static body fields merged into the JSON request payload",
    )

    # GraphQL Connector settings
    graphql_query: Optional[str] = Field(
        default=None,
        description="GraphQL query or mutation string",
    )
    operation_name: Optional[str] = Field(default=None)

    # Python Plugin Connector settings
    handler: Optional[str] = Field(
        default=None,
        description="Python import path in format 'module.submodule:function_name'",
    )

    # Mock Connector settings
    mock_response: Optional[Any] = Field(
        default=None,
        description="Static or templated response returned by the mock connector",
    )

    # Tool Input Schema (use either `parameters` shorthand OR raw `input_schema`)
    parameters: dict[str, ToolParameterConfig] = Field(
        default_factory=dict,
        description="Ergonomic map of parameter_name -> ToolParameterConfig",
    )
    input_schema: Optional[dict[str, Any]] = Field(
        default=None,
        description="Raw JSON Schema override for tool inputs",
    )

    # Output Transformation
    response: ResponseTransformConfig = Field(default_factory=ResponseTransformConfig)

    def build_json_schema(self) -> dict[str, Any]:
        """Compiles the tool's parameters into a strict JSON Schema object for MCP `list_tools`."""
        if self.input_schema:
            return self.input_schema

        properties: dict[str, Any] = {}
        required_fields: list[str] = []

        for param_name, param_cfg in self.parameters.items():
            prop: dict[str, Any] = {"type": param_cfg.type}
            if param_cfg.description:
                prop["description"] = param_cfg.description
            if param_cfg.default is not None:
                prop["default"] = param_cfg.default
            if param_cfg.enum is not None:
                prop["enum"] = param_cfg.enum
            if param_cfg.type == "array":
                prop["items"] = param_cfg.items or {"type": "string"}
            if param_cfg.type == "object" and param_cfg.properties:
                prop["properties"] = param_cfg.properties

            properties[param_name] = prop
            if param_cfg.required and param_cfg.default is None:
                required_fields.append(param_name)

        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required_fields:
            schema["required"] = required_fields
        return schema


class ResourceConfig(BaseModel):
    """Declarative definition of an MCP Resource or Resource Template (RFC 6570 URI template)."""

    uri: str = Field(
        ...,
        description="Resource URI (e.g. 'config://app/settings' or template 'orders://details/{order_id}')",
    )
    name: str = Field(..., description="Human-readable resource name")
    description: str = Field(default="", description="Description of what this resource provides")
    mime_type: str = Field(default="application/json")
    backend: Optional[str] = Field(default=None, description="Backend name if backed by an API")
    endpoint: Optional[str] = Field(
        default=None,
        description="Backend REST endpoint (supports {param} placeholders extracted from the resource URI)",
    )
    static_content: Optional[Any] = Field(
        default=None,
        description="Static JSON/text content if not fetched from a remote backend",
    )
    handler: Optional[str] = Field(
        default=None,
        description="Optional Python plugin function 'module:func' to generate resource content",
    )
    response: ResponseTransformConfig = Field(default_factory=ResponseTransformConfig)

    @property
    def is_template(self) -> bool:
        """Returns True if the URI contains `{param}` template variables."""
        return "{" in self.uri and "}" in self.uri


class PromptArgumentConfig(BaseModel):
    """Argument definition for a reusable MCP Prompt template."""

    name: str
    description: str = ""
    required: bool = True


class PromptConfig(BaseModel):
    """Declarative definition of an MCP Prompt template."""

    name: str = Field(..., description="Unique prompt identifier")
    description: str = Field(default="", description="What this prompt helps the user accomplish")
    arguments: list[PromptArgumentConfig] = Field(default_factory=list)
    template: str = Field(
        ...,
        description="Prompt message template using {argument_name} placeholders",
    )
    role: Literal["user", "assistant"] = Field(default="user")


class InboundAuthConfig(BaseModel):
    """Inbound security configuration protecting the MCP Server endpoints."""

    type: Literal["api_key", "jwt_hs256", "google_oidc", "none"] = Field(
        default="api_key",
        description="Inbound client authentication mechanism",
    )
    # For API_KEY (supports single key or comma-separated rotatable keys)
    api_key_env: str = Field(default="MCP_SERVER_API_KEY")
    # For JWT_HS256
    jwt_secret_env: str = Field(default="MCP_JWT_SECRET")
    issuer: Optional[str] = Field(default=None, description="Expected 'iss' claim in JWT")
    audience: Optional[str] = Field(default=None, description="Expected 'aud' claim in JWT / Google OIDC token")
    required_scopes: list[str] = Field(default_factory=list, description="Required scopes in JWT 'scope' or 'scp'")
    # For GOOGLE_OIDC
    allowed_emails: list[str] = Field(
        default_factory=list,
        description="Optional whitelist of caller email addresses or service accounts",
    )
    allowed_domains: list[str] = Field(
        default_factory=list,
        description="Optional whitelist of Google Workspace domains (e.g. ['acme.com'])",
    )


class ServerMetadataConfig(BaseModel):
    """Top-level MCP Server metadata and security settings."""

    name: str = Field(..., description="MCP Server name")
    version: str = Field(default="1.0.0")
    description: str = Field(default="Declarative MCP Server powered by MCPFactory")
    instructions: Optional[str] = Field(
        default=None,
        description="System instructions sent to the MCP client during initialization",
    )
    require_api_key_env: Optional[str] = Field(
        default="MCP_SERVER_API_KEY",
        description=(
            "Name of the environment variable holding an inbound API key (or comma-separated keys) to protect HTTP/SSE endpoints. "
            "If the env var is non-empty at runtime, requests must supply it via Authorization: Bearer or X-MCP-API-Key."
        ),
    )
    inbound_auth: Optional[InboundAuthConfig] = Field(
        default=None,
        description="Optional advanced inbound authentication configuration (API Keys, JWT HS256, or Google OIDC)",
    )


class MCPBlueprintConfig(BaseModel):
    """Root configuration model representing a complete MCP Server blueprint."""

    schema_version: str = Field(default="1.0")
    server: ServerMetadataConfig
    backends: dict[str, BackendConfig] = Field(default_factory=dict)
    tools: list[ToolConfig] = Field(default_factory=list)
    resources: list[ResourceConfig] = Field(default_factory=list)
    prompts: list[PromptConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self) -> "MCPBlueprintConfig":
        """Ensures every tool and resource references a valid backend or standalone connector."""
        seen_tools: set[str] = set()
        for tool in self.tools:
            if tool.name in seen_tools:
                raise ValueError(f"Duplicate tool name detected: '{tool.name}'")
            seen_tools.add(tool.name)

            effective_connector = tool.connector
            if not effective_connector:
                if tool.backend and tool.backend in self.backends:
                    effective_connector = self.backends[tool.backend].type
                elif tool.handler:
                    effective_connector = "python"
                elif tool.mock_response is not None:
                    effective_connector = "mock"
                else:
                    effective_connector = "rest"
                tool.connector = effective_connector

            if effective_connector in ("rest", "graphql"):
                if not tool.backend:
                    raise ValueError(
                        f"Tool '{tool.name}' uses connector '{effective_connector}' "
                        "but does not specify a 'backend'."
                    )
                if tool.backend not in self.backends:
                    raise ValueError(
                        f"Tool '{tool.name}' references unknown backend '{tool.backend}'. "
                        f"Available backends: {list(self.backends.keys())}"
                    )
            elif effective_connector == "python" and not tool.handler:
                raise ValueError(
                    f"Tool '{tool.name}' uses 'python' connector but is missing 'handler' (e.g. 'plugins.mod:fn')."
                )

        for resource in self.resources:
            if resource.backend and resource.backend not in self.backends:
                raise ValueError(
                    f"Resource '{resource.name}' references unknown backend '{resource.backend}'."
                )

        return self


def load_blueprint(path: str | Path, interpolate_env: bool = True) -> MCPBlueprintConfig:
    """
    Loads, interpolates environment variables, and validates an MCP JSON or YAML blueprint file.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"MCP Blueprint configuration file not found: {file_path}")

    raw_text = file_path.read_text(encoding="utf-8")
    suffix = file_path.suffix.lower()

    if suffix in (".yaml", ".yml"):
        import yaml

        raw_data = yaml.safe_load(raw_text)
    else:
        raw_data = json.loads(raw_text)

    if interpolate_env:
        raw_data = interpolate_env_vars(raw_data)

    return MCPBlueprintConfig.model_validate(raw_data)
