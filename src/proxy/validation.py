"""IP validation module for proxy testing.

This module provides functionality to validate proxy configurations by testing
if they can successfully fetch an external IP address.
"""

import asyncio
from urllib.parse import urlparse

import httpx
from loguru import logger
from pydantic import BaseModel, IPvAnyAddress

logger = logger.bind(topic="proxy_validation")

IP_CHECK_URL = "http://checkip.amazonaws.com"
MAX_IP_CHECK_RETRIES = 3  # Per location
VALIDATION_TIMEOUT = 10  # seconds


class ValidationResult(BaseModel):
    """Result of proxy IP validation.

    Attributes:
        success: Whether validation succeeded
        ip_address: The validated IP address (None if validation failed)
        error: Error message if validation failed (None if succeeded)
    """

    success: bool
    ip_address: IPvAnyAddress | None = None
    error: str | None = None


def mask_credentials(url: str) -> str:
    """Mask credentials in URL for safe logging.

    Args:
        url: URL potentially containing credentials

    Returns:
        URL with password masked as ****

    Example:
        >>> mask_credentials("http://user:pass@proxy.com:8889")
        'http://user:****@proxy.com:8889'
    """
    try:
        parsed = urlparse(url)
        if parsed.username and parsed.password:
            masked_netloc = f"{parsed.username}:****@{parsed.hostname}"
            if parsed.port:
                masked_netloc += f":{parsed.port}"
            return parsed._replace(netloc=masked_netloc).geturl()
        return url
    except Exception:
        # If parsing fails, return as-is (better than crashing)
        return url


async def validate_proxy_ip(
    proxy_url: str,
    *,
    username: str | None = None,
    password: str | None = None,
) -> ValidationResult:
    """Validate proxy by fetching external IP.

    Makes a request through the proxy to checkip.amazonaws.com to verify:
    1. Proxy connection works
    2. We can get a valid IP address through it

    Retries up to MAX_IP_CHECK_RETRIES times per proxy configuration.

    Args:
        proxy_url: Proxy server URL (e.g., "http://proxy.example.com:8889")
        username: Optional proxy username (if not in URL) - keyword-only
        password: Optional proxy password (if not in URL) - keyword-only

    Returns:
        ValidationResult with success status, IP address, and optional error message

    Example:
        >>> result = await validate_proxy_ip("http://proxy.com:8889", username="user", password="pass")
        >>> if result.success:
        ...     print(f"Proxy works! External IP: {result.ip_address}")
        ... else:
        ...     print(f"Validation failed: {result.error}")
    """
    auth = None
    if username and password:
        auth = httpx.BasicAuth(username, password)

    for attempt in range(1, MAX_IP_CHECK_RETRIES + 1):
        try:
            async with httpx.AsyncClient(
                proxy=proxy_url,
                auth=auth,
                timeout=VALIDATION_TIMEOUT,
            ) as client:
                logger.debug(
                    f"Validating proxy (attempt {attempt}/{MAX_IP_CHECK_RETRIES})",
                    proxy_url=mask_credentials(proxy_url),
                    has_auth=bool(auth),
                )

                response = await client.get(IP_CHECK_URL)
                response.raise_for_status()

                ip_str = response.text.strip()

                # Validate it's actually an IP address using Pydantic
                try:
                    # Use Pydantic's validation by creating a temporary model
                    result = ValidationResult(success=True, ip_address=ip_str)  # type: ignore[arg-type]
                    logger.info(
                        f"✓ Proxy validation succeeded",
                        attempt=attempt,
                        ip=str(result.ip_address),
                        proxy_url=mask_credentials(proxy_url),
                    )
                    return result
                except ValueError as e:
                    logger.warning(
                        f"Invalid IP format received",
                        ip=ip_str,
                        error=str(e),
                        proxy_url=mask_credentials(proxy_url),
                    )

        except httpx.TimeoutException as e:
            logger.warning(
                f"Proxy validation timeout",
                attempt=attempt,
                error=str(e),
                proxy_url=mask_credentials(proxy_url),
            )
        except httpx.ProxyError as e:
            logger.warning(
                f"Proxy connection error",
                attempt=attempt,
                error=str(e),
                proxy_url=mask_credentials(proxy_url),
            )
        except httpx.HTTPStatusError as e:
            logger.warning(
                f"HTTP error during validation",
                attempt=attempt,
                status_code=e.response.status_code,
                error=str(e),
                proxy_url=mask_credentials(proxy_url),
            )
        except Exception as e:
            logger.warning(
                f"Unexpected error during proxy validation",
                attempt=attempt,
                error=str(e),
                error_type=type(e).__name__,
                proxy_url=mask_credentials(proxy_url),
            )

        # Brief delay between retries (except on last attempt)
        if attempt < MAX_IP_CHECK_RETRIES:
            await asyncio.sleep(0.5)

    error_msg = f"Proxy validation failed after {MAX_IP_CHECK_RETRIES} attempts"
    logger.error(
        f"✗ {error_msg}",
        proxy_url=mask_credentials(proxy_url),
    )
    return ValidationResult(success=False, error=error_msg)
