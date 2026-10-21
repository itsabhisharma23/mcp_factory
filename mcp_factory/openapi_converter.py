"""
OpenAPI / Swagger to MCPFactory Blueprint Converter.

Turns any existing OpenAPI 3.x or Swagger 2.0 specification (JSON or YAML)
into a ready-to-run MCPFactory declarative JSON configuration file in one command.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def _sanitize_tool_name(raw_name: str) -> str:
    """Normalizes an operationId or HTTP path into a valid MCP tool name `[a-zA-Z0-9_-]+`."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw_name).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned.lower() or "unnamed_tool"


def _resolve_ref(spec: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Resolves local JSON pointer `$ref` references like `#/components/schemas/Order`."""
    if not isinstance(node, dict) or "$ref" not in node:
        return node
    ref_path = node["$ref"]
    if not ref_path.startswith("#/"):
        return node
    current: Any = spec
    for part in ref_path[2:].split("/"):
        if isinstance(current, dict):
            current = current.get(part, {})
        else:
            return node
    return current if isinstance(current, dict) else node


def convert_openapi_to_blueprint(
    openapi_spec: dict[str, Any],
    backend_name: str = "default_api",
    base_url_override: str | None = None,
) -> dict[str, Any]:
    """
    Converts an OpenAPI 3.x / Swagger 2.0 dictionary into an MCPFactory JSON blueprint dictionary.
    """
    info = openapi_spec.get("info", {})
    title = info.get("title", "Imported OpenAPI Service")
    version = info.get("version", "1.0.0")
    description = info.get("description", f"MCP Server auto-generated from OpenAPI spec for {title}")

    # Determine base_url from OpenAPI 3 `servers` or Swagger 2 `host` + `basePath`
    base_url = base_url_override
    if not base_url:
        servers = openapi_spec.get("servers", [])
        if servers and isinstance(servers[0], dict):
            base_url = servers[0].get("url", "https://api.example.com")
        elif "host" in openapi_spec:
            scheme = (openapi_spec.get("schemes") or ["https"])[0]
            host = openapi_spec["host"]
            base_path = openapi_spec.get("basePath", "")
            base_url = f"{scheme}://{host}{base_path}"
        else:
            base_url = "${BACKEND_BASE_URL:-https://api.example.com}"

    # Inspect securitySchemes to pre-configure outbound auth
    auth_block: dict[str, Any] = {"type": "none"}
    security_schemes = (
        openapi_spec.get("components", {}).get("securitySchemes", {})
        or openapi_spec.get("securityDefinitions", {})
    )
    for _, scheme_def in security_schemes.items():
        if not isinstance(scheme_def, dict):
            continue
        s_type = scheme_def.get("type", "").lower()
        if s_type == "http" and scheme_def.get("scheme", "").lower() == "bearer":
            auth_block = {"type": "bearer", "token": "${BACKEND_BEARER_TOKEN}"}
            break
        elif s_type == "apikey":
            in_loc = scheme_def.get("in", "header")
            param_name = scheme_def.get("name", "X-API-Key")
            if in_loc == "query":
                auth_block = {"type": "api_key", "query_param": param_name, "token": "${BACKEND_API_KEY}"}
            else:
                auth_block = {"type": "api_key", "header_name": param_name, "token": "${BACKEND_API_KEY}"}
            break
        elif s_type == "http" and scheme_def.get("scheme", "").lower() == "basic":
            auth_block = {
                "type": "basic",
                "username": "${BACKEND_USERNAME}",
                "password": "${BACKEND_PASSWORD}",
            }
            break

    tools: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    paths = openapi_spec.get("paths", {})

    for path_str, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue

        path_level_params = path_item.get("parameters", [])

        for method in ("get", "post", "put", "patch", "delete"):
            if method not in path_item:
                continue
            op = path_item[method]
            if not isinstance(op, dict):
                continue

            raw_op_id = op.get("operationId") or f"{method}_{path_str}"
            tool_name = _sanitize_tool_name(raw_op_id)
            if tool_name in seen_names:
                tool_name = f"{tool_name}_{method}"
            seen_names.add(tool_name)

            summary = op.get("summary") or op.get("description") or f"{method.upper()} {path_str}"
            parameters_map: dict[str, Any] = {}

            # 1. Path, Query, Header parameters
            all_params = list(path_level_params) + list(op.get("parameters", []))
            for raw_param in all_params:
                param = _resolve_ref(openapi_spec, raw_param)
                p_name = param.get("name")
                p_in = param.get("in", "query")
                if not p_name or p_in == "cookie":
                    continue

                p_schema = _resolve_ref(openapi_spec, param.get("schema", param))
                p_type = p_schema.get("type", "string")
                if p_type not in ("string", "integer", "number", "boolean", "array", "object"):
                    p_type = "string"

                param_entry: dict[str, Any] = {
                    "type": p_type,
                    "description": param.get("description", p_schema.get("description", "")),
                    "required": bool(param.get("required", p_in == "path")),
                    "location": p_in if p_in in ("path", "query", "header") else "auto",
                }
                if "default" in p_schema:
                    param_entry["default"] = p_schema["default"]
                if "enum" in p_schema:
                    param_entry["enum"] = p_schema["enum"]

                parameters_map[p_name] = param_entry

            # 2. Request Body (OpenAPI 3.x requestBody)
            if "requestBody" in op:
                req_body = _resolve_ref(openapi_spec, op["requestBody"])
                content = req_body.get("content", {})
                json_media = content.get("application/json", {})
                body_schema = _resolve_ref(openapi_spec, json_media.get("schema", {}))

                if body_schema.get("type") == "object" or "properties" in body_schema:
                    req_fields = set(body_schema.get("required", []))
                    for prop_name, raw_prop_schema in body_schema.get("properties", {}).items():
                        prop_schema = _resolve_ref(openapi_spec, raw_prop_schema)
                        p_type = prop_schema.get("type", "string")
                        if p_type not in ("string", "integer", "number", "boolean", "array", "object"):
                            p_type = "string"

                        prop_entry: dict[str, Any] = {
                            "type": p_type,
                            "description": prop_schema.get("description", ""),
                            "required": prop_name in req_fields,
                            "location": "body",
                        }
                        if "default" in prop_schema:
                            prop_entry["default"] = prop_schema["default"]
                        if "enum" in prop_schema:
                            prop_entry["enum"] = prop_schema["enum"]
                        parameters_map[prop_name] = prop_entry

            tools.append(
                {
                    "name": tool_name,
                    "description": summary,
                    "backend": backend_name,
                    "connector": "rest",
                    "method": method.upper(),
                    "endpoint": path_str,
                    "parameters": parameters_map,
                }
            )

    server_slug = _sanitize_tool_name(title).replace("_", "-")
    return {
        "schema_version": "1.0",
        "server": {
            "name": f"{server_slug}-mcp",
            "version": str(version),
            "description": description,
            "instructions": f"Use these tools to interact with the {title} backend service.",
        },
        "backends": {
            backend_name: {
                "type": "rest",
                "base_url": base_url,
                "timeout_seconds": 30.0,
                "max_retries": 2,
                "auth": auth_block,
            }
        },
        "tools": tools,
        "resources": [
            {
                "uri": f"openapi://{server_slug}/info",
                "name": f"{title} Service Info",
                "description": "Metadata about the imported OpenAPI service",
                "mime_type": "application/json",
                "static_content": {
                    "title": title,
                    "version": version,
                    "base_url": base_url,
                    "total_tools": len(tools),
                },
            }
        ],
        "prompts": [],
    }


def convert_openapi_file(
    input_path: str | Path,
    output_path: str | Path,
    backend_name: str = "default_api",
    base_url_override: str | None = None,
) -> dict[str, Any]:
    """Reads an OpenAPI JSON/YAML file, converts it to an MCPFactory blueprint, and writes it to disk."""
    in_file = Path(input_path)
    raw_text = in_file.read_text(encoding="utf-8")
    if in_file.suffix.lower() in (".yaml", ".yml"):
        import yaml

        spec = yaml.safe_load(raw_text)
    else:
        spec = json.loads(raw_text)

    blueprint = convert_openapi_to_blueprint(
        spec,
        backend_name=backend_name,
        base_url_override=base_url_override,
    )

    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(blueprint, indent=2) + "\n", encoding="utf-8")
    return blueprint
