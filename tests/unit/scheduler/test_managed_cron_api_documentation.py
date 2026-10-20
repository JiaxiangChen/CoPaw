# -*- coding: utf-8 -*-
"""验证新增接口的中文用途、参数及响应说明实际出现在 OpenAPI 中。"""

import re

from fastapi import FastAPI


def test_new_interface_documentation_is_available_in_openapi():
    from scheduler.app.routers import (
        managed_callbacks,
        managed_cron,
        managed_cron_broadcast,
        managed_cron_dispatch_state,
        managed_cron_controls,
        managed_cron_management,
    )
    from swe.app.crons import managed_api, managed_execution_api

    app = FastAPI()
    for module in (
        managed_callbacks,
        managed_cron,
        managed_cron_broadcast,
        managed_cron_dispatch_state,
        managed_cron_management,
        managed_cron_controls,
        managed_api,
        managed_execution_api,
    ):
        app.include_router(module.router, prefix="/api")
    schema = app.openapi()
    operations = [
        operation
        for methods in schema["paths"].values()
        for method, operation in methods.items()
        if method in {"get", "post", "put", "delete"}
    ]
    assert len(operations) == 32
    for operation in operations:
        assert re.search(
            r"[\u4e00-\u9fff]",
            operation.get("summary", ""),
        ), operation["operationId"]
        assert re.search(
            r"[\u4e00-\u9fff]",
            operation.get("description", ""),
        ), operation["operationId"]
    assert (
        "201"
        in schema["paths"]["/api/scheduler/cron/jobs"]["post"]["responses"]
    )
    assert (
        "202"
        in schema["paths"]["/api/scheduler/cron/jobs/{job_id}/run"]["post"][
            "responses"
        ]
    )
    assert (
        "202"
        in schema["paths"]["/api/internal/cron/managed/execute"]["post"][
            "responses"
        ]
    )
    properties = schema["components"]["schemas"]["CreateRequest"]["properties"]
    execute_schema = schema["components"]["schemas"]["ExecuteRequest"]
    recover_schema = schema["components"]["schemas"][
        "ExecutionIdentityRequest"
    ]
    assert "dispatch_token" in execute_schema["required"]
    assert "dispatch_token" not in recover_schema["properties"]
    assert re.search(
        r"[\u4e00-\u9fff]",
        properties["request_id"].get("description", ""),
    )
