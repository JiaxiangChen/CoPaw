# -*- coding: utf-8 -*-
"""新批看板结构只读检查，所有业务DDL均由部署方人工执行。"""

from .schema import ManagedSchemaUnavailable

REQUIRED_BOARD_COLUMNS = {
    "swe_cron_managed_batch_members": "batch_id member_id tenant_id source_id agent_id job_id initial_execution_id current_execution_id business_attempt member_role dispatch_order version created_at updated_at",
    "swe_cron_managed_dispatch_events": "event_order event_id event_key tenant_id source_id agent_id job_id execution_id batch_id member_id event_type execution_version actor_id occurred_at details_json",
}


async def assert_managed_board_schema_ready(db):
    for table, fields in REQUIRED_BOARD_COLUMNS.items():
        try:
            await db.fetch_all(
                "SELECT "
                + ",".join(fields.split())
                + f" FROM {table} LIMIT 0",
            )
        except Exception as exc:
            raise ManagedSchemaUnavailable(table) from exc
