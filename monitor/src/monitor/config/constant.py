# -*- coding: utf-8 -*-
"""Monitor 常量与环境变量加载工具."""

import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_ENV_VAR_OVERRIDES: ContextVar[dict[str, str]] = ContextVar(
    "monitor_env_var_overrides",
    default=None,  # type: ignore[arg-type]
)


def _get_overrides() -> dict[str, str]:
    """Get current env var overrides."""
    val = _ENV_VAR_OVERRIDES.get()
    return val if val is not None else {}


@contextmanager
def env_var_overrides(overrides: dict[str, str]):
    """Temporarily override environment variables."""
    current = _get_overrides().copy()
    current.update(overrides)
    token = _ENV_VAR_OVERRIDES.set(current)
    try:
        yield
    finally:
        _ENV_VAR_OVERRIDES.reset(token)


class EnvVarLoader:
    """Utility to load and parse environment variables with type safety."""

    @staticmethod
    def get_bool(env_var: str, default: bool = False) -> bool:
        """Get a boolean environment variable."""
        overrides = _get_overrides()
        val = overrides.get(
            env_var,
            os.environ.get(env_var, str(default)),
        ).lower()
        return val in ("true", "1", "yes")

    @staticmethod
    def get_float(
        env_var: str,
        default: float = 0.0,
        min_value: float | None = None,
        max_value: float | None = None,
        allow_inf: bool = False,
    ) -> float:
        """Get a float environment variable with optional bounds."""
        try:
            overrides = _get_overrides()
            value = float(
                overrides.get(env_var, os.environ.get(env_var, str(default))),
            )
            if min_value is not None and value < min_value:
                return min_value
            if max_value is not None and value > max_value:
                return max_value
            if not allow_inf and (
                value == float("inf") or value == float("-inf")
            ):
                return default
            return value
        except (TypeError, ValueError):
            return default

    @staticmethod
    def get_int(
        env_var: str,
        default: int = 0,
        min_value: int | None = None,
        max_value: int | None = None,
    ) -> int:
        """Get an integer environment variable with optional bounds."""
        try:
            overrides = _get_overrides()
            value = int(
                overrides.get(env_var, os.environ.get(env_var, str(default))),
            )
            if min_value is not None and value < min_value:
                return min_value
            if max_value is not None and value > max_value:
                return max_value
            return value
        except (TypeError, ValueError):
            return default

    @staticmethod
    def get_str(env_var: str, default: str = "") -> str:
        """Get a string environment variable."""
        overrides = _get_overrides()
        return overrides.get(env_var, os.environ.get(env_var, default))


# ============================================================
# 核心目录配置
# ============================================================

WORKING_DIR = (
    Path(EnvVarLoader.get_str("MONITOR_WORKING_DIR", "~/.monitor"))
    .expanduser()
    .resolve()
)

SECRET_DIR = (
    Path(
        EnvVarLoader.get_str(
            "MONITOR_SECRET_DIR",
            f"{WORKING_DIR}.secret",
        ),
    )
    .expanduser()
    .resolve()
)

CONFIG_FILE = EnvVarLoader.get_str("MONITOR_CONFIG_FILE", "config.json")

LOG_LEVEL_ENV = "MONITOR_LOG_LEVEL"

# ============================================================
# 应用配置
# ============================================================

ENV_NAME = EnvVarLoader.get_str("MONITOR_ENV", "prd")

DOCS_ENABLED = EnvVarLoader.get_bool("MONITOR_OPENAPI_DOCS", False)

CORS_ORIGINS = EnvVarLoader.get_str("MONITOR_CORS_ORIGINS", "").strip()

DEFAULT_PORT = EnvVarLoader.get_int("MONITOR_PORT", 9090, min_value=1)
DEFAULT_HOST = EnvVarLoader.get_str("MONITOR_HOST", "127.0.0.1")

# ============================================================
# 数据库配置
# ============================================================

DB_HOST = EnvVarLoader.get_str("MONITOR_DB_HOST", "")
DB_PORT = EnvVarLoader.get_int("MONITOR_DB_PORT", 3306, min_value=1)
DB_USER = EnvVarLoader.get_str("MONITOR_DB_USER", "root")
DB_ACCESS = EnvVarLoader.get_str("MONITOR_DB_ACCESS", "").removeprefix("BEE_")
DB_NAME = EnvVarLoader.get_str("MONITOR_DB_NAME", "monitor")
DB_MIN_CONN = EnvVarLoader.get_int("MONITOR_DB_MIN_CONN", 2, min_value=1)
DB_MAX_CONN = EnvVarLoader.get_int("MONITOR_DB_MAX_CONN", 10, min_value=1)
# 生产库通常已经由 DBA 建好表，Monitor 启动时不应默认执行 CREATE。
DB_INIT_TABLES = EnvVarLoader.get_bool("MONITOR_DB_INIT_TABLES", False)

# ============================================================
# 监控配置
# ============================================================

MONITOR_INTERVAL = EnvVarLoader.get_int(
    "MONITOR_MONITOR_INTERVAL",
    60,
    min_value=10,
)

ALERT_ENABLED = EnvVarLoader.get_bool("MONITOR_ALERT_ENABLED", False)

ALERT_RETRY_COUNT = EnvVarLoader.get_int(
    "MONITOR_ALERT_RETRY_COUNT",
    3,
    min_value=1,
)

# ============================================================
# 请求超时配置
# ============================================================

REQUEST_TIMEOUT_SECONDS = EnvVarLoader.get_float(
    "MONITOR_REQUEST_TIMEOUT_SECONDS",
    60.0,
    min_value=10.0,
)

API_CALL_TIMEOUT = EnvVarLoader.get_float(
    "MONITOR_API_CALL_TIMEOUT",
    30.0,
    min_value=5.0,
)

HEALTH_CHECK_TIMEOUT = EnvVarLoader.get_float(
    "MONITOR_HEALTH_CHECK_TIMEOUT",
    10.0,
    min_value=1.0,
)

# ============================================================
# Elasticsearch 配置
# ============================================================

ES_HOST = EnvVarLoader.get_str("ES_HOST", "")
ES_PORT = EnvVarLoader.get_int("ES_PORT", 9200, min_value=1)
ES_USER = EnvVarLoader.get_str("ES_USER", "")
ES_ACCESS = EnvVarLoader.get_str("ES_ACCESS", "").removeprefix("BEE_")
ES_INDEX = EnvVarLoader.get_str("ES_INDEX", "swe_model_outputs")
# SWE 定时任务恢复预热配置
# ============================================================

# SWE API 根地址。生产/调试环境建议通过环境变量配置为网关地址，不写死到代码。
SWE_API_BASE_URL = EnvVarLoader.get_str(
    "MONITOR_SWE_API_BASE_URL",
    "http://proxy-gateway.passuat.cmbchina.cn/gateway/swe/api",
).rstrip("/")

# 选择 /cron/jobs 是因为它会触发 SWE 侧 agent runtime 和 CronManager 加载。
SWE_WARMUP_ENDPOINT = EnvVarLoader.get_str(
    "MONITOR_SWE_WARMUP_ENDPOINT",
    "/cron/jobs",
)

# 固定请求头，例如 Authorization。动态身份头会在每个用户请求中覆盖补齐。
SWE_WARMUP_HEADERS_JSON = EnvVarLoader.get_str(
    "MONITOR_SWE_WARMUP_HEADERS_JSON",
    "",
)

# SWE 定时任务手动重跑配置。认证值必须由部署环境注入，避免密钥进入源码。
_SWE_CRON_RERUN_DEFAULT_BASE_URL = (
    "http://ai-assist-gateway.paas.cmbchina.cn/gateway/swe/api/cron/jobs"
    if ENV_NAME == "prd"
    else "http://ai-assist-gateway.paasuat.cmbchina.cn/gateway/swe/api/cron/jobs"
)
SWE_CRON_RERUN_BASE_URL = EnvVarLoader.get_str(
    "MONITOR_SWE_CRON_RERUN_BASE_URL",
    _SWE_CRON_RERUN_DEFAULT_BASE_URL,
).rstrip("/")
SWE_CRON_RERUN_AUTHORIZATION = EnvVarLoader.get_str(
    "MONITOR_SWE_CRON_RERUN_AUTHORIZATION",
    "",
)

# ============================================================
# 高频问题分析工作流配置
# ============================================================

HFQ_WORKFLOW_URL = EnvVarLoader.get_str(
    "MONITOR_HFQ_WORKFLOW_URL",
    "",
).strip()

HFQ_WORKFLOW_API_KEY = EnvVarLoader.get_str(
    "MONITOR_HFQ_WORKFLOW_API_KEY",
    "",
).strip()

HFQ_WORKFLOW_OPEN_ID = EnvVarLoader.get_str(
    "MONITOR_HFQ_WORKFLOW_OPEN_ID",
    "",
).strip()

HFQ_WORKFLOW_RESPONSE_MODE = EnvVarLoader.get_str(
    "MONITOR_HFQ_WORKFLOW_RESPONSE_MODE",
    "noStreaming",
).strip()

HFQ_WORKFLOW_TIMEOUT_SECONDS = EnvVarLoader.get_float(
    "MONITOR_HFQ_WORKFLOW_TIMEOUT_SECONDS",
    600.0,
    min_value=1.0,
)

HFQ_RESULT_WAIT_SECONDS = EnvVarLoader.get_float(
    "MONITOR_HFQ_RESULT_WAIT_SECONDS",
    420.0,
    min_value=0.0,
)

HFQ_RESULT_POLL_INTERVAL_SECONDS = EnvVarLoader.get_float(
    "MONITOR_HFQ_RESULT_POLL_INTERVAL_SECONDS",
    10.0,
    min_value=1.0,
)

# 定时任务定义表名。生产库使用 swe_cron_jobs，允许环境变量覆盖 schema 前缀。
SWE_WARMUP_CRON_TABLE = EnvVarLoader.get_str(
    "MONITOR_SWE_WARMUP_CRON_TABLE",
    "swe_cron_jobs",
)

# 用户量级约百级，限并发可以降低 SWE 刚启动时的瞬时压力。
SWE_WARMUP_CONCURRENCY = EnvVarLoader.get_int(
    "MONITOR_SWE_WARMUP_CONCURRENCY",
    5,
    min_value=1,
)

# SWE 和 Monitor 可能同时启动，短重试用于覆盖网关或 SWE 尚未就绪的窗口。
SWE_WARMUP_RETRIES = EnvVarLoader.get_int(
    "MONITOR_SWE_WARMUP_RETRIES",
    3,
    min_value=0,
)

# 重试间隔保持较短，目标是尽快恢复定时任务而不是长时间阻塞后台任务。
SWE_WARMUP_RETRY_DELAY_SECONDS = EnvVarLoader.get_float(
    "MONITOR_SWE_WARMUP_RETRY_DELAY_SECONDS",
    2.0,
    min_value=0.0,
)

# 单个用户预热只需要触发加载，不消费响应体内容，因此超时不宜设置过长。
SWE_WARMUP_TIMEOUT_SECONDS = EnvVarLoader.get_float(
    "MONITOR_SWE_WARMUP_TIMEOUT_SECONDS",
    10.0,
    min_value=1.0,
)

# ============================================================
# 用户信息 API 配置
# ============================================================

USER_INFO_API_URL = EnvVarLoader.get_str("MONITOR_USER_INFO_API_URL", "")
CUSTOMER_NAME_QUERY_URL = EnvVarLoader.get_str(
    "MONITOR_CUSTOMER_NAME_QUERY_URL",
    "",
)

# ============================================================
# 客户姓名提取 API 配置
# ============================================================

EXTRACT_CUSTOMER_NAMES_URL = EnvVarLoader.get_str(
    "MONITOR_EXTRACT_CUSTOMER_NAMES_URL",
    "",
)

# ============================================================
# 异步任务状态查询 API 配置
# ============================================================

ASYNC_TASK_QUERY_URL = EnvVarLoader.get_str("MONITOR_ASYNC_TASK_QUERY_URL", "")
ASYNC_TASK_APP_KEY = EnvVarLoader.get_str("MONITOR_ASYNC_TASK_APP_KEY", "")
ASYNC_TASK_ENV_TAG = EnvVarLoader.get_str("MONITOR_ASYNC_TASK_ENV_TAG", "")
ASYNC_TASK_API_KEY = EnvVarLoader.get_str("MONITOR_ASYNC_TASK_API_KEY", "")

RESULT_INDEX_PUSH_URL = EnvVarLoader.get_str(
    "MONITOR_RESULT_INDEX_PUSH_URL",
    "",
).strip()
RESULT_INDEX_PUSH_PLUGIN_ID = EnvVarLoader.get_str(
    "MONITOR_RESULT_INDEX_PUSH_PLUGIN_ID",
    "",
).strip()
RESULT_INDEX_PUSH_PLUGIN_NAME = EnvVarLoader.get_str(
    "MONITOR_RESULT_INDEX_PUSH_PLUGIN_NAME",
    "",
).strip()
RESULT_INDEX_PUSH_QUESTION = EnvVarLoader.get_str(
    "MONITOR_RESULT_INDEX_PUSH_QUESTION",
    "",
).strip()

# 超时时间阈值（小时），超过该时间不再查询状态，直接标记为 timeout
ASYNC_TASK_TIMEOUT_HOUR = EnvVarLoader.get_int(
    "MONITOR_ASYNC_TASK_TIMEOUT_HOUR",
    18,
    min_value=0,
    max_value=23,
)
