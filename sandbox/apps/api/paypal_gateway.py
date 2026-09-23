"""The single place the PayPal SDK is used.

Everything above this module (services, views) speaks in plain Python values and
our own error types; the SDK's client, models and exceptions never leak past here.
This is where the ``python-*`` companion guidance is applied: one long-lived client,
the OAuth-first error ladder, the echoed-amount check, and paginating the whole
reporting range.
"""

import logging
import threading
from decimal import Decimal

import httpx
from django.conf import settings
from django.utils import timezone
from pydantic import ValidationError

from paypal import PaypalClient
from paypal.core import ApiError as SdkApiError
from paypal.core import ClientCredentials, OAuthProviderError, RawError
from paypal.models import (
    AmountWithBreakdown,
    CaptureRequest,
    CardRequest,
    Customer,
    Error,
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

from . import errors

logger = logging.getLogger("api.paypal")

# Never sent -- nothing can have landed (python-error-handling).
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# Number of decimal places per currency. Two for almost everything; the exceptions
# are the zero- and three-decimal currencies (python-models).
_CURRENCY_EXPONENT = {"JPY": 0, "KRW": 0, "KWD": 3, "BHD": 3, "TND": 3}

_SANDBOX_URL = "https://api-m.sandbox.paypal.com"
_LIVE_URL = "https://api-m.paypal.com"

_client = None
_client_lock = threading.Lock()


class AuthorizationUnusable(errors.ApiError):
    """A capture failed because the authorization can no longer be captured.

    The service catches this to try a reauthorization before giving up.
    """

    status_code = 409


def format_amount(value, currency):
    """Format a Decimal as a PayPal money string with the currency's own scale."""
    places = _CURRENCY_EXPONENT.get(currency, 2)
    return str(Decimal(value).quantize(Decimal(1).scaleb(-places)))


def _base_url():
    if settings.PAYPAL_BASE_URL:
        return settings.PAYPAL_BASE_URL
    env = (settings.PAYPAL_ENVIRONMENT or "sandbox").lower()
    if env in ("live", "production"):
        return _LIVE_URL
    return _SANDBOX_URL


def get_client():
    """Return the process-wide PayPal client, building it lazily on first use.

    Long-lived by design: the client owns an httpx pool and caches the OAuth token,
    so rebuilding per request would re-handshake and re-fetch a token every time.
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                missing = [
                    n
                    for n in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET")
                    if not getattr(settings, n, "")
                ]
                if missing:
                    raise errors.ProviderConfigError(
                        "PayPal credentials not configured: " + ", ".join(missing),
                        status_code=502,
                    )
                _client = PaypalClient(
                    base_url=_base_url(),
                    oauth2=ClientCredentials(
                        client_id=settings.PAYPAL_CLIENT_ID,
                        client_secret=settings.PAYPAL_CLIENT_SECRET,
                    ),
                    timeout=30.0,
                )
    return _client


def _v(value):
    """Resolve an SDK member to a plain value, mapping UNSET to None."""
    # UNSET is falsy and not None; anything the SDK left unset reads as absent here.
    if value is None:
        return None
    # UnsetType has no useful attributes; treat it as absent.
    from paypal.core import UnsetType

    if isinstance(value, UnsetType):
        return None
    return value


def _issues(err):
    """Pull PayPal issue codes out of a typed Error body for operator-facing messages."""
    out = []
    if isinstance(err, Error):
        name = _v(err.name)
        if name:
            out.append(name)
        for detail in _v(err.details) or []:
            issue = _v(getattr(detail, "issue", None))
            if issue:
                out.append(issue)
    return out


def _raise_from_sdk(exc, *, write, may_have_landed=False):
    """Translate an SDK/httpx failure into one of our boundary errors.

    ``write`` marks a mutating call; ``may_have_landed`` is only meaningful for one.
    """
    if isinstance(exc, SdkApiError):
        err = exc.error
        status = exc.status_code
        # Auth/config first: nothing was ever attempted at the operation.
        if isinstance(err, OAuthProviderError):
            logger.error("PayPal credentials rejected: %s", _v(err.error))
            raise errors.ProviderConfigError("PayPal credentials rejected", status_code=502) from exc
        if status in (401, 403):
            logger.error("PayPal refused our credentials/scope: HTTP %s", status)
            raise errors.ProviderConfigError("PayPal refused this integration", status_code=502) from exc
        if status == 429:
            raise errors.ProviderUnavailable("PayPal rate-limited us", status_code=503) from exc
        issues = _issues(err)
        detail = ", ".join(issues) if issues else (err.text() if isinstance(err, RawError) else "")
        if status in (400, 404, 409, 422):
            # A genuine, actionable problem with this request. Surface PayPal's issue.
            msg = "PayPal rejected the request" + (": " + detail if detail else "")
            raise errors.ApiError(msg, status_code=status, code="paypal_rejected",
                                  extra={"issues": issues}) from exc
        # 5xx and any unmapped status -> our fault to report as upstream.
        raise errors.ProviderUnavailable("PayPal is unavailable", status_code=502) from exc

    if isinstance(exc, ValidationError):
        # Decode failure: the outcome is genuinely unknown on a write.
        raise errors.ProviderUnavailable(
            "PayPal sent an unreadable response",
            status_code=502 if not write else 504,
            outcome_unknown=write,
        ) from exc

    if isinstance(exc, _NEVER_SENT):
        raise errors.ProviderUnavailable(
            "Could not reach PayPal", status_code=502, outcome_unknown=False
        ) from exc

    if isinstance(exc, httpx.RequestError):
        raise errors.ProviderUnavailable(
            "No response from PayPal", status_code=504, outcome_unknown=True
        ) from exc

    raise exc


def _parse_time(value):
    value = _v(value)
    if not value:
        return None
    try:
        from django.utils.dateparse import parse_datetime

        return parse_datetime(value)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# High-level operations. Each returns plain values and raises our errors.
# ---------------------------------------------------------------------------


def create_paypal_order(order_number, invoice_reference, amount, currency):
    """Create a PayPal order (intent AUTHORIZE) for the given total. Returns its id.

    ``custom_id`` carries the Oscar order number (stable, used for reconciliation);
    ``invoice_id`` carries the globally-unique invoice reference.
    """
    client = get_client()
    value = format_amount(amount, currency)
    body = OrderRequest(
        intent="AUTHORIZE",
        purchase_units=[
            PurchaseUnitRequest(
                amount=AmountWithBreakdown(currency_code=currency, value=value),
                custom_id=order_number,
                invoice_id=invoice_reference,
                description="Oscar order %s" % order_number,
            )
        ],
    )
    try:
        order = client.orders.create_order(
            body,
            pay_pal_request_id="ord-%s-create" % order_number,
            prefer="return=representation",
        )
    except Exception as exc:  # noqa: BLE001 -- translated below
        _raise_from_sdk(exc, write=True, may_have_landed=True)
    paypal_order_id = _v(order.id)
    if not paypal_order_id:
        raise errors.ProviderUnavailable(
            "PayPal did not return an order id", status_code=502, outcome_unknown=True
        )
    return paypal_order_id


def _card_source(card=None, vault_id=None):
    if vault_id:
        return OrderAuthorizeRequestPaymentSource(card=CardRequest(vault_id=vault_id))
    return OrderAuthorizeRequestPaymentSource(
        card=CardRequest(
            number=card["number"],
            expiry=card["expiry"],
            security_code=card["security_code"],
            name=card.get("name") or "",
        )
    )


def authorize_paypal_order(paypal_order_id, order_number, *, card=None, vault_id=None):
    """Authorize (place a hold) on a PayPal order with a card or a vaulted card.

    Returns dict(authorization_id, status, expiry). Raises PaymentChallengeRequired
    if PayPal wants a browser approval instead of authorizing directly.
    """
    client = get_client()
    body = OrderAuthorizeRequest(payment_source=_card_source(card=card, vault_id=vault_id))
    try:
        resp = client.orders.authorize_order(
            paypal_order_id,
            body=body,
            pay_pal_request_id="ord-%s-auth" % order_number,
            prefer="return=representation",
        )
    except Exception as exc:  # noqa: BLE001
        _raise_from_sdk(exc, write=True, may_have_landed=True)

    status = _v(resp.status)
    links = _v(resp.links) or []
    if any(_v(link.rel) == "payer-action" for link in links) or status == "PAYER_ACTION_REQUIRED":
        raise errors.PaymentChallengeRequired(
            "PayPal requires the shopper to approve this card in a browser "
            "(3-D Secure challenge); this integration does not support the approval round-trip."
        )

    units = _v(resp.purchase_units) or []
    payments = _v(units[0].payments) if units else None
    auths = _v(payments.authorizations) if payments else None
    if not auths:
        raise errors.ProviderUnavailable(
            "PayPal authorized no payment for order %s (status %s)" % (paypal_order_id, status),
            status_code=502,
            outcome_unknown=True,
        )
    auth = auths[0]
    auth_id = _v(auth.id)
    if not auth_id:
        raise errors.ProviderUnavailable(
            "PayPal returned no authorization id", status_code=502, outcome_unknown=True
        )
    return {
        "authorization_id": auth_id,
        "status": _v(auth.status) or "",
        "expiry": _parse_time(auth.expiration_time),
    }


def get_authorization(authorization_id):
    """Return dict(status, expiry) for an authorization, or raise."""
    client = get_client()
    try:
        auth = client.payments.get_authorized_payment(authorization_id)
    except Exception as exc:  # noqa: BLE001
        _raise_from_sdk(exc, write=False)
    return {"status": _v(auth.status) or "", "expiry": _parse_time(auth.expiration_time)}


def reauthorize(authorization_id, amount, currency):
    """Renew a stale authorization. Returns dict(authorization_id, status, expiry)."""
    client = get_client()
    value = format_amount(amount, currency)
    try:
        auth = client.payments.reauthorize_payment(
            authorization_id,
            body=ReauthorizeRequest(amount=Money(currency_code=currency, value=value)),
            pay_pal_request_id="auth-%s-reauth" % authorization_id,
            prefer="return=representation",
        )
    except errors.ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_from_sdk(exc, write=True, may_have_landed=True)
    new_id = _v(auth.id) or authorization_id
    return {
        "authorization_id": new_id,
        "status": _v(auth.status) or "",
        "expiry": _parse_time(auth.expiration_time),
    }


def capture(authorization_id, order_number, amount, currency):
    """Capture an authorization. Returns the settlement breakdown.

    Raises AuthorizationUnusable when the authorization can no longer be captured
    (so the caller may reauthorize), and verifies the captured amount echoes ours.
    """
    client = get_client()
    value = format_amount(amount, currency)
    try:
        cap = client.payments.capture_authorized_payment(
            authorization_id,
            body=CaptureRequest(
                amount=Money(currency_code=currency, value=value), final_capture=True
            ),
            pay_pal_request_id="ord-%s-capture" % order_number,
            prefer="return=representation",
        )
    except SdkApiError as exc:
        # An expired/uncapturable authorization comes back 4xx with an issue code.
        if exc.status_code in (409, 422):
            issues = _issues(exc.error)
            raise AuthorizationUnusable(
                "Authorization cannot be captured: " + (", ".join(issues) or "unknown"),
                extra={"issues": issues},
            ) from exc
        _raise_from_sdk(exc, write=True, may_have_landed=True)
    except Exception as exc:  # noqa: BLE001
        _raise_from_sdk(exc, write=True, may_have_landed=True)

    capture_id = _v(cap.id)
    status = _v(cap.status) or ""
    if not capture_id:
        raise errors.ProviderUnavailable(
            "PayPal returned no capture id", status_code=502, outcome_unknown=True
        )

    gross = fee = net = None
    srb = _v(cap.seller_receivable_breakdown)
    if srb is not None:
        gross_money = _v(srb.gross_amount)
        if gross_money is not None:
            # Verify PayPal captured what we asked, comparing as Decimal.
            if Decimal(_v(gross_money.value)) != Decimal(value):
                raise errors.ProviderUnavailable(
                    "PayPal captured %s but we requested %s" % (_v(gross_money.value), value),
                    status_code=502,
                    outcome_unknown=True,
                )
            gross = Decimal(_v(gross_money.value))
        fee_money = _v(srb.paypal_fee)
        fee = Decimal(_v(fee_money.value)) if fee_money is not None else None
        net_money = _v(srb.net_amount)
        net = Decimal(_v(net_money.value)) if net_money is not None else None

    return {
        "capture_id": capture_id,
        "status": status,
        "gross_amount": gross,
        "paypal_fee": fee,
        "net_amount": net,
        "captured_at": _parse_time(cap.create_time) or timezone.now(),
    }


def void(authorization_id):
    """Void an authorization, releasing the held funds. Returns the new status."""
    client = get_client()
    try:
        auth = client.payments.void_payment(
            authorization_id,
            pay_pal_request_id="auth-%s-void" % authorization_id,
            prefer="return=representation",
        )
    except Exception as exc:  # noqa: BLE001
        _raise_from_sdk(exc, write=True, may_have_landed=True)
    return _v(auth.status) or "VOIDED"


def refund(capture_id, amount, currency, idempotency_key):
    """Refund a captured payment (full or partial). Returns dict(refund_id, status, amount)."""
    client = get_client()
    body = None
    if amount is not None:
        value = format_amount(amount, currency)
        body = RefundRequest(amount=Money(currency_code=currency, value=value))
    try:
        rf = client.payments.refund_captured_payment(
            capture_id,
            body=body,
            pay_pal_request_id=idempotency_key,
            prefer="return=representation",
        )
    except Exception as exc:  # noqa: BLE001
        _raise_from_sdk(exc, write=True, may_have_landed=True)
    refund_id = _v(rf.id)
    if not refund_id:
        raise errors.ProviderUnavailable(
            "PayPal returned no refund id", status_code=502, outcome_unknown=True
        )
    amt = _v(rf.amount)
    return {
        "refund_id": refund_id,
        "status": _v(rf.status) or "",
        "amount": Decimal(_v(amt.value)) if amt is not None else amount,
    }


def vault_card(user_id, card):
    """Vault a card at PayPal for a customer. Returns dict(token, brand, last_digits, expiry, name)."""
    client = get_client()
    body = PaymentTokenRequest(
        customer=Customer(merchant_customer_id="oscar-cust-%s" % user_id),
        payment_source=PaymentTokenRequestPaymentSource(
            card=PaymentTokenRequestCard(
                number=card["number"],
                expiry=card["expiry"],
                security_code=card["security_code"],
                name=card.get("name") or "",
            )
        ),
    )
    try:
        token = client.vault.create_payment_token(body)
    except Exception as exc:  # noqa: BLE001
        _raise_from_sdk(exc, write=True, may_have_landed=True)
    token_id = _v(token.id)
    if not token_id:
        raise errors.ProviderUnavailable(
            "PayPal returned no vault token id", status_code=502, outcome_unknown=True
        )
    source = _v(token.payment_source)
    card_entity = _v(source.card) if source is not None else None
    return {
        "token": token_id,
        "brand": _v(getattr(card_entity, "brand", None)) or "",
        "last_digits": _v(getattr(card_entity, "last_digits", None)) or (card["number"][-4:]),
        "expiry": _v(getattr(card_entity, "expiry", None)) or card["expiry"],
        "name": _v(getattr(card_entity, "name", None)) or (card.get("name") or ""),
    }


def delete_vault_token(token):
    """Delete a vaulted payment token. Idempotent-ish: a 404 is treated as gone."""
    client = get_client()
    try:
        result = client.vault.with_raw_response.delete_payment_token(token)
    except SdkApiError as exc:
        if exc.status_code == 404:
            return
        _raise_from_sdk(exc, write=True, may_have_landed=False)
    except Exception as exc:  # noqa: BLE001
        _raise_from_sdk(exc, write=True, may_have_landed=True)
    else:
        # Success is 204; anything else is a Failure we translate.
        from paypal.core import Failure

        if isinstance(result, Failure):
            if result.response.status_code == 404:
                return
            _raise_from_sdk(
                SdkApiError(error=result.error, response=result.response), write=True
            )


def search_transactions(start_date, end_date):
    """Return every PayPal transaction in [start_date, end_date], across all pages.

    ``start_date``/``end_date`` are ISO-8601 strings PayPal accepts. Returns a list
    of dicts. An empty list for a just-created range is expected (reporting lag).
    """
    client = get_client()
    max_pages = 200
    page = 1
    out = []
    truncated = False
    while True:
        try:
            resp = client.transaction_search.search_transactions(
                start_date,
                end_date,
                fields="transaction_info",
                page_size=100,
                page=page,
            )
        except Exception as exc:  # noqa: BLE001
            _raise_from_sdk(exc, write=False)
        for detail in _v(resp.transaction_details) or []:
            info = _v(detail.transaction_info)
            if info is None:
                continue
            amount = _v(info.transaction_amount)
            fee = _v(info.fee_amount)
            out.append(
                {
                    "transaction_id": _v(info.transaction_id),
                    "invoice_id": _v(info.invoice_id),
                    "custom_field": _v(info.custom_field),
                    "status": _v(info.transaction_status),
                    "event_code": _v(info.transaction_event_code),
                    "initiation_date": _v(info.transaction_initiation_date),
                    "amount_value": _v(amount.value) if amount is not None else None,
                    "amount_currency": _v(amount.currency_code) if amount is not None else None,
                    "fee_value": _v(fee.value) if fee is not None else None,
                }
            )
        total_pages = _v(resp.total_pages) or 1
        if page >= total_pages:
            break
        page += 1
        if page > max_pages:
            truncated = True
            break
    return {"transactions": out, "truncated": truncated}
