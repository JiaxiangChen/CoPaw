# -*- coding: utf-8 -*-
import hashlib
import json
import logging
import platform
from datetime import datetime, timezone
from typing import List, Optional, Union
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agentscope.message import Msg
from agentscope_runtime.engine.schemas.agent_schemas import (
    Message,
    TextContent,
    ImageContent,
    AudioContent,
    VideoContent,
    FileContent,
    DataContent,
    FunctionCall,
    FunctionCallOutput,
    MessageType,
)

from ...agents.utils.tool_summary import (
    generate_tool_call_summary,
    generate_tool_output_summary,
)
from ...agents.tool_failure import TOOL_GOVERNANCE_BLOCK_FIELD
from ...config import load_config  # pylint: disable=no-name-in-module
from .models import ChatMessage
from .operation_group import (
    OPERATION_GROUP_FIELD,
    attach_operation_group,
    normalize_operation_group,
)
from .tool_status import (
    apply_governance_tool_status,
    apply_running_tool_status,
    apply_terminal_tool_status,
)

logger = logging.getLogger(__name__)
_MISSING_SOURCE_ID_PLACEHOLDER = "(not provided)"
_RUNTIME_MESSAGE_ROLES = {"assistant", "system", "user", "tool"}


def legacy_message_id(
    session_id: str,
    position: int,
    timestamp: str | None,
    role: str | None,
    content: object,
) -> str:
    """Return a stable identity for persisted messages without a raw ID."""
    normalized_content = (
        content
        if isinstance(content, str)
        else json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    )
    material = "\x1f".join(
        (
            str(session_id or ""),
            str(position),
            str(timestamp or ""),
            str(role or ""),
            normalized_content,
        ),
    )
    return f"legacy:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _normalize_runtime_message_role(
    role: str | None,
    metadata: dict,
) -> str:
    """把不被 runtime schema 接受的角色降级为 system。"""
    if not isinstance(role, str) or not role:
        return "assistant"
    if role in _RUNTIME_MESSAGE_ROLES:
        return role
    metadata["original_role"] = role
    return "system"


def build_env_context(
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    channel: Optional[str] = None,
    working_dir: Optional[str] = None,
    source_id: Optional[str] = None,
    user_name: Optional[str] = None,
    add_hint: bool = True,
) -> str:
    """
    Build environment context with current request context prepended.

    Args:
        session_id: Current session ID
        user_id: Current user ID
        channel: Current channel name
        source_id: Current request source ID
        working_dir: Working directory path
        source_id: Current source ID (request origin)
        user_name: Current user name
        add_hint: Whether to add hint context
    Returns:
        Formatted environment context string
    """
    parts = []
    user_tz = load_config().user_timezone or "UTC"
    try:
        now = datetime.now(ZoneInfo(user_tz))
    except (ZoneInfoNotFoundError, KeyError):
        logger.warning("Invalid timezone %r, falling back to UTC", user_tz)
        now = datetime.now(timezone.utc)
        user_tz = "UTC"

    if session_id is not None:
        parts.append(f"- Session ID: {session_id}")
    if user_id is not None:
        parts.append(f"- User ID: {user_id}")
    if user_name is not None:
        parts.append(f"- User Name: {user_name}")
    if source_id is not None:
        parts.append(f"- Source ID: {source_id}")
    if channel is not None:
        parts.append(f"- Channel: {channel}")
    parts.append(
        "- Source ID: "
        + (source_id if source_id else _MISSING_SOURCE_ID_PLACEHOLDER),
    )

    parts.append(
        f"- OS: {platform.system()} {platform.release()} "
        f"({platform.machine()})",
    )

    if working_dir is not None:
        parts.append(f"- Working directory: {working_dir}")
    parts.append(
        f"- Current time: {now.strftime('%Y-%m-%d %H:%M:%S')} "
        f"{user_tz} ({now.strftime('%A')})",
    )

    if add_hint:
        parts.append(
            "- Important:\n"
            "  1. Prefer using skills when completing tasks "
            "(e.g. use the cron skill for scheduled tasks). "
            "Consult the relevant skill documentation if unsure.\n"
            "  2. When using write_file, if you want to avoid overwriting "
            "existing content, use read_file first to inspect the file, "
            "then use edit_file for partial updates or appending.\n"
            "  3. Use tool calls to perform actions. A response without a "
            "tool call indicates the task is complete. To continue a task, "
            "you must generate a tool call or provide useful feedback if "
            "you are blocked.\n",
        )

    return (
        "====================\n" + "\n".join(parts) + "\n===================="
    )


def _is_local_file_url(url: str) -> bool:
    """True if url is a local file reference (file:// or absolute path)."""
    if not url or not isinstance(url, str):
        return False
    s = url.strip()
    if not s:
        return False
    lower = s.lower()

    # Check for remote URLs
    if lower.startswith(("http://", "https://", "data:")):
        return False

    # Check for local file patterns: file://, Unix paths, or Windows drives
    return (
        lower.startswith("file:")
        or (s.startswith("/") and not s.startswith("//"))
        or (len(s) >= 2 and s[1] == ":" and s[0].isalpha())
    )


def _abspath_from_url(url: str) -> str:
    """Extract absolute path from file:// URL."""
    s = url.strip()
    if s.lower().startswith("file:"):
        s = s[5:]
    s = "/" + s.lstrip("/")
    return s


def _resolve_content_url(url: str) -> str:
    """If url is local, return filename only; frontend builds URL."""
    if not isinstance(url, str):
        return url
    if not _is_local_file_url(url):
        return url
    return _abspath_from_url(url)


# pylint: disable=too-many-branches,too-many-statements, too-many-nested-blocks
def _build_media_message_from_block(
    block: dict,
    role: str,
    metadata: dict,
) -> Message:
    output = block.get("output")
    media_message = None
    if isinstance(output, list):
        media_items = [
            item
            for item in output
            if isinstance(item, dict)
            and item.get("type") in ("image", "audio", "video", "file")
        ]
        if media_items:
            media_message = Message(
                type=MessageType.MESSAGE,
                role=role,
            )
            media_message.metadata = metadata

            for item in media_items:
                itype = item.get("type")

                if itype == "image":
                    kwargs = {}
                    source = item.get("source")
                    if (
                        isinstance(source, dict)
                        and source.get("type") == "url"
                    ):
                        kwargs["image_url"] = _resolve_content_url(
                            source.get("url", ""),
                        )
                    elif (
                        isinstance(source, dict)
                        and source.get("type") == "base64"
                    ):
                        media_type = source.get(
                            "media_type",
                            "image/jpeg",
                        )
                        base64_data = source.get("data", "")
                        kwargs["image_url"] = (
                            f"data:{media_type};base64,{base64_data}"
                        )
                    media_message.add_content(
                        new_content=ImageContent(
                            delta=False,
                            index=None,
                            **kwargs,
                        ),
                    )

                elif itype == "audio":
                    kwargs = {}
                    source = item.get("source")
                    if (
                        isinstance(source, dict)
                        and source.get("type") == "url"
                    ):
                        url = _resolve_content_url(
                            source.get("url", ""),
                        )
                        kwargs["data"] = url
                        try:
                            kwargs["format"] = urlparse(
                                url,
                            ).path.split(
                                ".",
                            )[-1]
                        except (
                            AttributeError,
                            IndexError,
                            ValueError,
                        ):
                            kwargs["format"] = None
                    elif (
                        isinstance(source, dict)
                        and source.get("type") == "base64"
                    ):
                        media_type = source.get("media_type")
                        base64_data = source.get("data", "")
                        kwargs["data"] = (
                            f"data:{media_type};base64,{base64_data}"
                        )
                        kwargs["format"] = media_type
                    media_message.add_content(
                        new_content=AudioContent(
                            delta=False,
                            index=None,
                            **kwargs,
                        ),
                    )

                elif itype == "video":
                    kwargs = {}
                    source = item.get("source")
                    if (
                        isinstance(source, dict)
                        and source.get("type") == "url"
                    ):
                        kwargs["video_url"] = _resolve_content_url(
                            source.get("url", ""),
                        )
                    elif (
                        isinstance(source, dict)
                        and source.get("type") == "base64"
                    ):
                        media_type = source.get(
                            "media_type",
                            "video/mp4",
                        )
                        base64_data = source.get("data", "")
                        kwargs["video_url"] = (
                            f"data:{media_type};base64,{base64_data}"
                        )
                    media_message.add_content(
                        new_content=VideoContent(
                            delta=False,
                            index=None,
                            **kwargs,
                        ),
                    )

                elif itype == "file":
                    kwargs = {"filename": item.get("filename", "")}
                    source = item.get("source")
                    if (
                        isinstance(source, dict)
                        and source.get("type") == "url"
                    ):
                        kwargs["file_url"] = _resolve_content_url(
                            source.get("url", ""),
                        )
                    elif (
                        isinstance(source, dict)
                        and source.get("type") == "base64"
                    ):
                        media_type = source.get(
                            "media_type",
                            "application/octet-stream",
                        )
                        base64_data = source.get("data", "")
                        kwargs["file_url"] = (
                            f"data:{media_type};base64,{base64_data}"
                        )
                    elif isinstance(source, str):
                        kwargs["file_url"] = _resolve_content_url(
                            source,
                        )
                    media_message.add_content(
                        new_content=FileContent(
                            delta=False,
                            index=None,
                            **kwargs,
                        ),
                    )
    return media_message


# pylint: disable=too-many-branches,too-many-statements, too-many-nested-blocks
def agentscope_msg_to_message(
    messages: Union[Msg, List[Msg]],
    *,
    session_id: str = "",
    position_offset: int = 0,
) -> List[ChatMessage]:
    """
    Convert AgentScope Msg(s) into one or more runtime Message objects.

    Args:
        messages: AgentScope message(s) from streaming.

    Returns:
        List[Message]: One or more constructed runtime Message objects.
    """
    if isinstance(messages, Msg):
        msgs = [messages]
    elif isinstance(messages, list):
        msgs = messages
    else:
        raise TypeError(f"Expected Msg or list[Msg], got {type(messages)}")

    results: List[ChatMessage] = []

    def attach_timestamp(
        message: Message,
        timestamp: str | None,
    ) -> ChatMessage:
        payload = message.model_dump()
        payload["timestamp"] = timestamp
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            stable_id = metadata.get("original_id")
            if isinstance(stable_id, str) and stable_id:
                payload["id"] = stable_id
        return ChatMessage.model_validate(payload)

    for position, msg in enumerate(msgs, start=position_offset):
        raw_id = getattr(msg, "id", None)
        raw_id = str(raw_id).strip() if raw_id else ""
        stable_id = raw_id or legacy_message_id(
            session_id,
            position,
            msg.timestamp,
            msg.role,
            msg.content,
        )
        metadata = {
            "original_id": stable_id,
            "original_name": msg.name,
            "metadata": msg.metadata,
        }
        role = _normalize_runtime_message_role(msg.role, metadata)

        if isinstance(msg.content, str):
            message = Message(type=MessageType.MESSAGE, role=role)
            message.metadata = metadata
            text_content = TextContent(
                delta=False,
                index=None,
                text=msg.content,
            )
            message.add_content(new_content=text_content)
            results.append(attach_timestamp(message, msg.timestamp))
            continue

        current_message = None
        current_type = None

        for block in msg.content:
            if isinstance(block, dict):
                btype = block.get("type", "text")
            else:
                continue

            if btype == "text":
                if current_type != MessageType.MESSAGE:
                    if current_message:
                        results.append(
                            attach_timestamp(
                                current_message.completed(),
                                msg.timestamp,
                            ),
                        )
                    current_message = Message(
                        type=MessageType.MESSAGE,
                        role=role,
                    )
                    current_message.metadata = metadata
                    current_type = MessageType.MESSAGE

                text_content = TextContent(
                    delta=False,
                    index=None,
                    text=block.get("text", ""),
                )
                current_message.add_content(new_content=text_content)

            elif btype == "thinking":
                if current_type != MessageType.REASONING:
                    if current_message:
                        results.append(
                            attach_timestamp(
                                current_message.completed(),
                                msg.timestamp,
                            ),
                        )
                    current_message = Message(
                        type=MessageType.REASONING,
                        role=role,
                    )
                    current_message.metadata = metadata
                    current_type = MessageType.REASONING

                text_content = TextContent(
                    delta=False,
                    index=None,
                    text=block.get("thinking", ""),
                )
                current_message.add_content(new_content=text_content)

            elif btype == "tool_use":
                if current_message:
                    results.append(
                        attach_timestamp(
                            current_message.completed(),
                            msg.timestamp,
                        ),
                    )

                current_message = Message(
                    type=MessageType.PLUGIN_CALL,
                    role=role,
                )
                current_message.metadata = metadata
                current_type = MessageType.PLUGIN_CALL

                if isinstance(block.get("input"), (dict, list)):
                    arguments = json.dumps(
                        block.get("input"),
                        ensure_ascii=False,
                    )
                else:
                    arguments = block.get("input")

                call_data = FunctionCall(
                    call_id=block.get("id"),
                    name=block.get("name"),
                    arguments=arguments,
                ).model_dump()

                tool_name = str(block.get("name") or "")

                # Generate user-friendly summary for tool call
                attach_operation_group(call_data, arguments)
                call_data["summary"] = generate_tool_call_summary(
                    tool_name=tool_name,
                    arguments=call_data["arguments"],
                    server_label=block.get("server_label"),
                )
                apply_running_tool_status(call_data)

                data_content = DataContent(
                    delta=False,
                    index=None,
                    data=call_data,
                )
                current_message.add_content(new_content=data_content)

            elif btype == "tool_result":
                if current_message:
                    results.append(
                        attach_timestamp(
                            current_message.completed(),
                            msg.timestamp,
                        ),
                    )

                current_message = Message(
                    type=MessageType.PLUGIN_CALL_OUTPUT,
                    role=role,
                )
                current_message.metadata = metadata
                current_type = MessageType.PLUGIN_CALL_OUTPUT

                if isinstance(block.get("output"), (dict, list)):
                    output = json.dumps(
                        block.get("output"),
                        ensure_ascii=False,
                    )
                else:
                    output = block.get("output")

                output_data = FunctionCallOutput(
                    call_id=block.get("id"),
                    name=block.get("name"),
                    output=output,
                ).model_dump(exclude_none=True)

                tool_name = str(block.get("name") or "")

                # Generate user-friendly summary for tool output
                output_data["output_summary"] = generate_tool_output_summary(
                    tool_name=tool_name,
                    output=output,
                    governance_status=block.get(
                        TOOL_GOVERNANCE_BLOCK_FIELD,
                    ),
                )
                apply_terminal_tool_status(
                    output_data,
                    raw_output=block.get("output"),
                )
                apply_governance_tool_status(
                    output_data,
                    block.get(TOOL_GOVERNANCE_BLOCK_FIELD),
                )
                operation_group = normalize_operation_group(
                    block.get(OPERATION_GROUP_FIELD),
                )
                if operation_group is not None:
                    output_data[OPERATION_GROUP_FIELD] = operation_group

                data_content = DataContent(
                    delta=False,
                    index=None,
                    data=output_data,
                )
                current_message.add_content(new_content=data_content)

                media_message = _build_media_message_from_block(
                    block,
                    role,
                    metadata,
                )
                if media_message:
                    results.append(
                        attach_timestamp(media_message, msg.timestamp),
                    )

            elif btype == "image":
                if current_type != MessageType.MESSAGE:
                    if current_message:
                        results.append(
                            attach_timestamp(
                                current_message.completed(),
                                msg.timestamp,
                            ),
                        )
                    current_message = Message(
                        type=MessageType.MESSAGE,
                        role=role,
                    )
                    current_message.metadata = metadata
                    current_type = MessageType.MESSAGE

                kwargs = {}
                if (
                    isinstance(block.get("source"), dict)
                    and block.get("source", {}).get("type") == "url"
                ):
                    url = block.get("source", {}).get("url")
                    url = _resolve_content_url(url)
                    kwargs["image_url"] = url

                elif (
                    isinstance(block.get("source"), dict)
                    and block.get("source").get("type") == "base64"
                ):
                    media_type = block.get("source", {}).get(
                        "media_type",
                        "image/jpeg",
                    )
                    base64_data = block.get("source", {}).get("data", "")
                    url = f"data:{media_type};base64,{base64_data}"
                    kwargs["image_url"] = url
                elif isinstance(block.get("image_url"), str):
                    kwargs["image_url"] = _resolve_content_url(
                        block["image_url"],
                    )

                image_content = ImageContent(
                    delta=False,
                    index=None,
                    **kwargs,
                )
                current_message.add_content(new_content=image_content)

            elif btype == "audio":
                if current_type != MessageType.MESSAGE:
                    if current_message:
                        results.append(
                            attach_timestamp(
                                current_message.completed(),
                                msg.timestamp,
                            ),
                        )
                    current_message = Message(
                        type=MessageType.MESSAGE,
                        role=role,
                    )
                    current_message.metadata = metadata
                    current_type = MessageType.MESSAGE

                kwargs = {}
                if (
                    isinstance(block.get("source"), dict)
                    and block.get("source", {}).get("type") == "url"
                ):
                    url = block.get("source", {}).get("url")
                    url = _resolve_content_url(url)
                    kwargs["data"] = url
                    try:
                        kwargs["format"] = urlparse(url).path.split(".")[-1]
                    except (AttributeError, IndexError, ValueError):
                        kwargs["format"] = None

                elif (
                    isinstance(block.get("source"), dict)
                    and block.get("source").get("type") == "base64"
                ):
                    media_type = block.get("source", {}).get("media_type")
                    base64_data = block.get("source", {}).get("data", "")
                    url = f"data:{media_type};base64,{base64_data}"
                    kwargs["data"] = url
                    kwargs["format"] = media_type
                else:
                    url = block.get("audio_url") or block.get("data")
                    if isinstance(url, str):
                        url = _resolve_content_url(url)
                        kwargs["data"] = url
                    kwargs["format"] = block.get("format")

                audio_content = AudioContent(
                    delta=False,
                    index=None,
                    **kwargs,
                )
                current_message.add_content(new_content=audio_content)

            elif btype == "video":
                if current_type != MessageType.MESSAGE:
                    if current_message:
                        results.append(
                            attach_timestamp(
                                current_message.completed(),
                                msg.timestamp,
                            ),
                        )
                    current_message = Message(
                        type=MessageType.MESSAGE,
                        role=role,
                    )
                    current_message.metadata = metadata
                    current_type = MessageType.MESSAGE

                kwargs = {}
                if (
                    isinstance(block.get("source"), dict)
                    and block.get("source", {}).get("type") == "url"
                ):
                    url = block.get("source", {}).get("url")
                    url = _resolve_content_url(url)
                    kwargs["video_url"] = url

                elif (
                    isinstance(block.get("source"), dict)
                    and block.get("source").get("type") == "base64"
                ):
                    media_type = block.get("source", {}).get(
                        "media_type",
                        "video/mp4",
                    )
                    base64_data = block.get("source", {}).get("data", "")
                    url = f"data:{media_type};base64,{base64_data}"
                    kwargs["video_url"] = url
                elif isinstance(block.get("video_url"), str):
                    kwargs["video_url"] = _resolve_content_url(
                        block["video_url"],
                    )

                video_content = VideoContent(
                    delta=False,
                    index=None,
                    **kwargs,
                )
                current_message.add_content(new_content=video_content)

            elif btype == "file":
                if current_type != MessageType.MESSAGE:
                    if current_message:
                        results.append(
                            attach_timestamp(
                                current_message.completed(),
                                msg.timestamp,
                            ),
                        )
                    current_message = Message(
                        type=MessageType.MESSAGE,
                        role=role,
                    )
                    current_message.metadata = metadata
                    current_type = MessageType.MESSAGE

                kwargs = {
                    "filename": block.get("filename")
                    or block.get("file_name"),
                }
                if (
                    isinstance(block.get("source"), dict)
                    and block.get("source", {}).get("type") == "url"
                ):
                    url = block.get("source", {}).get("url")
                    url = _resolve_content_url(url)
                    kwargs["file_url"] = url

                elif (
                    isinstance(block.get("source"), dict)
                    and block.get("source").get("type") == "base64"
                ):
                    media_type = block.get("source", {}).get(
                        "media_type",
                        "application/octet-stream",
                    )
                    base64_data = block.get("source", {}).get("data", "")
                    url = f"data:{media_type};base64,{base64_data}"
                    kwargs["file_url"] = url
                elif isinstance(block.get("source"), str):
                    url = _resolve_content_url(block.get("source", ""))
                    kwargs["file_url"] = url
                elif isinstance(block.get("file_url"), str):
                    kwargs["file_url"] = _resolve_content_url(
                        block["file_url"],
                    )
                elif isinstance(block.get("file_id"), str):
                    kwargs["file_id"] = block["file_id"]

                file_content = FileContent(
                    delta=False,
                    index=None,
                    **kwargs,
                )
                current_message.add_content(new_content=file_content)

            else:
                if current_type != MessageType.MESSAGE:
                    if current_message:
                        results.append(
                            attach_timestamp(
                                current_message.completed(),
                                msg.timestamp,
                            ),
                        )
                    current_message = Message(
                        type=MessageType.MESSAGE,
                        role=role,
                    )
                    current_message.metadata = metadata
                    current_type = MessageType.MESSAGE

                text_content = TextContent(
                    delta=False,
                    index=None,
                    text=str(block),
                )
                current_message.add_content(new_content=text_content)

        if current_message:
            results.append(
                attach_timestamp(current_message.completed(), msg.timestamp),
            )

    return results
