# -*- coding: utf-8 -*-
"""仅在独立本机临时 MySQL 验证四列 DDL、双写及人工回填。"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

import pytest

from scheduler.app.services.cron.managed.storage import TransactionConnection
from tests.fakes.cron_profile_mysql import (
    mysql_db,
    private_mysql,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("CRON_TEST_PRIVATE_MYSQL") != "1",
    reason="仅在显式允许时启动本机临时 MySQL",
)
ROOT = Path(__file__).resolve().parents[3]
FIELDS = ("tenant_name", "bbk_id", "skill_ids", "workflow_binding_id")


def statements(name):
    sql = (ROOT / "docs/deployment" / name).read_text(encoding="utf-8")
    return [
        part.strip()
        for part in re.sub(r"--[^\n]*", "", sql).split(";")
        if part.strip()
    ]


async def create_tables(db):
    for statement in statements("cron_phase2_manual_create.sql"):
        await db.execute(statement)


async def seed(db, job_id, spec, owner="scheduler"):
    encoded = (
        json.dumps(spec, ensure_ascii=False)
        if not isinstance(spec, str)
        else spec
    )
    await db.execute(
        "INSERT INTO swe_cron_jobs_v2 "
        "(tenant_id,source_id,agent_id,job_id,scope_id,name,task_type,cron_expr,timezone,spec_json,creator_user_id,management_owner,created_at,updated_at,tenant_name,bbk_id,skill_ids,workflow_binding_id) "
        "VALUES ('alice','bank','default',%s,'scope','report','text','0 9 * * *','Asia/Shanghai',%s,'alice',%s,%s,%s,'keep','keep','keep','keep')",
        (
            job_id,
            encoded,
            owner,
            datetime(2026, 10, 20),
            datetime(2026, 10, 20),
        ),
    )


@pytest.mark.asyncio
async def test_mysql_upgrade_and_safe_repeatable_backfill(mysql_db):
    await create_tables(mysql_db)
    for field in FIELDS:
        await mysql_db.execute(
            f"ALTER TABLE swe_cron_jobs_v2 DROP COLUMN {field}",
        )
    for statement in statements("cron_profile_columns_manual_upgrade.sql"):
        await mysql_db.execute(statement)
    values = dict(zip(FIELDS, ("张三", "100", "skill-a", "bind-a")))
    await seed(mysql_db, "legacy", values, "legacy")
    await seed(mysql_db, "new", values)
    await seed(mysql_db, "missing", {})
    await seed(mysql_db, "nulls", dict.fromkeys(FIELDS))
    await seed(mysql_db, "literal-null", dict.fromkeys(FIELDS, "null"))
    await seed(mysql_db, "invalid-json", "not-json")
    await seed(mysql_db, "array", [])
    await seed(mysql_db, "wrong-type", {"skill_ids": ["skill-a"]})
    await seed(mysql_db, "too-long", {"tenant_name": "字" * 256})
    await seed(mysql_db, "concurrent", values)
    original = await mysql_db.fetch_all(
        "SELECT * FROM swe_cron_jobs_v2 ORDER BY job_id",
    )
    async with mysql_db.pool.acquire() as connection:
        db = TransactionConnection(connection)
        for statement in statements(
            "cron_profile_columns_manual_backfill.sql",
        ):
            if statement.startswith("UPDATE swe_cron_jobs_v2"):
                await db.execute(
                    "UPDATE swe_cron_jobs_v2 SET spec_json=%s WHERE job_id='concurrent'",
                    (
                        json.dumps(
                            {**values, "bbk_id": "200"},
                            ensure_ascii=False,
                        ),
                    ),
                )
            await db.execute(statement)
    first = {
        item["job_id"]: item
        for item in await mysql_db.fetch_all("SELECT * FROM swe_cron_jobs_v2")
    }
    assert first["concurrent"]["bbk_id"] == "keep"
    async with mysql_db.pool.acquire() as connection:
        db = TransactionConnection(connection)
        for statement in statements(
            "cron_profile_columns_manual_backfill.sql",
        ):
            await db.execute(statement)
    final = {
        item["job_id"]: item
        for item in await mysql_db.fetch_all("SELECT * FROM swe_cron_jobs_v2")
    }
    assert final["concurrent"]["bbk_id"] == "200"
    for job_id in ("legacy", "new"):
        assert {field: final[job_id][field] for field in FIELDS} == values
    for job_id in ("missing", "nulls"):
        assert [final[job_id][field] for field in FIELDS] == ["", "", "", None]
    assert [final["literal-null"][field] for field in FIELDS] == ["null"] * 4
    for job_id in ("invalid-json", "array", "wrong-type", "too-long"):
        assert [final[job_id][field] for field in FIELDS] == ["keep"] * 4
    for before in original:
        after = final[before["job_id"]]
        for field in before.keys() - set(FIELDS) - {"spec_json"}:
            assert before[field] == after[field]
        if before["job_id"] != "concurrent":
            assert before["spec_json"] == after["spec_json"]


@pytest.mark.asyncio
async def test_mysql_management_writes_profile_columns_from_same_spec(
    mysql_db,
):
    from monitor.app.database.schema import CREATE_CRON_JOBS_TABLE
    from scheduler.app.services.cron.managed.identity import CallerIdentity
    from scheduler.app.services.cron.managed.management import (
        ManagementService,
    )
    from tests.unit.scheduler.test_managed_cron_management import (
        PlatformClient,
        ResourceClient,
        create_payload,
    )

    await create_tables(mysql_db)
    await mysql_db.execute(CREATE_CRON_JOBS_TABLE)
    await mysql_db.execute(
        "ALTER TABLE swe_cron_jobs ADD COLUMN workflow_binding_id VARCHAR(128)",
    )
    caller = CallerIdentity(
        "alice",
        "bank",
        "default",
        "alice",
        bbk_id="100",
        user_name="张三",
    )
    service = ManagementService(mysql_db, ResourceClient(), PlatformClient())
    result = await service.create(
        caller,
        create_payload(
            task_type="workflow",
            skill_ids="skill-a",
            workflow_binding_id="bind-a",
        ),
    )
    assert result["status"] == "completed"
    stored = await mysql_db.fetch_one("SELECT * FROM swe_cron_jobs_v2")
    assert [stored[field] for field in FIELDS] == [
        "张三",
        "100",
        "skill-a",
        "bind-a",
    ]
    spec = json.loads(stored["spec_json"])
    assert all(stored[field] == spec[field] for field in FIELDS)
