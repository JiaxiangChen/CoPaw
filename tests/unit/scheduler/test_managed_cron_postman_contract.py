# -*- coding: utf-8 -*-
"""对照实际注册路由和请求模型核对交付的Postman集合，不发送真实请求。"""

import json
import re
from pathlib import Path
from inspect import isclass
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

DOCUMENT = (
    Path(__file__).resolve().parents[3]
    / "docs/integrations/zhituo-scheduler-cron-api-v1.postman_collection.json"
)
COLLECTION = json.loads(DOCUMENT.read_text(encoding="utf-8"))


def requests(items):
    for item in items:
        if "request" in item:
            yield item
        else:
            yield from requests(item["item"])


@pytest.fixture
def routes():
    from scheduler.app.routers import (
        api_router,
        managed_cron_management,
        managed_cron_controls,
        managed_cron_broadcast,
        managed_cron_dispatch_state,
    )
    from swe.app.crons.api import router as old_swe_router

    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    app.include_router(old_swe_router, prefix="/api")
    models = {}
    for module in (
        managed_cron_management,
        managed_cron_controls,
        managed_cron_broadcast,
        managed_cron_dispatch_state,
    ):
        models.update(
            {
                name: value
                for name, value in vars(module).items()
                if isclass(value) and issubclass(value, BaseModel)
            },
        )
    return SimpleNamespace(schema=app.openapi(), models=models)


def _path_pattern(template):
    return "/".join(
        "[^/]+" if segment.startswith("{") else re.escape(segment)
        for segment in template.split("/")
    )


@pytest.mark.parametrize(
    "item",
    list(requests(COLLECTION["item"])),
    ids=lambda item: item["name"],
)
def test_delivered_request_matches_real_route_and_model(routes, item):
    command = item["request"]
    path = re.sub(
        r"^\{\{(?:scheduler|swe)_base_url\}\}",
        "",
        command["url"],
    ).split("?", 1)[0]
    method = command["method"].lower()
    operation = next(
        (
            operations[method]
            for template, operations in routes.schema["paths"].items()
            if method in operations
            and re.fullmatch(_path_pattern(template), path)
        ),
        None,
    )
    assert operation is not None, (command["method"], path)
    required_headers = {"X-User-Id", "X-Source-Id", "X-Agent-Id"}
    active_headers = {
        header["key"]
        for header in command["header"]
        if not header.get("disabled")
    }
    assert required_headers <= active_headers
    if "body" not in command:
        return
    variables = {
        variable["key"]: variable["value"]
        for variable in COLLECTION["variable"]
    }
    raw = re.sub(
        r"\{\{([^{}]+)\}\}",
        lambda match: variables[match[1]],
        command["body"]["raw"],
    )
    value = json.loads(raw)
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    model_name = schema["$ref"].rsplit("/", 1)[-1]
    routes.models[model_name].model_validate(value)


def test_collection_preserves_eight_required_capabilities():
    commands = [item["request"] for item in requests(COLLECTION["item"])]
    expected = {
        ("POST", "/api/scheduler/cron/jobs"),
        ("PUT", "/api/scheduler/cron/jobs/{{job_id}}"),
        ("DELETE", "/api/scheduler/cron/jobs/{{job_id}}"),
        ("GET", "/api/scheduler/cron/jobs/{{job_id}}"),
        ("POST", "/api/scheduler/cron/jobs/{{job_id}}/broadcast"),
        ("PUT", "/api/scheduler/cron/jobs/{{job_id}}/dispatch-state"),
        ("GET", "/api/scheduler/cron/jobs/{{job_id}}/broadcast/children"),
        ("GET", "/api/cron/workflow-bindings/{{skill_id}}"),
    }
    actual = {
        (
            command["method"],
            re.sub(
                r"^\{\{(?:scheduler|swe)_base_url\}\}",
                "",
                command["url"],
            ).split("?", 1)[0],
        )
        for command in commands
    }
    assert expected <= actual
    assert len(commands) == 22
