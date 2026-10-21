"""
Example Custom Python Plugin Handlers for MCPFactory.

When a backend service isn't a simple REST or GraphQL endpoint (for example, a direct
BigQuery SQL query, a Redis cache lookup, a multi-service aggregation, or custom pricing logic),
you can reference any sync or async Python function in your JSON blueprint via:

    "connector": "python",
    "handler": "plugins.custom_handlers:calculate_order_quote"
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


async def calculate_order_quote(
    unit_price: float,
    quantity: int,
    tier: str = "standard",
    tax_rate_percent: float = 8.25,
) -> dict[str, Any]:
    """
    Calculates an enterprise order quote with tiered volume discounts and tax.
    Demonstrates an async Python plugin tool callable via MCP.
    """
    discount_map = {
        "standard": 0.0,
        "silver": 0.05,
        "gold": 0.12,
        "enterprise": 0.20,
    }
    discount_rate = discount_map.get(tier.lower(), 0.0)
    if quantity >= 100:
        discount_rate = min(0.30, discount_rate + 0.05)

    subtotal = round(unit_price * quantity, 2)
    discount_amount = round(subtotal * discount_rate, 2)
    discounted_subtotal = round(subtotal - discount_amount, 2)
    tax_amount = round(discounted_subtotal * (tax_rate_percent / 100.0), 2)
    total = round(discounted_subtotal + tax_amount, 2)

    return {
        "unit_price": unit_price,
        "quantity": quantity,
        "customer_tier": tier,
        "subtotal": subtotal,
        "discount_rate_applied": discount_rate,
        "discount_amount": discount_amount,
        "tax_rate_percent": tax_rate_percent,
        "tax_amount": tax_amount,
        "final_total_usd": total,
        "quoted_at": datetime.now(timezone.utc).isoformat(),
    }


def get_runtime_diagnostics(environment: str = "cloud-run") -> dict[str, Any]:
    """
    Synchronous Python plugin handler that can be bound to an MCP Tool or MCP Resource.
    """
    return {
        "framework": "MCPFactory",
        "environment": environment,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "capabilities": [
            "REST Connector",
            "GraphQL Connector",
            "Python Plugin Connector",
            "Mock Connector",
            "OpenAPI Auto-Importer",
            "Cloud Run Stateless HTTP & SSE",
        ],
    }
