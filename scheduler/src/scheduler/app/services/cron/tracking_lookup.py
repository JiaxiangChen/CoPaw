# -*- coding: utf-8 -*-
"""当前授权来源内定位任务；定义缺行才允许新协议历史回退。"""

from .tracking_common import (
    BROADCASTS,
    DEFINITIONS,
    KEYS,
    MEMBERS,
    RUNS,
    STATES,
    TaskIdentity,
    TrackingNotFound,
    TrackingReader,
    TrackingUnavailable,
    identity_value,
    iso_time,
    json_object,
    new_protocol,
    public_value,
    select_fields,
    where_identity,
)

DEFINITION_FIELDS = (
    "name task_type tenant_name bbk_id skill_ids workflow_binding_id enabled "
    "cron_expr original_cron_expr timezone offset_minutes offset_window_hours "
    "enable_offset creator_user_id definition_version job_origin plan_id "
    "deleted_at created_at updated_at run_start_date run_end_date"
)
STATE_FIELDS = (
    "task_chat_id task_session_id ordinary_external_job_id batch_external_job_id "
    "dispatch_state pause_reason auto_paused_at next_run_at next_run_times_json "
    "last_status last_run_at last_error management_status management_version "
    "state_version unread_execution_count updated_at date_window_paused date_window_sync_required"
)
SPEC_FIELDS = (
    "text",
    "request",
    "runtime",
    "model_slot",
    "schedule",
    "run_date_range",
)
CANDIDATE_FIELDS = ",".join(
    (
        *KEYS,
        "name",
        "tenant_name",
        "task_type",
        "deleted_at",
        "management_owner",
        *("parent_" + name for name in KEYS),
    ),
)


def timed_fields(row, fields, timestamps):
    result = select_fields(row, fields)
    result.update({name: iso_time(row.get(name)) for name in timestamps})
    return result


class TrackingLookup:
    def __init__(self, db, source, *, reader=None):
        self.source = identity_value(source)
        self.reader = reader or TrackingReader(db)

    async def lookup(self, job_id, tenant_id=None, agent_id=None):
        selected = {"source_id": self.source, "job_id": identity_value(job_id)}
        for name, value in (("tenant_id", tenant_id), ("agent_id", agent_id)):
            if value is not None:
                selected[name] = identity_value(value)
        where = where_identity(names=tuple(selected))
        params = tuple(selected.values())
        definitions = await self.reader.rows(
            DEFINITIONS,
            f"SELECT {CANDIDATE_FIELDS} FROM {DEFINITIONS} WHERE {where} ORDER BY tenant_id,agent_id LIMIT 21",
            params,
            required=True,
        )
        histories = await self.reader.rows(
            RUNS,
            f"SELECT DISTINCT {','.join(KEYS)} FROM {RUNS} WHERE {where} AND {new_protocol()} "
            "ORDER BY tenant_id,agent_id LIMIT 21",
            params,
        )
        history_unavailable = self.reader.coverage.get(RUNS) == "unavailable"
        if not definitions and history_unavailable:
            raise TrackingUnavailable(
                "cron_tracking_storage_unavailable",
                query_id=self.reader.query_id,
            )
        candidates = {
            tuple(row[name] for name in KEYS): row for row in definitions
        }
        for row in histories:
            key = tuple(row[name] for name in KEYS)
            if key not in candidates:
                definition = await self.definition(TaskIdentity(*key))
                candidates[key] = definition or row
        visible = []
        for key, row in sorted(candidates.items()):
            if row.get("management_owner", "scheduler") == "scheduler":
                candidate = self._candidate(row)
                candidate["relation"] = await self.relation(
                    TaskIdentity(*key),
                    row if "management_owner" in row else None,
                )
                visible.append(candidate)
        overflow = (
            len(visible) > 20 or len(definitions) > 20 or len(histories) > 20
        )
        return {
            "candidates": visible[:20],
            "has_more": overflow,
            "requires_selection": overflow
            or history_unavailable
            or len(visible) != 1,
            **self.reader.metadata(),
        }

    @staticmethod
    def _candidate(row):
        return {
            **{name: row[name] for name in KEYS},
            **select_fields(row, "name tenant_name task_type"),
            "deleted": bool(row.get("deleted_at")),
            "history_only": "management_owner" not in row,
        }

    async def definition(self, identity):
        rows = await self.reader.rows(
            DEFINITIONS,
            f"SELECT * FROM {DEFINITIONS} WHERE {where_identity()} LIMIT 1",
            identity.values(),
            required=True,
        )
        return rows[0] if rows else None

    async def resolve(self, job_id, tenant_id, agent_id):
        identity = TaskIdentity(tenant_id, self.source, agent_id, job_id)
        definition = await self.definition(identity)
        if definition is not None:
            if definition.get("management_owner") != "scheduler":
                raise TrackingNotFound("cron_tracking_not_found")
            return identity, definition
        rows = await self.reader.rows(
            RUNS,
            f"SELECT execution_id FROM {RUNS} WHERE {where_identity()} AND {new_protocol()} LIMIT 1",
            identity.values(),
            required=True,
        )
        if not rows:
            raise TrackingNotFound("cron_tracking_not_found")
        return identity, None

    async def overview(self, job_id, tenant_id, agent_id):
        identity, definition = await self.resolve(job_id, tenant_id, agent_id)
        states = await self.reader.rows(
            STATES,
            f"SELECT * FROM {STATES} WHERE {where_identity()} LIMIT 1",
            identity.values(),
        )
        if not states:
            self.reader.warn("state_missing")
        relation = await self.relation(identity, definition)
        return {
            "identity": identity.public(),
            "history_only": definition is None,
            "definition": self.definition_view(definition),
            "state": (
                timed_fields(
                    states[0],
                    STATE_FIELDS,
                    (
                        "auto_paused_at",
                        "next_run_at",
                        "last_run_at",
                        "updated_at",
                    ),
                )
                if states
                else None
            ),
            "relation": relation,
            **self.reader.metadata(),
        }

    def definition_view(self, row):
        if row is None:
            return None
        spec = self.reader.object(
            row.get("spec_json"),
            "definition_spec_unavailable",
        )
        spec = {
            **spec,
            "schedule": {
                "type": "cron",
                "cron": row["cron_expr"],
                "timezone": row["timezone"],
            },
        }
        return {
            **timed_fields(
                row,
                DEFINITION_FIELDS,
                ("created_at", "updated_at", "deleted_at"),
            ),
            "spec": public_value(
                {
                    **{name: spec.get(name) for name in SPEC_FIELDS},
                    "meta": select_fields(
                        json_object(spec.get("meta")),
                        "notification_delay_minutes",
                    ),
                },
            ),
        }

    async def relation(self, identity, definition):
        if definition is None:
            return "unknown"
        parent = [definition.get("parent_" + name) for name in KEYS]
        if any(parent) and (
            not all(parent) or tuple(parent) == identity.values()
        ):
            return "unknown"
        checks = (
            (
                DEFINITIONS,
                where_identity(names=tuple("parent_" + name for name in KEYS)),
            ),
            (BROADCASTS, where_identity()),
            (MEMBERS, where_identity() + " AND member_role='parent'"),
        )
        for table, where in checks:
            rows = await self.reader.rows(
                table,
                f"SELECT 1 AS found FROM {table} WHERE {where} LIMIT 1",
                identity.values(),
            )
            if rows:
                return "unknown" if any(parent) else "parent"
        if all(parent):
            return "child"
        if any(
            self.reader.coverage.get(table) == "unavailable"
            for table, _ in checks
        ):
            return "unknown"
        return "ordinary"
