"""
Order operations driven through the API, built on Oscar's own basket and order machinery.

Each operation commits the order change first and only then messages the shopper, so a message
that cannot be sent never undoes, or fails, the order operation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from oscar.core.loading import get_class, get_model

from . import notifications
from .models import Notification

Basket = get_model('basket', 'Basket')
Country = get_model('address', 'Country')
Order = get_model('order', 'Order')
Product = get_model('catalogue', 'Product')
ShippingAddress = get_model('order', 'ShippingAddress')
OrderCreator = get_class('order.utils', 'OrderCreator')
OrderNumberGenerator = get_class('order.utils', 'OrderNumberGenerator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
Repository = get_class('shipping.repository', 'Repository')
Selector = get_class('partner.strategy', 'Selector')

DISPATCHED = 'Dispatched'
CANCELLED = 'Cancelled'

ADDRESS_FIELDS = {
    'title': 'title', 'firstName': 'first_name', 'lastName': 'last_name',
    'line1': 'line1', 'line2': 'line2', 'line3': 'line3', 'city': 'line4', 'state': 'state',
    'postcode': 'postcode', 'notes': 'notes',
}


class OrderRequestError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class RequestedLine:
    product_id: int
    quantity: int


def parse_lines(payload: Any) -> list[RequestedLine]:
    if not isinstance(payload, list) or not payload:
        raise OrderRequestError(400, '"lines" must be a non-empty list.')
    lines: list[RequestedLine] = []
    for item in payload:
        if not isinstance(item, dict):
            raise OrderRequestError(400, 'Each line must be an object.')
        product_id, quantity = item.get('productId'), item.get('quantity', 1)
        if not isinstance(product_id, int) or isinstance(product_id, bool):
            raise OrderRequestError(400, 'Each line needs an integer "productId".')
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= 99:
            raise OrderRequestError(400, '"quantity" must be an integer between 1 and 99.')
        lines.append(RequestedLine(product_id, quantity))
    return lines


def _shipping_address(data: Any) -> Any:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise OrderRequestError(400, '"shippingAddress" must be an object.')
    country_code = data.get('country')
    if not isinstance(country_code, str):
        raise OrderRequestError(400, '"shippingAddress.country" (ISO 3166-1 code) is required.')
    try:
        country = Country.objects.get(iso_3166_1_a2=country_code.upper())
    except Country.DoesNotExist:
        raise OrderRequestError(400, 'Unknown shipping country.') from None
    fields = {}
    for key, field in ADDRESS_FIELDS.items():
        value = data.get(key, '')
        if not isinstance(value, str):
            raise OrderRequestError(400, f'"shippingAddress.{key}" must be a string.')
        fields[field] = value.strip()
    if not fields['last_name'] or not fields['line1'] or not fields['line4']:
        raise OrderRequestError(400, 'A shipping address needs lastName, line1 and city.')
    return ShippingAddress(country=country, **fields)


def place_order(request: HttpRequest, lines: list[RequestedLine], address_data: Any
                ) -> tuple[Any, Notification | None]:
    user = request.user
    with transaction.atomic():
        basket = Basket.objects.create(owner=user)
        basket.strategy = Selector().strategy(request=request, user=user)
        for line in lines:
            try:
                product = Product.objects.get(pk=line.product_id)
            except Product.DoesNotExist:
                raise OrderRequestError(404, f'Product {line.product_id} not found.') from None
            info = basket.strategy.fetch_for_product(product)
            permitted, reason = info.availability.is_purchase_permitted(line.quantity)
            if not product.is_public or not permitted:
                raise OrderRequestError(
                    409, f'Product {line.product_id} cannot be bought: {reason or "unavailable"}')
            basket.add_product(product, line.quantity)
        shipping_address = _shipping_address(address_data)
        if basket.is_shipping_required() and shipping_address is None:
            raise OrderRequestError(400, 'These items need a "shippingAddress".')
        if shipping_address is not None:
            shipping_address.save()
        shipping_method = Repository().get_default_shipping_method(
            basket=basket, shipping_addr=shipping_address, user=user, request=request)
        shipping_charge = shipping_method.calculate(basket)
        total = OrderTotalCalculator(request).calculate(basket, shipping_charge)
        order = OrderCreator().place_order(
            basket=basket, total=total, shipping_method=shipping_method,
            shipping_charge=shipping_charge, user=user, shipping_address=shipping_address,
            order_number=OrderNumberGenerator().order_number(basket), request=request)
        basket.submit()
    notification = notifications.notify(
        order, Notification.KIND_PLACED,
        f'Thanks for your order {order.number} ({order.currency} {order.total_incl_tax}). '
        f'We will text you when it ships.')
    return order, notification


def dispatch(order: Any) -> list[Notification]:
    """Mark the order dispatched, tell the shopper, and queue the delivery follow-up."""
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if order.status != DISPATCHED:
            if DISPATCHED not in order.available_statuses():
                raise OrderRequestError(
                    409, f'An order in status "{order.status}" cannot be dispatched.')
            order.set_status(DISPATCHED)
    # Idempotent: a repeat (a caller retry) re-runs the steps below, which the claims de-duplicate.
    sent = []
    dispatched = notifications.notify(
        order, Notification.KIND_DISPATCHED,
        f'Good news: your order {order.number} is on its way.')
    if dispatched is not None:
        sent.append(dispatched)
    delay = timedelta(hours=float(settings.ORDER_SMS_FOLLOWUP_DELAY_HOURS))
    followup = notifications.notify(
        order, Notification.KIND_FOLLOWUP,
        f'How did the delivery of order {order.number} go? Reply and let us know.',
        send_at=(timezone.now() + delay).replace(microsecond=0))
    if followup is not None:
        sent.append(followup)
    return sent


def cancel(order: Any) -> list[Notification]:
    """Cancel the order, call off any follow-up not yet sent, and tell the shopper."""
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if order.status != CANCELLED:
            if CANCELLED not in order.available_statuses():
                raise OrderRequestError(
                    409, f'An order in status "{order.status}" cannot be cancelled.')
            order.set_status(CANCELLED)
    touched = []
    # Call off first: the follow-up must never reach the shopper, whatever happens next.
    for followup in Notification.objects.filter(order=order, kind=Notification.KIND_FOLLOWUP):
        touched.append(notifications.call_off(followup))
    cancelled = notifications.notify(
        order, Notification.KIND_CANCELLED, f'Your order {order.number} has been cancelled.')
    if cancelled is not None:
        touched.append(cancelled)
    return touched
