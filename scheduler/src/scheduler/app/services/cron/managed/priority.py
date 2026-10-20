# -*- coding: utf-8 -*-
"""复用用户优先、分行其次的既有排序规则，不引入新算法。"""

from .identity import identity_value
from ..batch_priority import priority_key, priority_snapshot

__all__ = ["normalize_priority", "priority_key", "priority_snapshot"]

# 一级分行编码与原 SWE 优先级编辑器保持一致，并由测试核对两边编码。
# Scheduler 独立部署，不能为校验配置而导入 SWE 运行时。
PRIMARY_BRANCHES = frozenset(
    """
100 110 120 121 122 123 124 125 127 128 129 311 315 351 371
411 431 451 471 510 512 513 531 532 535 551 571 574 577 591
592 595 731 755 757 769 771 791 851 871 898 931 951 972 991
""".split(),
)


def _ids(raw, maximum):
    if not isinstance(raw, list) or len(raw) > maximum:
        raise ValueError("invalid_cron_priority")
    if any(not isinstance(value, str) for value in raw):
        raise ValueError("invalid_cron_priority")
    values = [identity_value(value.strip()) for value in raw]
    if len(set(values)) != len(values):
        raise ValueError("duplicate_cron_priority_identity")
    return values


def normalize_priority(raw):
    users = _ids(raw.get("user_ids", []), 500)
    branches = _ids(raw.get("branch_ids", []), 100)
    if any(value not in PRIMARY_BRANCHES for value in branches):
        raise ValueError("invalid_primary_branch")
    return {"user_ids": users, "branch_ids": branches}
