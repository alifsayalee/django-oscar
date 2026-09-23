"""
Tests for the payments API. PayPal is faked at the SDK's transport seam, so the
real SDK builds every request; nothing leaves the process.

Run: python sandbox/manage.py test apps.payments
"""

import json
import re
from datetime import timedelta
from decimal import Decimal

import httpx
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from oscar.test.factories import create_product
from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpRequest, HttpResponse

from . import gateway
from .models import PayPalPayment, PayPalRefund, SavedCard

CARD = {"number": "4111 1111 1111 1111", "expiry": "2030-12", "securityCode": "123", "name": "Test Shopper",
        "billingAddress": {"line1": "1 Main St", "city": "San Jose", "state": "CA", "postalCode": "95131",
                           "countryCode": "US"}}


def json_response(status, body):
    return HttpResponse(status_code=status, headers={"content-type": "application/json"},
                        content=json.dumps(body).encode())


TOKEN = json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})


class RouterTransport:
    """Answers by (method, path regex); records every request. Queued answers are used in order."""

    def __init__(self):
        self.routes = []
        self.requests = []

    def on(self, method, pattern, *answers):
        self.routes.append((method, re.compile(pattern), list(answers)))
        return self

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = httpx.URL(request.url).path
        if path == "/v1/oauth2/token":
            return TOKEN
        for method, pattern, answers in self.routes:
            if method == request.method and pattern.search(path):
                answer = answers.pop(0) if len(answers) > 1 else answers[0]
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError("unexpected PayPal call %s %s" % (request.method, path))

    def close(self):
        pass

    def calls(self, method, pattern):
        return [r for r in self.requests if r.method == method and re.search(pattern, httpx.URL(r.url).path)]


def paypal_order(order_id="PPO1", status="COMPLETED", auth_status="CREATED", amount="20.00", auth_id="AUTH1",
                 created=None):
    created = created or timezone.now()
    return {
        "id": order_id, "status": status, "intent": "AUTHORIZE",
        "purchase_units": [{"payments": {"authorizations": [{
            "id": auth_id, "status": auth_status, "amount": {"currency_code": "USD", "value": amount},
            "create_time": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expiration_time": (created + timedelta(days=29)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }]}}],
    }


def authorization(auth_id="AUTH1", status="CREATED", amount="20.00", created=None, expires=None):
    created = created or timezone.now()
    expires = expires or created + timedelta(days=29)
    return {"id": auth_id, "status": status, "amount": {"currency_code": "USD", "value": amount},
            "create_time": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expiration_time": expires.strftime("%Y-%m-%dT%H:%M:%SZ")}


def capture(capture_id="CAP1", status="COMPLETED", amount="20.00", fee="1.00"):
    net = str(Decimal(amount) - Decimal(fee))
    return {"id": capture_id, "status": status, "amount": {"currency_code": "USD", "value": amount},
            "create_time": timezone.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "seller_receivable_breakdown": {
                "gross_amount": {"currency_code": "USD", "value": amount},
                "paypal_fee": {"currency_code": "USD", "value": fee},
                "net_amount": {"currency_code": "USD", "value": net}}}


def refund_body(refund_id="R1", status="COMPLETED", amount="5.00"):
    return {"id": refund_id, "status": status, "amount": {"currency_code": "USD", "value": amount},
            "create_time": timezone.now().isoformat()}


PAYPAL_ERROR_422 = json_response(422, {
    "name": "UNPROCESSABLE_ENTITY", "message": "The requested action could not be performed.", "debug_id": "dbg1",
    "details": [{"issue": "CARD_DECLINED", "description": "The card was declined."}]})


@override_settings(PAYPAL_CLIENT_ID="test-id", PAYPAL_CLIENT_SECRET="test-secret", PAYPAL_CURRENCY="USD",
                   PAYPAL_ENVIRONMENT="sandbox", PAYPAL_BASE_URL="")
class ApiTestCase(TestCase):
    def setUp(self):
        self.transport = RouterTransport()
        gateway.set_client(PaypalClient(
            base_url=gateway.SANDBOX_BASE_URL, custom_http_client=self.transport,
            oauth2=ClientCredentials(client_id="test-id", client_secret="test-secret"),
        ))
        self.addCleanup(gateway.set_client, None)
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw-alice-123")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw-bob-123")
        self.staff = User.objects.create_user("ops", "ops@example.com", "pw-ops-123", is_staff=True)
        self.product = create_product(price=Decimal("10.00"), num_in_stock=100)

    def as_user(self, user):
        self.client.force_login(user)

    def post(self, path, body=None, **headers):
        return self.client.post("/api" + path, data=json.dumps(body or {}), content_type="application/json",
                                headers=headers)

    def place(self, user=None, quantity=2):
        self.as_user(user or self.alice)
        response = self.post("/orders", {"items": [{"productId": self.product.pk, "quantity": quantity}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["orderId"]

    def paid_order(self):
        order_id = self.place()
        self.transport.on("POST", r"^/v2/checkout/orders$", json_response(201, paypal_order()))
        response = self.post("/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(response.status_code, 200, response.content)
        return order_id

    def fulfilled_order(self):
        order_id = self.paid_order()
        self.transport.on("GET", r"/authorizations/AUTH1$", json_response(200, authorization()))
        self.transport.on("POST", r"/authorizations/AUTH1/capture$", json_response(201, capture()))
        self.as_user(self.staff)
        response = self.post("/orders/%s/fulfil" % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.as_user(self.alice)
        return order_id


class OrderAndPayTests(ApiTestCase):
    def test_requires_login(self):
        response = self.post("/orders", {"items": []})
        self.assertEqual(response.status_code, 401)

    def test_order_total_comes_from_catalogue_in_configured_currency(self):
        order_id = self.place(quantity=2)
        body = self.client.get("/api/my-orders").json()["orders"][0]
        self.assertEqual((body["orderId"], body["total"], body["currency"], body["status"]),
                         (order_id, "20.00", "USD", "Awaiting payment"))

    def test_pay_authorizes_exact_total_and_sends_card_once(self):
        order_id = self.place()
        self.transport.on("POST", r"^/v2/checkout/orders$", json_response(201, paypal_order()))
        response = self.post("/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["status"], "Payment authorised")
        self.assertEqual(body["payment"]["status"], "authorized")
        self.assertEqual(body["payment"]["authorization"]["id"], "AUTH1")
        (sent,) = self.transport.calls("POST", r"^/v2/checkout/orders$")
        unit = sent.body.value["purchase_units"][0]
        self.assertEqual(unit["amount"], {"currency_code": "USD", "value": "20.00"})
        self.assertEqual(sent.body.value["intent"], "AUTHORIZE")
        self.assertEqual(sent.headers["prefer"], "return=representation")
        self.assertTrue(sent.headers["paypal-request-id"])
        # Card data went to PayPal, never to our database.
        payment = PayPalPayment.objects.get()
        self.assertNotIn("4111111111111111", json.dumps(list(PayPalPayment.objects.values())[0], default=str))
        self.assertEqual(payment.card_label, "CARD ****1111")

        # Double click: the payment already made is returned; PayPal is not called again.
        again = self.post("/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(self.transport.calls("POST", r"^/v2/checkout/orders$")), 1)

    def test_other_shopper_cannot_pay_or_see_order(self):
        order_id = self.place()
        self.as_user(self.bob)
        self.assertEqual(self.post("/orders/%s/pay" % order_id, {"card": CARD}).status_code, 404)
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"], [])

    def test_invalid_card_is_rejected_locally_without_echoing_it(self):
        order_id = self.place()
        response = self.post("/orders/%s/pay" % order_id, {"card": dict(CARD, number="4111111111111112")})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("4111111111111112", response.content.decode())
        self.assertEqual(self.transport.requests, [])

    def test_decline_releases_order_for_another_attempt(self):
        order_id = self.place()
        self.transport.on("POST", r"^/v2/checkout/orders$", PAYPAL_ERROR_422, json_response(201, paypal_order()))
        declined = self.post("/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(declined.status_code, 422)
        self.assertEqual(declined.json()["paypalIssue"], "CARD_DECLINED")
        retry = self.post("/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(retry.status_code, 200)
        first, second = self.transport.calls("POST", r"^/v2/checkout/orders$")
        self.assertNotEqual(first.headers["paypal-request-id"], second.headers["paypal-request-id"])

    def test_refused_connection_is_known_and_timeout_is_unknown(self):
        order_id = self.place()
        self.transport.on("POST", r"^/v2/checkout/orders$", httpx.ConnectError("refused"))
        unsent = self.post("/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(unsent.status_code, 502)
        self.assertNotIn("outcomeUnknown", unsent.json())
        self.assertEqual(PayPalPayment.objects.get().state, PayPalPayment.FAILED)

        self.transport.routes.clear()
        self.transport.on("POST", r"^/v2/checkout/orders$", httpx.ReadTimeout("no reply"),
                          json_response(201, paypal_order()))
        unknown = self.post("/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(unknown.status_code, 504)
        self.assertTrue(unknown.json()["outcomeUnknown"])
        payment = PayPalPayment.objects.exclude(state=PayPalPayment.FAILED).get()
        self.assertEqual(payment.state, PayPalPayment.UNKNOWN)

        # Resolving it resends under the SAME request id, so PayPal collapses a duplicate.
        resolved = self.post("/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(resolved.status_code, 200, resolved.content)
        sent = self.transport.calls("POST", r"^/v2/checkout/orders$")
        self.assertEqual(sent[-1].headers["paypal-request-id"], sent[-2].headers["paypal-request-id"])

    def test_credentials_rejected_is_a_configuration_error(self):
        order_id = self.place()

        class BadAuth(RouterTransport):
            def send(self, request):
                self.requests.append(request)
                return json_response(401, {"error": "invalid_client", "error_description": "bad"})

        gateway.set_client(PaypalClient(base_url=gateway.SANDBOX_BASE_URL, custom_http_client=BadAuth(),
                                        oauth2=ClientCredentials(client_id="x", client_secret="y")))
        response = self.post("/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual((response.status_code, response.json()["error"]), (502, "paypal_auth_failed"))

    def test_browser_challenge_is_refused_not_built(self):
        order_id = self.place()
        body = paypal_order(status="PAYER_ACTION_REQUIRED")
        body["purchase_units"] = []
        self.transport.on("POST", r"^/v2/checkout/orders$", json_response(200, body))
        response = self.post("/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual((response.status_code, response.json()["error"]), (422, "payer_action_required"))
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"][0]["status"], "Awaiting payment")

    def test_unlisted_authorization_status_is_not_authorized(self):
        order_id = self.place()
        self.transport.on("POST", r"^/v2/checkout/orders$",
                          json_response(201, paypal_order(auth_status="SOMETHING_NEW")))
        response = self.post("/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(PayPalPayment.objects.get().state, PayPalPayment.UNKNOWN)
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"][0]["status"], "Awaiting payment")

    def test_authorized_amount_mismatch_needs_review(self):
        order_id = self.place()
        self.transport.on("POST", r"^/v2/checkout/orders$", json_response(201, paypal_order(amount="19.00")))
        response = self.post("/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(response.json()["error"], "amount_mismatch")
        self.assertEqual(PayPalPayment.objects.get().state, PayPalPayment.NEEDS_REVIEW)


class FulfilCancelTests(ApiTestCase):
    def test_fulfil_is_staff_only(self):
        order_id = self.paid_order()
        self.assertEqual(self.post("/orders/%s/fulfil" % order_id).status_code, 403)

    def test_fulfil_captures_and_records_fee_and_net(self):
        order_id = self.fulfilled_order()
        payment = self.client.get("/api/my-orders").json()["orders"][0]["payment"]
        self.assertEqual(payment["status"], "captured")
        self.assertEqual((payment["capture"]["grossAmount"], payment["capture"]["paypalFee"],
                          payment["capture"]["netAmount"]), ("20.00", "1.00", "19.00"))
        (sent,) = self.transport.calls("POST", r"/capture$")
        self.assertEqual(sent.body.value["amount"], {"currency_code": "USD", "value": "20.00"})
        # Fulfilling again does not capture again.
        self.as_user(self.staff)
        self.assertEqual(self.post("/orders/%s/fulfil" % order_id).status_code, 200)
        self.assertEqual(len(self.transport.calls("POST", r"/capture$")), 1)

    def test_stale_authorization_is_renewed_before_capture(self):
        order_id = self.paid_order()
        old = timezone.now() - timedelta(days=5)
        self.transport.on("GET", r"/authorizations/AUTH1$", json_response(200, authorization(created=old)))
        self.transport.on("POST", r"/authorizations/AUTH1/reauthorize$",
                          json_response(201, authorization(auth_id="AUTH2")))
        self.transport.on("POST", r"/authorizations/AUTH2/capture$", json_response(201, capture()))
        self.as_user(self.staff)
        response = self.post("/orders/%s/fulfil" % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["payment"]["authorization"]["id"], "AUTH2")
        self.assertEqual(len(self.transport.calls("POST", r"/reauthorize$")), 1)

    def test_renewal_refused_explains_what_to_do(self):
        order_id = self.paid_order()
        old = timezone.now() - timedelta(days=5)
        self.transport.on("GET", r"/authorizations/AUTH1$", json_response(200, authorization(created=old)))
        self.transport.on("POST", r"/reauthorize$", PAYPAL_ERROR_422)
        self.as_user(self.staff)
        response = self.post("/orders/%s/fulfil" % order_id)
        self.assertEqual((response.status_code, response.json()["error"]), (409, "authorization_renewal_failed"))
        self.assertIn("Cancel this order", response.json()["message"])
        self.assertEqual(PayPalPayment.objects.get().state, PayPalPayment.AUTHORIZED)
        self.assertEqual(self.transport.calls("POST", r"/capture$"), [])

    def test_expired_authorization_is_not_captured(self):
        order_id = self.paid_order()
        old = timezone.now() - timedelta(days=31)
        self.transport.on("GET", r"/authorizations/AUTH1$",
                          json_response(200, authorization(created=old, expires=old + timedelta(days=29))))
        self.as_user(self.staff)
        response = self.post("/orders/%s/fulfil" % order_id)
        self.assertEqual((response.status_code, response.json()["error"]), (409, "authorization_expired"))
        self.assertEqual(self.transport.calls("POST", r"/capture$"), [])

    def test_cancel_voids_authorization(self):
        order_id = self.paid_order()
        self.transport.on("POST", r"/authorizations/AUTH1/void$",
                          json_response(200, authorization(status="VOIDED")))
        self.as_user(self.staff)
        response = self.post("/orders/%s/cancel" % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.json()["status"], response.json()["payment"]["status"]), ("Cancelled", "voided"))
        self.assertEqual(self.post("/orders/%s/cancel" % order_id).status_code, 200)
        self.assertEqual(len(self.transport.calls("POST", r"/void$")), 1)

    def test_cancel_after_fulfilment_is_refused(self):
        order_id = self.fulfilled_order()
        self.as_user(self.staff)
        self.assertEqual(self.post("/orders/%s/cancel" % order_id).status_code, 409)


class RefundTests(ApiTestCase):
    def test_partial_refunds_cannot_exceed_captured(self):
        order_id = self.fulfilled_order()
        self.transport.on("POST", r"/captures/CAP1/refund$", json_response(201, refund_body("R1", amount="15.00")),
                          json_response(201, refund_body("R2", amount="5.00")))
        first = self.post("/orders/%s/refunds" % order_id, {"amount": "15.00", "idempotencyKey": "k1"})
        self.assertEqual(first.status_code, 201, first.content)
        self.assertTrue(first.json()["refundId"])
        too_much = self.post("/orders/%s/refunds" % order_id, {"amount": "5.01", "idempotencyKey": "k2"})
        self.assertEqual((too_much.status_code, too_much.json()["refundableRemaining"]), (422, "5.00"))
        rest = self.post("/orders/%s/refunds" % order_id, {"amount": "5.00", "idempotencyKey": "k3"})
        self.assertEqual(rest.status_code, 201)
        self.assertEqual(len(self.transport.calls("POST", r"/refund$")), 2)
        payment = self.client.get("/api/my-orders").json()["orders"][0]["payment"]
        self.assertEqual((payment["status"], payment["refunded"]), ("refunded", "20.00"))

    def test_same_key_does_not_refund_twice(self):
        order_id = self.fulfilled_order()
        self.transport.on("POST", r"/refund$", json_response(201, refund_body()))
        first = self.post("/orders/%s/refunds" % order_id, {"amount": "5.00", "idempotencyKey": "same"})
        again = self.post("/orders/%s/refunds" % order_id, {"amount": "5.00", "idempotencyKey": "same"})
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(first.json()["refundId"], again.json()["refundId"])
        self.assertEqual(len(self.transport.calls("POST", r"/refund$")), 1)
        other_amount = self.post("/orders/%s/refunds" % order_id, {"amount": "6.00", "idempotencyKey": "same"})
        self.assertEqual(other_amount.status_code, 409)

    def test_refund_timeout_keeps_reservation_and_resends_same_request_id(self):
        order_id = self.fulfilled_order()
        self.transport.on("POST", r"/refund$", httpx.ReadTimeout("no reply"),
                          json_response(201, refund_body(amount="20.00")))
        unknown = self.post("/orders/%s/refunds" % order_id, {"idempotencyKey": "k"})
        self.assertEqual(unknown.status_code, 504)
        # The reservation still holds: a different refund cannot overshoot meanwhile.
        blocked = self.post("/orders/%s/refunds" % order_id, {"amount": "1.00", "idempotencyKey": "k-other"})
        self.assertEqual(blocked.status_code, 422)
        resolved = self.post("/orders/%s/refunds" % order_id, {"idempotencyKey": "k"})
        self.assertEqual(resolved.status_code, 200, resolved.content)
        first, second = self.transport.calls("POST", r"/refund$")
        self.assertEqual(first.headers["paypal-request-id"], second.headers["paypal-request-id"])

    def test_refund_requires_key_and_ownership(self):
        order_id = self.fulfilled_order()
        self.assertEqual(self.post("/orders/%s/refunds" % order_id, {"amount": "1.00"}).status_code, 400)
        self.as_user(self.bob)
        self.assertEqual(self.post("/orders/%s/refunds" % order_id,
                                   {"amount": "1.00", "idempotencyKey": "b"}).status_code, 404)

    def test_failed_refund_releases_reservation(self):
        order_id = self.fulfilled_order()
        self.transport.on("POST", r"/refund$", json_response(201, refund_body(status="FAILED", amount="20.00")))
        response = self.post("/orders/%s/refunds" % order_id, {"idempotencyKey": "k"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(PayPalPayment.objects.get().refund_reserved, 0)
        self.assertEqual(PayPalRefund.objects.get().state, PayPalRefund.FAILED)


class SavedCardTests(ApiTestCase):
    def save(self):
        self.transport.on("POST", r"^/v3/vault/payment-tokens$", json_response(201, {
            "id": "TOK1", "customer": {"id": "CUST1"},
            "payment_source": {"card": {"brand": "VISA", "last_digits": "1111", "expiry": "2030-12"}}}))
        self.as_user(self.alice)
        response = self.post("/payment-methods", {"card": CARD})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def test_save_list_and_describe_safely(self):
        card = self.save()
        self.assertEqual((card["brand"], card["lastDigits"]), ("VISA", "1111"))
        listed = self.client.get("/api/payment-methods").json()["paymentMethods"]
        self.assertEqual([c["paymentMethodId"] for c in listed], [card["paymentMethodId"]])
        self.assertNotIn("4111111111111111", json.dumps(listed))
        self.assertNotIn("TOK1", json.dumps(listed))
        self.as_user(self.bob)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])

    def test_pay_with_saved_card_sends_vault_id_and_second_save_reuses_customer(self):
        card = self.save()
        order_id = self.place()
        self.transport.on("POST", r"^/v2/checkout/orders$", json_response(201, paypal_order()))
        response = self.post("/orders/%s/pay" % order_id, {"paymentMethodId": card["paymentMethodId"]})
        self.assertEqual(response.status_code, 200, response.content)
        (sent,) = self.transport.calls("POST", r"^/v2/checkout/orders$")
        self.assertEqual(sent.body.value["payment_source"], {"card": {"vault_id": "TOK1"}})
        self.post("/payment-methods", {"card": CARD})
        second = self.transport.calls("POST", r"payment-tokens$")[-1]
        self.assertEqual(second.body.value["customer"], {"id": "CUST1"})

    def test_other_shopper_cannot_use_or_delete(self):
        card = self.save()
        self.as_user(self.bob)
        order_id = self.place(user=self.bob)
        self.assertEqual(self.post("/orders/%s/pay" % order_id,
                                   {"paymentMethodId": card["paymentMethodId"]}).status_code, 404)
        self.assertEqual(self.client.delete("/api/payment-methods/%s" % card["paymentMethodId"]).status_code, 404)
        self.assertEqual(SavedCard.objects.get().state, SavedCard.ACTIVE)

    def test_delete_removes_and_blocks_payment(self):
        card = self.save()
        self.transport.on("DELETE", r"/payment-tokens/TOK1$", HttpResponse(status_code=204, headers={}))
        response = self.client.delete("/api/payment-methods/%s" % card["paymentMethodId"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        order_id = self.place()
        self.assertEqual(self.post("/orders/%s/pay" % order_id,
                                   {"paymentMethodId": card["paymentMethodId"]}).status_code, 404)

    def test_idempotency_key_saves_once(self):
        self.transport.on("POST", r"payment-tokens$", json_response(201, {
            "id": "TOK1", "payment_source": {"card": {"brand": "VISA", "last_digits": "1111"}}}))
        self.as_user(self.alice)
        a = self.post("/payment-methods", {"card": CARD}, **{"Idempotency-Key": "save-1"})
        b = self.post("/payment-methods", {"card": CARD}, **{"Idempotency-Key": "save-1"})
        self.assertEqual((a.status_code, b.status_code), (201, 200))
        self.assertEqual(len(self.transport.calls("POST", r"payment-tokens$")), 1)


class ReconciliationTests(ApiTestCase):
    def txn(self, txn_id, amount, when, custom=""):
        return {"transaction_info": {
            "transaction_id": txn_id, "transaction_event_code": "T0006", "transaction_status": "S",
            "transaction_initiation_date": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "transaction_amount": {"currency_code": "USD", "value": amount}, "custom_field": custom}}

    def test_staff_only(self):
        self.as_user(self.alice)
        self.assertEqual(self.client.get("/api/reconciliation?from=2026-01-01T00:00:00Z&to=2026-01-02T00:00:00Z")
                         .status_code, 403)

    def test_walks_every_page_and_window_and_matches_sets(self):
        order_id = self.fulfilled_order()
        payment = PayPalPayment.objects.get()
        refund = PayPalRefund.objects.create(payment=payment, idempotency_key="k", amount=Decimal("5.00"),
                                             currency="USD", state=PayPalRefund.COMPLETED,
                                             paypal_refund_id="R1", refund_time=payment.capture_time)
        now = timezone.now()
        start, end = now - timedelta(days=40), now + timedelta(hours=1)
        page1 = {"transaction_details": [self.txn("CAP1", "20.00", payment.capture_time, order_id)],
                 "total_pages": 2, "page": 1, "last_refreshed_datetime": end.strftime("%Y-%m-%dT%H:%M:%SZ")}
        page2 = {"transaction_details": [self.txn("R1", "-5.00", refund.refund_time, order_id),
                                         self.txn("STRANGER", "9.00", payment.capture_time)],
                 "total_pages": 2, "page": 2}
        older = {"transaction_details": [], "total_pages": 0, "page": 1}
        self.transport.on("GET", r"/v1/reporting/transactions$",
                          json_response(200, older), json_response(200, page1), json_response(200, page2))
        self.as_user(self.staff)
        response = self.client.get("/api/reconciliation", {"from": start.isoformat(), "to": end.isoformat()})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual(report["windows"], 2)
        self.assertEqual(len(self.transport.calls("GET", r"/reporting/transactions$")), 3)
        self.assertEqual(sorted(m["transactionId"] for m in report["matched"]), ["CAP1", "R1"])
        self.assertTrue(all(m["amountMatches"] for m in report["matched"]))
        self.assertEqual([p["transactionId"] for p in report["paypalOnly"]], ["STRANGER"])
        self.assertEqual(report["appOnly"], [])
        self.assertFalse(report["truncated"])

    def test_rejects_bad_range(self):
        self.as_user(self.staff)
        self.assertEqual(self.client.get("/api/reconciliation", {"from": "nope", "to": "2026-01-01"}).status_code, 400)
        self.assertEqual(self.client.get("/api/reconciliation", {"from": "2026-02-01T00:00:00Z",
                                                                 "to": "2026-01-01T00:00:00Z"}).status_code, 400)


class ConfigurationTests(TestCase):
    @override_settings(PAYPAL_BASE_URL="https://paypal-proxy.internal.example", PAYPAL_ENVIRONMENT="sandbox")
    def test_base_url_override_is_used_verbatim(self):
        self.assertEqual(gateway.base_url(), "https://paypal-proxy.internal.example")

    @override_settings(PAYPAL_BASE_URL="", PAYPAL_ENVIRONMENT="sandbox")
    def test_sandbox_environment_selects_sandbox_host(self):
        self.assertEqual(gateway.base_url(), gateway.SANDBOX_BASE_URL)

    @override_settings(PAYPAL_BASE_URL="", PAYPAL_ENVIRONMENT="live")
    def test_other_environment_requires_explicit_base_url(self):
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured):
            gateway.base_url()

    @override_settings(PAYPAL_CLIENT_ID="", PAYPAL_CLIENT_SECRET="")
    def test_missing_credentials_stop_client_construction(self):
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured):
            gateway.build_client()

    @override_settings(PAYPAL_CLIENT_ID="id", PAYPAL_CLIENT_SECRET="secret", PAYPAL_ENVIRONMENT="sandbox",
                       PAYPAL_BASE_URL="https://paypal-proxy.internal.example")
    def test_token_request_goes_to_the_override_host(self):
        transport = RouterTransport().on("GET", r"/v2/payments/captures/C1$", json_response(200, capture("C1")))
        client = PaypalClient(base_url=gateway.base_url(), custom_http_client=transport,
                              oauth2=ClientCredentials(client_id="id", client_secret="secret"))
        client.payments.get_captured_payment("C1")
        hosts = {httpx.URL(r.url).host for r in transport.requests}
        self.assertEqual(hosts, {"paypal-proxy.internal.example"})
        self.assertEqual(len(transport.requests), 2)  # token + operation
