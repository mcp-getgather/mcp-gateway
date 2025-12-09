import functools
import inspect
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, ParamSpec, TypeVar, cast, overload

import logfire
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

from src.settings import settings

LOGGER_NAME = Path(__file__).parent.name  # Assume the parent directory name is the project name


def setup_logging():
    # setup logfire
    if settings.LOGFIRE_TOKEN:
        logfire.configure(
            service_name="mcp-gateway",
            send_to_logfire="if-token-present",
            token=settings.LOGFIRE_TOKEN,
            environment=settings.ENVIRONMENT,
            code_source=logfire.CodeSource(
                repository="https://github.com/mcp-getgather/mcp-gateway", revision="main"
            ),
            console=False,
            scrubbing=False,
        )

    _setup_logger(settings.LOG_LEVEL, settings.logs_dir, settings.VERBOSE)

    # setup sentry
    if settings.GATEWAY_SENTRY_DSN:
        logger.info("Initializing Sentry")
        sentry_sdk.init(
            dsn=settings.GATEWAY_SENTRY_DSN,
            _experiments={"enable_logs": True},
            integrations=[
                StarletteIntegration(transaction_style="url"),
                FastApiIntegration(transaction_style="url"),
                LoggingIntegration(level=logging.getLevelNamesMapping()[settings.LOG_LEVEL]),
            ],
            send_default_pii=True,
        )
    else:
        logger.warning("No GATEWAY_SENTRY_DSN provided, Sentry is disabled")

    # setup segment
    if settings.SEGMENT_WRITE_KEY:
        logger.info("Initializing Segment")
        analytics.write_key = settings.SEGMENT_WRITE_KEY
    else:
        logger.warning("No SEGMENT_WRITE_KEY provided, Segment is disabled")
        analytics.write_key = "disabled"
        analytics.debug = False
        analytics.send = False


LOG_FILE_TOPICS = frozenset(["manager", "service"])


def _setup_logger(level: str, logs_dir: Path | None = None, verbose: bool = False):
    logger.remove()

    rich_handler = RichHandler(rich_tracebacks=True, log_time_format="%X", markup=True)

    def _format_with_extra(record: "Record") -> str:
        message = record["message"]

        if record["extra"]:
            extra = yaml.dump(record["extra"], sort_keys=False, default_flow_style=False)
            extra_escaped = (
                extra.rstrip()
                .replace("{", "{{")
                .replace("}", "}}")
                .replace("<", r"\<")
                .replace(">", r"\>")
            )
            message = f"{message}\n{extra_escaped}"

        return message

    def _filter_decorator_logs(record: "Record") -> bool:
        """Filter out decorator logs unless in verbose mode."""
        if not verbose and record["extra"].get("decorator_log"):
            return False
        return True

    handlers: list[HandlerConfig] = [
        {
            "sink": rich_handler,
            "format": _format_with_extra,
            "level": level,
            "backtrace": True,
            "diagnose": True,
            "filter": _filter_decorator_logs,
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

    if settings.LOGFIRE_TOKEN:
        handlers.append(logfire.loguru_handler())

    logger.configure(handlers=handlers)

    # Override the loggers of external libraries to ensure consistent formatting
    for logger_name in (
        "uvicorn",
        "uvicorn.access",
        "uvicorn.error",
        "fastmcp",
        "fastmcp.fastmcp.server.auth.oauth_proxy",
        "fastmcp.fastmcp.server.auth.providers.github",
        "fastmcp.fastmcp.server.auth.providers.google",
    ):
        lib_logger = logging.getLogger(logger_name)
        lib_logger.handlers.clear()  # Remove existing handlers
        lib_logger.addHandler(rich_handler)
        lib_logger.propagate = False


def get_utcnow() -> datetime:
    """Return the current UTC time."""
    return datetime.now(timezone.utc)


def _format_args_kwargs(
    func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, str]:
    """Format function arguments for logging."""
    sig = inspect.signature(func)
    bound_args = sig.bind_partial(*args, **kwargs)
    bound_args.apply_defaults()

    formatted: dict[str, str] = {}
    for name, value in bound_args.arguments.items():
        # Truncate long values
        str_value = str(value)
        if len(str_value) > 200:
            str_value = str_value[:200] + "..."
        formatted[name] = str_value

    return formatted


P = ParamSpec("P")
R = TypeVar("R")


# Overloads for sync and async functions
@overload
def log_decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]: ...


@overload
def log_decorator(func: Callable[P, R]) -> Callable[P, R]: ...


def log_decorator(
    func: Callable[P, R] | Callable[P, Awaitable[R]],
) -> Callable[P, R] | Callable[P, Awaitable[R]]:
    """Wrap regular or coroutine function with extra logging.

    This method will record to Datadog start and end times for the
    decorated function. It will also log the exception to Sentry
    if one is raised.

    Usage example:

        @log_decorator
        def my_function(a, b):
            return a + b

        my_function(1, 2)
    """

    @functools.wraps(func)
    def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            start = get_utcnow()
            extra: dict[str, Any] = {"func": func.__name__, "decorator_log": True}

            # Add args and kwargs to extra
            try:
                extra["args"] = _format_args_kwargs(func, args, kwargs)
            except Exception:
                # If we can't format args, just skip it
                pass

            logger.debug(
                f"Starting {func.__name__}",
                extra=extra,
            )
            result = func(*args, **kwargs)
            duration = (get_utcnow() - start).total_seconds()
            extra["duration_sec"] = duration
            logger.debug(
                f"Finished {func.__name__}",
                extra=extra,
            )
            return cast(R, result)
        except Exception as e:
            with sentry_sdk.push_scope() as scope:
                scope.set_tag("func", func.__name__)

            error_extra: dict[str, Any] = {
                "func": func.__name__,
                "error": str(e),
                "decorator_log": True,
            }
            logger.exception(
                f"Exception raised in {func.__name__}",
                extra=error_extra,
            )

            # Re-raise the exception so it triggers Sentry
            raise

    @functools.wraps(func)
    async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        # NOTE: We may need to make the network calls asynchronous which is a lot more complex given loggers may not work well with async code

        extra: dict[str, Any] = {"func": func.__name__, "decorator_log": True}

        # Add args and kwargs to extra
        try:
            extra["args"] = _format_args_kwargs(func, args, kwargs)
        except Exception:
            # If we can't format args, just skip it
            pass

        try:
            start = get_utcnow()
            logger.debug(f"Starting {func.__name__}", extra=extra)

            # Await the asynchronous function
            result = await cast(Awaitable[R], func(*args, **kwargs))

            duration = (get_utcnow() - start).total_seconds()
            extra["duration_sec"] = duration
            logger.debug(f"Finished {func.__name__}", extra=extra)
            return result
        except Exception as e:
            with sentry_sdk.push_scope() as scope:
                scope.set_tag("func", func.__name__)

            async_error_extra: dict[str, Any] = {
                "func": func.__name__,
                "error": str(e),
                "decorator_log": True,
            }
            logger.exception(
                f"Exception raised in {func.__name__}",
                extra=async_error_extra,
            )

            # Re-raise the exception so it triggers Sentry
            raise

    if inspect.iscoroutinefunction(func):
        return async_wrapper  # type: ignore[return-value]
    else:
        return sync_wrapper  # type: ignore[return-value]
