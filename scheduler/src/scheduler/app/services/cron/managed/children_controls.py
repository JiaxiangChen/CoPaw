# -*- coding: utf-8 -*-
"""逐项组合选中子任务的操作，不把调用方伪装成接收人。"""

from dataclasses import replace
import logging

from .contracts import ManagedCronNotFound
from .definition import request_hash
from .executions import (
    ExecutionConflict,
    ExecutionRequest,
    ExecutionStore,
    execution_summary,
)
from .identity import identity_value
from .operations import ManagementConflict
from .repository import IDENTITY_FIELDS, ManagedCronRepository

logger = logging.getLogger(__name__)


def _selected(caller, payload):
    identity_value(payload["request_id"])
    items = payload.get("items")
    if (
        payload["action"] not in {"delete", "run"}
        or not isinstance(items, list)
        or not 1 <= len(items) <= 1000
    ):
        raise ValueError("invalid_cron_child_action")
    selected, seen = [], set()
    for item in items:
        child = replace(
            caller,
            tenant_id=identity_value(item["tenant_id"]),
            agent_id=identity_value(item.get("agent_id") or caller.agent_id),
        )
        key = child.job_key(item["job_id"])
        if key in seen:
            raise ValueError("duplicate_cron_child_identity")
        seen.add(key)
        selected.append((child, item))
    return selected


def _failure(exc):
    if isinstance(exc, ManagedCronNotFound):
        return 404, "cron_child_not_found"
    if isinstance(exc, (ManagementConflict, ExecutionConflict)):
        return 409, str(exc)
    if isinstance(exc, ValueError):
        return 400, "invalid_cron_child_action"
    return 503, "cron_child_operation_unavailable"


class ChildrenControlService:
    def __init__(self, db, management):
        self.db = db
        self.management = management

    async def apply(self, caller, parent_id, payload):
        selected = _selected(caller, payload)
        await ManagedCronRepository(self.db)._authorized_row(
            caller,
            parent_id,
            True,
        )
        logger.info(
            "开始处理选中子任务 action=%s source_job_id=%s item_count=%s",
            payload["action"],
            parent_id,
            len(selected),
        )
        results = []
        for child, item in selected:
            result = {
                **dict(zip(IDENTITY_FIELDS, child.job_key(item["job_id"]))),
            }
            try:
                result.update(
                    await self._one(caller, parent_id, child, item, payload),
                )
            except Exception as exc:
                code, error = _failure(exc)
                logger.warning(
                    "选中子任务操作失败 source_job_id=%s job_id=%s error_code=%s",
                    parent_id,
                    item["job_id"],
                    error,
                )
                result.update(
                    status="failed",
                    status_code=code,
                    error_code=error,
                )
            results.append(result)
        logger.info(
            "选中子任务处理结束 source_job_id=%s item_count=%s",
            parent_id,
            len(results),
        )
        return {
            "source_job_id": parent_id,
            "action": payload["action"],
            "results": results,
            "success_count": sum(
                item["status"] in {"completed", "accepted"} for item in results
            ),
            "failed_count": sum(
                item["status"] == "failed" for item in results
            ),
            "pending_count": sum(
                item["status"] == "pending" for item in results
            ),
        }

    async def _one(self, caller, parent_id, child, item, payload):
        job_id = item["job_id"]
        row = await ManagedCronRepository(self.db)._authorized_row(
            child,
            job_id,
            True,
        )
        parent = tuple(row.get(f"parent_{name}") for name in IDENTITY_FIELDS)
        if parent != caller.job_key(parent_id):
            raise ManagedCronNotFound("cron_child_not_found")
        request_id = request_hash(
            [
                payload["request_id"],
                caller.job_key(parent_id),
                child.job_key(job_id),
                payload["action"],
            ],
        )
        if payload["action"] == "run":
            execution, reused = await ExecutionStore(self.db).enqueue(
                child,
                job_id,
                ExecutionRequest(
                    "manual",
                    request_id,
                    expected_definition_version=item[
                        "expected_definition_version"
                    ],
                ),
            )
            return {
                "status": "accepted",
                "status_code": 202,
                "execution": execution_summary(execution),
                "reused": reused,
            }
        operation = await self.management.delete(
            child,
            job_id,
            {
                "request_id": request_id,
                "expected_definition_version": item[
                    "expected_definition_version"
                ],
            },
        )
        if operation["status"] == "completed":
            return {
                "status": "completed",
                "status_code": 200,
                "operation": operation,
            }
        failed = operation["status"] in {"failed", "superseded"}
        return {
            "status": "failed" if failed else "pending",
            "status_code": 503 if failed else 202,
            "operation": operation,
        }
