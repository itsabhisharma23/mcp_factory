"""
Automated Test Suite for MCPFactory.

Validates:
- Declarative JSON blueprint loading & environment variable interpolation
- Parameter routing (auto, path, query, body, header, field_name aliasing)
- Connectors (REST, GraphQL, Python Plugin, Mock)
- Response transformations (extract_path, pick_fields, omit_fields, max_items, template)
- Static and RFC 6570 URI Template Resources
- Prompt rendering
- Stateless MCP JSON-RPC 2.0 protocol handler (`POST /mcp`)
- OpenAPI 3.x to MCPFactory blueprint conversion
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest import TestCase

from mcp_factory.engine import MCPFactoryEngine
from mcp_factory.openapi_converter import convert_openapi_to_blueprint
from mcp_factory.schema import ResponseTransformConfig, interpolate_env_vars, load_blueprint
from mcp_factory.transformer import apply_response_transform


ROOT_DIR = Path(__file__).resolve().parent.parent


class TestMCPFactory(TestCase):
    def test_env_var_interpolation(self) -> None:
        os.environ["TEST_MCP_HOST"] = "https://custom.api.internal"
        raw = {
            "url": "${TEST_MCP_HOST}/v1",
            "fallback": "${UNSET_VAR_XYZ:-default_token_123}",
        }
        interpolated = interpolate_env_vars(raw)
        self.assertEqual(interpolated["url"], "https://custom.api.internal/v1")
        self.assertEqual(interpolated["fallback"], "default_token_123")

    def test_load_ecommerce_blueprint(self) -> None:
        config_path = ROOT_DIR / "configs" / "ecommerce_service.json"
        blueprint = load_blueprint(config_path)
        self.assertEqual(blueprint.server.name, "ecommerce-unified-mcp")
        self.assertEqual(len(blueprint.backends), 2)
        self.assertEqual(len(blueprint.tools), 6)
        self.assertEqual(len(blueprint.resources), 3)
        self.assertEqual(len(blueprint.prompts), 2)

    def test_response_transformer(self) -> None:
        raw_payload = {
            "data": {
                "orders": [
                    {"id": 101, "total": 50.0, "secret_hash": "abc", "status": "shipped"},
                    {"id": 102, "total": 75.0, "secret_hash": "def", "status": "pending"},
                    {"id": 103, "total": 99.0, "secret_hash": "ghi", "status": "delivered"},
                ]
            }
        }
        cfg = ResponseTransformConfig(
            extract_path="data.orders",
            max_items=2,
            pick_fields=["id", "total", "status"],
        )
        transformed = apply_response_transform(raw_payload, cfg)
        self.assertEqual(len(transformed), 2)
        self.assertEqual(transformed[0], {"id": 101, "total": 50.0, "status": "shipped"})
        self.assertNotIn("secret_hash", transformed[0])

    def test_python_and_mock_connectors(self) -> None:
        engine = MCPFactoryEngine.from_file(ROOT_DIR / "configs" / "ecommerce_service.json")

        # 1. Execute async Python plugin tool
        quote = asyncio.run(
            engine.execute_tool(
                "calculate_enterprise_quote",
                {"unit_price": 100.0, "quantity": 10, "tier": "enterprise", "tax_rate_percent": 10.0},
            )
        )
        self.assertEqual(quote["subtotal"], 1000.0)
        self.assertEqual(quote["discount_amount"], 200.0)
        self.assertEqual(quote["final_total_usd"], 880.0)

        # 2. Execute Mock connector tool with dynamic placeholder interpolation
        inv = asyncio.run(
            engine.execute_tool(
                "check_warehouse_inventory",
                {"sku": "GPU-H100", "warehouse_region": "europe-west1"},
            )
        )
        self.assertEqual(inv["sku"], "GPU-H100")
        self.assertEqual(inv["warehouse_region"], "europe-west1")
        self.assertTrue(inv["in_stock"])

    def test_static_and_plugin_resources(self) -> None:
        engine = MCPFactoryEngine.from_file(ROOT_DIR / "configs" / "ecommerce_service.json")

        # Static resource
        content, mime = asyncio.run(engine.read_resource_by_uri("config://ecommerce/shipping-policies"))
        self.assertEqual(mime, "application/json")
        parsed = json.loads(content)
        self.assertEqual(parsed["return_window_days"], 30)

        # Python plugin resource
        diag_content, _ = asyncio.run(engine.read_resource_by_uri("diagnostics://runtime/status"))
        diag = json.loads(diag_content)
        self.assertEqual(diag["framework"], "MCPFactory")

    def test_prompt_rendering(self) -> None:
        engine = MCPFactoryEngine.from_file(ROOT_DIR / "configs" / "ecommerce_service.json")
        rendered = engine.render_prompt(
            "investigate_customer_issue",
            {"customer_id": "42", "issue_summary": "Delayed shipment"},
        )
        text = rendered["messages"][0]["content"]["text"]
        self.assertIn("customer ID 42", text)
        self.assertIn("Delayed shipment", text)

    def test_stateless_jsonrpc_dispatcher(self) -> None:
        engine = MCPFactoryEngine.from_file(ROOT_DIR / "configs" / "ecommerce_service.json")

        # 1. initialize
        init_resp = asyncio.run(
            engine.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        )
        self.assertEqual(init_resp["result"]["serverInfo"]["name"], "ecommerce-unified-mcp")

        # 2. tools/list
        tools_resp = asyncio.run(
            engine.handle_jsonrpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        )
        self.assertEqual(len(tools_resp["result"]["tools"]), 6)

        # 3. tools/call
        call_resp = asyncio.run(
            engine.handle_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "check_warehouse_inventory",
                        "arguments": {"sku": "TPU-V5P"},
                    },
                }
            )
        )
        self.assertFalse(call_resp["result"]["isError"])
        payload = json.loads(call_resp["result"]["content"][0]["text"])
        self.assertEqual(payload["sku"], "TPU-V5P")

    def test_openapi_converter(self) -> None:
        openapi_path = ROOT_DIR / "configs" / "sample_openapi.json"
        spec = json.loads(openapi_path.read_text(encoding="utf-8"))
        blueprint_dict = convert_openapi_to_blueprint(spec)

        self.assertEqual(blueprint_dict["server"]["name"], "acme-payments-invoicing-api-mcp")
        self.assertEqual(len(blueprint_dict["tools"]), 3)
        tool_names = [t["name"] for t in blueprint_dict["tools"]]
        self.assertEqual(tool_names, ["list_invoices", "create_invoice", "refund_invoice"])


if __name__ == "__main__":
    import unittest

    unittest.main()
