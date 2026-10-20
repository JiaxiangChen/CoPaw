# -*- coding: utf-8 -*-
"""四列的人工 SQL 和应用启动字段契约必须同步。"""

from pathlib import Path

from scheduler.app.services.cron.managed.schema import REQUIRED_COLUMNS
from swe.app.crons.migration_repository import DEFINITION_FIELDS

ROOT = Path(__file__).resolve().parents[3]
FIELDS = {"tenant_name", "bbk_id", "skill_ids", "workflow_binding_id"}


def test_profile_columns_are_required_by_both_writers_and_manual_ddl():
    assert FIELDS <= set(REQUIRED_COLUMNS["swe_cron_jobs_v2"].split())
    assert FIELDS <= DEFINITION_FIELDS
    text = (ROOT / "docs/deployment/cron_phase2_manual_create.sql").read_text(
        encoding="utf-8",
    )
    definition = text.split("CREATE TABLE IF NOT EXISTS swe_cron_jobs_v2 (")[1]
    definition = definition.split(") ENGINE=InnoDB")[0]
    for field in FIELDS:
        assert f"  {field} VARCHAR(" in definition


def test_profile_upgrade_is_separate_from_backfill_and_subscription_is_deferred():
    folder = ROOT / "docs/deployment"
    upgrade = (folder / "cron_profile_columns_manual_upgrade.sql").read_text(
        encoding="utf-8",
    )
    backfill = (folder / "cron_profile_columns_manual_backfill.sql").read_text(
        encoding="utf-8",
    )
    assert "ADD COLUMN" in upgrade
    assert "UPDATE swe_cron_jobs_v2" in backfill
    assert "JSON_VALID" in backfill
    assert "CHAR_LENGTH" in backfill
    assert "subscription_key" not in upgrade + backfill
    for field in FIELDS:
        assert f"{field} VARCHAR(" in upgrade
        assert f"$.{field}" in backfill
