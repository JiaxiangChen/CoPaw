# -*- coding: utf-8 -*-
"""复用现有调度平台协议，将注册与启用分开处理。"""

import base64
import json
import logging
import os

import httpx
from croniter import croniter

from .operations import RegistrationRejected, RegistrationUnknown
from .swe_client import checked_base

logger = logging.getLogger(__name__)


def platform_cron(expression):
    parts = expression.split()
    if len(parts) != 5 or not croniter.is_valid(expression):
        raise ValueError("invalid_cron_schedule")
    expanded, _ = croniter.expand(expression)
    weekdays = expanded[4]
    if weekdays == ["*"]:
        return "0 " + " ".join((*parts[:4], "?"))
    days = ",".join(str(int(day) + 1) for day in weekdays)
    return f"0 {parts[0]} {parts[1]} ? {parts[3]} {days}"


def platform_config():
    prefix = "SWE_CRON_SCHEDULER_"
    result = {
        key.lower(): os.environ.get(prefix + key, "").strip()
        for key in (
            "BASE_URL",
            "AUTHOR",
            "ALARM_EMAIL",
            "CLIENT_NO",
            "CLIENT_KEY",
            "CLIENT_REMARK",
        )
    }
    # 既有 SWE 默认文件会提供空字符串，沿用原适配器的空值为 0 规则。
    result["job_group"] = int(
        os.environ.get(prefix + "JOB_GROUP", "").strip() or "0",
    )
    result["callback_base"] = os.environ.get("SWE_SERVER_DOMAIN", "").strip()
    return result


class ManagedPlatformClient:
    def __init__(self, config, *, http=None):
        self.config = dict(config)
        self.base_url = checked_base(config["base_url"])
        self.callback_base = checked_base(config["callback_base"])
        self.http = http

    async def register(self, operation, spec, mode):
        logger.info(
            "开始注册外部定时器 operation_id=%s job_id=%s mode=%s",
            operation["operation_id"],
            operation["job_id"],
            mode,
        )
        payload = self._payload(operation, spec, mode)
        try:
            result = await self._post("add-job", payload)
            external_id = str(result.get("content") or "")
            if not external_id or not external_id.isdigit():
                raise RegistrationUnknown()
            logger.info(
                "外部定时器注册完成 operation_id=%s job_id=%s external_id=%s",
                operation["operation_id"],
                operation["job_id"],
                external_id,
            )
            return external_id
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {400, 401, 403, 404, 405, 422}:
                raise RegistrationRejected() from exc
            raise RegistrationUnknown() from exc
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise RegistrationUnknown() from exc

    async def update(self, external_id, operation, spec, mode):
        payload = self._payload(operation, spec, mode)
        payload["id"] = int(external_id)
        await self._post("update-job", payload)

    async def set_enabled(self, external_id, enabled):
        await self._post(
            "update-job-run-states",
            {
                **self._credentials(),
                "id": int(external_id),
                "runFlag": 1 if enabled else 0,
            },
        )

    async def stop(self, external_id, operation):
        payload = json.loads(operation["payload_json"])
        spec = dict(payload["spec"])
        spec["name"] = "[已删除] " + spec["name"]
        await self.update(
            external_id,
            operation,
            spec,
            payload["dispatch_state"],
        )
        await self.set_enabled(external_id, False)

    def _credentials(self):
        return {
            "clientNo": self.config.get("client_no", ""),
            "clientKey": self.config.get("client_key", ""),
            "clientRemark": self.config.get("client_remark", ""),
        }

    def _payload(self, operation, spec, mode):
        callback = "ordinary" if mode == "ordinary" else "batch"
        prefix = (
            "[Scheduler]" if callback == "ordinary" else "[Scheduler][批调度]"
        )
        identity = {
            key: operation[key]
            for key in ("tenant_id", "source_id", "agent_id", "job_id")
        }
        params = {
            **identity,
            "task_type": "job",
            "scopeId": spec["scope_id"],
            "fromId": operation["actor_id"],
            "callback_protocol": "scheduler_managed_v1",
            "operation_id": operation["operation_id"],
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(params).encode(),
        ).decode()
        return {
            **self._credentials(),
            "jobGroup": self.config.get("job_group", 0),
            "author": self.config.get("author", ""),
            "alarmEmail": self.config.get("alarm_email", ""),
            "glueType": "GLUE_GROOVY",
            "glueRemark": spec["id"],
            "jobDesc": f"{prefix} {operation['tenant_id']}/{operation['source_id']} - {spec['name']}"[
                :200
            ],
            "jobCron": platform_cron(spec["schedule"]["cron"]),
            "jobAddress": f"{self.callback_base}/api/scheduler/cron/{callback}/callback",
            "jobParam": encoded,
        }

    async def _post(self, action, payload):
        logger.info("调用外部调度平台 action=%s", action)
        url = f"{self.base_url}/job-admin/v2/{action}"
        if self.http is not None:
            response = await self.http.post(url, json=payload)
        else:
            async with httpx.AsyncClient(
                timeout=30,
                follow_redirects=False,
            ) as client:
                response = await client.post(url, json=payload)
        logger.info(
            "外部调度平台已响应 action=%s http_status=%s",
            action,
            response.status_code,
        )
        response.raise_for_status()
        result = response.json()
        if isinstance(result, dict) and result.get("code") in {
            400,
            401,
            403,
            404,
            405,
            422,
        }:
            raise RegistrationRejected()
        if not isinstance(result, dict) or result.get("code") != 200:
            raise ValueError("cron_platform_request_failed")
        return result
