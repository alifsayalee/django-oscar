"""Thin service over the PayPal SDK.

Every method here wraps SDK calls in one error boundary (translating the SDK's
httpx / pydantic / ApiError failures into :mod:`errors`) and returns plain
Python data -- never an SDK model or an ``UNSET`` sentinel -- so nothing
SDK-shaped leaks into services, views or the database.
"""
import contextlib

import httpx
from pydantic import ValidationError

from paypal.core import UNSET, ApiError, Failure, OAuthProviderError, RawError
from paypal.models import (
    Address,
    AmountWithBreakdown,
    CardRequest,
    Customer,
    Error,
    Money,
    OrderRequest,
    PaymentSource,
    PaymentTokenRequest,
    PaymentTokenRequestCard,
    PaymentTokenRequestPaymentSource,
    PurchaseUnitRequest,
    ReauthorizeRequest,
    RefundRequest,
)

from . import errors
from .client import get_client

# Reconciliation paging backstop: PayPal capped transaction search at 100/page
# and 500 items/page; this bounds the walk regardless of what the provider says.
_MAX_TXN_PAGES = 100


def _present(value):
    """Return ``None`` for the SDK's ``UNSET`` sentinel, else the value itself."""
    return None if value is UNSET else value


def _status(value):
    """Coerce an open-enum status (enum member or raw str) to a plain wire string."""
    value = _present(value)
    return None if value is None else str(value)


def _money(money):
    money = _present(money)
    if money is None:
        return None
    return {"value": _present(money.value), "currency_code": _present(money.currency_code)}


def _safe_message(error):
    """Build a caller-safe, operator-actionable message from a typed PayPal
    ``Error`` body, including the fine-grained issue codes PayPal reports."""
    parts = []
    name = _present(getattr(error, "name", UNSET))
    message = _present(getattr(error, "message", UNSET))
    if name:
        parts.append(str(name))
    if message:
        parts.append(str(message))
    issues = []
    for detail in _present(getattr(error, "details", UNSET)) or []:
        issue = _present(getattr(detail, "issue", UNSET))
        description = _present(getattr(detail, "description", UNSET))
        field = _present(getattr(detail, "field", UNSET))
        if issue:
            piece = str(issue)
            if field:
                piece += f" ({field})"
            if description:
                piece += f": {description}"
            issues.append(piece)
    base = ": ".join(parts) if parts else "PayPal rejected the request"
    if issues:
        base += " [" + "; ".join(issues) + "]"
    return base


def _translate_api_error(error, status):
    """Map a decoded error body + status (from an ``ApiError`` or a raw
    ``Failure``) to one of our errors."""
    if isinstance(error, OAuthProviderError):
        return errors.PayPalConfigError("PayPal credentials were rejected")
    if status in (401, 403):
        return errors.PayPalConfigError("PayPal refused our credentials")
    if status == 429:
        return errors.PayPalUnavailable("PayPal is rate limiting requests", http_status=503)
    if isinstance(error, Error) and status in (400, 404, 409, 422):
        return errors.PayPalRejected(status, _safe_message(error))
    # Every 5xx and every unmapped 4xx (incl. RawError bodies) is ours to own.
    return errors.PayPalUnavailable("PayPal returned an unexpected error", http_status=502)


@contextlib.contextmanager
def _boundary():
    try:
        yield
    except ApiError as exc:
        raise _translate_api_error(exc.error, exc.status_code) from exc
    except ValidationError as exc:
        raise errors.PayPalUnreadable("Could not read PayPal's response") from exc
    except (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.PoolTimeout,
        httpx.ProxyError,
    ) as exc:
        # Nothing reached PayPal -- the outcome is known: nothing happened.
        raise errors.PayPalUnavailable(
            "PayPal is unreachable", http_status=502, outcome_unknown=False
        ) from exc
    except httpx.RequestError as exc:
        # It may have landed -- the outcome is unknown; callers must reconcile.
        raise errors.PayPalUnavailable(
            "PayPal did not respond", http_status=504, outcome_unknown=True
        ) from exc


def _address(data):
    if not data:
        return UNSET
    return Address(
        country_code=data.get("country_code", "US"),
        address_line_1=data.get("address_line_1", UNSET),
        address_line_2=data.get("address_line_2", UNSET),
        admin_area_2=data.get("city", UNSET),
        admin_area_1=data.get("state", UNSET),
        postal_code=data.get("postal_code", UNSET),
    )


class PayPalGateway:
    """Stateless orchestration of the PayPal operations this integration needs."""

    def __init__(self, client=None):
        self._client = client or get_client()

    # -- Vault (saved cards) -------------------------------------------------
    def vault_card(self, *, card, customer_id=None, merchant_customer_id=None, request_id=None):
        customer = (
            Customer(
                id=customer_id or UNSET,
                merchant_customer_id=merchant_customer_id or UNSET,
            )
            if (customer_id or merchant_customer_id)
            else UNSET
        )
        body = PaymentTokenRequest(
            customer=customer,
            payment_source=PaymentTokenRequestPaymentSource(
                card=PaymentTokenRequestCard(
                    name=card.get("name", UNSET),
                    number=card["number"],
                    expiry=card["expiry"],
                    security_code=card.get("security_code", UNSET),
                    billing_address=_address(card.get("billing_address")),
                )
            ),
        )
        with _boundary():
            token = self._client.vault.create_payment_token(
                body, pay_pal_request_id=request_id
            )
        vault_id = _present(token.id)
        if not vault_id:
            raise errors.PayPalUnreadable("PayPal did not return a vault id")
        cust = _present(token.customer)
        card_entity = None
        source = _present(token.payment_source)
        if source is not None:
            card_entity = _present(source.card)
        return {
            "vault_id": vault_id,
            "customer_id": _present(cust.id) if cust is not None else None,
            "brand": _status(card_entity.brand) if card_entity is not None else None,
            "last_digits": _present(card_entity.last_digits) if card_entity is not None else None,
            "expiry": _present(card_entity.expiry) if card_entity is not None else None,
            "name": _present(card_entity.name) if card_entity is not None else None,
        }

    def delete_card(self, vault_id):
        with _boundary():
            # Returns None; a 404 (already gone) is treated as success by callers.
            self._client.vault.delete_payment_token(vault_id)

    # -- Authorize (hold) ----------------------------------------------------
    def authorize_order(
        self,
        *,
        amount_value,
        currency,
        card=None,
        vault_id=None,
        custom_id=None,
        request_id=None,
    ):
        if vault_id:
            card_request = CardRequest(vault_id=vault_id)
        else:
            card_request = CardRequest(
                name=card.get("name", UNSET),
                number=card["number"],
                expiry=card["expiry"],
                security_code=card.get("security_code", UNSET),
                billing_address=_address(card.get("billing_address")),
            )
        body = OrderRequest(
            intent="AUTHORIZE",
            purchase_units=[
                PurchaseUnitRequest(
                    amount=AmountWithBreakdown(currency_code=currency, value=amount_value),
                    # custom_id (not invoice_id) is used for reconciliation matching:
                    # it need not be unique and surfaces as custom_field in the
                    # transaction search, whereas invoice_id must be globally unique.
                    custom_id=custom_id or UNSET,
                )
            ],
            payment_source=PaymentSource(card=card_request),
        )
        with _boundary():
            order = self._client.orders.create_order(
                body, pay_pal_request_id=request_id, prefer="return=representation"
            )
        order_id = _present(order.id)
        order_status = _status(order.status)
        if order_status == "PAYER_ACTION_REQUIRED":
            raise errors.PaymentChallengeRequired(
                "PayPal requires the shopper to approve this card in a browser"
            )
        auth = self._extract_authorization(order)
        if auth is None and order_id:
            # Fallback: order approved but not yet authorized -- authorize explicitly.
            with _boundary():
                authorized = self._client.orders.authorize_order(
                    order_id,
                    pay_pal_request_id=(request_id + "-a") if request_id else None,
                    prefer="return=representation",
                )
            auth = self._extract_authorization(authorized)
        if auth is None:
            raise errors.PayPalUnreadable("PayPal did not return an authorization")
        return {"paypal_order_id": order_id, "order_status": order_status, "authorization": auth}

    @staticmethod
    def _extract_authorization(order):
        for pu in _present(order.purchase_units) or []:
            payments = _present(pu.payments)
            if payments is None:
                continue
            for auth in _present(payments.authorizations) or []:
                if _present(auth.id):
                    amount = _money(auth.amount) or {}
                    return {
                        "id": _present(auth.id),
                        "status": _status(auth.status),
                        "amount_value": amount.get("value"),
                        "currency": amount.get("currency_code"),
                        "expiry": _present(auth.expiration_time),
                    }
        return None

    def get_authorization(self, auth_id):
        with _boundary():
            auth = self._client.payments.get_authorized_payment(auth_id)
        amount = _money(auth.amount) or {}
        return {
            "id": _present(auth.id),
            "status": _status(auth.status),
            "amount_value": amount.get("value"),
            "currency": amount.get("currency_code"),
            "expiry": _present(auth.expiration_time),
        }

    def reauthorize(self, auth_id, *, amount_value, currency, request_id=None):
        body = ReauthorizeRequest(amount=Money(currency_code=currency, value=amount_value))
        with _boundary():
            auth = self._client.payments.reauthorize_payment(
                auth_id, body=body, pay_pal_request_id=request_id
            )
        amount = _money(auth.amount) or {}
        return {
            "id": _present(auth.id),
            "status": _status(auth.status),
            "amount_value": amount.get("value"),
            "currency": amount.get("currency_code"),
            "expiry": _present(auth.expiration_time),
        }

    # -- Capture (take the money at fulfilment) ------------------------------
    def capture(self, auth_id, *, request_id=None):
        with _boundary():
            cap = self._client.payments.capture_authorized_payment(
                auth_id, pay_pal_request_id=request_id, prefer="return=representation"
            )
        capture_id = _present(cap.id)
        if not capture_id:
            raise errors.PayPalUnreadable("PayPal did not return a capture id")
        amount = _money(cap.amount) or {}
        breakdown = _present(cap.seller_receivable_breakdown)
        gross = fee = net = None
        if breakdown is not None:
            gross = (_money(breakdown.gross_amount) or {}).get("value")
            fee = (_money(breakdown.paypal_fee) or {}).get("value")
            net = (_money(breakdown.net_amount) or {}).get("value")
        return {
            "capture_id": capture_id,
            "status": _status(cap.status),
            "amount_value": amount.get("value"),
            "currency": amount.get("currency_code"),
            "gross": gross,
            "fee": fee,
            "net": net,
            "update_time": _present(cap.update_time),
        }

    # -- Void (release the hold on cancel) -----------------------------------
    def void(self, auth_id):
        """Void an authorization. A successful void answers 204 with an empty
        body, which the SDK cannot decode and surfaces as ``ValueError`` -- we
        treat that as success. A genuine failure comes back as a ``Failure``
        with a JSON body, which we translate normally."""
        try:
            result = self._client.payments.with_raw_response.void_payment(auth_id)
        except ValueError:
            return  # 204 empty body == voided
        except (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.PoolTimeout,
            httpx.ProxyError,
        ) as exc:
            raise errors.PayPalUnavailable(
                "PayPal is unreachable", http_status=502, outcome_unknown=False
            ) from exc
        except httpx.RequestError as exc:
            raise errors.PayPalUnavailable(
                "PayPal did not respond", http_status=504, outcome_unknown=True
            ) from exc
        if isinstance(result, Failure):
            raise _translate_api_error(result.error, result.response.status_code)
        # Success with a (non-empty) body -- already voided; nothing more to do.

    # -- Refund --------------------------------------------------------------
    def refund(self, capture_id, *, amount_value=None, currency=None, request_id=None):
        amount = (
            Money(currency_code=currency, value=amount_value)
            if amount_value is not None
            else UNSET
        )
        body = RefundRequest(amount=amount)
        with _boundary():
            refund = self._client.payments.refund_captured_payment(
                capture_id, body=body, pay_pal_request_id=request_id
            )
        refund_id = _present(refund.id)
        if not refund_id:
            raise errors.PayPalUnreadable("PayPal did not return a refund id")
        money = _money(refund.amount) or {}
        return {
            "refund_id": refund_id,
            "status": _status(refund.status),
            "amount_value": money.get("value"),
            "currency": money.get("currency_code"),
        }

    # -- Reconciliation (transaction search, all pages) ----------------------
    def search_transactions(self, start_date, end_date, *, currency=None):
        transactions = []
        truncated = False
        page = 1
        with _boundary():
            while True:
                response = self._client.transaction_search.search_transactions(
                    start_date,
                    end_date,
                    transaction_currency=currency or None,
                    fields="all",
                    page_size=100,
                    page=page,
                )
                for detail in _present(response.transaction_details) or []:
                    info = _present(detail.transaction_info)
                    if info is None:
                        continue
                    amount = _money(info.transaction_amount) or {}
                    transactions.append(
                        {
                            "transaction_id": _present(info.transaction_id),
                            "status": _present(info.transaction_status),
                            "value": amount.get("value"),
                            "currency": amount.get("currency_code"),
                            "initiation_date": _present(info.transaction_initiation_date),
                            "custom_field": _present(info.custom_field),
                            "event_code": _present(info.transaction_event_code),
                        }
                    )
                total_pages = _present(response.total_pages) or 1
                if page >= total_pages:
                    break
                page += 1
                if page > _MAX_TXN_PAGES:
                    truncated = True
                    break
        return {"transactions": transactions, "truncated": truncated}
