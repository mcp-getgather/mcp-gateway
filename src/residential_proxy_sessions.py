import re
import tomllib
from typing import Any, Literal, TypeAlias
from urllib.parse import urlparse

from loguru import logger
from pydantic import BaseModel

logger = logger.bind(topic="residential_proxy")


PROXY_NUMBER: TypeAlias = Literal[
    "proxy-0",
    "proxy-1",
    "proxy-2",
    "proxy-3",
    "proxy-4",
    "proxy-5",
    "proxy-6",
    "proxy-7",
    "proxy-8",
    "proxy-9",
]


class Location(BaseModel):
    """Location information for proxy configuration.

    Typically comes from x-location-info or x-location headers.
    """

    country: str | None = None
    state: str | None = None
    city: str | None = None
    city_compacted: str | None = None
    postal_code: str | None = None

    def model_post_init(self, __context: Any) -> None:
        """Compute city_compacted from city."""
        if self.city and not self.city_compacted:
            # Remove dashes, underscores, and spaces
            self.city_compacted = (
                self.city.lower().replace("-", "").replace("_", "").replace(" ", "")
            )

    def to_template_values(self) -> dict[str, Any]:
        """Convert to dict for template replacement.

        Returns normalized values suitable for template placeholders.
        """
        values: dict[str, Any] = {}

        if self.country:
            values["country"] = self.country.lower()
            # Only include state for US requests
            if self.state and self.country.lower() == "us":
                values["state"] = self.state.lower().replace(" ", "_")

        if self.city:
            values["city"] = self.city.lower().replace(" ", "_")
            if self.city_compacted:
                values["city_compacted"] = self.city_compacted

        if self.postal_code:
            values["postal_code"] = self.postal_code

        return values


class GetgatherProxyConfig(BaseModel):
    """Getgather proxy configuration model."""

    proxy_type: str
    server: str
    base_username: str | None = None
    password: str | None = None
    url: str | None = None

    def dump(self):
        """Serialize for logging."""
        return self.model_dump(exclude_none=True, mode="json")


class GetgatherProxies(BaseModel):
    proxies: dict[PROXY_NUMBER, GetgatherProxyConfig]

    def dump(self):
        """Serialize for logging and YAML export."""
        return {"proxies": {number: config.dump() for number, config in self.proxies.items()}}


class ProxyConfig(BaseModel):
    """Proxy configuration with support for templates and URL parsing."""

    proxy_number: PROXY_NUMBER = "proxy-0"
    proxy_name: str = ""
    url: str | None = None
    url_template: str | None = None
    username_template: str | None = None
    username: str | None = None
    password: str | None = None
    server: str | None = None
    masked_url: str | None = None
    hierarchy_fields: list[str] | None = None  # e.g., ["postal_code", "city", "state"] or ["city+state", "city"]

    def model_post_init(self, __context: Any) -> None:
        # either simple url or instantiated url_template
        if self.url:
            self._parse_url(self.url)
        else:
            if self.server:
                parsed = urlparse(self.server)
                self.url = f"{parsed.scheme}://{self.username}:{self.password}@{parsed.hostname}:{parsed.port}"

    def _parse_url(self, url: str) -> None:
        """Parse URL to extract base username, password, and server.

        Args:
            url: Full URL with credentials (e.g., 'user:pass@host:port' or 'http://user:pass@host:port')
        """
        parsed = urlparse(url)
        if not parsed.scheme:
            parsed = urlparse(url, scheme="http")

        self.masked_url = parsed._replace(
            netloc=f"{parsed.username}:****@{parsed.hostname}"
        ).geturl()

        self.username = parsed.username or None
        self.password = parsed.password or None

        # server is needed downstream as a standard way to specify proxy
        if parsed.hostname:
            scheme = parsed.scheme or "http"
            port = f":{parsed.port}" if parsed.port else ""
            self.server = f"{scheme}://{parsed.hostname}{port}"
        else:
            logger.warning(f"Could not parse hostname from URL: {self.masked_url}")
            self.server = url

    def dump(self):
        """Serialize for logging."""
        return self.model_dump(exclude_none=True, mode="json")


def build_proxy_config(
    proxy_config: ProxyConfig,
    profile_id: str,
    location: Location | None = None,
) -> ProxyConfig | None:
    """Build proxy configuration with dynamic parameter replacement.

    Args:
        proxy_config: ProxyConfig instance to build from
        profile_id: Profile ID to use as session identifier
        location: Optional Location model from x-location header

    Returns:
        ProxyConfig instance or
        None: If no server configured or proxy type is 'none'
    """
    # Handle 'none' proxy type - no proxy
    if proxy_config.proxy_name == "none":
        logger.info("Proxy type is 'none', skipping proxy")
        return None

    # Format 1: url_template (full URL with credentials and dynamic params)
    if proxy_config.url_template:
        full_url = _build_params(proxy_config.url_template, profile_id, location)
        logger.debug(f"Built full proxy URL from url_template: {full_url}")
        if not full_url:
            logger.warning("url_template resulted in empty string, skipping proxy")
            return None

        # Parse the built URL to extract components
        result_config = ProxyConfig(proxy_name=proxy_config.proxy_name, url=full_url)
        if not result_config.server:
            logger.warning(f"Failed to parse url_template result: {result_config.masked_url}")
            return None

        logger.info(
            "Built proxy config from url_template - "
            f"server: {result_config.server}, "
            f"username: {result_config.username}, "
            f"has_password: {bool(result_config.password)}"
        )
        return result_config

    # Format 2: Separate components (url + username_template + password)
    if not proxy_config.server:
        logger.info("No proxy server configured, skipping proxy")
        return None

    # Build username from base + template
    username = None

    # Priority: username_template > username
    if proxy_config.username_template:
        # Build from template (may not need username)
        params = _build_params(proxy_config.username_template, profile_id, location)
        if params:
            username = params
    elif proxy_config.username:
        # Use base username if no template
        username = proxy_config.username

    if username:
        logger.info(f"Built proxy username: {username}")

    result_config = ProxyConfig(
        proxy_name=proxy_config.proxy_name,
        server=proxy_config.server,
        username=username,
        password=proxy_config.password,
    )

    logger.info(
        f"Built proxy config - server: {proxy_config.server}, "
        f"username: {username}, "
        f"has_password: {bool(proxy_config.password)}"
    )
    return result_config


def _build_params(template: str, profile_id: str, location: Location | None) -> str:
    """Build params by only including segments with actual values.

    Splits template by placeholders and only joins segments that have
    values from profile_id and location.

    Examples:
    - Template: 'customer-{session_id}', profile_id='abc'
      -> 'customer-abc'
    - Template: 'cc-{country}-city-{city}', location(country='us')
      -> 'cc-us'
    - Template: 'state-us_{state}', location(country='us', state='ca')
      -> 'state-us_ca'

    Args:
        template: Template string with {placeholders}
        profile_id: Profile ID for session_id placeholder
        location: Optional Location model with geo data

    Returns:
        str: Params with only segments that have values, or empty string
    """
    # Build values dict from profile_id and location
    values: dict[str, Any] = {"session_id": profile_id}
    if location:
        values.update(location.to_template_values())

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
        name = "oxylabs_direct"
        url = "pr.oxylabs.io:7777"
        username_template = "customer-{session_id}"
        password = "secret123"
    """
    try:
        config_dict = tomllib.loads(toml_str)
        proxies: dict[str, ProxyConfig] = {}

        for name, proxy_data in config_dict.items():
            proxies[name] = ProxyConfig(
                proxy_name=proxy_data.get("name", "none"),  # type: ignore[arg-type]
                url=proxy_data.get("url"),  # type: ignore[arg-type]
                url_template=proxy_data.get("url_template"),  # type: ignore[arg-type]
                username_template=proxy_data.get("username_template"),  # type: ignore[arg-type]
                username=proxy_data.get("username"),  # type: ignore[arg-type]
                password=proxy_data.get("password"),  # type: ignore[arg-type]
                hierarchy_fields=proxy_data.get("hierarchy_fields"),  # type: ignore[arg-type]
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
    proxy_number: str | None,
    default_proxy_number: str | None,
    profile_id: str,
    location: Location | None = None,
) -> GetgatherProxies | None:
    """Select a proxy from TOML config and build its configuration.

    Selection priority:
    1. proxy_number (from x-proxy-type header)
    2. default_proxy_number (from settings.DEFAULT_PROXY_TYPE)
    3. First available proxy in config

    Args:
        toml_config: TOML configuration string with all available proxies
        proxy_number: Proxy number from x-proxy-type header (e.g., "proxy-1")
        default_proxy_number: Default proxy number from settings (e.g., "proxy-0")
        profile_id: Profile ID to use as session identifier
        location: Optional Location model from x-location-info header

    Returns:
        GetgatherProxies: Proxy configuration or None if no proxy
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
    target_number = proxy_number or default_proxy_number or None

    selected_proxy_config = None

    if target_number and target_number in all_proxies:
        selected_proxy_config = all_proxies[target_number]
        source = "x-proxy-type header" if proxy_number else "DEFAULT_PROXY_TYPE setting"
        logger.info(
            f"Selected proxy from {source}",
            proxy_number=target_number,
            proxy_name=selected_proxy_config.proxy_name,
        )
    elif target_number:
        logger.warning(f"Proxy '{target_number}' not found in config, will use first proxy")

    # If no target name or not found, use the first proxy
    if not selected_proxy_config:
        selected_proxy_number = next(iter(all_proxies.keys()))
        selected_proxy_config = all_proxies[selected_proxy_number]
        logger.info(
            "Using first available proxy",
            proxy_number=selected_proxy_number,
            proxy_name=selected_proxy_config.proxy_name,
        )

    # Build the proxy config with location info
    resolved_proxy = build_proxy_config(selected_proxy_config, profile_id, location)

    if not resolved_proxy:
        logger.info("Proxy config resolved to None (likely name='none')")
        return None

    # Build GetgatherProxies model with single proxy named "proxy-0"
    getgather_proxy = GetgatherProxyConfig(
        proxy_type=selected_proxy_config.proxy_name,
        server=resolved_proxy.server or "",
        base_username=resolved_proxy.username,
        password=resolved_proxy.password,
        url=resolved_proxy.url,
    )

    result = GetgatherProxies(proxies={"proxy-0": getgather_proxy})

    logger.info(
        "Built proxy config for container",
        original_number=selected_proxy_config.proxy_number,
        proxy_name=selected_proxy_config.proxy_name,
        server=resolved_proxy.server,
        has_username=bool(resolved_proxy.username),
        has_password=bool(resolved_proxy.password),
    )

    return result
