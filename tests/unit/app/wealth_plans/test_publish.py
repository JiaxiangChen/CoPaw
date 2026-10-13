# -*- coding: utf-8 -*-
"""发布编排的纯逻辑单元测试（不触碰真实 CronManager）。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.wealth_plans.models import PlanSceneRecord, WealthPlanRecord
from swe.app.wealth_plans.publish import (
    _build_job_spec,
    _build_request_input,
    _build_task_text,
    _broadcast_scene_job,
)


def make_scene(**overrides) -> PlanSceneRecord:
    scene = PlanSceneRecord(
        scene_id="skill-wealth-insurance-1",
        scene_name="保障潜客经营",
        category="insurance",
        cron_expr="30 8 * * mon,thu",
        direction="挖掘高潜保险客户",
        start_date="2026-09-01",
        end_date="2026-09-30",
        mcp_relations=["mcp-customer"],
    )
    for key, value in overrides.items():
        setattr(scene, key, value)
    return scene


def make_plan(targets: list[str]) -> WealthPlanRecord:
    return WealthPlanRecord(
        id="plan-1",
        sap_id="zhangwl",
        name="九月计划",
        scenes=[make_scene()],
        targets=[SimpleNamespace(sap_id=t) for t in targets],  # type: ignore[arg-type]
    )


def fake_request() -> SimpleNamespace:
    return SimpleNamespace(
        headers={},
        app=SimpleNamespace(
            state=SimpleNamespace(
                workflow_config_store=SimpleNamespace(
                    resolve_first_skill=AsyncMock(
                        return_value=SimpleNamespace(binding_id="binding-1"),
                    ),
                ),
            ),
        ),
        state=SimpleNamespace(
            tenant_id="zhangwl",
            bbk_id="100",
            source_id="RMASSIST",
            scope_id="scope-1",
            user_name="张**",
            agent_id=None,
        ),
    )


def test_build_task_text_composes_name_direction_and_period() -> None:
    text = _build_task_text(make_plan([]), make_scene())

    assert "经营规划「九月计划」场景「保障潜客经营」。" in text
    assert "经营方向：挖掘高潜保险客户。" in text
    assert "有效期：2026-09-01 至 2026-09-30" in text


def test_build_task_text_skips_optional_parts() -> None:
    scene = make_scene(direction=None, start_date=None, end_date=None)

    text = _build_task_text(make_plan([]), scene)

    assert "经营方向" not in text
    assert "有效期" not in text


def test_build_request_input_wraps_cron_example_in_message() -> None:
    scene = make_scene(cron_example="每日生成高潜保险客户名单")

    messages = _build_request_input(make_plan([]), scene)

    assert messages == [
        {
            "content": [{"text": "每日生成高潜保险客户名单", "type": "text"}],
            "role": "user",
            "type": "message",
        },
    ]


def test_build_request_input_falls_back_to_task_text() -> None:
    scene = make_scene(cron_example=None)

    messages = _build_request_input(make_plan([]), scene)

    assert messages[0]["content"][0]["text"] == _build_task_text(
        make_plan([]),
        scene,
    )


def test_build_job_spec_request_input_uses_cron_example() -> None:
    job = _build_job_spec(
        fake_request(),
        make_plan(["chenjy"]),
        make_scene(cron_example="每日生成高潜保险客户名单"),
        "job-1",
    )

    assert job.request.input == [
        {
            "content": [{"text": "每日生成高潜保险客户名单", "type": "text"}],
            "role": "user",
            "type": "message",
        },
    ]
    assert job.request.user_id == "zhangwl"


def test_build_job_spec_maps_identity_and_schedule() -> None:
    job = _build_job_spec(
        fake_request(),
        make_plan(["chenjy"]),
        make_scene(),
        "job-1",
    )

    assert job.id == "job-1"
    assert job.name == "九月计划｜保障潜客经营"
    assert job.tenant_id == "zhangwl"
    assert job.bbk_id == "100"
    assert job.schedule.cron == "30 8 * * mon,thu"
    assert job.schedule.timezone == "Asia/Shanghai"
    assert job.skill_ids == "skill-wealth-insurance-1"
    assert job.plan_id == "plan-1"
    assert job.model_dump()["plan_id"] == "plan-1"
    assert job.meta["wealth_plan_id"] == "plan-1"
    assert job.meta["wealth_item_id"] == ""
    assert job.meta["wealth_mcp_relations"] == ["mcp-customer"]
    assert job.dispatch.target.user_id == "zhangwl"


def test_scene_without_mcp_creates_workflow_job() -> None:
    job = _build_job_spec(
        fake_request(),
        make_plan([]),
        make_scene(mcp_relations=[]),
        "job-1",
    )

    assert job.task_type == "workflow"
    assert job.skill_ids == "skill-wealth-insurance-1"
    assert job.request is None
    assert job.text is None
    assert job.plan_id == "plan-1"


@pytest.mark.parametrize(
    "relations",
    [["mcp-customer"], ["    "]],
)
def test_scene_with_mcp_keeps_agent_job(relations) -> None:
    job = _build_job_spec(
        fake_request(),
        make_plan([]),
        make_scene(mcp_relations=relations, cron_example="生成客户名单"),
        "job-1",
    )

    assert job.task_type == "agent"
    assert job.request.input[0]["content"][0]["text"] == "生成客户名单"
    assert job.request.user_id == "zhangwl"


async def test_workflow_plan_publishes_without_market_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from swe.app.wealth_plans import market_dispatch, publish
    from swe.app.wealth_plans.store import WealthPlanStore

    monkeypatch.setenv("SWE_MARKET_API_BASE_URL", "http://market.local")
    jobs = []

    async def create_job(job):
        jobs.append(job)

    async def get_manager(_request):
        return SimpleNamespace(create_or_replace_job=create_job)

    def unexpected_client():
        pytest.fail("workflow 规划不能调用技能/MCP 下发接口")

    monkeypatch.setattr(publish, "get_cron_manager", get_manager)
    monkeypatch.setattr(market_dispatch, "_new_client", unexpected_client)
    plan = make_plan([])
    plan.source_id = "RMASSIST"
    plan.scenes = [make_scene(item_id="skill-item", mcp_relations=[])]
    store = WealthPlanStore()
    await store.create(plan)

    await publish._run_publish(fake_request(), store, plan.id)

    record = await store.get(plan.id)
    assert record.status == "published"
    assert len(jobs) == 1
    assert jobs[0].task_type == "workflow"
    assert record.scenes[0].cron_job_id == jobs[0].id
    assert record.skill_dispatch_task_id is None


async def test_broadcast_skipped_when_only_creator_is_target() -> None:
    # request 传 None：若未提前返回则会立刻报错，证明确实跳过了广播
    result = await _broadcast_scene_job(
        None,  # type: ignore[arg-type]
        make_plan(["zhangwl"]),
        SimpleNamespace(id="job-1"),
    )

    assert result is None


@pytest.mark.parametrize("batch_window", [None, 4, 0])
@pytest.mark.parametrize("relations", [[], ["mcp-customer"]])
async def test_broadcast_invoked_for_other_targets(
    monkeypatch: pytest.MonkeyPatch,
    batch_window: int | None,
    relations: list[str],
) -> None:
    from swe.app.crons import api as cron_api
    from swe.app.wealth_plans import publish

    plan = make_plan(["zhangwl", "chenjy"])
    job = _build_job_spec(
        fake_request(),
        plan,
        make_scene(mcp_relations=relations),
        "job-1",
    )
    if batch_window is not None:
        job.meta["broadcast_dispatch_intents_enabled"] = True
        job.meta["batch_dispatch_offset_window_hours"] = batch_window
    request = fake_request()
    request.app = SimpleNamespace(state=SimpleNamespace())
    manager = SimpleNamespace(get_job=AsyncMock(return_value=job))
    monkeypatch.setattr(
        publish,
        "get_cron_manager",
        AsyncMock(return_value=manager),
    )
    apply_batch = AsyncMock()
    monkeypatch.setattr(
        cron_api,
        "_apply_batch_dispatch_after_broadcast",
        apply_batch,
    )
    contexts = []

    def build_context(_request, _job, targets, identities, **options):
        contexts.append(options)
        assert targets == ["chenjy"]
        return SimpleNamespace()

    async def schedule(
        _request,
        source,
        context,
        targets,
        post_broadcast=None,
    ):
        assert source is job
        assert targets == ["chenjy"]
        if post_broadcast:
            await post_broadcast()
        return (
            cron_api.CronBroadcastTaskSnapshot(
                task_id="broadcast-1",
                agent_id="default",
                source_id="RMASSIST",
                tenant_id="zhangwl",
                job_id=job.id,
                target_key="targets",
                tenant_count=len(targets),
            ),
            False,
        )

    monkeypatch.setattr(cron_api, "_build_broadcast_context", build_context)
    monkeypatch.setattr(cron_api, "_schedule_broadcast_task", schedule)
    assert await _broadcast_scene_job(request, plan, job) == "broadcast-1"
    assert contexts[0]["enable_offset"] is False
    assert contexts[0]["offset_window_hours"] == 0
    if batch_window == 0:
        assert contexts[0]["enable_batch_dispatch"] is True
        apply_batch.assert_not_awaited()
    else:
        apply_batch.assert_awaited_once()
        assert apply_batch.await_args.kwargs["enable"] is True
        assert apply_batch.await_args.kwargs["offset_window_hours"] == 0
        assert apply_batch.await_args.kwargs["tenant_ids"] == ["chenjy"]
