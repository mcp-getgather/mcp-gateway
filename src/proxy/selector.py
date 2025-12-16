"""Validated proxy selection with hierarchical location fallback.

This module combines proxy configuration, location hierarchy, and IP validation
to select a working proxy configuration through intelligent fallback.
"""

from loguru import logger

from src.proxy.location_hierarchy import build_location_hierarchy, describe_location
from src.proxy.validation import validate_proxy_ip
from src.residential_proxy_sessions import (
    GetgatherProxies,
    GetgatherProxyConfig,
    Location,
    ProxyConfig,
    build_proxy_config,
)

logger = logger.bind(topic="proxy_selector")


async def select_and_validate_proxy(
    proxy_config: ProxyConfig,
    profile_id: str,
    location: Location | None,
    hierarchy_fields: list[str] | None = None,
) -> tuple[GetgatherProxies | None, str | None, Location | None]:
    """Select proxy and validate with hierarchical location fallback.

    Tries to build and validate proxy configurations using a hierarchy of
    location specifications, from most to least specific, until validation succeeds.

    Args:
        proxy_config: ProxyConfig instance with templates
        profile_id: Profile ID to use as session identifier
        location: Optional Location model from x-location-info header
        hierarchy_fields: Optional list of fields for hierarchy (from TOML config)

    Returns:
        Tuple of:
        - proxy_config: GetgatherProxies | None (validated config or None if all failed)
        - validated_ip: str | None (external IP if validation succeeded)
        - validated_location: Location | None (location level that succeeded)

    Example:
        >>> config = ProxyConfig(
        ...     proxy_name="my_proxy",
        ...     url="http://proxy.com:8889",
        ...     username_template="user-{session_id}-{location}",
        ...     password="secret"
        ... )
        >>> location = Location(country="us", state="california", city="los_angeles")
        >>> result, ip, loc = await select_and_validate_proxy(
        ...     config, "abc123", location, ["city", "state"]
        ... )
    """
    # Handle 'none' proxy type
    if proxy_config.proxy_name == "none":
        logger.info("Proxy type is 'none', skipping proxy")
        return None, None, None

    # If no location, try without location
    if not location:
        logger.info("No location provided, validating proxy without location")
        resolved = build_proxy_config(proxy_config, profile_id, None)
        if not resolved:
            logger.warning("Failed to build proxy config without location")
            return None, None, None

        success, ip = await validate_proxy_ip(
            resolved.server or "",
            resolved.username,
            resolved.password,
        )

        if success:
            logger.info(
                "✓ Proxy validated without location",
                ip=ip,
                proxy_name=proxy_config.proxy_name,
            )
            return _to_getgather_proxies(resolved, proxy_config.proxy_name), ip, None
        else:
            logger.error("✗ Proxy validation failed (no location)")
            return None, None, None

    # Build location hierarchy
    hierarchy = build_location_hierarchy(location, hierarchy_fields)

    if not hierarchy:
        logger.warning("Failed to build location hierarchy", location=location.model_dump())
        return None, None, None

    logger.info(
        f"Trying {len(hierarchy)} location levels for proxy validation",
        proxy_name=proxy_config.proxy_name,
        hierarchy_fields=hierarchy_fields,
        original_location=describe_location(location),
    )

    # Try each location in hierarchy
    for level, loc in enumerate(hierarchy, start=1):
        logger.info(
            f"Attempting level {level}/{len(hierarchy)}",
            location=describe_location(loc),
            level_details=loc.model_dump(exclude_none=True),
        )

        # Build proxy config with this location
        resolved = build_proxy_config(proxy_config, profile_id, loc)
        if not resolved:
            logger.warning(
                f"Failed to build proxy config at level {level}",
                location=describe_location(loc),
            )
            continue

        # Validate IP
        success, ip = await validate_proxy_ip(
            resolved.server or "",
            resolved.username,
            resolved.password,
        )

        if success:
            logger.info(
                f"✓ Proxy validated at level {level}/{len(hierarchy)}",
                location=describe_location(loc),
                ip=ip,
                proxy_name=proxy_config.proxy_name,
                validated_location=loc.model_dump(exclude_none=True),
            )
            return _to_getgather_proxies(resolved, proxy_config.proxy_name), ip, loc
        else:
            logger.warning(
                f"✗ Validation failed at level {level}/{len(hierarchy)}",
                location=describe_location(loc),
            )

    # All levels exhausted
    logger.error(
        "All location hierarchy levels failed validation",
        proxy_name=proxy_config.proxy_name,
        levels_tried=len(hierarchy),
        original_location=describe_location(location),
    )
    return None, None, None


def _to_getgather_proxies(resolved: ProxyConfig, proxy_name: str) -> GetgatherProxies:
    """Convert validated ProxyConfig to GetgatherProxies format.

    Args:
        resolved: Resolved ProxyConfig with server, username, password
        proxy_name: Original proxy name from TOML config

    Returns:
        GetgatherProxies object ready to write to container

    Example:
        >>> resolved = ProxyConfig(
        ...     proxy_name="my_proxy",
        ...     server="http://proxy.com:8889",
        ...     username="user-abc-country-us",
        ...     password="secret"
        ... )
        >>> proxies = _to_getgather_proxies(resolved, "my_proxy")
        >>> proxies.proxies["proxy-0"].server
        'http://proxy.com:8889'
    """
    getgather_proxy = GetgatherProxyConfig(
        proxy_type=proxy_name,
        server=resolved.server or "",
        base_username=resolved.username,
        password=resolved.password,
        url=resolved.url,
    )

    return GetgatherProxies(proxies={"proxy-0": getgather_proxy})
