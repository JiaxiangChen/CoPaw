# -*- coding: utf-8 -*-
"""Data models for cron subtask tracking.

Defines models for:
- SubtaskModel: Subtask record stored in database
- SubtaskCreateRequest: Request body for creating subtask
- SubtaskSyncStatusResponse: Response for sync status endpoint
- ExecutionAsyncStatusResponse: Response for async status sync endpoint
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

# ============================================================
# Database Models (映射数据库表结构)
# ============================================================


class SubtaskModel(BaseModel):
    """Subtask record model (maps to swe_cron_subtasks table).

    This model represents a subtask stored in the database.
    """

    id: Optional[int] = Field(default=None, description="主键ID")
    trace_id: str = Field(..., description="主任务trace_id")
    task_id: str = Field(..., description="子任务task_id")
    filename: str = Field(..., description="文件名")
    task_type: Optional[str] = Field(
        default=None,
        description="任务类型: list/plan",
    )
    custuid: Optional[str] = Field(default=None, description="任务中客户ID")
    cust_nm: Optional[str] = Field(default=None, description="任务中客户名称")
    bbk_org_id: Optional[str] = Field(
        default=None,
        description="客户归属二级分行ID",
    )
    template_id: Optional[int] = Field(
        default=None,
        description="模板ID，用于html渲染",
    )
    result_id: Optional[str] = Field(default=None, description="es数据doc_id")
    notification_content_wplus: Optional[str] = Field(
        default=None,
        description="W+渠道通知消息内容",
    )
    notification_content_zhaohu: Optional[str] = Field(
        default=None,
        description="招乎渠道通知消息内容",
    )
    need_notification: int = Field(
        default=1,
        ge=0,
        le=1,
        description="是否需要通知: 0-否, 1-是",
    )
    has_main_task: int = Field(
        default=1,
        ge=0,
        le=1,
        description="是否有关联主任务: 0-否, 1-是",
    )
    status: Optional[str] = Field(
        default=None,
        description="子任务状态: SUC/FAIL/PART_SUC/TIMEOUT",
    )
    info: str = Field(default="", description="预留扩展信息")
    created_at: Optional[datetime] = Field(
        default=None,
        description="创建时间",
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        description="更新时间",
    )


# ============================================================
# Request/Response Models
# ============================================================


class SubtaskCreateRequest(BaseModel):
    """Request body for creating a subtask record."""

    trace_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="主任务trace_id",
    )
    task_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="子任务task_id",
    )
    filename: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description="文件名",
    )
    task_type: Optional[str] = Field(
        default=None,
        max_length=16,
        description="任务类型: list/plan",
    )
    custuid: Optional[str] = Field(
        default=None,
        max_length=64,
        description="任务中客户ID",
    )
    cust_nm: Optional[str] = Field(
        default=None,
        max_length=255,
        description="任务中客户名称",
    )
    notification_content_wplus: Optional[str] = Field(
        default=None,
        max_length=5000,
        description="W+渠道通知消息内容",
    )
    notification_content_zhaohu: Optional[str] = Field(
        default=None,
        max_length=5000,
        description="招乎渠道通知消息内容",
    )
    need_notification: int = Field(
        default=1,
        ge=0,
        le=1,
        description="是否需要通知: 0-否, 1-是",
    )
    has_main_task: int = Field(
        default=1,
        ge=0,
        le=1,
        description="是否有关联主任务: 0-否, 1-是",
    )
    template_id: Optional[int] = Field(
        default=None,
        description="模板ID，用于html渲染",
    )
    result_id: Optional[str] = Field(
        default=None,
        max_length=100,
        description="es数据doc_id",
    )
    bbk_org_id: Optional[str] = Field(
        default=None,
        max_length=10,
        description="客户归属分行ID",
    )
    status: Optional[Literal["SUC", "FAIL", "TIMEOUT"]] = Field(
        default=None,
        description="子任务状态: SUC/FAIL/TIMEOUT",
    )
    info: str = Field(
        default="",
        description="预留扩展信息",
    )


class SubtaskCreateResponse(BaseModel):
    """Response for subtask creation."""

    success: bool = Field(default=True, description="是否成功")
    id: Optional[int] = Field(default=None, description="创建的记录ID")
    message: str = Field(default="Subtask created", description="消息")


class SubtaskSyncDetailItem(BaseModel):
    """Detail item for sync status response."""

    task_id: str = Field(..., description="子任务task_id")
    old_status: Optional[str] = Field(default=None, description="旧状态")
    new_status: Optional[str] = Field(default=None, description="新状态")
    error: Optional[str] = Field(default=None, description="错误信息")


class SubtaskSyncStatusResponse(BaseModel):
    """Response for sync status endpoint."""

    success: bool = Field(default=True, description="是否成功")
    total_scanned: int = Field(default=0, description="扫描总数")
    total_updated: int = Field(default=0, description="更新总数")
    total_failed: int = Field(default=0, description="失败总数")
    details: list[SubtaskSyncDetailItem] = Field(
        default_factory=list,
        description="详情列表",
    )


class ExecutionAsyncStatusResponse(BaseModel):
    """Response for async status sync endpoint."""

    success: bool = Field(default=True, description="是否成功")
    total_scanned: int = Field(default=0, description="扫描总数")
    total_updated: int = Field(default=0, description="更新总数")
    total_success: int = Field(default=0, description="成功数")
    total_error: int = Field(default=0, description="错误数")


class FailedExecutionRerunRequest(BaseModel):
    """Request for rerunning the latest failed cron executions."""

    tenant_ids: list[str] = Field(
        ...,
        min_length=1,
        description="需要重跑当天失败任务的租户 ID 列表",
    )


class FailedExecutionRerunResponse(BaseModel):
    """Result of rerunning failed cron executions."""

    rerun_count: int = Field(default=0, description="已提交后台重跑的任务数量")
