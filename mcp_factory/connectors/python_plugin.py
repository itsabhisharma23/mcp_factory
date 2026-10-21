"""
Python Plugin Connector for MCPFactory.

Allows binding custom Python functions (sync or async) to MCP tools or resources via
`"handler": "package.module:function_name"`.
Ideal for database queries (BigQuery, Postgres, Redis), complex aggregations, or SDK calls.
"""

from __future__ import annotations

from functools import lru_cache
import importlib
import inspect
from typing import Any, Callable

from mcp_factory.connectors.base import BaseConnector
from mcp_factory.schema import ToolConfig
from mcp_factory.transformer import apply_response_transform


@lru_cache(maxsize=256)
def load_python_handler(handler_path: str) -> Callable[..., Any]:
    """
    Dynamically imports and returns a callable from 'module.submodule:function_name'.
    """
    if ":" not in handler_path:
        raise ValueError(
            f"Invalid Python handler path '{handler_path}'. Expected format 'module.submodule:function_name'."
        )
    module_name, func_name = handler_path.split(":", 1)
    module = importlib.import_module(module_name)
    func = getattr(module, func_name, None)
    if func is None or not callable(func):
        raise ValueError(f"Handler '{func_name}' not found or not callable in module '{module_name}'.")
    return func


class PythonPluginConnector(BaseConnector):
    """Executes custom Python functions declared via `handler` in the MCP JSON config."""

    async def execute(self, tool: ToolConfig, arguments: dict[str, Any]) -> Any:
        if not tool.handler:
            raise ValueError(f"Python tool '{tool.name}' is missing 'handler'.")

        func = load_python_handler(tool.handler)

        # Populate default parameter values
        call_args = dict(arguments)
        for param_name, param_cfg in tool.parameters.items():
            if param_name not in call_args and param_cfg.default is not None:
                call_args[param_name] = param_cfg.default

        if inspect.iscoroutinefunction(func):
            raw_result = await func(**call_args)
        else:
            raw_result = func(**call_args)

        return apply_response_transform(raw_result, tool.response)
