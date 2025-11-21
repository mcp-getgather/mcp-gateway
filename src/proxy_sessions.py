"""
Proxy session management using hostname as session ID.
Stores proxy configuration per container hostname with OxyLabs formatting.
"""
import asyncio
import base64
from typing import Dict, Optional

import aiohttp

from src.logs import logger
from src.settings import settings

# Simple in-memory storage: hostname -> proxy location
_proxy_sessions: Dict[str, str] = {}

DEFAULT_COUNTRY = "us"
DEFAULT_STATE = "california"

# Proxy validation constants
IP_CHECK_URL = "https://checkip.amazonaws.com"
IP_CHECK_TIMEOUT = 30

# Valid US states for validation
VALID_US_STATES = {
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new_hampshire",
    "new_jersey",
    "new_mexico",
    "new_york",
    "north_carolina",
    "north_dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode_island",
    "south_carolina",
    "south_dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west_virginia",
    "wisconsin",
    "wyoming",
}


def get_proxy_location_for_hostname(hostname: str) -> Optional[str]:
    """Get stored proxy location for a hostname session."""
    return _proxy_sessions.get(hostname)


def set_proxy_location_for_hostname(hostname: str, location: str) -> None:
    """Store proxy location for a hostname session."""
    existing = _proxy_sessions.get(hostname)
    if existing != location:
        _proxy_sessions[hostname] = location
        logger.info(f"Updated proxy session for {hostname}: location={location}")
    else:
        logger.debug(f"Proxy location unchanged for {hostname}: {location}")


def clear_proxy_session(hostname: str) -> None:
    """Clear proxy session when container is released."""
    if hostname in _proxy_sessions:
        location = _proxy_sessions.pop(hostname)
        logger.info(f"Cleared proxy session for {hostname}: {location}")


def get_proxy_env_for_hostname(hostname: str) -> Dict[str, str]:
    """Get proxy environment variables for container creation/update."""
    location = get_proxy_location_for_hostname(hostname)
    if not location:
        return {}

    # Format for OxyLabs and return env vars for container
    oxylabs_config = format_oxylabs_proxy_config(location)

    # Return env vars that will be passed to container
    return {"PROXY_LOCATION": location, **oxylabs_config}


def parse_hierarchical_location(location_str: str | None) -> dict[str, str]:
    """
    Parse hierarchical location string into components.

    Supports formats like:
    - postalcode_90210_city_los_angeles_state_california_country_us
    - city_los_angeles_state_california_country_us
    - state_california_country_us
    - country_us

    Returns dict with keys: postal_code, city, state, country
    """
    location: dict[str, str] = {}

    if not location_str:
        return location

    # Parse hierarchical format
    parts = location_str.split("_")
    valid_keys = {"postalcode", "city", "state", "country"}
    i = 0

    while i < len(parts):
        key = parts[i].lower()

        if key not in valid_keys:
            i += 1
            continue

        # Find the next valid key to determine where this value ends
        value_parts = []
        j = i + 1

        while j < len(parts) and parts[j].lower() not in valid_keys:
            value_parts.append(parts[j].lower())
            j += 1

        if value_parts:
            value = "_".join(value_parts)

            if key == "postalcode":
                location["postal_code"] = value
            elif key == "city":
                location["city"] = value
            elif key == "state":
                location["state"] = value
            elif key == "country":
                location["country"] = value

        i = j

    return location


def validate_and_apply_defaults(location: dict[str, str]) -> dict[str, str]:
    """
    Validate location components and apply defaults for invalid inputs.

    - Country: Must be 2-char ISO code and alphabetic
    - State: Must be in VALID_US_STATES set (for US only)
    - Non-US countries: Remove postal_code and state
    """
    validated = location.copy()

    country = validated.get("country", "").lower()
    if not country or len(country) != 2 or not country.isalpha():
        logger.warning(f"Invalid country '{country}', using default: {DEFAULT_COUNTRY}")
        validated["country"] = DEFAULT_COUNTRY
        validated["state"] = DEFAULT_STATE
        country = DEFAULT_COUNTRY

    # For non-US countries, remove postal code and state
    if country != "us":
        if "postal_code" in validated:
            logger.warning(f"Removing postal code for non-US country: {country}")
            del validated["postal_code"]
        if "state" in validated:
            logger.warning(f"Removing state for non-US country: {country}")
            del validated["state"]

    validated["country"] = country

    # Validate state for US
    if country == "us" and "state" in validated:
        state = validated["state"].lower()
        if state not in VALID_US_STATES:
            logger.warning(f"Invalid US state '{state}', using default: {DEFAULT_STATE}")
            validated["state"] = DEFAULT_STATE
        else:
            validated["state"] = state

    return validated


def format_oxylabs_username_parts(location: dict[str, str]) -> list[str]:
    """
    Format OxyLabs username parts for a single location.

    OxyLabs targeting formats:
    - Postal code (US): cc-{country}-postalcode-{postal_code}
    - City: cc-{country}-city-{city}
    - State (US): st-us_{state}
    - Country: cc-{country}
    """
    parts = []
    country = location.get("country", "").upper()

    if location.get("postal_code"):
        # Postal code targeting: cc-{country}-postalcode-{postal_code}
        parts.extend([f"cc-{country}", f"postalcode-{location['postal_code']}"])
        logger.info(f"Using OxyLabs postal code targeting: {country}/{location['postal_code']}")

    elif location.get("city"):
        # City targeting: cc-{country}-city-{city}
        parts.extend([f"cc-{country}", f"city-{location['city']}"])
        logger.info(f"Using OxyLabs city targeting: {country}/{location['city']}")

    elif location.get("state") and country == "US":
        # State targeting (US only): st-us_{state}
        oxylabs_state = f"us_{location['state']}"
        parts.append(f"st-{oxylabs_state}")
        logger.info(f"Using OxyLabs US state targeting: {oxylabs_state}")

    elif country:
        # Country targeting: cc-{country}
        parts.append(f"cc-{country}")
        logger.info(f"Using OxyLabs country targeting: {country}")

    return parts


def format_oxylabs_proxy_config(location_str: str) -> dict[str, str]:
    """
    Parse location string and format for OxyLabs proxy configuration.
    Returns environment variables for container.
    """
    parsed_location = parse_hierarchical_location(location_str)

    validated_location = validate_and_apply_defaults(parsed_location)

    username_parts = format_oxylabs_username_parts(validated_location)

    formatted_username = "-".join(username_parts) if username_parts else ""

    logger.info(
        f"Formatted OxyLabs proxy config for location '{location_str}': {formatted_username}"
    )

    return {
        "OXYLABS_PROXY_USERNAME": formatted_username,
        "PROXY_LOCATION_PARSED": str(validated_location),
        "PROXY_LOCATION_RAW": location_str,
    }


def _is_valid_ip(ip: str) -> bool:
    """Validate IP address format."""
    try:
        parts = ip.split('.')
        return len(parts) == 4 and all(0 <= int(part) <= 255 for part in parts)
    except (ValueError, AttributeError):
        return False


async def validate_proxy_ip(
    proxy_host: str, proxy_port: int, proxy_auth: str, timeout: int = IP_CHECK_TIMEOUT
) -> tuple[bool, str, str]:
    """
    Validate that a proxy is working by making a test request to get external IP.

    Args:
        proxy_host: Proxy server hostname
        proxy_port: Proxy server port
        proxy_auth: Base64 encoded proxy authorization
        timeout: Request timeout in seconds

    Returns:
        tuple: (success: bool, ip_address: str, error_message: str)
    """
    try:
        proxy_url = f"http://{base64.b64decode(proxy_auth).decode()}@{proxy_host}:{proxy_port}"

        logger.info(f"Validating IP using proxy: {proxy_host}:{proxy_port}")

        timeout_config = aiohttp.ClientTimeout(total=timeout)

        async with aiohttp.ClientSession(timeout=timeout_config) as session:
            async with session.get(
                IP_CHECK_URL,
                proxy=proxy_url,
                ssl=False,  # Skip SSL verification for simplicity
            ) as response:
                if response.status == 200:
                    # Parse plain text response from AWS checkip
                    ip_address = (await response.text()).strip()
                    if ip_address and _is_valid_ip(ip_address):
                        return True, ip_address, ""
                    else:
                        return False, "", f"Invalid IP format: {ip_address}"
                else:
                    error_text = await response.text()
                    return False, "", f"HTTP error {response.status}: {error_text[:200]}"

    except asyncio.CancelledError:
        logger.debug("IP validation cancelled by client disconnection")
        return False, "", "Validation cancelled"
    except asyncio.TimeoutError:
        logger.warning("IP validation timed out")
        return False, "", "Validation timed out"
    except Exception as e:
        error_str = str(e)
        logger.warning(f"IP validation failed: {error_str}")
        if "ProxyError" in error_str:
            return False, "", f"Proxy connection failed: {error_str}"
        elif "timeout" in error_str.lower():
            return False, "", f"Connection timeout: {error_str}"
        else:
            return False, "", f"Request failed: {error_str}"


async def validate_oxylabs_proxy_for_location(location_str: str) -> tuple[bool, str, str]:
    """
    Validate OxyLabs proxy for a given location by constructing proxy config and testing.
    
    Args:
        location_str: Location string like "city_los_angeles_state_california_country_us"
        
    Returns:
        tuple: (success: bool, validated_ip: str, error_message: str)
    """
    try:
        # Parse endpoint into host and port
        endpoint_parts = settings.oxylabs_endpoint.split(':')
        proxy_host = endpoint_parts[0]
        proxy_port = int(endpoint_parts[1]) if len(endpoint_parts) > 1 else 7777
        
        if not settings.oxylabs_username or not settings.oxylabs_password:
            return False, "", "OxyLabs proxy credentials not configured"
        
        # Format location for OxyLabs username
        oxylabs_config = format_oxylabs_proxy_config(location_str)
        location_username = oxylabs_config.get("OXYLABS_PROXY_USERNAME", "")
        
        if not location_username:
            return False, "", f"Could not format location for OxyLabs: {location_str}"
        
        # Combine base username with location targeting
        full_username = f"{settings.oxylabs_username}-{location_username}"
        
        # Create base64 encoded auth
        auth_string = f"{full_username}:{settings.oxylabs_password}"
        proxy_auth = base64.b64encode(auth_string.encode()).decode()
        
        logger.info(f"Validating OxyLabs proxy with username: {full_username}")
        
        # Validate the proxy
        return await validate_proxy_ip(proxy_host, proxy_port, proxy_auth)
        
    except Exception as e:
        error_msg = f"Failed to validate OxyLabs proxy: {e}"
        logger.error(error_msg)
        return False, "", error_msg


async def intercept_and_store_proxy_location(headers: Dict[str, str], hostname: str) -> bool:
    """
    Main function to intercept x-location header, validate proxy, and store for hostname session.
    Called from MCP proxy when processing requests.
    
    Returns:
        bool: True if proxy was validated and stored, False otherwise
    """
    location = headers.get("x-location")
    if not location:
        return False
    
    logger.info(f"Intercepted proxy location for {hostname}: {location}")
    
    # Validate the proxy for this location
    is_valid, validated_ip, error_msg = await validate_oxylabs_proxy_for_location(location)
    
    if is_valid:
        set_proxy_location_for_hostname(hostname, location)
        logger.info(f"✅ Validated and stored proxy for {hostname}: {location} -> IP {validated_ip}")
        return True
    else:
        logger.warning(f"❌ Proxy validation failed for {hostname}: {location} - {error_msg}")
        return False
