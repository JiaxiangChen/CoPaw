# -*- coding: utf-8 -*-
"""Cron Agent 完成输出后的取消状态回归测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope_runtime.engine.schemas.agent_schemas import RunStatus

from swe.app.crons.manager import CronManager
from swe.app.crons.executor import AgentStreamState, CronExecutor
from swe.app.cron_result_metrics import (
    CRON_EMPTY_MODEL_OUTPUT_TOTAL,
    CRON_SESSION_COMMIT_FAILURE_TOTAL,
    CRON_SESSION_ROUTE_MISMATCH_TOTAL,
    get_cron_result_metrics,
    reset_cron_result_metrics_for_test,
)
from swe.app.runner.query_contracts import QueryPersistenceResult
from swe.app.runner.session import SafeJSONSession
from swe.app.crons.models import (
    CronJobRequest,
    CronJobSpec,
    DispatchSpec,
    DispatchTarget,
    JobsFile,
    JobRuntimeSpec,
    ScheduleSpec,
)
from swe.app.source_system_config.models import (
    EffectiveSourceSystemConfig,
    SourceSystemConfig,
)
from swe.app.source_system_config.runtime import bind_source_system_config
from swe.providers.models import ModelSlotConfig
from swe.tracing.models import TraceStatus


class _Repo:
    def __init__(self, job: CronJobSpec) -> None:
        self._job = job

    async def get_job(self, job_id: str) -> CronJobSpec | None:
        return self._job if job_id == self._job.id else None

    async def list_jobs(self) -> list[CronJobSpec]:
        return [self._job]

    async def load(self) -> JobsFile:
        return JobsFile(jobs=[self._job])

    async def save(self, jobs_file: JobsFile) -> None:
        self._job = jobs_file.jobs[0]


class _Runner:
    async def stream_query(self, _req):
        yield SimpleNamespace(
            object="message",
            status=RunStatus.Completed,
            content=[SimpleNamespace(type="text", text="done output")],
        )
        await asyncio.sleep(30)

    def get_query_persistence_result(self, **_kwargs):
        return QueryPersistenceResult(
            session_id="session-a",
            user_id="user-a",
            assistant_message_count=1,
            commit_attempted=True,
            committed=True,
            persisted_assistant_content=[
                {"type": "text", "text": "response output"},
            ],
        )


class _CommitConfirmedRunner(_Runner):
    """模拟在取消后完成 session cleanup 的 Runner。"""

    async def stream_query(self, _req):
        yield SimpleNamespace(
            object="message",
            status=RunStatus.Completed,
            content=[SimpleNamespace(type="text", text="done output")],
        )
        yield SimpleNamespace(
            object="response",
            status=RunStatus.Completed,
        )
        await asyncio.sleep(30)

    async def wait_for_query_persistence_result(self, **_kwargs):
        return QueryPersistenceResult(
            session_id="session-a",
            user_id="user-a",
            assistant_message_count=1,
            commit_attempted=True,
            committed=True,
        )


class _CompletedThenBlockingCloseRunner:
    """模拟最终 response 已到达，但关闭流时发生外部取消。"""

    def __init__(self) -> None:
        self.closing = asyncio.Event()

    async def stream_query(self, _req):
        try:
            yield SimpleNamespace(
                object="message",
                status=RunStatus.Completed,
                content=[SimpleNamespace(type="text", text="done output")],
            )
            yield SimpleNamespace(
                object="response",
                status=RunStatus.Completed,
            )
        finally:
            self.closing.set()
            await asyncio.Event().wait()

    async def wait_for_query_persistence_result(self, **_kwargs):
        return QueryPersistenceResult(
            session_id="session-a",
            user_id="user-a",
            assistant_message_count=1,
            commit_attempted=True,
            committed=True,
        )


class _EmptyCompletedCommitConfirmedRunner:
    """模拟 response 完成但无输出时、cleanup 已提交的取消窗口。"""

    def __init__(self) -> None:
        self.response_completed = asyncio.Event()

    async def stream_query(self, _req):
        self.response_completed.set()
        yield SimpleNamespace(
            object="response",
            status=RunStatus.Completed,
        )
        await asyncio.Event().wait()

    async def wait_for_query_persistence_result(self, **_kwargs):
        return QueryPersistenceResult(
            session_id="session-a",
            user_id="user-a",
            assistant_message_count=0,
            commit_attempted=True,
            committed=True,
        )


class _PendingRunner:
    async def stream_query(self, _req):
        if _never_emit_stream_chunk():
            yield None
        await asyncio.sleep(30)


class _EmptyStreamRunner:
    """模拟返回空流的 Runner（没有任何事件，包括 Completed 或 Failed）。"""

    async def stream_query(self, _req):
        # 不 yield 任何事件，直接返回
        return
        yield  # pylint: disable=unreachable # 使方法成为 generator


class _FailedRunner:
    """模拟模型调用失败的 Runner，返回 Failed 事件而不抛出异常。"""

    async def stream_query(self, _req):
        # 模拟 runner 在模型失败时的行为：yield Failed 事件，不抛出异常
        yield SimpleNamespace(
            object="message",
            status=RunStatus.Failed,
            error=SimpleNamespace(
                code="model_error",
                message="Model not available",
            ),
        )


class _ResponseFailedRunner:
    """模拟 Runtime 的标准 response 失败终态。"""

    async def stream_query(self, _req):
        for status in (
            RunStatus.Created,
            RunStatus.InProgress,
            RunStatus.Failed,
        ):
            yield SimpleNamespace(
                object="response",
                status=status,
                error=(
                    SimpleNamespace(
                        code="model_call_failed",
                        message="Authorization: Bearer secret-token",
                    )
                    if status == RunStatus.Failed
                    else None
                ),
            )


class _ResponseCompletedEmptyRunner:
    """模拟 Runtime 完成但没有 assistant message 的终态。"""

    async def stream_query(self, _req):
        for status in (
            RunStatus.Created,
            RunStatus.InProgress,
            RunStatus.Completed,
        ):
            yield SimpleNamespace(object="response", status=status)


class _MessageCompletedEmptyRunner:
    """模拟 completed message 未携带任何可展示 assistant 文本。"""

    async def stream_query(self, _req):
        yield SimpleNamespace(
            object="message",
            status=RunStatus.Completed,
            role="assistant",
            content=[],
        )
        yield SimpleNamespace(
            object="response",
            status=RunStatus.Completed,
        )


class _ResponseCompletedOutputRunner:
    """模拟仅通过 response.output 返回 assistant 文本的 Runtime。"""

    async def stream_query(self, _req):
        yield SimpleNamespace(
            object="response",
            status=RunStatus.Completed,
            id="response-with-output",
            output=[
                SimpleNamespace(
                    id="assistant-output-1",
                    role="assistant",
                    content=[
                        SimpleNamespace(
                            type="text",
                            text="response output",
                        ),
                    ],
                ),
            ],
        )

    def get_query_persistence_result(self, **_kwargs):
        return QueryPersistenceResult(
            session_id="session-a",
            user_id="user-a",
            assistant_message_count=1,
            commit_attempted=True,
            committed=True,
            persisted_assistant_content=[
                {"type": "text", "text": "response output"},
            ],
        )


class _MessageCompletedOutputOnlyRunner:
    """模拟只有 message/completed 而没有 response/completed 的异常流。"""

    async def stream_query(self, _req):
        yield SimpleNamespace(
            object="message",
            status=RunStatus.Completed,
            id="assistant-message-only",
            role="assistant",
            content=[SimpleNamespace(type="text", text="message only")],
        )

    def get_query_persistence_result(self, **_kwargs):
        return QueryPersistenceResult(
            session_id="session-a",
            user_id="user-a",
            assistant_message_count=1,
            commit_attempted=True,
            committed=True,
        )


class _ResponseCompletedOutputWithoutPersistenceRunner(
    _ResponseCompletedOutputRunner,
):
    """模拟没有 session 提交回执能力的旧 Runner。"""

    get_query_persistence_result = None


class _CommitFailedRunner(_ResponseCompletedOutputRunner):
    """模拟 Runtime 已完成但 session commit 未确认。"""

    def get_query_persistence_result(self, **_kwargs):
        return QueryPersistenceResult(
            session_id="session-a",
            user_id="user-a",
            assistant_message_count=1,
            commit_attempted=True,
            committed=False,
            commit_error="disk write failed",
        )


class _CommitWithoutAssistantRunner(_ResponseCompletedOutputRunner):
    """A receipt without persisted assistant content is not a success."""

    def get_query_persistence_result(self, **_kwargs):
        return QueryPersistenceResult(
            session_id="session-a",
            user_id="user-a",
            assistant_message_count=0,
            commit_attempted=True,
            committed=True,
        )


class _IdempotentReplayRunner(_ResponseCompletedOutputRunner):
    """A duplicate scheduler delivery reuses an existing persisted task run."""

    def get_query_persistence_result(self, **_kwargs):
        return QueryPersistenceResult(
            session_id="session-a",
            user_id="user-a",
            assistant_message_count=1,
            commit_attempted=False,
            committed=True,
            idempotent_replay=True,
            output_delivery_replay_supported=True,
            output_delivery_completed=True,
        )


class _ReplayableDeliveryRunner(_ResponseCompletedOutputRunner):
    """A replay retries output delivery until it receives a durable receipt."""

    def __init__(self) -> None:
        self.idempotent_replay = False
        self.output_delivery_completed = False
        self.delivery_marks = 0

    async def stream_query(self, _req):
        yield SimpleNamespace(
            object="message",
            status=RunStatus.Completed,
            content=[SimpleNamespace(type="text", text="replayed output")],
        )
        yield SimpleNamespace(object="response", status=RunStatus.Completed)

    def get_query_persistence_result(self, **_kwargs):
        return SimpleNamespace(
            session_id="session-a",
            user_id="user-a",
            assistant_message_count=1,
            commit_attempted=not self.idempotent_replay,
            committed=True,
            commit_error=None,
            idempotent_replay=self.idempotent_replay,
            output_delivery_replay_supported=True,
            output_delivery_completed=self.output_delivery_completed,
            persisted_assistant_content=[
                {"type": "text", "text": "persisted output"},
            ],
        )

    async def mark_cron_output_delivery_completed(self, **_kwargs):
        self.delivery_marks += 1
        self.output_delivery_completed = True


class _LegacyReplayDeliveryRunner(_ReplayableDeliveryRunner):
    """An old task run has no durable output-delivery schema marker."""

    def __init__(self) -> None:
        super().__init__()
        self.idempotent_replay = True

    def get_query_persistence_result(self, **_kwargs):
        return SimpleNamespace(
            session_id="session-a",
            user_id="user-a",
            assistant_message_count=1,
            commit_attempted=False,
            committed=True,
            idempotent_replay=True,
            output_delivery_completed=False,
            persisted_assistant_content=[
                {"type": "text", "text": "persisted output"},
            ],
        )


class _LegacyPersistenceReceiptRunner(_ResponseCompletedOutputRunner):
    """Older Runner adapters do not expose the replay receipt field."""

    def get_query_persistence_result(self, **_kwargs):
        return SimpleNamespace(
            session_id="session-a",
            user_id="user-a",
            assistant_message_count=1,
            commit_attempted=True,
            committed=True,
            commit_error=None,
        )


class _OutputThenResponseFailedRunner:
    """A failed response following text must not publish a task result."""

    async def stream_query(self, _req):
        yield SimpleNamespace(
            object="message",
            status=RunStatus.Completed,
            role="assistant",
            content=[SimpleNamespace(type="text", text="partial output")],
        )
        yield SimpleNamespace(
            object="response",
            status=RunStatus.Failed,
            error=SimpleNamespace(code="model_error", message="failed"),
        )

    def get_query_persistence_result(self, **_kwargs):
        return QueryPersistenceResult(
            session_id="session-a",
            user_id="user-a",
            assistant_message_count=1,
            commit_attempted=True,
            committed=True,
        )


class _ResponseCancelledThenBlockedRunner:
    """A Runtime terminal cancellation must not wait for a blocked iterator."""

    async def stream_query(self, _req):
        yield SimpleNamespace(
            object="response",
            status=RunStatus.Canceled,
            error=SimpleNamespace(
                code="upstream_cancelled",
                message="cancelled",
            ),
        )
        await asyncio.sleep(30)


class _MessageFailedThenBlockedRunner:
    """A terminal message failure must not wait for a stuck iterator."""

    async def stream_query(self, _req):
        yield SimpleNamespace(
            object="message",
            status=RunStatus.Failed,
            error=SimpleNamespace(code="model_error", message="failed"),
        )
        await asyncio.sleep(30)


class _SequenceDeduplicatedOutputRunner(_ResponseCompletedOutputRunner):
    """The Runtime may identify duplicate output only by sequence number."""

    async def stream_query(self, _req):
        yield SimpleNamespace(
            object="message",
            status=RunStatus.Completed,
            sequence_number=7,
            role="assistant",
            content=[SimpleNamespace(type="text", text="sequence output")],
        )

        yield SimpleNamespace(
            object="response",
            status=RunStatus.Completed,
            output=[
                SimpleNamespace(
                    sequence_number=7,
                    role="assistant",
                    content=[
                        SimpleNamespace(type="text", text="sequence output"),
                    ],
                ),
            ],
        )

    def get_query_persistence_result(self, **_kwargs):
        return QueryPersistenceResult(
            session_id="session-a",
            user_id="user-a",
            assistant_message_count=1,
            commit_attempted=True,
            committed=True,
            persisted_assistant_content=[
                {"type": "text", "text": "sequence output"},
            ],
        )


class _MessageAndResponseOutputRunner:
    """模拟 message 与 response.output 重复承载同一 assistant 文本。"""

    async def stream_query(self, _req):
        yield SimpleNamespace(
            object="message",
            status=RunStatus.Completed,
            id="assistant-output-1",
            role="assistant",
            content=[SimpleNamespace(type="text", text="deduplicated output")],
        )
        yield SimpleNamespace(
            object="response",
            status=RunStatus.Completed,
            id="response-with-duplicate-output",
            output=[
                SimpleNamespace(
                    id="assistant-output-1",
                    role="assistant",
                    content=[
                        SimpleNamespace(
                            type="text",
                            text="deduplicated output",
                        ),
                    ],
                ),
            ],
        )

    def get_query_persistence_result(self, **_kwargs):
        return QueryPersistenceResult(
            session_id="session-a",
            user_id="user-a",
            assistant_message_count=1,
            commit_attempted=True,
            committed=True,
        )


class _ResponseCancelledRunner:
    """模拟 Runtime 的标准 response 取消终态。"""

    async def stream_query(self, _req):
        yield SimpleNamespace(
            object="response",
            status=RunStatus.Canceled,
            error=SimpleNamespace(
                code="upstream_cancelled",
                message="Upstream request was cancelled",
            ),
        )


class _ResponseTerminalFailureRunner:
    """模拟 Runtime 的非 Failed 失败终态。"""

    def __init__(self, status: RunStatus, include_error: bool = True) -> None:
        self._status = status
        self._include_error = include_error

    async def stream_query(self, _req):
        yield SimpleNamespace(
            object="response",
            status=self._status,
            error=(
                SimpleNamespace(
                    code="terminal_error",
                    message="Runtime ended without a completed response",
                )
                if self._include_error
                else None
            ),
        )


class _OuterSequenceDeduplicatedOutputRunner(_ResponseCompletedOutputRunner):
    """Some Runtime adapters identify response output on the outer event."""

    async def stream_query(self, _req):
        yield SimpleNamespace(
            object="message",
            status=RunStatus.Completed,
            sequence_number=8,
            role="assistant",
            content=[
                SimpleNamespace(type="text", text="outer sequence output"),
            ],
        )

        yield SimpleNamespace(
            object="response",
            status=RunStatus.Completed,
            sequence_number=8,
            output=[
                SimpleNamespace(
                    role="assistant",
                    content=[
                        SimpleNamespace(
                            type="text",
                            text="outer sequence output",
                        ),
                    ],
                ),
            ],
        )

    def get_query_persistence_result(self, **_kwargs):
        return QueryPersistenceResult(
            session_id="session-a",
            user_id="user-a",
            assistant_message_count=1,
            commit_attempted=True,
            committed=True,
            persisted_assistant_content=[
                {"type": "text", "text": "outer sequence output"},
            ],
        )


class _ResponseCompletedThenBlockedRunner(_ResponseCompletedOutputRunner):
    """A terminal response must not depend on tail stream EOF."""

    async def stream_query(self, _req):
        async for event in super().stream_query(_req):
            yield event
        await asyncio.sleep(30)


class _ProgressThenCompletedRunner:
    """A long stream only needs its final assistant message for delivery."""

    async def stream_query(self, _req):
        for index in range(32):
            yield SimpleNamespace(
                object="response",
                status=RunStatus.InProgress,
                sequence_number=index,
            )
        yield SimpleNamespace(
            object="message",
            status=RunStatus.Completed,
            content=[SimpleNamespace(type="text", text="final output")],
        )
        yield SimpleNamespace(object="response", status=RunStatus.Completed)

    def get_query_persistence_result(self, **_kwargs):
        return QueryPersistenceResult(
            session_id="session-a",
            user_id="user-a",
            assistant_message_count=1,
            commit_attempted=True,
            committed=True,
        )


class _ChannelManager:
    def __init__(self) -> None:
        self.events: list[object] = []
        self.texts: list[dict[str, object]] = []

    async def send_event(self, **kwargs) -> bool:
        self.events.append(kwargs["event"])
        return True

    async def send_text(self, **kwargs) -> bool:
        self.texts.append(kwargs)
        return True


class _TaskSession:
    def __init__(self) -> None:
        self.state: dict[str, object] = {}

    async def mutate_session_state(self, *, mutator, **_kwargs) -> None:
        self.state = mutator(self.state)


class _FailOnceTaskSession(_TaskSession):
    def __init__(self) -> None:
        super().__init__()
        self._failed = False

    async def mutate_session_state(self, *, mutator, **_kwargs) -> None:
        if not self._failed:
            self._failed = True
            raise RuntimeError("session unavailable")
        await super().mutate_session_state(mutator=mutator)


class _TaskChatManager:
    def __init__(self, existing):
        self.existing = existing
        self.created: list[object] = []
        self.updated: list[object] = []

    async def get_chat(self, _chat_id):
        return self.existing

    async def get_or_create_chat(self, session_id, user_id, _channel, *, name):
        chat = SimpleNamespace(
            id="replacement-chat",
            session_id=session_id,
            user_id=user_id,
            name=name,
            meta={},
        )
        self.created.append(chat)
        return chat

    async def create_chat(self, chat):
        self.created.append(chat)
        return chat

    async def update_chat(self, chat):
        self.updated.append(chat)


class _SlowSendChannelManager(_ChannelManager):
    def __init__(self, send_started: asyncio.Event) -> None:
        super().__init__()
        self.send_started = send_started

    async def send_event(self, **kwargs) -> bool:
        self.events.append(kwargs["event"])
        self.send_started.set()
        await asyncio.sleep(30)
        return True


class _FailOnceChannelManager(_ChannelManager):
    def __init__(self) -> None:
        super().__init__()
        self.send_attempts = 0

    async def send_event(self, **kwargs) -> bool:
        self.send_attempts += 1
        if self.send_attempts == 1:
            raise RuntimeError("channel unavailable")
        return await super().send_event(**kwargs)


class _UnconfirmedChannelManager(_ChannelManager):
    """A channel that deliberately skips a final-message delivery."""

    async def send_event(self, **kwargs) -> bool:
        self.events.append(kwargs["event"])
        return False


class _MonitorSyncClient:
    def __init__(self) -> None:
        self.records: list[dict] = []

    async def record_execution(self, **kwargs) -> None:
        self.records.append(kwargs)


def _never_emit_stream_chunk() -> bool:
    return False


def _build_agent_job() -> CronJobSpec:
    return CronJobSpec(
        id="job-cancel-after-output",
        name="agent job",
        schedule=ScheduleSpec(cron="* * * * *"),
        task_type="agent",
        request=CronJobRequest(
            input=[{"content": [{"type": "text", "text": "ping"}]}],
        ),
        dispatch=DispatchSpec(
            channel="console",
            target=DispatchTarget(user_id="user-a", session_id="session-a"),
            meta={},
        ),
        runtime=JobRuntimeSpec(timeout_seconds=60),
    )


def _build_broadcast_agent_job() -> CronJobSpec:
    job = _build_agent_job()
    return job.model_copy(
        update={
            "meta": {
                "broadcast_offset_minutes": 20,
                "broadcast_notification_policy": "original_schedule",
                "broadcast_original_timezone": "Asia/Shanghai",
            },
        },
    )


def test_automatic_execution_applies_notification_delay():
    """自动执行成功时，任务级通知延迟应写入 Monitor due time。"""

    async def _run():
        job = _build_agent_job().model_copy(
            update={"meta": {"notification_delay_minutes": 120}},
        )
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_Runner(),
            channel_manager=_ChannelManager(),
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )
        actual_time = datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)

        await manager._sync_execution_to_monitor(  # pylint: disable=protected-access
            job=job,
            exec_status="success",
            actual_time=actual_time,
            end_time=actual_time,
            duration_ms=100,
            error_message="",
            output_preview="done",
            is_manual=False,
        )

        return monitor.records[-1], actual_time

    record, actual_time = asyncio.run(_run())

    assert record["notification_due_at"] == actual_time + timedelta(
        minutes=120,
    )


def test_manual_execution_does_not_apply_notification_delay():
    """手动执行保持即时通知，不套用任务级通知延迟。"""

    async def _run():
        job = _build_agent_job().model_copy(
            update={"meta": {"notification_delay_minutes": 120}},
        )
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_Runner(),
            channel_manager=_ChannelManager(),
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )
        actual_time = datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)

        await manager._sync_execution_to_monitor(  # pylint: disable=protected-access
            job=job,
            exec_status="success",
            actual_time=actual_time,
            end_time=actual_time,
            duration_ms=100,
            error_message="",
            output_preview="done",
            is_manual=True,
        )

        return monitor.records[-1]

    record = asyncio.run(_run())

    assert record["notification_due_at"] is None


def test_invalid_notification_delay_defaults_to_immediate():
    """非法通知延迟按 0 处理，避免 pending 记录被错误延后。"""

    async def _run():
        job = _build_agent_job().model_copy(
            update={"meta": {"notification_delay_minutes": "bad"}},
        )
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_Runner(),
            channel_manager=_ChannelManager(),
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )
        actual_time = datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)

        await manager._sync_execution_to_monitor(  # pylint: disable=protected-access
            job=job,
            exec_status="success",
            actual_time=actual_time,
            end_time=actual_time,
            duration_ms=100,
            error_message="",
            output_preview="done",
            is_manual=False,
        )

        return monitor.records[-1]

    record = asyncio.run(_run())

    assert record["notification_due_at"] is None


def test_weekend_notification_due_time_is_suppressed_when_enabled():
    """Source 开启周末抑制时，原始通知时间落到周末不再入队。"""

    async def _run():
        job = _build_agent_job().model_copy(
            update={
                "schedule": ScheduleSpec(
                    cron="* * * * *",
                    timezone="Asia/Shanghai",
                ),
                "meta": {"notification_delay_minutes": 60},
            },
        )
        effective = EffectiveSourceSystemConfig(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                {
                    "cron_notifications": {
                        "skip_weekend_zhaohu_enabled": True,
                    },
                },
            ).merged_with_defaults(),
            raw_config=SourceSystemConfig.model_validate(
                {
                    "cron_notifications": {
                        "skip_weekend_zhaohu_enabled": True,
                    },
                },
            ),
            version=3,
        )
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_Runner(),
            channel_manager=_ChannelManager(),
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )
        actual_time = datetime(2026, 6, 5, 15, 30, tzinfo=timezone.utc)

        with bind_source_system_config(effective):
            await manager._sync_execution_to_monitor(  # pylint: disable=protected-access
                job=job,
                exec_status="success",
                actual_time=actual_time,
                end_time=actual_time,
                duration_ms=100,
                error_message="",
                output_preview="done",
                is_manual=False,
            )

        return monitor.records[-1]

    record = asyncio.run(_run())

    assert record["notification_due_at"] is None
    assert record["notification_timezone"] == ""
    assert record["suppress_notification"] is True


def test_weekend_notification_uses_task_timezone_when_enabled():
    """周末抑制按任务时区判断，不按北京时间强制判断。"""

    async def _run():
        job = _build_agent_job().model_copy(
            update={
                "schedule": ScheduleSpec(
                    cron="* * * * *",
                    timezone="UTC",
                ),
                "meta": {"notification_delay_minutes": 60},
            },
        )
        effective = EffectiveSourceSystemConfig(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                {
                    "cron_notifications": {
                        "skip_weekend_zhaohu_enabled": True,
                    },
                },
            ).merged_with_defaults(),
            raw_config=SourceSystemConfig.model_validate(
                {
                    "cron_notifications": {
                        "skip_weekend_zhaohu_enabled": True,
                    },
                },
            ),
            version=3,
        )
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_Runner(),
            channel_manager=_ChannelManager(),
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )
        actual_time = datetime(2026, 6, 5, 15, 30, tzinfo=timezone.utc)

        with bind_source_system_config(effective):
            await manager._sync_execution_to_monitor(  # pylint: disable=protected-access
                job=job,
                exec_status="success",
                actual_time=actual_time,
                end_time=actual_time,
                duration_ms=100,
                error_message="",
                output_preview="done",
                is_manual=False,
            )

        return monitor.records[-1], actual_time

    record, actual_time = asyncio.run(_run())

    assert record["notification_due_at"] == actual_time + timedelta(minutes=60)
    assert record["notification_timezone"] == "UTC"
    assert record["suppress_notification"] is False


def test_weekend_notification_due_time_is_kept_by_default():
    """默认不改变存量定时任务的周末完成通知行为。"""

    async def _run():
        job = _build_agent_job().model_copy(
            update={
                "schedule": ScheduleSpec(
                    cron="* * * * *",
                    timezone="Asia/Shanghai",
                ),
                "meta": {"notification_delay_minutes": 60},
            },
        )
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_Runner(),
            channel_manager=_ChannelManager(),
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )
        actual_time = datetime(2026, 6, 5, 15, 30, tzinfo=timezone.utc)

        await manager._sync_execution_to_monitor(  # pylint: disable=protected-access
            job=job,
            exec_status="success",
            actual_time=actual_time,
            end_time=actual_time,
            duration_ms=100,
            error_message="",
            output_preview="done",
            is_manual=False,
        )

        return monitor.records[-1], actual_time

    record, actual_time = asyncio.run(_run())

    assert record["notification_due_at"] == actual_time + timedelta(minutes=60)
    assert record["notification_timezone"] == "Asia/Shanghai"
    assert record["suppress_notification"] is False


def test_completed_agent_output_cancelled_before_stream_close_is_cancelled(
    monkeypatch,
):
    """未确认 session 提交时，完成消息后的取消必须保持 cancelled。"""
    info_messages: list[str] = []

    def fake_executor_info(message, *args, **_kwargs) -> None:
        try:
            text = str(message) % args if args else str(message)
        except TypeError:
            text = str(message)
        info_messages.append(text)

    monkeypatch.setattr(
        "swe.app.crons.executor.logger.info",
        fake_executor_info,
    )

    async def _run():
        job = _build_agent_job()
        channel_manager = _ChannelManager()
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_Runner(),
            channel_manager=channel_manager,
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )

        task = asyncio.create_task(
            manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            ),
        )
        await asyncio.sleep(0.05)
        assert channel_manager.events == []

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        return manager, channel_manager, monitor

    manager, channel_manager, monitor = asyncio.run(_run())

    state = manager.get_state("job-cancel-after-output")
    assert channel_manager.events == []
    assert state.last_status == "cancelled"
    assert state.last_error == "Job was cancelled"
    assert monitor.records[-1]["status"] == "cancelled"
    assert any(
        "cancelled before completion" in message for message in info_messages
    )


def test_completed_agent_event_cancelled_before_persistence_is_not_sent():
    """Unconfirmed output is never forwarded when the run is cancelled."""

    async def _run():
        job = _build_agent_job()
        channel_manager = _ChannelManager()
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_Runner(),
            channel_manager=channel_manager,
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )

        task = asyncio.create_task(
            manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            ),
        )
        await asyncio.sleep(0.05)
        assert channel_manager.events == []

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        return manager, channel_manager, monitor

    manager, channel_manager, monitor = asyncio.run(_run())

    state = manager.get_state("job-cancel-after-output")
    assert channel_manager.events == []
    assert state.last_status == "cancelled"
    assert state.last_error == "Job was cancelled"
    assert monitor.records[-1]["status"] == "cancelled"


def test_completed_agent_cancelled_after_session_commit_keeps_success():
    """取消时等待到已提交的 session，才能保留 success。"""

    async def _run():
        job = _build_agent_job()
        manager = CronManager(
            repo=_Repo(job),
            runner=_CommitConfirmedRunner(),
            channel_manager=_ChannelManager(),
        )
        task = asyncio.create_task(
            manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            ),
        )
        await asyncio.sleep(0.05)
        task.cancel()
        await task
        return manager

    manager = asyncio.run(_run())
    assert (
        manager.get_state("job-cancel-after-output").last_status == "success"
    )


def test_confirmed_cancelled_completion_forwards_final_message():
    """完成后取消仍为成功时，确认落盘的最终消息必须投递。"""

    async def _run():
        job = _build_agent_job()
        runner = _CompletedThenBlockingCloseRunner()
        channel_manager = _ChannelManager()
        manager = CronManager(
            repo=_Repo(job),
            runner=runner,
            channel_manager=channel_manager,
        )
        task = asyncio.create_task(
            manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            ),
        )
        await runner.closing.wait()
        task.cancel()
        await task
        return manager, channel_manager

    manager, channel_manager = asyncio.run(_run())

    assert (
        manager.get_state("job-cancel-after-output").last_status == "success"
    )
    assert [event.object for event in channel_manager.events].count(
        "message",
    ) == 1


def test_empty_completed_agent_is_rejected_before_late_cancellation():
    """A completed response without output is terminally invalid."""

    async def _run():
        job = _build_agent_job()
        runner = _EmptyCompletedCommitConfirmedRunner()
        manager = CronManager(
            repo=_Repo(job),
            runner=runner,
            channel_manager=_ChannelManager(),
        )
        task = asyncio.create_task(
            manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            ),
        )
        await runner.response_completed.wait()
        with pytest.raises(RuntimeError, match="empty_model_output"):
            await task
        return manager

    manager = asyncio.run(_run())
    assert manager.get_state("job-cancel-after-output").last_status == "error"


def test_agent_cancelled_before_completed_output_keeps_cancelled():
    """Agent 完成消息前被取消时，仍应记录为真正取消。"""

    async def _run():
        job = _build_agent_job()
        channel_manager = _ChannelManager()
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_PendingRunner(),
            channel_manager=channel_manager,
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )

        task = asyncio.create_task(
            manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            ),
        )
        await asyncio.sleep(0.05)
        assert channel_manager.events == []

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        return manager, channel_manager, monitor

    manager, channel_manager, monitor = asyncio.run(_run())

    state = manager.get_state("job-cancel-after-output")
    assert channel_manager.events == []
    assert state.last_status == "cancelled"
    assert state.last_error == "Job was cancelled"
    assert monitor.records[-1]["status"] == "cancelled"


def test_failed_execution_still_syncs_model_meta(monkeypatch):
    async def _run():
        job = _build_agent_job().model_copy(
            update={
                "model_slot": ModelSlotConfig(
                    provider_id="openai",
                    model="gpt-5.4",
                ),
            },
        )
        channel_manager = _ChannelManager()
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_Runner(),
            channel_manager=channel_manager,
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )

        monkeypatch.setattr(
            "swe.app.crons.executor.ProviderManager",
            SimpleNamespace(
                ensure_tenant_provider_storage=lambda _tenant_id: None,
                get_instance=lambda _tenant_id: SimpleNamespace(
                    get_provider=lambda _provider_id: None,
                    get_active_model=lambda: ModelSlotConfig(
                        provider_id="anthropic",
                        model="claude-3-7-sonnet",
                    ),
                ),
            ),
        )

        async def fake_execute_job(
            _job,
            _target_user_id,
            _target_session_id,
            _dispatch_meta,
        ):
            raise RuntimeError("boom")

        manager._executor._execute_job = (  # pylint: disable=protected-access
            fake_execute_job
        )

        try:
            await manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            )
        except RuntimeError:
            pass

        return monitor

    monitor = asyncio.run(_run())

    assert monitor.records[-1]["status"] == "error"
    assert monitor.records[-1]["meta"] == {
        "original_model_slot": {
            "provider_id": "openai",
            "model": "gpt-5.4",
        },
        "effective_model_slot": {
            "provider_id": "anthropic",
            "model": "claude-3-7-sonnet",
        },
        "fallback_reason": "provider_not_found",
    }


def test_failed_event_marks_execution_as_error(monkeypatch):
    """当 runner yield Failed 事件时，应正确标记为错误而不是成功。

    这是针对模型调用失败场景的关键测试：
    - runner 不抛出异常，而是 yield Failed 事件
    - executor 应检测 Failed 事件并正确处理为失败
    - 不应将 CancelledError 视为成功
    """
    warning_messages: list[str] = []

    def fake_executor_warning(message, *args, **_kwargs) -> None:
        try:
            text = str(message) % args if args else str(message)
        except TypeError:
            text = str(message)
        warning_messages.append(text)

    monkeypatch.setattr(
        "swe.app.crons.executor.logger.warning",
        fake_executor_warning,
    )

    async def _run():
        job = _build_agent_job()
        channel_manager = _ChannelManager()
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_FailedRunner(),
            channel_manager=channel_manager,
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )

        try:
            await manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            )
        except RuntimeError:
            # executor 应在检测到 Failed 事件后抛出 RuntimeError
            pass

        return manager, channel_manager, monitor

    manager, channel_manager, monitor = asyncio.run(_run())

    state = manager.get_state("job-cancel-after-output")
    # 验证：应该记录为 error 或 cancelled，不是 success
    assert state.last_status in ("error", "cancelled")
    # 验证：Monitor 同步应记录为 error
    assert monitor.records[-1]["status"] == "error"
    # 验证：应该看到 failed 事件的日志
    assert any("failed" in message.lower() for message in warning_messages)


def test_failed_response_after_completed_message_marks_execution_as_error():
    """A final response failure must override earlier completed messages."""

    class FailedResponseRunner:
        async def stream_query(self, _req):
            yield SimpleNamespace(
                object="message",
                status=RunStatus.Completed,
                content=[SimpleNamespace(type="text", text="retrying")],
            )
            yield SimpleNamespace(
                object="response",
                status=RunStatus.Failed,
                error=SimpleNamespace(
                    code="model_call_failed",
                    message="Output token rate limit exceeded",
                ),
            )

    async def run():
        job = _build_agent_job()
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=FailedResponseRunner(),
            channel_manager=_ChannelManager(),
        )
        manager._monitor_sync_client = monitor
        with pytest.raises(RuntimeError, match="model_call_failed"):
            await manager._execute_once(job, is_manual=False)
        state = manager.get_state(job.id)
        assert state.last_status == "error"
        assert "Output token rate limit exceeded" in state.last_error
        assert monitor.records[-1]["status"] == "error"
        assert "model_call_failed" in monitor.records[-1]["error_message"]

    asyncio.run(run())


def test_empty_stream_marks_execution_as_error():
    """当 runner 返回空流（没有任何 Completed 或 Failed 事件）时，
    应正确标记为错误而不是成功。

    这是针对模型不可用等场景的测试：
    - runner 可能返回空流而不是 yield Failed 事件
    - executor 应检测没有 Completed 事件并正确处理为失败
    """

    async def _run():
        job = _build_agent_job()
        channel_manager = _ChannelManager()
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_EmptyStreamRunner(),
            channel_manager=channel_manager,
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )

        try:
            await manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            )
        except RuntimeError:
            # executor 应在检测到没有 Completed 事件后抛出 RuntimeError
            pass

        return manager, channel_manager, monitor

    manager, channel_manager, monitor = asyncio.run(_run())

    state = manager.get_state("job-cancel-after-output")
    # 验证：应该记录为 error，不是 success
    assert state.last_status == "error"
    # 验证：Monitor 同步应记录为 error
    assert monitor.records[-1]["status"] == "error"
    # 验证：没有发送任何事件
    assert len(channel_manager.events) == 0


def test_response_failed_marks_execution_with_terminal_error(
    monkeypatch,
):
    """Cron 应识别 Runtime 标准 response/failed，而不是误报未完成。"""
    info_messages: list[str] = []

    def fake_executor_info(message, *args, **_kwargs) -> None:
        info_messages.append(str(message) % args if args else str(message))

    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "swe.app.crons.executor.logger.info",
        fake_executor_info,
    )

    async def _run():
        job = _build_agent_job()
        monitor = _MonitorSyncClient()
        channel_manager = _ChannelManager()
        manager = CronManager(
            repo=_Repo(job),
            runner=_ResponseFailedRunner(),
            channel_manager=channel_manager,
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )

        with __import__("pytest").raises(RuntimeError):
            await manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            )
        return manager, monitor, channel_manager

    manager, monitor, channel_manager = asyncio.run(_run())

    assert manager.get_state("job-cancel-after-output").last_status == "error"
    assert monitor.records[-1]["error_message"].startswith(
        "Agent execution failed: model_call_failed: Authorization: ",
    )
    assert all("secret-token" not in message for message in info_messages)
    assert any(
        "event_index=3 event_class=SimpleNamespace object=response "
        "status=failed" in message
        and "error_code=model_call_failed" in message
        for message in info_messages
    )
    assert channel_manager.events[-1].status == RunStatus.Failed
    assert len(channel_manager.texts) == 1
    assert "model_call_failed" in channel_manager.texts[0]["text"]
    assert "secret-token" not in channel_manager.texts[0]["text"]


def test_response_completed_without_message_marks_execution_as_error(
    monkeypatch,
):
    """response/completed 没有 assistant 内容时不得记为普通成功。"""
    reset_cron_result_metrics_for_test()
    info_messages: list[str] = []

    def fake_manager_info(message, *args, **_kwargs) -> None:
        info_messages.append(message % args if args else message)

    monkeypatch.setattr(
        "swe.app.crons.manager.logger.info",
        fake_manager_info,
    )
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        job = _build_agent_job()
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_ResponseCompletedEmptyRunner(),
            channel_manager=_ChannelManager(),
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )
        with pytest.raises(RuntimeError, match="empty_model_output"):
            await manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            )
        return manager, monitor

    manager, monitor = asyncio.run(_run())

    assert manager.get_state("job-cancel-after-output").last_status == "error"
    assert monitor.records[-1]["status"] == "error"
    assert monitor.records[-1]["error_message"].endswith("empty_model_output")
    assert get_cron_result_metrics()[CRON_EMPTY_MODEL_OUTPUT_TOTAL] == 1
    final_decisions = [
        message
        for message in info_messages
        if message.startswith("cron final decision:")
    ]
    assert final_decisions
    assert "response_terminal_status=completed" in final_decisions[-1]
    assert "assistant_message_count=0" in final_decisions[-1]
    assert "session_state_committed=False" in final_decisions[-1]
    assert "final_error_code=empty_model_output" in final_decisions[-1]


def test_final_decision_logs_generic_error_when_terminal_code_is_empty(
    monkeypatch,
):
    """执行 key 冲突等 cleanup 异常不能在最终决策日志中丢失错误码。"""
    info_messages: list[str] = []

    def fake_manager_info(message, *args, **_kwargs) -> None:
        info_messages.append(message % args if args else message)

    monkeypatch.setattr(
        "swe.app.crons.manager.logger.info",
        fake_manager_info,
    )

    CronManager._log_final_execution_decision(  # pylint: disable=protected-access
        _build_agent_job(),
        exec_status="error",
        trace_id="trace-1",
        error_message="execution_key_conflict",
        execution_meta={"terminal_error_code": ""},
    )

    assert "final_error_code=execution_key_conflict" in info_messages[-1]


def test_final_decision_logs_actual_task_chat_session_id(monkeypatch):
    """Final diagnostics must not substitute a task session for the Chat value."""
    info_messages: list[str] = []

    def fake_manager_info(message, *args, **_kwargs) -> None:
        info_messages.append(message % args if args else message)

    monkeypatch.setattr(
        "swe.app.crons.manager.logger.info",
        fake_manager_info,
    )

    CronManager._log_final_execution_decision(  # pylint: disable=protected-access
        _build_agent_job(),
        exec_status="success",
        trace_id="trace-1",
        error_message="",
        execution_meta=None,
        chat_session_id="persisted-chat-session",
    )

    assert "chat_session_id=persisted-chat-session" in info_messages[-1]


def test_task_binding_replaces_chat_with_mismatched_route():
    """旧 chat 的 session、用户或 scope 不一致时必须新建正确绑定。"""
    reset_cron_result_metrics_for_test()

    async def _run():
        job = _build_agent_job().model_copy(
            update={
                "tenant_id": "tenant-a",
                "source_id": "source-a",
                "scope_id": "tenant-a::source-a",
                "meta": {
                    "creator_user_id": "user-a",
                    "task_chat_id": "old-chat",
                    "task_session_id": "session-a",
                },
            },
        )
        old_chat = SimpleNamespace(
            id="old-chat",
            session_id="wrong-session",
            user_id="wrong-user",
            meta={
                "task_tenant_id": "tenant-b",
                "task_source_id": "source-b",
                "task_scope_id": "tenant-b::source-b",
            },
        )
        chat_manager = _TaskChatManager(old_chat)
        manager = CronManager(
            repo=_Repo(job),
            runner=_EmptyStreamRunner(),
            channel_manager=_ChannelManager(),
            chat_manager=chat_manager,
        )
        return await manager._ensure_task_binding(job), chat_manager

    bound, chat_manager = asyncio.run(_run())
    assert bound.meta["task_chat_id"] != "old-chat"
    assert bound.meta["task_session_id"] == "session-a"
    assert bound.request.user_id == "user-a"
    assert bound.request.session_id == "session-a"
    assert (
        chat_manager.created[0].meta["task_scope_id"] == "tenant-a::source-a"
    )
    assert get_cron_result_metrics()[CRON_SESSION_ROUTE_MISMATCH_TOTAL] == 1


def test_execution_result_keeps_runtime_diagnostics_with_model_metadata():
    """模型选择元数据不能覆盖终态和 session 提交诊断。"""
    executor = CronExecutor(runner=object(), channel_manager=object())

    result = (
        executor._build_execution_result(  # pylint: disable=protected-access
            {
                "trace_id": "trace-1",
                "execution_meta": {
                    "assistant_message_count": 1,
                    "session_state_committed": True,
                },
            },
            execution_meta={"effective_model_slot": {"model": "model-1"}},
        )
    )

    assert result.execution_meta == {
        "effective_model_slot": {"model": "model-1"},
        "assistant_message_count": 1,
        "session_state_committed": True,
    }


def test_response_completed_output_marks_execution_as_success(monkeypatch):
    """response.completed.output 中的 assistant 文本应成为成功输出。"""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        job = _build_agent_job()
        monitor = _MonitorSyncClient()
        channel_manager = _ChannelManager()
        manager = CronManager(
            repo=_Repo(job),
            runner=_ResponseCompletedOutputRunner(),
            channel_manager=channel_manager,
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )
        result = await manager._executor.execute(job)
        await manager._execute_once(
            job,
            is_manual=False,
        )  # pylint: disable=protected-access
        return manager, monitor, result, channel_manager

    manager, monitor, result, channel_manager = asyncio.run(_run())

    assert (
        manager.get_state("job-cancel-after-output").last_status == "success"
    )
    assert monitor.records[-1]["status"] == "success"
    assert result.output_preview == "response output"
    assert [event.object for event in channel_manager.events] == [
        "message",
        "message",
    ]
    assert {event.content[0].text for event in channel_manager.events} == {
        "response output",
    }


def test_message_completed_without_response_terminal_marks_execution_as_error(
    monkeypatch,
):
    """message/completed 不是 Runtime 成功终态，即使输出已提交也必须报错。"""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        job = _build_agent_job()
        manager = CronManager(
            repo=_Repo(job),
            runner=_MessageCompletedOutputOnlyRunner(),
            channel_manager=_ChannelManager(),
        )
        with pytest.raises(RuntimeError, match="did not complete"):
            await manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            )
        return manager

    manager = asyncio.run(_run())
    assert manager.get_state("job-cancel-after-output").last_status == "error"


def test_response_completed_empty_output_rejects_legacy_allow_empty_output(
    monkeypatch,
):
    """遗留 allow_empty_output 透传值不能改变空输出失败语义。"""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        job = _build_agent_job()
        job.request = job.request.model_copy(
            update={"allow_empty_output": True},
        )
        manager = CronManager(
            repo=_Repo(job),
            runner=_ResponseCompletedEmptyRunner(),
            channel_manager=_ChannelManager(),
        )
        with pytest.raises(RuntimeError, match="empty_model_output"):
            await manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            )
        return manager

    manager = asyncio.run(_run())
    assert manager.get_state("job-cancel-after-output").last_status == "error"


def test_message_completed_without_text_marks_execution_as_empty_output_error(
    monkeypatch,
):
    """完成消息无文本时不能因日志变量错误掩盖空输出状态。"""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        job = _build_agent_job()
        manager = CronManager(
            repo=_Repo(job),
            runner=_MessageCompletedEmptyRunner(),
            channel_manager=_ChannelManager(),
        )
        with pytest.raises(RuntimeError, match="empty_model_output"):
            await manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            )

    asyncio.run(_run())


def test_response_completed_with_uncommitted_session_marks_execution_as_error(
    monkeypatch,
):
    """有输出但 session 提交失败时不得产生 success。"""
    reset_cron_result_metrics_for_test()
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        job = _build_agent_job()
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_CommitFailedRunner(),
            channel_manager=_ChannelManager(),
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )
        with pytest.raises(RuntimeError, match="session_state_commit"):
            await manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            )
        return manager, monitor

    manager, monitor = asyncio.run(_run())
    assert manager.get_state("job-cancel-after-output").last_status == "error"
    assert monitor.records[-1]["status"] == "error"
    assert get_cron_result_metrics()[CRON_SESSION_COMMIT_FAILURE_TOTAL] == 1


def test_response_completed_without_persisted_assistant_marks_execution_as_error(
    monkeypatch,
):
    """The persistence receipt must confirm an assistant actually reached session."""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        manager = CronManager(
            repo=_Repo(_build_agent_job()),
            runner=_CommitWithoutAssistantRunner(),
            channel_manager=_ChannelManager(),
        )
        with pytest.raises(RuntimeError, match="persisted_assistant_missing"):
            await manager._execute_once(  # pylint: disable=protected-access
                _build_agent_job(),
                is_manual=False,
            )

    asyncio.run(_run())


def test_failed_non_console_task_does_not_push_partial_output(monkeypatch):
    """Console pushes are deferred until the execution has passed all gates."""
    pushed: list[tuple[str, str]] = []

    async def record_push(_self, session_id, text, _tenant_id, **_kwargs):
        pushed.append((session_id, text))

    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._push_to_console",
        record_push,
    )

    async def _run():
        job = _build_agent_job().model_copy(
            update={
                "dispatch": DispatchSpec(
                    channel="zhaohu",
                    target=DispatchTarget(
                        user_id="user-a",
                        session_id="session-a",
                    ),
                ),
                "meta": {
                    "task_chat_id": "chat-a",
                    "task_session_id": "task-session-a",
                },
            },
        )
        manager = CronManager(
            repo=_Repo(job),
            runner=_OutputThenResponseFailedRunner(),
            channel_manager=_ChannelManager(),
        )
        with pytest.raises(RuntimeError, match="model_error"):
            await manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            )

    asyncio.run(_run())
    assert pushed == []


def test_successful_non_console_task_uses_persisted_session_only(monkeypatch):
    """Cron output must not rely on the transient Console notification store."""
    pushed: list[tuple[str, str]] = []

    async def record_push(_self, session_id, text, _tenant_id, **_kwargs):
        pushed.append((session_id, text))

    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._push_to_console",
        record_push,
    )

    async def _run():
        job = _build_agent_job().model_copy(
            update={
                "dispatch": DispatchSpec(
                    channel="zhaohu",
                    target=DispatchTarget(
                        user_id="user-a",
                        session_id="session-a",
                    ),
                ),
                "meta": {
                    "task_chat_id": "chat-a",
                    "task_session_id": "task-session-a",
                },
            },
        )
        manager = CronManager(
            repo=_Repo(job),
            runner=_ReplayableDeliveryRunner(),
            channel_manager=_ChannelManager(),
        )
        await manager._execute_once(  # pylint: disable=protected-access
            job,
            is_manual=False,
        )

    asyncio.run(_run())
    assert pushed == []


def test_idempotent_replay_with_delivery_receipt_records_task_success(
    monkeypatch,
):
    """A confirmed output replay records task success without resending it."""
    pushed: list[tuple[str, str]] = []

    async def record_push(_self, session_id, text, _tenant_id):
        pushed.append((session_id, text))

    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._push_to_console",
        record_push,
    )

    async def _run():
        job = _build_agent_job().model_copy(
            update={
                "dispatch": DispatchSpec(
                    channel="zhaohu",
                    target=DispatchTarget(
                        user_id="user-a",
                        session_id="session-a",
                    ),
                ),
                "meta": {
                    "task_chat_id": "chat-a",
                    "task_session_id": "task-session-a",
                },
            },
        )
        channel_manager = _ChannelManager()
        manager = CronManager(
            repo=_Repo(job),
            runner=_IdempotentReplayRunner(),
            channel_manager=channel_manager,
        )
        notifications = AsyncMock()
        manager._handle_success_notifications = notifications
        await manager._execute_once(  # pylint: disable=protected-access
            job,
            is_manual=False,
        )
        return manager, notifications, channel_manager, job

    manager, notifications, channel_manager, job = asyncio.run(_run())
    assert (
        manager.get_state("job-cancel-after-output").last_status == "success"
    )
    assert pushed == []
    notifications.assert_awaited_once_with(job, "")
    assert channel_manager.events == []


def test_replay_retries_output_delivery_until_receipt_is_persisted(
    monkeypatch,
):
    """A failed final-message delivery is retried once by the scheduler replay."""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        runner = _ReplayableDeliveryRunner()
        channel_manager = _FailOnceChannelManager()
        executor = CronExecutor(runner=runner, channel_manager=channel_manager)
        job = _build_agent_job().model_copy(
            update={
                "dispatch": DispatchSpec(
                    channel="zhaohu",
                    target=DispatchTarget(
                        user_id="user-a",
                        session_id="session-a",
                    ),
                ),
            },
        )
        with pytest.raises(RuntimeError, match="channel unavailable"):
            await executor.execute(job)
        runner.idempotent_replay = True
        await executor.execute(job)
        await executor.execute(job)
        return runner, channel_manager

    runner, channel_manager = asyncio.run(_run())
    assert channel_manager.send_attempts == 2
    assert runner.delivery_marks == 1
    assert [event.object for event in channel_manager.events] == ["message"]
    assert channel_manager.events[0].content[0].text == "persisted output"


def test_unconfirmed_external_delivery_is_not_recorded_as_success(
    monkeypatch,
):
    """A skipped channel delivery must not produce a durable receipt."""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        runner = _ReplayableDeliveryRunner()
        executor = CronExecutor(
            runner=runner,
            channel_manager=_UnconfirmedChannelManager(),
        )
        job = _build_agent_job().model_copy(
            update={
                "dispatch": DispatchSpec(
                    channel="zhaohu",
                    target=DispatchTarget(
                        user_id="user-a",
                        session_id="session-a",
                    ),
                ),
            },
        )
        with pytest.raises(RuntimeError, match="delivery not confirmed"):
            await executor.execute(job)
        return runner

    runner = asyncio.run(_run())

    assert runner.delivery_marks == 0


def test_text_replay_uses_persisted_delivery_receipt(monkeypatch):
    """Text jobs persist their result before an external send and replay once."""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        session = _TaskSession()
        channel_manager = _ChannelManager()
        executor = CronExecutor(
            runner=SimpleNamespace(session=session),
            channel_manager=channel_manager,
        )
        job = _build_agent_job().model_copy(
            update={
                "task_type": "text",
                "text": "scheduled text",
                "request": None,
                "dispatch": DispatchSpec(
                    channel="zhaohu",
                    target=DispatchTarget(
                        user_id="user-a",
                        session_id="session-a",
                    ),
                ),
                "meta": {
                    "creator_user_id": "user-a",
                    "task_session_id": "task-session-a",
                },
            },
        )
        await executor.execute(
            job,
            dispatch_meta={"cron_execution_key": "execution-1"},
        )
        await executor.execute(
            job,
            dispatch_meta={"cron_execution_key": "execution-1"},
        )
        return session.state, channel_manager

    state, channel_manager = asyncio.run(_run())

    assert len(state["task_messages"]) == 1
    assert (
        state["task_messages"][0]["metadata"]["output_delivery_completed"]
        is True
    )
    assert (
        state["task_messages"][0]["metadata"]["output_delivery_state"]
        == "completed"
    )
    assert len(channel_manager.texts) == 1


def test_unconfirmed_text_delivery_keeps_persisted_retry_state() -> None:
    class _UnconfirmedTextChannelManager(_ChannelManager):
        async def send_text(self, **kwargs) -> bool:
            self.texts.append(kwargs)
            return False

    async def _run():
        session = _TaskSession()
        executor = CronExecutor(
            runner=SimpleNamespace(session=session),
            channel_manager=_UnconfirmedTextChannelManager(),
        )
        job = _build_agent_job().model_copy(
            update={
                "task_type": "text",
                "text": "scheduled text",
                "request": None,
                "dispatch": DispatchSpec(
                    channel="zhaohu",
                    target=DispatchTarget(
                        user_id="user-a",
                        session_id="session-a",
                    ),
                ),
                "meta": {
                    "creator_user_id": "user-a",
                    "task_session_id": "task-session-a",
                },
            },
        )
        with pytest.raises(RuntimeError, match="text delivery not confirmed"):
            await executor.execute(
                job,
                dispatch_meta={"cron_execution_key": "execution-1"},
            )
        return session.state

    state = asyncio.run(_run())

    assert (
        state["task_messages"][0]["metadata"]["output_delivery_completed"]
        is False
    )


def test_text_delivery_remains_successful_when_trace_cleanup_is_cancelled(
    monkeypatch,
) -> None:
    async def _run():
        trace_cleanup_started = asyncio.Event()
        session = _TaskSession()
        executor = CronExecutor(
            runner=SimpleNamespace(session=session),
            channel_manager=_ChannelManager(),
        )
        job = _build_agent_job().model_copy(
            update={
                "task_type": "text",
                "text": "scheduled text",
                "request": None,
                "dispatch": DispatchSpec(
                    channel="zhaohu",
                    target=DispatchTarget(
                        user_id="user-a",
                        session_id="session-a",
                    ),
                ),
                "meta": {
                    "creator_user_id": "user-a",
                    "task_session_id": "task-session-a",
                },
            },
        )

        async def _create_trace(*_args, **_kwargs):
            return "trace-text"

        async def _end_trace(*_args, **_kwargs):
            trace_cleanup_started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(
            executor,
            "_create_trace_for_text_job",
            _create_trace,
        )
        monkeypatch.setattr(executor, "_end_trace_for_text_job", _end_trace)
        task = asyncio.create_task(
            executor.execute(
                job,
                dispatch_meta={"cron_execution_key": "execution-1"},
            ),
        )
        await trace_cleanup_started.wait()
        task.cancel()
        result = await task
        return result, session.state

    result, state = asyncio.run(_run())

    assert result.status == "success"
    assert (
        state["task_messages"][0]["metadata"]["output_delivery_completed"]
        is True
    )


def test_task_text_delivery_requires_an_execution_identity() -> None:
    async def _run():
        session = _TaskSession()
        channel_manager = _ChannelManager()
        executor = CronExecutor(
            runner=SimpleNamespace(session=session),
            channel_manager=channel_manager,
        )
        job = _build_agent_job().model_copy(
            update={
                "task_type": "text",
                "text": "scheduled text",
                "request": None,
                "dispatch": DispatchSpec(
                    channel="zhaohu",
                    target=DispatchTarget(
                        user_id="user-a",
                        session_id="session-a",
                    ),
                ),
                "meta": {
                    "creator_user_id": "user-a",
                    "task_session_id": "task-session-a",
                },
            },
        )

        with pytest.raises(RuntimeError, match="execution identity"):
            await executor.execute(job)
        return session.state, channel_manager

    state, channel_manager = asyncio.run(_run())

    assert state == {}
    assert channel_manager.texts == []


def test_text_delivery_without_a_task_requires_an_execution_identity() -> None:
    async def _run():
        channel_manager = _ChannelManager()
        executor = CronExecutor(
            runner=SimpleNamespace(),
            channel_manager=channel_manager,
        )
        job = _build_agent_job().model_copy(
            update={
                "task_type": "text",
                "text": "scheduled text",
                "request": None,
                "dispatch": DispatchSpec(
                    channel="zhaohu",
                    target=DispatchTarget(
                        user_id="user-a",
                        session_id="session-a",
                    ),
                ),
                "meta": {},
            },
        )

        with pytest.raises(RuntimeError, match="execution identity"):
            await executor.execute(job)
        return channel_manager

    channel_manager = asyncio.run(_run())

    assert channel_manager.texts == []


def test_text_delivery_requires_a_persisted_task_message() -> None:
    async def _run():
        channel_manager = _ChannelManager()
        executor = CronExecutor(
            runner=SimpleNamespace(),
            channel_manager=channel_manager,
        )
        job = _build_agent_job().model_copy(
            update={
                "task_type": "text",
                "text": "scheduled text",
                "request": None,
                "dispatch": DispatchSpec(
                    channel="zhaohu",
                    target=DispatchTarget(
                        user_id="user-a",
                        session_id="session-a",
                    ),
                ),
                "meta": {},
            },
        )

        with pytest.raises(RuntimeError, match="persistence unavailable"):
            await executor.execute(
                job,
                dispatch_meta={"cron_execution_key": "execution-1"},
            )
        return channel_manager

    channel_manager = asyncio.run(_run())

    assert channel_manager.texts == []


def test_text_delivery_rejects_execution_key_content_conflict() -> None:
    async def _run():
        session = _TaskSession()
        channel_manager = _ChannelManager()
        executor = CronExecutor(
            runner=SimpleNamespace(session=session),
            channel_manager=channel_manager,
        )
        job = _build_agent_job().model_copy(
            update={
                "task_type": "text",
                "text": "original text",
                "request": None,
                "dispatch": DispatchSpec(
                    channel="zhaohu",
                    target=DispatchTarget(
                        user_id="user-a",
                        session_id="session-a",
                    ),
                ),
                "meta": {
                    "creator_user_id": "user-a",
                    "task_session_id": "task-session-a",
                },
            },
        )
        dispatch_meta = {"cron_execution_key": "execution-1"}
        await executor.execute(job, dispatch_meta=dispatch_meta)

        with pytest.raises(RuntimeError, match="execution_key_conflict"):
            await executor.execute(
                job.model_copy(update={"text": "updated text"}),
                dispatch_meta=dispatch_meta,
            )
        return session.state, channel_manager

    state, channel_manager = asyncio.run(_run())

    assert len(channel_manager.texts) == 1
    assert state["task_messages"][0]["content"][0]["text"] == "original text"


def test_automatic_agent_delivery_requires_an_execution_identity(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        channel_manager = _ChannelManager()
        executor = CronExecutor(
            runner=_ResponseCompletedOutputRunner(),
            channel_manager=channel_manager,
        )

        with pytest.raises(RuntimeError, match="execution identity"):
            await executor.execute(
                _build_agent_job(),
                dispatch_meta={"cron_is_manual": False},
            )
        return channel_manager

    channel_manager = asyncio.run(_run())

    assert channel_manager.events == []


def test_console_text_delivery_requires_persisted_task_message() -> None:
    async def _run():
        channel_manager = _ChannelManager()
        executor = CronExecutor(
            runner=SimpleNamespace(),
            channel_manager=channel_manager,
        )
        job = _build_agent_job().model_copy(
            update={
                "task_type": "text",
                "text": "scheduled text",
                "request": None,
                "meta": {},
            },
        )

        with pytest.raises(RuntimeError, match="persistence unavailable"):
            await executor.execute(
                job,
                dispatch_meta={"cron_execution_key": "execution-1"},
            )
        return channel_manager

    channel_manager = asyncio.run(_run())

    assert channel_manager.texts == []


def test_blank_text_task_is_not_recorded_as_success() -> None:
    async def _run():
        executor = CronExecutor(
            runner=SimpleNamespace(session=_TaskSession()),
            channel_manager=_ChannelManager(),
        )
        job = _build_agent_job().model_copy(
            update={
                "task_type": "text",
                "text": "   ",
                "request": None,
            },
        )
        with pytest.raises(RuntimeError, match="empty text output"):
            await executor.execute(job)

    asyncio.run(_run())


def test_console_output_does_not_complete_external_delivery_receipt(
    monkeypatch,
):
    """A process-local Console notification is not a durable delivery ack."""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        runner = _ReplayableDeliveryRunner()
        executor = CronExecutor(
            runner=runner,
            channel_manager=_ChannelManager(),
        )
        await executor.execute(_build_agent_job())
        return runner

    runner = asyncio.run(_run())

    assert runner.delivery_marks == 0


def test_console_session_delivery_failure_is_non_fatal(tmp_path):
    """A local Console send failure must not fail the Cron execution."""

    async def _run():
        session = SafeJSONSession(str(tmp_path))
        await session.mutate_session_state(
            session_id="session-a",
            user_id="user-a",
            mutator=lambda _state: {
                "task_runs": [
                    {
                        "execution_key": "execution-1",
                        "persistence_key": "persistence-1",
                        "output_delivery_completed": False,
                    },
                ],
            },
        )
        executor = CronExecutor(
            runner=SimpleNamespace(session=session),
            channel_manager=_UnconfirmedChannelManager(),
        )
        stream_state = AgentStreamState(
            output_delivery_receipt_supported=True,
            completed_message_event=SimpleNamespace(
                object="message",
                status=RunStatus.Completed,
                content=[SimpleNamespace(type="text", text="output")],
            ),
        )
        job = _build_agent_job()
        req = {
            "session_id": "session-a",
            "user_id": "user-a",
            "cron_execution_key": "execution-1",
            "cron_persistence_key": "persistence-1",
        }
        await executor._deliver_persisted_agent_output(  # pylint: disable=protected-access
            job,
            "user-a",
            "session-a",
            {},
            stream_state,
            req,
        )
        state = await session.get_session_state_dict(
            "session-a",
            user_id="user-a",
        )
        return state, stream_state

    state, stream_state = asyncio.run(_run())

    assert state["task_runs"][0]["output_delivery_completed"] is True
    assert state["task_runs"][0]["output_delivery_state"] == "completed"
    assert stream_state.output_delivery_completed is True


def test_console_session_delivery_exception_is_non_fatal(tmp_path):
    """A local Console send exception must not fail the Cron execution."""

    async def _run():
        session = SafeJSONSession(str(tmp_path))
        await session.mutate_session_state(
            session_id="session-a",
            user_id="user-a",
            mutator=lambda _state: {
                "task_runs": [
                    {
                        "execution_key": "execution-1",
                        "persistence_key": "persistence-1",
                        "output_delivery_completed": False,
                    },
                ],
            },
        )
        executor = CronExecutor(
            runner=SimpleNamespace(session=session),
            channel_manager=_FailOnceChannelManager(),
        )
        stream_state = AgentStreamState(
            output_delivery_receipt_supported=True,
            completed_message_event=SimpleNamespace(
                object="message",
                status=RunStatus.Completed,
                content=[SimpleNamespace(type="text", text="output")],
            ),
        )
        job = _build_agent_job()
        req = {
            "session_id": "session-a",
            "user_id": "user-a",
            "cron_execution_key": "execution-1",
            "cron_persistence_key": "persistence-1",
        }
        await executor._deliver_persisted_agent_output(  # pylint: disable=protected-access
            job,
            "user-a",
            "session-a",
            {},
            stream_state,
            req,
        )
        state = await session.get_session_state_dict(
            "session-a",
            user_id="user-a",
        )
        return state, stream_state

    state, stream_state = asyncio.run(_run())

    assert state["task_runs"][0]["output_delivery_completed"] is True
    assert state["task_runs"][0]["output_delivery_state"] == "completed"
    assert stream_state.output_delivery_completed is True


def test_console_runner_delivery_exception_is_non_fatal():
    """The runner-receipt fallback ignores local Console send failures."""

    async def _run():
        runner = _ReplayableDeliveryRunner()
        channel_manager = _FailOnceChannelManager()
        executor = CronExecutor(
            runner=runner,
            channel_manager=channel_manager,
        )
        stream_state = AgentStreamState(
            output_delivery_receipt_supported=True,
            completed_message_event=SimpleNamespace(
                object="message",
                status=RunStatus.Completed,
                content=[SimpleNamespace(type="text", text="output")],
            ),
        )
        await executor._deliver_persisted_agent_output(  # pylint: disable=protected-access
            _build_agent_job(),
            "user-a",
            "session-a",
            {},
            stream_state,
            {
                "session_id": "session-a",
                "user_id": "user-a",
                "cron_execution_key": "execution-1",
                "cron_persistence_key": "persistence-1",
            },
        )
        return runner, channel_manager, stream_state

    runner, channel_manager, stream_state = asyncio.run(_run())

    assert channel_manager.send_attempts == 1
    assert runner.delivery_marks == 0
    assert stream_state.output_delivery_completed is False


def test_legacy_replay_does_not_redeliver_without_delivery_schema(monkeypatch):
    """Old task runs are skipped rather than replaying unknown side effects."""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        runner = _LegacyReplayDeliveryRunner()
        channel_manager = _ChannelManager()
        job = _build_agent_job().model_copy(
            update={
                "meta": {
                    "creator_user_id": "user-a",
                    "task_session_id": "session-a",
                },
            },
        )
        repo = _Repo(job)
        manager = CronManager(
            repo=repo,
            runner=runner,
            channel_manager=channel_manager,
        )
        await manager._execute_once(  # pylint: disable=protected-access
            job,
            is_manual=False,
            dispatch_meta={"cron_execution_key": "execution-1"},
        )
        return runner, channel_manager, repo._job

    runner, channel_manager, job = asyncio.run(_run())
    assert channel_manager.events == []
    assert runner.delivery_marks == 0
    assert job.meta.get("task_unread_execution_count", 0) == 0


def test_legacy_external_replay_without_delivery_schema_fails(
    monkeypatch,
):
    """Unknown external delivery state cannot be recorded as success."""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        runner = _LegacyReplayDeliveryRunner()
        channel_manager = _ChannelManager()
        job = _build_agent_job().model_copy(
            update={
                "dispatch": DispatchSpec(
                    channel="zhaohu",
                    target=DispatchTarget(
                        user_id="user-a",
                        session_id="session-a",
                    ),
                ),
                "meta": {
                    "creator_user_id": "user-a",
                    "task_session_id": "session-a",
                },
            },
        )
        manager = CronManager(
            repo=_Repo(job),
            runner=runner,
            channel_manager=channel_manager,
        )
        with pytest.raises(RuntimeError, match="receipt unavailable"):
            await manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
                dispatch_meta={"cron_execution_key": "execution-1"},
            )
        return manager, channel_manager

    manager, channel_manager = asyncio.run(_run())

    assert manager.get_state("job-cancel-after-output").last_status == "error"
    assert channel_manager.events == []


def test_console_delivery_failure_records_success_effects_once(monkeypatch):
    """Console delivery failures still apply success effects once."""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        job = _build_agent_job().model_copy(
            update={
                "meta": {
                    "creator_user_id": "user-a",
                    "task_session_id": "session-a",
                },
            },
        )
        runner = _ReplayableDeliveryRunner()
        channel_manager = _FailOnceChannelManager()
        manager = CronManager(
            repo=_Repo(job),
            runner=runner,
            channel_manager=channel_manager,
        )
        await manager._execute_once(  # pylint: disable=protected-access
            job,
            is_manual=False,
            dispatch_meta={"cron_execution_key": "execution-1"},
        )
        runner.idempotent_replay = True
        await manager._execute_once(  # pylint: disable=protected-access
            job,
            is_manual=False,
            dispatch_meta={"cron_execution_key": "execution-1"},
        )
        await manager._execute_once(  # pylint: disable=protected-access
            job,
            is_manual=False,
            dispatch_meta={"cron_execution_key": "execution-1"},
        )
        return manager._repo._job

    job = asyncio.run(_run())
    assert job.meta["task_unread_execution_count"] == 1


def test_task_success_effects_deduplicate_the_execution_key() -> None:
    """A scheduler replay cannot increment task unread count twice."""

    async def _run():
        job = _build_agent_job().model_copy(
            update={
                "meta": {
                    "creator_user_id": "user-a",
                    "task_session_id": "session-a",
                },
            },
        )
        repo = _Repo(job)
        manager = CronManager(
            repo=repo,
            runner=SimpleNamespace(session=None),
            channel_manager=_ChannelManager(),
        )
        await manager._record_task_execution_success(  # pylint: disable=protected-access
            job,
            "execution-1",
        )
        await manager._record_task_execution_success(  # pylint: disable=protected-access
            job,
            "execution-1",
        )
        return repo._job

    job = asyncio.run(_run())
    assert job.meta["task_unread_execution_count"] == 1


def test_text_task_replay_does_not_duplicate_session_message() -> None:
    async def _run():
        job = _build_agent_job().model_copy(
            update={
                "task_type": "text",
                "text": "scheduled text",
                "meta": {
                    "creator_user_id": "user-a",
                    "task_session_id": "session-a",
                },
            },
        )
        session = _TaskSession()
        manager = CronManager(
            repo=_Repo(job),
            runner=SimpleNamespace(session=session),
            channel_manager=_ChannelManager(),
        )
        await manager._record_task_execution_success(job, "execution-1")
        await manager._record_task_execution_success(job, "execution-1")
        return session.state

    state = asyncio.run(_run())
    assert len(state["task_messages"]) == 1


def test_text_task_replay_recovers_message_after_session_write_failure() -> (
    None
):
    async def _run():
        job = _build_agent_job().model_copy(
            update={
                "task_type": "text",
                "text": "scheduled text",
                "meta": {
                    "creator_user_id": "user-a",
                    "task_session_id": "session-a",
                },
            },
        )
        session = _FailOnceTaskSession()
        repo = _Repo(job)
        manager = CronManager(
            repo=repo,
            runner=SimpleNamespace(session=session),
            channel_manager=_ChannelManager(),
        )
        with pytest.raises(RuntimeError, match="session unavailable"):
            await manager._record_task_execution_success(job, "execution-1")
        assert not repo._job.meta.get("task_has_scheduled_result")
        await manager._record_task_execution_success(job, "execution-1")
        return session.state

    state = asyncio.run(_run())
    assert len(state["task_messages"]) == 1


def test_legacy_persistence_receipt_fails_before_external_delivery(
    monkeypatch,
):
    """External Agent delivery requires a durable receipt implementation."""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        job = _build_agent_job().model_copy(
            update={
                "dispatch": DispatchSpec(
                    channel="zhaohu",
                    target=DispatchTarget(
                        user_id="user-a",
                        session_id="session-a",
                    ),
                ),
            },
        )
        channel_manager = _ChannelManager()
        manager = CronManager(
            repo=_Repo(job),
            runner=_LegacyPersistenceReceiptRunner(),
            channel_manager=channel_manager,
        )
        with pytest.raises(
            RuntimeError,
            match="output delivery receipt unavailable",
        ):
            await manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            )
        return manager, channel_manager

    manager, channel_manager = asyncio.run(_run())
    assert manager.get_state("job-cancel-after-output").last_status == "error"
    assert channel_manager.events == []


def test_response_cancelled_terminal_does_not_become_timeout(monkeypatch):
    """A terminal cancellation closes the iterator rather than waiting for timeout."""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        manager = CronManager(
            repo=_Repo(_build_agent_job()),
            runner=_ResponseCancelledThenBlockedRunner(),
            channel_manager=_ChannelManager(),
        )
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(
                manager._execute_once(  # pylint: disable=protected-access
                    _build_agent_job(),
                    is_manual=False,
                ),
                timeout=0.2,
            )

    asyncio.run(_run())


def test_message_failed_terminal_does_not_become_timeout(monkeypatch):
    """A terminal message failure closes a blocked iterator immediately."""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        manager = CronManager(
            repo=_Repo(_build_agent_job()),
            runner=_MessageFailedThenBlockedRunner(),
            channel_manager=_ChannelManager(),
        )
        with pytest.raises(RuntimeError, match="model_error: failed"):
            await asyncio.wait_for(
                manager._execute_once(  # pylint: disable=protected-access
                    _build_agent_job(),
                    is_manual=False,
                ),
                timeout=0.2,
            )

    asyncio.run(_run())


def test_sequence_number_deduplicates_message_and_response_output(monkeypatch):
    """Sequence-only Runtime output is not duplicated in the stored preview."""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        executor = CronExecutor(
            runner=_SequenceDeduplicatedOutputRunner(),
            channel_manager=_ChannelManager(),
        )
        return await executor.execute(_build_agent_job())

    assert asyncio.run(_run()).output_preview == "sequence output"


def test_outer_sequence_number_deduplicates_message_and_response_output(
    monkeypatch,
):
    """An outer response sequence identifies its single assistant output."""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        executor = CronExecutor(
            runner=_OuterSequenceDeduplicatedOutputRunner(),
            channel_manager=_ChannelManager(),
        )
        return await executor.execute(_build_agent_job())

    assert asyncio.run(_run()).output_preview == "outer sequence output"


def test_response_completed_terminal_does_not_wait_for_tail_stream(
    monkeypatch,
):
    """A complete response produces success even when the iterator stalls."""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        manager = CronManager(
            repo=_Repo(_build_agent_job()),
            runner=_ResponseCompletedThenBlockedRunner(),
            channel_manager=_ChannelManager(),
        )
        await asyncio.wait_for(
            manager._execute_once(  # pylint: disable=protected-access
                _build_agent_job(),
                is_manual=False,
            ),
            timeout=0.2,
        )
        return manager

    manager = asyncio.run(_run())
    assert (
        manager.get_state("job-cancel-after-output").last_status == "success"
    )


def test_completed_cron_delivers_only_final_assistant_message(monkeypatch):
    """Progress events are neither streamed nor retained for final delivery."""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        channel_manager = _ChannelManager()
        executor = CronExecutor(
            runner=_ProgressThenCompletedRunner(),
            channel_manager=channel_manager,
        )
        await executor.execute(_build_agent_job())
        return channel_manager

    channel_manager = asyncio.run(_run())
    assert [event.object for event in channel_manager.events] == ["message"]


def test_response_completed_without_persistence_receipt_marks_execution_as_error(
    monkeypatch,
):
    """没有 session 回执接口时不能猜测提交成功。"""
    reset_cron_result_metrics_for_test()
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        job = _build_agent_job()
        manager = CronManager(
            repo=_Repo(job),
            runner=_ResponseCompletedOutputWithoutPersistenceRunner(),
            channel_manager=_ChannelManager(),
        )
        with pytest.raises(
            RuntimeError,
            match="persistence_result_unavailable",
        ):
            await manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            )
        return manager

    manager = asyncio.run(_run())
    assert manager.get_state("job-cancel-after-output").last_status == "error"
    assert get_cron_result_metrics()[CRON_SESSION_COMMIT_FAILURE_TOTAL] == 1


def test_message_and_response_output_are_deduplicated(monkeypatch):
    """message/completed 与 response.output 同一消息只应展示一次。"""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        job = _build_agent_job()
        manager = CronManager(
            repo=_Repo(job),
            runner=_MessageAndResponseOutputRunner(),
            channel_manager=_ChannelManager(),
        )
        return await manager._executor.execute(job)

    result = asyncio.run(_run())
    assert result.output_preview == "deduplicated output"


def test_response_cancelled_closes_trace_once_with_terminal_reason(
    monkeypatch,
):
    """response/canceled 只应结束一次 Trace，且保留 Runtime 原因。"""
    trace_ends: list[tuple[object, ...]] = []

    async def fake_start_trace(**_kwargs):
        return "response-cancelled-trace"

    async def fake_end_trace(*args):
        trace_ends.append(args)

    monkeypatch.setattr(
        "swe.app.crons.executor.has_trace_manager",
        lambda: True,
    )
    monkeypatch.setattr(
        "swe.app.crons.executor.get_trace_manager",
        lambda: SimpleNamespace(
            enabled=True,
            start_trace=fake_start_trace,
            end_trace=fake_end_trace,
        ),
    )
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        job = _build_agent_job()
        manager = CronManager(
            repo=_Repo(job),
            runner=_ResponseCancelledRunner(),
            channel_manager=_ChannelManager(),
        )
        with pytest.raises(asyncio.CancelledError):
            await manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            )
        return manager

    manager = asyncio.run(_run())

    assert (
        manager.get_state("job-cancel-after-output").last_status == "cancelled"
    )
    assert trace_ends == [
        (
            "response-cancelled-trace",
            TraceStatus.CANCELLED,
            "upstream_cancelled: Upstream request was cancelled",
        ),
    ]


@pytest.mark.parametrize(
    "status",
    [RunStatus.Rejected, RunStatus.Incomplete],
)
def test_response_terminal_failure_marks_execution_with_terminal_error(
    monkeypatch,
    status: RunStatus,
):
    """Runtime 的 rejected/incomplete 必须保留终态错误信息。"""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        job = _build_agent_job()
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_ResponseTerminalFailureRunner(status),
            channel_manager=_ChannelManager(),
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )
        with pytest.raises(RuntimeError, match="Agent execution failed"):
            await manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            )
        return manager, monitor

    manager, monitor = asyncio.run(_run())

    assert manager.get_state("job-cancel-after-output").last_status == "error"
    assert monitor.records[-1]["error_message"] == (
        "Agent execution failed: terminal_error: "
        "Runtime ended without a completed response"
    )


@pytest.mark.parametrize(
    "status",
    [RunStatus.Rejected, RunStatus.Incomplete],
)
def test_response_terminal_failure_without_error_uses_status(
    monkeypatch,
    status: RunStatus,
):
    """无 error payload 时，终态状态仍应出现在失败诊断中。"""
    monkeypatch.setattr(
        "swe.app.crons.executor.CronExecutor._resolve_execution_model",
        lambda *_args: None,
    )

    async def _run():
        job = _build_agent_job()
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_ResponseTerminalFailureRunner(status, include_error=False),
            channel_manager=_ChannelManager(),
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )
        with pytest.raises(
            RuntimeError,
            match=rf"Agent execution failed: {status}",
        ):
            await manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            )
        return monitor

    monitor = asyncio.run(_run())

    assert monitor.records[-1]["error_message"] == (
        f"Agent execution failed: {status}"
    )


def test_failed_execution_preserves_trace_id(monkeypatch):
    """验证执行失败时 trace_id 仍能正确传递到 Monitor。

    这是确保 trace_id 在失败场景下也能被保存的关键测试：
    - executor 在失败时应将 trace_id 附加到异常
    - manager 应从异常获取 trace_id
    - trace_id 应被同步到 Monitor
    """
    fake_trace_id = "test-trace-id-for-failure"

    async def fake_start_trace(**_kwargs):
        return fake_trace_id

    async def fake_end_trace(*_args, **_kwargs):
        return None

    # Mock trace_manager 使 trace_id 被创建
    monkeypatch.setattr(
        "swe.app.crons.executor.has_trace_manager",
        lambda: True,
    )
    monkeypatch.setattr(
        "swe.app.crons.executor.get_trace_manager",
        lambda: SimpleNamespace(
            enabled=True,
            start_trace=fake_start_trace,
            end_trace=fake_end_trace,
        ),
    )

    async def _run():
        job = _build_agent_job()
        channel_manager = _ChannelManager()
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_EmptyStreamRunner(),
            channel_manager=channel_manager,
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )

        try:
            await manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            )
        except RuntimeError:
            pass

        return monitor

    monitor = asyncio.run(_run())

    # 验证：trace_id 应被正确传递到 Monitor
    assert monitor.records[-1]["status"] == "error"
    assert monitor.records[-1]["trace_id"] == fake_trace_id


def test_auth_failure_preserves_trace_id_and_raises(monkeypatch):
    fake_trace_id = "test-trace-id-for-auth-failure"
    end_trace_calls: list[tuple[object, ...]] = []

    async def fake_start_trace(**_kwargs):
        return fake_trace_id

    async def fake_end_trace(*args, **_kwargs):
        end_trace_calls.append(args)

    async def fake_resolve_auth_token_for_execution(**_kwargs):
        raise ValueError("cron auth user_info is expired")

    monkeypatch.setattr(
        "swe.app.crons.executor.has_trace_manager",
        lambda: True,
    )
    monkeypatch.setattr(
        "swe.app.crons.executor.get_trace_manager",
        lambda: SimpleNamespace(
            enabled=True,
            start_trace=fake_start_trace,
            end_trace=fake_end_trace,
        ),
    )
    monkeypatch.setattr(
        "swe.app.crons.executor.resolve_auth_token_for_execution",
        fake_resolve_auth_token_for_execution,
    )

    async def _run():
        job = _build_agent_job()
        channel_manager = _ChannelManager()
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_Runner(),
            channel_manager=channel_manager,
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )

        captured_error = None
        try:
            await manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            )
        except RuntimeError as exc:
            captured_error = exc

        return captured_error, monitor

    captured_error, monitor = asyncio.run(_run())

    assert captured_error is not None
    assert str(captured_error) == (
        "cron auth user_info is expired; "
        "please refresh cron auth configuration"
    )
    assert monitor.records[-1]["status"] == "error"
    assert monitor.records[-1]["trace_id"] == fake_trace_id
    assert end_trace_calls == [
        (
            fake_trace_id,
            TraceStatus.ERROR,
            (
                "cron auth user_info is expired; "
                "please refresh cron auth configuration"
            ),
        ),
    ]


def test_manual_broadcast_execution_does_not_delay_notification():
    """手动执行分发任务时，不应沿用原计划的通知延迟。"""

    async def _run():
        job = _build_broadcast_agent_job()
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_Runner(),
            channel_manager=_ChannelManager(),
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )
        actual_time = datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)

        await manager._sync_execution_to_monitor(  # pylint: disable=protected-access
            job=job,
            exec_status="success",
            actual_time=actual_time,
            end_time=actual_time,
            duration_ms=100,
            error_message="",
            output_preview="done",
            is_manual=True,
        )

        return monitor.records[-1]

    record = asyncio.run(_run())

    assert record["notification_due_at"] is None


def test_automatic_broadcast_execution_keeps_original_schedule_delay():
    """自动执行分发任务时，仍按分发 offset 延迟通知。"""

    async def _run():
        job = _build_broadcast_agent_job()
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_Runner(),
            channel_manager=_ChannelManager(),
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )
        actual_time = datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)

        await manager._sync_execution_to_monitor(  # pylint: disable=protected-access
            job=job,
            exec_status="success",
            actual_time=actual_time,
            end_time=actual_time,
            duration_ms=100,
            error_message="",
            output_preview="done",
            is_manual=False,
        )

        return monitor.records[-1], actual_time

    record, actual_time = asyncio.run(_run())

    assert record["notification_due_at"] == actual_time + timedelta(minutes=20)
    assert record["notification_timezone"] == "Asia/Shanghai"


def test_automatic_broadcast_execution_stacks_notification_delay():
    """自动执行分发子任务时，通知延迟应叠加在分发 offset 之后。"""

    async def _run():
        job = _build_broadcast_agent_job().model_copy(
            update={
                "meta": {
                    **_build_broadcast_agent_job().meta,
                    "notification_delay_minutes": 120,
                },
            },
        )
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_Runner(),
            channel_manager=_ChannelManager(),
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )
        actual_time = datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)

        await manager._sync_execution_to_monitor(  # pylint: disable=protected-access
            job=job,
            exec_status="success",
            actual_time=actual_time,
            end_time=actual_time,
            duration_ms=100,
            error_message="",
            output_preview="done",
            is_manual=False,
        )

        return monitor.records[-1], actual_time

    record, actual_time = asyncio.run(_run())

    assert record["notification_due_at"] == actual_time + timedelta(
        minutes=140,
    )
    assert record["notification_timezone"] == "Asia/Shanghai"


def test_dispatch_managed_broadcast_notification_uses_parent_fire_time():
    """Dispatch-managed batch children ignore broadcast offset for notification."""

    async def _run():
        job = _build_broadcast_agent_job().model_copy(
            update={
                "meta": {
                    **_build_broadcast_agent_job().meta,
                    "notification_delay_minutes": 120,
                    "broadcast_dispatch_intents_enabled": True,
                },
            },
        )
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_Runner(),
            channel_manager=_ChannelManager(),
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )
        actual_time = datetime(2026, 6, 4, 10, 20, tzinfo=timezone.utc)
        parent_fire_at = datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)

        await manager._sync_execution_to_monitor(  # pylint: disable=protected-access
            job=job,
            exec_status="success",
            actual_time=actual_time,
            end_time=actual_time,
            duration_ms=100,
            error_message="",
            output_preview="done",
            is_manual=False,
            execution_meta={
                "cron_dispatch": {
                    "intent_id": 7,
                    "batch_id": "batch-1",
                    "parent_scheduled_fire_at": parent_fire_at.isoformat(),
                },
            },
        )

        return monitor.records[-1], parent_fire_at

    record, parent_fire_at = asyncio.run(_run())

    assert record["notification_due_at"] == parent_fire_at + timedelta(
        minutes=120,
    )
    assert record["notification_timezone"] == "Asia/Shanghai"


def test_dispatch_managed_weekend_notification_uses_task_timezone():
    """Scheduler 回写路径也按任务时区判断周末抑制。"""

    async def _run():
        base_job = _build_broadcast_agent_job()
        job = base_job.model_copy(
            update={
                "meta": {
                    **base_job.meta,
                    "notification_delay_minutes": 60,
                    "broadcast_dispatch_intents_enabled": True,
                    "broadcast_original_timezone": "UTC",
                },
            },
        )
        effective = EffectiveSourceSystemConfig(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                {
                    "cron_notifications": {
                        "skip_weekend_zhaohu_enabled": True,
                    },
                },
            ).merged_with_defaults(),
            raw_config=SourceSystemConfig.model_validate(
                {
                    "cron_notifications": {
                        "skip_weekend_zhaohu_enabled": True,
                    },
                },
            ),
            version=3,
        )
        monitor = _MonitorSyncClient()
        manager = CronManager(
            repo=_Repo(job),
            runner=_Runner(),
            channel_manager=_ChannelManager(),
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )
        actual_time = datetime(2026, 6, 5, 15, 45, tzinfo=timezone.utc)
        parent_fire_at = datetime(2026, 6, 5, 15, 30, tzinfo=timezone.utc)

        with bind_source_system_config(effective):
            await manager._sync_execution_to_monitor(  # pylint: disable=protected-access
                job=job,
                exec_status="success",
                actual_time=actual_time,
                end_time=actual_time,
                duration_ms=100,
                error_message="",
                output_preview="done",
                is_manual=False,
                execution_meta={
                    "cron_dispatch": {
                        "intent_id": 7,
                        "batch_id": "batch-1",
                        "parent_scheduled_fire_at": parent_fire_at.isoformat(),
                    },
                },
            )

        return monitor.records[-1], parent_fire_at

    record, parent_fire_at = asyncio.run(_run())

    assert record["notification_due_at"] == parent_fire_at + timedelta(
        minutes=60,
    )
    assert record["notification_timezone"] == "UTC"
    assert record["suppress_notification"] is False


def test_text_delivery_serializes_same_execution_key_on_shared_session(
    tmp_path,
):
    """A second worker must observe the first worker's durable receipt."""

    class _BlockingTextChannel:
        def __init__(self) -> None:
            self.send_started = asyncio.Event()
            self.release_send = asyncio.Event()
            self.send_attempts = 0

        async def send_text(self, **_kwargs) -> bool:
            self.send_attempts += 1
            self.send_started.set()
            await self.release_send.wait()
            return True

    async def _run() -> tuple[int, dict]:
        channel = _BlockingTextChannel()
        job = _build_agent_job().model_copy(
            update={
                "task_type": "text",
                "text": "scheduled text",
                "request": None,
                "dispatch": DispatchSpec(
                    channel="zhaohu",
                    target=DispatchTarget(
                        user_id="user-a",
                        session_id="session-a",
                    ),
                ),
                "meta": {
                    "task_session_id": "task-session-a",
                    "creator_user_id": "user-a",
                },
            },
        )
        execution_key = "execution-1"
        delivery_meta = {"cron_delivery_key": "cron:execution-1:output"}
        first = CronExecutor(
            runner=SimpleNamespace(session=SafeJSONSession(str(tmp_path))),
            channel_manager=channel,
        )
        second = CronExecutor(
            runner=SimpleNamespace(session=SafeJSONSession(str(tmp_path))),
            channel_manager=channel,
        )
        first_delivery = asyncio.create_task(
            first._deliver_text_output(  # pylint: disable=protected-access
                job,
                execution_key,
                "user-a",
                "session-a",
                delivery_meta,
            ),
        )
        await channel.send_started.wait()
        second_delivery = asyncio.create_task(
            second._deliver_text_output(  # pylint: disable=protected-access
                job,
                execution_key,
                "user-a",
                "session-a",
                delivery_meta,
            ),
        )
        await asyncio.sleep(0)
        assert channel.send_attempts == 1
        channel.release_send.set()
        await asyncio.gather(first_delivery, second_delivery)
        state = await first._runner.session.get_session_state_dict(
            "task-session-a",
            user_id="user-a",
        )
        return channel.send_attempts, state

    send_attempts, state = asyncio.run(_run())

    assert send_attempts == 1
    assert (
        state["task_messages"][0]["metadata"]["output_delivery_completed"]
        is True
    )


def test_agent_delivery_serializes_same_execution_key_on_shared_session(
    tmp_path,
):
    """A replay cannot send an already-receipted agent result again."""

    class _BlockingEventChannel:
        def __init__(self) -> None:
            self.send_started = asyncio.Event()
            self.release_send = asyncio.Event()
            self.send_attempts = 0

        async def send_event(self, **_kwargs) -> bool:
            self.send_attempts += 1
            self.send_started.set()
            await self.release_send.wait()
            return True

    def _stream_state() -> AgentStreamState:
        return AgentStreamState(
            output_delivery_receipt_supported=True,
            completed_message_event=SimpleNamespace(
                object="message",
                status=RunStatus.Completed,
                content=[SimpleNamespace(type="text", text="agent output")],
            ),
        )

    async def _run() -> tuple[int, dict]:
        channel = _BlockingEventChannel()
        job = _build_agent_job().model_copy(
            update={
                "dispatch": DispatchSpec(
                    channel="zhaohu",
                    target=DispatchTarget(
                        user_id="user-a",
                        session_id="session-a",
                    ),
                ),
            },
        )
        req = {
            "session_id": "session-a",
            "user_id": "user-a",
            "cron_execution_key": "execution-1",
            "cron_persistence_key": "persistence-1",
        }
        first_session = SafeJSONSession(str(tmp_path))
        await first_session.mutate_session_state(
            session_id="session-a",
            user_id="user-a",
            mutator=lambda _state: {
                "task_runs": [
                    {
                        "execution_key": "execution-1",
                        "persistence_key": "persistence-1",
                        "output_delivery_completed": False,
                    },
                ],
            },
        )
        first = CronExecutor(
            runner=SimpleNamespace(session=first_session),
            channel_manager=channel,
        )
        second = CronExecutor(
            runner=SimpleNamespace(session=SafeJSONSession(str(tmp_path))),
            channel_manager=channel,
        )
        first_delivery = asyncio.create_task(
            first._deliver_persisted_agent_output(  # pylint: disable=protected-access
                job,
                "user-a",
                "session-a",
                {},
                _stream_state(),
                req,
            ),
        )
        await channel.send_started.wait()
        second_delivery = asyncio.create_task(
            second._deliver_persisted_agent_output(  # pylint: disable=protected-access
                job,
                "user-a",
                "session-a",
                {},
                _stream_state(),
                req,
            ),
        )
        await asyncio.sleep(0)
        assert channel.send_attempts == 1
        channel.release_send.set()
        await asyncio.gather(first_delivery, second_delivery)
        state = await first_session.get_session_state_dict(
            "session-a",
            user_id="user-a",
        )
        return channel.send_attempts, state

    send_attempts, state = asyncio.run(_run())

    assert send_attempts == 1
    assert state["task_runs"][0]["output_delivery_completed"] is True
    assert state["task_runs"][0]["output_delivery_state"] == "completed"
