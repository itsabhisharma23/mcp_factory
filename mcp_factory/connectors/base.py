"""
Abstract Base Connector for MCPFactory.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from mcp_factory.schema import BackendConfig, ToolConfig


class BaseConnector(ABC):
    """Interface that all backend connectors (REST, GraphQL, Python Plugin, Mock) implement."""

    def __init__(self, backend_config: Optional[BackendConfig] = None) -> None:
        self.backend_config = backend_config

    @abstractmethod
    async def execute(self, tool: ToolConfig, arguments: dict[str, Any]) -> Any:
        """Executes the tool call with the provided arguments and returns the transformed payload."""
        raise NotImplementedError
