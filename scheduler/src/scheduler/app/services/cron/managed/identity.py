# -*- coding: utf-8 -*-
"""解析可信网关身份，不凭裸任务 ID 推断权限。"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import unquote


def identity_value(value: str) -> str:
    """沿用 SWE 身份安全规则，并校验数据库字段长度。"""
    if not value or value != value.strip() or len(value) > 128:
        raise ValueError("invalid_cron_identity")
    if any(part in value for part in ("..", "/", "\\")):
        raise ValueError("invalid_cron_identity")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("invalid_cron_identity")
    return value


def encode_scope(tenant: str, source: str) -> str:
    """使用与 SWE 一致的标准编码，不依赖 SWE 包。"""
    return ".".join(
        base64.urlsafe_b64encode(value.encode()).decode("ascii").rstrip("=")
        for value in (identity_value(tenant), identity_value(source))
    )


@dataclass(frozen=True)
class CallerIdentity:
    tenant_id: str
    source_id: str
    agent_id: str
    actor_id: str
    is_manager: bool = False
    bbk_id: str | None = None
    user_name: str | None = None

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> CallerIdentity:
        actor = identity_value(headers.get("x-user-id", ""))
        tenant = identity_value(headers.get("x-tenant-id", actor))
        source = identity_value(headers.get("x-source-id", ""))
        agent = identity_value(headers.get("x-agent-id", "default"))
        scope = encode_scope(tenant, source)
        provided = headers.get("x-scope-id")
        # Console 仍会传展示用 scope；权限依据明确的租户、来源计算。
        # 不反向拆分展示字符串，避免其中连字符导致身份歧义。
        accepted = {scope, f"scope.v1.{scope}", f"{tenant}-{source}"}
        if provided is not None and provided not in accepted:
            raise ValueError("conflicting_cron_scope")
        role = headers.get("x-user-role", "").strip().lower()
        # 角色名称沿用 SWE 来源配置接口的约定。
        # 这些身份 Header 必须经过网关认证和约束，不能信任客户端自报。
        bbk = headers.get("x-bbk-id") or None
        if bbk:
            identity_value(bbk)
        name = unquote(headers.get("x-user-name", "")).strip() or None
        if name and (len(name) > 255 or any(ord(char) < 32 for char in name)):
            raise ValueError("invalid_cron_display_identity")
        return cls(
            tenant,
            source,
            agent,
            actor,
            role in {"manager", "admin"},
            bbk,
            name,
        )

    def job_key(self, job_id: str) -> tuple[str, str, str, str]:
        return (
            self.tenant_id,
            self.source_id,
            self.agent_id,
            identity_value(job_id),
        )
