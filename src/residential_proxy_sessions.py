import re
import tomllib
from typing import Any
from urllib.parse import urlparse

from loguru import logger
from pydantic import BaseModel

logger = logger.bind(topic="residential_proxy")


class ProxyConfig(BaseModel):
    """Proxy configuration with support for templates and URL parsing."""

    proxy_type: str = "none"
    url: str | None = None
    url_template: str | None = None
    username_template: str | None = None
    base_username: str | None = None
    password: str | None = None

    @property
    def server(self) -> str | None:
        """Extract server (host:port) from URL."""
        if not self.url:
            return None

        url_with_scheme = self.url if "://" in self.url else f"http://{self.url}"
        parsed = urlparse(url_with_scheme)

        # Handle URLs with credentials like http://user:pass@host:port
        if parsed.hostname:
            port = f":{parsed.port}" if parsed.port else ""
            return f"{parsed.hostname}{port}"

        return None

    @property
    def masked_url(self) -> str:
        """Return URL with password masked for logging."""
        if not self.url:
            return ""

        if self.password and self.password in self.url:
            return self.url.replace(self.password, "***")

        return self.url

    def dump(self):
        """Serialize for logging."""
        return self.model_dump(exclude_none=True, mode="json")


def build_proxy_config(
    proxy_config: ProxyConfig,
    profile_id: str,
    location: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    """Build proxy configuration dict with dynamic parameter replacement.

    Args:
        proxy_config: ProxyConfig instance to build from
        profile_id: Profile ID to use as session identifier
        location: Optional location dict from x-location header with keys:
                 country, state, city, postal_code

    Returns:
        dict: Proxy configuration with server, username, password
        None: If no server configured or proxy type is 'none'
    """
    # Handle 'none' proxy type - no proxy
    if proxy_config.proxy_type == "none":
        logger.info("Proxy type is 'none', skipping proxy")
        return None

    # Extract values for template replacement
    values = _extract_values(profile_id, location)

    # Format 1: url_template (full URL with credentials and dynamic params)
    if proxy_config.url_template:
        full_url = _build_params(proxy_config.url_template, values)
        if not full_url:
            logger.warning("url_template resulted in empty string, skipping proxy")
            return None

        # Parse the built URL to extract components
        temp_config = ProxyConfig(url=full_url)
        if not temp_config.server:
            logger.warning(f"Failed to parse url_template result: {temp_config.masked_url}")
            return None

        result = {
            "server": temp_config.server,
        }
        if temp_config.base_username:
            result["username"] = temp_config.base_username
        if temp_config.password:
            result["password"] = temp_config.password

        logger.info(
            "Built proxy config from url_template - "
            f"server: {temp_config.server}, "
            f"username: {temp_config.base_username}, "
            f"has_password: {bool(temp_config.password)}"
        )
        return result

    # Format 2: Separate components (url + username_template + password)
    if not proxy_config.server:
        logger.info("No proxy server configured, skipping proxy")
        return None

    # Build username from base + template
    username = None

    # Priority: username_template > base_username
    if proxy_config.username_template:
        # Build from template (may not need base_username)
        params = _build_params(proxy_config.username_template, values)
        if params:
            username = params
    elif proxy_config.base_username:
        # Use base username if no template
        username = proxy_config.base_username

    if username:
        logger.info(f"Built proxy username: {username}")

    result = {
        "server": proxy_config.server,
    }
    if username:
        result["username"] = username
    if proxy_config.password:
        result["password"] = proxy_config.password

    logger.info(
        f"Built proxy config - server: {proxy_config.server}, "
        f"username: {username}, "
        f"has_password: {bool(proxy_config.password)}"
    )
    return result


def _extract_values(profile_id: str, location: dict[str, Any] | None) -> dict[str, Any]:
    """Extract replacement values from location dict.

    Args:
        profile_id: Profile ID to use as session identifier
        location: Optional location dict from x-location header

    Returns:
        dict: Mapping of placeholder names to values
    """
    values = {
        "session_id": profile_id,  # Use profile_id as session identifier
    }

    if not location:
        return values

    country = None
    if location.get("country"):
        country = location["country"].lower()
        values["country"] = country

    # Only include state for US requests (state-us_{state} format)
    if location.get("state") and country == "us":
        values["state"] = location["state"].lower().replace(" ", "_")

    if location.get("city"):
        values["city"] = location["city"].lower().replace(" ", "_")
        # city_compacted: removes dashes, underscores, and spaces
        city_compacted = location["city"].lower().replace("-", "").replace("_", "").replace(" ", "")
        values["city_compacted"] = city_compacted
    if location.get("postal_code"):
        values["postal_code"] = location["postal_code"]

    return values


def _build_params(template: str, values: dict[str, Any]) -> str:
    """Build params by only including segments with actual values.

    Splits template by placeholders and only joins segments that have
    values.

    Examples:
    - Template: 'cc-{country}-city-{city}', {'country': 'us'} -> 'cc-us'
    - Template: 'cc-{country}-city-{city}', values: {} -> ''
    - Template: 'state-us_{state}', {'state': 'ca'} -> 'state-us_ca'
    - Template: 'state-us_{state}', values: {} -> ''

    Args:
        template: Template string with {placeholders}
        values: Mapping of placeholder names to values

    Returns:
        str: Params with only segments that have values, or empty string
    """
    # Split by placeholders to get segments
    # We'll rebuild by only including segments where we have values
    parts: list[str] = []
    current = template

    # Find all placeholders in order
    placeholders: list[str] = re.findall(r"\{([^}]+)\}", template)

    for placeholder in placeholders:
        # Split on this placeholder
        before, _, after = current.partition(f"{{{placeholder}}}")

        # If we have a value for this placeholder, include the segment
        if placeholder in values and values[placeholder] is not None:
            parts.append(before + str(values[placeholder]))

        current = after

    # Add any remaining text
    if current:
        parts.append(current)

    result = "".join(parts)

    # Clean up separators at start/end
    result = result.strip("-_")

    return result


def parse_proxies_toml(toml_str: str) -> dict[str, ProxyConfig]:
    """Parse TOML-format proxy configuration string.

    Args:
        toml_str: TOML configuration string

    Returns:
        dict: Mapping of proxy names to ProxyConfig instances

    Example TOML:
        [proxy-0]
        type = "oxylabs_direct"
        url = "pr.oxylabs.io:7777"
        username_template = "customer-{session_id}"
        password = "secret123"
    """
    try:
        config_dict = tomllib.loads(toml_str)
        proxies: dict[str, ProxyConfig] = {}

        for name, proxy_data in config_dict.items():
            proxies[name] = ProxyConfig(
                proxy_type=proxy_data.get("type", "none"),  # type: ignore[arg-type]
                url=proxy_data.get("url"),  # type: ignore[arg-type]
                url_template=proxy_data.get("url_template"),  # type: ignore[arg-type]
                username_template=proxy_data.get("username_template"),  # type: ignore[arg-type]
                base_username=proxy_data.get("base_username"),  # type: ignore[arg-type]
                password=proxy_data.get("password"),  # type: ignore[arg-type]
            )

        logger.info(f"Parsed {len(proxies)} proxies from TOML config")
        return proxies

    except tomllib.TOMLDecodeError as e:
        logger.error(f"Failed to parse TOML config: {e}")
        return {}


def get_proxy_config(toml_config: str, proxy_name: str = "proxy-0") -> ProxyConfig | None:
    """Get a specific proxy configuration from TOML config.

    Args:
        toml_config: TOML configuration string
        proxy_name: Name of proxy to retrieve (default: proxy-0)

    Returns:
        ProxyConfig instance or None if not found
    """
    proxies = parse_proxies_toml(toml_config)
    return proxies.get(proxy_name)


def select_and_build_proxy_config(
    toml_config: str,
    proxy_name: str | None,
    default_proxy_name: str | None,
    profile_id: str,
    location: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Select a proxy from TOML config and build its configuration.

    Selection priority:
    1. proxy_name (from x-proxy-type header)
    2. default_proxy_name (from settings.DEFAULT_PROXY_TYPE)
    3. First available proxy in config

    Args:
        toml_config: TOML configuration string with all available proxies
        proxy_name: Proxy name from x-proxy-type header (e.g., "proxy-1")
        default_proxy_name: Default proxy name from settings (e.g., "proxy-0")
        profile_id: Profile ID to use as session identifier
        location: Optional location dict from x-location-info header

    Returns:
        dict: Single proxy config in format: {"proxy-0": {...}} or None if no proxy
    """
    if not toml_config:
        logger.info("No TOML config provided, skipping proxy selection")
        return None

    # Parse all available proxies
    all_proxies = parse_proxies_toml(toml_config)
    if not all_proxies:
        logger.info("No proxies found in TOML config")
        return None

    # Determine which proxy to use (priority order)
    target_name = proxy_name or default_proxy_name or None

    selected_proxy_name = None
    selected_proxy_config = None

    if target_name and target_name in all_proxies:
        selected_proxy_name = target_name
        selected_proxy_config = all_proxies[target_name]
        source = "x-proxy-type header" if proxy_name else "DEFAULT_PROXY_TYPE setting"
        logger.info(
            f"Selected proxy from {source}",
            proxy_name=selected_proxy_name,
            proxy_type=selected_proxy_config.proxy_type,
        )
    elif target_name:
        logger.warning(f"Proxy '{target_name}' not found in config, will use first proxy")

    # If no target name or not found, use the first proxy
    if not selected_proxy_config:
        selected_proxy_name = next(iter(all_proxies.keys()))
        selected_proxy_config = all_proxies[selected_proxy_name]
        logger.info(
            f"Using first available proxy",
            proxy_name=selected_proxy_name,
            proxy_type=selected_proxy_config.proxy_type,
        )

    # Build the proxy config with location info
    resolved_proxy = build_proxy_config(selected_proxy_config, profile_id, location)

    if not resolved_proxy:
        logger.info("Proxy config resolved to None (likely type='none')")
        return None

    # Return as single proxy named "proxy-0"
    result = {
        "proxy-0": {
            "type": selected_proxy_config.proxy_type,
            "server": resolved_proxy["server"],
            "username": resolved_proxy.get("username", ""),
            "password": resolved_proxy.get("password", ""),
        }
    }

    logger.info(
        f"Built proxy config for container",
        original_name=selected_proxy_name,
        proxy_type=selected_proxy_config.proxy_type,
        server=resolved_proxy["server"],
        has_username=bool(resolved_proxy.get("username")),
        has_password=bool(resolved_proxy.get("password")),
    )

    return result
