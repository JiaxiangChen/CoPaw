# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,unused-argument
import asyncio
import mimetypes
import os
import time
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import anyio
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from agentscope_runtime.engine.app import AgentApp

from ..config import load_config  # pylint: disable=no-name-in-module
from ..config.utils import get_config_path
from ..constant import (
    DOCS_ENABLED,
    LOG_LEVEL_ENV,
    CORS_ORIGINS,
    WORKING_DIR,
)
from ..__version__ import __version__
from ..utils.my_logging import (
    setup_logger,
    shutdown_logger,
)
from .auth import AuthMiddleware
from .middleware.tenant_identity import TenantIdentityMiddleware
from .middleware.tenant_workspace import TenantWorkspaceMiddleware
from .middleware.header_passthrough import HeaderPassthroughMiddleware
from .middleware.liveness_probe import LivenessProbeMiddleware
from .middleware.provider_models_timing import ProviderModelsTimingMiddleware
from .middleware.sse_diagnostic import SSEDiagnosticMiddleware
from .source_system_config.middleware import SourceSystemConfigMiddleware
from .routers import router as api_router, create_agent_scoped_router
from .routers.agent_scoped import AgentContextMiddleware
from .routers.voice import voice_router
from .multi_agent_manager import MultiAgentManager
from .workspace.tenant_pool import TenantWorkspacePool
from .migration import (
    ensure_default_agent_exists,
)
from .channels.registry import register_custom_channel_routes
from ..tracing import init_trace_manager, close_trace_manager
from ..tracing.agent_trace_sdk import shutdown_global_tracer
from .runner.model_call_error_detail import redact_sensitive_fragments
from ..database import get_database_config
from .service_heartbeat import start_service_heartbeat, stop_service_heartbeat
from .crons.notification_worker import CronNotificationWorker
from .runtime_diagnostic import RuntimeDiagnosticManager

# Apply log level on load so reload child process gets same level as CLI.
logger = setup_logger(os.environ.get(LOG_LEVEL_ENV, "info"))

_SKILL_SCAN_HISTORY_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_DYNAMIC_RUNNER_ERROR_MESSAGE_MAX_LENGTH = 512


def _safe_dynamic_runner_error_message(error: Exception) -> str:
    message = redact_sensitive_fragments(str(error))
    if len(message) <= _DYNAMIC_RUNNER_ERROR_MESSAGE_MAX_LENGTH:
        return message
    return message[:_DYNAMIC_RUNNER_ERROR_MESSAGE_MAX_LENGTH] + "..."


# Ensure static assets are served with browser-compatible MIME types across
# platforms (notably Windows may miss .js/.mjs mappings).
mimetypes.init()
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/wasm", ".wasm")


# Dynamic runner that selects the correct workspace runner based on request
class DynamicMultiAgentRunner:
    """Runner wrapper that dynamically routes to the correct workspace runner.

    This allows AgentApp to work with multiple agents by inspecting
    the X-Agent-Id header on each request.
    """

    def __init__(self):
        self.framework_type = "agentscope"
        self._multi_agent_manager = None

    def set_multi_agent_manager(self, manager):
        """Set the MultiAgentManager instance after initialization."""
        self._multi_agent_manager = manager

    async def _get_workspace_runner(self, request):
        """Get the correct workspace runner based on request."""
        from .agent_context import get_current_agent_id
        from ..config.context import get_current_effective_tenant_id

        # Get agent_id from context (set by middleware or header)
        agent_id = get_current_agent_id()
        tenant_id = get_current_effective_tenant_id()

        logger.debug(f"_get_workspace_runner: agent_id={agent_id}")

        # Get the correct workspace runner
        if not self._multi_agent_manager:
            raise RuntimeError("MultiAgentManager not initialized")

        try:
            workspace = await self._multi_agent_manager.get_agent(
                agent_id,
                tenant_id=tenant_id,
            )
            logger.debug(
                "Got workspace: %s, runner: %s",
                workspace.agent_id,
                workspace.runner,
            )
            return workspace.runner
        except ValueError as e:
            logger.error(
                "Agent not found: %s",
                _safe_dynamic_runner_error_message(e),
            )
            raise
        except Exception as e:
            safe_message = _safe_dynamic_runner_error_message(e)
            logger.error(
                "Error getting workspace runner: %s",
                safe_message,
            )
            raise

    async def stream_query(self, request, *args, **kwargs):
        """Dynamically route to the correct workspace runner."""
        from ..config.llm_workload import (
            LLM_WORKLOAD_CHAT,
            bind_llm_workload,
        )

        logger.debug("DynamicMultiAgentRunner.stream_query called")
        try:
            runner = await self._get_workspace_runner(request)
            logger.debug(f"Got runner: {runner}, type: {type(runner)}")
            # Delegate to the actual runner's stream_query generator
            count = 0
            with bind_llm_workload(LLM_WORKLOAD_CHAT):
                async for item in runner.stream_query(
                    request,
                    *args,
                    **kwargs,
                ):
                    count += 1
                    logger.debug(f"Yielding item #{count}: {type(item)}")
                    yield item
            logger.debug(f"stream_query completed, yielded {count} items")
        except Exception as e:
            from agentscope_runtime.engine.schemas.agent_schemas import (
                AgentResponse,
                Error,
            )

            safe_message = _safe_dynamic_runner_error_message(e)
            logger.error(
                "Error in stream_query: %s",
                safe_message,
            )
            request_id = (
                request.get("id")
                if isinstance(request, dict)
                else getattr(
                    request,
                    "id",
                    None,
                )
            )
            yield AgentResponse(id=request_id).failed(
                Error(
                    code="agent_runtime_error",
                    message=safe_message,
                ),
            )

    async def query_handler(self, request, *args, **kwargs):
        """Dynamically route to the correct workspace runner."""
        runner = await self._get_workspace_runner(request)
        # Delegate to the actual runner's query_handler generator
        async for item in runner.query_handler(request, *args, **kwargs):
            yield item

    # Async context manager support for AgentApp lifecycle
    async def __aenter__(self):
        """
        No-op context manager entry (workspaces manage their own runners).
        """
        return self

    async def __aexit__(self, _exc_type, _exc_val, _exc_tb):
        """No-op context manager exit (workspaces manage their own runners)."""
        return None


# Use dynamic runner for AgentApp
runner = DynamicMultiAgentRunner()

agent_app = AgentApp(
    app_name="Friday",
    app_description="A helpful assistant with background task support",
    runner=runner,
    enable_stream_task=True,
    stream_task_queue="stream_query",
    stream_task_timeout=300,
)

runtime_diagnostic_manager = RuntimeDiagnosticManager()


def _build_internal_cron_callback_url() -> str:
    """构造外部调度平台回调到 SWE 的内部 cron callback 地址。"""
    base = (
        os.environ.get("SWE_SERVER_DOMAIN", "").strip()
        or "http://localhost:8000"
    )
    return f"{base}/api/internal/cron/callback"


def _get_positive_int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default

    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid %s=%r; using default %s",
            name,
            raw_value,
            default,
        )
        return default

    if value <= 0:
        logger.warning(
            "Invalid %s=%r; using default %s",
            name,
            raw_value,
            default,
        )
        return default

    return value


def _configure_async_thread_pools() -> None:
    anyio_thread_tokens = _get_positive_int_env("ANYIO_THREAD_TOKENS", 32)
    asyncio_executor_workers = _get_positive_int_env(
        "ASYNCIO_EXECUTOR_WORKERS",
        32,
    )

    anyio_limiter = anyio.to_thread.current_default_thread_limiter()
    anyio_limiter.total_tokens = anyio_thread_tokens

    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        ThreadPoolExecutor(max_workers=asyncio_executor_workers),
    )
    logger.info(
        "Configured async thread pools: anyio_thread_tokens=%s, "
        "asyncio_executor_workers=%s",
        anyio_thread_tokens,
        asyncio_executor_workers,
    )


async def _reset_scope_sensitive_runtime_state(app: FastAPI) -> None:
    """在开始提供 source-scoped 流量前清空长期运行态缓存。"""
    existing_manager = getattr(app.state, "multi_agent_manager", None)
    if existing_manager is not None:
        logger.info(
            "Resetting existing MultiAgentManager before scope cutover...",
        )
        try:
            await existing_manager.stop_all()
            app.state.multi_agent_manager = None
        finally:
            runtime_diagnostic_manager.set_workspace_metrics(None)

    from ..providers.provider_manager import ProviderManager
    from ..providers.rate_limiter import reset_rate_limiter
    from ..tenant_models.manager import TenantModelManager

    ProviderManager.reset_instance_cache()
    TenantModelManager.invalidate_cache()
    reset_rate_limiter()
    runner.set_multi_agent_manager(None)
    logger.info("Scope-sensitive runtime caches reset")


async def _initialize_database_connection() -> Any | None:
    """初始化数据库连接；未配置远端数据库时返回 None。"""
    database_config = get_database_config()
    logger.info(
        "Database config: host=%s, port=%s, database=%s",
        database_config.host,
        database_config.port,
        database_config.database,
    )

    if database_config.host == "localhost":
        logger.info("Database connection is disabled for localhost")
        return None
    if not database_config.host:
        return None

    try:
        from ..database import DatabaseConnection

        db_connection = DatabaseConnection(database_config)
        await db_connection.connect()
        if not db_connection.is_connected:
            raise RuntimeError(
                "Database connection failed. Please check database configuration.",
            )
        logger.info(
            "Database connection established: %s",
            database_config.host,
        )
        return db_connection
    except Exception as e:
        import traceback

        logger.error(
            "Failed to initialize database connection: %s\n%s",
            e,
            traceback.format_exc(),
        )
        raise RuntimeError(
            "Database connection is required. Please check database configuration.",
        ) from e


def _env_enabled(key: str, default: str = "false") -> bool:
    return os.environ.get(key, default).lower() in ("true", "1", "yes")


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


async def _initialize_runtime_managers(
    app: FastAPI,
) -> tuple[TenantWorkspacePool, MultiAgentManager]:
    """初始化租户工作区池和多 Agent 管理器。"""
    logger.info("Initializing TenantWorkspacePool (registry only)...")
    tenant_workspace_pool = TenantWorkspacePool(WORKING_DIR)
    app.state.tenant_workspace_pool = tenant_workspace_pool

    logger.info("Initializing MultiAgentManager (container only)...")
    multi_agent_manager = MultiAgentManager()
    app.state.multi_agent_manager = multi_agent_manager

    if isinstance(runner, DynamicMultiAgentRunner):
        runner.set_multi_agent_manager(multi_agent_manager)

    async def _get_agent_by_id(agent_id: str = None):
        """按 ID 获取 Agent；未指定时使用当前激活 Agent。"""
        if agent_id is None:
            config = load_config(get_config_path())
            agent_id = config.agents.active_agent or "default"
        return await multi_agent_manager.get_agent(agent_id)

    app.state.get_agent_by_id = _get_agent_by_id
    return tenant_workspace_pool, multi_agent_manager


async def _initialize_tracing_manager(db_connection: Any | None) -> None:
    """根据环境变量初始化 tracing；失败只降级记录日志。"""
    try:
        from ..tracing.config import TracingConfig

        if not _env_enabled("SWE_TRACING_ENABLED"):
            logger.info("Tracing is disabled via SWE_TRACING_ENABLED")
            return

        tracing_config = TracingConfig(
            enabled=True,
            batch_size=_env_int("SWE_TRACING_BATCH_SIZE", 100),
            flush_interval=_env_int("SWE_TRACING_FLUSH_INTERVAL", 5),
            retention_days=_env_int("SWE_TRACING_RETENTION_DAYS", 30),
            sanitize_output=_env_enabled(
                "SWE_TRACING_SANITIZE_OUTPUT",
                "true",
            ),
            max_output_length=_env_int("SWE_TRACING_MAX_OUTPUT_LENGTH", 500),
            database=db_connection.config if db_connection else None,
        )
        await init_trace_manager(tracing_config, db_connection)
        logger.info(
            "Tracing manager initialized (db_mode=%s)",
            db_connection is not None,
        )
    except Exception as e:
        import traceback

        logger.warning(
            "Failed to initialize tracing manager: %s\n%s",
            e,
            traceback.format_exc(),
        )


def _initialize_source_system_config(
    app: FastAPI,
    db_connection: Any | None,
    tenant_workspace_pool: TenantWorkspacePool,
    multi_agent_manager: MultiAgentManager,
) -> None:
    """初始化 source system 配置和任务调度绑定。"""
    try:
        from .source_system_config.service import SourceSystemConfigService
        from .source_system_config.store import SourceSystemConfigStore
        from .source_system_config.task_binding_store import (
            SourceSystemTaskBindingStore,
        )
        from .source_system_config.task_scheduler import (
            SourceSystemTaskScheduler,
        )
        from .workspace.workspace import _build_scheduler_adapter

        source_config_service = SourceSystemConfigService(
            SourceSystemConfigStore(db_connection),
        )
        app.state.source_system_config_service = source_config_service
        app.state.source_system_task_scheduler = None

        has_task_binding_db = db_connection is not None and bool(
            getattr(db_connection, "is_connected", False),
        )
        if has_task_binding_db:
            app.state.source_system_task_scheduler = SourceSystemTaskScheduler(
                binding_store=SourceSystemTaskBindingStore(db_connection),
                scheduler_adapter=_build_scheduler_adapter(),
                callback_url=_build_internal_cron_callback_url(),
                tenant_scope_store_factory=(
                    lambda: tenant_workspace_pool.init_source_store
                ),
                multi_agent_manager=multi_agent_manager,
                tenant_dir_resolver=(
                    tenant_workspace_pool.get_tenant_workspace_dir
                ),
                continuous_governance_service_factory=(
                    lambda: getattr(
                        app.state,
                        "continuous_governance_service",
                        None,
                    )
                ),
                source_config_resolver=(
                    lambda source_id: source_config_service.resolve_config(
                        source_id,
                        force_refresh=True,
                    )
                ),
                agent_id="default",
            )
        else:
            logger.warning(
                "Source system task scheduler skipped: database is not connected",
            )
        multi_agent_manager.set_source_system_config_service(
            source_config_service,
        )
        tenant_workspace_pool.set_source_system_config_service(
            source_config_service,
        )
        logger.info("SourceSystemConfig module initialized")
    except Exception as e:
        logger.warning("Failed to initialize source system config: %s", e)


def _initialize_source_tools(app: FastAPI) -> None:
    """Initialize the source-owned tool catalogue outside tenant workspaces."""
    try:
        from ..constant import WORKING_DIR
        from .source_tools.service import (
            SourceToolService,
            install_source_tool_service,
        )
        from .source_tools.store import SourceToolStore

        app.state.source_tool_service = SourceToolService(
            SourceToolStore(WORKING_DIR / "source_tool_library"),
        )
        install_source_tool_service(app.state.source_tool_service)
        logger.info("Source tool library initialized")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to initialize source tool library: %s", exc)


async def _initialize_approval_audit_store(
    app: FastAPI,
    db_connection: Any | None,
) -> None:
    """初始化工具审批审计存储。"""
    try:
        from .approvals import get_approval_service
        from .approvals.store import ApprovalAuditStore

        approval_audit_store = ApprovalAuditStore(db_connection)
        app.state.approval_audit_store = approval_audit_store
        get_approval_service().set_store(approval_audit_store)
        if approval_audit_store.is_available:
            logger.info("Approval audit storage initialized")
        else:
            logger.warning(
                "Approval audit storage skipped: database is not connected",
            )
    except Exception as e:
        logger.warning("Failed to initialize approval audit storage: %s", e)


async def _initialize_skill_scan_history(
    app: FastAPI,
    db_connection: Any | None,
) -> None:
    """Initialize database-only skill scan history persistence."""
    from ..security.skill_scanner import install_skill_scan_history_recorder
    from ..security.skill_scanner.history import (
        SkillScanHistoryRecorder,
        SkillScanHistoryStore,
    )

    store = SkillScanHistoryStore(db_connection)
    app.state.skill_scan_history_store = store
    app.state.skill_scan_history_recorder = None
    install_skill_scan_history_recorder(None)
    if not store.is_available:
        logger.warning(
            "Skill scan history storage skipped: database is not connected",
        )
        return

    try:
        recorder = SkillScanHistoryRecorder(store)
        await recorder.start()
        app.state.skill_scan_history_recorder = recorder
        install_skill_scan_history_recorder(recorder)
        logger.info("Skill scan history storage initialized")
    except Exception as exc:
        app.state.skill_scan_history_store = SkillScanHistoryStore(None)
        install_skill_scan_history_recorder(None)
        logger.warning(
            "Failed to initialize skill scan history storage: %s",
            exc,
        )


async def _initialize_cron_broadcast_children_store(
    app: FastAPI,
    db_connection: Any | None,
) -> None:
    """初始化定时任务分发用户反查快照存储。"""
    try:
        from .crons.broadcast_children_store import CronBroadcastChildrenStore

        cron_broadcast_children_store = CronBroadcastChildrenStore(
            db_connection,
        )
        app.state.cron_broadcast_children_store = cron_broadcast_children_store
        if cron_broadcast_children_store.is_available:
            logger.info("Cron broadcast children snapshot storage initialized")
        else:
            logger.warning(
                "Cron broadcast children snapshot storage uses memory fallback",
            )
    except Exception as e:
        logger.warning(
            "Failed to initialize cron broadcast children storage: %s",
            e,
        )


async def _initialize_cron_broadcast_task_store(
    app: FastAPI,
    db_connection: Any | None,
) -> None:
    """初始化定时任务广播分发进度存储。"""
    try:
        from .crons.broadcast_task_store import CronBroadcastTaskStore

        cron_broadcast_task_store = CronBroadcastTaskStore(db_connection)
        app.state.cron_broadcast_task_store = cron_broadcast_task_store
        if cron_broadcast_task_store.is_available:
            logger.info("Cron broadcast task storage initialized")
        else:
            logger.warning(
                "Cron broadcast task storage uses memory fallback",
            )
    except Exception as e:
        logger.warning(
            "Failed to initialize cron broadcast task storage: %s",
            e,
        )


async def _initialize_skill_readiness(
    app: FastAPI,
    db_connection: Any | None,
    multi_agent_manager: MultiAgentManager,
) -> None:
    """初始化技能可执行性自检存储和服务。"""
    try:
        from .skill_readiness.service import build_skill_readiness_service
        from .skill_readiness.store import SkillReadinessStore

        skill_readiness_store = SkillReadinessStore(db_connection)
        app.state.skill_readiness_store = skill_readiness_store
        app.state.skill_readiness_service = build_skill_readiness_service(
            skill_readiness_store,
            multi_agent_manager=multi_agent_manager,
        )
        if skill_readiness_store.is_available:
            logger.info("SkillReadiness storage initialized")
        else:
            logger.warning(
                "SkillReadiness storage skipped: database is not connected",
            )
    except Exception as e:
        logger.warning("Failed to initialize skill readiness storage: %s", e)


def _initialize_continuous_governance(
    app: FastAPI,
    db_connection: Any | None,
    tenant_workspace_pool: TenantWorkspacePool,
    multi_agent_manager: MultiAgentManager,
) -> None:
    """初始化持续治理读模型服务。"""
    try:
        from .continuous_governance.service import ContinuousGovernanceService
        from .continuous_governance.store import ContinuousGovernanceStore

        app.state.continuous_governance_service = ContinuousGovernanceService(
            ContinuousGovernanceStore(db_connection),
        )
        multi_agent_manager.set_continuous_governance_service(
            app.state.continuous_governance_service,
        )
        tenant_workspace_pool.set_continuous_governance_service(
            app.state.continuous_governance_service,
        )
        logger.info("ContinuousGovernance module initialized")
    except Exception as e:
        logger.warning("Failed to initialize continuous governance: %s", e)


async def _initialize_database_backed_modules(
    db_connection: Any | None,
) -> None:
    """初始化只在数据库可用时启用的附属模块。"""
    if db_connection is None:
        return
    try:
        from .goals.registry import initialize_goal_service
        from .greeting.router import init_greeting_module
        from .featured_case.router import init_featured_case_module
        from .feedback.router import init_feedback_module
        from .skill_result.router import init_skill_result_module
        from .html_preview_clicks.router import (
            init_html_preview_click_module,
        )
        from .scenario_preset.router import init_scenario_preset_module
        from .chat_sharing.router import initialize_chat_sharing_module

        await initialize_goal_service(db_connection)
        init_greeting_module(db_connection)
        init_featured_case_module(db_connection)
        init_feedback_module(db_connection)
        init_skill_result_module(db_connection)
        init_html_preview_click_module(db_connection)
        await init_scenario_preset_module(db_connection)
        await initialize_chat_sharing_module(db_connection)
        logger.info(
            "Greeting, FeaturedCase, Feedback, SkillResult, HTML preview click "
            "ScenarioPreset and ChatSharing modules initialized",
        )

        from .workspace.tenant_init_source_store import (
            init_tenant_init_source_module,
        )

        init_tenant_init_source_module(db_connection)
        logger.info("TenantInitSource module initialized")

        from .channels.zhaohu.binding_store import (
            init_zhaohu_binding_module,
        )

        init_zhaohu_binding_module(db_connection)
        logger.info("ZhaohuChannelBinding module initialized")

        from .asset_upload_record.router import (
            init_asset_upload_record_module,
        )

        init_asset_upload_record_module(db_connection)
        logger.info("AssetUploadRecord module initialized")
    except Exception as e:
        logger.warning(
            "Failed to initialize greeting/featured_case modules: %s",
            e,
        )


async def _start_lifespan_background_services(
    app: FastAPI,
    multi_agent_manager: MultiAgentManager,
) -> None:
    """启动生命周期内常驻的后台服务。"""
    from ..security.tool_guard.watcher import ToolGuardConfigWatcher

    tool_guard_watcher = ToolGuardConfigWatcher()
    await tool_guard_watcher.start()
    app.state.tool_guard_config_watcher = tool_guard_watcher

    await start_service_heartbeat()
    # get_monitor_sync_client().schedule_swe_cron_warmup(
    #     start_delay_seconds=5.0,
    # )
    cron_notification_worker = CronNotificationWorker(
        multi_agent_manager=multi_agent_manager,
    )
    app.state.cron_notification_worker = cron_notification_worker
    cron_notification_worker.start()
    await multi_agent_manager.start_workspace_cleanup_loop()

    try:
        runtime_diagnostic_manager.set_workspace_metrics(
            multi_agent_manager.workspace_cache_metrics,
        )
        await runtime_diagnostic_manager.start()
    except Exception as e:
        logger.warning("Failed to start runtime diagnostic manager: %s", e)


# pylint: disable=too-many-statements
async def _shutdown_lifespan_resources(
    app: FastAPI,
    db_connection: Any | None,
) -> None:
    """按依赖顺序关闭生命周期资源。"""
    tool_guard_watcher = getattr(app.state, "tool_guard_config_watcher", None)
    if tool_guard_watcher is not None:
        await tool_guard_watcher.stop()

    try:
        from ..agents.tools.background_process import (
            managed_background_process_manager,
        )

        managed_background_process_manager.stop_all()
    except Exception as e:
        logger.warning("Error stopping managed background processes: %s", e)

    try:
        await runtime_diagnostic_manager.stop()
    except Exception as e:
        logger.warning("Error stopping runtime diagnostic manager: %s", e)
    finally:
        runtime_diagnostic_manager.set_workspace_metrics(None)

    startup_dispatch_task = getattr(
        app.state,
        "cron_startup_dispatch_broadcast_children_task",
        None,
    )
    if startup_dispatch_task is not None and not startup_dispatch_task.done():
        startup_dispatch_task.cancel()
        try:
            await startup_dispatch_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(
                "Error stopping startup dispatch child task: %s",
                e,
            )

    cron_notification_worker = getattr(
        app.state,
        "cron_notification_worker",
        None,
    )
    if cron_notification_worker is not None:
        try:
            await cron_notification_worker.stop()
        except Exception as e:
            logger.warning("Error stopping cron notification worker: %s", e)

    try:
        await close_trace_manager()
        logger.info("Tracing manager closed")
    except Exception as e:
        logger.warning("Error closing tracing manager: %s", e)

    try:
        shutdown_global_tracer()
    except Exception as e:
        logger.warning("Error closing AgentTraceSDK global tracer: %s", e)

    try:
        await stop_service_heartbeat()
    except Exception as e:
        logger.warning("Error stopping service heartbeat: %s", e)
    await _stop_multi_agent_manager(app)
    await _stop_tenant_workspace_pool(app)

    history_recorder = getattr(
        app.state,
        "skill_scan_history_recorder",
        None,
    )
    if history_recorder is not None:
        try:
            await asyncio.wait_for(
                history_recorder.stop(),
                timeout=_SKILL_SCAN_HISTORY_SHUTDOWN_TIMEOUT_SECONDS,
            )
            logger.info("Skill scan history recorder stopped")
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out stopping skill scan history recorder; "
                "continuing application shutdown",
            )
        except Exception as e:
            logger.warning("Error stopping skill scan history recorder: %s", e)
    try:
        from ..security.skill_scanner import (
            install_skill_scan_history_recorder,
        )

        install_skill_scan_history_recorder(None)
    except Exception as e:
        logger.warning("Error detaching skill scan history recorder: %s", e)

    if db_connection:
        try:
            await db_connection.close()
            logger.info("Database connection closed")
        except Exception as e:
            logger.warning("Error closing database connection: %s", e)

    logger.info("Application shutdown complete")

    shutdown_logger()


async def _stop_multi_agent_manager(app: FastAPI) -> None:
    multi_agent_mgr = getattr(app.state, "multi_agent_manager", None)
    if multi_agent_mgr is None:
        return
    logger.info("Stopping MultiAgentManager...")
    try:
        await multi_agent_mgr.stop_all()
    except Exception as e:
        logger.error(f"Error stopping MultiAgentManager: {e}")


async def _stop_tenant_workspace_pool(app: FastAPI) -> None:
    tenant_pool = getattr(app.state, "tenant_workspace_pool", None)
    if tenant_pool is None:
        return
    logger.info("Stopping all tenant workspaces...")
    try:
        await tenant_pool.stop_all()
    except Exception as e:
        logger.error(f"Error stopping tenant workspaces: {e}")


@asynccontextmanager
async def lifespan(
    app: FastAPI,
):
    startup_start_time = time.time()
    _configure_async_thread_pools()

    # Auto-register admin from env vars (for automated deployments)
    from .auth import auto_register_from_env

    auto_register_from_env()

    # --- Minimal startup: only ensure default agent declaration exists ---
    logger.info("Performing minimal startup...")
    ensure_default_agent_exists()

    # source-scoped cutover 需要在开始接流量前清空旧的进程级缓存，
    # 避免同一进程继续复用 tenant-only 运行态。
    await _reset_scope_sensitive_runtime_state(app)
    tenant_workspace_pool, multi_agent_manager = (
        await _initialize_runtime_managers(app)
    )

    # Note: ProviderManager, skill pool, and QA agent are initialized
    # on-demand via their respective feature entrypoints.
    # See design.md for lazy-loading architecture.

    # --- Initialize database connection (required for tracing and instance modules) ---
    db_connection = await _initialize_database_connection()
    app.state.db_connection = db_connection

    await _initialize_tracing_manager(db_connection)
    logger.info("Instance module initialized")

    # --- 初始化 source 系统配置模块 ---
    _initialize_source_system_config(
        app,
        db_connection,
        tenant_workspace_pool,
        multi_agent_manager,
    )
    _initialize_source_tools(app)

    # --- 初始化定时任务分发用户反查快照 ---
    await _initialize_approval_audit_store(app, db_connection)
    await _initialize_skill_scan_history(app, db_connection)
    await _initialize_cron_broadcast_children_store(app, db_connection)
    await _initialize_cron_broadcast_task_store(app, db_connection)

    # --- 初始化技能就绪检查存储 ---
    await _initialize_skill_readiness(
        app,
        db_connection,
        multi_agent_manager,
    )

    # --- 初始化持续治理管理侧数据库读模型 ---
    _initialize_continuous_governance(
        app,
        db_connection,
        tenant_workspace_pool,
        multi_agent_manager,
    )

    await _initialize_database_backed_modules(db_connection)

    startup_elapsed = time.time() - startup_start_time
    logger.info(
        f"Application startup completed in {startup_elapsed:.3f} seconds "
        f"(minimal initialization - runtimes deferred to first use)",
    )

    # 启动服务心跳任务
    await _start_lifespan_background_services(app, multi_agent_manager)

    try:
        yield
    finally:
        await _shutdown_lifespan_resources(app, db_connection)


app = FastAPI(
    lifespan=lifespan,
    docs_url="/docs" if DOCS_ENABLED else None,
    redoc_url="/redoc" if DOCS_ENABLED else None,
    openapi_url="/openapi.json" if DOCS_ENABLED else None,
)
app.state.runtime_diagnostic_manager = runtime_diagnostic_manager

# Apply CORS middleware if CORS_ORIGINS is set
# Note: add_middleware inserts at the beginning of the stack, so the LAST
# added middleware wraps the OUTERMOST and executes FIRST on requests.
# Order (last-added = first-executed): CORSMiddleware -> AuthMiddleware ->
#   AgentContextMiddleware -> TenantWorkspaceMiddleware -> TenantIdentityMiddleware
if CORS_ORIGINS:
    origins = [o.strip() for o in CORS_ORIGINS.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "Content-Disposition",
            "X-Swe-Msgid",
            "X-Swe-Sessionid",
            "X-Swe-Chatid",
        ],
    )

app.add_middleware(AuthMiddleware)

# Add agent context middleware for agent-scoped routes
app.add_middleware(AgentContextMiddleware)

# Add tenant workspace middleware (loads workspace from pool)
# Must execute after TenantIdentityMiddleware sets tenant_id
app.add_middleware(TenantWorkspaceMiddleware)

# 在身份解析后绑定 source 系统配置
app.add_middleware(SourceSystemConfigMiddleware)

# Add tenant identity middleware last so it executes FIRST
# This must set tenant_id before TenantWorkspaceMiddleware needs it
app.add_middleware(TenantIdentityMiddleware, default_tenant_id=None)

# Add header passthrough middleware for MCP server requests
# Extracts x-header-* headers and stores in context for MCP clients
app.add_middleware(HeaderPassthroughMiddleware)

# Track SSE responses as the outermost middleware.
app.add_middleware(
    SSEDiagnosticMiddleware,
    manager=runtime_diagnostic_manager,
)

# 只针对模型列表接口记录业务中间件入口到响应生成的总耗时。
app.add_middleware(ProviderModelsTimingMiddleware)

# Keep the Kubernetes liveness probe outside business middleware. This proves
# the process can answer HTTP without touching auth, tenant, source, DB, or
# Agent runtime paths.
app.add_middleware(LivenessProbeMiddleware)


# Console static dir: env, or swe package data (console), or cwd.
_CONSOLE_STATIC_ENV = "SWE_CONSOLE_STATIC_DIR"


def _resolve_console_static_dir() -> str:
    if os.environ.get(_CONSOLE_STATIC_ENV):
        return os.environ[_CONSOLE_STATIC_ENV]
    # Shipped dist lives in swe package as static data
    pkg_dir = Path(__file__).resolve().parent.parent
    candidate = pkg_dir / "console"
    if candidate.is_dir() and (candidate / "index.html").exists():
        return str(candidate)

    # Fallback to repo data
    repo_dir = pkg_dir.parent.parent
    candidate = repo_dir / "console" / "dist"
    if candidate.is_dir() and (candidate / "index.html").exists():
        return str(candidate)

    # Fallback to cwd data
    cwd = Path(os.getcwd())
    for subdir in ("console/dist", "console_dist"):
        candidate = cwd / subdir
        if candidate.is_dir() and (candidate / "index.html").exists():
            return str(candidate)

    fallback = cwd / "console" / "dist"
    logger.warning(
        f"Console static directory not found. Falling back to '{fallback}'.",
    )
    return str(fallback)


_CONSOLE_STATIC_DIR = _resolve_console_static_dir()
_CONSOLE_INDEX = (
    Path(_CONSOLE_STATIC_DIR) / "index.html" if _CONSOLE_STATIC_DIR else None
)
_console_path = Path(_CONSOLE_STATIC_DIR)
_assets_dir = _console_path / "assets"
logger.info(f"STATIC_DIR: {_CONSOLE_STATIC_DIR}")


@app.get("/")
def read_root():
    if _CONSOLE_INDEX and _CONSOLE_INDEX.exists():
        return FileResponse(_CONSOLE_INDEX)
    return {
        "message": (
            "SWE Web Console is not available. "
            "If you installed SWE from source code, please run "
            "`npm ci && npm run build` in SWE's `console/` "
            "directory, and restart SWE to enable the "
            "web console."
        ),
    }


@app.get("/api/version")
def get_version():
    """Return the current SWE version."""
    return {"version": __version__}


@app.get("/api/health/health")
def get_api_health():
    """Lightweight health check endpoint for load balancers and probes."""
    return {"status": "ok"}


app.include_router(api_router, prefix="/api")

# Agent-scoped router: /api/agents/{agentId}/chats, etc.
agent_scoped_router = create_agent_scoped_router()
app.include_router(agent_scoped_router, prefix="/api")


app.include_router(
    agent_app.router,
    prefix="/api/agent",
    tags=["agent"],
)

# Voice channel: Twilio-facing endpoints at root level (not under /api/).
# POST /voice/incoming, WS /voice/ws, POST /voice/status-callback
app.include_router(voice_router, tags=["voice"])

# Custom channel routes (before SPA catch-all to ensure route priority)
register_custom_channel_routes(app)


# 按运行时静态作用域提供文件：/static/{scope_id}/{agent_id}/{path}
# 这里的第一段路径是运行时 scope_id，不一定等于逻辑 user_id。
@app.get("/static/{scope_id}/{agent_id}/{file_name:path}")
async def serve_user_static(
    scope_id: str,
    agent_id: str,
    file_name: str,
):
    """从运行时静态作用域目录返回公开文件。

    Args:
        scope_id: 运行时静态作用域标识，用于定位租户或 source-scoped 目录
        agent_id: Agent 标识
        file_name: 静态目录下的相对文件路径

    Returns:
        文件存在时返回 ``FileResponse``，否则返回 404
    """
    from ..constant import WORKING_DIR

    logger.debug(f"Serving static files from scope {scope_id}")

    static_dir = (
        WORKING_DIR / scope_id / "workspaces" / agent_id / "static"
    ).resolve()

    # 防止通过 file_name 逃逸出当前 scope 的静态目录。
    try:
        target = (static_dir / file_name).resolve()
        target.relative_to(static_dir)
    except ValueError as exc:
        # Path traversal attempt detected
        raise HTTPException(
            status_code=400,
            detail="Invalid file path",
        ) from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    # Guess MIME type
    media_type, _ = mimetypes.guess_type(str(target))
    if media_type is None:
        media_type = "application/octet-stream"
    return FileResponse(Path(target), media_type=media_type)


def _serve_console_index():
    if _CONSOLE_INDEX and _CONSOLE_INDEX.exists():
        return FileResponse(_CONSOLE_INDEX)

    raise HTTPException(status_code=404, detail="Not Found")


def _serve_console_file(file_name: str, media_type: str):
    file_path = _console_path / file_name
    if file_path.is_file():
        return FileResponse(file_path, media_type=media_type)
    raise HTTPException(status_code=404, detail="Not Found")


def _console_logo():
    return _serve_console_file("logo.png", "image/png")


def _console_dark_logo():
    return _serve_console_file("dark-logo.png", "image/png")


def _console_icon():
    return _serve_console_file("swe-symbol.svg", "image/svg+xml")


def _console_dark_icon():
    return _serve_console_file("swe-dark.png", "image/png")


def _console_spa_alias(full_path: str = ""):
    _ = full_path
    return _serve_console_index()


def _console_assets(file_path: str):
    """Serve static assets from console assets directory.
    Uses dynamic file lookup so assets can be added after startup.
    """
    if not _assets_dir.is_dir():
        raise HTTPException(
            status_code=404,
            detail="Assets directory not found",
        )
    full_path = _assets_dir / file_path
    try:
        full_path.resolve().relative_to(_assets_dir.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Not Found") from exc
    if not full_path.is_file():
        raise HTTPException(status_code=404, detail="Not Found")
    # Guess content type
    content_type, _ = mimetypes.guess_type(str(full_path))
    return FileResponse(full_path, media_type=content_type)


def _console_spa(full_path: str):
    # Prevent catching common system/special paths
    if full_path in ("docs", "redoc", "openapi.json"):
        raise HTTPException(status_code=404, detail="Not Found")
    # Skip API routes (should already be matched due to registration order)
    if full_path.startswith("api/") or full_path == "api":
        raise HTTPException(status_code=404, detail="Not Found")
    return _serve_console_index()


def _register_console_static_routes() -> None:
    if not os.path.isdir(_CONSOLE_STATIC_DIR):
        return

    app.add_api_route("/logo.png", _console_logo, methods=["GET"])
    app.add_api_route("/dark-logo.png", _console_dark_logo, methods=["GET"])
    app.add_api_route("/swe-symbol.svg", _console_icon, methods=["GET"])
    app.add_api_route("/swe-dark.png", _console_dark_icon, methods=["GET"])
    if _assets_dir.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=str(_assets_dir)),
            name="assets",
        )
    app.add_api_route("/console", _console_spa_alias, methods=["GET"])
    app.add_api_route("/console/", _console_spa_alias, methods=["GET"])
    app.add_api_route(
        "/console/{full_path:path}",
        _console_spa_alias,
        methods=["GET"],
    )
    app.add_api_route(
        "/static/{file_path:path}",
        _console_assets,
        methods=["GET"],
    )
    # SPA fallback 必须最后注册，避免吞掉前面的 API 和系统路由。
    app.add_api_route("/{full_path:path}", _console_spa, methods=["GET"])


# Console static files and SPA fallback
# Register these AFTER API routes to ensure proper routing priority
_register_console_static_routes()
