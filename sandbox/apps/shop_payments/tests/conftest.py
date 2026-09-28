"""
Fixtures for the payments API tests.

PayPal is faked at the SDK's transport seam: a real ``PaypalClient`` builds and
decodes every request, and ``StubTransport`` answers by route.  The OAuth token
request is answered automatically so each test scripts only the operations.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client
from oscar.core.loading import get_model
from oscar.test import factories
from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpRequest, HttpResponse

from apps.shop_payments import paypal_client
from apps.shop_payments.paypal_client import LoggingTransport

Country = get_model("address", "Country")

Reply = HttpResponse | Exception | Callable[[HttpRequest], HttpResponse]


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(
        status_code=status, headers={"content-type": "application/json"}, content=json.dumps(body).encode()
    )


def token_response() -> HttpResponse:
    return json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})


class StubTransport:
    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], list[Reply]]] = []
        self.requests: list[HttpRequest] = []

    def on(self, method: str, path: str, *replies: Reply) -> None:
        self.routes.append((method, re.compile(path + "$"), list(replies)))

    def send(self, request: HttpRequest) -> HttpResponse:
        path = request.url.split("?")[0].split("paypal.com", 1)[-1]
        if path.endswith("/v1/oauth2/token"):
            return token_response()
        self.requests.append(request)
        for method, pattern, replies in self.routes:
            if method == request.method and pattern.search(path):
                reply = replies.pop(0) if len(replies) > 1 else replies[0]
                if isinstance(reply, Exception):
                    raise reply
                if callable(reply):
                    return reply(request)
                return reply
        raise AssertionError(f"unexpected PayPal call {request.method} {path}")

    def close(self) -> None:
        pass

    def calls(self, method: str, path: str) -> list[HttpRequest]:
        pattern = re.compile(path + "$")
        return [r for r in self.requests if r.method == method and pattern.search(r.url.split("?")[0])]


@pytest.fixture
def paypal_settings(settings: Any) -> Any:
    settings.PAYPAL_CLIENT_ID = "test-client"
    settings.PAYPAL_CLIENT_SECRET = "test-secret"
    settings.PAYPAL_ENVIRONMENT = "sandbox"
    settings.PAYPAL_CURRENCY = "USD"
    settings.PAYPAL_BASE_URL = None
    settings.PAYPAL_REFERENCE_PREFIX = "t"
    return settings


@pytest.fixture
def stub(paypal_settings: Any) -> Any:
    transport = StubTransport()
    client = PaypalClient(
        base_url=paypal_client.SANDBOX_BASE_URL,
        custom_http_client=LoggingTransport(transport),  # type: ignore[arg-type]
        oauth2=ClientCredentials(client_id="test-client", client_secret="test-secret"),
    )
    paypal_client.set_client(client)
    yield transport
    paypal_client.set_client(None)


@pytest.fixture
def product(db: Any) -> Any:
    Country.objects.get_or_create(
        iso_3166_1_a2="GB", defaults={"name": "United Kingdom", "printable_name": "United Kingdom"}
    )
    return factories.create_product(price=Decimal("10.00"), num_in_stock=50)


ADDRESS = {"firstName": "A", "lastName": "Shopper", "line1": "1 Road", "city": "London",
           "postcode": "SW1A 1AA", "countryCode": "GB"}
CARD = {"number": "4111111111111111", "expiry": "2030-12", "securityCode": "123"}


def api_client(user: Any) -> Client:
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def shopper(db: Any) -> Any:
    return factories.UserFactory(username="alice", email="alice@example.com")


@pytest.fixture
def other_shopper(db: Any) -> Any:
    return factories.UserFactory(username="bob", email="bob@example.com")


@pytest.fixture
def staff(db: Any) -> Any:
    return factories.UserFactory(username="op", email="op@example.com", is_staff=True)


def post(client: Client, path: str, body: dict[str, Any] | None = None, **headers: str) -> Any:
    return client.post(path, data=json.dumps(body or {}), content_type="application/json", headers=headers)


def place_order(client: Client, product: Any, quantity: int = 2) -> str:
    response = post(client, "/api/orders", {"items": [{"productId": product.pk, "quantity": quantity}],
                                            "shippingAddress": ADDRESS})
    assert response.status_code == 201, response.content
    return str(response.json()["orderId"])


# --- PayPal wire bodies ---------------------------------------------------------------------


def paypal_order(auth_status: str = "CREATED", value: str = "20.00", auth_id: str = "AUTH1",
                 created: str = "2026-09-28T01:00:00Z", expires: str = "2026-10-27T01:00:00Z") -> dict[str, Any]:
    return {
        "id": "ORDER1",
        "status": "COMPLETED",
        "create_time": created,
        "payment_source": {"card": {"last_digits": "1111", "brand": "VISA"}},
        "purchase_units": [{
            "reference_id": "x",
            "amount": {"currency_code": "USD", "value": value},
            "payments": {"authorizations": [{
                "id": auth_id, "status": auth_status, "amount": {"currency_code": "USD", "value": value},
                "create_time": created, "expiration_time": expires,
            }]},
        }],
    }


def authorization(status: str = "CREATED", auth_id: str = "AUTH1", value: str = "20.00",
                  created: str = "2026-09-28T01:00:00Z", expires: str = "2026-10-27T01:00:00Z") -> dict[str, Any]:
    return {"id": auth_id, "status": status, "amount": {"currency_code": "USD", "value": value},
            "create_time": created, "expiration_time": expires}


def capture(status: str = "COMPLETED", value: str = "20.00", capture_id: str = "CAP1") -> dict[str, Any]:
    return {
        "id": capture_id, "status": status, "amount": {"currency_code": "USD", "value": value},
        "create_time": "2026-09-28T02:00:00Z",
        "seller_receivable_breakdown": {
            "gross_amount": {"currency_code": "USD", "value": value},
            "paypal_fee": {"currency_code": "USD", "value": "0.88"},
            "net_amount": {"currency_code": "USD", "value": str(Decimal(value) - Decimal("0.88"))},
        },
    }


def refund(value: str, refund_id: str = "REF1", status: str = "COMPLETED") -> dict[str, Any]:
    return {"id": refund_id, "status": status, "amount": {"currency_code": "USD", "value": value},
            "create_time": "2026-09-28T03:00:00Z"}


def paypal_error(name: str, issue: str, description: str = "") -> dict[str, Any]:
    return {"name": name, "message": name.replace("_", " ").lower(), "debug_id": "dbg",
            "details": [{"issue": issue, "description": description or issue}]}
