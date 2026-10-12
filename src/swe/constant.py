# -*- coding: utf-8 -*-
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from dotenv import load_dotenv

# Project root directory
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Load base .env file (contains SWE_ENV and local overrides)
_base_env_path = _PROJECT_ROOT / ".env"
if _base_env_path.exists():
    load_dotenv(_base_env_path, override=False)


_ENV_VAR_OVERRIDES: ContextVar[dict[str, str]] = ContextVar(
    "swe_env_var_overrides",
    default=None,  # type: ignore[arg-type]
)


def _get_overrides() -> dict[str, str]:
    """Get current env var overrides, returning empty dict if not set."""
    val = _ENV_VAR_OVERRIDES.get()
    return val if val is not None else {}


@contextmanager
def env_var_overrides(overrides: dict[str, str]):
    current = _get_overrides().copy()
    current.update(overrides)
    token = _ENV_VAR_OVERRIDES.set(current)
    try:
        yield
    finally:
        _ENV_VAR_OVERRIDES.reset(token)


class EnvVarLoader:
    """Utility to load and parse environment variables with type safety
    and defaults.
    """

    @staticmethod
    def get_bool(env_var: str, default: bool = False) -> bool:
        """Get a boolean environment variable,
        interpreting common truthy values."""
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
        """Get a float environment variable with optional bounds
        and infinity handling."""
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
        """Get a string environment variable with a default fallback."""
        overrides = _get_overrides()
        return overrides.get(env_var, os.environ.get(env_var, default))


WORKING_DIR = (
    Path(EnvVarLoader.get_str("SWE_WORKING_DIR", "~/.swe"))
    .expanduser()
    .resolve()
)
SECRET_DIR = (
    Path(
        EnvVarLoader.get_str(
            "SWE_SECRET_DIR",
            f"{WORKING_DIR}.secret",
        ),
    )
    .expanduser()
    .resolve()
)

# Default media directory for channels (cross-platform)
DEFAULT_MEDIA_DIR = WORKING_DIR / "media"

# Default local provider directory
DEFAULT_LOCAL_PROVIDER_DIR = WORKING_DIR / "local_models"

JOBS_FILE = EnvVarLoader.get_str("SWE_JOBS_FILE", "jobs.json")

CHATS_FILE = EnvVarLoader.get_str("SWE_CHATS_FILE", "chats.json")

# Builtin multi-agent profile: SWE Q&A helper.
BUILTIN_QA_AGENT_ID = "SWE_QA_Agent_0.1beta1"
BUILTIN_QA_AGENT_NAME = "QA Agent"
# Default skills when the builtin QA workspace is first created only.
BUILTIN_QA_AGENT_SKILL_NAMES: tuple[str, ...] = (
    "guidance",
    "swe_source_index",
)

TOKEN_USAGE_FILE = EnvVarLoader.get_str(
    "SWE_TOKEN_USAGE_FILE",
    "token_usage.json",
)

# Tracing configuration
TRACING_ENABLED = EnvVarLoader.get_bool("SWE_TRACING_ENABLED", False)
TRACING_BATCH_SIZE = EnvVarLoader.get_int(
    "SWE_TRACING_BATCH_SIZE",
    100,
    min_value=1,
)
TRACING_FLUSH_INTERVAL = EnvVarLoader.get_int(
    "SWE_TRACING_FLUSH_INTERVAL",
    5,
    min_value=1,
)
TRACING_RETENTION_DAYS = EnvVarLoader.get_int(
    "SWE_TRACING_RETENTION_DAYS",
    30,
    min_value=0,
)
TRACING_SANITIZE_OUTPUT = EnvVarLoader.get_bool(
    "SWE_TRACING_SANITIZE_OUTPUT",
    True,
)
TRACING_MAX_OUTPUT_LENGTH = EnvVarLoader.get_int(
    "SWE_TRACING_MAX_OUTPUT_LENGTH",
    500,
    min_value=100,
)

CONFIG_FILE = EnvVarLoader.get_str("SWE_CONFIG_FILE", "config.json")

HEARTBEAT_FILE = EnvVarLoader.get_str("SWE_HEARTBEAT_FILE", "HEARTBEAT.md")
HEARTBEAT_DEFAULT_EVERY = "6h"
HEARTBEAT_DEFAULT_TARGET = "main"
HEARTBEAT_TARGET_LAST = "last"

# Debug history file for /dump_history and /load_history commands
DEBUG_HISTORY_FILE = EnvVarLoader.get_str(
    "SWE_DEBUG_HISTORY_FILE",
    "debug_history.jsonl",
)
MAX_LOAD_HISTORY_COUNT = 10000

# Env key for app log level (used by CLI and app load for reload child).
LOG_LEVEL_ENV = "SWE_LOG_LEVEL"

# Env to indicate running inside a container (e.g. Docker). Set to 1/true/yes.
RUNNING_IN_CONTAINER = EnvVarLoader.get_bool(
    "SWE_RUNNING_IN_CONTAINER",
    False,
)

# Timeout in seconds for checking if a provider is reachable.
MODEL_PROVIDER_CHECK_TIMEOUT = EnvVarLoader.get_float(
    "SWE_MODEL_PROVIDER_CHECK_TIMEOUT",
    5.0,
    min_value=0,
    allow_inf=False,
)

# Playwright: use system Chromium when set (e.g. in Docker).
PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH_ENV = "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"

# When True, expose /docs, /redoc, /openapi.json
# (dev only; keep False in prod).
DOCS_ENABLED = EnvVarLoader.get_bool("SWE_OPENAPI_DOCS", False)

# Memory directory
MEMORY_DIR = WORKING_DIR / "memory"

# Custom channel modules (installed via `swe channels install`); manager
# loads BaseChannel subclasses from here.
CUSTOM_CHANNELS_DIR = WORKING_DIR / "custom_channels"

# Local models directory
MODELS_DIR = WORKING_DIR / "models"

MEMORY_COMPACT_KEEP_RECENT = EnvVarLoader.get_int(
    "SWE_MEMORY_COMPACT_KEEP_RECENT",
    3,
    min_value=0,
)

# Memory compaction configuration
MEMORY_COMPACT_RATIO = EnvVarLoader.get_float(
    "SWE_MEMORY_COMPACT_RATIO",
    0.7,
    min_value=0,
    allow_inf=False,
)

DASHSCOPE_BASE_URL = EnvVarLoader.get_str(
    "DASHSCOPE_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
)

# CORS configuration — comma-separated list of allowed origins for dev mode.
# Example: SWE_CORS_ORIGINS="http://localhost:5173,http://127.0.0.1:5173"
# When unset, CORS middleware is not applied.
CORS_ORIGINS = EnvVarLoader.get_str("SWE_CORS_ORIGINS", "*").strip()

# LLM API retry configuration
LLM_MAX_RETRIES = EnvVarLoader.get_int(
    "SWE_LLM_MAX_RETRIES",
    3,
    min_value=0,
)

LLM_BACKOFF_BASE = EnvVarLoader.get_float(
    "SWE_LLM_BACKOFF_BASE",
    1.0,
    min_value=0.1,
)

LLM_BACKOFF_CAP = EnvVarLoader.get_float(
    "SWE_LLM_BACKOFF_CAP",
    10.0,
    min_value=0.5,
)

# LLM concurrency control
# Default maximum number of concurrent in-flight LLM calls per workload inside
# each tenant-local agent scope. Chat and cron can be split with agent config
# overrides; when unset, each workload uses this value independently. Tune to
# your API quota: start conservatively at 3-5 and increase carefully.
LLM_MAX_CONCURRENT = EnvVarLoader.get_int(
    "SWE_LLM_MAX_CONCURRENT",
    10,
    min_value=1,
)
DEFAULT_LLM_CHAT_MAX_CONCURRENT = 2
DEFAULT_LLM_CRON_MAX_CONCURRENT = 3

# Maximum queries per minute (QPM) per tenant-local agent scope, enforced via
# a 60-second sliding window. New requests that would exceed this limit wait
# before being dispatched to the API, proactively preventing 429s. This quota
# remains shared across chat and cron workloads.
# 0 = unlimited (disabled).
# Examples: Anthropic Tier-1 ≈ 50 QPM; OpenAI Tier-1 ≈ 500 QPM.
LLM_MAX_QPM = EnvVarLoader.get_int(
    "SWE_LLM_MAX_QPM",
    600,
    min_value=0,
)

# Default pause duration (seconds) applied to the tenant-local agent scope that
# receives a 429. The cooldown is shared across workloads and overridden by the
# API's Retry-After header when present.
LLM_RATE_LIMIT_PAUSE = EnvVarLoader.get_float(
    "SWE_LLM_RATE_LIMIT_PAUSE",
    5.0,
    min_value=1.0,
)

# Random jitter range (seconds) added on top of the scoped pause remaining
# time so concurrent waiters in that scope stagger their wake-up.
LLM_RATE_LIMIT_JITTER = EnvVarLoader.get_float(
    "SWE_LLM_RATE_LIMIT_JITTER",
    1.0,
    min_value=0.0,
)

# Default maximum time (seconds) a caller waits for its workload-specific
# semaphore slot before giving up with a RuntimeError rather than blocking
# indefinitely.
LLM_ACQUIRE_TIMEOUT = EnvVarLoader.get_float(
    "SWE_LLM_ACQUIRE_TIMEOUT",
    300.0,
    min_value=10.0,
)

# MCP runtime call timeout (seconds).
# Applied to call_tool() and list_tools() to prevent indefinite hangs
# when an MCP server process stalls.
MCP_CALL_TIMEOUT = EnvVarLoader.get_float(
    "SWE_MCP_CALL_TIMEOUT",
    120.0,
    min_value=10.0,
)

# Per-notification timeout for MCP call_tool (seconds).
# Each progress notification from the server resets this countdown.
# If no notification arrives within this window, the call is cancelled.
MCP_PER_NOTIFICATION_TIMEOUT = EnvVarLoader.get_float(
    "SWE_MCP_PER_NOTIFICATION_TIMEOUT",
    120.0,
    min_value=10.0,
)

# Maximum total timeout for MCP call_tool (seconds).
# A hard ceiling that prevents notification storms from extending the
# timeout indefinitely.  0 means no hard limit (fully backward compatible).
MCP_MAX_TOTAL_TIMEOUT = EnvVarLoader.get_float(
    "SWE_MCP_MAX_TOTAL_TIMEOUT",
    0.0,
    min_value=0.0,
)

# Query global timeout (seconds).
# Maximum wall-clock time for a single user request.  When exceeded the
# runner yields a timeout notification and terminates the query.
QUERY_TIMEOUT_SECONDS = EnvVarLoader.get_float(
    "SWE_QUERY_TIMEOUT_SECONDS",
    3600.0,  # 60 minutes
    min_value=60.0,
)

# LLM single-call timeout (seconds).
# Upper bound for a *single* LLM API call (including retries within the
# SDK).  Does not include back-off wait between retry attempts.
LLM_CALL_TIMEOUT = EnvVarLoader.get_float(
    "SWE_LLM_CALL_TIMEOUT",
    600.0,  # 10 minutes
    min_value=30.0,
)

# LLM streaming stall timeout (seconds).
# If no chunk arrives from a streaming LLM response within this window,
# the call is considered stalled and aborted.
LLM_STREAM_STALL_TIMEOUT = EnvVarLoader.get_float(
    "SWE_LLM_STREAM_STALL_TIMEOUT",
    120.0,  # 2 minutes
    min_value=10.0,
)

# Agent watchdog timeout (seconds).
# If the agent produces no output for this duration, it is considered
# stuck and will be automatically interrupted.
AGENT_WATCHDOG_TIMEOUT = EnvVarLoader.get_float(
    "SWE_AGENT_WATCHDOG_TIMEOUT",
    300.0,  # 5 minutes
    min_value=30.0,
)

# Query cleanup timeout (seconds).
# Applied to each operation in the query_handler finally block to prevent
# cleanup from blocking indefinitely (e.g. when the database is down).
QUERY_CLEANUP_TIMEOUT = EnvVarLoader.get_float(
    "SWE_QUERY_CLEANUP_TIMEOUT",
    30.0,
    min_value=5.0,
)

# 查询级自动重试配置
# 当 Agent 执行过程中抛出瞬时报错（网络超时、432 Token 限制等）时，
# 在 query_handler 层自动重试。默认禁用（max_retries=0）。
QUERY_RETRY_MAX_RETRIES = EnvVarLoader.get_int(
    "SWE_QUERY_RETRY_MAX_RETRIES",
    0,
    min_value=0,
)
QUERY_RETRY_BACKOFF_BASE = EnvVarLoader.get_float(
    "SWE_QUERY_RETRY_BACKOFF_BASE",
    2.0,
    min_value=0.5,
)
QUERY_RETRY_BACKOFF_CAP = EnvVarLoader.get_float(
    "SWE_QUERY_RETRY_BACKOFF_CAP",
    30.0,
    min_value=1.0,
)

# Agent interrupt timeout (seconds).
# Maximum time to wait for the agent to finish after being interrupted.
AGENT_INTERRUPT_TIMEOUT = EnvVarLoader.get_float(
    "SWE_AGENT_INTERRUPT_TIMEOUT",
    60.0,
    min_value=5.0,
)

# Channel consume timeout (seconds).
# Maximum time allowed to process a single batch of channel messages.
# Should be slightly larger than QUERY_TIMEOUT_SECONDS.
CHANNEL_CONSUME_TIMEOUT = EnvVarLoader.get_float(
    "SWE_CHANNEL_CONSUME_TIMEOUT",
    1900.0,
    min_value=60.0,
)

# Tool guard approval timeout (seconds).
try:
    TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS = max(
        float(
            os.environ.get("SWE_TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS", "7200"),
        ),
        1.0,
    )
except (TypeError, ValueError):
    TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS = 7200.0

# Marker prepended to every truncation notice.
# Format:
#   <<<TRUNCATED>>>
#   The output above was truncated.
#   The full content is saved to the file and contains Z lines in total.
#   This excerpt starts at line X and covers the next N bytes.
#   If the current content is not enough, call `read_file` with
#   file_path=<path> start_line=Y to read more.
#
# Split output on this marker to recover the original (untruncated) portion:
#   original = output.split(TRUNCATION_NOTICE_MARKER)[0]
TRUNCATION_NOTICE_MARKER = "<<<TRUNCATED>>>"

# Redis coordination for cron leadership (multi-instance support)
CRON_COORDINATION_ENABLED = EnvVarLoader.get_bool(
    "SWE_CRON_COORDINATION_ENABLED",
    False,
)

CRON_CLUSTER_MODE = EnvVarLoader.get_bool(
    "SWE_CRON_CLUSTER_MODE",
    False,
)

CRON_REDIS_URL = EnvVarLoader.get_str(
    "SWE_CRON_REDIS_URL",
    "redis://localhost:6379/0",
)

CRON_REDIS_ACCESS = EnvVarLoader.get_str(
    "SWE_CRON_REDIS_ACCESS",
    "",
)

CRON_CLUSTER_NODES = EnvVarLoader.get_str(
    "SWE_CRON_CLUSTER_NODES",
    "",
)

CRON_LEASE_TTL_SECONDS = EnvVarLoader.get_int(
    "SWE_CRON_LEASE_TTL_SECONDS",
    30,
    min_value=5,
)

CRON_LEASE_RENEW_INTERVAL_SECONDS = EnvVarLoader.get_int(
    "SWE_CRON_LEASE_RENEW_INTERVAL_SECONDS",
    10,
    min_value=1,
)

CRON_LEASE_RENEW_FAILURE_THRESHOLD = EnvVarLoader.get_int(
    "SWE_CRON_LEASE_RENEW_FAILURE_THRESHOLD",
    3,
    min_value=1,
)

CRON_LOCK_SAFETY_MARGIN_SECONDS = EnvVarLoader.get_int(
    "SWE_CRON_LOCK_SAFETY_MARGIN_SECONDS",
    30,
    min_value=5,
)

# WPlus integration configuration
CRON_WPLUS_CLIENT_ID = EnvVarLoader.get_str(
    "SWE_CRON_WPLUS_CLIENT_ID",
    "",
)

CRON_WPLUS_TOKEN_SECRET = EnvVarLoader.get_str(
    "SWE_CRON_WPLUS_TOKEN_SECRET",
    "",
)

CRON_WPLUS_TOKEN_URL = EnvVarLoader.get_str(
    "SWE_CRON_WPLUS_TOKEN_URL",
    "",
)

CRON_WPLUS_INFO_SECRTET = EnvVarLoader.get_str(
    "SWE_CRON_WPLUS_INFO_SECRTET",
    "",
)

CRON_WPLUS_INFO_URL = EnvVarLoader.get_str(
    "SWE_CRON_WPLUS_INFO_URL",
    "",
)

CRON_WPLUS_HOLDER_ID = EnvVarLoader.get_str(
    "SWE_CRON_WPLUS_HOLDER_ID",
    "",
)

CRON_WPLUS_CHANNEL = EnvVarLoader.get_str(
    "SWE_CRON_WPLUS_CHANNEL",
    "",
)

CRON_WPLUS_PRIVATE_KEY = EnvVarLoader.get_str(
    "SWE_CRON_WPLUS_PRIVATE_KEY",
    "",
)

# W+ message push configuration
CRON_WPLUS_MSG_NOTICE_ID = EnvVarLoader.get_str(
    "SWE_CRON_WPLUS_MSG_NOTICE_ID",
    "",
)
CRON_WPLUS_MSG_APP_ID = EnvVarLoader.get_str(
    "SWE_CRON_WPLUS_MSG_APP_ID",
    "",
)
CRON_WPLUS_MSG_URL = EnvVarLoader.get_str(
    "SWE_CRON_WPLUS_MSG_URL",
    "",
)
CRON_WPLUS_PC_MENU_DOMAIN = EnvVarLoader.get_str(
    "SWE_CRON_WPLUS_PC_MENU_DOMAIN",
    "",
)
CRON_WPLUS_MSG_HEADER_KEY = EnvVarLoader.get_str(
    "SWE_CRON_WPLUS_MSG_HEADER_KEY",
    "",
)
CRON_WPLUS_MSG_HEADER_VALUE = EnvVarLoader.get_str(
    "SWE_CRON_WPLUS_MSG_HEADER_VALUE",
    "",
)

# User Info API URL (区分环境的用户信息查询接口地址)
USER_INFO_API_URL = EnvVarLoader.get_str(
    "SWE_USER_INFO_API_URL",
    "",
)

# 机构岗位人员查询地址，由 dev/prd 环境配置提供。
USER_POSITION_API_URL = EnvVarLoader.get_str(
    "SWE_USER_POSITION_API_URL",
    "",
)
USER_POSITION_HEADER_KEY = EnvVarLoader.get_str(
    "SWE_USER_POSITION_HEADER_KEY",
    "",
)
USER_POSITION_HEADER_VALUE = EnvVarLoader.get_str(
    "SWE_USER_POSITION_HEADER_VALUE",
    "",
)
BRANCH_TREE_API_URL = EnvVarLoader.get_str(
    "SWE_BRANCH_TREE_API_URL",
    "",
)

# market 服务内部端点配置。
# bootstrap 完成后回调 market 同步 swe_skills 表，仅限集群内部访问；
# token 留空表示 market 侧不校验（开发模式）。
MARKET_INTERNAL_URL = EnvVarLoader.get_str(
    "SWE_MARKET_INTERNAL_URL",
    "http://market:8090",
)
MARKET_INTERNAL_TOKEN = EnvVarLoader.get_str(
    "SWE_MARKET_INTERNAL_TOKEN",
    "",
)
