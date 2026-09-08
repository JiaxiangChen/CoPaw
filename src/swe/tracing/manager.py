# -*- coding: utf-8 -*-
"""Trace manager module.

Provides the TraceManager for event collection, batching, and storage.
"""

import asyncio
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Optional

from .config import TracingConfig
from ..database import DatabaseConnection
from .models import EventType, Span, Trace, TraceStatus
from .store import TraceStore
from .sanitizer import sanitize_dict, sanitize_string

logger = logging.getLogger(__name__)

# Context variable for current trace
_current_trace: ContextVar[Optional["TraceContext"]] = ContextVar(
    "current_trace",
    default=None,
)


class TraceContext:
    """Context for the current trace."""

    def __init__(
        self,
        trace_id: str,
        user_id: str,
        session_id: str,
        channel: str,
        source_id: str,
        user_name: Optional[str] = None,
        bbk_id: Optional[str] = None,
        session_name: Optional[str] = None,
        attached: bool = False,  # 标记是否是 attach 到已存在的 trace
    ):
        self.trace_id = trace_id
        self.user_id = user_id
        self.user_name = user_name
        self.bbk_id = bbk_id
        self.session_id = session_id
        self.session_name = session_name
        self.channel = channel
        self.source_id = source_id
        self.start_time = datetime.now()
        self.trace: Optional[Trace] = None
        self._span_stack: list[str] = []
        self._active_skills: list[str] = []  # Active skill context stack
        self.skill_detector: Optional[Any] = None  # SkillInvocationDetector
        self.enabled_skills: list[str] = []  # Skills enabled for this trace
        self.attached = attached  # 如果为 True，表示不应由当前代码结束 trace

    def push_span(self, span_id: str) -> None:
        """Push a span ID onto the stack."""
        self._span_stack.append(span_id)

    def pop_span(self) -> Optional[str]:
        """Pop a span ID from the stack."""
        return self._span_stack.pop() if self._span_stack else None

    @property
    def current_span_id(self) -> Optional[str]:
        """Get current span ID."""
        return self._span_stack[-1] if self._span_stack else None

    def push_skill(self, skill_name: str) -> None:
        """Push a skill onto the active skill stack."""
        self._active_skills.append(skill_name)

    def pop_skill(self) -> Optional[str]:
        """Pop a skill from the active skill stack."""
        return self._active_skills.pop() if self._active_skills else None

    @property
    def current_skill(self) -> Optional[str]:
        """Get the currently active skill (top of stack)."""
        return self._active_skills[-1] if self._active_skills else None

    @property
    def active_skills(self) -> list[str]:
        """Get all active skills in the stack (copy)."""
        return list(self._active_skills)

    def set_skill_detector(
        self,
        detector: Any,
        enabled_skills: list[str],
    ) -> None:
        """Set the skill invocation detector.

        Args:
            detector: SkillInvocationDetector instance
            enabled_skills: List of enabled skill names
        """
        self.skill_detector = detector
        self.enabled_skills = enabled_skills


def get_current_trace() -> Optional[TraceContext]:
    """Get the current trace context."""
    return _current_trace.get()


def set_current_trace(ctx: Optional[TraceContext]) -> None:
    """Set the current trace context."""
    _current_trace.set(ctx)


def capture_current_trace_context() -> Optional[dict[str, Any]]:
    """捕获当前 trace 的轻量上下文快照。

    返回普通字典而不是 TraceContext 实例，避免调用方把可变上下文对象跨
    协程长期持有后又读到别的运行态。
    """
    ctx = get_current_trace()
    if ctx is None:
        return None
    return {
        "trace_id": ctx.trace_id,
        "user_id": ctx.user_id,
        "session_id": ctx.session_id,
        "channel": ctx.channel,
        "source_id": ctx.source_id,
        "user_name": ctx.user_name,
        "bbk_id": ctx.bbk_id,
    }


class TraceManager:
    """Manager for trace lifecycle and event collection.

    Handles:
    - Trace creation and completion
    - Span creation and updates
    - Batch writing for performance
    - Data sanitization
    """

    def __init__(
        self,
        config: TracingConfig,
        db: Optional[DatabaseConnection] = None,
    ):
        """Initialize trace manager.

        Args:
            config: Tracing configuration
            db: Optional database connection (can also be set during initialize).
                If provided, the connection is considered shared and will not be
                closed by this manager.
        """
        self.config = config
        self._store: Optional[TraceStore] = None
        self._db = db
        self._owns_db = db is None  # Only owns DB if we create it ourselves

        # Batch queue for spans
        self._span_queue: list[Span] = []
        self._span_queue_lock = asyncio.Lock()

        # Pending spans cache (for update before flush)
        self._pending_spans: dict[str, Span] = {}

        # Background flush task
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False

        # Active traces
        self._active_traces: dict[str, Trace] = {}

    @property
    def store(self) -> TraceStore:
        """Get trace store."""
        if self._store is None:
            raise RuntimeError(
                "TraceManager not initialized or tracing is disabled",
            )
        return self._store

    @property
    def enabled(self) -> bool:
        """Check if tracing is enabled."""
        return self.config.enabled

    @property
    def is_running(self) -> bool:
        """Check if the trace manager is running."""
        return self._running

    async def initialize(self) -> None:
        """Initialize the trace manager.

        If database connection is available, uses database storage.
        Otherwise, runs in log-only mode for debugging.
        """
        if not self.config.enabled:
            logger.info("Tracing is disabled")
            return

        # Try to create database connection if not provided
        if self._db is None and self.config.database:
            try:
                self._db = DatabaseConnection(self.config.database)
                await self._db.connect()
                self._owns_db = True  # We created it, so we own it
                logger.info(
                    "Database connection established: %s",
                    self.config.database.host,
                )
            except Exception as e:
                logger.warning(
                    "Failed to connect to database, using log-only mode: %s",
                    e,
                )
                self._db = None

        # Create store (with or without database)
        self._store = TraceStore(self.config, self._db, owns_db=self._owns_db)
        await self._store.initialize()

        # Start flush task only if we have a database
        self._running = True
        if self._db is not None:
            self._flush_task = asyncio.create_task(self._flush_loop())

        # Start cleanup task if retention is configured
        self._cleanup_task: Optional[asyncio.Task] = None
        if self.config.retention_days > 0:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        logger.info(
            "TraceManager initialized (database=%s, mode=%s)",
            self.config.database.host if self.config.database else "N/A",
            "database" if self._db else "log-only",
        )

    async def close(self) -> None:
        """Close the trace manager."""
        self._running = False

        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None

        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

        # Final flush
        await self._flush_spans()

        if self._store:
            await self._store.flush()
            await self._store.close()

        logger.info("TraceManager closed")

    # Trace lifecycle

    async def start_trace(
        self,
        user_id: str,
        session_id: str,
        channel: str,
        source_id: str,
        trace_id: Optional[str] = None,
        user_message: Optional[str] = None,
        user_name: Optional[str] = None,
        bbk_id: Optional[str] = None,
        session_name: Optional[str] = None,
        model_output: Optional[str] = None,
        attach_existing: bool = False,
        b3_trace_id: Optional[str] = None,
    ) -> str:
        """Start a new trace or attach to an existing one.

        Args:
            user_id: User identifier
            session_id: Session identifier
            channel: Channel identifier
            source_id: Source identifier for data isolation
            trace_id: Optional trace ID (generated if not provided)
            user_message: Optional user's input message
            user_name: Optional user name
            bbk_id: Optional BBK identifier
            session_name: Optional session name (derived from first message)
            model_output: Optional model output (for text-type cron jobs)
            attach_existing: If True and trace_id exists in DB, only set context
                without creating new database record
            b3_trace_id: Optional upstream B3 trace identifier

        Returns:
            Trace ID
        """
        if not self.enabled:
            return trace_id or str(uuid.uuid4())

        requested_trace_id = trace_id
        trace_id = trace_id or str(uuid.uuid4())

        # 如果是 attach_existing 模式，检查 trace 是否已存在
        if attach_existing:
            attach_result = await self._handle_attach_existing(
                trace_id,
                user_id,
                session_id,
                channel,
                source_id,
                user_name,
                bbk_id,
                session_name,
            )
            if attach_result == "attached":
                return trace_id
            if attach_result == "identity_mismatch" and requested_trace_id:
                trace_id = str(uuid.uuid4())

        # Sanitize inputs
        user_message, model_output = self._sanitize_inputs(
            user_message,
            model_output,
        )

        # 确定 effective_session_name
        effective_session_name = await self._determine_session_name(
            session_id,
            session_name,
        )

        # 创建 trace 并保存
        trace = Trace(
            trace_id=trace_id,
            b3_trace_id=b3_trace_id,
            source_id=source_id,
            user_id=user_id,
            user_name=user_name,
            bbk_id=bbk_id,
            session_id=session_id,
            session_name=effective_session_name,
            channel=channel,
            start_time=datetime.now(),
            status=TraceStatus.RUNNING,
            user_message=user_message,
            model_output=model_output,
        )

        await self.store.create_trace(trace)
        self._active_traces[trace_id] = trace

        # Create context
        ctx = TraceContext(
            trace_id,
            user_id,
            session_id,
            channel,
            source_id,
            user_name=user_name,
            bbk_id=bbk_id,
            session_name=effective_session_name,
        )
        ctx.trace = trace
        set_current_trace(ctx)

        return trace_id

    @staticmethod
    def _can_attach_to_trace(
        existing_trace: Trace,
        *,
        user_id: str,
        session_id: str,
        channel: str,
        source_id: str,
    ) -> bool:
        """判断请求身份是否允许复用已有 trace。"""
        return (
            (existing_trace.user_id or "") == user_id
            and (existing_trace.session_id or "") == session_id
            and (existing_trace.channel or "") == channel
            and (existing_trace.source_id or "") == source_id
        )

    async def _handle_attach_existing(
        self,
        trace_id: str,
        user_id: str,
        session_id: str,
        channel: str,
        source_id: str,
        user_name: Optional[str],
        bbk_id: Optional[str],
        session_name: Optional[str],
    ) -> Literal["attached", "not_found", "identity_mismatch"]:
        """处理 attach_existing 模式，检查并复用已存在的 trace。

        Args:
            trace_id: Trace ID
            user_id: User ID
            session_id: Session ID
            channel: Channel
            source_id: Source ID
            user_name: User name
            bbk_id: BBK ID
            session_name: Session name

        Returns:
            attached: 成功 attach 到已有 trace
            not_found: 未找到已有 trace，应沿用传入 trace_id 创建
            identity_mismatch: 找到了 trace，但身份不匹配，必须改用新 trace_id
        """
        existing_trace = self._active_traces.get(
            trace_id,
        ) or await self.store.get_trace(trace_id)
        if not existing_trace:
            return "not_found"

        if not self._can_attach_to_trace(
            existing_trace,
            user_id=user_id,
            session_id=session_id,
            channel=channel,
            source_id=source_id,
        ):
            logger.warning(
                "Reject attach_existing for trace_id=%s due to identity "
                "mismatch: incoming(user_id=%s, session_id=%s, channel=%s, "
                "source_id=%s) existing(user_id=%s, session_id=%s, "
                "channel=%s, source_id=%s)",
                trace_id,
                user_id,
                session_id,
                channel,
                source_id,
                existing_trace.user_id or "",
                existing_trace.session_id or "",
                existing_trace.channel or "",
                existing_trace.source_id or "",
            )
            return "identity_mismatch"

        # 仅设置 context，不创建数据库记录
        ctx = TraceContext(
            trace_id,
            existing_trace.user_id or user_id,
            existing_trace.session_id or session_id,
            existing_trace.channel or channel,
            existing_trace.source_id or source_id,
            user_name=user_name or existing_trace.user_name,
            bbk_id=bbk_id or existing_trace.bbk_id,
            session_name=session_name or existing_trace.session_name,
            attached=True,  # 标记为 attach，不应由当前代码结束
        )
        ctx.trace = existing_trace
        set_current_trace(ctx)
        return "attached"

    def _sanitize_inputs(
        self,
        user_message: Optional[str],
        model_output: Optional[str],
    ) -> tuple[Optional[str], Optional[str]]:
        """Sanitize user message and model output.

        Args:
            user_message: User message
            model_output: Model output

        Returns:
            Tuple of sanitized (user_message, model_output)
        """
        if self.config.sanitize_output and user_message:
            user_message = sanitize_string(
                user_message,
                self.config.max_output_length,
            )
        if self.config.sanitize_output and model_output:
            model_output = sanitize_string(
                model_output,
                self.config.max_output_length,
            )
        return user_message, model_output

    async def _determine_session_name(
        self,
        session_id: str,
        session_name: Optional[str],
    ) -> Optional[str]:
        """确定有效的 session_name。

        逻辑：
        1. 新增会话：写入当前消息作为 session_name
        2. 存量会话：查询第一条消息作为 session_name

        Args:
            session_id: Session ID
            session_name: 提供的 session name

        Returns:
            有效的 session_name
        """
        if not session_name:
            return None

        # 检查是否为存量会话（跨所有 source_id 查询）
        has_traces = await self.store.has_session_traces(session_id)
        if not has_traces:
            # 新增会话：写入当前消息作为 session_name
            return session_name

        # 存量会话：查询第一条消息作为 session_name
        first_msg = await self.store.get_session_first_message(session_id)
        if first_msg:
            return first_msg[:10]

        # 如果第一条消息为空，使用当前消息作为会话名称
        return session_name

    async def update_session_name(
        self,
        trace_id: str,
        session_name: str,
    ) -> None:
        """更新 trace 的 session_name（DB + 内存 TraceContext）。

        Args:
            trace_id: Trace 标识
            session_name: 新的会话名称
        """
        await self.store.update_session_name(trace_id, session_name)
        ctx = get_current_trace()
        if ctx and ctx.trace_id == trace_id:
            ctx.session_name = session_name
            if ctx.trace:
                ctx.trace.session_name = session_name

    async def setup_skill_detector(
        self,
        trace_id: str,
        enabled_skills: list[str],
        skill_runtime_profiles: Optional[dict[str, Any]] = None,
        workspace_dir: Optional[Path] = None,
        skill_tool_registry: Optional[Any] = None,
        skill_metadata: Optional[dict[str, Any]] = None,
        skill_dirs: Optional[dict[str, Path]] = None,
        skill_signatures: Optional[dict[str, str]] = None,
    ) -> None:
        """Set up skill invocation detector for a trace.

        This should be called after start_trace() to enable skill
        detection during the trace. Also performs Layer 0 detection
        on the user message if available.

        Args:
            trace_id: Trace identifier
            enabled_skills: List of skill names enabled for this trace
            workspace_dir: Optional workspace directory for reading skill manifest
        """
        if not self.enabled:
            return

        ctx = get_current_trace()
        if not ctx or ctx.trace_id != trace_id:
            return

        try:
            from ..agents.skill_invocation_detector import (
                SkillInvocationDetector,
            )
            from ..agents.skill_context_manager import (
                get_skill_context_manager,
            )
            from ..agents.skill_tool_registry import get_skill_tool_registry

            # Create detector with dependencies
            detector = SkillInvocationDetector(
                registry=skill_tool_registry or get_skill_tool_registry(),
                context_manager=get_skill_context_manager(),
                trace_manager=self,
                trace_id=trace_id,
                user_id=ctx.user_id,
                session_id=ctx.session_id,
                channel=ctx.channel,
                source_id=ctx.source_id,
                user_name=ctx.user_name,
                bbk_id=ctx.bbk_id,
                workspace_dir=workspace_dir,
                skill_dirs=skill_dirs,
                skill_signatures=skill_signatures,
            )
            detector.set_enabled_skills(enabled_skills, skill_metadata)
            if skill_runtime_profiles:
                detector.set_skill_runtime_profiles(skill_runtime_profiles)

            # Attach to context
            ctx.set_skill_detector(detector, enabled_skills)

        except Exception as e:
            logger.warning("Failed to setup skill detector: %s", e)

    async def end_trace(
        self,
        trace_id: str,
        status: TraceStatus = TraceStatus.COMPLETED,
        error: Optional[str] = None,
    ) -> None:
        """End a trace.

        Args:
            trace_id: Trace identifier
            status: Final status
            error: Optional error message
        """
        if not self.enabled:
            return

        # End skill detection before the final span flush because cleanup may
        # update existing spans or emit a final skill span.
        ctx = get_current_trace()
        if ctx and ctx.trace_id == trace_id and ctx.skill_detector:
            try:
                await ctx.skill_detector.on_reasoning_end()
            except Exception as e:
                logger.warning("Failed to end skill detection: %s", e)

        # Flush pending spans before ending trace with retry mechanism
        # 确保所有 spans 写入完成，避免 trace 结束后 spans 丢失
        max_flush_retries = 3
        for retry in range(max_flush_retries):
            await self._flush_spans()
            # 检查是否还有 pending spans（属于当前 trace 的）
            async with self._span_queue_lock:
                pending_for_trace = [
                    s for s in self._span_queue if s.trace_id == trace_id
                ]
            if not pending_for_trace:
                break
            if retry < max_flush_retries - 1:
                logger.warning(
                    "Flush retry %d/%d for trace %s, %d spans pending",
                    retry + 1,
                    max_flush_retries,
                    trace_id[:8],
                    len(pending_for_trace),
                )
                await asyncio.sleep(0.1)  # 短暂等待后重试

        # 最终检查：如果仍有 pending spans，记录错误但不阻塞
        async with self._span_queue_lock:
            final_pending = [
                s for s in self._span_queue if s.trace_id == trace_id
            ]
        if final_pending:
            logger.error(
                "Failed to flush %d spans for trace %s after %d retries. "
                "Spans may be lost: %s",
                len(final_pending),
                trace_id[:8],
                max_flush_retries,
                [s.span_id[:8] for s in final_pending[:5]],
            )

        trace = self._active_traces.pop(
            trace_id,
            None,
        ) or await self.store.get_trace(trace_id)
        if trace is None:
            logger.warning("Trace not found: %s", trace_id)
            return

        trace.end_time = datetime.now()
        trace.duration_ms = int(
            (trace.end_time - trace.start_time).total_seconds() * 1000,
        )
        trace.status = status
        trace.error = error

        await self.store.update_trace(trace)

        # Clear context
        if ctx and ctx.trace_id == trace_id:
            set_current_trace(None)

    # Span operations

    async def emit_span(
        self,
        trace_id: str,
        event_type: EventType,
        name: str,
        source_id: str,
        user_id: str = "",
        session_id: str = "",
        channel: str = "",
        model_name: Optional[str] = None,
        input_tokens: Optional[int] = None,
        tool_name: Optional[str] = None,
        skill_name: Optional[str] = None,
        skill_id: Optional[str] = None,
        tool_input: Optional[dict[str, Any]] = None,
        start_time: Optional[datetime] = None,
        mcp_server: Optional[str] = None,
        user_name: Optional[str] = None,
        bbk_id: Optional[str] = None,
    ) -> str:
        """Emit a new span event.

        Args:
            trace_id: Trace identifier
            event_type: Event type
            name: Span name
            source_id: Source identifier for data isolation
            user_id: User identifier
            session_id: Session identifier
            channel: Channel identifier
            model_name: Optional model name
            input_tokens: Optional input token count
            tool_name: Optional tool name
            skill_name: Optional skill name
            tool_input: Optional tool input (will be sanitized)
            start_time: Optional start time
            mcp_server: Optional MCP server name if this is an MCP tool
            user_name: Optional user name
            bbk_id: Optional BBK identifier

        Returns:
            Span ID
        """
        if not self.enabled:
            return str(uuid.uuid4())

        span_id = str(uuid.uuid4())

        # Sanitize tool input if configured
        if self.config.sanitize_output and tool_input:
            tool_input = sanitize_dict(
                tool_input,
                self.config.max_output_length,
            )

        span = Span(
            span_id=span_id,
            trace_id=trace_id,
            source_id=source_id,
            name=name,
            event_type=event_type,
            start_time=start_time or datetime.now(),
            user_id=user_id,
            user_name=user_name,
            bbk_id=bbk_id,
            session_id=session_id,
            channel=channel,
            model_name=model_name,
            input_tokens=input_tokens,
            tool_name=tool_name,
            skill_name=skill_name,
            skill_id=skill_id,
            tool_input=tool_input,
            mcp_server=mcp_server,
        )
        # Update trace statistics (skills_used, tools_used, input_tokens if > 0)
        self._update_trace_totals(trace_id, span, None, input_tokens)

        # Add to pending cache and queue atomically
        # 在锁内检查是否需要立即 flush，避免锁外检查导致的竞态条件
        need_flush = False
        async with self._span_queue_lock:
            self._pending_spans[span_id] = span
            self._span_queue.append(span)
            if len(self._span_queue) >= self.config.batch_size:
                need_flush = True

        # 在锁外执行 flush，使用 await 确保写入完成
        # 防止 end_trace 时数据尚未写入数据库
        if need_flush:
            await self._flush_spans()

        return span_id

    async def update_span(
        self,
        span_id: str,
        trace_id: str,
        output_tokens: Optional[int] = None,
        input_tokens: Optional[int] = None,
        tool_output: Optional[str] = None,
        error: Optional[str] = None,
        span: Optional[Span] = None,
    ) -> None:
        """Update an existing span.

        Args:
            span_id: Span identifier
            trace_id: Trace identifier
            output_tokens: Optional output token count
            input_tokens: Optional input token count (updates span if provided)
            tool_output: Optional tool output (will be sanitized)
            error: Optional error message
            span: Optional span object (to avoid re-fetching)
        """
        if not self.enabled:
            return

        span = await self._find_span(span_id, trace_id, span)
        if span is None:
            logger.warning("Span not found for update: %s", span_id)
            return
        if span.trace_id != trace_id:
            logger.error(
                "Cross-trace span update rejected: span_id=%s "
                "requested_trace_id=%s actual_trace_id=%s",
                span_id,
                trace_id,
                span.trace_id,
            )
            return

        self._update_span_fields(
            span,
            output_tokens,
            input_tokens,
            tool_output,
            error,
        )
        self._update_trace_totals(
            span.trace_id,
            span,
            output_tokens,
            input_tokens,
            is_update=True,
        )

        # Persist if not in pending cache
        if span_id not in self._pending_spans:
            await self.store.update_span(span)

    async def _find_span(
        self,
        span_id: str,
        trace_id: str,
        span: Optional[Span],
    ) -> Optional[Span]:
        """Find span from cache or store."""
        if span is not None:
            return span

        # Check pending cache first
        span = self._pending_spans.get(span_id)
        if span is not None:
            return span

        # Check store
        spans = await self.store.get_spans(trace_id)
        for s in spans:
            if s.span_id == span_id:
                return s

        return None

    def _update_span_fields(
        self,
        span: Span,
        output_tokens: Optional[int],
        input_tokens: Optional[int],
        tool_output: Optional[str],
        error: Optional[str],
    ) -> None:
        """Update span fields."""
        span.end_time = datetime.now()
        span.duration_ms = int(
            (span.end_time - span.start_time).total_seconds() * 1000,
        )
        span.output_tokens = output_tokens
        if input_tokens is not None and input_tokens > 0:
            span.input_tokens = input_tokens
        span.tool_output = (
            sanitize_string(tool_output, self.config.max_output_length)
            if self.config.sanitize_output
            else tool_output
        )
        span.error = error

    def _update_trace_totals(
        self,
        trace_id: str,
        span: Span,
        output_tokens: Optional[int] = None,
        input_tokens: Optional[int] = None,
        is_update: bool = False,
    ) -> None:
        """Update trace statistics from span.

        Args:
            trace_id: Trace identifier
            span: Span object
            output_tokens: Output token count (累加传入值)
            input_tokens: Input token count (累加传入值，仅在 update 时有值)
            is_update: If True, this is an update to existing span
        """
        trace = self._active_traces.get(trace_id)
        if not trace:
            return

        if output_tokens:
            trace.total_output_tokens += output_tokens
        # input_tokens 累加传入参数值（非 span 字段），避免重复累加
        if input_tokens and input_tokens > 0:
            trace.total_input_tokens += input_tokens
        # 只在 trace.model_name 为空时设置，保留第一个模型作为主模型
        # 避免用户切换活跃模型后覆盖 trace 的 model_name
        if span.model_name and not trace.model_name:
            trace.model_name = span.model_name
        if span.tool_name and span.tool_name not in trace.tools_used:
            trace.tools_used.append(span.tool_name)
        if span.skill_name and span.skill_name not in trace.skills_used:
            trace.skills_used.append(span.skill_name)

    async def emit_llm_input(
        self,
        trace_id: str,
        model_name: str,
        input_tokens: int,
        source_id: str,
        user_id: str = "",
        session_id: str = "",
        channel: str = "",
        user_name: Optional[str] = None,
        bbk_id: Optional[str] = None,
    ) -> str:
        """Emit LLM input event.

        Args:
            trace_id: Trace identifier
            model_name: Model name
            input_tokens: Input token count
            source_id: Source identifier for data isolation
            user_id: User identifier
            session_id: Session identifier
            channel: Channel identifier
            user_name: Optional user name
            bbk_id: Optional BBK identifier

        Returns:
            Span ID
        """
        return await self.emit_span(
            trace_id=trace_id,
            event_type=EventType.LLM_INPUT,
            name=f"llm_call_{model_name}",
            source_id=source_id,
            user_id=user_id,
            session_id=session_id,
            channel=channel,
            model_name=model_name,
            input_tokens=input_tokens,
            user_name=user_name,
            bbk_id=bbk_id,
        )

    async def emit_llm_output(
        self,
        trace_id: str,
        span_id: str,
        output_tokens: int,
        input_tokens: int = 0,
    ) -> None:
        """Emit LLM output event (updates the span).

        Args:
            trace_id: Trace identifier
            span_id: Span identifier from LLM input
            output_tokens: Output token count
            input_tokens: Input token count (updates span if provided)
        """
        await self.update_span(
            span_id=span_id,
            trace_id=trace_id,
            output_tokens=output_tokens,
            input_tokens=input_tokens if input_tokens > 0 else None,
        )

    async def emit_tool_call_start(
        self,
        trace_id: str,
        tool_name: str,
        tool_input: Optional[dict[str, Any]],
        source_id: str,
        user_id: str = "",
        session_id: str = "",
        channel: str = "",
        mcp_server: Optional[str] = None,
        user_name: Optional[str] = None,
        bbk_id: Optional[str] = None,
        use_precomputed_attribution: bool = False,
        precomputed_attribution: Optional[dict[str, Any]] = None,
    ) -> str:
        """Emit tool call start event with multi-skill attribution.

        This method uses the SkillInvocationDetector to determine skill
        attribution, which combines explicit declarations and inference
        for comprehensive coverage.

        Args:
            trace_id: Trace identifier
            tool_name: Tool name
            tool_input: Tool input
            source_id: Source identifier for data isolation
            user_id: User identifier
            session_id: Session identifier
            channel: Channel identifier
            mcp_server: Optional MCP server name if this is an MCP tool
            user_name: Optional user name
            bbk_id: Optional BBK identifier

        Returns:
            Span ID
        """
        # Determine skill attribution using detector
        ctx = get_current_trace()
        primary_skill: Optional[str] = None
        skill_id: Optional[str] = None

        if ctx and ctx.trace_id == trace_id:
            try:
                # Use the detector if available on context
                detector = getattr(ctx, "skill_detector", None)
                if detector:
                    if use_precomputed_attribution:
                        primary_skill = (precomputed_attribution or {}).get(
                            "primary_skill",
                        )
                    else:
                        primary_skill, _ = await detector.on_tool_call(
                            tool_name=tool_name,
                            tool_input=tool_input or {},
                            mcp_server=mcp_server,
                        )
                    if primary_skill and hasattr(detector, "_skill_ids"):
                        skill_id = detector._skill_ids.get(primary_skill)
                    primary_skill = self._resolve_skill_name_for_tool_span(
                        detector=detector,
                        primary_skill=primary_skill,
                        tool_name=tool_name,
                        tool_input=tool_input or {},
                    )
                else:
                    # Fallback to registry-based attribution
                    from ..agents.skill_tool_registry import (
                        get_skill_tool_registry,
                    )

                    registry = get_skill_tool_registry()

                    declared_skills = registry.get_skills_for_tool(tool_name)
                    active_skills = ctx.active_skills
                    all_skills = list(set(active_skills + declared_skills))

                    if all_skills:
                        primary_skill = sorted(all_skills)[0]
            except Exception as e:
                logger.warning("Failed to resolve skill attribution: %s", e)

        return await self.emit_span(
            trace_id=trace_id,
            event_type=EventType.TOOL_CALL_START,
            name=f"tool_{tool_name}",
            user_id=user_id,
            session_id=session_id,
            channel=channel,
            source_id=source_id,
            tool_name=tool_name,
            tool_input=tool_input,
            mcp_server=mcp_server,
            skill_name=primary_skill,
            skill_id=skill_id,
            user_name=user_name,
            bbk_id=bbk_id,
        )

    @staticmethod
    def _resolve_skill_name_for_tool_span(
        *,
        detector: Any,
        primary_skill: Optional[str],
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> Optional[str]:
        """仅在 tracing 写出层过滤 tool span 的 skill_name。"""
        if not primary_skill:
            return None

        skill_from_md_read = getattr(
            detector,
            "_detect_skill_from_skill_md_read",
            None,
        )
        if (
            callable(skill_from_md_read)
            and skill_from_md_read(
                tool_name,
                tool_input,
            )
            == primary_skill
        ):
            return primary_skill

        profile = None
        getter = getattr(detector, "get_skill_runtime_profile", None)
        if callable(getter):
            profile = getter(primary_skill)
        elif hasattr(detector, "_skill_runtime_profiles"):
            profile = getattr(detector, "_skill_runtime_profiles", {}).get(
                primary_skill,
            )

        if profile is not None and bool(
            getattr(profile, "has_hook_config", False),
        ):
            return None

        return primary_skill

    async def emit_tool_call_end(
        self,
        trace_id: str,
        span_id: str,
        tool_output: Optional[str],
        error: Optional[str] = None,
    ) -> None:
        """Emit tool call end event.

        Args:
            trace_id: Trace identifier
            span_id: Span identifier from tool call start
            tool_output: Tool output
            error: Optional error message
        """
        # First, find the span (in pending cache or store)
        span = self._pending_spans.get(span_id)
        if span is None:
            spans = await self.store.get_spans(trace_id)
            for s in spans:
                if s.span_id == span_id:
                    span = s
                    break

        if span is None:
            logger.warning("Span not found for tool_call_end: %s", span_id)
            return

        # Update event_type to TOOL_CALL_END for proper statistics
        span.event_type = EventType.TOOL_CALL_END
        # Update other fields, passing the span object to avoid re-fetching
        await self.update_span(
            span_id=span_id,
            trace_id=trace_id,
            tool_output=tool_output,
            error=error,
            span=span,
        )

    async def emit_skill_invocation(
        self,
        trace_id: str,
        skill_name: str,
        source_id: str,
        user_id: str = "",
        session_id: str = "",
        channel: str = "",
        skill_input: Optional[dict[str, Any]] = None,
        user_name: Optional[str] = None,
        bbk_id: Optional[str] = None,
        skill_id: Optional[str] = None,
    ) -> str:
        """Emit skill invocation event.

        Args:
            trace_id: Trace identifier
            skill_name: Skill name
            source_id: Source identifier for data isolation
            user_id: User identifier
            session_id: Session identifier
            channel: Channel identifier
            skill_input: Optional skill input parameters
            user_name: Optional user name
            bbk_id: Optional BBK identifier
            skill_id: Optional skill unique identifier

        Returns:
            Span ID
        """
        return await self.emit_span(
            trace_id=trace_id,
            event_type=EventType.SKILL_INVOCATION,
            name=f"skill_{skill_name}",
            source_id=source_id,
            user_id=user_id,
            session_id=session_id,
            channel=channel,
            skill_name=skill_name,
            skill_id=skill_id,
            tool_input=skill_input,
            user_name=user_name,
            bbk_id=bbk_id,
        )

    async def end_skill_invocation(
        self,
        trace_id: str,
        span_id: str,
        skill_output: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """End skill invocation event.

        Args:
            trace_id: Trace identifier
            span_id: Span identifier from skill invocation
            skill_output: Optional skill output
            error: Optional error message
        """
        await self.update_span(
            span_id=span_id,
            trace_id=trace_id,
            tool_output=skill_output,
            error=error,
        )

    # Background flush

    async def _flush_loop(self) -> None:
        """Background loop for flushing queued spans."""
        while self._running:
            try:
                await asyncio.sleep(self.config.flush_interval)
                await self._flush_spans()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in flush loop: %s", e)

    async def _flush_spans(self) -> None:
        """Flush queued spans to storage.

        Handles race condition with update_span and ensures data integrity:
        1. Keep spans in _pending_spans during INSERT
        2. After successful INSERT, check for spans updated during INSERT
        3. UPDATE those spans that were modified during the race window
        4. Only clear _pending_spans after successful write
        5. On failure, re-queue spans for retry (avoid data loss)
        """
        async with self._span_queue_lock:
            if not self._span_queue:
                return
            spans = self._span_queue.copy()
            self._span_queue.clear()
            # Record span IDs that were originally in queue (without end_time)
            # for later detection of updates during INSERT
            spans_before_insert = {
                span.span_id: span.end_time for span in spans
            }

        if not spans:
            return

        flush_success = False
        try:
            if self._db is None:
                self._flush_spans_log_only(spans)
                flush_success = True
            else:
                rowcount = await self.store.batch_create_spans(spans)
                # 验证写入是否成功（至少写入了一部分数据）
                if rowcount > 0:
                    await self._update_spans_modified_during_flush(
                        spans,
                        spans_before_insert,
                    )
                    flush_success = True
                else:
                    logger.warning(
                        "batch_create_spans returned 0 rows, treating as failure",
                    )
        except Exception as e:
            logger.error(
                "Failed to flush spans (will retry later): %s. "
                "Affected %d spans: %s",
                e,
                len(spans),
                [s.span_id[:8] for s in spans[:5]],  # 只显示前 5 个 span_id
            )

        # 只有写入成功时才清除 _pending_spans
        # 写入失败时将 spans 重新放回队列，等待下次 flush 重试
        async with self._span_queue_lock:
            if flush_success:
                for span in spans:
                    self._pending_spans.pop(span.span_id, None)
            else:
                # 将失败的 spans 重新放回队列头部，优先重试
                # 避免与新 spans 混在一起导致顺序混乱
                for span in reversed(spans):
                    self._span_queue.insert(0, span)
                logger.info(
                    "Re-queued %d failed spans for retry",
                    len(spans),
                )

    def _flush_spans_log_only(self, spans: list["Span"]) -> None:
        """Log skill-related spans in log-only mode."""
        for span in spans:
            if span.skill_name:
                logger.info(
                    "[SKILL SPAN] skill='%s', type=%s",
                    span.skill_name,
                    (
                        span.event_type.value
                        if hasattr(span.event_type, "value")
                        else span.event_type
                    ),
                )

    async def _update_spans_modified_during_flush(
        self,
        spans: list["Span"],
        spans_before_insert: dict[str, Optional["datetime"]],
    ) -> None:
        """UPDATE spans that were modified during INSERT race window.

        Args:
            spans: List of spans that were flushed
            spans_before_insert: Dict mapping span_id to original end_time
        """
        for span in spans:
            # If span has end_time now but didn't before, it was updated
            if (
                span.end_time is not None
                and spans_before_insert.get(span.span_id) is None
            ):
                try:
                    await self.store.update_span(span)
                except Exception as update_error:
                    logger.warning(
                        "Failed to update span %s after flush: %s",
                        span.span_id,
                        update_error,
                    )

    async def _cleanup_loop(self) -> None:
        """Background loop for cleaning up old trace data."""
        # Run cleanup once per day (at startup and then every 24 hours)
        while self._running:
            try:
                # Initial cleanup on startup
                await self._cleanup_old_data()

                # Wait 24 hours between cleanups
                await asyncio.sleep(24 * 60 * 60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in cleanup loop: %s", e)
                # Wait 1 hour before retry on error
                await asyncio.sleep(60 * 60)

    async def _cleanup_old_data(self) -> None:
        """Clean up trace data older than retention period."""
        if self.config.retention_days <= 0:
            return

        try:
            cutoff_date = datetime.now() - timedelta(
                days=self.config.retention_days,
            )
            logger.info(
                "Cleaning up trace data older than %s (retention: %d days)",
                cutoff_date.strftime("%Y-%m-%d"),
                self.config.retention_days,
            )

            # Clean up database data older than retention period
            await self.store.cleanup_old_data(cutoff_date)

        except Exception as e:
            logger.error("Failed to cleanup old data: %s", e)


# Global trace manager instance
_trace_manager: Optional[TraceManager] = None


def get_trace_manager() -> TraceManager:
    """Get the global trace manager.

    Raises:
        RuntimeError: If trace manager not initialized

    Returns:
        TraceManager instance
    """
    if _trace_manager is None:
        raise RuntimeError(
            "TraceManager not initialized. Call init_trace_manager() first.",
        )
    return _trace_manager


def has_trace_manager() -> bool:
    """Check if trace manager is initialized."""
    return _trace_manager is not None


async def init_trace_manager(
    config: Optional[TracingConfig] = None,
    db: Optional[DatabaseConnection] = None,
) -> TraceManager:
    """Initialize the global trace manager.

    Args:
        config: Optional tracing configuration (uses defaults if not provided)
        db: Optional database connection

    Returns:
        TraceManager instance
    """
    global _trace_manager

    if _trace_manager is not None:
        return _trace_manager

    config = config or TracingConfig()
    manager = TraceManager(config, db)
    try:
        await manager.initialize()
        _trace_manager = manager
    except Exception as e:
        # Clean up on failure - don't leave partially initialized manager
        logger.error("Failed to initialize TraceManager: %s", e)
        if manager.is_running:
            await manager.close()
        _trace_manager = None
        raise

    return _trace_manager


async def close_trace_manager() -> None:
    """Close the global trace manager."""
    global _trace_manager

    if _trace_manager is not None:
        await _trace_manager.close()
        _trace_manager = None
