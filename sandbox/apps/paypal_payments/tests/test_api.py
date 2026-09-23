"""The HTTP API end to end, with PayPal replaced by a stub transport under the real SDK."""
import json
import logging
from datetime import timedelta
from decimal import Decimal

import httpx
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from oscar.test.factories import create_product

from apps.paypal_payments.models import PayPalPayment, PayPalRefund, SavedCard

from .support import (authorization_body, capture_body, empty_response, json_response, order_body,
                      paypal_error, refund_body, stub_paypal)

CARD = {"number": "4111 1111 1111 1111", "expiry": "2030-12", "securityCode": "123", "name": "Test Shopper"}
PAYPAL_SETTINGS = dict(PAYPAL_CLIENT_ID="test-client", PAYPAL_CLIENT_SECRET="test-secret",
                       PAYPAL_ENVIRONMENT="sandbox", PAYPAL_CURRENCY="USD", PAYPAL_BASE_URL="")


@override_settings(**PAYPAL_SETTINGS)
class ApiTestCase(TestCase):
    def setUp(self):
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "pw")
        self.other = User.objects.create_user("other", "other@example.com", "pw")
        self.staff = User.objects.create_user("op", "op@example.com", "pw", is_staff=True)
        self.product = create_product(price=Decimal("12.50"), num_in_stock=100)
        self.client.force_login(self.shopper)

    def post(self, url, data=None, user=None, **headers):
        if user is not None:
            self.client.force_login(user)
        response = self.client.post(url, json.dumps(data or {}), content_type="application/json",
                                    headers=headers)
        if user is not None:
            self.client.force_login(self.shopper)
        return response

    def place(self, quantity=2):
        response = self.post("/api/orders", {"items": [{"productId": self.product.id, "quantity": quantity}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["orderId"]

    def paid_order(self):
        number = self.place()
        with stub_paypal(json_response(201, order_body("25.00"))):
            self.assertEqual(self.post("/api/orders/%s/pay" % number, {"card": CARD}).status_code, 200)
        return number

    def captured_order(self):
        number = self.paid_order()
        with stub_paypal(json_response(201, capture_body("25.00"))):
            self.assertEqual(self.post("/api/orders/%s/fulfil" % number, user=self.staff).status_code, 200)
        return number


class OrderAndPayTests(ApiTestCase):
    def test_place_order_awaits_payment(self):
        response = self.post("/api/orders", {"items": [{"productId": self.product.id, "quantity": 2}]})
        body = response.json()
        self.assertEqual((body["total"], body["currency"], body["paymentStatus"]),
                         ("25.00", "USD", "awaiting_payment"))
        self.assertIsInstance(body["orderId"], str)

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.post("/api/orders", {"items": []}).status_code, 401)

    def test_pay_authorizes_the_order_total_once(self):
        number = self.place()
        with stub_paypal(json_response(201, order_body("25.00"))) as transport:
            response = self.post("/api/orders/%s/pay" % number, {"card": CARD})
            again = self.post("/api/orders/%s/pay" % number, {"card": CARD})  # double click
        self.assertEqual(response.status_code, 200)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(transport.api_requests), 1)
        sent = transport.api_requests[0].body.value
        self.assertEqual(sent["purchase_units"][0]["amount"]["value"], "25.00")
        body = response.json()
        self.assertEqual((body["paymentStatus"], body["status"]), ("authorized", "Being processed"))
        self.assertEqual(body["payment"]["authorization"]["id"], "AUTH1")

    def test_card_number_never_logged_or_stored(self):
        number = self.place()
        paypal = stub_paypal(json_response(201, order_body("25.00")))
        with self.assertLogs("apps.paypal_payments", level=logging.DEBUG) as logs, paypal:
            logging.getLogger("apps.paypal_payments").info("start")
            self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertFalse(any("4111111111111111" in line or "4111 1111" in line for line in logs.output))
        stored = json.dumps(list(PayPalPayment.objects.values()), default=str)
        self.assertNotIn("4111111111111111", stored)

    def test_timeout_is_unknown_and_retry_reuses_the_same_request_id(self):
        number = self.place()
        with stub_paypal(httpx.ReadTimeout("slow")) as transport:
            response = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(response.status_code, 504)
        first_id = transport.api_requests[0].headers["paypal-request-id"]
        self.assertEqual(PayPalPayment.objects.get().status, "unknown")
        with stub_paypal(json_response(200, order_body("25.00"))) as transport:
            response = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(transport.api_requests[0].headers["paypal-request-id"], first_id)
        self.assertEqual(PayPalPayment.objects.count(), 1)

    def test_unsent_failure_releases_the_claim(self):
        number = self.place()
        with stub_paypal(httpx.ConnectError("refused")):
            self.assertEqual(self.post("/api/orders/%s/pay" % number, {"card": CARD}).status_code, 502)
        with stub_paypal(json_response(201, order_body("25.00"))) as transport:
            self.assertEqual(self.post("/api/orders/%s/pay" % number, {"card": CARD}).status_code, 200)
        first, second = PayPalPayment.objects.order_by("date_created")
        self.assertEqual((first.status, second.status), ("failed", "authorized"))
        self.assertEqual(transport.api_requests[0].headers["paypal-request-id"], str(second.request_id))

    def test_declined_card(self):
        number = self.place()
        with stub_paypal(paypal_error(422, "INSTRUMENT_DECLINED", "Declined.")):
            response = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["providerIssues"], ["INSTRUMENT_DECLINED"])
        self.assertEqual(PayPalPayment.objects.get().status, "failed")

    def test_payer_action_required_is_reported(self):
        number = self.place()
        with stub_paypal(json_response(200, order_body("25.00", order_status="PAYER_ACTION_REQUIRED"))):
            response = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (409, "payer_action_required"))

    def test_amount_mismatch_needs_review(self):
        number = self.place()
        with stub_paypal(json_response(201, order_body("24.00"))):
            response = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(PayPalPayment.objects.get().status, "needs_review")

    def test_other_shopper_cannot_see_or_pay(self):
        number = self.place()
        self.client.force_login(self.other)
        self.assertEqual(self.post("/api/orders/%s/pay" % number, {"card": CARD}).status_code, 404)
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"], [])

    def test_invalid_card_is_rejected_without_echo(self):
        number = self.place()
        response = self.post("/api/orders/%s/pay" % number, {"card": dict(CARD, number="4111abc")})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("4111abc", response.content.decode())


class FulfilCancelTests(ApiTestCase):
    def test_fulfil_is_staff_only(self):
        number = self.paid_order()
        self.assertEqual(self.post("/api/orders/%s/fulfil" % number).status_code, 403)

    def test_fulfil_captures_and_reports_fee_and_net(self):
        number = self.paid_order()
        with stub_paypal(json_response(201, capture_body("25.00", fee="1.03"))) as transport:
            response = self.post("/api/orders/%s/fulfil" % number, user=self.staff)
            again = self.post("/api/orders/%s/fulfil" % number, user=self.staff)
        capture = response.json()["payment"]["capture"]
        self.assertEqual((capture["amount"], capture["paypalFee"], capture["netAmount"]), ("25.00", "1.03", "23.97"))
        self.assertEqual(response.json()["status"], "Complete")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(transport.api_requests), 1)
        self.assertTrue(transport.api_requests[0].url.endswith("/v2/payments/authorizations/AUTH1/capture"))

    def test_stale_authorization_is_renewed_before_capture(self):
        number = self.paid_order()
        PayPalPayment.objects.update(authorized_at=timezone.now() - timedelta(days=5),
                                     authorization_expires_at=timezone.now() + timedelta(days=20))
        with stub_paypal(json_response(201, authorization_body("25.00", auth_id="AUTH2")),
                         json_response(201, capture_body("25.00"))) as transport:
            response = self.post("/api/orders/%s/fulfil" % number, user=self.staff)
        self.assertEqual(response.status_code, 200, response.content)
        urls = [r.url for r in transport.api_requests]
        self.assertTrue(urls[0].endswith("/authorizations/AUTH1/reauthorize"))
        self.assertTrue(urls[1].endswith("/authorizations/AUTH2/capture"))

    def test_refused_renewal_says_what_to_do(self):
        number = self.paid_order()
        PayPalPayment.objects.update(authorized_at=timezone.now() - timedelta(days=5))
        with stub_paypal(paypal_error(422, "REAUTHORIZATION_NOT_ALLOWED", "Not allowed.")):
            response = self.post("/api/orders/%s/fulfil" % number, user=self.staff)
        error = response.json()["error"]
        self.assertEqual((response.status_code, error["code"]), (409, "authorization_renewal_refused"))
        self.assertIn("Cancel the order", error["message"])
        # the operator can still cancel: the capture claim was released
        with stub_paypal(json_response(200, authorization_body("25.00", status="VOIDED", auth_id="AUTH1"))):
            self.assertEqual(self.post("/api/orders/%s/cancel" % number, user=self.staff).status_code, 200)

    def test_expired_authorization_is_not_sent(self):
        number = self.paid_order()
        PayPalPayment.objects.update(authorization_expires_at=timezone.now() - timedelta(minutes=1))
        with stub_paypal() as transport:
            response = self.post("/api/orders/%s/fulfil" % number, user=self.staff)
        self.assertEqual(response.json()["error"]["code"], "authorization_expired")
        self.assertEqual(transport.api_requests, [])

    def test_cancel_voids_the_hold(self):
        number = self.paid_order()
        with stub_paypal(json_response(200, authorization_body("25.00", status="VOIDED", auth_id="AUTH1"))) as t:
            response = self.post("/api/orders/%s/cancel" % number, user=self.staff)
            again = self.post("/api/orders/%s/cancel" % number, user=self.staff)
        self.assertEqual((response.json()["status"], response.json()["paymentStatus"]), ("Cancelled", "voided"))
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(t.api_requests), 1)
        self.assertTrue(t.api_requests[0].url.endswith("/authorizations/AUTH1/void"))

    def test_cancel_after_capture_is_refused(self):
        number = self.captured_order()
        self.assertEqual(self.post("/api/orders/%s/cancel" % number, user=self.staff).status_code, 409)

    def test_cancel_unpaid_order_needs_no_paypal(self):
        number = self.place()
        with stub_paypal() as transport:
            response = self.post("/api/orders/%s/cancel" % number, user=self.staff)
        self.assertEqual(response.json()["status"], "Cancelled")
        self.assertEqual(transport.api_requests, [])


class RefundTests(ApiTestCase):
    def refund(self, number, amount, key):
        data = {} if amount is None else {"amount": amount}
        return self.post("/api/orders/%s/refunds" % number, data, **{"Idempotency-Key": key})

    def test_partial_refunds_and_idempotency(self):
        number = self.captured_order()
        with stub_paypal(json_response(201, refund_body("10.00", refund_id="R1")),
                         json_response(201, refund_body("5.00", refund_id="R2"))) as transport:
            first = self.refund(number, "10.00", "k1")
            repeat = self.refund(number, "10.00", "k1")
            second = self.refund(number, "5.00", "k2")
        self.assertEqual((first.status_code, repeat.status_code, second.status_code), (201, 201, 201))
        self.assertEqual(first.json()["refundId"], repeat.json()["refundId"])
        self.assertIn("refundId", second.json())
        self.assertEqual(len(transport.api_requests), 2)
        self.assertEqual(second.json()["order"]["paymentStatus"], "partially_refunded")

    def test_never_beyond_captured(self):
        number = self.captured_order()
        with stub_paypal(json_response(201, refund_body("20.00"))) as transport:
            self.assertEqual(self.refund(number, "20.00", "a").status_code, 201)
            over = self.refund(number, "5.01", "b")
        self.assertEqual((over.status_code, over.json()["error"]["code"]), (409, "refund_exceeds_captured"))
        self.assertEqual(len(transport.api_requests), 1)

    def test_same_key_different_amount_is_rejected(self):
        number = self.captured_order()
        with stub_paypal(json_response(201, refund_body("1.00"))):
            self.refund(number, "1.00", "k")
            self.assertEqual(self.refund(number, "2.00", "k").status_code, 422)

    def test_rejected_refund_releases_the_reservation(self):
        number = self.captured_order()
        with stub_paypal(paypal_error(422, "REFUND_NOT_ALLOWED"), json_response(201, refund_body("25.00"))):
            self.assertEqual(self.refund(number, None, "a").status_code, 422)
            self.assertEqual(self.refund(number, None, "b").status_code, 201)
        self.assertEqual(PayPalPayment.objects.get(status="captured").refund_reserved, Decimal("25.00"))

    def test_unknown_refund_is_resent_under_same_request_id(self):
        number = self.captured_order()
        with stub_paypal(httpx.ReadTimeout("slow")) as t1:
            self.assertEqual(self.refund(number, "3.00", "k").status_code, 504)
        with stub_paypal(json_response(201, refund_body("3.00"))) as t2:
            self.assertEqual(self.refund(number, "3.00", "k").status_code, 201)
        self.assertEqual(t1.api_requests[0].headers["paypal-request-id"],
                         t2.api_requests[0].headers["paypal-request-id"])
        self.assertEqual(PayPalRefund.objects.count(), 1)

    def test_key_required(self):
        number = self.captured_order()
        self.assertEqual(self.post("/api/orders/%s/refunds" % number, {"amount": "1.00"}).status_code, 400)


class SavedCardTests(ApiTestCase):
    def save(self):
        body = {"id": "TOK1", "customer": {"id": "CUST1"},
                "payment_source": {"card": {"brand": "VISA", "last_digits": "1111", "expiry": "2030-12"}}}
        with stub_paypal(json_response(201, body)) as transport:
            response = self.post("/api/payment-methods", {"card": CARD}, **{"Idempotency-Key": "save-1"})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["paymentMethodId"], transport

    def test_save_describes_card_safely_and_stores_no_number(self):
        pm_id, transport = self.save()
        listed = self.client.get("/api/payment-methods").json()["paymentMethods"]
        self.assertEqual([(c["paymentMethodId"], c["brand"], c["lastDigits"]) for c in listed],
                         [(pm_id, "VISA", "1111")])
        self.assertNotIn("4111111111111111", json.dumps(list(SavedCard.objects.values()), default=str))
        self.assertNotIn("customer", transport.api_requests[0].body.value)  # first card: PayPal makes the customer

    def test_reuse_saved_card_to_pay(self):
        pm_id, _ = self.save()
        number = self.place()
        with stub_paypal(json_response(201, order_body("25.00"))) as transport:
            response = self.post("/api/orders/%s/pay" % number, {"paymentMethodId": pm_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(transport.api_requests[0].body.value["payment_source"], {"card": {"vault_id": "TOK1"}})

    def test_other_shopper_cannot_list_use_or_delete(self):
        pm_id, _ = self.save()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        self.assertEqual(self.client.delete("/api/payment-methods/%s" % pm_id).status_code, 404)
        number = self.place()
        self.assertEqual(self.post("/api/orders/%s/pay" % number, {"paymentMethodId": pm_id}).status_code, 404)

    def test_delete_hides_and_disables_even_if_paypal_fails(self):
        pm_id, _ = self.save()
        with stub_paypal(json_response(500, {"name": "INTERNAL"})):
            self.assertEqual(self.client.delete("/api/payment-methods/%s" % pm_id).status_code, 502)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        number = self.place()
        self.assertEqual(self.post("/api/orders/%s/pay" % number, {"paymentMethodId": pm_id}).status_code, 404)
        with stub_paypal(empty_response(404)):  # already gone at PayPal counts as deleted
            self.assertEqual(self.client.delete("/api/payment-methods/%s" % pm_id).status_code, 204)
        self.assertEqual(SavedCard.objects.get().status, "deleted")

    def test_second_card_reuses_the_paypal_customer(self):
        self.save()
        body = {"id": "TOK2", "customer": {"id": "CUST1"}, "payment_source": {"card": {"last_digits": "1111"}}}
        with stub_paypal(json_response(201, body)) as transport:
            self.post("/api/payment-methods", {"card": CARD}, **{"Idempotency-Key": "save-2"})
        self.assertEqual(transport.api_requests[0].body.value["customer"], {"id": "CUST1"})


class ReconciliationTests(ApiTestCase):
    def test_staff_only(self):
        self.assertEqual(self.client.get("/api/reconciliation?from=2026-09-01T00:00:00Z&to=2026-09-02T00:00:00Z")
                         .status_code, 403)

    def test_lines_up_provider_and_local_records(self):
        number = self.captured_order()
        payment = PayPalPayment.objects.get(status="captured")
        custom = "%s:%s" % (number, payment.request_id.hex)
        txns = {"transaction_details": [
            {"transaction_info": {"transaction_id": "CAP1", "transaction_initiation_date": "2026-09-23T10:00:00Z",
                                  "transaction_amount": {"currency_code": "USD", "value": "25.00"},
                                  "custom_field": custom}},
            {"transaction_info": {"transaction_id": "STRANGER", "transaction_initiation_date": "2026-09-23T12:00:00Z",
                                  "transaction_amount": {"currency_code": "USD", "value": "7.00"}}},
        ], "total_pages": 1, "last_refreshed_datetime": "2026-09-24T00:00:00Z"}
        self.client.force_login(self.staff)
        with stub_paypal(json_response(200, txns)):
            body = self.client.get("/api/reconciliation?from=2026-09-23T00:00:00Z&to=2026-09-24T00:00:00Z").json()
        self.assertEqual([m["paypalTransactionId"] for m in body["matched"]], ["CAP1"])
        self.assertEqual([m["paypalTransactionId"] for m in body["providerOnly"]], ["STRANGER"])
        self.assertEqual(body["localOnly"], [])
        self.assertTrue(body["complete"])


class ClaimDurabilityTests(ApiTestCase):
    def test_payment_views_commit_claims_independently_of_the_request(self):
        from apps.paypal_payments import views
        for view in (views.pay, views.fulfil, views.cancel, views.refunds, views.payment_methods,
                     views.payment_method, views.orders):
            self.assertIn("default", getattr(view, "_non_atomic_requests", set()), view.__name__)

    def test_claim_exists_before_paypal_is_called(self):
        number = self.place()
        seen = []
        with stub_paypal(json_response(201, order_body("25.00"))) as transport:
            original = transport.send

            def send(request):
                if request.url.endswith("/v2/checkout/orders"):
                    seen.append(list(PayPalPayment.objects.values_list("status", flat=True)))
                return original(request)
            transport.send = send
            self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(seen, [["sending"]])
