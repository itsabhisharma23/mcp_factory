"""
Mock / Static Connector for MCPFactory.

Enables rapid prototyping and testing of MCP servers before live backend APIs are available.
Supports dynamic `{argument}` interpolation inside mock response strings and dictionaries.
"""

from __future__ import annotations

from typing import Any

from mcp_factory.connectors.base import BaseConnector
from mcp_factory.schema import ToolConfig
from mcp_factory.transformer import apply_response_transform


def _interpolate_mock(template_obj: Any, arguments: dict[str, Any]) -> Any:
    """Recursively interpolates `{arg_name}` placeholders in mock response templates."""
    if isinstance(template_obj, str):
        result = template_obj
        for k, v in arguments.items():
            result = result.replace(f"{{{k}}}", str(v))
        return result
    elif isinstance(template_obj, dict):
        return {k: _interpolate_mock(v, arguments) for k, v in template_obj.items()}
    elif isinstance(template_obj, list):
        return [_interpolate_mock(item, arguments) for item in template_obj]
    return template_obj


class MockConnector(BaseConnector):
    """Returns static or argument-interpolated mock responses defined in the JSON blueprint."""

    async def execute(self, tool: ToolConfig, arguments: dict[str, Any]) -> Any:
        effective_args = dict(arguments)
        for param_name, param_cfg in tool.parameters.items():
            if param_name not in effective_args and param_cfg.default is not None:
                effective_args[param_name] = param_cfg.default

        mock_data = tool.mock_response
        if mock_data is None:
            mock_data = {"status": "ok", "tool": tool.name, "arguments": effective_args}
        else:
            mock_data = _interpolate_mock(mock_data, effective_args)

        return apply_response_transform(mock_data, tool.response)
