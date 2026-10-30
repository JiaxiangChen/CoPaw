# -*- coding: utf-8 -*-
"""成员控制必须遵守尚未完成的父组暂停，已读等非启停操作除外。"""

from .definition import request_hash
from .repository import IDENTITY_FIELDS


async def ensure_parent_not_pausing(tx, row):
    """防止冻结清单已暂停后，另一个成员请求又把任务开启。"""
    if not row.get("parent_job_id"):
        return
    key = tuple(row.get("parent_" + name) for name in IDENTITY_FIELDS)
    active = await tx.fetch_one(
        "SELECT operation_id FROM swe_cron_registration_journal WHERE active_key=%s AND action='pause_group'",
        (request_hash(key),),
    )
    if active:
        from .operations import ManagementConflict

        raise ManagementConflict("cron_parent_pause_in_progress")
