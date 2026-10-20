# -*- coding: utf-8 -*-
"""触发 SWE 资源准备；SWE 从共用操作日志核对任务和权限。"""

from urllib.parse import quote, urlsplit
import logging

import httpx

from .identity import encode_scope

logger = logging.getLogger(__name__)


def checked_base(value):
    value = value.rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("cron_service_url_not_configured")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid_cron_service_url")
    return value


class ManagedSweClient:
    def __init__(self, base_url, *, http=None, headers=None):
        self.base_url = checked_base(base_url).removesuffix("/api")
        self.http = http
        self.headers = {
            key: value
            for key, value in (headers or {}).items()
            if key.lower()
            in {
                "authorization",
                "x-header-cookie",
                "b3",
                "x-b3-traceid",
                "x-b3-spanid",
            }
        }

    async def prepare(self, operation, caller):
        return await self._call("prepare", operation, caller)

    async def cleanup(self, operation, caller):
        return await self._call("cleanup", operation, caller)

    async def _call(self, action, operation, caller):
        logger.info(
            "调用 SWE 执行资源接口 action=%s operation_id=%s job_id=%s",
            action,
            operation["operation_id"],
            operation["job_id"],
        )
        headers = {
            **self.headers,
            "X-User-Id": caller.actor_id,
            "X-Tenant-Id": caller.tenant_id,
            "X-Source-Id": caller.source_id,
            "X-Agent-Id": caller.agent_id,
            "X-Scope-Id": encode_scope(caller.tenant_id, caller.source_id),
        }
        if caller.is_manager:
            headers["X-User-Role"] = "manager"
        if caller.bbk_id:
            headers["X-Bbk-Id"] = caller.bbk_id
        if caller.user_name:
            headers["X-User-Name"] = quote(caller.user_name, safe="")
        body = {
            "operation_id": operation["operation_id"],
            "job_id": operation["job_id"],
        }
        url = f"{self.base_url}/api/internal/cron/managed/{action}"
        if self.http is not None:
            response = await self.http.post(url, headers=headers, json=body)
        else:
            async with httpx.AsyncClient(
                timeout=30,
                follow_redirects=False,
            ) as client:
                response = await client.post(url, headers=headers, json=body)
        logger.info(
            "SWE 执行资源接口已响应 action=%s operation_id=%s job_id=%s http_status=%s",
            action,
            operation["operation_id"],
            operation["job_id"],
            response.status_code,
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("invalid_swe_resource_response")
        return value
