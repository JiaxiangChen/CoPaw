# -*- coding: utf-8 -*-
"""执行事件保留当时快照，当前结果仅作独立补充。"""

from .tracking_common import (
    CALLS,
    EVENTS,
    HISTORY,
    RECEIPTS,
    RUNS,
    BEIJING,
    iso_time,
    json_object,
    new_protocol,
    same_identity,
    select_fields,
    where_identity,
)
from .tracking_nodes import make_node, manifest_member, recorded_reason

EVENT_MAP = {
    "queued": ("E01", "执行已登记 / 进入待派发队列", "scheduling"),
    "dispatching": ("E02", "开始派发至 SWE", "scheduling"),
    "accepted": ("E03", "SWE 已受理", "execution"),
    "running": ("E04", "开始执行", "execution"),
    "success": ("E05", "主执行成功", "execution"),
    "error": ("E06", "主执行失败", "execution"),
    "skipped": ("E07", "执行被跳过", "execution"),
    "cancelled": ("E08", "执行已取消", "execution"),
    "settled": ("E09", "Scheduler 已确认整轮结果", "execution"),
    "dispatch_unconfirmed": ("E10", "本次派发未获确认", "scheduling"),
    "transport_redelivery": ("E11", "重新派发（运输重投）", "scheduling"),
    "transport_requeued": ("E12", "重新排队（运输重投）", "scheduling"),
    "retry_accepted": ("E13", "重试 / 失败补跑已受理", "scheduling"),
    "retry_skipped": ("E14", "重试 / 失败补跑被跳过", "scheduling"),
}
FALLBACK_MAP = {
    "created_at": ("F01", "执行已登记"),
    "accepted_at": ("F02", "SWE 已受理"),
    "started_at": ("F03", "开始执行"),
    "finished_at": ("F04", "主执行结束记录"),
    "settled_at": ("F05", "Scheduler 已确认整轮结果"),
}
EVENT_DETAIL_FIELDS = (
    "status error_code failure_type definition_version settled_status settled_error_code "
    "business_attempt previous_execution_id reason call_id root_execution_id retry_count dispatch_lane"
)


class TrackingExecution:
    def __init__(self, reader, identity):
        self.reader = reader
        self.identity = identity
        self.runs = {}
        self.histories = {}
        self.receipts = {}
        self.calls = {}

    def reference(self, value):
        if value is None or value == "":
            return False
        if not isinstance(value, str) or len(value) > 128:
            self.reader.warn("reference_identity_unavailable")
            return False
        return True

    async def run(self, execution_id):
        if not execution_id:
            return None
        if execution_id not in self.runs:
            rows = await self.reader.rows(
                RUNS,
                f"SELECT * FROM {RUNS} WHERE execution_id=%s AND {where_identity()} AND {new_protocol()} LIMIT 1",
                (execution_id, *self.identity.values()),
            )
            self.runs[execution_id] = rows[0] if rows else None
        return self.runs[execution_id]

    async def history(self, run):
        if not run or not run.get("history_execution_id"):
            return None
        key = (run["execution_id"], run["history_execution_id"])
        if key not in self.histories:
            rows = await self.reader.rows(
                HISTORY,
                f"SELECT * FROM {HISTORY} WHERE id=%s AND tenant_id COLLATE utf8mb4_bin=%s "
                "AND job_id COLLATE utf8mb4_bin=%s LIMIT 1",
                (key[1], self.identity.tenant_id, self.identity.job_id),
            )
            row = rows[0] if rows else None
            meta = json_object(row.get("meta")) if row else {}
            expected = {
                "management_owner": "scheduler",
                "managed_execution_id": run["execution_id"],
                "source_id": self.identity.source_id,
                "agent_id": self.identity.agent_id,
            }
            valid = row is not None and all(
                meta.get(name) == value for name, value in expected.items()
            )
            self.histories[key] = row if valid else None
            if not valid:
                self.reader.warn("history_identity_unconfirmed")
        return self.histories[key]

    async def receipt(self, receipt_id):
        if not self.reference(receipt_id):
            return None
        if receipt_id not in self.receipts:
            rows = await self.reader.rows(
                RECEIPTS,
                f"SELECT * FROM {RECEIPTS} WHERE receipt_id=%s AND source_id COLLATE utf8mb4_bin=%s LIMIT 1",
                (receipt_id, self.identity.source_id),
            )
            self.receipts[receipt_id] = rows[0] if rows else None
        return self.receipts[receipt_id]

    async def entry(self, run, details):
        meta = json_object(run.get("dispatch_meta_json")) if run else {}
        if isinstance(meta.get("daily_failure_retry"), dict):
            return "daily_failure_retry"
        if details.get("call_id"):
            if await self.retry_call(details["call_id"]):
                return "daily_failure_retry"
        if meta.get("previous_execution_id") or str(
            details.get("reason", ""),
        ).startswith("cron_retry_"):
            return "manual_retry"
        if not run:
            return "unknown"
        if run["kind"] == "manual":
            return "manual"
        receipt = await self.receipt(meta.get("trigger_receipt_id"))
        if receipt and (
            same_identity(receipt, self.identity)
            or manifest_member(receipt, self.identity, run["execution_id"])
        ):
            return (
                receipt["kind"]
                if receipt["kind"] in {"ordinary", "batch"}
                else "unknown"
            )
        return "unknown"

    async def retry_call(self, call_id):
        if not self.reference(call_id):
            return None
        if call_id not in self.calls:
            rows = await self.reader.rows(
                CALLS,
                f"SELECT call_id,business_date,window_start,cutoff_at,status,error_code "
                f"FROM {CALLS} WHERE call_id=%s AND source_id COLLATE utf8mb4_bin=%s LIMIT 1",
                (call_id, self.identity.source_id),
            )
            self.calls[call_id] = rows[0] if rows else None
        return self.calls[call_id]

    async def retry_context(self, details, run):
        meta = json_object(run.get("dispatch_meta_json")) if run else {}
        marker = json_object(meta.get("daily_failure_retry"))
        call = await self.retry_call(
            details.get("call_id") or marker.get("call_id"),
        )
        if call is None:
            return None
        return {
            **select_fields(call, "call_id business_date status error_code"),
            "window_start": iso_time(call.get("window_start")),
            "cutoff_at": iso_time(call.get("cutoff_at")),
        }

    async def detail(self, run):
        if run is None:
            return {"execution_record": "missing"}
        result = select_fields(
            run,
            "kind definition_version provider_id model_id trace_id output_preview error_code settled_status settled_error_code",
        )
        result["execution_snapshot"] = select_fields(
            json_object(run.get("spec_json")),
            "name task_type text runtime",
        )
        for name in (
            "scheduled_fire_at",
            "not_before_at",
            "accepted_at",
            "started_at",
            "finished_at",
            "settled_at",
        ):
            result[name] = iso_time(run.get(name))
        meta = json_object(run.get("dispatch_meta_json"))
        result.update(
            select_fields(meta, "business_attempt previous_execution_id"),
        )
        outcome = json_object(run.get("result_json"))
        result.update(select_fields(outcome, "error_detail failure_type"))
        history = await self.history(run)
        if history:
            result.update(
                select_fields(
                    history,
                    "status async_status duration_ms output_preview error_message need_notification notification_status notification_timezone notification_error is_read is_final",
                ),
            )
            result["notification_failure_count"] = (
                history.get("notification_attempts") or 0
            )
            for name in (
                "notification_due_at",
                "notification_sent_at",
                "read_at",
            ):
                result[name] = iso_time(history.get(name), BEIJING)
            result["history_id"] = history["id"]
        elif run.get("history_execution_id"):
            result["history_association"] = "unconfirmed"
        return result

    async def event_node(self, row, source, *, entry_filter="all"):
        kind = row.get("event_type")
        if kind == "retry_request":
            return None
        selected = EVENT_MAP.get(
            kind,
            (
                "execution_event",
                "执行事件（" + str(kind)[:128] + "）",
                "execution",
            ),
        )
        node = make_node(
            row,
            *selected,
            source,
            basis=(
                "scheduler_confirmed"
                if kind == "settled"
                else "event_recorded"
            ),
        )
        details = self.event_details(row.get("details_json"))
        run = await self.run(row.get("execution_id"))
        node["entry"] = await self.entry(run, details)
        if entry_filter not in {"all", node["entry"]}:
            return None
        node["original_entry"] = node["entry"]
        node["recorded_outcome"] = (
            details.get("settled_status")
            if kind == "settled"
            else details.get("status")
        )
        node["current_status"] = run.get("status") if run else None
        node["details"] = {
            "recorded": select_fields(details, EVENT_DETAIL_FIELDS),
            **await self.detail(run),
        }
        node["history_id"] = node["details"].get("history_id")
        node["actor_id"] = row.get("actor_id")
        node["actor_kind"] = (
            "operator"
            if node["entry"] == "manual_retry"
            and kind in {"retry_accepted", "retry_skipped"}
            else "execution_context"
        )
        if node["entry"] == "daily_failure_retry":
            node["details"]["retry_initiator"] = "unrecorded"
            node["details"]["daily_retry_context"] = await self.retry_context(
                details,
                run,
            )
        if not run:
            node["gaps"].append("execution_record_missing")
        if node["node_type"] == "E01" and node["entry"] == "manual":
            node["title"] = "手动执行已受理并入队"
        recorded_reason(
            node,
            details.get("reason")
            or details.get("error_code")
            or details.get("settled_error_code"),
        )
        if kind == "settled":
            self.check_confirmation(node, row, run)
        return node

    def event_details(self, value):
        raw = self.reader.object(value, "event_details_unavailable")
        numeric = {"definition_version", "business_attempt", "retry_count"}
        selected = {}
        for name in EVENT_DETAIL_FIELDS.split():
            item = raw.get(name)
            if item is None:
                selected[name] = None
            elif name in numeric and type(item) is int and item >= 0:
                selected[name] = item
            elif name not in numeric and isinstance(item, str):
                selected[name] = item
            else:
                self.reader.warn("event_details_unavailable")
        return selected

    def check_confirmation(self, node, event, run):
        if (
            run
            and run.get("settled_at")
            and iso_time(event.get("occurred_at"))
            != iso_time(run["settled_at"])
        ):
            node["gaps"].append("settlement_event_mismatch")
            self.reader.warn("settlement_event_mismatch")

    async def fallback_node(self, row, source, *, entry_filter="all"):
        kind, title = FALLBACK_MAP[source.fact]
        node = make_node(
            row,
            kind,
            title,
            "scheduling" if kind == "F01" else "execution",
            source,
            basis=(
                "scheduler_confirmed"
                if kind == "F05"
                else "execution_timestamp"
            ),
        )
        current = await self.run(row["execution_id"])
        snapshot = current or row
        node["entry"] = await self.entry(snapshot, {})
        if entry_filter not in {"all", node["entry"]}:
            return None
        node["recorded_outcome"] = (
            row.get("settled_status")
            if kind == "F05"
            else row.get("status") if kind == "F04" else None
        )
        node["current_status"] = snapshot.get("status")
        node["actor_id"] = snapshot.get("actor_id")
        node["actor_kind"] = "execution_context"
        node["details"] = await self.detail(current)
        node["history_id"] = node["details"].get("history_id")
        node["evidence"] = "recorded_timestamp"
        return node
