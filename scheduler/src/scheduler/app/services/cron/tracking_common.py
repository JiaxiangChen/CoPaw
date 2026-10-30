# -*- coding: utf-8 -*-
"""单任务追踪的只读身份、时间和安全边界。"""

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
KEYS = ("tenant_id", "source_id", "agent_id", "job_id")
BEIJING = ZoneInfo("Asia/Shanghai")
DEFINITIONS = "swe_cron_jobs_v2"
STATES = "swe_cron_job_states"
RUNS = "swe_cron_execution_runs"
EVENTS = "swe_cron_managed_dispatch_events"
JOURNAL = "swe_cron_registration_journal"
RECEIPTS = "swe_cron_managed_trigger_receipts"
HISTORY = "swe_cron_executions"
MEMBERS = "swe_cron_managed_batch_members"
BROADCASTS = "swe_cron_managed_broadcast_tasks"
ITEMS = "swe_cron_managed_broadcast_items"
MODES = "swe_cron_managed_mode_items"
CALLS = "swe_cron_failure_retry_calls"


class TrackingNotFound(LookupError):
    pass


class TrackingUnavailable(RuntimeError):
    def __init__(self, message, *, query_id=None):
        self.query_id = query_id
        super().__init__(message)


def identity_value(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("invalid_cron_identity")
    if (
        len(value) > 128
        or ".." in value
        or re.search(r"[/\\\x00-\x1f\x7f]", value)
    ):
        raise ValueError("invalid_cron_identity")
    return value


@dataclass(frozen=True)
class TaskIdentity:
    tenant_id: str
    source_id: str
    agent_id: str
    job_id: str

    def __post_init__(self):
        for value in self.values():
            identity_value(value)

    def values(self):
        return tuple(getattr(self, name) for name in KEYS)

    def public(self):
        return asdict(self)


def where_identity(alias="", names=KEYS):
    prefix = alias + "." if alias else ""
    return " AND ".join(
        f"{prefix}{name} COLLATE utf8mb4_bin=%s" for name in names
    )


def same_identity(row, identity):
    return all(row.get(name) == getattr(identity, name) for name in KEYS)


def new_protocol(alias=""):
    field = (alias + "." if alias else "") + "dispatch_meta_json"
    return (
        "JSON_UNQUOTE(JSON_EXTRACT(CASE WHEN JSON_VALID("
        + field
        + ") THEN "
        + field
        + " ELSE '{}' END,'$.protocol'))='scheduler_managed_v1'"
    )


def json_field(column, path):
    return f"JSON_UNQUOTE(JSON_EXTRACT(CASE WHEN JSON_VALID({column}) THEN {column} ELSE '{{}}' END,'$.{path}'))"


def json_object(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def secret_key(name):
    key = str(name).lower().replace("_", "-")
    return key in {
        "authorization",
        "cookie",
        "password",
        "secret",
        "token",
        "api-key",
        "access-key",
        "credential",
        "credentials",
        "x-header-cookie",
    } or key.endswith(
        (
            "-token",
            "-password",
            "-secret",
            "-api-key",
            "-authorization",
            "-cookie",
        ),
    )


def safe_text(value, limit=4096):
    text = str(value)
    text = re.sub(r"(?i)\bBearer\s+[^\s,;\"']+", "Bearer [redacted]", text)
    text = re.sub(r"(?i)(cookie\s*:\s*)[^\n\r]+", r"\1[redacted]", text)
    text = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[redacted]@", text)
    text = re.sub(
        r"(?i)((?:authorization|cookie|password|secret|(?:access[_-]?)?token|api[_-]?key)[\"']?\s*[=:]\s*[\"']?)[^\s,;&\"']+",
        r"\1[redacted]",
        text,
    )
    return text[:limit]


def public_value(value, depth=0):
    if depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            safe_text(key, 128): public_value(item, depth + 1)
            for key, item in list(value.items())[:50]
            if not secret_key(key) and not str(key).startswith("_scheduler")
        }
    if isinstance(value, list):
        return [public_value(item, depth + 1) for item in value[:50]]
    if isinstance(value, datetime):
        return iso_time(value)
    if isinstance(value, date):
        return value.isoformat()
    return safe_text(value) if isinstance(value, str) else value


def select_fields(row, fields):
    return public_value({name: row.get(name) for name in fields.split()})


def parse_time(value, zone=timezone.utc):
    if value is None or value == "":
        return None
    try:
        parsed = (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            if isinstance(value, str)
            else value
        )
        if not isinstance(parsed, datetime):
            return None
        return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError):
        return None


def iso_time(value, zone=timezone.utc):
    parsed = parse_time(value, zone)
    return parsed.astimezone(BEIJING).isoformat() if parsed else None


class TrackingReader:
    def __init__(self, db, *, query_timeout=5, budget_seconds=15):
        self.db = db
        self.query_timeout = query_timeout
        self.deadline = time.monotonic() + budget_seconds
        self.coverage = {}
        self.warnings = []
        self.query_id = uuid4().hex

    def warn(self, code):
        if code not in self.warnings:
            self.warnings.append(code)

    def object(self, value, warning):
        try:
            result = value if isinstance(value, dict) else json.loads(value)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, TypeError):
            self.warn(warning)
            return {}

    async def rows(self, table, query, params=(), *, required=False):
        try:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                self.warn("query_budget_exhausted")
                raise TimeoutError()
            rows = await asyncio.wait_for(
                self.db.fetch_all(query, params),
                timeout=min(self.query_timeout, remaining),
            )
        except Exception as exc:
            self.coverage[table] = "unavailable"
            self.warn(table + "_unavailable")
            logger.warning(
                "Cron tracking source unavailable query_id=%s table=%s error_type=%s",
                self.query_id,
                table,
                type(exc).__name__,
            )
            if required:
                raise TrackingUnavailable(
                    "cron_tracking_storage_unavailable",
                    query_id=self.query_id,
                ) from exc
            return []
        if self.coverage.get(table) != "unavailable":
            self.coverage[table] = (
                "available"
                if rows or self.coverage.get(table) == "available"
                else "empty"
            )
        return rows

    def metadata(self):
        return {
            "coverage": dict(self.coverage),
            "warnings": list(self.warnings),
            "query_id": self.query_id,
            "queried_at": datetime.now(BEIJING).isoformat(),
        }
