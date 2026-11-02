# -*- coding: utf-8 -*-
"""Persistence contracts for complete cron authorization snapshots."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from swe.app.crons.auth_repository import (
    CronAuthRepository,
    CronAuthRepositoryUnavailable,
)
from swe.app.crons.auth_state import CronAuthState
from swe.config.context import encode_scope_id


class FakeDatabase:
    """In-memory SQL double with binary keys and MySQL upsert semantics."""

    is_connected = True

    def __init__(self):
        self.rows = {}
        self.calls = []
        self.failure: Exception | None = None

    def record(self, query, params):
        self.calls.append((query, params))
        if self.failure:
            raise RuntimeError(str(self.failure))

    async def execute(self, query, params=None):
        self.record(query, params)
        if query.startswith("SELECT"):
            return 0
        if query.startswith("DELETE"):
            return int(self.rows.pop(params[0], None) is not None)
        columns = re.search(r"\((.*?)\)\s*VALUES", query, re.S).group(1)
        names = [name.strip().strip("`") for name in columns.split(",")]
        row = dict(zip(names, params))
        key = row["storage_scope_id"]
        if key not in self.rows:
            self.rows[key] = row
            return 1
        updates = query.split("ON DUPLICATE KEY UPDATE", 1)[1]
        for name in re.findall(r"`?(\w+)`?\s*=\s*VALUES\(", updates):
            self.rows[key][name] = row[name]
        return 2

    async def fetch_one(self, query, params=None):
        self.record(query, params)
        return copy.deepcopy(self.rows.get(params[0]))

    async def fetch_all(self, query, params=None):
        self.record(query, params)
        keys = sorted(self.rows)
        if "WHERE" in query:
            keys = [key for key in keys if key > params[0]]
        return [copy.deepcopy(self.rows[key]) for key in keys[: params[-1]]]


@pytest.fixture
def db():
    return FakeDatabase()


@pytest.fixture
def repository(db):
    return CronAuthRepository(db)


@pytest.mark.asyncio
async def test_complete_snapshot_round_trips_without_losing_user_info(
    repository,
    db,
):
    offset_time = datetime(
        2026,
        10,
        29,
        18,
        30,
        tzinfo=timezone(timedelta(hours=8)),
    )
    state = CronAuthState(
        user_info={"未知": {"items": [1, None, True, {"opaque": "value"}]}},
        user_info_expires_at=offset_time,
        user_info_refreshed_at=offset_time,
        auth_token="secret-token",
        auth_token_expires_at=offset_time,
        cookie_header="session=secret-cookie",
        last_prefetch_at=offset_time,
        last_error="previous error",
    )
    await repository.upsert("default_RMASSIST", state)
    row = await repository.get("default_RMASSIST")
    assert row["storage_scope_id"] == "default_RMASSIST"
    assert repository.state_from_row(row) == state
    stored = next(iter(db.rows.values()))
    assert isinstance(stored["user_info"], str)
    assert json.loads(stored["user_info"]) == state.user_info
    for name in (
        "user_info_expires_at",
        "user_info_refreshed_at",
        "auth_token_expires_at",
        "last_prefetch_at",
    ):
        assert stored[name] == datetime(2026, 10, 29, 10, 30)
        assert (
            repository.state_from_row(row).model_dump()[name].tzinfo
            == timezone.utc
        )
    assert stored["created_at"].tzinfo is None
    assert stored["updated_at"] == stored["created_at"]
    assert "secret-token" not in db.calls[0][0]
    assert "secret-cookie" not in db.calls[0][0]


@pytest.mark.asyncio
async def test_user_info_uses_the_same_json_normalization_as_file_storage(
    repository,
):
    state = CronAuthState(
        user_info={
            "nested": {
                "observed_at": datetime(
                    2026,
                    10,
                    29,
                    10,
                    30,
                    tzinfo=timezone.utc,
                ),
                "identifier": UUID("aee4f7bb-c2a5-4fe3-b5aa-f1b7b3ab4753"),
            },
        },
    )
    expected = CronAuthState.model_validate(state.model_dump(mode="json"))
    await repository.upsert("scope", state)
    row = await repository.get("scope")
    assert json.loads(row["user_info"]) == expected.user_info
    assert repository.state_from_row(row) == expected


@pytest.mark.asyncio
async def test_upsert_replaces_snapshot_and_clears_nulls(repository, db):
    await repository.upsert(
        "tenant",
        CronAuthState(
            user_info={"old": True},
            auth_token="old",
            cookie_header="old",
            last_error="old",
            user_info_expires_at=datetime(2026, 10, 29),
        ),
    )
    created = next(iter(db.rows.values()))["created_at"]
    await repository.upsert("tenant", CronAuthState())
    row = await repository.get("tenant")
    assert repository.state_from_row(row) == CronAuthState()
    assert row["created_at"] == created
    assert row["updated_at"] >= created
    assert len(db.rows) == 1


@pytest.mark.asyncio
async def test_exact_storage_keys_remain_distinct_and_metadata_uses_scope(
    repository,
):
    scope = encode_scope_id("Alice", "RMASSIST")
    keys = [
        "default",
        "default_RMASSIST",
        "Alice",
        "alice",
        "Alice ",
        scope,
        f"scope.v1.{scope}",
    ]
    for key in keys:
        await repository.upsert(key, CronAuthState(user_info={"key": key}))
    for key in keys:
        row = await repository.get(key)
        assert row["storage_scope_id"] == key
        assert repository.state_from_row(row).user_info == {"key": key}
    row = await repository.get(scope)
    assert (row["tenant_id"], row["source_id"]) == ("Alice", "RMASSIST")
    assert (await repository.get("default"))["tenant_id"] is None
    assert (await repository.get("Alice"))["source_id"] is None


@pytest.mark.asyncio
async def test_delete_is_idempotent_and_does_not_affect_other_scope(
    repository,
):
    await repository.upsert("Alice", CronAuthState(auth_token="a"))
    await repository.upsert("alice", CronAuthState(auth_token="b"))
    await repository.delete("Alice")
    await repository.delete("Alice")
    assert await repository.get("Alice") is None
    assert (
        repository.state_from_row(await repository.get("alice")).auth_token
        == "b"
    )


@pytest.mark.asyncio
async def test_pages_use_binary_keyset_order_and_bound_limit(repository, db):
    keys = ["z", "A", "a", "default", "B"]
    for key in keys:
        await repository.upsert(key, CronAuthState())
    first = await repository.list_rows(None, 2)
    second = await repository.list_rows(first[-1]["storage_scope_id"], 2)
    last = await repository.list_rows(second[-1]["storage_scope_id"], 2)
    assert [r["storage_scope_id"] for r in first + second + last] == sorted(
        keys,
    )
    assert await repository.list_rows("z", 2) == []
    query, params = db.calls[-1]
    assert re.search(r"ORDER BY\s+BINARY\s+`?storage_scope_id`?", query)
    assert "OFFSET" not in query
    assert params == (b"z", 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1])
async def test_pages_reject_nonpositive_limits(repository, limit):
    with pytest.raises(ValueError, match="limit"):
        await repository.list_rows(None, limit)


@pytest.mark.asyncio
async def test_schema_check_only_selects_and_surfaces_missing_schema(
    repository,
    db,
):
    await repository.check_schema()
    assert all(query.startswith("SELECT") for query, _ in db.calls)
    assert "user_info_refreshed_at" in db.calls[-1][0]
    db.failure = RuntimeError("table missing with secret-token")
    with pytest.raises(CronAuthRepositoryUnavailable, match="schema") as error:
        await repository.check_schema()
    assert "secret-token" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["check_schema", "upsert", "delete", "get", "list_rows"],
)
async def test_all_operations_raise_when_database_unavailable(db, operation):
    db.is_connected = False
    repository = CronAuthRepository(db)
    args = {
        "upsert": ("scope", CronAuthState()),
        "delete": ("scope",),
        "get": ("scope",),
        "list_rows": (None, 10),
        "check_schema": (),
    }
    with pytest.raises(CronAuthRepositoryUnavailable, match="unavailable"):
        await getattr(repository, operation)(*args[operation])
    assert db.calls == []


@pytest.mark.asyncio
async def test_database_write_failure_is_strict_and_sanitized(
    repository,
    db,
    caplog,
):
    db.failure = RuntimeError("secret-token raw SQL failure")
    with pytest.raises(CronAuthRepositoryUnavailable) as error:
        await repository.upsert(
            "scope",
            CronAuthState(auth_token="secret-token"),
        )
    assert "secret-token" not in str(error.value)
    assert "secret-token" not in caplog.text


def test_manual_schema_is_idempotent_and_stores_json_as_text():
    sql = (
        Path(__file__).parents[3] / "scripts/sql/cron_auth_states.sql"
    ).read_text()
    assert "CREATE TABLE IF NOT EXISTS swe_cron_auth_states" in sql
    assert re.search(r"storage_scope_id\s+VARBINARY\(", sql)
    assert re.search(r"user_info\s+LONGTEXT", sql)
    assert "PRIMARY KEY (storage_scope_id)" in sql
