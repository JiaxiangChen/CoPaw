# -*- coding: utf-8 -*-
"""财富工作台规划的三表持久化：主表 + 场景子表 + 分发目标子表。

遵循 swe_cron_broadcast_tasks 的模式：配置数据库时走 aiomysql，
未配置时退化为进程内存储（进程重启即丢，仅供本地开发）。
"""

from __future__ import annotations

import copy
import logging
import uuid
from datetime import datetime
from typing import Any

from .models import (
    PUBLISH_STATUS_FAILED,
    PUBLISH_STATUS_PUBLISHING,
    SOURCE_LABEL_BY_ROLE,
    PlanSceneRecord,
    PlanTargetRecord,
    WealthPlanRecord,
)
from .roles import (
    ROLE_MIDDLE,
    ROLE_PRESIDENT,
    ROLE_RM,
    get_visible_source_labels,
)

logger = logging.getLogger(__name__)

_PLAN_TABLE = "swe_wealth_plans"
_SCENE_TABLE = "swe_wealth_plan_scenes"
_TARGET_TABLE = "swe_wealth_plan_targets"

_SCENE_RESERVING_SOURCE_LABELS_BY_ROLE = {
    ROLE_MIDDLE: frozenset({SOURCE_LABEL_BY_ROLE[ROLE_MIDDLE]}),
    ROLE_PRESIDENT: frozenset(
        {
            SOURCE_LABEL_BY_ROLE[ROLE_MIDDLE],
            SOURCE_LABEL_BY_ROLE[ROLE_PRESIDENT],
        },
    ),
    ROLE_RM: frozenset(
        {
            SOURCE_LABEL_BY_ROLE[ROLE_MIDDLE],
            SOURCE_LABEL_BY_ROLE[ROLE_PRESIDENT],
        },
    ),
}


def _scene_dates_overlap(
    requested: tuple[str | None, str | None],
    start: str | None,
    end: str | None,
) -> bool:
    """任务区间按闭区间比较；缺失边界视为无界。"""
    requested_start, requested_end = requested
    return not (
        (requested_start and end and requested_start > end)
        or (requested_end and start and requested_end < start)
    )


# 建表 SQL 见 scripts/sql/wealth_plan_tables.sql，由运维手动导入，启动时不自动建表。

_INSERT_PLAN_SQL = f"""
    INSERT INTO {_PLAN_TABLE} (
        id, sap_id, creator_name, bbk_id, source_id, agent_id,
        name, description, source_label, period_start, period_end,
        status, created_at, updated_at
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_UPDATE_PLAN_SQL = f"""
    UPDATE {_PLAN_TABLE} SET
        name = %s, description = %s, period_start = %s, period_end = %s,
        status = %s, publish_error = NULL, published_at = NULL,
        skill_dispatch_task_id = NULL, skill_dispatch_status = NULL,
        skill_dispatch_error = NULL,
        updated_at = %s
    WHERE id = %s
"""

_INSERT_SCENE_SQL = f"""
    INSERT INTO {_SCENE_TABLE} (
        plan_id, scene_id, item_id, scene_name, category, direction,
        cycle, start_date, end_date, cron_expr, cron_example, mcp_relations,
        cron_job_id, broadcast_task_id, sort_order
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_INSERT_TARGET_SQL = f"""
    INSERT INTO {_TARGET_TABLE} (plan_id, sap_id, target_name, sort_order)
    VALUES (%s, %s, %s, %s)
"""


def new_plan_id() -> str:
    """生成规划主键。"""
    return f"plan-{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _join_mcps(values: list[str]) -> str:
    return ",".join(v.strip() for v in values if v.strip())


def _split_mcps(raw: Any) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _row_to_scene(row: dict[str, Any]) -> PlanSceneRecord:
    return PlanSceneRecord(
        scene_id=row["scene_id"],
        scene_name=row["scene_name"],
        category=row["category"],
        cron_expr=row["cron_expr"],
        item_id=row.get("item_id"),
        direction=row.get("direction"),
        cycle=row.get("cycle"),
        start_date=row.get("start_date"),
        end_date=row.get("end_date"),
        cron_example=row.get("cron_example"),
        mcp_relations=_split_mcps(row.get("mcp_relations")),
        cron_job_id=row.get("cron_job_id"),
        broadcast_task_id=row.get("broadcast_task_id"),
        sort_order=int(row.get("sort_order") or 0),
    )


def _row_to_target(row: dict[str, Any]) -> PlanTargetRecord:
    return PlanTargetRecord(
        sap_id=row["sap_id"],
        target_name=row.get("target_name"),
        sort_order=int(row.get("sort_order") or 0),
    )


def _row_to_plan(
    row: dict[str, Any],
    scenes: list[PlanSceneRecord],
    targets: list[PlanTargetRecord],
) -> WealthPlanRecord:
    return WealthPlanRecord(
        id=row["id"],
        sap_id=row["sap_id"],
        name=row["name"],
        status=row["status"],
        creator_name=row.get("creator_name"),
        bbk_id=row.get("bbk_id"),
        source_id=row.get("source_id"),
        agent_id=row.get("agent_id"),
        description=row.get("description"),
        source_label=row.get("source_label"),
        period_start=row.get("period_start"),
        period_end=row.get("period_end"),
        publish_error=row.get("publish_error"),
        skill_dispatch_task_id=row.get("skill_dispatch_task_id"),
        skill_dispatch_status=row.get("skill_dispatch_status"),
        skill_dispatch_error=row.get("skill_dispatch_error"),
        scenes=scenes,
        targets=targets,
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        published_at=row.get("published_at"),
    )


class WealthPlanStore:
    """规划三表读写；无数据库时退化为进程内存储。"""

    def __init__(self, db: Any | None = None):
        self.db = db
        self._plans: dict[str, WealthPlanRecord] = {}

    @property
    def is_available(self) -> bool:
        """返回当前是否配置了数据库存储对象。"""
        return self.db is not None

    async def create(self, record: WealthPlanRecord) -> WealthPlanRecord:
        """写入规划主表 + 场景子表 + 目标子表。"""
        record.created_at = record.created_at or _now()
        record.updated_at = record.updated_at or _now()
        if not self.is_available:
            self._plans[record.id] = copy.deepcopy(record)
            return record
        await self.db.execute(_INSERT_PLAN_SQL, self._plan_params(record))
        await self._insert_scenes(record)
        await self._insert_targets(record)
        return record

    async def get(self, plan_id: str) -> WealthPlanRecord | None:
        """按主键读取规划（含场景与目标）。"""
        if not self.is_available:
            record = self._plans.get(plan_id)
            return copy.deepcopy(record) if record else None
        row = await self.db.fetch_one(
            f"SELECT * FROM {_PLAN_TABLE} WHERE id = %s",
            (plan_id,),
        )
        if row is None:
            return None
        return _row_to_plan(
            row,
            await self._fetch_scenes(plan_id),
            await self._fetch_targets(plan_id),
        )

    async def list_for_viewer(
        self,
        sap_id: str,
        role: str,
        bbk_id: str | None,
    ) -> list[WealthPlanRecord]:
        """按所属分行及角色可见范围查询，并按创建时间倒序。

        行长查看本行分行关注及自己创建的行长关注，中台仅查看分行关注；其余角色仅查看
        自己创建或分发给自己的本行规划。bbk_id 缺失时不返回数据。
        """
        if not bbk_id:
            return []
        source_labels = get_visible_source_labels(role)
        if not self.is_available:
            records = [
                record
                for record in self._plans.values()
                if self._visible_to(record, sap_id, role, bbk_id)
            ]
            records.sort(
                key=lambda r: r.created_at or datetime.min,
                reverse=True,
            )
            return copy.deepcopy(records)
        if source_labels is not None:
            placeholders = ", ".join(["%s"] * len(source_labels))
            creator_filter = ""
            params = (bbk_id, *source_labels)
            if role == ROLE_PRESIDENT:
                creator_filter = "AND (p.source_label <> %s OR p.sap_id = %s)"
                params += (SOURCE_LABEL_BY_ROLE[ROLE_PRESIDENT], sap_id)
            rows = await self.db.fetch_all(
                f"""
                SELECT p.* FROM {_PLAN_TABLE} p
                WHERE p.bbk_id = %s
                  AND p.source_label IN ({placeholders})
                  {creator_filter}
                ORDER BY p.created_at DESC
                """,
                params,
            )
        else:
            rows = await self.db.fetch_all(
                f"""
                SELECT DISTINCT p.* FROM {_PLAN_TABLE} p
                LEFT JOIN {_TARGET_TABLE} t ON t.plan_id = p.id
                WHERE p.bbk_id = %s
                  AND (p.sap_id = %s OR t.sap_id = %s)
                ORDER BY p.created_at DESC
                """,
                (bbk_id, sap_id, sap_id),
            )
        return [
            _row_to_plan(
                row,
                await self._fetch_scenes(row["id"]),
                await self._fetch_targets(row["id"]),
            )
            for row in rows
        ]

    async def find_scene_conflicts(
        self,
        role: str,
        sap_id: str,
        bbk_id: str | None,
        scene_ids: set[str],
        *,
        exclude_plan_id: str | None = None,
        scene_ranges: dict[str, tuple[str | None, str | None]] | None = None,
    ) -> dict[str, str]:
        """按发布角色查询任务日期重叠的场景及其占用规划名。"""
        if not bbk_id or not scene_ids:
            return {}
        source_labels = _SCENE_RESERVING_SOURCE_LABELS_BY_ROLE.get(role)
        if not source_labels:
            return {}
        ranges = scene_ranges or {}
        if not self.is_available:
            return self._find_scene_conflicts_in_memory(
                role,
                sap_id,
                bbk_id,
                scene_ids,
                exclude_plan_id,
                ranges,
            )

        ordered_scene_ids = sorted(scene_ids)
        scene_placeholders = ", ".join(["%s"] * len(ordered_scene_ids))
        source_placeholders = ", ".join(["%s"] * len(source_labels))
        exclude_sql = " AND p.id <> %s" if exclude_plan_id else ""
        params: list[str] = [
            bbk_id,
            PUBLISH_STATUS_FAILED,
        ]
        reservation_sql = f"p.source_label IN ({source_placeholders})"
        if role == ROLE_RM:
            reservation_sql = f"(p.sap_id = %s OR {reservation_sql})"
            params.append(sap_id)
        params.extend(sorted(source_labels))
        params.extend(ordered_scene_ids)
        if exclude_plan_id:
            params.append(exclude_plan_id)
        rows = await self.db.fetch_all(
            f"""
            SELECT p.name AS plan_name, s.scene_id,
                   s.start_date, s.end_date
            FROM {_PLAN_TABLE} p
            INNER JOIN {_SCENE_TABLE} s ON s.plan_id = p.id
            WHERE p.bbk_id = %s
              AND p.status <> %s
              AND {reservation_sql}
              AND s.scene_id IN ({scene_placeholders})
              {exclude_sql}
            ORDER BY p.created_at DESC
            """,
            tuple(params),
        )
        conflicts = {}
        for row in rows:
            if _scene_dates_overlap(
                ranges.get(row["scene_id"], (None, None)),
                row.get("start_date"),
                row.get("end_date"),
            ):
                conflicts.setdefault(row["scene_id"], row["plan_name"])
        return conflicts

    def _find_scene_conflicts_in_memory(
        self,
        role: str,
        sap_id: str,
        bbk_id: str,
        scene_ids: set[str],
        exclude_plan_id: str | None,
        ranges: dict[str, tuple[str | None, str | None]],
    ) -> dict[str, str]:
        """内存模式按创建时间倒序查找日期重叠的占用规划。"""
        records = sorted(
            self._plans.values(),
            key=lambda record: record.created_at or datetime.min,
            reverse=True,
        )
        conflicts: dict[str, str] = {}
        for record in records:
            if not self._reserves_scene(
                record,
                role,
                sap_id,
                bbk_id,
                exclude_plan_id,
            ):
                continue
            for scene in record.scenes:
                if scene.scene_id in scene_ids and _scene_dates_overlap(
                    ranges.get(scene.scene_id, (None, None)),
                    scene.start_date,
                    scene.end_date,
                ):
                    conflicts.setdefault(scene.scene_id, record.name)
        return conflicts

    async def update(self, record: WealthPlanRecord) -> None:
        """整体替换规划内容（场景与目标删除重插），状态重置为发布中。"""
        record.updated_at = _now()
        if not self.is_available:
            record.status = PUBLISH_STATUS_PUBLISHING
            record.publish_error = None
            record.published_at = None
            record.skill_dispatch_task_id = None
            record.skill_dispatch_status = None
            record.skill_dispatch_error = None
            self._plans[record.id] = copy.deepcopy(record)
            return
        await self.db.execute(
            _UPDATE_PLAN_SQL,
            (
                record.name,
                record.description,
                record.period_start,
                record.period_end,
                PUBLISH_STATUS_PUBLISHING,
                record.updated_at,
                record.id,
            ),
        )
        await self._delete_children(record.id)
        await self._insert_scenes(record)
        await self._insert_targets(record)

    async def delete(self, plan_id: str) -> None:
        """删除规划及其场景、目标。"""
        if not self.is_available:
            self._plans.pop(plan_id, None)
            return
        await self._delete_children(plan_id)
        await self.db.execute(
            f"DELETE FROM {_PLAN_TABLE} WHERE id = %s",
            (plan_id,),
        )

    async def set_publish_status(
        self,
        plan_id: str,
        status: str,
        error: str | None = None,
        published_at: datetime | None = None,
    ) -> None:
        """发布编排回写整体状态。"""
        if not self.is_available:
            record = self._plans.get(plan_id)
            if record is not None:
                record.status = status  # type: ignore[assignment]
                record.publish_error = error
                record.published_at = published_at
            return
        await self.db.execute(
            f"""
            UPDATE {_PLAN_TABLE}
            SET status = %s, publish_error = %s, published_at = %s
            WHERE id = %s
            """,
            (status, error, published_at, plan_id),
        )

    async def set_scene_links(
        self,
        plan_id: str,
        scene_id: str,
        cron_job_id: str,
        broadcast_task_id: str,
    ) -> None:
        """发布编排回写单个场景的定时任务与广播快照 id。"""
        if not self.is_available:
            record = self._plans.get(plan_id)
            if record is not None:
                for scene in record.scenes:
                    if scene.scene_id == scene_id:
                        scene.cron_job_id = cron_job_id
                        scene.broadcast_task_id = broadcast_task_id
            return
        await self.db.execute(
            f"""
            UPDATE {_SCENE_TABLE}
            SET cron_job_id = %s, broadcast_task_id = %s
            WHERE plan_id = %s AND scene_id = %s
            """,
            (cron_job_id, broadcast_task_id, plan_id, scene_id),
        )

    async def set_skill_dispatch(
        self,
        plan_id: str,
        batch_id: str,
        status: str,
        error: str | None,
    ) -> None:
        """技能/MCP 分发腿回写：skill_dispatch_task_id 存 market 批次 batch_id。"""
        if not self.is_available:
            record = self._plans.get(plan_id)
            if record is not None:
                record.skill_dispatch_task_id = batch_id or None
                record.skill_dispatch_status = status or None
                record.skill_dispatch_error = error
            return
        await self.db.execute(
            f"""
            UPDATE {_PLAN_TABLE}
            SET skill_dispatch_task_id = %s, skill_dispatch_status = %s,
                skill_dispatch_error = %s
            WHERE id = %s
            """,
            (batch_id or None, status or None, error, plan_id),
        )

    def _plan_params(self, record: WealthPlanRecord) -> tuple:
        return (
            record.id,
            record.sap_id,
            record.creator_name,
            record.bbk_id,
            record.source_id,
            record.agent_id,
            record.name,
            record.description,
            record.source_label,
            record.period_start,
            record.period_end,
            record.status,
            record.created_at,
            record.updated_at,
        )

    async def _insert_scenes(self, record: WealthPlanRecord) -> None:
        await self.db.execute_many(
            _INSERT_SCENE_SQL,
            [
                (
                    record.id,
                    scene.scene_id,
                    scene.item_id,
                    scene.scene_name,
                    scene.category,
                    scene.direction,
                    scene.cycle,
                    scene.start_date,
                    scene.end_date,
                    scene.cron_expr,
                    scene.cron_example,
                    _join_mcps(scene.mcp_relations),
                    scene.cron_job_id,
                    scene.broadcast_task_id,
                    scene.sort_order,
                )
                for scene in record.scenes
            ],
        )

    async def _insert_targets(self, record: WealthPlanRecord) -> None:
        await self.db.execute_many(
            _INSERT_TARGET_SQL,
            [
                (
                    record.id,
                    target.sap_id,
                    target.target_name,
                    target.sort_order,
                )
                for target in record.targets
            ],
        )

    async def _delete_children(self, plan_id: str) -> None:
        await self.db.execute(
            f"DELETE FROM {_SCENE_TABLE} WHERE plan_id = %s",
            (plan_id,),
        )
        await self.db.execute(
            f"DELETE FROM {_TARGET_TABLE} WHERE plan_id = %s",
            (plan_id,),
        )

    async def _fetch_scenes(self, plan_id: str) -> list[PlanSceneRecord]:
        rows = await self.db.fetch_all(
            f"SELECT * FROM {_SCENE_TABLE} WHERE plan_id = %s"
            " ORDER BY sort_order, id",
            (plan_id,),
        )
        return [_row_to_scene(row) for row in rows]

    async def _fetch_targets(self, plan_id: str) -> list[PlanTargetRecord]:
        rows = await self.db.fetch_all(
            f"SELECT * FROM {_TARGET_TABLE} WHERE plan_id = %s"
            " ORDER BY sort_order, id",
            (plan_id,),
        )
        return [_row_to_target(row) for row in rows]

    @staticmethod
    def _reserves_scene(
        record: WealthPlanRecord,
        role: str,
        sap_id: str,
        bbk_id: str,
        exclude_plan_id: str | None,
    ) -> bool:
        source_labels = _SCENE_RESERVING_SOURCE_LABELS_BY_ROLE.get(role)
        return bool(
            source_labels
            and record.id != exclude_plan_id
            and record.bbk_id == bbk_id
            and record.status != PUBLISH_STATUS_FAILED
            and (
                (role == ROLE_RM and record.sap_id == sap_id)
                or record.source_label in source_labels
            ),
        )

    @staticmethod
    def _visible_to(
        record: WealthPlanRecord,
        sap_id: str,
        role: str,
        bbk_id: str,
    ) -> bool:
        if record.bbk_id != bbk_id:
            return False
        if (
            role == ROLE_PRESIDENT
            and record.source_label == SOURCE_LABEL_BY_ROLE[ROLE_PRESIDENT]
            and record.sap_id != sap_id
        ):
            return False
        source_labels = get_visible_source_labels(role)
        if source_labels is not None:
            return record.source_label in source_labels
        if record.sap_id == sap_id:
            return True
        return any(target.sap_id == sap_id for target in record.targets)
