# -*- coding: utf-8 -*-
"""在现有会话产物中暂存待提交结果凭证，不作为任务定义或展示状态来源。"""

import json
from dataclasses import asdict

from .managed_history import safe_metadata
from .managed_results import ExecutionOutcome

RECEIPTS = "managed_cron_result_receipts"


def _binding(run):
    spec = json.loads(run["spec_json"])
    return spec["meta"]["task_session_id"], spec["meta"]["creator_user_id"]


class SessionResultReceipts:
    def __init__(self, session):
        self.session = session

    async def save(self, run, outcome):
        session, user = _binding(run)
        value = {
            "execution_id": run["execution_id"],
            "run_owner": run["run_owner"],
            "identity": [
                run[name]
                for name in ("tenant_id", "source_id", "agent_id", "job_id")
            ],
            "outcome": json.loads(
                json.dumps(
                    safe_metadata(asdict(outcome)),
                    ensure_ascii=False,
                    default=str,
                ),
            ),
        }

        def merge(state):
            updated = dict(state)
            receipts = dict(state.get(RECEIPTS) or {})
            receipts[run["execution_id"]] = value
            updated[RECEIPTS] = receipts
            return updated

        await self.session.mutate_session_state(
            session_id=session,
            user_id=user,
            mutator=merge,
            create_if_not_exist=True,
        )

    async def load(self, run):
        session, user = _binding(run)
        state = await self.session.get_session_state_dict(
            session_id=session,
            user_id=user,
            allow_not_exist=True,
        )
        value = (state.get(RECEIPTS) or {}).get(run["execution_id"])
        if not value:
            return None
        key = [
            run[name]
            for name in ("tenant_id", "source_id", "agent_id", "job_id")
        ]
        if value["identity"] != key or value["run_owner"] != run["run_owner"]:
            raise ValueError("cron_result_receipt_identity_conflict")
        return ExecutionOutcome(**value["outcome"])

    async def clear(self, run):
        session, user = _binding(run)

        def remove(state):
            updated = dict(state)
            receipts = dict(state.get(RECEIPTS) or {})
            receipts.pop(run["execution_id"], None)
            updated[RECEIPTS] = receipts
            return updated

        await self.session.mutate_session_state(
            session_id=session,
            user_id=user,
            mutator=remove,
            create_if_not_exist=False,
        )
