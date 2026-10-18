# -*- coding: utf-8 -*-
"""零人调排程只复用已有资源绑定，不调用 SWE 或重建执行环境。"""

import json
import logging

from .operations import IDENTITY_FIELDS, WHERE_JOB

logger = logging.getLogger(__name__)


class ExistingBindings:
    def __init__(self, db):
        self.db = db

    async def prepare(self, operation, _caller):
        key = tuple(operation[name] for name in IDENTITY_FIELDS)
        state = await self.db.fetch_one(
            f"SELECT task_chat_id, task_session_id FROM swe_cron_job_states WHERE {WHERE_JOB}",
            key,
        )
        if (
            not state
            or not state["task_chat_id"]
            or not state["task_session_id"]
        ):
            raise ValueError("cron_existing_binding_missing")
        spec = json.loads(operation["payload_json"])["spec"]
        spec["meta"].update(
            task_chat_id=state["task_chat_id"],
            task_session_id=state["task_session_id"],
        )
        logger.info(
            "调排程复用已有绑定，不调用 SWE operation_id=%s job_id=%s",
            operation["operation_id"],
            operation["job_id"],
        )
        return {
            "spec": spec,
            "chat_id": state["task_chat_id"],
            "session_id": state["task_session_id"],
        }
