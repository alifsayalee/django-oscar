"""
Tests for the PayPal payments API.

PayPal is faked at the SDK's transport seam (a stub ``HttpClient`` passed as
``custom_http_client``), so the real SDK builds and decodes every request.
Run with:  cd sandbox && python manage.py test apps.paypal_payments
"""

import json
import re
from datetime import timedelta
from decimal import Decimal as D

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from oscar.test.factories import create_product
from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpRequest, HttpResponse, OAuthToken

from . import gateway
from .models import OrderPayment, PayPalRefund, ProviderWrite, SavedCard

CARD = {
    "number": "4111 1111 1111 1111",
    "expiry": "2030-12",
    "securityCode": "123",
    "name": "Test Shopper",
    "billingAddress": {"line1": "1 Main St", "city": "San Jose", "state": "CA", "postcode": "95131", "countryCode": "US"},
}


def json_response(status, body):
    return HttpResponse(status_code=status, headers={"content-type": "application/json"}, content=json.dumps(body).encode())


class StubTransport:
    """The SDK's sync transport protocol: answers queued responses in order, or raises queued exceptions."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[HttpRequest] = []

    def queue(self, *responses):
        self.responses.extend(responses)

    def send(self, request):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("Unexpected PayPal call: %s %s" % (request.method, request.url))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self):
        pass

    def paths(self):
        return ["%s %s" % (r.method, httpx.URL(r.url).path) for r in self.requests]


class StubTokenSource:
    def fetch(self, credentials):
        return OAuthToken(access_token="t", token_type="Bearer")


def authorization(auth_id="AUTH1", status="CREATED", value="20.00", created=None):
    created = created or timezone.now()
    return {
        "id": auth_id,
        "status": status,
        "amount": {"currency_code": "USD", "value": value},
        "create_time": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiration_time": (created + timedelta(days=29)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def order_body(auth=None, status="COMPLETED", value="20.00", order_id="PPORDER1"):
    unit = {"reference_id": "default", "amount": {"currency_code": "USD", "value": value}}
    if auth is not None:
        unit["payments"] = {"authorizations": [auth]}
    return {
        "id": order_id,
        "status": status,
        "create_time": "2026-09-28T07:26:18Z",
        "payment_source": {"card": {"last_digits": "1111", "brand": "VISA"}},
        "purchase_units": [unit],
    }


def capture_body(capture_id="CAP1", status="COMPLETED", value="20.00", fee="1.01", net="18.99"):
    return {
        "id": capture_id,
        "status": status,
        "amount": {"currency_code": "USD", "value": value},
        "create_time": "2026-09-28T08:00:00Z",
        "seller_receivable_breakdown": {
            "gross_amount": {"currency_code": "USD", "value": value},
            "paypal_fee": {"currency_code": "USD", "value": fee},
            "net_amount": {"currency_code": "USD", "value": net},
        },
    }


def refund_body(refund_id="REF1", status="COMPLETED", value="5.00"):
    return {"id": refund_id, "status": status, "amount": {"currency_code": "USD", "value": value},
            "create_time": "2026-09-28T09:00:00Z"}


@override_settings(
    PAYPAL_CLIENT_ID="test-client",
    PAYPAL_CLIENT_SECRET="test-secret",
    PAYPAL_ENVIRONMENT="sandbox",
    PAYPAL_BASE_URL="",
    PAYPAL_CURRENCY="USD",
    PAYPAL_REFERENCE_PREFIX="test",
)
class PayPalApiTestCase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "pass-word-123")
        self.other = User.objects.create_user("other", "other@example.com", "pass-word-123")
        self.operator = User.objects.create_user("operator", "operator@example.com", "pass-word-123", is_staff=True)
        self.product = create_product(price=D("10.00"), num_in_stock=100)
        self.transport = StubTransport()
        self.paypal = PaypalClient(
            custom_http_client=self.transport,
            oauth2=ClientCredentials(client_id="test-client", client_secret="test-secret"),
            oauth2_token_source=StubTokenSource(),
        )
        override = gateway.client_override(self.paypal)
        override.__enter__()
        self.addCleanup(override.__exit__, None, None, None)

    # helpers

    def as_user(self, user):
        self.client.force_login(user)

    def post(self, url, body=None, **headers):
        return self.client.post(url, data=json.dumps(body or {}), content_type="application/json", headers=headers)

    def place_order(self, quantity=2):
        self.as_user(self.shopper)
        response = self.post("/api/orders", {"items": [{"productId": self.product.pk, "quantity": quantity}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["orderId"]

    def authorized_order(self):
        number = self.place_order()
        self.transport.queue(json_response(201, order_body(authorization())))
        response = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(response.status_code, 200, response.content)
        return number

    def fulfilled_order(self):
        number = self.authorized_order()
        self.as_user(self.operator)
        self.transport.queue(json_response(200, authorization()), json_response(201, capture_body()))
        response = self.post("/api/orders/%s/fulfil" % number)
        self.assertEqual(response.status_code, 200, response.content)
        self.as_user(self.shopper)
        return number


class OrderAndPayTests(PayPalApiTestCase):
    def test_order_starts_awaiting_payment_with_catalogue_total(self):
        number = self.place_order(quantity=2)
        order = self.client.get("/api/my-orders").json()["orders"][0]
        self.assertEqual(order["orderId"], number)
        self.assertEqual(order["status"], "Awaiting payment")
        self.assertEqual((order["total"], order["currency"]), ("20.00", "USD"))
        self.assertEqual(order["payment"]["state"], "awaiting_payment")

    def test_the_same_pay_twice_makes_one_provider_call_under_a_derived_reference(self):
        number = self.place_order()
        self.transport.queue(json_response(201, order_body(authorization())))
        first = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        second = self.post("/api/orders/%s/pay" % number, {"card": CARD})

        self.assertEqual(len(self.transport.requests), 1)
        sent = self.transport.requests[0]
        ref = "test:order:%s:a1:create" % number
        self.assertEqual(sent.headers["paypal-request-id"], gateway.request_id_for(ref))
        self.assertEqual(sent.body.value["intent"], "AUTHORIZE")
        self.assertEqual(sent.body.value["purchase_units"][0]["amount"], {"currency_code": "USD", "value": "20.00"})
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(second.json()["order"]["payment"]["authorization"]["id"], "AUTH1")
        self.assertEqual(first.json()["order"]["status"], "Payment authorised")

    def test_card_details_are_never_stored(self):
        number = self.place_order()
        self.transport.queue(json_response(201, order_body(authorization())))
        self.post("/api/orders/%s/pay" % number, {"card": CARD})
        for write in ProviderWrite.objects.all():
            self.assertNotIn("4111111111111111", json.dumps(write.data) + write.detail + write.ref)
        payment = OrderPayment.objects.get()
        self.assertEqual(payment.card_last_digits, "1111")

    def test_a_denied_authorization_is_not_done(self):
        number = self.place_order()
        self.transport.queue(json_response(201, order_body(authorization(status="DENIED"))))
        response = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()["outcome"], "failed")
        self.assertEqual(response.json()["order"]["status"], "Awaiting payment")

        # a new attempt goes out under a new reference
        self.transport.queue(json_response(201, order_body(authorization(auth_id="AUTH2"))))
        retry = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(
            self.transport.requests[-1].headers["paypal-request-id"],
            gateway.request_id_for("test:order:%s:a2:create" % number),
        )

    def test_an_unlisted_status_is_unknown_not_done(self):
        number = self.place_order()
        self.transport.queue(json_response(201, order_body(authorization(status="SOMETHING_NEW"))))
        response = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(response.status_code, 504)
        self.assertEqual(OrderPayment.objects.get().state, "awaiting_payment")

    def test_a_truncated_success_body_is_not_success(self):
        number = self.place_order()
        self.transport.queue(json_response(201, {}))
        response = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(response.status_code, 504)
        self.assertNotEqual(OrderPayment.objects.get().state, "authorized")

    def test_a_different_amount_from_paypal_needs_review(self):
        number = self.place_order()
        self.transport.queue(json_response(201, order_body(authorization(value="2.00"))))
        response = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(ProviderWrite.objects.get().outcome, "needs_review")

    def test_payer_action_required_is_reported_not_approved(self):
        number = self.place_order()
        self.transport.queue(json_response(201, order_body(None, status="PAYER_ACTION_REQUIRED")))
        response = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(response.status_code, 402)
        self.assertIn("3-D Secure", response.json()["message"])

    def test_refused_connection_is_known_and_read_timeout_is_unknown(self):
        number = self.place_order()
        self.transport.queue(httpx.ConnectError("refused"))
        unsent = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual((unsent.status_code, unsent.json()["error"]["outcomeUnknown"]), (502, False))
        self.assertFalse(ProviderWrite.objects.exists())  # nothing happened: the claim is released

        self.transport.queue(httpx.ReadTimeout("no reply"), httpx.ReadTimeout("no reply"))
        unknown = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual((unknown.status_code, unknown.json()["error"]["outcomeUnknown"]), (504, True))
        self.assertEqual(ProviderWrite.objects.get().outcome, "unknown")
        # the check resent under the SAME reference
        ids = {r.headers["paypal-request-id"] for r in self.transport.requests}
        self.assertEqual(len(ids), 1)

        # a later repeat settles it by the same reference, never a new one
        self.transport.queue(json_response(201, order_body(authorization())))
        settled = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(settled.status_code, 200)
        self.assertEqual({r.headers["paypal-request-id"] for r in self.transport.requests}, ids)

    def test_bad_credentials_are_a_server_fault(self):
        transport = StubTransport(json_response(401, {"error": "invalid_client"}))
        client = PaypalClient(custom_http_client=transport, oauth2=ClientCredentials(client_id="x", client_secret="y"))
        number = self.place_order()
        with gateway.client_override(client):
            response = self.post("/api/orders/%s/pay" % number, {"card": CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "paypal_auth_failed")

    def test_invalid_card_is_rejected_before_paypal(self):
        number = self.place_order()
        response = self.post("/api/orders/%s/pay" % number, {"card": {**CARD, "number": "4111111111111112"}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.transport.requests, [])


class FulfilCancelTests(PayPalApiTestCase):
    def test_fulfil_captures_and_records_fee_and_net(self):
        number = self.fulfilled_order()
        payment = self.client.get("/api/my-orders").json()["orders"][0]["payment"]
        self.assertEqual(payment["state"], "captured")
        self.assertEqual(
            (payment["capture"]["amount"], payment["capture"]["paypalFee"], payment["capture"]["netAmount"]),
            ("20.00", "1.01", "18.99"),
        )
        self.assertEqual(self.transport.paths()[-1], "POST /v2/payments/authorizations/AUTH1/capture")

    def test_a_stale_authorization_is_reauthorized_before_capture(self):
        number = self.authorized_order()
        old = timezone.now() - timedelta(days=5)
        OrderPayment.objects.update(authorization_created_at=old, original_authorization_at=old)
        self.as_user(self.operator)
        self.transport.queue(
            json_response(200, authorization(created=old)),
            json_response(201, authorization(auth_id="AUTH2")),
            json_response(201, capture_body()),
        )
        response = self.post("/api/orders/%s/fulfil" % number)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            self.transport.paths()[-2:],
            ["POST /v2/payments/authorizations/AUTH1/reauthorize", "POST /v2/payments/authorizations/AUTH2/capture"],
        )
        self.assertTrue(response.json()["order"]["payment"]["authorization"]["reauthorized"])

    def test_an_authorization_that_cannot_be_renewed_tells_the_operator_what_to_do(self):
        number = self.authorized_order()
        old = timezone.now() - timedelta(days=5)
        OrderPayment.objects.update(authorization_created_at=old, original_authorization_at=old, reauthorized=True)
        self.as_user(self.operator)
        self.transport.queue(json_response(200, authorization(created=old)))
        response = self.post("/api/orders/%s/fulfil" % number)
        self.assertEqual(response.status_code, 409)
        error = response.json()["error"]
        self.assertEqual(error["code"], "authorization_not_renewable")
        self.assertIn("ask the shopper to pay again", error["message"])
        self.assertEqual(len([p for p in self.transport.paths() if p.endswith("/capture")]), 0)

        # the shopper can pay again under a new attempt, and the operator can then fulfil
        self.as_user(self.shopper)
        self.transport.queue(json_response(201, order_body(authorization(auth_id="AUTH9"))))
        self.assertEqual(self.post("/api/orders/%s/pay" % number, {"card": CARD}).status_code, 200)

    def test_a_refused_reauthorization_is_reported_to_the_operator(self):
        number = self.authorized_order()
        old = timezone.now() - timedelta(days=5)
        OrderPayment.objects.update(authorization_created_at=old, original_authorization_at=old)
        self.as_user(self.operator)
        self.transport.queue(
            json_response(200, authorization(created=old)),
            json_response(422, {"name": "UNPROCESSABLE_ENTITY", "message": "m", "debug_id": "d",
                                "details": [{"issue": "REAUTHORIZATION_TOO_SOON", "description": "too soon"}]}),
        )
        response = self.post("/api/orders/%s/fulfil" % number)
        self.assertEqual(response.status_code, 409)
        self.assertIn("REAUTHORIZATION_TOO_SOON", response.json()["error"]["message"])

    def test_fulfil_twice_captures_once(self):
        number = self.fulfilled_order()
        calls = len(self.transport.requests)
        self.as_user(self.operator)
        self.assertEqual(self.post("/api/orders/%s/fulfil" % number).status_code, 200)
        self.assertEqual(len(self.transport.requests), calls)

    def test_cancel_before_fulfilment_voids_the_hold(self):
        number = self.authorized_order()
        self.as_user(self.operator)
        self.transport.queue(json_response(200, authorization(status="VOIDED")))
        response = self.post("/api/orders/%s/cancel" % number)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["order"]["status"], "Cancelled")
        self.assertEqual(response.json()["order"]["payment"]["state"], "voided")
        self.assertEqual(self.transport.paths()[-1], "POST /v2/payments/authorizations/AUTH1/void")

    def test_cancel_after_capture_is_refused(self):
        number = self.fulfilled_order()
        self.as_user(self.operator)
        response = self.post("/api/orders/%s/cancel" % number)
        self.assertEqual(response.status_code, 409)

    def test_operator_actions_need_staff(self):
        number = self.authorized_order()
        for url in ("/api/orders/%s/fulfil" % number, "/api/orders/%s/cancel" % number):
            self.assertEqual(self.post(url).status_code, 403)
        self.assertEqual(self.client.get("/api/reconciliation?from=2026-09-01T00:00:00Z&to=2026-09-02T00:00:00Z").status_code, 403)


class RefundTests(PayPalApiTestCase):
    def test_partial_refunds_never_exceed_the_capture(self):
        number = self.fulfilled_order()
        url = "/api/orders/%s/refunds" % number
        self.transport.queue(json_response(201, refund_body("REF1", value="15.00")))
        first = self.post(url, {"amount": "15.00"}, **{"Idempotency-Key": "r-1"})
        self.assertEqual(first.status_code, 201, first.content)
        self.assertIn("refundId", first.json())
        calls = len(self.transport.requests)

        over = self.post(url, {"amount": "10.00"}, **{"Idempotency-Key": "r-2"})
        self.assertEqual(over.status_code, 422)
        self.assertEqual(over.json()["error"]["refundable"], "5.00")
        self.assertEqual(len(self.transport.requests), calls)  # refused locally

        self.transport.queue(json_response(201, refund_body("REF2", value="5.00")))
        rest = self.post(url, {"amount": "5.00"}, **{"Idempotency-Key": "r-3"})
        self.assertEqual(rest.status_code, 201)
        self.assertEqual(rest.json()["order"]["payment"]["state"], "refunded")
        self.assertEqual(PayPalRefund.objects.filter(outcome="done").count(), 2)

    def test_the_same_key_refunds_once(self):
        number = self.fulfilled_order()
        url = "/api/orders/%s/refunds" % number
        self.transport.queue(json_response(201, refund_body()))
        first = self.post(url, {"amount": "5.00"}, **{"Idempotency-Key": "same"})
        second = self.post(url, {"amount": "5.00"}, **{"Idempotency-Key": "same"})
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertEqual(first.json()["refundId"], second.json()["refundId"])
        self.assertEqual(len([p for p in self.transport.paths() if p.endswith("/refund")]), 1)
        self.assertEqual(OrderPayment.objects.get().refunded_amount, D("5.00"))

    def test_refund_needs_an_idempotency_key(self):
        number = self.fulfilled_order()
        self.assertEqual(self.post("/api/orders/%s/refunds" % number, {"amount": "1.00"}).status_code, 400)

    def test_a_pending_refund_is_not_reported_done(self):
        number = self.fulfilled_order()
        self.transport.queue(json_response(201, refund_body(status="PENDING")))
        response = self.post("/api/orders/%s/refunds" % number, {"amount": "5.00"}, **{"Idempotency-Key": "p"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(OrderPayment.objects.get().refunded_amount, D("0.00"))


class OwnershipTests(PayPalApiTestCase):
    def test_one_shopper_cannot_see_or_act_on_anothers_order(self):
        number = self.authorized_order()
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"], [])
        self.assertEqual(self.post("/api/orders/%s/pay" % number, {"card": CARD}).status_code, 404)
        self.assertEqual(self.post("/api/orders/%s/refunds" % number, {}, **{"Idempotency-Key": "k"}).status_code, 404)

    def test_anonymous_callers_are_refused(self):
        self.assertEqual(self.client.get("/api/my-orders").status_code, 401)
        self.assertEqual(self.post("/api/orders", {"items": []}).status_code, 401)


class SavedCardTests(PayPalApiTestCase):
    def save_card(self, key="card-1"):
        self.as_user(self.shopper)
        self.transport.queue(json_response(201, {
            "id": "TOKEN1",
            "customer": {"id": "CUST1"},
            "payment_source": {"card": {"brand": "VISA", "last_digits": "1111", "expiry": "2030-12"}},
            "create_time": "2026-09-28T07:26:39.212Z",
        }))
        response = self.post("/api/payment-methods", {"card": CARD}, **{"Idempotency-Key": key})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["paymentMethodId"]

    def test_save_list_pay_and_delete(self):
        card_id = self.save_card()
        self.assertNotIn("number", json.dumps(self.transport.requests[-1].body.value).replace('"number"', ""))
        cards = self.client.get("/api/payment-methods").json()["paymentMethods"]
        self.assertEqual([(c["paymentMethodId"], c["brand"], c["lastDigits"]) for c in cards], [(card_id, "VISA", "1111")])
        self.assertNotIn("TOKEN1", json.dumps(cards))

        number = self.place_order()
        self.transport.queue(json_response(201, order_body(authorization())))
        paid = self.post("/api/orders/%s/pay" % number, {"paymentMethodId": card_id})
        self.assertEqual(paid.status_code, 200, paid.content)
        self.assertEqual(self.transport.requests[-1].body.value["payment_source"], {"card": {"vault_id": "TOKEN1"}})

        self.transport.queue(HttpResponse(status_code=204, headers={}))
        deleted = self.client.delete("/api/payment-methods/%s" % card_id)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["providerDeletion"], "done")
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])

        second = self.place_order()
        self.assertEqual(self.post("/api/orders/%s/pay" % second, {"paymentMethodId": card_id}).status_code, 404)

    def test_the_same_key_saves_once(self):
        card_id = self.save_card("dup")
        calls = len(self.transport.requests)
        again = self.post("/api/payment-methods", {"card": CARD}, **{"Idempotency-Key": "dup"})
        self.assertEqual(again.json()["paymentMethodId"], card_id)
        self.assertEqual(len(self.transport.requests), calls)
        self.assertEqual(SavedCard.objects.count(), 1)

    def test_a_card_belongs_to_its_shopper(self):
        card_id = self.save_card()
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        self.assertEqual(self.client.delete("/api/payment-methods/%s" % card_id).status_code, 404)
        number = self.place_order_as(self.other)
        self.assertEqual(self.post("/api/orders/%s/pay" % number, {"paymentMethodId": card_id}).status_code, 404)

    def test_provider_delete_failure_still_removes_the_card(self):
        card_id = self.save_card()
        self.transport.queue(httpx.ConnectError("down"))
        deleted = self.client.delete("/api/payment-methods/%s" % card_id)
        self.assertEqual((deleted.status_code, deleted.json()["providerDeletion"]), (200, "pending"))
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])

    def place_order_as(self, user):
        self.as_user(user)
        response = self.post("/api/orders", {"items": [{"productId": self.product.pk, "quantity": 1}]})
        return response.json()["orderId"]


class ReconciliationTests(PayPalApiTestCase):
    def test_every_page_is_read_and_both_sides_are_lined_up(self):
        number = self.fulfilled_order()
        capture = ProviderWrite.objects.get(operation="capture")
        start = (capture.provider_time - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = (capture.provider_time + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def page(n, total, txns):
            return json_response(200, {
                "transaction_details": [{"transaction_info": t} for t in txns],
                "page": n, "total_pages": total, "last_refreshed_datetime": "2026-09-29T00:00:00Z",
            })

        self.transport.queue(
            page(1, 2, [{"transaction_id": "CAP1", "transaction_amount": {"currency_code": "USD", "value": "20.00"}}]),
            page(2, 2, [{"transaction_id": "STRANGER", "transaction_amount": {"currency_code": "USD", "value": "7.00"}}]),
        )
        self.as_user(self.operator)
        report = self.client.get("/api/reconciliation", {"from": start, "to": end}).json()
        self.assertEqual(report["pagesFetched"], 2)
        self.assertEqual([m["local"]["paypalId"] for m in report["matched"]], ["CAP1"])
        self.assertEqual([p["transactionId"] for p in report["paypalOnly"]], ["STRANGER"])
        self.assertEqual(report["matched"][0]["local"]["orderId"], number)
        self.assertTrue(all(re.search(r"page=\d", r.url) for r in self.transport.requests[-2:]))

    def test_invalid_range_is_rejected(self):
        self.as_user(self.operator)
        response = self.client.get("/api/reconciliation", {"from": "2026-09-02T00:00:00Z", "to": "2026-09-01T00:00:00Z"})
        self.assertEqual(response.status_code, 400)
