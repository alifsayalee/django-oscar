"""HTTP endpoints for the PayPal payments + saved-cards API.

Every capability is a separately-invocable JSON endpoint under ``/api/``.
Callers authenticate with Django's own session login; the caller's identity is
taken from ``request.user``. Shopper endpoints act only on the caller's own data;
``fulfil``/``cancel``/``reconciliation`` are operator actions restricted to
``is_staff`` users.

Payment operations are idempotent in effect (see the persisted request-ids and
state guards): a double-click never authorizes or captures twice, and a repeated
refund under the same idempotency key never refunds twice.
"""

import datetime
import functools
import json
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from oscar.core.loading import get_model

from . import order_service
from .exceptions import PayPalError, OperationConflict
from .models import (
    PayPalCustomer,
    PayPalPayment,
    RefundRecord,
    SavedPaymentMethod,
)
from .paypal_service import PayPalService

Order = get_model('order', 'Order')

# A capturable authorization is renewed if it will expire within this buffer.
_STALE_BUFFER = datetime.timedelta(minutes=5)
# PayPal's reporting API caps a query window at 31 days; we chunk longer ranges.
_RECON_WINDOW = datetime.timedelta(days=31)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _json(data, status=200):
    return JsonResponse(data, status=status)


def _error(status, message, **extra):
    payload = {'error': message}
    payload.update(extra)
    return JsonResponse(payload, status=status)


def _parse_body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise _BadRequest('Request body must be valid JSON.')
    if not isinstance(data, dict):
        raise _BadRequest('Request body must be a JSON object.')
    return data


class _BadRequest(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def shopper_required(view):
    """Require an authenticated (logged-in) caller."""
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _error(401, 'Authentication required.')
        return view(request, *args, **kwargs)
    return wrapper


def staff_required(view):
    """Require an authenticated staff (operator) caller."""
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _error(401, 'Authentication required.')
        if not request.user.is_staff:
            return _error(403, 'Operator (staff) privileges required.')
        return view(request, *args, **kwargs)
    return wrapper


def _handle_service_errors(func):
    """Translate service/order/domain exceptions into JSON error responses."""
    @functools.wraps(func)
    def wrapper(request, *args, **kwargs):
        try:
            return func(request, *args, **kwargs)
        except _BadRequest as exc:
            return _error(400, exc.message)
        except order_service.OrderError as exc:
            return _error(exc.http_status, exc.message)
        except PayPalError as exc:
            extra = {}
            if getattr(exc, 'debug_id', None):
                extra['debugId'] = exc.debug_id
            if getattr(exc, 'details', None):
                extra['details'] = exc.details
            return _error(exc.http_status, exc.message, **extra)
    return wrapper


def _get_own_order(request, order_id):
    """Fetch an order the caller owns, or raise a 404-style bad request."""
    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        raise _BadRequest('Order not found.')
    if order.user_id != request.user.id:
        # Do not reveal existence of another shopper's order.
        raise _BadRequest('Order not found.')
    return order


def _get_any_order(order_id):
    try:
        return Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        raise _BadRequest('Order not found.')


def _payment_state(payment):
    return {
        'state': payment.state,
        'currency': payment.currency,
        'paypalOrderId': payment.paypal_order_id or None,
        'authorizationId': payment.authorization_id or None,
        'authorizationStatus': payment.authorization_status or None,
        'authorizationExpiry': (
            payment.authorization_expiry.isoformat()
            if payment.authorization_expiry else None),
        'captureId': payment.capture_id or None,
        'captureStatus': payment.capture_status or None,
        'grossAmount': _str(payment.gross_amount),
        'paypalFee': _str(payment.paypal_fee),
        'netAmount': _str(payment.net_amount),
        'amountRefunded': _str(payment.amount_refunded),
        'refundableAmount': _str(payment.refundable_amount),
    }


def _str(value):
    return None if value is None else str(value)


def _order_summary(order):
    payment = getattr(order, 'paypal_payment', None)
    data = {
        'orderId': order.id,
        'number': order.number,
        'status': order.status,
        'total': str(order.total_incl_tax),
        'orderCurrency': order.currency,
        'datePlaced': order.date_placed.isoformat() if order.date_placed else None,
        'payment': _payment_state(payment) if payment else None,
    }
    return data


def _card_from_request(data):
    """Build a PayPal card dict from caller-supplied one-off card details."""
    card = data.get('card')
    if not isinstance(card, dict):
        return None
    number = card.get('number')
    expiry = card.get('expiry')
    if not number or not expiry:
        raise _BadRequest('A one-off card needs "number" and "expiry" (YYYY-MM).')
    built = {'number': str(number), 'expiry': str(expiry)}
    if card.get('name'):
        built['name'] = str(card['name'])
    if card.get('securityCode'):
        built['security_code'] = str(card['securityCode'])
    billing = card.get('billingAddress')
    if isinstance(billing, dict) and billing.get('countryCode'):
        built['billing_address'] = _billing_address(billing)
    return built


def _billing_address(billing):
    address = {'country_code': str(billing['countryCode'])}
    if billing.get('addressLine1'):
        address['address_line_1'] = str(billing['addressLine1'])
    if billing.get('addressLine2'):
        address['address_line_2'] = str(billing['addressLine2'])
    if billing.get('adminArea2'):
        address['admin_area_2'] = str(billing['adminArea2'])
    if billing.get('adminArea1'):
        address['admin_area_1'] = str(billing['adminArea1'])
    if billing.get('postalCode'):
        address['postal_code'] = str(billing['postalCode'])
    return address


# ---------------------------------------------------------------------------
# Flow 1 — orders & payments
# ---------------------------------------------------------------------------

@csrf_exempt
@require_http_methods(['POST'])
@shopper_required
@_handle_service_errors
def create_order(request):
    data = _parse_body(request)
    items = data.get('items')
    if not isinstance(items, list) or not items:
        raise _BadRequest('"items" must be a non-empty list of {id, quantity}.')
    with transaction.atomic():
        order = order_service.place_order(request.user, items)
        payment = PayPalPayment.objects.create(
            order=order,
            currency=settings.PAYPAL_CURRENCY,
            state=PayPalPayment.AWAITING_PAYMENT,
        )
    return _json({
        'orderId': order.id,
        'number': order.number,
        'status': order.status,
        'total': str(order.total_incl_tax),
        'currency': payment.currency,
        'payment': _payment_state(payment),
    }, status=201)


@csrf_exempt
@require_http_methods(['POST'])
@shopper_required
@_handle_service_errors
def pay_order(request, order_id):
    data = _parse_body(request)
    order = _get_own_order(request, order_id)
    service = PayPalService()

    with transaction.atomic():
        payment = _payment_for_update(order)
        # Idempotency: an existing hold is returned unchanged on a repeat call.
        if payment.authorization_id and payment.state == PayPalPayment.AUTHORIZED:
            return _json({'orderId': order.id, 'payment': _payment_state(payment)})
        if payment.state not in (PayPalPayment.AWAITING_PAYMENT,):
            return _error(409, 'Order is not awaiting payment (state=%s).' % payment.state)

        payment_source = _resolve_payment_source(request, data)

        # Persist the idempotency key BEFORE the call so a retry (after a crash
        # or a double-click) reuses it and PayPal collapses the duplicate rather
        # than authorizing twice.
        if not payment.authorize_request_id:
            payment.authorize_request_id = uuid.uuid4().hex
            payment.save(update_fields=['authorize_request_id'])

        # Single-step: create the PayPal order with the card/vaulted source and
        # intent AUTHORIZE, which holds the funds and returns the authorization.
        # invoice_id must be unique per transaction (account setting); custom_id
        # carries the order number so reconciliation can match it back.
        invoice_id = '%s-%s' % (order.number, payment.authorize_request_id[:12])
        result = service.create_authorized_order(
            amount=order.total_incl_tax,
            currency=payment.currency,
            invoice_id=invoice_id,
            custom_id=str(order.number),
            description='Order %s' % order.number,
            payment_source=payment_source,
            request_id=payment.authorize_request_id,
        )
        payment.paypal_order_id = result['paypal_order_id']
        payment.authorization_id = result['authorization_id']
        payment.authorization_status = result['status']
        payment.authorization_expiry = result['expiry']
        payment.state = PayPalPayment.AUTHORIZED
        payment.save()
        order_service.record_authorization(
            order, order.total_incl_tax, payment.currency,
            reference=payment.authorization_id)

    return _json({'orderId': order.id, 'payment': _payment_state(payment)})


def _resolve_payment_source(request, data):
    """Return the PayPal payment_source dict from a saved card or one-off card."""
    method_id = data.get('paymentMethodId')
    if method_id is not None:
        try:
            saved = SavedPaymentMethod.objects.get(
                id=method_id, user=request.user, is_active=True)
        except SavedPaymentMethod.DoesNotExist:
            raise _BadRequest('Saved payment method not found.')
        return {'card': {'vault_id': saved.paypal_token_id}}
    card = _card_from_request(data)
    if card is None:
        raise _BadRequest(
            'Provide either "paymentMethodId" or a one-off "card".')
    return {'card': card}


@csrf_exempt
@require_http_methods(['POST'])
@staff_required
@_handle_service_errors
def fulfil_order(request, order_id):
    order = _get_any_order(order_id)
    service = PayPalService()

    with transaction.atomic():
        payment = _payment_for_update(order)
        if payment.state == PayPalPayment.CAPTURED:
            return _json({'orderId': order.id, 'payment': _payment_state(payment)})
        if payment.state != PayPalPayment.AUTHORIZED:
            return _error(409, 'Order cannot be fulfilled (state=%s).' % payment.state)

        authorization_id = _ensure_capturable_authorization(service, payment)

        if not payment.capture_request_id:
            payment.capture_request_id = uuid.uuid4().hex
            payment.save(update_fields=['capture_request_id'])

        result = service.capture(
            authorization_id=authorization_id,
            currency=payment.currency,
            request_id=payment.capture_request_id,
        )
        payment.capture_id = result['capture_id']
        payment.capture_status = result['status']
        payment.gross_amount = result['gross_amount']
        payment.paypal_fee = result['paypal_fee']
        payment.net_amount = result['net_amount']
        payment.state = PayPalPayment.CAPTURED
        payment.save()
        order_service.record_capture(
            order, result['gross_amount'] or order.total_incl_tax,
            reference=payment.capture_id)
        _try_set_status(order, 'Being processed')

    return _json({'orderId': order.id, 'payment': _payment_state(payment)})


def _ensure_capturable_authorization(service, payment):
    """Return an authorization id that can be captured, renewing a stale one.

    A hold that has expired (or is about to) is reauthorized. One that can no
    longer be renewed raises an operator-actionable conflict.
    """
    expiry = payment.authorization_expiry
    stale = expiry is not None and timezone.now() >= (expiry - _STALE_BUFFER)
    if not stale:
        return payment.authorization_id

    try:
        renewed = service.reauthorize(
            authorization_id=payment.authorization_id,
            request_id=uuid.uuid4().hex,
        )
    except PayPalError as exc:
        raise OperationConflict(
            'The payment hold for this order has expired and can no longer be '
            'renewed (%s). Ask the shopper to pay again before fulfilling.'
            % exc.message,
        ) from exc

    status = (renewed['status'] or '').upper()
    if status in ('DENIED', 'EXPIRED', 'VOIDED'):
        raise OperationConflict(
            'The payment hold for this order is %s and can no longer be '
            'captured. Ask the shopper to pay again before fulfilling.' % status)

    payment.authorization_id = renewed['authorization_id']
    payment.authorization_status = renewed['status']
    payment.authorization_expiry = renewed['expiry']
    payment.save(update_fields=[
        'authorization_id', 'authorization_status', 'authorization_expiry'])
    return payment.authorization_id


@csrf_exempt
@require_http_methods(['POST'])
@staff_required
@_handle_service_errors
def cancel_order(request, order_id):
    order = _get_any_order(order_id)
    service = PayPalService()

    with transaction.atomic():
        payment = _payment_for_update(order)
        if payment.state == PayPalPayment.VOIDED:
            return _json({'orderId': order.id, 'payment': _payment_state(payment)})
        if payment.state == PayPalPayment.CAPTURED:
            return _error(
                409, 'Order is already captured; issue a refund instead of cancelling.')
        if payment.state != PayPalPayment.AUTHORIZED:
            return _error(409, 'Order cannot be cancelled (state=%s).' % payment.state)

        service.void(
            authorization_id=payment.authorization_id,
            request_id=uuid.uuid4().hex,
        )
        payment.state = PayPalPayment.VOIDED
        payment.authorization_status = 'VOIDED'
        payment.save(update_fields=['state', 'authorization_status'])
        _try_set_status(order, 'Cancelled')

    return _json({'orderId': order.id, 'payment': _payment_state(payment)})


@csrf_exempt
@require_http_methods(['POST'])
@staff_required
@_handle_service_errors
def refund_order(request, order_id):
    data = _parse_body(request)
    order = _get_any_order(order_id)
    service = PayPalService()

    idempotency_key = data.get('idempotencyKey')
    if not idempotency_key:
        raise _BadRequest('"idempotencyKey" is required for refunds.')
    idempotency_key = str(idempotency_key)

    amount = _parse_amount(data.get('amount'))

    with transaction.atomic():
        payment = _payment_for_update(order)
        if payment.state not in (
                PayPalPayment.CAPTURED, PayPalPayment.PARTIALLY_REFUNDED):
            return _error(
                409, 'Order has no captured payment to refund (state=%s).'
                % payment.state)

        # Idempotency: a repeat under the same key returns the stored refund.
        existing = payment.refunds.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return _json({
                'orderId': order.id,
                'refundId': existing.paypal_refund_id,
                'refund': existing.describe(),
                'payment': _payment_state(payment),
            })

        refundable = payment.refundable_amount
        if amount is None:
            amount = refundable
        if amount <= Decimal('0.00'):
            raise _BadRequest('Refund amount must be positive.')
        if amount > refundable:
            return _error(
                409,
                'Refund of %s exceeds the refundable amount of %s.'
                % (amount, refundable))

        result = service.refund(
            capture_id=payment.capture_id,
            amount=amount,
            currency=payment.currency,
            request_id=idempotency_key,
        )
        record = RefundRecord.objects.create(
            payment=payment,
            idempotency_key=idempotency_key,
            paypal_refund_id=result['refund_id'],
            amount=result['amount'] or amount,
            status=result['status'],
        )
        payment.amount_refunded = (payment.amount_refunded or Decimal('0.00')) + record.amount
        if payment.amount_refunded >= payment.captured_amount:
            payment.state = PayPalPayment.REFUNDED
        else:
            payment.state = PayPalPayment.PARTIALLY_REFUNDED
        payment.save(update_fields=['amount_refunded', 'state'])
        order_service.record_refund(order, record.amount, reference=record.paypal_refund_id)

    return _json({
        'orderId': order.id,
        'refundId': record.paypal_refund_id,
        'refund': record.describe(),
        'payment': _payment_state(payment),
    }, status=201)


@csrf_exempt
@require_http_methods(['GET'])
@shopper_required
@_handle_service_errors
def my_orders(request):
    orders = (Order.objects
              .filter(user=request.user)
              .select_related('paypal_payment')
              .order_by('-date_placed'))
    return _json({'orders': [_order_summary(o) for o in orders]})


# ---------------------------------------------------------------------------
# Flow 2 — saved cards
# ---------------------------------------------------------------------------

@csrf_exempt
@require_http_methods(['POST', 'GET'])
@shopper_required
@_handle_service_errors
def payment_methods(request):
    if request.method == 'GET':
        methods = SavedPaymentMethod.objects.filter(
            user=request.user, is_active=True)
        return _json({'paymentMethods': [m.describe() for m in methods]})

    data = _parse_body(request)
    card = _card_from_request(data)
    if card is None:
        raise _BadRequest('A "card" object with number and expiry is required.')
    service = PayPalService()

    customer = getattr(request.user, 'paypal_customer', None)
    customer_id = customer.paypal_customer_id if customer else None

    result = service.vault_card(
        card=card,
        customer_id=customer_id,
        request_id=uuid.uuid4().hex,
    )

    with transaction.atomic():
        if result['customer_id'] and customer is None:
            PayPalCustomer.objects.get_or_create(
                user=request.user,
                defaults={'paypal_customer_id': result['customer_id']})
        label = data.get('label') or (
            '%s ****%s' % (result['brand'] or 'Card', result['last_digits']))
        saved = SavedPaymentMethod.objects.create(
            user=request.user,
            paypal_token_id=result['token_id'],
            brand=result['brand'],
            last_digits=result['last_digits'],
            expiry=result['expiry'],
            label=label,
        )

    body = {'paymentMethodId': saved.id}
    body.update(saved.describe())
    return _json(body, status=201)


@csrf_exempt
@require_http_methods(['DELETE'])
@shopper_required
@_handle_service_errors
def delete_payment_method(request, method_id):
    try:
        saved = SavedPaymentMethod.objects.get(
            id=method_id, user=request.user, is_active=True)
    except SavedPaymentMethod.DoesNotExist:
        return _error(404, 'Saved payment method not found.')

    service = PayPalService()
    service.delete_vault_token(saved.paypal_token_id)
    # Removed from PayPal's vault; remove it here too so it can no longer be
    # seen or used to pay.
    saved.delete()
    return _json({'deleted': True, 'paymentMethodId': method_id})


# ---------------------------------------------------------------------------
# reconciliation (operator)
# ---------------------------------------------------------------------------

@csrf_exempt
@require_http_methods(['GET'])
@staff_required
@_handle_service_errors
def reconciliation(request):
    start = _parse_iso(request.GET.get('from'), 'from')
    end = _parse_iso(request.GET.get('to'), 'to')
    if end <= start:
        raise _BadRequest('"to" must be after "from".')

    service = PayPalService()
    paypal_records = []
    window_start = start
    while window_start < end:
        window_end = min(window_start + _RECON_WINDOW, end)
        paypal_records.extend(service.search_transactions(
            start_date=_paypal_time(window_start),
            end_date=_paypal_time(window_end),
        ))
        window_start = window_end

    # Line PayPal's records up against this app's captured payments.
    app_payments = (PayPalPayment.objects
                    .filter(capture_id__gt='',
                            date_updated__gte=start,
                            date_updated__lte=end)
                    .select_related('order'))
    app_by_invoice = {p.order.number: p for p in app_payments}

    paypal_invoices = set()
    matched = []
    paypal_only = []
    for record in paypal_records:
        # custom_field carries the order number; invoice_id is unique-per-txn.
        invoice = record.get('custom_field') or record.get('invoice_id')
        if invoice:
            paypal_invoices.add(invoice)
        app_payment = app_by_invoice.get(invoice) if invoice else None
        entry = {'paypal': record}
        if app_payment is not None:
            entry['orderId'] = app_payment.order_id
            entry['orderNumber'] = app_payment.order.number
            entry['appState'] = app_payment.state
            matched.append(entry)
        else:
            paypal_only.append(record)

    app_only = [
        {
            'orderId': payment.order_id,
            'orderNumber': payment.order.number,
            'appState': payment.state,
            'captureId': payment.capture_id,
            'grossAmount': _str(payment.gross_amount),
        }
        for invoice, payment in app_by_invoice.items()
        if invoice not in paypal_invoices
    ]

    return _json({
        'from': start.isoformat(),
        'to': end.isoformat(),
        'paypalTransactionCount': len(paypal_records),
        'matched': matched,
        'paypalOnly': paypal_only,
        'appOnly': app_only,
    })


# ---------------------------------------------------------------------------
# small internals
# ---------------------------------------------------------------------------

def _payment_for_update(order):
    """Fetch (locking where supported) the order's PayPalPayment, creating it
    if the order predates this app."""
    payment = (PayPalPayment.objects
               .select_for_update()
               .filter(order=order)
               .first())
    if payment is None:
        payment, _ = PayPalPayment.objects.get_or_create(
            order=order,
            defaults={'currency': settings.PAYPAL_CURRENCY})
    return payment


def _try_set_status(order, new_status):
    """Move the Oscar order to ``new_status`` if the pipeline allows it."""
    try:
        if new_status in order.available_statuses():
            order.set_status(new_status)
    except Exception:
        # Status pipeline is advisory here; payment state is authoritative.
        pass


def _parse_amount(raw):
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise _BadRequest('"amount" must be a decimal string.')


def _parse_iso(raw, name):
    if not raw:
        raise _BadRequest('Query parameter "%s" (ISO-8601 date-time) is required.' % name)
    parsed = _parse_datetime_any(raw)
    if parsed is None:
        raise _BadRequest('"%s" must be an ISO-8601 date-time.' % name)
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime.timezone.utc)
    return parsed


def _parse_datetime_any(raw):
    from django.utils.dateparse import parse_datetime
    parsed = parse_datetime(raw)
    if parsed is not None:
        return parsed
    # Accept a bare date too.
    from django.utils.dateparse import parse_date
    d = parse_date(raw)
    if d is not None:
        return datetime.datetime(d.year, d.month, d.day)
    return None


def _paypal_time(dt):
    """Format a datetime as the RFC3339 string PayPal's reporting API expects."""
    return dt.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S-0000')
