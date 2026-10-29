# -*- coding: utf-8 -*-
"""Tenant init source mapping store.

Records which default_{source} template directory was used to initialize
each tenant, enabling source-based template isolation.
"""

import logging
from typing import Any, Optional

from ...utils.bbk import normalize_bbk_id_to_primary

logger = logging.getLogger(__name__)

# Global store singleton
_store: Optional["TenantInitSourceStore"] = None


class TenantInitSourceStore:
    """Store for tenant initialization source mapping.

    Follows the same dependency-injection pattern as InstanceStore:
    - Receives an optional database connection via constructor
    - Graceful degradation when database is unavailable
    """

    def __init__(self, db: Optional[Any] = None):
        """Initialize store.

        Args:
            db: Database connection (DatabaseConnection)
        """
        self.db = db
        self._use_db = db is not None and getattr(db, "is_connected", False)

    async def initialize(self) -> None:
        """Initialize store, checking database availability."""
        if self.db is not None and getattr(self.db, "is_connected", False):
            self._use_db = True
            logger.info("TenantInitSourceStore initialized with database")
        else:
            self._use_db = False
            logger.info("TenantInitSourceStore initialized without database")

    async def get_or_create(
        self,
        tenant_id: str,
        source_id: str,
        init_source: str,
        tenant_name: Optional[str] = None,
        bbk_id: Optional[str] = None,
        tenant_type: str = "tenant",
    ) -> str:
        """Get existing or create a new mapping record.

        Each (tenant_id, source_id) combination is unique. If the
        combination already exists, returns the stored init_source.
        Otherwise, creates a new record and returns the provided
        init_source.

        Args:
            tenant_id: The tenant identifier.
            source_id: The source identifier from X-Source-Id header.
            init_source: The template directory name used for initialization.
            tenant_name: The tenant/user name (optional).
            bbk_id: The BBK identifier (optional).

        Returns:
            The init_source value for this tenant-source combination.
        """
        if self._use_db:
            query = (
                "SELECT init_source FROM swe_tenant_init_source "
                "WHERE tenant_id = %s AND source_id = %s"
            )
            try:
                row = await self.db.fetch_one(query, (tenant_id, source_id))
                if row:
                    return row["init_source"]
            except Exception as e:
                logger.warning(
                    f"Failed to query init_source for tenant "
                    f"{tenant_id}: {e}",
                )
                return init_source

            normalized_bbk_id = normalize_bbk_id_to_primary(bbk_id)
            insert_query = (
                "INSERT INTO swe_tenant_init_source "
                "(tenant_id, source_id, tenant_name, bbk_id, init_source, tenant_type) "
                "VALUES (%s, %s, %s, %s, %s, %s)"
            )
            try:
                await self.db.execute(
                    insert_query,
                    (
                        tenant_id,
                        source_id,
                        tenant_name,
                        normalized_bbk_id,
                        init_source,
                        tenant_type,
                    ),
                )
                logger.info(
                    f"Created init_source mapping: tenant={tenant_id}, "
                    f"source={source_id}, tenant_name={tenant_name}, "
                    f"bbk_id={normalized_bbk_id}, init_source={init_source}, "
                    f"tenant_type={tenant_type}",
                )
            except Exception as e:
                logger.warning(
                    f"Failed to insert init_source mapping for tenant "
                    f"{tenant_id}: {e}",
                )

        return init_source

    async def get_sources_for_tenant(self, tenant_id: str) -> list[dict]:
        """Query all source records for a tenant.

        Args:
            tenant_id: The tenant identifier.

        Returns:
            List of dicts with source_id, tenant_name, bbk_id, init_source, created_at.
        """
        if not self._use_db:
            return []
        query = (
            "SELECT source_id, tenant_name, bbk_id, init_source, created_at "
            "FROM swe_tenant_init_source WHERE tenant_id = %s "
            "ORDER BY created_at"
        )
        try:
            rows = await self.db.fetch_all(query, (tenant_id,))
            return list(rows)
        except Exception as e:
            logger.warning(
                f"Failed to query sources for tenant={tenant_id}: {e}",
            )
            return []

    async def is_tenant_source(
        self,
        tenant_id: str,
        expected_source_id: str,
    ) -> bool:
        """判断租户是否属于指定来源。

        Args:
            tenant_id: 租户标识。
            expected_source_id: 期望的来源标识。

        Returns:
            如果租户在 swe_tenant_init_source 表中存在对应的 source_id 记录则返回 True。
        """
        if not self._use_db:
            return False
        query = (
            "SELECT 1 FROM swe_tenant_init_source "
            "WHERE tenant_id = %s AND source_id = %s LIMIT 1"
        )
        try:
            row = await self.db.fetch_one(
                query,
                (tenant_id, expected_source_id),
            )
            return row is not None
        except Exception as e:
            logger.warning(
                "Failed to check source for tenant=%s: %s",
                tenant_id,
                e,
            )
            return False

    async def get_by_source(
        self,
        source_id: str,
        *,
        include_templates: bool = False,
        bbk_id: str | None = None,
    ) -> list[dict]:
        """Query all tenants initialized from a given source.

        Args:
            source_id: The source identifier to filter by.
            bbk_id: Optional BBK identifier for exact matching.

        Returns:
            List of dicts with tenant_id, source_id, tenant_name, bbk_id, init_source, created_at.
        """
        if not self._use_db:
            return []
        query = (
            "SELECT tenant_id, source_id, tenant_name, bbk_id, init_source, "
            "tenant_type, created_at FROM swe_tenant_init_source "
            "WHERE source_id = %s "
        )
        params = (source_id,)
        if bbk_id is not None:
            query += "AND bbk_id = %s "
            params += (bbk_id,)
        if not include_templates:
            query += "AND (tenant_type IS NULL OR tenant_type != 'template') "
        query += "ORDER BY created_at"
        try:
            rows = await self.db.fetch_all(query, params)
            return list(rows)
        except Exception as e:
            logger.warning(
                f"Failed to query tenants by source_id={source_id}: {e}",
            )
            return []

    async def get_tenant_source_info(
        self,
        tenant_id: str,
        source_id: str,
    ) -> dict | None:
        """Query tenant identity fields for a tenant-source pair.

        Args:
            tenant_id: The tenant identifier.
            source_id: The source identifier.

        Returns:
            Dict containing tenant_name and bbk_id, or None if no record exists.
        """
        if not self._use_db:
            return None
        query = (
            "SELECT tenant_name, bbk_id FROM swe_tenant_init_source "
            "WHERE tenant_id = %s AND source_id = %s "
            "LIMIT 1"
        )
        try:
            row = await self.db.fetch_one(query, (tenant_id, source_id))
            return dict(row) if row else None
        except Exception as e:
            logger.warning(
                "Failed to query tenant source info for tenant=%s source=%s: %s",
                tenant_id,
                source_id,
                e,
            )
            return None

    async def get_all(
        self,
        page: int = 1,
        page_size: int = 20,
        *,
        include_templates: bool = False,
    ) -> tuple[list[dict], int]:
        """Get all mapping records with pagination.

        Args:
            page: Page number (1-based).
            page_size: Records per page.

        Returns:
            Tuple of (records list, total count).
        """
        if not self._use_db:
            return [], 0

        try:
            count_query = (
                "SELECT COUNT(*) as total FROM swe_tenant_init_source"
            )
            if not include_templates:
                count_query += (
                    " WHERE (tenant_type IS NULL OR tenant_type != 'template')"
                )
            count_row = await self.db.fetch_one(count_query)
            total = count_row["total"] if count_row else 0

            offset = (page - 1) * page_size
            query = (
                "SELECT tenant_id, source_id, tenant_name, bbk_id, init_source, "
                "tenant_type, created_at FROM swe_tenant_init_source "
            )
            if not include_templates:
                query += (
                    "WHERE (tenant_type IS NULL OR tenant_type != 'template') "
                )
            query += "ORDER BY created_at DESC LIMIT %s OFFSET %s"
            rows = await self.db.fetch_all(query, (page_size, offset))
            return list(rows), total
        except Exception as e:
            logger.warning(f"Failed to query all init_source mappings: {e}")
            return [], 0

    async def get_by_tenant_prefix(
        self,
        prefixes: list[str],
        *,
        include_templates: bool = False,
    ) -> list[dict]:
        """Query tenants whose tenant_id starts with given prefixes.

        Args:
            prefixes: List of prefixes to filter (e.g., ["80", "0", "IT"]).

        Returns:
            List of dicts with tenant_id, source_id, tenant_name, bbk_id.
        """
        if not self._use_db or not prefixes:
            return []

        # 构建 OR 条件
        conditions = " OR ".join([f"tenant_id LIKE '{p}%'" for p in prefixes])
        query = (
            "SELECT tenant_id, source_id, tenant_name, bbk_id, init_source, tenant_type "
            "FROM swe_tenant_init_source WHERE " + conditions
        )
        if not include_templates:
            query += " AND (tenant_type IS NULL OR tenant_type != 'template')"
        try:
            rows = await self.db.fetch_all(query)
            return list(rows)
        except Exception as e:
            logger.warning(
                f"Failed to query tenants by prefixes {prefixes}: {e}",
            )
            return []

    async def update_tenant_info(
        self,
        tenant_id: str,
        source_id: str,
        tenant_name: str | None = None,
        bbk_id: str | None = None,
    ) -> bool:
        """Update tenant_name and bbk_id for a tenant-source combination.

        Args:
            tenant_id: The tenant identifier.
            source_id: The source identifier.
            tenant_name: The tenant name to update (optional).
            bbk_id: The BBK ID to update (optional).

        Returns:
            True if update succeeded, False otherwise.
        """
        if not self._use_db:
            return False

        try:
            normalized_bbk_id = normalize_bbk_id_to_primary(bbk_id)
            query = (
                "UPDATE swe_tenant_init_source "
                "SET tenant_name = %s, bbk_id = %s "
                "WHERE tenant_id = %s AND source_id = %s"
            )
            await self.db.execute(
                query,
                (tenant_name, normalized_bbk_id, tenant_id, source_id),
            )
            logger.info(
                f"Updated tenant info: tenant={tenant_id}, "
                f"source={source_id}, tenant_name={tenant_name}, "
                f"bbk_id={normalized_bbk_id}",
            )
            return True
        except Exception as e:
            logger.warning(
                f"Failed to update tenant info for {tenant_id}: {e}",
            )
            return False

    async def get_bbk_by_source(
        self,
        source_id: str,
        *,
        include_templates: bool = False,
    ) -> list[dict]:
        """Query distinct bbk_ids for a given source.

        Args:
            source_id: The source identifier to filter by.

        Returns:
            List of dicts with bbk_id.
        """
        if not self._use_db:
            return []
        query = (
            "SELECT DISTINCT bbk_id FROM swe_tenant_init_source "
            "WHERE source_id = %s AND bbk_id IS NOT NULL AND bbk_id != '' "
        )
        if not include_templates:
            query += "AND (tenant_type IS NULL OR tenant_type != 'template') "
        query += "ORDER BY bbk_id"
        try:
            rows = await self.db.fetch_all(query, (source_id,))
            return list(rows)
        except Exception as e:
            logger.warning(
                f"Failed to query bbk_ids by source_id={source_id}: {e}",
            )
            return []


def init_tenant_init_source_module(db=None) -> None:
    """Initialize tenant init source module with database connection.

    Args:
        db: DatabaseConnection instance (optional, if None, module operates
            in stub mode without database persistence).
    """
    global _store

    if db is None or not getattr(db, "is_connected", False):
        _store = None
        logger.info(
            "TenantInitSourceStore initialized in stub mode (no database connection)",
        )
        return

    _store = TenantInitSourceStore(db)
    logger.info("TenantInitSourceStore initialized with database connection")


def get_tenant_init_source_store() -> Optional["TenantInitSourceStore"]:
    """Get the global TenantInitSourceStore instance.

    Returns:
        The store instance, or None if not initialized.
    """
    return _store


async def is_tenant_source(
    tenant_id: str,
    expected_source_id: str,
) -> bool:
    """判断租户是否属于指定来源的便捷函数。

    内部调用 TenantInitSourceStore.is_tenant_source，store 为 None 时返回 False。

    Args:
        tenant_id: 租户标识。
        expected_source_id: 期望的来源标识。

    Returns:
        如果租户在 swe_tenant_init_source 表中存在对应的 source_id 记录则返回 True。
    """
    store = get_tenant_init_source_store()
    if store is None:
        return False
    return await store.is_tenant_source(tenant_id, expected_source_id)
