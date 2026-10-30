# -*- coding: utf-8 -*-
"""操作单是请求及当前结果，不是完整的中间状态变更日志。"""

from .tracking_common import iso_time, json_object, select_fields
from .tracking_nodes import make_node, recorded_reason

ACTIONS = {
    "create": ("M01", "提交创建任务"),
    "update": ("M02", "提交修改任务"),
    "auto_pause": ("M05", "提交自动暂停同步"),
    "delete": ("M06", "提交删除任务"),
    "dispatch_state": ("M07", "提交调度方式切换"),
    "set_priority": ("M09", "提交优先级调整"),
    "mark_read": ("M10", "提交标读操作"),
    "pause_group": ("M13", "提交暂停任务及全部子任务"),
    "date_range": ("M14", "提交修改可执行时间区间"),
}


def journal_node(row, source):
    payload = json_object(row.get("payload_json"))
    action = row.get("action")
    if action == "batch_delete":
        return None
    kind, title = ACTIONS.get(action, ("management_record", "管理操作记录"))
    if action == "set_enabled":
        enabled = payload.get("enabled")
        kind, title = (
            ("M03", "提交启用 / 恢复")
            if enabled is True
            else ("M04", "提交停用 / 暂停")
        )
        if type(enabled) is not bool:
            kind, title = "management_record", "启停请求（目标状态未记录）"
    node = management_node(row, source, kind, title)
    node["details"].update(
        select_fields(
            payload,
            "enabled state auto_paused_at source_execution_id auto_pause_epoch run_date_range expected_management_version",
        ),
    )
    node["details"]["expected_version"] = row.get("expected_version")
    node["details"]["submitted_spec"] = select_fields(
        json_object(payload.get("spec")),
        "name task_type text schedule runtime",
    )
    if action == "auto_pause":
        node["actor_kind"] = "system"
    if action == "mark_read":
        node["details"]["read_watermark"] = json_object(
            row.get("progress_json"),
        ).get("read_watermark")
    recorded_reason(node, action, derived=True)
    return node


def management_node(row, source, kind, title):
    node = make_node(
        row,
        kind,
        title,
        "management",
        source,
        basis="operation_submitted",
    )
    node["entry"] = "management"
    node["actor_id"] = row.get("actor_id")
    node["actor_kind"] = "operator"
    node["current_status"] = row.get("status")
    node["details"] = {
        **select_fields(
            row,
            "action stage error_code request_id sync_status sync_operation_id",
        ),
        "current_recorded_status": row.get("status"),
        "last_updated_at": iso_time(row.get("updated_at")),
    }
    return node


def auxiliary_node(row, source):
    kinds = {
        "mode": ("M08", "整组切换涉及本任务"),
        "broadcast": ("M11", "本任务发起分发"),
        "broadcast_item": ("M12", "本任务的分发处理记录"),
        "delete": ("F06", "逻辑删除标记记录"),
    }
    node = management_node(row, source, *kinds[source.fact])
    if source.fact == "mode":
        node["current_status"] = row.get("item_status")
        node["details"]["current_recorded_status"] = row.get("item_status")
        node["details"]["parent_operation_status"] = row.get("status")
        node["details"]["state"] = json_object(row.get("payload_json")).get(
            "state",
        )
        node["details"]["item_error_code"] = row.get("item_error_code")
        node["details"]["item_status"] = row.get("item_status")
    if source.fact == "broadcast":
        node["details"]["source_definition_version"] = row.get(
            "source_definition_version",
        )
    if source.fact == "broadcast_item":
        node["details"].update(
            select_fields(
                row,
                "operation warning expected_definition_version",
            ),
        )
        node["details"]["result"] = select_fields(
            json_object(row.get("result_json")),
            "operation_id status error_code version",
        )
    if source.fact == "delete":
        node["evidence"] = "recorded_timestamp"
        node["actor_kind"] = "unrecorded"
        node["actor_id"] = None
        node["time_basis"] = "definition_deleted"
        node["current_status"] = "logically_deleted"
    return node
