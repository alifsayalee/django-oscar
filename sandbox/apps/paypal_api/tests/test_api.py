import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from oscar.test.factories import create_product

from apps.paypal_api import gateway
from apps.paypal_api.models import PayPalOperation, PayPalPayment
from apps.paypal_api.views import answer

from . import stubs
from .stubs import RouterTransport, json_response, paypal_error

CARD = {"number": "4111 1111 1111 1111", "expiry": "2030-12", "securityCode": "123", "name": "A Shopper"}
ORDERS = r"/v2/checkout/orders"
AUTHS = r"/v2/payments/authorizations/[^/]+"
SEARCH = r"/v1/reporting/transactions"

PAYPAL_SETTINGS = dict(
    PAYPAL_CLIENT_ID="id", PAYPAL_CLIENT_SECRET="secret", PAYPAL_ENVIRONMENT="sandbox",
    PAYPAL_CURRENCY="USD", PAYPAL_BASE_URL="", PAYPAL_REFERENCE_PREFIX="test",
)


@override_settings(**PAYPAL_SETTINGS)
class ApiTestCase(TestCase):
    def setUp(self) -> None:
        self.paypal = RouterTransport()
        gateway.set_client(stubs.client_over(self.paypal))
        self.addCleanup(gateway.set_client, None)
        User = get_user_model()
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "pw-shopper-1")
        self.other = User.objects.create_user("other", "other@example.com", "pw-other-1")
        self.staff = User.objects.create_user("op", "op@example.com", "pw-op-1", is_staff=True)
        self.product = create_product(price=Decimal("10.00"), num_in_stock=50)

    def as_user(self, user: Any) -> None:
        self.client.force_login(user)

    def post(self, url: str, body: dict[str, Any] | None = None, **headers: str) -> Any:
        return self.client.post(url, data=json.dumps(body or {}), content_type="application/json", headers=headers)

    def place(self, quantity: int = 2) -> str:
        self.as_user(self.shopper)
        response = self.post("/api/orders", {"items": [{"productId": self.product.pk, "quantity": quantity}]})
        self.assertEqual(response.status_code, 201, response.content)
        return str(response.json()["orderId"])

    def authorized_order(self, **auth: Any) -> str:
        number = self.place()
        self.paypal.on("POST", ORDERS, json_response(201, stubs.created_order(stubs.authorization(**auth))))
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 200, response.content)
        return number

    def captured_order(self) -> str:
        number = self.authorized_order()
        self.paypal.on("GET", AUTHS, json_response(200, stubs.authorization()))
        self.paypal.on("POST", AUTHS + "/capture", json_response(201, stubs.capture()))
        self.as_user(self.staff)
        self.assertEqual(self.post(f"/api/orders/{number}/fulfil").status_code, 200)
        self.as_user(self.shopper)
        return number


class OrderPlacementTests(ApiTestCase):
    def test_order_starts_awaiting_payment_with_catalogue_amount_and_configured_currency(self) -> None:
        number = self.place(quantity=2)
        order = self.client.get("/api/my-orders").json()["orders"][0]
        self.assertEqual(order["orderId"], number)
        self.assertEqual(order["status"], "Awaiting payment")
        self.assertEqual(order["currency"], "USD")
        self.assertEqual(order["payment"]["amount"], "20.00")
        self.assertEqual(order["payment"]["status"], "awaiting_payment")

    def test_unknown_product_is_rejected(self) -> None:
        self.as_user(self.shopper)
        response = self.post("/api/orders", {"items": [{"productId": 999999, "quantity": 1}]})
        self.assertEqual(response.status_code, 400)

    def test_login_required(self) -> None:
        self.assertEqual(self.client.get("/api/my-orders").status_code, 401)


class PayTests(ApiTestCase):
    def test_the_same_pay_twice_sends_one_reference_and_one_call(self) -> None:
        number = self.place()
        self.paypal.on("POST", ORDERS, json_response(201, stubs.created_order(stubs.authorization())))

        first = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        second = self.post(f"/api/orders/{number}/pay", {"card": CARD})

        creates = self.paypal.calls("POST", ORDERS)
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0].headers["paypal-request-id"], f"test:order:{number}:authorize:1")
        body = stubs.body_of(creates[0])
        self.assertEqual(body["intent"], "AUTHORIZE")
        self.assertEqual(body["purchase_units"][0]["amount"], {"currency_code": "USD", "value": "20.00"})
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(second.json()["order"]["payment"]["status"], "authorized")
        self.assertEqual(second.json()["order"]["status"], "Pending")

    def test_denied_authorization_is_failed_and_a_retry_uses_a_new_reference(self) -> None:
        number = self.place()
        self.paypal.on("POST", ORDERS,
                       json_response(201, stubs.created_order(stubs.authorization(status="DENIED"))),
                       json_response(201, stubs.created_order(stubs.authorization(auth_id="AUTH2"))))
        declined = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(declined.status_code, 409)
        self.assertEqual(declined.json()["order"]["payment"]["status"], "awaiting_payment")

        retried = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(retried.status_code, 200)
        refs = [r.headers["paypal-request-id"] for r in self.paypal.calls("POST", ORDERS)]
        self.assertEqual(refs, [f"test:order:{number}:authorize:1", f"test:order:{number}:authorize:2"])

    def test_an_unlisted_status_is_unknown_not_done(self) -> None:
        number = self.place()
        self.paypal.on("POST", ORDERS,
                       json_response(201, stubs.created_order(stubs.authorization(status="SOMETHING_NEW"))))
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 504)
        self.assertTrue(response.json()["outcomeUnknown"])
        self.assertEqual(PayPalPayment.objects.get(order__number=number).state, PayPalPayment.AWAITING)

    def test_a_different_echoed_amount_needs_review(self) -> None:
        number = self.place()
        self.paypal.on("POST", ORDERS,
                       json_response(201, stubs.created_order(stubs.authorization(value="2.00"))))
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "needs_review")
        self.assertEqual(PayPalOperation.objects.get(kind="authorize").outcome, "needs_review")

    def test_unsent_is_not_the_same_failure_as_unknown(self) -> None:
        number = self.place()
        self.paypal.on("POST", ORDERS, httpx.ConnectError("refused"))
        unsent = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(unsent.status_code, 502)
        self.assertNotIn("outcomeUnknown", unsent.json()["error"])
        self.assertEqual(self.paypal.calls("GET", SEARCH), [])  # nothing to look up

        self.paypal.on("POST", ORDERS, httpx.ReadTimeout("no reply"))
        self.paypal.on("GET", SEARCH, json_response(200, {"transaction_details": [], "total_pages": 1}))
        unknown = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(unknown.status_code, 504)
        self.assertTrue(unknown.json()["error"]["outcomeUnknown"])
        self.assertEqual(len(self.paypal.calls("GET", SEARCH)), 1)  # looked up by the invoice id
        self.assertEqual(PayPalOperation.objects.get(attempt=2).outcome, "unknown")

        # A repeat never creates again: it only looks up, and settles from PayPal's record.
        found = {"transaction_details": [{"transaction_info": {
            "transaction_id": "AUTH9", "invoice_id": f"test-{number}-2",
            "transaction_initiation_date": "2026-09-27T10:00:00Z"}}], "total_pages": 1}
        self.paypal.on("GET", SEARCH, json_response(200, found))
        self.paypal.on("GET", AUTHS, json_response(200, stubs.authorization(auth_id="AUTH9")))
        settled = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(settled.status_code, 200, settled.content)
        self.assertEqual(len(self.paypal.calls("POST", ORDERS)), 2)
        self.assertEqual(settled.json()["order"]["payment"]["authorization"]["id"], "AUTH9")

    def test_a_card_refusal_is_the_callers_error(self) -> None:
        number = self.place()
        self.paypal.on("POST", ORDERS, paypal_error(422, "TRANSACTION_REFUSED", "The request was refused"))
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 422)
        self.assertIn("refused", response.json()["error"]["message"])

    def test_another_shopper_cannot_see_or_pay_the_order(self) -> None:
        number = self.place()
        self.as_user(self.other)
        self.assertEqual(self.post(f"/api/orders/{number}/pay", {"card": CARD}).status_code, 404)
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"], [])
        self.assertEqual(self.paypal.requests, [])

    def test_invalid_card_never_reaches_paypal(self) -> None:
        number = self.place()
        response = self.post(f"/api/orders/{number}/pay", {"card": {**CARD, "number": "4111111111111112"}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.paypal.requests, [])

    def test_bad_credentials_are_a_site_error(self) -> None:
        number = self.place()
        self.paypal.token_response = json_response(401, {"error": "invalid_client"})
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 502)


class FulfilCancelTests(ApiTestCase):
    def test_fulfil_is_staff_only(self) -> None:
        number = self.authorized_order()
        self.assertEqual(self.post(f"/api/orders/{number}/fulfil").status_code, 403)
        self.assertEqual(self.post(f"/api/orders/{number}/cancel").status_code, 403)

    def test_fulfil_captures_and_records_fee_and_net(self) -> None:
        number = self.captured_order()
        payment = self.client.get("/api/my-orders").json()["orders"][0]["payment"]
        self.assertEqual(payment["status"], "captured")
        self.assertEqual(payment["capture"], {**payment["capture"], "amount": "20.00", "paypalFee": "0.88",
                                              "netAmount": "19.12", "id": "CAP1"})
        capture_call = self.paypal.calls("POST", AUTHS + "/capture")[0]
        self.assertEqual(stubs.body_of(capture_call)["amount"], {"currency_code": "USD", "value": "20.00"})
        self.assertEqual(capture_call.headers["prefer"], "return=representation")
        # A double-click on fulfil captures once
        self.as_user(self.staff)
        self.assertEqual(self.post(f"/api/orders/{number}/fulfil").status_code, 200)
        self.assertEqual(len(self.paypal.calls("POST", AUTHS + "/capture")), 1)

    def test_stale_authorization_is_renewed_before_capture(self) -> None:
        created = (timezone.now() - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        expires = (timezone.now() + timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        number = self.authorized_order(created=created, expires=expires)
        self.paypal.on("GET", AUTHS, json_response(200, stubs.authorization(created=created, expires=expires)))
        self.paypal.on("POST", AUTHS + "/reauthorize", json_response(201, stubs.authorization(auth_id="AUTH2")))
        self.paypal.on("POST", AUTHS + "/capture", json_response(201, stubs.capture()))
        self.as_user(self.staff)
        response = self.post(f"/api/orders/{number}/fulfil")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("renewed", response.json()["note"])
        capture_call = self.paypal.calls("POST", AUTHS + "/capture")[0]
        self.assertIn("/authorizations/AUTH2/capture", capture_call.url)
        auth = response.json()["order"]["payment"]["authorization"]
        self.assertEqual((auth["id"], auth["originalAuthorizationId"]), ("AUTH2", "AUTH1"))

    def test_expired_authorization_tells_the_operator_what_to_do(self) -> None:
        past = (timezone.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        number = self.authorized_order()
        self.paypal.on("GET", AUTHS, json_response(200, stubs.authorization(created="2026-01-01T00:00:00Z",
                                                                             expires=past)))
        self.as_user(self.staff)
        response = self.post(f"/api/orders/{number}/fulfil")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "authorization_expired")
        self.assertIn("pay again", response.json()["error"]["message"])
        self.assertEqual(self.paypal.calls("POST", AUTHS + "/capture"), [])

    def test_cancel_releases_the_hold(self) -> None:
        number = self.authorized_order()
        self.paypal.on("POST", AUTHS + "/void", json_response(200, stubs.authorization(status="VOIDED")))
        self.as_user(self.staff)
        response = self.post(f"/api/orders/{number}/cancel")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["order"]["status"], "Cancelled")
        self.assertEqual(response.json()["order"]["payment"]["status"], "voided")
        void_call = self.paypal.calls("POST", AUTHS + "/void")[0]
        self.assertEqual(void_call.headers["prefer"], "return=representation")
        # after cancel, fulfil is refused
        self.assertEqual(self.post(f"/api/orders/{number}/fulfil").status_code, 409)

    def test_cancel_after_fulfilment_is_refused(self) -> None:
        number = self.captured_order()
        self.as_user(self.staff)
        self.assertEqual(self.post(f"/api/orders/{number}/cancel").status_code, 409)


class RefundTests(ApiTestCase):
    def test_partial_refunds_never_exceed_the_capture(self) -> None:
        number = self.captured_order()
        self.paypal.on("POST", r"/v2/payments/captures/CAP1/refund",
                       json_response(201, stubs.refund("R1", value="15.00")),
                       json_response(201, stubs.refund("R2", value="5.00")))
        first = self.post(f"/api/orders/{number}/refunds", {"amount": "15.00"}, **{"Idempotency-Key": "k1"})
        self.assertEqual(first.status_code, 201, first.content)
        self.assertIn("refundId", first.json())
        too_much = self.post(f"/api/orders/{number}/refunds", {"amount": "5.01"}, **{"Idempotency-Key": "k2"})
        self.assertEqual(too_much.status_code, 409)
        self.assertEqual(too_much.json()["error"]["code"], "exceeds_refundable")
        rest = self.post(f"/api/orders/{number}/refunds", {"amount": "5.00"}, **{"Idempotency-Key": "k3"})
        self.assertEqual(rest.status_code, 201)
        payment = rest.json()["order"]["payment"]
        self.assertEqual((payment["refunded"], payment["refundable"]), ("20.00", "0.00"))
        self.assertEqual(len(self.paypal.calls("POST", r"/v2/payments/captures/CAP1/refund")), 2)

    def test_the_same_refund_key_refunds_once(self) -> None:
        number = self.captured_order()
        self.paypal.on("POST", r"/v2/payments/captures/CAP1/refund", json_response(201, stubs.refund()))
        a = self.post(f"/api/orders/{number}/refunds", {"amount": "5.00"}, **{"Idempotency-Key": "same"})
        b = self.post(f"/api/orders/{number}/refunds", {"amount": "5.00"}, **{"Idempotency-Key": "same"})
        self.assertEqual(a.json()["refundId"], b.json()["refundId"])
        calls = self.paypal.calls("POST", r"/v2/payments/captures/CAP1/refund")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].headers["paypal-request-id"].startswith(f"test:order:{number}:refund:"))
        reused = self.post(f"/api/orders/{number}/refunds", {"amount": "6.00"}, **{"Idempotency-Key": "same"})
        self.assertEqual(reused.status_code, 409)

    def test_a_refused_refund_releases_its_reservation(self) -> None:
        number = self.captured_order()
        self.paypal.on("POST", r"/v2/payments/captures/CAP1/refund", paypal_error(422, "REFUND_NOT_ALLOWED"))
        response = self.post(f"/api/orders/{number}/refunds", {"amount": "5.00"}, **{"Idempotency-Key": "x"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(PayPalPayment.objects.get(order__number=number).refund_reserved_minor, 0)

    def test_refund_requires_a_key_and_a_capture(self) -> None:
        number = self.authorized_order()
        self.assertEqual(self.post(f"/api/orders/{number}/refunds", {}).status_code, 400)
        self.assertEqual(
            self.post(f"/api/orders/{number}/refunds", {}, **{"Idempotency-Key": "k"}).status_code, 409)


class SavedCardTests(ApiTestCase):
    def save(self) -> str:
        self.as_user(self.shopper)
        self.paypal.on("POST", r"/v3/vault/payment-tokens", json_response(200, stubs.token()))
        response = self.post("/api/payment-methods", {"card": CARD}, **{"Idempotency-Key": "card-1"})
        self.assertEqual(response.status_code, 201, response.content)
        return str(response.json()["paymentMethodId"])

    def test_save_list_pay_and_delete(self) -> None:
        method_id = self.save()
        cards = self.client.get("/api/payment-methods").json()["paymentMethods"]
        self.assertEqual(cards, [{"paymentMethodId": method_id, "brand": "VISA", "lastDigits": "1111",
                                  "expiry": "2030-12", "label": "VISA ending 1111, expires 12/2030"}])
        self.assertNotIn("4111111111111111", json.dumps(cards))

        number = self.place()
        self.paypal.on("POST", ORDERS, json_response(201, stubs.created_order(stubs.authorization())))
        paid = self.post(f"/api/orders/{number}/pay", {"paymentMethodId": method_id})
        self.assertEqual(paid.status_code, 200, paid.content)
        source = stubs.body_of(self.paypal.calls("POST", ORDERS)[0])["payment_source"]
        self.assertEqual(source, {"card": {"vault_id": "TOK1"}})

        self.paypal.on("DELETE", r"/v3/vault/payment-tokens/TOK1", json_response(204, {}))
        self.assertEqual(self.client.delete(f"/api/payment-methods/{method_id}").status_code, 204)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        second = self.place()
        unusable = self.post(f"/api/orders/{second}/pay", {"paymentMethodId": method_id})
        self.assertEqual(unusable.status_code, 400)

    def test_saving_twice_with_one_key_vaults_once(self) -> None:
        self.save()
        again = self.post("/api/payment-methods", {"card": CARD}, **{"Idempotency-Key": "card-1"})
        self.assertEqual(again.status_code, 201)
        self.assertEqual(len(self.paypal.calls("POST", r"/v3/vault/payment-tokens")), 1)

    def test_a_card_belongs_to_its_shopper(self) -> None:
        method_id = self.save()
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        self.assertEqual(self.client.delete(f"/api/payment-methods/{method_id}").status_code, 404)
        self.client.force_login(self.other)
        response = self.post("/api/orders", {"items": [{"productId": self.product.pk, "quantity": 1}]})
        number = response.json()["orderId"]
        self.assertEqual(self.post(f"/api/orders/{number}/pay", {"paymentMethodId": method_id}).status_code, 400)
        self.assertEqual(self.paypal.calls("POST", ORDERS), [])


class ReconciliationTests(ApiTestCase):
    def test_pages_the_whole_range_and_lines_records_up(self) -> None:
        number = self.captured_order()
        capture_time = "2026-09-27T11:00:00Z"
        page1 = {"total_pages": 2, "page": 1, "last_refreshed_datetime": "2026-09-27T23:00:00Z",
                 "transaction_details": [{"transaction_info": {
                     "transaction_id": "CAP1", "transaction_event_code": "T0006",
                     "transaction_initiation_date": capture_time, "invoice_id": f"test-{number}-1",
                     "transaction_amount": {"currency_code": "USD", "value": "20.00"}}}]}
        page2 = {"total_pages": 2, "page": 2, "last_refreshed_datetime": "2026-09-27T23:00:00Z",
                 "transaction_details": [{"transaction_info": {
                     "transaction_id": "STRANGER", "transaction_initiation_date": capture_time,
                     "transaction_amount": {"currency_code": "USD", "value": "9.00"}}}]}
        self.paypal.on("GET", SEARCH, json_response(200, page1), json_response(200, page2))
        self.as_user(self.staff)
        response = self.client.get("/api/reconciliation",
                                   {"from": "2026-09-27T00:00:00Z", "to": "2026-09-28T00:00:00Z"})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual(len(self.paypal.calls("GET", SEARCH)), 2)
        self.assertEqual([m["paypalId"] for m in report["matched"]], ["CAP1"])
        self.assertEqual([p["transactionId"] for p in report["paypalOnly"]], ["STRANGER"])
        # the authorization (10:00) is in the app but not in PayPal's list
        self.assertEqual([a["paypalId"] for a in report["appOnly"]], ["AUTH1"])

    def test_long_ranges_are_split_into_31_day_windows(self) -> None:
        self.paypal.on("GET", SEARCH, json_response(200, {"total_pages": 1, "transaction_details": []}))
        self.as_user(self.staff)
        response = self.client.get("/api/reconciliation",
                                   {"from": "2026-06-01T00:00:00Z", "to": "2026-09-01T00:00:00Z"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.paypal.calls("GET", SEARCH)), 4)

    def test_staff_only_and_validated(self) -> None:
        self.as_user(self.shopper)
        self.assertEqual(self.client.get("/api/reconciliation").status_code, 403)
        self.as_user(self.staff)
        self.assertEqual(self.client.get("/api/reconciliation", {"from": "x", "to": "y"}).status_code, 400)


class AnswerTests(TestCase):
    def test_a_not_done_outcome_never_answers_success(self) -> None:
        for outcome in ("pending", "sending", "failed", "needs_review", "unknown", "surprise"):
            self.assertNotIn(answer(outcome, {}).status_code, (200, 201, 204))
        self.assertEqual(answer("done", {}).status_code, 200)


class StatusMapperTests(TestCase):
    def test_every_member_is_mapped_and_unlisted_values_are_unknown(self) -> None:
        from paypal.models.enums import AuthorizationStatus as A, CaptureStatus as C, RefundStatus as R

        from apps.paypal_api import safe_write as sw

        self.assertEqual([sw.authorization_outcome(s) for s in A],
                         ["done", "unknown", "failed", "unknown", "failed", "pending"])
        self.assertEqual([sw.void_outcome(s) for s in A], ["unknown", "failed", "done", "failed", "done", "unknown"])
        self.assertEqual([sw.capture_outcome(s) for s in C],
                         ["done", "failed", "unknown", "pending", "failed", "failed"])
        self.assertEqual([sw.refund_outcome(s) for s in R], ["failed", "failed", "pending", "done"])
        for mapper in (sw.authorization_outcome, sw.void_outcome, sw.capture_outcome, sw.refund_outcome):
            self.assertEqual(mapper("SOMETHING_NEW"), "unknown")
        self.assertEqual(sw.order_outcome(("PAYER_ACTION_REQUIRED", None)), "pending")
        self.assertEqual(sw.order_outcome(("COMPLETED", None)), "unknown")
        self.assertEqual(sw.vault_outcome(("TOKENIZED", None)), "done")
        self.assertEqual(sw.vault_outcome(("TOKENIZED", "FAILED")), "failed")
        self.assertEqual(sw.vault_outcome(("INCOMPLETE", None)), "unknown")
