# -*- coding: utf-8 -*-
"""外部平台主动调用的单次日期维护，不创建内部周期任务。"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .managed_cron_controls import control_service
from ..services.cron.managed.date_ranges import DateMaintenanceService
from ..services.cron.managed.diagnostics import format_management_error
from ..services.cron.managed.identity import identity_value

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/scheduler/cron/date-ranges",
    tags=["cron-date-ranges"],
)


class ApplyDatesRequest(BaseModel):
    """平台不能传用户 Header；只允许选取一个来源，不接收历史日期。"""

    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=128)

    @field_validator("source_id")
    @classmethod
    def validate_source(cls, value):
        return identity_value(value)


def date_service(controls=Depends(control_service)):
    return DateMaintenanceService(controls.db, controls.platform)


@router.post(
    "/apply",
    summary="按北京时间保留当日有效入口并预开次日入口",
)
async def apply_dates(body: ApplyDatesRequest, service=Depends(date_service)):
    """同步维护今天/明天的入口；实际执行仍按当天校验，不需用户 Header。"""
    try:
        return await service.apply(body.source_id)
    except Exception as error:
        logger.error(
            "日期维护调用失败 source_id=%s\n%s",
            body.source_id,
            format_management_error(error),
        )
        raise HTTPException(
            503,
            "cron_date_range_storage_unavailable",
        ) from error
