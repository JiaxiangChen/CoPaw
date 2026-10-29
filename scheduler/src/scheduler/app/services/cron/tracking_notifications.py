# -*- coding: utf-8 -*-
"""只将有持久时间的发送/标读事实加入时间轴。"""

from .tracking_nodes import make_node


async def history_node(row, source, execution):
    run = await execution.run(row["linked_execution_id"])
    history = await execution.history(run)
    if not history or history["id"] != row["id"]:
        return None
    mapping = {
        "notification": (
            "N01",
            "通知标记为已发送",
            "notification",
            "notification_marked",
        ),
        "read": ("N02", "结果已标读", "notification", "read_marked"),
        "history_end": (
            "FH01",
            "执行历史结束记录",
            "execution",
            "history_timestamp",
        ),
    }
    kind, title, category, basis = mapping[source.fact]
    node = make_node(row, kind, title, category, source, basis=basis)
    node["execution_id"] = run["execution_id"]
    node["history_id"] = history["id"]
    node["entry"] = await execution.entry(run, {})
    node["recorded_outcome"] = (
        "sent"
        if kind == "N01"
        else "read" if kind == "N02" else history.get("status")
    )
    node["details"] = await execution.detail(run)
    node["evidence"] = "recorded_timestamp"
    return node
