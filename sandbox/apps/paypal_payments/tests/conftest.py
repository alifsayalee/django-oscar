"""
Test harness: a real PayPalServerSdkClient whose transport is a stub.

Faking the transport (not the client) keeps the SDK's own request building,
serialisation, decoding and error mapping in play, so tests can assert on the
exact request PayPal would have received.
"""

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client as DjangoClient
from oscar.core.loading import get_model
from pay_pal_server_sdk import PayPalServerSdkClient
from pay_pal_server_sdk.core import HttpRequest, HttpResponse, OAuthToken

from apps.paypal_payments import client as client_module

Product = get_model("catalogue", "Product")
ProductClass = get_model("catalogue", "ProductClass")
Partner = get_model("partner", "Partner")
StockRecord = get_model("partner", "StockRecord")

BASE = "https://paypal.test"


@dataclass
class StubResponse:
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    chunks: list[bytes] = field(default_factory=list)
    url: str = BASE
    closed: bool = False

    def iter_bytes(self, chunk_size: int | None) -> Iterator[bytes]:
        yield from self.chunks
        self.close()

    def read(self) -> bytes:
        self.close()
        return b"".join(self.chunks)

    def close(self) -> None:
        self.closed = True


def json_response(status: int, body: object) -> StubResponse:
    return StubResponse(status_code=status, headers={"content-type": "application/json"},
                        chunks=[json.dumps(body).encode()])


class StubTransport:
    """Answers each request from the first queued route whose method and path match."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], list[object]]] = []
        self.requests: list[HttpRequest] = []

    def on(self, method: str, path: str, *responses: object) -> None:
        self.routes.append((method, re.compile(path), list(responses)))

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = request.url[len(BASE):].split("?")[0]
        for method, pattern, queue in self.routes:
            if method == request.method and pattern.fullmatch(path) and queue:
                answer = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(answer, Exception):
                    raise answer
                assert isinstance(answer, StubResponse)
                return answer
        raise AssertionError(f"unexpected PayPal call {request.method} {path}")

    def close(self) -> None:
        pass

    def calls(self, method: str, path: str) -> list[HttpRequest]:
        pattern = re.compile(path)
        return [r for r in self.requests
                if r.method == method and pattern.fullmatch(r.url[len(BASE):].split("?")[0])]


class StubTokenSource:
    def fetch(self, credentials: object) -> OAuthToken:
        return OAuthToken(access_token="test-token", token_type="Bearer", expires_in=3600)


@pytest.fixture
def paypal(settings):
    settings.PAYPAL_CURRENCY = "USD"
    settings.PAYPAL_INVOICE_PREFIX = "TEST-"
    transport = StubTransport()
    sdk = PayPalServerSdkClient(
        base_url=BASE,
        custom_http_client=transport,
        retry_options=0,
        oauth2={"client_id": "id", "client_secret": "secret"},
        oauth2_token_source=StubTokenSource(),
    )
    client_module.set_client(sdk)
    yield transport
    client_module.set_client(None)


# ---------------------------------------------------------------------------
# Canned PayPal bodies (wire shape)
# ---------------------------------------------------------------------------


def authorized_order_body(amount, *, auth_id="AUTH-1", order_id="PPORDER-1", status="COMPLETED",
                          auth_status="CREATED", created="2026-09-30T09:00:00Z", expires="2026-10-29T09:00:00Z"):
    return {
        "id": order_id,
        "status": status,
        "intent": "AUTHORIZE",
        "payment_source": {"card": {"brand": "VISA", "last_digits": "1111"}},
        "purchase_units": [{
            "payments": {"authorizations": [{
                "id": auth_id, "status": auth_status,
                "amount": {"currency_code": "USD", "value": amount},
                "create_time": created, "expiration_time": expires,
            }]},
        }],
    }


def authorization_body(auth_id="AUTH-1", status="CREATED", created="2026-09-30T09:00:00Z",
                       expires="2099-10-29T09:00:00Z"):
    return {"id": auth_id, "status": status, "create_time": created, "expiration_time": expires}


def capture_body(amount, *, capture_id="CAP-1", fee="0.81", net=None, status="COMPLETED"):
    net = net or str(Decimal(amount) - Decimal(fee))
    return {
        "id": capture_id, "status": status,
        "amount": {"currency_code": "USD", "value": amount},
        "seller_receivable_breakdown": {
            "gross_amount": {"currency_code": "USD", "value": amount},
            "paypal_fee": {"currency_code": "USD", "value": fee},
            "net_amount": {"currency_code": "USD", "value": net},
        },
        "create_time": "2026-09-30T10:00:00Z",
    }


def refund_body(refund_id="REF-1", amount="1.00", status="COMPLETED"):
    return {"id": refund_id, "status": status, "amount": {"currency_code": "USD", "value": amount}}


def paypal_error(status, issue, name="UNPROCESSABLE_ENTITY"):
    return json_response(status, {
        "name": name, "message": "The requested action could not be performed.", "debug_id": "dbg123",
        "details": [{"issue": issue, "description": f"{issue} happened."}],
    })


# ---------------------------------------------------------------------------
# Django fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def product(db):
    product_class = ProductClass.objects.create(name="Book", requires_shipping=False, track_stock=True)
    product = Product.objects.create(title="Test book", product_class=product_class, structure="standalone")
    partner = Partner.objects.create(name="Partner")
    StockRecord.objects.create(product=product, partner=partner, partner_sku="SKU-1",
                               price=Decimal("12.34"), price_currency="GBP", num_in_stock=10)
    return product


def _user(username, *, staff=False):
    return get_user_model().objects.create_user(
        username=username, email=f"{username}@example.com", password="pass-word-123", is_staff=staff
    )


@pytest.fixture
def shopper(db):
    return _user("shopper")


@pytest.fixture
def other_shopper(db):
    return _user("other")


@pytest.fixture
def operator(db):
    return _user("operator", staff=True)


def api_client(user=None):
    c = DjangoClient()
    if user is not None:
        c.force_login(user)
    return c


@pytest.fixture
def shopper_api(shopper):
    return api_client(shopper)


@pytest.fixture
def operator_api(operator):
    return api_client(operator)


def post(c, url, body=None, **headers):
    return c.post(url, data=json.dumps(body or {}), content_type="application/json", headers=headers)
