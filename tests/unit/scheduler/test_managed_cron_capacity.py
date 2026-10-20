# -*- coding: utf-8 -*-
"""共用模型池统计新旧在途占位，未启用新运行时不触碰新表。"""

from datetime import timedelta

import pytest

from tests.unit.scheduler.test_managed_cron_executions import (
    enqueue,
    execution_setup,
)


class Cursor:
    def __init__(self, db):
        self.db = db
        self.rows = []

    async def execute(self, sql, params):
        self.rows = await self.db.fetch_all(
            sql,
            tuple(
                value.isoformat(" ") if hasattr(value, "isoformat") else value
                for value in params
            ),
        )

    async def fetchall(self):
        return self.rows


def configure_pool(db, now):
    db.conn.create_function("JSON_UNQUOTE", 1, lambda value: value)
    db.conn.executescript("""
        CREATE TABLE swe_cron_dispatch_scope_leases (
            source_id TEXT, provider_id TEXT, model_id TEXT,
            lock_owner TEXT, lease_expires_at TEXT
        );
        CREATE TABLE swe_cron_dispatch_worker_capacity (
            id INTEGER, source_id TEXT, provider_id TEXT, model_id TEXT,
            strategy_id TEXT, effective_workers INTEGER, created_at TEXT
        );
        CREATE TABLE swe_cron_dispatch_intents (
            id INTEGER, source_id TEXT, provider_id TEXT, model_id TEXT, status TEXT
        );
    """)
    db.conn.execute(
        "INSERT INTO swe_cron_dispatch_scope_leases VALUES ('bank','default','default','worker',?)",
        ((now + timedelta(minutes=5)).isoformat(" "),),
    )
    db.conn.execute(
        "INSERT INTO swe_cron_dispatch_worker_capacity VALUES (1,'bank','default','default','default',2,?)",
        (now.isoformat(" "),),
    )


@pytest.mark.asyncio
async def test_shared_capacity_counts_new_and_old_occupants_under_same_scope_lock(
    execution_setup,
    monkeypatch,
):
    from scheduler.app.services.cron import capacity_claim
    from scheduler.app.services.cron.managed.operations import utc_now

    db, _, _ = execution_setup
    row, _ = await enqueue(execution_setup)
    now = utc_now()
    configure_pool(db, now)
    monkeypatch.setattr(
        capacity_claim,
        "MANAGED_CAPACITY_ACCOUNTING",
        True,
        raising=False,
    )
    policy = {
        "strategy_id": "default",
        "effective_workers": 100,
        "min_workers": 1,
        "max_workers": 200,
    }

    async def available():
        return await capacity_claim.available_capacity(
            Cursor(db),
            ("bank", "default", "default"),
            "worker",
            now,
            policy,
        )

    assert await available() == 2
    for status in ("dispatching", "accepted", "running"):
        db.conn.execute(
            "UPDATE swe_cron_execution_runs SET status=?",
            (status,),
        )
        assert await available() == 1
    db.conn.execute(
        "INSERT INTO swe_cron_dispatch_intents VALUES (1,'bank','default','default','dispatched')",
    )
    assert await available() == 0
    db.conn.execute("UPDATE swe_cron_execution_runs SET status='error'")
    assert await available() == 1
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET status='running', model_id='another-model'",
    )
    assert await available() == 1
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET model_id='default', dispatch_meta_json='{}'",
    )
    assert await available() == 1
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET dispatch_meta_json='invalid json'",
    )
    with pytest.raises(Exception, match="malformed JSON"):
        await available()
    monkeypatch.setattr(capacity_claim, "MANAGED_CAPACITY_ACCOUNTING", False)
    assert await available() == 1
    assert row["execution_id"]
