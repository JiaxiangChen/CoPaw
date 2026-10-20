# -*- coding: utf-8 -*-
"""技能查询直接使用展开列，不受旧定义 JSON 内容影响。"""

import os

import pytest

from swe.app.crons.skill_job_lookup import MANAGED_SKILL_JOB_SQL
from tests.fakes.cron_profile_mysql import mysql_db, private_mysql
from tests.integrated.scheduler.test_cron_profile_columns_mysql import (
    create_tables,
    seed,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("CRON_TEST_PRIVATE_MYSQL") != "1",
    reason="仅在显式允许时启动本机临时 MySQL",
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spec",
    ["invalid-json", {"skill_ids": "other-skill", "bbk_id": "json-branch"}],
)
async def test_managed_lookup_uses_columns_and_preserves_filters(
    mysql_db,
    spec,
):
    await create_tables(mysql_db)
    await seed(mysql_db, "winner", spec)
    await seed(mysql_db, "a-older", {})
    await seed(mysql_db, "z-partial", {})
    await seed(
        mysql_db,
        "z-json-only",
        {"skill_ids": "first,report,last", "bbk_id": "wrong-json-branch"},
    )
    await mysql_db.execute(
        "UPDATE swe_cron_jobs_v2 SET bbk_id='2002', skill_ids='first,report,last' "
        "WHERE job_id IN ('winner','a-older')",
    )
    await mysql_db.execute(
        "UPDATE swe_cron_jobs_v2 SET skill_ids='report-extra' WHERE job_id='z-partial'",
    )
    for job_id, column, value in (
        ("z-tenant", "tenant_id", "other"),
        ("z-source", "source_id", "other"),
        ("z-agent", "agent_id", "other"),
        ("z-deleted", "deleted_at", "2026-10-20 01:00:00"),
    ):
        await seed(mysql_db, job_id, {})
        await mysql_db.execute(
            f"UPDATE swe_cron_jobs_v2 SET `{column}`=%s, skill_ids='report' WHERE job_id=%s",
            (value, job_id),
        )
    found = await mysql_db.fetch_one(
        MANAGED_SKILL_JOB_SQL,
        ("alice", "bank", "default", "report"),
    )
    assert found == {"job_id": "winner", "bbk_id": "2002"}
