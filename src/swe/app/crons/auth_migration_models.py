# -*- coding: utf-8 -*-
"""Strict operator requests and credential-free migration diagnostics."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from ...config.context import is_valid_identity_value

Operation = Literal["preview", "import", "verify", "prune"]
Phase = Literal["files", "database"]
FilterValues = Annotated[list[str], Field(min_length=1, max_length=200)]


def safe_scope(value: str) -> bool:
    """Accept an exact directory basename without normalizing its identity."""
    return bool(
        value
        and value not in {".", ".."}
        and not value.startswith(".")
        and len(value.encode("utf-8")) <= 1024
        and not any(c in value for c in ("/", "\\", "\x00"))
        and not any(ord(c) < 32 or ord(c) == 127 for c in value),
    )


class AuthMigrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation: Operation
    tenant_ids: FilterValues | None = None
    source_ids: FilterValues | None = None
    storage_scope_ids: FilterValues | None = None
    page_size: int = Field(default=100, ge=1, le=200)

    @field_validator("tenant_ids", "source_ids", "storage_scope_ids")
    @classmethod
    def validate_filters(cls, values, info):
        if values is None:
            return values
        validator = (
            safe_scope
            if info.field_name == "storage_scope_ids"
            else is_valid_identity_value
        )
        if len(set(values)) != len(values) or not all(
            validator(value) for value in values
        ):
            raise ValueError("invalid_or_duplicate_filter")
        return values

    @model_validator(mode="after")
    def explicit_prune(self):
        if self.operation == "prune" and not any(
            (self.tenant_ids, self.source_ids, self.storage_scope_ids),
        ):
            raise ValueError("prune_requires_explicit_range")
        return self


class AuthMigrationItem(BaseModel):
    storage_scope_id: str
    tenant_id: str | None = None
    source_id: str | None = None
    status: str
    reason: str | None = None
    differences: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class AuthMigrationPage(BaseModel):
    phase: Phase
    counts: dict[str, int] = Field(default_factory=dict)
    items: list[AuthMigrationItem] = Field(default_factory=list)
    scanned_files: int = 0
    scanned_database_rows: int = 0
    next_phase: Phase = "files"
    next_after: str | None = None
    scan_complete: bool = False


class AuthMigrationTaskStatus(BaseModel):
    task_id: str
    status: str
    progress: dict = Field(default_factory=dict)
    error: str | None = None
    pages: list[dict] = Field(default_factory=list)
    next_after_page: int | None = None
