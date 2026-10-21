"""
Response Transformation Engine for MCPFactory.

Shapes, filters, slices, and formats raw backend responses before returning them to the LLM,
preventing context-window bloat and hiding sensitive internal fields.
"""

from __future__ import annotations

import json
from typing import Any

from mcp_factory.schema import ResponseTransformConfig


def extract_by_path(data: Any, path: str) -> Any:
    """
    Extracts nested data using a dot-separated path (e.g., 'data.orders' or 'results.0.user').
    """
    if not path:
        return data

    current = data
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            if part.isdigit():
                idx = int(part)
                current = current[idx] if 0 <= idx < len(current) else None
            else:
                # Project field across list of dicts! e.g. 'items.name'
                current = [item.get(part) for item in current if isinstance(item, dict) and part in item]
        else:
            return None
    return current


def _filter_dict(item: dict[str, Any], pick: list[str] | None, omit: list[str] | None) -> dict[str, Any]:
    """Filters a single dictionary according to pick_fields and omit_fields."""
    result = dict(item)
    if pick is not None:
        result = {k: v for k, v in result.items() if k in pick}
    if omit is not None:
        result = {k: v for k, v in result.items() if k not in omit}
    return result


def apply_response_transform(data: Any, config: ResponseTransformConfig) -> Any:
    """
    Applies extract_path, max_items, pick_fields, omit_fields, and template formatting
    to a raw backend response.
    """
    result = data

    # 1. Extract nested path
    if config.extract_path:
        result = extract_by_path(result, config.extract_path)

    # 2. Slice list if max_items is configured
    if isinstance(result, list) and config.max_items is not None and config.max_items > 0:
        result = result[: config.max_items]

    # 3. Pick / Omit fields on dict or list of dicts
    if config.pick_fields is not None or config.omit_fields is not None:
        if isinstance(result, dict):
            result = _filter_dict(result, config.pick_fields, config.omit_fields)
        elif isinstance(result, list):
            result = [
                _filter_dict(item, config.pick_fields, config.omit_fields)
                if isinstance(item, dict)
                else item
                for item in result
            ]

    # 4. Apply string template if specified
    if config.template:
        try:
            if isinstance(result, dict):
                return config.template.format(**result)
            return config.template.format(result=result, data=result)
        except Exception:
            # Fallback if template keys don't match exactly
            return f"{config.template}\n{json.dumps(result, indent=2, default=str)}"

    return result


def format_for_mcp(data: Any) -> str:
    """Serializes transformed output cleanly into a text string for MCP TextContent."""
    if isinstance(data, str):
        return data
    try:
        return json.dumps(data, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(data)
