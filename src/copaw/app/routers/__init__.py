# -*- coding: utf-8 -*-
from fastapi import APIRouter

from .agent import router as agent_router
from .config import router as config_router
from .console import router as console_router
from .envs import router as envs_router
from .files import router as files_router
from .local_models import router as local_models_router
from .mcp import router as mcp_router
from .providers import router as providers_router
from .skills import router as skills_router
from .tracing import router as tracing_router
from .users import router as users_router
from .workspace import router as workspace_router
from .zhaohu import zhaohu_router
from ..crons.api import router as cron_router
from ..runner.api import router as runner_router
from ..backup import router as backup_router
from ..backup.batch_router import router as backup_batch_router


router = APIRouter()

router.include_router(agent_router)
router.include_router(config_router)
router.include_router(console_router)
router.include_router(cron_router)
router.include_router(files_router)
router.include_router(local_models_router)
router.include_router(mcp_router)
router.include_router(providers_router)
router.include_router(runner_router)
router.include_router(skills_router)
router.include_router(tracing_router)
router.include_router(users_router)
router.include_router(workspace_router)
router.include_router(envs_router)
router.include_router(backup_router)
router.include_router(backup_batch_router)
router.include_router(zhaohu_router)

__all__ = ["router"]
