"""
Tests for the PayPal payments API.

The SDK client is real; only its transport is faked, so every request goes
through the SDK's own request building. The fake answers by route, in order,
and records every request so the tests can count provider calls and read the
references that were sent.
"""

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from oscar.test.factories import create_product
from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpRequest, HttpResponse, JsonBody

from . import gateway
from .models import Outcome, PayPalOperation, PayPalPayment, PayPalRefund, SavedCard

CARD = {
    "number": "4111 1111 1111 1111",
    "expiry": "2030-12",
    "securityCode": "123",
    "name": "Test Shopper",
    "billingAddress": {
        "addressLine1": "1 Main St",
        "city": "San Jose",
        "state": "CA",
        "postalCode": "95131",
        "countryCode": "US",
    },
}

Responder = HttpResponse | Exception | Callable[[HttpRequest], HttpResponse]


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(
        status_code=status, headers={"content-type": "application/json"}, content=json.dumps(body).encode()
    )


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class RoutingTransport:
    """Satisfies the SDK's sync transport protocol. Answers queued per route."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], list[Responder]]] = []
        self.requests: list[HttpRequest] = []

    def on(self, method: str, path: str, *responses: Responder) -> None:
        self.routes.append((method, re.compile(path + r"(\?|$)"), list(responses)))

    def send(self, request: HttpRequest) -> HttpResponse:
        if request.url.endswith("/v1/oauth2/token"):
            return json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})
        self.requests.append(request)
        path = request.url.split("paypal.test", 1)[1]
        for method, pattern, responses in self.routes:
            if method == request.method and pattern.match(path) and responses:
                answer = responses.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer(request) if callable(answer) else answer
        raise AssertionError(f"unexpected PayPal call {request.method} {path}")

    def close(self) -> None: ...

    def calls(self, method: str, fragment: str) -> list[HttpRequest]:
        return [r for r in self.requests if r.method == method and fragment in r.url]


def request_id(req: HttpRequest) -> str:
    return req.headers["paypal-request-id"]


def body_of(req: HttpRequest) -> Any:
    assert isinstance(req.body, JsonBody)
    return req.body.value


# --------------------------------------------------------------- canned PayPal bodies


def order_shell(order_id: str = "PPORDER1", amount: str = "20.00", status: str = "CREATED") -> dict[str, Any]:
    return {
        "id": order_id,
        "status": status,
        "create_time": iso(datetime.now(UTC)),
        "purchase_units": [{"reference_id": "x", "amount": {"currency_code": "USD", "value": amount}}],
    }


def authorized_order(
    auth_id: str = "AUTH1",
    amount: str = "20.00",
    status: str = "CREATED",
    created: datetime | None = None,
    order_id: str = "PPORDER1",
) -> dict[str, Any]:
    created = created or datetime.now(UTC)
    return {
        "id": order_id,
        "status": "COMPLETED",
        "purchase_units": [
            {
                "payments": {
                    "authorizations": [
                        {
                            "id": auth_id,
                            "status": status,
                            "amount": {"currency_code": "USD", "value": amount},
                            "create_time": iso(created),
                            "expiration_time": iso(created + timedelta(days=29)),
                        }
                    ]
                }
            }
        ],
    }


def authorization(
    auth_id: str = "AUTH1", status: str = "CREATED", created: datetime | None = None, amount: str = "20.00"
) -> dict[str, Any]:
    created = created or datetime.now(UTC)
    return {
        "id": auth_id,
        "status": status,
        "amount": {"currency_code": "USD", "value": amount},
        "create_time": iso(created),
        "update_time": iso(created),
        "expiration_time": iso(created + timedelta(days=29)),
    }


def capture(capture_id: str = "CAP1", status: str = "COMPLETED", amount: str = "20.00") -> dict[str, Any]:
    return {
        "id": capture_id,
        "status": status,
        "amount": {"currency_code": "USD", "value": amount},
        "create_time": iso(datetime.now(UTC)),
        "seller_receivable_breakdown": {
            "gross_amount": {"currency_code": "USD", "value": amount},
            "paypal_fee": {"currency_code": "USD", "value": "1.09"},
            "net_amount": {"currency_code": "USD", "value": str(Decimal(amount) - Decimal("1.09"))},
        },
    }


def refund(refund_id: str, amount: str, status: str = "COMPLETED") -> dict[str, Any]:
    return {
        "id": refund_id,
        "status": status,
        "amount": {"currency_code": "USD", "value": amount},
        "create_time": iso(datetime.now(UTC)),
    }


def token(token_id: str = "TOKEN1") -> dict[str, Any]:
    return {
        "id": token_id,
        "customer": {"id": "CUST1"},
        "payment_source": {
            "card": {"brand": "VISA", "last_digits": "1111", "expiry": "2030-12", "name": "Test Shopper"}
        },
    }


@override_settings(PAYPAL_CURRENCY="USD", PAYPAL_ENVIRONMENT="sandbox", PAYPAL_REFERENCE_PREFIX="t")
class PayPalApiTestCase(TestCase):
    def setUp(self) -> None:
        User = get_user_model()
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "pw-shopper-123")
        self.other = User.objects.create_user("other", "other@example.com", "pw-other-123")
        self.staff = User.objects.create_user("op", "op@example.com", "pw-staff-123", is_staff=True)
        self.product = create_product(price=Decimal("10.00"), num_in_stock=100)
        self.transport = RoutingTransport()
        gateway.set_client_for_tests(
            PaypalClient(
                base_url="https://paypal.test",
                custom_http_client=self.transport,
                oauth2=ClientCredentials(client_id="id", client_secret="secret"),
            )
        )
        self.addCleanup(gateway.set_client_for_tests, None)
        self.client.force_login(self.shopper)

    # helpers ---------------------------------------------------------

    def post(self, url: str, body: object = None, **headers: str) -> Any:
        return self.client.post(url, data=json.dumps(body or {}), content_type="application/json", headers=headers)

    def place_order(self, quantity: int = 2) -> str:
        response = self.post("/api/orders", {"items": [{"productId": self.product.pk, "quantity": quantity}]})
        self.assertEqual(response.status_code, 201, response.content)
        number = response.json()["orderId"]
        self.assertIsInstance(number, str)
        return str(number)

    def authorize(self, number: str, auth_created: datetime | None = None) -> Any:
        self.transport.on("POST", "/v2/checkout/orders", json_response(200, order_shell()))
        self.transport.on(
            "POST", "/v2/checkout/orders/PPORDER1/authorize", json_response(201, authorized_order(created=auth_created))
        )
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def as_staff(self) -> None:
        self.client.force_login(self.staff)

    def fulfil(self, number: str) -> Any:
        self.as_staff()
        self.transport.on("GET", "/v2/payments/authorizations/AUTH1", json_response(200, authorization()))
        self.transport.on("POST", "/v2/payments/authorizations/AUTH1/capture", json_response(201, capture()))
        response = self.post(f"/api/orders/{number}/fulfil")
        self.assertEqual(response.status_code, 200, response.content)
        return response


class PayFlowTests(PayPalApiTestCase):
    def test_order_starts_awaiting_payment_with_catalogue_total(self) -> None:
        number = self.place_order(quantity=2)
        payment = PayPalPayment.objects.get(order__number=number)
        self.assertEqual(payment.state, PayPalPayment.AWAITING_PAYMENT)
        self.assertEqual(payment.amount, Decimal("20.00"))
        self.assertEqual(payment.order.status, "Pending payment")
        self.assertEqual(payment.order.lines.get().quantity, 2)

    def test_pay_authorizes_exact_total_and_holds_no_card_data(self) -> None:
        number = self.place_order()
        body = self.authorize(number).json()
        self.assertEqual(body["payment"]["state"], "authorized")
        self.assertEqual(body["payment"]["authorization"]["id"], "AUTH1")

        create, auth = self.transport.requests
        self.assertEqual(body_of(create)["intent"], "AUTHORIZE")
        self.assertNotIn("payment_source", body_of(create))  # no money moves on create
        self.assertEqual(body_of(create)["purchase_units"][0]["amount"], {"currency_code": "USD", "value": "20.00"})
        self.assertEqual(body_of(auth)["payment_source"]["card"]["number"], "4111111111111111")
        prefix = PayPalOperation.objects.get(kind="create_order").reference.split(":")[0]
        self.assertEqual(request_id(create), f"{prefix}:{number}:create:1")
        self.assertEqual(request_id(auth), f"{prefix}:{number}:authorize:1")

        payment = PayPalPayment.objects.get(order__number=number)
        self.assertEqual(payment.order.status, "Pending")
        self.assertEqual(_source(payment).amount_allocated, Decimal("20.00"))
        dumped = json.dumps([list(PayPalOperation.objects.values()), list(PayPalPayment.objects.values())], default=str)
        self.assertNotIn("4111111111111111", dumped)
        self.assertNotIn('"123"', dumped)

    def test_double_click_pay_authorizes_once(self) -> None:
        number = self.place_order()
        self.authorize(number)
        again = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["payment"]["state"], "authorized")
        self.assertEqual(len(self.transport.calls("POST", "/authorize")), 1)
        self.assertEqual(len(self.transport.calls("POST", "/v2/checkout/orders")), 2)  # create + authorize

    def test_declined_card_is_402_and_a_new_attempt_uses_new_references(self) -> None:
        number = self.place_order()
        self.transport.on(
            "POST",
            "/v2/checkout/orders",
            json_response(200, order_shell()),
            json_response(200, order_shell("PPORDER2")),
        )
        declined = {
            "name": "UNPROCESSABLE_ENTITY",
            "message": "refused",
            "debug_id": "d1",
            "details": [{"issue": "TRANSACTION_REFUSED", "description": "The request was refused"}],
        }
        self.transport.on("POST", "/v2/checkout/orders/PPORDER1/authorize", json_response(422, declined))
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()["error"], "payment_declined")
        self.assertEqual(PayPalPayment.objects.get(order__number=number).state, "declined")

        self.transport.on(
            "POST", "/v2/checkout/orders/PPORDER2/authorize", json_response(201, authorized_order(order_id="PPORDER2"))
        )
        retry = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(retry.status_code, 200, retry.content)
        refs = [request_id(r) for r in self.transport.requests]
        self.assertTrue(refs[-1].endswith(":authorize:2"))
        self.assertEqual(len(set(refs)), len(refs))

    def test_authorize_timeout_is_settled_by_order_lookup_not_a_second_authorize(self) -> None:
        number = self.place_order()
        self.transport.on("POST", "/v2/checkout/orders", json_response(200, order_shell()))
        self.transport.on("POST", "/v2/checkout/orders/PPORDER1/authorize", httpx.ReadTimeout("no reply"))
        self.transport.on("GET", "/v2/checkout/orders/PPORDER1", json_response(200, authorized_order()))
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["payment"]["state"], "authorized")
        self.assertEqual(len(self.transport.calls("POST", "/authorize")), 1)

    def test_unsent_and_unknown_failures_answer_differently(self) -> None:
        unsent_order = self.place_order()
        self.transport.on("POST", "/v2/checkout/orders", httpx.ConnectError("refused"))
        unsent = self.post(f"/api/orders/{unsent_order}/pay", {"card": CARD})

        unknown_order = self.place_order()
        self.transport.on("POST", "/v2/checkout/orders", httpx.ReadTimeout("no reply"), httpx.ReadTimeout("again"))
        unknown = self.post(f"/api/orders/{unknown_order}/pay", {"card": CARD})

        self.assertEqual((unsent.status_code, unsent.json().get("outcomeUnknown", False)), (502, False))
        self.assertEqual((unknown.status_code, unknown.json().get("outcomeUnknown")), (504, True))
        self.assertEqual(PayPalPayment.objects.get(order__number=unsent_order).state, "declined")
        self.assertEqual(PayPalPayment.objects.get(order__number=unknown_order).state, "authorizing")
        # The unknown one was checked by resending under the SAME reference.
        refs = [request_id(r) for r in self.transport.requests if unknown_order in request_id(r)]
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0], refs[1])

    def test_authorized_amount_mismatch_needs_review(self) -> None:
        number = self.place_order()
        self.transport.on("POST", "/v2/checkout/orders", json_response(200, order_shell()))
        self.transport.on(
            "POST", "/v2/checkout/orders/PPORDER1/authorize", json_response(201, authorized_order(amount="19.99"))
        )
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "needs_review")
        self.assertEqual(PayPalPayment.objects.get(order__number=number).state, "needs_review")

    def test_unlisted_authorization_status_is_not_success(self) -> None:
        number = self.place_order()
        self.transport.on("POST", "/v2/checkout/orders", json_response(200, order_shell()))
        self.transport.on(
            "POST",
            "/v2/checkout/orders/PPORDER1/authorize",
            json_response(201, authorized_order(status="SOMETHING_NEW")),
        )
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 504)
        self.assertNotEqual(PayPalPayment.objects.get(order__number=number).state, "authorized")

    def test_denied_authorization_with_an_id_is_declined_not_success(self) -> None:
        number = self.place_order()
        self.transport.on("POST", "/v2/checkout/orders", json_response(200, order_shell()))
        self.transport.on(
            "POST", "/v2/checkout/orders/PPORDER1/authorize", json_response(201, authorized_order(status="DENIED"))
        )
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual((response.status_code, response.json()["error"]), (402, "payment_declined"))
        payment = PayPalPayment.objects.get(order__number=number)
        self.assertEqual((payment.state, payment.order.status), ("declined", "Pending payment"))
        self.assertIsNone(payment.source)

    def test_payer_action_required_is_reported_not_handled(self) -> None:
        number = self.place_order()
        self.transport.on("POST", "/v2/checkout/orders", json_response(200, order_shell()))
        self.transport.on(
            "POST",
            "/v2/checkout/orders/PPORDER1/authorize",
            json_response(200, {"id": "PPORDER1", "status": "PAYER_ACTION_REQUIRED"}),
        )
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["payment"]["state"], "payer_action_required")

    def test_bad_credentials_are_a_provider_config_error(self) -> None:
        number = self.place_order()

        class BadCreds(RoutingTransport):
            def send(self, request: HttpRequest) -> HttpResponse:
                return json_response(401, {"error": "invalid_client"})

        gateway.set_client_for_tests(
            PaypalClient(
                base_url="https://paypal.test",
                custom_http_client=BadCreds(),
                oauth2=ClientCredentials(client_id="x", client_secret="y"),
            )
        )
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "provider_config")


class FulfilCancelTests(PayPalApiTestCase):
    def test_fulfil_captures_and_records_fee_and_net(self) -> None:
        number = self.place_order()
        self.authorize(number)
        body = self.fulfil(number).json()
        cap = body["payment"]["capture"]
        self.assertEqual((cap["amount"], cap["paypalFee"], cap["netAmount"]), ("20.00", "1.09", "18.91"))
        self.assertEqual(body["payment"]["state"], "captured")
        payment = PayPalPayment.objects.get(order__number=number)
        self.assertEqual(payment.order.status, "Complete")
        self.assertEqual(_source(payment).amount_debited, Decimal("20.00"))
        capture_req = self.transport.calls("POST", "/capture")[0]
        self.assertEqual(body_of(capture_req)["amount"], {"currency_code": "USD", "value": "20.00"})
        # A repeat does not capture again.
        again = self.post(f"/api/orders/{number}/fulfil")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(self.transport.calls("POST", "/capture")), 1)

    def test_fulfil_is_staff_only(self) -> None:
        number = self.place_order()
        self.authorize(number)
        self.assertEqual(self.post(f"/api/orders/{number}/fulfil").status_code, 403)
        self.assertEqual(self.post(f"/api/orders/{number}/cancel").status_code, 403)

    def test_capture_timeout_then_repeat_resends_same_reference(self) -> None:
        number = self.place_order()
        self.authorize(number)
        self.as_staff()
        self.transport.on("GET", "/v2/payments/authorizations/AUTH1", json_response(200, authorization()))
        self.transport.on(
            "POST",
            "/v2/payments/authorizations/AUTH1/capture",
            httpx.ReadTimeout("no reply"),
            httpx.ReadTimeout("still nothing"),
            json_response(201, capture()),
        )
        first = self.post(f"/api/orders/{number}/fulfil")
        self.assertEqual((first.status_code, first.json()["outcomeUnknown"]), (504, True))
        self.assertEqual(PayPalPayment.objects.get(order__number=number).state, "authorized")
        second = self.post(f"/api/orders/{number}/fulfil")
        self.assertEqual(second.status_code, 200, second.content)
        refs = {request_id(r) for r in self.transport.calls("POST", "/capture")}
        self.assertEqual(len(refs), 1)

    def test_unlisted_capture_status_is_not_captured(self) -> None:
        number = self.place_order()
        self.authorize(number)
        self.as_staff()
        self.transport.on("GET", "/v2/payments/authorizations/AUTH1", json_response(200, authorization()))
        self.transport.on(
            "POST", "/v2/payments/authorizations/AUTH1/capture", json_response(201, capture(status="SOMETHING_NEW"))
        )
        response = self.post(f"/api/orders/{number}/fulfil")
        self.assertEqual(response.status_code, 504)
        self.assertNotEqual(PayPalPayment.objects.get(order__number=number).state, "captured")

    def test_stale_authorization_is_renewed_before_capture(self) -> None:
        number = self.place_order()
        old = datetime.now(UTC) - timedelta(days=5)
        self.authorize(number, auth_created=old)
        self.as_staff()
        self.transport.on("GET", "/v2/payments/authorizations/AUTH1", json_response(200, authorization(created=old)))
        self.transport.on(
            "POST", "/v2/payments/authorizations/AUTH1/reauthorize", json_response(201, authorization("AUTH2"))
        )
        self.transport.on("POST", "/v2/payments/authorizations/AUTH2/capture", json_response(201, capture()))
        response = self.post(f"/api/orders/{number}/fulfil")
        self.assertEqual(response.status_code, 200, response.content)
        auth = response.json()["payment"]["authorization"]
        self.assertEqual((auth["id"], auth["reauthorized"]), ("AUTH2", True))

    def test_expired_authorization_says_what_to_do(self) -> None:
        number = self.place_order()
        self.authorize(number)
        self.as_staff()
        expired = authorization(status="VOIDED", created=datetime.now(UTC) - timedelta(days=31))
        self.transport.on("GET", "/v2/payments/authorizations/AUTH1", json_response(200, expired))
        response = self.post(f"/api/orders/{number}/fulfil")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "authorization_not_renewable")
        self.assertIn("Cancel this order", response.json()["message"])
        self.assertEqual(self.transport.calls("POST", "/capture"), [])

    def test_refused_reauthorization_says_what_to_do(self) -> None:
        number = self.place_order()
        old = datetime.now(UTC) - timedelta(days=5)
        self.authorize(number, auth_created=old)
        self.as_staff()
        self.transport.on("GET", "/v2/payments/authorizations/AUTH1", json_response(200, authorization(created=old)))
        refused = {
            "name": "UNPROCESSABLE_ENTITY",
            "message": "no",
            "debug_id": "d",
            "details": [{"issue": "REAUTHORIZATION_NOT_ALLOWED", "description": "Not allowed"}],
        }
        self.transport.on("POST", "/v2/payments/authorizations/AUTH1/reauthorize", json_response(422, refused))
        response = self.post(f"/api/orders/{number}/fulfil")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "authorization_not_renewable")
        self.assertEqual(PayPalPayment.objects.get(order__number=number).state, "authorized")

    def test_cancel_voids_the_hold(self) -> None:
        number = self.place_order()
        self.authorize(number)
        self.as_staff()
        self.transport.on(
            "POST", "/v2/payments/authorizations/AUTH1/void", json_response(200, authorization(status="VOIDED"))
        )
        response = self.post(f"/api/orders/{number}/cancel")
        self.assertEqual(response.status_code, 200, response.content)
        payment = PayPalPayment.objects.get(order__number=number)
        self.assertEqual((payment.state, payment.order.status), ("voided", "Cancelled"))
        self.assertEqual(self.post(f"/api/orders/{number}/cancel").status_code, 200)
        self.assertEqual(len(self.transport.calls("POST", "/void")), 1)

    def test_cancel_unpaid_order_needs_no_provider_call(self) -> None:
        number = self.place_order()
        self.as_staff()
        response = self.post(f"/api/orders/{number}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["payment"]["state"], "cancelled")
        self.assertEqual(self.transport.requests, [])

    def test_cancel_after_fulfilment_is_refused(self) -> None:
        number = self.place_order()
        self.authorize(number)
        self.fulfil(number)
        self.assertEqual(self.post(f"/api/orders/{number}/cancel").json()["error"], "already_fulfilled")


class RefundTests(PayPalApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.number = self.place_order()
        self.authorize(self.number)
        self.fulfil(self.number)
        self.client.force_login(self.shopper)

    def test_refund_requires_idempotency_key(self) -> None:
        self.assertEqual(self.post(f"/api/orders/{self.number}/refunds", {"amount": "5.00"}).status_code, 400)

    def test_same_key_refunds_once_distinct_keys_refund_twice(self) -> None:
        self.transport.on(
            "POST",
            "/v2/payments/captures/CAP1/refund",
            json_response(201, refund("R1", "5.00")),
            json_response(201, refund("R2", "5.00")),
        )
        url = f"/api/orders/{self.number}/refunds"
        first = self.post(url, {"amount": "5.00"}, **{"Idempotency-Key": "k1"})
        repeat = self.post(url, {"amount": "5.00"}, **{"Idempotency-Key": "k1"})
        second = self.post(url, {"amount": "5.00"}, **{"Idempotency-Key": "k2"})
        self.assertEqual((first.status_code, repeat.status_code, second.status_code), (201, 201, 201))
        self.assertEqual(first.json()["refundId"], repeat.json()["refundId"])
        self.assertNotEqual(first.json()["refundId"], second.json()["refundId"])
        self.assertEqual(len(self.transport.calls("POST", "/refund")), 2)
        payment = PayPalPayment.objects.get(order__number=self.number)
        self.assertEqual((payment.state, payment.refunded_amount), ("partially_refunded", Decimal("10.00")))
        self.assertEqual(_source(payment).amount_refunded, Decimal("10.00"))

    def test_cannot_refund_beyond_captured(self) -> None:
        self.transport.on("POST", "/v2/payments/captures/CAP1/refund", json_response(201, refund("R1", "15.00")))
        url = f"/api/orders/{self.number}/refunds"
        self.assertEqual(self.post(url, {"amount": "15.00"}, **{"Idempotency-Key": "a"}).status_code, 201)
        over = self.post(url, {"amount": "6.00"}, **{"Idempotency-Key": "b"})
        self.assertEqual(over.status_code, 422)
        self.assertEqual(over.json()["refundable"], "5.00")
        self.assertEqual(len(self.transport.calls("POST", "/refund")), 1)

    def test_full_refund_without_amount(self) -> None:
        self.transport.on("POST", "/v2/payments/captures/CAP1/refund", json_response(201, refund("R1", "20.00")))
        response = self.post(f"/api/orders/{self.number}/refunds", {}, **{"Idempotency-Key": "full"})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["payment"]["state"], "refunded")
        self.assertEqual(body_of(self.transport.calls("POST", "/refund")[0])["amount"]["value"], "20.00")

    def test_same_key_different_amount_is_rejected(self) -> None:
        self.transport.on("POST", "/v2/payments/captures/CAP1/refund", json_response(201, refund("R1", "5.00")))
        url = f"/api/orders/{self.number}/refunds"
        self.post(url, {"amount": "5.00"}, **{"Idempotency-Key": "k"})
        self.assertEqual(self.post(url, {"amount": "6.00"}, **{"Idempotency-Key": "k"}).status_code, 409)

    def test_failed_refund_releases_its_reservation(self) -> None:
        self.transport.on(
            "POST",
            "/v2/payments/captures/CAP1/refund",
            json_response(201, refund("R1", "20.00", "FAILED")),
            json_response(201, refund("R2", "20.00")),
        )
        url = f"/api/orders/{self.number}/refunds"
        self.assertEqual(self.post(url, {}, **{"Idempotency-Key": "f1"}).status_code, 402)
        self.assertEqual(PayPalRefund.objects.get(idempotency_key="f1").outcome, Outcome.FAILED)
        self.assertEqual(self.post(url, {}, **{"Idempotency-Key": "f2"}).status_code, 201)

    def test_other_shopper_cannot_refund(self) -> None:
        self.client.force_login(self.other)
        response = self.post(f"/api/orders/{self.number}/refunds", {}, **{"Idempotency-Key": "x"})
        self.assertEqual(response.status_code, 404)


class SavedCardTests(PayPalApiTestCase):
    def save(self) -> str:
        self.transport.on("POST", "/v3/vault/payment-tokens", json_response(200, token()))
        response = self.post("/api/payment-methods", {"card": CARD}, **{"Idempotency-Key": "save-1"})
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual((body["brand"], body["lastDigits"]), ("VISA", "1111"))
        self.assertNotIn("number", body)
        return str(body["paymentMethodId"])

    def test_save_list_pay_delete(self) -> None:
        method_id = self.save()
        listed = self.client.get("/api/payment-methods").json()["paymentMethods"]
        self.assertEqual([m["paymentMethodId"] for m in listed], [method_id])
        self.assertNotIn("4111111111111111", json.dumps(list(SavedCard.objects.values()), default=str))

        number = self.place_order()
        self.transport.on("POST", "/v2/checkout/orders", json_response(200, order_shell()))
        self.transport.on("POST", "/v2/checkout/orders/PPORDER1/authorize", json_response(201, authorized_order()))
        paid = self.post(f"/api/orders/{number}/pay", {"paymentMethodId": method_id})
        self.assertEqual(paid.status_code, 200, paid.content)
        self.assertEqual(
            body_of(self.transport.calls("POST", "/authorize")[0])["payment_source"], {"card": {"vault_id": "TOKEN1"}}
        )

        self.transport.on("DELETE", "/v3/vault/payment-tokens/TOKEN1", HttpResponse(status_code=204, headers={}))
        self.assertEqual(self.client.delete(f"/api/payment-methods/{method_id}").status_code, 204)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        self.assertTrue(SavedCard.objects.get(public_id=method_id).provider_deleted)
        second = self.place_order()
        self.assertEqual(self.post(f"/api/orders/{second}/pay", {"paymentMethodId": method_id}).status_code, 404)

    def test_repeat_save_under_same_key_vaults_once(self) -> None:
        first = self.save()
        again = self.post("/api/payment-methods", {"card": CARD}, **{"Idempotency-Key": "save-1"})
        self.assertEqual(again.json()["paymentMethodId"], first)
        self.assertEqual(len(self.transport.calls("POST", "/v3/vault/payment-tokens")), 1)

    def test_cards_belong_to_their_owner(self) -> None:
        method_id = self.save()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        self.assertEqual(self.client.delete(f"/api/payment-methods/{method_id}").status_code, 404)
        self.product2 = create_product(price=Decimal("3.00"), num_in_stock=5)
        response = self.post("/api/orders", {"items": [{"productId": self.product2.pk, "quantity": 1}]})
        number = response.json()["orderId"]
        self.assertEqual(self.post(f"/api/orders/{number}/pay", {"paymentMethodId": method_id}).status_code, 404)
        self.assertEqual(self.transport.calls("POST", "/v2/checkout"), [])


class OwnershipTests(PayPalApiTestCase):
    def test_orders_are_private(self) -> None:
        number = self.place_order()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"], [])
        self.assertEqual(self.post(f"/api/orders/{number}/pay", {"card": CARD}).status_code, 404)

    def test_my_orders_lists_payment_state(self) -> None:
        number = self.place_order()
        self.authorize(number)
        orders = self.client.get("/api/my-orders").json()["orders"]
        self.assertEqual([(o["orderId"], o["payment"]["state"]) for o in orders], [(number, "authorized")])

    def test_anonymous_is_401(self) -> None:
        self.client.logout()
        self.assertEqual(self.client.get("/api/my-orders").status_code, 401)


class ReconciliationTests(PayPalApiTestCase):
    def test_reads_every_page_of_every_window_and_lines_up_both_sides(self) -> None:
        number = self.place_order()
        self.authorize(number)
        self.fulfil(number)  # staff now logged in
        now = datetime.now(UTC)

        def txn(tid: str, code: str, amount: str, when: datetime) -> dict[str, Any]:
            return {
                "transaction_info": {
                    "transaction_id": tid,
                    "transaction_event_code": code,
                    "transaction_initiation_date": iso(when),
                    "transaction_status": "S",
                    "transaction_amount": {"currency_code": "USD", "value": amount},
                }
            }

        pages = {
            ("1", "early"): {
                "transaction_details": [txn("OLD1", "T0006", "9.00", now - timedelta(days=38))],
                "total_pages": 1,
            },
            ("1", "late"): {"transaction_details": [txn("AUTH1", "T1300", "20.00", now)], "total_pages": 2},
            ("2", "late"): {
                "transaction_details": [txn("CAP1", "T0006", "20.00", now), txn("STRAY", "T0006", "4.00", now)],
                "total_pages": 2,
            },
        }

        def answer(request: HttpRequest) -> HttpResponse:
            query = dict(p.split("=", 1) for p in request.url.split("?", 1)[1].split("&"))
            window = "late" if query["end_date"].startswith(now.strftime("%Y-%m-%d")) else "early"
            return json_response(200, pages[(query["page"], window)])

        self.transport.on("GET", "/v1/reporting/transactions", answer, answer, answer)
        start = (now - timedelta(days=40)).replace(microsecond=0)
        response = self.client.get(
            "/api/reconciliation", {"from": start.isoformat(), "to": (now + timedelta(minutes=5)).isoformat()}
        )
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual(len(self.transport.calls("GET", "/v1/reporting/transactions")), 3)
        self.assertEqual(report["paypalTransactionCount"], 4)
        matched_ids = {t["transactionId"] for o in report["matched"] for t in o["paypalTransactions"]}
        self.assertEqual(matched_ids, {"AUTH1", "CAP1"})
        self.assertEqual({t["transactionId"] for t in report["paypalOnly"]}, {"OLD1", "STRAY"})
        self.assertEqual(report["localOnly"], [])

    def test_reconciliation_is_staff_only(self) -> None:
        response = self.client.get(
            "/api/reconciliation", {"from": "2026-01-01T00:00:00Z", "to": "2026-01-02T00:00:00Z"}
        )
        self.assertEqual(response.status_code, 403)


def _source(payment: PayPalPayment) -> Any:
    assert payment.source is not None
    return payment.source
