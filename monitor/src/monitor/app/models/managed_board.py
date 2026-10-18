# -*- coding: utf-8 -*-
"""新看板查询模型收敛筛选参数，列表与明细分页界限一致。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class BoardListQuery(BaseModel):
    start_time: datetime | None = None
    end_time: datetime | None = None
    status: (
        Literal["received", "pending", "running", "completed", "failed"] | None
    ) = None
    query: str | None = Field(default=None, max_length=200)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=200)


class BoardDetailQuery(BaseModel):
    member_page: int = Field(default=1, ge=1)
    member_limit: int = Field(default=50, ge=1, le=200)
    member_status: (
        Literal[
            "queued",
            "dispatching",
            "accepted",
            "running",
            "success",
            "error",
            "dispatch_failed",
            "skipped",
            "cancelled",
        ]
        | None
    ) = None
    member_role: Literal["parent", "child"] | None = None
    member_query: str | None = Field(default=None, max_length=200)
    event_page: int = Field(default=1, ge=1)
    event_limit: int = Field(default=50, ge=1, le=200)
    event_type: str | None = Field(default=None, max_length=64)
