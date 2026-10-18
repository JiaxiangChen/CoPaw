# -*- coding: utf-8 -*-
"""共用运行占位定义，不将主执行完成误当作异步链路已结束。"""


def occupied_run_sql(alias=""):
    prefix = f"{alias}." if alias else ""
    return (
        f"({prefix}status IN ('dispatching','accepted','running') OR "
        f"({prefix}status='success' AND {prefix}settled_status IS NULL))"
    )
