# -*- coding: utf-8 -*-
"""财富工作台规划的持久化记录与接口契约模型。

领域术语见根目录 CONTEXT.md「Wealth Workbench Language」：
sapId 是财富域唯一的用户身份词，与请求头 X-User-Id / X-Tenant-Id 同值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PlanPublishStatus = Literal["publishing", "published", "publish_failed"]

PUBLISH_STATUS_PUBLISHING: PlanPublishStatus = "publishing"
PUBLISH_STATUS_PUBLISHED: PlanPublishStatus = "published"
PUBLISH_STATUS_FAILED: PlanPublishStatus = "publish_failed"

# 看板状态栏文案（与前端 statusClass 的关键字着色逻辑对齐）
BOARD_STATUS_PUBLISHING = "发布中"
BOARD_STATUS_PUBLISH_FAILED = "发布失败"
BOARD_STATUS_DISTRIBUTING = "分发中"
BOARD_STATUS_DISTRIBUTED = "已自动下发"
BOARD_STATUS_DISTRIBUTE_FAILED = "分发失败"

# 产品大类中文名 ↔ 英文 code，外部接口入参与落库统一使用英文 code
CATEGORY_CODE_BY_LABEL: dict[str, str] = {
    "保险": "insurance",
    "贷款": "loan",
    "存款": "deposit",
    "理财": "finance",
    "基金": "fund",
    "代发": "payroll",
    "其他": "other",
}
CATEGORY_LABEL_BY_CODE: dict[str, str] = {
    code: label for label, code in CATEGORY_CODE_BY_LABEL.items()
}

# 规划来源标签按创建人角色派生
SOURCE_LABEL_BY_ROLE: dict[str, str] = {
    "rm": "我的关注",
    "president": "行长关注",
    "middle": "分行关注",
}

WEALTH_TIMEZONE = "Asia/Shanghai"


class _StrictModel(BaseModel):
    """接口模型默认拒绝未知字段，避免前端注入未声明语义。"""

    model_config = ConfigDict(extra="forbid")


class PlanSceneInput(_StrictModel):
    """创建/修改规划时单个已选经营场景的入参。"""

    scene_id: str = Field(
        min_length=1,
        description="场景标识（外部接口 skillId）",
    )
    scene_name: str = Field(min_length=1)
    category: str = Field(min_length=1, description="产品大类英文 code")
    item_id: str | None = None
    direction: str | None = None
    cycle: str | None = Field(
        default=None,
        description="任务周期：本月/本季/今日/T+1日/自定义",
    )
    start_date: str | None = None
    end_date: str | None = None
    cron_expr: str = Field(min_length=1, description="执行排程 cron 表达式")
    cron_example: str | None = Field(
        default=None,
        description="场景技能自带的 cron 示例文本（定时任务请求内容来源）",
    )
    mcp_relations: list[str] = Field(default_factory=list)


class PlanTargetInput(_StrictModel):
    """创建/修改规划时单个分发目标的入参。"""

    sap_id: str = Field(min_length=1)
    name: str | None = None


class PlanUpsertRequest(_StrictModel):
    """创建或整体替换规划的入参。"""

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    source_label: str | None = Field(
        default=None,
        description="规划来源标签：我的关注/行长关注/分行关注，由前端按角色派生",
    )
    scenes: list[PlanSceneInput] = Field(min_length=1)
    targets: list[PlanTargetInput] = Field(default_factory=list)


@dataclass(slots=True)
class PlanSceneRecord:
    """swe_wealth_plan_scenes 行。"""

    scene_id: str
    scene_name: str
    category: str
    cron_expr: str
    item_id: str | None = None
    direction: str | None = None
    cycle: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    cron_example: str | None = None
    mcp_relations: list[str] = field(default_factory=list)
    cron_job_id: str | None = None
    broadcast_task_id: str | None = None
    sort_order: int = 0


@dataclass(slots=True)
class PlanTargetRecord:
    """swe_wealth_plan_targets 行。"""

    sap_id: str
    target_name: str | None = None
    sort_order: int = 0


@dataclass(slots=True)
class WealthPlanRecord:
    """swe_wealth_plans 行 + 关联场景与目标。"""

    id: str
    sap_id: str
    name: str
    status: PlanPublishStatus = PUBLISH_STATUS_PUBLISHING
    creator_name: str | None = None
    bbk_id: str | None = None
    source_id: str | None = None
    agent_id: str | None = None
    description: str | None = None
    source_label: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    publish_error: str | None = None
    # 技能/MCP 分发腿（market 批量分发）：task_id 列存放批次 batch_id
    skill_dispatch_task_id: str | None = None
    skill_dispatch_status: str | None = None
    skill_dispatch_error: str | None = None
    scenes: list[PlanSceneRecord] = field(default_factory=list)
    targets: list[PlanTargetRecord] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    published_at: datetime | None = None


class PlanSceneView(BaseModel):
    """看板/详情返回的单个场景。"""

    scene_id: str
    scene_name: str
    category: str
    category_label: str
    item_id: str | None = None
    direction: str | None = None
    cycle: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    cron_expr: str
    cron_example: str | None = None
    mcp_relations: list[str] = Field(default_factory=list)
    dispatch_status: str = ""


class PlanTargetView(BaseModel):
    """看板/详情返回的分发目标。"""

    sap_id: str
    name: str | None = None
    dispatch_status: str = ""


class PlanView(BaseModel):
    """看板列表/详情返回的规划视图。"""

    id: str
    name: str
    description: str | None = None
    source_label: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    publish_status: PlanPublishStatus
    board_status: str
    publish_error: str | None = None
    editable: bool
    scenes: list[PlanSceneView] = Field(default_factory=list)
    targets: list[PlanTargetView] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    published_at: str | None = None


class PlanListResponse(BaseModel):
    items: list[PlanView] = Field(default_factory=list)


class PlanCreateResponse(BaseModel):
    id: str
    publish_status: PlanPublishStatus


class SceneSkillItem(BaseModel):
    """经营场景查询响应项，字段与外部 skill-config 接口保持一致。"""

    skillId: str
    itemId: str | None = None
    senceName: str
    category: str
    senceDesc: str | None = None
    cronExample: str | None = None
    mcpRelationList: list[str] = Field(default_factory=list)
    skillBbkLabel: str | None = None
    bbkId: str | None = None


class SceneSkillListResponse(BaseModel):
    items: list[SceneSkillItem] = Field(default_factory=list)


class SkillStatQuery(_StrictModel):
    """技能统计查询项：按技能 + 起止日期区间统计（yyyy-MM-dd）。"""

    skillId: str = Field(min_length=1)
    startDate: str = Field(min_length=1)
    endDate: str = Field(min_length=1)


class SkillStatsRequest(_StrictModel):
    """看板「目标客户/已生成任务」统计入参；bbkId 由后端按请求上下文注入。"""

    skills: list[SkillStatQuery] = Field(min_length=1, max_length=200)


class SkillStatItem(BaseModel):
    """技能统计结果项，字段与外部 skill-stats 接口保持一致。"""

    skillId: str
    targetCustomerCount: int = 0
    generatedTaskCount: int = 0


class SkillStatsResponse(BaseModel):
    items: list[SkillStatItem] = Field(default_factory=list)


class NameListSkill(BaseModel):
    """客户命中的技能，直接来自外部 name-list 的 skillList。"""

    skillId: str
    skillName: str


class NameListField(BaseModel):
    """外部接口的正式字段名保留 filed 拼写。"""

    filedName: str
    filedNameCn: str


class NameListFieldValue(NameListField):
    filedValue: str | int | float | bool | None = None


class NameListSkillFields(BaseModel):
    skillId: str
    groupField: list[str] = Field(default_factory=list)
    fields: list[NameListField] = Field(default_factory=list)


class NameListItem(BaseModel):
    """客户名单明细，字段与外部 name-list 接口 data.list 保持一致。"""

    custUid: str
    custNm: str
    sapId: str | None = None
    bbkOrgId: str | None = None
    filename: str | None = None
    recomReason: str | None = None
    skillList: list[NameListSkill] = Field(default_factory=list)
    strongContactTime: str | None = None
    touchMethod: str | None = None
    touched: int | None = None
    fieldList: list[NameListFieldValue] = Field(default_factory=list)


class NameListResponse(BaseModel):
    targetCusCount: int | None = None
    todayTouchCount: int | None = None
    sevenDayTouchCount: int | None = None
    unTouchedCount: int | None = None
    items: list[NameListItem] = Field(default_factory=list)
    skillFieldList: list[NameListSkillFields] = Field(default_factory=list)
    allFields: list[NameListField] = Field(default_factory=list)
