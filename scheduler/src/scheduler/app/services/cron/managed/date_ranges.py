# -*- coding: utf-8 -*-
"""日期配置事务与外部平台主动调用的单次维护，不创建内部定时任务。"""

import json
import logging
from datetime import datetime, timedelta

from .date_policy import BEIJING, columns, platform_window_open, reason
from .definition import canonical, request_hash
from .diagnostics import format_management_error
from .distribution import _children
from .identity import identity_value
from .operations import (
    IDENTITY_FIELDS,
    JOURNAL,
    WHERE_JOB,
    ManagementConflict,
    utc_now,
)
from .projection import write_projection
from .repository import ManagedCronRepository
from .storage import transaction, update

logger = logging.getLogger(__name__)


async def apply_group_range(tx, current, operation):
    """在同一事务更新父子日期，不借完整正文更新绕过源任务保护。"""
    key = tuple(current[name] for name in IDENTITY_FIELDS)
    period = json.loads(operation["payload_json"])["run_date_range"]
    refs = [current, *await _children(tx, key, [])]
    for ref in refs:
        item_key = tuple(ref[name] for name in IDENTITY_FIELDS)
        await _lock_definition(tx, item_key)
        active = await tx.fetch_one(
            f"SELECT operation_id FROM {JOURNAL} WHERE active_key=%s",
            (request_hash(item_key),),
        )
        if active:
            raise ManagementConflict("cron_management_in_progress")
        row = await ManagedCronRepository(tx)._get_row(item_key, True)
        state = await tx.fetch_one(
            f"SELECT * FROM swe_cron_job_states WHERE {WHERE_JOB} FOR UPDATE",
            item_key,
        )
        if state is None or state["management_status"] != "ready":
            raise ManagementConflict("cron_job_not_ready")
        spec = json.loads(row["spec_json"])
        spec["run_date_range"] = period
        await update(
            tx,
            "swe_cron_jobs_v2",
            {
                **columns(spec),
                "spec_json": canonical(spec),
                "updated_at": utc_now(),
            },
            WHERE_JOB,
            item_key,
        )
        values = _state_values(state, bool(reason(spec)), configuration=True)
        await update(tx, "swe_cron_job_states", values, WHERE_JOB, item_key)
        await write_projection(
            tx,
            spec,
            {**state, **values},
            item_key[2],
            utc_now(),
        )
    progress = json.loads(operation["progress_json"])
    progress["affected_count"] = len(refs)
    operation["progress_json"] = canonical(progress)
    await update(
        tx,
        JOURNAL,
        {"progress_json": operation["progress_json"]},
        "operation_id=%s",
        (operation["operation_id"],),
    )


async def _lock_definition(tx, key):
    await tx.fetch_one(
        f"SELECT job_id FROM swe_cron_jobs_v2 WHERE {WHERE_JOB} FOR UPDATE",
        key,
    )


def _state_values(state, paused, *, configuration=False):
    changed = bool(state.get("date_window_paused")) != paused
    increment = int(changed or configuration)
    version = state["state_version"] + increment
    result = {
        "date_window_paused": paused,
        "date_window_sync_required": True,
        "state_version": version,
        "management_version": state["management_version"] + increment,
        "updated_at": utc_now(),
    }
    if paused and (changed or configuration):
        result["queue_invalidation_version"] = version
    return result


class DateMaintenanceService:
    """每次请求同步完成本来源维护；不保存调用进度或恢复历史时间点。"""

    def __init__(self, db, platform):
        self.db, self.platform = db, platform

    async def apply(self, source_id):
        source_id = identity_value(source_id)
        now = datetime.now(BEIJING)
        result = {
            "source_id": source_id,
            "evaluated_date": now.date().isoformat(),
            "preopen_date": (now + timedelta(days=1)).date().isoformat(),
            "scanned_count": 0,
            "date_paused_count": 0,
            "date_released_count": 0,
            "platform_updated_count": 0,
            "failed_count": 0,
            "failures": [],
        }
        after, platform_results = None, {}
        while True:
            page = await self._page(source_id, after)
            if not page:
                break
            for item in page:
                key = (
                    item["tenant_id"],
                    source_id,
                    item["agent_id"],
                    item["job_id"],
                )
                result["scanned_count"] += 1
                try:
                    counts = await self._one(key, now, platform_results)
                    for name, amount in counts.items():
                        result[name] += amount
                except Exception as error:
                    self._failure(result, key, error)
            after = tuple(
                page[-1][name] for name in ("tenant_id", "agent_id", "job_id")
            )
        result["failures_truncated"] = result["failed_count"] > len(
            result["failures"],
        )
        return result

    async def _page(self, source, after):
        joins = " AND ".join(
            f"d.{name}=s.{name} AND d.{name} COLLATE utf8mb4_bin=s.{name}"
            for name in IDENTITY_FIELDS
        )
        cursor = (
            " AND (d.tenant_id,d.agent_id,d.job_id)>(%s,%s,%s)"
            if after
            else ""
        )
        return await self.db.fetch_all(
            "SELECT d.tenant_id,d.agent_id,d.job_id FROM swe_cron_jobs_v2 d "
            f"JOIN swe_cron_job_states s ON {joins} WHERE d.source_id=%s AND d.source_id COLLATE utf8mb4_bin=%s "
            "AND d.management_owner='scheduler' AND d.deleted_at IS NULL AND s.management_status='ready' "
            "AND (d.run_start_date IS NOT NULL OR d.run_end_date IS NOT NULL OR s.date_window_paused=1 OR s.date_window_sync_required=1)"
            + cursor
            + " ORDER BY d.tenant_id,d.agent_id,d.job_id LIMIT 200",
            (source, source, *(after or ())),
        )

    async def _one(self, key, now, cache):
        row, state, values = await self._refresh(key, now)
        external, enabled = await self._binding(row, now)
        cache_key = (external, enabled)
        called = cache_key not in cache
        if called:
            try:
                await self.platform.set_enabled(external, enabled)
            except Exception as error:
                cache[cache_key] = error
            else:
                cache[cache_key] = None
        if cache[cache_key] is not None:
            raise cache[cache_key]
        changed = await update(
            self.db,
            "swe_cron_job_states",
            {"date_window_sync_required": False},
            WHERE_JOB + " AND state_version=%s AND management_version=%s",
            (*key, values["state_version"], values["management_version"]),
        )
        if changed != 1:
            raise ManagementConflict("cron_date_range_changed_during_sync")
        return {
            "date_paused_count": int(values["date_window_paused"]),
            "date_released_count": int(
                bool(state.get("date_window_paused"))
                and not values["date_window_paused"],
            ),
            "platform_updated_count": int(called),
        }

    async def _refresh(self, key, now):
        async with transaction(self.db) as tx:
            await _lock_definition(tx, key)
            row = await ManagedCronRepository(tx)._get_row(key, True)
            state = await tx.fetch_one(
                f"SELECT * FROM swe_cron_job_states WHERE {WHERE_JOB} FOR UPDATE",
                key,
            )
            if (
                row["deleted_at"]
                or state is None
                or state["management_status"] != "ready"
            ):
                raise ManagementConflict("cron_job_not_ready")
            current_reason = reason(row, now)
            # 预开标记可能跨过有效日而未刷新，过期仍必须推进旧队列水位。
            values = _state_values(
                state,
                bool(current_reason),
                configuration=current_reason == "date_expired",
            )
            await update(tx, "swe_cron_job_states", values, WHERE_JOB, key)
            await write_projection(
                tx,
                json.loads(row["spec_json"]),
                {**state, **values},
                key[2],
                utc_now(),
            )
        return row, state, values

    async def _binding(self, row, now):
        if row["state_dispatch_state"] == "ordinary":
            external = row["state_ordinary_external_job_id"]
            enabled = bool(
                row["enabled"]
                and not row.get("state_pause_reason")
                and platform_window_open(row, now),
            )
        else:
            root = await self._root(row)
            key = tuple(root[name] for name in IDENTITY_FIELDS)
            control = await self.db.fetch_one(
                f"SELECT state FROM swe_cron_managed_dispatch_controls WHERE {WHERE_JOB}",
                key,
            )
            target = (
                control["state"] if control else root["state_dispatch_state"]
            )
            external = root["state_batch_external_job_id"]
            enabled = target == "batch_enabled" and platform_window_open(
                root,
                now,
            )
        if not external:
            raise ManagementConflict("cron_external_binding_missing")
        return external, enabled

    async def _root(self, row):
        if not row.get("parent_job_id"):
            return row
        key = tuple(row.get("parent_" + name) for name in IDENTITY_FIELDS)
        if key[1] != row["source_id"]:
            raise ManagementConflict("cron_date_range_parent_invalid")
        return await ManagedCronRepository(self.db)._get_row(key, False)

    @staticmethod
    def _failure(result, key, error):
        result["failed_count"] += 1
        if len(result["failures"]) < 50:
            result["failures"].append(
                {
                    "tenant_id": key[0],
                    "agent_id": key[2],
                    "job_id": key[3],
                    "error_code": "cron_date_range_sync_failed",
                },
            )
        logger.error(
            "日期维护未完成 source_id=%s job_id=%s\n%s",
            key[1],
            key[3],
            format_management_error(error),
        )
