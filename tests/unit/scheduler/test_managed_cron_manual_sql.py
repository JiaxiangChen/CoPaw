# -*- coding: utf-8 -*-
"""静态核对人工建表材料与实际字段契约，不连接数据库或执行 DDL。"""

import re
from pathlib import Path

import pytest

from scheduler.app.services.cron.managed.schema import REQUIRED_COLUMNS

ROOT = Path(__file__).resolve().parents[3]
SQL = ROOT / "docs/deployment/cron_phase2_manual_create.sql"
MANAGED_TABLES = {
    name for name in REQUIRED_COLUMNS if name.startswith("swe_cron_managed_")
} | {
    "swe_cron_jobs_v2",
    "swe_cron_job_states",
    "swe_cron_registration_journal",
    "swe_cron_execution_runs",
}


def _definitions():
    text = SQL.read_text(encoding="utf-8")
    return dict(
        re.findall(
            r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\) ENGINE=InnoDB[^;]*;",
            text,
            re.S,
        ),
    )


def test_manual_create_covers_exactly_new_protocol_tables():
    definitions = _definitions()
    assert set(definitions) == MANAGED_TABLES
    for table, body in definitions.items():
        columns = set(
            re.findall(
                r"^\s+(\w+)\s+(?:VARCHAR|CHAR|BIGINT|INT|TINYINT|DATETIME|LONGTEXT|TEXT)\b",
                body,
                re.M,
            ),
        )
        assert set(REQUIRED_COLUMNS[table].split()) <= columns, table
        for line in body.splitlines():
            if re.match(
                r"\s+\w+\s+(?:VARCHAR|CHAR|BIGINT|INT|TINYINT|DATETIME|LONGTEXT|TEXT)\b",
                line,
            ):
                assert re.search(r"COMMENT '[^']*[\u4e00-\u9fff]", line), line


def test_manual_create_preserves_identity_idempotency_and_monotonic_order():
    definitions = _definitions()
    for table in (
        "swe_cron_jobs_v2",
        "swe_cron_job_states",
        "swe_cron_managed_dispatch_controls",
    ):
        assert (
            "PRIMARY KEY (tenant_id, source_id, agent_id, job_id)"
            in definitions[table]
        )
    assert (
        "UNIQUE KEY uk_cron_management_active (active_key)"
        in definitions["swe_cron_registration_journal"]
    )
    assert (
        "UNIQUE KEY uk_managed_broadcast_active (active_key)"
        in definitions["swe_cron_managed_broadcast_tasks"]
    )
    runs = definitions["swe_cron_execution_runs"]
    assert "queue_order BIGINT UNSIGNED NOT NULL AUTO_INCREMENT" in runs
    assert "UNIQUE KEY uk_managed_queue_order (queue_order)" in runs
    assert "UNIQUE KEY uk_managed_execution_key (execution_key)" in runs
    assert (
        "UNIQUE KEY uk_managed_trigger_callback (callback_key)"
        in definitions["swe_cron_managed_trigger_receipts"]
    )
    assert not re.search(r"DATETIME(?!\(6\))", SQL.read_text(encoding="utf-8"))


def test_manual_material_preserves_phase_one_fields_and_covers_all_dependencies():
    from swe.app.crons.migration_repository import (
        DEFINITION_FIELDS,
        STATE_FIELDS,
    )

    definitions = _definitions()
    extra_definition = {
        "imported_at",
        "source_observed_at",
        "migration_batch_id",
    }
    extra_state = {"state_observed_at", "runtime_updated_at"}
    for table, fields in (
        ("swe_cron_jobs_v2", DEFINITION_FIELDS | extra_definition),
        ("swe_cron_job_states", STATE_FIELDS | extra_state),
    ):
        for column in fields:
            assert re.search(rf"^\s+{column}\s+", definitions[table], re.M), (
                table,
                column,
            )
    audit = (ROOT / "docs/deployment/cron_phase2_schema_audit.sql").read_text(
        encoding="utf-8",
    )
    for table, fields in REQUIRED_COLUMNS.items():
        for column in fields.split():
            assert (
                f"SELECT '{table}' AS table_name, '{column}' AS column_name"
                in audit
            )
    statements = re.sub(r"--[^\n]*", "", audit)
    assert not re.search(
        r"\b(?:INSERT|UPDATE|DELETE|ALTER|DROP|CREATE|TRUNCATE)\b",
        statements,
        re.I,
    )


@pytest.mark.parametrize(
    "table,fields",
    [
        (
            "swe_workflow_bindings",
            "binding_id source_id skill_id current_version enabled",
        ),
        (
            "swe_workflow_binding_versions",
            "binding_id version skill_id config_json",
        ),
    ],
)
def test_manual_audit_also_covers_existing_workflow_lookup_dependencies(
    table,
    fields,
):
    audit = (ROOT / "docs/deployment/cron_phase2_schema_audit.sql").read_text(
        encoding="utf-8",
    )
    for column in fields.split():
        assert (
            f"SELECT '{table}' AS table_name, '{column}' AS column_name"
            in audit
        )
