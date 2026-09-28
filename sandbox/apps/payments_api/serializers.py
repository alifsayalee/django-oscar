"""Request parsing (never echoing card data) and response bodies."""

import re
from datetime import datetime, timezone as dt_timezone

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import PayPalPayment
from .paypal_gateway.money import parse_amount
from .paypal_gateway.operations import BillingAddress, CardInput
from .services import ApiProblem, ShippingInput

MAX_LINES = 50
MAX_QUANTITY = 99


def _invalid(field, message):
    return ApiProblem(400, "invalid_request", message, field=field)


def _text(data, key, field, *, required=True, max_length=255):
    value = data.get(key)
    if value is None or value == "":
        if required:
            raise _invalid(field, f"{field} is required.")
        return ""
    if not isinstance(value, str) or len(value) > max_length:
        raise _invalid(field, f"{field} must be a string of at most {max_length} characters.")
    return value.strip()


def parse_order_request(data):
    items = data.get("items")
    if not isinstance(items, list) or not items or len(items) > MAX_LINES:
        raise _invalid("items", f"items must be a list of 1 to {MAX_LINES} entries.")
    parsed = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise _invalid(f"items[{i}]", "Each item must be an object.")
        product_id, quantity = item.get("productId"), item.get("quantity", 1)
        if isinstance(product_id, bool) or not isinstance(product_id, int):
            raise _invalid(f"items[{i}].productId", "productId must be an integer catalogue id.")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_QUANTITY:
            raise _invalid(f"items[{i}].quantity", f"quantity must be an integer from 1 to {MAX_QUANTITY}.")
        parsed.append((product_id, quantity))

    shipping = None
    raw = data.get("shippingAddress")
    if raw is not None:
        if not isinstance(raw, dict):
            raise _invalid("shippingAddress", "shippingAddress must be an object.")
        shipping = ShippingInput(
            first_name=_text(raw, "firstName", "shippingAddress.firstName"),
            last_name=_text(raw, "lastName", "shippingAddress.lastName"),
            line1=_text(raw, "line1", "shippingAddress.line1"),
            line2=_text(raw, "line2", "shippingAddress.line2", required=False),
            city=_text(raw, "city", "shippingAddress.city"),
            state=_text(raw, "state", "shippingAddress.state", required=False),
            postcode=_text(raw, "postcode", "shippingAddress.postcode", max_length=64),
            country_code=_text(raw, "countryCode", "shippingAddress.countryCode", max_length=2),
            phone_number=_text(raw, "phoneNumber", "shippingAddress.phoneNumber", required=False, max_length=32),
        )
    return parsed, shipping


def _luhn_ok(number):
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


_EXPIRY_ISO = re.compile(r"^(\d{4})-(\d{2})$")
_EXPIRY_SHORT = re.compile(r"^(\d{2})\s*/\s*(\d{2}|\d{4})$")


def _expiry(value):
    if not isinstance(value, str):
        raise _invalid("card.expiry", "card.expiry must be YYYY-MM or MM/YY.")
    m = _EXPIRY_ISO.match(value.strip())
    if m:
        year, month = int(m.group(1)), int(m.group(2))
    else:
        m = _EXPIRY_SHORT.match(value.strip())
        if not m:
            raise _invalid("card.expiry", "card.expiry must be YYYY-MM or MM/YY.")
        month, year = int(m.group(1)), int(m.group(2))
        year = year + 2000 if year < 100 else year
    if not 1 <= month <= 12:
        raise _invalid("card.expiry", "card.expiry has an invalid month.")
    now = timezone.now()
    if (year, month) < (now.year, now.month):
        raise _invalid("card.expiry", "The card has expired.")
    return f"{year:04d}-{month:02d}"


def parse_card(raw):
    """Validate card input ourselves so no SDK/pydantic error can ever echo a card number."""
    if not isinstance(raw, dict):
        raise _invalid("card", "card must be an object.")
    number = raw.get("number")
    if not isinstance(number, str):
        raise _invalid("card.number", "card.number must be a string.")
    number = re.sub(r"[\s-]", "", number)
    if not number.isdigit() or not 12 <= len(number) <= 19 or not _luhn_ok(number):
        raise _invalid("card.number", "card.number is not a valid card number.")
    cvc = raw.get("securityCode")
    if not isinstance(cvc, str) or not cvc.isdigit() or len(cvc) not in (3, 4):
        raise _invalid("card.securityCode", "card.securityCode must be 3 or 4 digits.")
    name = _text(raw, "name", "card.name", max_length=300)
    billing = None
    addr = raw.get("billingAddress")
    if addr is not None:
        if not isinstance(addr, dict):
            raise _invalid("card.billingAddress", "card.billingAddress must be an object.")
        country = _text(addr, "countryCode", "card.billingAddress.countryCode", max_length=2).upper()
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise _invalid("card.billingAddress.countryCode", "countryCode must be a 2-letter ISO code.")
        billing = BillingAddress(
            country_code=country,
            address_line_1=_text(addr, "addressLine1", "card.billingAddress.addressLine1", required=False, max_length=300),
            address_line_2=_text(addr, "addressLine2", "card.billingAddress.addressLine2", required=False, max_length=300),
            admin_area_2=_text(addr, "city", "card.billingAddress.city", required=False, max_length=120),
            admin_area_1=_text(addr, "state", "card.billingAddress.state", required=False, max_length=300),
            postal_code=_text(addr, "postalCode", "card.billingAddress.postalCode", required=False, max_length=60),
        )
    return CardInput(number=number, expiry=_expiry(raw.get("expiry")), security_code=cvc, name=name,
                     billing_address=billing)


def parse_pay_request(data):
    has_card, has_saved = "card" in data, "paymentMethodId" in data
    if has_card == has_saved:
        raise _invalid("card", "Send exactly one of card or paymentMethodId.")
    if has_saved:
        pm = data.get("paymentMethodId")
        if not isinstance(pm, str) or not re.fullmatch(r"[0-9a-fA-F-]{32,36}", pm):
            raise _invalid("paymentMethodId", "paymentMethodId is not a saved card id.")
        return None, pm
    return parse_card(data.get("card")), None


def parse_refund_request(data, header_key, currency):
    key = data.get("idempotencyKey") or header_key
    if not isinstance(key, str) or not key.strip() or len(key) > 100:
        raise _invalid("idempotencyKey", "An idempotencyKey (or Idempotency-Key header) of 1-100 characters is "
                                         "required.")
    amount = None
    if data.get("amount") is not None:
        try:
            amount = parse_amount(data["amount"], currency)
        except ValueError as exc:
            raise _invalid("amount", str(exc)) from exc
    note = data.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > 255):
        raise _invalid("note", "note must be a string of at most 255 characters.")
    return key.strip(), amount, note


def parse_iso(value, field):
    if not value:
        raise _invalid(field, f"{field} is required (ISO-8601 date-time).")
    parsed = parse_datetime(value.replace(" ", "+")) if "T" in value else None
    if parsed is None:
        raise _invalid(field, f"{field} must be an ISO-8601 date-time, e.g. 2026-09-01T00:00:00Z.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


# --- responses ----------------------------------------------------------------------------------------

def _money(value):
    return None if value is None else str(value)


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else None


def payment_state(payment):
    if payment.status == PayPalPayment.CAPTURED and payment.refunded_amount > 0:
        return "refunded" if payment.refunded_amount >= payment.captured_amount else "partially_refunded"
    return payment.status


def payment_json(payment):
    return {
        "status": payment_state(payment),
        "amount": _money(payment.amount),
        "currency": payment.currency,
        "attempt": payment.attempt,
        "card": ({"brand": payment.card_brand, "lastDigits": payment.card_last_digits,
                  "paymentMethodId": str(payment.saved_card_id) if payment.saved_card_id else None}
                 if payment.card_last_digits else None),
        "paypalOrderId": payment.paypal_order_id or None,
        "authorization": ({
            "id": payment.authorization_id,
            "status": payment.authorization_status,
            "authorizedAt": _iso(payment.authorized_at),
            "renewedAt": _iso(payment.authorization_renewed_at),
            "expiresAt": _iso(payment.authorization_expires_at),
        } if payment.authorization_id else None),
        "capture": ({
            "id": payment.capture_id,
            "status": payment.capture_status,
            "capturedAmount": _money(payment.captured_amount),
            "paypalFee": _money(payment.paypal_fee),
            "netAmount": _money(payment.net_amount),
            "capturedAt": _iso(payment.captured_at),
        } if payment.capture_id else None),
        "refundedAmount": _money(payment.refunded_amount),
        "refundableAmount": _money(payment.captured_amount - payment.refund_reserved),
        "refunds": [refund_json(r, include_payment=False) for r in payment.refunds.all()],
        "lastError": ({"code": payment.last_error_code, "message": payment.last_error_message}
                      if payment.last_error_code else None),
    }


def order_json(order):
    payment = getattr(order, "paypal_payment", None)
    return {
        "orderId": str(order.number),
        "status": order.status,
        "currency": order.currency,
        "total": _money(order.total_incl_tax),
        "shipping": _money(order.shipping_incl_tax),
        "datePlaced": _iso(order.date_placed),
        "lines": [
            {
                "productId": line.product_id,
                "title": line.title,
                "quantity": line.quantity,
                "unitPrice": _money(line.unit_price_incl_tax),
                "lineTotal": _money(line.line_price_incl_tax),
            }
            for line in order.lines.all()
        ],
        "payment": payment_json(payment) if payment else None,
    }


def refund_json(r, include_payment=True):
    body = {
        "refundId": str(r.pk),
        "status": r.status,
        "amount": _money(r.amount),
        "currency": r.currency,
        "idempotencyKey": r.idempotency_key,
        "paypalRefundId": r.paypal_refund_id or None,
        "paypalStatus": r.paypal_status or None,
        "createdAt": _iso(r.created_at),
    }
    if include_payment:
        body["orderId"] = str(r.payment.order.number)
        body["payment"] = payment_json(r.payment)
    return body


def card_json(card):
    return {
        "paymentMethodId": str(card.pk),
        "brand": card.brand,
        "lastDigits": card.last_digits,
        "expiry": card.expiry,
        "createdAt": _iso(card.created_at),
    }
