"""
Transport & Web Server Layer for MCPFactory.

Supports:
1. Cloud Run HTTP Mode (`MCP_TRANSPORT=http`):
   - `POST /mcp`                 : Stateless Streamable HTTP JSON-RPC 2.0 MCP Endpoint
   - `GET  /sse`                 : Official MCP Server-Sent Events (SSE) stream endpoint
   - `POST /messages/`           : Official MCP SSE client message ingestion endpoint
   - `GET  /health` & `GET /`    : Cloud Run health & readiness check
   - `GET  /catalog`             : Full JSON catalog of tools, resources, and prompts
   - `POST /api/tools/{name}`    : Direct REST Bridge to invoke any MCP tool via standard HTTP POST
2. Local Stdio Mode (`MCP_TRANSPORT=stdio`):
   - Standard input/output transport for Claude Desktop, Cursor, and local IDE agents.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from mcp_factory.auth import current_request_headers, verify_inbound_request
from mcp_factory.engine import MCPFactoryEngine

logger = logging.getLogger("mcp_factory.server")


def _check_auth(request: Request, engine: MCPFactoryEngine) -> Optional[JSONResponse]:
    """
    Validates inbound authentication (API Keys, JWT HS256, or Google OIDC)
    and populates `current_request_headers` ContextVar for Zero-Trust passthrough delegation.
    """
    lower_headers = {k.lower(): v for k, v in request.headers.items()}
    current_request_headers.set(lower_headers)

    ok, error_msg = verify_inbound_request(dict(request.headers), engine.config.server)
    if not ok:
        return JSONResponse(
            {"error": "Unauthorized", "message": error_msg},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return None


def create_http_app(engine: MCPFactoryEngine) -> Starlette:
    """
    Creates an ASGI Starlette application optimized for Google Cloud Run.
    Exposes both Stateless Streamable HTTP (`/mcp`), Stateful SSE (`/sse` + `/messages/`),
    and developer REST endpoints (`/health`, `/catalog`, `/api/tools/{tool_name}`).
    """
    start_time = time.time()

    async def health_handler(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "healthy",
                "server": engine.config.server.name,
                "version": engine.config.server.version,
                "description": engine.config.server.description,
                "uptime_seconds": round(time.time() - start_time, 2),
                "counts": {
                    "backends": len(engine.config.backends),
                    "tools": len(engine.config.tools),
                    "resources": len(engine.config.resources),
                    "prompts": len(engine.config.prompts),
                },
                "endpoints": {
                    "mcp_jsonrpc_http": "/mcp",
                    "mcp_sse_stream": "/sse",
                    "mcp_sse_messages": "/messages/",
                    "catalog": "/catalog",
                    "rest_tool_bridge": "/api/tools/{tool_name}",
                },
            }
        )

    async def catalog_handler(request: Request) -> JSONResponse:
        auth_err = _check_auth(request, engine)
        if auth_err:
            return auth_err
        return JSONResponse(engine.get_catalog_summary())

    async def mcp_jsonrpc_handler(request: Request) -> Response:
        """
        Stateless Streamable HTTP JSON-RPC 2.0 endpoint (`POST /mcp`).
        Ideal for Cloud Run horizontal scaling without sticky session requirements.
        """
        auth_err = _check_auth(request, engine)
        if auth_err:
            return auth_err

        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error: Invalid JSON body"},
                },
                status_code=400,
            )

        # Support JSON-RPC batch requests as well as single requests
        if isinstance(payload, list):
            responses = []
            for item in payload:
                resp = await engine.handle_jsonrpc(item)
                if resp is not None:
                    responses.append(resp)
            if not responses:
                return Response(status_code=202)
            return JSONResponse(responses)

        response_data = await engine.handle_jsonrpc(payload)
        if response_data is None:
            return Response(status_code=202)
        return JSONResponse(response_data)

    async def direct_rest_tool_handler(request: Request) -> JSONResponse:
        """
        REST-to-MCP Bridge (`POST /api/tools/{tool_name}`).
        Allows invoking any MCP tool directly via standard REST JSON POST.
        """
        auth_err = _check_auth(request, engine)
        if auth_err:
            return auth_err

        tool_name = request.path_params["tool_name"]
        try:
            body = await request.json() if await request.body() else {}
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        try:
            result = await engine.execute_tool(tool_name, body)
            return JSONResponse({"tool": tool_name, "result": result})
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except Exception as exc:
            logger.exception("Error executing tool '%s' via REST bridge", tool_name)
            return JSONResponse({"error": str(exc)}, status_code=500)

    routes: list[Any] = [
        Route("/", health_handler, methods=["GET"]),
        Route("/health", health_handler, methods=["GET"]),
        Route("/catalog", catalog_handler, methods=["GET"]),
        Route("/mcp", mcp_jsonrpc_handler, methods=["POST"]),
        Route("/api/tools/{tool_name}", direct_rest_tool_handler, methods=["POST"]),
    ]

    # Mount official MCP SSE transport if `mcp` SDK is available
    if engine.mcp_server is not None:
        try:
            from mcp.server.sse import SseServerTransport

            sse_transport = SseServerTransport("/messages/")

            async def handle_sse(request: Request) -> Response:
                auth_err = _check_auth(request, engine)
                if auth_err:
                    return auth_err

                async with sse_transport.connect_sse(
                    request.scope, request.receive, request._send
                ) as streams:
                    await engine.mcp_server.run(
                        streams[0],
                        streams[1],
                        engine.mcp_server.create_initialization_options(),
                    )
                return Response()

            routes.append(Route("/sse", endpoint=handle_sse, methods=["GET"]))
            routes.append(Mount("/messages/", app=sse_transport.handle_post_message))
        except ImportError:
            logger.warning("mcp.server.sse not available; /sse endpoint disabled.")

    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )
    ]

    return Starlette(debug=False, routes=routes, middleware=middleware)


async def run_stdio_server(engine: MCPFactoryEngine) -> None:
    """Runs the MCP server over standard input/output (stdio) for local desktop clients."""
    if engine.mcp_server is None:
        raise RuntimeError("The 'mcp' Python package is required to run in stdio mode.")

    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await engine.mcp_server.run(
            read_stream,
            write_stream,
            engine.mcp_server.create_initialization_options(),
        )


def run_http_server(
    engine: MCPFactoryEngine,
    host: str = "0.0.0.0",
    port: int = 8080,
    log_level: str = "info",
) -> None:
    """Runs the ASGI HTTP server using Uvicorn (Cloud Run entrypoint)."""
    import uvicorn

    app = create_http_app(engine)
    logger.info(
        "Starting MCPFactory HTTP Server '%s' on http://%s:%d (Tools: %d, Resources: %d, Prompts: %d)",
        engine.config.server.name,
        host,
        port,
        len(engine.config.tools),
        len(engine.config.resources),
        len(engine.config.prompts),
    )
    uvicorn.run(app, host=host, port=port, log_level=log_level)


def create_app_from_env() -> Starlette:
    """
    ASGI factory function (`uvicorn mcp_factory.server:create_app_from_env --factory`)
    reading `MCP_CONFIG_PATH` from the environment.
    """
    config_path = os.environ.get("MCP_CONFIG_PATH", "configs/ecommerce_service.json")
    engine = MCPFactoryEngine.from_file(config_path)
    return create_http_app(engine)
