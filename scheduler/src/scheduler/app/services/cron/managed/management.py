# -*- coding: utf-8 -*-
"""同步完成任务管理，持久保存外部步骤并支持人工续办。"""

import json
import logging

from .diagnostics import format_management_error
from .operations import (
    LeaseLost,
    ManagementConflict,
    ManagementStore,
    RegistrationUnknown,
    operation_summary,
)

__all__ = ["ManagementService", "ManagementConflict", "RegistrationUnknown"]
logger = logging.getLogger(__name__)


class ManagementService:
    def __init__(self, db, resources, platform, *, grant=None):
        self.store = ManagementStore(db, grant=grant)
        self.resources = resources
        self.platform = platform

    async def create(self, caller, payload):
        return await self._submit(caller, "create", payload)

    async def update(self, caller, job_id, payload):
        return await self._submit(caller, "update", payload, job_id)

    async def delete(self, caller, job_id, payload):
        return await self._submit(caller, "delete", payload, job_id)

    async def _submit(self, caller, action, payload, job_id=None):
        operation, reused = await self.store.begin(
            caller,
            action,
            payload,
            job_id,
        )
        logger.info(
            "定时任务管理请求已受理 action=%s operation_id=%s job_id=%s reused=%s",
            action,
            operation["operation_id"],
            operation["job_id"],
            reused,
        )
        if not reused:
            return await self.retry(
                caller,
                operation["job_id"],
                operation["operation_id"],
            )
        return operation_summary(operation)

    async def retry(self, caller, job_id, operation_id):
        if self.store.grant is None:
            existing = await self.store.get(caller, job_id, operation_id)
            if json.loads(existing["payload_json"]).get("broadcast_context"):
                raise ManagementConflict("cron_broadcast_requires_new_round")
        operation = await self.store.claim(caller, job_id, operation_id)
        if operation is None:
            return operation_summary(
                await self.store.get(caller, job_id, operation_id),
            )
        progress = json.loads(operation["progress_json"])
        logger.info(
            "开始处理定时任务管理步骤 operation_id=%s job_id=%s stage=%s",
            operation_id,
            job_id,
            operation["stage"],
        )
        try:
            if operation["action"] == "delete":
                finished = await self._cleanup(caller, operation, progress)
            else:
                await self._prepare(caller, operation, progress)
                await self._sync_platform(operation, progress)
                finished = True
            if finished:
                await self.store.finish(operation, progress)
        except LeaseLost:
            raise
        except RegistrationUnknown:
            await self.store.checkpoint(
                operation,
                "platform",
                progress,
                status="failed",
                error="platform_registration_failed",
            )
        except Exception as exc:
            code = (
                "swe_prepare_failed"
                if operation["stage"] == "prepare"
                else "cron_management_step_failed"
            )
            logger.error(
                "定时任务管理失败 operation_id=%s job_id=%s stage=%s "
                "error_code=%s error_type=%s\n%s",
                operation_id,
                job_id,
                operation["stage"],
                code,
                type(exc).__name__,
                format_management_error(exc),
            )
            await self.store.checkpoint(
                operation,
                operation["stage"],
                progress,
                status="failed",
                error=code,
            )
        result = operation_summary(
            await self.store.get(caller, job_id, operation_id),
        )
        logger.info(
            "定时任务管理步骤处理结束 operation_id=%s job_id=%s status=%s stage=%s error_code=%s",
            operation_id,
            job_id,
            result["status"],
            result["stage"],
            result["error_code"],
        )
        return result

    async def _prepare(self, caller, operation, progress):
        if not progress.get("prepared"):
            await self._request_preparation(caller, operation, progress)
        await self.store.save_preparation(operation, progress["prepared"])
        await self.store.checkpoint(operation, "platform", progress)

    async def _request_preparation(self, caller, operation, progress):
        prepared = await self.resources.prepare(operation, caller)
        original = json.loads(operation["payload_json"])["spec"]
        spec = prepared["spec"]
        if any(
            spec.get(key) != original.get(key)
            for key in (
                "id",
                "tenant_id",
                "source_id",
                "scope_id",
                "name",
                "enabled",
                "task_type",
            )
        ):
            raise ValueError("swe_prepared_identity_conflict")
        if not prepared.get("chat_id") or not prepared.get("session_id"):
            raise ValueError("swe_prepared_binding_missing")
        if (
            spec.get("meta", {}).get("creator_user_id")
            != original["meta"]["creator_user_id"]
        ):
            raise ValueError("swe_prepared_owner_conflict")
        progress["prepared"] = prepared
        await self.store.checkpoint(operation, "prepare_persist", progress)

    async def _sync_platform(self, operation, progress):
        if progress.get("platform_synced"):
            return
        payload = json.loads(operation["payload_json"])
        spec = progress["prepared"]["spec"]
        state = await self.store.state(operation)
        mode = (
            state["dispatch_state"]
            if operation["action"] == "update"
            else payload["dispatch_state"]
        )
        if mode != "ordinary" and (
            self.store.grant is not None
            or await self.store.has_parent(operation)
        ):
            # 批子任务只由源批入口触发，公开编辑也不能另建独立批定时器。
            logger.info(
                "批子任务沿用源批入口 operation_id=%s job_id=%s mode=%s",
                operation["operation_id"],
                operation["job_id"],
                mode,
            )
            progress["platform_synced"] = True
            await self.store.checkpoint(operation, "publish", progress)
            return
        field = (
            "ordinary_external_job_id"
            if mode == "ordinary"
            else "batch_external_job_id"
        )
        external_id = progress.get("external_id") or state.get(field)
        if not external_id:
            external_id = await self._registration(
                operation,
                progress,
                spec,
                mode,
            )
            progress["external_id"] = external_id
            await self.store.checkpoint(operation, "platform", progress)
        elif operation["action"] == "update" and not progress.get(
            "platform_updated",
        ):
            await self.platform.update(external_id, operation, spec, mode)
            progress["platform_updated"] = True
            await self.store.checkpoint(operation, "platform", progress)
        if state.get(field) != external_id:
            progress["external_id"] = external_id
            await self.store.save_binding(operation, progress, mode)
        enabled = mode == "batch_enabled" or (
            mode == "ordinary" and spec["enabled"]
        )
        await self.platform.set_enabled(external_id, enabled)
        progress["platform_synced"] = True
        await self.store.checkpoint(operation, "publish", progress)

    async def _registration(self, operation, progress, spec, mode):
        if progress.get("registration_started"):
            logger.warning(
                "未保存平台注册ID，按未注册重新请求，可能遗留重复定时器 operation_id=%s job_id=%s",
                operation["operation_id"],
                operation["job_id"],
            )
        progress["registration_started"] = True
        await self.store.checkpoint(operation, "platform", progress)
        try:
            return await self.platform.register(operation, spec, mode)
        except Exception as exc:
            progress["registration_started"] = False
            await self.store.checkpoint(operation, "platform", progress)
            logger.warning(
                "未取得平台注册ID，按注册失败处理，原操作重试可能重复注册 operation_id=%s error_type=%s",
                operation["operation_id"],
                type(exc).__name__,
            )
            raise

    async def _cleanup(self, caller, operation, progress):
        state = await self.store.state(operation)
        prior_timers = await self._prior_timers(operation, progress)
        stopped = progress.setdefault("stopped", [])
        timers = [
            state.get(field)
            for field in ("ordinary_external_job_id", "batch_external_job_id")
        ]
        for external_id in [*timers, *prior_timers]:
            if external_id and external_id not in stopped:
                await self.platform.stop(external_id, operation)
                stopped.append(external_id)
                await self.store.checkpoint(operation, "cleanup", progress)
        if await self.store.has_running_execution(operation):
            await self.store.checkpoint(
                operation,
                "cleanup_waiting",
                progress,
                status="pending",
            )
            return False
        if not progress.get("cleaned"):
            response = await self.resources.cleanup(operation, caller)
            if response.get("deferred"):
                await self.store.checkpoint(
                    operation,
                    "cleanup_waiting",
                    progress,
                    status="pending",
                )
                return False
            if not response.get("cleaned"):
                raise RuntimeError("swe_cleanup_incomplete")
            progress["cleaned"] = True
            await self.store.checkpoint(operation, "cleanup", progress)
        return True

    async def _prior_timers(self, operation, progress):
        resolved = progress.setdefault("prior_timers", {})
        for prior in progress.get("prior_operations", []):
            prior_id = prior["operation_id"]
            if prior_id in resolved:
                continue
            previous = json.loads(prior["progress_json"])
            external_id = previous.get("external_id")
            if not external_id and previous.get("registration_started"):
                logger.warning(
                    "历史操作没有已保存平台ID，按未注册跳过平台清理 operation_id=%s prior_operation_id=%s",
                    operation["operation_id"],
                    prior_id,
                )
            resolved[prior_id] = external_id
            await self.store.checkpoint(operation, "stop", progress)
        return [value for value in resolved.values() if value]
