# -*- coding: utf-8 -*-
"""Factory for creating chat models and formatters.

This module provides a unified factory for creating chat model instances
and their corresponding formatters based on configuration.

Example:
    >>> from swe.agents.model_factory import create_model_and_formatter
    >>> model, formatter = create_model_and_formatter()
"""

import base64
import copy
import json
import logging
import os
import time
from typing import Callable, List, Sequence, Tuple, Type, Any, Union, Optional
from urllib.parse import urlparse

from agentscope.formatter import FormatterBase, OpenAIChatFormatter
from agentscope.model import ChatModelBase, OpenAIChatModel

try:
    from agentscope.formatter import AnthropicChatFormatter
    from agentscope.model import AnthropicChatModel
except ImportError:  # pragma: no cover - compatibility fallback
    AnthropicChatFormatter = None
    AnthropicChatModel = None

try:
    from agentscope.formatter import GeminiChatFormatter
    from agentscope.model import GeminiChatModel
except ImportError:  # pragma: no cover - compatibility fallback
    GeminiChatFormatter = None
    GeminiChatModel = None

from .hook_runtime.messages import HOOK_ADDITIONAL_CONTEXT_PREFIX
from .utils.tool_message_utils import _sanitize_tool_messages
from ..constant import (
    DEFAULT_LLM_CHAT_MAX_CONCURRENT,
    DEFAULT_LLM_CRON_MAX_CONCURRENT,
)
from ..providers import ProviderManager
from ..providers.retry_chat_model import (
    RetryChatModel,
    RetryConfig,
    RateLimitConfig,
)
from ..token_usage import TokenRecordingModelWrapper
from ..tracing import TracingModelWrapper, has_trace_manager, get_trace_manager


def _file_url_to_path(url: str) -> str:
    """
    Strip file:// to path. On Windows file:///C:/path -> C:/path not /C:/path.
    """
    s = url.removeprefix("file://")
    # Windows: file:///C:/path yields "/C:/path"; remove leading slash.
    if len(s) >= 3 and s.startswith("/") and s[1].isalpha() and s[2] == ":":
        s = s[1:]
    return s


logger = logging.getLogger(__name__)

_SUPPORTED_IMAGE_EXTENSIONS: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_SUPPORTED_VIDEO_EXTENSIONS: dict[str, str] = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mpeg": "video/mpeg",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
}


# TODO: remove after agentscope anthropic formatter updated
def _format_anthropic_media_block(block: dict) -> dict:
    """Format an image or video block for Anthropic API.

    If the source is a URLSource pointing to a local file it will be
    converted to base64.  Web URLs are passed through as-is.

    Args:
        block (`dict`):
            A block dict with ``type`` of ``"image"`` or ``"video"``.

    Returns:
        `dict`: Formatted block for the Anthropic API.

    Raises:
        `ValueError`:
            If the source type or media format is not supported.
    """
    typ = block["type"]
    extensions = (
        _SUPPORTED_IMAGE_EXTENSIONS
        if typ == "image"
        else _SUPPORTED_VIDEO_EXTENSIONS
    )

    source = block["source"]

    if source["type"] == "base64":
        return {**block}

    url = source["url"]
    raw_url = _file_url_to_path(url)

    if os.path.exists(raw_url) and os.path.isfile(raw_url):
        ext = os.path.splitext(raw_url)[1].lower()
        media_type = extensions.get(ext)
        if media_type:
            with open(raw_url, "rb") as f:
                data = base64.b64encode(f.read()).decode(
                    "utf-8",
                )
            return {
                "type": typ,
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data,
                },
            }

    parsed_url = urlparse(raw_url)
    if parsed_url.scheme not in ("", "file"):
        return {
            "type": typ,
            "source": {
                "type": "url",
                "url": url,
            },
        }

    raise ValueError(
        f'Invalid {typ} URL: "{url}". '
        "It should be a local file or a web URL.",
    )


def _format_openai_video_block(video_block: dict) -> dict:
    """Format a video block for OpenAI-compatible API.

    Local files are converted to base64 data URLs; web URLs are
    passed through directly.

    Args:
        video_block (`dict`):
            The video block to format.

    Returns:
        `dict`:
            ``{"type": "video_url", "video_url": {"url": ...}}``.

    Raises:
        `ValueError`:
            If the source type or video format is not supported.
    """
    source = video_block["source"]
    if source["type"] == "base64":
        media_type = source["media_type"]
        url = f"data:{media_type};base64,{source['data']}"
    elif source["type"] == "url":
        raw_url = source["url"].removeprefix("file://")
        if os.path.exists(raw_url) and os.path.isfile(raw_url):
            ext = os.path.splitext(raw_url)[1].lower()
            media_type = _SUPPORTED_VIDEO_EXTENSIONS.get(ext)
            if not media_type:
                raise ValueError(
                    f"Unsupported video extension: {ext}",
                )
            with open(raw_url, "rb") as f:
                data = base64.b64encode(
                    f.read(),
                ).decode("utf-8")
            url = f"data:{media_type};base64,{data}"
        else:
            parsed = urlparse(raw_url)
            if parsed.scheme not in ("", "file"):
                url = source["url"]
            else:
                raise ValueError(
                    f"Invalid video URL: "
                    f'"{source["url"]}". '
                    "It should be a local file "
                    "or a web URL.",
                )
    else:
        raise ValueError(
            "Unsupported video source type: " f"{source['type']}",
        )

    return {
        "type": "video_url",
        "video_url": {"url": url},
    }


def _replace_video_placeholders(
    messages: list[dict],
    video_subs: dict[str, dict],
) -> None:
    """Replace video placeholder text blocks with formatted
    video blocks in OpenAI-formatted messages."""
    for fmt_msg in messages:
        content = fmt_msg.get("content")
        if not isinstance(content, list):
            continue
        new_content = []
        for item in content:
            if (
                isinstance(item, dict)
                and item.get("type") == "text"
                and item.get("text") in video_subs
            ):
                new_content.append(
                    _format_openai_video_block(
                        video_subs[item["text"]],
                    ),
                )
            else:
                new_content.append(item)
        fmt_msg["content"] = new_content


def _format_anthropic_output_items(output: list) -> list:
    """Format a list of tool_result output blocks for Anthropic API,
    converting image and video blocks as needed."""
    return [
        (
            _format_anthropic_media_block(item)
            if item.get("type") in ("image", "video")
            else item
        )
        for item in output
    ]


def _append_to_initial_anthropic_system(
    messages: list[dict],
    content_blocks: list[dict],
) -> None:
    """把后续 system 上下文合并到 Anthropic 唯一允许的首条 system 消息。"""
    if not content_blocks:
        return
    if messages and messages[0].get("role") == "system":
        messages[0].setdefault("content", []).extend(content_blocks)
        return
    messages.insert(
        0,
        {
            "role": "system",
            "content": [*content_blocks],
        },
    )


# TODO: remove after agentscope anthropic formatter updated
def _format_anthropic_messages(  # pylint: disable=too-many-branches
    msgs: list,
) -> list[dict]:
    """Format messages for Anthropic API with image/video block support.

    This replaces the default ``AnthropicChatFormatter._format`` so that
    ``_format_anthropic_media_block`` is applied to both top-level media
    blocks and media blocks nested inside ``tool_result`` outputs.
    """
    messages: list[dict] = []
    for index, msg in enumerate(msgs):
        content_blocks: list[dict] = []

        for block in msg.get_content_blocks():
            typ = block.get("type")
            if typ in ["thinking", "text"]:
                content_blocks.append({**block})

            elif typ in ("image", "video"):
                content_blocks.append(
                    _format_anthropic_media_block(block),
                )

            elif typ == "tool_use":
                content_blocks.append(
                    {
                        "id": block.get("id"),
                        "type": "tool_use",
                        "name": block.get("name"),
                        "input": block.get("input", {}),
                    },
                )

            elif typ == "tool_result":
                output = block.get("output")
                if output is None:
                    content_value: list = [
                        {"type": "text", "text": None},
                    ]
                elif isinstance(output, list):
                    content_value = _format_anthropic_output_items(output)
                else:
                    content_value = [
                        {"type": "text", "text": str(output)},
                    ]
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": block.get("id"),
                                "content": content_value,
                            },
                        ],
                    },
                )

        if msg.role == "system" and index != 0:
            if _is_persisted_hook_follow_up_message(
                {
                    "role": msg.role,
                    "content": content_blocks,
                },
            ):
                _append_to_initial_anthropic_system(
                    messages,
                    content_blocks,
                )
                continue
            role = "user"
        else:
            role = msg.role

        msg_anthropic: dict = {
            "role": role,
            "content": content_blocks or None,
        }

        if msg_anthropic["content"] or msg_anthropic.get(
            "tool_calls",
        ):
            messages.append(msg_anthropic)

    return messages


# Mapping from chat model class to formatter class
_CHAT_MODEL_FORMATTER_MAP: dict[Type[ChatModelBase], Type[FormatterBase]] = {
    OpenAIChatModel: OpenAIChatFormatter,
}
if AnthropicChatModel is not None and AnthropicChatFormatter is not None:
    _CHAT_MODEL_FORMATTER_MAP[AnthropicChatModel] = AnthropicChatFormatter
if GeminiChatModel is not None and GeminiChatFormatter is not None:
    _CHAT_MODEL_FORMATTER_MAP[GeminiChatModel] = GeminiChatFormatter


def _get_formatter_for_chat_model(
    chat_model_class: Type[ChatModelBase],
) -> Type[FormatterBase]:
    """Get the appropriate formatter class for a chat model.

    Args:
        chat_model_class: The chat model class

    Returns:
        Corresponding formatter class, defaults to OpenAIChatFormatter
    """
    return _CHAT_MODEL_FORMATTER_MAP.get(
        chat_model_class,
        OpenAIChatFormatter,
    )


def _substitute_video_blocks(
    msgs: list,
) -> dict[str, dict]:
    """Replace video blocks in msgs with text placeholders.

    Returns a mapping from placeholder text to the original video
    block so they can be restored later.
    """
    video_subs: dict[str, dict] = {}
    for msg in msgs:
        if not isinstance(msg.content, list):
            continue
        for i, blk in enumerate(msg.content):
            if isinstance(blk, dict) and blk.get("type") == "video":
                ph = f"__SWE_VID_{id(blk)}__"
                video_subs[ph] = blk
                msg.content[i] = {
                    "type": "text",
                    "text": ph,
                }
    return video_subs


def _restore_video_blocks(
    msgs: list,
    video_subs: dict[str, dict],
) -> None:
    """Restore original video blocks in msgs after formatting."""
    for msg in msgs:
        if not isinstance(msg.content, list):
            continue
        for i, blk in enumerate(msg.content):
            if (
                isinstance(blk, dict)
                and blk.get("type") == "text"
                and blk.get("text") in video_subs
            ):
                msg.content[i] = video_subs[blk["text"]]


def _promote_tool_result_videos(
    msgs: list,
    messages: list[dict],
) -> list[dict]:
    """Inject promoted video user messages after tool result messages.

    Mirrors the image promotion that agentscope's formatter does
    for ``promote_tool_result_images``, but for video blocks.
    """
    promotions: dict[str, tuple[str, list]] = {}
    for msg in msgs:
        for block in msg.get_content_blocks():
            if block.get("type") != "tool_result":
                continue
            output = block.get("output")
            if not isinstance(output, list):
                continue
            videos = [
                (
                    item.get("source", {}).get("url", ""),
                    item,
                )
                for item in output
                if isinstance(item, dict) and item.get("type") == "video"
            ]
            if videos:
                promotions[block.get("id")] = (
                    block.get("name", ""),
                    videos,
                )

    if not promotions:
        return messages

    new_messages: list[dict] = []
    for fmt_msg in messages:
        new_messages.append(fmt_msg)
        tcid = fmt_msg.get("tool_call_id")
        if tcid not in promotions:
            continue
        tool_name, videos = promotions[tcid]
        promoted: list[dict] = [
            {
                "type": "text",
                "text": "<system-info>The following are "
                "the video contents from the tool "
                f"result of '{tool_name}':",
            },
        ]
        for url, vid_block in videos:
            promoted.append(
                {
                    "type": "text",
                    "text": f"\n- The video from '{url}': ",
                },
            )
            promoted.append(
                _format_openai_video_block(vid_block),
            )
        promoted.append(
            {"type": "text", "text": "</system-info>"},
        )
        new_messages.append(
            {"role": "user", "content": promoted},
        )
    return new_messages


# pylint: disable-next=too-many-statements
def _create_file_block_support_formatter(
    base_formatter_class: Type[FormatterBase],
) -> Type[FormatterBase]:
    """Create a formatter class with file block support.

    This factory function extends any Formatter class to support file blocks
    in tool results, which are not natively supported by AgentScope.

    Args:
        base_formatter_class: Base formatter class to extend

    Returns:
        Enhanced formatter class with file block support
    """

    class FileBlockSupportFormatter(base_formatter_class):
        """Formatter with file block support for tool results."""

        # pylint: disable=too-many-branches
        async def _format(self, msgs):
            """Override to sanitize tool messages, handle thinking blocks,
            and relay ``extra_content`` (Gemini thought_signature).

            This prevents OpenAI API errors from improperly paired
            tool messages, preserves reasoning_content from "thinking"
            blocks that the base formatter skips, and ensures
            ``extra_content`` on tool_use blocks (e.g. Gemini
            thought_signature) is carried through to the API request.
            """
            msgs = _sanitize_tool_messages(msgs)

            reasoning_contents = {}
            extra_contents: dict[str, Any] = {}
            for msg in msgs:
                if msg.role != "assistant":
                    continue
                for block in msg.get_content_blocks():
                    if block.get("type") == "thinking":
                        thinking = block.get("thinking", "")
                        if thinking:
                            reasoning_contents[id(msg)] = thinking
                        break
                for block in msg.get_content_blocks():
                    if (
                        block.get("type") == "tool_use"
                        and "extra_content" in block
                    ):
                        extra_contents[block["id"]] = block["extra_content"]

            # Convert file:// URLs to paths for all media blocks,
            # TODO: remove this after AgentScope updated
            for msg in msgs:
                for block in msg.get_content_blocks():
                    if block.get("type") in ("image", "audio", "video"):
                        source = block.get("source")
                        if (
                            isinstance(source, dict)
                            and source.get("type") == "url"
                            and isinstance(source.get("url"), str)
                            and source["url"].startswith("file://")
                        ):
                            source["url"] = _file_url_to_path(source["url"])

            # For Anthropic, fully override formatting to handle
            # media blocks (top-level & inside tool_result output).
            # TODO: remove after agentscope anthropic formatter updated
            if AnthropicChatFormatter is not None and issubclass(
                base_formatter_class,
                AnthropicChatFormatter,
            ):
                messages = _format_anthropic_messages(msgs)
            else:
                # Gemini handles video natively; for others
                # (OpenAI) we inject it via placeholders.
                _needs_video = not (
                    GeminiChatFormatter is not None
                    and issubclass(
                        base_formatter_class,
                        GeminiChatFormatter,
                    )
                )
                video_subs: dict[str, dict] = {}
                if _needs_video:
                    video_subs = _substitute_video_blocks(
                        msgs,
                    )

                messages = await super()._format(msgs)

                if video_subs:
                    _replace_video_placeholders(
                        messages,
                        video_subs,
                    )
                    _restore_video_blocks(msgs, video_subs)

                if _needs_video and getattr(
                    self,
                    "promote_tool_result_images",
                    False,
                ):
                    messages = _promote_tool_result_videos(
                        msgs,
                        messages,
                    )

            if extra_contents:
                for message in messages:
                    for tc in message.get("tool_calls", []):
                        ec = extra_contents.get(tc.get("id"))
                        if ec:
                            tc["extra_content"] = ec

            if reasoning_contents:
                # Build a list of reasoning values aligned with surviving
                # assistant messages.  The parent formatter drops
                # thinking-only messages (no content/tool_calls), so we
                # predict survivors and collect reasoning only for those.
                aligned_reasoning = []
                for m in (msg for msg in msgs if msg.role == "assistant"):
                    is_thinking_only = (
                        isinstance(m.content, list)
                        and m.content
                        and all(b.get("type") == "thinking" for b in m.content)
                    )
                    if not is_thinking_only:
                        aligned_reasoning.append(
                            reasoning_contents.get(id(m)),
                        )

                out_assistant = [
                    m for m in messages if m.get("role") == "assistant"
                ]

                if len(aligned_reasoning) != len(out_assistant):
                    logger.warning(
                        "Assistant message count mismatch after formatting "
                        "(%d expected survivors, %d actual). "
                        "Skipping reasoning_content injection.",
                        len(aligned_reasoning),
                        len(out_assistant),
                    )
                else:
                    for i, out_msg in enumerate(out_assistant):
                        if aligned_reasoning[i]:
                            out_msg["reasoning_content"] = aligned_reasoning[i]

            return _strip_top_level_message_name(messages)

        @staticmethod
        def _extract_text_from_tool_result_dict(
            output: dict,
        ) -> str | None:
            content = output.get("content")
            if not isinstance(content, list):
                return None

            textual_output = [
                block["text"]
                for block in content
                if (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                )
            ]
            if not textual_output:
                return None
            return "\n".join(textual_output)

        @staticmethod
        def _convert_non_file_tool_result_block(
            block: dict,
        ) -> tuple[str, Sequence[Tuple[str, dict]]]:
            return base_formatter_class.convert_tool_result_to_string(
                [block],
            )

        @staticmethod
        def _convert_file_block_tool_result(
            block: dict,
        ) -> tuple[str, tuple[str, dict]]:
            file_path = block.get("path", "") or block.get("url", "")
            file_name = block.get("name", file_path)
            text = (
                f"The returned file '{file_name}' "
                f"can be found at: {file_path}"
            )
            return text, (file_path, block)

        @staticmethod
        def _join_tool_result_text(
            textual_output: list[str],
        ) -> str:
            if not textual_output:
                return ""
            if len(textual_output) == 1:
                return textual_output[0]
            return "\n".join(f"- {item}" for item in textual_output)

        @staticmethod
        def _convert_tool_result_with_file_blocks(
            output: List[dict],
            error: ValueError,
        ) -> tuple[str, Sequence[Tuple[str, dict]]]:
            textual_output: list[str] = []
            multimodal_data: list[Tuple[str, dict]] = []

            for block in output:
                if not isinstance(block, dict) or "type" not in block:
                    raise ValueError(
                        f"Invalid block: {block}, "
                        "expected a dict with 'type' key",
                    ) from error

                if block["type"] == "file":
                    text, file_data = (
                        FileBlockSupportFormatter._convert_file_block_tool_result(
                            block,
                        )
                    )
                    textual_output.append(text)
                    multimodal_data.append(file_data)
                    continue

                text, data = (
                    FileBlockSupportFormatter._convert_non_file_tool_result_block(
                        block,
                    )
                )
                textual_output.append(text)
                multimodal_data.extend(data)

            return (
                FileBlockSupportFormatter._join_tool_result_text(
                    textual_output,
                ),
                multimodal_data,
            )

        @staticmethod
        def convert_tool_result_to_string(
            output: Union[str, List[dict], dict],
        ) -> tuple[str, Sequence[Tuple[str, dict]]]:
            """Extend parent class to support file blocks.

            Uses try-first strategy for compatibility with parent class.

            Args:
                output: Tool result output (string or list of blocks)

            Returns:
                Tuple of (text_representation, multimodal_data)
            """
            if isinstance(output, str):
                return output, []

            if isinstance(output, dict):
                text = FileBlockSupportFormatter._extract_text_from_tool_result_dict(
                    output,
                )
                if text is not None:
                    return text, []
                return json.dumps(output, ensure_ascii=False), []

            try:
                return base_formatter_class.convert_tool_result_to_string(
                    output,
                )
            except ValueError as error:
                if "Unsupported block type: file" not in str(error):
                    raise
                return FileBlockSupportFormatter._convert_tool_result_with_file_blocks(
                    output,
                    error,
                )

    FileBlockSupportFormatter.__name__ = (
        f"FileBlockSupport{base_formatter_class.__name__}"
    )
    return FileBlockSupportFormatter


def _strip_top_level_message_name(
    messages: list[dict],
) -> list[dict]:
    """清理 OpenAI-compatible 后端容易拒绝的消息字段。"""
    for index, message in enumerate(messages):
        message.pop("name", None)
        if (
            index != 0
            and message.get("role") == "system"
            and not _is_persisted_hook_follow_up_message(message)
        ):
            message["role"] = "user"
    return messages


def _is_persisted_hook_follow_up_message(message: dict) -> bool:
    """判断消息是否为需要跨 turn 保留 system 语义的 hook 上下文。"""
    content = message.get("content")
    if isinstance(content, str):
        return content.startswith(HOOK_ADDITIONAL_CONTEXT_PREFIX)
    if not isinstance(content, list):
        return False

    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            and block["text"].startswith(HOOK_ADDITIONAL_CONTEXT_PREFIX)
        ):
            return True
    return False


def _get_agent_id(
    agent_id: Optional[str],
    tenant_id: Optional[str] = None,
) -> Optional[str]:
    """Resolve agent_id from parameter or context."""
    if agent_id is not None:
        return agent_id
    try:
        from ..app.agent_context import get_current_agent_id

        return get_current_agent_id(tenant_id)
    except Exception:
        return None


def _get_tenant_id() -> Optional[str]:
    """Get current tenant ID."""
    try:
        from ..config.context import get_current_effective_tenant_id

        return get_current_effective_tenant_id()
    except Exception:
        return None


def _get_model_slot(
    manager: "ProviderManager",
):
    """Get active model slot from provider manager."""
    from ..tenant_models.models import ModelSlot
    from ..app.crons.model_slot_context import (
        get_current_model_slot_override,
    )

    override = get_current_model_slot_override()
    if override and override.provider_id and override.model:
        return ModelSlot(
            provider_id=override.provider_id,
            model=override.model,
        )

    active_model = manager.get_active_model()
    if (
        not active_model
        or not active_model.provider_id
        or not active_model.model
    ):
        return None
    return ModelSlot(
        provider_id=active_model.provider_id,
        model=active_model.model,
    )


def _get_retry_config(
    agent_id: Optional[str],
    tenant_id: Optional[str] = None,
) -> Optional[RetryConfig]:
    """Load retry config for agent if available."""
    if not agent_id:
        return None
    try:
        from ..config.config import load_agent_config

        if tenant_id:
            agent_config = load_agent_config(agent_id, tenant_id=tenant_id)
        else:
            agent_config = load_agent_config(agent_id)
        return RetryConfig(
            enabled=agent_config.running.llm_retry_enabled,
            max_retries=agent_config.running.llm_max_retries,
            backoff_base=agent_config.running.llm_backoff_base,
            backoff_cap=agent_config.running.llm_backoff_cap,
        )
    except Exception:
        return None


def _optional_int_config_value(
    obj: Any,
    name: str,
    default: int | None = None,
) -> int | None:
    value = getattr(obj, name, None)
    if isinstance(value, bool):
        return default
    if value is None:
        return default
    return value if isinstance(value, int) else default


def _optional_float_config_value(obj: Any, name: str) -> float | None:
    value = getattr(obj, name, None)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _build_rate_limit_config(running: Any) -> RateLimitConfig:
    return RateLimitConfig(
        max_concurrent=running.llm_max_concurrent,
        chat_max_concurrent=_optional_int_config_value(
            running,
            "llm_chat_max_concurrent",
            DEFAULT_LLM_CHAT_MAX_CONCURRENT,
        ),
        cron_max_concurrent=_optional_int_config_value(
            running,
            "llm_cron_max_concurrent",
            DEFAULT_LLM_CRON_MAX_CONCURRENT,
        ),
        max_qpm=running.llm_max_qpm,
        pause_seconds=running.llm_rate_limit_pause,
        jitter_range=running.llm_rate_limit_jitter,
        acquire_timeout=running.llm_acquire_timeout,
        chat_acquire_timeout=_optional_float_config_value(
            running,
            "llm_chat_acquire_timeout",
        ),
        cron_acquire_timeout=_optional_float_config_value(
            running,
            "llm_cron_acquire_timeout",
        ),
    )


def _get_rate_limit_config(
    agent_id: Optional[str],
    tenant_id: Optional[str] = None,
) -> Optional[RateLimitConfig]:
    """Load rate limit config for agent if available."""
    if not agent_id:
        return None
    try:
        from ..config.config import load_agent_config

        if tenant_id:
            agent_config = load_agent_config(agent_id, tenant_id=tenant_id)
        else:
            agent_config = load_agent_config(agent_id)
        rate_limit_config = _build_rate_limit_config(agent_config.running)
        from ..app.source_system_config import (
            resolve_llm_rate_limiter_config,
        )

        return resolve_llm_rate_limiter_config(rate_limit_config)
    except Exception:
        return None


def _get_model_runtime_configs(
    agent_id: Optional[str],
    tenant_id: Optional[str] = None,
) -> tuple[Optional[RetryConfig], Optional[RateLimitConfig]]:
    """Load retry and rate-limit config with one tenant-local config read."""
    if not agent_id:
        return None, None
    try:
        from ..config.config import load_agent_config

        if tenant_id:
            agent_config = load_agent_config(agent_id, tenant_id=tenant_id)
        else:
            agent_config = load_agent_config(agent_id)
        rate_limit_config = _build_rate_limit_config(agent_config.running)
        from ..app.source_system_config import (
            resolve_llm_rate_limiter_config,
        )

        return (
            RetryConfig(
                enabled=agent_config.running.llm_retry_enabled,
                max_retries=agent_config.running.llm_max_retries,
                backoff_base=agent_config.running.llm_backoff_base,
                backoff_cap=agent_config.running.llm_backoff_cap,
            ),
            resolve_llm_rate_limiter_config(rate_limit_config),
        )
    except Exception:
        return None, None


def _wrap_model_with_tracing(
    provider_id: str,
    model: ChatModelBase,
    trace_context: Optional[dict[str, Any]] = None,
) -> ChatModelBase:
    """Wrap model with tracing and token recording.

    Always applies TokenRecordingModelWrapper for token usage tracking.
    When tracing is enabled, wraps with TracingModelWrapper on top.
    """
    # Always wrap with token recording first
    wrapped = TokenRecordingModelWrapper(provider_id, model)

    # Then optionally wrap with tracing
    if has_trace_manager():
        try:
            trace_mgr = get_trace_manager()
            if trace_mgr.enabled:
                return TracingModelWrapper(
                    provider_id,
                    wrapped,
                    trace_context=trace_context,
                )
        except RuntimeError:
            pass
    return wrapped


def create_model_and_formatter(
    agent_id: Optional[str] = None,
    trace_context: Optional[dict[str, Any]] = None,
    model_slot_override: Any | None = None,
    model_provider_override: Any | None = None,
    fallback_model_slot: Any | None = None,
    fallback_model_provider: Any | None = None,
    resolved_model_info: dict[str, str] | None = None,
    on_model_config_resolved: Callable[[Any], None] | None = None,
    on_model_provider_resolved: Callable[[Any], None] | None = None,
) -> Tuple[ChatModelBase, FormatterBase]:
    """Factory method to create model and formatter instances.

    This method handles both local and remote models, selecting the
    appropriate chat model class and formatter based on configuration.

    Args:
        agent_id: Optional agent ID to load agent-specific model config.
            If None, tries to get from context, then falls back to global.

    Returns:
        Tuple of (model_instance, formatter_instance)

    Example:
        >>> model, formatter = create_model_and_formatter()
    """
    started_at = time.perf_counter()

    # Resolve tenant and tenant-local agent identity.
    tenant_id = _get_tenant_id()
    resolved_agent_id = _get_agent_id(agent_id, tenant_id)

    try:
        # Try to get model from tenant-aware ProviderManager
        # This is the primary and only supported path for active model resolution
        model_slot = None
        retry_config, rate_limit_config = _get_model_runtime_configs(
            resolved_agent_id,
            tenant_id,
        )

        # Snapshot workers supply both a frozen slot and provider. Avoid the
        # tenant manager so later provider changes cannot affect that run.
        manager = None
        if (
            model_slot_override is not None
            and model_provider_override is not None
        ):
            model_slot = model_slot_override
        else:
            ProviderManager.ensure_tenant_provider_storage(tenant_id)
            manager = ProviderManager.get_instance(tenant_id)
            model_slot = model_slot_override or _get_model_slot(manager)
        if (
            not model_slot
            or not model_slot.provider_id
            or not model_slot.model
        ):
            raise ValueError(
                "No tenant model configuration found. "
                "Please configure a model for this tenant using the admin panel "
                "or ensure provider configuration is properly set. "
                "Multi-tenant isolation requires explicit model config.",
            )

        # Get provider and create model instance
        provider = model_provider_override
        if provider is None and manager is not None:
            provider = manager.get_provider(model_slot.provider_id)
        if provider is None:
            raise ValueError(f"Provider '{model_slot.provider_id}' not found.")

        try:
            model_config = provider.get_model_config(model_slot.model)
            model = provider.get_chat_model_instance(
                model_slot.model,
                generation_kwargs=provider.build_generation_kwargs(
                    model_config,
                ),
            )
            provider_id = model_slot.provider_id
            resolved_slot = model_slot
            resolved_provider = provider
        except Exception:
            if (
                fallback_model_slot is None
                or fallback_model_provider is None
                or not fallback_model_slot.provider_id
                or not fallback_model_slot.model
            ):
                raise
            model_config = fallback_model_provider.get_model_config(
                fallback_model_slot.model,
            )
            model = fallback_model_provider.get_chat_model_instance(
                fallback_model_slot.model,
                generation_kwargs=(
                    fallback_model_provider.build_generation_kwargs(
                        model_config,
                    )
                ),
            )
            provider_id = fallback_model_slot.provider_id
            resolved_slot = fallback_model_slot
            resolved_provider = fallback_model_provider

        if on_model_config_resolved is not None:
            on_model_config_resolved(model_config)
        if on_model_provider_resolved is not None:
            model_copy = getattr(resolved_provider, "model_copy", None)
            snapshot = (
                model_copy(deep=True)
                if callable(model_copy)
                else copy.deepcopy(resolved_provider)
            )
            on_model_provider_resolved(snapshot)

        if resolved_model_info is not None:
            resolved_model_info.clear()
            resolved_model_info.update(
                {
                    "provider_id": provider_id,
                    "model": resolved_slot.model,
                },
            )

        # Create the formatter based on the real model class
        formatter = _create_formatter_instance(model.__class__)

        # Wrap with tracing and token recording
        wrapped_model = _wrap_model_with_tracing(
            provider_id,
            model,
            trace_context=trace_context,
        )

        # Wrap with retry logic for transient LLM API errors
        wrapped_model = RetryChatModel(
            wrapped_model,
            retry_config=retry_config,
            rate_limit_config=rate_limit_config,
            tenant_id=tenant_id,
            agent_id=resolved_agent_id,
            on_retry=None,  # 模型层重试回调，后续可通过上下文变量传递事件
        )

        return wrapped_model, formatter
    finally:
        logger.debug(
            "create_model_and_formatter_duration_ms=%d tenant_id=%s "
            "agent_id=%s",
            int((time.perf_counter() - started_at) * 1000),
            tenant_id,
            resolved_agent_id,
        )


def _create_formatter_instance(
    chat_model_class: Type[ChatModelBase],
) -> FormatterBase:
    """Create a formatter instance for the given chat model class.

    The formatter is enhanced with file block support for handling
    file outputs in tool results.

    Args:
        chat_model_class: The chat model class

    Returns:
        Formatter instance with file block support
    """
    base_formatter_class = _get_formatter_for_chat_model(chat_model_class)
    formatter_class = _create_file_block_support_formatter(
        base_formatter_class,
    )
    kwargs: dict[str, Any] = {}
    if issubclass(
        base_formatter_class,
        (OpenAIChatFormatter, GeminiChatFormatter),
    ):
        kwargs["promote_tool_result_images"] = True
    return formatter_class(**kwargs)


__all__ = [
    "create_model_and_formatter",
]
