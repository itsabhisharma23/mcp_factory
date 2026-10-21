"""
Connector Registry for MCPFactory.
"""

from mcp_factory.connectors.base import BaseConnector
from mcp_factory.connectors.graphql import GraphQLConnector
from mcp_factory.connectors.mock import MockConnector
from mcp_factory.connectors.python_plugin import PythonPluginConnector, load_python_handler
from mcp_factory.connectors.rest import RestConnector

__all__ = [
    "BaseConnector",
    "RestConnector",
    "GraphQLConnector",
    "PythonPluginConnector",
    "MockConnector",
    "load_python_handler",
]
