# -*- coding: utf-8 -*-
"""Shared fixtures for critical runtime path integration tests."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator, Iterable

import pytest
import uvicorn
from agentscope.model import ChatModelBase
from agentscope.model._model_response import ChatResponse
from agentscope_runtime.engine.schemas.agent_schemas import (
    AgentRequest,
    ContentType,
    Message,
    Role,
    TextContent,
)

from swe.app.crons.models import (
    CronJobRequest,
    CronJobSpec,
    DispatchSpec,
    DispatchTarget,
    JobRuntimeSpec,
    ScheduleSpec,
)
from swe.config.config import (
    AgentProfileConfig,
    AgentProfileRef,
    AgentsConfig,
    AgentsRunningConfig,
    Config,
    MCPClientConfig,
    MCPConfig,
    save_agent_config,
)
from swe.config.utils import save_config
from swe.providers.models import ModelSlotConfig


@dataclass
class LoopbackMCPServer:
    url: str
    required_header_name: str
    required_header_value: str
    calls: list[dict[str, Any]]


class RecordingChannelManager:
    def __init__(self) -> None:
        self.texts: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

    async def send_text(self, **kwargs: Any) -> None:
        self.texts.append(kwargs)

    async def send_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


class DeterministicChatModel(ChatModelBase):
    """A local bottom-level model with deterministic responses."""

    def __init__(self, responses: Iterable[Any], *, stream: bool = False):
        super().__init__(model_name="critical-path-model", stream=stream)
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> ChatResponse:
        self.calls.append({"args": args, "kwargs": kwargs})
        if not self.responses:
            raise AssertionError("deterministic model has no response left")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, ChatResponse):
            return item
        return ChatResponse(content=item)


class FakeProvider:
    def __init__(
        self,
        model: ChatModelBase,
        model_id: str = "critical",
        generation_kwargs: dict[str, Any] | None = None,
    ):
        self.model = model
        self.model_id = model_id
        self.generation_kwargs = generation_kwargs or {}
        self.request_generation_kwargs: list[dict[str, Any]] = []

    def has_model(self, model_id: str) -> bool:
        return model_id == self.model_id

    def get_chat_model_instance(
        self,
        model_id: str,
        generation_kwargs: dict[str, Any] | None = None,
    ) -> ChatModelBase:
        if not self.has_model(model_id):
            raise ValueError(f"unknown test model: {model_id}")
        self.request_generation_kwargs.append(generation_kwargs or {})
        return self.model

    def get_model_config(self, model_id: str):
        if not self.has_model(model_id):
            raise ValueError(f"unknown test model: {model_id}")
        from swe.providers.provider import ModelRuntimeConfig

        return ModelRuntimeConfig()

    def build_generation_kwargs(self, _config) -> dict[str, Any]:
        return dict(self.generation_kwargs)


class FakeProviderManager:
    def __init__(
        self,
        provider: FakeProvider,
        *,
        provider_id: str = "critical-provider",
        model_id: str = "critical",
    ) -> None:
        self.provider = provider
        self.active_model = ModelSlotConfig(
            provider_id=provider_id,
            model=model_id,
        )

    def get_active_model(self) -> ModelSlotConfig:
        return self.active_model

    def get_provider(self, provider_id: str) -> FakeProvider | None:
        if provider_id == self.active_model.provider_id:
            return self.provider
        return None


def unused_tcp_port() -> int:
    with contextlib.closing(
        socket.socket(socket.AF_INET, socket.SOCK_STREAM),
    ) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_agent_request(
    *,
    text: str = "run critical path",
    session_id: str = "session-critical",
    user_id: str = "user-critical",
    channel: str = "console",
) -> AgentRequest:
    return AgentRequest(
        session_id=session_id,
        user_id=user_id,
        input=[
            Message(
                role=Role.USER,
                content=[TextContent(type=ContentType.TEXT, text=text)],
            ),
        ],
        channel=channel,
    )


def build_agent_job(
    *,
    workspace_dir: Path,
    text: str = "run critical path",
    timeout_seconds: int = 10,
    channel: str = "console",
) -> CronJobSpec:
    return CronJobSpec(
        id="critical-agent-job",
        name="Critical Agent Job",
        tenant_id="tenant-critical",
        source_id="source-critical",
        schedule=ScheduleSpec(cron="* * * * *"),
        task_type="agent",
        request=CronJobRequest(input=build_agent_request(text=text).input),
        dispatch=DispatchSpec(
            channel=channel,
            target=DispatchTarget(
                user_id="user-critical",
                session_id="session-critical",
            ),
            meta={"workspace_dir": str(workspace_dir)},
        ),
        runtime=JobRuntimeSpec(timeout_seconds=timeout_seconds),
    )


def build_text_job(
    *,
    workspace_dir: Path,
    text: str = "scheduled text",
) -> CronJobSpec:
    return CronJobSpec(
        id="critical-text-job",
        name="Critical Text Job",
        tenant_id="tenant-critical",
        source_id="source-critical",
        schedule=ScheduleSpec(cron="* * * * *"),
        task_type="text",
        text=text,
        dispatch=DispatchSpec(
            channel="console",
            target=DispatchTarget(
                user_id="user-critical",
                session_id="session-critical",
            ),
            meta={"workspace_dir": str(workspace_dir)},
        ),
    )


@pytest.fixture
def recording_channel_manager() -> RecordingChannelManager:
    return RecordingChannelManager()


@pytest.fixture
async def loopback_mcp_server() -> AsyncGenerator[LoopbackMCPServer, None]:
    from mcp.server.fastmcp import FastMCP
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    header_name = "x-app-id"
    header_value = "critical-app"
    calls: list[dict[str, Any]] = []
    port = unused_tcp_port()
    mcp = FastMCP(
        "critical-path-mcp",
        host="127.0.0.1",
        port=port,
        stateless_http=True,
        json_response=True,
    )

    @mcp.tool()
    def echo(text: str) -> str:
        calls.append({"tool": "echo", "text": text})
        return f"echo:{text}"

    app = mcp.streamable_http_app()

    class RequireAppHeaderMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if request.headers.get(header_name) != header_value:
                return JSONResponse(
                    {"error": "missing critical app header"},
                    status_code=403,
                )
            return await call_next(request)

    app.add_middleware(RequireAppHeaderMiddleware)

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    deadline = asyncio.get_running_loop().time() + 5
    while not server.started:
        if task.done():
            task.result()
        if asyncio.get_running_loop().time() > deadline:
            task.cancel()
            raise TimeoutError("loopback FastMCP server did not start")
        await asyncio.sleep(0.01)

    try:
        yield LoopbackMCPServer(
            url=f"http://127.0.0.1:{port}/mcp",
            required_header_name="X-App-Id",
            required_header_value=header_value,
            calls=calls,
        )
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)


@pytest.fixture
def isolated_agent_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Create a tenant-local root config and agent profile on disk."""

    monkeypatch.setattr("swe.config.utils.WORKING_DIR", tmp_path)
    monkeypatch.setattr("swe.config.config.WORKING_DIR", tmp_path)

    workspace_dir = tmp_path / "workspaces" / "critical-agent"
    workspace_dir.mkdir(parents=True)
    config = Config(
        agents=AgentsConfig(
            active_agent="critical-agent",
            profiles={
                "critical-agent": AgentProfileRef(
                    id="critical-agent",
                    workspace_dir=str(workspace_dir),
                ),
            },
        ),
    )
    save_config(config)

    def _write_agent_config(
        *,
        mcp: MCPConfig | None = None,
        max_iters: int = 3,
    ) -> AgentProfileConfig:
        agent_config = AgentProfileConfig(
            id="critical-agent",
            name="Critical Agent",
            workspace_dir=str(workspace_dir),
            mcp=mcp,
            running=AgentsRunningConfig(
                max_iters=max_iters,
                llm_retry_enabled=True,
                llm_max_retries=1,
                llm_backoff_base=0.1,
                llm_backoff_cap=0.5,
            ),
            system_prompt_files=[],
        )
        save_agent_config("critical-agent", agent_config)
        return agent_config

    return workspace_dir, _write_agent_config


def mcp_config_for(server: LoopbackMCPServer) -> MCPConfig:
    return MCPConfig(
        clients={
            "critical": MCPClientConfig(
                name="critical",
                transport="streamable_http",
                url=server.url,
                headers={
                    server.required_header_name: server.required_header_value,
                },
            ),
        },
    )
