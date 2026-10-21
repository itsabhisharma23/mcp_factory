# ============================================================================
# MCPFactory - Production Dockerfile for Google Cloud Run
# ============================================================================
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    HOST=0.0.0.0 \
    MCP_TRANSPORT=http \
    MCP_CONFIG_PATH=configs/ecommerce_service.json

WORKDIR /app

# Create a non-root user for Cloud Run security best practices
RUN groupadd -r mcpuser && useradd -r -g mcpuser -d /app -s /sbin/nologin mcpuser

# Install dependencies first for optimal Docker layer caching
COPY requirements.txt pyproject.toml ./
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Copy framework code, plugins, and declarative JSON blueprints
COPY mcp_factory/ ./mcp_factory/
COPY plugins/ ./plugins/
COPY configs/ ./configs/

# Install the mcp-factory package in editable/local mode
RUN pip install --no-deps -e .

# Validate default blueprint at build time to fail fast on syntax errors
RUN python -m mcp_factory.cli validate "${MCP_CONFIG_PATH}"

RUN chown -R mcpuser:mcpuser /app
USER mcpuser

EXPOSE 8080

# Start the Cloud Run HTTP/SSE/JSON-RPC server
CMD ["sh", "-c", "python -m mcp_factory.cli run --config ${MCP_CONFIG_PATH} --transport http --host 0.0.0.0 --port ${PORT:-8080}"]
