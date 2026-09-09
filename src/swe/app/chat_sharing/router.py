# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...constant import WORKING_DIR
from ..runner.api import (
    _authorize_chat,
    _archive_page,
    _build_chat_history,
    _message_original_id,
    _slice_answer_turn,
    get_workspace,
    _read_history_state,
)
from ..runner.models import ChatHistory
from .models import ChatShareOptions, ChatShareSnapshot
from .service import ChatSharingService
from .store import ChatShareStore

router = APIRouter(tags=["chat-sharing"])
_service: ChatSharingService | None = None
_ANSWER_TURN_STATUSES = frozenset(
    {
        "admitting",
        "running",
        "stopping",
        "completed",
        "cancelled",
        "stopped",
        "failed",
    },
)


class ChatShareCreateRequest(BaseModel):
    turn_ids: list[str] = Field(default_factory=list)


class ChatShareCreateResponse(BaseModel):
    token: str
    share_path: str


def _request_tenant_id(request: Request) -> str:
    state = getattr(request, "state", None)
    value = (
        getattr(state, "effective_tenant_id", None)
        or getattr(state, "tenant_id", None)
        or "default"
    )
    return str(value)


async def _load_share_messages(chat, workspace, history: ChatHistory):
    """Combine compacted archive pages with the current-memory history."""
    pages = []
    cursor: str | None = None
    while True:
        page = await _archive_page(workspace, chat.id, cursor, 50)
        if page.messages:
            pages.append(page.messages)
        if not page.has_more or not page.next_cursor:
            break
        if page.next_cursor == cursor:
            raise OSError("Archive cursor did not advance")
        cursor = page.next_cursor

    archived = [message for page in reversed(pages) for message in page]
    return [*archived, *history.messages]


def _message_turn_status(message: Any) -> str | None:
    metadata = getattr(message, "metadata", None)
    if isinstance(metadata, dict):
        for candidate in (metadata, metadata.get("metadata")):
            if isinstance(candidate, dict):
                status = candidate.get("turn_status")
                if isinstance(status, str) and status:
                    return status
    status = getattr(message, "status", None)
    if isinstance(status, str) and status in _ANSWER_TURN_STATUSES:
        return status
    return None


def _turn_statuses(
    messages: list[Any],
    state: dict[str, Any],
) -> dict[str, str]:
    raw_states = state.get("turn_states")
    statuses: dict[str, str] = {}
    for turn_id, turn in (raw_states or {}).items():
        if not isinstance(turn, dict):
            continue
        status = turn.get("status")
        if isinstance(status, str):
            statuses[str(turn_id)] = status
    for message in messages:
        if getattr(message, "role", None) != "user":
            continue
        turn_id = _message_original_id(message)
        if not turn_id or turn_id in statuses:
            continue
        turn = _slice_answer_turn(messages, msgid=turn_id) or []
        output = turn[1:]
        if not output:
            continue
        output_statuses = [_message_turn_status(item) for item in output]
        if "failed" in output_statuses:
            statuses[turn_id] = "failed"
        elif "stopping" in output_statuses or "running" in output_statuses:
            statuses[turn_id] = next(
                status
                for status in ("stopping", "running")
                if status in output_statuses
            )
        else:
            non_completed = next(
                (
                    status
                    for status in output_statuses
                    if status is not None and status != "completed"
                ),
                None,
            )
            statuses[turn_id] = non_completed or "completed"
    return statuses


def _message_payload(message: Any) -> dict[str, Any]:
    if hasattr(message, "model_dump"):
        return message.model_dump(mode="json")
    if isinstance(message, dict):
        return dict(message)
    raise TypeError("Unsupported chat message")


async def _share_context(chat, workspace):
    history: ChatHistory = await _build_chat_history(
        chat,
        session=workspace.runner.session,
        workspace=workspace,
        non_blocking=True,
    )
    messages = await _load_share_messages(chat, workspace, history)
    state = await _read_history_state(
        workspace.runner.session,
        chat.session_id,
        chat.user_id,
    )
    return history, messages, _turn_statuses(messages, state)


def init_chat_sharing_module(db: Any | None = None) -> None:
    global _service
    if db is None or not getattr(db, "is_connected", False):
        raise RuntimeError("Chat sharing requires a connected database")
    _service = ChatSharingService(
        ChatShareStore(db),
        WORKING_DIR / "chat_shares",
    )


async def initialize_chat_sharing_module(db: Any | None = None) -> None:
    init_chat_sharing_module(db)


def get_service() -> ChatSharingService:
    if _service is None:
        raise HTTPException(status_code=503, detail="Chat sharing unavailable")
    return _service


def _request_user_id(request: Request) -> str:
    value = getattr(getattr(request, "state", None), "user_id", None)
    if not isinstance(value, str) or not value:
        raise HTTPException(
            status_code=403,
            detail="Request user identity is required",
        )
    return value


@router.get(
    "/chats/{chat_id}/share-options",
    response_model=ChatShareOptions,
)
async def get_chat_share_options(
    chat_id: str,
    request: Request,
    workspace=Depends(get_workspace),
) -> ChatShareOptions:
    """Return selectable history and authoritative per-turn statuses."""
    _request_user_id(request)
    try:
        chat = await workspace.chat_manager.get_chat(chat_id)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Chat sharing unavailable",
        ) from exc
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    _authorize_chat(request, chat, workspace)
    try:
        _, messages, statuses = await _share_context(chat, workspace)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Chat sharing unavailable",
        ) from exc
    return ChatShareOptions(
        chat_name=chat.name or "分享的会话",
        messages=[_message_payload(message) for message in messages],
        turn_statuses=statuses,
    )


@router.post(
    "/chats/{chat_id}/share",
    response_model=ChatShareCreateResponse,
)
async def create_chat_share(
    chat_id: str,
    payload: ChatShareCreateRequest,
    request: Request,
    workspace=Depends(get_workspace),
) -> ChatShareCreateResponse:
    service = get_service()
    user_id = _request_user_id(request)
    try:
        chat = await workspace.chat_manager.get_chat(chat_id)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Chat sharing unavailable",
        ) from exc
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    _authorize_chat(request, chat, workspace)
    try:
        _, messages, statuses = await _share_context(chat, workspace)
        record = await service.create_snapshot(
            chat_id=chat.id,
            chat_name=chat.name,
            messages=messages,
            selected_turn_ids=payload.turn_ids,
            turn_statuses=statuses,
            creator_id=user_id,
            tenant_id=_request_tenant_id(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Chat sharing unavailable",
        ) from exc
    return ChatShareCreateResponse(
        token=record.token,
        share_path=f"/chat-share/{record.token}",
    )


@router.get("/chat-shares/{token}", response_model=ChatShareSnapshot)
async def get_chat_share(token: str) -> ChatShareSnapshot:
    service = get_service()
    try:
        payload = await service.get_snapshot(token)
        snapshot = ChatShareSnapshot.model_validate(payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Share not found") from exc
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Chat sharing unavailable",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Chat sharing unavailable",
        ) from exc
    return snapshot
