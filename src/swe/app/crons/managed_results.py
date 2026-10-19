# -*- coding: utf-8 -*-
"""SWE 直接写共库执行结果，通过持久运行身份隔离过期执行者。"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .managed_history import history_values, safe_metadata
from .managed_events import append_run_event
from .managed_failure import public_error_message
from ..source_system_config.runtime import (
    get_current_source_system_config,
    resolve_cron_unread_auto_pause_config,
)

KEYS = ("tenant_id", "source_id", "agent_id", "job_id")
WHERE = " AND ".join(f"`{name}`=%s" for name in KEYS)
TABLE = "swe_cron_execution_runs"
TERMINAL = frozenset({"success", "error", "skipped", "cancelled"})
logger = logging.getLogger(__name__)


class ExecutionNotFound(LookupError):
    """当前完整任务身份下不存在对应执行记录。"""


class ExecutionLeaseLost(RuntimeError):
    """过期执行者不能提交或覆盖执行结果。"""


def _validate_dispatch(run, token):
    """受理尚未开始的请求时，必须仍持有 Scheduler 当前有效派发占位。"""
    deadline = run.get("dispatch_lease_until")
    if isinstance(deadline, str):
        deadline = datetime.fromisoformat(deadline)
    if (
        run["status"] != "dispatching"
        or not token
        or run["dispatch_owner"] != token
        or deadline is None
        or deadline <= utc_now()
    ):
        raise ExecutionLeaseLost("cron_dispatch_lease_lost")


@dataclass(frozen=True)
class ExecutionOutcome:
    status: str
    preview: str = ""
    error_code: str | None = None
    trace_id: str = ""
    input_snapshot: dict | None = None
    execution_meta: dict | None = None
    executor_leader: str = ""


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _update(db, table, values, where, params):
    # 表名、字段名和条件均来自内部常量，禁止拼接 HTTP 输入。
    setters = ", ".join(f"`{name}`=%s" for name in values)
    return await db.execute(
        f"UPDATE {table} SET {setters} WHERE {where}",
        (*values.values(), *params),
    )


async def _insert_history(db, values):
    columns = ", ".join(f"`{name}`" for name in values)
    marks = ", ".join("%s" for _ in values)
    await db.execute(
        f"INSERT INTO swe_cron_executions ({columns}) VALUES ({marks})",
        tuple(values.values()),
    )
    result = await db.fetch_one("SELECT LAST_INSERT_ID() AS id")
    if result is None or not result["id"]:
        raise RuntimeError("cron_history_identity_missing")
    return int(result["id"])


class ExecutionWriter:
    def __init__(self, db):
        self.db = db

    async def get(self, execution_id, key):
        async with self.db.transaction():
            _, run = await self._locked(execution_id, key)
            return run

    async def _locked(self, execution_id, key):
        definition = await self.db.fetch_one(
            f"SELECT * FROM swe_cron_jobs_v2 WHERE {WHERE} FOR UPDATE",
            key,
        )
        if (
            definition is None
            or definition["management_owner"] != "scheduler"
            or tuple(definition[name] for name in KEYS) != key
        ):
            raise ExecutionNotFound("cron_execution_not_found")
        run = await self.db.fetch_one(
            f"SELECT * FROM {TABLE} WHERE execution_id=%s AND {WHERE} FOR UPDATE",
            (execution_id, *key),
        )
        if run is None or tuple(run[name] for name in KEYS) != key:
            raise ExecutionNotFound("cron_execution_not_found")
        return definition, run

    async def _change(self, run, values):
        values = {
            **values,
            "version": run["version"] + 1,
            "updated_at": utc_now(),
        }
        changed = await _update(
            self.db,
            TABLE,
            values,
            "execution_id=%s AND version=%s",
            (run["execution_id"], run["version"]),
        )
        if changed != 1:
            raise ExecutionLeaseLost("cron_execution_lease_lost")
        result = {**run, **values}
        if "status" in values and values["status"] != run["status"]:
            await append_run_event(self.db, result, values["status"])
        return result

    async def accept(self, execution_id, key, *, dispatch_token=None):
        async with self.db.transaction():
            _, run = await self._locked(execution_id, key)
            if run["status"] not in {
                "queued",
                "dispatching",
                "dispatch_failed",
            }:
                return run
            if dispatch_token is not None:
                _validate_dispatch(run, dispatch_token)
            return await self._change(
                run,
                {
                    "status": "accepted",
                    "accepted_at": utc_now(),
                    "error_code": None,
                },
            )

    async def start(self, execution_id, key, owner):
        async with self.db.transaction():
            definition, run = await self._locked(execution_id, key)
            if run["status"] != "accepted":
                return None
            state = await self._state(key)
            if (
                definition["deleted_at"]
                or not definition["enabled"]
                or state.get("pause_reason")
                or int(run["enqueued_state_version"])
                < int(state.get("queue_invalidation_version") or 0)
            ):
                await self._complete(
                    definition,
                    run,
                    ExecutionOutcome(
                        "skipped",
                        error_code="cron_job_disabled_before_start",
                    ),
                )
                return None
            due = run.get("not_before_at")
            if isinstance(due, str):
                due = datetime.fromisoformat(due)
            if due is not None and due > utc_now():
                return None
            spec = json.loads(run["spec_json"])
            active = await self.db.fetch_one(
                f"SELECT COUNT(*) AS count FROM {TABLE} WHERE {WHERE} AND status='running'",
                key,
            )
            if active["count"] >= spec["runtime"]["max_concurrency"]:
                return None
            now = utc_now()
            run = await self._change(
                run,
                {
                    "status": "running",
                    "run_owner": owner,
                    "started_at": now,
                    "started_order": state["state_version"] + 1,
                    "run_lease_until": now
                    + timedelta(
                        seconds=spec["runtime"]["timeout_seconds"] + 120,
                    ),
                },
            )
            if not definition["deleted_at"]:
                await self._write_state(
                    key,
                    state,
                    {
                        "last_status": "running",
                        "last_run_at": now,
                        "last_error": None,
                    },
                )
            return run

    async def expire(self, execution_id, key):
        """将失联执行标为结果未确认的错误，不自动重跑业务任务。"""
        async with self.db.transaction():
            definition, run = await self._locked(execution_id, key)
            deadline = run.get("run_lease_until")
            if isinstance(deadline, str):
                deadline = datetime.fromisoformat(deadline)
            if (
                run["status"] != "running"
                or deadline is None
                or deadline > utc_now()
            ):
                return False
            await self._complete(
                definition,
                run,
                ExecutionOutcome(
                    "error",
                    error_code="cron_execution_lease_expired",
                ),
            )
        logger.warning(
            "失联执行状态已收敛 execution_id=%s error_code=cron_execution_lease_expired",
            execution_id,
        )
        return True

    async def _state(self, key):
        state = await self.db.fetch_one(
            f"SELECT * FROM swe_cron_job_states WHERE {WHERE} FOR UPDATE",
            key,
        )
        if state is None or tuple(state[name] for name in KEYS) != key:
            raise RuntimeError("cron_state_record_missing")
        return state

    async def _write_state(self, key, current, values):
        await _update(
            self.db,
            "swe_cron_job_states",
            {
                **values,
                "state_version": current["state_version"] + 1,
                "updated_at": utc_now(),
            },
            WHERE,
            key,
        )

    async def complete(self, execution_id, key, owner, outcome):
        if outcome.status not in TERMINAL:
            raise ValueError("invalid_cron_execution_status")
        async with self.db.transaction():
            definition, run = await self._locked(execution_id, key)
            if run["status"] in TERMINAL:
                return False
            if run["status"] != "running" or run["run_owner"] != owner:
                raise ExecutionLeaseLost("cron_execution_lease_lost")
            await self._complete(definition, run, outcome)
        logger.info(
            "执行结果事务已提交 execution_id=%s status=%s",
            execution_id,
            outcome.status,
        )
        return True

    async def _complete(self, definition, run, outcome):
        now = utc_now()
        history_id = await _insert_history(
            self.db,
            history_values(
                run,
                outcome,
                now,
                deleted=bool(definition["deleted_at"]),
            ),
        )
        await self._finalize_previous_history(run)
        await self._change(
            run,
            {
                "status": outcome.status,
                "finished_at": now,
                "history_execution_id": history_id,
                "output_preview": outcome.preview[:512],
                "trace_id": outcome.trace_id[:64],
                "error_code": outcome.error_code,
                "result_json": json.dumps(
                    safe_metadata(outcome.execution_meta or {}),
                    ensure_ascii=False,
                    default=str,
                ),
                "run_lease_until": None,
            },
        )
        if not definition["deleted_at"]:
            await self._result_state(
                run,
                outcome,
                now,
                bool(definition["enabled"]),
            )
        else:
            await self._release_stale_running(run, outcome)

    async def _finalize_previous_history(self, run):
        """与新历史同事务替换业务重试结果，不影响其他任务或其他批次。"""
        meta = json.loads(run["dispatch_meta_json"])
        previous_id = meta.get("previous_execution_id")
        if run["kind"] != "batch" or not previous_id:
            return
        key = tuple(run[name] for name in KEYS)
        previous = await self.db.fetch_one(
            f"SELECT history_execution_id, dispatch_meta_json, kind FROM {TABLE} "
            f"WHERE execution_id=%s AND {WHERE} FOR UPDATE",
            (previous_id, *key),
        )
        if previous is None or previous["kind"] != "batch":
            raise ValueError("cron_retry_history_identity_invalid")
        prior_meta = json.loads(previous["dispatch_meta_json"])
        if not meta.get("batch_id") or meta["batch_id"] != prior_meta.get(
            "batch_id",
        ):
            raise ValueError("cron_retry_history_batch_invalid")
        if previous["history_execution_id"] is not None:
            await self.db.execute(
                "UPDATE swe_cron_executions SET is_final=0 "
                "WHERE id=%s AND job_id=%s AND tenant_id=%s",
                (
                    previous["history_execution_id"],
                    run["job_id"],
                    run["tenant_id"],
                ),
            )

    async def _runtime_values(self, key, outcome):
        active = await self.db.fetch_one(
            f"SELECT COUNT(*) AS count FROM {TABLE} WHERE {WHERE} AND status='running'",
            key,
        )
        if active["count"]:
            return {"last_status": "running"}
        latest = await self.db.fetch_one(
            f"SELECT status, error_code, result_json FROM {TABLE} WHERE {WHERE} AND started_at IS NOT NULL "
            "ORDER BY started_order DESC, execution_id DESC LIMIT 1",
            key,
        )
        return {
            "last_status": latest["status"] if latest else outcome.status,
            "last_error": (
                public_error_message(
                    latest["error_code"],
                    latest.get("result_json"),
                )
                if latest
                else public_error_message(
                    outcome.error_code,
                    outcome.execution_meta,
                )
            ),
        }

    async def _release_stale_running(self, run, outcome):
        key = tuple(run[name] for name in KEYS)
        state = await self._state(key)
        if state["last_status"] == "running":
            await self._write_state(
                key,
                state,
                await self._runtime_values(key, outcome),
            )

    async def _result_state(self, run, outcome, now, enabled):
        key = tuple(run[name] for name in KEYS)
        state = await self._state(key)
        latest = await self.db.fetch_one(
            f"SELECT execution_id FROM {TABLE} WHERE {WHERE} "
            "AND status='success' "
            "ORDER BY started_order DESC, execution_id DESC LIMIT 1",
            key,
        )
        values = await self._runtime_values(key, outcome)
        if outcome.status == "success":
            # 手动也产生展示和未读，但不直接触发自动未读暂停。
            values.update(
                self._success_values(
                    state,
                    outcome,
                    now,
                    enabled and run["kind"] != "manual",
                ),
            )
            if latest and latest["execution_id"] != run["execution_id"]:
                values.pop("latest_scheduled_preview", None)
                values.pop("last_scheduled_run_at", None)
        await self._write_state(key, state, values)

    @staticmethod
    def _success_values(state, outcome, now, allow_auto_pause):
        unread = int(state.get("unread_execution_count") or 0) + 1
        values = {
            "has_scheduled_result": 1,
            "latest_scheduled_preview": outcome.preview[:10],
            "last_scheduled_run_at": now,
            "unread_execution_count": unread,
        }
        config = resolve_cron_unread_auto_pause_config(
            get_current_source_system_config(),
        )
        if (
            config.enabled
            and unread >= config.threshold
            and allow_auto_pause
            and not state.get("pause_reason")
        ):
            values.update(
                {
                    "pause_reason": "auto_unread_threshold",
                    "auto_paused_at": now,
                    "queue_invalidation_version": state["state_version"] + 1,
                },
            )
        return values
