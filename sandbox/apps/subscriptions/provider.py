"""
The process-wide Maxio gateway, built lazily on first use.

Lazy so that importing the app (tests, management commands) never needs
credentials, and so that forking servers build it after the fork.
"""

import atexit
import threading

from django.conf import settings

from .maxio.client import MaxioConfig, build_client
from .maxio.gateway import MaxioGateway

_lock = threading.Lock()
_gateway = None


def config_from_settings():
    return MaxioConfig(
        api_key=settings.MAXIO_API_KEY,
        site_subdomain=settings.MAXIO_SITE_SUBDOMAIN,
        product_family=settings.MAXIO_DEFAULT_PRODUCT_FAMILY,
        environment=settings.MAXIO_ENVIRONMENT,
        base_url=settings.MAXIO_BASE_URL or None,
    )


def get_gateway():
    global _gateway
    if _gateway is None:
        with _lock:
            if _gateway is None:
                config = config_from_settings()
                gateway = MaxioGateway(build_client(config), config.product_family)
                atexit.register(gateway.close)
                _gateway = gateway
    return _gateway


def set_gateway(gateway):
    """Replace the process-wide gateway (tests); returns the previous one."""
    global _gateway
    with _lock:
        previous, _gateway = _gateway, gateway
    return previous
