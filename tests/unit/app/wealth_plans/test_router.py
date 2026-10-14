# -*- coding: utf-8 -*-
"""wealth plans 路由单元测试：CRUD 权限、状态聚合与场景查询兜底。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from swe.app.crons.broadcast_task_store import CronBroadcastTaskStore
from swe.app.wealth_plans import router as wealth_router
from swe.app.wealth_plans.models import PlanUpsertRequest, SceneSkillItem
from swe.app.wealth_plans.store import WealthPlanStore

VIEWER = {"X-User-Id": "zhangwl", "X-Bbk-Id": "100"}


async def test_create_and_update_shared_scene_keep_current_plan_branch(
    monkeypatch: pytest.MonkeyPatch,
    store: WealthPlanStore,
) -> None:
    async def fetch_shared_scene(request, category):
        return [
            SceneSkillItem(
                skillId="skill-wealth-insurance-1",
                senceName="保障潜客经营",
                category="insurance",
                bbkId="100",
            ),
        ]

    monkeypatch.setattr(
        wealth_router,
        "_fetch_external_scene_skills",
        fetch_shared_scene,
    )
    monkeypatch.setattr(wealth_router, "launch_publish", lambda *args: None)
    request = SimpleNamespace(
        state=SimpleNamespace(bbk_id="110"),
        headers=VIEWER,
        app=SimpleNamespace(state=SimpleNamespace(wealth_plan_store=store)),
    )
    body = PlanUpsertRequest(**plan_payload())

    created = await wealth_router.create_plan(request, body)
    await store.set_publish_status(created.id, "published")
    updated = await wealth_router.update_plan(request, created.id, body)

    assert updated.id == created.id
    record = await store.get(created.id)
    assert record.bbk_id == "110"
    assert record.scenes[0].scene_id == "skill-wealth-insurance-1"


@pytest.mark.parametrize(
    "start,end,expected_status",
    [
        ("2026-10-01", "2026-10-31", 200),
        ("2026-09-30", "2026-10-31", 409),
    ],
)
def test_create_same_scene_in_separate_task_periods(
    client: TestClient,
    start: str,
    end: str,
    expected_status: int,
) -> None:
    headers = {**VIEWER, "X-Position-Id": "RB1101"}
    assert (
        client.post(
            "/api/wealth/plans",
            json=plan_payload(),
            headers=headers,
        ).status_code
        == 200
    )
    payload = plan_payload(name="十月规划")
    payload["scenes"][0].update(start_date=start, end_date=end)
    response = client.post(
        "/api/wealth/plans",
        json=payload,
        headers=headers,
    )
    assert response.status_code == expected_status
    if expected_status == 409:
        assert "任务周期重叠" in response.json()["detail"]


async def test_edit_scene_dates_rechecks_overlap(
    client: TestClient,
    store: WealthPlanStore,
) -> None:
    headers = {**VIEWER, "X-Position-Id": "RB1101"}
    september = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers=headers,
    ).json()
    await store.set_publish_status(september["id"], "published")
    october = plan_payload(name="十月规划")
    october["scenes"][0].update(
        start_date="2026-10-01",
        end_date="2026-10-31",
    )
    assert (
        client.post(
            "/api/wealth/plans",
            json=october,
            headers=headers,
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"/api/wealth/plans/{september['id']}",
            json=october,
            headers=headers,
        ).status_code
        == 409
    )
    november = plan_payload(name="十一月规划")
    november["scenes"][0].update(
        start_date="2026-11-01",
        end_date="2026-11-30",
    )
    assert (
        client.put(
            f"/api/wealth/plans/{september['id']}",
            json=november,
            headers=headers,
        ).status_code
        == 200
    )


@pytest.fixture()
def store() -> WealthPlanStore:
    return WealthPlanStore()


@pytest.fixture()
def app(
    store: WealthPlanStore,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    launches: list[str] = []
    cascades: list[str] = []

    def fake_launch(request, _store, plan_id: str) -> None:
        launches.append(plan_id)

    async def fake_cascade(request, _store, plan) -> None:
        cascades.append(plan.id)

    monkeypatch.setattr(wealth_router, "launch_publish", fake_launch)
    monkeypatch.setattr(wealth_router, "cascade_delete", fake_cascade)

    async def allow_scenes_for_unrelated_tests(request, body) -> None:
        return None

    monkeypatch.setattr(
        wealth_router,
        "_validate_plan_scenes_for_branch",
        allow_scenes_for_unrelated_tests,
        raising=False,
    )

    application = FastAPI()
    application.state.wealth_plan_store = store
    application.state.wealth_plan_launches = launches
    application.state.wealth_plan_cascades = cascades

    @application.middleware("http")
    async def _bbk_state(request, call_next):  # 模拟租户中间件的 bbk 注入
        bbk = request.headers.get("X-Bbk-Id")
        if bbk:
            request.state.bbk_id = bbk
        return await call_next(request)

    application.include_router(wealth_router.router, prefix="/api")
    return application


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def plan_payload(**overrides) -> dict:
    payload = {
        "name": "九月重点客户经营计划",
        "source_label": "行长关注",
        "scenes": [
            {
                "scene_id": "skill-wealth-insurance-1",
                "scene_name": "保障潜客经营",
                "category": "insurance",
                "direction": "挖掘高潜保险客户",
                "cycle": "本月",
                "start_date": "2026-09-01",
                "end_date": "2026-09-30",
                "cron_expr": "0 9 * * *",
                "cron_example": "每日生成高潜保险客户名单",
                "mcp_relations": ["mcp-customer"],
            },
        ],
        "targets": [{"sap_id": "chenjy", "name": "陈**"}],
    }
    payload.update(overrides)
    return payload


def test_create_plan_persists_and_launches_publish(
    client: TestClient,
    app: FastAPI,
) -> None:
    resp = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers=VIEWER,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["publish_status"] == "publishing"
    assert app.state.wealth_plan_launches == [body["id"]]


def test_create_plan_applies_scene_branch_validation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_cross_branch_scene(request, body) -> None:
        raise HTTPException(
            status_code=403,
            detail="当前分行无权使用该经营场景",
        )

    monkeypatch.setattr(
        wealth_router,
        "_validate_plan_scenes_for_branch",
        reject_cross_branch_scene,
        raising=False,
    )

    resp = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers={**VIEWER, "X-Bbk-Id": "110"},
    )

    assert resp.status_code == 403


@pytest.mark.parametrize(
    ("owner_headers", "source_label"),
    [
        ({"X-User-Id": "president", "X-Position-Id": "RB1101"}, "行长关注"),
        ({"X-User-Id": "middle", "X-Position-Id": "RB0301"}, "分行关注"),
        ({"X-User-Id": "rm-1", "X-Position-Id": "RB0101"}, "我的关注"),
    ],
)
def test_rm_cannot_publish_scene_reserved_by_management_or_self(
    client: TestClient,
    owner_headers: dict[str, str],
    source_label: str,
) -> None:
    owner_payload = plan_payload(
        name=f"{source_label}规划",
        source_label=source_label,
    )
    created = client.post(
        "/api/wealth/plans",
        json=owner_payload,
        headers={**owner_headers, "X-Bbk-Id": "100"},
    )
    assert created.status_code == 200

    response = client.post(
        "/api/wealth/plans",
        json=plan_payload(source_label="我的关注"),
        headers={
            "X-User-Id": "rm-1",
            "X-Bbk-Id": "100",
            "X-Position-Id": "RB0101",
        },
    )

    assert response.status_code == 409
    assert "保障潜客经营" in response.json()["detail"]
    assert f"{source_label}规划" in response.json()["detail"]


def test_other_rm_plan_does_not_reserve_scene(client: TestClient) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(source_label="我的关注"),
        headers={
            "X-User-Id": "rm-2",
            "X-Bbk-Id": "100",
            "X-Position-Id": "RB0101",
        },
    )
    assert created.status_code == 200

    response = client.post(
        "/api/wealth/plans",
        json=plan_payload(source_label="我的关注"),
        headers={
            "X-User-Id": "rm-1",
            "X-Bbk-Id": "100",
            "X-Position-Id": "RB0101",
        },
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("owner_position", "actor_position", "expected_status"),
    [
        ("RB0301", "RB0301", 409),  # 中台受中台规划约束
        ("RB1101", "RB0301", 200),  # 中台不受行长规划约束
        ("RB0101", "RB0301", 200),  # 中台不受客户经理规划约束
        ("RB0301", "RB1101", 409),  # 行长受中台规划约束
        ("RB1101", "RB1101", 409),  # 行长受行长规划约束
        ("RB0101", "RB1101", 200),  # 行长不受客户经理规划约束
    ],
)
def test_management_scene_reservation_matrix(
    client: TestClient,
    owner_position: str,
    actor_position: str,
    expected_status: int,
) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(name="已占用规划"),
        headers={
            "X-User-Id": "owner",
            "X-Bbk-Id": "100",
            "X-Position-Id": owner_position,
        },
    )
    assert created.status_code == 200

    response = client.post(
        "/api/wealth/plans",
        json=plan_payload(name="新规划"),
        headers={
            "X-User-Id": "actor",
            "X-Bbk-Id": "100",
            "X-Position-Id": actor_position,
        },
    )

    assert response.status_code == expected_status
    if expected_status == 409:
        assert "保障潜客经营" in response.json()["detail"]
        assert "已占用规划" in response.json()["detail"]


def test_create_plan_derives_source_label_from_request_role(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/wealth/plans",
        json=plan_payload(source_label="分行关注"),
        headers={
            "X-User-Id": "rm-1",
            "X-Bbk-Id": "100",
            "X-Position-Id": "RB0101",
        },
    )
    assert response.status_code == 200

    plans = client.get(
        "/api/wealth/plans",
        headers={
            "X-User-Id": "rm-1",
            "X-Bbk-Id": "100",
            "X-Position-Id": "RB0101",
        },
    ).json()["items"]

    assert plans[0]["source_label"] == "我的关注"


@pytest.mark.parametrize("skill_bbk_id", ["110", "100"])
async def test_scene_branch_validation_accepts_current_branch_skill(
    monkeypatch: pytest.MonkeyPatch,
    skill_bbk_id: str,
) -> None:
    request = SimpleNamespace(state=SimpleNamespace(bbk_id="110"))
    body = PlanUpsertRequest(**plan_payload())

    async def fetch_current_branch_scenes(request, category):
        return [
            SceneSkillItem(
                skillId="skill-wealth-insurance-1",
                senceName="保障潜客经营",
                category="insurance",
                bbkId=skill_bbk_id,
            ),
        ]

    monkeypatch.setattr(
        wealth_router,
        "_fetch_external_scene_skills",
        fetch_current_branch_scenes,
    )

    await wealth_router._validate_plan_scenes_for_branch(request, body)


@pytest.mark.parametrize("skill_bbk_id", ["121", None])
async def test_scene_branch_validation_rejects_cross_branch_or_unowned_skill(
    monkeypatch: pytest.MonkeyPatch,
    skill_bbk_id: str | None,
) -> None:
    request = SimpleNamespace(state=SimpleNamespace(bbk_id="110"))
    body = PlanUpsertRequest(**plan_payload())

    async def fetch_other_branch_scenes(request, category):
        return [
            SceneSkillItem(
                skillId="skill-wealth-insurance-1",
                senceName="保障潜客经营",
                category="insurance",
                bbkId=skill_bbk_id,
            ),
        ]

    monkeypatch.setattr(
        wealth_router,
        "_fetch_external_scene_skills",
        fetch_other_branch_scenes,
    )

    with pytest.raises(HTTPException) as exc_info:
        await wealth_router._validate_plan_scenes_for_branch(request, body)

    assert exc_info.value.status_code == 403


async def test_scene_branch_validation_rejects_missing_request_branch() -> (
    None
):
    request = SimpleNamespace(state=SimpleNamespace(bbk_id=None))
    body = PlanUpsertRequest(**plan_payload())

    with pytest.raises(HTTPException) as exc_info:
        await wealth_router._validate_plan_scenes_for_branch(request, body)

    assert exc_info.value.status_code == 403


async def test_update_plan_applies_scene_branch_validation(
    client: TestClient,
    store: WealthPlanStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers={**VIEWER, "X-Bbk-Id": "110"},
    ).json()
    await store.set_publish_status(created["id"], "published")

    async def reject_cross_branch_scene(request, body) -> None:
        raise HTTPException(
            status_code=403,
            detail="当前分行无权使用该经营场景",
        )

    monkeypatch.setattr(
        wealth_router,
        "_validate_plan_scenes_for_branch",
        reject_cross_branch_scene,
        raising=False,
    )

    resp = client.put(
        f"/api/wealth/plans/{created['id']}",
        json=plan_payload(),
        headers={**VIEWER, "X-Bbk-Id": "110"},
    )

    assert resp.status_code == 403


def test_list_personal_roles_visible_by_branch_and_relation(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers=VIEWER,
    ).json()

    creator_items = client.get("/api/wealth/plans", headers=VIEWER).json()
    target_items = client.get(
        "/api/wealth/plans",
        headers={"X-User-Id": "chenjy", "X-Bbk-Id": "100"},
    ).json()
    outsider_items = client.get(
        "/api/wealth/plans",
        headers={"X-User-Id": "wangly", "X-Bbk-Id": "100"},
    ).json()
    other_branch_items = client.get(
        "/api/wealth/plans",
        headers={"X-User-Id": "zhangwl", "X-Bbk-Id": "200"},
    ).json()

    assert [p["id"] for p in creator_items["items"]] == [created["id"]]
    assert creator_items["items"][0]["editable"] is True
    assert creator_items["items"][0]["board_status"] == "发布中"
    assert (
        creator_items["items"][0]["scenes"][0]["cron_example"]
        == "每日生成高潜保险客户名单"
    )
    assert [p["id"] for p in target_items["items"]] == [created["id"]]
    assert target_items["items"][0]["editable"] is False
    assert outsider_items["items"] == []
    assert other_branch_items["items"] == []


def test_financial_supervisor_has_president_scope(client: TestClient) -> None:
    """RB0108 沿用行长的创建人隔离，发布来源为行长关注。"""
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers={**VIEWER, "X-Bbk-Id": "100", "X-Position-Id": "RB1101"},
    ).json()
    supervisor = {
        "X-User-Id": "supervisor",
        "X-Bbk-Id": "100",
        "X-Position-Id": "RB0108",
    }
    items = client.get("/api/wealth/plans", headers=supervisor).json()["items"]
    assert items == []
    assert (
        client.get(
            f"/api/wealth/plans/{created['id']}",
            headers=supervisor,
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/wealth/plans",
            headers={**supervisor, "X-Bbk-Id": "200"},
        ).json()["items"]
        == []
    )

    supervisor_payload = plan_payload(name="主管规划")
    supervisor_payload["scenes"][0].update(
        start_date="2026-10-01",
        end_date="2026-10-31",
    )
    published = client.post(
        "/api/wealth/plans",
        json=supervisor_payload,
        headers=supervisor,
    )
    assert published.status_code == 200
    detail = client.get(
        f"/api/wealth/plans/{published.json()['id']}",
        headers=supervisor,
    ).json()
    assert detail["source_label"] == "行长关注"


def test_list_branch_wide_for_president_and_middle(client: TestClient) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers={**VIEWER, "X-Bbk-Id": "100", "X-Position-Id": "RB0301"},
    ).json()

    president = {
        "X-User-Id": "wangly",
        "X-Bbk-Id": "100",
        "X-Position-Id": "RB1101",
    }
    middle = {**president, "X-Position-Id": "RB0301"}
    other_middle = {**president, "X-Position-Id": "RB0305"}
    former_middle = {**president, "X-Position-Id": "RB0304"}
    rm = {**president, "X-Position-Id": "RB0101"}
    other_branch = {**president, "X-Bbk-Id": "200"}

    for headers in (president, middle, other_middle):
        items = client.get("/api/wealth/plans", headers=headers).json()[
            "items"
        ]
        assert [p["id"] for p in items] == [created["id"]]
        assert items[0]["editable"] is False  # 可见但只读
    assert (
        client.get("/api/wealth/plans", headers=former_middle).json()["items"]
        == []
    )
    assert client.get("/api/wealth/plans", headers=rm).json()["items"] == []
    assert (
        client.get("/api/wealth/plans", headers=other_branch).json()["items"]
        == []
    )


def test_detail_visibility_combines_branch_and_role_scope(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers={**VIEWER, "X-Bbk-Id": "100", "X-Position-Id": "RB1101"},
    ).json()
    management_peer = {
        "X-User-Id": "wangly",
        "X-Bbk-Id": "100",
        "X-Position-Id": "RB0306",
    }

    detail = client.get(
        f"/api/wealth/plans/{created['id']}",
        headers=management_peer,
    )
    assert detail.status_code == 403
    assert (
        client.get(
            f"/api/wealth/plans/{created['id']}",
            headers={**VIEWER, "X-Position-Id": "RB1101"},
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/wealth/plans",
            headers=management_peer,
        ).json()["items"]
        == []
    )

    target = {
        "X-User-Id": "chenjy",
        "X-Bbk-Id": "100",
        "X-Position-Id": "RB0101",
    }
    assert (
        client.get(
            f"/api/wealth/plans/{created['id']}",
            headers=target,
        ).status_code
        == 200
    )

    personal_outsider = {**target, "X-User-Id": "wangly"}
    assert (
        client.get(
            f"/api/wealth/plans/{created['id']}",
            headers=personal_outsider,
        ).status_code
        == 403
    )

    branch_outsider = {**management_peer, "X-Bbk-Id": "200"}
    assert (
        client.get(
            f"/api/wealth/plans/{created['id']}",
            headers=branch_outsider,
        ).status_code
        == 403
    )


def test_get_detail_forbidden_for_outsider(client: TestClient) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers=VIEWER,
    ).json()

    resp = client.get(
        f"/api/wealth/plans/{created['id']}",
        headers={"X-User-Id": "zhangwl", "X-Bbk-Id": "200"},
    )

    assert resp.status_code == 403


def test_update_rejected_while_publishing(client: TestClient) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers=VIEWER,
    ).json()

    resp = client.put(
        f"/api/wealth/plans/{created['id']}",
        json=plan_payload(name="改名"),
        headers=VIEWER,
    )

    assert resp.status_code == 409


async def test_update_relaunches_after_published(
    app: FastAPI,
    client: TestClient,
    store: WealthPlanStore,
) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers=VIEWER,
    ).json()
    await store.set_publish_status(created["id"], "published")

    resp = client.put(
        f"/api/wealth/plans/{created['id']}",
        json=plan_payload(name="十月计划"),
        headers=VIEWER,
    )

    assert resp.status_code == 200
    assert app.state.wealth_plan_launches == [created["id"], created["id"]]
    record = await store.get(created["id"])
    assert record is not None and record.name == "十月计划"


async def test_rm_can_update_own_published_plan_schedule(
    client: TestClient,
    store: WealthPlanStore,
) -> None:
    headers = {
        "X-User-Id": "rm-1",
        "X-Bbk-Id": "100",
        "X-Position-Id": "RB0101",
    }
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(source_label="我的关注"),
        headers=headers,
    ).json()
    await store.set_publish_status(created["id"], "published")
    occupied = client.post(
        "/api/wealth/plans",
        json=plan_payload(name="行长历史规划"),
        headers={
            "X-User-Id": "president",
            "X-Bbk-Id": "100",
            "X-Position-Id": "RB1101",
        },
    )
    assert occupied.status_code == 200
    payload = plan_payload(source_label="我的关注")
    payload["scenes"][0]["cron_expr"] = "0 10 * * *"

    response = client.put(
        f"/api/wealth/plans/{created['id']}",
        json=payload,
        headers=headers,
    )

    assert response.status_code == 200


async def test_rm_update_still_rejects_new_scene_reserved_by_management(
    client: TestClient,
    store: WealthPlanStore,
) -> None:
    rm_headers = {
        "X-User-Id": "rm-1",
        "X-Bbk-Id": "100",
        "X-Position-Id": "RB0101",
    }
    own_payload = plan_payload(source_label="我的关注")
    own_payload["scenes"][0]["scene_id"] = "scene-own"
    created = client.post(
        "/api/wealth/plans",
        json=own_payload,
        headers=rm_headers,
    ).json()
    await store.set_publish_status(created["id"], "published")

    management_payload = plan_payload(name="行长规划")
    management_payload["scenes"][0]["scene_id"] = "scene-management"
    occupied = client.post(
        "/api/wealth/plans",
        json=management_payload,
        headers={
            "X-User-Id": "president",
            "X-Bbk-Id": "100",
            "X-Position-Id": "RB1101",
        },
    )
    assert occupied.status_code == 200

    update_payload = plan_payload(source_label="我的关注")
    retained_scene = {**update_payload["scenes"][0], "scene_id": "scene-own"}
    added_scene = {
        **update_payload["scenes"][0],
        "scene_id": "scene-management",
        "scene_name": "新增经营场景",
    }
    update_payload["scenes"] = [retained_scene, added_scene]

    response = client.put(
        f"/api/wealth/plans/{created['id']}",
        json=update_payload,
        headers=rm_headers,
    )

    assert response.status_code == 409
    assert "新增经营场景" in response.json()["detail"]


def test_update_forbidden_for_target(client: TestClient) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers=VIEWER,
    ).json()

    resp = client.put(
        f"/api/wealth/plans/{created['id']}",
        json=plan_payload(),
        headers={"X-User-Id": "chenjy"},
    )

    assert resp.status_code == 403


def test_delete_rejected_while_publishing(client: TestClient) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers=VIEWER,
    ).json()

    resp = client.delete(f"/api/wealth/plans/{created['id']}", headers=VIEWER)

    assert resp.status_code == 409


async def test_delete_cascades_and_removes(
    client: TestClient,
    app: FastAPI,
    store: WealthPlanStore,
) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers=VIEWER,
    ).json()
    await store.set_publish_status(created["id"], "published")

    forbidden = client.delete(
        f"/api/wealth/plans/{created['id']}",
        headers={"X-User-Id": "chenjy"},
    )
    resp = client.delete(f"/api/wealth/plans/{created['id']}", headers=VIEWER)

    assert forbidden.status_code == 403
    assert resp.status_code == 200
    assert app.state.wealth_plan_cascades == [created["id"]]
    assert (
        client.get("/api/wealth/plans", headers=VIEWER).json()["items"] == []
    )


def test_scene_skills_rejects_unknown_category(client: TestClient) -> None:
    resp = client.get("/api/wealth/scene-skills?category=unknown")

    assert resp.status_code == 400


def test_scene_skills_empty_when_external_absent(
    client: TestClient,
) -> None:
    """外部接口未配置/不可达时返回空列表，不做假数据兜底。"""
    resp = client.get("/api/wealth/scene-skills?category=insurance")

    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_scene_skills_accepts_other_category(client: TestClient) -> None:
    resp = client.get("/api/wealth/scene-skills?category=other")

    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_scene_skills_allows_empty_category(client: TestClient) -> None:
    """category 为空串表示查询全部大类，不再 400。"""
    resp = client.get("/api/wealth/scene-skills?category=")

    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_name_list_allows_empty_skill_id(client: TestClient) -> None:
    """数组含空条目时忽略空值，查询仍限定在有效技能内。"""
    resp = client.post(
        "/api/wealth/name-list",
        json={"sap_id": "10086", "skill_id": ["", "skill-a"]},
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "items": [],
        "skillFieldList": [],
        "allFields": [],
        "targetCusCount": None,
        "todayTouchCount": None,
        "sevenDayTouchCount": None,
        "unTouchedCount": None,
    }


def test_name_list_requires_skill_ids(client: TestClient) -> None:
    for body, status in [
        ({"sap_id": "10086"}, 422),
        ({"skill_id": [""]}, 400),
        ({"skill_id": []}, 400),
        ({"skill_id": "skill-a"}, 422),
    ]:
        assert (
            client.post("/api/wealth/name-list", json=body).status_code
            == status
        )


def test_name_list_accepts_large_skill_array(client, monkeypatch):
    captured = {}

    async def fetch(request, skill_ids, sap_id, touched):
        captured["skill_ids"] = skill_ids
        return wealth_router.NameListResponse()

    monkeypatch.setattr(wealth_router, "_fetch_external_name_list", fetch)
    skills = [f"skill-{index}" for index in range(2000)]
    response = client.post(
        "/api/wealth/name-list",
        json={"skill_id": skills, "sap_id": "10086", "touched": 2},
    )
    assert response.status_code == 200
    assert captured["skill_ids"] == skills


def test_name_list_empty_when_external_absent(client: TestClient) -> None:
    """外部接口未配置/不可达时返回空列表，不做假数据兜底。"""
    resp = client.post(
        "/api/wealth/name-list",
        json={"skill_id": ["loan_verify"]},
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "items": [],
        "skillFieldList": [],
        "allFields": [],
        "targetCusCount": None,
        "todayTouchCount": None,
        "sevenDayTouchCount": None,
        "unTouchedCount": None,
    }


def test_name_list_allows_empty_sap_id(client: TestClient) -> None:
    """sap_id 可空（缺省场景/兼容调用），参数合法。"""
    resp = client.post(
        "/api/wealth/name-list",
        json={"skill_id": ["loan_verify"], "sap_id": ""},
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "items": [],
        "skillFieldList": [],
        "allFields": [],
        "targetCusCount": None,
        "todayTouchCount": None,
        "sevenDayTouchCount": None,
        "unTouchedCount": None,
    }


def test_name_list_rejects_invalid_touched(client: TestClient) -> None:
    """touched 仅接受 0 未触达 / 1 已触达 / 2 全部。"""
    resp = client.post(
        "/api/wealth/name-list",
        json={"skill_id": ["skill-a"], "touched": 3},
    )

    assert resp.status_code == 400

    ok = client.post(
        "/api/wealth/name-list",
        json={"skill_id": ["skill-a"], "touched": 2},
    )
    assert ok.status_code == 200


def test_name_list_forwards_touch_filter_context(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """触达筛选请求应携带支行、岗位和登录态。"""
    captured: dict = {}

    class FakeResponse:
        def json(self) -> dict:
            return {
                "code": "200",
                "message": "成功",
                "data": {
                    "list": [
                        {
                            "custUid": "PNCIF6571543777",
                            "custNm": "王*",
                            "sapId": "80280256",
                            "bbkOrgId": "755480",
                            "filename": "http://example.test/customer",
                            "recomReason": "命中条件说明",
                            "skillList": [
                                {
                                    "skillId": "SKILL0001",
                                    "skillName": "贷款经营",
                                },
                            ],
                            "strongContactTime": "2026-09-16 10:30:00",
                            "touchMethod": "电话",
                            "touched": 1,
                            "fieldList": [
                                {
                                    "filedName": "age",
                                    "filedNameCn": "年龄",
                                    "filedValue": 0,
                                },
                            ],
                        },
                    ],
                    "skillFieldList": [
                        {
                            "skillId": "SKILL0001",
                            "groupField": ["age"],
                            "fields": [
                                {"filedName": "age", "filedNameCn": "年龄"},
                            ],
                        },
                    ],
                    "allFields": [{"filedName": "age", "filedNameCn": "年龄"}],
                    "targetCusCount": 100,
                    "todayTouchCount": 0,
                    "sevenDayTouchCount": 25,
                    "unTouchedCount": 80,
                },
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args) -> bool:
            return False

        async def post(
            self,
            url: str,
            json: dict,
            headers: dict | None = None,
        ) -> FakeResponse:
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setenv("SWE_SKILL_CONFIG_API_BASE", "http://external.test")
    monkeypatch.setattr(wealth_router.httpx, "AsyncClient", FakeClient)

    resp = client.post(
        "/api/wealth/name-list",
        json={
            "skill_id": ["SKILL0001", "SKILL0002", "SKILL0001"],
            "sap_id": "80280256",
            "touched": 0,
        },
        headers={
            **VIEWER,
            "X-Bbk-Id": "755",
            "X-Org-Code": "755480",
            "X-Position-Id": "RB0101",
            "x-header-cookie": "session=active",
        },
    )

    assert resp.status_code == 200
    assert captured["url"].endswith("/api/agent/workspace/name-list")
    assert captured["json"] == {
        "bbkId": "755",
        "platformSource": "WP",
        "pageSource": "WP_AGENT_WORKSPACE_TASK_LIST",
        "touched": 0,
        "skillIds": ["SKILL0001", "SKILL0002"],
        "sapId": "80280256",
        "subBbkId": "755480",
        "posId": "RB0101",
    }
    assert captured["headers"] == {"Cookie": "session=active"}
    for key, count in {
        "targetCusCount": 100,
        "todayTouchCount": 0,
        "sevenDayTouchCount": 25,
        "unTouchedCount": 80,
    }.items():
        assert resp.json()[key] == count
    assert resp.json()["items"] == [
        {
            "custUid": "PNCIF6571543777",
            "custNm": "王*",
            "sapId": "80280256",
            "bbkOrgId": "755480",
            "filename": "http://example.test/customer",
            "recomReason": "命中条件说明",
            "skillList": [{"skillId": "SKILL0001", "skillName": "贷款经营"}],
            "strongContactTime": "2026-09-16 10:30:00",
            "touchMethod": "电话",
            "touched": 1,
            "fieldList": [
                {"filedName": "age", "filedNameCn": "年龄", "filedValue": 0},
            ],
        },
    ]

    assert resp.json()["skillFieldList"][0]["groupField"] == ["age"]
    assert resp.json()["allFields"] == [
        {"filedName": "age", "filedNameCn": "年龄"},
    ]


def test_skill_stats_empty_when_external_absent(client: TestClient) -> None:
    """外部接口未配置/不可达时返回空列表，由前端保持占位。"""
    resp = client.post(
        "/api/wealth/skill-stats",
        json={
            "skills": [
                {
                    "skillId": "s1",
                    "startDate": "2026-09-01",
                    "endDate": "2026-09-30",
                },
            ],
        },
        headers=VIEWER,
    )

    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_skill_stats_requires_non_empty_skills(client: TestClient) -> None:
    resp = client.post(
        "/api/wealth/skill-stats",
        json={"skills": []},
        headers=VIEWER,
    )

    assert resp.status_code == 422


@pytest.mark.parametrize(
    ("position_id", "expected_sap_id"),
    [
        ("RB0101", "zhangwl"),
        ("RB1101", None),
        ("RB0301", None),
    ],
)
def test_skill_stats_forwards_role_scope(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    position_id: str,
    expected_sap_id: str | None,
) -> None:
    """代理层注入机构范围，客户经理额外注入本人 sapId。"""
    captured: dict = {}

    class FakeResponse:
        def json(self) -> dict:
            return {
                "code": "200",
                "data": {
                    "items": [
                        {
                            "skillId": "s1",
                            "targetCustomerCount": 5,
                            "generatedTaskCount": 2,
                        },
                    ],
                },
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args) -> bool:
            return False

        async def post(self, url: str, json: dict) -> FakeResponse:
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setenv("SWE_SKILL_CONFIG_API_BASE", "http://external.test")
    monkeypatch.setattr(wealth_router.httpx, "AsyncClient", FakeClient)

    resp = client.post(
        "/api/wealth/skill-stats",
        json={
            "skills": [
                {
                    "skillId": "s1",
                    "startDate": "2026-09-01",
                    "endDate": "2026-09-30",
                },
            ],
        },
        headers={
            **VIEWER,
            "X-Bbk-Id": "755",
            "X-Position-Id": position_id,
        },
    )

    assert resp.status_code == 200
    assert resp.json()["items"][0]["targetCustomerCount"] == 5
    assert captured["url"].endswith("/api/agent/workspace/skill-stats")
    assert captured["json"]["bbkId"] == "755"
    assert captured["json"]["skills"][0]["skillId"] == "s1"
    if expected_sap_id is None:
        assert "sapId" not in captured["json"]
    else:
        assert captured["json"]["sapId"] == expected_sap_id


async def _make_broadcast_store(
    task_status: str,
    results: list[dict] | None = None,
    targets: list[str] | None = None,
) -> tuple[CronBroadcastTaskStore, str]:
    store = CronBroadcastTaskStore()
    snapshot, _ = await store.start_task(
        agent_id="default",
        source_id="RMASSIST",
        tenant_id="zhangwl",
        job_id="job-1",
        target_tenant_ids=targets or ["chenjy"],
    )
    for result in results or []:
        await store.record_target_result(snapshot.task_id, result)
    if task_status == "completed":
        await store.finish_task(snapshot.task_id)
    elif task_status == "failed":
        await store.record_task_failed(snapshot.task_id, "boom")
    return store, snapshot.task_id


async def test_board_status_aggregates_broadcast(
    app: FastAPI,
    client: TestClient,
    store: WealthPlanStore,
) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers=VIEWER,
    ).json()
    await store.set_publish_status(created["id"], "published")
    broadcast_store, task_id = await _make_broadcast_store(
        "completed",
        results=[{"tenant_id": "chenjy", "success": True}],
    )
    await store.set_scene_links(
        created["id"],
        "skill-wealth-insurance-1",
        "job-1",
        task_id,
    )
    app.state.cron_broadcast_task_store = broadcast_store

    creator_view = client.get("/api/wealth/plans", headers=VIEWER).json()
    target_view = client.get(
        "/api/wealth/plans",
        headers={"X-User-Id": "chenjy", "X-Bbk-Id": "100"},
    ).json()

    assert creator_view["items"][0]["board_status"] == "已自动下发"
    assert target_view["items"][0]["board_status"] == "已自动下发"


async def test_board_status_recipient_failure(
    app: FastAPI,
    client: TestClient,
    store: WealthPlanStore,
) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(
            targets=[
                {"sap_id": "chenjy", "name": "陈**"},
                {"sap_id": "liuxt", "name": "刘**"},
            ],
        ),
        headers=VIEWER,
    ).json()
    await store.set_publish_status(created["id"], "published")
    broadcast_store, task_id = await _make_broadcast_store(
        "completed",
        results=[
            {"tenant_id": "chenjy", "success": True},
            {"tenant_id": "liuxt", "success": False},
        ],
        targets=["chenjy", "liuxt"],
    )
    await store.set_scene_links(
        created["id"],
        "skill-wealth-insurance-1",
        "job-1",
        task_id,
    )
    app.state.cron_broadcast_task_store = broadcast_store

    creator_view = client.get("/api/wealth/plans", headers=VIEWER).json()
    chenjy_view = client.get(
        "/api/wealth/plans",
        headers={"X-User-Id": "chenjy", "X-Bbk-Id": "100"},
    ).json()
    liuxt_view = client.get(
        "/api/wealth/plans",
        headers={"X-User-Id": "liuxt", "X-Bbk-Id": "100"},
    ).json()

    assert creator_view["items"][0]["board_status"] == "分发失败"
    assert chenjy_view["items"][0]["board_status"] == "已自动下发"
    assert liuxt_view["items"][0]["board_status"] == "分发失败"


async def test_board_status_distributing_while_running(
    app: FastAPI,
    client: TestClient,
    store: WealthPlanStore,
) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers=VIEWER,
    ).json()
    await store.set_publish_status(created["id"], "published")
    broadcast_store, task_id = await _make_broadcast_store("running")
    await store.set_scene_links(
        created["id"],
        "skill-wealth-insurance-1",
        "job-1",
        task_id,
    )
    app.state.cron_broadcast_task_store = broadcast_store

    creator_view = client.get("/api/wealth/plans", headers=VIEWER).json()

    assert creator_view["items"][0]["board_status"] == "分发中"


@pytest.mark.parametrize(
    "position,allowed_sources",
    [
        ("RB1101", {"分行关注", "行长关注"}),
        ("RB0306", {"分行关注", "行长关注"}),
        ("RB0108", {"分行关注", "行长关注"}),
        ("RB0301", {"分行关注"}),
        ("RB0305", {"分行关注"}),
    ],
)
async def test_management_list_and_detail_share_source_scope(
    client: TestClient,
    store: WealthPlanStore,
    position: str,
    allowed_sources: set[str],
) -> None:
    from swe.app.wealth_plans.models import PlanTargetRecord, WealthPlanRecord

    records = [
        WealthPlanRecord(
            id=plan_id,
            sap_id=creator,
            name=plan_id,
            bbk_id="100",
            source_label=source,
            targets=[PlanTargetRecord(sap_id="viewer")],
        )
        for plan_id, creator, source in [
            ("branch", "viewer", "分行关注"),
            ("president", "viewer", "行长关注"),
            ("personal", "viewer", "我的关注"),
            ("target-personal", "other", "我的关注"),
            ("peer-president", "other", "行长关注"),
            ("peer-branch", "other", "分行关注"),
        ]
    ]
    for record in records:
        await store.create(record)
    headers = {
        "X-User-Id": "viewer",
        "X-Bbk-Id": "100",
        "X-Position-Id": position,
    }
    items = client.get("/api/wealth/plans", headers=headers).json()["items"]
    assert {item["source_label"] for item in items} == allowed_sources
    assert "peer-president" not in {item["id"] for item in items}
    assert "peer-branch" in {item["id"] for item in items}
    for record in records:
        response = client.get(
            f"/api/wealth/plans/{record.id}",
            headers=headers,
        )
        assert response.status_code == (
            200
            if record.source_label in allowed_sources
            and record.id != "peer-president"
            else 403
        )


@pytest.mark.parametrize("target,expected", [("c-rm", 200), ("a-rm", 409)])
def test_middle_ab_plan_allows_c_branch_recipients(client, target, expected):
    created = client.post(
        "/api/wealth/plans",
        headers={**VIEWER, "X-Position-Id": "RB0301"},
        json=plan_payload(targets=[{"sap_id": "a-rm"}, {"sap_id": "b-rm"}]),
    )
    assert created.status_code == 200
    response = client.post(
        "/api/wealth/plans",
        headers={
            **VIEWER,
            "X-User-Id": "c-president",
            "X-Position-Id": "RB1101",
        },
        json=plan_payload(targets=[{"sap_id": target}]),
    )
    assert response.status_code == expected
