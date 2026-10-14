# -*- coding: utf-8 -*-
"""财富工作台规划接口：/api/wealth/plans CRUD + 场景查询代理。

发布编排异步执行：创建/修改接口落库后立即返回，
后台任务建定时任务并广播分发，状态回写后由列表/详情接口聚合展示。
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..crons.broadcast_task_store import CronBroadcastTaskStore
from .market_dispatch import (
    BATCH_STATUS_FAILED,
    BATCH_STATUS_PARTIAL_FAILED,
    BATCH_STATUS_SUCCEEDED,
    TERMINAL_BATCH_STATUSES,
    query_batch_status,
)
from .models import (
    BOARD_STATUS_DISTRIBUTE_FAILED,
    BOARD_STATUS_DISTRIBUTED,
    BOARD_STATUS_DISTRIBUTING,
    BOARD_STATUS_PUBLISH_FAILED,
    BOARD_STATUS_PUBLISHING,
    CATEGORY_LABEL_BY_CODE,
    PUBLISH_STATUS_FAILED,
    PUBLISH_STATUS_PUBLISHING,
    PUBLISH_STATUS_PUBLISHED,
    SOURCE_LABEL_BY_ROLE,
    NameListItem,
    NameListResponse,
    PlanCreateResponse,
    PlanListResponse,
    PlanSceneRecord,
    PlanSceneView,
    PlanTargetRecord,
    PlanTargetView,
    PlanUpsertRequest,
    PlanView,
    SceneSkillItem,
    SceneSkillListResponse,
    SkillStatItem,
    SkillStatQuery,
    SkillStatsRequest,
    SkillStatsResponse,
    WealthPlanRecord,
)
from .publish import (
    cascade_delete,
    delete_removed_scene_jobs,
    launch_publish,
)
from .roles import (
    ROLE_PRESIDENT,
    ROLE_RM,
    can_view_branch_wide,
    get_visible_source_labels,
    resolve_role,
)
from .store import WealthPlanStore, new_plan_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/wealth", tags=["wealth"])

# 外部 agent-workspace 服务基础地址（skill-config 与 name-list 同 host）：
# 默认值在 src/swe/config/envs/{dev,prd}.json 维护，
# 启动时由 load_env_defaults() 注入 os.environ；进程环境变量/K8s env 优先。
_SKILL_CONFIG_API_BASE_ENV = "SWE_SKILL_CONFIG_API_BASE"
_SKILL_CONFIG_PATH = "/api/agent/workspace/skill-config/list"
_SKILL_STATS_PATH = "/api/agent/workspace/skill-stats"
_NAME_LIST_PATH = "/api/agent/workspace/name-list"
_SKILL_CONFIG_TIMEOUT_SECONDS = 8
_SHARED_SCENE_BBK_ID = "100"

_SNAP_RUNNING = "running"
_SNAP_COMPLETED = "completed"
_SNAP_FAILED = "failed"


def _get_store(request: Request) -> WealthPlanStore:
    store = getattr(request.app.state, "wealth_plan_store", None)
    if store is None:
        store = WealthPlanStore()
        request.app.state.wealth_plan_store = store
    return store


def _request_sap_id(request: Request) -> str:
    """当前用户 sapId：与中间件注入的 tenant_id / X-User-Id 同值。"""
    sap_id = getattr(request.state, "tenant_id", None) or request.headers.get(
        "X-User-Id",
    )
    return (sap_id or "default").strip()


def _request_role(request: Request) -> str:
    """当前用户角色：由 X-Position-Id 解析（前端 authHeaders 透传父系统岗位）。"""
    return resolve_role(request.headers.get("X-Position-Id"))


def _fmt_dt(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%d %H:%M") if value else None


# ---------------------------------------------------------------------------
# 经营场景查询（外部接口代理）
# ---------------------------------------------------------------------------


@router.get("/scene-skills", response_model=SceneSkillListResponse)
async def list_scene_skills(
    request: Request,
    category: str = "",
) -> SceneSkillListResponse:
    """按产品大类查询子技能场景；category 为空串表示查询全部大类。

    外部接口不可用时返回空列表，由前端展示空态，不做假数据兜底。
    """
    if category and category not in CATEGORY_LABEL_BY_CODE:
        raise HTTPException(status_code=400, detail="unknown category")
    items = await _fetch_external_scene_skills(request, category)
    return SceneSkillListResponse(items=items)


async def _fetch_external_scene_skills(
    request: Request,
    category: str,
) -> list[SceneSkillItem]:
    base = os.environ.get(_SKILL_CONFIG_API_BASE_ENV, "").strip().rstrip("/")
    if not base:
        return []
    bbk_id = getattr(request.state, "bbk_id", None) or ""
    try:
        async with httpx.AsyncClient(
            timeout=_SKILL_CONFIG_TIMEOUT_SECONDS,
        ) as client:
            resp = await client.post(
                f"{base}{_SKILL_CONFIG_PATH}",
                json={"bbkId": bbk_id, "category": category},
            )
            payload = resp.json()
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("skill-config list request failed: %s", exc)
        return []
    if str(payload.get("code")) != "200":
        logger.warning("skill-config list rejected: %s", payload.get("code"))
        return []
    rows = payload.get("data") or []
    return [
        item
        for row in rows
        if (item := _parse_external_scene(row)) is not None
    ]


def _parse_external_scene(row: Any) -> SceneSkillItem | None:
    if not isinstance(row, dict) or not row.get("skillId"):
        return None
    try:
        return SceneSkillItem(**row)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 技能统计查询（外部接口代理）：看板「目标客户 / 已生成任务」
# ---------------------------------------------------------------------------


@router.post("/skill-stats", response_model=SkillStatsResponse)
async def query_skill_stats(
    request: Request,
    body: SkillStatsRequest,
) -> SkillStatsResponse:
    """按技能 + 日期区间统计目标客户数与已生成任务数。

    bbkId 由请求上下文注入；外部接口不可用时返回空列表，
    由前端保持占位展示，不做假数据兜底。
    """
    items = await _fetch_external_skill_stats(request, body.skills)
    return SkillStatsResponse(items=items)


async def _fetch_external_skill_stats(
    request: Request,
    skills: list[SkillStatQuery],
) -> list[SkillStatItem]:
    base = os.environ.get(_SKILL_CONFIG_API_BASE_ENV, "").strip().rstrip("/")
    if not base:
        return []
    bbk_id = getattr(request.state, "bbk_id", None) or ""
    body = {
        "bbkId": bbk_id,
        "skills": [s.model_dump() for s in skills],
    }
    if _request_role(request) == ROLE_RM:
        body["sapId"] = _request_sap_id(request)
    try:
        async with httpx.AsyncClient(
            timeout=_SKILL_CONFIG_TIMEOUT_SECONDS,
        ) as client:
            resp = await client.post(f"{base}{_SKILL_STATS_PATH}", json=body)
            payload = resp.json()
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("skill-stats request failed: %s", exc)
        return []
    if str(payload.get("code")) != "200":
        logger.warning("skill-stats rejected: %s", payload.get("code"))
        return []
    data = payload.get("data") or {}
    return [
        item
        for row in data.get("items") or []
        if (item := _parse_skill_stat(row)) is not None
    ]


def _parse_skill_stat(row: Any) -> SkillStatItem | None:
    if not isinstance(row, dict) or not row.get("skillId"):
        return None
    try:
        return SkillStatItem(**row)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 客户名单查询（外部接口代理）
# ---------------------------------------------------------------------------


class NameListQueryRequest(BaseModel):
    """名单查询参数通过 JSON 传递，避免技能数组受 URL 长度限制。"""

    skill_id: list[str]
    sap_id: str = ""
    touched: int | None = None


@router.post("/name-list", response_model=NameListResponse)
async def list_name_list(
    request: Request,
    body: NameListQueryRequest,
) -> NameListResponse:
    """客户名单查询；两个视角都传 sap_id（当前登录客户经理）。

    skill_id 必传数组；经营视角传当前场景，其余口径传当天全部场景。
    touched 透传外部接口的触达状态过滤：0 未触达（待触达）/
    1 已触达（已完成）/ 2 全部（今日任务）；缺省不过滤。
    外部接口不可用时返回空列表，由前端展示空态，不做假数据兜底。
    """
    if body.touched is not None and body.touched not in (0, 1, 2):
        raise HTTPException(status_code=400, detail="invalid touched")
    skill_ids = list(
        dict.fromkeys(
            value.strip() for value in body.skill_id if value.strip()
        ),
    )
    if not skill_ids:
        raise HTTPException(
            status_code=400,
            detail="skill_id must not be empty",
        )
    return await _fetch_external_name_list(
        request,
        skill_ids,
        body.sap_id,
        body.touched,
    )


async def _fetch_external_name_list(
    request: Request,
    skill_id: list[str],
    sap_id: str,
    touched: int | None,
) -> NameListResponse:
    base = os.environ.get(_SKILL_CONFIG_API_BASE_ENV, "").strip().rstrip("/")
    if not base:
        return NameListResponse()
    bbk_id = getattr(request.state, "bbk_id", None) or ""
    body: dict[str, Any] = {
        "skillIds": skill_id,
        "bbkId": bbk_id,
        "platformSource": "WP",
        "pageSource": "WP_AGENT_WORKSPACE_TASK_LIST",
    }
    if touched is not None:
        body["touched"] = touched
    if sap_id:
        body["sapId"] = sap_id
    sub_bbk_id = request.headers.get("X-Org-Code")
    if sub_bbk_id:
        body["subBbkId"] = sub_bbk_id
    pos_id = request.headers.get("X-Position-Id")
    if pos_id:
        body["posId"] = pos_id
    cookie = request.headers.get("cookie") or request.headers.get(
        "x-header-cookie",
    )
    headers = {"Cookie": cookie} if cookie else None
    try:
        async with httpx.AsyncClient(
            timeout=_SKILL_CONFIG_TIMEOUT_SECONDS,
        ) as client:
            resp = await client.post(
                f"{base}{_NAME_LIST_PATH}",
                json=body,
                headers=headers,
            )
            payload = resp.json()
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("name-list request failed: %s", exc)
        return NameListResponse()
    return _parse_name_list_response(payload)


def _parse_name_list_response(payload: dict[str, Any]) -> NameListResponse:
    if str(payload.get("code")) != "200":
        logger.warning("name-list rejected: %s", payload.get("code"))
        return NameListResponse()
    data = payload.get("data") or {}
    items: list[NameListItem] = []
    for row in data.get("list") or []:
        if (item := _parse_name_list_item(row)) is not None:
            items.append(item)
    return NameListResponse(
        items=items,
        targetCusCount=data.get("targetCusCount"),
        todayTouchCount=data.get("todayTouchCount"),
        sevenDayTouchCount=data.get("sevenDayTouchCount"),
        unTouchedCount=data.get("unTouchedCount"),
        skillFieldList=data.get("skillFieldList") or [],
        allFields=data.get("allFields") or [],
    )


def _parse_name_list_item(row: Any) -> NameListItem | None:
    if not isinstance(row, dict) or not row.get("custUid"):
        return None
    try:
        return NameListItem(**row)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 规划 CRUD
# ---------------------------------------------------------------------------


async def _validate_plan_scenes_for_branch(
    request: Request,
    body: PlanUpsertRequest,
) -> None:
    """允许规划使用当前分行及 bbkId=100 的共享技能场景。"""
    current_bbk_id = str(
        getattr(request.state, "bbk_id", None) or "",
    ).strip()
    if not current_bbk_id:
        raise HTTPException(status_code=403, detail="无法识别当前用户所属分行")

    categories = list(dict.fromkeys(scene.category for scene in body.scenes))
    scene_groups = await asyncio.gather(
        *(
            _fetch_external_scene_skills(request, category)
            for category in categories
        ),
    )
    allowed_scenes = {
        (item.skillId, item.category)
        for group in scene_groups
        for item in group
        if str(item.bbkId or "").strip()
        in {current_bbk_id, _SHARED_SCENE_BBK_ID}
    }
    if any(
        (scene.scene_id, scene.category) not in allowed_scenes
        for scene in body.scenes
    ):
        raise HTTPException(
            status_code=403,
            detail="所选经营场景不属于当前分行或共享场景（100），或已失效",
        )


async def _validate_scene_conflicts(
    request: Request,
    store: WealthPlanStore,
    body: PlanUpsertRequest,
    *,
    exclude_plan_id: str | None = None,
    scene_ids: set[str] | None = None,
) -> None:
    """按发布角色矩阵校验本行已被占用的经营场景。"""
    checked_scene_ids = (
        scene_ids
        if scene_ids is not None
        else {scene.scene_id for scene in body.scenes}
    )
    conflicts = await store.find_scene_conflicts(
        _request_role(request),
        _request_sap_id(request),
        getattr(request.state, "bbk_id", None),
        checked_scene_ids,
        exclude_plan_id=exclude_plan_id,
        target_sap_ids={target.sap_id for target in body.targets},
        scene_ranges={
            scene.scene_id: (scene.start_date, scene.end_date)
            for scene in body.scenes
        },
    )
    if not conflicts:
        return
    scene_names = {scene.scene_id: scene.scene_name for scene in body.scenes}
    items = "、".join(
        f"「{scene_names[scene_id]}」（规划「{plan_name}」）"
        for scene_id, plan_name in conflicts.items()
    )
    raise HTTPException(
        status_code=409,
        detail=f"以下经营场景已被选用且任务周期重叠：{items}，请调整后重新发布",
    )


@router.post("/plans", response_model=PlanCreateResponse)
async def create_plan(
    request: Request,
    body: PlanUpsertRequest,
) -> PlanCreateResponse:
    """创建规划并异步发布：落库即返回，编排结果回写状态。"""
    await _validate_plan_scenes_for_branch(request, body)
    store = _get_store(request)
    await _validate_scene_conflicts(request, store, body)
    record = _build_record(request, body, plan_id=new_plan_id())
    await store.create(record)
    launch_publish(request, store, record.id)
    return PlanCreateResponse(
        id=record.id,
        publish_status=PUBLISH_STATUS_PUBLISHING,
    )


@router.get("/plans", response_model=PlanListResponse)
async def list_plans(request: Request) -> PlanListResponse:
    """看板列表：先限定本行，再按角色和创建/分发关系确定可见范围。"""
    store = _get_store(request)
    viewer = _request_sap_id(request)
    records = await store.list_for_viewer(
        viewer,
        _request_role(request),
        getattr(request.state, "bbk_id", None),
    )
    items = [await _build_view(request, record, viewer) for record in records]
    return PlanListResponse(items=items)


@router.get("/plans/{plan_id}", response_model=PlanView)
async def get_plan(request: Request, plan_id: str) -> PlanView:
    store = _get_store(request)
    viewer = _request_sap_id(request)
    record = await _get_visible_plan(
        store,
        plan_id,
        viewer,
        _request_role(request),
        getattr(request.state, "bbk_id", None),
    )
    return await _build_view(request, record, viewer)


@router.put("/plans/{plan_id}", response_model=PlanCreateResponse)
async def update_plan(
    request: Request,
    plan_id: str,
    body: PlanUpsertRequest,
) -> PlanCreateResponse:
    """整体替换规划并重新发布；仅创建人可操作。"""
    store = _get_store(request)
    viewer = _request_sap_id(request)
    old = await _get_editable_plan(store, plan_id, viewer)
    if old.status == PUBLISH_STATUS_PUBLISHING:
        raise HTTPException(status_code=409, detail="规划发布中，请稍后再修改")
    await _validate_plan_scenes_for_branch(request, body)
    retained_ranges = {
        (scene.scene_id, scene.start_date, scene.end_date)
        for scene in old.scenes
    }
    await _validate_scene_conflicts(
        request,
        store,
        body,
        exclude_plan_id=plan_id,
        scene_ids={
            scene.scene_id
            for scene in body.scenes
            if (scene.scene_id, scene.start_date, scene.end_date)
            not in retained_ranges
        },
    )
    record = _build_record(request, body, plan_id=plan_id)
    _carry_scene_links(old, record)
    await store.update(record)
    await delete_removed_scene_jobs(
        request,
        old,
        {scene.scene_id for scene in record.scenes},
    )
    launch_publish(request, store, plan_id)
    return PlanCreateResponse(
        id=plan_id,
        publish_status=PUBLISH_STATUS_PUBLISHING,
    )


@router.delete("/plans/{plan_id}")
async def delete_plan(request: Request, plan_id: str) -> dict[str, bool]:
    """移除规划：级联删除定时任务与已分发子任务；仅创建人可操作。"""
    store = _get_store(request)
    viewer = _request_sap_id(request)
    record = await _get_editable_plan(store, plan_id, viewer)
    if record.status == PUBLISH_STATUS_PUBLISHING:
        # 发布编排进行中删除会产生孤儿定时任务（后台任务仍会继续建 job）
        raise HTTPException(status_code=409, detail="规划发布中，请稍后再移除")
    await cascade_delete(request, store, record)
    await store.delete(plan_id)
    return {"deleted": True}


# ---------------------------------------------------------------------------
# 内部助手
# ---------------------------------------------------------------------------


async def _get_visible_plan(
    store: WealthPlanStore,
    plan_id: str,
    viewer: str,
    role: str,
    bbk_id: str | None,
) -> WealthPlanRecord:
    record = await store.get(plan_id)
    if record is None:
        raise HTTPException(status_code=404, detail="plan not found")
    if not bbk_id or record.bbk_id != bbk_id:
        raise HTTPException(status_code=403, detail="forbidden")
    if (
        role == ROLE_PRESIDENT
        and record.source_label == SOURCE_LABEL_BY_ROLE[ROLE_PRESIDENT]
        and record.sap_id != viewer
    ):
        raise HTTPException(status_code=403, detail="forbidden")
    source_labels = get_visible_source_labels(role)
    if source_labels is not None and record.source_label not in source_labels:
        raise HTTPException(status_code=403, detail="forbidden")
    is_creator = record.sap_id == viewer
    is_target = any(target.sap_id == viewer for target in record.targets)
    if not can_view_branch_wide(role) and not is_creator and not is_target:
        raise HTTPException(status_code=403, detail="forbidden")
    return record


async def _get_editable_plan(
    store: WealthPlanStore,
    plan_id: str,
    viewer: str,
) -> WealthPlanRecord:
    record = await store.get(plan_id)
    if record is None:
        raise HTTPException(status_code=404, detail="plan not found")
    if record.sap_id != viewer:
        raise HTTPException(status_code=403, detail="仅创建人可以操作该规划")
    return record


def _build_record(
    request: Request,
    body: PlanUpsertRequest,
    plan_id: str,
) -> WealthPlanRecord:
    state = request.state
    starts = sorted(s.start_date for s in body.scenes if s.start_date)
    ends = sorted(s.end_date for s in body.scenes if s.end_date)
    return WealthPlanRecord(
        id=plan_id,
        sap_id=_request_sap_id(request),
        creator_name=getattr(state, "user_name", None),
        bbk_id=getattr(state, "bbk_id", None),
        source_id=getattr(state, "source_id", None),
        agent_id=getattr(state, "agent_id", None) or "default",
        name=body.name.strip(),
        description=body.description,
        source_label=(
            SOURCE_LABEL_BY_ROLE.get(_request_role(request))
            or body.source_label
        ),
        period_start=starts[0] if starts else None,
        period_end=ends[-1] if ends else None,
        scenes=[
            PlanSceneRecord(
                scene_id=scene.scene_id,
                scene_name=scene.scene_name,
                category=scene.category,
                cron_expr=scene.cron_expr,
                item_id=scene.item_id,
                direction=scene.direction,
                cycle=scene.cycle,
                start_date=scene.start_date,
                end_date=scene.end_date,
                cron_example=scene.cron_example,
                mcp_relations=list(scene.mcp_relations),
                sort_order=index,
            )
            for index, scene in enumerate(body.scenes)
        ],
        targets=[
            PlanTargetRecord(
                sap_id=target.sap_id,
                target_name=target.name,
                sort_order=index,
            )
            for index, target in enumerate(body.targets)
        ],
    )


def _carry_scene_links(
    old: WealthPlanRecord,
    record: WealthPlanRecord,
) -> None:
    """修改规划时沿用既有场景的定时任务 id，编排据此替换而非新建。"""
    links = {s.scene_id: s for s in old.scenes}
    for scene in record.scenes:
        previous = links.get(scene.scene_id)
        if previous is not None:
            scene.cron_job_id = previous.cron_job_id
            scene.broadcast_task_id = previous.broadcast_task_id


async def _build_view(
    request: Request,
    record: WealthPlanRecord,
    viewer: str,
) -> PlanView:
    board_status = await _board_status(request, record, viewer)
    return PlanView(
        id=record.id,
        name=record.name,
        description=record.description,
        source_label=record.source_label,
        period_start=record.period_start,
        period_end=record.period_end,
        publish_status=record.status,
        board_status=board_status,
        publish_error=record.publish_error,
        editable=record.sap_id == viewer,
        scenes=[
            PlanSceneView(
                scene_id=scene.scene_id,
                scene_name=scene.scene_name,
                category=scene.category,
                category_label=CATEGORY_LABEL_BY_CODE.get(
                    scene.category,
                    scene.category,
                ),
                item_id=scene.item_id,
                direction=scene.direction,
                cycle=scene.cycle,
                start_date=scene.start_date,
                end_date=scene.end_date,
                cron_expr=scene.cron_expr,
                cron_example=scene.cron_example,
                mcp_relations=list(scene.mcp_relations),
            )
            for scene in record.scenes
        ],
        targets=[
            PlanTargetView(sap_id=t.sap_id, name=t.target_name)
            for t in record.targets
        ],
        created_at=_fmt_dt(record.created_at),
        updated_at=_fmt_dt(record.updated_at),
        published_at=_fmt_dt(record.published_at),
    )


async def _board_status(
    request: Request,
    record: WealthPlanRecord,
    viewer: str,
) -> str:
    """状态栏口径：发布生命周期 + 各场景广播分发状态聚合。"""
    if record.status == PUBLISH_STATUS_PUBLISHING:
        return BOARD_STATUS_PUBLISHING
    if record.status == PUBLISH_STATUS_FAILED:
        return BOARD_STATUS_PUBLISH_FAILED
    legs = await _distribution_legs(request, record, viewer)
    if not legs:
        return BOARD_STATUS_DISTRIBUTED
    if any(leg == _SNAP_FAILED for leg in legs):
        return BOARD_STATUS_DISTRIBUTE_FAILED
    if any(leg == _SNAP_RUNNING for leg in legs):
        return BOARD_STATUS_DISTRIBUTING
    return BOARD_STATUS_DISTRIBUTED


async def _distribution_legs(
    request: Request,
    record: WealthPlanRecord,
    viewer: str,
) -> list[str]:
    """逐场景汇总定时任务广播腿状态；无广播任务的场景（仅发给自己）视为成功。"""
    store = getattr(request.app.state, "cron_broadcast_task_store", None)
    is_creator = record.sap_id == viewer
    legs: list[str] = []
    for scene in record.scenes:
        if not scene.broadcast_task_id:
            continue
        legs.append(await _scene_leg_status(store, scene, viewer, is_creator))
    skill_leg = await _skill_leg_status(request, record)
    if skill_leg:
        legs.append(skill_leg)
    return legs


async def _skill_leg_status(
    request: Request,
    record: WealthPlanRecord,
) -> str | None:
    """技能/MCP 分发腿状态；非终态时向 market 服务刷新一次并回写。"""
    status = record.skill_dispatch_status
    if not status:
        return None
    if status not in TERMINAL_BATCH_STATUSES and record.skill_dispatch_task_id:
        fresh = await query_batch_status(
            record.source_id or "",
            record.skill_dispatch_task_id,
        )
        if fresh:
            status = fresh
            store = _get_store(request)
            await store.set_skill_dispatch(
                record.id,
                record.skill_dispatch_task_id,
                fresh,
                record.skill_dispatch_error,
            )
    if status == BATCH_STATUS_SUCCEEDED:
        return _SNAP_COMPLETED
    if status in (BATCH_STATUS_FAILED, BATCH_STATUS_PARTIAL_FAILED):
        return _SNAP_FAILED
    return _SNAP_RUNNING


async def _scene_leg_status(
    store: CronBroadcastTaskStore | None,
    scene: PlanSceneRecord,
    viewer: str,
    is_creator: bool,
) -> str:
    if store is None:
        return _SNAP_COMPLETED
    snapshot = await store.get_task(scene.broadcast_task_id or "")
    if snapshot is None:
        return _SNAP_COMPLETED
    if is_creator:
        return _aggregate_snapshot_status(snapshot.status)
    return _recipient_leg_status(snapshot, viewer)


def _aggregate_snapshot_status(status: str) -> str:
    if status == _SNAP_FAILED:
        return _SNAP_FAILED
    if status == _SNAP_RUNNING:
        return _SNAP_RUNNING
    return _SNAP_COMPLETED


def _recipient_leg_status(snapshot: Any, viewer: str) -> str:
    for result in snapshot.results:
        if result.get("tenant_id") == viewer:
            return _SNAP_COMPLETED if result.get("success") else _SNAP_FAILED
    if snapshot.status == _SNAP_RUNNING:
        return _SNAP_RUNNING
    return _SNAP_FAILED
