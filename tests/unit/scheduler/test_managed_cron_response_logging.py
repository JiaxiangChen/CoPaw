# -*- coding: utf-8 -*-
"""失败响应保留可定位的 JSON 信息，但不记录请求输入和认证字段。"""

import json
import logging

import httpx
import pytest

from scheduler.app.services.cron.managed.identity import CallerIdentity
from scheduler.app.services.cron.managed.swe_client import ManagedSweClient


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["prepare", "cleanup"])
@pytest.mark.parametrize("status", [302, 422, 503])
async def test_failed_resource_response_is_logged_without_exposing_inputs(
    action,
    status,
    caplog,
):
    body = {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "operation_id"],
                "msg": "Field required",
                "input": {"text": "private-task-body"},
                "ctx": {"error": "private-context"},
            },
        ],
        "token": "private-response-token",
        "nested": {"clientKey": "private-client-key"},
        "headers": {"Authorization": "Bearer private-header"},
        "message": "Bearer private-message-token; cookie: private-cookie",
    }

    async def respond(_request):
        return httpx.Response(status, json=body)

    caplog.set_level(logging.ERROR)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
    ) as http:
        client = ManagedSweClient(
            "http://swe",
            http=http,
            headers={"Authorization": "Bearer private-request-token"},
        )
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await getattr(client, action)(
                {
                    "operation_id": "op-response-log",
                    "job_id": "job-response-log",
                },
                CallerIdentity("alice", "bank", "default", "alice"),
            )
    assert caught.value.response.status_code == status
    record = next(
        (
            item
            for item in caplog.records
            if "response_body=" in item.getMessage()
        ),
        None,
    )
    assert record is not None
    message = record.getMessage()
    assert record.levelno == logging.ERROR
    assert f"action={action}" in message
    assert f"http_status={status}" in message
    assert "operation_id=op-response-log" in message
    assert "job_id=job-response-log" in message
    value = json.loads(message.split("response_body=", 1)[1])
    assert value["detail"][0]["loc"] == ["body", "operation_id"]
    assert value["detail"][0]["msg"] == "Field required"
    assert "redacted" in message
    for secret in (
        "private-task-body",
        "private-context",
        "private-response-token",
        "private-client-key",
        "private-header",
        "private-message-token",
        "private-cookie",
        "private-request-token",
    ):
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_successful_resource_response_never_logs_its_body(caplog):
    async def respond(_request):
        return httpx.Response(
            200,
            json={"spec": {"text": "private-task-body"}},
        )

    caplog.set_level(logging.INFO)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
    ) as http:
        result = await ManagedSweClient("http://swe", http=http).prepare(
            {"operation_id": "op", "job_id": "job"},
            CallerIdentity("alice", "bank", "default", "alice"),
        )
    assert result == {"spec": {"text": "private-task-body"}}
    assert "private-task-body" not in caplog.text
    assert "response_body=" not in caplog.text


@pytest.mark.parametrize(
    "content",
    [
        b"private-plain-body",
        b"\xff\xfe\x00",
        b'"private-json-string"',
        b'{"detail":"' + b"x" * 9000 + b'"}',
    ],
    ids=["non-json", "encoding", "scalar", "oversized"],
)
def test_unsafe_or_oversized_response_body_is_omitted(content):
    from scheduler.app.services.cron.managed.diagnostics import (
        format_swe_response_body,
    )

    result = format_swe_response_body(httpx.Response(422, content=content))
    assert "omitted" in result
    assert "private-" not in result
    assert len(result) < 200


def test_deep_json_body_uses_bounded_depth_even_if_json_parser_accepts_it():
    from scheduler.app.services.cron.managed.diagnostics import (
        format_swe_response_body,
    )

    content = b"[" * 1200 + b"]" * 1200
    result = format_swe_response_body(httpx.Response(422, content=content))
    assert "truncated" in result or "omitted" in result
    assert len(result) < 200


def test_long_json_log_is_truncated_after_sensitive_values_are_redacted():
    from scheduler.app.services.cron.managed.diagnostics import (
        format_swe_response_body,
    )

    response = httpx.Response(
        422,
        json={
            "token": "private-token",
            "detail": "x" * 5000 + "not-logged-tail",
        },
    )
    result = format_swe_response_body(response)
    assert result.endswith("[truncated]")
    assert "private-token" not in result
    assert "not-logged-tail" not in result
    assert len(result) <= 4112
