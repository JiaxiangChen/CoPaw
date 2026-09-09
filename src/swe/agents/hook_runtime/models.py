# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HookEventName(str, Enum):
    SESSION_START = "SessionStart"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    POST_TOOL_USE_FAILURE = "PostToolUseFailure"
    STOP = "Stop"


PROMPT_HANDLER_MAX_PROMPT_LENGTH = 20000
PROMPT_HANDLER_BLOCKABLE_EVENTS = {
    HookEventName.SESSION_START,
    HookEventName.USER_PROMPT_SUBMIT,
    HookEventName.PRE_TOOL_USE,
    HookEventName.POST_TOOL_USE,
    HookEventName.POST_TOOL_USE_FAILURE,
    HookEventName.STOP,
}


class PermissionMode(str, Enum):
    DEFAULT = "default"
    PLAN = "plan"
    ACCEPT_EDITS = "acceptEdits"
    AUTO = "auto"
    DONT_ASK = "dontAsk"
    BYPASS_PERMISSIONS = "bypassPermissions"


class EffortLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class FailPolicy(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"


class HookDecision(str, Enum):
    NONE = "none"
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"
    BLOCK = "block"
    STOP = "stop"


class EffortConfig(BaseModel):
    level: EffortLevel


class HookContext(BaseModel):
    """Claude-style hook envelope with Swe runtime metadata."""

    model_config = ConfigDict(extra="allow", use_enum_values=True)

    session_id: str
    transcript_path: str
    cwd: str
    hook_event_name: HookEventName
    tenant_id: str
    effective_tenant_id: str
    user_id: str
    agent_id: str
    channel: str
    permission_mode: PermissionMode | None = None
    effort: EffortConfig | None = None
    agent_type: str | None = None
    source_id: str | None = None
    trace_id: str | None = None
    workspace_dir: str | None = None
    chat_id: str | None = None
    turn_id: str | None = None
    source: Literal["startup", "resume", "clear", "compact"] | None = None
    model: str | None = None
    prompt: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    tool_response: Any = None
    assistant_response: str | None = None
    error: str | None = None
    conversation_snapshot: list[dict[str, Any]] | None = None
    conversation_snapshot_meta: dict[str, Any] | None = None

    def to_handler_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class HookOutput(BaseModel):
    """Public handler output parsed from Claude-style JSON."""

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        use_enum_values=True,
    )

    continue_: bool | None = Field(default=None, alias="continue")
    stop_reason: str | None = Field(default=None, alias="stopReason")
    suppress_output: bool | None = Field(default=None, alias="suppressOutput")
    system_message: str | None = Field(default=None, alias="systemMessage")
    decision: str | None = None
    reason: str | None = None
    hook_specific_output: dict[str, Any] = Field(
        default_factory=dict,
        alias="hookSpecificOutput",
    )


class AdditionalContext(BaseModel):
    handler_id: str
    context: str


class HookHandlerResult(BaseModel):
    """Normalized single-handler result."""

    handler_id: str
    order: int
    output: HookOutput = Field(default_factory=HookOutput)
    decision: HookDecision = HookDecision.NONE
    reason: str = ""
    failed: bool = False
    failure_type: str = ""
    replacement_text: str | None = None
    output_transform: bool = False


class HookPermissionDecision(BaseModel):
    """Permission decision emitted by a single hook handler."""

    handler_id: str
    decision: HookDecision
    reason: str = ""


class MergedHookResult(BaseModel):
    decision: HookDecision = HookDecision.NONE
    reason: str = ""
    additional_context: list[AdditionalContext] = Field(default_factory=list)
    hook_specific_outputs: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
    )
    permission_decisions: list[HookPermissionDecision] = Field(
        default_factory=list,
    )
    updated_input: dict[str, Any] | None = None
    session_title: str | None = None
    suppress_output: bool = False
    system_messages: list[str] = Field(default_factory=list)
    has_blocking_failure: bool = False
    blocking_failure_reason: str = ""

    @property
    def blocked(self) -> bool:
        return self.decision in {
            HookDecision.BLOCK,
            HookDecision.DENY,
            HookDecision.STOP,
        }


class StopHookExecutionResult(BaseModel):
    final_response: str
    validation_result: MergedHookResult = Field(
        default_factory=MergedHookResult,
    )
    transformation_failed: bool = False
    transformation_failure_reason: str = ""


class BaseHookHandlerConfig(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
        use_enum_values=True,
    )

    id: str
    type: str
    if_condition: str = Field(default="", alias="if")
    timeout: float = Field(default=10.0, gt=0)
    status_message: str = Field(default="", alias="statusMessage")
    once: bool = False
    output_transform: bool = Field(default=False, alias="outputTransform")
    include_conversation_snapshot: bool = Field(
        default=False,
        alias="includeConversationSnapshot",
    )
    conversation_snapshot_limit: int = Field(
        default=50,
        ge=1,
        le=200,
        alias="conversationSnapshotLimit",
    )
    fail_policy: FailPolicy = Field(
        default=FailPolicy.ALLOW,
        alias="failPolicy",
    )

    def target_identity(self) -> str:
        return ""


class CommandHookHandlerConfig(BaseHookHandlerConfig):
    type: Literal["command"] = "command"
    command: str = ""
    argv: list[str] = Field(default_factory=list)
    shell: Literal["sh", "bash", "zsh", "cmd", "powershell"] | None = None
    cwd: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    async_execution: bool = Field(default=False, alias="async")
    async_rewake: bool = Field(default=False, alias="asyncRewake")

    @model_validator(mode="after")
    def validate_command(self) -> "CommandHookHandlerConfig":
        if self.async_execution or self.async_rewake:
            raise ValueError("command async behavior is not supported")
        if not self.command and not self.argv:
            raise ValueError("command handler requires command or argv")
        return self

    def target_identity(self) -> str:
        if self.argv:
            return "\x00".join(self.argv)
        return self.command


class HttpHookHandlerConfig(BaseHookHandlerConfig):
    type: Literal["http"] = "http"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    header_secret_refs: dict[str, str] = Field(
        default_factory=dict,
        alias="headerSecretRefs",
    )
    allowed_env_vars: list[str] = Field(
        default_factory=list,
        alias="allowedEnvVars",
    )

    @model_validator(mode="after")
    def validate_url(self) -> "HttpHookHandlerConfig":
        if not self.url.strip():
            raise ValueError("http handler requires non-empty url")
        return self

    def target_identity(self) -> str:
        return self.url


class PromptHookHandlerConfig(BaseHookHandlerConfig):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        use_enum_values=True,
    )

    type: Literal["prompt"] = "prompt"
    prompt: str = Field(max_length=PROMPT_HANDLER_MAX_PROMPT_LENGTH)
    fail_policy: FailPolicy = Field(
        default=FailPolicy.BLOCK,
        alias="failPolicy",
    )

    @model_validator(mode="after")
    def validate_prompt(self) -> "PromptHookHandlerConfig":
        if not self.prompt.strip():
            raise ValueError("prompt handler requires non-empty prompt")
        return self

    def target_identity(self) -> str:
        digest = hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()
        return f"prompt:{digest}"


HookHandlerConfig = Annotated[
    CommandHookHandlerConfig | HttpHookHandlerConfig | PromptHookHandlerConfig,
    Field(discriminator="type"),
]


class HookMatcherConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tools: list[str] = Field(default_factory=list)

    def matches(self, context: HookContext) -> bool:
        if self.tools:
            return bool(context.tool_name and context.tool_name in self.tools)
        return True


class HookMatcherGroupConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = ""
    matcher: HookMatcherConfig = Field(default_factory=HookMatcherConfig)
    hooks: list[HookHandlerConfig] = Field(default_factory=list)


class HookConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    events: dict[HookEventName, list[HookMatcherGroupConfig]] = Field(
        default_factory=dict,
    )

    def handler_ids(self) -> set[str]:
        ids: set[str] = set()
        for groups in self.events.values():
            for group in groups:
                ids.update(handler.id for handler in group.hooks)
        return ids

    @model_validator(mode="after")
    def validate_prompt_handler_events(self) -> "HookConfig":
        for event_name, groups in self.events.items():
            for group in groups:
                for handler in group.hooks:
                    validate_handler_event(handler, event_name)
        return self


def validate_handler_event(
    handler: HookHandlerConfig,
    event_name: HookEventName,
) -> None:
    if (
        isinstance(handler, PromptHookHandlerConfig)
        and event_name not in PROMPT_HANDLER_BLOCKABLE_EVENTS
    ):
        raise ValueError(
            "prompt hook handlers must be configured on blockable events only",
        )
    if handler.output_transform and event_name != HookEventName.STOP:
        raise ValueError(
            "outputTransform handlers must be configured on Stop only",
        )
    if handler.output_transform and handler.once:
        raise ValueError("outputTransform handlers cannot use once")


def skill_hook_handler_definition(
    event_name: HookEventName | str,
    group: HookMatcherGroupConfig,
    handler: HookHandlerConfig,
) -> str:
    """Return the stable source definition used to guard Skill once state."""
    return json.dumps(
        {
            "event": str(getattr(event_name, "value", event_name)),
            "group": group.id,
            "matcher": group.matcher.model_dump(
                mode="json",
                by_alias=True,
            ),
            "handler": handler.model_dump(
                mode="json",
                by_alias=True,
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class HookOverlayEntry(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    hook_id: str = Field(alias="hookId")
    enabled: bool | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = Field(default=None, alias="expiresAt")
    reason: str = ""

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now


class LoadedSkillHookSource(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
        use_enum_values=True,
    )

    source_id: str = Field(alias="sourceId")
    skill_name: str = Field(alias="skillName")
    skill_root: str = Field(alias="skillRoot")
    source_path: str = Field(alias="sourcePath")
    hook_config: HookConfig = Field(alias="hookConfig")
    loaded_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        alias="loadedAt",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_loaded_skill_source(self) -> "LoadedSkillHookSource":
        namespace = f"skill:{self.skill_name}:"
        expected_source_id = f"skill:{self.skill_name}"
        if self.source_id != expected_source_id:
            raise ValueError(
                "loaded skill hook source id must match skill namespace",
            )
        seen_handlers: set[str] = set()
        for groups in self.hook_config.events.values():
            for group in groups:
                if group.id and not group.id.startswith(namespace):
                    raise ValueError(
                        "loaded skill hook matcher group ids must be namespaced",
                    )
                for handler in group.hooks:
                    if not handler.id.startswith(namespace):
                        raise ValueError(
                            "loaded skill hook handler ids must be namespaced",
                        )
                    if handler.id in seen_handlers:
                        raise ValueError(
                            "duplicate loaded skill hook handler id",
                        )
                    seen_handlers.add(handler.id)
        return self

    def handler_ids(self) -> set[str]:
        return self.hook_config.handler_ids()


class SkillHookFileVersion(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    exists: bool
    mtime_ns: int = Field(default=0, alias="mtimeNs")
    size: int = 0
    inode: int = 0


class MonitoredSkillHookSource(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
        use_enum_values=True,
    )

    source_id: str = Field(alias="sourceId")
    skill_name: str = Field(alias="skillName")
    skill_root: str = Field(alias="skillRoot")
    source_path: str = Field(alias="sourcePath")
    file_version: SkillHookFileVersion | None = Field(
        default=None,
        alias="fileVersion",
    )

    @model_validator(mode="after")
    def validate_monitored_skill_source(self) -> "MonitoredSkillHookSource":
        if self.source_id != f"skill:{self.skill_name}":
            raise ValueError(
                "monitored skill hook source id must match skill namespace",
            )
        return self


class HookSessionState(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
        use_enum_values=True,
    )

    loaded_skill_sources: list[LoadedSkillHookSource] = Field(
        default_factory=list,
        alias="loadedSkillSources",
    )
    monitored_skill_sources: list[MonitoredSkillHookSource] = Field(
        default_factory=list,
        alias="monitoredSkillSources",
    )
    entries: list[HookOverlayEntry] = Field(default_factory=list)
    once_executed: dict[str, bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_session_state(self) -> "HookSessionState":
        seen_source_ids: set[str] = set()
        seen_skill_names: set[str] = set()
        seen_monitored_skill_names: set[str] = set()
        available_skill_handler_ids: set[str] = set()
        for source in self.loaded_skill_sources:
            if source.source_id in seen_source_ids:
                raise ValueError("duplicate loaded skill hook source id")
            if source.skill_name in seen_skill_names:
                raise ValueError("duplicate loaded skill hook skill name")
            seen_source_ids.add(source.source_id)
            seen_skill_names.add(source.skill_name)
            available_skill_handler_ids.update(source.handler_ids())

        for source in self.monitored_skill_sources:
            if source.skill_name in seen_monitored_skill_names:
                raise ValueError("duplicate monitored skill hook skill name")
            seen_monitored_skill_names.add(source.skill_name)

        for entry in self.entries:
            if entry.hook_id.startswith("skill:"):
                if entry.hook_id not in available_skill_handler_ids:
                    raise ValueError(
                        "overlay references unknown loaded skill hook id",
                    )
        return self

    def loaded_skill_handler_ids(self) -> set[str]:
        ids: set[str] = set()
        for source in self.loaded_skill_sources:
            ids.update(source.handler_ids())
        return ids

    def has_loaded_skill_sources(self) -> bool:
        return bool(self.loaded_skill_sources)

    def has_monitored_skill_sources(self) -> bool:
        return bool(self.monitored_skill_sources)


class HookSessionOverlay(HookSessionState):
    """Backward-compatible name for persisted session hook state."""


class EffectiveHookHandler(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    handler: HookHandlerConfig
    group_id: str
    order: int
    dedupe_key: str
    source: str = "tenant"
    skill_definition: str | None = None

    def success(self, raw_output: dict[str, Any] | None) -> HookHandlerResult:
        from .output import normalize_hook_output

        return normalize_hook_output(
            handler_id=self.handler.id,
            order=self.order,
            raw_output=raw_output or {},
        )

    def failure(self, reason: str, failure_type: str) -> HookHandlerResult:
        decision = (
            HookDecision.BLOCK
            if self.handler.fail_policy == FailPolicy.BLOCK
            else HookDecision.NONE
        )
        return HookHandlerResult(
            handler_id=self.handler.id,
            order=self.order,
            decision=decision,
            reason=reason,
            failed=True,
            failure_type=failure_type,
        )


class EffectiveHookPlan(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    event_name: HookEventName
    context: HookContext
    handlers: tuple[EffectiveHookHandler, ...] = ()


def copy_handler_with_overrides(
    handler: HookHandlerConfig,
    overrides: dict[str, Any],
) -> HookHandlerConfig:
    if not overrides:
        return handler
    data = handler.model_dump(by_alias=True, mode="json")
    merged = copy.deepcopy(data)
    merged.update(overrides)
    return type(handler).model_validate(merged)
