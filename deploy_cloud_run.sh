#!/usr/bin/env bash
# ============================================================================
# MCPFactory - One-Command Google Cloud Run Deployment Script
# ============================================================================
# Usage:
#   ./deploy_cloud_run.sh
#
# Or with custom parameters:
#   PROJECT_ID="my-gcp-project" \
#   REGION="us-central1" \
#   SERVICE_NAME="ecommerce-mcp" \
#   MCP_CONFIG_PATH="configs/ecommerce_service.json" \
#   ./deploy_cloud_run.sh
# ============================================================================

set -euo pipefail

# 1. Configuration & Defaults
MCP_CONFIG_PATH="${MCP_CONFIG_PATH:-configs/ecommerce_service.json}"
REGION="${REGION:-us-central1}"
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || echo "")}"
ALLOW_UNAUTHENTICATED="${ALLOW_UNAUTHENTICATED:-true}"
MIN_INSTANCES="${MIN_INSTANCES:-0}"
MAX_INSTANCES="${MAX_INSTANCES:-10}"
MEMORY="${MEMORY:-512Mi}"
CPU="${CPU:-1}"
TIMEOUT="${TIMEOUT:-3600}" # 3600s supports long-lived MCP SSE streams on Cloud Run

if [[ ! -f "${MCP_CONFIG_PATH}" ]]; then
  echo "❌ Error: Blueprint file '${MCP_CONFIG_PATH}' not found."
  exit 1
fi

# 2. Validate Blueprint Locally Before Deploying
echo "🔍 Validating MCP blueprint: ${MCP_CONFIG_PATH} ..."
if [[ -x ".venv/bin/python" ]]; then
  PYTHONPATH="." .venv/bin/python -m mcp_factory.cli validate "${MCP_CONFIG_PATH}"
elif python3 -c "import pydantic" &>/dev/null; then
  PYTHONPATH="." python3 -m mcp_factory.cli validate "${MCP_CONFIG_PATH}"
else
  python3 -m json.tool "${MCP_CONFIG_PATH}" >/dev/null
  echo "✅ JSON syntax valid (full schema validation will run inside Cloud Build container)."
fi

# Extract default service name from JSON blueprint if SERVICE_NAME not explicitly provided
if [[ -z "${SERVICE_NAME:-}" ]]; then
  SERVICE_NAME=$(python3 -c "import json; print(json.load(open('${MCP_CONFIG_PATH}'))['server']['name'].lower().replace('_', '-'))" 2>/dev/null || echo "mcp-factory-server")
fi

if [[ -z "${PROJECT_ID}" ]]; then
  echo "❌ Error: GCP PROJECT_ID is not set. Run 'gcloud config set project YOUR_PROJECT_ID' or export PROJECT_ID=..."
  exit 1
fi

echo "============================================================================"
echo "🚀 Deploying MCP Server to Google Cloud Run"
echo "----------------------------------------------------------------------------"
echo "   Project ID   : ${PROJECT_ID}"
echo "   Service Name : ${SERVICE_NAME}"
echo "   Region       : ${REGION}"
echo "   Blueprint    : ${MCP_CONFIG_PATH}"
echo "   Public Auth  : ${ALLOW_UNAUTHENTICATED}"
echo "============================================================================"

# 3. Ensure Required Google Cloud APIs are Enabled
echo "⚙️  Ensuring Cloud Run, Cloud Build, and Artifact Registry APIs are enabled..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  --project "${PROJECT_ID}"

# 4. Prepare Auth & Environment Flags
AUTH_FLAG="--allow-unauthenticated"
if [[ "${ALLOW_UNAUTHENTICATED}" != "true" ]]; then
  AUTH_FLAG="--no-allow-unauthenticated"
fi

ENV_VARS="MCP_CONFIG_PATH=${MCP_CONFIG_PATH},MCP_TRANSPORT=http"
if [[ -n "${MCP_SERVER_API_KEY:-}" ]]; then
  ENV_VARS="${ENV_VARS},MCP_SERVER_API_KEY=${MCP_SERVER_API_KEY}"
fi
if [[ -n "${EXTRA_ENV_VARS:-}" ]]; then
  ENV_VARS="${ENV_VARS},${EXTRA_ENV_VARS}"
fi

# 5. Deploy to Google Cloud Run from Source
# Note: --session-affinity ensures stateful MCP SSE POST /messages/ hit the same instance as GET /sse
echo "📦 Building container & deploying to Cloud Run..."
gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --platform managed \
  ${AUTH_FLAG} \
  --session-affinity \
  --timeout "${TIMEOUT}" \
  --memory "${MEMORY}" \
  --cpu "${CPU}" \
  --min-instances "${MIN_INSTANCES}" \
  --max-instances "${MAX_INSTANCES}" \
  --set-env-vars "${ENV_VARS}"

# 6. Fetch Deployed Service URL & Print Connection Instructions
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --format "value(status.url)")

echo ""
echo "============================================================================"
echo "✅ Deployment Complete!"
echo "============================================================================"
echo "🌐 Cloud Run Service URL : ${SERVICE_URL}"
echo "🩺 Health Check          : ${SERVICE_URL}/health"
echo "📖 Tool/Resource Catalog : ${SERVICE_URL}/catalog"
echo "⚡ Stateless MCP HTTP    : ${SERVICE_URL}/mcp"
echo "📡 MCP SSE Endpoint      : ${SERVICE_URL}/sse"
echo ""
echo "🧪 Quick Test (List MCP Tools via JSON-RPC over HTTP):"
echo "curl -s -X POST ${SERVICE_URL}/mcp \\"
echo "  -H 'Content-Type: application/json' \\"
echo "  -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}' | jq ."
echo "============================================================================"

