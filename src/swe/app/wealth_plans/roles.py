# -*- coding: utf-8 -*-
"""财富工作台的服务端角色解析与数据可见范围。

岗位编号 → 角色映射必须与前端 console/src/pages/WealthWorkbench/permissions.ts
的 POSITION_ROLE_MAP 保持一致；未识别岗位按最少权限处理（同客户经理口径）。

规划可见范围先按 bbk_id 隔离，再按角色收窄：
- 客户经理及未知角色：自己创建的规划 + 分发给自己的规划；
- 支行行长：本行分行关注、自己创建的行长关注规划；分行中台：本行分行关注规划。
"""

from __future__ import annotations

from .models import SOURCE_LABEL_BY_ROLE

ROLE_RM = "rm"
ROLE_PRESIDENT = "president"
ROLE_MIDDLE = "middle"
ROLE_UNKNOWN = "unknown"

POSITION_ROLE_MAP: dict[str, str] = {
    "RB0101": ROLE_RM,
    "RB1101": ROLE_PRESIDENT,
    "RB0306": ROLE_PRESIDENT,
    "RB0108": ROLE_PRESIDENT,  # 理财主管岗（综合），权限等同支行行长
    "RB0301": ROLE_MIDDLE,
    "RB0305": ROLE_MIDDLE,
}

BRANCH_WIDE_ROLES = (ROLE_PRESIDENT, ROLE_MIDDLE)

_VISIBLE_SOURCE_LABELS_BY_ROLE = {
    ROLE_PRESIDENT: (
        SOURCE_LABEL_BY_ROLE[ROLE_MIDDLE],
        SOURCE_LABEL_BY_ROLE[ROLE_PRESIDENT],
    ),
    ROLE_MIDDLE: (SOURCE_LABEL_BY_ROLE[ROLE_MIDDLE],),
}


def get_visible_source_labels(role: str) -> tuple[str, ...] | None:
    """管理角色按来源收窄；其余角色沿用创建/分发关系判断。"""
    return _VISIBLE_SOURCE_LABELS_BY_ROLE.get(role)


def resolve_role(position_id: str | None) -> str:
    """按岗位编号解析角色；缺失或未命中返回 unknown（最少权限）。"""
    if not position_id:
        return ROLE_UNKNOWN
    return POSITION_ROLE_MAP.get(position_id, ROLE_UNKNOWN)


def can_view_branch_wide(role: str) -> bool:
    """该角色是否具有本行管理范围（仍需按来源过滤）。"""
    return role in BRANCH_WIDE_ROLES
