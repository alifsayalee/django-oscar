"""Test seam: a real SDK client over a stub transport (no network), plus PayPal-shaped response bodies."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from oscar.test.factories import create_product

from apps.payments import paypal_gateway as gw
from paypal.core import HttpRequest, HttpResponse

PAYPAL_SETTINGS = {
    "PAYPAL_CLIENT_ID": "test-client-id",
    "PAYPAL_CLIENT_SECRET": "test-client-secret",
    "PAYPAL_ENVIRONMENT": "sandbox",
    "PAYPAL_CURRENCY": "USD",
    "PAYPAL_BASE_URL": None,
    "PAYMENTS_REFERENCE_PREFIX": "tst",
    "ALLOWED_HOSTS": ["testserver", "localhost"],
}

CARD = {
    "number": "4111 1111 1111 1111",
    "expiry": "2030-12",
    "securityCode": "123",
    "name": "Test Shopper",
    "billingAddress": {
        "line1": "1 Main St",
        "city": "San Jose",
        "state": "CA",
        "postalCode": "95131",
        "countryCode": "US",
    },
}
FULL_NUMBER = "4111111111111111"


def json_response(status, body):
    return HttpResponse(
        status_code=status, headers={"content-type": "application/json"}, content=json.dumps(body).encode()
    )


def token_response():
    return json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})


def error_response(status, issue, name="UNPROCESSABLE_ENTITY"):
    return json_response(
        status, {"name": name, "message": "refused", "debug_id": "dbg1", "details": [{"issue": issue}]}
    )


class StubTransport:
    """Satisfies the SDK's sync transport protocol. Answers (or raises) in order; records every request."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests: list[HttpRequest] = []

    def queue(self, *responses):
        self._responses.extend(responses)

    def send(self, request):
        self.requests.append(request)
        if not self._responses:
            raise AssertionError(f"unexpected PayPal call: {request.method} {request.url}")
        answer = self._responses.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def close(self):
        pass

    def calls(self):
        """Operation requests, the token fetch excluded: (method, path)."""
        return [
            (r.method, r.url.split("paypal.com", 1)[1].split("?")[0])
            for r in self.requests
            if not r.url.endswith("/v1/oauth2/token")
        ]

    def operation_requests(self):
        return [r for r in self.requests if not r.url.endswith("/v1/oauth2/token")]


def iso(delta):
    return (datetime.now(timezone.utc) + delta).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def authorization_body(
    auth_id="AUTH1", status="CREATED", amount="20.00", currency="USD", create_time=None, expiration_time=None
):
    create_time = create_time or iso(timedelta(minutes=-1))
    expiration_time = expiration_time or iso(timedelta(days=29))
    return {
        "id": auth_id,
        "status": status,
        "amount": {"currency_code": currency, "value": amount},
        "create_time": create_time,
        "expiration_time": expiration_time,
    }


def checkout_order_body(order_status="COMPLETED", auth=None, order_id="ORDER1"):
    body = {
        "id": order_id,
        "status": order_status,
        "create_time": "2026-09-25T10:00:00Z",
        "payment_source": {"card": {"last_digits": "1111", "brand": "VISA"}},
        "purchase_units": [{"reference_id": "x"}],
    }
    if auth is not None:
        body["purchase_units"][0]["payments"] = {"authorizations": [auth]}
    return body


def capture_body(capture_id="CAP1", status="COMPLETED", amount="20.00", fee="0.88", net="19.12"):
    return {
        "id": capture_id,
        "status": status,
        "amount": {"currency_code": "USD", "value": amount},
        "create_time": "2026-09-25T11:00:00Z",
        "seller_receivable_breakdown": {
            "gross_amount": {"currency_code": "USD", "value": amount},
            "paypal_fee": {"currency_code": "USD", "value": fee},
            "net_amount": {"currency_code": "USD", "value": net},
        },
    }


def refund_body(refund_id="REF1", status="COMPLETED", amount="5.00"):
    return {
        "id": refund_id,
        "status": status,
        "amount": {"currency_code": "USD", "value": amount},
        "create_time": "2026-09-25T12:00:00Z",
    }


def payment_token_body(token_id="TOK1", customer_id="CUST1"):
    return {
        "id": token_id,
        "customer": {"id": customer_id},
        "payment_source": {
            "card": {"last_digits": "1111", "brand": "VISA", "expiry": "2030-12", "name": "Test Shopper"}
        },
    }


@override_settings(**PAYPAL_SETTINGS)
class PaymentsTestCase(TestCase):
    """Users, a 10.00 product, and a PayPal client whose transport is a stub."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.shopper = User.objects.create_user("shopper", "shopper@example.com", "pw-shopper-123")
        cls.other = User.objects.create_user("other", "other@example.com", "pw-other-123")
        cls.operator = User.objects.create_user("operator", "operator@example.com", "pw-operator-123", is_staff=True)
        cls.product = create_product(price=Decimal("10.00"), num_in_stock=100)

    def setUp(self):
        self.transport = StubTransport(token_response())
        gw.use_client(
            gw.build_client(
                gw.PayPalConfig.from_values(
                    client_id="test-client-id",
                    client_secret="test-client-secret",
                    environment="sandbox",
                    currency="USD",
                    base_url=None,
                    timeout=5.0,
                ),
                self.transport,
            )
        )
        self.addCleanup(gw.use_client, None)
        self.client.force_login(self.shopper)

    def as_user(self, user):
        self.client.force_login(user)

    def post(self, url, body=None, **headers):
        return self.client.post(url, data=json.dumps(body or {}), content_type="application/json", headers=headers)

    def place_order(self, quantity=2):
        response = self.post("/api/orders", {"items": [{"itemId": self.product.pk, "quantity": quantity}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["orderId"]

    def paid_order(self):
        order_id = self.place_order()
        self.transport.queue(json_response(200, checkout_order_body(auth=authorization_body())))
        response = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 200, response.content)
        return order_id

    def fulfilled_order(self):
        order_id = self.paid_order()
        self.as_user(self.operator)
        self.transport.queue(json_response(200, capture_body()))
        response = self.post(f"/api/orders/{order_id}/fulfil")
        self.assertEqual(response.status_code, 200, response.content)
        self.as_user(self.shopper)
        return order_id
