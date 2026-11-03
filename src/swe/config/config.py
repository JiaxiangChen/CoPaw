# -*- coding: utf-8 -*-
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, Dict, List, Literal
from enum import Enum

from pydantic import BaseModel, Field, ConfigDict, model_validator
import shortuuid

from .timezone import detect_system_timezone
from ..constant import (
    DEFAULT_LLM_CHAT_MAX_CONCURRENT,
    DEFAULT_LLM_CRON_MAX_CONCURRENT,
    EnvVarLoader,
    HEARTBEAT_DEFAULT_EVERY,
    HEARTBEAT_DEFAULT_TARGET,
    LLM_ACQUIRE_TIMEOUT,
    LLM_BACKOFF_BASE,
    LLM_BACKOFF_CAP,
    LLM_MAX_CONCURRENT,
    LLM_MAX_RETRIES,
    LLM_MAX_QPM,
    LLM_RATE_LIMIT_JITTER,
    LLM_RATE_LIMIT_PAUSE,
    WORKING_DIR,
    TRACING_ENABLED,
    TRACING_BATCH_SIZE,
    TRACING_FLUSH_INTERVAL,
    TRACING_RETENTION_DAYS,
    TRACING_SANITIZE_OUTPUT,
    TRACING_MAX_OUTPUT_LENGTH,
    CRON_COORDINATION_ENABLED,
    CRON_CLUSTER_MODE,
    CRON_REDIS_URL,
    CRON_CLUSTER_NODES,
    CRON_LEASE_TTL_SECONDS,
    CRON_LEASE_RENEW_INTERVAL_SECONDS,
    CRON_LEASE_RENEW_FAILURE_THRESHOLD,
    CRON_LOCK_SAFETY_MARGIN_SECONDS,
)
from ..providers.models import ModelSlotConfig
from ..tracing.config import TracingConfig
from ..agents.hook_runtime.models import HookConfig

LEGACY_DEFAULT_SYSTEM_PROMPT_FILES = (
    "AGENTS.md",
    "SOUL.md",
    "PROFILE.md",
)
DEFAULT_SYSTEM_PROMPT_FILES = (
    *LEGACY_DEFAULT_SYSTEM_PROMPT_FILES,
    "MEMORY.md",
)


def generate_short_agent_id() -> str:
    """Generate a 6-character short UUID for agent identification.

    Returns:
        6-character short UUID string
    """
    return shortuuid.ShortUUID().random(length=6)


def get_default_system_prompt_files() -> list[str]:
    """Return the default workspace system prompt files."""
    return list(DEFAULT_SYSTEM_PROMPT_FILES)


def normalize_system_prompt_files(
    files: list[str] | tuple[str, ...] | None,
) -> list[str]:
    """Upgrade only the historical default prompt-file selection.

    Existing custom selections are preserved verbatim. Historical defaults are
    normalized to the current ordered default that includes MEMORY.md.
    """
    if files is None:
        return get_default_system_prompt_files()

    normalized_files = list(files)
    if len(normalized_files) == len(
        LEGACY_DEFAULT_SYSTEM_PROMPT_FILES,
    ) and set(normalized_files) == set(LEGACY_DEFAULT_SYSTEM_PROMPT_FILES):
        return get_default_system_prompt_files()
    return normalized_files


class BaseChannelConfig(BaseModel):
    """Base for channel config (read from config.json, no env)."""

    enabled: bool = False
    bot_prefix: str = ""
    filter_tool_messages: bool = False
    filter_thinking: bool = False
    dm_policy: Literal["open", "allowlist"] = "open"
    group_policy: Literal["open", "allowlist"] = "open"
    allow_from: List[str] = Field(default_factory=list)
    deny_message: str = ""
    require_mention: bool = False


class ZhaohuConfig(BaseChannelConfig):
    enabled: bool = True
    push_url: str = Field(
        default_factory=lambda: EnvVarLoader.get_str(
            "SWE_ZHAOHU_PUSH_URL",
            "",
        ),
    )
    sys_id: str = "RMS"
    filter_thinking: bool = True
    robot_open_id: str = Field(
        default_factory=lambda: EnvVarLoader.get_str(
            "SWE_ZHAOHU_ROBOT_OPEN_ID",
            "",
        ),
    )
    channel: str = "ZH"
    net: str = "DMZ"
    user_query_url: str = Field(
        default_factory=lambda: EnvVarLoader.get_str(
            "SWE_ZHAOHU_USER_QUERY_URL",
            "",
        ),
    )
    extract_url: str = Field(
        default_factory=lambda: EnvVarLoader.get_str(
            "SWE_ZHAOHU_EXTRACT_URL",
            "",
        ),
    )
    # Cron 任务完成通知 W+ 跳转配置
    cron_task_menu_id: str = Field(
        default_factory=lambda: EnvVarLoader.get_str(
            "SWE_ZHAOHU_CRON_TASK_MENU_ID",
            "",
        ),
    )
    cron_task_error_page: str = Field(
        default_factory=lambda: EnvVarLoader.get_str(
            "SWE_ZHAOHU_CRON_TASK_ERROR_PAGE",
            "",
        ),
    )
    cron_task_sys_id: str = Field(
        default_factory=lambda: EnvVarLoader.get_str(
            "SWE_ZHAOHU_CRON_TASK_SYS_ID",
            "",
        ),
    )
    oauth_url: str = Field(
        default_factory=lambda: EnvVarLoader.get_str(
            "SWE_ZHAOHU_OAUTH_URL",
            "",
        ),
    )
    client_id: str = Field(
        default_factory=lambda: EnvVarLoader.get_str(
            "SWE_ZHAOHU_CLIENT_ID",
            "",
        ),
    )
    client_secret: str = Field(
        default_factory=lambda: EnvVarLoader.get_str(
            "SWE_ZHAOHU_CLIENT_SECRET_POSEIDON",
            "",
        ),
    )
    custom_card_url: str = Field(
        default_factory=lambda: EnvVarLoader.get_str(
            "SWE_ZHAOHU_CUSTOM_CARD_URL",
            "",
        ),
    )
    # Intent recognition API configuration
    intent_url: str = Field(
        default_factory=lambda: EnvVarLoader.get_str(
            "SWE_ZHAOHU_INTENT_URL",
            "",
        ),
    )
    intent_open_id: str = Field(
        default_factory=lambda: EnvVarLoader.get_str(
            "SWE_ZHAOHU_INTENT_OPEN_ID",
            "",
        ),
    )
    intent_api_key: str = Field(
        default_factory=lambda: EnvVarLoader.get_str(
            "SWE_ZHAOHU_INTENT_API_KEY",
            "",
        ),
    )
    session_end_push_enabled: bool = False
    # 会话结束推送跳转链接前缀（使用人在招乎配置页自行配置，配置后推送附带跳转链接）
    session_end_push_link_prefix: str = Field(
        default_factory=lambda: EnvVarLoader.get_str(
            "SWE_ZHAOHU_SESSION_END_PUSH_LINK_PREFIX",
            "",
        ),
    )
    # 跳转链接使用的 ID 类型："chat_id" 或 "session_id"
    session_end_push_link_id_type: str = Field(
        default_factory=lambda: EnvVarLoader.get_str(
            "SWE_ZHAOHU_SESSION_END_PUSH_LINK_ID_TYPE",
            "session_id",
        ),
    )


class ConsoleConfig(BaseChannelConfig):
    """Console channel: prints agent responses to stdout."""

    enabled: bool = True
    media_dir: Optional[str] = None


class ChannelConfig(BaseModel):
    """Built-in channel configs; extra keys allowed for plugin channels."""

    model_config = ConfigDict(extra="allow")

    console: ConsoleConfig = ConsoleConfig()
    zhaohu: ZhaohuConfig = ZhaohuConfig()


CHANNEL_MANAGEMENT_META_KEYS = frozenset({"_constraints", "isBuiltin"})
CONSOLE_CHANNEL_REASON_KEY = "channels.constraints.console_enabled_mandatory"
CONSOLE_CHANNEL_REASON = (
    "Console channel is system-managed and always enabled."
)


def get_channel_management_constraints(
    channel_name: str,
) -> dict[str, dict[str, Any]]:
    """Return sparse field-level constraints for channel management."""
    if channel_name != "console":
        return {}
    return {
        "enabled": {
            "readOnly": True,
            "enforcedValue": True,
            "reasonKey": CONSOLE_CHANNEL_REASON_KEY,
            "reason": CONSOLE_CHANNEL_REASON,
        },
    }


def strip_channel_management_meta(value: Any) -> Any:
    """Remove management-only metadata from incoming channel payloads."""
    if isinstance(value, dict):
        return {
            key: item
            for key, item in value.items()
            if key not in CHANNEL_MANAGEMENT_META_KEYS
        }
    return value


def normalize_single_channel_config(
    channel_name: str,
    value: Any,
    *,
    materialize_missing: bool = False,
):
    """Normalize one channel config according to system invariants."""
    if channel_name != "console":
        return strip_channel_management_meta(value)

    if value is None:
        return ConsoleConfig() if materialize_missing else None

    value = strip_channel_management_meta(value)
    if isinstance(value, dict):
        normalized = dict(value)
        normalized["enabled"] = True
        return normalized

    if isinstance(value, BaseModel):
        copied = value.model_copy(deep=True)
        if hasattr(copied, "enabled"):
            copied.enabled = True
        return copied

    if isinstance(value, SimpleNamespace):
        normalized = SimpleNamespace(**vars(value))
        normalized.enabled = True
        return normalized

    if hasattr(value, "enabled"):
        setattr(value, "enabled", True)
    return value


def normalize_channel_config_set(
    channels: ChannelConfig | None,
    *,
    materialize_missing_console: bool = False,
) -> ChannelConfig | None:
    """Normalize a ChannelConfig object without writing it back to disk."""
    if channels is None:
        if not materialize_missing_console:
            return None
        normalized = ChannelConfig()
        normalized.console.enabled = True
        normalized.zhaohu.enabled = False
        return normalized

    normalized = channels.model_copy(deep=True)
    normalized.console = normalize_single_channel_config(
        "console",
        normalized.console,
        materialize_missing=True,
    )
    return normalized


class LastApiConfig(BaseModel):
    host: Optional[str] = None
    port: Optional[int] = None


class ActiveHoursConfig(BaseModel):
    """Optional active window for heartbeat (e.g. 08:00–22:00)."""

    start: str = "08:00"
    end: str = "22:00"


class HeartbeatConfig(BaseModel):
    """Heartbeat: run agent with HEARTBEAT.md as query at interval."""

    model_config = {"populate_by_name": True}

    enabled: bool = Field(default=False, description="Whether heartbeat is on")
    every: str = Field(default=HEARTBEAT_DEFAULT_EVERY)
    target: str = Field(default=HEARTBEAT_DEFAULT_TARGET)
    active_hours: Optional[ActiveHoursConfig] = Field(
        default=None,
        alias="activeHours",
    )


class AgentsDefaultsConfig(BaseModel):
    heartbeat: Optional[HeartbeatConfig] = None


class EmbeddingConfig(BaseModel):
    """Embedding model configuration."""

    model_config = ConfigDict(extra="ignore")

    backend: str = Field(
        default="openai",
        description="Embedding backend (openai, etc.)",
    )
    api_key: str = Field(
        default="",
        description="API key for embedding provider",
    )
    base_url: str = Field(default="", description="Base URL for embedding API")
    model_name: str = Field(default="", description="Embedding model name")
    dimensions: int = Field(default=1024, description="Embedding dimensions")
    enable_cache: bool = Field(
        default=True,
        description="Whether to enable embedding cache",
    )
    use_dimensions: bool = Field(
        default=False,
        description="Whether to use custom dimensions",
    )
    max_cache_size: int = Field(default=3000, description="Maximum cache size")
    max_input_length: int = Field(
        default=8192,
        description="Maximum input length for embedding",
    )
    max_batch_size: int = Field(
        default=10,
        description="Maximum batch size for embedding",
    )


class ContextCompactConfig(BaseModel):
    """Context compaction and token-counting configuration."""

    model_config = ConfigDict(extra="ignore")

    token_count_model: str = Field(
        default="default",
        description="Model to use for token counting",
    )

    token_count_use_mirror: bool = Field(
        default=False,
        description="Whether to use HuggingFace mirror for token counting",
    )

    token_count_estimate_divisor: float = Field(
        default=4,
        ge=2,
        le=5,
        description=(
            "Divisor for byte-based token estimation (byte_len / divisor)"
        ),
    )

    context_compact_enabled: bool = Field(
        default=True,
        description="Whether to enable automatic context compaction",
    )

    lightweight_governance_ratio: float = Field(
        default=0.65,
        ge=0.30,
        le=0.79,
        description=(
            "Start asynchronously preparing checkpoint-compaction candidates "
            "when context usage reaches this fraction of max_input_length"
        ),
    )

    precompaction_step_ratio: float = Field(
        default=0.05,
        ge=0.01,
        le=0.20,
        description=(
            "Prepare at most one newer checkpoint-compaction candidate for "
            "each additional fraction of max_input_length consumed"
        ),
    )

    memory_compact_ratio: float = Field(
        default=0.80,
        ge=0.31,
        le=0.89,
        description=(
            "Active compaction threshold ratio: install a valid prepared "
            "candidate or compact when context reaches this fraction of "
            "max_input_length"
        ),
    )

    emergency_compact_ratio: float = Field(
        default=0.90,
        ge=0.32,
        le=0.95,
        description=(
            "Emergency degradation threshold ratio: preferentially install a "
            "valid prepared candidate before deterministic fallback compaction"
        ),
    )

    memory_reserve_ratio: float = Field(
        default=0.1,
        ge=0.05,
        le=0.3,
        description=(
            "Context reserve threshold ratio: the most recent fraction of the "
            "context is preserved after compaction to maintain continuity"
        ),
    )

    compact_with_thinking_block: bool = Field(
        default=True,
        description="Whether to include thinking blocks when compacting",
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_context_compact_stages(cls, data: Any) -> Any:
        """Backfill valid stages for persisted pre-checkpoint configs.

        Old configs contain only ``memory_compact_ratio``. Preserve that active
        threshold when possible and place the lightweight stage below it, so an
        existing agent can load before its configuration is next saved.
        """
        if not isinstance(data, dict):
            return data

        stage_fields = (
            "lightweight_governance_ratio",
            "precompaction_step_ratio",
            "emergency_compact_ratio",
        )
        if "memory_compact_ratio" not in data or any(
            field in data for field in stage_fields
        ):
            return data

        try:
            legacy_active_ratio = float(data["memory_compact_ratio"])
        except (TypeError, ValueError):
            return data

        active_ratio = min(max(legacy_active_ratio, 0.31), 0.89)
        lightweight_ratio = max(0.30, min(0.65, active_ratio - 0.05))
        step_ratio = round(
            max(0.01, min(0.05, active_ratio - lightweight_ratio)),
            2,
        )
        emergency_ratio = round(
            max(0.90, min(0.95, active_ratio + 0.05)),
            2,
        )

        return {
            **data,
            "lightweight_governance_ratio": lightweight_ratio,
            "precompaction_step_ratio": step_ratio,
            "memory_compact_ratio": active_ratio,
            "emergency_compact_ratio": emergency_ratio,
        }

    @model_validator(mode="after")
    def validate_context_compact_stages(self) -> "ContextCompactConfig":
        """Require the ordered lightweight, active, and emergency stages."""
        if not (
            self.lightweight_governance_ratio
            < self.memory_compact_ratio
            < self.emergency_compact_ratio
        ):
            raise ValueError(
                "lightweight, active, and emergency ratios must increase",
            )
        return self


class ToolResultCompactConfig(BaseModel):
    """Tool result compaction thresholds and retention configuration."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(
        default=True,
        description="Whether to enable tool result compaction",
    )

    recent_n: int = Field(
        default=2,
        ge=1,
        le=10,
        description="Number of recent messages to use recent_max_bytes for",
    )

    old_max_bytes: int = Field(
        default=3000,
        ge=100,
        description=(
            "Byte threshold for old messages in tool result compaction"
        ),
    )

    recent_max_bytes: int = Field(
        default=50000,
        ge=1000,
        description=(
            "Byte threshold for recent messages in tool result compaction"
        ),
    )

    retention_days: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Number of days to retain tool result files",
    )


class MemorySummaryConfig(BaseModel):
    """Memory summarization and search configuration."""

    model_config = ConfigDict(extra="ignore")

    memory_summary_enabled: bool = Field(
        default=True,
        description="Whether to enable memory summarization during compaction",
    )

    force_memory_search: bool = Field(
        default=False,
        description="Whether to force memory search on every turn",
    )

    force_max_results: int = Field(
        default=1,
        ge=1,
        description=(
            "Maximum number of results to return when force memory"
            " search is enabled"
        ),
    )

    force_min_score: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum relevance score for results when force memory"
            " search is enabled"
        ),
    )

    rebuild_memory_index_on_start: bool = Field(
        default=False,
        description=(
            "Whether to clear and rebuild the memory search index when the"
            " agent starts. Set to False to skip re-indexing and only monitor"
            " new file changes."
        ),
    )

    dream_cron: str = Field(
        default="",
        description=(
            "Cron expression for dream-based memory optimization job "
            "(empty string to disable). Default: disabled (empty string)."
        ),
    )


class SuggestionMode(str, Enum):
    """猜你想问生成模式."""

    DISABLED = "disabled"  # 完全禁用
    BACKEND_GENERATE = "backend_generate"  # 后端生成（原有模式）
    QA_EXTRACTION_ONLY = "qa_extraction_only"  # 仅提取 Q&A（新模式）


class SuggestionConfig(BaseModel):
    """猜你想问功能配置 - 在模型回答后异步生成后续问题建议."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(
        default=True,
        description="是否启用猜你想问功能",
    )
    mode: SuggestionMode = Field(
        default=SuggestionMode.BACKEND_GENERATE,
        description="猜你想问生成模式",
    )
    max_suggestions: int = Field(
        default=3,
        ge=1,
        le=5,
        description="最多生成的问题数量",
    )
    timeout_seconds: float = Field(
        default=5.0,
        ge=1.0,
        le=15.0,
        description="建议生成超时时间（秒）",
    )
    user_message_max_length: int = Field(
        default=200,
        ge=50,
        le=500,
        description="用户提问截断长度（字符）",
    )
    assistant_response_max_length: int = Field(
        default=500,
        ge=200,
        le=2000,
        description="助手回答截断长度（字符）",
    )
    qa_content_total_max_length: int = Field(
        default=1500,
        ge=500,
        le=3000,
        description="Q&A 内容总长度上限（字符）",
    )
    qa_content_max_age_seconds: int = Field(
        default=120,
        ge=30,
        le=300,
        description="Q&A 内容存储有效期（秒）",
    )


class PostTurnValidationConfig(BaseModel):
    """已退役的回答后校验配置，仅为兼容旧配置保留。"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(
        default=False,
        description="已废弃：回答后校验功能已下线，此配置不再生效",
    )
    max_confirmed_turns: Optional[int] = Field(
        default=None,
        ge=0,
        le=5,
        description="单次用户请求最多允许用户确认续跑的轮数",
    )
    max_auto_turns: int = Field(
        default=2,
        ge=0,
        le=5,
        description="每次用户请求或确认后最多允许全自动续跑的轮数",
    )
    timeout_seconds: float = Field(
        default=8.0,
        ge=1.0,
        le=20.0,
        description="后校验模型调用超时时间（秒）",
    )
    user_message_max_length: int = Field(
        default=300,
        ge=50,
        le=1000,
        description="后校验时保留的用户任务长度上限",
    )
    assistant_response_max_length: int = Field(
        default=1200,
        ge=200,
        le=4000,
        description="后校验时保留的助手回答长度上限",
    )


class QueryRetryConfig(BaseModel):
    """Query 级别重试配置 - 当整轮对话因可重试瞬时错误失败时自动重试。"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(
        default=False,
        description="是否启用 Query 级别重试",
    )
    max_retries: int = Field(
        default=3,
        ge=1,
        description="最大重试次数",
    )
    backoff_base: float = Field(
        default=2.0,
        ge=0.5,
        description="指数退避基础延迟（秒）",
    )
    backoff_cap: float = Field(
        default=30.0,
        ge=1.0,
        description="退避延迟上限（秒），须大于等于 backoff_base",
    )

    @model_validator(mode="after")
    def validate_backoff_cap(self) -> "QueryRetryConfig":
        """确保 backoff_cap >= backoff_base。"""
        if self.backoff_cap < self.backoff_base:
            raise ValueError(
                "backoff_cap must be greater than or equal to backoff_base",
            )
        return self


class HookRuntimeRunningConfig(BaseModel):
    """Hook Runtime 自动续跑预算配置。"""

    model_config = ConfigDict(extra="ignore")

    max_stop_turns: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Stop 阻断后允许自动续跑的最大轮数；" "未配置时使用 Runner 默认值"
        ),
    )
    max_automatic_follow_up_turns: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "单次请求中所有自动续跑机制共享的最大轮数；"
            "未配置时按各机制预算推导"
        ),
    )
    max_stop_transform_seconds: float = Field(
        default=30.0,
        gt=0,
        description="Stop 输出变换流水线的总时限（秒）",
    )
    max_stop_append_seconds: float = Field(
        default=30.0,
        gt=0,
        allow_inf_nan=False,
        description="Stop 追加消息阶段的总时限（秒）",
    )

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_before_stop_budget(cls, data: Any) -> Any:
        if isinstance(data, dict) and "max_before_stop_turns" in data:
            raise ValueError(
                "max_before_stop_turns has been removed; use max_stop_turns",
            )
        return data


class AgentsRunningConfig(BaseModel):
    """Agent runtime behavior configuration."""

    model_config = ConfigDict(extra="ignore")

    max_iters: int = Field(
        default=100,
        ge=1,
        description=(
            "Maximum number of reasoning-acting iterations for ReAct agent"
        ),
    )

    max_stop_turns: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Stop 阻断后允许自动续跑的最大轮数；"
            "未配置时由 hook_runtime 配置或默认值决定"
        ),
    )

    max_automatic_follow_up_turns: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "单次请求中所有自动续跑机制共享的最大轮数；"
            "兼容旧版 running 顶层配置"
        ),
    )

    hook_runtime: Optional[HookRuntimeRunningConfig] = Field(
        default=None,
        description=(
            "Hook Runtime 相关运行预算配置；配置后优先于 "
            "running 顶层兼容字段"
        ),
    )

    query_retry: QueryRetryConfig = Field(
        default_factory=QueryRetryConfig,
        description="Query 级别重试配置",
    )

    llm_retry_enabled: bool = Field(
        default=LLM_MAX_RETRIES > 0,
        description="Whether to auto-retry transient LLM API errors",
    )

    llm_max_retries: int = Field(
        default=max(LLM_MAX_RETRIES, 1),
        ge=1,
        description="Maximum retry attempts for transient LLM API errors",
    )

    llm_backoff_base: float = Field(
        default=LLM_BACKOFF_BASE,
        ge=0.1,
        description="Base delay in seconds for exponential LLM retry backoff",
    )

    llm_backoff_cap: float = Field(
        default=LLM_BACKOFF_CAP,
        ge=0.5,
        description=(
            "Maximum delay cap in seconds for LLM retry backoff; "
            "must be greater than or equal to the base delay"
        ),
    )

    llm_max_concurrent: int = Field(
        default=LLM_MAX_CONCURRENT,
        ge=1,
        description=(
            "Fallback maximum number of concurrent in-flight LLM calls for "
            "this tenant-local agent scope. Chat and cron workloads use "
            "separate pools with workload-specific defaults."
        ),
    )

    llm_chat_max_concurrent: Optional[int] = Field(
        default=DEFAULT_LLM_CHAT_MAX_CONCURRENT,
        ge=1,
        description=(
            "Maximum concurrent in-flight LLM calls for chat workload "
            "traffic in this tenant-local agent scope. When unset or null, "
            "the chat default is used."
        ),
    )

    llm_cron_max_concurrent: Optional[int] = Field(
        default=DEFAULT_LLM_CRON_MAX_CONCURRENT,
        ge=1,
        description=(
            "Maximum concurrent in-flight LLM calls for cron and heartbeat "
            "workload traffic in this tenant-local agent scope. When unset "
            "or null, the cron default is used."
        ),
    )

    llm_max_qpm: int = Field(
        default=LLM_MAX_QPM,
        ge=0,
        description=(
            "Maximum queries per minute for this tenant-local agent scope "
            "(60-second sliding window). New requests that would exceed this "
            "limit wait before being dispatched; 0 = disabled. This quota is "
            "shared across chat and cron workloads."
        ),
    )

    llm_rate_limit_pause: float = Field(
        default=LLM_RATE_LIMIT_PAUSE,
        ge=1.0,
        description=(
            "Default pause duration (seconds) applied to this tenant-local "
            "agent scope when a 429 rate-limit response is received. The "
            "cooldown is shared across chat and cron workloads."
        ),
    )

    llm_rate_limit_jitter: float = Field(
        default=LLM_RATE_LIMIT_JITTER,
        ge=0.0,
        description=(
            "Random jitter range (seconds) added on top of the pause so "
            "concurrent waiters in this agent scope stagger their wake-up."
        ),
    )

    llm_acquire_timeout: float = Field(
        default=LLM_ACQUIRE_TIMEOUT,
        ge=10.0,
        description=(
            "Default maximum time (seconds) a caller waits to acquire its "
            "workload-specific rate-limiter slot before giving up with an "
            "error."
        ),
    )

    llm_chat_acquire_timeout: Optional[float] = Field(
        default=None,
        ge=10.0,
        description=(
            "Optional maximum time (seconds) chat workload callers wait for "
            "their LLM concurrency slot. When unset, llm_acquire_timeout is "
            "used."
        ),
    )

    llm_cron_acquire_timeout: Optional[float] = Field(
        default=None,
        ge=10.0,
        description=(
            "Optional maximum time (seconds) cron and heartbeat workload "
            "callers wait for their LLM concurrency slot. When unset, "
            "llm_acquire_timeout is used."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def fill_workload_concurrency_defaults(
        cls,
        data: Any,
    ) -> Any:
        """Treat missing/null workload concurrency fields as defaults."""
        if not isinstance(data, dict):
            return data

        normalized = dict(data)
        if normalized.get("llm_chat_max_concurrent") is None:
            normalized["llm_chat_max_concurrent"] = (
                DEFAULT_LLM_CHAT_MAX_CONCURRENT
            )
        if normalized.get("llm_cron_max_concurrent") is None:
            normalized["llm_cron_max_concurrent"] = (
                DEFAULT_LLM_CRON_MAX_CONCURRENT
            )
        return normalized

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_before_stop_budget(cls, data: Any) -> Any:
        if isinstance(data, dict) and "max_before_stop_turns" in data:
            raise ValueError(
                "max_before_stop_turns has been removed; use max_stop_turns",
            )
        return data

    @model_validator(mode="after")
    def validate_llm_retry_backoff(self) -> "AgentsRunningConfig":
        """Validate LLM retry backoff relationships."""
        if self.llm_backoff_cap < self.llm_backoff_base:
            raise ValueError(
                "llm_backoff_cap must be greater than or equal to "
                "llm_backoff_base",
            )
        return self

    @model_validator(mode="after")
    def validate_llm_acquire_timeout(self) -> "AgentsRunningConfig":
        """Validate LLM acquire timeout relationships."""
        cooldown = self.llm_rate_limit_pause + self.llm_rate_limit_jitter
        for field_name in (
            "llm_acquire_timeout",
            "llm_chat_acquire_timeout",
            "llm_cron_acquire_timeout",
        ):
            value = getattr(self, field_name)
            if value is not None and value <= cooldown:
                raise ValueError(
                    f"{field_name} must be greater than "
                    "llm_rate_limit_pause + llm_rate_limit_jitter",
                )
        return self

    max_input_length: int = Field(
        default=128 * 1024,  # 128K = 131072 tokens
        ge=1000,
        description=(
            "Maximum input length (tokens) for the model context window"
        ),
    )

    history_max_length: int = Field(
        default=10000,
        ge=1000,
        description="Maximum length for /history command output",
    )

    context_compact: ContextCompactConfig = Field(
        default_factory=ContextCompactConfig,
        description="Context compaction configuration",
    )

    tool_result_compact: ToolResultCompactConfig = Field(
        default_factory=ToolResultCompactConfig,
        description="Tool result compaction configuration",
    )

    memory_summary: MemorySummaryConfig = Field(
        default_factory=MemorySummaryConfig,
        description="Memory summarization and search configuration",
    )

    embedding_config: EmbeddingConfig = Field(
        default_factory=EmbeddingConfig,
        description="Embedding model configuration",
    )

    memory_manager_backend: Literal["remelight"] = Field(
        default="remelight",
        description=(
            "Memory manager backend type. "
            "Currently only 'remelight' is supported."
        ),
    )

    tracing: TracingConfig = Field(
        default_factory=lambda: TracingConfig(
            enabled=TRACING_ENABLED,
            batch_size=TRACING_BATCH_SIZE,
            flush_interval=TRACING_FLUSH_INTERVAL,
            retention_days=TRACING_RETENTION_DAYS,
            sanitize_output=TRACING_SANITIZE_OUTPUT,
            max_output_length=TRACING_MAX_OUTPUT_LENGTH,
        ),
        description="Tracing configuration for request tracking and analytics",
    )

    suggestions: SuggestionConfig = Field(
        default_factory=SuggestionConfig,
        description="猜你想问功能配置",
    )

    post_turn_validation: PostTurnValidationConfig = Field(
        default_factory=PostTurnValidationConfig,
        description="已废弃：回答后校验与自动续跑功能已下线",
    )

    @property
    def memory_compact_reserve(self) -> int:
        """Memory compact reserve size (tokens)."""
        return int(
            self.max_input_length * self.context_compact.memory_reserve_ratio,
        )

    @property
    def memory_compact_threshold(self) -> int:
        """Memory compact threshold size (tokens)."""
        return int(
            self.max_input_length * self.context_compact.memory_compact_ratio,
        )


class AgentsLLMRoutingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(default=False)
    mode: Literal["local_first", "cloud_first"] = Field(
        default="local_first",
        description=(
            "local_first routes to the local slot by default; cloud_first "
            "routes to the cloud slot by default. Smarter switching can be "
            "added later without changing the dual-slot config shape."
        ),
    )
    local: ModelSlotConfig = Field(
        default_factory=ModelSlotConfig,
        description="Local model slot (required when routing is enabled).",
    )
    cloud: Optional[ModelSlotConfig] = Field(
        default=None,
        description=(
            "Optional explicit cloud model slot; when null, uses "
            "providers.json active_llm."
        ),
    )


class AgentProfileRef(BaseModel):
    """Agent Profile reference (stored in root config.json).

    Only contains ID and workspace directory reference.
    Full agent configuration is stored in workspace/agent.json.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., description="Unique agent ID")
    workspace_dir: str = Field(
        ...,
        description="Path to agent's workspace directory",
    )
    enabled: bool = Field(
        default=True,
        description="Whether agent is enabled (controls instance loading)",
    )


class AgentProfileConfig(BaseModel):
    """Complete Agent Profile configuration (stored in workspace/agent.json).

    Each agent has its own configuration file with all settings.
    """

    id: str = Field(..., description="Unique agent ID")
    name: str = Field(..., description="Human-readable agent name")
    description: str = Field(default="", description="Agent description")
    workspace_dir: str = Field(
        default="",
        description="Path to agent's workspace (optional, for reference)",
    )

    # Agent-specific configurations
    channels: Optional["ChannelConfig"] = Field(
        default=None,
        description="Channel configurations for this agent",
    )
    mcp: Optional["MCPConfig"] = Field(
        default=None,
        description="MCP clients for this agent",
    )
    heartbeat: Optional[HeartbeatConfig] = Field(
        default=None,
        description="Heartbeat configuration for this agent",
    )
    last_dispatch: Optional["LastDispatchConfig"] = Field(
        default=None,
        description="Last dispatch target for this agent",
    )
    running: AgentsRunningConfig = Field(
        default_factory=AgentsRunningConfig,
        description="Runtime configuration",
    )
    llm_routing: AgentsLLMRoutingConfig = Field(
        default_factory=AgentsLLMRoutingConfig,
        description="LLM routing configuration",
    )
    language: str = Field(
        default="zh",
        description="Language setting for this agent",
    )
    system_prompt_files: List[str] = Field(
        default_factory=get_default_system_prompt_files,
        description="System prompt markdown files",
    )
    tools: Optional["ToolsConfig"] = Field(
        default=None,
        description="Tools configuration for this agent",
    )
    security: Optional["SecurityConfig"] = Field(
        default=None,
        description="Security configuration for this agent",
    )
    hooks: HookConfig = Field(
        default_factory=HookConfig,
        description="Agent-scoped lifecycle hook configuration",
    )

    @model_validator(mode="after")
    def _normalize_system_prompt_files(self):
        self.system_prompt_files = normalize_system_prompt_files(
            self.system_prompt_files,
        )
        return self


class AgentsConfig(BaseModel):
    """Agents configuration (root config.json only contains references)."""

    active_agent: str = Field(
        default="default",
        description="Currently active agent ID",
    )
    profiles: Dict[str, AgentProfileRef] = Field(
        default_factory=lambda: {
            "default": AgentProfileRef(
                id="default",
                workspace_dir=f"{WORKING_DIR}/workspaces/default",
            ),
        },
        description="Agent profile references (ID and workspace path only)",
    )

    # Legacy fields for backward compatibility (deprecated)
    # These fields MUST have default values (not None) to support downgrade
    defaults: Optional[AgentsDefaultsConfig] = None
    running: AgentsRunningConfig = Field(
        default_factory=AgentsRunningConfig,
    )
    llm_routing: AgentsLLMRoutingConfig = Field(
        default_factory=AgentsLLMRoutingConfig,
    )
    language: str = Field(default="zh")
    installed_md_files_language: Optional[str] = None
    system_prompt_files: List[str] = Field(
        default_factory=get_default_system_prompt_files,
    )
    audio_mode: Literal["auto", "native"] = Field(
        default="auto",
        description=(
            "How to handle incoming audio/voice messages. "
            '"auto": transcribe if a provider is available, otherwise show '
            "file-uploaded placeholder; "
            '"native": send audio blocks directly to the model '
            "(may need ffmpeg)."
        ),
    )

    transcription_provider_type: Literal[
        "disabled",
        "whisper_api",
        "local_whisper",
    ] = Field(
        default="disabled",
        description=(
            "Transcription backend. "
            '"disabled": no transcription; '
            '"whisper_api": remote OpenAI-compatible endpoint; '
            '"local_whisper": locally installed openai-whisper.'
        ),
    )
    transcription_provider_id: str = Field(
        default="",
        description=(
            "Provider ID for Whisper API transcription. "
            "Empty = no provider selected. "
            'Only used when transcription_provider_type is "whisper_api".'
        ),
    )
    transcription_model: str = Field(
        default="whisper-1",
        description=(
            "Model name for Whisper API transcription. "
            'e.g. "whisper-1", "whisper-large-v3".'
        ),
    )

    @model_validator(mode="after")
    def _normalize_system_prompt_files(self):
        self.system_prompt_files = normalize_system_prompt_files(
            self.system_prompt_files,
        )
        return self


class LastDispatchConfig(BaseModel):
    """Last channel/user/session that received a user-originated reply."""

    channel: str = ""
    user_id: str = ""
    session_id: str = ""


class MCPClientConfig(BaseModel):
    """Configuration for a single MCP client."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    description: str = ""
    enabled: bool = True
    transport: Literal["stdio", "streamable_http", "sse"] = "stdio"
    url: str = ""
    headers: Dict[str, str] = Field(default_factory=dict)
    command: str = ""
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    # 市场分发相关字段
    source: str = Field(
        default="",
        description="来源标识：空=我创建的；marketplace:{item_id}=市场分发的",
    )
    market_client_key: str = Field(
        default="",
        description="市场来源的 client_key",
    )
    distributed_by: str = Field(
        default="",
        description="分发者 user_id",
    )
    # 懒加载预留字段
    lazy_load: bool = Field(
        default=False,
        description="是否延迟加载 MCP 客户端",
    )
    # 时间戳字段
    created_at: str = Field(
        default="",
        description="创建时间 ISO8601",
    )
    updated_at: str = Field(
        default="",
        description="更新时间 ISO8601",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_fields(cls, data):
        """Normalize common MCP field aliases from third-party examples."""
        if not isinstance(data, dict):
            return data

        payload = dict(data)

        if "isActive" in payload and "enabled" not in payload:
            payload["enabled"] = payload["isActive"]

        if "baseUrl" in payload and "url" not in payload:
            payload["url"] = payload["baseUrl"]

        if "type" in payload and "transport" not in payload:
            payload["transport"] = payload["type"]

        if (
            "transport" not in payload
            and (payload.get("url") or payload.get("baseUrl"))
            and not payload.get("command")
        ):
            payload["transport"] = "streamable_http"

        raw_transport = payload.get("transport")
        if isinstance(raw_transport, str):
            normalized = raw_transport.strip().lower()
            transport_alias_map = {
                "streamablehttp": "streamable_http",
                "http": "streamable_http",
                "stdio": "stdio",
                "sse": "sse",
            }
            payload["transport"] = transport_alias_map.get(
                normalized,
                normalized,
            )

        return payload

    @model_validator(mode="after")
    def _validate_transport_config(self):
        """Validate required fields for each MCP transport type."""
        if self.transport == "stdio":
            if not self.command.strip():
                raise ValueError("stdio MCP client requires non-empty command")
            return self

        if not self.url.strip():
            raise ValueError(
                f"{self.transport} MCP client requires non-empty url",
            )
        return self


class MCPConfig(BaseModel):
    """MCP clients configuration.

    Uses a dict to allow dynamic client definitions.
    Default tavily_search client is created and auto-enabled if API key exists.
    """

    clients: Dict[str, MCPClientConfig] = Field(
        default_factory=lambda: {
            "tavily_search": MCPClientConfig(
                name="tavily_mcp",
                # Auto-enable if TAVILY_API_KEY exists in environment
                enabled=bool(EnvVarLoader.get_str("TAVILY_API_KEY")),
                command="npx",
                args=["-y", "tavily-mcp@latest"],
                env={"TAVILY_API_KEY": EnvVarLoader.get_str("TAVILY_API_KEY")},
            ),
        },
    )


class BuiltinToolConfig(BaseModel):
    """Configuration for a single built-in tool."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., description="Tool function name")
    enabled: bool = Field(
        default=True,
        description="Whether the tool is enabled",
    )
    description: str = Field(default="", description="Tool description")
    display_to_user: bool = Field(
        default=True,
        description="Whether tool output is rendered to user channels",
    )
    async_execution: bool = Field(
        default=False,
        description="Whether to execute the tool asynchronously in background",
    )


def _default_builtin_tools() -> Dict[str, BuiltinToolConfig]:
    """Return a fresh copy of the canonical built-in tool definitions."""
    return {
        "execute_shell_command": BuiltinToolConfig(
            name="execute_shell_command",
            enabled=True,
            description="Execute shell commands",
        ),
        "start_background_process": BuiltinToolConfig(
            name="start_background_process",
            enabled=False,
            description="Start a managed background shell process",
        ),
        "list_background_processes": BuiltinToolConfig(
            name="list_background_processes",
            enabled=False,
            description="List managed background shell processes",
        ),
        "get_process_output": BuiltinToolConfig(
            name="get_process_output",
            enabled=False,
            description="Read managed background process output",
        ),
        "stop_background_process": BuiltinToolConfig(
            name="stop_background_process",
            enabled=False,
            description="Stop a managed background shell process",
        ),
        "read_file": BuiltinToolConfig(
            name="read_file",
            enabled=True,
            description="Read file contents",
        ),
        "write_file": BuiltinToolConfig(
            name="write_file",
            enabled=True,
            description="Write content to file",
        ),
        "edit_file": BuiltinToolConfig(
            name="edit_file",
            enabled=True,
            description="Edit file using find-and-replace",
        ),
        "grep_search": BuiltinToolConfig(
            name="grep_search",
            enabled=True,
            description="Search file contents by pattern",
        ),
        "glob_search": BuiltinToolConfig(
            name="glob_search",
            enabled=True,
            description="Find files matching a glob pattern",
        ),
        "get_current_time": BuiltinToolConfig(
            name="get_current_time",
            enabled=True,
            description="Get current date and time",
        ),
        "copy_file_to_static": BuiltinToolConfig(
            name="copy_file_to_static",
            enabled=True,
            description="copy file to static",
        ),
        "update_task_progress": BuiltinToolConfig(
            name="update_task_progress",
            enabled=True,
            description="update task progress state",
            display_to_user=False,
        ),
        "emit_wplus_sop_event": BuiltinToolConfig(
            name="emit_wplus_sop_event",
            enabled=True,
            description="commit a typed W+ SOP workspace event",
            display_to_user=False,
        ),
    }


RETIRED_BUILTIN_TOOL_NAMES = frozenset(
    {
        "get_token_usage",
        "view_image",
        "view_video",
        "set_user_timezone",
    },
)


class ToolsConfig(BaseModel):
    """Built-in tools management configuration."""

    builtin_tools: Dict[str, BuiltinToolConfig] = Field(
        default_factory=_default_builtin_tools,
    )

    @model_validator(mode="after")
    def _merge_default_tools(self):
        """Ensure new code-defined tools are present in saved configs."""
        for name in RETIRED_BUILTIN_TOOL_NAMES:
            self.builtin_tools.pop(name, None)
        for name, tc in _default_builtin_tools().items():
            if name not in self.builtin_tools:
                self.builtin_tools[name] = tc
        return self


def build_qa_agent_tools_config() -> ToolsConfig:
    """Tools preset for builtin ``default_qa_agent`` (first workspace init).

    Only these are enabled: execute_shell_command, read_file, edit_file,
    write_file. All other built-ins are disabled.
    """
    allow = frozenset(
        {
            "execute_shell_command",
            "read_file",
            "write_file",
            "edit_file",
        },
    )
    builtin_tools = {
        name: tc.model_copy(update={"enabled": name in allow})
        for name, tc in _default_builtin_tools().items()
    }
    return ToolsConfig(builtin_tools=builtin_tools)


class ToolGuardRuleConfig(BaseModel):
    """A single user-defined guard rule (stored in config.json)."""

    id: str
    tools: List[str] = Field(default_factory=list)
    params: List[str] = Field(default_factory=list)
    category: str = "command_injection"
    severity: str = "HIGH"
    patterns: List[str] = Field(default_factory=list)
    exclude_patterns: List[str] = Field(default_factory=list)
    description: str = ""
    remediation: str = ""


class ToolGuardConfig(BaseModel):
    """Tool guard settings under ``security.tool_guard``.

    ``guarded_tools``: ``None`` → use built-in default set; empty list → guard
    nothing; non-empty list → guard only those tools.
    """

    enabled: bool = True
    guarded_tools: Optional[List[str]] = None
    denied_tools: List[str] = Field(default_factory=list)
    custom_rules: List[ToolGuardRuleConfig] = Field(default_factory=list)
    disabled_rules: List[str] = Field(default_factory=list)


class FileGuardConfig(BaseModel):
    """File guard settings under ``security.file_guard``."""

    enabled: bool = True
    sensitive_files: List[str] = Field(default_factory=list)


class SkillScannerWhitelistEntry(BaseModel):
    """A whitelisted skill (identified by name + content hash)."""

    skill_name: str
    content_hash: str = Field(
        default="",
        description="SHA-256 of concatenated file contents at whitelist time. "
        "Empty string means any content is allowed.",
    )
    added_at: str = Field(
        default="",
        description="ISO 8601 timestamp when the entry was added.",
    )


class SkillScannerConfig(BaseModel):
    """Skill scanner settings under ``security.skill_scanner``.

    ``mode`` controls the scanner behavior:
    * ``"block"`` – scan and block unsafe skills.
    * ``"warn"``  – scan but only log warnings, do not block (default).
    * ``"off"``   – disable scanning entirely.
    """

    mode: Literal["block", "warn", "off"] = Field(
        default="warn",
        description="Scanner mode: block, warn, or off.",
    )
    timeout: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Max seconds to wait for a scan to complete.",
    )
    whitelist: List[SkillScannerWhitelistEntry] = Field(
        default_factory=list,
        description="Skills that bypass security scanning.",
    )


class ProcessLimitsConfig(BaseModel):
    """Global process-limit policy for tenant-scoped subprocesses."""

    enabled: bool = True
    shell: bool = True
    mcp_stdio: bool = False
    cpu_time_limit_seconds: int | None = Field(default=90, ge=1)
    memory_max_mb: int | None = Field(default=34000, ge=1)
    shell_max_concurrent: int | None = Field(default=5, ge=1)
    shell_acquire_timeout_seconds: float = Field(default=5, gt=0)

    @model_validator(mode="after")
    def validate_enabled_policy(self) -> "ProcessLimitsConfig":
        """Reject enabled policies that cannot enforce anything."""
        if not self.enabled:
            return self
        if not self.shell and not self.mcp_stdio:
            raise ValueError(
                "enabled process_limits policy must target shell or mcp_stdio",
            )
        has_process_ceiling = (
            self.cpu_time_limit_seconds is not None
            or self.memory_max_mb is not None
        )
        has_shell_concurrency_limit = (
            self.shell and self.shell_max_concurrent is not None
        )
        if not has_process_ceiling and not has_shell_concurrency_limit:
            raise ValueError(
                "enabled process_limits policy requires at least one "
                "applicable limit",
            )
        return self


class SkillHookHttpConfig(BaseModel):
    """Tenant-approved destinations for skill-owned HTTP hooks."""

    approved_urls: List[str] = Field(
        default_factory=list,
        description=(
            "Exact HTTP endpoint URLs that skill-owned hook handlers may call."
        ),
    )


class SecurityConfig(BaseModel):
    """Top-level ``security`` section in config.json."""

    tool_guard: ToolGuardConfig = Field(default_factory=ToolGuardConfig)
    file_guard: FileGuardConfig = Field(default_factory=FileGuardConfig)
    skill_scanner: SkillScannerConfig = Field(
        default_factory=SkillScannerConfig,
    )
    skill_hook_http: SkillHookHttpConfig = Field(
        default_factory=SkillHookHttpConfig,
    )
    process_limits: ProcessLimitsConfig = Field(
        default_factory=ProcessLimitsConfig,
    )


class ServiceHeartbeatConfig(BaseModel):
    """服务心跳配置：向远程接口定期发送心跳信号。

    用于服务注册和健康检查，在服务启动时开启后台心跳任务，
    进程结束前发送关闭信号（enabled=false）。

    需要配置的环境变量：
    - SWE_SERVICE_HEARTBEAT_ENABLED: 是否启用（默认true）
    - SWE_SERVICE_HEARTBEAT_URL: 心跳接口地址（必填）
    - SWE_SERVICE_HEARTBEAT_INTERVAL: 心跳间隔秒数（默认30）
    - SWE_SERVICE_HEARTBEAT_INSTANCE_PORT: 实例端口（默认8088）
    - SWE_SERVICE_HEARTBEAT_WEIGHT: 权重（默认1）
    - SWE_SERVICE_HEARTBEAT_SERVICE_NAME: 服务名称（默认swe）

    容器自带的环境变量（自动获取，无需配置）：
    - CMB_CAAS_SERVICEUNITID: 服务单元标识
    - CMB_CLUSTER: 可用区标识

    config.json 中无需配置，所有值通过 property 从环境变量动态获取。
    """

    # Pydantic 模型配置：允许额外字段以兼容旧配置文件
    model_config = ConfigDict(extra="ignore")

    @property
    def enabled(self) -> bool:
        """从环境变量获取是否启用。"""
        return EnvVarLoader.get_bool(
            "SWE_SERVICE_HEARTBEAT_ENABLED",
            default=True,
        )

    @property
    def service_name(self) -> str:
        """从环境变量获取服务名称。"""
        return EnvVarLoader.get_str(
            "SWE_SERVICE_HEARTBEAT_SERVICE_NAME",
            "swe",
        )

    @property
    def url(self) -> str:
        """从环境变量获取心跳URL。"""
        return EnvVarLoader.get_str("SWE_SERVICE_HEARTBEAT_URL", "")

    @property
    def interval_seconds(self) -> int:
        """从环境变量获取心跳间隔秒数。"""
        return EnvVarLoader.get_int(
            "SWE_SERVICE_HEARTBEAT_INTERVAL",
            default=20,
            min_value=5,
            max_value=300,
        )

    @property
    def instance_port(self) -> int:
        """从环境变量获取实例端口。"""
        return EnvVarLoader.get_int(
            "SWE_SERVICE_HEARTBEAT_INSTANCE_PORT",
            default=8088,
            min_value=1,
            max_value=65535,
        )

    @property
    def weight(self) -> int:
        """从环境变量获取权重。"""
        return EnvVarLoader.get_int(
            "SWE_SERVICE_HEARTBEAT_WEIGHT",
            default=1,
            min_value=1,
            max_value=100,
        )


def _parse_cluster_nodes(env_value: str) -> List[Dict[str, Any]]:
    """Parse cluster nodes from environment variable string.

    Format: "host1:port1,host2:port2,host3:port3"
    Returns: [{"host": "host1", "port": port1}, ...]
    """
    if not env_value or not env_value.strip():
        return []

    nodes = []
    for node_str in env_value.split(","):
        node_str = node_str.strip()
        if not node_str:
            continue
        if ":" in node_str:
            host, port_str = node_str.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                port = 6379
            nodes.append({"host": host.strip(), "port": port})
        else:
            nodes.append({"host": node_str, "port": 6379})
    return nodes


class CronCoordinationConfig(BaseModel):
    """Redis-based cron coordination configuration.

    Controls multi-instance cron leadership election, scheduler preflight,
    and jobs.json definition convergence. Execution-lock settings remain for
    explicit legacy paths and are not the default timed execution contract.
    Supports both standalone Redis and Redis Cluster modes. Defaults are
    derived from environment-backed cron constants and code fallbacks rather
    than root config.json.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(
        default=CRON_COORDINATION_ENABLED,
        description="Enable Redis coordination for cron leadership election",
    )
    # Connection mode: standalone or cluster
    cluster_mode: bool = Field(
        default=CRON_CLUSTER_MODE,
        description="Use Redis Cluster mode instead of standalone Redis",
    )
    # Standalone mode configuration
    redis_url: str = Field(
        default=CRON_REDIS_URL,
        description="Redis connection URL for standalone mode",
    )
    # Cluster mode configuration
    cluster_nodes: List[Dict[str, Any]] = Field(
        default_factory=lambda: _parse_cluster_nodes(CRON_CLUSTER_NODES),
        description="List of cluster nodes as dicts with 'host' and 'port' keys. "
        "Example: [{'host': 'node1', 'port': 6379}, {'host': 'node2', 'port': 6379}]",
    )
    cluster_skip_full_coverage_check: bool = Field(
        default=True,
        description="Skip full coverage check for cluster (useful with some cluster setups)",
    )
    cluster_max_connections: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Maximum connections in cluster mode",
    )
    # Lease configuration
    lease_ttl_seconds: int = Field(
        default=CRON_LEASE_TTL_SECONDS,
        ge=5,
        le=300,
        description="Lease TTL in seconds (must be > renew interval)",
    )
    lease_renew_interval_seconds: int = Field(
        default=CRON_LEASE_RENEW_INTERVAL_SECONDS,
        ge=1,
        le=60,
        description="How often to renew lease",
    )
    lease_renew_failure_threshold: int = Field(
        default=CRON_LEASE_RENEW_FAILURE_THRESHOLD,
        ge=1,
        le=10,
        description="Consecutive failures before considering lease lost",
    )
    # Execution lock configuration
    lock_safety_margin_seconds: int = Field(
        default=CRON_LOCK_SAFETY_MARGIN_SECONDS,
        ge=5,
        le=300,
        description="Additional time added to job timeout for legacy execution-lock paths",
    )
    definition_lock_timeout_seconds: float = Field(
        default=10.0,
        ge=0.05,
        le=300,
        description="How long to wait for the shared jobs.json definition lock",
    )
    # Reload pub/sub configuration
    reload_channel_prefix: str = Field(
        default="swe:cron:reload",
        description="Redis pub/sub channel prefix for reload signals",
    )

    @model_validator(mode="after")
    def validate_lease_config(self) -> "CronCoordinationConfig":
        """Validate lease configuration."""
        if self.lease_ttl_seconds <= self.lease_renew_interval_seconds:
            raise ValueError(
                "lease_ttl_seconds must be greater than "
                "lease_renew_interval_seconds",
            )
        return self


class Config(BaseModel):
    """Root config (config.json)."""

    channels: ChannelConfig = ChannelConfig()
    mcp: MCPConfig = MCPConfig()
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    last_api: LastApiConfig = LastApiConfig()
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    last_dispatch: Optional[LastDispatchConfig] = None
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    hooks: HookConfig = Field(
        default_factory=HookConfig,
        description="Tenant-scoped lifecycle hook configuration",
    )
    service_heartbeat: ServiceHeartbeatConfig = Field(
        default_factory=ServiceHeartbeatConfig,
        description="服务心跳配置",
    )
    show_tool_details: bool = True
    user_timezone: str = Field(
        default_factory=detect_system_timezone,
        description="User IANA timezone (e.g. Asia/Shanghai). "
        "Defaults to the system timezone.",
    )


ChannelConfigUnion = ConsoleConfig


# Agent configuration utility functions


def _resolve_agent_root_config_path(
    config_path: Path | None = None,
    tenant_id: str | None = None,
) -> Path | None:
    """Resolve the root config path for agent load/save helpers."""
    if config_path is not None:
        return Path(config_path).expanduser()
    if tenant_id is None:
        return None

    from .utils import get_tenant_storage_config_path

    return get_tenant_storage_config_path(tenant_id)


def load_agent_config(
    agent_id: str,
    config_path: Path | None = None,
    *,
    tenant_id: str | None = None,
) -> AgentProfileConfig:
    """Load agent's complete configuration from workspace/agent.json.

    Args:
        agent_id: Agent ID to load
        config_path: Optional root config.json path to resolve agent refs from
        tenant_id: Optional tenant ID to resolve tenant-scoped root config

    Returns:
        AgentProfileConfig: Complete agent configuration

    Raises:
        ValueError: If agent ID not found in root config
    """
    from .utils import load_config

    resolved_config_path = _resolve_agent_root_config_path(
        config_path=config_path,
        tenant_id=tenant_id,
    )
    config = load_config(resolved_config_path)

    if agent_id not in config.agents.profiles:
        raise ValueError(f"Agent '{agent_id}' not found in config")

    agent_ref = config.agents.profiles[agent_id]
    workspace_dir = Path(agent_ref.workspace_dir).expanduser()
    agent_config_path = workspace_dir / "agent.json"

    if not agent_config_path.exists():
        raise FileNotFoundError(
            f"Agent config not found: {agent_config_path}",
        )

    with open(agent_config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Ensure required fields exist for legacy data
    if "id" not in data:
        data["id"] = agent_id
    if "name" not in data:
        data["name"] = agent_id.title()

    # Normalize legacy ~/.swe-bound paths to current WORKING_DIR.
    # This keeps SWE_WORKING_DIR effective even if existing agent.json
    # contains older hard-coded paths like "~/.swe/media".
    try:
        from .utils import _normalize_working_dir_bound_paths

        normalized_data = _normalize_working_dir_bound_paths(data)
        if isinstance(normalized_data, dict):
            data = normalized_data
    except Exception:
        pass

    data["system_prompt_files"] = normalize_system_prompt_files(
        data.get("system_prompt_files"),
    )

    return AgentProfileConfig(**data)


def save_agent_config(
    agent_id: str,
    agent_config: AgentProfileConfig,
    config_path: Path | None = None,
    *,
    tenant_id: str | None = None,
) -> None:
    """Save agent configuration to workspace/agent.json.

    Args:
        agent_id: Agent ID
        agent_config: Complete agent configuration to save
        config_path: Optional root config.json path to resolve agent refs from
        tenant_id: Optional tenant ID to resolve tenant-scoped root config

    Raises:
        ValueError: If agent ID not found in root config
    """
    from .utils import load_config

    resolved_config_path = _resolve_agent_root_config_path(
        config_path=config_path,
        tenant_id=tenant_id,
    )
    config = load_config(resolved_config_path)

    if agent_id not in config.agents.profiles:
        raise ValueError(f"Agent '{agent_id}' not found in config")

    agent_ref = config.agents.profiles[agent_id]
    workspace_dir = Path(agent_ref.workspace_dir).expanduser()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    agent_config_path = workspace_dir / "agent.json"

    with open(agent_config_path, "w", encoding="utf-8") as f:
        json.dump(
            agent_config.model_dump(exclude_none=True),
            f,
            ensure_ascii=False,
            indent=2,
        )


def migrate_legacy_config_to_multi_agent() -> bool:
    """Migrate legacy single-agent config to new multi-agent structure.

    Returns:
        bool: True if migration was performed, False if already migrated
    """
    from .utils import load_config, save_config

    config = load_config()

    # Check if already migrated (new structure has only AgentProfileRef)
    if "default" in config.agents.profiles:
        agent_ref = config.agents.profiles["default"]
        # If it's already a AgentProfileRef, migration done
        if isinstance(agent_ref, AgentProfileRef):
            # Check if default agent config exists
            workspace_dir = Path(agent_ref.workspace_dir).expanduser()
            agent_config_path = workspace_dir / "agent.json"
            if agent_config_path.exists():
                return False  # Already migrated

    # Perform migration
    print("Migrating legacy config to multi-agent structure...")

    # Extract legacy agent configuration
    legacy_agents = config.agents

    # Create default agent workspace
    default_workspace = Path(f"{WORKING_DIR}/workspaces/default").expanduser()
    default_workspace.mkdir(parents=True, exist_ok=True)

    # Create default agent configuration from legacy settings
    default_agent_config = AgentProfileConfig(
        id="default",
        name="Default Agent",
        description="Default SWE agent",
        workspace_dir=str(default_workspace),
        channels=config.channels if config.channels else None,
        mcp=config.mcp if config.mcp else None,
        heartbeat=(
            legacy_agents.defaults.heartbeat
            if legacy_agents.defaults
            else None
        ),
        running=(
            legacy_agents.running
            if legacy_agents.running
            else AgentsRunningConfig()
        ),
        llm_routing=(
            legacy_agents.llm_routing
            if legacy_agents.llm_routing
            else AgentsLLMRoutingConfig()
        ),
        system_prompt_files=normalize_system_prompt_files(
            (
                legacy_agents.system_prompt_files
                if legacy_agents.system_prompt_files
                else None
            ),
        ),
        tools=config.tools if config.tools else None,
        security=config.security if config.security else None,
    )

    # Save default agent configuration to workspace
    agent_config_path = default_workspace / "agent.json"
    with open(agent_config_path, "w", encoding="utf-8") as f:
        json.dump(
            default_agent_config.model_dump(exclude_none=True),
            f,
            ensure_ascii=False,
            indent=2,
        )

    # Migrate existing workspace files from legacy default working dir.
    # When SWE_WORKING_DIR is customized, historical data may still exist
    # under "~/.swe".
    old_workspace = Path("~/.swe").expanduser().resolve()

    # Move sessions, memory, and other workspace files
    for item_name in ["sessions", "memory", "jobs.json"]:
        old_path = old_workspace / item_name
        if old_path.exists():
            new_path = default_workspace / item_name
            if not new_path.exists():
                import shutil

                if old_path.is_dir():
                    shutil.copytree(old_path, new_path)
                else:
                    shutil.copy2(old_path, new_path)
                print(f"  Migrated {item_name} to default workspace")

    # Copy markdown files used by the default system prompt.
    for md_file in get_default_system_prompt_files():
        old_md = old_workspace / md_file
        if old_md.exists():
            new_md = default_workspace / md_file
            if not new_md.exists():
                import shutil

                shutil.copy2(old_md, new_md)
                print(f"  Migrated {md_file} to default workspace")

    # Update root config.json to new structure
    # CRITICAL: Preserve legacy agent fields for downgrade compatibility
    config.agents = AgentsConfig(
        active_agent="default",
        profiles={
            "default": AgentProfileRef(
                id="default",
                workspace_dir=str(default_workspace),
            ),
        },
        # Preserve legacy fields with values from migrated agent config
        running=default_agent_config.running,
        llm_routing=default_agent_config.llm_routing,
        language=(
            default_agent_config.language
            if hasattr(default_agent_config, "language")
            else "zh"
        ),
        system_prompt_files=default_agent_config.system_prompt_files,
    )

    # IMPORTANT: Keep channels, mcp, tools, security in root config for
    # backward compatibility. Do NOT clear these fields.
    # Old versions expect these fields to exist with valid values.

    save_config(config)

    print("Migration completed successfully!")
    print(f"  Default agent workspace: {default_workspace}")
    print(f"  Default agent config: {agent_config_path}")

    return True
