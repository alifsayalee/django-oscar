"""Maxio Advanced Billing SDK client factory (sync, module-scoped).

Django runs under WSGI, so we use the sync ``MaxioAdvancedBillingClient`` and
hold a single long-lived instance behind a lazily-initialised module global --
never one per request (python-client-initialization). All configuration is read
from Django settings at first use; nothing is hardcoded and no secret value is
written into the repository.
"""

import atexit
import threading

from django.conf import settings

from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import BasicAuthCredentials

from .errors import MaxioConfigurationError

# Environments the SDK accepts (its `environment` keyword is case-sensitive and
# lowercase, but the deployment supplies MAXIO_ENVIRONMENT as e.g. "US").
_VALID_ENVIRONMENTS = ('us', 'eu', 'maxio_api_gateway')

# A short timeout: this sits on a user-facing request path and the SDK performs
# no retries, so the timeout genuinely bounds the call.
_TIMEOUT_SECONDS = 15.0

_client = None
_lock = threading.Lock()


def _server_config(environment):
    """Build the `server_config` for the `production` server.

    Prefer an explicit MAXIO_BASE_URL override verbatim; otherwise resolve the
    default `https://{site}.chargify.com` template by supplying the site
    subdomain.
    """
    base_url = getattr(settings, 'MAXIO_BASE_URL', '') or ''
    if base_url:
        return {'production': {environment: {'base_url': base_url}}}
    site = getattr(settings, 'MAXIO_SITE_SUBDOMAIN', '') or ''
    if not site:
        raise MaxioConfigurationError(
            'Neither MAXIO_BASE_URL nor MAXIO_SITE_SUBDOMAIN is configured.')
    return {'production': {environment: {'site': site}}}


def _build_client():
    api_key = getattr(settings, 'MAXIO_API_KEY', '') or ''
    if not api_key:
        raise MaxioConfigurationError('MAXIO_API_KEY is not configured.')

    environment = (getattr(settings, 'MAXIO_ENVIRONMENT', '') or 'us').lower()
    if environment not in _VALID_ENVIRONMENTS:
        raise MaxioConfigurationError(
            f'MAXIO_ENVIRONMENT {environment!r} is not one of {_VALID_ENVIRONMENTS}.')

    try:
        return MaxioAdvancedBillingClient(
            # Maxio HTTP Basic auth: API key as the username, literal "x" as the
            # password (per the SDK's own quickstart: `curl -u <api_key>:x`).
            basic_auth=BasicAuthCredentials(username=api_key, password='x'),
            environment=environment,
            server_config=_server_config(environment),
            timeout=_TIMEOUT_SECONDS,
        )
    except (ValueError, TypeError) as exc:
        raise MaxioConfigurationError(f'Invalid Maxio configuration: {exc}') from exc


def get_client():
    """Return the shared client, building it on first use (double-checked)."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                client = _build_client()
                atexit.register(close_client)
                _client = client
    return _client


def close_client():
    """Close the shared client's connection pool. Registered with atexit."""
    global _client
    if _client is not None:
        try:
            _client.close()
        finally:
            _client = None
