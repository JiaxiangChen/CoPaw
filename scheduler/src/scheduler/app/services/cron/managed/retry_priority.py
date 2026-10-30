# -*- coding: utf-8 -*-
"""判定原批正常尝试是否已结束，只影响重跑优先级，不阻塞其他批次。"""

from .batch_failures import member_rows


async def lane_for_batch(db, batch_id):
    """复用自动重试资格，避免把等待下一次业务尝试的批次误认为已结束。"""
    joined = "FROM swe_cron_managed_batch_members m JOIN swe_cron_execution_runs r ON r.execution_id=m.current_execution_id WHERE m.batch_id=%s"
    prior = await db.fetch_one(
        "SELECT r.execution_id "
        + joined
        + " AND r.dispatch_lane='urgent' LIMIT 1",
        (batch_id,),
    )
    if prior:
        # 加急只能由服务器在正常批结束后写入，是后续重跑的已结束证明。
        return "urgent"
    active = await db.fetch_one(
        "SELECT r.execution_id "
        + joined
        + " AND (r.status NOT IN ('success','error','skipped','cancelled') OR (r.status='success' AND r.settled_status IS NULL)) LIMIT 1",
        (batch_id,),
    )
    if active:
        return "normal"
    receipt = await db.fetch_one(
        "SELECT * FROM swe_cron_managed_trigger_receipts WHERE batch_id=%s AND kind='batch' AND status='accepted'",
        (batch_id,),
    )
    if receipt is None:
        return "normal"
    rows = await member_rows(db, receipt)
    if not rows:
        return "normal"
    from .auto_retry import AutoBatchRetry

    for row in rows:
        if await AutoBatchRetry._eligible(db, row):
            return "normal"
    return "urgent"
