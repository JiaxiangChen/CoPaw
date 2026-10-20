# -*- coding: utf-8 -*-
"""按技能查询已绑定定时任务的只读接口。

任务的 skill_ids 以逗号分隔保存，按等值比较会漏掉列表中间的技能，
因此统一用 FIND_IN_SET 做成员匹配。旧表 swe_cron_jobs 与新表
swe_cron_jobs_v2 各有一个查询入口，命中多行时取最新创建且未删除的一条。
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

router = APIRouter(prefix="/cron/skill-jobs", tags=["cron"])
logger = logging.getLogger(__name__)

DEFAULT_AGENT_ID = "default"
SKILL_JOB_NOT_FOUND = "cron_skill_job_not_found"
DATABASE_UNAVAILABLE = "cron_database_unavailable"

# 旧表的 skill_ids 是逗号分隔列，排除软删除后按创建时间取最新一条。
LEGACY_SKILL_JOB_SQL = (
    "SELECT id, bbk_id FROM swe_cron_jobs "
    "WHERE tenant_id = %s AND source_id = %s "
    "AND FIND_IN_SET(%s, skill_ids) > 0 "
    "AND deleted_at IS NULL AND status != 'deleted' "
    "ORDER BY created_at DESC, id DESC LIMIT 1"
)
# 新表直接使用与完整定义同步保存的展开列，不再解析 spec_json。
MANAGED_SKILL_JOB_SQL = (
    "SELECT job_id, bbk_id FROM swe_cron_jobs_v2 "
    "WHERE tenant_id = %s AND source_id = %s AND agent_id = %s "
    "AND FIND_IN_SET(%s, skill_ids) > 0 "
    "AND deleted_at IS NULL "
    "ORDER BY created_at DESC, job_id DESC LIMIT 1"
)


class LegacySkillJobRequest(BaseModel):
    """旧表查询入参，身份字段均必填。"""

    model_config = ConfigDict(extra="forbid")

    skill_id: str = Field(
        min_length=1,
        max_length=200,
        description="技能 ID，任务内以逗号分隔保存多个",
    )
    tenant_id: str = Field(
        min_length=1,
        max_length=128,
        description="任务所属租户",
    )
    source_id: str = Field(
        min_length=1,
        max_length=128,
        description="任务所属来源系统",
    )

    @field_validator("*", mode="before")
    @classmethod
    def _strip(cls, value):
        """去掉调用方可能带入的首尾空白，避免成员匹配失配。"""
        return value.strip() if isinstance(value, str) else value


class ManagedSkillJobRequest(LegacySkillJobRequest):
    """新表查询入参，额外限定智能体。"""

    agent_id: str = Field(
        default=DEFAULT_AGENT_ID,
        min_length=1,
        max_length=128,
        description="任务所属智能体，默认 default",
    )


class LegacySkillJobBinding(BaseModel):
    """旧表命中的任务标识。"""

    id: str = Field(description="swe_cron_jobs 任务 ID")
    bbk_id: str = Field(default="", description="任务所属分行")


class ManagedSkillJobBinding(BaseModel):
    """新表命中的任务标识。"""

    job_id: str = Field(description="swe_cron_jobs_v2 任务定义标识")
    bbk_id: str = Field(default="", description="任务所属分行")


def _database(request: Request):
    db = getattr(request.app.state, "db_connection", None)
    if db is None or not getattr(db, "is_connected", False):
        raise HTTPException(503, DATABASE_UNAVAILABLE)
    return db


async def _fetch_one(db, sql, params):
    try:
        return await db.fetch_one(sql, params)
    except Exception as exc:
        logger.error(
            "定时任务技能查询失败 error_type=%s",
            type(exc).__name__,
        )
        raise HTTPException(503, DATABASE_UNAVAILABLE) from exc


@router.post(
    "/legacy",
    response_model=LegacySkillJobBinding,
    summary="按技能查询旧表最新任务",
)
async def get_legacy_job_by_skill(
    request: Request,
    body: LegacySkillJobRequest,
) -> LegacySkillJobBinding:
    """从 swe_cron_jobs 取绑定该技能的最新未删除任务。

    skill_ids 允许逗号分隔保存多个技能，所以按 FIND_IN_SET 成员匹配；
    命中多行时按 created_at、id 倒序取第一条，保证同一输入稳定返回同一任务。
    """
    db = _database(request)
    row = await _fetch_one(
        db,
        LEGACY_SKILL_JOB_SQL,
        (body.tenant_id, body.source_id, body.skill_id),
    )
    if row is None:
        raise HTTPException(404, SKILL_JOB_NOT_FOUND)
    return LegacySkillJobBinding(id=row["id"], bbk_id=row.get("bbk_id") or "")


@router.post(
    "/managed",
    response_model=ManagedSkillJobBinding,
    summary="按技能查询新表最新任务",
)
async def get_managed_job_by_skill(
    request: Request,
    body: ManagedSkillJobRequest,
) -> ManagedSkillJobBinding:
    """从 swe_cron_jobs_v2 取绑定该技能的最新未删除任务标识。

    新表按 tenant_id、source_id、agent_id、job_id 区分任务，技能和分行直接
    读取 skill_ids、bbk_id 列；同样在命中多行时取最新创建的一条。
    """
    db = _database(request)
    row = await _fetch_one(
        db,
        MANAGED_SKILL_JOB_SQL,
        (body.tenant_id, body.source_id, body.agent_id, body.skill_id),
    )
    if row is None:
        raise HTTPException(404, SKILL_JOB_NOT_FOUND)
    return ManagedSkillJobBinding(
        job_id=row["job_id"],
        bbk_id=row.get("bbk_id") or "",
    )
