"""
Tests for the PayPal payments API.

PayPal is faked at the SDK's transport seam (``custom_http_client``), so the
real SDK builds and decodes every request; nothing reaches the network.
Run: ``cd sandbox && python manage.py test apps.paypal_payments``
"""

import json
import re
from datetime import timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import httpx
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from oscar.test.factories import create_product
from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpRequest, HttpResponse, JsonBody

from . import gateway
from .models import OrderPayment, PaymentOperation, SavedCard

TEST_CARD = {"number": "4111 1111 1111 1111", "expiry": "2030-12", "securityCode": "123", "name": "Test Shopper"}


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(
        status_code=status, headers={"content-type": "application/json"}, content=json.dumps(body).encode()
    )


def paypal_error(status: int, issue: str, description: str) -> HttpResponse:
    return json_response(
        status,
        {
            "name": "UNPROCESSABLE_ENTITY",
            "message": "The requested action could not be performed.",
            "debug_id": "dbg123",
            "details": [{"issue": issue, "description": description}],
        },
    )


def iso(delta: timedelta = timedelta()) -> str:
    return (timezone.now() + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def order_body(
    amount: str, auth_status: str = "CREATED", auth_id: str = "AUTH1", status: str = "COMPLETED"
) -> dict[str, Any]:
    return {
        "id": "PPORDER1",
        "status": status,
        "intent": "AUTHORIZE",
        "payment_source": {"card": {"brand": "VISA", "last_digits": "1111"}},
        "purchase_units": [
            {
                "reference_id": "ref",
                "payments": {
                    "authorizations": [
                        {
                            "id": auth_id,
                            "status": auth_status,
                            "amount": {"currency_code": "USD", "value": amount},
                            "create_time": iso(),
                            "expiration_time": iso(timedelta(days=29)),
                        }
                    ]
                },
            }
        ],
    }


def capture_body(
    amount: str, fee: str = "0.79", capture_id: str = "CAP1", status: str = "COMPLETED"
) -> dict[str, Any]:
    net = str(Decimal(amount) - Decimal(fee))
    return {
        "id": capture_id,
        "status": status,
        "amount": {"currency_code": "USD", "value": amount},
        "seller_receivable_breakdown": {
            "gross_amount": {"currency_code": "USD", "value": amount},
            "paypal_fee": {"currency_code": "USD", "value": fee},
            "net_amount": {"currency_code": "USD", "value": net},
        },
        "create_time": iso(),
    }


def refund_body(amount: str, refund_id: str = "REF1", status: str = "COMPLETED") -> dict[str, Any]:
    amount_money = {"currency_code": "USD", "value": amount}
    return {"id": refund_id, "status": status, "amount": amount_money, "create_time": iso()}


class StubPayPal:
    """A fake PayPal behind the SDK's transport protocol (send + close)."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], list[HttpResponse | Exception]]] = []
        self.requests: list[HttpRequest] = []

    def on(self, method: str, pattern: str, *responses: HttpResponse | Exception) -> None:
        self.routes.insert(0, (method, re.compile(pattern), list(responses)))

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = httpx.URL(request.url).path
        if path == "/v1/oauth2/token":
            return json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})
        for method, pattern, responses in self.routes:
            if method == request.method and pattern.search(path):
                response = responses.pop(0) if len(responses) > 1 else responses[0]
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"unexpected PayPal call {request.method} {path}")

    def close(self) -> None:
        pass

    def calls(self, method: str, pattern: str) -> list[HttpRequest]:
        return [
            r for r in self.requests if r.method == method and re.search(pattern, httpx.URL(r.url).path)
        ]


def body_of(request: HttpRequest) -> Any:
    assert isinstance(request.body, JsonBody)
    return request.body.value


@override_settings(
    PAYPAL_CLIENT_ID="test-client",
    PAYPAL_CLIENT_SECRET="test-secret",
    PAYPAL_ENVIRONMENT="sandbox",
    PAYPAL_CURRENCY="USD",
    PAYPAL_BASE_URL="",
)
class PayPalApiTestCase(TestCase):
    def setUp(self) -> None:
        self.paypal = StubPayPal()
        client = PaypalClient(
            base_url="https://paypal.test",
            oauth2=ClientCredentials(client_id="test-client", client_secret="test-secret"),
            custom_http_client=gateway.RecordingTransport(self.paypal),
        )
        self.previous_client = gateway.set_client(client)
        self.product = create_product(price=Decimal("12.34"), num_in_stock=100)
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "password-123456")
        self.other = User.objects.create_user("other", "other@example.com", "password-123456")
        self.staff = User.objects.create_user("operator", "op@example.com", "password-123456", is_staff=True)
        self.api = Client()
        self.api.force_login(self.shopper)
        self.operator = Client()
        self.operator.force_login(self.staff)

    def tearDown(self) -> None:
        gateway.set_client(self.previous_client)

    # -- helpers -----------------------------------------------------------

    def post(self, client: Client, url: str, data: object = None, **headers: str) -> Any:
        return client.post(url, data=json.dumps(data or {}), content_type="application/json", headers=headers)

    def place_order(self, quantity: int = 1) -> str:
        items = [{"productId": self.product.pk, "quantity": quantity}]
        response = self.post(self.api, "/api/orders", {"items": items})
        self.assertEqual(response.status_code, 201, response.content)
        return str(response.json()["orderId"])

    def paid_order(self, quantity: int = 1) -> str:
        order_id = self.place_order(quantity)
        total = str(Decimal("12.34") * quantity)
        self.paypal.on("POST", r"^/v2/checkout/orders$", json_response(201, order_body(total)))
        response = self.post(self.api, f"/api/orders/{order_id}/pay", {"card": TEST_CARD})
        self.assertEqual(response.status_code, 201, response.content)
        return order_id

    def fulfilled_order(self, quantity: int = 1) -> str:
        order_id = self.paid_order(quantity)
        total = str(Decimal("12.34") * quantity)
        self.paypal.on("GET", r"/v2/payments/authorizations/AUTH1$", json_response(200, {
            "id": "AUTH1", "status": "CREATED", "create_time": iso(), "expiration_time": iso(timedelta(days=29))}))
        self.paypal.on("POST", r"/authorizations/AUTH1/capture$", json_response(201, capture_body(total)))
        response = self.post(self.operator, f"/api/orders/{order_id}/fulfil")
        self.assertEqual(response.status_code, 200, response.content)
        return order_id

    def payment(self, order_id: str) -> OrderPayment:
        return OrderPayment.objects.get(order__number=order_id)


class OrderTests(PayPalApiTestCase):
    def test_place_order_uses_catalogue_price_and_configured_currency(self) -> None:
        response = self.post(self.api, "/api/orders", {"items": [{"productId": self.product.pk, "quantity": 2}]})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIn("orderId", body)
        self.assertEqual(body["payment"]["state"], "awaiting_payment")
        self.assertEqual(body["payment"]["amount"], "24.68")
        self.assertEqual(body["payment"]["currency"], "USD")
        self.assertEqual(body["lines"][0]["quantity"], 2)
        self.assertEqual(self.paypal.requests, [])

    def test_unknown_product_is_rejected(self) -> None:
        response = self.post(self.api, "/api/orders", {"items": [{"productId": 999999, "quantity": 1}]})
        self.assertEqual(response.status_code, 422)

    def test_anonymous_callers_are_refused(self) -> None:
        response = self.post(Client(), "/api/orders", {"items": [{"productId": self.product.pk}]})
        self.assertEqual(response.status_code, 401)

    def test_csrf_is_enforced_for_session_callers(self) -> None:
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.shopper)
        response = self.post(client, "/api/orders", {"items": [{"productId": self.product.pk}]})
        self.assertEqual(response.status_code, 403)

    def test_my_orders_lists_only_the_callers_orders(self) -> None:
        order_id = self.place_order()
        other = Client()
        other.force_login(self.other)
        self.assertEqual(other.get("/api/my-orders").json()["orders"], [])
        orders = self.api.get("/api/my-orders").json()["orders"]
        self.assertEqual([o["orderId"] for o in orders], [order_id])


class AuthorizeTests(PayPalApiTestCase):
    def test_card_payment_authorizes_the_order_total(self) -> None:
        order_id = self.place_order(2)
        self.paypal.on("POST", r"^/v2/checkout/orders$", json_response(201, order_body("24.68")))
        response = self.post(self.api, f"/api/orders/{order_id}/pay", {"card": TEST_CARD})
        self.assertEqual(response.status_code, 201, response.content)
        payment = response.json()["payment"]
        self.assertEqual(payment["state"], "authorized")
        self.assertEqual(payment["authorization"]["id"], "AUTH1")
        self.assertEqual(payment["paymentMethod"]["lastDigits"], "1111")

        (request,) = self.paypal.calls("POST", r"^/v2/checkout/orders$")
        sent = body_of(request)
        self.assertEqual(sent["intent"], "AUTHORIZE")
        self.assertEqual(sent["purchase_units"][0]["amount"], {"currency_code": "USD", "value": "24.68"})
        self.assertEqual(sent["purchase_units"][0]["custom_id"], order_id)
        self.assertEqual(sent["payment_source"]["card"]["number"], "4111111111111111")
        self.assertTrue(request.headers["paypal-request-id"])
        self.assertEqual(request.headers["prefer"], "return=representation")
        # The card number is never stored.
        stored = json.dumps(list(OrderPayment.objects.values()), default=str) + json.dumps(
            list(PaymentOperation.objects.values()), default=str)
        self.assertNotIn("4111111111111111", stored)

    def test_double_click_never_authorizes_twice(self) -> None:
        order_id = self.paid_order()
        response = self.post(self.api, f"/api/orders/{order_id}/pay", {"card": TEST_CARD})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.paypal.calls("POST", r"^/v2/checkout/orders$")), 1)

    def test_declined_card_can_be_retried(self) -> None:
        order_id = self.place_order()
        self.paypal.on("POST", r"^/v2/checkout/orders$", paypal_error(422, "TEST_DECLINE", "Card refused."))
        response = self.post(self.api, f"/api/orders/{order_id}/pay", {"card": TEST_CARD})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["paypalIssue"], "TEST_DECLINE")
        self.assertEqual(self.payment(order_id).state, "failed")
        self.paypal.on("POST", r"^/v2/checkout/orders$", json_response(201, order_body("12.34")))
        response = self.post(self.api, f"/api/orders/{order_id}/pay", {"card": TEST_CARD})
        self.assertEqual(response.status_code, 201)
        first, second = self.paypal.calls("POST", r"^/v2/checkout/orders$")
        self.assertNotEqual(first.headers["paypal-request-id"], second.headers["paypal-request-id"])
        self.assertNotEqual(
            body_of(first)["purchase_units"][0]["invoice_id"], body_of(second)["purchase_units"][0]["invoice_id"]
        )

    def test_denied_authorization_is_not_a_success(self) -> None:
        order_id = self.place_order()
        self.paypal.on("POST", r"^/v2/checkout/orders$", json_response(201, order_body("12.34", auth_status="DENIED")))
        response = self.post(self.api, f"/api/orders/{order_id}/pay", {"card": TEST_CARD})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(self.payment(order_id).state, "failed")

    def test_unlisted_status_is_unknown_not_authorized(self) -> None:
        order_id = self.place_order()
        self.paypal.on("POST", r"^/v2/checkout/orders$",
                       json_response(201, order_body("12.34", auth_status="SOMETHING_NEW")))
        response = self.post(self.api, f"/api/orders/{order_id}/pay", {"card": TEST_CARD})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.payment(order_id).state, "unknown")

    def test_amount_mismatch_needs_review(self) -> None:
        order_id = self.place_order()
        self.paypal.on("POST", r"^/v2/checkout/orders$", json_response(201, order_body("12.00")))
        response = self.post(self.api, f"/api/orders/{order_id}/pay", {"card": TEST_CARD})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.payment(order_id).state, "needs_review")

    def test_refused_connection_is_known_and_retryable(self) -> None:
        order_id = self.place_order()
        self.paypal.on("POST", r"^/v2/checkout/orders$", httpx.ConnectError("refused"))
        response = self.post(self.api, f"/api/orders/{order_id}/pay", {"card": TEST_CARD})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("outcomeUnknown", response.json())
        self.assertEqual(self.payment(order_id).state, "failed")
        self.assertEqual(len(self.paypal.calls("POST", r"^/v2/checkout/orders$")), 1)

    def test_timeout_is_looked_up_under_the_same_request_id(self) -> None:
        order_id = self.place_order()
        self.paypal.on(
            "POST", r"^/v2/checkout/orders$", httpx.ReadTimeout("no reply"), json_response(200, order_body("12.34"))
        )
        response = self.post(self.api, f"/api/orders/{order_id}/pay", {"card": TEST_CARD})
        self.assertEqual(response.status_code, 201, response.content)
        first, second = self.paypal.calls("POST", r"^/v2/checkout/orders$")
        self.assertEqual(first.headers["paypal-request-id"], second.headers["paypal-request-id"])

    def test_unresolved_timeout_is_unknown_and_resumes_under_the_same_request_id(self) -> None:
        order_id = self.place_order()
        self.paypal.on("POST", r"^/v2/checkout/orders$", httpx.ReadTimeout("no reply"))
        response = self.post(self.api, f"/api/orders/{order_id}/pay", {"card": TEST_CARD})
        self.assertEqual(response.status_code, 504)
        self.assertTrue(response.json()["reference"])
        self.assertEqual(self.payment(order_id).state, "unknown")
        self.paypal.on("POST", r"^/v2/checkout/orders$", json_response(200, order_body("12.34")))
        response = self.post(self.api, f"/api/orders/{order_id}/pay", {"card": TEST_CARD})
        self.assertEqual(response.status_code, 201)
        sent = self.paypal.calls("POST", r"^/v2/checkout/orders$")
        self.assertEqual(len({r.headers["paypal-request-id"] for r in sent}), 1)
        # A resend must be the identical request, invoice id included.
        self.assertEqual(len({body_of(r)["purchase_units"][0]["invoice_id"] for r in sent}), 1)

    def test_payer_action_required_is_reported_not_round_tripped(self) -> None:
        order_id = self.place_order()
        body = {"id": "PPORDER1", "status": "PAYER_ACTION_REQUIRED"}
        self.paypal.on("POST", r"^/v2/checkout/orders$", json_response(200, body))
        response = self.post(self.api, f"/api/orders/{order_id}/pay", {"card": TEST_CARD})
        self.assertEqual(response.status_code, 402)
        self.assertIn("browser", response.json()["message"])

    def test_another_shoppers_order_is_invisible(self) -> None:
        order_id = self.place_order()
        other = Client()
        other.force_login(self.other)
        response = self.post(other, f"/api/orders/{order_id}/pay", {"card": TEST_CARD})
        self.assertEqual(response.status_code, 404)

    def test_bad_merchant_credentials_are_a_server_problem(self) -> None:
        order_id = self.place_order()

        class BadToken(StubPayPal):
            def send(self, request: HttpRequest) -> HttpResponse:
                self.requests.append(request)
                return json_response(401, {"error": "invalid_client", "error_description": "bad"})

        gateway.set_client(PaypalClient(
            base_url="https://paypal.test",
            oauth2=ClientCredentials(client_id="x", client_secret="y"),
            custom_http_client=gateway.RecordingTransport(BadToken()),
        ))
        response = self.post(self.api, f"/api/orders/{order_id}/pay", {"card": TEST_CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.payment(order_id).state, "failed")


class FulfilCancelTests(PayPalApiTestCase):
    def test_fulfil_captures_and_reports_fee_and_net(self) -> None:
        order_id = self.fulfilled_order()
        capture = self.api.get("/api/my-orders").json()["orders"][0]["payment"]["capture"]
        self.assertEqual(capture, {**capture, "amount": "12.34", "paypalFee": "0.79", "netAmount": "11.55"})
        (request,) = self.paypal.calls("POST", r"/capture$")
        self.assertEqual(body_of(request)["amount"], {"currency_code": "USD", "value": "12.34"})
        self.assertEqual(self.payment(order_id).order.status, "Complete")

    def test_fulfil_is_staff_only(self) -> None:
        order_id = self.paid_order()
        response = self.post(self.api, f"/api/orders/{order_id}/fulfil")
        self.assertEqual(response.status_code, 403)

    def test_fulfil_twice_captures_once(self) -> None:
        order_id = self.fulfilled_order()
        response = self.post(self.operator, f"/api/orders/{order_id}/fulfil")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.paypal.calls("POST", r"/capture$")), 1)

    def test_stale_authorization_is_renewed_before_capture(self) -> None:
        order_id = self.paid_order()
        OrderPayment.objects.filter(order__number=order_id).update(
            authorization_time=timezone.now() - timedelta(days=5))
        self.paypal.on("GET", r"/authorizations/AUTH1$", json_response(200, {
            "id": "AUTH1", "status": "CREATED", "create_time": iso(-timedelta(days=5)),
            "expiration_time": iso(timedelta(days=24))}))
        self.paypal.on("POST", r"/authorizations/AUTH1/reauthorize$", json_response(201, {
            "id": "AUTH2", "status": "CREATED", "amount": {"currency_code": "USD", "value": "12.34"},
            "create_time": iso(), "expiration_time": iso(timedelta(days=24))}))
        self.paypal.on("POST", r"/authorizations/AUTH2/capture$", json_response(201, capture_body("12.34")))
        response = self.post(self.operator, f"/api/orders/{order_id}/fulfil")
        self.assertEqual(response.status_code, 200, response.content)
        payment = self.payment(order_id)
        self.assertEqual((payment.state, payment.authorization_id, payment.reauthorized), ("captured", "AUTH2", True))

    def test_unrenewable_authorization_explains_what_to_do(self) -> None:
        order_id = self.paid_order()
        self.paypal.on("GET", r"/authorizations/AUTH1$", json_response(200, {
            "id": "AUTH1", "status": "CREATED", "create_time": iso(-timedelta(days=10)),
            "expiration_time": iso(timedelta(days=19))}))
        self.paypal.on("POST", r"/reauthorize$", paypal_error(422, "TEST_NO_REAUTH", "Renewal refused."))
        self.paypal.on("POST", r"/capture$", paypal_error(422, "TEST_NO_CAPTURE", "Capture refused."))
        response = self.post(self.operator, f"/api/orders/{order_id}/fulfil")
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["error"], "authorization_not_renewable")
        self.assertIn("TEST_NO_REAUTH", body["message"])
        self.assertIn("Cancel the order", body["message"])
        self.assertEqual(self.payment(order_id).state, "authorized")

    def test_expired_authorization_asks_for_a_new_payment(self) -> None:
        order_id = self.paid_order()
        self.paypal.on("GET", r"/authorizations/AUTH1$", json_response(200, {
            "id": "AUTH1", "status": "CREATED", "create_time": iso(-timedelta(days=30)),
            "expiration_time": iso(-timedelta(days=1))}))
        response = self.post(self.operator, f"/api/orders/{order_id}/fulfil")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "authorization_expired")
        self.assertEqual(self.payment(order_id).state, "expired")
        self.assertEqual(self.paypal.calls("POST", r"/capture$"), [])

    def test_cancel_voids_the_hold(self) -> None:
        order_id = self.paid_order()
        self.paypal.on("POST", r"/authorizations/AUTH1/void$", json_response(200, {"id": "AUTH1", "status": "VOIDED"}))
        response = self.post(self.operator, f"/api/orders/{order_id}/cancel")
        self.assertEqual(response.status_code, 200, response.content)
        payment = self.payment(order_id)
        self.assertEqual((payment.state, payment.order.status), ("voided", "Cancelled"))
        response = self.post(self.operator, f"/api/orders/{order_id}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.paypal.calls("POST", r"/void$")), 1)

    def test_cancel_after_fulfilment_points_to_refunds(self) -> None:
        order_id = self.fulfilled_order()
        response = self.post(self.operator, f"/api/orders/{order_id}/cancel")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "already_fulfilled")


class RefundTests(PayPalApiTestCase):
    def refund(self, order_id: str, key: str, amount: str | None = None) -> Any:
        data = {"amount": amount} if amount is not None else {}
        return self.post(self.api, f"/api/orders/{order_id}/refunds", data, **{"Idempotency-Key": key})

    def test_partial_then_remaining_refund(self) -> None:
        order_id = self.fulfilled_order(2)  # 24.68
        self.paypal.on("POST", r"/captures/CAP1/refund$", json_response(201, refund_body("10.00", "REF1")),
                       json_response(201, refund_body("14.68", "REF2")))
        first = self.refund(order_id, "k1", "10.00")
        self.assertEqual(first.status_code, 201, first.content)
        self.assertIn("refundId", first.json())
        self.assertEqual(self.payment(order_id).state, "partially_refunded")
        second = self.refund(order_id, "k2")
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(second.json()["amount"], "14.68")
        payment = self.payment(order_id)
        self.assertEqual((payment.state, payment.refunded_amount), ("refunded", Decimal("24.68")))
        sent = [body_of(r)["amount"]["value"] for r in self.paypal.calls("POST", r"/refund$")]
        self.assertEqual(sent, ["10.00", "14.68"])

    def test_same_key_never_refunds_twice(self) -> None:
        order_id = self.fulfilled_order()
        self.paypal.on("POST", r"/refund$", json_response(201, refund_body("5.00")))
        first = self.refund(order_id, "same", "5.00")
        again = self.refund(order_id, "same", "5.00")
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(first.json()["refundId"], again.json()["refundId"])
        self.assertEqual(len(self.paypal.calls("POST", r"/refund$")), 1)

    def test_same_key_different_amount_is_rejected(self) -> None:
        order_id = self.fulfilled_order()
        self.paypal.on("POST", r"/refund$", json_response(201, refund_body("5.00")))
        self.refund(order_id, "same", "5.00")
        response = self.refund(order_id, "same", "6.00")
        self.assertEqual(response.status_code, 422)

    def test_cannot_refund_more_than_captured(self) -> None:
        order_id = self.fulfilled_order()
        self.paypal.on("POST", r"/refund$", json_response(201, refund_body("10.00")))
        self.refund(order_id, "a", "10.00")
        response = self.refund(order_id, "b", "2.35")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["refundable"], "2.34")
        self.assertEqual(len(self.paypal.calls("POST", r"/refund$")), 1)

    def test_rejected_refund_releases_the_reservation(self) -> None:
        order_id = self.fulfilled_order()
        self.paypal.on("POST", r"/refund$", paypal_error(422, "TEST_REFUND_REFUSED", "No."))
        response = self.refund(order_id, "a", "12.34")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.payment(order_id).refund_reserved, Decimal("0.00"))

    def test_refund_requires_fulfilment_and_a_key(self) -> None:
        order_id = self.paid_order()
        self.assertEqual(self.refund(order_id, "a", "1.00").status_code, 409)
        response = self.post(self.api, f"/api/orders/{order_id}/refunds", {"amount": "1.00"})
        self.assertEqual(response.status_code, 400)

    def test_another_shoppers_order_cannot_be_refunded(self) -> None:
        order_id = self.fulfilled_order()
        other = Client()
        other.force_login(self.other)
        response = self.post(other, f"/api/orders/{order_id}/refunds", {"amount": "1.00"}, **{"Idempotency-Key": "x"})
        self.assertEqual(response.status_code, 404)


class SavedCardTests(PayPalApiTestCase):
    def save(self, idempotency_key: str | None = None) -> Any:
        self.paypal.on("POST", r"^/v3/vault/payment-tokens$", json_response(201, {
            "id": "TOKEN1", "customer": {"id": "CUST1"},
            "payment_source": {"card": {"brand": "VISA", "last_digits": "1111", "expiry": "2030-12",
                                        "name": "Test Shopper"}}}))
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return self.post(self.api, "/api/payment-methods", {"card": TEST_CARD}, **headers)

    def test_save_list_pay_delete(self) -> None:
        response = self.save()
        self.assertEqual(response.status_code, 201, response.content)
        card = response.json()
        self.assertEqual((card["brand"], card["lastDigits"]), ("VISA", "1111"))
        self.assertNotIn("4111111111111111", response.content.decode())
        method_id = card["paymentMethodId"]
        self.assertEqual(
            [c["paymentMethodId"] for c in self.api.get("/api/payment-methods").json()["paymentMethods"]],
            [method_id])

        order_id = self.place_order()
        self.paypal.on("POST", r"^/v2/checkout/orders$", json_response(201, order_body("12.34")))
        response = self.post(self.api, f"/api/orders/{order_id}/pay", {"paymentMethodId": method_id})
        self.assertEqual(response.status_code, 201, response.content)
        (request,) = self.paypal.calls("POST", r"^/v2/checkout/orders$")
        self.assertEqual(body_of(request)["payment_source"], {"card": {"vault_id": "TOKEN1"}})

        self.paypal.on("DELETE", r"/v3/vault/payment-tokens/TOKEN1$", HttpResponse(status_code=204, headers={}))
        self.assertEqual(self.api.delete(f"/api/payment-methods/{method_id}").status_code, 204)
        self.assertEqual(self.api.get("/api/payment-methods").json()["paymentMethods"], [])
        order_id = self.place_order()
        response = self.post(self.api, f"/api/orders/{order_id}/pay", {"paymentMethodId": method_id})
        self.assertEqual(response.status_code, 404)

    def test_second_card_reuses_the_paypal_customer(self) -> None:
        self.save()
        self.paypal.on("POST", r"^/v3/vault/payment-tokens$", json_response(201, {
            "id": "TOKEN2", "customer": {"id": "CUST1"},
            "payment_source": {"card": {"brand": "VISA", "last_digits": "1111"}}}))
        self.post(self.api, "/api/payment-methods", {"card": TEST_CARD})
        second = self.paypal.calls("POST", r"^/v3/vault/payment-tokens$")[-1]
        self.assertEqual(body_of(second)["customer"], {"id": "CUST1"})

    def test_idempotency_key_saves_once(self) -> None:
        first = self.save("save-1")
        again = self.save("save-1")
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(len(self.paypal.calls("POST", r"^/v3/vault/payment-tokens$")), 1)

    def test_cards_are_private_to_their_owner(self) -> None:
        method_id = self.save().json()["paymentMethodId"]
        other = Client()
        other.force_login(self.other)
        self.assertEqual(other.get("/api/payment-methods").json()["paymentMethods"], [])
        self.assertEqual(other.delete(f"/api/payment-methods/{method_id}").status_code, 404)
        other_order = self.post(other, "/api/orders", {"items": [{"productId": self.product.pk}]}).json()["orderId"]
        response = self.post(other, f"/api/orders/{other_order}/pay", {"paymentMethodId": method_id})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(SavedCard.objects.get().state, "active")

    def test_token_already_gone_at_paypal_counts_as_deleted(self) -> None:
        method_id = self.save().json()["paymentMethodId"]
        # PayPal's 404 body here does not match its declared error schema; the
        # boundary classifies it by the status actually received.
        self.paypal.on("DELETE", r"/payment-tokens/TOKEN1$", json_response(404, {
            "name": "RESOURCE_NOT_FOUND", "message": "gone", "debug_id": "d",
            "links": [{"href": "https://example.test", "method": "GET"}]}))
        self.assertEqual(self.api.delete(f"/api/payment-methods/{method_id}").status_code, 204)
        self.assertEqual(SavedCard.objects.get().state, "deleted")


class ReconciliationTests(PayPalApiTestCase):
    def test_lines_up_paypal_and_local_records_across_pages(self) -> None:
        order_id = self.fulfilled_order()
        payment = self.payment(order_id)
        capture_time = payment.capture_time
        assert capture_time is not None
        page1 = {
            "transaction_details": [{"transaction_info": {
                "transaction_id": "CAP1", "transaction_event_code": "T0006",
                "transaction_initiation_date": capture_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "transaction_amount": {"currency_code": "USD", "value": "12.34"},
                "fee_amount": {"currency_code": "USD", "value": "-0.79"}, "custom_field": order_id}}],
            "page": 1, "total_pages": 2, "last_refreshed_datetime": iso(timedelta(hours=1)),
        }
        page2 = {
            "transaction_details": [{"transaction_info": {
                "transaction_id": "STRANGER1", "transaction_event_code": "T0006",
                "transaction_initiation_date": iso(),
                "transaction_amount": {"currency_code": "USD", "value": "1.00"}}}],
            "page": 2, "total_pages": 2, "last_refreshed_datetime": iso(timedelta(hours=1)),
        }
        self.paypal.on("GET", r"/v1/reporting/transactions$", json_response(200, page1), json_response(200, page2))
        start, end = iso(-timedelta(days=1)), iso(timedelta(days=1))
        response = self.operator.get("/api/reconciliation", {"from": start, "to": end})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertTrue(report["complete"])
        self.assertEqual([m["orderId"] for m in report["matched"]], [order_id])
        self.assertEqual([p["transactionId"] for p in report["paypalOnly"]], ["STRANGER1"])
        # The authorization was never reported by PayPal in this fake: local-only.
        self.assertEqual([row["kind"] for row in report["localOnly"]], ["authorize"])
        pages = [httpx.URL(r.url).params["page"] for r in self.paypal.calls("GET", r"/v1/reporting/transactions$")]
        self.assertEqual(pages, ["1", "2"])

    def test_long_ranges_are_split_into_31_day_searches(self) -> None:
        self.paypal.on("GET", r"/v1/reporting/transactions$", json_response(200, {"transaction_details": [],
                                                                                 "total_pages": 0}))
        response = self.operator.get(
            "/api/reconciliation", {"from": "2026-01-01T00:00:00Z", "to": "2026-03-15T00:00:00Z"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(self.paypal.calls("GET", r"/v1/reporting/transactions$")), 3)

    @patch("apps.paypal_payments.reconciliation.RETRY_DELAYS", (0.0, 0.0))
    def test_transient_search_failure_is_retried(self) -> None:
        empty = {"transaction_details": [], "total_pages": 1}
        self.paypal.on("GET", r"/v1/reporting/transactions$", json_response(503, {"name": "SERVICE_UNAVAILABLE"}),
                       json_response(200, empty))
        response = self.operator.get("/api/reconciliation", {"from": iso(-timedelta(days=1)), "to": iso()})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(self.paypal.calls("GET", r"/v1/reporting/transactions$")), 2)

    def test_is_staff_only(self) -> None:
        response = self.api.get("/api/reconciliation", {"from": iso(), "to": iso(timedelta(days=1))})
        self.assertEqual(response.status_code, 403)


class GatewayTests(PayPalApiTestCase):
    def test_unsent_and_unknown_failures_differ(self) -> None:
        def fail(error: Exception) -> gateway.PayPalError:
            def raise_it() -> None:
                raise error
            try:
                gateway.call(raise_it)
            except gateway.PayPalError as e:
                return e
            raise AssertionError("no error")

        unsent = fail(httpx.ConnectError("refused"))
        unknown = fail(httpx.ReadTimeout("no reply"))
        self.assertEqual((unsent.status_code, unsent.outcome_unknown), (502, False))
        self.assertEqual((unknown.status_code, unknown.outcome_unknown), (504, True))

    @override_settings(PAYPAL_BASE_URL="https://paypal.override.test")
    def test_base_url_override_wins(self) -> None:
        self.assertEqual(gateway.resolve_base_url(), "https://paypal.override.test")

    @override_settings(PAYPAL_ENVIRONMENT="elsewhere")
    def test_unknown_environment_without_base_url_is_refused(self) -> None:
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured):
            gateway.resolve_base_url()
