"""IP validation module for proxy testing.

This module provides functionality to validate proxy configurations by testing
if they can successfully fetch an external IP address.
"""

import asyncio
import re
from typing import Optional
from urllib.parse import urlparse

import httpx
from loguru import logger

logger = logger.bind(topic="proxy_validation")

IP_CHECK_URL = "http://checkip.amazonaws.com"
MAX_IP_CHECK_RETRIES = 3  # Per location
VALIDATION_TIMEOUT = 10  # seconds


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
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Validate proxy by fetching external IP.

    Makes a request through the proxy to checkip.amazonaws.com to verify:
    1. Proxy connection works
    2. We can get a valid IP address through it

    Retries up to MAX_IP_CHECK_RETRIES times per proxy configuration.

    Args:
        proxy_url: Proxy server URL (e.g., "http://proxy.example.com:8889")
        username: Optional proxy username (if not in URL)
        password: Optional proxy password (if not in URL)

    Returns:
        (success: bool, ip_address: Optional[str])

    Example:
        >>> success, ip = await validate_proxy_ip("http://proxy.com:8889", "user", "pass")
        >>> if success:
        ...     print(f"Proxy works! External IP: {ip}")
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

                ip = response.text.strip()

                # Validate it's actually an IP address (IPv4 or IPv6)
                if re.match(r'^(\d{1,3}\.){3}\d{1,3}$', ip) or ':' in ip:
                    logger.info(
                        f"✓ Proxy validation succeeded",
                        attempt=attempt,
                        ip=ip,
                        proxy_url=mask_credentials(proxy_url),
                    )
                    return True, ip
                else:
                    logger.warning(
                        f"Invalid IP format received",
                        ip=ip,
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

    logger.error(
        f"✗ Proxy validation failed after {MAX_IP_CHECK_RETRIES} attempts",
        proxy_url=mask_credentials(proxy_url),
    )
    return False, None
