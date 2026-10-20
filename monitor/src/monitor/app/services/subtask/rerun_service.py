# -*- coding: utf-8 -*-
"""Rerun cron jobs whose latest execution failed asynchronously."""

import asyncio
import logging
from time import perf_counter
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
        self._background_tasks: set[asyncio.Task] = set()

    async def rerun_failed_executions(
        self,
        tenant_ids: list[str],
    ) -> FailedExecutionRerunResponse:
        """Rerun jobs whose latest execution today is an async failure."""
        total_started_at = perf_counter()
        if not tenant_ids:
            return FailedExecutionRerunResponse()
        if not self.db:
            raise RuntimeError("Database not connected")
        if not self._authorization:
            raise RuntimeError(
                "MONITOR_SWE_CRON_RERUN_AUTHORIZATION is not configured",
            )

        query_started_at = perf_counter()
        jobs = await self._fetch_failed_jobs(tenant_ids)
        query_elapsed_ms = (perf_counter() - query_started_at) * 1000
        logger.info(
            "Fetched async-failed cron jobs: tenant_count=%d job_count=%d "
            "elapsed_ms=%.2f",
            len(tenant_ids),
            len(jobs),
            query_elapsed_ms,
        )

        if jobs:
            task = asyncio.create_task(self._rerun_jobs(jobs))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        logger.info(
            "Submitted async-failed cron rerun: tenant_count=%d job_count=%d "
            "elapsed_ms=%.2f",
            len(tenant_ids),
            len(jobs),
            (perf_counter() - total_started_at) * 1000,
        )
        return FailedExecutionRerunResponse(rerun_count=len(jobs))

    async def _rerun_jobs(self, jobs: list[dict]) -> None:
        """Call the gateway sequentially in a background task."""
        batch_started_at = perf_counter()
        client_started_at = perf_counter()
        try:
            async with self._client_factory(timeout=self._timeout) as client:
                logger.info(
                    "Initialized cron rerun HTTP client: elapsed_ms=%.2f",
                    (perf_counter() - client_started_at) * 1000,
                )
                for job in jobs:
                    await self._rerun_job(
                        client,
                        job["job_id"],
                        job["tenant_id"],
                    )
            logger.info(
                "Completed background cron rerun batch: job_count=%d elapsed_ms=%.2f",
                len(jobs),
                (perf_counter() - batch_started_at) * 1000,
            )
        except Exception:
            logger.exception(
                "Background cron rerun batch failed: job_count=%d elapsed_ms=%.2f",
                len(jobs),
                (perf_counter() - batch_started_at) * 1000,
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
        request_started_at = perf_counter()
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
            started = (
                isinstance(payload, dict) and payload.get("started") is True
            )
            logger.info(
                "Completed cron job rerun request: job_id=%s status_code=%d "
                "started=%s elapsed_ms=%.2f",
                job_id,
                response.status_code,
                started,
                (perf_counter() - request_started_at) * 1000,
            )
            return started
        except (httpx.HTTPError, ValueError) as exc:
            logger.error(
                "Failed to rerun cron job: job_id=%s elapsed_ms=%.2f error=%s",
                job_id,
                (perf_counter() - request_started_at) * 1000,
                exc,
            )
            return False


_rerun_service: RerunService | None = None


def get_rerun_service() -> RerunService:
    """Get the global rerun service."""
    global _rerun_service
    if _rerun_service is None:
        _rerun_service = RerunService(db=get_db_connection())
    return _rerun_service
