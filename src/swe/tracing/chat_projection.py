# -*- coding: utf-8 -*-
"""Explicit chat payload projections with a total serialized byte budget."""

from __future__ import annotations

import json
import os
from typing import Any

from .sanitizer import sanitize_trace_value

_DEFAULT_LIMIT = 1024 * 1024


def chat_limit() -> int:
    try:
        return max(
            1024,
            int(
                os.environ.get(
                    "SWE_AGENT_TRACE_CHAT_MAX_BYTES",
                    _DEFAULT_LIMIT,
                ),
            ),
        )
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def project_content(content: Any) -> Any:
    """Retain semantic blocks; never export binary media or arbitrary objects."""
    if isinstance(content, str) or content is None:
        return content
    if not isinstance(content, (list, tuple)):
        return [{"type": "unknown", "omitted_reason": "unsupported_block"}]
    blocks = []
    for block in content:
        if not isinstance(block, dict):
            blocks.append(
                {"type": "unknown", "omitted_reason": "unsupported_block"},
            )
            continue
        kind = block.get("type", "unknown")
        if kind in {"text", "input_text", "output_text", "thinking"}:
            field = "thinking" if kind == "thinking" else "text"
            blocks.append({"type": kind, field: block.get(field, "")})
        elif kind in {"tool_use", "tool_result"}:
            allowed = (
                ("id", "name", "input")
                if kind == "tool_use"
                else (
                    "id",
                    "name",
                    "tool_use_id",
                    "is_error",
                    "output",
                )
            )
            projected = {
                "type": kind,
                **{k: block[k] for k in allowed if k in block},
            }
            if kind == "tool_result" and "content" in block:
                projected["content"] = project_content(block["content"])
            blocks.append(projected)
        else:
            blocks.append(
                {"type": str(kind), "omitted_reason": "unsupported_block"},
            )
    return blocks


def project_message(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        result = {
            k: message[k]
            for k in ("role", "name", "tool_call_id")
            if k in message
        }
        result["content"] = project_content(message.get("content"))
        if "tool_calls" in message:
            result["tool_calls"] = [
                {k: call[k] for k in ("id", "type", "function") if k in call}
                for call in message["tool_calls"]
                if isinstance(call, dict)
            ]
        return result
    return {
        "role": str(getattr(message, "role", "assistant")),
        "content": project_content(getattr(message, "content", None)),
    }


def _shrink(value: Any) -> str | None:
    """Reduce the largest leaf, keeping the resulting payload valid JSON."""
    candidates: list[tuple[int, Any, Any, Any, str]] = []

    def visit(item: Any, path: str):
        if isinstance(item, dict):
            for key, child in item.items():
                if key == "capture":
                    continue
                child_path = f"{path}.{key}"
                if isinstance(child, str) and len(child) > 16:
                    candidates.append(
                        (
                            len(child.encode("utf-8")),
                            item,
                            key,
                            child,
                            child_path,
                        ),
                    )
                elif isinstance(child, (dict, list)):
                    visit(child, child_path)
        elif isinstance(item, list):
            if item:
                candidates.append(
                    (
                        len(_encode(item).encode("utf-8")),
                        item,
                        None,
                        None,
                        path,
                    ),
                )
            for index, child in enumerate(item):
                if isinstance(child, (dict, list)):
                    visit(child, f"{path}[{index}]")

    visit(value, "$")
    if not candidates:
        return None
    leaves = [item for item in candidates if item[2] is not None]
    _, parent, key, child, path = max(
        leaves or candidates,
        key=lambda candidate: candidate[0],
    )
    if key is None:
        del parent[: max(1, len(parent) // 2)]
    else:
        parent[key] = child[: len(child) // 2] + "…"
    return path


def bounded_payload(payload: dict[str, Any]) -> dict[str, Any]:
    limit = chat_limit()
    safe = sanitize_trace_value(
        payload,
        max_bytes=limit,
        max_depth=20,
        max_items=4096,
    )
    result = safe.value
    result["capture"] = {
        "truncated": safe.truncated,
        "limit_bytes": limit,
        "omitted_count": 0,
        "omitted_paths": (
            ["$: depth/item/string limit"] if safe.truncated else []
        ),
        "redacted": safe.value != payload and not safe.truncated,
    }
    while len(_encode(result).encode("utf-8")) > limit:
        result["capture"]["truncated"] = True
        result["capture"]["omitted_count"] += 1
        path = _shrink(result)
        if path is None:
            return {
                **{
                    k: result[k]
                    for k in ("schema_version", "status", "complete", "role")
                    if k in result
                },
                "capture": result["capture"],
            }
        paths = result["capture"]["omitted_paths"]
        if len(paths) < 8 and path not in paths:
            paths.append(path[:80])
    return result


def project_chat_input(
    messages: Any,
    tools: Any,
    tool_choice: Any,
) -> dict[str, Any]:
    return bounded_payload(
        {
            "schema_version": 2,
            "messages": [project_message(message) for message in messages],
            "tools": list(tools or []),
            "tool_choice": tool_choice,
        },
    )


def project_chat_output(
    result: Any,
    status: str = "completed",
) -> dict[str, Any]:
    message = project_message(result)
    content = message["content"]
    blocks = (
        content
        if isinstance(content, list)
        else [{"type": "text", "text": content or ""}]
    )
    output = bounded_payload(
        {
            "schema_version": 2,
            "role": message["role"],
            "content": blocks,
            "status": status,
            "complete": status == "completed",
            "text": "".join(
                b.get("text", "") for b in blocks if b.get("type") == "text"
            ),
            "text_truncated": False,
            "tool_call_names": [
                b["name"]
                for b in blocks
                if b.get("type") == "tool_use" and b.get("name")
            ],
        },
    )
    output["text_truncated"] = output["capture"]["truncated"]
    return output
