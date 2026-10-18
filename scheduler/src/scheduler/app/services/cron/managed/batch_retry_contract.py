# -*- coding: utf-8 -*-
"""人工批重试明确选择候选版本，Body 不携带权限和执行正文。"""

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class BatchRetryConflict(ValueError):
    """同一幂等请求不能改动候选或人工确认。"""


class RetryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member_id: str = Field(min_length=1, max_length=64)
    execution_id: str = Field(min_length=1, max_length=64)
    member_version: int = Field(ge=1, strict=True)
    execution_version: int = Field(ge=1, strict=True)
    definition_version: int = Field(ge=1, strict=True)
    business_attempt: int = Field(ge=1, strict=True)


class ManagedBatchRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    confirm_stopped: StrictBool = False
    candidates: list[RetryCandidate] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_candidates(self):
        if len({item.member_id for item in self.candidates}) != len(
            self.candidates,
        ):
            raise ValueError("cron_retry_duplicate_member")
        return self
