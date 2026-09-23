"""Thin, well-bounded wrapper over the PayPal SDK.

Every SDK call goes through :meth:`PayPalService._call`, which converts the four
failure kinds the SDK can raise (per python-error-handling) into this app's own
small exception family:

* a failed token fetch (``OAuthProviderError``) -> ``PayPalConfigError`` (our
  misconfiguration; nothing was sent);
* a typed PayPal rejection (``Error``) -> ``PayPalRejected`` (client 4xx);
* an opaque/undocumented error (``RawError``) -> ``PayPalFailure``;
* a decode failure (``ValidationError``/``ValueError``) -> ``PayPalUnreadable``
  (outcome unknown);
* a transport failure (``httpx.HTTPError``) -> ``PayPalUnavailable``.

Request models are built from the contract sheet in ``pay-pal-server-sdk-plan.md``.
Write responses are asserted for the ids we depend on immediately, since an
absent id on a 2xx means the outcome is unknown, not that nothing happened.

Money is represented on the wire as a decimal string scaled to the currency; we
build every amount with ``Decimal`` and format to two places, never via float.
"""

import contextlib
from decimal import ROUND_HALF_UP, Decimal

import httpx
from django.utils.dateparse import parse_datetime
from pydantic import ValidationError

from paypal.core import (
    UNSET,
    ApiError,
    Failure,
    OAuthProviderError,
    RawError,
    Success,
)
from paypal.models import Error

from . import client as client_module
from .exceptions import (
    PayPalConfigError,
    PayPalFailure,
    PayPalRejected,
    PayPalUnavailable,
    PayPalUnreadable,
)

AUTHORIZE_INTENT = 'AUTHORIZE'


def _money(amount, currency):
    """Return the PayPal money dict for a Decimal amount and currency."""
    value = Decimal(amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return {'currency_code': currency, 'value': f'{value:.2f}'}


def _v(value):
    """Resolve an SDK member to a plain value, mapping the UNSET sentinel to None."""
    if value is UNSET:
        return None
    return value


def _error_details(error):
    """Extract PayPal's per-issue error details into plain dicts for the caller."""
    details = _v(error.details) or []
    out = []
    for detail in details:
        out.append({
            'issue': _v(detail.issue),
            'field': _v(detail.field),
            'description': _v(detail.description),
        })
    return out


def _decimal_or_none(money):
    money = _v(money)
    if money is None:
        return None
    value = _v(money.value)
    return None if value is None else Decimal(value)


class PayPalService:
    """Stateless helper around the PayPal SDK client.

    Accepts an explicit client for testing (transport stub); defaults to the
    process-wide client.
    """

    def __init__(self, client=None):
        self._client = client

    @property
    def client(self):
        if self._client is not None:
            return self._client
        return client_module.get_client()

    # -- error boundary ---------------------------------------------------

    @contextlib.contextmanager
    def _call(self, action):
        """Run an SDK call, translating every failure kind into a boundary error.

        ``action`` is a short human phrase used in error messages/logs.
        """
        try:
            yield
        except ApiError as exc:
            error = exc.error
            # Auth/token failures surface here (lazy token fetch) but mean our
            # configuration is wrong; nothing was sent. Check first.
            if isinstance(error, OAuthProviderError):
                raise PayPalConfigError(
                    'PayPal credentials were rejected (%s).' % _v(error.error)
                ) from exc
            if isinstance(error, Error):
                raise PayPalRejected(
                    exc.status_code,
                    '%s: %s' % (action, error.message),
                    debug_id=error.debug_id,
                    name=error.name,
                    details=_error_details(error),
                ) from exc
            # RawError arm — undocumented status/opaque body.
            detail = ''
            if isinstance(error, RawError):
                with contextlib.suppress(Exception):
                    detail = error.text()
            raise PayPalFailure(
                'PayPal returned an unexpected error while %s (HTTP %s).'
                % (action, exc.status_code),
                http_status=502,
            ) from exc
        except ValidationError as exc:
            raise PayPalUnreadable(
                'PayPal returned an unreadable response while %s; outcome unknown.'
                % action
            ) from exc
        except httpx.HTTPError as exc:
            raise PayPalUnavailable(
                'PayPal could not be reached while %s.' % action
            ) from exc

    # -- orders / authorization ------------------------------------------

    def create_order(self, *, amount, currency, invoice_id, description,
                     request_id, custom_id=None):
        """Create a PayPal Orders v2 order with intent AUTHORIZE. Returns its id."""
        body = {
            'intent': AUTHORIZE_INTENT,
            'purchase_units': [self._purchase_unit(
                amount, currency, invoice_id, custom_id, description)],
        }
        with self._call('creating the PayPal order'):
            order = self.client.orders.create_order(body, pay_pal_request_id=request_id)
        order_id = _v(order.id)
        if not order_id:
            raise PayPalUnreadable(
                'PayPal did not return an order id; outcome unknown.')
        return order_id

    @staticmethod
    def _purchase_unit(amount, currency, invoice_id, custom_id, description):
        """Build a purchase unit.

        ``invoice_id`` must be unique per transaction (the merchant account
        enforces this); ``custom_id`` carries the app's order number so PayPal's
        transaction reporting can be reconciled back to an order.
        """
        return {
            'amount': _money(amount, currency),
            'invoice_id': invoice_id,
            'custom_id': custom_id if custom_id is not None else invoice_id,
            'description': (description or '')[:127],
        }

    def create_authorized_order(self, *, amount, currency, invoice_id, description,
                                payment_source, request_id, custom_id=None):
        """Create an order and authorize (hold) the total in one call.

        For direct-card and vaulted-card sources PayPal processes the card at
        create time when ``payment_source`` is supplied with intent AUTHORIZE,
        creating the authorization inline (single-step). The ``PayPal-Request-Id``
        makes the whole create idempotent, so a retry returns the same order and
        authorization rather than charging twice.
        """
        body = {
            'intent': AUTHORIZE_INTENT,
            'purchase_units': [self._purchase_unit(
                amount, currency, invoice_id, custom_id, description)],
            'payment_source': payment_source,
        }
        with self._call('authorizing the payment'):
            order = self.client.orders.create_order(
                body,
                pay_pal_request_id=request_id,
                prefer='return=representation',
            )
        order_id = _v(order.id)
        auth = self._first_authorization(order)
        if not order_id or auth is None or not _v(auth.id):
            raise PayPalUnreadable(
                'PayPal did not return an authorization; outcome unknown.')
        return {
            'paypal_order_id': order_id,
            'order_status': str(_v(order.status) or ''),
            'authorization_id': _v(auth.id),
            'status': str(_v(auth.status) or ''),
            'expiry': self._parse_dt(_v(auth.expiration_time)),
        }

    def authorize_order(self, *, paypal_order_id, payment_source, request_id):
        """Authorize (hold) the order total. Returns dict with authorization info."""
        body = {'payment_source': payment_source}
        with self._call('authorizing the payment'):
            resp = self.client.orders.authorize_order(
                paypal_order_id,
                body=body,
                pay_pal_request_id=request_id,
                prefer='return=representation',
            )
        auth = self._first_authorization(resp)
        if auth is None or not _v(auth.id):
            raise PayPalUnreadable(
                'PayPal did not return an authorization id; outcome unknown.')
        return {
            'authorization_id': _v(auth.id),
            'status': str(_v(auth.status) or ''),
            'expiry': self._parse_dt(_v(auth.expiration_time)),
        }

    def get_authorization(self, authorization_id):
        with self._call('reading the authorization'):
            auth = self.client.payments.get_authorized_payment(authorization_id)
        return {
            'authorization_id': _v(auth.id),
            'status': str(_v(auth.status) or ''),
            'expiry': self._parse_dt(_v(auth.expiration_time)),
        }

    def reauthorize(self, *, authorization_id, request_id):
        with self._call('renewing the authorization'):
            auth = self.client.payments.reauthorize_payment(
                authorization_id,
                pay_pal_request_id=request_id,
                prefer='return=representation',
            )
        new_id = _v(auth.id)
        if not new_id:
            raise PayPalUnreadable(
                'PayPal did not return a renewed authorization; outcome unknown.')
        return {
            'authorization_id': new_id,
            'status': str(_v(auth.status) or ''),
            'expiry': self._parse_dt(_v(auth.expiration_time)),
        }

    def capture(self, *, authorization_id, currency, request_id):
        """Capture (take) the authorized payment. Returns capture + fee breakdown."""
        with self._call('capturing the payment'):
            cap = self.client.payments.capture_authorized_payment(
                authorization_id,
                body={'final_capture': True},
                pay_pal_request_id=request_id,
                prefer='return=representation',
            )
        capture_id = _v(cap.id)
        if not capture_id:
            raise PayPalUnreadable(
                'PayPal did not return a capture id; outcome unknown.')
        breakdown = _v(cap.seller_receivable_breakdown)
        gross = fee = net = None
        if breakdown is not None:
            gross = _decimal_or_none(breakdown.gross_amount)
            fee = _decimal_or_none(breakdown.paypal_fee)
            net = _decimal_or_none(breakdown.net_amount)
        return {
            'capture_id': capture_id,
            'status': str(_v(cap.status) or ''),
            'gross_amount': gross,
            'paypal_fee': fee,
            'net_amount': net,
            'currency': currency,
        }

    def void(self, *, authorization_id, request_id):
        """Void (release) the authorization. Returns the resulting status."""
        # return=representation makes void answer 200 with a body; the default
        # (minimal) answers 204 with an empty body the SDK cannot decode.
        with self._call('cancelling the authorization'):
            auth = self.client.payments.void_payment(
                authorization_id,
                pay_pal_request_id=request_id,
                prefer='return=representation',
            )
        return {'status': str(_v(auth.status) or 'VOIDED')}

    def refund(self, *, capture_id, amount, currency, request_id):
        """Refund a captured payment, in full (amount=None) or in part."""
        body = {}
        if amount is not None:
            body['amount'] = _money(amount, currency)
        with self._call('refunding the payment'):
            refund = self.client.payments.refund_captured_payment(
                capture_id,
                body=body or None,
                pay_pal_request_id=request_id,
                prefer='return=representation',
            )
        refund_id = _v(refund.id)
        if not refund_id:
            raise PayPalUnreadable(
                'PayPal did not return a refund id; outcome unknown.')
        refunded = _decimal_or_none(refund.amount)
        return {
            'refund_id': refund_id,
            'status': str(_v(refund.status) or ''),
            'amount': refunded,
        }

    # -- vault (saved cards) ---------------------------------------------

    def vault_card(self, *, card, customer_id, request_id):
        """Vault a card. Returns token id, customer id and safe display fields."""
        payment_source = {'card': card}
        body = {'payment_source': payment_source}
        if customer_id:
            body['customer'] = {'id': customer_id}
        with self._call('saving the card'):
            token = self.client.vault.create_payment_token(
                body, pay_pal_request_id=request_id)
        token_id = _v(token.id)
        if not token_id:
            raise PayPalUnreadable(
                'PayPal did not return a vault token id; outcome unknown.')
        result = {
            'token_id': token_id,
            'customer_id': None,
            'brand': '',
            'last_digits': '',
            'expiry': '',
        }
        customer = _v(token.customer)
        if customer is not None:
            result['customer_id'] = _v(customer.id)
        source = _v(token.payment_source)
        if source is not None:
            card_entity = _v(source.card)
            if card_entity is not None:
                result['brand'] = str(_v(card_entity.brand) or '')
                result['last_digits'] = str(_v(card_entity.last_digits) or '')
                result['expiry'] = str(_v(card_entity.expiry) or '')
        return result

    def delete_vault_token(self, token_id):
        """Delete a vaulted token. Returns True on success (204)."""
        with self._call('removing the saved card'):
            result = self.client.vault.with_raw_response.delete_payment_token(token_id)
        if isinstance(result, Success):
            return True
        if isinstance(result, Failure):
            error = result.error
            status = result.response.status_code
            if isinstance(error, Error):
                raise PayPalRejected(
                    status,
                    'removing the saved card: %s' % error.message,
                    debug_id=error.debug_id,
                    name=error.name,
                )
            raise PayPalFailure(
                'PayPal returned an unexpected error while removing the saved '
                'card (HTTP %s).' % status)
        return True

    def list_vault_tokens(self, customer_id):
        """List a customer's vaulted tokens (used for verification/reconciliation)."""
        with self._call('listing saved cards'):
            resp = self.client.vault.list_customer_payment_tokens(customer_id)
        tokens = _v(resp.payment_tokens) or []
        return [_v(t.id) for t in tokens if _v(t.id)]

    # -- reconciliation ---------------------------------------------------

    def search_transactions(self, *, start_date, end_date):
        """Return every PayPal transaction in [start_date, end_date].

        Walks all pages so the report covers the whole range, not just page 1.
        Each record is normalised to a plain dict.
        """
        records = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            with self._call('reading PayPal transactions'):
                resp = self.client.transaction_search.search_transactions(
                    start_date, end_date,
                    fields='transaction_info',
                    page_size=100,
                    page=page,
                )
            total_pages = _v(resp.total_pages) or 1
            for detail in (_v(resp.transaction_details) or []):
                info = _v(detail.transaction_info)
                if info is None:
                    continue
                amount = _decimal_or_none(info.transaction_amount)
                money = _v(info.transaction_amount)
                records.append({
                    'transaction_id': _v(info.transaction_id),
                    'invoice_id': _v(info.invoice_id),
                    'custom_field': _v(info.custom_field),
                    'status': _v(info.transaction_status),
                    'amount': None if amount is None else str(amount),
                    'currency': None if money is None else _v(money.currency_code),
                    'initiation_date': _v(info.transaction_initiation_date),
                })
            page += 1
        return records

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _first_authorization(resp):
        units = _v(resp.purchase_units) or []
        for unit in units:
            payments = _v(unit.payments)
            if payments is None:
                continue
            authorizations = _v(payments.authorizations) or []
            if authorizations:
                return authorizations[0]
        return None

    @staticmethod
    def _parse_dt(text):
        if not text:
            return None
        return parse_datetime(text)
