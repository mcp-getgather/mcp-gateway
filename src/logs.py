import logging
from pathlib import Path
from typing import Any

import segment.analytics as analytics
import sentry_sdk
from rich.logging import RichHandler
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

LOGGER_NAME = Path(__file__).parent.name  # Assume the parent directory name is the project name


def setup_logging(
    *, level: str = "INFO", sentry_dsn: str | None = None, segment_write_key: str | None = None
):
    rich_handler = RichHandler(
        rich_tracebacks=True,
        markup=True,
        show_time=True,
        show_level=True,
        show_path=False,
    )
    rich_handler.setFormatter(StructuredFormatter())

    # reconfigure uvicorn.error, uvicorn.access and fastapi
    for name in ["uvicorn.error", "uvicorn.access", "fastapi"]:
        _logger = logging.getLogger(name)
        _logger.handlers.clear()
        _logger.addHandler(rich_handler)
        _logger.propagate = False

    # Configure the root logger to INFO level, and app logger to the level
    # specified in the .env
    logging.basicConfig(level="INFO", format="%(message)s", datefmt="[%X]", handlers=[rich_handler])
    logging.getLogger(LOGGER_NAME).setLevel(level)

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

    if segment_write_key:
        logger.info("Initializing Segment")
        analytics.write_key = segment_write_key
    else:
        logger.warning("No SEGMENT_WRITE_KEY provided, Segment is disabled")
        analytics.write_key = "disabled"
        analytics.debug = False
        analytics.send = False


logger = logging.getLogger(LOGGER_NAME)


class StructuredFormatter(logging.Formatter):
    """Custom formatter that handles extra fields in log records."""

    def format(self, record: logging.LogRecord) -> str:
        # Get the base formatted message
        base_msg = super().format(record)

        # Extract extra fields (anything not in the standard LogRecord attributes)
        # Include all possible LogRecord attributes to avoid conflicts
        standard_attrs = {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "getMessage",
            "exc_info",
            "exc_text",
            "stack_info",
            "message",
            "asctime",
            "taskName",
        }

        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in standard_attrs and not k.startswith("_")
        }

        if extras:
            # Color code the extras section based on log level
            level_colors = {
                "DEBUG": "dim blue",
                "INFO": "cyan",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "bold red",
            }
            color = level_colors.get(record.levelname, "white")

            # Always use multi-line format for better readability
            extras_lines: list[str] = []
            for key, value in extras.items():
                value_any: Any = value  # Type annotation for unknown LogRecord fields

                # Handle complex values like dicts
                if isinstance(value_any, dict):
                    if len(value_any) <= 3:  # Small dicts inline  # type: ignore[arg-type]
                        value_str = str(value_any)  # type: ignore[arg-type]
                    else:  # Large dicts formatted
                        dict_items = [f"{k}={v}" for k, v in value_any.items()]  # type: ignore[misc]
                        value_str = "{\n      " + ",\n      ".join(dict_items) + "\n    }"
                elif isinstance(value_any, (list, tuple)) and len(value_any) > 3:  # type: ignore[arg-type]
                    # Format long lists/tuples nicely
                    items = [str(item) for item in value_any]  # type: ignore[misc]
                    value_str = "[\n      " + ",\n      ".join(items) + "\n    }"
                else:
                    value_str = str(value_any)  # type: ignore[arg-type]

                extras_lines.append(f"[{color}]    {key}:[/{color}] {value_str}")

            extras_str = "\n" + "\n".join(extras_lines) + "\n"
            return f"{base_msg}\n{extras_str}"

        return base_msg


# The StructuredFormatter handles extra fields automatically when using logger.info(msg, extra={...})
