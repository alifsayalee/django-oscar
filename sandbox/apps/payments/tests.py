"""
Tests for the PayPal API app. PayPal is replaced at the SDK's transport seam
(``custom_http_client``), so the real SDK builds and decodes every request.

Run from ``sandbox/``:  python manage.py test apps.payments
"""

import json
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from oscar.core.loading import get_model
from oscar.test.factories import create_product
from paypal import PaypalClient
from paypal.core import HttpRequest, HttpResponse, OAuthToken

from . import gateway
from .models import PayPalPayment, PayPalRefund, PayPalTransaction, SavedCard

Order = get_model("order", "Order")

CARD = {"number": "4111111111111111", "expiry": "2030-12", "securityCode": "123", "name": "T Shopper"}
PASSWORD = "Sandbox-pass-2026"


def json_response(status: int, body: Any) -> HttpResponse:
    return HttpResponse(status_code=status, headers={"content-type": "application/json"},
                        content=json.dumps(body).encode())


def sent_json(request: HttpRequest) -> Any:
    """The JSON payload the SDK serialized for ``request``."""
    body: Any = request.body
    return body.value


def error_body(issue: str, name: str = "UNPROCESSABLE_ENTITY") -> dict[str, Any]:
    return {"name": name, "message": "failed", "debug_id": "dbg1", "details": [{"issue": issue}]}


class StubTransport:
    """Routes each request by (method, path suffix) to a queue of handlers."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[Any]] = {}
        self.requests: list[HttpRequest] = []

    def on(self, method: str, suffix: str, *answers: Any) -> None:
        self.routes.setdefault((method, suffix), []).extend(answers)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = httpx.URL(request.url).path
        for (method, suffix), answers in self.routes.items():
            if request.method == method and path.endswith(suffix) and answers:
                answer = answers.pop(0) if len(answers) > 1 else answers[0]
                if isinstance(answer, Exception):
                    raise answer
                result: HttpResponse = answer(request) if callable(answer) else answer
                return result
        raise AssertionError("unexpected PayPal call %s %s" % (request.method, path))

    def close(self) -> None:
        pass

    def calls(self, method: str, suffix: str) -> list[HttpRequest]:
        return [r for r in self.requests
                if r.method == method and httpx.URL(r.url).path.endswith(suffix)]


class StubTokenSource:
    def fetch(self, credentials: Any) -> OAuthToken:
        return OAuthToken(access_token="test-token", token_type="Bearer")


def order_answer(amount: str, *, auth_status: str = "CREATED", order_status: str = "COMPLETED",
                 auth_id: str = "AUTH1", currency: str = "USD") -> dict[str, Any]:
    now = timezone.now()
    return {
        "id": "PPORDER1",
        "status": order_status,
        "payment_source": {"card": {"brand": "VISA", "last_digits": "1111"}},
        "purchase_units": [{"payments": {"authorizations": [{
            "id": auth_id, "status": auth_status, "amount": {"currency_code": currency, "value": amount},
            "create_time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expiration_time": (now + timedelta(days=29)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }]}}],
    }


def capture_answer(amount: str, *, status: str = "COMPLETED", capture_id: str = "CAP1") -> dict[str, Any]:
    return {
        "id": capture_id, "status": status, "amount": {"currency_code": "USD", "value": amount},
        "create_time": timezone.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seller_receivable_breakdown": {
            "gross_amount": {"currency_code": "USD", "value": amount},
            "paypal_fee": {"currency_code": "USD", "value": "0.99"},
            "net_amount": {"currency_code": "USD", "value": str(Decimal(amount) - Decimal("0.99"))},
        },
    }


def auth_answer(*, status: str = "CREATED", age: timedelta = timedelta(0), auth_id: str = "AUTH1",
                amount: str = "30.00") -> dict[str, Any]:
    created = timezone.now() - age
    return {"id": auth_id, "status": status, "amount": {"currency_code": "USD", "value": amount},
            "create_time": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expiration_time": (created + timedelta(days=29)).strftime("%Y-%m-%dT%H:%M:%SZ")}


@override_settings(PAYPAL_CLIENT_ID="test-id", PAYPAL_CLIENT_SECRET="test-secret", PAYPAL_CURRENCY="USD",
                   PAYPAL_ENVIRONMENT="sandbox", PAYPAL_BASE_URL="")
class ApiTestCase(TestCase):
    def setUp(self) -> None:
        self.transport = StubTransport()
        client = PaypalClient(base_url="https://paypal.test", custom_http_client=self.transport,
                              oauth2={"client_id": "test-id", "client_secret": "test-secret"},
                              oauth2_token_source=StubTokenSource())
        old = gateway.set_client(client)
        self.addCleanup(gateway.set_client, old)
        User = get_user_model()
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", PASSWORD)
        self.other = User.objects.create_user("other", "other@example.com", PASSWORD)
        self.staff = User.objects.create_user("operator", "operator@example.com", PASSWORD, is_staff=True)
        self.product = create_product(price=Decimal("15.00"), num_in_stock=100)

    def as_user(self, user: Any) -> None:
        self.client.force_login(user)

    def post(self, path: str, body: Any = None, **headers: str) -> Any:
        return self.client.post(path, data=json.dumps(body or {}), content_type="application/json",
                                headers=headers)

    def place_order(self, quantity: int = 2) -> str:
        self.as_user(self.shopper)
        r = self.post("/api/orders", {"items": [{"itemId": self.product.pk, "quantity": quantity}]})
        self.assertEqual(r.status_code, 201, r.content)
        return str(r.json()["orderId"])

    def authorized_order(self) -> str:
        number = self.place_order()
        self.transport.on("POST", "/v2/checkout/orders", json_response(201, order_answer("30.00")))
        r = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(r.status_code, 200, r.content)
        return number

    def captured_order(self) -> str:
        number = self.authorized_order()
        self.transport.on("GET", "/v2/payments/authorizations/AUTH1", json_response(200, auth_answer()))
        self.transport.on("POST", "/authorizations/AUTH1/capture", json_response(201, capture_answer("30.00")))
        self.as_user(self.staff)
        r = self.post("/api/orders/%s/fulfil" % number)
        self.assertEqual(r.status_code, 200, r.content)
        self.as_user(self.shopper)
        return number

    def payment(self, number: str) -> PayPalPayment:
        return PayPalPayment.objects.get(order__number=number)


class OrderAndAccessTests(ApiTestCase):
    def test_order_is_created_awaiting_payment_with_catalogue_total(self) -> None:
        number = self.place_order(quantity=2)
        order = Order.objects.get(number=number)
        self.assertEqual(order.status, "Pending")
        self.assertEqual(order.lines.get().quantity, 2)
        payment = self.payment(number)
        self.assertEqual((payment.state, payment.amount, payment.currency),
                         (PayPalPayment.AWAITING_PAYMENT, Decimal("30.00"), "USD"))

    def test_anonymous_is_401_and_shopper_cannot_run_operator_actions(self) -> None:
        self.assertEqual(self.client.get("/api/my-orders").status_code, 401)
        number = self.place_order()
        self.assertEqual(self.post("/api/orders/%s/fulfil" % number).status_code, 403)
        self.assertEqual(self.post("/api/orders/%s/cancel" % number).status_code, 403)
        self.assertEqual(self.client.get("/api/reconciliation?from=2026-01-01T00:00:00Z&to=2026-01-02T00:00:00Z")
                         .status_code, 403)

    def test_another_shoppers_order_is_not_found(self) -> None:
        number = self.place_order()
        self.as_user(self.other)
        self.assertEqual(self.post("/api/orders/%s/pay" % number, {"card": CARD}).status_code, 404)
        self.assertEqual(self.post("/api/orders/%s/refunds" % number, {}, **{"Idempotency-Key": "k"}).status_code,
                         404)
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"], [])
        self.assertEqual(self.transport.requests, [])

    def test_unknown_item_is_rejected(self) -> None:
        self.as_user(self.shopper)
        r = self.post("/api/orders", {"items": [{"itemId": 999999, "quantity": 1}]})
        self.assertEqual(r.status_code, 422)


class PayTests(ApiTestCase):
    def test_authorizes_the_order_total_and_never_sends_card_data_back(self) -> None:
        number = self.authorized_order()
        (sent,) = self.transport.calls("POST", "/v2/checkout/orders")
        body = sent_json(sent)
        self.assertEqual(body["intent"], "AUTHORIZE")
        self.assertEqual(body["purchase_units"][0]["amount"], {"currency_code": "USD", "value": "30.00"})
        self.assertEqual(sent.headers["prefer"], "return=representation")
        self.assertTrue(sent.headers["paypal-request-id"].endswith("-auth-1"))
        payment = self.payment(number)
        self.assertEqual(payment.state, PayPalPayment.AUTHORIZED)
        self.assertEqual(Order.objects.get(number=number).status, "Being processed")
        self.assertTrue(PayPalTransaction.objects.filter(paypal_id="AUTH1", kind="authorization").exists())
        listing = self.client.get("/api/my-orders").content.decode()
        self.assertNotIn("4111111111111111", listing)

    def test_double_pay_does_not_authorize_twice(self) -> None:
        number = self.authorized_order()
        r = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(self.transport.calls("POST", "/v2/checkout/orders")), 1)

    def test_amount_mismatch_is_needs_review_not_authorized(self) -> None:
        number = self.place_order()
        self.transport.on("POST", "/v2/checkout/orders", json_response(201, order_answer("29.00")))
        r = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.payment(number).state, PayPalPayment.NEEDS_REVIEW)
        self.assertEqual(Order.objects.get(number=number).status, "Pending")

    def test_denied_authorization_is_a_decline_and_a_retry_is_a_new_attempt(self) -> None:
        number = self.place_order()
        self.transport.on("POST", "/v2/checkout/orders",
                          json_response(201, order_answer("30.00", auth_status="DENIED")),
                          json_response(201, order_answer("30.00", auth_id="AUTH2")))
        r = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "card_declined"))
        self.assertEqual(self.payment(number).state, PayPalPayment.AUTHORIZATION_FAILED)
        r = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(r.status_code, 200)
        ids = [c.headers["paypal-request-id"] for c in self.transport.calls("POST", "/v2/checkout/orders")]
        self.assertEqual([i.rsplit("-", 1)[1] for i in ids], ["1", "2"])

    def test_payer_action_required_is_reported_not_followed(self) -> None:
        number = self.place_order()
        answer = order_answer("30.00", order_status="PAYER_ACTION_REQUIRED")
        answer.pop("purchase_units")
        self.transport.on("POST", "/v2/checkout/orders", json_response(200, answer))
        r = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(r.json()["error"]["code"], "payer_action_required")

    def test_read_timeout_is_unknown_and_retry_reuses_the_request_id(self) -> None:
        number = self.place_order()
        self.transport.on("POST", "/v2/checkout/orders", httpx.ReadTimeout("no reply"),
                          json_response(200, order_answer("30.00")))
        r = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(r.status_code, 504)
        self.assertTrue(r.json()["error"]["outcomeUnknown"])
        self.assertEqual(self.payment(number).state, PayPalPayment.AUTHORIZATION_UNKNOWN)
        r = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(r.status_code, 200)
        first, second = self.transport.calls("POST", "/v2/checkout/orders")
        self.assertEqual(first.headers["paypal-request-id"], second.headers["paypal-request-id"])

    def test_refused_connection_is_known_and_releases_the_claim(self) -> None:
        number = self.place_order()
        self.transport.on("POST", "/v2/checkout/orders", httpx.ConnectError("refused"))
        r = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(r.status_code, 502)
        self.assertFalse(r.json()["error"]["outcomeUnknown"])
        self.assertEqual(self.payment(number).state, PayPalPayment.AWAITING_PAYMENT)

    def test_invalid_card_never_reaches_paypal(self) -> None:
        number = self.place_order()
        r = self.post("/api/orders/%s/pay" % number, {"card": {**CARD, "number": "4111111111111112"}})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.transport.requests, [])


class FulfilCancelTests(ApiTestCase):
    def test_fulfil_captures_and_records_fee_and_net(self) -> None:
        number = self.captured_order()
        payment = self.payment(number)
        self.assertEqual((payment.state, payment.captured_amount, payment.paypal_fee, payment.net_amount),
                         (PayPalPayment.CAPTURED, Decimal("30.00"), Decimal("0.99"), Decimal("29.01")))
        self.assertEqual(Order.objects.get(number=number).status, "Complete")
        (capture,) = self.transport.calls("POST", "/capture")
        self.assertEqual(sent_json(capture)["amount"]["value"], "30.00")

    def test_stale_hold_is_renewed_before_capture(self) -> None:
        number = self.authorized_order()
        self.transport.on("GET", "/v2/payments/authorizations/AUTH1",
                          json_response(200, auth_answer(age=timedelta(days=5))))
        self.transport.on("POST", "/authorizations/AUTH1/reauthorize",
                          json_response(201, auth_answer(auth_id="AUTH9")))
        self.transport.on("POST", "/authorizations/AUTH9/capture", json_response(201, capture_answer("30.00")))
        self.as_user(self.staff)
        r = self.post("/api/orders/%s/fulfil" % number)
        self.assertEqual(r.status_code, 200, r.content)
        payment = self.payment(number)
        self.assertEqual((payment.authorization_id, payment.reauthorized, payment.state),
                         ("AUTH9", True, PayPalPayment.CAPTURED))

    def test_hold_that_cannot_be_renewed_says_what_to_do(self) -> None:
        number = self.authorized_order()
        self.transport.on("GET", "/v2/payments/authorizations/AUTH1", json_response(200, auth_answer()))
        self.transport.on("POST", "/authorizations/AUTH1/capture",
                          json_response(422, error_body("AUTHORIZATION_EXPIRED")))
        self.transport.on("POST", "/authorizations/AUTH1/reauthorize",
                          json_response(422, error_body("REAUTHORIZATION_TOO_SOON")))
        self.as_user(self.staff)
        r = self.post("/api/orders/%s/fulfil" % number)
        self.assertEqual(r.status_code, 409)
        error = r.json()["error"]
        self.assertEqual(error["code"], "authorization_expired")
        self.assertIn("pay again", error["message"])
        self.assertEqual(self.payment(number).state, PayPalPayment.AUTHORIZATION_EXPIRED)

    def test_capture_timeout_is_unknown_and_refulfil_reuses_request_id(self) -> None:
        number = self.authorized_order()
        self.transport.on("GET", "/v2/payments/authorizations/AUTH1", json_response(200, auth_answer()))
        self.transport.on("POST", "/authorizations/AUTH1/capture", httpx.ReadTimeout("slow"),
                          json_response(200, capture_answer("30.00")))
        self.as_user(self.staff)
        self.assertEqual(self.post("/api/orders/%s/fulfil" % number).status_code, 504)
        self.assertEqual(self.payment(number).state, PayPalPayment.CAPTURE_UNKNOWN)
        self.assertEqual(self.post("/api/orders/%s/fulfil" % number).status_code, 200)
        first, second = self.transport.calls("POST", "/capture")
        self.assertEqual(first.headers["paypal-request-id"], second.headers["paypal-request-id"])

    def test_cancel_releases_the_hold(self) -> None:
        number = self.authorized_order()
        self.transport.on("POST", "/authorizations/AUTH1/void",
                          json_response(200, {**auth_answer(status="VOIDED")}))
        self.as_user(self.staff)
        r = self.post("/api/orders/%s/cancel" % number)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.payment(number).state, PayPalPayment.VOIDED)
        self.assertEqual(Order.objects.get(number=number).status, "Cancelled")
        self.assertEqual(self.post("/api/orders/%s/fulfil" % number).status_code, 409)

    def test_cancel_after_fulfilment_is_refused(self) -> None:
        number = self.captured_order()
        self.as_user(self.staff)
        self.assertEqual(self.post("/api/orders/%s/cancel" % number).status_code, 409)


class RefundTests(ApiTestCase):
    def refund_answer(self, amount: str, refund_id: str) -> Callable[[HttpRequest], HttpResponse]:
        return lambda _req: json_response(201, {"id": refund_id, "status": "COMPLETED",
                                                "amount": {"currency_code": "USD", "value": amount}})

    def test_partial_refunds_keys_and_cap(self) -> None:
        number = self.captured_order()
        self.transport.on("POST", "/captures/CAP1/refund",
                          self.refund_answer("10.00", "R1"), self.refund_answer("20.00", "R2"))
        url = "/api/orders/%s/refunds" % number
        r1 = self.post(url, {"amount": "10.00"}, **{"Idempotency-Key": "k1"})
        self.assertEqual(r1.status_code, 201, r1.content)
        self.assertTrue(r1.json()["refundId"])
        again = self.post(url, {"amount": "10.00"}, **{"Idempotency-Key": "k1"})
        self.assertEqual((again.status_code, again.json()["refundId"]), (200, r1.json()["refundId"]))
        self.assertEqual(self.post(url, {"amount": "11.00"}, **{"Idempotency-Key": "k1"}).status_code, 409)
        over = self.post(url, {"amount": "20.01"}, **{"Idempotency-Key": "k2"})
        self.assertEqual((over.status_code, over.json()["error"]["code"]), (409, "refund_exceeds_capture"))
        rest = self.post(url, {}, **{"Idempotency-Key": "k3"})
        self.assertEqual(rest.status_code, 201, rest.content)
        payment = self.payment(number)
        self.assertEqual((payment.state, payment.refunded_amount, payment.refund_reserved),
                         (PayPalPayment.REFUNDED, Decimal("30.00"), Decimal("30.00")))
        self.assertEqual(len(self.transport.calls("POST", "/refund")), 2)

    def test_rejected_refund_releases_its_reservation(self) -> None:
        number = self.captured_order()
        self.transport.on("POST", "/captures/CAP1/refund", json_response(422, error_body("REFUND_NOT_ALLOWED")))
        r = self.post("/api/orders/%s/refunds" % number, {"amount": "5.00"}, **{"Idempotency-Key": "k"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.payment(number).refund_reserved, Decimal("0"))
        self.assertEqual(PayPalRefund.objects.get().status, PayPalRefund.FAILED)

    def test_refund_requires_a_key_and_a_capture(self) -> None:
        number = self.authorized_order()
        self.assertEqual(self.post("/api/orders/%s/refunds" % number, {"amount": "1.00"}).status_code, 422)
        r = self.post("/api/orders/%s/refunds" % number, {"amount": "1.00"}, **{"Idempotency-Key": "k"})
        self.assertEqual(r.status_code, 409)


class SavedCardTests(ApiTestCase):
    def save(self) -> str:
        self.as_user(self.shopper)
        self.transport.on("POST", "/v3/vault/payment-tokens", json_response(200, {
            "id": "TOK1", "customer": {"id": "CUST1"},
            "payment_source": {"card": {"brand": "VISA", "last_digits": "1111", "expiry": "2030-12"}}}))
        r = self.post("/api/payment-methods", {"card": CARD}, **{"Idempotency-Key": "save-1"})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertNotIn("number", r.json())
        return str(r.json()["paymentMethodId"])

    def test_save_list_pay_delete(self) -> None:
        method_id = self.save()
        self.assertEqual(SavedCard.objects.get().vault_token_id, "TOK1")
        self.assertEqual([c["lastDigits"] for c in self.client.get("/api/payment-methods").json()["paymentMethods"]],
                         ["1111"])
        # Same Idempotency-Key does not vault twice
        again = self.post("/api/payment-methods", {"card": CARD}, **{"Idempotency-Key": "save-1"})
        self.assertEqual((again.status_code, again.json()["paymentMethodId"]), (200, method_id))
        self.assertEqual(len(self.transport.calls("POST", "/payment-tokens")), 1)

        number = self.place_order()
        self.transport.on("POST", "/v2/checkout/orders", json_response(201, order_answer("30.00")))
        r = self.post("/api/orders/%s/pay" % number, {"paymentMethodId": method_id})
        self.assertEqual(r.status_code, 200, r.content)
        sent = sent_json(self.transport.calls("POST", "/v2/checkout/orders")[-1])
        self.assertEqual(sent["payment_source"], {"card": {"vault_id": "TOK1"}})

        self.transport.on("DELETE", "/v3/vault/payment-tokens/TOK1", HttpResponse(status_code=204, headers={}))
        self.assertEqual(self.client.delete("/api/payment-methods/%s" % method_id).status_code, 200)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        second = self.place_order()
        r = self.post("/api/orders/%s/pay" % second, {"paymentMethodId": method_id})
        self.assertEqual(r.status_code, 404)

    def test_another_shopper_cannot_see_use_or_delete_a_card(self) -> None:
        method_id = self.save()
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        r = self.post("/api/orders", {"items": [{"itemId": self.product.pk, "quantity": 1}]})
        number = r.json()["orderId"]
        self.assertEqual(self.post("/api/orders/%s/pay" % number, {"paymentMethodId": method_id}).status_code, 404)
        self.assertEqual(self.client.delete("/api/payment-methods/%s" % method_id).status_code, 404)
        self.assertEqual(SavedCard.objects.get().status, SavedCard.ACTIVE)


class ReconciliationTests(ApiTestCase):
    def test_reads_every_page_and_lines_both_sides_up(self) -> None:
        number = self.captured_order()
        start = timezone.now() - timedelta(days=1)
        end = timezone.now() + timedelta(minutes=1)
        refreshed = (timezone.now() + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        t = timezone.now().strftime("%Y-%m-%dT%H:%M:%SZ")

        def info(txn_id: str, value: str) -> dict[str, Any]:
            return {"transaction_info": {"transaction_id": txn_id, "transaction_initiation_date": t,
                                         "transaction_updated_date": t,
                                         "transaction_amount": {"currency_code": "USD", "value": value}}}

        def page(request: HttpRequest) -> HttpResponse:
            params = httpx.URL(request.url).params
            self.assertEqual(params["balance_affecting_records_only"], "N")
            if params["page"] == "1":
                return json_response(200, {"transaction_details": [info("AUTH1", "30.00")],
                                           "total_pages": 2, "last_refreshed_datetime": refreshed})
            return json_response(200, {"transaction_details": [info("CAP1", "30.00"), info("STRANGER", "9.00")],
                                       "total_pages": 2, "last_refreshed_datetime": refreshed})

        self.transport.on("GET", "/v1/reporting/transactions", page)
        PayPalTransaction.objects.create(payment=self.payment(number), kind="refund", paypal_id="LOCALONLY",
                                         amount=Decimal("1.00"), currency="USD", provider_time=timezone.now())
        self.as_user(self.staff)
        r = self.client.get("/api/reconciliation", {"from": start.isoformat(), "to": end.isoformat()})
        self.assertEqual(r.status_code, 200, r.content)
        report = r.json()
        self.assertEqual(len(self.transport.calls("GET", "/v1/reporting/transactions")), 2)
        self.assertEqual({m["paypalId"] for m in report["matched"]}, {"AUTH1", "CAP1"})
        self.assertTrue(all(m["orderId"] == number and m["amountMatches"] for m in report["matched"]))
        self.assertEqual([p["paypalId"] for p in report["paypalOnly"]], ["STRANGER"])
        self.assertEqual([a["paypalId"] for a in report["appOnly"]], ["LOCALONLY"])
        self.assertFalse(report["truncated"])

    def test_long_range_is_split_into_31_day_queries(self) -> None:
        self.transport.on("GET", "/v1/reporting/transactions", json_response(200, {"total_pages": 1}))
        self.as_user(self.staff)
        r = self.client.get("/api/reconciliation", {"from": "2026-01-01T00:00:00Z", "to": "2026-03-15T00:00:00Z"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(len(self.transport.calls("GET", "/v1/reporting/transactions")), 3)

    def test_bad_range_is_rejected(self) -> None:
        self.as_user(self.staff)
        r = self.client.get("/api/reconciliation", {"from": "2026-03-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.client.get("/api/reconciliation", {"from": "nope", "to": "x"}).status_code, 422)


class ConfigurationTests(TestCase):
    @override_settings(PAYPAL_BASE_URL="https://paypal.example/proxy/", PAYPAL_ENVIRONMENT="live")
    def test_base_url_override_wins(self) -> None:
        self.assertEqual(gateway.config().base_url, "https://paypal.example/proxy")

    @override_settings(PAYPAL_BASE_URL="", PAYPAL_ENVIRONMENT="production-ish")
    def test_unknown_environment_is_refused(self) -> None:
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaises(ImproperlyConfigured):
            gateway.config()

    @override_settings(PAYPAL_CLIENT_ID="", PAYPAL_CLIENT_SECRET="")
    def test_missing_credentials_are_refused(self) -> None:
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaises(ImproperlyConfigured):
            gateway.build_client()

    def test_rejected_credentials_are_our_configuration_error(self) -> None:
        transport = StubTransport()
        transport.on("POST", "/v1/oauth2/token", json_response(401, {"error": "invalid_client"}))
        client = PaypalClient(base_url="https://paypal.test", custom_http_client=transport,
                              oauth2={"client_id": "x", "client_secret": "y"})
        with self.assertRaises(gateway.ProviderError) as ctx:
            gateway.call("lookup", lambda: client.payments.get_captured_payment("CAP1"))
        self.assertEqual((ctx.exception.status_code, ctx.exception.outcome_unknown), (502, False))
