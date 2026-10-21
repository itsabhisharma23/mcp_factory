"""
MCPFactory: Declarative JSON/YAML-driven Model Context Protocol (MCP) Server Framework.
Build and deploy MCP servers for any backend service onto Google Cloud Run in minutes.
"""

__version__ = "1.0.0"

from mcp_factory.schema import MCPBlueprintConfig, load_blueprint
from mcp_factory.engine import MCPFactoryEngine

__all__ = ["MCPBlueprintConfig", "load_blueprint", "MCPFactoryEngine", "__version__"]
