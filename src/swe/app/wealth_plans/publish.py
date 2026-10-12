# -*- coding: utf-8 -*-
"""财富规划的发布编排与级联清理。

发布 = 每个已选场景创建一个定时任务（CronJobSpec），
再复用 cron 广播机制分发到各分发目标租户，全部异步执行；
创建接口落库后立即返回，编排结果回写规划状态与场景关联 id。
"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from fastapi import Request

from ..crons.api import (
    CronBroadcastRequest,
    _bind_workflow_config,
    _build_broadcast_context,
    _build_child_management_context,
    _list_broadcast_children_for_tenants,
    _normalize_broadcast_targets,
    _schedule_broadcast_task,
    _workflow_store,
    get_cron_manager,
)
from ..crons.models import (
    CronJobRequest,
    CronJobSpec,
    DispatchSpec,
    DispatchTarget,
    ScheduleSpec,
)
from .models import (
    PUBLISH_STATUS_FAILED,
    PUBLISH_STATUS_PUBLISHED,
    WEALTH_TIMEZONE,
    PlanSceneRecord,
    WealthPlanRecord,
)
from .market_dispatch import submit_distribution
from .store import WealthPlanStore, _now

logger = logging.getLogger(__name__)

_TASK_NAME_SEP = "｜"


def launch_publish(
    request: Request,
    store: WealthPlanStore,
    plan_id: str,
) -> None:
    """后台启动发布编排；失败结果写入规划状态，不抛给调用方。"""
    task = asyncio.create_task(
        _run_publish(request, store, plan_id),
        name=f"wealth-plan-publish-{plan_id}",
    )
    tasks = _background_tasks(request)
    tasks[plan_id] = task
    task.add_done_callback(lambda _task: tasks.pop(plan_id, None))


async def cascade_delete(
    request: Request,
    store: WealthPlanStore,  # noqa: ARG001 预留：清理技能/MCP 分发时回写
    plan: WealthPlanRecord,
) -> None:
    """移除规划时级联删除各场景的定时任务与已分发子任务。"""
    for scene in plan.scenes:
        if scene.cron_job_id:
            await _delete_scene_job(request, plan, scene)


async def delete_removed_scene_jobs(
    request: Request,
    old_plan: WealthPlanRecord,
    new_scene_ids: set[str],
) -> None:
    """修改规划时，清理被移除场景的定时任务与已分发子任务。"""
    for scene in old_plan.scenes:
        if scene.scene_id not in new_scene_ids and scene.cron_job_id:
            await _delete_scene_job(request, old_plan, scene)


def _background_tasks(request: Request) -> dict[str, asyncio.Task]:
    tasks = getattr(request.app.state, "wealth_plan_tasks", None)
    if tasks is None:
        tasks = {}
        request.app.state.wealth_plan_tasks = tasks
    return tasks


async def _run_publish(
    request: Request,
    store: WealthPlanStore,
    plan_id: str,
) -> None:
    plan = await store.get(plan_id)
    if plan is None:
        logger.warning("wealth plan %s vanished before publish", plan_id)
        return
    try:
        await _publish_all_scenes(request, store, plan)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("wealth plan %s publish failed", plan_id)
        await store.set_publish_status(
            plan_id,
            PUBLISH_STATUS_FAILED,
            str(exc),
        )
        return
    await _dispatch_skills(store, plan)
    await store.set_publish_status(
        plan_id,
        PUBLISH_STATUS_PUBLISHED,
        published_at=_now(),
    )


async def _dispatch_skills(
    store: WealthPlanStore,
    plan: WealthPlanRecord,
) -> None:
    """技能/MCP 分发腿：提交 market 批量分发并回写批次状态。

    该腿失败不翻整体发布状态（定时任务腿已成功），
    以 skill_dispatch_status 独立跟踪，由看板状态聚合呈现。
    """
    try:
        outcome = await submit_distribution(plan)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("wealth plan %s skill dispatch failed", plan.id)
        await store.set_skill_dispatch(plan.id, "", "failed", str(exc))
        return
    if outcome is None:
        return
    batch_id, status = outcome
    await store.set_skill_dispatch(plan.id, batch_id, status, None)


async def _publish_all_scenes(
    request: Request,
    store: WealthPlanStore,
    plan: WealthPlanRecord,
) -> None:
    mgr = await get_cron_manager(request)
    for scene in plan.scenes:
        job_id = scene.cron_job_id or str(uuid4())
        job = _build_job_spec(request, plan, scene, job_id)
        if job.task_type == "workflow":
            job = await _bind_workflow_config(job, _workflow_store(request))
        await mgr.create_or_replace_job(job)
        broadcast_task_id = await _broadcast_scene_job(
            request,
            plan,
            job,
        )
        await store.set_scene_links(
            plan.id,
            scene.scene_id,
            job.id,
            broadcast_task_id or scene.broadcast_task_id or "",
        )


def _build_job_spec(
    request: Request,
    plan: WealthPlanRecord,
    scene: PlanSceneRecord,
    job_id: str,
) -> CronJobSpec:
    state = request.state
    task_text = _build_task_text(plan, scene)
    return CronJobSpec(
        id=job_id,
        name=f"{plan.name}{_TASK_NAME_SEP}{scene.scene_name}",
        tenant_id=getattr(state, "tenant_id", None),
        bbk_id=getattr(state, "bbk_id", None),
        source_id=getattr(state, "source_id", None),
        scope_id=getattr(state, "scope_id", None),
        plan_id=plan.id,
        tenant_name=getattr(state, "user_name", None),
        schedule=ScheduleSpec(cron=scene.cron_expr, timezone=WEALTH_TIMEZONE),
        task_type="agent" if scene.mcp_relations else "workflow",
        text=task_text,
        request=CronJobRequest(
            input=_build_request_input(plan, scene),
            user_id=plan.sap_id,
        ),
        skill_ids=scene.scene_id,
        dispatch=DispatchSpec(
            target=DispatchTarget(user_id=plan.sap_id, session_id=""),
        ),
        meta={
            "creator_user_id": plan.sap_id,
            "wealth_plan_id": plan.id,
            "wealth_scene_id": scene.scene_id,
            "wealth_item_id": scene.item_id or "",
            "wealth_mcp_relations": list(scene.mcp_relations),
        },
    )


def _build_task_text(plan: WealthPlanRecord, scene: PlanSceneRecord) -> str:
    """任务文本 = 规划名 + 场景名 + 经营方向 + 有效期（展示与兜底用）。"""
    parts = [f"经营规划「{plan.name}」场景「{scene.scene_name}」。"]
    if scene.direction:
        parts.append(f"经营方向：{scene.direction}。")
    if scene.start_date and scene.end_date:
        parts.append(
            f"有效期：{scene.start_date} 至 {scene.end_date}，"
            "仅在该期间内生成经营名单。",
        )
    return "".join(parts)


def _build_request_input(
    plan: WealthPlanRecord,
    scene: PlanSceneRecord,
) -> list[dict]:
    """定时任务「请求内容」：固定 message 结构，text 取场景技能的 cronExample。

    cronExample 缺失时退化为任务文本，避免发出空指令。
    """
    text = (scene.cron_example or "").strip() or _build_task_text(plan, scene)
    return [
        {
            "content": [{"text": text, "type": "text"}],
            "role": "user",
            "type": "message",
        },
    ]


async def _broadcast_scene_job(
    request: Request,
    plan: WealthPlanRecord,
    job: CronJobSpec,
) -> str | None:
    """把场景任务广播到分发目标；目标仅创建人本人时跳过（源任务即在本人租户）。"""
    target_ids = [t.sap_id for t in plan.targets if t.sap_id != plan.sap_id]
    if not target_ids:
        return None
    body = CronBroadcastRequest(target_tenant_ids=target_ids)
    normalized, identity_by_tenant = _normalize_broadcast_targets(body)
    context = _build_broadcast_context(
        request,
        job,
        normalized,
        identity_by_tenant,
    )
    snapshot, _reused = await _schedule_broadcast_task(
        request,
        job,
        context,
        normalized,
    )
    return snapshot.task_id


async def _delete_scene_job(
    request: Request,
    plan: WealthPlanRecord,
    scene: PlanSceneRecord,
) -> None:
    """删除场景源任务及分发到各目标租户的子任务（尽力而为）。"""
    mgr = await get_cron_manager(request)
    source_job = await mgr.get_job(scene.cron_job_id or "")
    if source_job is None:
        return
    target_ids = [t.sap_id for t in plan.targets if t.sap_id != plan.sap_id]
    if target_ids:
        await _delete_broadcast_children(request, source_job, target_ids)
    await mgr.delete_job(source_job.id)


async def _delete_broadcast_children(
    request: Request,
    source_job: CronJobSpec,
    target_ids: list[str],
) -> None:
    from ..crons.api import _get_target_cron_manager

    context = await _build_child_management_context(
        request,
        source_job,
        target_ids,
    )
    children = await _list_broadcast_children_for_tenants(context, target_ids)
    for child in children:
        try:
            target_mgr, _ = await _get_target_cron_manager(
                context,
                child.tenant_id,
                bootstrap=False,
            )
            await target_mgr.delete_job(child.job_id)
        except Exception:  # pylint: disable=broad-except
            logger.warning(
                "delete wealth broadcast child failed: tenant=%s job=%s",
                child.tenant_id,
                child.job_id,
            )
