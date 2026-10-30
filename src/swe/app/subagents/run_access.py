# -*- coding: utf-8 -*-
"""Persisted Background SubAgent records visible to one Chat session."""

from pathlib import Path
from typing import Any, Mapping

from .models import BackgroundSubAgentRunRecord


def record_belongs_to_chat(
    record: BackgroundSubAgentRunRecord,
    request_context: Mapping[str, Any],
) -> bool:
    """Require a reliable session and any recorded Chat identity."""
    session_id = str(request_context.get("session_id") or "")
    if not session_id.strip() or record.spec.parent_thread_id != session_id:
        return False
    return not record.spec.parent_chat_id or record.spec.parent_chat_id == str(
        request_context.get("chat_id") or "",
    )


def read_chat_run_record(
    run_store_dir: Path,
    request_context: Mapping[str, Any],
    run_id: str,
    *,
    strict: bool = False,
) -> BackgroundSubAgentRunRecord | None:
    """Read and authorize a record before invoking supervisor side effects."""
    if not run_id or Path(run_id).name != run_id:
        return None
    try:
        record = BackgroundSubAgentRunRecord.model_validate_json(
            (run_store_dir / f"{run_id}.json").read_text(encoding="utf-8"),
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        if strict:
            raise
        return None
    if strict and record.run_id != run_id:
        raise ValueError("run record identity mismatch")
    return record if record_belongs_to_chat(record, request_context) else None


def read_chat_run_records(
    run_store_dir: Path,
    request_context: Mapping[str, Any],
) -> list[BackgroundSubAgentRunRecord]:
    """Project retained records without consuming supervisor notifications."""
    if not str(request_context.get("session_id") or "").strip():
        return []
    records = []
    for path in run_store_dir.glob("subagent-*.json"):
        record = read_chat_run_record(
            run_store_dir,
            request_context,
            path.stem,
        )
        if record is not None:
            records.append(record)
    return sorted(records, key=lambda item: item.created_at)


def read_chat_run_by_nickname(
    run_store_dir: Path,
    request_context: Mapping[str, Any],
    nickname: str,
) -> BackgroundSubAgentRunRecord | None:
    """Resolve an exact nickname only when one Chat-owned record matches."""
    matches = [
        record
        for record in read_chat_run_records(run_store_dir, request_context)
        if record.nickname == nickname
    ]
    return matches[0] if len(matches) == 1 else None
