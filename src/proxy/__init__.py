"""Proxy validation and selection module.

This module provides IP validation and hierarchical location fallback
for proxy configurations.
"""

from src.proxy.location_hierarchy import build_location_hierarchy, describe_location
from src.proxy.selector import select_and_validate_proxy
from src.proxy.validation import mask_credentials, validate_proxy_ip

__all__ = [
    "validate_proxy_ip",
    "mask_credentials",
    "build_location_hierarchy",
    "describe_location",
    "select_and_validate_proxy",
]
