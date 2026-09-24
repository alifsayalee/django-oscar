import json
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from typing import Any

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from oscar.test.factories import create_product
from paypal.core import HttpRequest, HttpResponse

from apps.payments.paypal_client import build_client, set_client

TEST_PAYPAL = {
    "PAYPAL_CLIENT_ID": "test-client-id",
    "PAYPAL_CLIENT_SECRET": "test-client-secret",
    "PAYPAL_ENVIRONMENT": "sandbox",
    "PAYPAL_CURRENCY": "USD",
    "PAYPAL_BASE_URL": "",
    "PAYPAL_REFERENCE_PREFIX": "test",
}

CARD = {"number": "4111 1111 1111 1111", "expiry": "2030-12", "securityCode": "123", "name": "Test Shopper"}


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def token_response() -> HttpResponse:
    return json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})


def paypal_error(status: int, name: str = "UNPROCESSABLE_ENTITY", issue: str = "INSTRUMENT_DECLINED") -> HttpResponse:
    return json_response(
        status,
        {"name": name, "message": "The requested action could not be performed.", "debug_id": "d1",
         "details": [{"issue": issue, "description": "Declined."}]},
    )


class StubTransport:
    """The SDK's sync transport protocol; answers queued responses in order.

    A queued ``Exception`` is raised instead of answered."""

    def __init__(self) -> None:
        self.queue: list[HttpResponse | Exception] = [token_response()]
        self.requests: list[HttpRequest] = []

    def add(self, *items: HttpResponse | Exception) -> "StubTransport":
        self.queue.extend(items)
        return self

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self.queue:
            raise AssertionError(f"unexpected PayPal call: {request.method} {request.url}")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        pass

    @property
    def api_requests(self) -> list[HttpRequest]:
        return [r for r in self.requests if not r.url.endswith("/v1/oauth2/token")]


def iso(dt: datetime) -> str:
    return dt.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now() -> datetime:
    return datetime.now(dt_timezone.utc)


def authorization(auth_id: str = "AUTH1", status: str = "CREATED", value: str = "20.00",
                  created: datetime | None = None) -> dict[str, Any]:
    created = created or now()
    return {
        "id": auth_id, "status": status, "amount": {"currency_code": "USD", "value": value},
        "create_time": iso(created), "update_time": iso(created),
        "expiration_time": iso(created + timedelta(days=29)),
    }


def order_body(status: str = "COMPLETED", auth: dict[str, Any] | None = None, order_id: str = "PPORDER1") -> dict[str, Any]:
    unit: dict[str, Any] = {"reference_id": "default", "amount": {"currency_code": "USD", "value": "20.00"}}
    if auth is not None:
        unit["payments"] = {"authorizations": [auth]}
    return {
        "id": order_id, "status": status, "intent": "AUTHORIZE", "create_time": iso(now()),
        "payment_source": {"card": {"last_digits": "1111", "brand": "VISA"}},
        "purchase_units": [unit],
    }


def capture_body(capture_id: str = "CAP1", status: str = "COMPLETED", value: str = "20.00") -> dict[str, Any]:
    return {
        "id": capture_id, "status": status, "amount": {"currency_code": "USD", "value": value},
        "final_capture": True, "create_time": iso(now()),
        "seller_receivable_breakdown": {
            "gross_amount": {"currency_code": "USD", "value": value},
            "paypal_fee": {"currency_code": "USD", "value": "1.07"},
            "net_amount": {"currency_code": "USD", "value": str(Decimal(value) - Decimal("1.07"))},
        },
    }


def refund_body(refund_id: str, value: str, status: str = "COMPLETED") -> dict[str, Any]:
    return {"id": refund_id, "status": status, "amount": {"currency_code": "USD", "value": value},
            "create_time": iso(now())}


@override_settings(**TEST_PAYPAL)
class ApiTestCase(TestCase):
    def setUp(self) -> None:
        User = get_user_model()
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "pw-shopper-123")
        self.other = User.objects.create_user("other", "other@example.com", "pw-other-123")
        self.operator = User.objects.create_user("operator", "op@example.com", "pw-op-123", is_staff=True)
        self.product = create_product(price=Decimal("10.00"), num_in_stock=50)
        self.transport = StubTransport()
        self._previous = set_client(build_client(self.transport))
        self.client.force_login(self.shopper)

    def tearDown(self) -> None:
        set_client(self._previous)

    def post(self, url: str, body: object = None, **extra: Any) -> Any:
        return self.client.post(url, data=json.dumps(body or {}), content_type="application/json", **extra)

    def as_user(self, user: Any) -> None:
        self.client.logout()
        self.client.force_login(user)

    def place_order(self, quantity: int = 2) -> str:
        response = self.post("/api/orders", {"items": [{"productId": self.product.pk, "quantity": quantity}]})
        self.assertEqual(response.status_code, 201, response.content)
        return str(response.json()["orderId"])

    def paid_order(self, auth: dict[str, Any] | None = None) -> str:
        number = self.place_order()
        self.transport.add(json_response(201, order_body(auth=auth or authorization())))
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 200, response.content)
        return number

    def captured_order(self) -> str:
        number = self.paid_order()
        self.as_user(self.operator)
        self.transport.add(json_response(200, authorization()), json_response(201, capture_body()))
        response = self.post(f"/api/orders/{number}/fulfil")
        self.assertEqual(response.status_code, 200, response.content)
        self.as_user(self.shopper)
        return number
