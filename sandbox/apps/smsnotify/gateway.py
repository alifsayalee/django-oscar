"""The only place this project talks to Twilio.

Every Twilio interaction goes through the ``twilio-sdk`` APIMatic SDK (import
root ``twilio_sdk``). The SDK client is built lazily, once, and held as a
module-global (WSGI/Django pattern): it owns a pooled httpx transport and must be
long-lived, never rebuilt per request. Secrets are read from Django settings at
build time, never at import.

The SDK performs NO retries. We add a small bounded retry around idempotent
*reads* only (lookup, fetch, list); *sends* are one-shot -- retrying a send could
double-charge and double-text the shopper.
"""

import logging
import threading
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx
from django.conf import settings
from pydantic import ValidationError

from twilio_sdk import Client
from twilio_sdk.core import UNSET, ApiError, BasicAuthCredentials

from . import exceptions
from .status import map_status

log = logging.getLogger("smsnotify.gateway")

_client = None
_client_lock = threading.Lock()

# Twilio's own scheduled-message window is 15 min .. 7 days; a short read retry.
_READ_RETRIES = 2
_RETRY_BACKOFF = 0.5


def _required(name):
    value = getattr(settings, name, "") or ""
    if not value:
        raise exceptions.ProviderConfigError(
            f"Twilio setting {name} is not configured."
        )
    return value


def get_client():
    """Return the shared, lazily-built Twilio client (thread-safe, double-checked)."""
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        account_sid = _required("TWILIO_ACCOUNT_SID")
        auth_token = _required("TWILIO_AUTH_TOKEN")
        base_url = getattr(settings, "TWILIO_BASE_URL", "") or ""
        # Several servers, one environment -> server_config with each server's
        # base_url directly. TWILIO_BASE_URL overrides ONLY the messaging API
        # (server key "default" = api.twilio.com); Lookup (default4) is untouched.
        server_config = {"default": {"base_url": base_url}} if base_url else None
        _client = Client(
            account_sid_auth_token=BasicAuthCredentials(
                username=account_sid, password=auth_token
            ),
            server_config=server_config,
            timeout=15.0,
        )
        return _client


def reset_client_for_tests(client):
    """Test seam: install a client built with a stub transport."""
    global _client
    with _client_lock:
        _client = client


# --------------------------------------------------------------------------- #
# Error boundary
# --------------------------------------------------------------------------- #

# httpx failures that provably never left this process (nothing happened upstream).
_NEVER_SENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
)


def _raise_from_api_error(e, *, is_write):
    status = e.status_code
    if status in (401, 403):
        raise exceptions.ProviderConfigError() from e
    if status == 429:
        raise exceptions.ProviderUnavailable(
            "Messaging provider rate-limited us.", status_code=503, outcome_unknown=False
        ) from e
    if status == 404:
        raise exceptions.ProviderRejected(404, "Not found at the messaging provider.") from e
    if 400 <= status < 500:
        # A genuine rejection: the request was understood and refused. Do not leak
        # the raw provider body (it can echo the destination number).
        raise exceptions.ProviderRejected(status, "Messaging provider rejected the request.") from e
    # 5xx: on a write it may still have landed.
    raise exceptions.ProviderUnavailable(
        "Messaging provider error.", status_code=502, outcome_unknown=is_write
    ) from e


def _translate(fn, *, is_write):
    """Run ``fn`` and convert every SDK failure kind into a ProviderError."""
    try:
        return fn()
    except ApiError as e:
        _raise_from_api_error(e, is_write=is_write)
    except ValidationError as e:
        # Decode failure: bypasses both response modes. Outcome unknown.
        raise exceptions.ProviderUnreadable() from e
    except _NEVER_SENT as e:
        raise exceptions.ProviderUnavailable(
            "Could not reach the messaging provider.", status_code=502, outcome_unknown=False
        ) from e
    except httpx.RequestError as e:
        # Sent, but no readable answer -> may have landed.
        raise exceptions.ProviderUnavailable(
            "No response from the messaging provider.", status_code=504, outcome_unknown=is_write
        ) from e


def _read(fn):
    """Idempotent read with a small bounded retry on transient failures."""
    last = None
    for attempt in range(_READ_RETRIES + 1):
        try:
            return _translate(fn, is_write=False)
        except (exceptions.ProviderUnavailable, exceptions.ProviderUnreadable) as e:
            last = e
            if attempt < _READ_RETRIES:
                time.sleep(_RETRY_BACKOFF * (attempt + 1))
                continue
            raise
    raise last  # unreachable


# --------------------------------------------------------------------------- #
# Result shapes
# --------------------------------------------------------------------------- #


@dataclass
class SendResult:
    sid: "str | None"
    raw_status: "str | None"
    status: str
    error_code: "int | None"
    error_message: "str | None"


def _unset_to_none(value):
    return None if value is UNSET else value


def _result_from_message(msg) -> SendResult:
    raw = _unset_to_none(msg.status)
    return SendResult(
        sid=_unset_to_none(msg.sid),
        raw_status=str(raw) if raw is not None else None,
        status=map_status(raw),
        error_code=_unset_to_none(msg.error_code),
        error_message=_unset_to_none(msg.error_message),
    )


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #


def validate_number(number, country_code=None):
    """Validate a number and return the provider's canonical E.164 form.

    Twilio Lookup v1: 200 -> usable, returns canonical ``phone_number``; 404 ->
    not a usable destination (raised as ProviderRejected 404). We use v1, not v2:
    v2's response carries ``null`` data-package fields that the SDK model rejects
    at decode (a decode failure on every basic lookup); v1 decodes cleanly.
    Server ``default4`` (lookups.twilio.com) -- NOT affected by TWILIO_BASE_URL.
    """
    client = get_client()
    kwargs = {}
    if country_code:
        kwargs["country_code"] = country_code

    def call():
        return client.lookups_v1_phone_number_api.fetch_phone_number2(number, **kwargs)

    resp = _read(call)
    canonical = _unset_to_none(resp.phone_number)
    if not canonical:
        # 200 but no phone_number -> outcome unreadable.
        raise exceptions.ProviderUnreadable("Lookup returned no canonical number.")
    return canonical


def send_sms(to, body):
    """Send an SMS immediately from the configured sending number. One-shot."""
    client = get_client()
    account_sid = _required("TWILIO_ACCOUNT_SID")
    from_number = _required("TWILIO_FROM_NUMBER")

    def call():
        return client.api20100401_message.create_message(
            account_sid, to, from_=from_number, body=body
        )

    msg = _translate(call, is_write=True)
    return _result_from_message(msg)


def schedule_sms(to, body, send_at):
    """Schedule an SMS for later delivery via the Messaging Service.

    Twilio schedules require a Messaging Service SID, ``schedule_type='fixed'`` and
    an aware ``send_at`` 15 min..7 days out; ``from_`` must not be combined with
    the messaging service for scheduling. One-shot.
    """
    client = get_client()
    account_sid = _required("TWILIO_ACCOUNT_SID")
    messaging_service_sid = _required("TWILIO_MESSAGING_SERVICE_SID")

    def call():
        return client.api20100401_message.create_message(
            account_sid,
            to,
            messaging_service_sid=messaging_service_sid,
            schedule_type="fixed",
            send_at=send_at,
            body=body,
        )

    msg = _translate(call, is_write=True)
    return _result_from_message(msg)


# A message is briefly not updatable just after creation (Twilio eventual
# consistency): the update endpoint answers 404 for a few seconds even though GET
# already returns it. Both cancel and redact are idempotent, so we retry a 404
# for a short window before giving up.
_UPDATE_404_RETRIES = 6
_UPDATE_404_BACKOFF = 1.5


def _update_message_with_404_retry(call):
    last = None
    for attempt in range(_UPDATE_404_RETRIES + 1):
        try:
            return _result_from_message(_translate(call, is_write=True))
        except exceptions.ProviderRejected as e:
            if e.status_code == 404 and attempt < _UPDATE_404_RETRIES:
                last = e
                time.sleep(_UPDATE_404_BACKOFF)
                continue
            raise
    raise last  # unreachable


def cancel_scheduled(sid):
    """Cancel a still-scheduled message. Returns the refreshed SendResult.

    Retries a transient 404 (just-scheduled, not yet updatable). A persistent 404
    or any other error is raised.
    """
    client = get_client()
    account_sid = _required("TWILIO_ACCOUNT_SID")

    def call():
        return client.api20100401_message.update_message(account_sid, sid, status="canceled")

    return _update_message_with_404_retry(call)


def redact_content(sid):
    """Redact a message's body provider-side (empty body). Record survives.

    Retries a transient 404 in case disposal is requested moments after sending.
    """
    client = get_client()
    account_sid = _required("TWILIO_ACCOUNT_SID")

    def call():
        return client.api20100401_message.update_message(account_sid, sid, body="")

    return _update_message_with_404_retry(call)


def fetch_status(sid):
    """Refresh a message's delivery outcome from the provider (idempotent read)."""
    client = get_client()
    account_sid = _required("TWILIO_ACCOUNT_SID")

    def call():
        return client.api20100401_message.fetch_message(account_sid, sid)

    msg = _read(call)
    return _result_from_message(msg)


@dataclass
class ReconMessage:
    sid: "str | None"
    status: str
    raw_status: "str | None"
    to: "str | None"
    from_: "str | None"
    date_sent: "str | None"
    error_code: "int | None"


_MAX_PAGES = 100


def list_sent_messages(from_number, date_from, date_to):
    """List the provider's messages sent FROM ``from_number`` in a datetime range.

    ``date_from``/``date_to`` are aware datetimes; Twilio's DateSent filters are
    day-granular, so the caller widens to whole-day boundaries and narrows the
    result by the real instants (see services.reconcile). Paginated, bounded by
    ``_MAX_PAGES``. Asks the provider only for THIS number's traffic.
    """
    client = get_client()
    account_sid = _required("TWILIO_ACCOUNT_SID")
    out = []
    page_token = None
    truncated = False

    for _ in range(_MAX_PAGES):
        tok = page_token

        def call():
            kwargs = dict(
                from_=from_number,
                date_sent_query=date_to,          # wire DateSent<  (upper bound)
                date_sent_query_query=date_from,  # wire DateSent>  (lower bound)
                page_size=100,
            )
            if tok:
                kwargs["page_token"] = tok
            return client.api20100401_message.list_message(account_sid, **kwargs)

        resp = _read(call)
        for m in (_unset_to_none(resp.messages) or []):
            raw = _unset_to_none(m.status)
            out.append(
                ReconMessage(
                    sid=_unset_to_none(m.sid),
                    status=map_status(raw),
                    raw_status=str(raw) if raw is not None else None,
                    to=_unset_to_none(m.to),
                    from_=_unset_to_none(m.from_),
                    date_sent=_unset_to_none(m.date_sent),
                    error_code=_unset_to_none(m.error_code),
                )
            )
        next_uri = _unset_to_none(resp.next_page_uri)
        if not next_uri:
            break
        qs = parse_qs(urlparse(next_uri).query)
        tokens = qs.get("PageToken")
        page_token = tokens[0] if tokens else None
        if not page_token:
            break
    else:
        truncated = True

    return out, truncated
