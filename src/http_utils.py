from contextvars import ContextVar

from loguru import logger

incoming_headers_context: ContextVar[dict[str, str]] = ContextVar("incoming_headers", default={})


def get_server_origin() -> str:
    """
    Get the public origin from the incoming headers.
    If the server is behind a proxy (e.g., load balancer), the public origin is the origin of the proxy.
    """
    headers = incoming_headers_context.get()
    proto = headers.get("x-forwarded-proto") or "http"
    host = headers.get("x-forwarded-host") or headers.get("host")
    if not host:
        host = "localhost:9000"
        logger.warning("No host found in headers, using default localhost:9000")

    origin = f"{proto}://{host}"
    logger.info(f"Retrieved server origin for request", origin=origin)
    return origin
