# -*- coding: utf-8 -*-
"""使用原定时任务执行器运行数据库中冻结的执行快照。"""

import asyncio
import json
import logging
from uuid import uuid4

from .managed_results import ExecutionOutcome, TERMINAL
from .models import CronJobSpec
from .managed_failure import error_metadata, public_failure_detail

logger = logging.getLogger(__name__)


def _failure_outcome(error):
    status, code = "error", "cron_execution_failed"
    if isinstance(error, asyncio.CancelledError):
        status, code = "cancelled", "cron_execution_cancelled"
    elif isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        code = "cron_execution_timeout"
    metadata = (
        error_metadata(error)
        if status == "error"
        else getattr(error, "cron_execution_meta", None)
    )
    return ExecutionOutcome(
        status,
        error_code=code,
        trace_id=str(getattr(error, "cron_trace_id", "")),
        execution_meta={
            **(metadata if isinstance(metadata, dict) else {}),
            "error_detail": public_failure_detail(error),
        },
    )


class ManagedExecutionService:
    def __init__(
        self,
        writer,
        engine,
        workflow_engine=None,
        *,
        workspace_dir=None,
        receipts=None,
    ):
        self.writer = writer
        self.engine = engine
        self.workflow_engine = workflow_engine
        self.workspace_dir = workspace_dir
        self.receipts = receipts

    async def execute(self, execution_id, key, *, live_headers=None):
        await self.writer.accept(execution_id, key)
        owner = str(uuid4())
        run = await self.writer.start(execution_id, key, owner)
        if run is None:
            logger.debug(
                "执行无需重复启动或暂不可运行 execution_id=%s",
                execution_id,
            )
            return
        logger.info(
            "开始执行新定时任务 execution_id=%s job_id=%s kind=%s definition_version=%s",
            execution_id,
            run["job_id"],
            run["kind"],
            run["definition_version"],
        )
        outcome = await self._execute_snapshot(run, live_headers)
        if self.receipts is not None:
            try:
                await self.receipts.save(run, outcome)
            except Exception as exc:
                logger.warning(
                    "结果凭证暂存失败，仍尝试提交共库 execution_id=%s error_type=%s",
                    execution_id,
                    type(exc).__name__,
                )
        # 重试提交数据库结果时不能再次运行 Agent 或 Workflow。
        # 未完成结果提交时保留真实的非终态，不能在数据库中虚报成功。
        committed = await self._commit(execution_id, key, owner, outcome)
        if committed:
            await self._clear_receipt(run)
        logger.info(
            "新定时任务结果提交结束 execution_id=%s status=%s committed=%s error_code=%s",
            execution_id,
            outcome.status,
            committed,
            outcome.error_code,
        )

    async def _commit(self, execution_id, key, owner, outcome):
        for attempt in range(3):
            try:
                return await asyncio.shield(
                    self.writer.complete(execution_id, key, owner, outcome),
                )
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(0)
        raise RuntimeError("unreachable_cron_result_retry")

    async def _clear_receipt(self, run):
        if self.receipts is None:
            return
        try:
            await self.receipts.clear(run)
        except Exception as exc:
            logger.warning(
                "结果已落库但凭证清理待恢复 execution_id=%s error_type=%s",
                run["execution_id"],
                type(exc).__name__,
            )

    async def recover(self, execution_id, key):
        run = await self.writer.get(execution_id, key)
        if run["status"] in TERMINAL:
            await self._clear_receipt(run)
            return True
        if self.receipts is None or run["status"] != "running":
            return False
        outcome = await self.receipts.load(run)
        if outcome is None:
            return False
        committed = await self._commit(
            execution_id,
            key,
            run["run_owner"],
            outcome,
        )
        if committed:
            await self._clear_receipt(run)
            logger.info(
                "已恢复提交既有执行结果，未重跑业务 execution_id=%s",
                execution_id,
            )
        return committed

    async def _execute_snapshot(self, run, live_headers):
        try:
            spec = CronJobSpec.model_validate(json.loads(run["spec_json"]))
            spec = self._runtime_spec(spec)
            meta = self._dispatch_meta(run, spec, live_headers)
            if self.workspace_dir is not None:
                meta["workspace_dir"] = str(self.workspace_dir)
            engine = (
                self.workflow_engine
                if spec.task_type == "workflow"
                else self.engine
            )
            if engine is None:
                raise RuntimeError("cron_executor_unavailable")
            result = await asyncio.wait_for(
                engine.execute(spec, dispatch_meta=meta),
                timeout=spec.runtime.timeout_seconds,
            )
            return ExecutionOutcome(
                result.status,
                preview=result.output_preview,
                trace_id=result.trace_id,
                input_snapshot=result.input_snapshot,
                execution_meta=result.execution_meta,
                executor_leader=result.executor_leader,
            )
        except (Exception, asyncio.CancelledError) as exc:
            logger.warning(
                "新定时任务执行器返回异常 execution_id=%s error_type=%s",
                run["execution_id"],
                type(exc).__name__,
            )
            return _failure_outcome(exc)

    def _runtime_spec(self, spec):
        if self.workspace_dir is None:
            return spec
        dispatch = spec.dispatch.model_copy(
            update={
                "meta": {
                    **spec.dispatch.meta,
                    "workspace_dir": str(self.workspace_dir),
                },
            },
        )
        return spec.model_copy(update={"dispatch": dispatch})

    @staticmethod
    def _dispatch_meta(run, spec, live_headers):
        meta = json.loads(run["dispatch_meta_json"])
        headers = {
            str(name).lower(): str(value)
            for name, value in (live_headers or {}).items()
            if str(name).lower()
            in {
                "authorization",
                "x-header-cookie",
                "b3",
                "x-b3-traceid",
                "x-b3-spanid",
            }
        }
        headers["cron_job_id"] = spec.id
        meta["passthrough_headers"] = headers
        meta["cron_execution_key"] = run["execution_id"]
        return meta
