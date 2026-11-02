# -*- coding: utf-8 -*-
"""HTML 预览点击统计数据库存储。"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .models import (
    HtmlPreviewClickEventCreate,
    HtmlPreviewClickEventItem,
    HtmlPreviewClickSummaryItem,
    HtmlPreviewEventType,
    HtmlPreviewTemplateType,
    HtmlPreviewCustomerClickItem,
    HtmlPreviewCustomerClickSummaryItem,
    HtmlPreviewListSnapshotCreate,
    HtmlPreviewListSummaryItem,
    HtmlPreviewListSummaryResponse,
)

CUSTOMER_SUMMARY_SCAN_LIMIT = 10000
DB_TIME_OFFSET_HOURS = 8
DB_TIMEZONE = timezone(timedelta(hours=DB_TIME_OFFSET_HOURS))


class HtmlPreviewClickStore:
    """负责 HTML 预览点击事件、名单快照的落库与聚合查询。"""

    def __init__(self, db: Optional[Any] = None):
        """初始化点击统计存储。

        Args:
            db: 已连接的数据库对象
        """
        self.db = db
        self._use_db = db is not None and db.is_connected

    @staticmethod
    def _clean_text(value: Optional[str]) -> Optional[str]:
        """清理空字符串，避免聚合字段里混入无意义值。"""
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        return stripped or None

    @staticmethod
    def _shift_to_db_timezone(value: Optional[datetime]) -> datetime:
        """把入库时间统一转换为数据库使用的东八区时间。"""
        if value is None:
            return datetime.now()
        if value.tzinfo is None:
            return value + timedelta(hours=DB_TIME_OFFSET_HOURS)
        return value.astimezone(DB_TIMEZONE).replace(tzinfo=None)

    @classmethod
    def _list_key(cls, file_url: str, list_key: Optional[str]) -> str:
        """统一名单主键，默认用文件 URL 兼容老数据。"""
        return cls._clean_text(list_key) or file_url

    @classmethod
    def _list_name(
        cls,
        file_name: Optional[str],
        list_name: Optional[str],
        file_url: Optional[str] = None,
    ) -> str:
        """统一名单展示名，优先使用显式名单名。"""
        return (
            cls._clean_text(list_name)
            or cls._clean_text(file_name)
            or cls._clean_text(file_url)
            or "未知名单"
        )

    @staticmethod
    def _encode_json(value: Optional[dict[str, str]]) -> Optional[str]:
        """把扩展信息序列化为 JSON。

        避免数据库驱动差异影响写入。
        """
        if not value:
            return None
        cleaned = {
            str(key).strip(): str(item).strip()
            for key, item in value.items()
            if str(key).strip() and str(item).strip()
        }
        return json.dumps(cleaned, ensure_ascii=False) if cleaned else None

    @staticmethod
    def _decode_json(value: Any) -> Optional[dict[str, str]]:
        """解析数据库中的 JSON 扩展字段。"""
        if not value:
            return None
        if isinstance(value, dict):
            return {str(key): str(item) for key, item in value.items()}
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        if not isinstance(parsed, dict):
            return None
        return {str(key): str(item) for key, item in parsed.items()}

    @staticmethod
    def _encode_customer_info(
        value: Optional[dict[str, str]],
    ) -> Optional[str]:
        """序列化点击事件里的客户扩展信息。"""
        return HtmlPreviewClickStore._encode_json(value)

    @staticmethod
    def _decode_customer_info(value: Any) -> Optional[dict[str, str]]:
        """解析点击事件里的客户扩展信息。"""
        return HtmlPreviewClickStore._decode_json(value)

    @classmethod
    def _get_customer_identity(
        cls,
        customer_info: Any,
        customer_id: Optional[str] = None,
        customer_name: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        """从结构化字段和扩展信息中提取客户 ID 与展示姓名。"""
        info = cls._decode_customer_info(customer_info) or {}
        resolved_id = cls._clean_text(customer_id) or cls._clean_text(
            info.get("customer_id"),
        )
        resolved_name = (
            cls._clean_text(customer_name)
            or cls._clean_text(info.get("name"))
            or cls._clean_text(info.get("customer_name"))
            or cls._clean_text(info.get("客户姓名"))
            or cls._clean_text(info.get("客户名称"))
            or cls._clean_text(info.get("姓名"))
        )
        return resolved_id, resolved_name

    @classmethod
    def _classify_button(cls, row: dict[str, Any]) -> str:
        """把按钮点击归类为洞察、电访、查看方案或其他。"""
        explicit = cls._clean_text(row.get("button_type"))
        if explicit in {"insight", "phone", "plan", "other"}:
            return explicit

        button_id = (cls._clean_text(row.get("button_id")) or "").lower()
        button_name = cls._clean_text(row.get("button_name")) or ""
        button_text = cls._clean_text(row.get("button_text")) or ""
        if (
            "plan" in button_id
            or "查看方案" in button_name
            or "查看方案" in button_text
        ):
            return "plan"
        if (
            "phone" in button_id
            or "电访" in button_name
            or "电访" in button_text
        ):
            return "phone"
        if (
            "insight" in button_id
            or "洞察" in button_name
            or "洞察" in button_text
        ):
            return "insight"
        return "other"

    @classmethod
    def _normalize_event(
        cls,
        event: HtmlPreviewClickEventCreate,
    ) -> dict[str, Any]:
        """补齐点击事件的核心分析字段。"""
        customer_id, customer_name = cls._get_customer_identity(
            event.customer_info,
            event.customer_id,
            event.customer_name,
        )
        list_key = cls._list_key(event.file_url, event.list_key)
        list_name = cls._list_name(
            event.file_name,
            event.list_name,
            event.file_url,
        )
        button_type = None
        if event.event_type == "button_click":
            button_type = cls._classify_button(
                {
                    "button_type": event.button_type,
                    "button_id": event.button_id,
                    "button_name": event.button_name,
                    "button_text": event.button_text,
                },
            )
        return {
            "list_key": list_key,
            "list_name": list_name,
            "button_type": button_type,
            "customer_id": customer_id,
            "customer_name": customer_name,
        }

    @staticmethod
    def _to_summary_item(row: dict[str, Any]) -> HtmlPreviewClickSummaryItem:
        """把数据库行转换为点击聚合模型。"""
        return HtmlPreviewClickSummaryItem(
            button_label=row.get("button_label") or "未知按钮",
            button_id=row.get("button_id"),
            button_name=row.get("button_name"),
            button_text=row.get("button_text"),
            bbk_id=row.get("bbk_id"),
            cron_task_id=row.get("cron_task_id"),
            cron_task_name=row.get("cron_task_name"),
            list_key=row.get("list_key"),
            list_name=row.get("list_name"),
            file_url=row.get("file_url"),
            file_name=row.get("file_name"),
            click_count=int(row.get("click_count") or 0),
            last_clicked_at=row.get("last_clicked_at"),
        )

    @staticmethod
    def _to_customer_summary_item(
        row: dict[str, Any],
    ) -> HtmlPreviewCustomerClickSummaryItem:
        """把数据库行转换为客户维度点击聚合模型。"""
        return HtmlPreviewCustomerClickSummaryItem(
            customer_id=row.get("customer_id"),
            customer_name=row.get("customer_name") or "未知客户",
            insight_count=int(row.get("insight_count") or 0),
            phone_count=int(row.get("phone_count") or 0),
            plan_count=int(row.get("plan_count") or 0),
            total_click_count=int(row.get("total_click_count") or 0),
            last_clicked_user_id=row.get("last_clicked_user_id"),
            last_clicked_user_name=row.get("last_clicked_user_name"),
            last_clicked_at=row.get("last_clicked_at"),
        )

    @classmethod
    def _to_event_item(cls, row: dict[str, Any]) -> HtmlPreviewClickEventItem:
        """把数据库行转换为点击明细模型。"""
        customer_id, customer_name = cls._get_customer_identity(
            row.get("customer_info"),
            row.get("customer_id"),
            row.get("customer_name"),
        )
        return HtmlPreviewClickEventItem(
            id=int(row.get("id") or 0),
            source_id=row.get("source_id"),
            user_id=row.get("user_id"),
            user_name=row.get("user_name"),
            bbk_id=row.get("bbk_id"),
            cron_task_id=row.get("cron_task_id"),
            cron_task_name=row.get("cron_task_name"),
            file_url=row.get("file_url") or "",
            file_name=row.get("file_name"),
            list_key=row.get("list_key"),
            list_name=row.get("list_name"),
            button_id=row.get("button_id"),
            button_name=row.get("button_name"),
            button_text=row.get("button_text"),
            button_type=row.get("button_type"),
            customer_id=customer_id,
            customer_name=customer_name,
            customer_info=cls._decode_customer_info(row.get("customer_info")),
            clicked_at=row.get("clicked_at"),
            event_type=row.get("event_type") or "button_click",
            template_type=row.get("template_type"),
            template_id=row.get("template_id"),
            result_id=row.get("result_id"),
            event_target_id=row.get("event_target_id"),
            event_target_name=row.get("event_target_name"),
            trace_id=row.get("trace_id"),
            page_source=row.get("page_source"),
            platform_source=row.get("platform_source"),
        )

    async def create_event(
        self,
        event: HtmlPreviewClickEventCreate,
    ) -> None:
        """保存一条 HTML 预览行为事件明细。"""
        if not self._use_db:
            return

        normalized = self._normalize_event(event)
        query = """
            INSERT INTO swe_html_preview_click_events (
                source_id,
                user_id,
                user_name,
                bbk_id,
                cron_task_id,
                cron_task_name,
                file_url,
                file_name,
                list_key,
                list_name,
                button_id,
                button_name,
                button_text,
                button_type,
                customer_id,
                customer_name,
                customer_info,
                clicked_at,
                event_type,
                template_type,
                template_id,
                result_id,
                event_target_id,
                event_target_name,
                trace_id,
                page_source,
                platform_source
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
        """
        await self.db.execute(
            query,
            (
                self._clean_text(event.source_id),
                self._clean_text(event.user_id),
                self._clean_text(event.user_name),
                self._clean_text(event.bbk_id),
                self._clean_text(event.cron_task_id),
                self._clean_text(event.cron_task_name),
                event.file_url,
                self._clean_text(event.file_name),
                normalized["list_key"],
                normalized["list_name"],
                self._clean_text(event.button_id),
                self._clean_text(event.button_name),
                self._clean_text(event.button_text),
                normalized["button_type"],
                normalized["customer_id"],
                normalized["customer_name"],
                self._encode_customer_info(event.customer_info),
                self._shift_to_db_timezone(event.clicked_at),
                event.event_type,
                event.template_type,
                event.template_id,
                self._clean_text(event.result_id),
                self._clean_text(event.event_target_id),
                self._clean_text(event.event_target_name),
                self._clean_text(event.trace_id),
                self._clean_text(event.page_source),
                self._clean_text(event.platform_source),
            ),
        )

    async def create_list_snapshot(
        self,
        snapshot: HtmlPreviewListSnapshotCreate,
    ) -> int:
        """保存一份 HTML 名单客户快照。"""
        if not self._use_db:
            return 0

        list_key = self._list_key(snapshot.file_url, snapshot.list_key)
        list_name = self._list_name(
            snapshot.file_name,
            snapshot.list_name,
            snapshot.file_url,
        )
        snapshot_at = self._shift_to_db_timezone(snapshot.snapshot_at)
        delete_query = """
            DELETE FROM swe_html_preview_list_snapshots
            WHERE source_id <=> %s AND bbk_id <=> %s AND list_key = %s
        """
        await self.db.execute(
            delete_query,
            (
                self._clean_text(snapshot.source_id),
                self._clean_text(snapshot.bbk_id),
                list_key,
            ),
        )

        insert_query = """
            INSERT INTO swe_html_preview_list_snapshots (
                source_id,
                bbk_id,
                cron_task_id,
                cron_task_name,
                list_key,
                list_name,
                file_url,
                file_name,
                customer_id,
                customer_name,
                extra_info,
                snapshot_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        inserted = 0
        seen: set[str] = set()
        for customer in snapshot.customers:
            customer_id = self._clean_text(customer.customer_id)
            customer_name = self._clean_text(customer.customer_name)
            if not customer_id and not customer_name:
                continue
            unique_key = customer_id or f"name:{customer_name}"
            if unique_key in seen:
                continue
            seen.add(unique_key)
            await self.db.execute(
                insert_query,
                (
                    self._clean_text(snapshot.source_id),
                    self._clean_text(snapshot.bbk_id),
                    self._clean_text(snapshot.cron_task_id),
                    self._clean_text(snapshot.cron_task_name),
                    list_key,
                    list_name,
                    snapshot.file_url,
                    self._clean_text(snapshot.file_name),
                    customer_id,
                    customer_name or "未知客户",
                    self._encode_json(customer.extra_info),
                    snapshot_at,
                ),
            )
            inserted += 1
        return inserted

    async def list_events(
        self,
        *,
        source_id: Optional[str],
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        bbk_ids: Optional[list[str]] = None,
        cron_task_id: Optional[str] = None,
        file_url: Optional[str] = None,
        list_key: Optional[str] = None,
        event_type: Optional[HtmlPreviewEventType] = "button_click",
        template_type: Optional[HtmlPreviewTemplateType] = None,
        template_id: Optional[int] = None,
        result_id: Optional[str] = None,
        event_target_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[HtmlPreviewClickEventItem]:
        """查询 HTML 预览行为事件明细。"""
        if not self._use_db:
            return []

        where_sql, params = self._build_event_where_clause(
            source_id=source_id,
            start_time=start_time,
            end_time=end_time,
            bbk_ids=bbk_ids,
            cron_task_id=cron_task_id,
            file_url=file_url,
            list_key=list_key,
            event_type=event_type,
            template_type=template_type,
            template_id=template_id,
            result_id=result_id,
            event_target_id=event_target_id,
            trace_id=trace_id,
        )
        safe_limit = max(1, min(limit, 200))
        safe_offset = max(0, offset)
        query = f"""
            SELECT
                id,
                source_id,
                user_id,
                user_name,
                bbk_id,
                cron_task_id,
                cron_task_name,
                file_url,
                file_name,
                list_key,
                list_name,
                button_id,
                button_name,
                button_text,
                button_type,
                customer_id,
                customer_name,
                customer_info,
                event_type,
                template_type,
                template_id,
                result_id,
                event_target_id,
                event_target_name,
                trace_id,
                page_source,
                platform_source,
                clicked_at
            FROM swe_html_preview_click_events
            {where_sql}
            ORDER BY clicked_at DESC, id DESC
            LIMIT {safe_limit} OFFSET {safe_offset}
        """
        rows = await self.db.fetch_all(query, tuple(params))
        return [self._to_event_item(row) for row in rows]

    async def list_summary(
        self,
        *,
        source_id: Optional[str],
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        bbk_ids: Optional[list[str]] = None,
        cron_task_id: Optional[str] = None,
        file_url: Optional[str] = None,
        list_key: Optional[str] = None,
        limit: int = 100,
    ) -> list[HtmlPreviewClickSummaryItem]:
        """按按钮、任务和 HTML 文件聚合点击次数。"""
        if not self._use_db:
            return []

        where_sql, params = self._build_event_where_clause(
            source_id=source_id,
            start_time=start_time,
            end_time=end_time,
            bbk_ids=bbk_ids,
            cron_task_id=cron_task_id,
            file_url=file_url,
            list_key=list_key,
            event_type="button_click",
        )
        safe_limit = max(1, min(limit, 200))

        query = f"""
            SELECT
                COALESCE(
                    NULLIF(button_name, ''),
                    NULLIF(button_text, ''),
                    NULLIF(button_id, ''),
                    '未知按钮'
                ) AS button_label,
                button_id,
                button_name,
                button_text,
                cron_task_id,
                cron_task_name,
                list_key,
                list_name,
                file_url,
                file_name,
                COUNT(*) AS click_count,
                MAX(clicked_at) AS last_clicked_at
            FROM swe_html_preview_click_events
            {where_sql}
            GROUP BY
                button_id,
                button_name,
                button_text,
                cron_task_id,
                cron_task_name,
                list_key,
                list_name,
                file_url,
                file_name
            ORDER BY click_count DESC, last_clicked_at DESC
            LIMIT {safe_limit}
        """
        rows = await self.db.fetch_all(query, tuple(params))
        return [self._to_summary_item(row) for row in rows]

    async def list_customer_summary(
        self,
        *,
        source_id: Optional[str],
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        bbk_ids: Optional[list[str]] = None,
        cron_task_id: Optional[str] = None,
        file_url: Optional[str] = None,
        list_key: Optional[str] = None,
        limit: int = 100,
    ) -> list[HtmlPreviewCustomerClickSummaryItem]:
        """按客户聚合洞察和电访按钮点击次数。"""
        items = await self.list_customer_clicks(
            source_id=source_id,
            start_time=start_time,
            end_time=end_time,
            bbk_ids=bbk_ids,
            cron_task_id=cron_task_id,
            file_url=file_url,
            list_key=list_key,
            include_unclicked=False,
            limit=limit,
        )
        return [
            HtmlPreviewCustomerClickSummaryItem(
                customer_id=item.customer_id,
                customer_name=item.customer_name,
                insight_count=item.insight_count,
                phone_count=item.phone_count,
                plan_count=item.plan_count,
                total_click_count=item.total_click_count,
                last_clicked_user_id=item.last_clicked_user_id,
                last_clicked_user_name=item.last_clicked_user_name,
                last_clicked_at=item.last_clicked_at,
            )
            for item in items
        ]

    async def list_lists(
        self,
        *,
        source_id: Optional[str],
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        bbk_ids: Optional[list[str]] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> HtmlPreviewListSummaryResponse:
        """查询名单维度统计。"""
        if not self._use_db:
            return self._empty_list_page(page=page, page_size=page_size)

        snapshot_rows = await self._fetch_snapshot_list_summary_rows(
            source_id=source_id,
            bbk_ids=bbk_ids,
        )
        event_rows = await self._fetch_event_list_summary_rows(
            source_id=source_id,
            start_time=start_time,
            end_time=end_time,
            bbk_ids=bbk_ids,
        )
        customer_rows = await self._fetch_list_customer_union_rows(
            source_id=source_id,
            start_time=start_time,
            end_time=end_time,
            bbk_ids=bbk_ids,
        )
        grouped = self._build_list_summary_from_aggregates(
            snapshot_rows,
            event_rows,
            customer_rows,
        )
        safe_page = max(1, page)
        safe_page_size = max(1, min(page_size, 100))
        start = (safe_page - 1) * safe_page_size
        end = start + safe_page_size
        return HtmlPreviewListSummaryResponse(
            success=True,
            total=len(grouped),
            clicked_list_count=sum(
                1 for item in grouped if item.total_click_count > 0
            ),
            page=safe_page,
            page_size=safe_page_size,
            summary=self._build_list_summary_total(grouped),
            items=grouped[start:end],
        )

    async def list_customer_clicks(
        self,
        *,
        source_id: Optional[str],
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        bbk_ids: Optional[list[str]] = None,
        cron_task_id: Optional[str] = None,
        file_url: Optional[str] = None,
        list_key: Optional[str] = None,
        include_unclicked: bool = False,
        limit: int = 100,
    ) -> list[HtmlPreviewCustomerClickItem]:
        """查询客户维度洞察和电访点击明细。"""
        if not self._use_db:
            return []

        snapshot_rows = []
        if include_unclicked:
            snapshot_rows = await self._fetch_snapshot_rows(
                source_id=source_id,
                bbk_ids=bbk_ids,
                list_key=list_key,
            )
        event_rows = await self._fetch_event_rows(
            source_id=source_id,
            start_time=start_time,
            end_time=end_time,
            bbk_ids=bbk_ids,
            cron_task_id=cron_task_id,
            file_url=file_url,
            list_key=list_key,
        )
        rows = self._build_customer_click_items(
            snapshot_rows=snapshot_rows,
            event_rows=event_rows,
            include_unclicked=include_unclicked,
        )
        return rows[: max(1, min(limit, 500))]

    async def _fetch_event_rows(
        self,
        *,
        source_id: Optional[str],
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        bbk_ids: Optional[list[str]],
        cron_task_id: Optional[str],
        file_url: Optional[str],
        list_key: Optional[str],
    ) -> list[dict[str, Any]]:
        """查询用于应用层聚合的点击明细。"""
        where_sql, params = self._build_event_where_clause(
            source_id=source_id,
            start_time=start_time,
            end_time=end_time,
            bbk_ids=bbk_ids,
            cron_task_id=cron_task_id,
            file_url=file_url,
            list_key=list_key,
            event_type="button_click",
        )
        query = f"""
            SELECT
                id,
                user_id,
                user_name,
                cron_task_id,
                cron_task_name,
                file_url,
                file_name,
                list_key,
                list_name,
                button_id,
                button_name,
                button_text,
                button_type,
                customer_id,
                customer_name,
                customer_info,
                clicked_at
            FROM swe_html_preview_click_events
            {where_sql}
            ORDER BY clicked_at DESC, id DESC
            LIMIT {CUSTOMER_SUMMARY_SCAN_LIMIT}
        """
        return await self.db.fetch_all(query, tuple(params))

    async def _fetch_event_list_summary_rows(
        self,
        *,
        source_id: Optional[str],
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        bbk_ids: Optional[list[str]],
    ) -> list[dict[str, Any]]:
        """按名单聚合点击事件，避免截断明细行后再统计。"""
        where_sql, params = self._build_event_where_clause(
            source_id=source_id,
            start_time=start_time,
            end_time=end_time,
            bbk_ids=bbk_ids,
            cron_task_id=None,
            file_url=None,
            list_key=None,
            event_type="button_click",
        )
        list_key_expr = self._sql_list_key_expr()
        # 无文件地址且无名单标识的工作台点击不属于 HTML 名单。
        where_sql = self._append_sql_condition(
            where_sql,
            f"{list_key_expr} <> ''",
        )
        customer_key_expr = self._sql_event_customer_key_expr()
        button_type_expr = self._sql_button_type_expr()
        valid_click_condition = (
            f"{button_type_expr} IN ('insight', 'phone', 'plan')"
        )
        query = f"""
            SELECT
                {list_key_expr} AS list_key,
                COALESCE(
                    NULLIF(MAX(list_name), ''),
                    NULLIF(MAX(file_name), ''),
                    NULLIF(MAX(file_url), ''),
                    '未知名单'
                ) AS list_name,
                MAX(file_url) AS file_url,
                MAX(file_name) AS file_name,
                MAX(cron_task_id) AS cron_task_id,
                MAX(cron_task_name) AS cron_task_name,
                COUNT(
                    DISTINCT CASE
                        WHEN {valid_click_condition}
                        THEN {customer_key_expr}
                        ELSE NULL
                    END
                ) AS clicked_customer_count,
                SUM(CASE WHEN {button_type_expr} = 'insight' THEN 1 ELSE 0 END)
                    AS insight_count,
                SUM(CASE WHEN {button_type_expr} = 'phone' THEN 1 ELSE 0 END)
                    AS phone_count,
                SUM(CASE WHEN {button_type_expr} = 'plan' THEN 1 ELSE 0 END)
                    AS plan_count,
                SUM(
                    CASE
                        WHEN {button_type_expr} IN ('insight', 'phone', 'plan')
                        THEN 1
                        ELSE 0
                    END
                ) AS total_click_count,
                MAX(
                    CASE
                        WHEN {valid_click_condition}
                        THEN clicked_at
                        ELSE NULL
                    END
                ) AS last_clicked_at
            FROM swe_html_preview_click_events
            {where_sql}
            GROUP BY {list_key_expr}
        """
        return await self.db.fetch_all(query, tuple(params))

    async def _fetch_snapshot_rows(
        self,
        *,
        source_id: Optional[str],
        bbk_ids: Optional[list[str]],
        list_key: Optional[str],
    ) -> list[dict[str, Any]]:
        """查询名单客户快照。"""
        where_sql, params = self._build_snapshot_where_clause(
            source_id=source_id,
            bbk_ids=bbk_ids,
            list_key=list_key,
        )
        query = f"""
            SELECT
                source_id,
                bbk_id,
                cron_task_id,
                cron_task_name,
                list_key,
                list_name,
                file_url,
                file_name,
                customer_id,
                customer_name,
                extra_info,
                snapshot_at
            FROM swe_html_preview_list_snapshots
            {where_sql}
            ORDER BY snapshot_at DESC
            LIMIT {CUSTOMER_SUMMARY_SCAN_LIMIT}
        """
        return await self.db.fetch_all(query, tuple(params))

    async def _fetch_snapshot_list_summary_rows(
        self,
        *,
        source_id: Optional[str],
        bbk_ids: Optional[list[str]],
    ) -> list[dict[str, Any]]:
        """按名单聚合客户快照，避免扫描客户明细后再分页。"""
        where_sql, params = self._build_snapshot_where_clause(
            source_id=source_id,
            bbk_ids=bbk_ids,
            list_key=None,
        )
        list_key_expr = self._sql_list_key_expr()
        customer_key_expr = self._sql_snapshot_customer_key_expr()
        query = f"""
            SELECT
                {list_key_expr} AS list_key,
                COALESCE(
                    NULLIF(MAX(list_name), ''),
                    NULLIF(MAX(file_name), ''),
                    NULLIF(MAX(file_url), ''),
                    '未知名单'
                ) AS list_name,
                MAX(file_url) AS file_url,
                MAX(file_name) AS file_name,
                MAX(cron_task_id) AS cron_task_id,
                MAX(cron_task_name) AS cron_task_name,
                COUNT(DISTINCT {customer_key_expr}) AS customer_count
            FROM swe_html_preview_list_snapshots
            {where_sql}
            GROUP BY {list_key_expr}
        """
        return await self.db.fetch_all(query, tuple(params))

    async def _fetch_list_customer_union_rows(
        self,
        *,
        source_id: Optional[str],
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        bbk_ids: Optional[list[str]],
    ) -> list[dict[str, Any]]:
        """按名单统计快照客户与有效点击客户的并集数量。"""
        snapshot_where_sql, snapshot_params = (
            self._build_snapshot_where_clause(
                source_id=source_id,
                bbk_ids=bbk_ids,
                list_key=None,
            )
        )
        event_where_sql, event_params = self._build_event_where_clause(
            source_id=source_id,
            start_time=start_time,
            end_time=end_time,
            bbk_ids=bbk_ids,
            cron_task_id=None,
            file_url=None,
            list_key=None,
            event_type="button_click",
        )
        list_key_expr = self._sql_list_key_expr()
        snapshot_customer_key_expr = self._sql_snapshot_customer_key_expr()
        event_customer_key_expr = self._sql_event_customer_key_expr()
        button_type_expr = self._sql_button_type_expr()
        event_where_with_valid_clicks = self._append_sql_condition(
            event_where_sql,
            f"{button_type_expr} IN ('insight', 'phone', 'plan')",
        )
        query = f"""
            SELECT
                merged.list_key,
                COUNT(DISTINCT merged.customer_key) AS customer_count
            FROM (
                SELECT
                    {list_key_expr} AS list_key,
                    {snapshot_customer_key_expr} AS customer_key
                FROM swe_html_preview_list_snapshots
                {snapshot_where_sql}
                UNION
                SELECT
                    {list_key_expr} AS list_key,
                    {event_customer_key_expr} AS customer_key
                FROM swe_html_preview_click_events
                {event_where_with_valid_clicks}
            ) AS merged
            WHERE merged.list_key IS NOT NULL
                AND merged.list_key <> ''
                AND merged.customer_key IS NOT NULL
            GROUP BY merged.list_key
        """
        return await self.db.fetch_all(
            query,
            tuple(snapshot_params + event_params),
        )

    @staticmethod
    def _append_sql_condition(where_sql: str, condition: str) -> str:
        """在已有 WHERE 片段后追加一个 AND 条件。"""
        if where_sql.strip():
            return f"{where_sql} AND {condition}"
        return f"WHERE {condition}"

    @staticmethod
    def _sql_list_key_expr() -> str:
        """名单 SQL 去重键，保留旧数据 file_url 兜底。"""
        return "COALESCE(NULLIF(list_key, ''), file_url)"

    @staticmethod
    def _sql_snapshot_customer_key_expr() -> str:
        """名单快照客户 SQL 去重键，保留姓名兜底。"""
        return """
            COALESCE(
                NULLIF(customer_id, ''),
                CONCAT('name:', COALESCE(NULLIF(customer_name, ''), '未知客户'))
            )
        """

    @staticmethod
    def _sql_event_customer_key_expr() -> str:
        """点击事件客户 SQL 去重键，兼容结构字段和 JSON 字段。"""
        return """
            COALESCE(
                NULLIF(customer_id, ''),
                NULLIF(
                    JSON_UNQUOTE(JSON_EXTRACT(customer_info, '$.customer_id')),
                    ''
                ),
                CASE
                    WHEN COALESCE(
                        NULLIF(customer_name, ''),
                        NULLIF(
                            JSON_UNQUOTE(JSON_EXTRACT(customer_info, '$.name')),
                            ''
                        ),
                        NULLIF(
                            JSON_UNQUOTE(
                                JSON_EXTRACT(customer_info, '$.customer_name')
                            ),
                            ''
                        ),
                        NULLIF(
                            JSON_UNQUOTE(
                                JSON_EXTRACT(customer_info, '$."客户姓名"')
                            ),
                            ''
                        ),
                        NULLIF(
                            JSON_UNQUOTE(
                                JSON_EXTRACT(customer_info, '$."客户名称"')
                            ),
                            ''
                        ),
                        NULLIF(
                            JSON_UNQUOTE(
                                JSON_EXTRACT(customer_info, '$."姓名"')
                            ),
                            ''
                        )
                    ) IS NOT NULL THEN CONCAT(
                        'name:',
                        COALESCE(
                            NULLIF(customer_name, ''),
                            NULLIF(
                                JSON_UNQUOTE(
                                    JSON_EXTRACT(customer_info, '$.name')
                                ),
                                ''
                            ),
                            NULLIF(
                                JSON_UNQUOTE(
                                    JSON_EXTRACT(
                                        customer_info,
                                        '$.customer_name'
                                    )
                                ),
                                ''
                            ),
                            NULLIF(
                                JSON_UNQUOTE(
                                    JSON_EXTRACT(
                                        customer_info,
                                        '$."客户姓名"'
                                    )
                                ),
                                ''
                            ),
                            NULLIF(
                                JSON_UNQUOTE(
                                    JSON_EXTRACT(
                                        customer_info,
                                        '$."客户名称"'
                                    )
                                ),
                                ''
                            ),
                            NULLIF(
                                JSON_UNQUOTE(
                                    JSON_EXTRACT(customer_info, '$."姓名"')
                                ),
                                ''
                            )
                        )
                    )
                    ELSE NULL
                END
            )
        """

    @staticmethod
    def _sql_button_type_expr() -> str:
        """点击类型 SQL 归类，兼容旧数据缺失 button_type 的情况。"""
        return """
            CASE
                WHEN button_type IN ('insight', 'phone', 'plan', 'other')
                    THEN button_type
                WHEN LOWER(COALESCE(button_id, '')) LIKE '%%plan%%'
                    OR COALESCE(button_name, '') LIKE '%%查看方案%%'
                    OR COALESCE(button_text, '') LIKE '%%查看方案%%'
                    THEN 'plan'
                WHEN LOWER(COALESCE(button_id, '')) LIKE '%%phone%%'
                    OR COALESCE(button_name, '') LIKE '%%电访%%'
                    OR COALESCE(button_text, '') LIKE '%%电访%%'
                    THEN 'phone'
                WHEN LOWER(COALESCE(button_id, '')) LIKE '%%insight%%'
                    OR COALESCE(button_name, '') LIKE '%%洞察%%'
                    OR COALESCE(button_text, '') LIKE '%%洞察%%'
                    THEN 'insight'
                ELSE 'other'
            END
        """

    @classmethod
    def _build_list_summary(
        cls,
        snapshot_rows: list[dict[str, Any]],
        event_rows: list[dict[str, Any]],
    ) -> list[HtmlPreviewListSummaryItem]:
        """组合名单快照和点击事件，生成名单维度统计。"""
        grouped: dict[str, dict[str, Any]] = {}
        for row in snapshot_rows:
            list_key = cls._list_key(
                row.get("file_url") or "",
                row.get("list_key"),
            )
            item = grouped.setdefault(
                list_key,
                {
                    "list_key": list_key,
                    "list_name": cls._list_name(
                        row.get("file_name"),
                        row.get("list_name"),
                        row.get("file_url"),
                    ),
                    "file_url": row.get("file_url"),
                    "file_name": row.get("file_name"),
                    "cron_task_id": row.get("cron_task_id"),
                    "cron_task_name": row.get("cron_task_name"),
                    "customers": set(),
                    "clicked_customers": set(),
                    "insight_count": 0,
                    "phone_count": 0,
                    "plan_count": 0,
                    "total_click_count": 0,
                    "last_clicked_at": None,
                },
            )
            customer_key = (
                row.get("customer_id") or f"name:{row.get('customer_name')}"
            )
            if customer_key:
                item["customers"].add(customer_key)

        for row in event_rows:
            list_key = cls._list_key(
                row.get("file_url") or "",
                row.get("list_key"),
            )
            item = grouped.setdefault(
                list_key,
                {
                    "list_key": list_key,
                    "list_name": cls._list_name(
                        row.get("file_name"),
                        row.get("list_name"),
                        row.get("file_url"),
                    ),
                    "file_url": row.get("file_url"),
                    "file_name": row.get("file_name"),
                    "cron_task_id": row.get("cron_task_id"),
                    "cron_task_name": row.get("cron_task_name"),
                    "customers": set(),
                    "clicked_customers": set(),
                    "insight_count": 0,
                    "phone_count": 0,
                    "plan_count": 0,
                    "total_click_count": 0,
                    "last_clicked_at": None,
                },
            )
            cls._apply_event_to_group(item, row)

        items = [
            HtmlPreviewListSummaryItem(
                list_key=row["list_key"],
                list_name=row["list_name"],
                file_url=row.get("file_url"),
                file_name=row.get("file_name"),
                cron_task_id=row.get("cron_task_id"),
                cron_task_name=row.get("cron_task_name"),
                customer_count=len(row["customers"]),
                clicked_customer_count=len(row["clicked_customers"]),
                insight_count=row["insight_count"],
                phone_count=row["phone_count"],
                plan_count=row["plan_count"],
                total_click_count=row["total_click_count"],
                last_clicked_at=row.get("last_clicked_at"),
            )
            for row in grouped.values()
        ]
        return sorted(
            items,
            key=lambda item: (
                item.total_click_count,
                item.last_clicked_at or datetime.min,
            ),
            reverse=True,
        )

    @classmethod
    def _build_list_summary_from_aggregates(
        cls,
        snapshot_rows: list[dict[str, Any]],
        event_rows: list[dict[str, Any]],
        customer_rows: list[dict[str, Any]],
    ) -> list[HtmlPreviewListSummaryItem]:
        """组合名单级聚合行，生成名单维度统计。"""
        grouped: dict[str, dict[str, Any]] = {}
        for row in snapshot_rows:
            item = cls._empty_list_summary_aggregate_group(
                row,
                customer_count=int(row.get("customer_count") or 0),
            )
            grouped[item["list_key"]] = item

        for row in event_rows:
            list_key = cls._list_key(
                row.get("file_url") or "",
                row.get("list_key"),
            )
            item = grouped.setdefault(
                list_key,
                cls._empty_list_summary_aggregate_group(row),
            )
            cls._apply_event_aggregate_to_list_summary_group(item, row)

        for row in customer_rows:
            cls._apply_union_customer_count_to_list_summary_grouped(
                grouped,
                row,
            )

        return cls._list_summary_items_from_aggregate_groups(
            grouped.values(),
        )

    @classmethod
    def _empty_list_summary_aggregate_group(
        cls,
        row: dict[str, Any],
        *,
        customer_count: int = 0,
    ) -> dict[str, Any]:
        """创建名单聚合空行，并保留当前行可用的展示字段。"""
        list_key = cls._list_key(
            row.get("file_url") or "",
            row.get("list_key"),
        )
        return {
            "list_key": list_key,
            "list_name": cls._list_name(
                row.get("file_name"),
                row.get("list_name"),
                row.get("file_url"),
            ),
            "file_url": row.get("file_url"),
            "file_name": row.get("file_name"),
            "cron_task_id": row.get("cron_task_id"),
            "cron_task_name": row.get("cron_task_name"),
            "customer_count": customer_count,
            "clicked_customer_count": 0,
            "insight_count": 0,
            "phone_count": 0,
            "plan_count": 0,
            "total_click_count": 0,
            "last_clicked_at": None,
        }

    @classmethod
    def _apply_event_aggregate_to_list_summary_group(
        cls,
        item: dict[str, Any],
        row: dict[str, Any],
    ) -> None:
        """把名单聚合查询结果合并到当前名单行。"""
        clicked_customer_count = int(row.get("clicked_customer_count") or 0)
        item["customer_count"] = max(
            int(item.get("customer_count") or 0),
            clicked_customer_count,
        )
        item["clicked_customer_count"] = clicked_customer_count
        item["insight_count"] = int(row.get("insight_count") or 0)
        item["phone_count"] = int(row.get("phone_count") or 0)
        item["plan_count"] = int(row.get("plan_count") or 0)
        item["total_click_count"] = int(row.get("total_click_count") or 0)
        item["last_clicked_at"] = row.get("last_clicked_at")
        for field in (
            "file_url",
            "file_name",
            "cron_task_id",
            "cron_task_name",
        ):
            if row.get(field) and not item.get(field):
                item[field] = row.get(field)

    @classmethod
    def _apply_union_customer_count_to_list_summary_grouped(
        cls,
        grouped: dict[str, dict[str, Any]],
        row: dict[str, Any],
    ) -> None:
        """用客户并集聚合结果覆盖名单客户总数。"""
        list_key = cls._list_key("", row.get("list_key"))
        if list_key not in grouped:
            return
        grouped[list_key]["customer_count"] = int(
            row.get("customer_count") or 0,
        )

    @staticmethod
    def _list_summary_items_from_aggregate_groups(
        grouped_rows: Any,
    ) -> list[HtmlPreviewListSummaryItem]:
        """把名单聚合中间态转换为按当前规则排序的响应项。"""
        items = [
            HtmlPreviewListSummaryItem(
                list_key=row["list_key"],
                list_name=row["list_name"],
                file_url=row.get("file_url"),
                file_name=row.get("file_name"),
                cron_task_id=row.get("cron_task_id"),
                cron_task_name=row.get("cron_task_name"),
                customer_count=row["customer_count"],
                clicked_customer_count=row["clicked_customer_count"],
                insight_count=row["insight_count"],
                phone_count=row["phone_count"],
                plan_count=row["plan_count"],
                total_click_count=row["total_click_count"],
                last_clicked_at=row.get("last_clicked_at"),
            )
            for row in grouped_rows
        ]
        return sorted(
            items,
            key=lambda item: (
                item.total_click_count,
                item.last_clicked_at or datetime.min,
            ),
            reverse=True,
        )

    @staticmethod
    def _empty_list_page(
        *,
        page: int,
        page_size: int,
    ) -> HtmlPreviewListSummaryResponse:
        """构造空名单分页结果。"""
        safe_page = max(1, page)
        safe_page_size = max(1, min(page_size, 100))
        return HtmlPreviewListSummaryResponse(
            success=True,
            total=0,
            clicked_list_count=0,
            page=safe_page,
            page_size=safe_page_size,
            summary=HtmlPreviewListSummaryItem(
                list_key="all",
                list_name="全部名单",
            ),
            items=[],
        )

    @staticmethod
    def _build_list_summary_total(
        items: list[HtmlPreviewListSummaryItem],
    ) -> HtmlPreviewListSummaryItem:
        """汇总当前筛选条件下的全量名单指标。"""
        return HtmlPreviewListSummaryItem(
            list_key="all",
            list_name="全部名单",
            customer_count=sum(item.customer_count for item in items),
            clicked_customer_count=sum(
                item.clicked_customer_count for item in items
            ),
            insight_count=sum(item.insight_count for item in items),
            phone_count=sum(item.phone_count for item in items),
            plan_count=sum(item.plan_count for item in items),
            total_click_count=sum(item.total_click_count for item in items),
            last_clicked_at=max(
                (
                    item.last_clicked_at
                    for item in items
                    if item.last_clicked_at
                ),
                default=None,
            ),
        )

    @classmethod
    def _build_customer_click_items(
        cls,
        *,
        snapshot_rows: list[dict[str, Any]],
        event_rows: list[dict[str, Any]],
        include_unclicked: bool,
    ) -> list[HtmlPreviewCustomerClickItem]:
        """组合名单快照和点击事件，生成客户维度统计。"""
        grouped: dict[str, dict[str, Any]] = {}
        if include_unclicked:
            for row in snapshot_rows:
                key = cls._customer_group_key(
                    row.get("customer_id"),
                    row.get("customer_name"),
                )
                if not key:
                    continue
                grouped.setdefault(
                    key,
                    cls._empty_customer_group(
                        customer_id=row.get("customer_id"),
                        customer_name=row.get("customer_name"),
                        list_key=row.get("list_key"),
                        list_name=row.get("list_name"),
                    ),
                )

        for row in event_rows:
            customer_id, customer_name = cls._get_customer_identity(
                row.get("customer_info"),
                row.get("customer_id"),
                row.get("customer_name"),
            )
            key = cls._customer_group_key(customer_id, customer_name)
            if not key:
                continue
            item = grouped.setdefault(
                key,
                cls._empty_customer_group(
                    customer_id=customer_id,
                    customer_name=customer_name,
                    list_key=row.get("list_key"),
                    list_name=row.get("list_name"),
                ),
            )
            cls._apply_event_to_group(item, row)

        items = [
            HtmlPreviewCustomerClickItem(
                customer_id=row.get("customer_id"),
                customer_name=row.get("customer_name") or "未知客户",
                list_key=row.get("list_key"),
                list_name=row.get("list_name"),
                insight_count=row["insight_count"],
                phone_count=row["phone_count"],
                plan_count=row["plan_count"],
                total_click_count=row["total_click_count"],
                last_clicked_user_id=row.get("last_clicked_user_id"),
                last_clicked_user_name=row.get("last_clicked_user_name"),
                manager_clicks=cls._manager_click_items(row["managers"]),
                last_clicked_at=row.get("last_clicked_at"),
            )
            for row in grouped.values()
            if include_unclicked or row["total_click_count"] > 0
        ]
        return sorted(
            items,
            key=lambda item: (
                item.total_click_count,
                item.last_clicked_at or datetime.min,
            ),
            reverse=True,
        )

    @classmethod
    def _customer_group_key(
        cls,
        customer_id: Optional[str],
        customer_name: Optional[str],
    ) -> Optional[str]:
        """构造客户去重键，优先按客户 ID 去重。"""
        cleaned_id = cls._clean_text(customer_id)
        cleaned_name = cls._clean_text(customer_name)
        if cleaned_id:
            return cleaned_id
        if cleaned_name:
            return f"name:{cleaned_name}"
        return None

    @classmethod
    def _empty_customer_group(
        cls,
        *,
        customer_id: Optional[str],
        customer_name: Optional[str],
        list_key: Optional[str],
        list_name: Optional[str],
    ) -> dict[str, Any]:
        """创建客户统计空行。"""
        return {
            "customer_id": cls._clean_text(customer_id),
            "customer_name": cls._clean_text(customer_name) or "未知客户",
            "list_key": cls._clean_text(list_key),
            "list_name": cls._clean_text(list_name),
            "insight_count": 0,
            "phone_count": 0,
            "plan_count": 0,
            "total_click_count": 0,
            "last_clicked_user_id": None,
            "last_clicked_user_name": None,
            "managers": {},
            "last_clicked_at": None,
        }

    @classmethod
    def _apply_event_to_group(
        cls,
        item: dict[str, Any],
        row: dict[str, Any],
    ) -> None:
        """把一条点击事件累加到聚合行。"""
        button_type = cls._classify_button(row)
        if button_type == "insight":
            item["insight_count"] += 1
        elif button_type == "phone":
            item["phone_count"] += 1
        elif button_type == "plan":
            item["plan_count"] += 1
        else:
            return

        item["total_click_count"] += 1
        customer_id, customer_name = cls._get_customer_identity(
            row.get("customer_info"),
            row.get("customer_id"),
            row.get("customer_name"),
        )
        customer_key = cls._customer_group_key(customer_id, customer_name)
        if customer_key and "clicked_customers" in item:
            item["clicked_customers"].add(customer_key)
        if customer_key and "customers" in item:
            item["customers"].add(customer_key)

        cls._apply_manager_click(item, row, button_type)

        clicked_at = row.get("clicked_at")
        last_clicked_at = item.get("last_clicked_at")
        if clicked_at and (
            not last_clicked_at or clicked_at > last_clicked_at
        ):
            item["last_clicked_at"] = clicked_at
            item["last_clicked_user_id"] = cls._clean_text(row.get("user_id"))
            item["last_clicked_user_name"] = cls._clean_text(
                row.get("user_name"),
            )
            if customer_name and "customer_name" in item:
                item["customer_name"] = customer_name

    @classmethod
    def _apply_manager_click(
        cls,
        item: dict[str, Any],
        row: dict[str, Any],
        button_type: str,
    ) -> None:
        """按点击人统计客户经理维度点击次数。"""
        user_id = cls._clean_text(row.get("user_id"))
        user_name = cls._clean_text(row.get("user_name"))
        managers = item.get("managers")
        if not user_id or not isinstance(managers, dict):
            return

        manager = managers.setdefault(
            user_id,
            {
                "user_id": user_id,
                "user_name": user_name,
                "insight_count": 0,
                "phone_count": 0,
                "plan_count": 0,
                "total_click_count": 0,
                "last_clicked_at": None,
            },
        )
        manager[f"{button_type}_count"] += 1
        manager["total_click_count"] += 1
        if user_name and not manager.get("user_name"):
            manager["user_name"] = user_name
        clicked_at = row.get("clicked_at")
        last_clicked_at = manager.get("last_clicked_at")
        if clicked_at and (
            not last_clicked_at or clicked_at > last_clicked_at
        ):
            manager["last_clicked_at"] = clicked_at
            manager["user_name"] = user_name or manager.get("user_name")

    @staticmethod
    def _manager_click_items(
        managers: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """把客户经理点击聚合映射为响应列表。"""
        return sorted(
            managers.values(),
            key=lambda item: (
                item["total_click_count"],
                item.get("last_clicked_at") or datetime.min,
            ),
            reverse=True,
        )

    def _build_event_where_clause(
        self,
        *,
        source_id: Optional[str],
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        bbk_ids: Optional[list[str]] = None,
        cron_task_id: Optional[str] = None,
        file_url: Optional[str] = None,
        list_key: Optional[str] = None,
        event_type: Optional[HtmlPreviewEventType] = None,
        template_type: Optional[HtmlPreviewTemplateType] = None,
        template_id: Optional[int] = None,
        result_id: Optional[str] = None,
        event_target_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> tuple[str, list[Any]]:
        """构造点击查询的公共筛选条件。"""
        where_sql, params = self._build_where_clause(
            source_id=source_id,
            time_column="clicked_at",
            start_time=start_time,
            end_time=end_time,
            bbk_ids=bbk_ids,
            cron_task_id=cron_task_id,
            file_url=file_url,
            list_key=list_key,
            event_type=event_type,
        )
        dimension_filters = (
            ("template_type", template_type),
            ("template_id", template_id),
            ("result_id", result_id),
            ("event_target_id", event_target_id),
            ("trace_id", trace_id),
        )
        dimension_clauses = []
        for column, value in dimension_filters:
            if value is not None:
                dimension_clauses.append(f"{column} = %s")
                params.append(value)
        if dimension_clauses:
            conjunction = " AND " if where_sql else "WHERE "
            where_sql += conjunction + " AND ".join(dimension_clauses)
        return where_sql, params

    def _build_snapshot_where_clause(
        self,
        *,
        source_id: Optional[str],
        bbk_ids: Optional[list[str]] = None,
        list_key: Optional[str] = None,
    ) -> tuple[str, list[Any]]:
        """构造名单快照查询的公共筛选条件。"""
        return self._build_where_clause(
            source_id=source_id,
            time_column="snapshot_at",
            bbk_ids=bbk_ids,
            list_key=list_key,
        )

    def _build_where_clause(
        self,
        *,
        source_id: Optional[str],
        time_column: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        bbk_ids: Optional[list[str]] = None,
        cron_task_id: Optional[str] = None,
        file_url: Optional[str] = None,
        list_key: Optional[str] = None,
        event_type: Optional[HtmlPreviewEventType] = None,
    ) -> tuple[str, list[Any]]:
        """构造查询的公共筛选条件。"""
        where_clauses: list[str] = []
        params: list[Any] = []
        if source_id:
            where_clauses.append("source_id <=> %s")
            params.append(source_id)
        if start_time:
            where_clauses.append(f"{time_column} >= %s")
            params.append(start_time)
        if end_time:
            where_clauses.append(f"{time_column} <= %s")
            params.append(end_time)
        if bbk_ids:
            placeholders = ", ".join(["%s"] * len(bbk_ids))
            where_clauses.append(f"bbk_id IN ({placeholders})")
            params.extend(bbk_ids)
        if cron_task_id:
            where_clauses.append("cron_task_id = %s")
            params.append(cron_task_id)
        if file_url:
            where_clauses.append("file_url = %s")
            params.append(file_url)
        if list_key:
            where_clauses.append("list_key = %s")
            params.append(list_key)
        if event_type:
            where_clauses.append("event_type = %s")
            params.append(event_type)

        where_sql = (
            f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        )
        return where_sql, params
