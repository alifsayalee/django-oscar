"""
Tests for the PayPal integration. PayPal is faked at the SDK's transport seam
(``custom_http_client``), so the real SDK builds every request and decodes
every response; nothing reaches the network.

Run with:  cd sandbox && python manage.py test apps.payments
"""

import json
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal as D
from typing import Any

import httpx
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from oscar.test.factories import create_product
from pay_pal_server_sdk import PayPalServerSdkClient
from pay_pal_server_sdk.core import ClientCredentials, HttpRequest, HttpResponse, JsonBody

from . import paypal
from .models import OrderPayment, Outcome, PaymentState, ProviderWrite, SavedCard

CARD = {"number": "4111111111111111", "expiry": "2030-12", "securityCode": "123", "name": "Test Shopper"}


# --- the stub transport --------------------------------------------------------------

@dataclass
class StubResponse:
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    chunks: list[bytes] = field(default_factory=list)
    url: str = "https://paypal.test/"
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
    return StubResponse(status, {"content-type": "application/json"}, [json.dumps(body).encode()])


Reply = StubResponse | Exception | Callable[[HttpRequest], StubResponse]


class RoutingTransport:
    """Answers by (method, path regex); each route replays its replies in order, the last one repeating."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], list[Reply]]] = []
        self.requests: list[HttpRequest] = []
        self.token_status = 200

    def on(self, method: str, pattern: str, *replies: Reply) -> None:
        self.routes.insert(0, (method, re.compile(pattern), list(replies)))

    def send(self, request: HttpRequest) -> HttpResponse:
        path = httpx.URL(request.url).path
        if path == "/v1/oauth2/token":
            if self.token_status != 200:
                return json_response(self.token_status, {"error": "invalid_client"})
            return json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})
        self.requests.append(request)
        for method, pattern, replies in self.routes:
            if method == request.method and pattern.fullmatch(path):
                reply = replies.pop(0) if len(replies) > 1 else replies[0]
                if isinstance(reply, Exception):
                    raise reply
                if callable(reply) and not isinstance(reply, StubResponse):
                    return reply(request)
                return reply
        raise AssertionError(f"unexpected PayPal call {request.method} {path}")

    def close(self) -> None:
        pass

    def calls(self, method: str, pattern: str) -> list[HttpRequest]:
        rx = re.compile(pattern)
        return [r for r in self.requests if r.method == method and rx.fullmatch(httpx.URL(r.url).path)]


def body_of(request: HttpRequest) -> Any:
    assert isinstance(request.body, JsonBody)
    return request.body.value


# --- canned PayPal bodies ------------------------------------------------------------

def money(value: str) -> dict[str, str]:
    return {"currency_code": "USD", "value": value}


def order_body(status: str = "APPROVED", value: str = "30.00") -> dict[str, Any]:
    return {"id": "PO1", "status": status, "create_time": "2026-09-29T00:00:00Z",
            "purchase_units": [{"amount": money(value)}]}


def authorization(status: str = "CREATED", value: str = "30.00", auth_id: str = "AUTH1") -> dict[str, Any]:
    now = timezone.now()
    return {"id": auth_id, "status": status, "amount": money(value),
            "create_time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expiration_time": (now + timedelta(days=29)).strftime("%Y-%m-%dT%H:%M:%SZ")}


def authorize_body(status: str = "CREATED", value: str = "30.00") -> dict[str, Any]:
    return {"id": "PO1", "status": "COMPLETED",
            "purchase_units": [{"payments": {"authorizations": [authorization(status, value)]}}]}


def capture_body(status: str = "COMPLETED", value: str = "30.00") -> dict[str, Any]:
    return {"id": "CAP1", "status": status, "amount": money(value), "create_time": "2026-09-29T01:00:00Z",
            "seller_receivable_breakdown": {"gross_amount": money(value), "paypal_fee": money("1.27"),
                                            "net_amount": money("28.73")}}


def refund_body(value: str, refund_id: str = "R1", status: str = "COMPLETED") -> dict[str, Any]:
    return {"id": refund_id, "status": status, "amount": money(value), "create_time": "2026-09-29T02:00:00Z"}


# --- base -------------------------------------------------------------------------------

@override_settings(PAYPAL_CLIENT_ID="test-id", PAYPAL_CLIENT_SECRET="test-secret", PAYPAL_CURRENCY="USD",
                   PAYPAL_ENVIRONMENT="sandbox", PAYPAL_BASE_URL="", PAYPAL_REFERENCE_PREFIX="t")
class PayPalTestCase(TestCase):
    def setUp(self) -> None:
        self.transport = RoutingTransport()
        paypal.set_client(PayPalServerSdkClient(
            custom_http_client=self.transport,
            oauth2=ClientCredentials(client_id="test-id", client_secret="test-secret"),
        ))
        self.addCleanup(paypal.set_client, None)
        self.product = create_product(price=D("15.00"), num_in_stock=100)
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw-alice-123")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw-bob-123")
        self.staff = User.objects.create_user("ops", "ops@example.com", "pw-ops-123", is_staff=True)

    # helpers
    def as_user(self, user: User) -> None:
        self.client.force_login(user)

    def post(self, url: str, data: dict[str, Any] | None = None, **headers: str) -> Any:
        return self.client.post(url, json.dumps(data or {}), content_type="application/json", headers=headers)

    def place_order(self, quantity: int = 2) -> str:
        self.as_user(self.alice)
        r = self.post("/api/orders", {"items": [{"productId": self.product.pk, "quantity": quantity}]})
        self.assertEqual(r.status_code, 201, r.content)
        return str(r.json()["orderId"])

    def authorized_order(self) -> str:
        order_id = self.place_order()
        self.transport.on("POST", r"/v2/checkout/orders", json_response(201, order_body()))
        self.transport.on("POST", r"/v2/checkout/orders/PO1/authorize", json_response(201, authorize_body()))
        r = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual(r.status_code, 200, r.content)
        return order_id

    def captured_order(self) -> str:
        order_id = self.authorized_order()
        self.transport.on("GET", r"/v2/payments/authorizations/AUTH1", json_response(200, authorization()))
        self.transport.on("POST", r"/v2/payments/authorizations/AUTH1/capture", json_response(201, capture_body()))
        self.as_user(self.staff)
        r = self.post(f"/api/orders/{order_id}/fulfil")
        self.assertEqual(r.status_code, 200, r.content)
        self.as_user(self.alice)
        return order_id


# --- configuration ---------------------------------------------------------------------

class ConfigurationTests(TestCase):
    @override_settings(PAYPAL_BASE_URL="https://paypal-proxy.example/", PAYPAL_ENVIRONMENT="sandbox")
    def test_base_url_override_is_used_verbatim(self) -> None:
        self.assertEqual(paypal.resolve_base_url(), "https://paypal-proxy.example/")

    @override_settings(PAYPAL_BASE_URL="", PAYPAL_ENVIRONMENT="sandbox")
    def test_sandbox_environment_selects_the_sdk_sandbox_host(self) -> None:
        self.assertEqual(paypal.resolve_base_url(), "https://api-m.sandbox.paypal.com")

    @override_settings(PAYPAL_BASE_URL="", PAYPAL_ENVIRONMENT="production")
    def test_unknown_environment_without_base_url_fails_loudly(self) -> None:
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaises(ImproperlyConfigured):
            paypal.resolve_base_url()

    @override_settings(PAYPAL_CLIENT_ID="", PAYPAL_CLIENT_SECRET="x")
    def test_missing_credentials_never_build_an_unauthenticated_client(self) -> None:
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaises(ImproperlyConfigured):
            paypal.build_client()


# --- access control ----------------------------------------------------------------------

class AccessTests(PayPalTestCase):
    def test_anonymous_is_refused(self) -> None:
        self.assertEqual(self.client.get("/api/my-orders").status_code, 401)

    def test_shopper_cannot_see_or_act_on_anothers_order(self) -> None:
        order_id = self.place_order()
        self.as_user(self.bob)
        self.assertEqual(self.post(f"/api/orders/{order_id}/pay", {"card": CARD}).status_code, 404)
        self.assertEqual(self.post(f"/api/orders/{order_id}/refunds", {}, **{"Idempotency-Key": "k"}).status_code, 404)
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"], [])
        self.assertEqual(self.transport.requests, [])

    def test_operator_actions_need_staff(self) -> None:
        order_id = self.place_order()
        self.assertEqual(self.post(f"/api/orders/{order_id}/fulfil").status_code, 403)
        self.assertEqual(self.post(f"/api/orders/{order_id}/cancel").status_code, 403)
        self.assertEqual(self.client.get("/api/reconciliation?from=2026-01-01T00:00:00Z&to=2026-01-02T00:00:00Z")
                         .status_code, 403)


# --- pay ---------------------------------------------------------------------------------

class PayTests(PayPalTestCase):
    def test_order_starts_awaiting_payment(self) -> None:
        order_id = self.place_order()
        order = self.client.get("/api/my-orders").json()["orders"][0]
        self.assertEqual(order["orderId"], order_id)
        self.assertEqual(order["payment"]["state"], "awaiting_payment")
        self.assertEqual(order["payment"]["amount"], "30.00")

    def test_authorizes_exact_total_and_double_click_makes_no_second_call(self) -> None:
        order_id = self.authorized_order()
        r = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})  # the double-click
        self.assertEqual(r.status_code, 200)
        creates = self.transport.calls("POST", r"/v2/checkout/orders")
        self.assertEqual(len(creates), 1)
        self.assertEqual(len(self.transport.calls("POST", r"/v2/checkout/orders/PO1/authorize")), 1)
        sent = body_of(creates[0])
        self.assertEqual(sent["intent"], "AUTHORIZE")
        self.assertEqual(sent["purchase_units"][0]["amount"], {"currency_code": "USD", "value": "30.00"})
        payment = OrderPayment.objects.get()
        self.assertEqual(creates[0].headers["paypal-request-id"], f"t:o{payment.order_id}:a1:create")
        self.assertEqual(payment.state, PaymentState.AUTHORIZED)
        assert payment.source is not None
        self.assertEqual(payment.source.amount_allocated, D("30.00"))  # Oscar's payment Source reused

    def test_card_details_are_never_stored(self) -> None:
        self.authorized_order()
        for row in ProviderWrite.objects.all():
            self.assertNotIn("4111111111111111", json.dumps([row.ref, row.detail, row.provider_id]))
        self.assertEqual(OrderPayment.objects.get().card_label, "Card ending 1111")

    def test_denied_authorization_is_not_success(self) -> None:
        order_id = self.place_order()
        self.transport.on("POST", r"/v2/checkout/orders", json_response(201, order_body()))
        self.transport.on("POST", r"/v2/checkout/orders/PO1/authorize", json_response(201, authorize_body("DENIED")))
        r = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(OrderPayment.objects.get().state, PaymentState.PAYMENT_FAILED)

    def test_unlisted_status_is_unknown_not_done(self) -> None:
        order_id = self.place_order()
        self.transport.on("POST", r"/v2/checkout/orders", json_response(201, order_body()))
        self.transport.on("POST", r"/v2/checkout/orders/PO1/authorize",
                          json_response(201, authorize_body("SOMETHING_NEW")))
        r = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual(r.status_code, 504)
        self.assertTrue(r.json()["outcomeUnknown"])
        self.assertNotEqual(OrderPayment.objects.get().state, PaymentState.AUTHORIZED)

    def test_payer_action_required_is_reported_not_approved(self) -> None:
        order_id = self.place_order()
        self.transport.on("POST", r"/v2/checkout/orders", json_response(201, order_body("PAYER_ACTION_REQUIRED")))
        r = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual(r.status_code, 409)
        self.assertIn("browser", r.json()["message"])
        self.assertEqual(self.transport.calls("POST", r"/v2/checkout/orders/PO1/authorize"), [])

    def test_amount_mismatch_needs_review(self) -> None:
        order_id = self.place_order()
        self.transport.on("POST", r"/v2/checkout/orders", json_response(201, order_body()))
        self.transport.on("POST", r"/v2/checkout/orders/PO1/authorize",
                          json_response(201, authorize_body(value="29.99")))
        r = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(ProviderWrite.objects.get(kind="authorize").outcome, Outcome.NEEDS_REVIEW)

    def test_refused_connection_and_read_timeout_are_different_outcomes(self) -> None:
        order_id = self.place_order()
        self.transport.on("POST", r"/v2/checkout/orders", httpx.ConnectError("refused"))
        unsent = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual((unsent.status_code, unsent.json().get("outcomeUnknown", False)), (502, False))
        self.assertFalse(ProviderWrite.objects.exists())  # nothing happened: the claim was released

        order2 = self.place_order()
        self.transport.on("POST", r"/v2/checkout/orders", httpx.ReadTimeout("no reply"))
        unknown = self.post(f"/api/orders/{order2}/pay", {"card": CARD})
        self.assertEqual((unknown.status_code, unknown.json()["outcomeUnknown"]), (504, True))
        self.assertEqual(ProviderWrite.objects.get().outcome, Outcome.UNKNOWN)

    def test_unknown_outcome_is_settled_by_a_same_reference_resend(self) -> None:
        order_id = self.place_order()
        self.transport.on("POST", r"/v2/checkout/orders", httpx.ReadTimeout("no reply"), json_response(201, order_body()))
        self.transport.on("POST", r"/v2/checkout/orders/PO1/authorize", json_response(201, authorize_body()))
        self.assertEqual(self.post(f"/api/orders/{order_id}/pay", {"card": CARD}).status_code, 504)
        self.assertEqual(self.post(f"/api/orders/{order_id}/pay", {"card": CARD}).status_code, 200)
        creates = self.transport.calls("POST", r"/v2/checkout/orders")
        self.assertEqual(len(creates), 2)
        self.assertEqual(creates[0].headers["paypal-request-id"], creates[1].headers["paypal-request-id"])

    def test_a_request_racing_an_in_flight_claim_makes_no_provider_call(self) -> None:
        order_id = self.place_order()
        payment = OrderPayment.objects.get()
        # Another worker holds the claim and is still waiting for PayPal.
        ProviderWrite.objects.create(ref=f"t:o{payment.order_id}:a1:create", kind="create_order",
                                     outcome=Outcome.SENDING, claimed_at=timezone.now())
        r = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(self.transport.requests, [])

    def test_bad_credentials_are_our_problem_not_the_callers(self) -> None:
        order_id = self.place_order()
        self.transport.token_status = 401
        r = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual(r.status_code, 502)

    def test_invalid_card_is_rejected_before_paypal(self) -> None:
        order_id = self.place_order()
        r = self.post(f"/api/orders/{order_id}/pay", {"card": {**CARD, "number": "4111111111111112"}})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.transport.requests, [])


# --- fulfil / cancel -----------------------------------------------------------------------

class FulfilCancelTests(PayPalTestCase):
    def test_fulfil_captures_and_records_fee_and_net(self) -> None:
        order_id = self.captured_order()
        payment = self.client.get("/api/my-orders").json()["orders"][0]["payment"]
        self.assertEqual(payment["capture"]["amount"], "30.00")
        self.assertEqual(payment["capture"]["paypalFee"], "1.27")
        self.assertEqual(payment["capture"]["netAmount"], "28.73")
        self.as_user(self.staff)
        self.assertEqual(self.post(f"/api/orders/{order_id}/fulfil").status_code, 200)
        self.assertEqual(len(self.transport.calls("POST", r"/v2/payments/authorizations/AUTH1/capture")), 1)
        capture = self.transport.calls("POST", r"/v2/payments/authorizations/AUTH1/capture")[0]
        self.assertEqual(body_of(capture)["amount"], {"currency_code": "USD", "value": "30.00"})

    def test_stale_authorization_is_renewed_before_capture(self) -> None:
        order_id = self.authorized_order()
        OrderPayment.objects.update(authorized_at=timezone.now() - timedelta(days=5),
                                    honor_period_start=timezone.now() - timedelta(days=5))
        self.transport.on("GET", r"/v2/payments/authorizations/AUTH1", json_response(200, authorization()))
        self.transport.on("POST", r"/v2/payments/authorizations/AUTH1/reauthorize",
                          json_response(201, authorization(auth_id="AUTH2")))
        self.transport.on("POST", r"/v2/payments/authorizations/AUTH2/capture", json_response(201, capture_body()))
        self.as_user(self.staff)
        r = self.post(f"/api/orders/{order_id}/fulfil")
        self.assertEqual(r.status_code, 200, r.content)
        payment = OrderPayment.objects.get()
        self.assertEqual((payment.authorization_id, payment.reauthorizations, payment.state), ("AUTH2", 1, "captured"))

    def test_authorization_past_renewal_window_says_what_to_do(self) -> None:
        order_id = self.authorized_order()
        OrderPayment.objects.update(authorized_at=timezone.now() - timedelta(days=30))
        self.transport.on("GET", r"/v2/payments/authorizations/AUTH1", json_response(200, authorization()))
        self.as_user(self.staff)
        r = self.post(f"/api/orders/{order_id}/fulfil")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "authorization_not_renewable")
        self.assertIn("cancel the order", r.json()["message"])
        self.assertEqual(self.transport.calls("POST", r"/v2/payments/authorizations/AUTH1/capture"), [])

    def test_refused_reauthorization_says_what_to_do(self) -> None:
        order_id = self.authorized_order()
        OrderPayment.objects.update(honor_period_start=timezone.now() - timedelta(days=4))
        self.transport.on("GET", r"/v2/payments/authorizations/AUTH1", json_response(200, authorization()))
        self.transport.on("POST", r"/v2/payments/authorizations/AUTH1/reauthorize", json_response(
            422, {"name": "UNPROCESSABLE_ENTITY", "message": "cannot reauthorize", "debug_id": "d1"}))
        self.as_user(self.staff)
        r = self.post(f"/api/orders/{order_id}/fulfil")
        self.assertEqual(r.status_code, 409)
        self.assertIn("refused to renew", r.json()["message"])
        self.assertEqual(OrderPayment.objects.get().state, PaymentState.AUTHORIZED)

    def test_cancel_voids_the_hold(self) -> None:
        order_id = self.authorized_order()
        self.transport.on("POST", r"/v2/payments/authorizations/AUTH1/void",
                          json_response(200, authorization("VOIDED")))
        self.as_user(self.staff)
        self.assertEqual(self.post(f"/api/orders/{order_id}/cancel").status_code, 200)
        self.assertEqual(self.post(f"/api/orders/{order_id}/cancel").status_code, 200)
        self.assertEqual(len(self.transport.calls("POST", r"/v2/payments/authorizations/AUTH1/void")), 1)
        payment = OrderPayment.objects.get()
        self.assertEqual((payment.state, payment.order.status), ("cancelled", "Cancelled"))

    def test_void_rejected_because_already_voided_counts_as_landed(self) -> None:
        order_id = self.authorized_order()
        self.transport.on("POST", r"/v2/payments/authorizations/AUTH1/void", json_response(
            422, {"name": "UNPROCESSABLE_ENTITY", "message": "already voided", "debug_id": "d2"}))
        self.transport.on("GET", r"/v2/payments/authorizations/AUTH1", json_response(200, authorization("VOIDED")))
        self.as_user(self.staff)
        self.assertEqual(self.post(f"/api/orders/{order_id}/cancel").status_code, 200)

    def test_cancel_after_fulfilment_is_refused(self) -> None:
        order_id = self.captured_order()
        self.as_user(self.staff)
        self.assertEqual(self.post(f"/api/orders/{order_id}/cancel").status_code, 409)


# --- refunds ---------------------------------------------------------------------------------

class RefundTests(PayPalTestCase):
    def test_same_key_refunds_once_distinct_keys_refund_twice(self) -> None:
        order_id = self.captured_order()
        self.transport.on("POST", r"/v2/payments/captures/CAP1/refund",
                          json_response(201, refund_body("10.00", "R1")), json_response(201, refund_body("10.00", "R2")))
        url = f"/api/orders/{order_id}/refunds"
        first = self.post(url, {"amount": "10.00"}, **{"Idempotency-Key": "a"})
        again = self.post(url, {"amount": "10.00"}, **{"Idempotency-Key": "a"})
        other = self.post(url, {"amount": "10.00"}, **{"Idempotency-Key": "b"})
        self.assertEqual([first.status_code, again.status_code, other.status_code], [200, 200, 200])
        self.assertEqual(first.json()["refundId"], again.json()["refundId"])
        self.assertNotEqual(first.json()["refundId"], other.json()["refundId"])
        calls = self.transport.calls("POST", r"/v2/payments/captures/CAP1/refund")
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0].headers["paypal-request-id"], calls[1].headers["paypal-request-id"])
        self.assertEqual(OrderPayment.objects.get().refunded_amount, D("20.00"))

    def test_never_refundable_beyond_capture(self) -> None:
        order_id = self.captured_order()
        self.transport.on("POST", r"/v2/payments/captures/CAP1/refund", json_response(201, refund_body("25.00")))
        url = f"/api/orders/{order_id}/refunds"
        self.assertEqual(self.post(url, {"amount": "25.00"}, **{"Idempotency-Key": "a"}).status_code, 200)
        r = self.post(url, {"amount": "5.01"}, **{"Idempotency-Key": "b"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "exceeds_refundable")

    def test_unknown_refund_keeps_its_reservation(self) -> None:
        order_id = self.captured_order()
        self.transport.on("POST", r"/v2/payments/captures/CAP1/refund", httpx.ReadTimeout("no reply"))
        url = f"/api/orders/{order_id}/refunds"
        self.assertEqual(self.post(url, {"amount": "30.00"}, **{"Idempotency-Key": "a"}).status_code, 504)
        # The first refund may have landed: nothing more may be refunded meanwhile.
        self.assertEqual(self.post(url, {"amount": "1.00"}, **{"Idempotency-Key": "b"}).status_code, 409)

    def test_key_reused_with_different_amount_is_refused(self) -> None:
        order_id = self.captured_order()
        self.transport.on("POST", r"/v2/payments/captures/CAP1/refund", json_response(201, refund_body("5.00")))
        url = f"/api/orders/{order_id}/refunds"
        self.post(url, {"amount": "5.00"}, **{"Idempotency-Key": "a"})
        self.assertEqual(self.post(url, {"amount": "6.00"}, **{"Idempotency-Key": "a"}).status_code, 422)

    def test_refund_requires_key_and_capture(self) -> None:
        order_id = self.authorized_order()
        self.assertEqual(self.post(f"/api/orders/{order_id}/refunds", {}).status_code, 422)
        self.assertEqual(self.post(f"/api/orders/{order_id}/refunds", {}, **{"Idempotency-Key": "a"}).status_code, 409)


# --- saved cards ---------------------------------------------------------------------------------

class SavedCardTests(PayPalTestCase):
    token = {"id": "TOK1", "customer": {"id": "CUST1"},
             "payment_source": {"card": {"last_digits": "1111", "brand": "VISA", "expiry": "2030-12"}}}

    def save(self) -> str:
        self.transport.on("POST", r"/v3/vault/payment-tokens", json_response(200, self.token))
        self.as_user(self.alice)
        r = self.post("/api/payment-methods", {"card": CARD})
        self.assertEqual(r.status_code, 201, r.content)
        return str(r.json()["paymentMethodId"])

    def test_save_list_is_safe_and_scoped(self) -> None:
        pm = self.save()
        cards = self.client.get("/api/payment-methods").json()["paymentMethods"]
        self.assertEqual([(c["paymentMethodId"], c["lastDigits"], c["brand"]) for c in cards], [(pm, "1111", "VISA")])
        self.assertNotIn("4111111111111111", json.dumps(cards))
        self.assertFalse(SavedCard.objects.filter(last_digits__gt="9999").exists())
        self.as_user(self.bob)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        self.assertEqual(self.client.delete(f"/api/payment-methods/{pm}").status_code, 404)

    def test_saving_the_same_card_twice_vaults_once(self) -> None:
        first = self.save()
        second = self.save()
        self.assertEqual(first, second)
        self.assertEqual(len(self.transport.calls("POST", r"/v3/vault/payment-tokens")), 1)

    def test_saved_card_pays_by_vault_id_and_is_not_usable_by_others(self) -> None:
        pm = self.save()
        order_id = self.place_order()
        self.transport.on("POST", r"/v2/checkout/orders", json_response(201, order_body()))
        self.transport.on("POST", r"/v2/checkout/orders/PO1/authorize", json_response(201, authorize_body()))
        self.assertEqual(self.post(f"/api/orders/{order_id}/pay", {"paymentMethodId": pm}).status_code, 200)
        sent = body_of(self.transport.calls("POST", r"/v2/checkout/orders")[0])
        self.assertEqual(sent["payment_source"], {"card": {"vault_id": "TOK1"}})

        self.as_user(self.bob)
        bob_order = self.client.post("/api/orders", json.dumps({"items": [{"productId": self.product.pk}]}),
                                     content_type="application/json").json()["orderId"]
        r = self.post(f"/api/orders/{bob_order}/pay", {"paymentMethodId": pm})
        self.assertEqual(r.status_code, 404)

    def test_deleted_card_is_gone_and_unusable(self) -> None:
        pm = self.save()
        self.transport.on("DELETE", r"/v3/vault/payment-tokens/TOK1", StubResponse(204))
        self.assertEqual(self.client.delete(f"/api/payment-methods/{pm}").status_code, 200)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        order_id = self.place_order()
        self.assertEqual(self.post(f"/api/orders/{order_id}/pay", {"paymentMethodId": pm}).status_code, 404)

    def test_delete_with_lost_answer_hides_card_and_resends_same_id(self) -> None:
        pm = self.save()
        self.transport.on("DELETE", r"/v3/vault/payment-tokens/TOK1", httpx.ReadTimeout("x"), StubResponse(204))
        self.assertEqual(self.client.delete(f"/api/payment-methods/{pm}").status_code, 504)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        self.assertEqual(self.client.delete(f"/api/payment-methods/{pm}").status_code, 200)
        self.assertEqual(len(self.transport.calls("DELETE", r"/v3/vault/payment-tokens/TOK1")), 2)


# --- reconciliation ----------------------------------------------------------------------------------

class ReconciliationTests(PayPalTestCase):
    def test_covers_every_page_and_lines_up_both_sides(self) -> None:
        self.captured_order()
        ProviderWrite.objects.filter(kind="capture").update(provider_time=timezone.now() - timedelta(days=2))
        when = (timezone.now() - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def txn(txn_id: str) -> dict[str, Any]:
            return {"transaction_info": {"transaction_id": txn_id, "transaction_initiation_date": when,
                                         "transaction_amount": money("30.00")}}

        def search(request: HttpRequest) -> StubResponse:
            page = int(httpx.URL(request.url).params.get("page", "1"))
            details = [txn("CAP1")] if page == 1 else [txn("STRANGER")]
            return json_response(200, {"transaction_details": details, "page": page, "total_pages": 2,
                                       "last_refreshed_datetime": when})

        self.transport.on("GET", r"/v1/reporting/transactions", search)
        self.as_user(self.staff)
        start = (timezone.now() - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = timezone.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        r = self.client.get("/api/reconciliation", {"from": start, "to": end})
        self.assertEqual(r.status_code, 200, r.content)
        report = r.json()
        self.assertEqual(report["summary"]["matched"], 1)
        self.assertEqual([p["transactionId"] for p in report["paypalOnly"]], ["STRANGER"])
        self.assertEqual(report["localOnly"], [])
        searches = self.transport.calls("GET", r"/v1/reporting/transactions")
        self.assertEqual(len(searches), 4)  # 45 days = two windows (31-day limit) x two pages
