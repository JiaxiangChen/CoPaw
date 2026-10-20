# -*- coding: utf-8 -*-
"""Contracts for the opt-in, two-table Cron snapshot import."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...config.context import is_valid_identity_value


class MigrationRequest(BaseModel):
    """A bounded page of a snapshot operation, never a filesystem path."""

    model_config = ConfigDict(extra="forbid")

    tenant_ids: list[str] | None = None
    source_ids: list[str] | None = None
    agent_ids: list[str] | None = None
    job_ids: list[str] | None = None
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, max_length=8192)

    @field_validator("tenant_ids", "source_ids", "agent_ids", "job_ids")
    @classmethod
    def validate_filter(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value or len(value) > 1000:
            raise ValueError("filters must contain 1 to 1000 identities")
        if any(
            not is_valid_identity_value(v) or v != v.strip() or len(v) > 128
            for v in value
        ):
            raise ValueError("invalid filter identity")
        return sorted(set(value))


@dataclass
class SnapshotCandidate:
    """Validated definition and presence-aware state; no live handles."""

    definition: dict[str, Any]
    state_patch: dict[str, Any]
    observed_at: datetime
    source_key: str
    fingerprint: str
    file_version: int
    memory_available: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (
            self.definition["tenant_id"],
            self.definition["source_id"],
            self.definition["agent_id"],
            self.definition["job_id"],
        )


@dataclass
class SourceItem:
    source_key: str
    job_id: str | None = None
    candidate: SnapshotCandidate | None = None
    reason: str | None = None


@dataclass
class SourcePage:
    items: list[SourceItem] = field(default_factory=list)
    next_cursor: str | None = None
    scanned_files: int = 0
    scanned_jobs: int = 0


class MigrationItemResult(BaseModel):
    source_key: str
    job_id: str | None = None
    identity: dict[str, str] | None = None
    status: str
    reason: str | None = None
    differences: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    memory_available: bool | None = None


class MigrationResponse(BaseModel):
    operation: str
    instance_id: str
    counts: dict[str, int]
    scanned_files: int
    scanned_jobs: int
    memory_incomplete: bool
    partial_failure: bool
    next_cursor: str | None = None
    items: list[MigrationItemResult]
    phase: str = "source"
    scanned_database_rows: int = 0
    definition_scope: str = "visible_filesystem"
    memory_scope: str = "loaded_on_current_instance"


class MigrationTaskRequest(MigrationRequest):
    """A complete scan; limit controls internal pages, not the total scope."""

    operation: Literal["preview", "import", "verify"] = "import"
    cursor: None = Field(default=None, exclude=True)


class MigrationTaskStatus(BaseModel):
    task_id: str
    status: str
    progress: dict[str, Any]
    error: str | None = None
    pages: list[dict[str, Any]] = Field(default_factory=list)
    next_after_page: int | None = None
