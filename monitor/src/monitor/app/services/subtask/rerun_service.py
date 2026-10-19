# -*- coding: utf-8 -*-
"""Rerun cron jobs whose latest execution failed asynchronously."""

import logging
from uuid import uuid4

import httpx

from ....config.constant import (
    API_CALL_TIMEOUT,
    SWE_CRON_RERUN_AUTHORIZATION,
    SWE_CRON_RERUN_BASE_URL,
)
from ...database.connection import get_db_connection
from ...models.subtask import FailedExecutionRerunResponse

logger = logging.getLogger(__name__)


class RerunService:
    """Query today's latest failed executions and trigger manual reruns."""

    def __init__(
        self,
        db=None,
        *,
        base_url: str = SWE_CRON_RERUN_BASE_URL,
        authorization: str = SWE_CRON_RERUN_AUTHORIZATION,
        timeout: float = API_CALL_TIMEOUT,
        client_factory=httpx.AsyncClient,
    ) -> None:
        self.db = db
        self._base_url = base_url.rstrip("/")
        self._authorization = authorization
        self._timeout = timeout
        self._client_factory = client_factory

    async def rerun_failed_executions(
        self,
        tenant_ids: list[str],
    ) -> FailedExecutionRerunResponse:
        """Rerun jobs whose latest execution today is an async failure."""
        if not tenant_ids:
            return FailedExecutionRerunResponse()
        if not self.db:
            raise RuntimeError("Database not connected")
        if not self._authorization:
            raise RuntimeError(
                "MONITOR_SWE_CRON_RERUN_AUTHORIZATION is not configured",
            )

        jobs = await self._fetch_failed_jobs(tenant_ids)
        rerun_count, failed_count = 0, 0
        async with self._client_factory(timeout=self._timeout) as client:
            for job in jobs:
                if await self._rerun_job(
                    client,
                    job["job_id"],
                    job["tenant_id"],
                ):
                    rerun_count += 1
                else:
                    failed_count += 1
        return FailedExecutionRerunResponse(
            rerun_count=rerun_count,
            failed_count=failed_count,
        )

    async def _fetch_failed_jobs(self, tenant_ids: list[str]) -> list[dict]:
        placeholders = ", ".join(["%s"] * len(tenant_ids))
        query = f"""
            SELECT job_id, tenant_id
            FROM (
                SELECT
                    scj.id AS job_id,
                    scj.tenant_id,
                    sce.status,
                    sce.async_status,
                    ROW_NUMBER() OVER (
                        PARTITION BY scj.id ORDER BY sce.actual_time DESC
                    ) AS rn
                FROM swe_cron_jobs scj
                INNER JOIN swe_cron_executions sce ON scj.id = sce.job_id
                WHERE scj.tenant_id IN ({placeholders})
                  AND sce.actual_time >= CURDATE()
            ) latest
            WHERE rn = 1
              AND status = 'success'
              AND async_status = 'error'
        """
        return await self.db.fetch_all(query, tuple(tenant_ids))

    async def _rerun_job(
        self,
        client: httpx.AsyncClient,
        job_id: str,
        tenant_id: str,
    ) -> bool:
        headers = {
            "Content-Type": "application/json",
            "X-User-Id": tenant_id,
            "X-Tenant-Id": tenant_id,
            "X-Source-Id": "RMASSIST",
            "X-Agent-Id": "default",
            "X-Auth-Authorization": self._authorization,
        }
        try:
            response = await client.post(
                f"{self._base_url}/{job_id}/run",
                headers=headers,
                json={"request_id": str(uuid4())},
            )
            response.raise_for_status()
            payload = response.json()
            return isinstance(payload, dict) and payload.get("started") is True
        except (httpx.HTTPError, ValueError) as exc:
            logger.error("Failed to rerun cron job %s: %s", job_id, exc)
            return False


_rerun_service: RerunService | None = None


def get_rerun_service() -> RerunService:
    """Get the global rerun service."""
    global _rerun_service
    if _rerun_service is None:
        _rerun_service = RerunService(db=get_db_connection())
    return _rerun_service
