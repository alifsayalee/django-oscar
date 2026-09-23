"""The single seam between this app and the PayPal SDK.

Everything that talks to PayPal goes through :class:`PayPalGateway`. It owns the
long-lived SDK client, applies one error boundary that turns the SDK's exception
kinds into this app's small failure vocabulary (:mod:`.exceptions`), and hands
back plain decoded SDK models to the service layer.

Grounded in the contract sheet in ``pay-pal-server-sdk-plan.md`` (which came from
the SDK map + source), never from memory.
"""
import logging
import time
import uuid

import httpx
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from pydantic import ValidationError

from paypal import PaypalClient
from paypal.core import ApiError, ClientCredentials, OAuthProviderError, UNSET
from paypal.models import (
    AmountWithBreakdown,
    CaptureRequest,
    CardRequest,
    Customer,
    Money,
    OrderAuthorizeRequest,
    OrderAuthorizeRequestPaymentSource,
    OrderRequest,
    PaymentTokenRequest,
    PaymentTokenRequestCard,
    PaymentTokenRequestPaymentSource,
    PurchaseUnitRequest,
    ReauthorizeRequest,
    RefundRequest,
)
from paypal.models import Error as PayPalError

from . import money
from .exceptions import (
    ProviderConfigError,
    ProviderError,
    ProviderRejected,
    ProviderUnavailable,
    ProviderUnreadable,
)

logger = logging.getLogger("paypal_checkout")

# Transport failures that happen before the request leaves — nothing landed.
_NEVER_SENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
)

_SANDBOX_URL = "https://api-m.sandbox.paypal.com"
_LIVE_URL = "https://api-m.paypal.com"

# Statuses the caller genuinely caused (passed through as-is). Everything else a
# provider 4xx/5xx reports is treated as our fault by the view layer.
_CALLER_FAULT_STATUSES = {400, 404, 409, 422}


def resolve_base_url():
    """The API base URL, honouring PAYPAL_BASE_URL verbatim when set."""
    override = getattr(settings, "PAYPAL_BASE_URL", "") or ""
    if override:
        return override
    env = (getattr(settings, "PAYPAL_ENVIRONMENT", "sandbox") or "sandbox").lower()
    if env in ("sandbox", "test", "development"):
        return _SANDBOX_URL
    if env in ("live", "production", "prod"):
        return _LIVE_URL
    raise ProviderConfigError(f"Unknown PAYPAL_ENVIRONMENT: {env!r}")


class _LoggingTransport:
    """Wraps the SDK's transport to log method/URL/status (never bodies/headers).

    Only used when PAYPAL_DEBUG_WIRE is on — the seam for verifying a new call.
    """

    def __init__(self, inner):
        self._inner = inner

    def send(self, request):
        started = time.monotonic()
        response = self._inner.send(request)
        logger.info(
            "paypal %s %s -> %s (%.0f ms)",
            request.method, request.url, response.status_code,
            (time.monotonic() - started) * 1000,
        )
        return response

    def close(self):
        self._inner.close()


_client = None


def get_client():
    """Return the long-lived SDK client, building it on first use.

    The presence check for credentials lives here (not in settings), so importing
    settings never raises and a missing variable stops a real request with a clear
    message that names it.
    """
    global _client
    if _client is not None:
        return _client

    missing = [
        name for name in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET")
        if not getattr(settings, name, "")
    ]
    if missing:
        raise ProviderConfigError(
            "Missing PayPal credentials: " + ", ".join(missing)
        )

    kwargs = dict(
        oauth2=ClientCredentials(
            client_id=settings.PAYPAL_CLIENT_ID,
            client_secret=settings.PAYPAL_CLIENT_SECRET,
        ),
        base_url=resolve_base_url(),
        timeout=45.0,
    )
    if getattr(settings, "PAYPAL_DEBUG_WIRE", False):
        from paypal.core import HttpxClient  # local import: only needed when debugging
        kwargs["custom_http_client"] = _LoggingTransport(HttpxClient(timeout=45.0))
    _client = PaypalClient(**kwargs)
    return _client


def reset_client():
    """Drop the cached client (used by tests that inject a transport)."""
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:  # pragma: no cover - best effort
            pass
    _client = None


def set_client(client):
    """Install a client instance directly (used by tests)."""
    global _client
    _client = client


# ---------------------------------------------------------------------------
# small helpers for reading UNSET-safe values off SDK models
# ---------------------------------------------------------------------------

def _val(value):
    """Return ``value`` or ``None`` if it is the SDK's UNSET sentinel."""
    return None if value is UNSET else value


def _enum_str(value):
    """Render an (open) enum member or plain string as its wire string."""
    value = _val(value)
    if value is None:
        return ""
    return str(value)


def _parse_dt(value):
    value = _val(value)
    if not value:
        return None
    dt = parse_datetime(value)
    if dt is not None and timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.utc)
    return dt


def new_request_id():
    return uuid.uuid4().hex


class PayPalGateway:
    """Thin, error-translating facade over the SDK's controllers."""

    def __init__(self, client=None):
        self._client = client

    @property
    def client(self):
        return self._client or get_client()

    # -- error boundary ----------------------------------------------------

    def _translate(self, exc, *, is_write):
        """Map an SDK/transport exception to a ProviderError (returns it)."""
        if isinstance(exc, ApiError):
            err = exc.error
            status = exc.status_code
            # Configuration / auth: nothing was attempted — our fault.
            if isinstance(err, OAuthProviderError):
                logger.error("paypal auth failed: %s (%s)",
                             getattr(err, "error", None),
                             getattr(err, "error_description", None))
                return ProviderConfigError("PayPal credentials were rejected.")
            if status in (401, 403):
                return ProviderConfigError("PayPal refused this integration (auth).")
            if status == 429:
                return ProviderUnavailable("PayPal rate limit reached.", status_code=503)
            # Caller-actionable rejection with a typed body.
            if isinstance(err, PayPalError) and status in _CALLER_FAULT_STATUSES:
                message = _val(getattr(err, "message", None)) or "PayPal rejected the request."
                return ProviderRejected(
                    message, status_code=status,
                    provider_status=_val(getattr(err, "name", None)),
                    debug_id=_val(getattr(err, "debug_id", None)),
                )
            # 5xx and every unmapped/typed-less status: our problem.
            detail = getattr(err, "text", None)
            text = detail() if callable(detail) else "PayPal error."
            logger.error("paypal error %s: %s", status, text[:500])
            return ProviderError(f"PayPal error (HTTP {status}).", status_code=502)

        if isinstance(exc, ValidationError):
            return ProviderUnreadable(
                "PayPal response could not be read.", outcome_unknown=is_write)

        if isinstance(exc, _NEVER_SENT):
            return ProviderUnavailable(
                "Could not reach PayPal.", status_code=502, outcome_unknown=False)

        if isinstance(exc, httpx.RequestError):
            return ProviderUnavailable(
                "No response from PayPal.", status_code=504, outcome_unknown=True)

        return None  # not ours to translate

    def _call(self, op, *, is_write, empty_ok=False):
        """Run ``op`` and translate any failure into a ProviderError.

        ``empty_ok`` handles the operations (void) that legitimately return a 2xx
        with no body: the SDK's success decoder then raises a plain ``ValueError``
        ("Response body is not valid JSON"), which for those ops means success, so
        we return ``None`` rather than reporting an unreadable response.
        """
        try:
            return op()
        except ValueError as exc:
            # A plain ValueError from the pipeline is a 2xx whose body would not
            # decode (ValidationError is handled below and is a *type* mismatch).
            if empty_ok and not isinstance(exc, ValidationError):
                return None
            translated = self._translate(exc, is_write=is_write)
            raise (translated or ProviderUnreadable(
                "PayPal response could not be read.", outcome_unknown=is_write)) from exc
        except (ApiError, httpx.HTTPError, ValidationError) as exc:
            translated = self._translate(exc, is_write=is_write)
            if translated is None:
                raise
            raise translated from exc

    # -- orders ------------------------------------------------------------

    def create_order(self, *, currency, value, invoice_id, custom_id, request_id):
        body = OrderRequest(
            intent="AUTHORIZE",
            purchase_units=[PurchaseUnitRequest(
                amount=AmountWithBreakdown(currency_code=currency, value=value),
                invoice_id=invoice_id,
                custom_id=custom_id,
                description=f"Oscar sandbox order {custom_id}",
            )],
        )
        order = self._call(
            lambda: self.client.orders.create_order(body, pay_pal_request_id=request_id),
            is_write=True,
        )
        return order

    def authorize_order(self, paypal_order_id, *, card_request, request_id):
        body = OrderAuthorizeRequest(
            payment_source=OrderAuthorizeRequestPaymentSource(card=card_request),
        )
        return self._call(
            lambda: self.client.orders.authorize_order(
                paypal_order_id, body=body,
                pay_pal_request_id=request_id, prefer="return=representation"),
            is_write=True,
        )

    # -- payments ----------------------------------------------------------

    def capture(self, authorization_id, *, request_id):
        body = CaptureRequest(final_capture=True)
        return self._call(
            lambda: self.client.payments.capture_authorized_payment(
                authorization_id, body=body,
                pay_pal_request_id=request_id, prefer="return=representation"),
            is_write=True,
        )

    def reauthorize(self, authorization_id, *, currency, value, request_id):
        body = ReauthorizeRequest(amount=Money(currency_code=currency, value=value))
        return self._call(
            lambda: self.client.payments.reauthorize_payment(
                authorization_id, body=body,
                pay_pal_request_id=request_id, prefer="return=representation"),
            is_write=True,
        )

    def void(self, authorization_id, *, request_id):
        # empty_ok: void returns 204/empty in some cases; a 2xx with no body is a
        # successful void, not an unreadable response.
        return self._call(
            lambda: self.client.payments.void_payment(
                authorization_id,
                pay_pal_request_id=request_id, prefer="return=representation"),
            is_write=True, empty_ok=True,
        )

    def refund(self, capture_id, *, currency, value, request_id, full=False):
        body = RefundRequest() if full else RefundRequest(
            amount=Money(currency_code=currency, value=value))
        return self._call(
            lambda: self.client.payments.refund_captured_payment(
                capture_id, body=body,
                pay_pal_request_id=request_id, prefer="return=representation"),
            is_write=True,
        )

    def get_captured_payment(self, capture_id):
        return self._call(
            lambda: self.client.payments.get_captured_payment(capture_id),
            is_write=False,
        )

    # -- vault -------------------------------------------------------------

    def create_payment_token(self, *, customer_id, card_request, request_id, attempts=3):
        """Vault a card. Retries on a 5xx, reusing the SAME request id so PayPal
        can de-duplicate — the sandbox intermittently 500s on this endpoint."""
        body = PaymentTokenRequest(
            customer=Customer(id=customer_id),
            payment_source=PaymentTokenRequestPaymentSource(card=card_request),
        )
        last = None
        for attempt in range(attempts):
            try:
                return self.client.vault.create_payment_token(
                    body, pay_pal_request_id=request_id)
            except ApiError as exc:
                if exc.status_code >= 500 and attempt < attempts - 1:
                    logger.warning("vault create 5xx (attempt %s), retrying", attempt)
                    last = exc
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise self._translate(exc, is_write=True) from exc
            except (httpx.HTTPError, ValidationError, ValueError) as exc:
                raise (self._translate(exc, is_write=True) or ProviderUnreadable(
                    "PayPal response could not be read.")) from exc
        # Exhausted retries on 5xx.
        raise self._translate(last, is_write=True) from last

    def delete_payment_token(self, token_id):
        """Delete a vaulted token. Returns the HTTP status (204 ok, 404 gone)."""
        try:
            result = self.client.vault.with_raw_response.delete_payment_token(token_id)
        except (ApiError, httpx.HTTPError, ValidationError, ValueError) as exc:
            raise (self._translate(exc, is_write=True) or ProviderUnreadable(
                "PayPal response could not be read.")) from exc
        return result.response.status_code

    # -- reporting ---------------------------------------------------------

    def search_transactions(self, *, start_date, end_date, page, page_size=500):
        return self._call(
            lambda: self.client.transaction_search.search_transactions(
                start_date, end_date, fields="transaction_info",
                page_size=page_size, page=page),
            is_write=False,
        )
