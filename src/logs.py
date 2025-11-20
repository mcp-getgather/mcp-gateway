import logging
from pathlib import Path
from typing import TYPE_CHECKING

import segment.analytics as analytics
import sentry_sdk
import yaml
from loguru import logger
from rich.logging import RichHandler
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

if TYPE_CHECKING:
    from loguru import HandlerConfig, Record

LOGGER_NAME = Path(__file__).parent.name  # Assume the parent directory name is the project name


def setup_logging(
    *,
    level: str = "INFO",
    logs_dir: Path | None = None,
    sentry_dsn: str | None = None,
    segment_write_key: str | None = None,
):
    _setup_logger(level, logs_dir)

    # setup sentry
    if sentry_dsn:
        logger.info("Initializing Sentry")
        sentry_sdk.init(
            dsn=sentry_dsn,
            _experiments={"enable_logs": True},
            integrations=[
                StarletteIntegration(transaction_style="url"),
                FastApiIntegration(transaction_style="url"),
                LoggingIntegration(level=logging.getLevelNamesMapping()[level]),
            ],
            send_default_pii=True,
        )
    else:
        logger.warning("No GATEWAY_SENTRY_DSN provided, Sentry is disabled")

    # setup segment
    if segment_write_key:
        logger.info("Initializing Segment")
        analytics.write_key = segment_write_key
    else:
        logger.warning("No SEGMENT_WRITE_KEY provided, Segment is disabled")
        analytics.write_key = "disabled"
        analytics.debug = False
        analytics.send = False


LOG_FILE_TOPICS = frozenset(["manager", "service"])


def _setup_logger(level: str, logs_dir: Path | None = None):
    logger.remove()

    rich_handler = RichHandler(rich_tracebacks=True, log_time_format="%X", markup=True)

    def _format_with_extra(record: "Record") -> str:
        message = record["message"]

        if record["extra"]:
            extra = yaml.dump(record["extra"], sort_keys=False, default_flow_style=False)
            message = f"{message}\n{extra.rstrip()}"

        return message

    handlers: list[HandlerConfig] = [
        {
            "sink": rich_handler,
            "format": _format_with_extra,
            "level": level,
            "backtrace": True,
            "diagnose": True,
        }
    ]
    if logs_dir:
        logfile = (logs_dir / f"containers.log").as_posix()

        def _filter_for_file(record: "Record") -> bool:
            return record["extra"].get("topic") in LOG_FILE_TOPICS

        handlers.append({
            "sink": logfile,
            "format": "{message}",
            "level": "INFO",
            "rotation": "100 MB",
            "retention": "30 days",
            "serialize": True,
            "enqueue": True,
            "backtrace": True,
            "diagnose": False,
            "filter": _filter_for_file,
        })

    logger.configure(handlers=handlers)

    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.handlers.clear()  # Remove existing handlers
        uvicorn_logger.addHandler(rich_handler)
        uvicorn_logger.propagate = False
