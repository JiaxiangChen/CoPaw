# -*- coding: utf-8 -*-
"""Subtask sync service for external API calls and status updates.

Provides methods for:
- Syncing subtask status from external API
- Computing execution async_status from subtask statuses
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional, Tuple

import httpx

from ....config.constant import (
    ASYNC_TASK_QUERY_URL,
    ASYNC_TASK_APP_KEY,
    ASYNC_TASK_ENV_TAG,
    ASYNC_TASK_API_KEY,
    RESULT_INDEX_PUSH_PLUGIN_ID,
    RESULT_INDEX_PUSH_PLUGIN_NAME,
    RESULT_INDEX_PUSH_QUESTION,
    RESULT_INDEX_PUSH_URL,
)
from .query_service import QueryService, get_query_service
from ...models.subtask import (
    SubtaskModel,
    SubtaskSyncStatusResponse,
    SubtaskSyncDetailItem,
    ExecutionAsyncStatusResponse,
)

logger = logging.getLogger(__name__)

# 外部 API 超时
API_TIMEOUT = 10.0

RESULT_INDEX_PUSH_TIMEOUT = 10.0

# 每批处理数量
BATCH_SIZE = 100

# 有效状态值
VALID_SUBTASK_STATUSES = ("SUC", "FAIL", "PART_SUC")

# 兜底超时小时数
FALLBACK_TIMEOUT_HOURS = 2


class SyncService:
    """Service for syncing subtask status from external API."""

    def __init__(self, query_service: Optional[QueryService] = None):
        """Initialize sync service.

        Args:
            query_service: Query service for database operations
        """
        self.query_service = query_service or get_query_service()
        self._client: Optional[httpx.AsyncClient] = None

    def _is_configured(self) -> bool:
        """Check if external API is configured."""
        return bool(
            ASYNC_TASK_QUERY_URL
            and ASYNC_TASK_APP_KEY
            and ASYNC_TASK_ENV_TAG
            and ASYNC_TASK_API_KEY,
        )

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=API_TIMEOUT)
        return self._client

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _is_result_index_push_configured(self) -> bool:
        """Check if result-index push API is configured."""
        return bool(
            RESULT_INDEX_PUSH_URL
            and RESULT_INDEX_PUSH_PLUGIN_ID
            and RESULT_INDEX_PUSH_PLUGIN_NAME
            and RESULT_INDEX_PUSH_QUESTION,
        )

    @staticmethod
    def _dedupe_result_index_users(
        users: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """Deduplicate result-index users by custUid and bbkId."""
        deduped = []
        seen = set()
        for user in users:
            cust_uid = (user.get("custUid") or "").strip()
            bbk_id = (user.get("bbkId") or "").strip()
            if not cust_uid or not bbk_id:
                continue
            key = (cust_uid, bbk_id)
            if key in seen:
                continue
            seen.add(key)
            deduped.append({"custUid": cust_uid, "bbkId": bbk_id})
        return deduped

    async def _push_result_index_users(
        self,
        users: list[dict[str, str]],
    ) -> None:
        """Push successful result-index users to third party."""
        user_info_list = self._dedupe_result_index_users(users)
        if not user_info_list:
            return
        if not self._is_result_index_push_configured():
            logger.info(
                "Result-index push API not configured, skipped: users=%d",
                len(user_info_list),
            )
            return

        payload = {
            "pluginId": RESULT_INDEX_PUSH_PLUGIN_ID,
            "pluginName": RESULT_INDEX_PUSH_PLUGIN_NAME,
            "question": RESULT_INDEX_PUSH_QUESTION,
            "userInfoList": user_info_list,
        }
        try:
            async with httpx.AsyncClient(
                timeout=RESULT_INDEX_PUSH_TIMEOUT,
            ) as client:
                response = await client.post(
                    RESULT_INDEX_PUSH_URL,
                    headers={"Content-Type": "application/json"},
                    json=payload,
                )
            if response.status_code >= 400:
                logger.warning(
                    "Result-index user push failed: status=%d users=%d",
                    response.status_code,
                    len(user_info_list),
                )
                return
            logger.info(
                "Result-index users pushed: users=%d",
                len(user_info_list),
            )
        except Exception as e:
            logger.warning("Result-index user push failed: %s", e)

    def _schedule_result_index_user_push(
        self,
        users: list[dict[str, str]],
    ) -> None:
        """Schedule result-index user push without blocking sync response."""
        if not users:
            return
        asyncio.create_task(self._push_result_index_users(users))

    def _build_api_url(self, task_id: str) -> str:
        """Build external API URL for task status query."""
        return (
            f"{ASYNC_TASK_QUERY_URL}/app/{ASYNC_TASK_APP_KEY}"
            f"/tag/{ASYNC_TASK_ENV_TAG}/result/query/{task_id}"
        )

    def _parse_api_response(
        self,
        data: dict,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Parse API response and extract status.

        Args:
            data: API response JSON

        Returns:
            Tuple of (status, error_message)
        """
        return_code = data.get("returnCode", "")
        if return_code != "SUC0000":
            return "FAIL", f"returnCode={return_code}"

        body = data.get("body", {})
        if not body:
            return "FAIL", "No body"

        status = body.get("status", "")
        if status not in VALID_SUBTASK_STATUSES:
            return None, f"Invalid status={status}"

        return status, None

    async def _query_task_status(
        self,
        client: httpx.AsyncClient,
        task_id: str,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Query task status from external API.

        Args:
            client: HTTP client
            task_id: Subtask task_id

        Returns:
            Tuple of (status, error_message)
        """
        url = self._build_api_url(task_id)

        try:
            api_response = await client.post(
                url,
                headers={
                    "Content-type": "application/json;charset=utf-8",
                    "API-Key": ASYNC_TASK_API_KEY,
                },
                json={},
            )

            if api_response.status_code != 200:
                return None, f"API returned {api_response.status_code}"

            return self._parse_api_response(api_response.json())

        except httpx.TimeoutException:
            return None, "API timeout"
        except httpx.RequestError as e:
            return None, str(e)
        except Exception as e:
            logger.error(
                "Unexpected error querying task %s: %s",
                task_id[:20],
                e,
            )
            return None, str(e)

    def _check_pending_timeout(
        self,
        subtask: SubtaskModel,
        now: datetime,
    ) -> bool:
        """Check if pending subtask exceeds fallback timeout hours.

        Args:
            subtask: Subtask to check
            now: Current datetime

        Returns:
            True if exceeds timeout, False otherwise
        """
        if not subtask.created_at:
            return False
        hours_pending = (now - subtask.created_at).total_seconds() / 3600
        return hours_pending > FALLBACK_TIMEOUT_HOURS

    async def _process_pending_subtask(
        self,
        client: httpx.AsyncClient,
        subtask: SubtaskModel,
        now: datetime,
    ) -> SubtaskSyncDetailItem:
        """Process subtask with NULL/empty status.

        Args:
            client: HTTP client
            subtask: Subtask to process
            now: Current datetime

        Returns:
            SubtaskSyncDetailItem with processing result
        """
        detail = SubtaskSyncDetailItem(
            task_id=subtask.task_id,
            old_status=subtask.status,
        )

        # 固定 task_id="default" 的子任务直接标记成功
        if subtask.task_id == "default":
            if subtask.has_main_task == 0:
                await self.query_service.complete_standalone_success_subtask(
                    subtask,
                )
            else:
                await self.query_service.update_subtask_status(
                    subtask.task_id,
                    subtask.trace_id,
                    "SUC",
                    "",
                )
            detail.new_status = "SUC"
            logger.info(
                "Auto-marked default subtask as SUC: trace_id=%s",
                subtask.trace_id[:20],
            )
            return detail

        # 兜底检查：超过2小时的pending子任务标记TIMEOUT
        if self._check_pending_timeout(subtask, now):
            created_at = subtask.created_at
            assert created_at is not None
            hours_pending = int(
                (now - created_at).total_seconds() / 3600,
            )
            logger.warning(
                "Subtask pending over %dh, marking TIMEOUT: task_id=%s hours=%d",
                FALLBACK_TIMEOUT_HOURS,
                subtask.task_id,
                hours_pending,
            )
            await self.query_service.update_subtask_status(
                subtask.task_id,
                subtask.trace_id,
                "TIMEOUT",
                "",
            )
            detail.new_status = "TIMEOUT"
            detail.error = (
                f"Pending over {FALLBACK_TIMEOUT_HOURS}h, fallback TIMEOUT"
            )
            return detail

        if not self._is_configured():
            detail.error = "API not configured"
            return detail

        status, error = await self._query_task_status(client, subtask.task_id)
        if status:
            if status == "SUC" and subtask.has_main_task == 0:
                await self.query_service.complete_standalone_success_subtask(
                    subtask,
                )
            else:
                await self.query_service.update_subtask_status(
                    subtask.task_id,
                    subtask.trace_id,
                    status,
                    error or "",
                )
            detail.new_status = status
            logger.info(
                "Synced subtask status: task_id=%s status=%s",
                subtask.task_id[:20],
                status,
            )
        else:
            detail.error = error or "Unknown error"
            logger.warning(
                "Failed to query task %s: %s",
                subtask.task_id[:20],
                detail.error,
            )
        return detail

    async def sync_subtask_status(
        self,
        batch_size: int = BATCH_SIZE,
    ) -> SubtaskSyncStatusResponse:
        """Sync subtask statuses from external API.

        只查询无状态的子任务，查询API并更新。
        过2小时的pending子任务标记TIMEOUT（兜底）。
        FAIL/PART_SUC/TIMEOUT/SUC 视为终态，不再查询。
        """
        now = datetime.now()

        logger.info("Starting subtask sync: time=%s", now.strftime("%H:%M"))

        subtasks = await self.query_service.get_today_pending_subtasks(
            limit=batch_size,
        )
        if not subtasks:
            logger.debug("No pending subtasks to sync")
            return SubtaskSyncStatusResponse(
                success=True,
                total_scanned=0,
                total_updated=0,
                total_failed=0,
            )

        response = SubtaskSyncStatusResponse(
            success=True,
            total_scanned=len(subtasks),
        )

        client = await self._get_client()

        for subtask in subtasks:
            detail = await self._process_pending_subtask(client, subtask, now)
            response.details.append(detail)
            if detail.new_status:
                response.total_updated += 1
            else:
                response.total_failed += 1

        return response

    async def sync_execution_async_status(
        self,
        batch_size: int = 200,  # noqa: ARG002 - 保留参数兼容性
    ) -> ExecutionAsyncStatusResponse:
        """Sync execution async_status from subtask statuses.

        使用 JOIN 批量更新，高效处理大量数据：
        - 没有 subtasks 或全部 SUC → success
        - 存在 FAIL/PART_SUC/TIMEOUT 且没有 pending → error
        - 存在 pending 子任务 → 不更新，等待下次同步
        """
        logger.info("Starting execution async_status sync (batch mode)")

        (
            success_count,
            error_count,
            indexed_count,
            indexed_users,
        ) = await self.query_service.batch_update_execution_async_status()
        self._schedule_result_index_user_push(indexed_users)

        total_updated = success_count + error_count
        logger.info(
            "Execution async_status sync completed: updated=%d indexed=%d",
            total_updated,
            indexed_count,
        )

        return ExecutionAsyncStatusResponse(
            success=True,
            total_scanned=total_updated,
            total_updated=total_updated,
            total_success=success_count,
            total_error=error_count,
        )


# Global service instance
_sync_service: Optional[SyncService] = None


def get_sync_service() -> SyncService:
    """Get the global SyncService instance."""
    global _sync_service
    if _sync_service is None:
        _sync_service = SyncService()
    return _sync_service
