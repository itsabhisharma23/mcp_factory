"""
REST / HTTP Connector for MCPFactory.

Supports:
- Automatic or explicit parameter routing to URL path ({id}), query params, headers, or JSON body
- Field name aliasing (e.g. tool arg `user_id` -> backend field `userId`)
- Outbound authentication (API Key, Bearer, Basic, OAuth2, GCP ID/Access Token)
- Automatic retries with exponential backoff on transient failures
- Response path extraction & field filtering
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

from mcp_factory.auth import BackendAuthenticator
from mcp_factory.connectors.base import BaseConnector
from mcp_factory.schema import BackendConfig, ToolConfig
from mcp_factory.transformer import apply_response_transform

logger = logging.getLogger("mcp_factory.connectors.rest")

PATH_PARAM_REGEX = re.compile(r"\{([a-zA-Z0-9_]+)\}")


class RestConnector(BaseConnector):
    """Executes declarative REST/HTTP tool calls against a configured backend."""

    def __init__(self, backend_config: BackendConfig) -> None:
        super().__init__(backend_config)
        self.backend_config = backend_config
        self.authenticator = BackendAuthenticator(
            backend_config.auth,
            base_url=backend_config.base_url,
        )

    def _build_url(self, endpoint: str, path_params: dict[str, Any]) -> str:
        """Interpolates `{param}` placeholders in the endpoint and joins with `base_url`."""
        resolved_endpoint = endpoint
        for key, val in path_params.items():
            placeholder = f"{{{key}}}"
            if placeholder in resolved_endpoint:
                resolved_endpoint = resolved_endpoint.replace(
                    placeholder, quote(str(val), safe="")
                )

        if resolved_endpoint.startswith(("http://", "https://")):
            return resolved_endpoint

        base = (self.backend_config.base_url or "").rstrip("/")
        path = resolved_endpoint if resolved_endpoint.startswith("/") else f"/{resolved_endpoint}"
        return f"{base}{path}"

    def _route_arguments(
        self,
        tool: ToolConfig,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, str]]:
        """
        Routes tool call arguments into `(path_params, query_params, body_params, header_params)`.
        """
        path_placeholders = set(PATH_PARAM_REGEX.findall(tool.endpoint))

        path_params: dict[str, Any] = {}
        query_params: dict[str, Any] = dict(tool.static_query)
        body_params: dict[str, Any] = dict(tool.static_body)
        header_params: dict[str, str] = {}

        # Merge default values from parameter schema if omitted by caller
        effective_args = dict(arguments)
        for param_name, param_cfg in tool.parameters.items():
            if param_name not in effective_args and param_cfg.default is not None:
                effective_args[param_name] = param_cfg.default

        for arg_name, arg_value in effective_args.items():
            if arg_value is None:
                continue

            param_cfg = tool.parameters.get(arg_name)
            location = param_cfg.location if param_cfg else "auto"
            target_name = (param_cfg.field_name if param_cfg and param_cfg.field_name else arg_name)

            if location == "path" or (location == "auto" and arg_name in path_placeholders):
                path_params[arg_name] = arg_value
            elif location == "header":
                header_params[target_name] = str(arg_value)
            elif location == "query":
                query_params[target_name] = arg_value
            elif location == "body":
                body_params[target_name] = arg_value
            else:
                # location == "auto" and not in URL path
                if tool.method in ("GET", "DELETE"):
                    query_params[target_name] = arg_value
                else:
                    body_params[target_name] = arg_value

        return path_params, query_params, body_params, header_params

    async def execute(self, tool: ToolConfig, arguments: dict[str, Any]) -> Any:
        path_params, query_params, body_params, arg_headers = self._route_arguments(tool, arguments)
        url = self._build_url(tool.endpoint, path_params)

        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "MCPFactory/1.0",
            **self.backend_config.headers,
            **tool.headers,
            **arg_headers,
        }

        # Apply outbound authentication
        str_query = {k: str(v) if not isinstance(v, list) else v for k, v in query_params.items()}
        headers, str_query = await self.authenticator.apply_auth(headers, str_query)

        max_attempts = max(1, self.backend_config.max_retries + 1)
        last_error: Exception | None = None

        async with httpx.AsyncClient(
            timeout=self.backend_config.timeout_seconds,
            verify=self.backend_config.verify_ssl,
            follow_redirects=True,
        ) as client:
            for attempt in range(1, max_attempts + 1):
                try:
                    request_kwargs: dict[str, Any] = {
                        "method": tool.method,
                        "url": url,
                        "headers": headers,
                        "params": str_query,
                    }
                    if tool.method in ("POST", "PUT", "PATCH") and body_params:
                        request_kwargs["json"] = body_params

                    response = await client.request(**request_kwargs)

                    # Retry on transient 502/503/504
                    if response.status_code in (502, 503, 504) and attempt < max_attempts:
                        await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
                        continue

                    response.raise_for_status()

                    content_type = response.headers.get("content-type", "")
                    if "application/json" in content_type or response.text.strip().startswith(("{", "[")):
                        raw_data = response.json()
                    else:
                        raw_data = response.text

                    return apply_response_transform(raw_data, tool.response)

                except httpx.HTTPStatusError as exc:
                    error_body = exc.response.text[:1000]
                    return {
                        "error": True,
                        "status_code": exc.response.status_code,
                        "message": f"HTTP {exc.response.status_code} from {tool.method} {url}",
                        "details": error_body,
                    }
                except (httpx.RequestError, httpx.TimeoutException) as exc:
                    last_error = exc
                    if attempt < max_attempts:
                        await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
                        continue

        return {
            "error": True,
            "message": f"Request to {url} failed after {max_attempts} attempts: {last_error}",
        }
