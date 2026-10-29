# -*- coding: utf-8 -*-
"""本地验收专用：真实追踪接口 + 内存 SQLite，不连接业务数据库。"""

import json
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI

from scheduler.app.routers.cron_task_tracking import router, tracking_db
from tests.fakes.cron_task_tracking import TrackingDatabase

app = FastAPI()
db = TrackingDatabase()
db.add("tracking-local-job")
db.conn.execute(
    "UPDATE swe_cron_jobs_v2 SET name='客户经营日报 · 通知结果追踪'",
)
identity = {
    "tenant_id": "alice",
    "source_id": "bank",
    "agent_id": "default",
    "job_id": "tracking-local-job",
}
now = datetime.now(timezone.utc).replace(tzinfo=None)
base = now - timedelta(minutes=15)


def stamp(minutes):
    return (base + timedelta(minutes=minutes)).isoformat(" ")


run = db.run(
    "local-run",
    job_id=identity["job_id"],
    kind="batch",
    created_at=stamp(1),
    started_at=stamp(4),
    finished_at=stamp(5),
    settled_at=stamp(6),
    settled_status="success",
    history_execution_id=1,
    output_preview="日报已生成，包含 12 位客户的跟进建议。",
    dispatch_meta_json=json.dumps(
        {
            "protocol": "scheduler_managed_v1",
            "trigger_receipt_id": "local-receipt",
        },
    ),
)
db.insert(
    "swe_cron_managed_trigger_receipts",
    **identity,
    receipt_id="local-receipt",
    kind="batch",
    status="accepted",
    created_at=stamp(0),
    response_json="{}",
    members_json="[]",
)
db.insert(
    "swe_cron_registration_journal",
    **identity,
    operation_id="local-create",
    action="create",
    actor_id="admin",
    status="completed",
    stage="completed",
    payload_json="{}",
    created_at=stamp(-1),
    updated_at=stamp(0),
)
db.insert(
    "swe_cron_managed_broadcast_tasks",
    **identity,
    task_id="local-broadcast",
    status="completed",
    created_at=(base - timedelta(days=2)).isoformat(" "),
)
for index, kind in enumerate(
    ("queued", "dispatching", "accepted", "running", "success", "settled"),
    1,
):
    db.insert(
        "swe_cron_managed_dispatch_events",
        **identity,
        event_order=index,
        event_id=f"local-event-{index}",
        event_key=f"local-key-{index}",
        execution_id="local-run",
        event_type=kind,
        occurred_at=stamp(index),
        details_json=json.dumps(
            {
                "status": (
                    "success" if kind in {"success", "settled"} else kind
                ),
                "settled_status": "success" if kind == "settled" else None,
            },
        ),
    )
beijing = (base + timedelta(hours=8, minutes=8)).isoformat(" ")
db.insert(
    "swe_cron_executions",
    id=1,
    tenant_id="alice",
    job_id=identity["job_id"],
    status="success",
    async_status="success",
    output_preview=run["output_preview"],
    notification_status="sent",
    notification_sent_at=beijing,
    notification_attempts=2,
    is_read=0,
    meta=json.dumps(
        {
            "management_owner": "scheduler",
            "managed_execution_id": "local-run",
            "source_id": "bank",
            "agent_id": "default",
        },
    ),
)
app.include_router(router, prefix="/api")
app.dependency_overrides[tracking_db] = lambda: db
