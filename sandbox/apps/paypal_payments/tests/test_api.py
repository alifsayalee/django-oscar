import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from oscar.test.factories import create_product

from apps.paypal_payments import gateway
from apps.paypal_payments.models import PaymentState, PayPalPayment, SavedCard

from . import fakes
from .fakes import json_response

CARD = {
    "number": "4111 1111 1111 1111",
    "expiry": "2030-12",
    "securityCode": "123",
    "name": "Alice Shopper",
    "billingAddress": {
        "addressLine1": "1 Main St",
        "adminArea2": "San Jose",
        "adminArea1": "CA",
        "postalCode": "95131",
        "countryCode": "US",
    },
}
PAN = "4111111111111111"

PAYPAL = dict(
    PAYPAL_CLIENT_ID="test-client-id",
    PAYPAL_CLIENT_SECRET="test-client-secret",
    PAYPAL_ENVIRONMENT="sandbox",
    PAYPAL_CURRENCY="USD",
    PAYPAL_BASE_URL="",
)


@override_settings(**PAYPAL)
class ApiTestCase(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(
            "alice", "alice@example.com", "pw-alice-123"
        )
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw-bob-123")
        self.staff = User.objects.create_user(
            "ops", "ops@example.com", "pw-ops-123", is_staff=True
        )
        self.product = create_product(price=Decimal("15.00"), num_in_stock=10)
        self.paypal = fakes.install(fakes.FakePayPal())
        self.addCleanup(fakes.uninstall)
        self.a, self.b, self.ops = Client(), Client(), Client()
        self.a.force_login(self.alice)
        self.b.force_login(self.bob)
        self.ops.force_login(self.staff)

    # -- helpers
    def post(self, client, url, body=None, **headers):
        return client.post(
            url,
            data=json.dumps(body or {}),
            content_type="application/json",
            headers=headers,
        )

    def place(self, client=None, quantity=1):
        r = self.post(
            client or self.a,
            "/api/orders",
            {"lines": [{"productId": self.product.pk, "quantity": quantity}]},
        )
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["orderId"]

    def authorized_order(self, amount="15.00", quantity=1):
        number = self.place(quantity=quantity)
        self.paypal.on(
            "POST",
            "/v2/checkout/orders",
            json_response(201, fakes.order_completed(amount=amount)),
        )
        r = self.post(self.a, "/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(r.status_code, 200, r.content)
        return number

    def captured_order(self, amount="15.00", quantity=1):
        number = self.authorized_order(amount, quantity)
        self.paypal.on(
            "GET",
            "/v2/payments/authorizations/",
            json_response(200, fakes.authorization(amount=amount)),
        )
        self.paypal.on(
            "POST",
            "/v2/payments/authorizations/",
            json_response(201, fakes.capture(amount=amount)),
        )
        r = self.post(self.ops, "/api/orders/%s/fulfil" % number)
        self.assertEqual(r.status_code, 200, r.content)
        return number

    def payment(self, number):
        return PayPalPayment.objects.get(order__number=number)


class OrderTests(ApiTestCase):
    def test_place_order_uses_catalogue_price_and_configured_currency(self):
        r = self.post(
            self.a,
            "/api/orders",
            {"lines": [{"productId": self.product.pk, "quantity": 2}]},
        )
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertIn("orderId", body)
        self.assertEqual(body["status"], "Awaiting payment")
        self.assertEqual(body["total"], "30.00")
        self.assertEqual(body["currency"], "USD")
        self.assertEqual(body["payment"]["state"], "awaiting_payment")
        self.assertEqual(self.paypal.calls(), [])

    def test_place_order_validation(self):
        self.assertEqual(
            self.post(self.a, "/api/orders", {"lines": []}).status_code, 400
        )
        self.assertEqual(
            self.post(
                self.a, "/api/orders", {"lines": [{"productId": 999999}]}
            ).status_code,
            404,
        )
        r = self.post(
            self.a,
            "/api/orders",
            {"lines": [{"productId": self.product.pk, "quantity": 50}]},
        )
        self.assertEqual(r.status_code, 422)  # only 10 in stock

    def test_requires_login(self):
        r = Client().post("/api/orders", data="{}", content_type="application/json")
        self.assertEqual(r.status_code, 401)

    def test_csrf_is_enforced_for_session_callers(self):
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.alice)
        r = c.post(
            "/api/orders",
            data=json.dumps({"lines": [{"productId": self.product.pk}]}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403)
        token = c.get("/api/csrf").json()["csrfToken"]
        r = c.post(
            "/api/orders",
            data=json.dumps({"lines": [{"productId": self.product.pk}]}),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(r.status_code, 201)

    def test_session_login(self):
        c = Client()
        r = self.post(c, "/api/session", {"username": "alice", "password": "wrong"})
        self.assertEqual(r.status_code, 401)
        r = self.post(
            c, "/api/session", {"username": "alice", "password": "pw-alice-123"}
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["isStaff"])

    def test_orders_are_private(self):
        number = self.place()
        self.assertEqual(self.b.get("/api/orders/%s" % number).status_code, 404)
        self.assertEqual(
            self.post(
                self.b, "/api/orders/%s/pay" % number, {"card": CARD}
            ).status_code,
            404,
        )
        self.assertEqual(self.b.get("/api/my-orders").json()["orders"], [])
        self.assertEqual(len(self.a.get("/api/my-orders").json()["orders"]), 1)

    def test_operator_actions_require_staff(self):
        number = self.place()
        for action in ("fulfil", "cancel"):
            self.assertEqual(
                self.post(self.a, "/api/orders/%s/%s" % (number, action)).status_code,
                403,
            )
        self.assertEqual(
            self.a.get(
                "/api/reconciliation?from=2026-01-01T00:00:00Z&to=2026-01-02T00:00:00Z"
            ).status_code,
            403,
        )


class PayTests(ApiTestCase):
    def test_authorizes_order_total_with_card(self):
        number = self.place(quantity=2)
        self.paypal.on(
            "POST",
            "/v2/checkout/orders",
            json_response(201, fakes.order_completed(amount="30.00")),
        )
        with self.assertLogs("apps.paypal_payments", level="INFO") as logs:
            r = self.post(self.a, "/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["status"], "Payment authorized")
        self.assertEqual(body["payment"]["state"], "authorized")
        self.assertEqual(body["payment"]["authorization"]["id"], "AUTH1")
        self.assertEqual(
            body["payment"]["card"],
            {"brand": "VISA", "lastDigits": "1111", "paymentMethodId": None},
        )

        (req,) = self.paypal.calls("POST", "/v2/checkout/orders")
        sent = fakes.FakePayPal.body(req)
        self.assertEqual(sent["intent"], "AUTHORIZE")
        unit = sent["purchase_units"][0]
        self.assertEqual(unit["amount"], {"currency_code": "USD", "value": "30.00"})
        self.assertTrue(unit["custom_id"].startswith(number + "-"))
        self.assertEqual(sent["payment_source"]["card"]["number"], PAN)
        self.assertEqual(
            sent["payment_source"]["card"]["billing_address"]["country_code"], "US"
        )
        self.assertTrue(req.headers["paypal-request-id"].startswith("oscar-"))
        self.assertEqual(req.headers["prefer"], "return=representation")
        self.assertEqual(req.headers["authorization"], "Bearer test-token")
        # The card number never reaches our logs or database.
        self.assertNotIn(PAN, "\n".join(logs.output))
        payment = self.payment(number)
        self.assertNotIn(
            PAN,
            json.dumps(
                {f.name: str(getattr(payment, f.attname)) for f in payment._meta.fields}
            ),
        )
        # Oscar bookkeeping
        self.assertEqual(payment.source.amount_allocated, Decimal("30.00"))
        self.assertEqual(
            payment.order.payment_events.get().event_type.name, "Authorised"
        )

    def test_double_click_does_not_authorize_twice(self):
        number = self.authorized_order()
        r = self.post(self.a, "/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.paypal.calls("POST", "/v2/checkout/orders")), 1)

    def test_in_flight_claim_blocks_concurrent_pay(self):
        number = self.place()
        PayPalPayment.objects.filter(order__number=number).update(
            state=PaymentState.AUTHORIZING
        )
        r = self.post(self.a, "/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.paypal.calls(), [])

    def test_declined_authorization_is_not_success(self):
        number = self.place()
        self.paypal.on(
            "POST",
            "/v2/checkout/orders",
            json_response(201, fakes.order_completed(auth_status="DENIED")),
        )
        r = self.post(self.a, "/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(r.status_code, 402)
        self.assertEqual(self.payment(number).state, PaymentState.PAYMENT_FAILED)
        # A new attempt after a definitive failure uses a new request id.
        self.paypal.on(
            "POST", "/v2/checkout/orders", json_response(201, fakes.order_completed())
        )
        self.assertEqual(
            self.post(
                self.a, "/api/orders/%s/pay" % number, {"card": CARD}
            ).status_code,
            200,
        )
        first, second = self.paypal.calls("POST", "/v2/checkout/orders")
        self.assertNotEqual(
            first.headers["paypal-request-id"], second.headers["paypal-request-id"]
        )

    def test_unlisted_status_is_unknown_not_authorized(self):
        number = self.place()
        self.paypal.on(
            "POST",
            "/v2/checkout/orders",
            json_response(201, fakes.order_completed(auth_status="SOMETHING_NEW")),
        )
        r = self.post(self.a, "/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(self.payment(number).state, PaymentState.NEEDS_REVIEW)
        self.assertEqual(self.payment(number).order.status, "Awaiting payment")

    def test_amount_mismatch_needs_review(self):
        number = self.place()
        self.paypal.on(
            "POST",
            "/v2/checkout/orders",
            json_response(201, fakes.order_completed(amount="14.99")),
        )
        r = self.post(self.a, "/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(self.payment(number).state, PaymentState.NEEDS_REVIEW)

    def test_provider_422_is_a_decline(self):
        number = self.place()
        self.paypal.on(
            "POST",
            "/v2/checkout/orders",
            json_response(422, fakes.paypal_error(issue="INSTRUMENT_DECLINED")),
        )
        r = self.post(self.a, "/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(r.status_code, 402)
        self.assertIn(
            "INSTRUMENT_DECLINED: desc of INSTRUMENT_DECLINED", r.json()["paypalIssues"]
        )
        self.assertEqual(self.payment(number).state, PaymentState.PAYMENT_FAILED)

    def test_payer_action_required_is_refused(self):
        number = self.place()
        body = fakes.order_completed(order_status="PAYER_ACTION_REQUIRED")
        body["purchase_units"][0]["payments"] = {}
        self.paypal.on("POST", "/v2/checkout/orders", json_response(200, body))
        r = self.post(self.a, "/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(r.status_code, 422)
        self.assertIn("browser", r.json()["error"])

    def test_created_order_is_then_authorized(self):
        number = self.place()
        created = {"id": "PPORDER1", "status": "APPROVED"}
        self.paypal.on(
            "POST",
            "/v2/checkout/orders/PPORDER1/authorize",
            json_response(201, fakes.order_completed()),
        )
        self.paypal.on("POST", "/v2/checkout/orders", json_response(201, created))
        r = self.post(self.a, "/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(
            len(self.paypal.calls("POST", "/v2/checkout/orders/PPORDER1/authorize")), 1
        )

    def test_read_timeout_is_unknown_and_retry_reuses_request_id(self):
        number = self.place()
        self.paypal.on("POST", "/v2/checkout/orders", httpx.ReadTimeout("no reply"))
        r = self.post(self.a, "/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(r.status_code, 504)
        self.assertTrue(r.json()["outcomeUnknown"])
        self.assertEqual(self.payment(number).state, PaymentState.AUTHORIZATION_UNKNOWN)
        self.paypal.on(
            "POST", "/v2/checkout/orders", json_response(200, fakes.order_completed())
        )
        self.assertEqual(
            self.post(
                self.a, "/api/orders/%s/pay" % number, {"card": CARD}
            ).status_code,
            200,
        )
        first, second = self.paypal.calls("POST", "/v2/checkout/orders")
        self.assertEqual(
            first.headers["paypal-request-id"], second.headers["paypal-request-id"]
        )

    def test_refused_connection_is_known_and_restores_state(self):
        number = self.place()
        self.paypal.on("POST", "/v2/checkout/orders", httpx.ConnectError("refused"))
        r = self.post(self.a, "/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(r.status_code, 502)
        self.assertFalse(r.json()["outcomeUnknown"])
        payment = self.payment(number)
        self.assertEqual(
            (payment.state, payment.attempt), (PaymentState.AWAITING_PAYMENT, 0)
        )

    def test_bad_credentials_are_a_configuration_error(self):
        number = self.place()
        self.paypal.token = json_response(
            401, {"error": "invalid_client", "error_description": "bad"}
        )
        r = self.post(self.a, "/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(r.status_code, 502)
        self.assertIn("credentials", r.json()["error"])
        self.assertEqual(self.payment(number).state, PaymentState.AWAITING_PAYMENT)

    def test_invalid_card_is_rejected_without_calling_paypal(self):
        number = self.place()
        bad = dict(CARD, number="4111111111111112")
        r = self.post(self.a, "/api/orders/%s/pay" % number, {"card": bad})
        self.assertEqual(r.status_code, 422)
        self.assertNotIn("4111111111111112", r.content.decode())
        self.assertEqual(self.paypal.calls(), [])


class FulfilCancelTests(ApiTestCase):
    def test_fulfil_captures_and_records_fee_and_net(self):
        number = self.authorized_order()
        self.paypal.on(
            "GET",
            "/v2/payments/authorizations/AUTH1",
            json_response(200, fakes.authorization()),
        )
        self.paypal.on(
            "POST",
            "/v2/payments/authorizations/AUTH1/capture",
            json_response(201, fakes.capture()),
        )
        r = self.post(self.ops, "/api/orders/%s/fulfil" % number)
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["status"], "Complete")
        self.assertEqual(body["payment"]["capture"]["amount"], "15.00")
        self.assertEqual(body["payment"]["capture"]["paypalFee"], "0.74")
        self.assertEqual(body["payment"]["capture"]["netAmount"], "14.26")
        (req,) = self.paypal.calls("POST", "/v2/payments/authorizations/AUTH1/capture")
        self.assertEqual(
            fakes.FakePayPal.body(req)["amount"],
            {"currency_code": "USD", "value": "15.00"},
        )
        # Fulfilling again does not capture again.
        self.assertEqual(
            self.post(self.ops, "/api/orders/%s/fulfil" % number).status_code, 200
        )
        self.assertEqual(
            len(self.paypal.calls("POST", "/v2/payments/authorizations/AUTH1/capture")),
            1,
        )
        self.assertEqual(self.payment(number).source.amount_debited, Decimal("15.00"))

    def test_stale_authorization_is_renewed_before_capture(self):
        number = self.authorized_order()
        PayPalPayment.objects.filter(order__number=number).update(
            authorized_at=datetime.now(timezone.utc) - timedelta(days=5),
            original_authorized_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        self.paypal.on(
            "GET",
            "/v2/payments/authorizations/AUTH1",
            json_response(200, fakes.authorization(create_time=fakes.ts(5))),
        )
        self.paypal.on(
            "POST",
            "/v2/payments/authorizations/AUTH1/reauthorize",
            json_response(201, fakes.authorization(auth_id="AUTH2")),
        )
        self.paypal.on(
            "POST",
            "/v2/payments/authorizations/AUTH2/capture",
            json_response(201, fakes.capture()),
        )
        r = self.post(self.ops, "/api/orders/%s/fulfil" % number)
        self.assertEqual(r.status_code, 200, r.content)
        payment = r.json()["payment"]
        self.assertEqual(payment["authorization"]["id"], "AUTH2")
        self.assertEqual(payment["authorization"]["previousIds"], ["AUTH1"])
        self.assertEqual(payment["state"], "captured")
        (req,) = self.paypal.calls(
            "POST", "/v2/payments/authorizations/AUTH1/reauthorize"
        )
        self.assertEqual(
            fakes.FakePayPal.body(req),
            {"amount": {"currency_code": "USD", "value": "15.00"}},
        )

    def test_authorization_too_old_to_renew_is_explained(self):
        number = self.authorized_order()
        PayPalPayment.objects.filter(order__number=number).update(
            authorized_at=datetime.now(timezone.utc) - timedelta(days=30),
            original_authorized_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        self.paypal.on(
            "GET",
            "/v2/payments/authorizations/AUTH1",
            json_response(
                200,
                fakes.authorization(
                    create_time=fakes.ts(30), expiration_time=fakes.ts(1)
                ),
            ),
        )
        r = self.post(self.ops, "/api/orders/%s/fulfil" % number)
        self.assertEqual(r.status_code, 409)
        message = r.json()["error"]
        self.assertIn("can no longer be renewed", message)
        self.assertIn("Cancel the order", message)
        self.assertEqual(self.paypal.calls("POST", "/v2/payments/authorizations/"), [])
        self.assertEqual(self.payment(number).state, PaymentState.AUTHORIZED)

    def test_refused_renewal_is_actionable(self):
        number = self.authorized_order()
        PayPalPayment.objects.filter(order__number=number).update(
            authorized_at=datetime.now(timezone.utc) - timedelta(days=5)
        )
        self.paypal.on(
            "GET",
            "/v2/payments/authorizations/AUTH1",
            json_response(200, fakes.authorization(create_time=fakes.ts(5))),
        )
        self.paypal.on(
            "POST",
            "/v2/payments/authorizations/AUTH1/reauthorize",
            json_response(422, fakes.paypal_error(issue="REAUTHORIZATION_NOT_ALLOWED")),
        )
        r = self.post(self.ops, "/api/orders/%s/fulfil" % number)
        self.assertEqual(r.status_code, 409)
        self.assertIn("refused to renew", r.json()["error"])
        self.assertIn("ask the shopper to pay again", r.json()["error"])

    def test_capture_timeout_is_unknown_and_resent_under_same_id(self):
        number = self.authorized_order()
        self.paypal.on(
            "GET",
            "/v2/payments/authorizations/AUTH1",
            json_response(200, fakes.authorization()),
        )
        self.paypal.on(
            "POST",
            "/v2/payments/authorizations/AUTH1/capture",
            httpx.ReadTimeout("no reply"),
        )
        r = self.post(self.ops, "/api/orders/%s/fulfil" % number)
        self.assertEqual(r.status_code, 504)
        self.assertEqual(self.payment(number).state, PaymentState.CAPTURE_UNKNOWN)
        self.paypal.on(
            "POST",
            "/v2/payments/authorizations/AUTH1/capture",
            json_response(200, fakes.capture()),
        )
        self.assertEqual(
            self.post(self.ops, "/api/orders/%s/fulfil" % number).status_code, 200
        )
        first, second = self.paypal.calls(
            "POST", "/v2/payments/authorizations/AUTH1/capture"
        )
        self.assertEqual(
            first.headers["paypal-request-id"], second.headers["paypal-request-id"]
        )

    def test_fulfil_unpaid_order_is_refused(self):
        number = self.place()
        r = self.post(self.ops, "/api/orders/%s/fulfil" % number)
        self.assertEqual(r.status_code, 409)
        self.assertIn("not been paid", r.json()["error"])

    def test_cancel_releases_hold(self):
        number = self.authorized_order()
        voided = fakes.authorization(status="VOIDED")
        self.paypal.on(
            "POST", "/v2/payments/authorizations/AUTH1/void", json_response(200, voided)
        )
        r = self.post(self.ops, "/api/orders/%s/cancel" % number)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "Cancelled")
        self.assertEqual(r.json()["payment"]["state"], "voided")
        self.assertEqual(
            self.post(self.ops, "/api/orders/%s/cancel" % number).status_code, 200
        )
        self.assertEqual(
            len(self.paypal.calls("POST", "/v2/payments/authorizations/AUTH1/void")), 1
        )

    def test_cancel_unpaid_order_needs_no_paypal_call(self):
        number = self.place()
        r = self.post(self.ops, "/api/orders/%s/cancel" % number)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["payment"]["state"], "cancelled")
        self.assertEqual(self.paypal.calls(), [])

    def test_cancel_after_fulfilment_is_refused(self):
        number = self.captured_order()
        r = self.post(self.ops, "/api/orders/%s/cancel" % number)
        self.assertEqual(r.status_code, 409)
        self.assertIn("refund", r.json()["error"])


class RefundTests(ApiTestCase):
    def refund(self, number, key, amount=None, client=None):
        body = {} if amount is None else {"amount": amount}
        return self.post(
            client or self.a,
            "/api/orders/%s/refunds" % number,
            body,
            **{"Idempotency-Key": key},
        )

    def test_partial_refunds_and_ceiling(self):
        number = self.captured_order()
        self.paypal.on(
            "POST",
            "/v2/payments/captures/CAP1/refund",
            json_response(201, fakes.refund("R1", amount="5.00")),
            json_response(201, fakes.refund("R2", amount="10.00")),
        )
        r1 = self.refund(number, "k1", "5.00")
        self.assertEqual(r1.status_code, 201, r1.content)
        self.assertIn("refundId", r1.json())
        self.assertEqual(
            r1.json()["order"]["payment"]["refundState"], "partially_refunded"
        )
        # Two distinct partial refunds of the same capture are legitimate.
        r2 = self.refund(number, "k2", "10.00")
        self.assertEqual(r2.status_code, 201, r2.content)
        self.assertNotEqual(r1.json()["refundId"], r2.json()["refundId"])
        # ...but nothing beyond what was captured.
        r3 = self.refund(number, "k3", "0.01")
        self.assertEqual(r3.status_code, 409)
        self.assertEqual(
            len(self.paypal.calls("POST", "/v2/payments/captures/CAP1/refund")), 2
        )
        payment = self.payment(number)
        self.assertEqual(payment.refunded_amount, Decimal("15.00"))
        self.assertEqual(payment.source.amount_refunded, Decimal("15.00"))
        first, second = self.paypal.calls("POST", "/v2/payments/captures/CAP1/refund")
        self.assertNotEqual(
            first.headers["paypal-request-id"], second.headers["paypal-request-id"]
        )

    def test_same_key_does_not_refund_twice(self):
        number = self.captured_order()
        self.paypal.on(
            "POST",
            "/v2/payments/captures/CAP1/refund",
            json_response(201, fakes.refund(amount="5.00")),
        )
        r1 = self.refund(number, "same", "5.00")
        r2 = self.refund(number, "same", "5.00")
        self.assertEqual((r1.status_code, r2.status_code), (201, 200))
        self.assertEqual(r1.json()["refundId"], r2.json()["refundId"])
        self.assertEqual(
            len(self.paypal.calls("POST", "/v2/payments/captures/CAP1/refund")), 1
        )
        self.assertEqual(self.refund(number, "same", "6.00").status_code, 422)

    def test_full_refund_by_default(self):
        number = self.captured_order()
        self.paypal.on(
            "POST",
            "/v2/payments/captures/CAP1/refund",
            json_response(201, fakes.refund(amount="15.00")),
        )
        r = self.refund(number, "all")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["amount"], "15.00")
        self.assertEqual(r.json()["order"]["payment"]["refundState"], "refunded")

    def test_refund_requires_key_and_capture(self):
        number = self.authorized_order()
        r = self.post(self.a, "/api/orders/%s/refunds" % number, {"amount": "1.00"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.refund(number, "k", "1.00").status_code, 409)

    def test_rejected_refund_releases_reservation(self):
        number = self.captured_order()
        self.paypal.on(
            "POST",
            "/v2/payments/captures/CAP1/refund",
            json_response(422, fakes.paypal_error(issue="REFUND_NOT_ALLOWED")),
            json_response(201, fakes.refund(amount="15.00")),
        )
        self.assertEqual(self.refund(number, "a", "15.00").status_code, 422)
        self.assertEqual(self.payment(number).refund_reserved, Decimal("0"))
        self.assertEqual(self.refund(number, "b", "15.00").status_code, 201)

    def test_refund_timeout_holds_reservation_and_resends_same_id(self):
        number = self.captured_order()
        self.paypal.on(
            "POST",
            "/v2/payments/captures/CAP1/refund",
            httpx.ReadTimeout("slow"),
            json_response(201, fakes.refund(amount="15.00")),
        )
        r = self.refund(number, "t", "15.00")
        self.assertEqual(r.status_code, 504)
        self.assertEqual(
            self.refund(number, "other", "1.00").status_code, 409
        )  # still reserved
        r = self.refund(number, "t", "15.00")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["state"], "completed")
        first, second = self.paypal.calls("POST", "/v2/payments/captures/CAP1/refund")
        self.assertEqual(
            first.headers["paypal-request-id"], second.headers["paypal-request-id"]
        )

    def test_other_shopper_cannot_refund(self):
        number = self.captured_order()
        self.assertEqual(
            self.refund(number, "x", "1.00", client=self.b).status_code, 404
        )


class SavedCardTests(ApiTestCase):
    def save(self, client=None, key=None):
        headers = {"Idempotency-Key": key} if key else {}
        return self.post(
            client or self.a, "/api/payment-methods", {"card": CARD}, **headers
        )

    def test_save_list_pay_delete(self):
        self.paypal.on(
            "POST",
            "/v3/vault/payment-tokens",
            json_response(201, fakes.payment_token()),
        )
        r = self.save()
        self.assertEqual(r.status_code, 201, r.content)
        card = r.json()
        self.assertEqual(card["brand"], "VISA")
        self.assertEqual(card["lastDigits"], "1111")
        self.assertNotIn(PAN, r.content.decode())
        pm = card["paymentMethodId"]
        (req,) = self.paypal.calls("POST", "/v3/vault/payment-tokens")
        self.assertEqual(
            fakes.FakePayPal.body(req)["payment_source"]["card"]["number"], PAN
        )

        listed = self.a.get("/api/payment-methods").json()["paymentMethods"]
        self.assertEqual([c["paymentMethodId"] for c in listed], [pm])
        self.assertEqual(
            self.b.get("/api/payment-methods").json()["paymentMethods"], []
        )

        # Pay with the saved card: the vault token goes to PayPal, not card details.
        number = self.place()
        self.paypal.on(
            "POST", "/v2/checkout/orders", json_response(201, fakes.order_completed())
        )
        r = self.post(self.a, "/api/orders/%s/pay" % number, {"paymentMethodId": pm})
        self.assertEqual(r.status_code, 200, r.content)
        (req,) = self.paypal.calls("POST", "/v2/checkout/orders")
        self.assertEqual(
            fakes.FakePayPal.body(req)["payment_source"],
            {"card": {"vault_id": "TOKEN1"}},
        )

        # Another shopper can neither use nor delete it.
        other = self.place(client=self.b)
        self.assertEqual(
            self.post(
                self.b, "/api/orders/%s/pay" % other, {"paymentMethodId": pm}
            ).status_code,
            404,
        )
        self.assertEqual(self.b.delete("/api/payment-methods/%s" % pm).status_code, 404)

        self.paypal.on(
            "DELETE",
            "/v3/vault/payment-tokens/TOKEN1",
            fakes.HttpResponse(status_code=204, headers={}),
        )
        self.assertEqual(self.a.delete("/api/payment-methods/%s" % pm).status_code, 204)
        self.assertEqual(
            self.a.get("/api/payment-methods").json()["paymentMethods"], []
        )
        number2 = self.place()
        r = self.post(self.a, "/api/orders/%s/pay" % number2, {"paymentMethodId": pm})
        self.assertEqual(r.status_code, 404)

    def test_card_data_is_never_stored(self):
        self.paypal.on(
            "POST",
            "/v3/vault/payment-tokens",
            json_response(201, fakes.payment_token()),
        )
        self.save()
        stored = json.dumps(list(SavedCard.objects.values()), default=str)
        self.assertNotIn(PAN, stored)
        self.assertNotIn('"123"', stored)

    def test_second_card_reuses_paypal_customer(self):
        self.paypal.on(
            "POST",
            "/v3/vault/payment-tokens",
            json_response(201, fakes.payment_token()),
            json_response(201, fakes.payment_token("TOKEN2")),
        )
        self.save()
        self.save()
        first, second = self.paypal.calls("POST", "/v3/vault/payment-tokens")
        self.assertNotIn("customer", fakes.FakePayPal.body(first))
        self.assertEqual(fakes.FakePayPal.body(second)["customer"], {"id": "CUST1"})

    def test_idempotency_key_prevents_double_save(self):
        self.paypal.on(
            "POST",
            "/v3/vault/payment-tokens",
            json_response(201, fakes.payment_token()),
        )
        r1, r2 = self.save(key="card-1"), self.save(key="card-1")
        self.assertEqual((r1.status_code, r2.status_code), (201, 200))
        self.assertEqual(r1.json()["paymentMethodId"], r2.json()["paymentMethodId"])
        self.assertEqual(len(self.paypal.calls("POST", "/v3/vault/payment-tokens")), 1)

    def test_rejected_card(self):
        self.paypal.on(
            "POST", "/v3/vault/payment-tokens", json_response(422, fakes.paypal_error())
        )
        self.assertEqual(self.save().status_code, 422)
        self.assertEqual(
            self.a.get("/api/payment-methods").json()["paymentMethods"], []
        )

    def test_delete_already_gone_at_paypal(self):
        self.paypal.on(
            "POST",
            "/v3/vault/payment-tokens",
            json_response(201, fakes.payment_token()),
        )
        pm = self.save().json()["paymentMethodId"]
        self.paypal.on(
            "DELETE",
            "/v3/vault/payment-tokens/TOKEN1",
            json_response(404, fakes.paypal_error(name="RESOURCE_NOT_FOUND")),
        )
        self.assertEqual(self.a.delete("/api/payment-methods/%s" % pm).status_code, 204)


@override_settings(**PAYPAL)
class ConfigurationTests(TestCase):
    def tearDown(self):
        fakes.uninstall()

    def test_base_url_override_is_used_for_token_and_api(self):
        fake = fakes.FakePayPal()
        fake.on(
            "GET", "/v2/payments/captures/C1", json_response(200, fakes.capture("C1"))
        )
        with self.settings(PAYPAL_BASE_URL="https://paypal.example.test"):
            fakes.install(fake)
            from apps.paypal_payments import paypal_calls

            paypal_calls.get_capture("C1")
        self.assertTrue(
            all(r.url.startswith("https://paypal.example.test/") for r in fake.requests)
        )
        self.assertEqual(len(fake.requests), 2)  # token + capture

    def test_environment_resolution(self):
        with self.settings(PAYPAL_BASE_URL="", PAYPAL_ENVIRONMENT="sandbox"):
            self.assertEqual(
                gateway.resolve_base_url(), "https://api-m.sandbox.paypal.com"
            )
        with self.settings(PAYPAL_BASE_URL="", PAYPAL_ENVIRONMENT="elsewhere"):
            with self.assertRaises(Exception):
                gateway.resolve_base_url()

    def test_missing_credentials_refuse_to_build(self):
        with self.settings(PAYPAL_CLIENT_ID=""):
            with self.assertRaisesMessage(Exception, "PAYPAL_CLIENT_ID"):
                gateway.build_client(transport=fakes.FakePayPal())


class ReconciliationTests(ApiTestCase):
    def search_page(self, txns, page, total_pages, refreshed=None):
        return json_response(
            200,
            {
                "transaction_details": [{"transaction_info": t} for t in txns],
                "page": page,
                "total_pages": total_pages,
                "total_items": 99,
                "last_refreshed_datetime": refreshed or fakes.ts(-1),
            },
        )

    def txn(self, txn_id, when, amount="15.00", custom=None, code="T0006"):
        info = {
            "transaction_id": txn_id,
            "transaction_event_code": code,
            "transaction_initiation_date": when,
            "transaction_amount": {"currency_code": "USD", "value": amount},
            "transaction_status": "S",
        }
        if custom:
            info["custom_field"] = custom
        return info

    def test_report_walks_all_pages_and_lines_up_orders(self):
        number = self.captured_order()
        payment = self.payment(number)
        start = datetime.now(timezone.utc) - timedelta(days=1)
        end = datetime.now(timezone.utc) + timedelta(minutes=5)
        inside = fakes.ts(0.01)
        self.paypal.on(
            "GET",
            "/v1/reporting/transactions",
            self.search_page(
                [self.txn("CAP1", inside), self.txn("STRANGER", inside, "99.00")], 1, 2
            ),
            self.search_page(
                [
                    self.txn(
                        "AUTH-REPORT-ID",
                        inside,
                        "15.00",
                        custom=payment.custom_id,
                        code="T1200",
                    ),
                    self.txn(
                        "OLD", fakes.ts(3)
                    ),  # outside the requested window: narrowed away
                ],
                2,
                2,
            ),
        )
        r = self.ops.get(
            "/api/reconciliation", {"from": start.isoformat(), "to": end.isoformat()}
        )
        self.assertEqual(r.status_code, 200, r.content)
        report = r.json()
        self.assertTrue(report["complete"])
        self.assertEqual(len(self.paypal.calls("GET", "/v1/reporting/transactions")), 2)
        (matched,) = report["matched"]
        self.assertEqual(matched["orderId"], number)
        ids = sorted(t["transactionId"] for t in matched["transactions"])
        self.assertEqual(ids, ["AUTH-REPORT-ID", "CAP1"])
        self.assertEqual(
            [t["transactionId"] for t in report["paypalOnly"]], ["STRANGER"]
        )
        self.assertEqual(report["appOnly"], [])
        self.assertEqual(report["summary"]["paypalTransactions"], 3)
        page2 = self.paypal.calls("GET", "/v1/reporting/transactions")[1]
        self.assertIn("page=2", page2.url)
        self.assertIn("balance_affecting_records_only=N", page2.url)

    def test_app_only_and_not_yet_reported(self):
        number = self.captured_order()
        start = datetime.now(timezone.utc) - timedelta(days=1)
        end = datetime.now(timezone.utc) + timedelta(minutes=5)
        self.paypal.on(
            "GET",
            "/v1/reporting/transactions",
            self.search_page([], 1, 0, refreshed=fakes.ts(0.5)),
        )
        report = self.ops.get(
            "/api/reconciliation", {"from": start.isoformat(), "to": end.isoformat()}
        ).json()
        self.assertEqual(report["appOnly"], [])
        kinds = sorted(i["kind"] for i in report["notYetReported"])
        self.assertEqual(kinds, ["authorization", "capture"])
        self.assertTrue(all(i["orderId"] == number for i in report["notYetReported"]))

        PayPalPayment.objects.filter(order__number=number).update(
            captured_at=datetime.now(timezone.utc) - timedelta(hours=20)
        )
        self.paypal.on(
            "GET",
            "/v1/reporting/transactions",
            self.search_page([], 1, 0, refreshed=fakes.ts(0.5)),
        )
        report = self.ops.get(
            "/api/reconciliation", {"from": start.isoformat(), "to": end.isoformat()}
        ).json()
        self.assertEqual([i["paypalId"] for i in report["appOnly"]], ["CAP1"])

    def test_long_range_is_split_into_31_day_windows(self):
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=70)
        for _ in range(3):
            self.paypal.on(
                "GET", "/v1/reporting/transactions", self.search_page([], 1, 1)
            )
        r = self.ops.get(
            "/api/reconciliation", {"from": start.isoformat(), "to": end.isoformat()}
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(len(self.paypal.calls("GET", "/v1/reporting/transactions")), 3)

    def test_page_cap_marks_report_incomplete(self):
        from unittest import mock

        from apps.paypal_payments import paypal_calls

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=1)
        for page in (1, 2):
            self.paypal.on(
                "GET",
                "/v1/reporting/transactions",
                self.search_page([self.txn("X%d" % page, fakes.ts(0.1))], page, 50),
            )
        with mock.patch.object(paypal_calls, "MAX_PAGES_PER_WINDOW", 2):
            report = self.ops.get(
                "/api/reconciliation",
                {"from": start.isoformat(), "to": end.isoformat()},
            ).json()
        self.assertFalse(report["complete"])
        self.assertTrue(report["truncated"])
        self.assertEqual(report["summary"]["paypalTransactions"], 2)

    def test_bad_range(self):
        r = self.ops.get(
            "/api/reconciliation", {"from": "nope", "to": "2026-01-01T00:00:00Z"}
        )
        self.assertEqual(r.status_code, 400)
        r = self.ops.get(
            "/api/reconciliation",
            {"from": "2026-02-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
        )
        self.assertEqual(r.status_code, 400)


class LoggingTests(ApiTestCase):
    def test_transport_log_has_no_secrets_or_bodies(self):
        number = self.place()
        self.paypal.on(
            "POST", "/v2/checkout/orders", json_response(201, fakes.order_completed())
        )
        with self.assertLogs(
            "apps.paypal_payments.gateway", level=logging.INFO
        ) as logs:
            self.post(self.a, "/api/orders/%s/pay" % number, {"card": CARD})
        output = "\n".join(logs.output)
        self.assertIn(
            "POST https://api-m.sandbox.paypal.com/v2/checkout/orders -> 201", output
        )
        for secret in (
            PAN,
            "test-client-secret",
            "test-client-id",
            "test-token",
            "Alice Shopper",
        ):
            self.assertNotIn(secret, output)
