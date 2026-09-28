"""
Tests for the PayPal payments API. PayPal is faked at the SDK's transport seam,
so the real SDK builds every request and decodes every answer.

Run from sandbox/:  python manage.py test apps.paypal_payments
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from oscar.core.loading import get_model
from oscar.test.factories import create_product
from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpRequest, HttpResponse
from paypal.models.enums import AuthorizationStatus, CaptureStatus, OrderStatus, RefundStatus

from . import gateway, services
from .models import PayPalPayment, PayPalRefund, ProviderWrite, SavedCard
from .safe_write import deterministic_ref

Country = get_model("address", "Country")

CARD = {"number": "4111111111111111", "expiry": "2030-12", "securityCode": "123", "name": "Test Shopper"}
SHIP = {"firstName": "T", "lastName": "S", "line1": "1 Main St", "city": "San Jose", "postcode": "95131",
        "countryCode": "US"}

Reply = HttpResponse | Exception | Callable[[HttpRequest], HttpResponse]


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(status_code=status, headers={"content-type": "application/json"},
                        content=json.dumps(body).encode())


def token_response() -> HttpResponse:
    return json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})


def paypal_error(status: int, issue: str) -> HttpResponse:
    return json_response(status, {"name": "UNPROCESSABLE_ENTITY", "message": "Refused.", "debug_id": "d1",
                                  "details": [{"issue": issue, "description": "Refused.", "value": "4111111111111111"}]})


class StubTransport:
    """Satisfies the SDK's transport protocol; answers the token request
    itself and then the queued replies, in order."""

    def __init__(self) -> None:
        self.replies: list[Reply] = []
        self.requests: list[HttpRequest] = []

    def queue(self, *replies: Reply) -> None:
        self.replies.extend(replies)

    def send(self, request: HttpRequest) -> HttpResponse:
        if request.url.endswith("/v1/oauth2/token"):
            return token_response()
        self.requests.append(request)
        if not self.replies:
            raise AssertionError(f"unexpected PayPal call {request.method} {request.url}")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(request)
        return reply

    def close(self) -> None:
        pass

    def request_ids(self) -> list[str | None]:
        return [r.headers.get("paypal-request-id") for r in self.requests]


def order_body(auth_status: str = "CREATED", value: str = "20.00", order_status: str = "COMPLETED",
               with_auth: bool = True) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "PPORDER1", "status": order_status, "intent": "AUTHORIZE",
        "payment_source": {"card": {"last_digits": "1111", "brand": "VISA"}},
        "purchase_units": [{"amount": {"currency_code": "USD", "value": value}}],
    }
    if with_auth:
        body["purchase_units"][0]["payments"] = {"authorizations": [{
            "id": "AUTH1", "status": auth_status, "amount": {"currency_code": "USD", "value": value},
            "create_time": "2026-09-01T10:00:00Z", "expiration_time": "2026-09-30T10:00:00Z",
        }]}
    return body


def capture_body(status: str = "COMPLETED", value: str = "20.00", capture_id: str = "CAP1") -> dict[str, Any]:
    return {"id": capture_id, "status": status, "amount": {"currency_code": "USD", "value": value},
            "create_time": "2026-09-02T10:00:00Z",
            "seller_receivable_breakdown": {"gross_amount": {"currency_code": "USD", "value": value},
                                            "paypal_fee": {"currency_code": "USD", "value": "0.88"},
                                            "net_amount": {"currency_code": "USD", "value": "19.12"}}}


def refund_body(status: str = "COMPLETED", value: str = "5.00", refund_id: str = "REF1") -> dict[str, Any]:
    return {"id": refund_id, "status": status, "amount": {"currency_code": "USD", "value": value},
            "create_time": "2026-09-03T10:00:00Z"}


@override_settings(PAYPAL_CLIENT_ID="test-id", PAYPAL_CLIENT_SECRET="test-secret", PAYPAL_CURRENCY="USD",
                   PAYPAL_ENVIRONMENT="sandbox", PAYPAL_BASE_URL="", PAYPAL_REFERENCE_PREFIX="t")
class ApiTestCase(TestCase):
    def setUp(self) -> None:
        self.transport = StubTransport()
        gateway._client = PaypalClient(
            base_url="https://api-m.sandbox.paypal.com", custom_http_client=self.transport,
            oauth2=ClientCredentials(client_id="test-id", client_secret="test-secret"),
        )
        self.addCleanup(setattr, gateway, "_client", None)
        Country.objects.get_or_create(iso_3166_1_a2="US", defaults={
            "iso_3166_1_a3": "USA", "iso_3166_1_numeric": "840", "printable_name": "United States",
            "name": "United States", "is_shipping_country": True})
        self.product = create_product(price=Decimal("10.00"), num_in_stock=50)
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw-alice-123")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw-bob-123")
        self.staff = User.objects.create_user("ops", "ops@example.com", "pw-ops-123", is_staff=True)

    # helpers ---------------------------------------------------------------

    def as_user(self, user: User) -> None:
        self.client.force_login(user)

    def post(self, url: str, data: dict[str, Any] | None = None, **headers: str) -> Any:
        return self.client.post(url, json.dumps(data or {}), content_type="application/json", headers=headers)

    def place_order(self, quantity: int = 2) -> str:
        self.as_user(self.alice)
        r = self.post("/api/orders", {"items": [{"itemId": self.product.pk, "quantity": quantity}],
                                      "shippingAddress": SHIP})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertIsInstance(r.json()["orderId"], str)  # the same type every endpoint returns
        return str(r.json()["orderId"])

    def paid_order(self) -> str:
        number = self.place_order()
        self.transport.queue(json_response(201, order_body()))
        self.assertEqual(self.post(f"/api/orders/{number}/pay", {"card": CARD}).status_code, 200)
        return number

    def captured_order(self) -> str:
        number = self.paid_order()
        PayPalPayment.objects.filter(order__number=number).update(authorized_at=timezone.now())
        self.transport.queue(json_response(201, capture_body()))
        self.as_user(self.staff)
        self.assertEqual(self.post(f"/api/orders/{number}/fulfil").status_code, 200)
        self.as_user(self.alice)
        return number


class StatusMappingTests(TestCase):
    def test_every_listed_status_has_a_deliberate_outcome_and_unlisted_ones_are_unknown(self) -> None:
        self.assertEqual(services.authorize_outcome((AuthorizationStatus.CREATED, OrderStatus.COMPLETED)), "done")
        self.assertEqual(services.authorize_outcome((AuthorizationStatus.PENDING, OrderStatus.COMPLETED)), "pending")
        self.assertEqual(services.authorize_outcome((AuthorizationStatus.DENIED, OrderStatus.COMPLETED)), "failed")
        self.assertEqual(services.authorize_outcome((None, OrderStatus.PAYER_ACTION_REQUIRED)), "failed")
        self.assertEqual(services.authorize_outcome((None, OrderStatus.COMPLETED)), "unknown")
        self.assertEqual(services.authorize_outcome(("SOMETHING_NEW", OrderStatus.COMPLETED)), "unknown")
        self.assertEqual(services.capture_outcome(CaptureStatus.PENDING), "pending")
        self.assertEqual(services.capture_outcome(CaptureStatus.REFUNDED), "failed")
        self.assertEqual(services.capture_outcome("SOMETHING_NEW"), "unknown")
        self.assertEqual(services.void_outcome(AuthorizationStatus.VOIDED), "done")
        self.assertEqual(services.void_outcome(AuthorizationStatus.CAPTURED), "failed")
        self.assertEqual(services.void_outcome(AuthorizationStatus.CREATED), "unknown")
        self.assertEqual(services.refund_outcome(RefundStatus.CANCELLED), "failed")
        self.assertEqual(services.refund_outcome("SOMETHING_NEW"), "unknown")
        self.assertEqual(services.delete_outcome(204), "done")
        self.assertEqual(services.delete_outcome(404), "done")
        self.assertEqual(services.delete_outcome(503), "unknown")

    def test_a_not_done_outcome_never_answers_success(self) -> None:
        from .views import answer
        for outcome in ("pending", "sending", "failed", "needs_review", "unknown", "surprise"):
            self.assertNotIn(answer(outcome, {}).status_code, (200, 201, 204), outcome)


class PayTests(ApiTestCase):
    def test_order_is_placed_from_catalogue_prices_and_awaits_payment(self) -> None:
        number = self.place_order(quantity=2)
        payment = PayPalPayment.objects.get(order__number=number)
        self.assertEqual((payment.status, payment.amount, payment.currency), ("awaiting_payment", Decimal("20.00"), "USD"))
        self.assertEqual(payment.order.user, self.alice)

    def test_authorize_holds_the_order_total_and_never_stores_the_card(self) -> None:
        number = self.place_order()
        self.transport.queue(json_response(201, order_body()))
        r = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(r.status_code, 200, r.content)
        sent = self.transport.requests[0]
        self.assertTrue(sent.url.endswith("/v2/checkout/orders"))
        self.assertEqual(sent.body.value["intent"], "AUTHORIZE")  # type: ignore[union-attr]
        self.assertEqual(sent.body.value["purchase_units"][0]["amount"], {"currency_code": "USD", "value": "20.00"})  # type: ignore[union-attr]
        self.assertEqual(sent.headers["paypal-request-id"], deterministic_ref(number, "auth", 1))
        self.assertEqual(r.json()["payment"]["state"], "authorized")
        self.assertNotIn("4111111111111111", json.dumps([list(m.objects.values()) for m in
                                                          (PayPalPayment, ProviderWrite, SavedCard)], default=str))

    def test_the_same_pay_twice_makes_one_provider_call(self) -> None:
        number = self.place_order()
        self.transport.queue(json_response(201, order_body()))
        first = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        second = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(first.json()["payment"]["authorization"], second.json()["payment"]["authorization"])

    def test_a_claim_in_flight_answers_in_progress_without_calling_paypal(self) -> None:
        number = self.place_order()
        ProviderWrite.objects.create(ref=deterministic_ref(number, "auth", 1), operation="authorize",
                                     claimed_at=timezone.now())
        r = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(self.transport.requests, [])

    def test_declined_card_is_a_known_failure_and_the_next_try_is_a_new_write(self) -> None:
        number = self.place_order()
        self.transport.queue(json_response(201, order_body(auth_status="DENIED")))
        r = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(r.status_code, 402)
        self.transport.queue(json_response(201, order_body()))
        self.assertEqual(self.post(f"/api/orders/{number}/pay", {"card": CARD}).status_code, 200)
        self.assertEqual(self.transport.request_ids(),
                         [deterministic_ref(number, "auth", 1), deterministic_ref(number, "auth", 2)])

    def test_payer_action_required_is_reported_not_built_around(self) -> None:
        number = self.place_order()
        self.transport.queue(json_response(201, order_body(order_status="PAYER_ACTION_REQUIRED", with_auth=False)))
        r = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(r.status_code, 402)
        self.assertIn("browser", r.json()["message"])

    def test_unsent_is_not_the_same_failure_as_unknown(self) -> None:
        number = self.place_order()
        self.transport.queue(httpx.ConnectError("refused"))
        unsent = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual((unsent.status_code, unsent.json()["error"].get("outcomeUnknown", False)), (502, False))
        self.assertFalse(ProviderWrite.objects.exists())  # nothing happened: claim released

        # the answer is lost, and so is the same-reference check
        self.transport.queue(httpx.ReadTimeout("no reply"), httpx.ReadTimeout("no reply"))
        unknown = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual((unknown.status_code, unknown.json()["error"]["outcomeUnknown"]), (504, True))
        self.assertEqual(ProviderWrite.objects.get().outcome, "unknown")

        # the next request settles it under the SAME reference: PayPal replays the original
        self.transport.queue(json_response(201, order_body()))
        settled = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(settled.status_code, 200)
        ref = deterministic_ref(number, "auth", 1)
        self.assertEqual(self.transport.request_ids(), [ref, ref, ref, ref])

    def test_amount_other_than_asked_needs_review(self) -> None:
        number = self.place_order()
        self.transport.queue(json_response(201, order_body(value="19.99")))
        r = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(PayPalPayment.objects.get(order__number=number).status, "needs_review")

    def test_bad_credentials_are_our_problem_not_the_callers(self) -> None:
        number = self.place_order()

        class RefusingTransport(StubTransport):
            def send(self, request: HttpRequest) -> HttpResponse:
                return json_response(401, {"error": "invalid_client", "error_description": "bad"})

        gateway._client = PaypalClient(base_url="https://api-m.sandbox.paypal.com",
                                       custom_http_client=RefusingTransport(),
                                       oauth2=ClientCredentials(client_id="x", client_secret="y"))
        r = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(r.status_code, 502)

    def test_a_shopper_cannot_see_or_pay_anothers_order(self) -> None:
        number = self.place_order()
        self.as_user(self.bob)
        self.assertEqual(self.post(f"/api/orders/{number}/pay", {"card": CARD}).status_code, 404)
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"], [])

    def test_invalid_card_is_rejected_without_echoing_it(self) -> None:
        number = self.place_order()
        r = self.post(f"/api/orders/{number}/pay", {"card": {**CARD, "number": "4111111111111112"}})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("4111111111111112", r.content.decode())


class FulfilTests(ApiTestCase):
    def test_shoppers_cannot_fulfil_cancel_or_reconcile(self) -> None:
        number = self.paid_order()
        self.assertEqual(self.post(f"/api/orders/{number}/fulfil").status_code, 403)
        self.assertEqual(self.post(f"/api/orders/{number}/cancel").status_code, 403)
        self.assertEqual(self.client.get("/api/reconciliation?from=2026-01-01T00:00:00Z&to=2026-01-02T00:00:00Z").status_code, 403)

    def test_capture_records_amount_fee_and_net(self) -> None:
        number = self.captured_order()
        payment = PayPalPayment.objects.get(order__number=number)
        self.assertEqual((payment.status, payment.captured_amount, payment.paypal_fee, payment.net_amount),
                         ("captured", Decimal("20.00"), Decimal("0.88"), Decimal("19.12")))
        self.assertEqual(payment.order.status, "Complete")
        capture = self.transport.requests[-1]
        self.assertEqual(capture.headers["paypal-request-id"], deterministic_ref(number, "capture", "AUTH1"))

    def test_stale_hold_is_renewed_before_capture(self) -> None:
        number = self.paid_order()
        PayPalPayment.objects.filter(order__number=number).update(authorized_at=timezone.now() - timedelta(days=4))
        reauth = {"id": "AUTH2", "status": "CREATED", "amount": {"currency_code": "USD", "value": "20.00"},
                  "create_time": "2026-09-05T10:00:00Z", "expiration_time": "2026-09-30T10:00:00Z"}
        self.transport.queue(json_response(201, reauth), json_response(201, capture_body()))
        self.as_user(self.staff)
        r = self.post(f"/api/orders/{number}/fulfil")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(self.transport.requests[-2].url.endswith("/authorizations/AUTH1/reauthorize"))
        self.assertTrue(self.transport.requests[-1].url.endswith("/authorizations/AUTH2/capture"))

    def test_expired_hold_that_cannot_be_renewed_tells_the_operator_what_to_do(self) -> None:
        number = self.paid_order()
        PayPalPayment.objects.filter(order__number=number).update(authorized_at=timezone.now())
        expired = {"id": "AUTH1", "status": "EXPIRED", "amount": {"currency_code": "USD", "value": "20.00"},
                   "expiration_time": "2026-09-01T10:00:00Z"}
        self.transport.queue(
            paypal_error(422, "AUTHORIZATION_EXPIRED"),       # capture refused
            json_response(200, expired),                      # the hold is gone
            paypal_error(422, "REAUTHORIZATION_NOT_ALLOWED"), # and cannot be renewed
            json_response(200, expired),
        )
        self.as_user(self.staff)
        r = self.post(f"/api/orders/{number}/fulfil")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "authorization_expired")
        self.assertIn("Cancel this order", r.json()["error"]["message"])
        self.assertNotIn("4111111111111111", r.content.decode())  # PayPal's echoed `value` is dropped
        self.assertEqual(PayPalPayment.objects.get(order__number=number).status, "authorized")

    def test_cancel_releases_the_hold(self) -> None:
        number = self.paid_order()
        self.transport.queue(json_response(200, {"id": "AUTH1", "status": "VOIDED", "update_time": "2026-09-02T10:00:00Z"}))
        self.as_user(self.staff)
        r = self.post(f"/api/orders/{number}/cancel")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["payment"]["state"], r.json()["status"]), ("voided", "Cancelled"))
        self.assertEqual(self.post(f"/api/orders/{number}/cancel").status_code, 200)  # repeat: no new call
        self.assertEqual(len(self.transport.requests), 2)  # authorize + one void

    def test_cancel_after_capture_is_refused(self) -> None:
        number = self.captured_order()
        self.as_user(self.staff)
        self.assertEqual(self.post(f"/api/orders/{number}/cancel").status_code, 409)


class RefundTests(ApiTestCase):
    def test_same_key_refunds_once_and_distinct_partials_both_go_through(self) -> None:
        number = self.captured_order()
        self.transport.queue(json_response(201, refund_body(value="5.00", refund_id="R1")))
        a = self.post(f"/api/orders/{number}/refunds", {"amount": "5.00"}, **{"Idempotency-Key": "k1"})
        again = self.post(f"/api/orders/{number}/refunds", {"amount": "5.00"}, **{"Idempotency-Key": "k1"})
        self.assertEqual(a.status_code, 201, a.content)
        self.assertEqual(a.json()["refundId"], again.json()["refundId"])
        self.transport.queue(json_response(201, refund_body(value="5.00", refund_id="R2")))
        b = self.post(f"/api/orders/{number}/refunds", {"amount": "5.00"}, **{"Idempotency-Key": "k2"})
        self.assertEqual(b.status_code, 201)
        self.assertNotEqual(a.json()["refundId"], b.json()["refundId"])
        refunds = [r for r in self.transport.requests if r.url.endswith("/refund")]
        self.assertEqual(len(refunds), 2)
        self.assertNotEqual(refunds[0].headers["paypal-request-id"], refunds[1].headers["paypal-request-id"])
        self.assertEqual(PayPalPayment.objects.get(order__number=number).status, "partially_refunded")

    def test_refund_can_never_exceed_the_capture(self) -> None:
        number = self.captured_order()
        self.transport.queue(json_response(201, refund_body(value="15.00")))
        self.assertEqual(self.post(f"/api/orders/{number}/refunds", {"amount": "15.00"},
                                   **{"Idempotency-Key": "k1"}).status_code, 201)
        r = self.post(f"/api/orders/{number}/refunds", {"amount": "5.01"}, **{"Idempotency-Key": "k2"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "refund_exceeds_capture")

    def test_a_refused_refund_gives_the_amount_back(self) -> None:
        number = self.captured_order()
        self.transport.queue(paypal_error(422, "REFUND_NOT_ALLOWED"))
        self.assertEqual(self.post(f"/api/orders/{number}/refunds", {"amount": "20.00"},
                                   **{"Idempotency-Key": "k1"}).status_code, 422)
        self.assertEqual(PayPalPayment.objects.get(order__number=number).refund_reserved, Decimal("0.00"))
        self.assertEqual(PayPalRefund.objects.get().status, "failed")

    def test_pending_refund_is_202_and_keeps_its_reservation(self) -> None:
        number = self.captured_order()
        self.transport.queue(json_response(201, refund_body(status="PENDING", value="20.00")))
        r = self.post(f"/api/orders/{number}/refunds", {"amount": "20.00"}, **{"Idempotency-Key": "k1"})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(PayPalPayment.objects.get(order__number=number).refund_reserved, Decimal("20.00"))

    def test_refund_requires_an_idempotency_key(self) -> None:
        number = self.captured_order()
        self.assertEqual(self.post(f"/api/orders/{number}/refunds", {"amount": "1.00"}).status_code, 400)


class SavedCardTests(ApiTestCase):
    def vault_reply(self, token: str = "TOK1") -> HttpResponse:
        return json_response(200, {"id": token, "customer": {"id": "CUST1"},
                                   "payment_source": {"card": {"brand": "VISA", "last_digits": "1111", "expiry": "2030-12"}}})

    def test_save_list_pay_and_delete(self) -> None:
        self.as_user(self.alice)
        self.transport.queue(self.vault_reply())
        r = self.post("/api/payment-methods", {"card": CARD}, **{"Idempotency-Key": "s1"})
        self.assertEqual(r.status_code, 201, r.content)
        method_id = r.json()["paymentMethodId"]
        self.assertEqual(r.json()["lastDigits"], "1111")
        self.assertNotIn("number", r.json())
        again = self.post("/api/payment-methods", {"card": CARD}, **{"Idempotency-Key": "s1"})
        self.assertEqual(again.json()["paymentMethodId"], method_id)
        self.assertEqual(len(self.transport.requests), 1)

        number = self.place_order()
        self.transport.queue(json_response(201, order_body()))
        self.assertEqual(self.post(f"/api/orders/{number}/pay", {"paymentMethodId": method_id}).status_code, 200)
        card_sent = self.transport.requests[-1].body.value["payment_source"]["card"]  # type: ignore[union-attr]
        self.assertEqual(card_sent["vault_id"], "TOK1")
        self.assertNotIn("number", card_sent)

        self.transport.queue(HttpResponse(status_code=204, headers={}))
        r = self.client.delete(f"/api/payment-methods/{method_id}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["removedAtPayPal"])
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        other = self.place_order()
        self.assertEqual(self.post(f"/api/orders/{other}/pay", {"paymentMethodId": method_id}).status_code, 404)

    def test_cards_belong_to_their_shopper(self) -> None:
        card = SavedCard.objects.create(user=self.alice, token_id="TOK9", brand="VISA", last_digits="1111")
        self.as_user(self.bob)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        self.assertEqual(self.client.delete(f"/api/payment-methods/{card.public_id}").status_code, 404)
        self.assertIsNone(SavedCard.objects.get(pk=card.pk).deleted_at)

    def test_card_removed_locally_even_if_paypal_is_down(self) -> None:
        card = SavedCard.objects.create(user=self.alice, token_id="TOK9", brand="VISA", last_digits="1111")
        self.as_user(self.alice)
        self.transport.queue(json_response(500, {}), json_response(500, {}))
        r = self.client.delete(f"/api/payment-methods/{card.public_id}")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["removedAtPayPal"])
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])


class ReconciliationTests(ApiTestCase):
    def test_covers_every_page_and_window_and_lines_up_both_sides(self) -> None:
        number = self.captured_order()
        PayPalPayment.objects.filter(order__number=number).update(
            captured_at=timezone.now() - timedelta(days=40))

        def page(txn_id: str, total_pages: int, page_no: int) -> HttpResponse:
            return json_response(200, {"transaction_details": [{"transaction_info": {
                "transaction_id": txn_id, "transaction_event_code": "T0006",
                "transaction_initiation_date": (timezone.now() - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "transaction_amount": {"currency_code": "USD", "value": "20.00"}}}],
                "page": page_no, "total_pages": total_pages})

        # 45 days -> two windows (31 + 14); the first window has two pages.
        self.transport.queue(page("CAP1", 2, 1), page("STRANGER", 2, 2), json_response(200, {"transaction_details": [], "total_pages": 0}))
        self.as_user(self.staff)
        end = timezone.now()
        start = end - timedelta(days=45)
        r = self.client.get("/api/reconciliation", {"from": start.isoformat(), "to": end.isoformat()})
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual([m["paypal"]["transactionId"] for m in body["matched"]], ["CAP1"])
        self.assertEqual([t["transactionId"] for t in body["paypalOnly"]], ["STRANGER"])
        searches = [q for q in self.transport.requests if "/v1/reporting/transactions" in q.url]
        self.assertEqual(len(searches), 3)
        self.assertIn("page=2", searches[1].url)

    def test_rejects_a_bad_range(self) -> None:
        self.as_user(self.staff)
        self.assertEqual(self.client.get("/api/reconciliation", {"from": "nope", "to": "2026-01-01T00:00:00Z"}).status_code, 400)
