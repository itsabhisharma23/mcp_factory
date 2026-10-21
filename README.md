# MCPFactory: Declarative JSON-Driven MCP Framework for Any Backend Service

**MCPFactory** is a production-grade blueprint and runtime engine that makes creating **Model Context Protocol (MCP)** servers for any backend service—REST APIs, GraphQL endpoints, Google Cloud APIs (BigQuery, Cloud Run, Vertex AI), OpenAPI/Swagger specs, or custom Python logic—as simple as editing a single JSON file and deploying to **Google Cloud Run** with one command.

---

## ✨ Why MCPFactory?

Traditionally, building an MCP server requires writing boilerplate Python/TypeScript code for every endpoint, manually defining input schemas, writing HTTP client logic, handling retries/authentication, and configuring ASGI/SSE web servers for cloud deployment.

With **MCPFactory**, you define **what** your backend does in a declarative JSON blueprint (`configs/*.json`), and the engine automatically handles:

1. **Zero-Code Declarative JSON/YAML Blueprints**: Define backends, tools, resources (including RFC 6570 URI templates), and prompts in JSON.
2. **Multi-Protocol Backend Connectors**:
   - **`rest`**: Automatic URL path interpolation (`/users/{user_id}`), query params, headers, JSON body routing, and field-name aliasing (`customer_id` $\rightarrow$ `userId`).
   - **`graphql`**: Maps MCP tool arguments directly into GraphQL `variables` and executes queries or mutations.
   - **`python`**: Binds custom sync/async Python functions (`plugins.custom_handlers:calculate_order_quote`) for SQL/BigQuery queries, SDK calls, or complex orchestration.
   - **`mock`**: Returns static or argument-interpolated mock payloads for instant prototyping before backend APIs exist.
3. **1-Command OpenAPI / Swagger Importer**: Convert any existing `openapi.json` into an MCP blueprint automatically (`mcp-factory import-openapi`).
4. **LLM Context-Window Optimization (`response` transformer)**: Extract nested JSON paths (`data.orders`), whitelist fields (`pick_fields`), strip sensitive fields (`omit_fields`), and cap list sizes (`max_items`) so raw backend payloads never flood the LLM context window.
5. **Enterprise Outbound & Inbound Auth**:
   - **Outbound**: `api_key`, `bearer`, `basic`, `oauth2_client_credentials` (with auto-caching), `gcp_id_token` (Cloud Run-to-Cloud Run OIDC), and `gcp_access_token` (Google Cloud APIs via ADC).
   - **Inbound**: Protect your Cloud Run MCP server with `MCP_SERVER_API_KEY` (`Authorization: Bearer` or `X-MCP-API-Key`).
6. **Cloud Run Native Dual-Transport Architecture**:
   - **Stateless Streamable HTTP (`POST /mcp`)**: Scales horizontally from 0 to 1,000 Cloud Run instances without sticky-session state issues.
   - **Official MCP SSE (`GET /sse` + `POST /messages/`)**: Compatible with SSE-based MCP clients (configured with Cloud Run `--session-affinity`).
   - **Local Stdio (`--transport stdio`)**: Plugs directly into Claude Desktop, Cursor, and Windsurf locally.
   - **REST-to-MCP Bridge (`POST /api/tools/{tool_name}`)**: Invoke any MCP tool via standard REST JSON.

---

## 🏗️ System Architecture

```mermaid
flowchart LR
    subgraph Clients["AI & MCP Clients"]
        C1["Claude Desktop / Cursor"]
        C2["Vertex AI / LangGraph / ADK"]
        C3["HTTP / REST Clients"]
    end

    subgraph CloudRun["Google Cloud Run (MCPFactory Container)"]
        direction TB
        Auth["Inbound Auth Guard\n(MCP_SERVER_API_KEY / IAM)"]
        Transports["Transport Layer (server.py)\n• POST /mcp (Stateless JSON-RPC)\n• GET /sse + POST /messages/\n• POST /api/tools/{name}\n• GET /health & /catalog"]
        Engine["MCPFactoryEngine (engine.py)\n• Blueprint Loader & Env Interpolator\n• Official MCP Server & JSON-RPC Router\n• Response Transformer (pick/omit/extract)"]
        
        subgraph Connectors["Modular Backend Connectors"]
            R["REST Connector"]
            G["GraphQL Connector"]
            P["Python Plugin Connector"]
            M["Mock Connector"]
        end

        Auth --> Transports --> Engine --> Connectors
    end

    subgraph Backends["Any Backend Service"]
        B1["Internal / Public REST APIs"]
        B2["GraphQL Microservices"]
        B3["Google Cloud APIs & BigQuery\n(via gcp_access_token / gcp_id_token)"]
        B4["Databases & Custom SDKs"]
    end

    Clients -->|"HTTPS / SSE / Stdio"| Auth
    R --> B1
    R --> B3
    G --> B2
    P --> B4
```

---

## 📂 Repository Structure

```text
MCPFactory/
├── README.md                        # Quickstart, JSON Reference & Cloud Run Guide
├── explain.md                       # Deep-dive explanation of MCP concepts & code architecture
├── pyproject.toml                   # Package configuration & `mcp-factory` CLI entrypoint
├── requirements.txt                 # Python dependencies
├── Dockerfile                       # Production non-root container image for Cloud Run
├── cloudbuild.yaml                  # Automated Google Cloud Build -> Cloud Run CI/CD pipeline
├── deploy_cloud_run.sh              # One-command Google Cloud Run deployment script
├── .env.example                     # Sample environment variables
├── mcp_factory/                     # Core Framework Package
│   ├── __init__.py
│   ├── schema.py                    # Pydantic v2 models & ${ENV_VAR:-default} interpolator
│   ├── auth.py                      # Outbound (API Key, Bearer, OAuth2, GCP IAM) & Inbound Auth
│   ├── transformer.py               # Response extractor, field picker/omitter & list slicer
│   ├── engine.py                    # Dynamic MCP Server compiler & JSON-RPC 2.0 dispatcher
│   ├── server.py                    # Starlette ASGI HTTP/SSE app & local stdio runner
│   ├── openapi_converter.py         # OpenAPI 3.x / Swagger -> MCP blueprint converter
│   ├── cli.py                       # `mcp-factory` command-line interface
│   └── connectors/                  # Modular Backend Execution Connectors
│       ├── base.py                  # Abstract BaseConnector interface
│       ├── rest.py                  # REST/HTTP connector with smart parameter routing
│       ├── graphql.py               # GraphQL query & mutation connector
│       ├── python_plugin.py         # Dynamic Python function loader (`module:func`)
│       └── mock.py                  # Static & argument-interpolated mock connector
├── plugins/
│   └── custom_handlers.py           # Example sync & async Python plugin handlers
├── configs/
│   ├── ecommerce_service.json       # Showcase blueprint (REST + GraphQL + Python + Mock)
│   ├── gcp_cloud_ops.json           # Zero-code BigQuery & Cloud Run OIDC blueprint
│   └── sample_openapi.json          # Sample OpenAPI 3.0 spec for converter testing
└── tests/
    └── test_factory.py              # Automated unit & integration test suite
```

---

## 🚀 60-Second Quickstart

### 1. Install Dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install --no-deps -e .
```

### 2. Validate a Declarative Blueprint

```bash
mcp-factory validate configs/ecommerce_service.json
```

Output:
```text
✅ Blueprint 'configs/ecommerce_service.json' is valid!
   Server   : ecommerce-unified-mcp (v1.2.0)
   Backends : 2 (customer_rest_api, countries_graphql)
   Tools    : 6
   Resources: 3
   Prompts  : 2
```

### 3. Test Any Tool Directly from the CLI

Before connecting an LLM client, you can execute any configured tool directly from your terminal:

```bash
# Test a Python Plugin tool
mcp-factory call configs/ecommerce_service.json calculate_enterprise_quote \
  --args '{"unit_price": 250.0, "quantity": 20, "tier": "enterprise"}'

# Test a Mock Connector tool
mcp-factory call configs/ecommerce_service.json check_warehouse_inventory \
  --args '{"sku": "GPU-H100-80G", "warehouse_region": "us-central1"}'

# Test a Live REST API tool (fetches from JSONPlaceholder and filters fields)
mcp-factory call configs/ecommerce_service.json get_customer_profile \
  --args '{"user_id": 1}'
```

### 4. Start the MCP Server Locally (HTTP / Cloud Run Mode)

```bash
mcp-factory run --config configs/ecommerce_service.json --transport http --port 8080
```

Once running, test the endpoints in another terminal:

```bash
# 1. Health & Readiness Check
curl -s http://localhost:8080/health | jq .

# 2. Full Tool, Schema, Resource & Prompt Catalog
curl -s http://localhost:8080/catalog | jq .

# 3. Standard MCP JSON-RPC 2.0 (List Tools)
curl -s -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq .

# 4. Standard MCP JSON-RPC 2.0 (Call a Tool)
curl -s -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
      "name": "calculate_enterprise_quote",
      "arguments": {"unit_price": 120, "quantity": 50, "tier": "gold"}
    }
  }' | jq .

# 5. Direct REST-to-MCP Bridge
curl -s -X POST http://localhost:8080/api/tools/check_warehouse_inventory \
  -H "Content-Type: application/json" \
  -d '{"sku": "TPU-V5P"}' | jq .
```

---

## 🧩 Declarative JSON Blueprint Guide

Creating a new MCP server requires zero Python code. Run `mcp-factory init configs/my_service.json` or create a JSON file with the following sections:

### 1. Environment Variable Interpolation (`${VAR:-default}`)
Anywhere in your JSON file, you can reference environment variables with optional fallback defaults:
```json
"base_url": "${ORDER_SERVICE_BASE_URL:-https://api.example.com/v1}",
"token": "${ORDER_SERVICE_API_KEY}"
```

### 2. Defining Backends & Authentication (`backends`)

MCPFactory supports **7 outbound authentication modes** out of the box:

| Auth `type` | Description | Key Fields |
| :--- | :--- | :--- |
| `none` | No authentication | — |
| `api_key` | Injects API key into header or query param | `header_name` (or `query_param`), `token` |
| `bearer` | Injects `Authorization: Bearer <token>` | `token` |
| `basic` | HTTP Basic Auth (`base64(user:pass)`) | `username`, `password` |
| `oauth2_client_credentials` | Fetches & caches OAuth2 token automatically | `token_url`, `client_id`, `client_secret`, `scopes` |
| `gcp_id_token` | Google Cloud OIDC ID token (Cloud Run $\rightarrow$ Cloud Run) | `audience` (target Cloud Run URL) |
| `gcp_access_token` | Google Cloud IAM token via ADC (BigQuery, Vertex AI, GCS) | `scopes` |

### 3. Defining Tools (`tools`)

Every tool maps an LLM tool call to a backend execution:

```json
{
  "name": "get_customer_profile",
  "description": "Fetches a customer's contact details by numeric user ID.",
  "backend": "customer_rest_api",
  "connector": "rest",
  "method": "GET",
  "endpoint": "/users/{user_id}",
  "parameters": {
    "user_id": {
      "type": "integer",
      "description": "Unique numeric customer ID",
      "required": true,
      "location": "path"
    }
  },
  "response": {
    "extract_path": "",
    "pick_fields": ["id", "name", "email", "company"]
  }
}
```

#### Smart Parameter Routing (`location: "auto"`)
If you omit `"location"` on a parameter, MCPFactory defaults to `"auto"`:
1. If `{param_name}` appears in `"endpoint"` (e.g., `/users/{user_id}`), it is automatically interpolated into the **URL path**.
2. Otherwise, for `GET` and `DELETE` requests, it is sent as a **URL query parameter**.
3. For `POST`, `PUT`, and `PATCH` requests, it is sent as a **JSON request body field**.
4. Use `"field_name": "userId"` to expose clean `snake_case` parameter names (`customer_id`) to the LLM while sending `camelCase` (`userId`) to your backend API.

---

## 🔄 Auto-Generating an MCP Server from OpenAPI / Swagger

Already have an `openapi.json` or Swagger specification for your backend service? Convert it into an MCPFactory blueprint in one command:

```bash
mcp-factory import-openapi \
  --input configs/sample_openapi.json \
  --output configs/payments_mcp.json
```

Then validate and inspect the generated tools:

```bash
mcp-factory validate configs/payments_mcp.json
mcp-factory inspect configs/payments_mcp.json
```

---

## ☁️ Deploying to Google Cloud Run

MCPFactory is engineered specifically for serverless execution on **Google Cloud Run**.

### Option 1: One-Command Deployment Script (`deploy_cloud_run.sh`)

```bash
chmod +x deploy_cloud_run.sh

PROJECT_ID="your-gcp-project-id" \
REGION="us-central1" \
SERVICE_NAME="ecommerce-mcp" \
MCP_CONFIG_PATH="configs/ecommerce_service.json" \
./deploy_cloud_run.sh
```

Or via the CLI wrapper:

```bash
# Preview deployment command with --dry-run
mcp-factory deploy configs/ecommerce_service.json \
  --project your-gcp-project-id \
  --region us-central1 \
  --dry-run

# Execute deployment
mcp-factory deploy configs/ecommerce_service.json \
  --project your-gcp-project-id \
  --region us-central1
```

### Option 2: Automated CI/CD with Cloud Build (`cloudbuild.yaml`)

```bash
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=_SERVICE_NAME=ecommerce-mcp,_MCP_CONFIG_PATH=configs/ecommerce_service.json
```

### Connecting MCP Clients to Your Cloud Run Service

Once deployed, Cloud Run provides an HTTPS URL such as `https://ecommerce-mcp-xyz-uc.a.run.app`.

1. **Streamable HTTP / JSON-RPC Clients**:
   - Endpoint: `POST https://ecommerce-mcp-xyz-uc.a.run.app/mcp`
2. **SSE (Server-Sent Events) MCP Clients**:
   - SSE Endpoint: `https://ecommerce-mcp-xyz-uc.a.run.app/sse`
3. **Claude Desktop / Cursor (via `mcp-remote` or local `stdio`)**:
   - **Local Stdio** (`claude_desktop_config.json`):
     ```json
     {
       "mcpServers": {
         "ecommerce-mcp": {
           "command": "python3",
           "args": [
             "-m", "mcp_factory.cli", "run",
             "--config", "/absolute/path/to/MCPFactory/configs/ecommerce_service.json",
             "--transport", "stdio"
           ]
         }
       }
     }
     ```
   - **Remote Cloud Run SSE** (`claude_desktop_config.json` using `mcp-remote`):
     ```json
     {
       "mcpServers": {
         "ecommerce-cloud-run": {
           "command": "npx",
           "args": [
             "-y",
             "mcp-remote",
             "https://ecommerce-mcp-xyz-uc.a.run.app/sse",
             "--header",
             "Authorization: Bearer YOUR_MCP_SERVER_API_KEY"
           ]
         }
       }
     }
     ```

---

## 🧪 Running Automated Tests

```bash
pytest -v
# or using standard library unittest:
python3 -m unittest discover -s tests -v
```

---

## 📖 Deep-Dive Architectural & Code Walkthrough

See **[explain.md](./explain.md)** for a comprehensive explanation of all Model Context Protocol concepts, transport internals (`stdio` vs. `SSE` vs. `Streamable HTTP`), dynamic schema compilation, and a module-by-module walkthrough of the codebase.
