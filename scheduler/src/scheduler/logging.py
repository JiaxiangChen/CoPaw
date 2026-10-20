# -*- coding: utf-8 -*-
"""为独立 Scheduler 配置业务日志，不调整其他库的根日志级别。"""

import logging
import os
import sys


def configure_logging():
    """覆盖直接 Uvicorn 和 CLI 入口，已有输出配置保持不变。"""
    level = getattr(
        logging,
        os.environ.get("SCHEDULER_LOG_LEVEL", "info").strip().upper(),
        logging.INFO,
    )
    if not isinstance(level, int):
        level = logging.INFO
    logger = logging.getLogger("scheduler")
    logger.setLevel(level)
    if not logger.hasHandlers():
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s:%(lineno)d %(message)s",
            ),
        )
        logger.addHandler(handler)
