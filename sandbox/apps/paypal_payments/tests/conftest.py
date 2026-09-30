"""
Test seam: a stub at the SDK's transport protocol, wrapped in the app's real
LoggingTransport, so every test runs the real request building, decoding and
error boundary without touching the network.
"""

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from django.test import Client
from oscar.test.factories import create_product
from pay_pal_server_sdk import PayPalServerSdkClient
from pay_pal_server_sdk.core import HttpRequest, OAuthToken
from pay_pal_server_sdk.models import (
    AuthorizationWithAdditionalData,
    CapturedPayment,
    CardResponse,
    Money,
    Order,
    OrdersCapture,
    PaymentAuthorization,
    PaymentCollection,
    PaymentSourceResponse,
    PurchaseUnit,
    Refund,
    SellerReceivableBreakdown,
)

from apps.paypal_payments import client as client_module
from apps.paypal_payments.transport import LoggingTransport


@dataclass
class StubResponse:
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    chunks: list = field(default_factory=list)
    url: str = "https://paypal.test/"
    closed: bool = False

    def iter_bytes(self, chunk_size) -> Iterator[bytes]:
        yield from self.chunks
        self.close()

    def read(self) -> bytes:
        self.close()
        return b"".join(self.chunks)

    def close(self) -> None:
        self.closed = True


def json_response(status, body):
    return StubResponse(status_code=status, headers={"content-type": "application/json"},
                        chunks=[json.dumps(body).encode()])


class StubTransport:
    """Answers queued responses in order; a queued exception is raised instead (a transport failure)."""

    def __init__(self):
        self.queue = []
        self.requests: list[HttpRequest] = []

    def add(self, *items):
        self.queue.extend(items)

    def send(self, request):
        self.requests.append(request)
        if not self.queue:
            raise AssertionError(f"unexpected PayPal call: {request.method} {request.url}")
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        pass

    def paths(self):
        return [f"{r.method} {str(r.url).split('paypal.test', 1)[-1].split('?')[0]}" for r in self.requests]


class StubTokenSource:
    def fetch(self, credentials):
        return OAuthToken(access_token="t", token_type="Bearer")


@pytest.fixture
def paypal(settings):
    settings.PAYPAL_CURRENCY = "USD"
    transport = StubTransport()
    sdk = PayPalServerSdkClient(
        base_url="https://paypal.test",
        custom_http_client=LoggingTransport(transport),
        retry_options=0,
        oauth2={"client_id": "id", "client_secret": "secret"},
        oauth2_token_source=StubTokenSource(),
    )
    previous = client_module.set_client(sdk)
    yield transport
    client_module.set_client(previous)
    assert not transport.queue, f"queued PayPal responses never requested: {transport.queue}"


# ---- users and catalogue -----------------------------------------------------


@pytest.fixture
def shopper(django_user_model):
    return django_user_model.objects.create_user("shopper", "shopper@example.com", "pw")


@pytest.fixture
def other(django_user_model):
    return django_user_model.objects.create_user("other", "other@example.com", "pw")


@pytest.fixture
def staff(django_user_model):
    return django_user_model.objects.create_user("operator", "operator@example.com", "pw", is_staff=True)


def api_client(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture
def shopper_api(shopper):
    return api_client(shopper)


@pytest.fixture
def other_api(other):
    return api_client(other)


@pytest.fixture
def staff_api(staff):
    return api_client(staff)


@pytest.fixture
def product(db):
    return create_product(title="Widget", price=D("10.00"), num_in_stock=100)


@pytest.fixture
def order_id(shopper_api, product):
    """A 2 x 10.00 order placed through the API (total 20.00)."""
    r = shopper_api.post("/api/orders", {"items": [{"itemId": product.pk, "quantity": 2}]},
                         content_type="application/json")
    assert r.status_code == 201, r.content
    return r.json()["orderId"]


# ---- PayPal response bodies, built from the SDK's own models -----------------

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def usd(value):
    return Money(currency_code="USD", value=value)


def auth(auth_id="AUTH-1", status="CREATED", value="20.00", created=None):
    created = created or NOW
    return AuthorizationWithAdditionalData(
        id=auth_id, status=status, amount=usd(value), create_time=iso(created),
        expiration_time=iso(created + timedelta(days=29)),
    )


def orders_capture(capture_id="CAP-1", status="COMPLETED", value="20.00"):
    return OrdersCapture(id=capture_id, status=status, amount=usd(value), create_time=iso(NOW),
                         seller_receivable_breakdown=SellerReceivableBreakdown(
                             gross_amount=usd(value), paypal_fee=usd("0.88"), net_amount=usd("19.12")))


def order_body(order_id="PO-1", status="COMPLETED", auths=(), captures=(), refunds=()):
    return Order(
        id=order_id,
        status=status,
        payment_source=PaymentSourceResponse(card=CardResponse(brand="VISA", last_digits="1111")),
        purchase_units=[PurchaseUnit(payments=PaymentCollection(
            authorizations=list(auths), captures=list(captures), refunds=list(refunds)))],
    ).to_dict()


def capture_body(capture_id="CAP-1", status="COMPLETED", value="20.00"):
    return CapturedPayment(
        id=capture_id, status=status, amount=usd(value), create_time=iso(NOW),
        seller_receivable_breakdown=SellerReceivableBreakdown(
            gross_amount=usd(value), paypal_fee=usd("0.88"), net_amount=usd("19.12")),
    ).to_dict()


def payment_auth_body(auth_id="AUTH-2", status="CREATED", value="20.00", created=None):
    created = created or NOW
    return PaymentAuthorization(id=auth_id, status=status, amount=usd(value), create_time=iso(created),
                                expiration_time=iso(created + timedelta(days=29))).to_dict()


def refund_body(refund_id="REF-1", status="COMPLETED", value="5.00", custom_id=None):
    kwargs = {"custom_id": custom_id} if custom_id else {}
    return Refund(id=refund_id, status=status, amount=usd(value), create_time=iso(NOW), **kwargs).to_dict()


def paypal_error(status, name="UNPROCESSABLE_ENTITY", issue="INSTRUMENT_DECLINED"):
    return json_response(status, {"name": name, "message": "m", "debug_id": "d",
                                  "details": [{"issue": issue, "description": "x"}]})


CARD = {"number": "4111111111111111", "expiry": "2030-12", "securityCode": "123", "name": "Test Shopper"}


def post(c, url, body=None, **headers):
    return c.post(url, body or {}, content_type="application/json", **headers)


def pay_with_card(shopper_api, paypal, order_id, auth_created=None):
    paypal.add(json_response(201, order_body(auths=[auth(created=auth_created)])))
    r = post(shopper_api, f"/api/orders/{order_id}/pay", {"card": CARD})
    assert r.status_code == 200, r.content
    return r


def pay_and_fulfil(shopper_api, staff_api, paypal, order_id):
    pay_with_card(shopper_api, paypal, order_id)
    paypal.add(json_response(201, capture_body()))
    r = post(staff_api, f"/api/orders/{order_id}/fulfil")
    assert r.status_code == 200, r.content
    return r
