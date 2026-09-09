# -*- coding: utf-8 -*-
"""Cron Agent 完成输出后的取消状态回归测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from agentscope_runtime.engine.schemas.agent_schemas import RunStatus

from swe.app.crons.manager import CronManager
from swe.app.crons.models import (
    CronJobRequest,
    CronJobSpec,
    DispatchSpec,
    DispatchTarget,
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


class _Runner:
    async def stream_query(self, _req):
        yield SimpleNamespace(
            object="message",
            status=RunStatus.Completed,
            content=[SimpleNamespace(type="text", text="done output")],
        )
        await asyncio.sleep(30)


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
    """模拟 Runtime 完成但没有 assistant message 的合法终态。"""

    async def stream_query(self, _req):
        for status in (
            RunStatus.Created,
            RunStatus.InProgress,
            RunStatus.Completed,
        ):
            yield SimpleNamespace(object="response", status=status)


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


class _ChannelManager:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def send_event(self, **kwargs) -> None:
        self.events.append(kwargs["event"])


class _SlowSendChannelManager(_ChannelManager):
    def __init__(self, send_started: asyncio.Event) -> None:
        super().__init__()
        self.send_started = send_started

    async def send_event(self, **kwargs) -> None:
        self.events.append(kwargs["event"])
        self.send_started.set()
        await asyncio.sleep(30)


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


def test_completed_agent_output_cancelled_before_stream_close_keeps_success(
    monkeypatch,
):
    """Agent 已输出完成消息后被取消，最终状态仍应保留为成功。"""
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
        assert len(channel_manager.events) == 1

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        return manager, channel_manager, monitor

    manager, channel_manager, monitor = asyncio.run(_run())

    state = manager.get_state("job-cancel-after-output")
    assert len(channel_manager.events) == 1
    assert state.last_status == "success"
    assert state.last_error is None
    assert monitor.records[-1]["status"] == "success"
    assert any(
        "cancellation after completed output" in message
        for message in info_messages
    )


def test_completed_agent_event_cancelled_during_send_keeps_success():
    """Agent 完成事件发送过程中被取消，也应按已完成任务处理。"""

    async def _run():
        job = _build_agent_job()
        send_started = asyncio.Event()
        channel_manager = _SlowSendChannelManager(send_started)
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
        await send_started.wait()

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        return manager, channel_manager, monitor

    manager, channel_manager, monitor = asyncio.run(_run())

    state = manager.get_state("job-cancel-after-output")
    assert len(channel_manager.events) == 1
    assert state.last_status == "success"
    assert state.last_error is None
    assert monitor.records[-1]["status"] == "success"


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
        manager = CronManager(
            repo=_Repo(job),
            runner=_ResponseFailedRunner(),
            channel_manager=_ChannelManager(),
        )
        manager._monitor_sync_client = (
            monitor  # pylint: disable=protected-access
        )

        with __import__("pytest").raises(RuntimeError):
            await manager._execute_once(  # pylint: disable=protected-access
                job,
                is_manual=False,
            )
        return manager, monitor

    manager, monitor = asyncio.run(_run())

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


def test_response_completed_without_message_marks_execution_as_success(
    monkeypatch,
):
    """Runtime 的 response/completed 是 Cron 成功终态，即使 output 为空。"""
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
        await manager._execute_once(  # pylint: disable=protected-access
            job,
            is_manual=False,
        )
        return manager, monitor

    manager, monitor = asyncio.run(_run())

    assert (
        manager.get_state("job-cancel-after-output").last_status == "success"
    )
    assert monitor.records[-1]["status"] == "success"


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

    def fake_resolve_auth_token_for_execution(**_kwargs):
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
