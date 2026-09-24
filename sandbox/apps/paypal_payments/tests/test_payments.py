from datetime import timedelta
from decimal import Decimal

import httpx
from django.test import Client
from django.utils import timezone
from paypal import PaypalClient
from paypal.core import HttpRequest, HttpResponse

from apps.paypal_payments import gateway
from apps.paypal_payments.models import PaymentOperation, PayPalPayment

from . import stubs
from .base import CARD, PREFIX, ApiTestCase


class PlaceOrderTests(ApiTestCase):
    def test_order_uses_catalogue_prices_and_configured_currency(self) -> None:
        order_id = self.place_order(quantity=2)
        payment = PayPalPayment.objects.get(order__number=order_id)
        self.assertEqual(payment.amount, Decimal("25.00"))
        self.assertEqual(payment.currency, "USD")
        self.assertEqual(payment.order.currency, "USD")
        self.assertEqual(payment.state, PayPalPayment.AWAITING_PAYMENT)
        self.assertEqual(payment.order.lines.get().quantity, 2)

    def test_requires_login(self) -> None:
        response = self.post("/api/orders", {"lines": [{"productId": self.product.pk, "quantity": 1}]})
        self.assertEqual(response.status_code, 401)

    def test_rejects_unknown_product(self) -> None:
        self.as_user(self.alice)
        response = self.post("/api/orders", {"lines": [{"productId": 999999, "quantity": 1}]})
        self.assertEqual(response.status_code, 400)

    def test_csrf_is_enforced_for_session_callers(self) -> None:
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.alice)
        response = client.post("/api/orders", data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 403)


class PayTests(ApiTestCase):
    def test_card_payment_authorizes_the_order_total(self) -> None:
        order_id = self.place_order()
        self.transport.queue(stubs.json_response(
            201, stubs.order("PPO1", "25.00", auth=stubs.authorization("AUTH1", "25.00"))))

        response = self.post("/api/orders/%s/pay" % order_id, {"card": CARD})

        self.assertEqual(response.status_code, 200, response.content)
        payment = response.json()["payment"]
        self.assertEqual(payment["state"], "authorized")
        self.assertEqual(payment["authorization"]["id"], "AUTH1")
        self.assertEqual(payment["card"], {"brand": "VISA", "lastDigits": "1111", "paymentMethodId": None})
        (request,) = self.transport.requests
        reference = "%s-%s-pay-1" % (PREFIX, order_id)
        self.assertEqual(request.headers["paypal-request-id"], reference)
        body = self.transport.body(request)
        self.assertEqual(body["intent"], "AUTHORIZE")
        self.assertEqual(body["purchase_units"][0]["amount"], {"currency_code": "USD", "value": "25.00"})
        self.assertEqual(body["purchase_units"][0]["invoice_id"], reference)
        self.assertEqual(body["payment_source"]["card"]["number"], "4111111111111111")
        self.assertEqual(request.headers["prefer"], "return=representation")

    def test_double_click_authorizes_once(self) -> None:
        order_id = self.place_order()
        self.transport.queue(stubs.json_response(
            201, stubs.order("PPO1", "25.00", auth=stubs.authorization("AUTH1", "25.00"))))
        first = self.post("/api/orders/%s/pay" % order_id, {"card": CARD})
        second = self.post("/api/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["payment"], second.json()["payment"])
        self.assertEqual(PaymentOperation.objects.filter(kind="pay").count(), 1)

    def test_request_in_flight_answers_in_progress_without_calling_paypal(self) -> None:
        order_id = self.place_order()
        payment = PayPalPayment.objects.get(order__number=order_id)
        PaymentOperation.objects.create(reference="%s-%s-pay-1" % (PREFIX, order_id), kind="pay", payment=payment,
                                        amount=payment.amount, currency="USD")
        payment.state = PayPalPayment.AUTHORIZING
        payment.save()
        response = self.post("/api/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.transport.requests, [])

    def test_denied_authorization_is_a_failure_and_allows_a_new_attempt(self) -> None:
        order_id = self.place_order()
        self.transport.queue(stubs.json_response(
            201, stubs.order("PPO1", "25.00", auth=stubs.authorization("AUTH1", "25.00", status="DENIED"))))
        response = self.post("/api/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()["payment"]["state"], "payment_failed")

        self.transport.queue(stubs.json_response(
            201, stubs.order("PPO2", "25.00", auth=stubs.authorization("AUTH2", "25.00"))))
        retry = self.post("/api/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(self.transport.requests[-1].headers["paypal-request-id"], "%s-%s-pay-2" % (PREFIX, order_id))

    def test_an_unlisted_status_is_not_success(self) -> None:
        order_id = self.place_order()
        self.transport.queue(stubs.json_response(
            201, stubs.order("PPO1", "25.00", auth=stubs.authorization("AUTH1", "25.00", status="SOMETHING_NEW"))))
        response = self.post("/api/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["paymentOutcome"], "unknown")
        payment = PayPalPayment.objects.get(order__number=order_id)
        self.assertEqual(payment.state, PayPalPayment.AUTHORIZING)
        self.assertEqual(PaymentOperation.objects.get(kind="pay").outcome, PaymentOperation.UNKNOWN)

    def test_payer_action_required_is_reported_not_followed(self) -> None:
        order_id = self.place_order()
        self.transport.queue(stubs.json_response(200, stubs.order("PPO1", "25.00", status="PAYER_ACTION_REQUIRED")))
        response = self.post("/api/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(response.status_code, 402)
        self.assertIn("browser", response.json()["message"])

    def test_echoed_amount_mismatch_is_held_for_review(self) -> None:
        order_id = self.place_order()
        self.transport.queue(stubs.json_response(
            201, stubs.order("PPO1", "25.00", auth=stubs.authorization("AUTH1", "2.50"))))
        response = self.post("/api/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "amount_mismatch")
        self.assertEqual(PayPalPayment.objects.get(order__number=order_id).state, PayPalPayment.NEEDS_REVIEW)

    def test_never_sent_is_a_known_failure(self) -> None:
        order_id = self.place_order()
        self.transport.queue(httpx.ConnectError("refused"))
        response = self.post("/api/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("outcomeUnknown", response.json())
        self.assertEqual(PaymentOperation.objects.get(kind="pay").outcome, PaymentOperation.FAILED)

    def test_no_reply_is_an_unknown_outcome_settled_by_lookup_not_resend(self) -> None:
        order_id = self.place_order()
        self.transport.queue(httpx.ReadTimeout("no reply"), stubs.json_response(200, stubs.search_page([])))
        response = self.post("/api/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(response.status_code, 504)
        self.assertTrue(response.json()["outcomeUnknown"])
        op = PaymentOperation.objects.get(kind="pay")
        self.assertEqual(op.outcome, PaymentOperation.UNKNOWN)

        # The repeat looks the authorization up by the invoice id it carried;
        # it never sends a second create_order.
        reference = "%s-%s-pay-1" % (PREFIX, order_id)
        self.transport.requests.clear()
        self.transport.queue(
            stubs.json_response(200, stubs.search_page([
                stubs.transaction_row("AUTH9", timezone.now().strftime("%Y-%m-%dT%H:%M:%SZ"), "25.00",
                                      invoice_id=reference)])),
            stubs.json_response(200, stubs.authorization("AUTH9", "25.00")),
        )
        again = self.post("/api/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual(self.transport.paths(), ["GET /v1/reporting/transactions",
                                                  "GET /v2/payments/authorizations/AUTH9"])
        self.assertEqual(again.json()["payment"]["authorization"]["id"], "AUTH9")

    def test_duplicate_invoice_is_a_landing_to_look_up(self) -> None:
        order_id = self.place_order()
        reference = "%s-%s-pay-1" % (PREFIX, order_id)
        self.transport.queue(
            stubs.paypal_error(422, "DUPLICATE_INVOICE_ID"),
            stubs.json_response(200, stubs.search_page([
                stubs.transaction_row("AUTH7", timezone.now().strftime("%Y-%m-%dT%H:%M:%SZ"), invoice_id=reference)])),
            stubs.json_response(200, stubs.authorization("AUTH7", "25.00")),
        )
        response = self.post("/api/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(response.status_code, 200, response.content)

    def test_other_shoppers_cannot_pay_or_see_an_order(self) -> None:
        order_id = self.place_order()
        self.as_user(self.bob)
        self.assertEqual(self.post("/api/orders/%s/pay" % order_id, {"card": CARD}).status_code, 404)
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"], [])

    def test_card_number_is_not_stored(self) -> None:
        order_id = self.place_order()
        self.transport.queue(stubs.json_response(
            201, stubs.order("PPO1", "25.00", auth=stubs.authorization("AUTH1", "25.00"))))
        self.post("/api/orders/%s/pay" % order_id, {"card": CARD})
        for model in (PayPalPayment, PaymentOperation):
            for row in model.objects.values():
                self.assertNotIn("4111111111111111", str(row))

    def test_bad_credentials_are_a_configuration_error(self) -> None:
        order_id = self.place_order()

        class RejectingTokens(stubs.StubTransport):
            def send(self, request: HttpRequest) -> HttpResponse:
                self.requests.append(request)
                return stubs.json_response(401, {"error": "invalid_client"})

        transport = RejectingTokens()
        gateway.install_client(PaypalClient(base_url="https://paypal.test", custom_http_client=transport,
                                            oauth2={"client_id": "x", "client_secret": "y"}))
        response = self.post("/api/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "paypal_auth_failed")
        self.assertEqual([r.url.rsplit("/", 3)[-3:] for r in transport.requests], [["v1", "oauth2", "token"]])
        # Nothing was sent to create an order, so a new attempt is allowed.
        self.assertEqual(PaymentOperation.objects.get(kind="pay").outcome, PaymentOperation.FAILED)


class FulfilTests(ApiTestCase):
    def test_fulfil_captures_and_records_fee_and_net(self) -> None:
        order_id = self.authorized_order()
        self.as_user(self.staff)
        self.transport.queue(stubs.json_response(201, stubs.capture("CAP1", "25.00", fee="1.03")))
        response = self.post("/api/orders/%s/fulfil" % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["status"], "Complete")
        self.assertEqual(body["payment"]["capture"]["amount"], "25.00")
        self.assertEqual(body["payment"]["capture"]["paypalFee"], "1.03")
        self.assertEqual(body["payment"]["capture"]["netAmount"], "23.97")
        (request,) = self.transport.requests
        self.assertTrue(request.url.endswith("/v2/payments/authorizations/AUTH1/capture"))
        self.assertEqual(self.transport.body(request)["amount"], {"currency_code": "USD", "value": "25.00"})
        self.assertEqual(request.headers["paypal-request-id"], "%s-%s-cap-1" % (PREFIX, order_id))

        again = self.post("/api/orders/%s/fulfil" % order_id)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(self.transport.requests), 1)

    def test_fulfil_is_staff_only(self) -> None:
        order_id = self.authorized_order()
        self.assertEqual(self.post("/api/orders/%s/fulfil" % order_id).status_code, 403)

    def test_pending_capture_is_not_done_until_paypal_completes_it(self) -> None:
        order_id = self.authorized_order()
        self.as_user(self.staff)
        self.transport.queue(stubs.json_response(201, stubs.capture("CAP1", "25.00", status="PENDING")))
        response = self.post("/api/orders/%s/fulfil" % order_id)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["payment"]["state"], "capture_pending")

        self.transport.queue(stubs.json_response(200, stubs.capture("CAP1", "25.00")))
        response = self.post("/api/orders/%s/fulfil" % order_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["payment"]["state"], "captured")
        self.assertEqual(self.transport.paths()[-1], "GET /v2/payments/captures/CAP1")

    def test_unknown_capture_is_checked_by_resending_the_same_request_id(self) -> None:
        order_id = self.authorized_order()
        self.as_user(self.staff)
        self.transport.queue(httpx.ReadTimeout("no reply"), stubs.json_response(201, stubs.capture("CAP1", "25.00")))
        response = self.post("/api/orders/%s/fulfil" % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        ids = [r.headers["paypal-request-id"] for r in self.transport.requests]
        self.assertEqual(ids, ["%s-%s-cap-1" % (PREFIX, order_id)] * 2)

    def test_stale_authorization_is_reauthorized_before_capture(self) -> None:
        order_id = self.authorized_order()
        PayPalPayment.objects.filter(order__number=order_id).update(
            authorization_created_at=timezone.now() - timedelta(days=5))
        self.as_user(self.staff)
        self.transport.queue(
            stubs.json_response(201, stubs.authorization("AUTH2", "25.00")),
            stubs.json_response(201, stubs.capture("CAP1", "25.00")),
        )
        response = self.post("/api/orders/%s/fulfil" % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.transport.paths(), [
            "POST /v2/payments/authorizations/AUTH1/reauthorize",
            "POST /v2/payments/authorizations/AUTH2/capture",
        ])
        self.assertEqual(response.json()["payment"]["authorization"]["reauthorizedFrom"], "AUTH1")

    def test_expired_authorization_is_explained_to_the_operator(self) -> None:
        order_id = self.authorized_order()
        PayPalPayment.objects.filter(order__number=order_id).update(
            authorization_expires_at=timezone.now() - timedelta(minutes=1))
        self.as_user(self.staff)
        response = self.post("/api/orders/%s/fulfil" % order_id)
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["error"], "authorization_expired")
        self.assertIn("pay again", body["message"])
        self.assertEqual(self.transport.requests, [])

        # The shopper can pay again, and the order becomes fulfillable.
        self.as_user(self.alice)
        self.transport.queue(stubs.json_response(
            201, stubs.order("PPO2", "25.00", auth=stubs.authorization("AUTH3", "25.00"))))
        self.assertEqual(self.post("/api/orders/%s/pay" % order_id, {"card": CARD}).status_code, 200)

    def test_refused_capture_on_a_dead_authorization_is_explained(self) -> None:
        order_id = self.authorized_order()
        self.as_user(self.staff)
        self.transport.queue(
            stubs.paypal_error(422, "SOME_CAPTURE_REFUSAL"),
            stubs.json_response(200, stubs.authorization("AUTH1", "25.00", status="VOIDED")),
        )
        response = self.post("/api/orders/%s/fulfil" % order_id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "authorization_expired")


class CancelTests(ApiTestCase):
    def test_cancel_voids_the_hold(self) -> None:
        order_id = self.authorized_order()
        self.as_user(self.staff)
        self.transport.queue(stubs.json_response(200, stubs.authorization("AUTH1", "25.00", status="VOIDED")))
        response = self.post("/api/orders/%s/cancel" % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "Cancelled")
        self.assertEqual(response.json()["payment"]["state"], "cancelled")
        self.assertTrue(self.transport.requests[0].url.endswith("/v2/payments/authorizations/AUTH1/void"))
        self.assertEqual(self.post("/api/orders/%s/cancel" % order_id).status_code, 200)
        self.assertEqual(len(self.transport.requests), 1)

    def test_cannot_cancel_after_fulfilment(self) -> None:
        order_id = self.captured_order()
        self.as_user(self.staff)
        response = self.post("/api/orders/%s/cancel" % order_id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.transport.requests, [])

    def test_cancel_is_staff_only(self) -> None:
        order_id = self.authorized_order()
        self.assertEqual(self.post("/api/orders/%s/cancel" % order_id).status_code, 403)


class RefundTests(ApiTestCase):
    def refund(self, order_id: str, amount: str | None, key: str) -> object:
        body = {"amount": amount} if amount is not None else {}
        return self.post("/api/orders/%s/refunds" % order_id, body, **{"Idempotency-Key": key})

    def test_partial_refunds_under_distinct_keys_both_go_through(self) -> None:
        order_id = self.captured_order()
        self.transport.queue(
            stubs.json_response(201, stubs.refund("RF1", "5.00")), stubs.json_response(200, stubs.capture("CAP1", "25.00", status="PARTIALLY_REFUNDED")),
            stubs.json_response(201, stubs.refund("RF2", "3.00")), stubs.json_response(200, stubs.capture("CAP1", "25.00", status="PARTIALLY_REFUNDED")),
        )
        first = self.refund(order_id, "5.00", "k1")
        second = self.refund(order_id, "3.00", "k2")
        self.assertEqual(first.status_code, 201, first.content)  # type: ignore[attr-defined]
        self.assertEqual(second.status_code, 201)  # type: ignore[attr-defined]
        self.assertNotEqual(first.json()["refundId"], second.json()["refundId"])  # type: ignore[attr-defined]
        payment = PayPalPayment.objects.get(order__number=order_id)
        self.assertEqual(payment.refunded_amount, Decimal("8.00"))
        self.assertEqual(payment.state, PayPalPayment.PARTIALLY_REFUNDED)

    def test_same_key_refunds_once(self) -> None:
        order_id = self.captured_order()
        self.transport.queue(stubs.json_response(201, stubs.refund("RF1", "5.00")),
                             stubs.json_response(200, stubs.capture("CAP1", "25.00")))
        first = self.refund(order_id, "5.00", "same")
        self.transport.queue(stubs.json_response(200, stubs.capture("CAP1", "25.00")))
        second = self.refund(order_id, "5.00", "same")
        refund_calls = [p for p in self.transport.paths() if p.endswith("/refund")]
        self.assertEqual(len(refund_calls), 1)
        self.assertEqual(first.json()["refundId"], second.json()["refundId"])  # type: ignore[attr-defined]

    def test_same_key_with_a_different_amount_is_rejected(self) -> None:
        order_id = self.captured_order()
        self.transport.queue(stubs.json_response(201, stubs.refund("RF1", "5.00")),
                             stubs.json_response(200, stubs.capture("CAP1", "25.00")))
        self.refund(order_id, "5.00", "same")
        response = self.refund(order_id, "6.00", "same")
        self.assertEqual(response.status_code, 422)  # type: ignore[attr-defined]

    def test_cannot_refund_beyond_what_was_captured(self) -> None:
        order_id = self.captured_order()
        self.transport.queue(stubs.json_response(201, stubs.refund("RF1", "20.00")),
                             stubs.json_response(200, stubs.capture("CAP1", "25.00")))
        self.refund(order_id, "20.00", "a")
        self.transport.requests.clear()
        response = self.refund(order_id, "5.01", "b")
        self.assertEqual(response.status_code, 422)  # type: ignore[attr-defined]
        self.assertEqual(response.json()["refundable"], "5.00")  # type: ignore[attr-defined]
        self.assertEqual(self.transport.requests, [])

    def test_in_flight_refunds_count_against_the_refundable_amount(self) -> None:
        order_id = self.captured_order()
        payment = PayPalPayment.objects.get(order__number=order_id)
        PaymentOperation.objects.create(reference="other", kind="refund", payment=payment,
                                        amount=Decimal("24.00"), currency="USD", outcome="unknown")
        response = self.refund(order_id, "2.00", "c")
        self.assertEqual(response.status_code, 422)  # type: ignore[attr-defined]

    def test_full_refund_when_amount_omitted(self) -> None:
        order_id = self.captured_order()
        self.transport.queue(stubs.json_response(201, stubs.refund("RF1", "25.00")),
                             stubs.json_response(200, stubs.capture("CAP1", "25.00", status="REFUNDED")))
        response = self.refund(order_id, None, "full")
        self.assertEqual(response.status_code, 201)  # type: ignore[attr-defined]
        self.assertEqual(self.transport.body(self.transport.requests[0])["amount"]["value"], "25.00")
        self.assertEqual(PayPalPayment.objects.get(order__number=order_id).state, PayPalPayment.REFUNDED)

    def test_unknown_refund_is_checked_under_the_same_request_id(self) -> None:
        order_id = self.captured_order()
        self.transport.queue(httpx.ReadTimeout("no reply"), httpx.ReadTimeout("no reply"))
        response = self.refund(order_id, "5.00", "slow")
        self.assertEqual(response.status_code, 504)  # type: ignore[attr-defined]
        self.transport.queue(stubs.json_response(201, stubs.refund("RF1", "5.00")),
                             stubs.json_response(200, stubs.capture("CAP1", "25.00")))
        again = self.refund(order_id, "5.00", "slow")
        self.assertEqual(again.status_code, 201)  # type: ignore[attr-defined]
        ids = {r.headers["paypal-request-id"] for r in self.transport.requests if r.url.endswith("/refund")}
        self.assertEqual(len(ids), 1)

    def test_refund_requires_an_idempotency_key(self) -> None:
        order_id = self.captured_order()
        response = self.post("/api/orders/%s/refunds" % order_id, {"amount": "1.00"})
        self.assertEqual(response.status_code, 400)

    def test_other_shoppers_cannot_refund(self) -> None:
        order_id = self.captured_order()
        self.as_user(self.bob)
        self.assertEqual(self.refund(order_id, "1.00", "x").status_code, 404)  # type: ignore[attr-defined]

    def test_refund_before_fulfilment_is_refused(self) -> None:
        order_id = self.authorized_order()
        self.assertEqual(self.refund(order_id, "1.00", "x").status_code, 409)  # type: ignore[attr-defined]
