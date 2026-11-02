# -*- coding: utf-8 -*-
"""Subtask query service for database operations.

Provides methods for creating and querying subtask records.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

import httpx

from ....config.constant import API_CALL_TIMEOUT, CUSTOMER_NAME_QUERY_URL
from ...database.connection import get_db_connection
from ...models.subtask import SubtaskModel, SubtaskCreateResponse
from ....utils.bbk import normalize_bbk_id_to_primary

logger = logging.getLogger(__name__)

# 每批处理数量
BATCH_SIZE = 50

CUSTOMER_NAME_FIELD = "EAC_NM"

RESULT_INDEX_REQUIRED_FIELDS = (
    "first_bbk_id",
    "tenant_id",
    "bbk_org_id",
    "skill_id",
    "job_id",
    "result_type",
    "result_id",
)

STANDALONE_RESULT_SOURCE_ID = "RMASSIST"
STANDALONE_INFO_REQUIRED_FIELDS = (
    "tenant_id",
    "skill_id",
    "job_id",
    "first_bbk_id",
)


class QueryService:
    """Service for subtask database operations."""

    def __init__(self, db=None):
        """Initialize query service.

        Args:
            db: Database connection
        """
        self.db = db

    async def create_subtask(
        self,
        trace_id: str,
        task_id: str,
        filename: str,
        task_type: Optional[str] = None,
        custuid: Optional[str] = None,
        bbk_org_id: Optional[str] = None,
        cust_nm: Optional[str] = None,
        notification_content_wplus: Optional[str] = None,
        notification_content_zhaohu: Optional[str] = None,
        need_notification: int = 1,
        has_main_task: int = 1,
        template_id: Optional[int] = None,
        result_id: Optional[str] = None,
        status: Optional[str] = None,
        info: str = "",
    ) -> SubtaskCreateResponse:
        """Create a subtask record.

        Args:
            trace_id: Main task trace_id
            task_id: Subtask task_id
            filename: File name
            task_type: Task type (list/plan)
            custuid: Customer ID
            bbk_org_id: Customer branch ID for reference
            cust_nm: Customer name
            notification_content_wplus: W+ channel notification content
            notification_content_zhaohu: Zhaohu channel notification content
            need_notification: Whether notification is needed (0 or 1)
            has_main_task: Whether the subtask has a main task (0 or 1)
            template_id: Template ID for html content rendering
            result_id: ES document ID for reference
            status: Subtask status (SUC/FAIL/TIMEOUT)
            info: Additional subtask information

        Returns:
            SubtaskCreateResponse with creation result
        """
        if not self.db:
            logger.warning("Database not connected, skipping subtask creation")
            return SubtaskCreateResponse(
                success=False,
                message="Database not connected",
            )

        # Check if already exists (idempotent)
        existing = await self._get_subtask_by_trace_and_task(trace_id, task_id)
        if existing:
            logger.debug(
                "Subtask already exists: trace_id=%s task_id=%s",
                trace_id,
                task_id,
            )
            return SubtaskCreateResponse(
                success=True,
                id=existing.id,
                message="Subtask already exists",
            )

        # Insert new record
        query = """
            INSERT INTO swe_cron_subtasks (
                trace_id,
                task_id,
                filename,
                task_type,
                custuid,
                bbk_org_id,
                cust_nm,
                notification_content_wplus,
                notification_content_zhaohu,
                need_notification,
                has_main_task,
                status,
                info,
                created_at,
                updated_at,
                template_id,
                result_id
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, NULL, %s, %s
            )
        """
        now = datetime.now()
        await self.db.execute(
            query,
            (
                trace_id,
                task_id,
                filename,
                task_type,
                custuid,
                bbk_org_id,
                cust_nm,
                notification_content_wplus,
                notification_content_zhaohu,
                need_notification,
                has_main_task,
                status,
                info,
                now,
                template_id,
                result_id,
            ),
        )

        # Get the inserted ID
        id_query = "SELECT LAST_INSERT_ID() AS id"
        row = await self.db.fetch_one(id_query)
        inserted_id = row.get("id") if row else None

        logger.info(
            "Created subtask: trace_id=%s task_id=%s filename=%s id=%s",
            trace_id,
            task_id,
            filename,
            inserted_id,
        )

        return SubtaskCreateResponse(
            success=True,
            id=inserted_id,
            message="Subtask created",
        )

    async def _get_subtask_by_trace_and_task(
        self,
        trace_id: str,
        task_id: str,
    ) -> Optional[SubtaskModel]:
        """Get subtask by trace_id and task_id.

        Args:
            trace_id: Main task trace_id
            task_id: Subtask task_id

        Returns:
            SubtaskModel or None
        """
        if not self.db:
            return None

        query = """
            SELECT id, trace_id, task_id, filename, task_type, custuid, cust_nm,
                   notification_content_wplus, notification_content_zhaohu,
                   need_notification, has_main_task, status, info,
                   bbk_org_id, template_id, result_id, created_at, updated_at
            FROM swe_cron_subtasks
            WHERE trace_id = %s AND task_id = %s
        """
        row = await self.db.fetch_one(query, (trace_id, task_id))
        if not row:
            return None

        return SubtaskModel(
            id=row.get("id"),
            trace_id=row.get("trace_id") or "",
            task_id=row.get("task_id") or "",
            filename=row.get("filename") or "",
            task_type=row.get("task_type"),
            custuid=row.get("custuid"),
            cust_nm=row.get("cust_nm"),
            notification_content_wplus=row.get("notification_content_wplus"),
            notification_content_zhaohu=row.get("notification_content_zhaohu"),
            need_notification=row.get("need_notification", 1),
            has_main_task=row.get("has_main_task", 1),
            status=row.get("status"),
            info=row.get("info") or "",
            bbk_org_id=row.get("bbk_org_id"),
            template_id=row.get("template_id"),
            result_id=row.get("result_id"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    async def get_pending_subtasks(
        self,
        limit: int = BATCH_SIZE,
    ) -> list[SubtaskModel]:
        """Get subtasks with NULL or empty status.

        Args:
            limit: Maximum number of records to return

        Returns:
            List of SubtaskModel
        """
        if not self.db:
            return []

        query = """
            SELECT id, trace_id, task_id, filename, task_type, custuid, cust_nm,
                   notification_content_wplus, notification_content_zhaohu,
                   need_notification, has_main_task, status, info,
                   bbk_org_id, template_id, result_id, created_at, updated_at
            FROM swe_cron_subtasks
            WHERE status IS NULL OR status = ''
            ORDER BY created_at ASC
            LIMIT %s
        """
        rows = await self.db.fetch_all(query, (limit,))
        return [
            SubtaskModel(
                id=row.get("id"),
                trace_id=row.get("trace_id") or "",
                task_id=row.get("task_id") or "",
                filename=row.get("filename") or "",
                task_type=row.get("task_type"),
                custuid=row.get("custuid"),
                cust_nm=row.get("cust_nm"),
                notification_content_wplus=row.get(
                    "notification_content_wplus",
                ),
                notification_content_zhaohu=row.get(
                    "notification_content_zhaohu",
                ),
                need_notification=row.get("need_notification", 1),
                has_main_task=row.get("has_main_task", 1),
                status=row.get("status"),
                info=row.get("info") or "",
                bbk_org_id=row.get("bbk_org_id"),
                template_id=row.get("template_id"),
                result_id=row.get("result_id"),
                created_at=row.get("created_at"),
                updated_at=row.get("updated_at"),
            )
            for row in rows
        ]

    async def get_today_pending_subtasks(
        self,
        limit: int = BATCH_SIZE,
    ) -> list[SubtaskModel]:
        """Get subtasks for status sync.

        查询范围：
        只查询无状态的子任务（status IS NULL OR status = ''），不限制时间

        Args:
            limit: Maximum number of records to return

        Returns:
            List of SubtaskModel
        """
        if not self.db:
            return []

        query = """
            SELECT id, trace_id, task_id, filename, task_type, custuid, cust_nm,
                   notification_content_wplus, notification_content_zhaohu,
                   need_notification, has_main_task, status, info,
                   bbk_org_id, template_id, result_id, created_at, updated_at
            FROM swe_cron_subtasks
            WHERE status IS NULL OR status = ''
            ORDER BY created_at ASC
            LIMIT %s
        """
        rows = await self.db.fetch_all(query, (limit,))
        return [
            SubtaskModel(
                id=row.get("id"),
                trace_id=row.get("trace_id") or "",
                task_id=row.get("task_id") or "",
                filename=row.get("filename") or "",
                task_type=row.get("task_type"),
                custuid=row.get("custuid"),
                cust_nm=row.get("cust_nm"),
                notification_content_wplus=row.get(
                    "notification_content_wplus",
                ),
                notification_content_zhaohu=row.get(
                    "notification_content_zhaohu",
                ),
                need_notification=row.get("need_notification", 1),
                has_main_task=row.get("has_main_task", 1),
                status=row.get("status"),
                info=row.get("info") or "",
                bbk_org_id=row.get("bbk_org_id"),
                template_id=row.get("template_id"),
                result_id=row.get("result_id"),
                created_at=row.get("created_at"),
                updated_at=row.get("updated_at"),
            )
            for row in rows
        ]

    async def get_subtasks_by_trace_id(
        self,
        trace_id: str,
    ) -> list[SubtaskModel]:
        """Get all subtasks for a trace_id.

        Args:
            trace_id: Main task trace_id

        Returns:
            List of SubtaskModel
        """
        if not self.db:
            return []

        query = """
            SELECT id, trace_id, task_id, filename, task_type, custuid, cust_nm,
                   notification_content_wplus, notification_content_zhaohu,
                   need_notification, has_main_task, status, info,
                   bbk_org_id, template_id, result_id, created_at, updated_at
            FROM swe_cron_subtasks
            WHERE trace_id = %s
        """
        rows = await self.db.fetch_all(query, (trace_id,))
        return [
            SubtaskModel(
                id=row.get("id"),
                trace_id=row.get("trace_id") or "",
                task_id=row.get("task_id") or "",
                filename=row.get("filename") or "",
                task_type=row.get("task_type"),
                custuid=row.get("custuid"),
                cust_nm=row.get("cust_nm"),
                notification_content_wplus=row.get(
                    "notification_content_wplus",
                ),
                notification_content_zhaohu=row.get(
                    "notification_content_zhaohu",
                ),
                need_notification=row.get("need_notification", 1),
                has_main_task=row.get("has_main_task", 1),
                status=row.get("status"),
                info=row.get("info") or "",
                bbk_org_id=row.get("bbk_org_id"),
                template_id=row.get("template_id"),
                result_id=row.get("result_id"),
                created_at=row.get("created_at"),
                updated_at=row.get("updated_at"),
            )
            for row in rows
        ]

    async def update_subtask_status(
        self,
        task_id: str,
        trace_id: str,
        status: str,
        error_msg: str,
    ) -> bool:
        """Update subtask status.

        Args:
            task_id: Subtask task_id
            trace_id: Main task trace_id
            status: New status value
            error_msg: error msg

        Returns:
            True if updated, False otherwise
        """
        if not self.db:
            return False

        query = """
            UPDATE swe_cron_subtasks
            SET status = %s, updated_at = %s, info = %s
            WHERE task_id = %s AND trace_id = %s
        """
        now = datetime.now()
        await self.db.execute(
            query,
            (status, now, error_msg, task_id, trace_id),
        )

        logger.debug(
            "Updated subtask status: task_id=%s trace_id=%s status=%s error_msg=%s",
            task_id[:20],
            trace_id[:20],
            status,
            error_msg,
        )
        return True

    @staticmethod
    def _parse_standalone_result_info(subtask: SubtaskModel) -> Optional[dict]:
        """Parse index metadata without imposing a format on stored info."""
        try:
            metadata = json.loads(subtask.info)
        except (TypeError, ValueError):
            metadata = None

        if not isinstance(metadata, dict):
            logger.warning(
                "Skipped standalone subtask result index: "
                "subtask_id=%s task_id=%s reason=invalid_info_json",
                subtask.id,
                subtask.task_id,
            )
            return None

        missing_fields = [
            field
            for field in STANDALONE_INFO_REQUIRED_FIELDS
            if not isinstance(metadata.get(field), str)
            or not metadata[field].strip()
        ]
        if missing_fields:
            logger.warning(
                "Skipped standalone subtask result index: "
                "subtask_id=%s task_id=%s reason=missing_info_fields "
                "fields=%s",
                subtask.id,
                subtask.task_id,
                ",".join(missing_fields),
            )
            return None
        return metadata

    async def _update_subtask_status_preserving_info(
        self,
        subtask: SubtaskModel,
        status: str,
    ) -> None:
        """Update a standalone subtask status without overwriting metadata."""
        await self.db.execute(
            """
                UPDATE swe_cron_subtasks
                SET status = %s, updated_at = %s
                WHERE id = %s
            """,
            (status, datetime.now(), subtask.id),
        )

    async def complete_standalone_success_subtask(
        self,
        subtask: SubtaskModel,
    ) -> bool:
        """Complete a standalone subtask and index a valid plan result."""
        metadata = self._parse_standalone_result_info(subtask)
        if (
            metadata is None
            or subtask.task_type != "plan"
            or subtask.id is None
            or not subtask.template_id
            or subtask.template_id <= 0
        ):
            await self._update_subtask_status_preserving_info(subtask, "SUC")
            return False

        customer_names = {}
        if not (subtask.cust_nm or "").strip() and subtask.custuid:
            customer_names = await self._query_customer_names(
                {subtask.custuid},
            )

        completed_at = datetime.now()
        execution = {
            "source_id": STANDALONE_RESULT_SOURCE_ID,
            "tenant_id": metadata["tenant_id"].strip(),
            "bbk_id": metadata["first_bbk_id"].strip(),
            "job_id": metadata["job_id"].strip(),
            "execution_id": subtask.id,
            "trace_id": subtask.trace_id,
        }
        subtask_row = {
            "subtask_id": subtask.id,
            "task_id": subtask.task_id,
            "task_type": subtask.task_type,
            "custuid": subtask.custuid,
            "cust_nm": subtask.cust_nm,
            "bbk_org_id": subtask.bbk_org_id,
            "template_id": subtask.template_id,
            "result_id": subtask.result_id,
            "filename": subtask.filename,
            "status": "SUC",
        }
        result_row = self._build_result_index_row(
            execution,
            subtask_row,
            metadata["skill_id"].strip(),
            completed_at,
            completed_at + timedelta(days=30),
            customer_names,
        )
        result_row["first_bbk_id"] = metadata["first_bbk_id"].strip()
        if not self._is_valid_result_index_row(result_row):
            await self._update_subtask_status_preserving_info(subtask, "SUC")
            return False

        await self._write_standalone_result_transaction(subtask, result_row)
        return True

    async def _write_standalone_result_transaction(
        self,
        subtask: SubtaskModel,
        row: dict,
    ) -> None:
        """Atomically supersede the old plan result and complete the subtask."""
        stale_query = """
            UPDATE swe_cron_result_index
            SET is_latest_success = 0, updated_at = %s
            WHERE source_id = %s
              AND tenant_id = %s
              AND first_bbk_id = %s
              AND bbk_org_id = %s
              AND skill_id = %s
              AND result_type = %s
              AND custuid = %s
              AND is_latest_success = 1
              AND execution_id <> %s
        """
        upsert_query = """
            INSERT INTO swe_cron_result_index (
                source_id, tenant_id, first_bbk_id, bbk_org_id, custuid,
                cust_nm, skill_id, job_id, execution_id, trace_id,
                subtask_id, task_id, result_type, template_id, result_id,
                filename, status, is_latest_success, execution_at,
                updated_at, expire_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, 1, %s, %s, %s
            )
            ON DUPLICATE KEY UPDATE
                source_id = VALUES(source_id),
                tenant_id = VALUES(tenant_id),
                first_bbk_id = VALUES(first_bbk_id),
                bbk_org_id = VALUES(bbk_org_id),
                custuid = VALUES(custuid),
                cust_nm = VALUES(cust_nm),
                job_id = VALUES(job_id),
                execution_id = VALUES(execution_id),
                trace_id = VALUES(trace_id),
                task_id = VALUES(task_id),
                result_type = VALUES(result_type),
                template_id = VALUES(template_id),
                result_id = VALUES(result_id),
                filename = VALUES(filename),
                status = VALUES(status),
                is_latest_success = 1,
                execution_at = VALUES(execution_at),
                updated_at = VALUES(updated_at),
                expire_at = VALUES(expire_at)
        """
        status_query = """
            UPDATE swe_cron_subtasks
            SET status = 'SUC', updated_at = %s
            WHERE id = %s
        """

        async with self.db.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        stale_query,
                        self._build_result_index_stale_params(row),
                    )
                    await cursor.execute(
                        upsert_query,
                        self._build_result_index_upsert_params(row),
                    )
                    await cursor.execute(
                        status_query,
                        (datetime.now(), subtask.id),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def get_pending_executions(
        self,
        limit: int = 100,
    ) -> list[dict]:
        """Get executions with NULL or empty async_status.

        Args:
            limit: Maximum number of records to return

        Returns:
            List of execution dicts with id and trace_id
        """
        if not self.db:
            return []

        query = """
            SELECT id, trace_id
            FROM swe_cron_executions
            WHERE async_status IS NULL OR async_status = ''
            ORDER BY created_at ASC
            LIMIT %s
        """
        rows = await self.db.fetch_all(query, (limit,))
        return [
            {
                "id": row.get("id"),
                "trace_id": row.get("trace_id") or "",
            }
            for row in rows
        ]

    async def update_execution_async_status(
        self,
        execution_id: int,
        async_status: str,
    ) -> bool:
        """Update execution async_status.

        Args:
            execution_id: Execution ID
            async_status: New async_status value (success/error)

        Returns:
            True if updated, False otherwise
        """
        if not self.db:
            return False

        query = """
            UPDATE swe_cron_executions
            SET async_status = %s
            WHERE id = %s
        """
        await self.db.execute(query, (async_status, execution_id))

        logger.debug(
            "Updated execution async_status: id=%s async_status=%s",
            execution_id,
            async_status,
        )
        return True

    async def _get_completed_execution_candidates(self) -> list[dict]:
        """Get pending executions whose subtasks have all completed."""
        query = """
            SELECT
                e.id AS execution_id,
                e.job_id,
                e.trace_id,
                e.actual_time,
                e.created_at,
                j.tenant_id,
                j.bbk_id,
                j.source_id,
                j.skill_ids
            FROM swe_cron_executions e
            JOIN swe_cron_jobs j ON j.id = e.job_id
            WHERE (e.async_status IS NULL OR e.async_status = '')
              AND e.status = 'success'
              AND NOT EXISTS (
                  SELECT 1 FROM swe_cron_subtasks s
                  WHERE s.trace_id = e.trace_id
                  AND (s.status IS NULL OR s.status = '')
              )
        """
        return await self.db.fetch_all(query)

    def _split_skill_ids(self, skill_ids: Optional[str]) -> list[str]:
        """Split comma-separated skill ids while preserving order."""
        values = []
        seen = set()
        for skill_id in (skill_ids or "").split(","):
            normalized = skill_id.strip()
            if normalized and normalized not in seen:
                values.append(normalized)
                seen.add(normalized)
        return values

    async def _get_success_subtasks_for_trace(
        self,
        trace_id: str,
    ) -> list[dict]:
        """Get successful list/plan subtasks for a trace."""
        query = """
            SELECT
                id AS subtask_id,
                trace_id,
                task_id,
                filename,
                task_type,
                custuid,
                cust_nm,
                bbk_org_id,
                template_id,
                result_id,
                status,
                created_at
            FROM swe_cron_subtasks
            WHERE trace_id = %s
              AND status = 'SUC'
              AND task_type IN ('list', 'plan')
              AND template_id IS NOT NULL
              AND template_id > 0
              AND result_id IS NOT NULL
              AND result_id <> ''
            ORDER BY id DESC
        """
        rows = await self.db.fetch_all(query, (trace_id,))
        return self._dedupe_success_subtasks(rows)

    @staticmethod
    def _dedupe_success_subtasks(rows: list[dict]) -> list[dict]:
        """Keep one successful list per trace and one plan per customer.

        Queries order rows by descending primary key, so the first row for a
        logical result is the newest one.  The database may contain duplicate
        subtasks from retries or older writes; indexing all of them would
        create duplicate result-index records.
        """
        deduped: list[dict] = []
        seen: set[tuple] = set()
        ordered_rows = sorted(
            rows,
            key=lambda row: row.get("subtask_id") or row.get("id") or 0,
            reverse=True,
        )
        for row in ordered_rows:
            task_type = row.get("task_type")
            if task_type == "list":
                key = (task_type, row.get("trace_id"))
            elif task_type == "plan":
                key = (task_type, row.get("trace_id"), row.get("custuid"))
            else:
                continue
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        return deduped

    async def _get_success_subtasks_for_traces(
        self,
        trace_ids: list[str],
    ) -> dict[str, list[dict]]:
        """Get successful list/plan subtasks grouped by trace id."""
        if not trace_ids:
            return {}

        placeholders = ", ".join(["%s"] * len(trace_ids))
        query = f"""
            SELECT
                id AS subtask_id,
                trace_id,
                task_id,
                filename,
                task_type,
                custuid,
                cust_nm,
                bbk_org_id,
                template_id,
                result_id,
                status,
                created_at
            FROM swe_cron_subtasks
            WHERE trace_id IN ({placeholders})
              AND status = 'SUC'
              AND task_type IN ('list', 'plan')
              AND template_id IS NOT NULL
              AND template_id > 0
              AND result_id IS NOT NULL
              AND result_id <> ''
            ORDER BY id DESC
        """
        rows = await self.db.fetch_all(query, tuple(trace_ids))
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            trace_id = row.get("trace_id") or ""
            grouped.setdefault(trace_id, []).append(row)
        for trace_id, subtasks in grouped.items():
            grouped[trace_id] = self._dedupe_success_subtasks(subtasks)
        return grouped

    async def _mark_previous_result_index_stale(
        self,
        row: dict,
    ) -> None:
        """Mark previous successful result-index rows as non-latest."""
        if row["result_type"] == "plan":
            query = """
                UPDATE swe_cron_result_index
                SET is_latest_success = 0, updated_at = %s
                WHERE source_id = %s
                  AND tenant_id = %s
                  AND first_bbk_id = %s
                  AND bbk_org_id = %s
                  AND skill_id = %s
                  AND job_id = %s
                  AND result_type = %s
                  AND custuid = %s
                  AND is_latest_success = 1
                  AND execution_id <> %s
            """
            params = (
                datetime.now(),
                row["source_id"],
                row["tenant_id"],
                row["first_bbk_id"],
                row["bbk_org_id"],
                row["skill_id"],
                row["job_id"],
                row["result_type"],
                row["custuid"],
                row["execution_id"],
            )
        else:
            query = """
                UPDATE swe_cron_result_index
                SET is_latest_success = 0, updated_at = %s
                WHERE source_id = %s
                  AND tenant_id = %s
                  AND first_bbk_id = %s
                  AND bbk_org_id = %s
                  AND skill_id = %s
                  AND job_id = %s
                  AND result_type = %s
                  AND is_latest_success = 1
                  AND execution_id <> %s
            """
            params = (
                datetime.now(),
                row["source_id"],
                row["tenant_id"],
                row["first_bbk_id"],
                row["bbk_org_id"],
                row["skill_id"],
                row["job_id"],
                row["result_type"],
                row["execution_id"],
            )
        await self.db.execute(query, params)

    async def _upsert_result_index_row(self, row: dict) -> None:
        """Insert or update a result-index row."""
        query = """
            INSERT INTO swe_cron_result_index (
                source_id,
                tenant_id,
                first_bbk_id,
                bbk_org_id,
                custuid,
                cust_nm,
                skill_id,
                job_id,
                execution_id,
                trace_id,
                subtask_id,
                task_id,
                result_type,
                template_id,
                result_id,
                filename,
                status,
                is_latest_success,
                execution_at,
                updated_at,
                expire_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, 1, %s, %s, %s
            )
            ON DUPLICATE KEY UPDATE
                source_id = VALUES(source_id),
                tenant_id = VALUES(tenant_id),
                first_bbk_id = VALUES(first_bbk_id),
                bbk_org_id = VALUES(bbk_org_id),
                custuid = VALUES(custuid),
                cust_nm = VALUES(cust_nm),
                job_id = VALUES(job_id),
                execution_id = VALUES(execution_id),
                trace_id = VALUES(trace_id),
                task_id = VALUES(task_id),
                result_type = VALUES(result_type),
                template_id = VALUES(template_id),
                result_id = VALUES(result_id),
                filename = VALUES(filename),
                status = VALUES(status),
                is_latest_success = 1,
                execution_at = VALUES(execution_at),
                updated_at = VALUES(updated_at),
                expire_at = VALUES(expire_at)
        """
        params = (
            row["source_id"],
            row["tenant_id"],
            row["first_bbk_id"],
            row["bbk_org_id"],
            row["custuid"],
            row["cust_nm"],
            row["skill_id"],
            row["job_id"],
            row["execution_id"],
            row["trace_id"],
            row["subtask_id"],
            row["task_id"],
            row["result_type"],
            row["template_id"],
            row["result_id"],
            row["filename"],
            row["status"],
            row["execution_at"],
            datetime.now(),
            row["expire_at"],
        )
        await self.db.execute(query, params)

    @staticmethod
    def _build_result_index_stale_params(row: dict) -> tuple:
        """Build params for stale result-index updates."""
        common_params = (
            datetime.now(),
            row["source_id"],
            row["tenant_id"],
            row["first_bbk_id"],
            row["bbk_org_id"],
            row["skill_id"],
            row["result_type"],
        )
        if row["result_type"] == "plan":
            return common_params + (row["custuid"], row["execution_id"])
        return common_params + (row["execution_id"],)

    @staticmethod
    def _build_result_index_upsert_params(row: dict) -> tuple:
        """Build params for result-index upserts."""
        return (
            row["source_id"],
            row["tenant_id"],
            row["first_bbk_id"],
            row["bbk_org_id"],
            row["custuid"],
            row["cust_nm"],
            row["skill_id"],
            row["job_id"],
            row["execution_id"],
            row["trace_id"],
            row["subtask_id"],
            row["task_id"],
            row["result_type"],
            row["template_id"],
            row["result_id"],
            row["filename"],
            row["status"],
            row["execution_at"],
            datetime.now(),
            row["expire_at"],
        )

    def _build_result_index_row(
        self,
        execution: dict,
        subtask: dict,
        skill_id: str,
        execution_at: datetime,
        expire_at: datetime,
        customer_names: dict[str, str],
    ) -> dict:
        """Build one query-index row for a successful subtask result."""
        bbk_org_id = subtask.get("bbk_org_id") or ""
        custuid = subtask.get("custuid") or ""
        customer_name = subtask.get("cust_nm") or customer_names.get(custuid)
        first_bbk_id = (
            normalize_bbk_id_to_primary(bbk_org_id)
            or execution.get("bbk_id")
            or ""
        )
        return {
            "source_id": execution.get("source_id") or "",
            "tenant_id": execution.get("tenant_id") or "",
            "first_bbk_id": first_bbk_id,
            "bbk_org_id": bbk_org_id,
            "custuid": custuid,
            "cust_nm": self._mask_customer_name(customer_name),
            "skill_id": skill_id,
            "job_id": execution.get("job_id") or "",
            "execution_id": execution.get("execution_id"),
            "trace_id": execution.get("trace_id") or "",
            "subtask_id": subtask.get("subtask_id"),
            "task_id": subtask.get("task_id") or "",
            "result_type": subtask.get("task_type"),
            "template_id": subtask.get("template_id"),
            "result_id": subtask.get("result_id"),
            "filename": subtask.get("filename"),
            "status": subtask.get("status"),
            "execution_at": execution_at,
            "expire_at": expire_at,
        }

    async def _write_result_index_row(self, row: dict) -> None:
        """Mark older rows stale, then upsert the latest result-index row."""
        await self._mark_previous_result_index_stale(row)
        await self._upsert_result_index_row(row)

    async def _write_result_index_rows(self, rows: list[dict]) -> None:
        """Batch mark stale rows, then batch upsert latest result-index rows."""
        if not rows:
            return

        plan_stale_query = """
            UPDATE swe_cron_result_index
            SET is_latest_success = 0, updated_at = %s
            WHERE source_id = %s
              AND tenant_id = %s
              AND first_bbk_id = %s
              AND bbk_org_id = %s
              AND skill_id = %s
              AND result_type = %s
              AND custuid = %s
              AND is_latest_success = 1
              AND execution_id <> %s
        """
        non_plan_stale_query = """
            UPDATE swe_cron_result_index
            SET is_latest_success = 0, updated_at = %s
            WHERE source_id = %s
              AND tenant_id = %s
              AND first_bbk_id = %s
              AND bbk_org_id = %s
              AND skill_id = %s
              AND result_type = %s
              AND is_latest_success = 1
              AND execution_id <> %s
        """
        upsert_query = """
            INSERT INTO swe_cron_result_index (
                source_id,
                tenant_id,
                first_bbk_id,
                bbk_org_id,
                custuid,
                cust_nm,
                skill_id,
                job_id,
                execution_id,
                trace_id,
                subtask_id,
                task_id,
                result_type,
                template_id,
                result_id,
                filename,
                status,
                is_latest_success,
                execution_at,
                updated_at,
                expire_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, 1, %s, %s, %s
            )
            ON DUPLICATE KEY UPDATE
                source_id = VALUES(source_id),
                tenant_id = VALUES(tenant_id),
                first_bbk_id = VALUES(first_bbk_id),
                bbk_org_id = VALUES(bbk_org_id),
                custuid = VALUES(custuid),
                cust_nm = VALUES(cust_nm),
                job_id = VALUES(job_id),
                execution_id = VALUES(execution_id),
                trace_id = VALUES(trace_id),
                task_id = VALUES(task_id),
                result_type = VALUES(result_type),
                template_id = VALUES(template_id),
                result_id = VALUES(result_id),
                filename = VALUES(filename),
                status = VALUES(status),
                is_latest_success = 1,
                execution_at = VALUES(execution_at),
                updated_at = VALUES(updated_at),
                expire_at = VALUES(expire_at)
        """

        plan_stale_params = [
            self._build_result_index_stale_params(row)
            for row in rows
            if row["result_type"] == "plan"
        ]
        non_plan_stale_params = [
            self._build_result_index_stale_params(row)
            for row in rows
            if row["result_type"] != "plan"
        ]
        upsert_params = [
            self._build_result_index_upsert_params(row) for row in rows
        ]

        await self.db.execute_many(plan_stale_query, plan_stale_params)
        await self.db.execute_many(non_plan_stale_query, non_plan_stale_params)
        await self.db.execute_many(upsert_query, upsert_params)

    @staticmethod
    def _mask_customer_name(name: Optional[str]) -> Optional[str]:
        """Mask a customer name unless it has already been masked."""
        if name is None:
            return None

        clean_name = name.strip()
        if not clean_name:
            return None
        if "*" in clean_name or len(clean_name) == 1:
            return clean_name
        if len(clean_name) == 2:
            return f"{clean_name[0]}*"
        return f"{clean_name[0]}{'*' * (len(clean_name) - 2)}{clean_name[-1]}"

    @staticmethod
    def _reverse_custuid(custuid: str) -> str:
        """Build the customer-name query row key."""
        return custuid[::-1]

    def _extract_customer_names(self, data: dict) -> dict[str, str]:
        """Extract customer names from the batch-query API response."""
        customer_names = {}
        rows = data.get("data", [])
        if not isinstance(rows, list):
            return customer_names

        for item in rows:
            if not isinstance(item, dict):
                continue
            row_key = item.get("row")
            values = item.get("values")
            if not row_key or not isinstance(values, dict):
                continue
            fields = values.get("f")
            if not isinstance(fields, dict):
                continue
            customer_name = fields.get(CUSTOMER_NAME_FIELD)
            if customer_name:
                customer_names[row_key] = customer_name
        return customer_names

    async def _query_customer_names(
        self,
        custuids: set[str],
    ) -> dict[str, str]:
        """Batch query customer names by custuid."""
        if not CUSTOMER_NAME_QUERY_URL or not custuids:
            return {}

        row_to_custuid = {
            self._reverse_custuid(custuid): custuid
            for custuid in custuids
            if custuid
        }
        if not row_to_custuid:
            return {}

        try:
            async with httpx.AsyncClient(timeout=API_CALL_TIMEOUT) as client:
                response = await client.post(
                    CUSTOMER_NAME_QUERY_URL,
                    headers={"Content-Type": "application/json"},
                    json={"rows": sorted(row_to_custuid.keys())},
                )

            if response.status_code != 200:
                logger.warning(
                    "Customer name query failed: status=%d",
                    response.status_code,
                )
                return {}

            row_names = self._extract_customer_names(response.json())
            return {
                row_to_custuid[row]: name
                for row, name in row_names.items()
                if row in row_to_custuid
            }
        except Exception as e:
            logger.warning("Customer name query failed: %s", e)
            return {}

    async def _get_missing_customer_names(
        self,
        subtasks: list[dict],
    ) -> dict[str, str]:
        """Query names only for subtasks missing cust_nm."""
        custuids = {
            subtask.get("custuid") or ""
            for subtask in subtasks
            if not (subtask.get("cust_nm") or "").strip()
        }
        return await self._query_customer_names(custuids)

    async def _get_missing_customer_names_for_traces(
        self,
        subtasks_by_trace: dict[str, list[dict]],
    ) -> dict[str, str]:
        """Query customer names for all subtasks missing cust_nm."""
        custuids = {
            subtask.get("custuid") or ""
            for subtasks in subtasks_by_trace.values()
            for subtask in subtasks
            if not (subtask.get("cust_nm") or "").strip()
        }
        return await self._query_customer_names(custuids)

    def _build_result_index_rows_for_execution(
        self,
        execution: dict,
        subtasks: list[dict],
        skill_ids: list[str],
        customer_names: dict[str, str],
    ) -> list[dict]:
        """Build all result-index rows for one successful execution."""
        execution_at = (
            execution.get("actual_time")
            or execution.get("created_at")
            or datetime.now()
        )
        expire_at = execution_at + timedelta(days=30)
        return [
            self._build_result_index_row(
                execution,
                subtask,
                skill_id,
                execution_at,
                expire_at,
                customer_names,
            )
            for subtask in subtasks
            for skill_id in skill_ids
        ]

    @staticmethod
    def _is_valid_result_index_row(row: dict) -> bool:
        """Validate a result-index row before writing or pushing it."""
        missing_fields = [
            field
            for field in RESULT_INDEX_REQUIRED_FIELDS
            if not isinstance(row.get(field), str) or not row[field].strip()
        ]
        if missing_fields:
            logger.warning(
                "Skipped result-index row with missing required fields: "
                "execution_id=%s fields=%s",
                row.get("execution_id"),
                ",".join(missing_fields),
            )
            return False

        invalid_bbk_fields = [
            field
            for field in ("first_bbk_id", "bbk_org_id")
            if len(row[field].strip()) != 3
        ]
        if invalid_bbk_fields:
            logger.warning(
                "Skipped result-index row with invalid BBK id length: "
                "execution_id=%s fields=%s",
                row.get("execution_id"),
                ",".join(invalid_bbk_fields),
            )
            return False

        if row["result_type"] == "plan":
            missing_plan_fields = [
                field
                for field in ("custuid", "cust_nm")
                if not isinstance(row.get(field), str)
                or not row[field].strip()
            ]
            if missing_plan_fields:
                logger.warning(
                    "Skipped plan result-index row with missing customer data: "
                    "execution_id=%s fields=%s",
                    row.get("execution_id"),
                    ",".join(missing_plan_fields),
                )
                return False

        return True

    async def _build_result_index_users(
        self,
        rows: list[dict],
    ) -> list[dict[str, str]]:
        """Build push user info from successfully indexed rows."""
        first_bbk_ids = list(
            dict.fromkeys(
                row["first_bbk_id"] for row in rows if row.get("first_bbk_id")
            ),
        )
        if not first_bbk_ids:
            return []

        placeholders = ", ".join(["%s"] * len(first_bbk_ids))
        skill_config_rows = await self.db.fetch_all(
            f"""
                SELECT bbk_id, skill_id
                FROM swe_skill_config
                WHERE bbk_id IN ({placeholders})
                  AND customer_insight_enabled = 1
            """,
            tuple(first_bbk_ids),
        )
        enabled_skills = {
            (config["bbk_id"], config["skill_id"])
            for config in skill_config_rows
        }

        return [
            {"custUid": row["custuid"], "bbkId": row["bbk_org_id"]}
            for row in rows
            if row.get("custuid")
            and row.get("bbk_org_id")
            and (row["first_bbk_id"], row["skill_id"]) in enabled_skills
        ]

    async def _index_success_execution_results(
        self,
        executions: list[dict],
    ) -> tuple[int, list[dict[str, str]]]:
        """Write query-index rows for successful executions."""
        trace_ids = [
            execution.get("trace_id") or ""
            for execution in executions
            if execution.get("trace_id")
        ]
        subtasks_by_trace = await self._get_success_subtasks_for_traces(
            trace_ids,
        )
        customer_names = await self._get_missing_customer_names_for_traces(
            subtasks_by_trace,
        )

        result_rows: list[dict] = []
        for execution in executions:
            skill_ids = self._split_skill_ids(execution.get("skill_ids"))
            if not skill_ids:
                continue

            subtasks = subtasks_by_trace.get(
                execution.get("trace_id") or "",
                [],
            )
            if not subtasks:
                continue

            result_rows.extend(
                self._build_result_index_rows_for_execution(
                    execution,
                    subtasks,
                    skill_ids,
                    customer_names,
                ),
            )

        valid_result_rows = [
            row for row in result_rows if self._is_valid_result_index_row(row)
        ]
        await self._write_result_index_rows(valid_result_rows)
        return len(valid_result_rows), await self._build_result_index_users(
            valid_result_rows,
        )

    async def batch_update_execution_async_status(
        self,
    ) -> Tuple[int, int, int, list[dict[str, str]]]:
        """Batch update execution async_status using JOIN with subtasks.

        使用 SQL JOIN 批量更新，高效处理大量数据。
        同时更新 need_notification 字段：
        - 若不存在子任务则 need_notification = 1
        - 若存在子任务则按照 task_type='list' 的 need_notification 字段更新

        Returns:
            Tuple of (success_count, error_count, indexed_count, indexed_users)
        """
        if not self.db:
            return 0, 0, 0, []

        completed_executions = await self._get_completed_execution_candidates()
        if not completed_executions:
            return 0, 0, 0, []

        execution_ids = [
            execution["execution_id"] for execution in completed_executions
        ]
        execution_placeholders = ", ".join(["%s"] * len(execution_ids))

        # 更新 success：没有 pending 子任务且没有 error 子任务
        # 同时设置 need_notification：
        # - 无子任务时设为 1
        # - 有子任务时取 task_type='list' 的 need_notification 值
        success_query = f"""
            UPDATE swe_cron_executions e
            SET
                async_status = 'success',
                need_notification = COALESCE(
                    (
                        SELECT s.need_notification
                        FROM swe_cron_subtasks s
                        WHERE s.trace_id = e.trace_id
                        AND s.task_type = 'list'
                        LIMIT 1
                    ),
                    1
                )
            WHERE (e.async_status IS NULL OR e.async_status = '')
            AND e.status = 'success'
            AND e.id IN ({execution_placeholders})
            AND NOT EXISTS (
                SELECT 1 FROM swe_cron_subtasks s
                WHERE s.trace_id = e.trace_id
                AND (s.status IS NULL OR s.status = '' OR s.status IN ('FAIL', 'PART_SUC', 'TIMEOUT'))
            )
        """
        success_count = await self.db.execute(
            success_query,
            tuple(execution_ids),
        )

        # 更新 error：有 error 子任务且没有 pending 子任务
        # 同时设置 need_notification 同上逻辑
        error_query = f"""
            UPDATE swe_cron_executions e
            SET
                async_status = 'error',
                need_notification = COALESCE(
                    (
                        SELECT s.need_notification
                        FROM swe_cron_subtasks s
                        WHERE s.trace_id = e.trace_id
                        AND s.task_type = 'list'
                        LIMIT 1
                    ),
                    1
                )
            WHERE (e.async_status IS NULL OR e.async_status = '')
            AND e.id IN ({execution_placeholders})
            AND EXISTS (
                SELECT 1 FROM swe_cron_subtasks s
                WHERE s.trace_id = e.trace_id
                AND s.status IN ('FAIL', 'PART_SUC', 'TIMEOUT')
            )
            AND NOT EXISTS (
                SELECT 1 FROM swe_cron_subtasks s
                WHERE s.trace_id = e.trace_id
                AND (s.status IS NULL OR s.status = '')
            )
        """
        error_count = await self.db.execute(
            error_query,
            tuple(execution_ids),
        )
        (
            indexed_count,
            indexed_users,
        ) = await self._index_success_execution_results(
            completed_executions,
        )

        logger.info(
            "Batch updated execution async_status and need_notification: "
            "success=%d error=%d indexed=%d",
            success_count,
            error_count,
            indexed_count,
        )
        return success_count, error_count, indexed_count, indexed_users


# Global service instance
_query_service: Optional[QueryService] = None


def get_query_service() -> QueryService:
    """Get the global QueryService instance.

    Returns:
        QueryService instance
    """
    global _query_service
    if _query_service is None:
        db = get_db_connection()
        _query_service = QueryService(db=db)
    return _query_service
