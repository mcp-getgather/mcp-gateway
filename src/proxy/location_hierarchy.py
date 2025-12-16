"""Config-driven location hierarchy builder for proxy fallback.

This module builds location hierarchies based on configuration, allowing
flexible fallback strategies for different proxy providers.

The hierarchy is controlled by the `hierarchy_fields` config option, which
can specify either:
- Individual fields to try (e.g., ["postal_code", "city", "state"])
- Combined fields (e.g., ["city+state", "city", "state"])
"""

from loguru import logger

from src.residential_proxy_sessions import Location

logger = logger.bind(topic="proxy_location_hierarchy")

# Default hierarchy if not specified in config
DEFAULT_HIERARCHY_FIELDS = ["postal_code", "city", "state"]


def build_location_hierarchy(
    location: Location,
    hierarchy_fields: list[str] | None = None,
) -> list[Location]:
    """Build location hierarchy based on config-driven field specification.

    The hierarchy_fields config determines which fields to try and in what order.
    Each field (or field combination) is tried with country, then country alone.

    Args:
        location: Location with country, state, city, postal_code fields
        hierarchy_fields: List of fields to try in order. Can be:
            - Individual fields: ["postal_code", "city", "state"]
            - Combined fields: ["city+state", "city", "state"]
            - None: Uses DEFAULT_HIERARCHY_FIELDS

    Returns:
        List of Location objects ordered from most to least specific

    Examples:
        >>> # Individual fields (Oxylabs/IPRoyal style)
        >>> loc = Location(country="us", state="california", city="los_angeles", postal_code="90001")
        >>> hierarchy = build_location_hierarchy(loc, ["postal_code", "city", "state"])
        >>> # Returns: [postal_code+country, city+country, state+country, country]

        >>> # Combined fields (Decodo style)
        >>> hierarchy = build_location_hierarchy(loc, ["city+state", "city", "state"])
        >>> # Returns: [city+state+country, city+country, state+country, country]
    """
    if not location or not location.country:
        logger.warning("Cannot build hierarchy: no country provided")
        return []

    if hierarchy_fields is None:
        hierarchy_fields = DEFAULT_HIERARCHY_FIELDS
        logger.debug("Using default hierarchy fields", fields=hierarchy_fields)

    hierarchy: list[Location] = []

    for field_spec in hierarchy_fields:
        # Check if this is a combined field (e.g., "city+state")
        if "+" in field_spec:
            # Combined fields
            fields = field_spec.split("+")
            loc_dict = _build_location_dict(location, fields)
            if loc_dict:
                hierarchy.append(Location(**loc_dict))
                logger.debug(
                    f"Added hierarchy level: {field_spec} + country",
                    location=loc_dict,
                )
        else:
            # Individual field
            loc_dict = _build_location_dict(location, [field_spec])
            if loc_dict:
                hierarchy.append(Location(**loc_dict))
                logger.debug(
                    f"Added hierarchy level: {field_spec} + country",
                    location=loc_dict,
                )

    # Always add country-only as final fallback
    if location.country:
        hierarchy.append(Location(country=location.country))
        logger.debug(
            "Added hierarchy level: country only",
            country=location.country,
        )

    logger.info(
        f"Built location hierarchy with {len(hierarchy)} levels",
        original_location=location.model_dump(exclude_none=True),
        hierarchy_fields=hierarchy_fields,
        levels=len(hierarchy),
    )

    return hierarchy


def _build_location_dict(location: Location, fields: list[str]) -> dict[str, str] | None:
    """Build location dict with specified fields plus country.

    Args:
        location: Source location object
        fields: List of field names to include (e.g., ["city", "state"])

    Returns:
        Dict with country and specified fields, or None if any required field is missing

    Example:
        >>> loc = Location(country="us", state="california", city="los_angeles")
        >>> _build_location_dict(loc, ["city", "state"])
        {'country': 'us', 'city': 'los_angeles', 'state': 'california'}
    """
    loc_dict: dict[str, str] = {"country": location.country}

    for field in fields:
        value = getattr(location, field, None)
        if not value:
            # Required field is missing, skip this combination
            logger.debug(
                f"Skipping hierarchy level: missing field '{field}'",
                fields=fields,
            )
            return None
        loc_dict[field] = value

    return loc_dict


def describe_location(location: Location) -> str:
    """Get human-readable description of location.

    Args:
        location: Location object

    Returns:
        String description like "los_angeles, california, us" or "california, us"

    Example:
        >>> loc = Location(country="us", state="california", city="los_angeles")
        >>> describe_location(loc)
        'los_angeles, california, us'
    """
    parts = []
    if location.postal_code:
        parts.append(location.postal_code)
    if location.city:
        parts.append(location.city)
    if location.state:
        parts.append(location.state)
    if location.country:
        parts.append(location.country)

    return ", ".join(parts) if parts else "no location"


def detect_hierarchy_fields_from_template(template: str) -> list[str] | None:
    """Auto-detect hierarchy fields from template placeholders.

    Analyzes a template string to determine which location fields are used,
    and returns a sensible default hierarchy based on those fields.

    Args:
        template: Template string with {placeholders}

    Returns:
        List of hierarchy fields, or None if no location placeholders found

    Example:
        >>> detect_hierarchy_fields_from_template("user-{session_id}-{country}-{state}-{city}")
        ['city', 'state']
        >>> detect_hierarchy_fields_from_template("user-{session_id}")
        None
    """
    has_postal = "{postal_code}" in template
    has_city = "{city}" in template
    has_state = "{state}" in template
    has_country = "{country}" in template

    # If no location fields, no hierarchy needed
    if not any([has_postal, has_city, has_state, has_country]):
        return None

    # Build hierarchy based on which fields are present
    fields = []
    if has_postal:
        fields.append("postal_code")
    if has_city:
        fields.append("city")
    if has_state:
        fields.append("state")

    return fields if fields else None
