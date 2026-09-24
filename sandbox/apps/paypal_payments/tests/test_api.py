"""
API tests for the PayPal payments app.

PayPal is faked at the SDK's transport seam: the real ``PaypalClient`` builds
and serialises every request, and ``StubPayPal`` answers it by route. Run with
``cd sandbox && python manage.py test apps.paypal_payments``.
"""
import datetime
import json
from decimal import Decimal

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from oscar.test.factories import create_product
from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpRequest, HttpResponse

from apps.paypal_payments import paypal_client
from apps.paypal_payments.models import PayPalPayment, PayPalRefund, PayPalSavedCard

User = get_user_model()

CARD = {
    "number": "4111 1111 1111 1111",
    "expiry": "2030-12",
    "securityCode": "123",
    "name": "Test Shopper",
    "billingAddress": {"addressLine1": "1 Main St", "city": "San Jose", "state": "CA",
                       "postalCode": "95131", "countryCode": "US"},
}


def json_response(status, body=None):
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=b"" if body is None else json.dumps(body).encode(),
    )


def paypal_error(status, issue, name="UNPROCESSABLE_ENTITY"):
    return json_response(status, {
        "name": name, "message": "The requested action could not be performed.", "debug_id": "dbg123",
        "details": [{"issue": issue, "description": "%s happened." % issue}],
    })


def iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class StubPayPal:
    """Satisfies the SDK's sync transport protocol and answers by route."""

    def __init__(self):
        self.requests: list[HttpRequest] = []
        self.routes: list[tuple[str, str, object]] = []

    def on(self, method, path_fragment, *responses):
        """Queue responses (HttpResponse or exception) for a route; the last one repeats."""
        self.routes.append((method, path_fragment, list(responses)))

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if request.url.endswith("/v1/oauth2/token"):
            return json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})
        for method, fragment, queue in self.routes:
            if request.method == method and request.url.split("?")[0].endswith(fragment):
                item = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(item, Exception):
                    raise item
                return item
        raise AssertionError("Unexpected PayPal call %s %s" % (request.method, request.url))

    def close(self):
        pass

    def calls(self, method, fragment):
        return [r for r in self.requests if r.method == method and r.url.split("?")[0].endswith(fragment)]


def order_response(amount, *, order_status="COMPLETED", auth_status="CREATED", auth_id="AUTH1",
                   order_id="PPORDER1", created=None):
    created = created or timezone.now()
    return json_response(201, {
        "id": order_id, "status": order_status, "intent": "AUTHORIZE",
        "payment_source": {"card": {"name": "Test Shopper", "last_digits": "1111", "brand": "VISA"}},
        "purchase_units": [{
            "reference_id": "default", "amount": {"currency_code": "USD", "value": amount},
            "payments": {"authorizations": [{
                "id": auth_id, "status": auth_status, "amount": {"currency_code": "USD", "value": amount},
                "expiration_time": iso(created + datetime.timedelta(days=29)), "create_time": iso(created),
            }]},
        }],
    })


def capture_response(amount, *, capture_id="CAP1", status="COMPLETED", fee="0.81", net=None):
    net = net or str(Decimal(amount) - Decimal(fee))
    return json_response(201, {
        "id": capture_id, "status": status, "amount": {"currency_code": "USD", "value": amount},
        "final_capture": True, "create_time": iso(timezone.now()),
        "seller_receivable_breakdown": {
            "gross_amount": {"currency_code": "USD", "value": amount},
            "paypal_fee": {"currency_code": "USD", "value": fee},
            "net_amount": {"currency_code": "USD", "value": net},
        },
    })


def refund_response(amount, refund_id="REF1", status="COMPLETED"):
    return json_response(201, {
        "id": refund_id, "status": status, "amount": {"currency_code": "USD", "value": amount},
        "create_time": "2026-09-23T16:42:09-07:00",
    })


def token_response(token_id="TOK1", customer_id="CUST1"):
    return json_response(200, {
        "id": token_id, "customer": {"id": customer_id},
        "payment_source": {"card": {"name": "Test Shopper", "last_digits": "1111", "brand": "VISA",
                                    "expiry": "2030-12"}},
    })


@override_settings(PAYPAL_CURRENCY="USD")
class PayPalApiTestCase(TestCase):
    def setUp(self):
        self.stub = StubPayPal()
        paypal_client.set_client(PaypalClient(
            base_url="https://paypal.test", custom_http_client=self.stub,
            oauth2=ClientCredentials(client_id="id", client_secret="secret"),
        ))
        self.addCleanup(paypal_client.set_client, None)
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "pass-word-123")
        self.other = User.objects.create_user("other", "other@example.com", "pass-word-123")
        self.staff = User.objects.create_user("op", "op@example.com", "pass-word-123", is_staff=True)
        self.product_a = create_product(price=Decimal("12.34"), num_in_stock=50)
        self.product_b = create_product(price=Decimal("5.00"), num_in_stock=50)
        self.client.force_login(self.shopper)

    # helpers -------------------------------------------------------------

    def post(self, url, body=None, **headers):
        return self.client.post(url, data=json.dumps(body or {}), content_type="application/json", headers=headers)

    def as_user(self, user):
        self.client.force_login(user)

    def place_order(self):
        response = self.post("/api/orders", {"items": [
            {"productId": self.product_a.pk, "quantity": 2}, {"productId": self.product_b.pk, "quantity": 1}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def paid_order(self):
        order = self.place_order()
        self.stub.on("POST", "/v2/checkout/orders", order_response(order["total"]))
        response = self.post("/api/orders/%s/pay" % order["orderId"], {"card": CARD})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def fulfilled_order(self):
        order = self.paid_order()
        self.stub.on("POST", "/capture", capture_response(order["total"]))
        self.as_user(self.staff)
        response = self.post("/api/orders/%s/fulfil" % order["orderId"])
        self.assertEqual(response.status_code, 200, response.content)
        self.as_user(self.shopper)
        return response.json()


class OrderTests(PayPalApiTestCase):
    def test_requires_session_login(self):
        self.client.logout()
        self.assertEqual(self.post("/api/orders", {"items": []}).status_code, 401)
        self.assertEqual(self.client.get("/api/my-orders").status_code, 401)

    def test_place_order_prices_from_catalogue_in_configured_currency(self):
        order = self.place_order()
        self.assertIn("orderId", order)
        self.assertEqual(order["total"], "29.68")
        self.assertEqual(order["currency"], "USD")
        self.assertEqual(order["status"], "Pending")
        self.assertEqual(order["paymentState"], "awaiting_payment")
        self.assertEqual(len(order["lines"]), 2)

    def test_unknown_product_is_rejected(self):
        response = self.post("/api/orders", {"items": [{"productId": 999999, "quantity": 1}]})
        self.assertEqual(response.status_code, 422)

    def test_my_orders_only_lists_callers_orders(self):
        order = self.place_order()
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"], [])
        self.as_user(self.shopper)
        ids = [o["orderId"] for o in self.client.get("/api/my-orders").json()["orders"]]
        self.assertEqual(ids, [order["orderId"]])


class PayTests(PayPalApiTestCase):
    def test_authorizes_the_order_total(self):
        order = self.paid_order()
        self.assertEqual(order["paymentState"], "authorized")
        self.assertEqual(order["status"], "Being processed")
        self.assertEqual(order["payment"]["paypal"]["authorizationId"], "AUTH1")
        self.assertEqual(order["payment"]["card"], {"brand": "VISA", "lastDigits": "1111"})

        [call] = self.stub.calls("POST", "/v2/checkout/orders")
        body = call.body.value
        self.assertEqual(body["intent"], "AUTHORIZE")
        self.assertEqual(body["purchase_units"][0]["amount"], {"currency_code": "USD", "value": "29.68"})
        payment = PayPalPayment.objects.get()
        self.assertEqual(body["purchase_units"][0]["invoice_id"], payment.invoice_id)
        self.assertTrue(payment.invoice_id.startswith(order["number"] + "-"))
        self.assertEqual(body["payment_source"]["card"]["number"], "4111111111111111")
        payment = PayPalPayment.objects.get()
        self.assertEqual(call.headers["paypal-request-id"], str(payment.reference))
        self.assertEqual(call.headers["prefer"], "return=representation")

    def test_card_details_are_not_stored(self):
        self.paid_order()
        for model in (PayPalPayment,):
            for row in model.objects.values():
                self.assertNotIn("4111111111111111", json.dumps(row, default=str))
                self.assertNotIn("123", [str(v) for v in row.values()])

    def test_double_click_authorizes_once(self):
        order = self.paid_order()
        response = self.post("/api/orders/%s/pay" % order["orderId"], {"card": CARD})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.stub.calls("POST", "/v2/checkout/orders")), 1)

    def test_declined_card_is_not_authorized_and_can_be_retried(self):
        order = self.place_order()
        self.stub.on("POST", "/v2/checkout/orders",
                     order_response(order["total"], auth_status="DENIED"),
                     order_response(order["total"], auth_id="AUTH2", order_id="PPORDER2"))
        response = self.post("/api/orders/%s/pay" % order["orderId"], {"card": CARD})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(PayPalPayment.objects.get().status, "failed")
        response = self.post("/api/orders/%s/pay" % order["orderId"], {"card": CARD})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["payment"]["paypal"]["authorizationId"], "AUTH2")

    def test_unlisted_status_is_not_authorized(self):
        order = self.place_order()
        self.stub.on("POST", "/v2/checkout/orders", order_response(order["total"], auth_status="SOMETHING_NEW"))
        response = self.post("/api/orders/%s/pay" % order["orderId"], {"card": CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(PayPalPayment.objects.get().status, "unknown")

    def test_three_d_secure_challenge_is_reported_not_authorized(self):
        order = self.place_order()
        self.stub.on("POST", "/v2/checkout/orders", json_response(200, {"id": "X", "status": "PAYER_ACTION_REQUIRED"}))
        response = self.post("/api/orders/%s/pay" % order["orderId"], {"card": CARD})
        self.assertEqual(response.status_code, 402)
        self.assertIn("3-D Secure", response.json()["error"]["message"])

    def test_amount_mismatch_needs_review(self):
        order = self.place_order()
        self.stub.on("POST", "/v2/checkout/orders", order_response("1.00"))
        response = self.post("/api/orders/%s/pay" % order["orderId"], {"card": CARD})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(PayPalPayment.objects.get().status, "needs_review")

    def test_refused_connection_is_known_but_read_timeout_is_unknown(self):
        order = self.place_order()
        self.stub.on("POST", "/v2/checkout/orders", httpx.ConnectError("refused"))
        unsent = self.post("/api/orders/%s/pay" % order["orderId"], {"card": CARD})
        self.assertEqual(unsent.status_code, 502)
        self.assertNotIn("outcomeUnknown", unsent.json()["error"])
        self.assertEqual(PayPalPayment.objects.get().status, "failed")

        self.stub.routes.clear()
        self.stub.on("POST", "/v2/checkout/orders", httpx.ReadTimeout("no reply"), order_response(order["total"]))
        unknown = self.post("/api/orders/%s/pay" % order["orderId"], {"card": CARD})
        self.assertEqual(unknown.status_code, 504)
        self.assertTrue(unknown.json()["error"]["outcomeUnknown"])
        payment = PayPalPayment.objects.exclude(status="failed").get()
        self.assertEqual(payment.status, "unknown")

        # Retrying resolves it by resending under the SAME PayPal-Request-Id.
        resolved = self.post("/api/orders/%s/pay" % order["orderId"], {"card": CARD})
        self.assertEqual(resolved.status_code, 200, resolved.content)
        calls = self.stub.calls("POST", "/v2/checkout/orders")
        self.assertEqual(calls[-1].headers["paypal-request-id"], calls[-2].headers["paypal-request-id"])
        self.assertEqual(calls[-1].headers["paypal-request-id"], str(payment.reference))

    def test_bad_credentials_are_a_server_side_error(self):
        order = self.place_order()
        stub = self.stub

        def send(request):
            stub.requests.append(request)
            return json_response(401, {"error": "invalid_client", "error_description": "bad"})

        stub.send = send
        response = self.post("/api/orders/%s/pay" % order["orderId"], {"card": CARD})
        self.assertEqual(response.status_code, 502)

    def test_other_shopper_cannot_pay_or_see_order(self):
        order = self.place_order()
        self.as_user(self.other)
        response = self.post("/api/orders/%s/pay" % order["orderId"], {"card": CARD})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.stub.requests, [])

    def test_invalid_card_is_rejected_before_paypal(self):
        order = self.place_order()
        bad = dict(CARD, number="4111111111111112")
        self.assertEqual(self.post("/api/orders/%s/pay" % order["orderId"], {"card": bad}).status_code, 400)
        self.assertEqual(self.stub.requests, [])


class FulfilCancelTests(PayPalApiTestCase):
    def test_fulfil_is_staff_only(self):
        order = self.paid_order()
        self.assertEqual(self.post("/api/orders/%s/fulfil" % order["orderId"]).status_code, 403)
        self.assertEqual(self.post("/api/orders/%s/cancel" % order["orderId"]).status_code, 403)

    def test_fulfil_captures_and_reports_fee_and_net(self):
        order = self.fulfilled_order()
        self.assertEqual(order["status"], "Complete")
        payment = order["payment"]
        self.assertEqual(payment["status"], "captured")
        self.assertEqual(payment["capturedAmount"], "29.68")
        self.assertEqual(payment["paypalFee"], "0.81")
        self.assertEqual(payment["netAmount"], "28.87")
        self.assertEqual(payment["paypal"]["captureId"], "CAP1")
        [call] = self.stub.calls("POST", "/v2/payments/authorizations/AUTH1/capture")
        self.assertEqual(call.body.value, {"final_capture": True})

    def test_fulfil_twice_captures_once(self):
        order = self.fulfilled_order()
        self.as_user(self.staff)
        self.assertEqual(self.post("/api/orders/%s/fulfil" % order["orderId"]).status_code, 200)
        self.assertEqual(len(self.stub.calls("POST", "/capture")), 1)

    def test_stale_authorization_is_renewed_before_capture(self):
        order = self.paid_order()
        PayPalPayment.objects.update(authorized_at=timezone.now() - datetime.timedelta(days=5))
        self.stub.on("POST", "/reauthorize", json_response(201, {
            "id": "AUTH2", "status": "CREATED", "amount": {"currency_code": "USD", "value": order["total"]},
            "create_time": iso(timezone.now()),
            "expiration_time": iso(timezone.now() + datetime.timedelta(days=24)),
        }))
        self.stub.on("POST", "/capture", capture_response(order["total"]))
        self.as_user(self.staff)
        response = self.post("/api/orders/%s/fulfil" % order["orderId"])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(self.stub.calls("POST", "/authorizations/AUTH1/reauthorize")), 1)
        self.assertEqual(len(self.stub.calls("POST", "/authorizations/AUTH2/capture")), 1)
        self.assertEqual(response.json()["payment"]["paypal"]["previousAuthorizationIds"], ["AUTH1"])

    def test_expired_authorization_explains_what_to_do(self):
        order = self.paid_order()
        PayPalPayment.objects.update(authorization_expires_at=timezone.now() - datetime.timedelta(hours=1))
        self.as_user(self.staff)
        response = self.post("/api/orders/%s/fulfil" % order["orderId"])
        self.assertEqual(response.status_code, 409)
        error = response.json()["error"]
        self.assertEqual(error["code"], "authorization_expired")
        self.assertIn("pay again", error["message"])
        self.assertIn("No money was taken", error["message"])
        self.assertEqual(self.stub.calls("POST", "/capture"), [])

    def test_unrenewable_authorization_explains_what_to_do(self):
        order = self.paid_order()
        PayPalPayment.objects.update(authorized_at=timezone.now() - datetime.timedelta(days=10))
        self.stub.on("POST", "/reauthorize", paypal_error(422, "REAUTHORIZATION_NOT_ALLOWED"))
        self.stub.on("POST", "/capture", paypal_error(422, "AUTHORIZATION_EXPIRED"))
        self.stub.on("GET", "/v2/payments/authorizations/AUTH1",
                     json_response(200, {"id": "AUTH1", "status": "CREATED"}))
        self.as_user(self.staff)
        response = self.post("/api/orders/%s/fulfil" % order["orderId"])
        self.assertEqual(response.status_code, 409)
        self.assertIn("REAUTHORIZATION_NOT_ALLOWED", response.json()["error"]["message"])
        self.assertEqual(PayPalPayment.objects.get().status, "expired")

    def test_capture_timeout_is_resent_under_same_request_id(self):
        order = self.paid_order()
        self.stub.on("POST", "/capture", httpx.ReadTimeout("no reply"), capture_response(order["total"]))
        self.as_user(self.staff)
        first = self.post("/api/orders/%s/fulfil" % order["orderId"])
        self.assertEqual(first.status_code, 504)
        PayPalPayment.objects.update(updated_at=timezone.now() - datetime.timedelta(minutes=5))
        second = self.post("/api/orders/%s/fulfil" % order["orderId"])
        self.assertEqual(second.status_code, 200, second.content)
        calls = self.stub.calls("POST", "/capture")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].headers["paypal-request-id"], calls[1].headers["paypal-request-id"])

    def test_cancel_before_fulfilment_voids_the_hold(self):
        order = self.paid_order()
        self.stub.on("POST", "/void", json_response(200, {"id": "AUTH1", "status": "VOIDED"}))
        self.as_user(self.staff)
        response = self.post("/api/orders/%s/cancel" % order["orderId"])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "Cancelled")
        self.assertEqual(response.json()["paymentState"], "voided")
        self.assertEqual(self.stub.calls("POST", "/capture"), [])
        # A second cancel is a no-op.
        self.assertEqual(self.post("/api/orders/%s/cancel" % order["orderId"]).status_code, 200)
        self.assertEqual(len(self.stub.calls("POST", "/void")), 1)

    def test_cancel_after_fulfilment_points_to_refunds(self):
        order = self.fulfilled_order()
        self.as_user(self.staff)
        response = self.post("/api/orders/%s/cancel" % order["orderId"])
        self.assertEqual(response.status_code, 409)


class RefundTests(PayPalApiTestCase):
    def refund(self, order, key, amount=None):
        body = {} if amount is None else {"amount": amount}
        return self.post("/api/orders/%s/refunds" % order["orderId"], body, **{"Idempotency-Key": key})

    def test_partial_refunds_and_limit(self):
        order = self.fulfilled_order()
        self.stub.on("POST", "/refund", refund_response("10.00", "REF1"), refund_response("5.00", "REF2"))
        first = self.refund(order, "k1", "10.00")
        self.assertEqual(first.status_code, 201, first.content)
        self.assertIn("refundId", first.json())
        self.assertEqual(first.json()["paypalRefundId"], "REF1")
        second = self.refund(order, "k2", "5.00")
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(first.json()["refundId"], second.json()["refundId"])
        too_much = self.refund(order, "k3", "20.00")
        self.assertEqual(too_much.status_code, 422)
        self.assertEqual(too_much.json()["error"]["refundableAmount"], "14.68")
        self.assertEqual(len(self.stub.calls("POST", "/refund")), 2)
        payment = PayPalPayment.objects.get()
        self.assertEqual(payment.status, "partially_refunded")
        self.assertEqual(payment.refunded_amount, Decimal("15.00"))

    def test_same_key_refunds_once(self):
        order = self.fulfilled_order()
        self.stub.on("POST", "/refund", refund_response("10.00"))
        a = self.refund(order, "same", "10.00")
        b = self.refund(order, "same", "10.00")
        self.assertEqual(a.json()["refundId"], b.json()["refundId"])
        self.assertEqual(len(self.stub.calls("POST", "/refund")), 1)
        self.assertEqual(self.refund(order, "same", "3.00").status_code, 422)

    def test_full_refund_without_amount(self):
        order = self.fulfilled_order()
        self.stub.on("POST", "/refund", refund_response("29.68"))
        response = self.refund(order, "full")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["amount"], "29.68")
        self.assertEqual(PayPalPayment.objects.get().status, "refunded")
        self.assertEqual(self.refund(order, "again", "0.01").status_code, 422)

    def test_rejected_refund_releases_reservation(self):
        order = self.fulfilled_order()
        self.stub.on("POST", "/refund", paypal_error(422, "REFUND_AMOUNT_EXCEEDED"), refund_response("29.68"))
        self.assertEqual(self.refund(order, "a", "29.68").status_code, 422)
        self.assertEqual(PayPalRefund.objects.get().status, "failed")
        self.assertEqual(self.refund(order, "b", "29.68").status_code, 201)

    def test_idempotency_key_required(self):
        order = self.fulfilled_order()
        response = self.post("/api/orders/%s/refunds" % order["orderId"], {"amount": "1.00"})
        self.assertEqual(response.status_code, 400)

    def test_refund_before_fulfilment_is_refused(self):
        order = self.paid_order()
        self.assertEqual(self.refund(order, "k", "1.00").status_code, 409)

    def test_other_shopper_cannot_refund(self):
        order = self.fulfilled_order()
        self.as_user(self.other)
        self.assertEqual(self.refund(order, "k", "1.00").status_code, 404)


class SavedCardTests(PayPalApiTestCase):
    def save(self, key="card-1"):
        self.stub.on("POST", "/v3/vault/payment-tokens", token_response())
        response = self.post("/api/payment-methods", {"card": CARD}, **{"Idempotency-Key": key})
        self.assertIn(response.status_code, (200, 201), response.content)
        return response.json()

    def test_save_list_reuse_delete(self):
        card = self.save()
        self.assertIn("paymentMethodId", card)
        self.assertEqual((card["brand"], card["lastDigits"]), ("VISA", "1111"))
        self.assertNotIn("number", card)
        listed = self.client.get("/api/payment-methods").json()["paymentMethods"]
        self.assertEqual([c["paymentMethodId"] for c in listed], [card["paymentMethodId"]])

        order = self.place_order()
        self.stub.on("POST", "/v2/checkout/orders", order_response(order["total"]))
        paid = self.post("/api/orders/%s/pay" % order["orderId"], {"paymentMethodId": card["paymentMethodId"]})
        self.assertEqual(paid.status_code, 200, paid.content)
        self.assertEqual(self.stub.calls("POST", "/v2/checkout/orders")[-1].body.value["payment_source"],
                         {"card": {"vault_id": "TOK1"}})

        self.stub.on("DELETE", "/v3/vault/payment-tokens/TOK1", HttpResponse(status_code=204, headers={}))
        self.assertEqual(self.client.delete("/api/payment-methods/%s" % card["paymentMethodId"]).status_code, 204)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        order2 = self.place_order()
        refused = self.post("/api/orders/%s/pay" % order2["orderId"], {"paymentMethodId": card["paymentMethodId"]})
        self.assertEqual(refused.status_code, 422)

    def test_card_number_never_stored(self):
        self.save()
        row = PayPalSavedCard.objects.values().get()
        self.assertNotIn("4111111111111111", json.dumps(row, default=str))
        bankcard = PayPalSavedCard.objects.get().bankcard
        self.assertEqual(bankcard.number, "XXXX-XXXX-XXXX-1111")
        self.assertEqual(bankcard.partner_reference, "TOK1")

    def test_second_card_reuses_paypal_customer(self):
        self.save("a")
        self.stub.routes.clear()
        self.save("b")
        calls = self.stub.calls("POST", "/v3/vault/payment-tokens")
        self.assertNotIn("customer", calls[0].body.value)
        self.assertEqual(calls[1].body.value["customer"], {"id": "CUST1"})

    def test_same_key_saves_once(self):
        first = self.save("dup")
        second = self.save("dup")
        self.assertEqual(first["paymentMethodId"], second["paymentMethodId"])
        self.assertEqual(len(self.stub.calls("POST", "/v3/vault/payment-tokens")), 1)

    def test_cards_are_private_to_their_owner(self):
        card = self.save()
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        self.assertEqual(self.client.delete("/api/payment-methods/%s" % card["paymentMethodId"]).status_code, 404)
        self.as_user(self.shopper)
        order = self.place_order()
        self.as_user(self.other)
        # Even on an order of their own, another shopper cannot pay with it.
        other_order = self.post("/api/orders", {"items": [{"productId": self.product_b.pk, "quantity": 1}]}).json()
        response = self.post("/api/orders/%s/pay" % other_order["orderId"],
                             {"paymentMethodId": card["paymentMethodId"]})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.stub.calls("POST", "/v2/checkout/orders"), [])
        self.assertTrue(order)


class ReconciliationTests(PayPalApiTestCase):
    def search_page(self, page, total_pages, txns):
        return json_response(200, {
            "transaction_details": [{"transaction_info": t} for t in txns],
            "page": page, "total_pages": total_pages,
            "last_refreshed_datetime": iso(timezone.now() + datetime.timedelta(hours=1)),
        })

    def test_staff_only(self):
        self.assertEqual(self.client.get("/api/reconciliation?from=2026-01-01T00:00:00Z&to=2026-01-02T00:00:00Z")
                         .status_code, 403)

    def test_lines_up_both_sides_across_pages(self):
        order = self.fulfilled_order()
        now = timezone.now()
        ours = {"transaction_id": "CAP1", "transaction_event_code": "T0006", "transaction_status": "S",
                "transaction_initiation_date": iso(now), "invoice_id": PayPalPayment.objects.get().invoice_id,
                "transaction_amount": {"currency_code": "USD", "value": "29.68"}}
        theirs = {"transaction_id": "STRANGER", "transaction_event_code": "T0006", "transaction_status": "S",
                  "transaction_initiation_date": iso(now), "invoice_id": "NOT-OURS",
                  "transaction_amount": {"currency_code": "USD", "value": "7.00"}}
        self.stub.on("GET", "/v1/reporting/transactions",
                     self.search_page(1, 2, [theirs]), self.search_page(2, 2, [ours]))
        self.as_user(self.staff)
        start = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        response = self.client.get("/api/reconciliation", {"from": start, "to": end})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual(len(self.stub.calls("GET", "/v1/reporting/transactions")), 2)
        self.assertEqual(report["summary"]["matched"], 1)
        self.assertEqual(report["matched"][0]["local"]["orderId"], order["orderId"])
        self.assertTrue(report["matched"][0]["amountsAgree"])
        self.assertEqual([t["transactionId"] for t in report["paypalOnly"]], ["STRANGER"])
        self.assertFalse(report["truncated"])

    def test_long_range_is_split_into_31_day_windows(self):
        self.stub.on("GET", "/v1/reporting/transactions", self.search_page(1, 1, []))
        self.as_user(self.staff)
        response = self.client.get("/api/reconciliation",
                                   {"from": "2026-01-01T00:00:00Z", "to": "2026-03-15T00:00:00Z"})
        self.assertEqual(response.status_code, 200)
        calls = self.stub.calls("GET", "/v1/reporting/transactions")
        self.assertEqual(len(calls), 3)
        self.assertIn("start_date=2026-01-01T00%3A00%3A00Z", calls[0].url)

    def test_app_payment_missing_at_paypal_is_visible(self):
        self.fulfilled_order()
        PayPalPayment.objects.update(captured_at=timezone.now() - datetime.timedelta(days=3))
        self.stub.on("GET", "/v1/reporting/transactions", self.search_page(1, 1, []))
        self.as_user(self.staff)
        now = timezone.now()
        response = self.client.get("/api/reconciliation", {
            "from": (now - datetime.timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
        report = response.json()
        self.assertEqual(report["summary"]["appOnly"] + report["summary"]["notYetReportedByPaypal"], 1)

    def test_bad_range(self):
        self.as_user(self.staff)
        self.assertEqual(self.client.get("/api/reconciliation?from=nope&to=2026-01-01T00:00:00Z").status_code, 400)


@override_settings(PAYPAL_CURRENCY="USD")
class ConcurrencyTests(TransactionTestCase):
    """The claim row is committed before PayPal is called (no request-wide transaction)."""

    def test_claim_survives_a_failed_call(self):
        stub = StubPayPal()
        stub.on("POST", "/v2/checkout/orders", httpx.ReadTimeout("no reply"))
        paypal_client.set_client(PaypalClient(
            base_url="https://paypal.test", custom_http_client=stub,
            oauth2=ClientCredentials(client_id="id", client_secret="secret")))
        self.addCleanup(paypal_client.set_client, None)
        user = User.objects.create_user("s", "s@example.com", "pass-word-123")
        product = create_product(price=Decimal("3.00"), num_in_stock=5)
        self.client.force_login(user)
        order = self.client.post("/api/orders", data=json.dumps({"items": [{"productId": product.pk}]}),
                                 content_type="application/json").json()
        response = self.client.post("/api/orders/%s/pay" % order["orderId"], data=json.dumps({"card": CARD}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 504)
        self.assertEqual(PayPalPayment.objects.get().status, "unknown")


class ConfigurationTests(TestCase):
    @override_settings(PAYPAL_CLIENT_ID="", PAYPAL_CLIENT_SECRET="x")
    def test_missing_credentials_are_named(self):
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaisesMessage(ImproperlyConfigured, "PAYPAL_CLIENT_ID"):
            paypal_client.build_client()

    @override_settings(PAYPAL_BASE_URL="https://mock.example/", PAYPAL_ENVIRONMENT="live")
    def test_base_url_override_wins(self):
        self.assertEqual(paypal_client.resolve_base_url(), "https://mock.example/")

    @override_settings(PAYPAL_BASE_URL="", PAYPAL_ENVIRONMENT="live")
    def test_unknown_environment_requires_base_url(self):
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaisesMessage(ImproperlyConfigured, "PAYPAL_BASE_URL"):
            paypal_client.resolve_base_url()

    @override_settings(PAYPAL_BASE_URL="", PAYPAL_ENVIRONMENT="sandbox")
    def test_sandbox_host(self):
        self.assertEqual(paypal_client.resolve_base_url(), "https://api-m.sandbox.paypal.com")
