# -*- coding: utf-8 -*-
"""新版定时任务管理接口的公共查询契约。"""

from dataclasses import dataclass

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class QueryOptions:
    limit: int = 50
    cursor: str | None = None
    include_deleted: bool = False
    tenant_ids: list[str] | None = None


class JobPage(BaseModel):
    items: list[dict] = Field(default_factory=list)
    next_cursor: str | None = None


class ChildrenPage(JobPage):
    source_job_id: str


class ManagedCronUnavailable(RuntimeError):
    """共用表结构或已保存的任务数据无法安全读取。"""


class ManagedCronNotFound(LookupError):
    """当前完整身份下不存在有权访问的 Scheduler 管理对象。"""
