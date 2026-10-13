# -*- coding: utf-8 -*-
"""WealthPlanStore 内存模式单元测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from swe.app.wealth_plans.models import (
    PUBLISH_STATUS_FAILED,
    PUBLISH_STATUS_PUBLISHED,
    PUBLISH_STATUS_PUBLISHING,
    PlanSceneRecord,
    PlanTargetRecord,
    WealthPlanRecord,
)
from swe.app.wealth_plans.store import WealthPlanStore, new_plan_id


@pytest.mark.asyncio
@pytest.mark.parametrize("database", [False, True])
@pytest.mark.parametrize(
    "start,end,conflict",
    [
        ("2026-10-01", "2026-10-31", False),
        ("2026-08-01", "2026-08-31", False),
        ("2026-09-30", "2026-10-31", True),
        ("2026-08-01", "2026-09-01", True),
        ("2026-09-10", "2026-09-20", True),
        ("2026-08-01", "2026-10-31", True),
        (None, None, True),
    ],
)
async def test_scene_conflicts_use_task_date_overlap(
    database: bool,
    start: str | None,
    end: str | None,
    conflict: bool,
) -> None:
    plan = make_plan()
    scene = plan.scenes[0]
    scene.start_date, scene.end_date = "2026-09-01", "2026-09-30"
    db = AsyncMock() if database else None
    store = WealthPlanStore(db)
    if database:
        db.fetch_all.return_value = [
            {
                "scene_id": scene.scene_id,
                "plan_name": plan.name,
                "start_date": scene.start_date,
                "end_date": scene.end_date,
            },
        ]
    else:
        await store.create(plan)
    conflicts = await store.find_scene_conflicts(
        "rm",
        "zhangwl",
        "100",
        {scene.scene_id},
        scene_ranges={scene.scene_id: (start, end)},
    )
    assert bool(conflicts) is conflict


def make_plan(
    plan_id: str = "plan-1",
    sap_id: str = "zhangwl",
    targets: list[str] | None = None,
    bbk_id: str = "100",
    source_label: str = "行长关注",
    scene_id: str = "skill-wealth-insurance-1",
) -> WealthPlanRecord:
    return WealthPlanRecord(
        id=plan_id,
        sap_id=sap_id,
        bbk_id=bbk_id,
        name="九月重点客户经营计划",
        source_label=source_label,
        scenes=[
            PlanSceneRecord(
                scene_id=scene_id,
                scene_name="保障潜客经营",
                category="insurance",
                cron_expr="0 9 * * *",
                cron_example="每日生成高潜保险客户名单",
                direction="挖掘高潜保险客户",
                mcp_relations=["mcp-customer", "mcp-insurance"],
            ),
        ],
        targets=[
            PlanTargetRecord(sap_id=t, target_name=f"用户{t}", sort_order=i)
            for i, t in enumerate(targets or [])
        ],
    )


@pytest.mark.asyncio
async def test_create_and_get_roundtrip() -> None:
    store = WealthPlanStore()
    await store.create(make_plan(targets=["chenjy", "liuxt"]))

    record = await store.get("plan-1")

    assert record is not None
    assert record.sap_id == "zhangwl"
    assert record.status == PUBLISH_STATUS_PUBLISHING
    assert record.scenes[0].mcp_relations == ["mcp-customer", "mcp-insurance"]
    assert record.scenes[0].cron_example == "每日生成高潜保险客户名单"
    assert [t.sap_id for t in record.targets] == ["chenjy", "liuxt"]
    assert record.created_at is not None


@pytest.mark.asyncio
async def test_list_for_viewer_scopes_personal_roles_by_branch_and_relation() -> (
    None
):
    store = WealthPlanStore()
    await store.create(
        make_plan("plan-1", sap_id="zhangwl", targets=["chenjy"]),
    )
    await store.create(make_plan("plan-2", sap_id="liuxt", targets=[]))
    await store.create(
        make_plan(
            "plan-3",
            sap_id="waibu",
            targets=["zhangwl", "chenjy"],
            bbk_id="200",
        ),
    )

    creator_view = await store.list_for_viewer("zhangwl", "rm", "100")
    target_view = await store.list_for_viewer("chenjy", "rm", "100")
    rm_outsider_view = await store.list_for_viewer("wangly", "rm", "100")
    unknown_outsider_view = await store.list_for_viewer(
        "wangly",
        "unknown",
        "100",
    )
    other_branch_view = await store.list_for_viewer("zhangwl", "rm", "200")
    no_bbk_view = await store.list_for_viewer("zhangwl", "rm", None)

    assert {r.id for r in creator_view} == {"plan-1"}
    assert {r.id for r in target_view} == {"plan-1"}
    assert rm_outsider_view == []
    assert unknown_outsider_view == []
    assert {r.id for r in other_branch_view} == {"plan-3"}
    assert no_bbk_view == []


@pytest.mark.asyncio
async def test_find_scene_conflicts_reserves_management_and_own_plans() -> (
    None
):
    store = WealthPlanStore()
    plans = [
        make_plan("president", sap_id="president", scene_id="scene-president"),
        make_plan(
            "middle",
            sap_id="middle",
            source_label="分行关注",
            scene_id="scene-middle",
        ),
        make_plan(
            "self",
            sap_id="rm-1",
            source_label="我的关注",
            scene_id="scene-self",
        ),
        make_plan(
            "other-rm",
            sap_id="rm-2",
            source_label="我的关注",
            scene_id="scene-other-rm",
        ),
        make_plan(
            "other-branch",
            sap_id="president-2",
            bbk_id="200",
            scene_id="scene-other-branch",
        ),
        make_plan(
            "failed",
            sap_id="president-3",
            scene_id="scene-failed",
        ),
    ]
    for plan in plans:
        await store.create(plan)
    await store.set_publish_status("failed", PUBLISH_STATUS_FAILED)

    conflicts = await store.find_scene_conflicts(
        "rm",
        "rm-1",
        "100",
        {
            "scene-president",
            "scene-middle",
            "scene-self",
            "scene-other-rm",
            "scene-other-branch",
            "scene-failed",
        },
    )

    assert conflicts == {
        "scene-president": "九月重点客户经营计划",
        "scene-middle": "九月重点客户经营计划",
        "scene-self": "九月重点客户经营计划",
    }


@pytest.mark.asyncio
async def test_find_scene_conflicts_excludes_plan_being_edited() -> None:
    store = WealthPlanStore()
    await store.create(
        make_plan(
            "self",
            sap_id="rm-1",
            source_label="我的关注",
            scene_id="scene-self",
        ),
    )

    conflicts = await store.find_scene_conflicts(
        "rm",
        "rm-1",
        "100",
        {"scene-self"},
        exclude_plan_id="self",
    )

    assert conflicts == {}


@pytest.mark.asyncio
async def test_find_scene_conflicts_database_query_scopes_reservations() -> (
    None
):
    db = AsyncMock()
    db.fetch_all.return_value = [
        {"scene_id": "scene-1", "plan_name": "行长重点规划"},
    ]
    store = WealthPlanStore(db)

    conflicts = await store.find_scene_conflicts(
        "rm",
        "rm-1",
        "100",
        {"scene-1"},
        exclude_plan_id="plan-self",
    )

    assert conflicts == {"scene-1": "行长重点规划"}
    sql, params = db.fetch_all.await_args.args
    normalized_sql = " ".join(sql.split())
    assert "p.bbk_id = %s" in normalized_sql
    assert "p.status <> %s" in normalized_sql
    assert "p.sap_id = %s OR p.source_label IN (%s, %s)" in normalized_sql
    assert "s.scene_id IN (%s)" in normalized_sql
    assert "p.id <> %s" in normalized_sql
    assert params == (
        "100",
        PUBLISH_STATUS_FAILED,
        "rm-1",
        "分行关注",
        "行长关注",
        "scene-1",
        "plan-self",
    )


@pytest.mark.asyncio
async def test_list_for_viewer_branch_wide_for_president_and_middle() -> None:
    store = WealthPlanStore()
    await store.create(
        make_plan("plan-1", sap_id="zhangwl", targets=["chenjy"]),
    )
    await store.create(make_plan("plan-2", sap_id="liuxt", targets=[]))
    other_branch = make_plan(
        "plan-3",
        sap_id="waibu",
        targets=[],
        bbk_id="200",
    )
    await store.create(other_branch)

    president_view = await store.list_for_viewer("wangly", "president", "100")
    middle_view = await store.list_for_viewer("wangly", "middle", "100")
    rm_view = await store.list_for_viewer("wangly", "rm", "100")
    no_bbk_view = await store.list_for_viewer("wangly", "president", None)

    assert president_view == []
    assert {
        r.id
        for r in await store.list_for_viewer("zhangwl", "president", "100")
    } == {"plan-1"}
    assert {
        r.id for r in await store.list_for_viewer("liuxt", "president", "100")
    } == {"plan-2"}
    assert middle_view == []
    assert rm_view == []
    assert no_bbk_view == []


@pytest.mark.asyncio
async def test_list_for_viewer_database_query_combines_branch_and_relation() -> (
    None
):
    db = AsyncMock()
    db.fetch_all.return_value = []
    store = WealthPlanStore(db)

    assert await store.list_for_viewer("wangly", "unknown", "100") == []

    sql, params = db.fetch_all.await_args.args
    normalized_sql = " ".join(sql.split())
    assert "WHERE p.bbk_id = %s" in normalized_sql
    assert "p.sap_id = %s OR t.sap_id = %s" in normalized_sql
    assert params == ("100", "wangly", "wangly")


@pytest.mark.asyncio
async def test_list_for_viewer_database_query_is_branch_wide_for_management() -> (
    None
):
    db = AsyncMock()
    db.fetch_all.return_value = []
    store = WealthPlanStore(db)

    assert await store.list_for_viewer("wangly", "president", "100") == []

    sql, params = db.fetch_all.await_args.args
    normalized_sql = " ".join(sql.split())
    assert "WHERE p.bbk_id = %s" in normalized_sql
    assert "p.source_label <> %s OR p.sap_id = %s" in normalized_sql
    assert "p.source_label IN (%s, %s)" in normalized_sql
    assert params == ("100", "分行关注", "行长关注", "行长关注", "wangly")


@pytest.mark.asyncio
async def test_update_replaces_children_and_resets_status() -> None:
    store = WealthPlanStore()
    plan = make_plan(targets=["chenjy"])
    await store.create(plan)
    await store.set_publish_status("plan-1", PUBLISH_STATUS_PUBLISHED)

    updated = make_plan(targets=["zhaom"])
    updated.name = "十月计划"
    updated.scenes[0].cron_job_id = "job-keep"  # 沿用既有任务 id
    await store.update(updated)

    record = await store.get("plan-1")
    assert record is not None
    assert record.name == "十月计划"
    assert record.status == PUBLISH_STATUS_PUBLISHING
    assert [t.sap_id for t in record.targets] == ["zhaom"]
    assert record.scenes[0].cron_job_id == "job-keep"


@pytest.mark.asyncio
async def test_update_clears_skill_dispatch_columns() -> None:
    store = WealthPlanStore()
    await store.create(make_plan())
    await store.set_skill_dispatch(
        "plan-1",
        batch_id="wealth-plan-plan-1-abcd1234",
        status="partial",
        error="部分目标分发失败",
    )

    updated = make_plan()
    updated.name = "十月计划"
    await store.update(updated)

    record = await store.get("plan-1")
    assert record is not None
    assert record.skill_dispatch_task_id is None
    assert record.skill_dispatch_status is None
    assert record.skill_dispatch_error is None


@pytest.mark.asyncio
async def test_delete_removes_plan() -> None:
    store = WealthPlanStore()
    await store.create(make_plan())

    await store.delete("plan-1")

    assert await store.get("plan-1") is None
    assert await store.list_for_viewer("zhangwl", "rm", None) == []


@pytest.mark.asyncio
async def test_set_scene_links_writes_job_and_broadcast_ids() -> None:
    store = WealthPlanStore()
    await store.create(make_plan())

    await store.set_scene_links(
        "plan-1",
        "skill-wealth-insurance-1",
        "job-1",
        "task-1",
    )

    record = await store.get("plan-1")
    assert record is not None
    assert record.scenes[0].cron_job_id == "job-1"
    assert record.scenes[0].broadcast_task_id == "task-1"


def test_new_plan_id_format() -> None:
    plan_id = new_plan_id()
    assert plan_id.startswith("plan-")
    assert len(plan_id) == len("plan-") + 12


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role,expected",
    [
        ("president", {"branch", "president"}),
        ("middle", {"branch"}),
        ("rm", {"branch", "president", "personal"}),
    ],
)
async def test_list_sources_excludes_lower_role_plans_even_for_creator_or_target(
    role: str,
    expected: set[str],
) -> None:
    store = WealthPlanStore()
    for plan_id, source in [
        ("branch", "分行关注"),
        ("president", "行长关注"),
        ("personal", "我的关注"),
    ]:
        await store.create(
            make_plan(plan_id, sap_id="viewer", source_label=source),
        )
        await store.create(
            make_plan(
                f"outside-{plan_id}",
                sap_id="viewer",
                bbk_id="200",
                source_label=source,
            ),
        )
    await store.create(
        make_plan(
            "target-personal",
            sap_id="other",
            targets=["viewer"],
            source_label="我的关注",
        ),
    )
    records = await store.list_for_viewer("viewer", role, "100")
    if role == "rm":
        expected = expected | {"target-personal"}
    assert {record.id for record in records} == expected


@pytest.mark.asyncio
async def test_middle_database_query_filters_branch_focus_only() -> None:
    db = AsyncMock()
    db.fetch_all.return_value = []
    store = WealthPlanStore(db)
    assert await store.list_for_viewer("viewer", "middle", "100") == []
    sql, params = db.fetch_all.await_args.args
    assert "p.source_label IN (%s)" in " ".join(sql.split())
    assert params == ("100", "分行关注")


@pytest.mark.asyncio
async def test_scene_conflicts_filter_recipient_intersection_in_memory() -> (
    None
):
    store = WealthPlanStore()
    await store.create(make_plan(source_label="分行关注", targets=["a", "b"]))
    args = ("president", "c-president", "100", {"skill-wealth-insurance-1"})
    assert await store.find_scene_conflicts(*args, target_sap_ids={"c"}) == {}
    assert await store.find_scene_conflicts(*args, target_sap_ids=set()) == {}
    assert await store.find_scene_conflicts(*args, target_sap_ids={"a", "c"})


@pytest.mark.asyncio
async def test_scene_conflicts_database_filters_targets_with_exists() -> None:
    db = AsyncMock()
    db.fetch_all.return_value = []
    store = WealthPlanStore(db)
    await store.find_scene_conflicts(
        "president",
        "c-president",
        "100",
        {"scene"},
        target_sap_ids={"c-rm"},
        exclude_plan_id="editing",
    )
    sql, params = db.fetch_all.await_args.args
    assert "AND EXISTS" in sql
    assert "t.plan_id = p.id AND t.sap_id IN (%s)" in sql
    assert params[-2:] == ("c-rm", "editing")
