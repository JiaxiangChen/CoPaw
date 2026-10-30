# -*- coding: utf-8 -*-
"""Console APIs: push messages, chat, and file upload for chat."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    Coroutine,
    Dict,
    Literal,
    Optional,
    Union,
)
from urllib.parse import quote, unquote, urlparse

import anyio
from agentscope_runtime.engine.schemas.agent_schemas import AgentRequest
from fastapi import (
    APIRouter,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from pydantic import BaseModel, Field
from starlette.responses import Response, StreamingResponse

from ...config.context import resolve_request_effective_tenant_id
from ..b3_headers import (
    B3_CONTEXT_META_KEY,
    B3_TRACE_ID_HEADER,
    extract_b3_context,
)
from ..agent_context import (
    get_agent_and_config_for_request,
    get_agent_for_request,
    resolve_file_manager_source_scope_location,
    resolve_file_manager_workspace_dir,
)
from ..context_references import (
    ContextReferencesResponse,
    context_reference_directory,
)
from ..document_annotations import (
    DocumentAnnotationValidationError,
    build_document_annotation_directive,
    validate_document_annotation_request,
)
from ..file_manager import (
    FileManagerConflictError,
    FileManagerDirectoryListing,
    FileManagerItem,
    FileManagerNotFoundError,
    FileManagerPathError,
    FileManagerService,
    FileManagerTextPreview,
    FileManagerUploadTooLargeError,
    get_file_manager_service,
)
from ..file_manager_execution import (
    run_file_manager_mutation,
    run_file_manager_read,
)
from ..runner.context_references import MAX_CONTEXT_REFERENCES
from ..answer_turn.coordinator import TurnSettlementPendingError
from ..answer_turn.models import TurnIdentity, TurnStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/console", tags=["console"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
DENIED_CHAT_ATTACHMENT_EXECUTABLE_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyw",
        ".java",
        ".class",
        ".jar",
        ".js",
        ".mjs",
        ".cjs",
        ".jsx",
        ".ts",
        ".tsx",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".ps1",
        ".bat",
        ".cmd",
        ".php",
        ".rb",
        ".pl",
        ".lua",
        ".go",
        ".rs",
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hpp",
        ".cs",
        ".kt",
        ".kts",
        ".swift",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
    },
)
_RECONNECT_ATTACH_ATTEMPTS = 10
_RECONNECT_ATTACH_RETRY_DELAY_SECONDS = 0.1
_CONSOLE_SSE_HEARTBEAT_SECONDS = 15
_CHAT_FILE_LIST_LIMIT = 500
_TEXT_SNIFF_BYTES = 4096
_TEXT_PREVIEW_MIME_PREFIX = "text/"
_TEXT_PREVIEW_MIME_TYPES = {
    "application/json",
    "application/xml",
    "application/x-yaml",
    "application/toml",
}
PreviewType = Literal[
    "image",
    "video",
    "audio",
    "office",
    "pdf",
    "markdown",
    "text",
    "html",
    "other",
]
_PREVIEW_TYPES: tuple[PreviewType, ...] = (
    "image",
    "video",
    "audio",
    "office",
    "pdf",
    "markdown",
    "text",
    "html",
    "other",
)
_PREVIEW_TYPE_BY_EXTENSION: dict[str, PreviewType] = {
    **dict.fromkeys(
        ("png", "jpg", "jpeg", "gif", "bmp", "webp", "svg"),
        "image",
    ),
    **dict.fromkeys(
        ("mp4", "avi", "mov", "wmv", "flv", "mkv", "webm"),
        "video",
    ),
    **dict.fromkeys(
        ("mp3", "wav", "flac", "ape", "aac", "ogg", "m4a"),
        "audio",
    ),
    **dict.fromkeys(("doc", "docx", "xls", "xlsx", "ppt", "pptx"), "office"),
    "pdf": "pdf",
    "md": "markdown",
    "mdx": "markdown",
    "html": "html",
    "htm": "html",
    "xhtml": "html",
    **dict.fromkeys(
        (
            "txt",
            "json",
            "xml",
            "csv",
            "log",
            "yaml",
            "yml",
            "toml",
            "ini",
            "conf",
            "config",
            "env",
            "sh",
            "bash",
            "zsh",
            "ps1",
            "bat",
            "cmd",
        ),
        "text",
    ),
}
_PREVIEW_TYPE_BY_MIME: dict[str, PreviewType] = {
    "application/pdf": "pdf",
    "text/html": "html",
    "application/xhtml+xml": "html",
}


def _request_runtime_tenant_id(request: Request) -> str | None:
    """优先返回请求已解析的 runtime scope，避免回退到逻辑 tenant。"""
    return resolve_request_effective_tenant_id(
        getattr(request.state, "tenant_id", None),
        getattr(request.state, "source_id", None),
        getattr(request.state, "scope_id", None),
    )


_PREVIEW_MIME_PREFIXES: tuple[tuple[str, PreviewType], ...] = (
    ("image/", "image"),
    ("video/", "video"),
    ("audio/", "audio"),
)
_UPLOADED_STORED_NAME_PATTERN = re.compile(r"^[0-9a-f]{32}_(?P<name>.+)$")


class GeneratedFileItem(BaseModel):
    """聊天相关文件列表项。"""

    name: str = Field(..., description="文件名")
    display_name: str = Field(..., description="用于界面展示的文件名")
    relative_path: str = Field(..., description="相对来源目录的路径")
    file_url: str = Field(..., description="文件绝对路径")
    size: int = Field(..., description="文件大小，单位字节")
    modified_at: str = Field(..., description="最后修改时间")
    mime_type: str | None = Field(default=None, description="文件 MIME 类型")
    preview_type: Literal[
        "image",
        "video",
        "audio",
        "office",
        "pdf",
        "markdown",
        "text",
        "html",
        "other",
    ] = Field(default="other", description="前端预览类型")
    source: Literal["generated", "uploaded"] = Field(
        ...,
        description="文件来源：generated 表示生成文件，uploaded 表示上传文件",
    )


class GeneratedFilesResponse(BaseModel):
    """聊天相关文件列表响应。"""

    files: list[GeneratedFileItem] = Field(default_factory=list)


def _file_manager_http_error(error: FileManagerPathError) -> HTTPException:
    """Map controlled filesystem errors without revealing host paths."""

    if isinstance(error, FileManagerNotFoundError):
        return HTTPException(
            status_code=404,
            detail="File manager item not found",
        )
    if isinstance(error, FileManagerConflictError):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=403, detail=str(error))


class FileManagerTextSaveRequest(BaseModel):
    root: str
    path: str
    content: str
    revision: str


def _file_manager_actor(request: Request) -> str:
    """Return a bounded audit actor without depending on one auth scheme."""

    for candidate in (
        request.headers.get("X-Actor"),
        request.headers.get("X-User-Id"),
        getattr(request.state, "actor", None),
        getattr(request.state, "user_id", None),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:256]
    return "unknown"


def _audit_file_manager_mutation(
    request: Request,
    *,
    action: str,
    path: str,
    outcome: str,
) -> None:
    """Best-effort mutation audit; never include or log file contents."""

    try:
        logger.info(
            "file_manager.audit",
            extra={
                "actor": _file_manager_actor(request),
                "time": datetime.now(timezone.utc).isoformat(),
                "action": action,
                "path": path,
                "outcome": outcome,
            },
        )
    except Exception:
        # Audit delivery must not turn a successful filesystem mutation into a
        # request failure.
        pass


def _file_manager_upload_audit_path(directory: str, filename: str) -> str:
    """Keep early upload-rejection audit paths bounded and path-shaped."""

    safe_filename = _safe_filename(filename or "file")
    safe_parts = [
        part
        for part in directory.split("/")
        if part not in {"", ".", ".."}
        and "\\" not in part
        and "\x00" not in part
    ]
    return "/".join([*safe_parts, safe_filename])


def _file_manager_download_disposition(filename: str) -> str:
    """Create a header-safe attachment filename without leaking a path."""

    if (
        filename.isascii()
        and all(32 <= ord(character) <= 126 for character in filename)
        and '"' not in filename
        and "\\" not in filename
    ):
        return f'attachment; filename="{filename}"'
    return f"attachment; filename*=UTF-8''{quote(filename, safe='')}"


class _FileManagerDownloadStream:
    """A bounded, idempotently-closeable streaming download descriptor."""

    def __init__(self, file_descriptor: int, size_bytes: int) -> None:
        self._file_descriptor: int | None = file_descriptor
        self._remaining = size_bytes

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        if self._file_descriptor is None or self._remaining <= 0:
            self.close()
            raise StopIteration
        try:
            chunk = os.read(
                self._file_descriptor,
                min(64 * 1024, self._remaining),
            )
        except OSError:
            self.close()
            raise
        if not chunk:
            self.close()
            raise StopIteration
        self._remaining -= len(chunk)
        if self._remaining == 0:
            self.close()
        return chunk

    def close(self) -> None:
        """Release the descriptor even when a response never starts streaming."""

        file_descriptor, self._file_descriptor = self._file_descriptor, None
        if file_descriptor is None:
            return
        try:
            os.close(file_descriptor)
        except OSError:
            # A cancelled stream or a completed iterator may already close it.
            pass


class _FileManagerDownloadResponse(StreamingResponse):
    """A download response that closes its descriptor on every exit path."""

    def __init__(
        self,
        stream: _FileManagerDownloadStream,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._file_manager_stream = stream
        super().__init__(
            stream,
            media_type="application/octet-stream",
            headers=headers,
        )

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._file_manager_stream.close()


def _looks_like_text_file(path: Path) -> bool:
    """在缺少后缀时通过内容嗅探判断是否可按文本预览。"""
    try:
        sample = path.read_bytes()[:_TEXT_SNIFF_BYTES]
    except OSError:
        return False
    if not sample:
        return True
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _resolve_display_name(
    file_name: str,
    source: Literal["generated", "uploaded"],
) -> str:
    """上传文件展示原始文件名，生成文件展示实际文件名。"""
    if source != "uploaded":
        return file_name
    match = _UPLOADED_STORED_NAME_PATTERN.match(file_name)
    if not match:
        return file_name
    return match.group("name") or file_name


def _resolve_preview_type_from_mime(
    mime_type: str | None,
) -> PreviewType | None:
    """在后缀无法判断时，根据 MIME 推断前端预览类型。"""
    if not mime_type:
        return None

    for prefix, preview_type in _PREVIEW_MIME_PREFIXES:
        if mime_type.startswith(prefix):
            return preview_type

    preview_type = _PREVIEW_TYPE_BY_MIME.get(mime_type)
    if preview_type is not None:
        return preview_type

    if (
        mime_type.startswith(_TEXT_PREVIEW_MIME_PREFIX)
        or mime_type in _TEXT_PREVIEW_MIME_TYPES
    ):
        return "text"
    return None


def _resolve_preview_type(
    path: Path,
    mime_type: str | None,
) -> PreviewType:
    """根据后缀、MIME 与内容嗅探给前端提供稳定预览类型。"""
    ext = path.suffix.lower().lstrip(".")
    preview_type = _PREVIEW_TYPE_BY_EXTENSION.get(ext)
    if preview_type is not None:
        return preview_type

    preview_type = _resolve_preview_type_from_mime(mime_type)
    if preview_type is not None:
        return preview_type

    if _looks_like_text_file(path):
        return "text"
    return "other"


def _collect_chat_files_from_dir(
    root_dir: Path,
    source: Literal["generated", "uploaded"],
) -> list[GeneratedFileItem]:
    """从指定目录收集聊天文件，并限制路径只来自该目录内部。"""
    if not root_dir.is_dir():
        return []

    items: list[GeneratedFileItem] = []
    for path in root_dir.rglob("*"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        try:
            relative_path = resolved.relative_to(root_dir).as_posix()
        except ValueError:
            continue
        stat = resolved.stat()
        mime_type, _ = mimetypes.guess_type(str(resolved))
        items.append(
            GeneratedFileItem(
                name=resolved.name,
                display_name=_resolve_display_name(resolved.name, source),
                relative_path=relative_path,
                file_url=str(resolved),
                size=stat.st_size,
                modified_at=datetime.fromtimestamp(
                    stat.st_mtime,
                ).isoformat(),
                mime_type=mime_type,
                preview_type=_resolve_preview_type(resolved, mime_type),
                source=source,
            ),
        )
    return items


async def _resolve_console_media_dir(workspace, workspace_dir: Path) -> Path:
    """解析 Console 上传目录，保持文件列表与上传接口使用同一位置。"""
    channel_manager = getattr(workspace, "channel_manager", None)
    if channel_manager is not None:
        console_channel = await channel_manager.get_channel("console")
        media_dir = getattr(console_channel, "media_dir", None)
        if media_dir:
            return Path(media_dir).expanduser().resolve()
    return (workspace_dir / "media").resolve()


async def _stream_with_keepalive(
    source: AsyncGenerator[str, None],
    interval: float = _CONSOLE_SSE_HEARTBEAT_SECONDS,
) -> AsyncGenerator[str, None]:
    """Wrap an SSE generator with keepalive comment frames.

    When no real event arrives within *interval* seconds, emits an SSE
    comment line ``: keep-alive\\n\\n`` which is ignored by EventSource
    but keeps reverse proxies (nginx, ALB) from closing the connection
    due to idle timeout.
    """

    async def _next_item(
        it: AsyncGenerator[str, None],
    ) -> tuple[str, bool]:
        """Return (event, True) or ('', False) when the iterator is done."""
        try:
            return await it.__anext__(), True
        except StopAsyncIteration:
            return "", False

    pending = asyncio.ensure_future(_next_item(source))
    try:
        while True:
            done, _ = await asyncio.wait(
                (pending,),
                timeout=interval,
            )
            if done:
                event_data, has_more = pending.result()
                if not has_more:
                    return
                yield event_data
                pending = asyncio.ensure_future(_next_item(source))
            else:
                # No real event within interval — send keepalive
                yield ": keep-alive\n\n"
    finally:
        with anyio.CancelScope(shield=True):
            pending.cancel()
            try:
                await pending
            except (asyncio.CancelledError, StopAsyncIteration):
                pass
            await source.aclose()


def _safe_filename(name: str) -> str:
    """Safe basename, alphanumeric/./-/_, max 200 chars."""
    base = Path(name).name if name else "file"
    return re.sub(r"[^\w.\-]", "_", base)[:200] or "file"


def _has_denied_chat_attachment_extension(filename: str | None) -> bool:
    """Return True when the outer filename extension is denied."""
    suffix = Path(filename or "").suffix.lower()
    return suffix in DENIED_CHAT_ATTACHMENT_EXECUTABLE_EXTENSIONS


def _extract_content_parts_from_mapping(request_data: dict) -> list[Any]:
    """从 dict 形态请求中提取完整 content parts。"""
    input_data = request_data.get("input", [])
    content_parts: list[Any] = []
    for content_part in input_data:
        if hasattr(content_part, "content"):
            content_parts.extend(list(content_part.content or []))
            continue
        if isinstance(content_part, dict) and "content" in content_part:
            content_parts.extend(content_part.get("content") or [])
    return content_parts


def _extract_payload_fields_from_request(
    request_data: AgentRequest,
) -> tuple[str, str, str, Any, Any, Any, Any, Any, list[Any]]:
    """提取 AgentRequest 形态请求的核心字段。"""
    channel_meta = getattr(request_data, "channel_meta", None) or {}
    selected_skill_names = getattr(
        request_data,
        "selected_skill_names",
        None,
    )
    if selected_skill_names is None:
        selected_skill_names = channel_meta.get("selected_skill_names")
    return (
        getattr(request_data, "channel", None) or "console",
        request_data.user_id or "default",
        request_data.session_id or "default",
        getattr(request_data, "user_name", None)
        or channel_meta.get("user_name"),
        getattr(request_data, "bbk_id", None) or channel_meta.get("bbk_id"),
        getattr(request_data, "system_prompt_injections", None),
        getattr(request_data, "file_url_network", None),
        selected_skill_names,
        list(request_data.input[0].content) if request_data.input else [],
    )


def _extract_payload_fields_from_mapping(
    request_data: dict,
) -> tuple[str, str, str, Any, Any, Any, Any, Any, list[Any]]:
    """提取 dict 形态请求的核心字段。"""
    return (
        request_data.get("channel", "console"),
        request_data.get("user_id", "default"),
        request_data.get("session_id", "default"),
        request_data.get("user_name"),
        request_data.get("bbk_id"),
        request_data.get("system_prompt_injections"),
        request_data.get("file_url_network"),
        request_data.get("selected_skill_names"),
        _extract_content_parts_from_mapping(request_data),
    )


def _extract_context_references(
    request_data: Union[AgentRequest, dict],
) -> Any:
    """Keep structured one-turn context references intact for the runner."""
    if isinstance(request_data, AgentRequest):
        channel_meta = getattr(request_data, "channel_meta", None) or {}
        value = getattr(request_data, "context_references", None)
        if value is None and isinstance(channel_meta, dict):
            value = channel_meta.get("context_references")
        return value
    return request_data.get("context_references")


def _extract_selected_expert_id(
    request_data: Union[AgentRequest, dict],
) -> str | None:
    """Keep an explicit expert selection available for the runner."""
    if isinstance(request_data, AgentRequest):
        channel_meta = getattr(request_data, "channel_meta", None) or {}
        value = getattr(request_data, "selected_expert_id", None)
        if value is None and isinstance(channel_meta, dict):
            value = channel_meta.get("selected_expert_id")
    else:
        value = request_data.get("selected_expert_id")
        channel_meta = request_data.get("channel_meta")
        if value is None and isinstance(channel_meta, dict):
            value = channel_meta.get("selected_expert_id")
    if isinstance(value, str):
        value = value.strip()
    return value or None


def _extract_plan_mode(request_data: Union[AgentRequest, dict]) -> str | None:
    """Keep supported Plan Mode requests in Console channel metadata."""
    if isinstance(request_data, AgentRequest):
        channel_meta = getattr(request_data, "channel_meta", None) or {}
        mode = getattr(request_data, "mode", None) or channel_meta.get("mode")
    else:
        mode = request_data.get("mode")
    return mode if mode in {"plan", "normal"} else None


def _extract_goal_id(request_data: Union[AgentRequest, dict]) -> str | None:
    """Carry the server-owned Goal id into the existing Console run path."""
    if isinstance(request_data, AgentRequest):
        channel_meta = getattr(request_data, "channel_meta", None) or {}
        value = getattr(request_data, "goal_id", None) or channel_meta.get(
            "goal_id",
        )
    else:
        value = request_data.get("goal_id")
        channel_meta = request_data.get("channel_meta")
        if value is None and isinstance(channel_meta, dict):
            value = channel_meta.get("goal_id")
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        return None
    return value.strip()


def _extract_goal_mode_enabled(
    request_data: Union[AgentRequest, dict],
) -> bool:
    """Read the explicit, one-request Goal Mode selector."""
    if isinstance(request_data, AgentRequest):
        channel_meta = getattr(request_data, "channel_meta", None) or {}
        value = getattr(request_data, "goal_mode_enabled", None)
        if value is None:
            value = channel_meta.get("goal_mode_enabled")
    else:
        value = request_data.get("goal_mode_enabled")
        channel_meta = request_data.get("channel_meta")
        if value is None and isinstance(channel_meta, dict):
            value = channel_meta.get("goal_mode_enabled")
    return value is True


def _local_path_from_console_attachment_url(url: object) -> Path | None:
    """Resolve the local path encoded in a Console attachment preview URL."""
    if not isinstance(url, str) or not url:
        return None
    parsed = urlparse(url)
    preview_prefix = "/files/preview/"
    path = parsed.path
    if path.startswith(preview_prefix):
        return Path("/" + unquote(path.removeprefix(preview_prefix)))
    if not parsed.scheme:
        return Path(url)
    return None


async def _append_uploaded_attachment_references(
    native_payload: dict[str, Any],
    workspace: Any,
) -> None:
    """Expose same-turn Console attachments as trusted workspace references."""
    workspace_dir_value = getattr(workspace, "workspace_dir", None)
    if not workspace_dir_value:
        return
    try:
        workspace_dir = Path(workspace_dir_value).resolve()
        workspace_media_dir = (workspace_dir / "media").resolve()
        workspace_media_dir.relative_to(workspace_dir)
    except OSError:
        return
    except ValueError:
        return

    existing_references = native_payload.get("meta", {}).get(
        "context_references",
    )
    if existing_references is None:
        existing_references = []
    if not isinstance(existing_references, list):
        return

    attachment_references: list[dict[str, str]] = []
    for content in native_payload.get("content_parts", []):
        file_url = (
            content.get("file_url")
            if isinstance(content, dict)
            else getattr(content, "file_url", None)
        )
        attachment_path = _local_path_from_console_attachment_url(file_url)
        if attachment_path is None:
            continue
        try:
            resolved_path = attachment_path.resolve()
            relative_path = resolved_path.relative_to(workspace_media_dir)
        except (OSError, ValueError):
            continue
        if not resolved_path.is_file():
            continue
        relative_path_text = relative_path.as_posix()
        reference_id = f"workspace_file:media/{relative_path_text}"
        attachment_references.append(
            {
                "type": "workspace_file",
                "id": reference_id,
                "root": "media",
                "relative_path": relative_path_text,
            },
        )

    if attachment_references:
        attachment_ids = {
            reference["id"] for reference in attachment_references
        }
        existing_references = [
            reference
            for reference in existing_references
            if not (
                isinstance(reference, dict)
                and reference.get("id") in attachment_ids
            )
        ]
        existing_references = [
            *[
                reference
                for reference in existing_references
                if isinstance(reference, dict)
                and reference.get("type") == "skill"
            ],
            *[
                reference
                for reference in existing_references
                if not (
                    isinstance(reference, dict)
                    and reference.get("type") == "skill"
                )
            ],
        ]
        attachment_limit = max(
            1,
            MAX_CONTEXT_REFERENCES - len(existing_references),
        )
        attachment_references = attachment_references[:attachment_limit]
        existing_limit = MAX_CONTEXT_REFERENCES - len(attachment_references)
        native_payload["meta"]["context_references"] = [
            *existing_references[:existing_limit],
            *attachment_references,
        ]


async def _append_document_annotation_context(
    request_data: Union[AgentRequest, dict],
    native_payload: dict[str, Any],
    workspace: Any,
) -> None:
    """Validate optional annotation data and add server-owned turn context."""
    if isinstance(request_data, AgentRequest):
        raw_bundle = getattr(request_data, "document_annotations", None)
    else:
        raw_bundle = request_data.get("document_annotations")
    if raw_bundle is None:
        return

    workspace_dir = getattr(workspace, "workspace_dir", None)
    if not workspace_dir:
        raise DocumentAnnotationValidationError(
            "annotation workspace is not configured",
        )
    context = validate_document_annotation_request(
        raw_bundle,
        content_parts=native_payload.get("content_parts", []),
        workspace_dir=Path(workspace_dir),
    )
    meta = native_payload.setdefault("meta", {})
    existing_injections = meta.get("system_prompt_injections") or []
    if not isinstance(existing_injections, list):
        raise DocumentAnnotationValidationError(
            "system_prompt_injections must be a list",
        )
    meta["system_prompt_injections"] = [
        *existing_injections,
        build_document_annotation_directive(context),
    ]
    meta["_document_annotation_context"] = {
        "source_path": str(context.source_path),
        "expected_annotation_ids": list(context.expected_annotation_ids),
    }


def _extract_wplus_user_scope(
    request_data: Union[AgentRequest, dict],
) -> object | None:
    """Read only caller-owned structured metadata, never visible message text."""
    if isinstance(request_data, AgentRequest):
        channel_meta = getattr(request_data, "channel_meta", None) or {}
        direct = getattr(request_data, "user_scope", None)
    else:
        channel_meta = request_data.get("channel_meta") or {}
        direct = request_data.get("user_scope")
    if direct is not None:
        return direct
    return (
        channel_meta.get("user_scope")
        if isinstance(channel_meta, dict)
        else None
    )


def _extract_console_chat_id(request_data) -> str | None:
    value = (
        request_data.get("chat_id")
        if isinstance(request_data, dict)
        else getattr(request_data, "chat_id", None)
    )
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Invalid chat_id")
    return value.strip()


def _extract_session_and_payload(request_data: Union[AgentRequest, dict]):
    """Extract run_key (ChatSpec.id), session_id, and native payload.

    Align with qwenpaw: keep full multimodal content parts (text/file/image/audio/video)
    instead of dropping non-text blocks.

    run_key must be ChatSpec.id (chat_id) so it matches list_chats/get_chat.
    """
    if isinstance(request_data, AgentRequest):
        (
            _channel_id,
            sender_id,
            session_id,
            user_name,
            bbk_id,
            system_prompt_injections,
            file_url_network,
            selected_skill_names,
            content_parts,
        ) = _extract_payload_fields_from_request(request_data)
    else:
        (
            _channel_id,
            sender_id,
            session_id,
            user_name,
            bbk_id,
            system_prompt_injections,
            file_url_network,
            selected_skill_names,
            content_parts,
        ) = _extract_payload_fields_from_mapping(request_data)

    native_payload: dict[str, Any] = {
        # /console/chat always executes through the Console runtime.  Do not
        # trust the client-provided channel when resolving effective skills.
        "channel_id": _channel_id,
        "sender_id": sender_id,
        "content_parts": content_parts,
        "meta": {
            "session_id": session_id,
            "user_id": sender_id,
        },
    }
    requested_chat_id = _extract_console_chat_id(request_data)
    if requested_chat_id is not None:
        native_payload["chat_id"] = requested_chat_id
    if system_prompt_injections is not None:
        native_payload["meta"][
            "system_prompt_injections"
        ] = system_prompt_injections
    if file_url_network is not None:
        native_payload["meta"]["file_url_network"] = file_url_network
    if selected_skill_names is not None:
        native_payload["meta"]["selected_skill_names"] = selected_skill_names
    plan_mode = _extract_plan_mode(request_data)
    if plan_mode is not None:
        native_payload["meta"]["mode"] = plan_mode
    goal_id = _extract_goal_id(request_data)
    if goal_id is not None:
        native_payload["meta"]["goal_id"] = goal_id
    if _extract_goal_mode_enabled(request_data):
        native_payload["meta"]["goal_mode_enabled"] = True
    context_references = _extract_context_references(request_data)
    if context_references is not None:
        native_payload["meta"]["context_references"] = context_references
    _apply_expert_payload_metadata(native_payload["meta"], request_data)
    scenario_preset_id = _extract_scenario_preset_id(request_data)
    if scenario_preset_id is not None:
        native_payload["meta"]["scenario_preset_id"] = scenario_preset_id
    memory_user_scope = _extract_wplus_user_scope(request_data)
    if memory_user_scope is not None:
        native_payload["meta"]["wplus_user_scope"] = memory_user_scope
    if user_name:
        native_payload["meta"]["user_name"] = user_name
    if bbk_id:
        native_payload["meta"]["bbk_id"] = bbk_id
    return native_payload


def _apply_expert_payload_metadata(
    meta: dict[str, Any],
    request_data: Any,
) -> None:
    selected_expert_id = _extract_selected_expert_id(request_data)
    if selected_expert_id is not None:
        meta["selected_expert_id"] = selected_expert_id
    mapping = (
        request_data
        if isinstance(request_data, dict)
        else (
            request_data.model_dump()
            if hasattr(request_data, "model_dump")
            else vars(request_data)
        )
    )
    revision = mapping.get("expert_binding_revision")
    if revision is None:
        revision = (mapping.get("channel_meta") or {}).get(
            "expert_binding_revision",
        )
    if revision is not None:
        meta["expert_binding_revision"] = revision


def _extract_scenario_preset_id(
    request_data: Union[AgentRequest, dict],
) -> str | None:
    """Read the optional first-message scenario selection without trusting text."""
    mapping = (
        request_data
        if isinstance(request_data, dict)
        else (
            request_data.model_dump()
            if hasattr(request_data, "model_dump")
            else dict(getattr(request_data, "__dict__", {}))
        )
    )
    value = mapping.get("scenario_preset_id")
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > 64
    ):
        raise ValueError("Invalid scenario_preset_id")
    return value.strip()


def _derive_chat_name(native_payload: dict) -> str:
    """Build a display name for a newly created chat."""
    if not native_payload["content_parts"]:
        return "New Chat"

    content = native_payload["content_parts"][0]
    if not content:
        return "Media Message"
    if isinstance(content, dict):
        return content.get("text", "New Chat")[:10]
    if hasattr(content, "text"):
        return content.text[:10]
    return "Media Message"


async def _attach_reconnect_queue(
    workspace,
    tracker,
    session_id: str,
    channel_id: str,
    msgid: str | None = None,
) -> tuple[asyncio.Queue, str, TurnIdentity]:
    """Attach to a running chat by chat_id or logical session_id."""
    for attempt in range(_RECONNECT_ATTACH_ATTEMPTS):
        chat = await workspace.chat_manager.get_chat(session_id)
        if chat is not None:
            coordinator = workspace.answer_turn_coordinator
            lease = await coordinator.attach(chat.id, msgid=msgid)
            if lease is not None:
                return lease.queue, chat.id, lease.identity

        chat_id = await workspace.chat_manager.get_chat_id_by_session(
            session_id,
            channel_id,
        )
        if chat_id is not None:
            coordinator = workspace.answer_turn_coordinator
            lease = await coordinator.attach(chat_id, msgid=msgid)
            if lease is not None:
                return lease.queue, chat_id, lease.identity

        if attempt < _RECONNECT_ATTACH_ATTEMPTS - 1:
            await asyncio.sleep(_RECONNECT_ATTACH_RETRY_DELAY_SECONDS)

    raise HTTPException(
        status_code=404,
        detail="No running chat for this session",
    )


def _current_recovery_chat_is_authorized(
    chat: Any,
    *,
    sender_id: str,
    channel_id: str,
    identity: dict[str, str],
) -> bool:
    """Check a recovery target before revealing whether it has a live turn."""
    if (
        getattr(chat, "user_id", None) != sender_id
        or getattr(chat, "channel", None) != channel_id
    ):
        return False
    chat_meta = getattr(chat, "meta", None) or {}
    for key in ("source_id", "agent_id"):
        expected = identity.get(key)
        stored = chat_meta.get(key)
        if expected and stored and stored != expected:
            return False
    return True


async def _get_authorized_recovery_chat(
    manager: Any,
    candidate_id: object,
    *,
    sender_id: str,
    channel_id: str,
    identity: dict[str, str],
) -> Any | None:
    """Load a candidate chat and hide unauthorized candidates."""
    if not isinstance(candidate_id, str) or not candidate_id:
        return None
    chat = await manager.get_chat(candidate_id)
    if chat is None or not _current_recovery_chat_is_authorized(
        chat,
        sender_id=sender_id,
        channel_id=channel_id,
        identity=identity,
    ):
        return None
    return chat


async def _resolve_current_recovery_chat(
    workspace: Any,
    *,
    requested_chat_id: object,
    session_id: str,
    sender_id: str,
    channel_id: str,
    identity: dict[str, str],
) -> Any | None:
    """Resolve a recovery target without trusting an optional chat id."""
    manager = workspace.chat_manager

    for candidate_id in (requested_chat_id, session_id):
        chat = await _get_authorized_recovery_chat(
            manager,
            candidate_id,
            sender_id=sender_id,
            channel_id=channel_id,
            identity=identity,
        )
        if chat is not None:
            return chat

    chat = await manager.get_chat_by_session(
        session_id,
        channel_id,
        sender_id,
    )
    if chat is not None and _current_recovery_chat_is_authorized(
        chat,
        sender_id=sender_id,
        channel_id=channel_id,
        identity=identity,
    ):
        return chat
    return None


async def _current_recovery_terminal_snapshot(
    workspace: Any,
    chat: Any,
) -> tuple[dict[str, Any], str | None, str | None]:
    """Build the durable terminal recovery payload for one selected Chat."""
    from ..runner.api import _build_chat_history, _read_history_state

    session = workspace.runner.session
    state = await _read_history_state(
        session,
        chat.session_id,
        chat.user_id,
    )
    history = await _build_chat_history(
        chat,
        session=session,
        workspace=workspace,
        status_override="idle",
        non_blocking=True,
        state=state,
    )
    turn_states = state.get("turn_states")
    if state.get("schema_version") == 3:
        latest = await session.get_latest_turn_state(
            chat.session_id,
            chat.user_id,
            state,
            chat.id,
        )
        turn_states = dict([latest]) if latest is not None else {}
    if not isinstance(turn_states, dict):
        return history.model_dump(mode="json"), None, None
    for msgid, turn_state in reversed(tuple(turn_states.items())):
        if not isinstance(turn_state, dict):
            continue
        if turn_state.get("chat_id") not in (None, chat.id):
            continue
        status = turn_state.get("status")
        if not isinstance(status, str):
            continue
        if status == "admitted":

            orphan_msgid = msgid

            def reconcile_orphaned_turn(
                state: dict[str, Any],
                orphan_msgid: str = orphan_msgid,
            ) -> dict[str, Any]:
                states = state.get("turn_states")
                if isinstance(states, dict) and isinstance(
                    states.get(orphan_msgid),
                    dict,
                ):
                    states[orphan_msgid]["status"] = "failed"
                return state

            await session.mutate_session_state(
                chat.session_id,
                reconcile_orphaned_turn,
                user_id=chat.user_id,
            )
            status = "failed"
        if status not in {"completed", "stopped", "cancelled", "failed"}:
            continue
        return (
            history.model_dump(mode="json"),
            msgid if isinstance(msgid, str) and msgid else None,
            "stopped" if status == "cancelled" else status,
        )
    return history.model_dump(mode="json"), None, None


def _console_chat_stream_headers(
    *,
    chat_id: str | None = None,
    session_id: str,
    msgid: str | None,
) -> dict[str, str]:
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    if chat_id:
        headers["X-Swe-Chatid"] = chat_id
    if msgid:
        headers["X-Swe-Msgid"] = msgid
        headers["X-Swe-Sessionid"] = session_id
    return headers


def _build_console_chat_meta(
    workspace: Any,
    native_payload: dict[str, Any],
) -> dict[str, Any]:
    meta = (
        {"agent_id": workspace.agent_id}
        if getattr(workspace, "agent_id", None)
        else {}
    )
    source_id = native_payload["meta"].get("source_id")
    if source_id:
        meta["source_id"] = source_id
    return meta


def _has_explicit_chat_target(chat_id: str | None, session_id: str) -> bool:
    if not chat_id:
        return False
    if chat_id != session_id:
        return True
    # Older Console clients also put the unresolved logical ID in chat_id.
    try:
        uuid.UUID(chat_id)
    except ValueError:
        return False
    return True


async def _get_or_create_console_chat(
    workspace: Any,
    session_id: str,
    native_payload: dict[str, Any],
) -> tuple[Any, str | None, bool]:
    scenario_preset_id = native_payload["meta"].get("scenario_preset_id")
    chat_meta = _build_console_chat_meta(workspace, native_payload)
    requested_chat_id = native_payload.get("chat_id")
    if _has_explicit_chat_target(requested_chat_id, session_id):
        chat = await _get_authorized_recovery_chat(
            workspace.chat_manager,
            requested_chat_id,
            sender_id=native_payload["sender_id"],
            channel_id=native_payload["channel_id"],
            identity=chat_meta,
        )
        if chat is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        if chat.session_id != session_id:
            raise HTTPException(
                status_code=409,
                detail="Chat session identity mismatch",
            )
        return chat, scenario_preset_id, False
    if not scenario_preset_id:
        chat = await workspace.chat_manager.get_or_create_chat(
            session_id,
            native_payload["sender_id"],
            native_payload["channel_id"],
            name=_derive_chat_name(native_payload),
            meta=chat_meta or None,
        )
        return chat, None, False

    from ..scenario_preset.router import get_service as get_scenario_service
    from ..scenario_preset.runtime import initialize_scenario_snapshot

    async def snapshot_factory(chat):
        workspace_dir = getattr(workspace, "workspace_dir", None)
        resource_root = (
            Path(workspace_dir) / ".scenario_sessions" / chat.id
            if workspace_dir is not None
            else None
        )
        return await initialize_scenario_snapshot(
            service=get_scenario_service(),
            source_id=native_payload["meta"]["source_id"],
            scenario_id=scenario_preset_id,
            agent_id=getattr(workspace, "agent_id", None),
            workspace_dir=workspace_dir,
            agent_config=getattr(workspace, "config", None),
            bbk_id=native_payload["meta"].get("bbk_id"),
            session_resource_root=resource_root,
        )

    try:
        chat, created = (
            await workspace.chat_manager.get_or_create_scenario_chat(
                session_id,
                native_payload["sender_id"],
                native_payload["channel_id"],
                _derive_chat_name(native_payload),
                chat_meta,
                snapshot_factory,
            )
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail="Scenario preset is no longer available",
        ) from exc
    return chat, scenario_preset_id, created


def _validate_and_attach_scenario_snapshot(
    workspace: Any,
    chat: Any,
    scenario_preset_id: str | None,
    native_payload: dict[str, Any],
) -> None:
    if not scenario_preset_id:
        return
    from ..scenario_preset.runtime import get_scenario_snapshot

    snapshot = get_scenario_snapshot(chat.meta)
    if snapshot is None:
        raise HTTPException(
            status_code=409,
            detail="Scenario selection is only available for a new chat",
        )
    if snapshot.get("scenario_id") != scenario_preset_id:
        raise HTTPException(
            status_code=409,
            detail="Scenario selection is locked for this chat",
        )
    if snapshot.get("agent_id") not in (None, workspace.agent_id):
        raise HTTPException(
            status_code=409,
            detail="Scenario chat is bound to another Agent",
        )
    native_payload["meta"]["scenario_preset_snapshot"] = snapshot
    native_payload["meta"]["scenario_preset_snapshot_source"] = "chat_meta"


async def _start_or_attach_console_turn(
    coordinator: Any,
    chat: Any,
    native_payload: dict[str, Any],
    console_channel: Any,
    msgid: str,
    before_start: Callable[[], None] | None,
) -> Any:
    if await coordinator.status(chat.id) == TurnStatus.STOPPING:
        raise HTTPException(status_code=409, detail="Chat is stopping")
    if chat.channel and chat.channel != "console":
        native_payload["meta"]["session_channel"] = chat.channel
    kwargs = {"msgid": msgid}
    if before_start is not None:
        kwargs["before_start"] = before_start
    return await coordinator.start_or_attach(
        chat.id,
        native_payload,
        _console_turn_producer(console_channel),
        **kwargs,
    )


async def _start_new_chat(
    workspace,
    tracker,
    console_channel,
    session_id,
    native_payload,
    *,
    before_start: Callable[[], None] | None = None,
    include_run_status: bool = False,
):
    """创建新会话并启动 stream，返回 (queue, run_key, msgid)。"""
    msgid = str(uuid.uuid4())
    chat, scenario_preset_id, created = await _get_or_create_console_chat(
        workspace,
        session_id,
        native_payload,
    )
    _validate_and_attach_scenario_snapshot(
        workspace,
        chat,
        scenario_preset_id,
        native_payload,
    )
    native_payload["meta"]["chat_id"] = chat.id
    from ..runner.expert_main_admission import admit_expert_main

    expert_token = await admit_expert_main(
        workspace,
        chat,
        native_payload,
        msgid,
    )
    coordinator = workspace.answer_turn_coordinator
    try:
        if coordinator is None:
            raise RuntimeError("answer-turn coordinator is not configured")
        lease = await _start_or_attach_console_turn(
            coordinator,
            chat,
            native_payload,
            console_channel,
            msgid,
            before_start,
        )
    except BaseException:
        if expert_token:
            await workspace.runner.discard_expert_main_execution(expert_token)
        if scenario_preset_id and created:
            await workspace.chat_manager.delete_chats([chat.id])
        raise
    queue = lease.queue
    is_new_run = lease.is_new_run
    if expert_token and not is_new_run:
        await workspace.runner.discard_expert_main_execution(expert_token)
    msgid = lease.identity.msgid
    native_payload["meta"]["msgid"] = msgid
    native_payload["meta"]["answer_turn_identity"] = lease.identity
    if include_run_status:
        return queue, chat.id, msgid, is_new_run
    return queue, chat.id, msgid


def _console_turn_producer(console_channel: Any):
    async def producer(identity: TurnIdentity, payload: dict[str, Any]):
        bound = {
            **payload,
            "meta": {
                **(payload.get("meta") or {}),
                "answer_turn_identity": identity,
                "msgid": identity.msgid,
            },
        }
        async for event in console_channel.stream_one(bound):
            yield event

    return producer


def _validate_console_chat_identity(
    request: Request,
    workspace,
    native_payload: dict,
) -> None:
    authenticated_user_id = str(
        getattr(request.state, "user_id", None) or "",
    ).strip()
    payload_sender_id = str(native_payload.get("sender_id") or "").strip()
    if authenticated_user_id and payload_sender_id != authenticated_user_id:
        raise HTTPException(
            status_code=403,
            detail="Console sender does not match authenticated user",
        )

    authenticated_agent_id = str(
        getattr(request.state, "agent_id", None) or "",
    ).strip()
    workspace_agent_id = str(
        getattr(workspace, "agent_id", None) or "",
    ).strip()
    if (
        authenticated_agent_id
        and workspace_agent_id
        and authenticated_agent_id != workspace_agent_id
    ):
        raise HTTPException(
            status_code=403,
            detail="Console Agent does not match authenticated Agent",
        )


@router.post(
    "/chat",
    status_code=200,
    summary="Chat with console (streaming response)",
    description="Agent API Request Format. See runtime.agentscope.io. "
    "Use body.reconnect=true to attach to a running stream.",
)
async def post_console_chat(
    request_data: Union[AgentRequest, dict],
    request: Request,
) -> StreamingResponse:
    """Stream agent response. Run continues in background after disconnect.
    Stop via POST /console/chat/stop. Reconnect with body.reconnect=true.
    """
    workspace = await get_agent_for_request(request)
    console_channel = await workspace.channel_manager.get_channel("console")
    if console_channel is None:
        raise HTTPException(
            status_code=503,
            detail="Channel Console not found",
        )
    try:
        native_payload = _extract_session_and_payload(request_data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    await _append_uploaded_attachment_references(native_payload, workspace)
    try:
        await _append_document_annotation_context(
            request_data,
            native_payload,
            workspace,
        )
    except DocumentAnnotationValidationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    _inject_request_metadata(request, native_payload)
    identity = _resolve_console_identity(request, native_payload, workspace)
    _inject_user_context(request, native_payload)

    session_id = console_channel.resolve_session_id(
        sender_id=native_payload["sender_id"],
        channel_meta=native_payload["meta"],
    )
    logger.debug("Console chat: resolved session_id=%s", session_id)

    request_mapping = (
        request_data
        if isinstance(request_data, dict)
        else request_data.model_dump()
    )
    is_reconnect = request_mapping.get("reconnect") is True
    is_current_reconnect = request_mapping.get("reconnect_mode") == "current"
    is_reconnect = is_reconnect or is_current_reconnect

    if not is_reconnect:
        wplus_result, suppression_ctx = await _try_wplus_entry_intercept(
            workspace=workspace,
            native_payload=native_payload,
            identity=identity,
            session_id=session_id,
            request_mapping=request_mapping,
        )
        if wplus_result is not None:
            return wplus_result
    else:
        suppression_ctx = None

    from ..runner.sse_replay import ReplayUnavailable

    try:
        return await _dispatch_console_stream(
            workspace=workspace,
            console_channel=console_channel,
            native_payload=native_payload,
            session_id=session_id,
            identity=identity,
            request_mapping=request_mapping,
            is_reconnect=is_reconnect,
            is_current_reconnect=is_current_reconnect,
            suppression_ctx=suppression_ctx,
        )
    except ReplayUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
            headers={"Retry-After": "1"},
        ) from exc


# ---------------------------------------------------------------------------
# post_console_chat helpers
# ---------------------------------------------------------------------------


class _SuppressionContext:
    __slots__ = (
        "suppress_implicit",
        "service",
        "proposal_id",
        "token",
        "entry_text",
        "chat_id",
    )

    def __init__(
        self,
        *,
        suppress_implicit: bool,
        service: Any,
        proposal_id: str,
        token: str,
        entry_text: str,
        chat_id: str | None = None,
    ) -> None:
        self.suppress_implicit = suppress_implicit
        self.service = service
        self.proposal_id = proposal_id
        self.token = token
        self.entry_text = entry_text
        self.chat_id = chat_id


def _inject_request_metadata(
    request: Request,
    native_payload: dict[str, Any],
) -> None:
    try:
        b3_context = extract_b3_context(request.headers)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if b3_context:
        native_payload["meta"][B3_CONTEXT_META_KEY] = b3_context
        native_payload["meta"]["b3_trace_id"] = b3_context[B3_TRACE_ID_HEADER]
    source_id = getattr(
        request.state,
        "source_id",
        None,
    ) or request.headers.get(
        "X-Source-Id",
    )
    if not source_id:
        raise HTTPException(
            status_code=400,
            detail="X-Source-Id header is required",
        )
    native_payload["meta"]["source_id"] = source_id


def _resolve_console_identity(
    request: Request,
    native_payload: dict[str, Any],
    workspace: Any,
) -> dict[str, str]:
    """Extract and validate authenticated identity from request state."""
    _validate_console_chat_identity(request, workspace, native_payload)
    identity = {
        "tenant_id": str(
            getattr(request.state, "tenant_id", None) or "",
        ).strip(),
        "source_id": str(
            getattr(request.state, "source_id", None) or "",
        ).strip(),
        "user_id": str(
            getattr(request.state, "user_id", None) or "",
        ).strip(),
        "agent_id": str(
            getattr(request.state, "agent_id", None) or "",
        ).strip(),
    }
    workspace_agent_id = str(
        getattr(workspace, "agent_id", None) or "",
    ).strip()
    identity["agent_id"] = identity["agent_id"] or workspace_agent_id
    return identity


def _inject_user_context(
    request: Request,
    native_payload: dict[str, Any],
) -> None:
    request_state = getattr(request, "state", None)
    if not request_state:
        return
    user_name = getattr(request_state, "user_name", None)
    bbk_id = getattr(request_state, "bbk_id", None)
    if user_name:
        native_payload["meta"]["user_name"] = user_name
    if bbk_id:
        native_payload["meta"]["bbk_id"] = bbk_id


def _build_suppression_before_start(
    ctx: _SuppressionContext | None,
    native_payload: dict[str, Any],
) -> Callable[[], None] | None:
    if ctx is None or not ctx.suppress_implicit:
        return None
    assert ctx.service is not None
    claim_id = (
        ctx.service.claim_suppression(
            proposal_id=ctx.proposal_id,
            suppression_token=ctx.token,
            original_text=ctx.entry_text,
        )
        or ""
    )
    if not claim_id:
        raise HTTPException(
            status_code=409,
            detail="The original Chat request was already replayed",
        )
    native_payload["meta"]["wplus_sop_replay_claim_id"] = claim_id

    def consume_replay_claim() -> None:
        assert ctx.service is not None
        consumed = ctx.service.consume_suppression(
            proposal_id=ctx.proposal_id,
            claim_id=claim_id,
            suppression_token=ctx.token,
            original_text=ctx.entry_text,
        )
        if not consumed:
            raise HTTPException(
                status_code=409,
                detail="The original Chat request was already replayed",
            )

    return consume_replay_claim


def _release_suppression_on_failure(
    ctx: _SuppressionContext | None,
    native_payload: dict[str, Any],
) -> None:
    if ctx is None or not ctx.suppress_implicit:
        return
    claim_id = str(
        native_payload.get("meta", {}).get("wplus_sop_replay_claim_id", ""),
    )
    try:
        ctx.service.release_suppression_claim(
            proposal_id=ctx.proposal_id,
            claim_id=claim_id,
        )
    except Exception:
        pass


def _validate_suppression_new_run(
    ctx: _SuppressionContext | None,
    tracker: Any,
    run_key: str,
    queue: Any,
    is_new_run: bool,
    native_payload: dict[str, Any],
) -> None:
    if ctx is None or not ctx.suppress_implicit:
        return
    if is_new_run:
        return
    claim_id = str(
        native_payload.get("meta", {}).get("wplus_sop_replay_claim_id", ""),
    )
    assert ctx.service is not None
    ctx.service.release_suppression_claim(
        proposal_id=ctx.proposal_id,
        claim_id=claim_id,
    )
    raise HTTPException(
        status_code=409,
        detail="The original Chat request is already running",
    )


def _check_wplus_active_session_lock(
    workspace: Any,
    chat: Any,
    tid: str,
    sid: str,
    uid: str,
    aid: str,
) -> Any:
    """Return active session if chat has identity, or raise if locked."""
    from ..wplus_sop.models import OwnershipTuple
    from ..wplus_sop.service import WPlusSopService

    if chat is None or not all((tid, sid, uid, aid)):
        return None
    ownership = OwnershipTuple(
        tenant_id=tid,
        source_id=sid,
        user_id=uid,
        agent_id=aid,
        chat_id=chat.id,
        logical_chat_session_id=chat.session_id,
    )
    wplus_service = WPlusSopService(workspace=workspace, ownership=ownership)
    active_session = wplus_service.get_active_session()
    if (
        active_session is not None
        and active_session.projection.locks_chat_input
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "This Chat is locked by an active W+ SOP Session; "
                "continue in the W+ workspace"
            ),
        )
    return active_session


def _resolve_wplus_suppression(
    request_mapping: dict[str, Any],
    chat: Any,
    entry_text: str,
    tid: str,
    sid: str,
    uid: str,
    aid: str,
    workspace: Any,
) -> tuple[bool, Any, str, str]:
    """Validate suppression request and return (suppress, service, proposal_id, token)."""
    from ..wplus_sop.models import OwnershipTuple
    from ..wplus_sop.service import WPlusSopService

    suppression = request_mapping.get("wplus_sop_suppression")
    if not isinstance(suppression, dict) or chat is None:
        return False, None, "", ""
    if not all((tid, sid, uid, aid)):
        raise HTTPException(status_code=400, detail="Identity required")
    ownership = OwnershipTuple(
        tenant_id=tid,
        source_id=sid,
        user_id=uid,
        agent_id=aid,
        chat_id=chat.id,
        logical_chat_session_id=chat.session_id,
    )
    wplus_service = WPlusSopService(workspace=workspace, ownership=ownership)
    suppress_implicit = wplus_service.validate_suppression(
        proposal_id=str(suppression.get("proposal_id") or ""),
        suppression_token=str(suppression.get("token") or ""),
        original_text=entry_text,
    )
    if not suppress_implicit:
        raise HTTPException(
            status_code=404,
            detail="W+ SOP proposal not found",
        )
    return (
        True,
        wplus_service,
        str(suppression.get("proposal_id") or ""),
        str(suppression.get("token") or ""),
    )


async def _try_wplus_entry_intercept(
    *,
    workspace: Any,
    native_payload: dict[str, Any],
    identity: dict[str, str],
    session_id: str,
    request_mapping: dict[str, Any],
) -> tuple[StreamingResponse | None, _SuppressionContext | None]:
    """Intercept W+ SOP entry. Returns (response, suppression_ctx)."""
    from ..wplus_sop.entry import classify_wplus_entry, extract_entry_text
    from ..wplus_sop.models import OwnershipTuple
    from ..wplus_sop.memory_policy import (
        WPlusMemoryPolicyError,
        normalize_anonymous_user_scope,
    )
    from ..wplus_sop.service import WPlusSopService

    get_chat_by_session = getattr(
        workspace.chat_manager,
        "get_chat_by_session",
        None,
    )
    tid = identity["tenant_id"]
    sid = identity["source_id"]
    uid = identity["user_id"]
    aid = identity["agent_id"]
    has_identity = all((tid, sid, uid, aid))
    chat = (
        await get_chat_by_session(
            session_id,
            channel=native_payload["channel_id"],
            user_id=uid,
        )
        if callable(get_chat_by_session) and has_identity
        else None
    )
    entry_text = extract_entry_text(native_payload["content_parts"])

    active_session = _check_wplus_active_session_lock(
        workspace,
        chat,
        tid,
        sid,
        uid,
        aid,
    )

    (
        suppress_implicit,
        suppression_service,
        suppression_proposal_id,
        suppression_token,
    ) = _resolve_wplus_suppression(
        request_mapping,
        chat,
        entry_text,
        tid,
        sid,
        uid,
        aid,
        workspace,
    )

    classification = classify_wplus_entry(
        selected_skill_names=native_payload["meta"].get(
            "selected_skill_names",
        ),
        message_text=entry_text,
        suppress_entry=suppress_implicit,
    )
    if not classification.should_offer:
        if chat is not None:
            native_payload["meta"]["chat_id"] = chat.id
        return None, (
            _SuppressionContext(
                suppress_implicit=suppress_implicit,
                service=suppression_service,
                proposal_id=suppression_proposal_id,
                token=suppression_token,
                entry_text=entry_text,
                chat_id=chat.id if chat is not None else None,
            )
            if suppress_implicit
            else None
        )

    if active_session is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "This Chat already has a paused W+ SOP Session; "
                "resume it in the W+ workspace"
            ),
        )
    if not all((tid, sid, uid, aid)):
        raise HTTPException(
            status_code=400,
            detail="W+ SOP entry requires tenant/source/user/agent",
        )
    if chat is None:
        chat = await workspace.chat_manager.get_or_create_chat(
            session_id,
            uid,
            native_payload["channel_id"],
            name=_derive_chat_name(native_payload),
            meta={"agent_id": aid},
        )
    ownership = OwnershipTuple(
        tenant_id=tid,
        source_id=sid,
        user_id=uid,
        agent_id=aid,
        chat_id=chat.id,
        logical_chat_session_id=chat.session_id,
    )
    wplus_service = WPlusSopService(workspace=workspace, ownership=ownership)
    try:
        memory_user_scope = normalize_anonymous_user_scope(
            native_payload["meta"].get("wplus_user_scope"),
        )
        proposal = wplus_service.create_entry_proposal(
            original_text=entry_text,
            mode=classification.mode or "explicit",
            memory_user_scope=memory_user_scope,
        )
    except WPlusMemoryPolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    chat.meta = {
        **(chat.meta or {}),
        "wplus_sop_entry_proposal": {
            "proposal_id": proposal.proposal_id,
            "mode": proposal.detection_mode.value,
            "status": proposal.status.value,
        },
    }
    await workspace.chat_manager.update_chat(chat)

    async def entry_event_generator() -> AsyncGenerator[str, None]:
        data = {
            "object": "wplus_sop_entry_proposal",
            "status": "completed",
            "proposal_id": proposal.proposal_id,
            "mode": proposal.detection_mode.value,
            "confidence": classification.confidence,
            "chat_id": chat.id,
            "session_id": chat.session_id,
            "title": "进入 W+ SOP 工作台",
            "message": "Claw 将替你完成逐环节澄清、系统预跑和反馈重跑。",
        }
        yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    return (
        StreamingResponse(
            entry_event_generator(),
            media_type="text/event-stream",
            headers=_console_chat_stream_headers(
                session_id=session_id,
                msgid=None,
            ),
        ),
        None,
    )


async def _terminal_recovery_response(
    workspace: Any,
    chat: Any,
) -> StreamingResponse:
    """Build the SSE response used when a recovered turn already ended."""
    history, msgid, turn_status = await _current_recovery_terminal_snapshot(
        workspace,
        chat,
    )
    snapshot = {
        "object": "chat_snapshot",
        "chat_id": chat.id,
        "msgid": msgid,
        "turn_status": turn_status,
        "history": history,
    }

    async def terminal_event_generator() -> AsyncGenerator[str, None]:
        yield (
            "event: chat.snapshot\n"
            f"data: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
        )

    return StreamingResponse(
        terminal_event_generator(),
        media_type="text/event-stream",
        headers=_console_chat_stream_headers(
            chat_id=chat.id if msgid else None,
            session_id=chat.session_id if msgid else "",
            msgid=msgid,
        ),
    )


async def _resolve_current_reconnect_target(
    *,
    workspace: Any,
    native_payload: dict[str, Any],
    session_id: str,
    identity: dict[str, str],
) -> tuple[asyncio.Queue, str, TurnIdentity, str] | StreamingResponse:
    """Resolve a current reconnect to a live lease or terminal snapshot."""
    chat = await _resolve_current_recovery_chat(
        workspace,
        requested_chat_id=native_payload.get("chat_id"),
        session_id=session_id,
        sender_id=native_payload["sender_id"],
        channel_id=native_payload["channel_id"],
        identity=identity,
    )
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    coordinator = workspace.answer_turn_coordinator
    recover_current = getattr(coordinator, "recover_current", None)
    if recover_current is not None:
        try:
            selected = await recover_current(
                chat.id,
                lambda: _terminal_recovery_response(workspace, chat),
            )
        except TurnSettlementPendingError as exc:
            raise HTTPException(
                status_code=503,
                detail="Chat settlement is pending",
                headers={"Retry-After": "1"},
            ) from exc
        if isinstance(selected, StreamingResponse):
            return selected
        lease = selected
    else:
        lease = await coordinator.attach(chat.id)
    if lease is None:
        status_reader = getattr(coordinator, "status", None)
        coordinator_status = (
            await status_reader(chat.id) if status_reader is not None else None
        )
        if coordinator_status is not None:
            raise HTTPException(
                status_code=503,
                detail="Chat settlement is pending",
                headers={"Retry-After": "1"},
            )
        settlement_pending = getattr(coordinator, "settlement_pending", None)
        if settlement_pending is not None and await settlement_pending(
            chat.id,
        ):
            raise HTTPException(
                status_code=503,
                detail="Chat settlement is pending",
                headers={"Retry-After": "1"},
            )
        return await _terminal_recovery_response(workspace, chat)
    native_payload["meta"]["resolved_session_id"] = chat.session_id
    return lease.queue, chat.id, lease.identity, lease.identity.msgid


async def _start_console_stream_target(
    *,
    workspace: Any,
    tracker: Any,
    console_channel: Any,
    session_id: str,
    native_payload: dict[str, Any],
    suppression_ctx: _SuppressionContext | None,
) -> tuple[asyncio.Queue, str, TurnIdentity | None, str | None]:
    """Start a new chat and apply suppression bookkeeping."""
    before_start = _build_suppression_before_start(
        suppression_ctx,
        native_payload,
    )
    try:
        queue, run_key, msgid, is_new_run = await _start_new_chat(
            workspace,
            tracker,
            console_channel,
            session_id,
            native_payload,
            before_start=before_start,
            include_run_status=True,
        )
    except TurnSettlementPendingError as exc:
        _release_suppression_on_failure(suppression_ctx, native_payload)
        raise HTTPException(
            status_code=503,
            detail="Chat settlement is pending",
            headers={"Retry-After": "1"},
        ) from exc
    except Exception:
        _release_suppression_on_failure(suppression_ctx, native_payload)
        raise
    _validate_suppression_new_run(
        suppression_ctx,
        tracker,
        run_key,
        queue,
        is_new_run,
        native_payload,
    )
    return (
        queue,
        run_key,
        native_payload["meta"].get("answer_turn_identity"),
        msgid,
    )


async def _dispatch_console_stream(
    *,
    workspace: Any,
    console_channel: Any,
    native_payload: dict[str, Any],
    session_id: str,
    identity: dict[str, str],
    request_mapping: dict[str, Any],
    is_reconnect: bool,
    is_current_reconnect: bool = False,
    suppression_ctx: _SuppressionContext | None = None,
) -> StreamingResponse:
    """Execute the actual chat run and stream the response."""
    tracker = workspace.task_tracker
    msgid: str | None = None

    if is_current_reconnect:
        current_target = await _resolve_current_reconnect_target(
            workspace=workspace,
            native_payload={
                **native_payload,
                "chat_id": request_mapping.get("chat_id"),
            },
            session_id=session_id,
            identity=identity,
        )
        if isinstance(current_target, StreamingResponse):
            return current_target
        queue, run_key, stream_identity, msgid = current_target
    elif is_reconnect:
        requested_msgid = native_payload.get("meta", {}).get("msgid")
        queue, run_key, stream_identity = await _attach_reconnect_queue(
            workspace,
            tracker,
            session_id,
            native_payload["channel_id"],
            requested_msgid if isinstance(requested_msgid, str) else None,
        )
    else:
        queue, run_key, stream_identity, msgid = (
            await _start_console_stream_target(
                workspace=workspace,
                tracker=tracker,
                console_channel=console_channel,
                session_id=session_id,
                native_payload=native_payload,
                suppression_ctx=suppression_ctx,
            )
        )

    from ..runner.sse_replay import ReplaySubscription, ReplayUnavailable
    from ..runner.sse_response import ReplayStreamingResponse

    resumable = (
        request_mapping.get("stream_protocol") == "cursor-v1"
        and isinstance(queue, ReplaySubscription)
        and stream_identity is not None
    )
    if resumable:
        try:
            queue.resume(
                stream_identity.turn_id,
                request_mapping.get("after_event_id"),
            )
        except ReplayUnavailable as exc:
            await tracker.detach_subscriber(stream_identity, queue)
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    async def event_generator() -> AsyncGenerator[str, None]:
        if stream_identity is None:
            raise RuntimeError("answer-turn identity is missing")
        stream_it = tracker.stream(stream_identity, queue)
        try:
            yield ": keep-alive\n\n"
            try:
                async for event_data in stream_it:
                    if resumable and not event_data.startswith(
                        "event: stream.",
                    ):
                        event_data = (
                            f"id: {stream_identity.turn_id}:{queue.last_sequence}\n"
                            + event_data
                        )
                    yield event_data
                if resumable:
                    yield 'event: stream.end\ndata: {"object":"stream_end"}\n\n'
            except Exception as e:
                logger.exception("Console chat stream error")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            # ASGI disconnect uses level cancellation; cleanup may wait for
            # the tracker lock and must finish before leaving its cancel scope.
            with anyio.CancelScope(shield=True):
                await stream_it.aclose()
                # The initial keepalive may block before stream_it starts.
                if isinstance(queue, ReplaySubscription):
                    await tracker.detach_subscriber(stream_identity, queue)

    stream_headers = _console_chat_stream_headers(
        chat_id=run_key,
        session_id=(
            native_payload.get("meta", {}).get("resolved_session_id")
            or session_id
        ),
        msgid=(
            stream_identity.msgid
            if is_current_reconnect and stream_identity is not None
            else msgid
        ),
    )
    if resumable:
        stream_headers["X-Swe-Stream-Protocol"] = "cursor-v1"
    response_class = (
        ReplayStreamingResponse if resumable else StreamingResponse
    )
    response_options = {}
    if resumable:

        async def detach_unstarted_subscription():
            await tracker.detach_subscriber(stream_identity, queue)

        response_options["on_close"] = detach_unstarted_subscription
    return response_class(
        _stream_with_keepalive(event_generator()),
        media_type="text/event-stream",
        headers=stream_headers,
        **response_options,
    )


def _console_stop_request_identity(request: Request) -> tuple[str, str]:
    """Return the caller identity available to the Console Stop endpoint."""
    user_id = str(
        getattr(request.state, "user_id", None)
        or request.headers.get("X-User-Id")
        or "",
    ).strip()
    source_id = str(
        getattr(request.state, "source_id", None)
        or request.headers.get("X-Source-Id")
        or "",
    ).strip()
    return user_id, source_id


async def _claim_console_stop(
    coordinator: Any,
    chat_id: str,
    msgid: str | None,
) -> tuple[bool, str | None, str | None, str]:
    identity = await coordinator.current_identity(chat_id)
    if identity is None:
        return False, None, None, "idle"
    claim = await coordinator.claim_stop(identity, msgid=msgid)
    return (
        claim.accepted,
        claim.identity.chat_id if claim.identity else None,
        claim.identity.msgid if claim.identity else None,
        (
            getattr(claim.status, "value", claim.status)
            if claim.status
            else "idle"
        ),
    )


async def _cancel_console_turn_subagents(
    workspace: Any,
    chat_id: str,
    msgid: str,
) -> None:
    """Best-effort cancel locally managed SubAgents for an accepted Stop."""
    try:
        from ...agents.tools.subagent_background import (
            build_background_subagent_scope,
            get_default_background_subagent_supervisor,
        )

        config = workspace.config
        request_context = {
            "tenant_id": getattr(workspace, "tenant_id", None),
            "agent_id": getattr(workspace, "agent_id", None),
            "chat_id": chat_id,
            "msgid": msgid,
        }
        supervisor = getattr(workspace, "subagent_supervisor", None)
        if supervisor is None:
            supervisor = get_default_background_subagent_supervisor()
        scope = build_background_subagent_scope(
            parent_agent_config=config,
            request_context=request_context,
        )
        cancel_turn_runs = getattr(supervisor, "cancel_turn_runs", None)
        if callable(cancel_turn_runs):
            await cancel_turn_runs(scope, chat_id=chat_id, msgid=msgid)
    except Exception:
        logger.exception(
            "Failed to cancel stopped-turn SubAgents chat_id=%s msgid=%s",
            chat_id,
            msgid,
        )


async def _interrupt_console_goal(
    workspace: Any,
    chat_id: str,
    msgid: str,
) -> None:
    """Interrupt an active Goal owned by the stopped Chat, if any."""
    try:
        from ..goals.registry import get_goal_service

        service = get_goal_service()
        if service is None:
            return
        goal = await service.recent_for_chat(chat_id)
        if goal is None:
            return
        interrupt_turn_if_matches = getattr(
            service,
            "interrupt_turn_if_matches",
            None,
        )
        if not callable(interrupt_turn_if_matches):
            return
        await interrupt_turn_if_matches(
            goal.goal_id,
            msgid,
            "Chat Stop interrupted the active Goal turn",
        )
    except Exception:
        logger.exception(
            "Failed to interrupt Goal for stopped Chat %s",
            chat_id,
        )


def _console_stop_idle_response() -> dict[str, Any]:
    return {"stopped": False, "accepted": False, "status": "idle"}


async def _resolve_console_stop_chat_id(
    workspace: Any,
    coordinator: Any,
    *,
    requested_chat_id: str | None,
    session_id: str | None,
    user_id: str,
    source_id: str,
) -> str | None:
    if requested_chat_id is not None or not session_id:
        return requested_chat_id
    candidates = await workspace.chat_manager.list_chats(
        user_id=user_id,
        channel="console",
    )
    matches = [
        chat
        for chat in candidates
        if chat.session_id == session_id
        and (
            not source_id
            or str(
                (getattr(chat, "meta", None) or {}).get("source_id") or "",
            )
            == source_id
        )
    ]
    active_matches = [
        chat
        for chat in matches
        if await coordinator.current_identity(chat.id) is not None
    ]
    return active_matches[0].id if len(active_matches) == 1 else None


def _is_authorized_console_stop_chat(
    chat: Any,
    *,
    user_id: str,
    source_id: str,
) -> bool:
    if chat is None or getattr(chat, "channel", "console") != "console":
        return False
    chat_source_id = str(
        (getattr(chat, "meta", None) or {}).get("source_id") or "",
    ).strip()
    return str(getattr(chat, "user_id", "")) == user_id and (
        not chat_source_id or chat_source_id == source_id
    )


@router.post(
    "/chat/stop",
    status_code=200,
    summary="Stop running console chat",
)
async def post_console_chat_stop(
    request: Request,
    chat_id: str | None = Query(None, description="Chat id (ChatSpec.id)"),
    msgid: str | None = Query(None, description="User question message id"),
    session_id: str | None = Query(
        None,
        description="Early startup session id",
    ),
) -> dict:
    """Stop one Console answer turn with legacy-compatible fallbacks."""
    workspace = await get_agent_for_request(request)
    coordinator = workspace.answer_turn_coordinator
    if coordinator is None:
        raise HTTPException(
            status_code=503,
            detail="Answer-turn coordinator not available",
        )
    target_chat_id = chat_id
    if target_chat_id is None and msgid is not None:
        return _console_stop_idle_response()
    request_user_id, request_source_id = _console_stop_request_identity(
        request,
    )
    if not request_user_id:
        logger.warning("Rejected Console Stop without caller identity")
        return _console_stop_idle_response()
    target_chat_id = await _resolve_console_stop_chat_id(
        workspace,
        coordinator,
        requested_chat_id=target_chat_id,
        session_id=session_id,
        user_id=request_user_id,
        source_id=request_source_id,
    )
    if target_chat_id is None:
        return _console_stop_idle_response()
    chat = await workspace.chat_manager.get_chat(target_chat_id)
    if not _is_authorized_console_stop_chat(
        chat,
        user_id=request_user_id,
        source_id=request_source_id,
    ):
        logger.warning(
            "Rejected Console Stop target chat_id=%s",
            target_chat_id,
        )
        return _console_stop_idle_response()
    accepted, claimed_chat_id, claimed_msgid, status = (
        await _claim_console_stop(
            coordinator,
            target_chat_id,
            msgid,
        )
    )
    if not accepted:
        return _console_stop_idle_response()
    return {
        "stopped": True,
        "accepted": True,
        "status": status,
        "chat_id": claimed_chat_id,
        "msgid": claimed_msgid,
    }


def _save_console_upload_sync(
    data: bytes,
    *,
    media_dir: Path,
    workspace_dir: Path | None,
    stored_name: str,
) -> Path:
    """Persist an uploaded attachment outside the event-loop thread."""
    media_dir.mkdir(parents=True, exist_ok=True)
    path = (media_dir / stored_name).resolve()
    path.write_bytes(data)
    if workspace_dir is None:
        return path

    workspace_media_dir = (workspace_dir / "media").resolve()
    workspace_media_dir.relative_to(workspace_dir)
    if workspace_media_dir == media_dir.resolve():
        return path
    workspace_media_dir.mkdir(parents=True, exist_ok=True)
    context_path = workspace_media_dir / stored_name
    context_path.write_bytes(data)
    return context_path


@router.post("/upload", response_model=dict, summary="Upload file for chat")
async def post_console_upload(
    request: Request,
    file: UploadFile = File(..., description="File to attach"),
) -> dict:
    """Save to console channel media_dir."""

    if _has_denied_chat_attachment_extension(file.filename):
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type for chat attachment upload",
        )

    workspace = await get_agent_for_request(request)
    console_channel = await workspace.channel_manager.get_channel("console")
    if console_channel is None:
        raise HTTPException(
            status_code=503,
            detail="Channel Console not found",
        )
    media_dir = console_channel.media_dir
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail="File too large (max "
            f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB)",
        )
    safe_name = _safe_filename(file.filename or "file")
    stored_name = f"{uuid.uuid4().hex}_{safe_name}"
    workspace_dir_value = getattr(workspace, "workspace_dir", None)
    workspace_dir = (
        Path(workspace_dir_value).resolve() if workspace_dir_value else None
    )
    try:
        context_path = await run_file_manager_mutation(
            _save_console_upload_sync,
            data,
            media_dir=media_dir,
            workspace_dir=workspace_dir,
            stored_name=stored_name,
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=500,
            detail="Failed to persist uploaded file",
        ) from exc
    return {
        "url": context_path,
        "file_name": safe_name,
        "size": len(data),
    }


@router.get(
    "/generated-files",
    response_model=GeneratedFilesResponse,
    summary="列出当前聊天工作区相关文件",
)
async def get_console_generated_files(
    request: Request,
    sort: str = Query(
        "desc",
        pattern="^(asc|desc)$",
        description="按修改时间排序：asc 或 desc",
    ),
    source: str = Query(
        "all",
        pattern="^(all|generated|uploaded)$",
        description="文件来源：all、generated 或 uploaded",
    ),
) -> GeneratedFilesResponse:
    """列出当前 Agent 工作区 static 与 media 目录下的聊天相关文件。"""
    workspace = await get_agent_for_request(request)
    workspace_dir = Path(workspace.workspace_dir)
    items: list[GeneratedFileItem] = []
    if source in ("all", "generated"):
        items.extend(
            _collect_chat_files_from_dir(
                (workspace_dir / "static").resolve(),
                "generated",
            ),
        )
    if source in ("all", "uploaded"):
        media_dir = await _resolve_console_media_dir(
            workspace,
            workspace_dir,
        )
        items.extend(
            _collect_chat_files_from_dir(
                media_dir,
                "uploaded",
            ),
        )

    reverse = sort != "asc"
    items.sort(key=lambda item: item.modified_at, reverse=reverse)
    return GeneratedFilesResponse(
        files=items[:_CHAT_FILE_LIST_LIMIT],
    )


async def _get_file_manager_service_for_request(
    request: Request,
) -> FileManagerService:
    """Construct a request-bound service with its tenant source root."""

    workspace_dir = await resolve_file_manager_workspace_dir(request)
    source_scope_location = resolve_file_manager_source_scope_location(request)
    return get_file_manager_service(
        workspace_dir,
        source_scope_base_dir=source_scope_location.base_dir,
        source_scope_component=source_scope_location.component,
    )


@router.get(
    "/file-manager/directories",
    response_model=FileManagerDirectoryListing,
    summary="List a controlled chat file-manager directory",
)
async def get_file_manager_directory(
    request: Request,
    root: str = Query(..., description="Controlled file-manager root"),
    path: str = Query("", description="Relative POSIX directory path"),
    cursor: str | None = Query(None, description="Signed directory cursor"),
    q: str = Query("", max_length=512, description="Direct-child name filter"),
) -> FileManagerDirectoryListing:
    """List direct children for the workspace bound to this request only."""

    service = await _get_file_manager_service_for_request(request)
    try:
        return await run_file_manager_read(
            service.list_directory,
            root,
            path,
            cursor=cursor,
            query=q or None,
        )
    except FileManagerPathError as exc:
        raise _file_manager_http_error(exc) from exc


@router.get(
    "/file-manager/files/read",
    response_model=FileManagerTextPreview,
    summary="Read a bounded controlled text-file preview",
)
async def get_file_manager_file_preview(
    request: Request,
    root: str = Query(..., description="Controlled file-manager root"),
    path: str = Query(..., description="Relative POSIX file path"),
) -> FileManagerTextPreview:
    """Return at most one MiB of UTF-8 text without auditing reads."""

    service = await _get_file_manager_service_for_request(request)
    try:
        return await run_file_manager_read(
            service.read_text_preview,
            root,
            path,
        )
    except FileManagerPathError as exc:
        raise _file_manager_http_error(exc) from exc


@router.get(
    "/file-manager/files/download",
    summary="Download one controlled regular file as an attachment",
)
async def get_file_manager_file_download(
    request: Request,
    root: str = Query(..., description="Controlled file-manager root"),
    path: str = Query(..., description="Relative POSIX file path"),
) -> StreamingResponse:
    """Stream a single regular file from a no-follow descriptor, unaudited."""

    service = await _get_file_manager_service_for_request(request)
    try:
        download = await run_file_manager_read(
            service.open_file_for_download,
            root,
            path,
        )
    except FileManagerPathError as exc:
        raise _file_manager_http_error(exc) from exc
    stream = _FileManagerDownloadStream(
        download.file_descriptor,
        download.size_bytes,
    )
    return _FileManagerDownloadResponse(
        stream,
        headers={
            "Content-Disposition": _file_manager_download_disposition(
                download.filename,
            ),
            "Content-Length": str(download.size_bytes),
        },
    )


@router.put(
    "/file-manager/files/text",
    response_model=FileManagerTextPreview,
    summary="Save one revision-checked controlled text file",
)
async def put_file_manager_text_file(
    request: Request,
    body: FileManagerTextSaveRequest,
) -> FileManagerTextPreview:
    """Save small UTF-8 text only, rejecting stale revisions instead of overwrite."""

    service = await _get_file_manager_service_for_request(request)
    try:
        result = await run_file_manager_mutation(
            service.save_text,
            body.root,
            body.path,
            body.content,
            body.revision,
        )
    except FileManagerPathError as exc:
        _audit_file_manager_mutation(
            request,
            action="save",
            path=body.path,
            outcome="failure",
        )
        raise _file_manager_http_error(exc) from exc
    _audit_file_manager_mutation(
        request,
        action="save",
        path=body.path,
        outcome="success",
    )
    return result


@router.post(
    "/file-manager/files/upload",
    response_model=FileManagerItem,
    summary="Upload a new controlled file without replacing an existing name",
)
async def post_file_manager_upload(
    request: Request,
    file: UploadFile = File(..., description="File to upload"),
    root: str = Query(..., description="Controlled file-manager root"),
    path: str = Query(
        "",
        description="Existing relative destination directory",
    ),
) -> FileManagerItem:
    """Upload to the currently browsed directory, never renaming or replacing."""

    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        _audit_file_manager_mutation(
            request,
            action="upload",
            path=_file_manager_upload_audit_path(path, file.filename or ""),
            outcome="failure",
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)"
            ),
        )
    service = await _get_file_manager_service_for_request(request)
    filename = file.filename or ""
    try:
        item = await run_file_manager_mutation(
            service.upload_stream,
            root,
            path,
            filename,
            file.file,
        )
    except FileManagerUploadTooLargeError as exc:
        _audit_file_manager_mutation(
            request,
            action="upload",
            path=_file_manager_upload_audit_path(path, filename),
            outcome="failure",
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileManagerPathError as exc:
        _audit_file_manager_mutation(
            request,
            action="upload",
            path=path,
            outcome="failure",
        )
        raise _file_manager_http_error(exc) from exc
    _audit_file_manager_mutation(
        request,
        action="upload",
        path=item.path,
        outcome="success",
    )
    return item


@router.delete(
    "/file-manager/files",
    summary="Recoverably archive one controlled regular file",
)
async def delete_file_manager_file(
    request: Request,
    root: str = Query(..., description="Controlled file-manager root"),
    path: str = Query(..., description="Relative regular file path"),
) -> dict[str, str]:
    """Move a file-manager file into the governance recycle-bin archive."""

    service = await _get_file_manager_service_for_request(request)
    try:
        archived = await run_file_manager_mutation(
            service.archive_file,
            root,
            path,
            actor=_file_manager_actor(request),
        )
    except FileManagerPathError as exc:
        _audit_file_manager_mutation(
            request,
            action="archive",
            path=path,
            outcome="failure",
        )
        raise _file_manager_http_error(exc) from exc
    _audit_file_manager_mutation(
        request,
        action="archive",
        path=archived.original_path,
        outcome="success",
    )
    return {
        "archive_item_id": archived.archive_item_id,
        "original_path": archived.original_path,
    }


@router.delete(
    "/file-manager/directories",
    status_code=204,
    summary="Permanently delete one controlled directory",
)
async def delete_file_manager_directory(
    request: Request,
    root: str = Query(..., description="Controlled file-manager root"),
    path: str = Query(..., description="Relative directory path"),
) -> Response:
    """Permanently remove one controlled directory and its contents."""

    service = await _get_file_manager_service_for_request(request)
    try:
        await run_file_manager_mutation(
            service.delete_directory,
            root,
            path,
        )
    except FileManagerPathError as exc:
        _audit_file_manager_mutation(
            request,
            action="delete_directory",
            path=path,
            outcome="failure",
        )
        raise _file_manager_http_error(exc) from exc
    _audit_file_manager_mutation(
        request,
        action="delete_directory",
        path=path,
        outcome="success",
    )
    return Response(status_code=204)


@router.post(
    "/file-manager/recycle/{archive_item_id}/restore",
    summary="Restore one recycle item to its original path without overwrite",
)
async def post_file_manager_recycle_restore(
    request: Request,
    archive_item_id: str,
) -> dict[str, str]:
    """Restore one archive item; a collision leaves the archive untouched."""

    service = await _get_file_manager_service_for_request(request)
    try:
        restored = await run_file_manager_mutation(
            service.restore_recycle_item,
            archive_item_id,
            actor=_file_manager_actor(request),
        )
    except FileManagerPathError as exc:
        _audit_file_manager_mutation(
            request,
            action="restore",
            path=archive_item_id,
            outcome="failure",
        )
        raise _file_manager_http_error(exc) from exc
    _audit_file_manager_mutation(
        request,
        action="restore",
        path=restored.original_path,
        outcome="success",
    )
    return {
        "archive_item_id": restored.archive_item_id,
        "original_path": restored.original_path,
    }


@router.delete(
    "/file-manager/recycle/{archive_item_id}",
    summary="Permanently delete one recycle item",
)
async def delete_file_manager_recycle_item(
    request: Request,
    archive_item_id: str,
) -> dict[str, str]:
    """Remove archived bytes and index entry after the UI confirmation."""

    service = await _get_file_manager_service_for_request(request)
    try:
        purged = await run_file_manager_mutation(
            service.purge_recycle_item,
            archive_item_id,
            actor=_file_manager_actor(request),
        )
    except FileManagerPathError as exc:
        _audit_file_manager_mutation(
            request,
            action="purge",
            path=archive_item_id,
            outcome="failure",
        )
        raise _file_manager_http_error(exc) from exc
    _audit_file_manager_mutation(
        request,
        action="purge",
        path=purged.original_path,
        outcome="success",
    )
    return {
        "archive_item_id": purged.archive_item_id,
        "original_path": purged.original_path,
    }


@router.get(
    "/context-references",
    response_model=ContextReferencesResponse,
    summary="Discover context references for the Console composer",
)
async def get_context_references(
    request: Request,
    q: str = Query("", max_length=512),
) -> ContextReferencesResponse:
    """Return cached, scope-bound Skills, MCP tools, and matching files."""
    workspace, agent_config = await get_agent_and_config_for_request(request)
    workspace_dir = Path(workspace.workspace_dir)
    return await context_reference_directory.discover(
        workspace=workspace,
        agent_config=agent_config,
        query=q,
        media_dir=await _resolve_console_media_dir(workspace, workspace_dir),
    )


@router.get("/push-messages")
async def get_push_messages(
    request: Request,
    session_id: str | None = Query(None, description="Session id"),
):
    """Return pending push messages for the current tenant session.

    If session_id is provided, returns messages for that specific session.
    If session_id is not provided, returns all messages for the tenant.
    """
    from ..console_push_store import take, take_all

    tenant_id = _request_runtime_tenant_id(request)

    if session_id:
        messages = await take(session_id, tenant_id=tenant_id)
    else:
        messages = await take_all(tenant_id=tenant_id)

    return {"messages": messages}


@router.get("/suggestions")
async def get_suggestions(
    request: Request,
    session_id: str = Query(
        ...,
        description="Session id to get suggestions for",
    ),
):
    """Return generated suggestions for the session.

    猜你想问建议在后台异步生成，前端在主响应完成后轮询此接口获取。
    获取后建议会被移除，不会重复返回。
    """
    from ..suggestions import take_suggestions

    tenant_id = _request_runtime_tenant_id(request)
    suggestions = await take_suggestions(session_id, tenant_id=tenant_id)
    return {"suggestions": suggestions}


class QAContentRequest(BaseModel):
    """Q&A 内容请求模型."""

    chat_id: str = Field(..., description="Chat id (backend chat.id)")
    user_message: str = Field(..., description="User message text")


class QAContentResponse(BaseModel):
    """Q&A 内容响应模型."""

    success: bool = Field(..., description="Whether Q&A content was found")
    qa_content: Optional[Dict[str, str]] = Field(
        default=None,
        description="Extracted Q&A content (user_message, assistant_response)",
    )


@router.post("/suggestions/qa-content", response_model=QAContentResponse)
async def get_suggestions_qa_content(
    request: Request,
    body: QAContentRequest,
):
    """根据用户问题获取后端提取的 Q&A 内容.

    前端在响应完成后调用此接口，获取后端提取的 Q&A 关键内容，
    用于调用外部 suggestions API。

    Args:
        chat_id: 后端 chat.id（UUID）
        user_message: 用户问题文本（用于匹配）

    Returns:
        success: 是否找到 Q&A 内容
        qa_content: 提取后的用户问题和助手回答（总长度不超过配置上限）
    """
    from ..suggestions import get_qa_content

    tenant_id = _request_runtime_tenant_id(request)

    entry = await get_qa_content(
        chat_id=body.chat_id,
        user_message=body.user_message,
        tenant_id=tenant_id,
    )

    if entry is None:
        return QAContentResponse(
            success=False,
            qa_content=None,
        )

    return QAContentResponse(
        success=True,
        qa_content=entry,
    )
