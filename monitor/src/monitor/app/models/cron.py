# -*- coding: utf-8 -*-
"""Data models for cron job monitoring.

Defines models for:
- CronJobModel: Task definition stored in database
- ExecutionModel: Execution history stored in database
- SyncRequest models: Request bodies for sync APIs
- Query models: Request/response models for query APIs
"""

import json
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Generic, List, Literal, Optional, TypeVar

from pydantic import BaseModel, Field, model_validator

# ============================================================
# Enums
# ============================================================


class TaskType(str, Enum):
    """Task type for cron jobs."""

    TEXT = "text"
    AGENT = "agent"
    WORKFLOW = "workflow"


class JobStatus(str, Enum):
    """Status for cron jobs."""

    ACTIVE = "active"
    PAUSED = "paused"
    DELETED = "deleted"


class ExecutionStatus(str, Enum):
    """Status for execution records."""

    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"
    RUNNING = "running"


# ============================================================
# Database Models (映射数据库表结构)
# ============================================================


class CronJobModel(BaseModel):
    """Cron job definition model (maps to cron_jobs table).

    This model represents a cron job stored in the database.
    Used for both reading from database and syncing from SWE.
    """

    id: str = Field(..., description="任务ID (UUID)")
    name: str = Field(..., description="任务名称")
    tenant_id: str = Field(..., description="租户ID (分行号)")
    tenant_name: str = Field(
        default="",
        description="租户姓名 (X-User-Name header)",
    )
    bbk_id: str = Field(default="", description="分行号 (X-Bbk-Id header)")
    source_id: str = Field(
        default="",
        description="来源标识 (X-Source-Id header)",
    )
    enabled: bool = Field(default=True, description="是否启用")
    task_type: str = Field(..., description="任务类型: text/agent/workflow")
    workflow_binding_id: Optional[str] = None

    # 调度配置
    cron_expr: str = Field(..., description="cron表达式 (5字段)")
    timezone: str = Field(default="UTC", description="时区")

    # 执行目标
    channel: str = Field(..., description="分发渠道")
    target_user_id: str = Field(default="", description="目标用户ID")
    target_session_id: str = Field(default="", description="目标会话ID")

    # 执行配置
    timeout_seconds: int = Field(default=7200, description="超时秒数")
    max_concurrency: int = Field(default=1, description="最大并发数")
    misfire_grace_seconds: int = Field(
        default=300,
        description="misfire容错秒数",
    )

    # 任务内容
    text_content: str = Field(default="", description="text类型任务内容")
    request_input: str = Field(default="", description="agent类型请求输入")

    # 任务元数据
    creator_user_id: str = Field(default="", description="创建者用户ID")
    task_chat_id: str = Field(default="", description="关联聊天ID")
    task_session_id: str = Field(default="", description="关联会话ID")
    job_origin: str = Field(default="manual", description="任务来源")
    subscription_key: str = Field(default="", description="订阅任务稳定分组ID")
    skill_ids: str = Field(default="", description="绑定技能ID，逗号分隔")
    broadcast_source_job_id: str = Field(
        default="",
        description="分发源定时任务ID (从meta中提取)",
    )
    meta: str = Field(default="", description="扩展元数据 (JSON字符串)")

    # 状态追踪
    status: str = Field(
        default="active",
        description="状态: active/paused/deleted",
    )
    pause_reason: str = Field(default="", description="暂停原因")

    # 时间戳
    created_at: Optional[datetime] = Field(
        default=None,
        description="创建时间",
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        description="更新时间",
    )
    deleted_at: Optional[datetime] = Field(
        default=None,
        description="删除时间",
    )

    # 统计字段（不在数据库中，运行时计算）
    execution_count: int = Field(default=0, description="已执行次数")
    today_status: Optional[str] = Field(
        default=None,
        description="今日最新执行状态: success/error/cancelled/timeout/skipped",
    )

    def get_meta_dict(self) -> Dict[str, Any]:
        """Parse meta string to dict."""
        if not self.meta:
            return {}
        try:
            return json.loads(self.meta)
        except json.JSONDecodeError:
            return {}

    def get_request_input_dict(self) -> Dict[str, Any]:
        """Parse request_input string to dict."""
        if not self.request_input:
            return {}
        try:
            return json.loads(self.request_input)
        except json.JSONDecodeError:
            return {}


class ExecutionModel(BaseModel):
    """Execution history model (maps to cron_executions table).

    This model represents a single execution record stored in the database.
    """

    id: Optional[int] = Field(default=None, description="执行记录ID")
    job_id: str = Field(..., description="任务ID")
    job_name: str = Field(default="", description="任务名称 (冗余存储)")
    tenant_id: str = Field(..., description="租户ID (分行号)")
    tenant_name: Optional[str] = Field(
        default=None,
        description="租户姓名 (从任务表JOIN获取，可能为空)",
    )
    bbk_id: str = Field(default="", description="分行号")

    # 执行时间
    scheduled_time: Optional[datetime] = Field(
        default=None,
        description="计划执行时间",
    )
    actual_time: datetime = Field(..., description="实际开始时间")
    end_time: Optional[datetime] = Field(default=None, description="结束时间")
    duration_ms: int = Field(default=0, description="执行耗时 (毫秒)")

    # 执行状态
    status: str = Field(
        ...,
        description="状态: success/error/cancelled/timeout/skipped",
    )
    async_status: Optional[str] = Field(
        default=None,
        description="异步任务执行状态: success/error",
    )
    need_notification: int = Field(
        default=0,
        ge=0,
        le=1,
        description="是否需要通知: 0-否, 1-是",
    )
    error_message: str = Field(default="", description="错误信息")

    # 执行上下文
    instance_id: str = Field(default="", description="执行实例标识")
    executor_leader: str = Field(default="", description="执行者 leader ID")
    is_manual: bool = Field(default=False, description="是否手动触发")

    # 可追溯链路
    trace_id: str = Field(default="", description="关联的 trace ID")
    session_id: str = Field(default="", description="关联的 session ID")

    # 执行结果预览
    input_snapshot: str = Field(
        default="",
        description="执行时的输入快照 (JSON字符串)",
    )
    output_preview: str = Field(default="", description="输出预览 (前100字符)")

    # 执行元数据
    meta: str = Field(default="", description="执行元数据 (JSON字符串)")
    dispatch_intent_id: Optional[int] = Field(
        default=None,
        description="批调度派发意图ID",
    )
    dispatch_batch_id: str = Field(default="", description="批调度批次ID")
    dispatch_attempt: Optional[int] = Field(
        default=None,
        description="批调度派发尝试次数",
    )

    # 已读状态
    notification_status: str = Field(
        default="not_required",
        description="通知状态",
    )
    notification_due_at: Optional[datetime] = Field(
        default=None,
        description="计划通知时间",
    )
    notification_timezone: str = Field(default="", description="通知计算时区")
    notification_sent_at: Optional[datetime] = Field(
        default=None,
        description="通知发送时间",
    )
    notification_attempts: int = Field(default=0, description="通知尝试次数")
    notification_error: str = Field(default="", description="通知错误")
    notification_lock_owner: str = Field(
        default="",
        description="通知锁持有者",
    )
    notification_locked_at: Optional[datetime] = Field(
        default=None,
        description="通知锁时间",
    )

    is_read: bool = Field(default=False, description="是否已读")
    read_at: Optional[datetime] = Field(
        default=None,
        description="已读时间",
    )

    # 时间戳
    created_at: Optional[datetime] = Field(
        default=None,
        description="记录创建时间",
    )

    def get_meta_dict(self) -> Dict[str, Any]:
        """Parse meta string to dict."""
        if not self.meta:
            return {}
        try:
            return json.loads(self.meta)
        except json.JSONDecodeError:
            return {}

    def get_input_snapshot_dict(self) -> Dict[str, Any]:
        """Parse input_snapshot string to dict."""
        if not self.input_snapshot:
            return {}
        try:
            return json.loads(self.input_snapshot)
        except json.JSONDecodeError:
            return {}


# ============================================================
# Sync Request Models (供 SWE 双写调用)
# ============================================================


class LatestExecutionSubtaskCountResponse(BaseModel):
    """Latest execution identity and its subtask count for a cron job."""

    job_id: str = Field(..., description="Cron job ID")
    execution_id: Optional[int] = Field(
        default=None,
        description="Latest execution record ID",
    )
    trace_id: Optional[str] = Field(
        default=None,
        description="Latest execution trace ID",
    )
    subtask_count: int = Field(
        default=0,
        ge=0,
        description="Number of subtasks associated with the latest trace",
    )


class CronJobSyncRequest(BaseModel):
    """Request body for syncing a cron job from SWE.

    Maps from CronJobSpec in SWE to database fields.
    """

    id: str = Field(..., description="任务ID")
    plan_id: Optional[str] = Field(
        default=None,
        max_length=255,
        description="关联计划ID",
    )
    name: str = Field(..., description="任务名称")
    tenant_id: str = Field(default="", description="租户ID")
    tenant_name: str = Field(
        default="",
        description="租户姓名 (X-User-Name header)",
    )
    bbk_id: str = Field(default="", description="分行号 (X-Bbk-Id header)")
    source_id: str = Field(
        default="",
        description="来源标识 (X-Source-Id header)",
    )
    enabled: bool = Field(default=True, description="是否启用")
    task_type: str = Field(default="agent", description="任务类型")
    workflow_binding_id: Optional[str] = None

    # 调度配置
    cron_expr: str = Field(..., description="cron表达式")
    timezone: str = Field(default="UTC", description="时区")

    # 执行目标
    channel: str = Field(default="", description="分发渠道")
    target_user_id: str = Field(default="", description="目标用户ID")
    target_session_id: str = Field(default="", description="目标会话ID")

    # 执行配置
    timeout_seconds: int = Field(default=7200, description="超时秒数")
    max_concurrency: int = Field(default=1, description="最大并发数")
    misfire_grace_seconds: int = Field(
        default=300,
        description="misfire容错秒数",
    )

    # 任务内容
    text_content: str = Field(default="", description="text类型任务内容")
    request_input: str = Field(
        default="",
        description="agent类型请求输入 (JSON字符串)",
    )

    # 任务元数据
    creator_user_id: str = Field(default="", description="创建者用户ID")
    task_chat_id: str = Field(default="", description="关联聊天ID")
    task_session_id: str = Field(default="", description="关联会话ID")
    job_origin: str = Field(default="manual", description="任务来源")
    subscription_key: str = Field(default="", description="订阅任务稳定分组ID")
    skill_ids: str = Field(
        default="",
        max_length=200,
        description="绑定技能ID，逗号分隔",
    )
    meta: str = Field(default="", description="扩展元数据 (JSON字符串)")

    # 状态
    status: str = Field(default="active", description="状态")
    pause_reason: str = Field(default="", description="暂停原因")

    @model_validator(mode="after")
    def validate_workflow_binding(self) -> "CronJobSyncRequest":
        if self.task_type == "workflow":
            if not self.workflow_binding_id:
                raise ValueError(
                    "workflow_binding_id is required for workflow",
                )
        elif self.workflow_binding_id:
            raise ValueError("workflow_binding_id is only valid for workflow")
        return self


class ExecutionSyncRequest(BaseModel):
    """Request body for recording an execution from SWE.

    Maps from CronJob execution context to database fields.
    """

    job_id: str = Field(..., description="任务ID")
    job_name: str = Field(default="", description="任务名称")
    tenant_id: str = Field(default="", description="租户ID")
    source_id: str = Field(default="", description="source ID")

    # 执行时间
    scheduled_time: Optional[datetime] = Field(
        default=None,
        description="计划执行时间",
    )
    actual_time: datetime = Field(..., description="实际开始时间")
    end_time: Optional[datetime] = Field(default=None, description="结束时间")
    duration_ms: int = Field(default=0, description="执行耗时 (毫秒)")

    # 执行状态
    status: str = Field(..., description="状态")
    error_message: str = Field(default="", description="错误信息")

    # 执行上下文
    instance_id: str = Field(default="", description="执行实例标识")
    executor_leader: str = Field(default="", description="执行者 leader ID")
    is_manual: bool = Field(default=False, description="是否手动触发")

    # 可追溯链路
    trace_id: str = Field(default="", description="关联的 trace ID")
    session_id: str = Field(default="", description="关联的 session ID")

    # 执行结果预览
    input_snapshot: str = Field(
        default="",
        description="执行时的输入快照 (JSON字符串)",
    )
    output_preview: str = Field(default="", description="输出预览")

    # 执行元数据
    meta: str = Field(default="", description="执行元数据 (JSON字符串)")
    dispatch_intent_id: Optional[int] = Field(
        default=None,
        description="批调度派发意图ID",
    )
    dispatch_batch_id: str = Field(default="", description="批调度批次ID")
    dispatch_attempt: Optional[int] = Field(
        default=None,
        description="批调度派发尝试次数",
    )

    # 已读状态（手动执行且成功的任务默认已读）
    notification_status: str = Field(
        default="not_required",
        description="通知状态",
    )
    notification_due_at: Optional[datetime] = Field(
        default=None,
        description="计划通知时间",
    )
    notification_timezone: str = Field(default="", description="通知计算时区")

    is_read: bool = Field(default=False, description="是否已读")
    read_at: Optional[datetime] = Field(
        default=None,
        description="已读时间",
    )


# ============================================================
# Query Models (供前端查询)
# ============================================================


class CronJobQueryParams(BaseModel):
    """Query parameters for listing cron jobs."""

    tenant_id: Optional[str] = Field(default=None, description="租户ID筛选")
    bbk_id: Optional[str] = Field(default=None, description="分行号筛选")
    source_id: Optional[str] = Field(default=None, description="来源标识筛选")
    creator_user_id: Optional[str] = Field(
        default=None,
        description="创建者ID筛选",
    )
    job_origin: Optional[str] = Field(default=None, description="任务来源筛选")
    status: Optional[str] = Field(default=None, description="状态筛选")
    enabled: Optional[bool] = Field(default=None, description="是否启用筛选")
    page: int = Field(default=1, ge=1, description="页码")
    page_size: int = Field(default=10, ge=1, le=100, description="每页数量")


class ExecutionQueryParams(BaseModel):
    """Query parameters for listing execution history."""

    job_id: Optional[str] = Field(default=None, description="任务ID筛选")
    tenant_id: Optional[str] = Field(default=None, description="租户ID筛选")
    bbk_id: Optional[str] = Field(default=None, description="分行号筛选")
    source_id: Optional[str] = Field(default=None, description="来源标识筛选")
    status: Optional[str] = Field(default=None, description="执行状态筛选")
    start_time: Optional[datetime] = Field(
        default=None,
        description="开始时间范围",
    )
    end_time: Optional[datetime] = Field(
        default=None,
        description="结束时间范围",
    )
    page: int = Field(default=1, ge=1, description="页码")
    page_size: int = Field(default=10, ge=1, le=100, description="每页数量")


class BroadcastSourceJobQueryParams(BaseModel):
    """Query parameters for listing jobs by broadcast source."""

    tenant_id: Optional[str] = Field(default=None, description="租户ID筛选")
    bbk_id: Optional[str] = Field(
        default=None,
        description="分行号筛选（二级分行号）",
    )
    source_id: Optional[str] = Field(default=None, description="来源标识筛选")
    broadcast_source_job_id: str = Field(..., description="分发源定时任务ID")


class ExportQueryParams(BaseModel):
    """Query parameters for exporting data."""

    tenant_id: Optional[str] = Field(default=None, description="租户ID筛选")
    status: Optional[str] = Field(default=None, description="状态筛选")
    start_time: Optional[datetime] = Field(
        default=None,
        description="开始时间范围",
    )
    end_time: Optional[datetime] = Field(
        default=None,
        description="结束时间范围",
    )


# ============================================================
# Response Models
# ============================================================


T = TypeVar("T")
CronScheduleBucketMinutes = Literal[5, 10, 15, 30, 60]


class PaginatedResponse(BaseModel, Generic[T]):
    """Paginated response for list queries."""

    items: List[T] = Field(default_factory=list, description="数据列表")
    total: int = Field(default=0, description="总数量")
    page: int = Field(default=1, description="当前页码")
    page_size: int = Field(default=10, description="每页数量")


class CronScheduleDistributionDiagnostics(BaseModel):
    """Bounded definition diagnostics for a schedule calculation."""

    invalid_cron_jobs: int = Field(
        default=0,
        ge=0,
        description="无效 Cron 任务数",
    )
    invalid_timezone_jobs: int = Field(
        default=0,
        ge=0,
        description="使用 UTC 回退的无效时区任务数",
    )
    unsupported_task_type_jobs: int = Field(
        default=0,
        ge=0,
        description="被排除的不支持任务类型数",
    )
    invalid_metadata_jobs: int = Field(
        default=0,
        ge=0,
        description="元数据无法解析的任务数",
    )
    managed_child_jobs: int = Field(
        default=0,
        ge=0,
        description="被批调度托管并排除的子任务数",
    )


class CronScheduleDistributionBucket(BaseModel):
    """One start-aligned, half-open planned firing bucket."""

    start_time: datetime = Field(..., description="区间开始时间（UTC，含）")
    end_time: datetime = Field(..., description="区间结束时间（UTC，不含）")
    text_count: int = Field(default=0, ge=0, description="Text 计划触发次数")
    agent_count: int = Field(default=0, ge=0, description="Agent 计划触发次数")
    workflow_count: int = Field(
        default=0,
        ge=0,
        description="Workflow 计划触发次数",
    )
    total_count: int = Field(default=0, ge=0, description="计划触发总次数")


class CronScheduleDistributionResponse(BaseModel):
    """Planned firing distribution for the selected range."""

    start_time: datetime = Field(..., description="统计开始时间（UTC，含）")
    end_time: datetime = Field(..., description="统计结束时间（UTC，不含）")
    bucket_minutes: CronScheduleBucketMinutes = Field(
        ...,
        description="桶间隔分钟数",
    )
    calculated_at: datetime = Field(..., description="计算完成时间（UTC）")
    definition_revision: str = Field(..., description="任务定义确定性版本")
    eligible_job_count: int = Field(
        default=0,
        ge=0,
        description="通过定义校验并参与计划计算的任务数",
    )
    text_count: int = Field(default=0, ge=0, description="Text 计划触发次数")
    agent_count: int = Field(default=0, ge=0, description="Agent 计划触发次数")
    workflow_count: int = Field(
        default=0,
        ge=0,
        description="Workflow 计划触发次数",
    )
    total_count: int = Field(default=0, ge=0, description="计划触发总次数")
    buckets: List[CronScheduleDistributionBucket] = Field(
        default_factory=list,
        description="按时间顺序排列的统计桶",
    )
    diagnostics: CronScheduleDistributionDiagnostics = Field(
        default_factory=CronScheduleDistributionDiagnostics,
        description="被排除或回退的任务定义诊断",
    )


class CronScheduleOccurrenceItem(BaseModel):
    """Whitelisted planned firing detail row."""

    scheduled_at: datetime = Field(..., description="计划触发时间（UTC）")
    job_id: str = Field(..., description="任务 ID")
    job_name: str = Field(..., description="任务名称")
    user_name: str = Field(..., description="用户姓名")
    user_id: str = Field(..., description="用户账号 ID")
    task_type: TaskType = Field(..., description="任务类型")
    cron_expr: str = Field(..., description="Cron 表达式")
    timezone: str = Field(..., description="任务配置时区")


class CronScheduleDistributionDetailsResponse(BaseModel):
    """Paginated planned firing occurrences for one selected bucket."""

    start_time: datetime = Field(..., description="区间开始时间（UTC，含）")
    end_time: datetime = Field(..., description="区间结束时间（UTC，不含）")
    task_type: Optional[TaskType] = Field(
        default=None,
        description="任务类型筛选",
    )
    calculated_at: datetime = Field(..., description="计算完成时间（UTC）")
    definition_revision: str = Field(..., description="任务定义确定性版本")
    items: List[CronScheduleOccurrenceItem] = Field(
        default_factory=list,
        description="计划触发明细",
    )
    total: int = Field(default=0, ge=0, description="明细总数")
    page: int = Field(default=1, ge=1, description="当前页码")
    page_size: int = Field(default=20, ge=1, le=100, description="每页数量")
    diagnostics: CronScheduleDistributionDiagnostics = Field(
        default_factory=CronScheduleDistributionDiagnostics,
        description="被排除或回退的任务定义诊断",
    )


class CronScheduleDistributionErrorDetail(BaseModel):
    """Stable error detail returned only by schedule distribution routes."""

    code: str = Field(..., description="稳定错误码")
    message: str = Field(..., description="可读错误信息")
    actual_revision: Optional[str] = Field(
        default=None,
        description="定义变更冲突时的当前版本",
    )


class CronScheduleDistributionErrorResponse(BaseModel):
    """Stable HTTP error envelope for schedule distribution routes."""

    detail: CronScheduleDistributionErrorDetail


class SyncJobResponse(BaseModel):
    """Response for sync job API."""

    synced: bool = Field(default=True, description="是否同步成功")


class DeleteJobResponse(BaseModel):
    """Response for delete job API."""

    deleted: bool = Field(default=True, description="是否删除成功")


class RecordExecutionResponse(BaseModel):
    """Response for record execution API."""

    recorded: bool = Field(default=True, description="是否记录成功")
    execution_id: Optional[int] = Field(default=None, description="执行记录ID")


class ExecutionDetailResponse(ExecutionModel):
    """Detailed execution response with additional info."""

    # 可以添加额外信息，如关联的 job 信息
    job_name: str = Field(default="", description="任务名称")


class MarkReadResponse(BaseModel):
    """Response for mark job as read API."""

    marked: bool = Field(default=True, description="是否标记成功")
    count: int = Field(default=0, description="标记已读的记录数")


class UnreadCountItem(BaseModel):
    """Single unread count item."""

    job_id: str = Field(..., description="任务ID")
    job_name: str = Field(..., description="任务名称")
    unread_count: int = Field(default=0, description="未读数量")


class UnreadCountResponse(BaseModel):
    """Response for unread count API."""

    items: List[UnreadCountItem] = Field(
        default_factory=list,
        description="各任务未读数量列表",
    )
    total_unread: int = Field(default=0, description="总未读数量")


class CronOverviewMetricItem(BaseModel):
    """Single metric item for the cron overview page."""

    key: str = Field(..., description="Metric key")
    value: float = Field(default=0, description="Metric value")
    compare: str = Field(
        default="",
        description="Comparison text (e.g., '+12.5%')",
    )
    trend: Optional[str] = Field(
        default=None,
        description="Trend: 'up' or 'down'",
    )


class CronOverviewDistributionItem(BaseModel):
    """Single distribution item for charts."""

    name: str = Field(..., description="Item name")
    value: int = Field(default=0, description="Item count")
    percent: float = Field(default=0.0, description="Item percentage")
    color: Optional[str] = Field(default=None, description="Item color")


class CronOverviewBranchExecutionItem(BaseModel):
    """Execution distribution for one branch."""

    name: str = Field(..., description="Branch ID")
    success: int = Field(default=0, description="Success count")
    failed: int = Field(default=0, description="Failure count")
    skipped: int = Field(default=0, description="Skipped count")


class CronOverviewBranchReadItem(BaseModel):
    """Read distribution for one branch."""

    name: str = Field(..., description="Branch ID")
    read: int = Field(default=0, description="Read success execution count")
    unread: int = Field(
        default=0,
        description="Unread success execution count",
    )


class CronOverviewResponse(BaseModel):
    """Aggregated response for the cron overview page."""

    start_time: Optional[datetime] = Field(
        default=None,
        description="Range start",
    )
    end_time: Optional[datetime] = Field(default=None, description="Range end")
    metrics: List[CronOverviewMetricItem] = Field(default_factory=list)
    task_status: List[CronOverviewDistributionItem] = Field(
        default_factory=list,
    )
    execution_result: List[CronOverviewDistributionItem] = Field(
        default_factory=list,
    )
    read_status: List[CronOverviewDistributionItem] = Field(
        default_factory=list,
    )
    failure_reasons: List[CronOverviewDistributionItem] = Field(
        default_factory=list,
    )
    branch_tasks: List[CronOverviewDistributionItem] = Field(
        default_factory=list,
    )
    branch_execution: List[CronOverviewBranchExecutionItem] = Field(
        default_factory=list,
    )
    branch_read: List[CronOverviewBranchReadItem] = Field(default_factory=list)


class CronDispatchBatchStats(BaseModel):
    """Aggregated batch-dispatch counts for the selected source and range."""

    total_batches: int = Field(default=0, description="批次数")
    running_batches: int = Field(default=0, description="运行中批次数")
    completed_batches: int = Field(default=0, description="已完成批次数")
    failed_batches: int = Field(default=0, description="失败批次数")
    total_intents: int = Field(default=0, description="Intent 总数")
    completed_intents: int = Field(default=0, description="已完成 Intent 数")
    failed_intents: int = Field(default=0, description="失败 Intent 数")
    skipped_intents: int = Field(default=0, description="跳过／取消 Intent 数")
    pending_intents: int = Field(default=0, description="未完成 Intent 数")


class CronDispatchDetailQueryParams(BaseModel):
    """Pagination and filtering for one batch-dispatch detail view."""

    intent_page: int = Field(default=1, ge=1)
    intent_limit: int = Field(default=100, ge=1, le=500)
    intent_query: Optional[str] = Field(default=None, max_length=256)
    intent_role: Optional[str] = Field(default=None, max_length=16)
    intent_status: Optional[str] = Field(default=None, max_length=16)
    event_page: int = Field(default=1, ge=1)
    event_limit: int = Field(default=100, ge=1, le=500)


class CronDispatchBatchItem(BaseModel):
    """One batch-dispatch batch row."""

    batch_id: str = Field(..., description="批调度批次 ID")
    parent_job_id: str = Field(default="", description="父任务 ID")
    parent_job_name: str = Field(default="", description="定时任务名称")
    parent_external_job_id: str = Field(
        default="",
        description="调度平台任务 ID",
    )
    tenant_id: str = Field(default="", description="父任务租户 ID")
    source_id: str = Field(default="", description="渠道 ID")
    provider_id: str = Field(default="", description="Provider ID")
    model_id: str = Field(default="", description="模型 ID")
    agent_id: str = Field(default="", description="Agent ID")
    scheduled_fire_at: Optional[datetime] = Field(
        default=None,
        description="父任务计划执行时间",
    )
    callback_received_at: Optional[datetime] = Field(
        default=None,
        description="调度平台回调到达时间",
    )
    status: str = Field(default="", description="批次状态")
    lock_owner: str = Field(default="", description="批次 owner")
    locked_at: Optional[datetime] = Field(default=None, description="锁定时间")
    total_count: int = Field(default=0, description="Intent 总数")
    completed_count: int = Field(default=0, description="完成数")
    failed_count: int = Field(default=0, description="失败数")
    skipped_count: int = Field(default=0, description="跳过／取消数")
    dispatch_paused: Optional[bool] = Field(
        default=None,
        description="独立批调度暂停状态；空表示未知",
    )
    error_message: str = Field(default="", description="错误摘要")
    completed_at: Optional[datetime] = Field(
        default=None,
        description="完成时间",
    )
    created_at: Optional[datetime] = Field(
        default=None,
        description="创建时间",
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        description="更新时间",
    )


class CronDispatchPriority(BaseModel):
    basis: str = "default"
    user_rank: Optional[int] = None
    branch_rank: Optional[int] = None
    branch_id: str = ""


class CronDispatchIntentItem(BaseModel):
    """One batch-dispatch intent row."""

    id: int = Field(..., description="Intent ID")
    batch_id: str = Field(default="", description="批次 ID")
    intent_role: str = Field(default="", description="parent/child")
    status: str = Field(default="", description="Intent 状态")
    source_id: str = Field(default="", description="渠道 ID")
    provider_id: str = Field(default="", description="Provider ID")
    model_id: str = Field(default="", description="模型 ID")
    tenant_id: str = Field(default="", description="租户 ID")
    agent_id: str = Field(default="", description="Agent ID")
    job_id: str = Field(default="", description="任务 ID")
    parent_job_id: str = Field(default="", description="父任务 ID")
    scheduled_fire_at: Optional[datetime] = Field(default=None)
    due_at: Optional[datetime] = Field(default=None, description="可领取时间")
    dispatch_order: int = Field(default=0, description="批内分发顺序")
    viewer_heat_score: float = Field(default=0.0, description="热度分")
    priority: Optional[CronDispatchPriority] = None
    attempt_count: int = Field(default=0, description="尝试次数")
    max_attempts: int = Field(default=0, description="最大尝试次数")
    lock_owner: str = Field(default="", description="worker owner")
    locked_at: Optional[datetime] = Field(default=None)
    acked_at: Optional[datetime] = Field(default=None)
    completed_at: Optional[datetime] = Field(default=None)
    error_message: str = Field(default="", description="错误信息")
    created_at: Optional[datetime] = Field(default=None)
    updated_at: Optional[datetime] = Field(default=None)


class CronDispatchEventItem(BaseModel):
    """One batch-dispatch event row."""

    id: int = Field(..., description="事件 ID")
    batch_id: str = Field(default="", description="批次 ID")
    intent_id: Optional[int] = Field(default=None, description="Intent ID")
    event_type: str = Field(default="", description="事件类型")
    worker_id: str = Field(default="", description="worker ID")
    job_id: str = Field(default="", description="任务 ID")
    tenant_id: str = Field(default="", description="租户 ID")
    source_id: str = Field(default="", description="渠道 ID")
    details: Optional[Dict[str, Any]] = Field(
        default=None,
        description="事件详情",
    )
    created_at: Optional[datetime] = Field(
        default=None,
        description="创建时间",
    )


class CronDispatchBatchesResponse(BaseModel):
    """Paginated batch-dispatch batch response."""

    source_id: str = Field(default="", description="当前渠道 ID")
    start_time: Optional[datetime] = Field(
        default=None,
        description="开始时间",
    )
    end_time: Optional[datetime] = Field(default=None, description="结束时间")
    stats: CronDispatchBatchStats = Field(
        default_factory=CronDispatchBatchStats,
    )
    items: List[CronDispatchBatchItem] = Field(default_factory=list)
    total: int = Field(default=0, description="总数")
    page: int = Field(default=1, description="页码")
    page_size: int = Field(default=10, description="每页数量")


class CronDispatchBatchDetailResponse(BaseModel):
    """Batch-dispatch detail response for one batch."""

    batch: CronDispatchBatchItem
    intents: List[CronDispatchIntentItem] = Field(default_factory=list)
    intent_total: int = Field(default=0, description="Intent 总数")
    intent_filtered_total: int = Field(
        default=0,
        description="筛选后 Intent 数",
    )
    intent_page: int = Field(default=1, description="Intent 页码")
    intent_page_size: int = Field(default=100, description="Intent 每页数量")
    events: List[CronDispatchEventItem] = Field(default_factory=list)
    event_total: int = Field(default=0, description="事件总数")
    event_page: int = Field(default=1, description="事件页码")
    event_page_size: int = Field(default=100, description="事件每页数量")


class CronDispatchPolicyItem(BaseModel):
    """Source/provider/model worker policy row."""

    source_id: str = Field(default="", description="渠道 ID")
    provider_id: str = Field(default="", description="Provider ID")
    model_id: str = Field(default="", description="模型 ID")
    default_strategy_id: str = Field(default="", description="默认策略 ID")
    strategy_schedule: Any = Field(default=None)
    enabled: bool = Field(default=True, description="是否启用")
    strategy: Optional[Dict[str, Any]] = Field(
        default=None,
        description="策略详情",
    )
    created_at: Optional[datetime] = Field(default=None)
    updated_at: Optional[datetime] = Field(default=None)


class CronDispatchCapacityItem(BaseModel):
    """Worker capacity snapshot row."""

    id: int = Field(..., description="快照 ID")
    worker_id: str = Field(default="", description="Worker ID")
    source_id: str = Field(default="", description="渠道 ID")
    provider_id: str = Field(default="", description="Provider ID")
    model_id: str = Field(default="", description="模型 ID")
    strategy_id: str = Field(default="", description="策略 ID")
    previous_workers: int = Field(default=0)
    baseline_workers: int = Field(default=0)
    min_workers: int = Field(default=0)
    max_workers: int = Field(default=0)
    effective_workers: int = Field(default=0)
    pending_count: int = Field(default=0)
    claimed_count: int = Field(default=0)
    running_count: int = Field(default=0)
    success_count: int = Field(default=0)
    failure_count: int = Field(default=0)
    error_rate: float = Field(default=0.0)
    matched_rule: Optional[Dict[str, Any]] = Field(default=None)
    avg_latency_ms: int = Field(default=0)
    decision_reason: str = Field(default="", description="调整原因")
    created_at: Optional[datetime] = Field(default=None)


class CronDispatchWorkersResponse(BaseModel):
    """Source-level policy and worker-capacity response."""

    source_id: str = Field(default="", description="当前渠道 ID")
    capacity_events_next_cursor: Optional[str] = None
    policies: List[CronDispatchPolicyItem] = Field(default_factory=list)
    current_capacity: List[CronDispatchCapacityItem] = Field(
        default_factory=list,
    )
    capacity_events: List[CronDispatchCapacityItem] = Field(
        default_factory=list,
    )


class SubscriptionOverviewItem(BaseModel):
    """订阅任务概览聚合项。"""

    subscription_key: str = Field(..., description="订阅任务稳定分组ID")
    task_name: str = Field(..., description="任务名称")
    subscriber_count: int = Field(default=0, description="订阅人数")
    total_task_count: int = Field(default=0, description="总任务数")
    running_task_count: int = Field(default=0, description="执行中任务数")
    pending_task_count: int = Field(default=0, description="待执行任务数")
    executed_task_count: int = Field(default=0, description="已执行任务数")
    failed_task_count: int = Field(default=0, description="执行失败任务数")
    avg_duration_ms: float = Field(default=0.0, description="平均耗时")
    success_rate: float = Field(default=0.0, description="执行成功率")


class SubscriptionDetailItem(BaseModel):
    """订阅任务详情弹窗条目。"""

    job_id: str = Field(..., description="任务ID")
    subscriber_id: str = Field(default="", description="订阅人ID")
    subscriber_name: str = Field(default="", description="订阅人名称")
    bbk_id: str = Field(default="", description="所属机构")
    enabled: bool = Field(default=True, description="启用状态")
    execution_status: str = Field(default="pending", description="执行状态")
    execution_time: Optional[datetime] = Field(
        default=None,
        description="执行时间",
    )


# ============================================================
# 新定时任务概览页面响应模型
# ============================================================


class CronOverviewStatsResponse(BaseModel):
    """定时任务概览统计响应。"""

    start_date: str = Field(..., description="开始日期")
    end_date: str = Field(..., description="结束日期")
    total_tasks: int = Field(
        default=0,
        description="定时任务总数（不包含已删除）",
    )
    new_cron_tasks: int = Field(
        default=0,
        description="新增定时任务数（时间范围内创建的任务）",
    )
    total_executions: int = Field(default=0, description="总执行次数")
    branch_count: int = Field(default=0, description="分行数量")
    tenant_count: int = Field(default=0, description="租户数量")
    success_rate: float = Field(default=0.0, description="执行成功率")
    success_count: int = Field(
        default=0,
        description="执行成功数（status='success' AND async_status='success'）",
    )
    running_count: int = Field(
        default=0,
        description="运行中数（status='success' AND async_status IS NULL）",
    )
    read_tasks: int = Field(
        default=0,
        description="成功且已读执行次数",
    )
    read_rate: float = Field(default=0.0, description="已读率")
    report_rate: float = Field(default=0.0, description="查看报告任务率")
    report_count: int = Field(default=0, description="查看报告任务数")
    insight_count: int = Field(default=0, description="点击去洞察任务数")
    phone_count: int = Field(default=0, description="点击去电访任务数")
    error_count: int = Field(
        default=0,
        description="执行失败数（综合判断）",
    )
    error_rate: float = Field(default=0.0, description="执行失败率")


class CronBranchRankingItem(BaseModel):
    """分行综合排行单项。"""

    bbk_id: str = Field(..., description="分行ID")
    bbk_name: str = Field(..., description="分行名称")
    skill_count: int = Field(default=0, description="技能数（白名单内）")
    total_tasks: int = Field(default=0, description="任务总数（生效中）")
    success_count: int = Field(default=0, description="成功执行数")
    read_tasks: int = Field(default=0, description="已读任务数")
    involved_managers: int = Field(default=0, description="涉及客户经理数")
    result_view_managers: int = Field(
        default=0,
        description="查看结果的客户经理数",
    )
    plan_managers: int = Field(default=0, description="查看经营方案客户经理数")
    insight_managers: int = Field(default=0, description="去洞察的客户经理数")
    phone_managers: int = Field(default=0, description="去电访的客户经理数")
    recommended_customers: int = Field(default=0, description="推荐的客户数")
    viewed_customers: int = Field(
        default=0,
        description="被客户经理查看的客户数",
    )
    contacted_customers: int = Field(default=0, description="接触客户数")
    contact_rate: float = Field(default=0.0, description="客户接触率")
    insight_customers: int = Field(default=0, description="去洞察客户数")
    phone_customers: int = Field(default=0, description="去电访客户数")


class CronBranchRankingResponse(BaseModel):
    """分行综合排行响应。"""

    start_date: str = Field(..., description="开始日期")
    end_date: str = Field(..., description="结束日期")
    items: List[CronBranchRankingItem] = Field(
        default_factory=list,
        description="分行综合排行列表",
    )


class CronBranchTaskRankingItem(BaseModel):
    """分行任务视角排行单项。"""

    bbk_id: str = Field(..., description="分行ID")
    bbk_name: str = Field(..., description="分行名称")
    manager_count: int = Field(default=0, description="覆盖客户经理数")
    total_tasks: int = Field(default=0, description="定时任务数")
    success_count: int = Field(default=0, description="成功执行数")
    success_rate: float = Field(default=0.0, description="成功率")
    read_tasks: int = Field(default=0, description="已读任务数")
    plan_count: int = Field(default=0, description="查看方案任务数")
    insight_count: int = Field(default=0, description="点击去洞察任务数")
    phone_count: int = Field(default=0, description="点击去电访任务数")
    plan_clicks: int = Field(default=0, description="方案点击数")
    insight_clicks: int = Field(default=0, description="洞察点击数")
    phone_clicks: int = Field(default=0, description="电访点击数")
    error_count: int = Field(default=0, description="报错执行次数")


class CronBranchTaskRankingResponse(BaseModel):
    """分行任务视角排行响应。"""

    start_date: str = Field(..., description="开始日期")
    end_date: str = Field(..., description="结束日期")
    items: List[CronBranchTaskRankingItem] = Field(
        default_factory=list,
        description="分行任务视角排行列表",
    )


class CronErrorReasonItem(BaseModel):
    """报错原因单项。"""

    reason: str = Field(..., description="报错原因")
    count: int = Field(default=0, description="数量")
    percent: float = Field(default=0.0, description="比例")


class CronBranchErrorRankItem(BaseModel):
    """分行异常排行单项。"""

    bbk_id: str = Field(..., description="分行ID")
    bbk_name: str = Field(..., description="分行名称")
    total_executions: int = Field(default=0, description="该分行总执行次数")
    error_count: int = Field(default=0, description="报错次数")
    error_rate: float = Field(default=0.0, description="报错率")
    affected_managers: int = Field(default=0, description="受影响客户经理数量")


class CronBranchErrorResponse(BaseModel):
    """分行层异常执行数据响应。"""

    start_date: str = Field(..., description="开始日期")
    end_date: str = Field(..., description="结束日期")
    affected_branch_count: int = Field(
        default=0,
        description="受影响的分行数量",
    )
    affected_manager_count: int = Field(
        default=0,
        description="受影响的客户经理数量",
    )
    error_reasons: List[CronErrorReasonItem] = Field(
        default_factory=list,
        description="报错原因分布",
    )
    branch_error_rank: List[CronBranchErrorRankItem] = Field(
        default_factory=list,
        description="分行异常排行",
    )


# ============================================================
# Branch skill drill-down models
# ============================================================


class BranchSkillItem(BaseModel):
    """分行技能维度单项。"""

    skill_name: str = Field(..., description="技能名称")
    cron_task_count: int = Field(default=0, description="定时任务数")
    success_count: int = Field(default=0, description="成功执行数")
    success_rate: float = Field(default=0.0, description="成功率")
    read_count: int = Field(default=0, description="已读任务数")
    error_count: int = Field(default=0, description="报错次数")


class BranchSkillResponse(BaseModel):
    """分行技能维度响应。"""

    start_date: str = Field(..., description="开始日期")
    end_date: str = Field(..., description="结束日期")
    bbk_id: str = Field(..., description="分行ID")
    bbk_name: str = Field(..., description="分行名称")
    items: List[BranchSkillItem] = Field(
        default_factory=list,
        description="技能维度列表",
    )


class BranchManagerSummaryItem(BaseModel):
    """分行客户经理汇总单项。"""

    user_id: str = Field(..., description="客户经理ID")
    user_name: str = Field(default="", description="客户经理姓名")
    skill_count: int = Field(default=0, description="技能数量（使用的技能数）")
    total_tasks: int = Field(default=0, description="任务总数（生效中的任务）")
    success_count: int = Field(default=0, description="成功执行数")
    read_tasks: int = Field(default=0, description="已读任务数")
    recommended_customers: int = Field(default=0, description="推荐的客户数")
    viewed_customers: int = Field(
        default=0,
        description="被客户经理查看的客户数",
    )
    contacted_customers: int = Field(default=0, description="接触客户数")
    contact_rate: float = Field(default=0.0, description="客户接触率")
    insight_customers: int = Field(default=0, description="去洞察客户数")
    phone_customers: int = Field(default=0, description="去电访客户数")


class BranchManagerSummaryResponse(BaseModel):
    """分行客户经理汇总响应。"""

    start_date: str = Field(..., description="开始日期")
    end_date: str = Field(..., description="结束日期")
    bbk_id: str = Field(..., description="分行ID")
    bbk_name: str = Field(..., description="分行名称")
    items: List[BranchManagerSummaryItem] = Field(
        default_factory=list,
        description="客户经理汇总列表",
    )


class BranchSkillManagerItem(BaseModel):
    """分行+技能的客户经理维度单项。"""

    user_id: str = Field(..., description="客户经理ID")
    user_name: str = Field(default="", description="客户经理姓名")
    read_count: int = Field(default=0, description="已读次数")
    plan_count: int = Field(default=0, description="方案次数")
    insight_count: int = Field(default=0, description="洞察次数")
    phone_count: int = Field(default=0, description="电访次数")
    last_click_time: Optional[str] = Field(
        default=None,
        description="最后一次点击时间",
    )


class BranchSkillManagerResponse(BaseModel):
    """分行+技能的客户经理维度响应。"""

    start_date: str = Field(..., description="开始日期")
    end_date: str = Field(..., description="结束日期")
    bbk_id: str = Field(..., description="分行ID")
    skill_name: str = Field(..., description="技能名称")
    items: List[BranchSkillManagerItem] = Field(
        default_factory=list,
        description="客户经理维度列表",
    )


class BranchSkillManagerCustomerItem(BaseModel):
    """分行+技能+客户经理的客户维度单项。"""

    customer_id: str = Field(default="", description="客户ID")
    customer_name: str = Field(default="", description="客户名称")
    clicked_plan: bool = Field(default=False, description="是否点击方案")
    clicked_insight: bool = Field(default=False, description="是否点击洞察")
    clicked_phone: bool = Field(default=False, description="是否点击电访")
    click_time: Optional[str] = Field(
        default=None,
        description="点击客户的时间",
    )


class BranchSkillManagerCustomerResponse(BaseModel):
    """分行+技能+客户经理的客户维度响应。"""

    start_date: str = Field(..., description="开始日期")
    end_date: str = Field(..., description="结束日期")
    bbk_id: str = Field(..., description="分行ID")
    skill_name: str = Field(..., description="技能名称")
    user_id: str = Field(..., description="客户经理ID")
    items: List[BranchSkillManagerCustomerItem] = Field(
        default_factory=list,
        description="客户维度列表",
    )


class ManagerSkillItem(BaseModel):
    """客户经理技能维度单项。"""

    skill_name: str = Field(..., description="技能名称")
    cron_task_count: int = Field(default=0, description="定时任务数")
    success_count: int = Field(default=0, description="成功执行数")
    success_rate: float = Field(default=0.0, description="成功率")
    read_count: int = Field(default=0, description="已读任务数")
    error_count: int = Field(default=0, description="报错次数")


class ManagerSkillResponse(BaseModel):
    """客户经理技能维度响应。"""

    start_date: str = Field(..., description="开始日期")
    end_date: str = Field(..., description="结束日期")
    bbk_id: str = Field(..., description="分行ID")
    user_id: str = Field(..., description="客户经理ID")
    user_name: str = Field(..., description="客户经理姓名")
    items: List[ManagerSkillItem] = Field(
        default_factory=list,
        description="技能维度列表",
    )


class ManagerCustomerItem(BaseModel):
    """客户经理客户维度单项。"""

    customer_id: str = Field(default="", description="客户ID")
    customer_name: str = Field(default="", description="客户名称")
    clicked_plan: bool = Field(default=False, description="是否点击方案")
    clicked_insight: bool = Field(default=False, description="是否点击洞察")
    clicked_phone: bool = Field(default=False, description="是否点击电访")
    click_time: Optional[str] = Field(
        default=None,
        description="点击客户的时间",
    )


class ManagerCustomerResponse(BaseModel):
    """客户经理客户维度响应。"""

    start_date: str = Field(..., description="开始日期")
    end_date: str = Field(..., description="结束日期")
    bbk_id: str = Field(..., description="分行ID")
    user_id: str = Field(..., description="客户经理ID")
    user_name: str = Field(..., description="客户经理姓名")
    items: List[ManagerCustomerItem] = Field(
        default_factory=list,
        description="客户维度列表",
    )


# ============================================================
# Helper functions for converting from SWE models
# ============================================================


def convert_spec_to_sync_request(
    spec_dict: Dict[str, Any],
) -> CronJobSyncRequest:
    """Convert CronJobSpec dict from SWE to CronJobSyncRequest.

    Args:
        spec_dict: Dict representation of CronJobSpec from SWE

    Returns:
        CronJobSyncRequest for syncing to Monitor
    """
    # Extract schedule
    schedule = spec_dict.get("schedule", {})
    cron_expr = schedule.get("cron", "")
    timezone = schedule.get("timezone", "UTC")

    # Extract dispatch
    dispatch = spec_dict.get("dispatch", {})
    channel = dispatch.get("channel", "")
    target = dispatch.get("target", {})
    target_user_id = target.get("user_id", "")
    target_session_id = target.get("session_id", "")

    # Extract runtime
    runtime = spec_dict.get("runtime", {})
    timeout_seconds = runtime.get("timeout_seconds", 7200)
    max_concurrency = runtime.get("max_concurrency", 1)
    misfire_grace_seconds = runtime.get("misfire_grace_seconds", 300)

    # Extract meta
    meta = spec_dict.get("meta", {})
    creator_user_id = meta.get("creator_user_id", "")
    task_chat_id = meta.get("task_chat_id", "")
    task_session_id = meta.get("task_session_id", "")
    subscription_key = meta.get("subscription_key", "")
    job_origin = (
        meta.get("job_origin")
        or ("subscription" if subscription_key else "")
        or "manual"
    )
    pause_reason = meta.get("pause_reason", "")

    # Determine status
    enabled = spec_dict.get("enabled", True)
    status = (
        "active"
        if enabled and not pause_reason
        else "paused" if pause_reason else "active"
    )

    # Extract request_input
    request = spec_dict.get("request", {})
    request_input_str = ""
    if request:
        request_input_str = json.dumps(request, ensure_ascii=False)

    return CronJobSyncRequest(
        id=spec_dict.get("id", ""),
        name=spec_dict.get("name", ""),
        tenant_id=spec_dict.get("tenant_id", ""),
        bbk_id=spec_dict.get("bbk_id", ""),
        source_id=spec_dict.get("source_id", ""),
        enabled=enabled,
        task_type=spec_dict.get("task_type", "agent"),
        cron_expr=cron_expr,
        timezone=timezone,
        channel=channel,
        target_user_id=target_user_id,
        target_session_id=target_session_id,
        timeout_seconds=timeout_seconds,
        max_concurrency=max_concurrency,
        misfire_grace_seconds=misfire_grace_seconds,
        text_content=spec_dict.get("text", ""),
        request_input=request_input_str,
        creator_user_id=creator_user_id,
        task_chat_id=task_chat_id,
        task_session_id=task_session_id,
        job_origin=job_origin,
        subscription_key=subscription_key,
        skill_ids=spec_dict.get("skill_ids", ""),
        meta=json.dumps(meta, ensure_ascii=False) if meta else "",
        status=status,
        pause_reason=pause_reason,
    )
