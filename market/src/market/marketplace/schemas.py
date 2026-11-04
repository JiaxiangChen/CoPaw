# -*- coding: utf-8 -*-
"""API 请求/响应模型."""

from __future__ import annotations

from typing import Any, Literal, Optional
from pydantic import BaseModel, Field

from .models import ExpertVersion, ExpertVersionsManifest


class PublishSkillRequest(BaseModel):
    """上架技能请求体."""

    name: str
    chinese_name: str = ""
    description: str = ""
    creator_id: str
    creator_name: str = ""
    category_id: Optional[int] = None
    bbk_ids: list[str] = Field(default_factory=list)
    skill_json: dict = Field(default_factory=dict)
    skill_md: str = ""
    # 可选：指定用户技能目录名，用于同步整个目录
    skill_name: Optional[str] = None
    agent_id: str = "default"
    overwrite: bool = False
    # 用户工作区版本号，用于版本快照的 source_user_version
    source_user_version: str = ""
    # 同步模式：直接传递用户已有的 skill_id 和 cn_name，无需再解析
    skill_id: str = ""
    cn_name: str = ""
    # 是否纳入统计
    include_in_statistics: bool = False


class DistributeRequest(BaseModel):
    """分发技能请求体."""

    target_type: Literal["all", "bbk_id", "user_id"]
    target_values: list[str] = Field(default_factory=list)


class MarketSkillResponse(BaseModel):
    """市场技能列表/详情响应."""

    item_id: str
    name: str
    skill_id: str = ""
    chinese_name: str = ""
    description: str
    version: str
    creator_id: str
    creator_name: str
    category_id: Optional[int]
    bbk_ids: list[str]
    status: str
    created_at: Optional[str]
    updated_at: Optional[str]
    call_count: int = 0
    user_count: int = 0
    version_unchanged: bool = False
    include_in_statistics: bool = False  # 是否纳入统计


class SkillUserStat(BaseModel):
    """技能详情页调用客户明细."""

    user_id: str
    user_name: str
    call_count: int


class MarketSkillDetail(MarketSkillResponse):
    """技能详情（含调用客户明细）."""

    user_stats: list[SkillUserStat] = Field(default_factory=list)


class PublishExpertRequest(BaseModel):
    """上架社区专家请求体."""

    definition_id: str
    agent_id: str = "default"
    category_id: int | None = None
    bbk_ids: list[str] = Field(default_factory=list)
    overwrite: bool = False


class ExpertInstallRequest(BaseModel):
    """接收专家到一个 Agent Profile。"""

    agent_id: str = "default"


class ExpertDistributionRequest(BaseModel):
    """管理员分发专家。"""

    target_type: Literal["all", "bbk_id", "user_id"]
    target_values: list[str] = Field(default_factory=list)


class ExpertOperationResult(BaseModel):
    user_id: str
    success: bool
    definition_id: str | None = None
    reason: str | None = None


class ExpertDistributionResponse(BaseModel):
    item_id: str
    distributed_count: int
    conflict_count: int = 0
    results: list[ExpertOperationResult] = Field(default_factory=list)


class ExpertRecallRequest(BaseModel):
    target_user_ids: list[str] | None = None


class ExpertRecallResponse(BaseModel):
    item_id: str
    recalled_count: int
    failed_count: int = 0
    results: list[ExpertOperationResult] = Field(default_factory=list)


class MarketExpertResponse(BaseModel):
    """社区专家列表/详情响应."""

    item_id: str
    name: str
    description: str = ""
    version: str = "1.0.0"
    creator_id: str
    creator_name: str = ""
    category_id: Optional[int] = None
    bbk_ids: list[str] = Field(default_factory=list)
    status: str = "active"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    version_unchanged: bool = False


class MarketExpertDetail(MarketExpertResponse):
    """社区专家详情."""

    versions: list[ExpertVersion] = Field(default_factory=list)
    definition: dict[str, Any] = Field(default_factory=dict)


class ExpertVersionListResponse(ExpertVersionsManifest):
    """社区专家版本列表响应."""


class MySkillItem(BaseModel):
    """我的技能列表条目."""

    skill_name: str  # 目录名，用于 API 操作标识
    display_name: str = ""  # 展示名称，从 skill.json 的 name 字段读取
    source: str
    description: str = ""
    version: Optional[str] = None
    received_version: Optional[str] = None
    market_version: Optional[str] = None
    distributed_by: Optional[str] = None
    is_received: bool = False
    has_update: bool = False
    enabled: bool = True
    category: Optional[str] = None
    creator_name: Optional[str] = None
    created_at: Optional[str] = None  # 技能创建/接收时间
    updated_at: Optional[str] = None  # 技能最后更新时间
    # 新增字段
    skill_id: str = ""  # 唯一标识符，跨租户共享
    cn_name: str = Field(default="", max_length=256)  # 中文展示名


class BatchOperationRequest(BaseModel):
    """批量操作请求."""

    skills: list[str]


class SkillOperationResult(BaseModel):
    """单个技能操作结果."""

    skill_name: str
    success: bool
    reason: str | None = None


class BatchOperationResponse(BaseModel):
    """批量操作响应."""

    results: dict[str, Any]
    success_count: int
    failed_count: int


class DistributeConflictItem(BaseModel):
    """分发冲突明细."""

    user_id: str
    skill_name: str
    reason: str


class DistributeTenantResult(BaseModel):
    """单个用户的技能分发结果。"""

    user_id: str
    success: bool
    status: str
    skill_name: str = ""
    error: str | None = None


class DistributeResponse(BaseModel):
    """分发结果."""

    distributed_count: int
    conflict_count: int = 0
    failed_count: int = 0
    conflicts: list[DistributeConflictItem] = []
    results: list[DistributeTenantResult] = []
    item_id: str


class AsyncTaskSubmitResponse(BaseModel):
    """异步任务提交响应。"""

    task_id: str
    status: str = "queued"
    reused: bool = False


class BatchDistributionRequest(BaseModel):
    """批量分发技能和 MCP 请求体。"""

    batch_id: str = Field(..., min_length=1, max_length=128)
    skill_item_ids: list[str] = Field(default_factory=list)
    mcp_item_ids: list[str] = Field(default_factory=list)
    target_tenant_ids: list[str] = Field(default_factory=list)
    overwrite: bool = True


class BatchDistributionTask(BaseModel):
    """批量分发中的单个异步任务。"""

    task_id: str
    resource_type: Literal["skill", "mcp"]
    item_id: str
    status: str = "queued"


class BatchDistributionResponse(BaseModel):
    """批量分发提交响应。"""

    batch_id: str
    status: str = "queued"
    reused: bool = False
    task_ids: list[str] = Field(default_factory=list)
    tasks: list[BatchDistributionTask] = Field(default_factory=list)


class BatchDistributionQueryItem(BaseModel):
    """单个批次查询结果。"""

    batch_id: str
    status: str = "queued"
    task_ids: list[str] = Field(default_factory=list)
    tasks: list[BatchDistributionTask] = Field(default_factory=list)
    total_task_count: int = 0
    done_task_count: int = 0
    failed_task_count: int = 0


class BatchDistributionQueryResponse(BaseModel):
    """批量分发查询响应。"""

    batches: list[BatchDistributionQueryItem] = Field(default_factory=list)


class FileTreeNode(BaseModel):
    """文件树节点."""

    name: str
    type: Literal["file", "directory"]
    path: str
    children: list["FileTreeNode"] | None = None


class FileContentResponse(BaseModel):
    """文件内容响应."""

    content: str
    file_type: str  # "markdown" | "json" | "text" | "binary"


class OperationResponse(BaseModel):
    """操作结果响应."""

    success: bool = True
    message: str | None = None


class UploadSkillResponse(BaseModel):
    """技能上传响应."""

    imported: list[str] = Field(default_factory=list)
    count: int = 0
    enabled: bool = True
    name: str | None = None
    description: str | None = None
    skill_id: str | None = None
    cn_name: str | None = None
    conflicts: list[dict] | None = None
    version_unchanged: bool = False


class ParseZipResponse(BaseModel):
    """解析 zip 文件响应."""

    skill_name: str | None = None
    cn_name: str | None = None
    skill_id: str | None = None
    description: str | None = None
    exists: bool = False
    error: str | None = None
    skill_id_reused: bool = False  # 应用市场场景：是否复用已有 skill_id
    skill_id_conflict: str | None = None  # 我的技能场景：skill_id 冲突提示
    skill_id_used_count: int = 0  # 应用市场场景：持有该 skill_id 的用户数量
    skill_id_used_by: list[str] = Field(
        default_factory=list,
    )  # 应用市场场景：用户列表（最多3个）


class MCPDistributionRequest(BaseModel):
    """MCP 分发请求体，语义与现有 MCP 菜单分发到用户保持一致。"""

    target_tenant_ids: list[str] = Field(default_factory=list)
    overwrite: bool = True


class MCPDistributionTenantResult(BaseModel):
    """单个用户的 MCP 分发结果。"""

    tenant_id: str
    tenant_name: str | None = None
    success: bool
    bootstrapped: bool = False
    default_agent_updated: list[str] = Field(default_factory=list)
    skipped: bool = False
    error: Optional[str] = None


class MCPDistributionResponse(BaseModel):
    """MCP 分发响应。"""

    source_agent_id: str
    results: list[MCPDistributionTenantResult] = Field(default_factory=list)


class MarketMCPItem(BaseModel):
    """市场 MCP 列表项."""

    item_id: str
    client_key: str
    name: str
    chinese_name: str = ""
    description: str = ""
    guidance: str = ""
    version: str = "1.0.0"
    creator_id: str
    creator_name: str = ""
    category_id: Optional[int] = None
    bbk_ids: list[str] = Field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    version_unchanged: bool = False
    call_count: int = 0
    user_count: int = 0


class MCPConfigDetail(BaseModel):
    """MCP 配置详情."""

    transport: str = "stdio"
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    lazy_load: bool = False


class MCPUserStat(BaseModel):
    """MCP 用户统计."""

    user_id: str
    user_name: str
    call_count: int


class MarketMCPDetail(MarketMCPItem):
    """市场 MCP 详情."""

    config: MCPConfigDetail
    user_stats: list[MCPUserStat] = Field(default_factory=list)


class PublishMCPRequest(BaseModel):
    """发布 MCP 到市场请求."""

    client_key: str
    name: str
    chinese_name: str = ""
    description: str = ""
    guidance: str = ""
    creator_id: str
    creator_name: str = ""
    category_id: Optional[int] = None
    bbk_ids: list[str] = Field(default_factory=list)
    config: dict
    overwrite: bool = False
    version: str = ""  # 发布者本地版本号，首次发布时作为市场条目初始版本
    # T9 R5/R6 新增：分别记录"内容来源用户"与"操作者"
    source_user_id: str = ""
    source_user_name: str = ""
    source_user_version: str = ""
    operator_id: str = ""
    operator_name: str = ""


class UploadMCPResponse(BaseModel):
    """上传 MCP 响应."""

    success: bool
    error: Optional[str] = None
    version_unchanged: bool = False


class UpdateMarketMCPMetadataRequest(BaseModel):
    """MCP 市场元数据更新请求体。"""

    chinese_name: str | None = None
    description: str | None = None
    guidance: str | None = None
    bbk_ids: list[str] = Field(default_factory=list)


class DistributionRecord(BaseModel):
    """分发记录."""

    target_user_id: str
    target_user_name: str = ""
    target_bbk_id: str = ""
    distributed_at: Optional[str] = None


class RecallRequest(BaseModel):
    """撤回请求体."""

    target_user_ids: list[str] | None = None  # 不传则撤回所有
    force: bool = False  # 强制撤回，不验证来源是否匹配
    skill_name: str | None = None  # 按技能名称撤回（技能专用）
    mcp_name: str | None = None  # 按 MCP 名称撤回（MCP 专用）


class RecallResultItem(BaseModel):
    """单个用户的撤回结果."""

    user_id: str
    success: bool
    reason: str | None = None


class RecallResponse(BaseModel):
    """撤回结果响应."""

    recalled_count: int
    failed_count: int = 0
    results: list[RecallResultItem] = Field(default_factory=list)
    item_id: str


class DistributionPreviewRequest(BaseModel):
    """分发预览请求体."""

    source_id: str
    tenant_ids: list[str] = Field(default_factory=list)


class UserSkillStatus(BaseModel):
    """用户技能状态."""

    tenant_id: str
    tenant_name: str | None = None
    bbk_id: str | None = None
    status: str  # first_time / update / conflict
    current_version: str | None = None  # update 时显示当前版本


class DistributionPreviewResponse(BaseModel):
    """分发预览响应."""

    skill_version: str
    users: list[UserSkillStatus] = Field(default_factory=list)
    distributed_user_ids: list[str] = Field(default_factory=list)


# ============================================================
# RPC 接口模型
# ============================================================


class SkillQueryRequest(BaseModel):
    """技能查询请求体."""

    skill_names: list[str] = Field(
        ...,
        description="技能名称列表，最多 100 个",
        max_length=100,
    )
    source_types: Optional[list[str]] = Field(
        default=None,
        description="来源类型过滤：builtin/customized/marketplace",
    )
    enabled_only: bool = Field(
        default=False,
        description="是否只返回已启用技能，默认查询所有",
    )


class SkillInfo(BaseModel):
    """技能基本信息（纯技能属性）."""

    skill_id: str = Field(description="技能唯一标识符")
    skill_name: str = Field(description="技能名称（目录名）")
    cn_name: str = Field(default="", description="中文展示名")
    source: str = Field(description="来源类型：builtin/customized/marketplace")
    enabled: bool = Field(description="是否启用")
    version_text: str = Field(default="1.0.0", description="版本号")


class SkillQueryResult(BaseModel):
    """单个技能查询结果."""

    skill_name: str = Field(description="查询的技能名称")
    found: bool = Field(description="是否找到")
    skill: Optional[SkillInfo] = Field(
        default=None,
        description="技能信息，未找到时为 null",
    )


class SkillQueryResponse(BaseModel):
    """技能查询响应."""

    results: list[SkillQueryResult] = Field(
        description="查询结果列表，按请求顺序",
    )
    total_requested: int = Field(description="请求查询的技能数量")
    total_found: int = Field(description="找到的技能数量")


class BranchCount(BaseModel):
    """分行计数."""

    bbk_id: str = Field(description="分行 ID")
    skill_count: int = Field(description="技能数量")
    mcp_count: int = Field(description="MCP 数量")
    total_unique_skill_count: int = Field(description="唯一技能总数（去重）")
    total_unique_mcp_count: int = Field(description="唯一MCP总数（去重）")


class BranchCountsResponse(BaseModel):
    """分行列表及计数响应."""

    branches: list[BranchCount] = Field(description="分行列表及计数")


class MarketBrowseCategory(BaseModel):
    """统一市场浏览的分类 facet."""

    id: int
    name: str
    count: int = 0


class MarketBrowseBranch(BaseModel):
    """统一市场浏览的分行 facet."""

    bbk_id: str
    count: int = 0


class MarketBrowseResponse(BaseModel):
    """统一市场浏览结果及两侧 facet 统计."""

    resource_type: str
    stats_available: bool = True
    items: list[dict] = Field(default_factory=list)
    total: int = 0
    category_total: int = 0
    branch_total: int = 0
    categories: list[MarketBrowseCategory] = Field(default_factory=list)
    branches: list[MarketBrowseBranch] = Field(default_factory=list)
