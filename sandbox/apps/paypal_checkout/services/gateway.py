"""Thin gateway over the PayPal SDK.

Every PayPal call goes through here. This is the one place that:

* builds request bodies from our domain values (money as ``Decimal`` -> string),
* chooses the right response mode and ``prefer`` header (notably ``void`` must
  use ``return=representation`` or PayPal answers 204 and the SDK decoder raises),
* translates every SDK / transport failure into an :mod:`errors` domain
  exception carrying an HTTP status and a caller-safe message.

No PayPal fact here is written from memory; the shapes come from the SDK map and
the model source, recorded in ``pay-pal-server-sdk-plan.md``.
"""

import contextlib
from decimal import Decimal

import httpx
from django.utils.dateparse import parse_datetime
from pydantic import ValidationError

from paypal.core import ApiError, OAuthProviderError, RawError

from .. import errors
from .client import get_client

# PayPal honor period for a card authorization before it must be re-authorized.
# (Authorizations are capturable for up to ~29 days, but only guaranteed for the
# first ~3; we renew rather than fail a stale one at fulfilment.)


def _money(amount, currency):
    return {"currency_code": currency, "value": "%0.2f" % Decimal(amount)}


def _s(value):
    """Coerce an SDK member (which may be the UNSET sentinel) to a plain value."""
    from paypal.core import UNSET

    if value is UNSET or value is None:
        return None
    return value


def _dec(money):
    """Decimal value from an SDK ``Money`` model, or None."""
    money = _s(money)
    if money is None:
        return None
    value = _s(getattr(money, "value", None))
    return Decimal(value) if value is not None else None


def _first(seq):
    seq = _s(seq)
    if not seq:
        return None
    return seq[0]


@contextlib.contextmanager
def _guard(action):
    """Translate SDK/transport failures for a single PayPal call into domain errors.

    Ordered most-specific first; auth (our config) before the operation's own
    rejection; the two transport arms carry different outcomes (never-sent vs
    may-have-landed).
    """
    try:
        yield
    except ApiError as e:
        status = e.status_code
        if isinstance(e.error, OAuthProviderError):
            raise errors.PayPalConfigError(
                "PayPal credentials were rejected.", detail=str(getattr(e.error, "error", ""))
            ) from e
        if status in (401, 403):
            raise errors.PayPalConfigError(
                "PayPal refused this merchant's credentials.", detail=_err_detail(e)
            ) from e
        if status == 429:
            raise errors.PayPalUnavailable(
                "PayPal is rate limiting requests; try again shortly.",
                status_code=503,
            ) from e
        if status in (400, 404, 409, 422):
            raise errors.PayPalRejected(
                _err_message(e) or ("PayPal rejected the %s request." % action),
                status_code=(404 if status == 404 else 409 if status == 409 else 400),
                detail=_err_detail(e),
            ) from e
        raise errors.PayPalUnavailable(
            "PayPal returned an unexpected error during %s." % action,
            status_code=502,
            detail=_err_detail(e),
        ) from e
    except ValidationError as e:
        # A body that did not decode. The outcome is unknown (the write may have
        # taken effect); never report a rejection as an outage.
        raise errors.PayPalUnreadable(
            "PayPal's response to %s could not be read; outcome unknown." % action,
            detail=str(e),
        ) from e
    except ValueError as e:
        # A non-JSON / empty body reaching the decoder (e.g. an undocumented 204).
        raise errors.PayPalUnreadable(
            "PayPal's response to %s could not be read; outcome unknown." % action,
            detail=str(e),
        ) from e
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError) as e:
        raise errors.PayPalUnavailable(
            "PayPal could not be reached; nothing was sent.",
            status_code=502,
            outcome_unknown=False,
            detail=str(e),
        ) from e
    except httpx.RequestError as e:
        raise errors.PayPalUnavailable(
            "PayPal did not respond; the request may have been received.",
            status_code=504,
            outcome_unknown=True,
            detail=str(e),
        ) from e


def _err_message(e):
    err = getattr(e, "error", None)
    if isinstance(err, RawError):
        return None
    message = _s(getattr(err, "message", None))
    details = _s(getattr(err, "details", None))
    if details:
        issue = _s(getattr(details[0], "issue", None)) or _s(getattr(details[0], "description", None))
        if issue:
            return "%s (%s)" % (message or "PayPal rejected the request", issue)
    return message


def _err_detail(e):
    err = getattr(e, "error", None)
    if isinstance(err, RawError):
        return err.text()[:1000]
    try:
        return err.to_dict()
    except Exception:  # pragma: no cover
        return str(err)


# ---------------------------------------------------------------------------
# Order authorization (put a hold on the money, do not take it)
# ---------------------------------------------------------------------------

def _authorize_from_source(payment_source, amount, currency, invoice_id, custom_id, request_id):
    client = get_client()
    body = {
        "intent": "AUTHORIZE",
        "purchase_units": [
            {
                "amount": _money(amount, currency),
                "invoice_id": invoice_id,
                "custom_id": custom_id,
                "description": "Order %s" % invoice_id,
            }
        ],
        "payment_source": payment_source,
    }
    with _guard("authorization"):
        order = client.orders.create_order(
            body=body,
            pay_pal_request_id=request_id,
            prefer="return=representation",
        )
        pu = _first(order.purchase_units)
        auth = None
        if pu is not None and _s(pu.payments) is not None:
            auth = _first(pu.payments.authorizations)
        if auth is None:
            # Fallback: a payment source that did not auto-authorize on create.
            resp = client.orders.authorize_order(
                _s(order.id), prefer="return=representation"
            )
            pu = _first(resp.purchase_units)
            auth = _first(pu.payments.authorizations) if pu is not None else None
    if auth is None or _s(auth.id) is None:
        raise errors.PayPalUnreadable(
            "PayPal did not return an authorization for the order; outcome unknown."
        )
    return {
        "paypal_order_id": _s(order.id),
        "authorization_id": _s(auth.id),
        "authorization_status": str(_s(auth.status) or ""),
        "expiration_time": _parse_dt(_s(auth.expiration_time)),
    }


def authorize_with_card(card, amount, currency, invoice_id, custom_id, request_id):
    return _authorize_from_source({"card": card}, amount, currency, invoice_id, custom_id, request_id)


def authorize_with_vault(vault_id, amount, currency, invoice_id, custom_id, request_id):
    return _authorize_from_source(
        {"card": {"vault_id": vault_id}}, amount, currency, invoice_id, custom_id, request_id
    )


def get_authorization(authorization_id):
    client = get_client()
    with _guard("authorization lookup"):
        auth = client.payments.get_authorized_payment(authorization_id)
    return {
        "id": _s(auth.id),
        "status": str(_s(auth.status) or ""),
        "expiration_time": _parse_dt(_s(auth.expiration_time)),
    }


def reauthorize(authorization_id):
    client = get_client()
    with _guard("re-authorization"):
        auth = client.payments.reauthorize_payment(
            authorization_id, prefer="return=representation"
        )
    if _s(auth.id) is None:
        raise errors.PayPalUnreadable("PayPal did not return a renewed authorization.")
    return {
        "id": _s(auth.id),
        "status": str(_s(auth.status) or ""),
        "expiration_time": _parse_dt(_s(auth.expiration_time)),
    }


# ---------------------------------------------------------------------------
# Capture (take the money) at fulfilment
# ---------------------------------------------------------------------------

def capture(authorization_id, request_id):
    client = get_client()
    with _guard("capture"):
        cap = client.payments.capture_authorized_payment(
            authorization_id,
            pay_pal_request_id=request_id,
            prefer="return=representation",
        )
    if _s(cap.id) is None:
        raise errors.PayPalUnreadable("PayPal did not return a capture id; outcome unknown.")
    srb = _s(cap.seller_receivable_breakdown)
    fee = net = gross = None
    if srb is not None:
        gross = _dec(srb.gross_amount)
        fee = _dec(srb.paypal_fee)
        net = _dec(srb.net_amount)
    return {
        "capture_id": _s(cap.id),
        "status": str(_s(cap.status) or ""),
        "amount": _dec(cap.amount) if _dec(cap.amount) is not None else gross,
        "paypal_fee": fee,
        "net_amount": net,
    }


# ---------------------------------------------------------------------------
# Cancel (void the hold) before fulfilment
# ---------------------------------------------------------------------------

def void(authorization_id):
    client = get_client()
    # prefer=return=representation is required: with the default the endpoint
    # answers 204 and the SDK decoder raises on the empty body.
    with _guard("void"):
        auth = client.payments.void_payment(
            authorization_id, prefer="return=representation"
        )
    return str(_s(auth.status) or "VOIDED")


# ---------------------------------------------------------------------------
# Refund a captured payment
# ---------------------------------------------------------------------------

def refund(capture_id, amount, currency, request_id):
    client = get_client()
    with _guard("refund"):
        ref = client.payments.refund_captured_payment(
            capture_id,
            pay_pal_request_id=request_id,
            prefer="return=representation",
            body={"amount": _money(amount, currency)},
        )
    if _s(ref.id) is None:
        raise errors.PayPalUnreadable("PayPal did not return a refund id; outcome unknown.")
    return {
        "refund_id": _s(ref.id),
        "status": str(_s(ref.status) or ""),
        "amount": _dec(ref.amount),
    }


# ---------------------------------------------------------------------------
# Vault: save / delete a card
# ---------------------------------------------------------------------------

def create_vault_token(card, request_id):
    client = get_client()
    with _guard("saving the card"):
        token = client.vault.create_payment_token(
            body={"payment_source": {"card": card}},
            pay_pal_request_id=request_id,
        )
    token_id = _s(token.id)
    if token_id is None:
        raise errors.PayPalUnreadable("PayPal did not return a saved-card id; outcome unknown.")
    customer = _s(token.customer)
    customer_id = _s(customer.id) if customer is not None else None
    card_out = {}
    ps = _s(token.payment_source)
    if ps is not None and _s(ps.card) is not None:
        c = ps.card
        card_out = {
            "brand": str(_s(c.brand) or ""),
            "last_digits": _s(c.last_digits) or "",
            "expiry": _s(c.expiry) or "",
            "name": _s(c.name) or "",
        }
    return {"token_id": token_id, "customer_id": customer_id, "card": card_out}


def delete_vault_token(token_id):
    client = get_client()
    # delete returns 204/None; use the raw peer so an unexpected error status is a
    # Failure we can translate rather than a bare None.
    from paypal.core import Failure

    with _guard("removing the saved card"):
        result = client.vault.with_raw_response.delete_payment_token(token_id)
    if isinstance(result, Failure):
        status = result.response.status_code
        raise errors.PayPalRejected(
            "PayPal could not remove the saved card.",
            status_code=(404 if status == 404 else 400),
        )


# ---------------------------------------------------------------------------
# Reconciliation: PayPal's own transaction record for a date range
# ---------------------------------------------------------------------------

# PayPal's reporting API rejects a window wider than 31 days, so we chunk.
import datetime as _dt

_MAX_WINDOW = _dt.timedelta(days=31)


def _report_format(dt):
    """Format an aware datetime the way PayPal reporting expects (RFC3339)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S%z")


def search_transactions(from_dt, to_dt):
    """Return every transaction PayPal reports for ``[from_dt, to_dt]``.

    Covers the whole range, not just the first page: the range is split into
    windows of at most 31 days (PayPal's per-query limit) and every page of every
    window is fetched. Transactions are de-duplicated by transaction id. Returns
    a list of plain dicts.
    """
    client = get_client()
    results = {}
    with _guard("reconciliation"):
        window_start = from_dt
        while window_start < to_dt:
            window_end = min(window_start + _MAX_WINDOW, to_dt)
            page = 1
            total_pages = 1
            while page <= total_pages:
                resp = client.transaction_search.search_transactions(
                    _report_format(window_start),
                    _report_format(window_end),
                    fields="transaction_info",
                    page_size=100,
                    page=page,
                )
                total_pages = _s(resp.total_pages) or 1
                for detail in _s(resp.transaction_details) or []:
                    info = _s(detail.transaction_info)
                    if info is None:
                        continue
                    txn_id = _s(info.transaction_id)
                    results[txn_id or id(info)] = {
                        "transaction_id": txn_id,
                        "invoice_id": _s(info.invoice_id),
                        "custom_field": _s(info.custom_field),
                        "status": _s(info.transaction_status),
                        "amount": _dec(info.transaction_amount),
                        "fee": _dec(info.fee_amount),
                        "currency": _currency(info.transaction_amount),
                        "initiation_date": _s(info.transaction_initiation_date),
                    }
                page += 1
            window_start = window_end
    return list(results.values())


def _currency(money):
    money = _s(money)
    if money is None:
        return None
    return _s(getattr(money, "currency_code", None))


def _parse_dt(value):
    if not value:
        return None
    return parse_datetime(value)
