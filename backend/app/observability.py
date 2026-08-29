from __future__ import annotations

import logging
import os
import sys
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path


REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="-")
LOGGER_NAME = "wanda"


class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = REQUEST_ID.get()
        return True


def setup_logging() -> logging.Logger:
    """Configure bounded, sanitized console and file logs once per process."""
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s request_id=%(request_id)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    context_filter = RequestContextFilter()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.addFilter(context_filter)
    logger.addHandler(console)

    log_path = Path(os.getenv("WANDA_LOG_PATH", "logs/app.log"))
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(context_filter)
        logger.addHandler(file_handler)
    except OSError as error:
        logger.warning("event=log_file_unavailable error_type=%s", type(error).__name__)

    logger.info("event=logging_ready log_path=%s", log_path)
    return logger


LOGGER = setup_logging()
