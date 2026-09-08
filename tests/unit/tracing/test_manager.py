# -*- coding: utf-8 -*-
"""Tests for TraceManager and TraceContext."""

# pylint: disable=protected-access,redefined-outer-name,unused-import

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swe.agents.skill_tool_registry import SkillToolRegistry
from swe.tracing.config import TracingConfig
from swe.tracing.manager import (
    TraceContext,
    TraceManager,
    get_current_trace,
    set_current_trace,
    get_trace_manager,
    init_trace_manager,
    close_trace_manager,
    has_trace_manager,
)
from swe.tracing.models import EventType, TraceStatus
from swe.database.config import DatabaseConfig


@pytest.fixture(autouse=True)
def reset_global_manager():
    """Reset global trace manager before and after each test."""
    import swe.tracing.manager as manager_module

    manager_module._trace_manager = None  # pylint: disable=protected-access
    yield
    manager_module._trace_manager = None  # pylint: disable=protected-access


@pytest.fixture
def enabled_config():
    """Create enabled tracing config."""
    return TracingConfig(enabled=True, batch_size=10, flush_interval=1)


@pytest.fixture
def disabled_config():
    """Create disabled tracing config."""
    return TracingConfig(enabled=False)


@pytest.fixture
def mock_db_config():
    """Create mock database config."""
    return DatabaseConfig(
        host="localhost",
        port=3306,
        user="test",
        password="test",
        database="test_db",
    )


@pytest.fixture
def mock_db():
    """Create mock database connection."""
    db = MagicMock()
    db.is_connected = True
    db.config = DatabaseConfig(host="localhost", port=3306, database="test")
    db.fetch_one = AsyncMock(return_value=None)
    db.fetch_all = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value=1)
    db.execute_many = AsyncMock(side_effect=lambda _q, params: len(params))
    return db


class TestTraceContext:
    """Tests for TraceContext class."""

    def test_creation(self):
        """Test creating TraceContext."""
        ctx = TraceContext(
            trace_id="trace-1",
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        assert ctx.trace_id == "trace-1"
        assert ctx.user_id == "user-1"
        assert ctx.session_id == "session-1"
        assert ctx.channel == "console"
        assert ctx.source_id == "default"
        assert ctx.trace is None

    def test_span_stack(self):
        """Test span stack operations."""
        ctx = TraceContext(
            trace_id="trace-1",
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        # Initially empty
        assert ctx.current_span_id is None

        # Push spans
        ctx.push_span("span-1")
        assert ctx.current_span_id == "span-1"

        ctx.push_span("span-2")
        assert ctx.current_span_id == "span-2"

        # Pop spans
        assert ctx.pop_span() == "span-2"
        assert ctx.current_span_id == "span-1"

        assert ctx.pop_span() == "span-1"
        assert ctx.current_span_id is None

    def test_pop_empty_stack(self):
        """Test popping from empty stack returns None."""
        ctx = TraceContext(
            trace_id="trace-1",
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        assert ctx.pop_span() is None


class TestCurrentTraceContext:
    """Tests for get_current_trace and set_current_trace."""

    def test_get_default_none(self):
        """Test get_current_trace returns None by default."""
        assert get_current_trace() is None

    def test_set_and_get(self):
        """Test set_current_trace and get_current_trace."""
        ctx = TraceContext(
            trace_id="trace-1",
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        set_current_trace(ctx)
        assert get_current_trace() is ctx

        set_current_trace(None)
        assert get_current_trace() is None

    @pytest.mark.asyncio
    async def test_create_task_inherits_trace_context_snapshot(self):
        """子任务会继承创建瞬间的 trace context 快照，便于排查串链。"""
        trace_a = TraceContext(
            trace_id="trace-a",
            user_id="user-a",
            session_id="session-a",
            channel="console",
            source_id="source-a",
        )
        trace_b = TraceContext(
            trace_id="trace-b",
            user_id="user-b",
            session_id="session-b",
            channel="console",
            source_id="source-b",
        )

        gate = asyncio.Event()
        observed: dict[str, str | None] = {}

        async def read_trace_after_switch() -> None:
            await gate.wait()
            ctx = get_current_trace()
            observed["trace_id"] = ctx.trace_id if ctx else None

        set_current_trace(trace_a)
        task = asyncio.create_task(read_trace_after_switch())
        set_current_trace(trace_b)
        gate.set()
        await task

        assert observed["trace_id"] == "trace-a"
        assert get_current_trace() is trace_b

        set_current_trace(None)


class TestTraceManager:
    """Tests for TraceManager class."""

    def test_creation_disabled(self, disabled_config):
        """Test TraceManager creation with disabled config."""
        manager = TraceManager(disabled_config)

        assert manager.enabled is False
        assert manager.config == disabled_config

    def test_creation_enabled(self, enabled_config):
        """Test TraceManager creation with enabled config."""
        manager = TraceManager(enabled_config)

        assert manager.enabled is True

    @pytest.mark.asyncio
    async def test_initialize_disabled(self, disabled_config):
        """Test initializing disabled manager."""
        manager = TraceManager(disabled_config)
        await manager.initialize()

        # Should not create store when disabled
        assert manager._store is None  # pylint: disable=protected-access

    @pytest.mark.asyncio
    async def test_initialize_enabled_with_db(self, enabled_config, mock_db):
        """Test initializing enabled manager with provided database."""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        assert manager._store is not None  # pylint: disable=protected-access
        assert manager._running is True  # pylint: disable=protected-access

        await manager.close()

    @pytest.mark.asyncio
    async def test_close(self, enabled_config, mock_db):
        """Test closing manager."""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()
        await manager.close()

        assert manager._running is False  # pylint: disable=protected-access
        assert manager._flush_task is None  # pylint: disable=protected-access

    @pytest.mark.asyncio
    async def test_start_trace_disabled(self, disabled_config):
        """Test start_trace when disabled returns a UUID."""
        manager = TraceManager(disabled_config)

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        assert trace_id is not None
        # Should not create active trace
        assert (
            trace_id not in manager._active_traces
        )  # pylint: disable=protected-access

    @pytest.mark.asyncio
    async def test_start_trace_enabled(self, enabled_config, mock_db):
        """Test start_trace when enabled."""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
            user_message="Hello",
        )

        assert trace_id is not None
        assert (
            trace_id in manager._active_traces
        )  # pylint: disable=protected-access
        assert get_current_trace() is not None

        await manager.close()

    @pytest.mark.asyncio
    async def test_start_trace_persists_b3_trace_id(
        self,
        enabled_config,
        mock_db,
    ):
        """A new trace keeps execution and upstream B3 identities distinct."""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        try:
            trace_id = await manager.start_trace(
                user_id="user-1",
                session_id="session-1",
                channel="console",
                source_id="default",
                trace_id="execution-trace-id",
                b3_trace_id="upstream-b3-trace-id",
            )

            trace = manager._active_traces[trace_id]
            assert trace.trace_id == "execution-trace-id"
            assert trace.b3_trace_id == "upstream-b3-trace-id"
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_attach_existing_trace_with_matching_identity(
        self,
        enabled_config,
        mock_db,
    ):
        """Matching attach_existing request should reuse the trace."""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="source-1",
        )

        attached_trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="source-1",
            trace_id=trace_id,
            attach_existing=True,
        )

        assert attached_trace_id == trace_id
        assert get_current_trace() is not None
        assert get_current_trace().trace_id == trace_id
        assert get_current_trace().attached is True

        await manager.close()

    @pytest.mark.asyncio
    async def test_attach_existing_trace_with_mismatched_identity_uses_new_id(
        self,
        enabled_config,
        mock_db,
    ):
        """Mismatched attach_existing request must not reuse the old trace."""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="source-1",
        )

        new_trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-2",
            channel="console",
            source_id="source-1",
            trace_id=trace_id,
            attach_existing=True,
        )

        assert new_trace_id != trace_id
        assert get_current_trace() is not None
        assert get_current_trace().trace_id == new_trace_id
        assert get_current_trace().session_id == "session-2"
        assert get_current_trace().attached is False
        assert trace_id in manager._active_traces
        assert new_trace_id in manager._active_traces

        await manager.close()

    @pytest.mark.asyncio
    async def test_attach_existing_trace_not_found_keeps_requested_id(
        self,
        enabled_config,
        mock_db,
    ):
        """Missing attach target should still reuse caller-provided trace_id."""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        requested_trace_id = "external-trace-id"
        new_trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="source-1",
            trace_id=requested_trace_id,
            attach_existing=True,
        )

        assert new_trace_id == requested_trace_id
        assert get_current_trace() is not None
        assert get_current_trace().trace_id == requested_trace_id
        assert get_current_trace().attached is False
        assert requested_trace_id in manager._active_traces

        await manager.close()

    @pytest.mark.asyncio
    async def test_end_trace(self, enabled_config, mock_db):
        """Test end_trace updates trace status."""
        mock_db.fetch_one.return_value = {
            "trace_id": "test-trace",
            "user_id": "user-1",
            "session_id": "session-1",
            "channel": "console",
            "start_time": datetime.now(),
            "end_time": None,
            "duration_ms": None,
            "status": "running",
            "user_message": None,
            "error": None,
            "model_name": None,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "tools_used": "[]",
            "skills_used": "[]",
        }
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        await manager.end_trace(trace_id, TraceStatus.COMPLETED)

        assert (
            trace_id not in manager._active_traces
        )  # pylint: disable=protected-access

        await manager.close()

    @pytest.mark.asyncio
    async def test_end_trace_flushes_spans_emitted_by_skill_detector(
        self,
        enabled_config,
        mock_db,
    ):
        """技能检测收尾产生的 span 也必须在 trace 收尾前落库。"""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        class Detector:
            async def on_reasoning_end(self):
                await manager.emit_skill_invocation(
                    trace_id=trace_id,
                    skill_name="pdf",
                    source_id="default",
                )

        ctx = get_current_trace()
        assert ctx is not None
        ctx.skill_detector = Detector()

        await manager.end_trace(trace_id, TraceStatus.COMPLETED)

        assert len(manager._span_queue) == 0
        mock_db.execute_many.assert_called_once()
        _, params_list = mock_db.execute_many.call_args.args
        assert len(params_list) == 1
        assert params_list[0][1] == trace_id
        assert params_list[0][15] == "pdf"

        await manager.close()

    @pytest.mark.asyncio
    async def test_emit_span_disabled(self, disabled_config):
        """Test emit_span when disabled returns a UUID."""
        manager = TraceManager(disabled_config)

        span_id = await manager.emit_span(
            trace_id="trace-1",
            event_type=EventType.LLM_INPUT,
            name="test_span",
            source_id="default",
        )

        assert span_id is not None

    @pytest.mark.asyncio
    async def test_emit_span_enabled(self, enabled_config, mock_db):
        """Test emit_span when enabled."""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        span_id = await manager.emit_span(
            trace_id=trace_id,
            event_type=EventType.LLM_INPUT,
            name="llm_call_gpt-4",
            model_name="gpt-4",
            input_tokens=100,
            source_id="default",
        )

        assert span_id is not None

        # Check span is in queue
        assert (
            span_id in manager._pending_spans
        )  # pylint: disable=protected-access

        await manager.close()

    @pytest.mark.asyncio
    async def test_emit_llm_input(self, enabled_config, mock_db):
        """Test emit_llm_input convenience method."""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        span_id = await manager.emit_llm_input(
            trace_id=trace_id,
            model_name="gpt-4",
            input_tokens=100,
            source_id="default",
            user_id="user-1",
            session_id="session-1",
            channel="console",
        )

        assert span_id is not None
        span = manager._pending_spans[
            span_id
        ]  # pylint: disable=protected-access
        assert span.event_type == EventType.LLM_INPUT
        assert span.model_name == "gpt-4"
        assert span.input_tokens == 100

        await manager.close()

    @pytest.mark.asyncio
    async def test_emit_llm_output(self, enabled_config, mock_db):
        """Test emit_llm_output updates span."""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        span_id = await manager.emit_llm_input(
            trace_id=trace_id,
            model_name="gpt-4",
            input_tokens=100,
            source_id="default",
        )

        await manager.emit_llm_output(
            trace_id=trace_id,
            span_id=span_id,
            output_tokens=200,
        )

        span = manager._pending_spans[
            span_id
        ]  # pylint: disable=protected-access
        assert span.output_tokens == 200
        assert span.duration_ms is not None

        await manager.close()

    @pytest.mark.asyncio
    async def test_emit_tool_call(self, enabled_config, mock_db):
        """Test tool call start and end."""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        span_id = await manager.emit_tool_call_start(
            trace_id=trace_id,
            tool_name="browser_control",
            tool_input={"url": "https://example.com"},
            source_id="default",
        )

        assert span_id is not None
        span = manager._pending_spans[
            span_id
        ]  # pylint: disable=protected-access
        assert span.event_type == EventType.TOOL_CALL_START
        assert span.tool_name == "browser_control"

        await manager.emit_tool_call_end(
            trace_id=trace_id,
            span_id=span_id,
            tool_output="success",
        )

        span = manager._pending_spans[
            span_id
        ]  # pylint: disable=protected-access
        assert span.event_type == EventType.TOOL_CALL_END
        assert span.tool_output == "success"

        await manager.close()

    @pytest.mark.asyncio
    async def test_emit_tool_call_with_error(self, enabled_config, mock_db):
        """Test tool call end with error."""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        span_id = await manager.emit_tool_call_start(
            trace_id=trace_id,
            tool_name="browser_control",
            tool_input={"url": "https://example.com"},
            source_id="default",
        )

        await manager.emit_tool_call_end(
            trace_id=trace_id,
            span_id=span_id,
            tool_output=None,
            error="Connection failed",
        )

        span = manager._pending_spans[
            span_id
        ]  # pylint: disable=protected-access
        assert span.error == "Connection failed"

        await manager.close()

    @pytest.mark.asyncio
    async def test_emit_skill_invocation(self, enabled_config, mock_db):
        """Test skill invocation."""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        span_id = await manager.emit_skill_invocation(
            trace_id=trace_id,
            skill_name="pdf",
            source_id="default",
        )

        assert span_id is not None
        span = manager._pending_spans[
            span_id
        ]  # pylint: disable=protected-access
        assert span.event_type == EventType.SKILL_INVOCATION
        assert span.skill_name == "pdf"

        await manager.close()

    @pytest.mark.asyncio
    async def test_emit_mcp_tool(self, enabled_config, mock_db):
        """Test MCP tool call."""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        span_id = await manager.emit_tool_call_start(
            trace_id=trace_id,
            tool_name="get_weather",
            tool_input={"city": "Beijing"},
            mcp_server="weather-server",
            source_id="default",
        )

        span = manager._pending_spans[
            span_id
        ]  # pylint: disable=protected-access
        assert span.mcp_server == "weather-server"

        await manager.close()

    @pytest.mark.asyncio
    async def test_emit_tool_call_filters_hook_skill_from_span(
        self,
        enabled_config,
        mock_db,
    ):
        """hook 运行时技能仍可被识别，但不应写入 tool span skill_name。"""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        class FakeDetector:
            def __init__(self):
                self._skill_runtime_profiles = {
                    "hook-http-demo": type(
                        "Profile",
                        (),
                        {"has_hook_config": True},
                    )(),
                }

            async def on_tool_call(self, **kwargs):
                return "hook-http-demo", {"hook-http-demo": 1.0}

            def get_skill_description(self, skill_name):
                return f"desc:{skill_name}"

            def get_skill_runtime_profile(self, skill_name):
                return self._skill_runtime_profiles.get(skill_name)

        from swe.tracing.manager import get_current_trace

        ctx = get_current_trace()
        assert ctx is not None
        ctx.set_skill_detector(FakeDetector(), ["hook-http-demo"])

        span_id = await manager.emit_tool_call_start(
            trace_id=trace_id,
            tool_name="execute_shell_command",
            tool_input={"command": "echo hello"},
            source_id="default",
        )

        span = manager._pending_spans[
            span_id
        ]  # pylint: disable=protected-access
        assert span.skill_name is None
        # skill_description 字段已从 Span 模型移除，不再写入 span
        assert not hasattr(span, "skill_description")

        await manager.close()

    @pytest.mark.asyncio
    async def test_emit_tool_call_keeps_hook_skill_md_read_in_span(
        self,
        enabled_config,
        mock_db,
    ):
        """hook 技能读取自身 SKILL.md 时，tool span 仍应保留 skill_name。"""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        class FakeDetector:
            def __init__(self):
                self._skill_runtime_profiles = {
                    "hook-http-demo": type(
                        "Profile",
                        (),
                        {"has_hook_config": True},
                    )(),
                }

            async def on_tool_call(self, **kwargs):
                return "hook-http-demo", {"hook-http-demo": 1.0}

            def get_skill_description(self, skill_name):
                return f"desc:{skill_name}"

            def get_skill_runtime_profile(self, skill_name):
                return self._skill_runtime_profiles.get(skill_name)

            def _detect_skill_from_skill_md_read(self, tool_name, tool_input):
                if (
                    tool_name == "read_file"
                    and tool_input.get(
                        "file_path",
                    )
                    == "/workspace/skills/hook-http-demo/SKILL.md"
                ):
                    return "hook-http-demo"
                return None

        from swe.tracing.manager import get_current_trace

        ctx = get_current_trace()
        assert ctx is not None
        ctx.set_skill_detector(FakeDetector(), ["hook-http-demo"])

        span_id = await manager.emit_tool_call_start(
            trace_id=trace_id,
            tool_name="read_file",
            tool_input={
                "file_path": "/workspace/skills/hook-http-demo/SKILL.md",
            },
            source_id="default",
        )

        span = manager._pending_spans[
            span_id
        ]  # pylint: disable=protected-access
        assert span.skill_name == "hook-http-demo"

        await manager.close()

    @pytest.mark.asyncio
    async def test_emit_tool_call_keeps_non_hook_skill_in_span(
        self,
        enabled_config,
        mock_db,
    ):
        """普通技能的 tool span 仍应保留 skill_name。"""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        class FakeDetector:
            async def on_tool_call(self, **kwargs):
                return "weather", {"weather": 1.0}

            def get_skill_description(self, skill_name):
                return f"desc:{skill_name}"

            def get_skill_runtime_profile(self, skill_name):
                return type(
                    "Profile",
                    (),
                    {"has_hook_config": False},
                )()

        ctx = get_current_trace()
        assert ctx is not None
        ctx.set_skill_detector(FakeDetector(), ["weather"])

        span_id = await manager.emit_tool_call_start(
            trace_id=trace_id,
            tool_name="weather_query",
            tool_input={"location": "Shanghai"},
            source_id="default",
        )

        span = manager._pending_spans[
            span_id
        ]  # pylint: disable=protected-access
        assert span.skill_name == "weather"
        # skill_description 字段已从 Span 模型移除
        assert not hasattr(span, "skill_description")

        await manager.close()

    @pytest.mark.asyncio
    async def test_emit_tool_call_uses_precomputed_attribution_once(
        self,
        enabled_config,
        mock_db,
    ):
        """预计算 attribution 存在时，不应再次调用 detector.on_tool_call。"""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        class FakeDetector:
            on_tool_call = AsyncMock(
                side_effect=AssertionError(
                    "detector.on_tool_call should not be called twice",
                ),
            )

            def get_skill_description(self, skill_name):
                return f"desc:{skill_name}"

            def get_skill_runtime_profile(self, skill_name):
                return type(
                    "Profile",
                    (),
                    {"has_hook_config": False},
                )()

        ctx = get_current_trace()
        assert ctx is not None
        ctx.set_skill_detector(FakeDetector(), ["fill-metadata"])

        span_id = await manager.emit_tool_call_start(
            trace_id=trace_id,
            tool_name="read_file",
            tool_input={"file_path": "steps/step1.md"},
            source_id="default",
            use_precomputed_attribution=True,
            precomputed_attribution={"primary_skill": "fill-metadata"},
        )

        span = manager._pending_spans[
            span_id
        ]  # pylint: disable=protected-access
        assert span.skill_name == "fill-metadata"
        # skill_description 字段已从 Span 模型移除
        assert not hasattr(span, "skill_description")

        await manager.close()

    @pytest.mark.asyncio
    async def test_user_message_sanitization(self, mock_db):
        """Test that user message is sanitized."""
        config = TracingConfig(
            enabled=True,
            sanitize_output=True,
            max_output_length=100,
        )
        manager = TraceManager(config, mock_db)
        await manager.initialize()

        long_message = "x" * 1000
        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
            user_message=long_message,
        )

        trace = manager._active_traces[trace_id]
        assert len(trace.user_message) < len(long_message)

        await manager.close()

    @pytest.mark.asyncio
    async def test_tool_input_sanitization(self, mock_db):
        """Test that tool input is sanitized."""
        config = TracingConfig(enabled=True, sanitize_output=True)
        manager = TraceManager(config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        await manager.emit_tool_call_start(
            trace_id=trace_id,
            tool_name="test_tool",
            tool_input={"api_key": "secret123", "data": "normal"},
            source_id="default",
        )

        span = list(manager._pending_spans.values())[
            0
        ]  # pylint: disable=protected-access
        assert span.tool_input["api_key"] == "[REDACTED]"
        assert span.tool_input["data"] == "normal"

        await manager.close()

    @pytest.mark.asyncio
    async def test_trace_totals_updated(self, enabled_config, mock_db):
        """Test that trace totals are updated from spans."""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        # Emit LLM call
        span_id = await manager.emit_llm_input(
            trace_id=trace_id,
            model_name="gpt-4",
            input_tokens=100,
            source_id="default",
        )
        await manager.emit_llm_output(trace_id, span_id, output_tokens=200)

        # Check trace totals
        trace = manager._active_traces[
            trace_id
        ]  # pylint: disable=protected-access
        assert trace.total_input_tokens == 100
        assert trace.total_output_tokens == 200
        assert trace.model_name == "gpt-4"

        await manager.close()

    @pytest.mark.asyncio
    async def test_update_span_rejects_cross_trace_pending_span(
        self,
        enabled_config,
        mock_db,
        caplog,
    ):
        """跨 trace 更新 pending span 时必须拒绝，避免污染汇总字段。"""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_a = await manager.start_trace(
            user_id="user-1",
            session_id="session-a",
            channel="console",
            source_id="default",
        )
        trace_b = await manager.start_trace(
            user_id="user-1",
            session_id="session-b",
            channel="console",
            source_id="default",
        )

        span_id = await manager.emit_tool_call_start(
            trace_id=trace_b,
            tool_name="read_file",
            tool_input={"path": "README.md"},
            source_id="default",
        )

        with caplog.at_level("ERROR"):
            await manager.emit_tool_call_end(
                trace_id=trace_a,
                span_id=span_id,
                tool_output="ok",
            )

        trace_a_obj = manager._active_traces[trace_a]
        trace_b_obj = manager._active_traces[trace_b]
        span = manager._pending_spans[span_id]

        assert trace_a_obj.tools_used == []
        assert trace_b_obj.tools_used == ["read_file"]
        assert span.end_time is None
        await manager.close()


class TestGlobalManager:
    """Tests for global manager functions."""

    def test_has_trace_manager_false_initially(self):
        """Test has_trace_manager returns False initially."""
        assert has_trace_manager() is False

    def test_get_trace_manager_raises_when_not_initialized(self):
        """Test get_trace_manager raises when not initialized."""
        with pytest.raises(RuntimeError, match="not initialized"):
            get_trace_manager()

    @pytest.mark.asyncio
    async def test_init_trace_manager(self, mock_db):
        """Test init_trace_manager creates manager."""
        config = TracingConfig(enabled=True)
        manager = await init_trace_manager(config, mock_db)

        assert manager is not None
        assert has_trace_manager() is True
        assert get_trace_manager() is manager

        await close_trace_manager()

    @pytest.mark.asyncio
    async def test_close_trace_manager(self, mock_db):
        """Test close_trace_manager closes and clears manager."""
        config = TracingConfig(enabled=True)
        await init_trace_manager(config, mock_db)

        assert has_trace_manager() is True

        await close_trace_manager()

        assert has_trace_manager() is False

    @pytest.mark.asyncio
    async def test_init_trace_manager_idempotent(self, mock_db):
        """Test init_trace_manager returns existing manager if initialized."""
        config = TracingConfig(enabled=True)
        manager1 = await init_trace_manager(config, mock_db)
        manager2 = await init_trace_manager(config, mock_db)

        assert manager1 is manager2

        await close_trace_manager()


class TestBatchFlush:
    """Tests for batch flushing behavior."""

    @pytest.mark.asyncio
    async def test_flush_on_batch_size(self, mock_db):
        """Test that flush happens when batch size is reached."""
        config = TracingConfig(enabled=True, batch_size=3, flush_interval=60)
        manager = TraceManager(config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        # Emit spans up to batch size
        for i in range(3):
            await manager.emit_span(
                trace_id=trace_id,
                event_type=EventType.LLM_INPUT,
                name=f"span_{i}",
                source_id="default",
            )

        # Wait a bit for async flush
        import asyncio

        await asyncio.sleep(0.1)

        # Queue should be cleared after flush
        assert (
            len(manager._span_queue) == 0
        )  # pylint: disable=protected-access

        await manager.close()

    @pytest.mark.asyncio
    async def test_manual_flush(self, enabled_config, mock_db):
        """Test manual flush via close."""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="default",
        )

        # Emit a span
        await manager.emit_span(
            trace_id=trace_id,
            event_type=EventType.LLM_INPUT,
            name="test_span",
            source_id="default",
        )

        # Close should flush remaining spans
        await manager.close()

        # Verify the span queue is cleared
        assert len(manager._span_queue) == 0


class TestOwnsDb:
    """Tests for database ownership behavior."""

    @pytest.mark.asyncio
    async def test_owns_db_when_created_internally(self, enabled_config):
        """Test that manager owns DB when it creates the connection."""
        with patch("swe.tracing.manager.DatabaseConnection") as MockDB:
            mock_conn = MagicMock()
            mock_conn.is_connected = True
            mock_conn.connect = AsyncMock()
            MockDB.return_value = mock_conn

            # No DB provided, manager should create and own it
            manager = TraceManager(enabled_config, db=None)
            assert manager._owns_db is True

    @pytest.mark.asyncio
    async def test_does_not_own_db_when_provided(
        self,
        enabled_config,
        mock_db,
    ):
        """Test that manager does not own DB when connection is provided."""
        manager = TraceManager(enabled_config, mock_db)
        assert manager._owns_db is False

        await manager.initialize()
        await manager.close()

        # DB should not be closed since we don't own it
        mock_db.close.assert_not_called()


class TestSessionName:
    """Tests for session_name field in tracing."""

    def test_trace_context_with_session_name(self):
        """Test creating TraceContext with session_name."""
        ctx = TraceContext(
            trace_id="trace-1",
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="source-1",
            user_name="Test User",
            bbk_id="bbk-001",
            session_name="My First Chat",
        )

        assert ctx.session_name == "My First Chat"

    def test_trace_context_without_session_name(self):
        """Test creating TraceContext without session_name defaults to None."""
        ctx = TraceContext(
            trace_id="trace-1",
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="source-1",
        )

        assert ctx.session_name is None

    @pytest.mark.asyncio
    async def test_start_trace_with_session_name(
        self,
        enabled_config,
        mock_db,
    ):
        """Test start_trace with session_name parameter."""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="source-1",
            session_name="Important Discussion",
        )

        assert trace_id is not None
        trace = manager._active_traces[trace_id]
        assert trace.session_name == "Important Discussion"

        # Check context also has session_name
        ctx = get_current_trace()
        assert ctx is not None
        assert ctx.session_name == "Important Discussion"

        await manager.close()

    @pytest.mark.asyncio
    async def test_start_trace_without_session_name(
        self,
        enabled_config,
        mock_db,
    ):
        """Test start_trace without session_name parameter."""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="source-1",
        )

        assert trace_id is not None
        trace = manager._active_traces[trace_id]
        assert trace.session_name is None

        await manager.close()

    @pytest.mark.asyncio
    async def test_session_name_with_user_info(self, enabled_config, mock_db):
        """Test session_name works together with user_name and bbk_id."""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="source-1",
            user_name="John Doe",
            bbk_id="branch-001",
            session_name="Client Meeting Notes",
        )

        trace = manager._active_traces[trace_id]
        assert trace.session_name == "Client Meeting Notes"
        assert trace.user_name == "John Doe"
        assert trace.bbk_id == "branch-001"

        # Verify all fields are in context
        ctx = get_current_trace()
        assert ctx.session_name == "Client Meeting Notes"
        assert ctx.user_name == "John Doe"
        assert ctx.bbk_id == "branch-001"

        await manager.close()

    @pytest.mark.asyncio
    async def test_setup_skill_detector_does_not_infer_from_user_message(
        self,
        enabled_config,
        mock_db,
        monkeypatch,
    ):
        """追踪初始化不会从用户正文推断 skill。"""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="source-1",
            user_message="use xlsx to analyze",
        )

        detector_instance = MagicMock()
        detector_instance.detect_from_user_message.return_value = (
            "xlsx",
            0.9,
        )
        detector_instance.start_skill = AsyncMock()

        monkeypatch.setattr(
            "swe.agents.skill_invocation_detector.SkillInvocationDetector",
            lambda **kwargs: detector_instance,
        )

        await manager.setup_skill_detector(
            trace_id=trace_id,
            enabled_skills=["xlsx"],
        )

        detector_instance.detect_from_user_message.assert_not_called()
        detector_instance.start_skill.assert_not_awaited()
        await manager.close()

    @pytest.mark.asyncio
    async def test_setup_skill_detector_uses_supplied_skill_registry(
        self,
        enabled_config,
        mock_db,
        monkeypatch,
    ):
        """追踪技能探测器应使用调用方提供的 registry 快照。"""
        manager = TraceManager(enabled_config, mock_db)
        await manager.initialize()
        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="source-1",
        )
        registry = SkillToolRegistry()
        captured_kwargs = {}
        detector_instance = MagicMock()

        def build_detector(**kwargs):
            captured_kwargs.update(kwargs)
            return detector_instance

        monkeypatch.setattr(
            "swe.agents.skill_invocation_detector.SkillInvocationDetector",
            build_detector,
        )

        await manager.setup_skill_detector(
            trace_id=trace_id,
            enabled_skills=["xlsx"],
            skill_tool_registry=registry,
        )

        assert captured_kwargs["registry"] is registry
        await manager.close()

    def test_trace_model_with_session_name(self):
        """Test Trace model with session_name field."""
        from swe.tracing.models import Trace

        trace = Trace(
            trace_id="trace-1",
            source_id="source-1",
            user_id="user-1",
            session_id="session-1",
            session_name="Project Discussion",
            channel="console",
            start_time=datetime.now(),
        )

        assert trace.session_name == "Project Discussion"

    def test_trace_model_without_session_name(self):
        """Test Trace model without session_name defaults to None."""
        from swe.tracing.models import Trace

        trace = Trace(
            trace_id="trace-1",
            source_id="source-1",
            user_id="user-1",
            session_id="session-1",
            channel="console",
            start_time=datetime.now(),
        )

        assert trace.session_name is None
