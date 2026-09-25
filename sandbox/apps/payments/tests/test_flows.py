import json
from datetime import timedelta
from decimal import Decimal

import httpx
from django.utils import timezone

from oscar.core.loading import get_model

from apps.payments import safe_write
from apps.payments.models import PaymentOperation, PayPalPayment

from .support import (
    CARD,
    FULL_NUMBER,
    PaymentsTestCase,
    authorization_body,
    capture_body,
    checkout_order_body,
    error_response,
    json_response,
    payment_token_body,
    refund_body,
)

Order = get_model("order", "Order")
Bankcard = get_model("payment", "Bankcard")


def payment_for(order_id):
    return PayPalPayment.objects.get(order__number=order_id)


class AccessTests(PaymentsTestCase):
    def test_anonymous_callers_are_refused(self):
        self.client.logout()
        self.assertEqual(self.client.get("/api/my-orders").status_code, 401)
        self.assertEqual(self.post("/api/orders", {"items": []}).status_code, 401)

    def test_operator_actions_need_staff(self):
        order_id = self.paid_order()
        for action in ("fulfil", "cancel"):
            self.assertEqual(self.post(f"/api/orders/{order_id}/{action}").status_code, 403)
        response = self.client.get(
            "/api/reconciliation", {"from": "2026-09-01T00:00:00Z", "to": "2026-09-02T00:00:00Z"}
        )
        self.assertEqual(response.status_code, 403)

    def test_one_shopper_cannot_see_or_act_on_anothers_order(self):
        order_id = self.fulfilled_order()
        self.as_user(self.other)
        self.assertEqual(self.post(f"/api/orders/{order_id}/pay", {"card": CARD}).status_code, 404)
        response = self.post(f"/api/orders/{order_id}/refunds", {"amount": "1.00"}, Idempotency_Key="k")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get("/api/my-orders").json(), {"orders": []})


class PlaceOrderTests(PaymentsTestCase):
    def test_order_uses_oscar_models_and_awaits_payment(self):
        order_id = self.place_order(quantity=3)
        order = Order.objects.get(number=order_id)
        self.assertEqual(order.status, "Awaiting payment")
        self.assertEqual(order.user, self.shopper)
        self.assertEqual(order.total_incl_tax, Decimal("30.00"))
        self.assertEqual(order.currency, "USD")
        self.assertEqual(order.lines.get().quantity, 3)
        self.assertEqual(payment_for(order_id).lifecycle, PayPalPayment.AWAITING_PAYMENT)
        self.assertEqual(self.transport.calls(), [])  # nothing is sent to PayPal until payment

    def test_idempotency_key_places_one_order(self):
        body = {"items": [{"itemId": self.product.pk, "quantity": 1}]}
        first = self.post("/api/orders", body, Idempotency_Key="order-key-1")
        second = self.post("/api/orders", body, Idempotency_Key="order-key-1")
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(first.json()["orderId"], second.json()["orderId"])
        self.assertEqual(Order.objects.filter(user=self.shopper).count(), 1)

    def test_unknown_item_is_404(self):
        response = self.post("/api/orders", {"items": [{"itemId": 999999, "quantity": 1}]})
        self.assertEqual(response.status_code, 404)


class PayTests(PaymentsTestCase):
    def test_authorizes_exactly_the_order_total_with_a_derived_reference(self):
        order_id = self.place_order()
        self.transport.queue(json_response(200, checkout_order_body(auth=authorization_body())))
        response = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["payment"]["status"], "authorized")
        request = self.transport.operation_requests()[0]
        self.assertEqual((request.method, request.url.split("paypal.com")[1]), ("POST", "/v2/checkout/orders"))
        unit = request.body.value["purchase_units"][0]
        self.assertEqual(request.body.value["intent"], "AUTHORIZE")
        self.assertEqual(unit["amount"], {"currency_code": "USD", "value": "20.00"})
        self.assertEqual(unit["invoice_id"], f"tst-{order_id}-1")
        self.assertEqual(request.headers["prefer"], "return=representation")
        ref = f"tst:{order_id}:pay:1"
        self.assertEqual(request.headers["paypal-request-id"], safe_write.request_id(ref))

        order = Order.objects.get(number=order_id)
        self.assertEqual(order.status, "Payment authorised")
        payment = payment_for(order_id)
        self.assertEqual((payment.authorization_id, payment.card_last_digits), ("AUTH1", "1111"))
        self.assertEqual(payment.source.amount_allocated, Decimal("20.00"))

    def test_the_same_pay_twice_sends_one_authorization(self):
        order_id = self.place_order()
        self.transport.queue(json_response(200, checkout_order_body(auth=authorization_body())))
        first = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        second = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})  # a double click

        self.assertEqual(len(self.transport.calls()), 1)
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(second.json()["payment"]["authorization"]["id"], "AUTH1")

    def test_an_in_flight_attempt_is_answered_in_progress_without_a_call(self):
        order_id = self.place_order()
        payment = payment_for(order_id)
        PayPalPayment.objects.filter(pk=payment.pk).update(lifecycle=PayPalPayment.AUTHORIZING)
        PaymentOperation.objects.create(
            ref=f"tst:{order_id}:pay:1",
            kind=PaymentOperation.CREATE_ORDER,
            payment=payment,
            order=payment.order,
            claimed_at=timezone.now(),
        )
        response = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.transport.calls(), [])

    def test_a_denied_authorization_is_not_done_and_a_new_attempt_is_allowed(self):
        order_id = self.place_order()
        self.transport.queue(json_response(200, checkout_order_body(auth=authorization_body(status="DENIED"))))
        response = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(payment_for(order_id).lifecycle, PayPalPayment.AWAITING_PAYMENT)
        self.assertEqual(Order.objects.get(number=order_id).status, "Awaiting payment")

        self.transport.queue(json_response(200, checkout_order_body(auth=authorization_body(auth_id="AUTH2"))))
        retry = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual(retry.status_code, 200)
        second = self.transport.operation_requests()[1]
        self.assertEqual(second.body.value["purchase_units"][0]["invoice_id"], f"tst-{order_id}-2")

    def test_an_unlisted_status_is_unknown_not_done(self):
        order_id = self.place_order()
        self.transport.queue(json_response(200, checkout_order_body(auth=authorization_body(status="SOMETHING_NEW"))))
        response = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payment_for(order_id).lifecycle, PayPalPayment.AUTHORIZING)
        self.assertEqual(Order.objects.get(number=order_id).status, "Awaiting payment")

    def test_a_held_amount_that_differs_from_the_total_needs_review(self):
        order_id = self.place_order()
        self.transport.queue(json_response(200, checkout_order_body(auth=authorization_body(amount="19.99"))))
        response = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "amount_mismatch")
        self.assertEqual(PaymentOperation.objects.get(kind="create_order").outcome, PaymentOperation.NEEDS_REVIEW)
        self.assertNotEqual(payment_for(order_id).lifecycle, PayPalPayment.AUTHORIZED)

    def test_payer_action_required_is_reported_not_approved(self):
        order_id = self.place_order()
        self.transport.queue(json_response(200, checkout_order_body(order_status="PAYER_ACTION_REQUIRED")))
        response = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "payer_action_required")
        # The operator can still cancel: nothing was ever held.
        self.as_user(self.operator)
        self.assertEqual(self.post(f"/api/orders/{order_id}/cancel").status_code, 200)
        self.assertEqual(len(self.transport.calls()), 1)

    def test_approved_order_is_authorized_in_a_second_step(self):
        order_id = self.place_order()
        self.transport.queue(
            json_response(200, checkout_order_body(order_status="APPROVED")),
            json_response(201, checkout_order_body(auth=authorization_body())),
        )
        response = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.transport.calls()[1], ("POST", "/v2/checkout/orders/ORDER1/authorize"))

    def test_refused_connection_is_known_but_read_timeout_is_unknown(self):
        unsent_order, unknown_order = self.place_order(), self.place_order()

        self.transport.queue(httpx.ConnectError("refused"))
        unsent = self.post(f"/api/orders/{unsent_order}/pay", {"card": CARD})

        self.transport.queue(
            httpx.ReadTimeout("no reply"), json_response(200, {"transaction_details": [], "total_pages": 1})
        )
        unknown = self.post(f"/api/orders/{unknown_order}/pay", {"card": CARD})

        self.assertEqual((unsent.status_code, unsent.json()["error"]["outcomeUnknown"]), (502, False))
        self.assertEqual((unknown.status_code, unknown.json()["error"]["outcomeUnknown"]), (504, True))
        self.assertEqual(payment_for(unsent_order).lifecycle, PayPalPayment.AWAITING_PAYMENT)
        self.assertEqual(payment_for(unknown_order).lifecycle, PayPalPayment.AUTHORIZING)
        # Only the unknown one was looked up, by the invoice id it was sent with.
        self.assertEqual(self.transport.calls()[-1], ("GET", "/v1/reporting/transactions"))

    def test_a_repeat_after_an_unknown_outcome_looks_up_and_never_resends(self):
        order_id = self.place_order()
        self.transport.queue(httpx.ReadTimeout("no reply"), json_response(200, {"transaction_details": []}))
        self.assertEqual(self.post(f"/api/orders/{order_id}/pay", {"card": CARD}).status_code, 504)

        found = {
            "transaction_details": [
                {
                    "transaction_info": {
                        "transaction_id": "AUTH9",
                        "invoice_id": f"tst-{order_id}-1",
                        "transaction_initiation_date": timezone.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                }
            ],
            "total_pages": 1,
        }
        self.transport.queue(json_response(200, found), json_response(200, authorization_body(auth_id="AUTH9")))
        response = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(payment_for(order_id).authorization_id, "AUTH9")
        creates = [c for c in self.transport.calls() if c == ("POST", "/v2/checkout/orders")]
        self.assertEqual(len(creates), 1)  # the create went out once; the repeat only looked it up

    def test_duplicate_invoice_means_an_earlier_attempt_landed(self):
        order_id = self.place_order()
        found = {
            "transaction_details": [
                {
                    "transaction_info": {
                        "transaction_id": "AUTH7",
                        "invoice_id": f"tst-{order_id}-1",
                        "transaction_initiation_date": timezone.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                }
            ],
            "total_pages": 1,
        }
        self.transport.queue(
            error_response(422, "DUPLICATE_INVOICE_ID"),
            json_response(200, found),
            json_response(200, authorization_body(auth_id="AUTH7")),
        )
        response = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(payment_for(order_id).authorization_id, "AUTH7")

    def test_bad_credentials_are_a_configuration_error(self):
        from apps.payments import paypal_gateway as gw

        self.transport = type(self.transport)(json_response(401, {"error": "invalid_client"}))
        gw.use_client(
            gw.build_client(
                gw.PayPalConfig.from_values(
                    client_id="x",
                    client_secret="y",
                    environment="sandbox",
                    currency="USD",
                    base_url=None,
                    timeout=5.0,
                ),
                self.transport,
            )
        )
        order_id = self.place_order()
        response = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (502, "provider_auth"))
        self.assertEqual(payment_for(order_id).lifecycle, PayPalPayment.AWAITING_PAYMENT)

    def test_invalid_card_is_rejected_before_paypal(self):
        order_id = self.place_order()
        response = self.post(f"/api/orders/{order_id}/pay", {"card": {**CARD, "number": "4111111111111112"}})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("4111111111111112", response.content.decode())
        self.assertEqual(self.transport.calls(), [])
        self.assertEqual(payment_for(order_id).lifecycle, PayPalPayment.AWAITING_PAYMENT)


class FulfilTests(PaymentsTestCase):
    def test_fulfil_captures_and_shows_fee_and_net(self):
        order_id = self.paid_order()
        self.as_user(self.operator)
        self.transport.queue(json_response(200, capture_body()))
        response = self.post(f"/api/orders/{order_id}/fulfil")

        self.assertEqual(response.status_code, 200)
        capture = response.json()["payment"]["capture"]
        self.assertEqual((capture["amount"], capture["paypalFee"], capture["netAmount"]), ("20.00", "0.88", "19.12"))
        request = self.transport.operation_requests()[-1]
        self.assertTrue(request.url.endswith("/v2/payments/authorizations/AUTH1/capture"))
        self.assertEqual(request.body.value["amount"], {"currency_code": "USD", "value": "20.00"})
        self.assertEqual(request.headers["paypal-request-id"], safe_write.request_id(f"tst:{order_id}:capture:AUTH1"))
        self.assertEqual(Order.objects.get(number=order_id).status, "Complete")
        self.assertEqual(payment_for(order_id).source.amount_debited, Decimal("20.00"))

        again = self.post(f"/api/orders/{order_id}/fulfil")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(self.transport.calls()), 2)  # authorize + one capture, never two

    def test_a_pending_capture_is_not_fulfilled(self):
        order_id = self.paid_order()
        self.as_user(self.operator)
        self.transport.queue(json_response(200, capture_body(status="PENDING")))
        response = self.post(f"/api/orders/{order_id}/fulfil")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(Order.objects.get(number=order_id).status, "Payment authorised")
        # A later fulfil re-reads the capture (a GET), and settles it from PayPal's word.
        self.transport.queue(json_response(200, capture_body(status="COMPLETED")))
        response = self.post(f"/api/orders/{order_id}/fulfil")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.transport.calls()[-1], ("GET", "/v2/payments/captures/CAP1"))
        self.assertEqual(Order.objects.get(number=order_id).status, "Complete")

    def test_a_stale_authorization_is_renewed_before_capture(self):
        order_id = self.paid_order()
        PayPalPayment.objects.filter(order__number=order_id).update(authorized_at=timezone.now() - timedelta(days=4))
        self.as_user(self.operator)
        self.transport.queue(
            json_response(200, authorization_body(auth_id="AUTH1-R")),
            json_response(200, capture_body()),
        )
        response = self.post(f"/api/orders/{order_id}/fulfil")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            self.transport.calls()[1:],
            [
                ("POST", "/v2/payments/authorizations/AUTH1/reauthorize"),
                ("POST", "/v2/payments/authorizations/AUTH1-R/capture"),
            ],
        )
        self.assertTrue(response.json()["payment"]["authorization"]["reauthorized"])

    def test_an_expired_authorization_says_what_to_do(self):
        order_id = self.paid_order()
        PayPalPayment.objects.filter(order__number=order_id).update(
            authorized_at=timezone.now() - timedelta(days=30),
            authorization_expires_at=timezone.now() - timedelta(days=1),
        )
        self.as_user(self.operator)
        response = self.post(f"/api/orders/{order_id}/fulfil")
        self.assertEqual(response.status_code, 409)
        error = response.json()["error"]
        self.assertEqual(error["code"], "authorization_not_renewable")
        self.assertIn("Cancel the order", error["message"])
        self.assertEqual(len(self.transport.calls()), 1)  # nothing sent for fulfilment
        self.assertEqual(payment_for(order_id).lifecycle, PayPalPayment.AUTHORIZED)

    def test_renewal_and_capture_both_refused_is_not_renewable(self):
        order_id = self.paid_order()
        PayPalPayment.objects.filter(order__number=order_id).update(authorized_at=timezone.now() - timedelta(days=5))
        self.as_user(self.operator)
        self.transport.queue(
            error_response(422, "REAUTHORIZATION_TOO_SOON"),
            error_response(422, "AUTHORIZATION_EXPIRED"),
        )
        response = self.post(f"/api/orders/{order_id}/fulfil")
        self.assertEqual(response.status_code, 409)
        message = response.json()["error"]["message"]
        self.assertIn("REAUTHORIZATION_TOO_SOON", message)
        self.assertIn("AUTHORIZATION_EXPIRED", message)
        self.assertEqual(payment_for(order_id).lifecycle, PayPalPayment.AUTHORIZED)

    def test_a_capture_timeout_is_rechecked_with_the_same_key(self):
        order_id = self.paid_order()
        self.as_user(self.operator)
        self.transport.queue(httpx.ReadTimeout("no reply"), json_response(200, capture_body()))
        response = self.post(f"/api/orders/{order_id}/fulfil")
        self.assertEqual(response.status_code, 200, response.content)
        first, second = self.transport.operation_requests()[-2:]
        self.assertEqual(first.headers["paypal-request-id"], second.headers["paypal-request-id"])


class CancelTests(PaymentsTestCase):
    def test_cancel_releases_the_hold(self):
        order_id = self.paid_order()
        self.as_user(self.operator)
        self.transport.queue(json_response(200, authorization_body(status="VOIDED")))
        response = self.post(f"/api/orders/{order_id}/cancel")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["payment"]["status"], "voided")
        request = self.transport.operation_requests()[-1]
        self.assertTrue(request.url.endswith("/v2/payments/authorizations/AUTH1/void"))
        self.assertEqual(request.headers["prefer"], "return=representation")
        self.assertEqual(Order.objects.get(number=order_id).status, "Cancelled")
        self.assertEqual(self.post(f"/api/orders/{order_id}/fulfil").status_code, 409)

    def test_an_already_voided_authorization_is_looked_up(self):
        order_id = self.paid_order()
        self.as_user(self.operator)
        self.transport.queue(
            error_response(422, "PREVIOUSLY_VOIDED"), json_response(200, authorization_body(status="VOIDED"))
        )
        self.assertEqual(self.post(f"/api/orders/{order_id}/cancel").status_code, 200)

    def test_cannot_cancel_after_fulfilment(self):
        order_id = self.fulfilled_order()
        self.as_user(self.operator)
        self.assertEqual(self.post(f"/api/orders/{order_id}/cancel").status_code, 409)

    def test_cancel_before_payment_moves_no_money(self):
        order_id = self.place_order()
        self.as_user(self.operator)
        self.assertEqual(self.post(f"/api/orders/{order_id}/cancel").status_code, 200)
        self.assertEqual(self.transport.calls(), [])


class RefundTests(PaymentsTestCase):
    def refund(self, order_id, key, amount=None):
        return self.post(
            f"/api/orders/{order_id}/refunds", {} if amount is None else {"amount": amount}, Idempotency_Key=key
        )

    def test_partial_refunds_never_exceed_the_capture(self):
        order_id = self.fulfilled_order()
        self.transport.queue(json_response(201, refund_body("R1", amount="5.00")))
        first = self.refund(order_id, "k1", "5.00")
        self.assertEqual(first.status_code, 201, first.content)
        self.assertIn("refundId", first.json())

        repeat = self.refund(order_id, "k1", "5.00")  # same key: no second refund
        self.assertEqual(repeat.json()["refundId"], first.json()["refundId"])

        too_much = self.refund(order_id, "k2", "15.01")
        self.assertEqual(too_much.status_code, 422)
        self.assertEqual(too_much.json()["error"]["refundableAmount"], "15.00")

        self.transport.queue(json_response(201, refund_body("R2", amount="15.00")))
        rest = self.refund(order_id, "k3")  # no amount: whatever remains
        self.assertEqual(rest.status_code, 201)
        refunds = [r for r in self.transport.operation_requests() if r.url.endswith("/refund")]
        self.assertEqual([r.body.value["amount"]["value"] for r in refunds], ["5.00", "15.00"])
        self.assertEqual(rest.json()["payment"]["status"], "refunded")
        self.assertEqual(self.refund(order_id, "k4", "0.01").status_code, 422)

    def test_same_key_with_a_different_amount_is_refused(self):
        order_id = self.fulfilled_order()
        self.transport.queue(json_response(201, refund_body("R1", amount="5.00")))
        self.refund(order_id, "k1", "5.00")
        response = self.refund(order_id, "k1", "6.00")
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (422, "idempotency_key_reused"))

    def test_refund_needs_a_key_and_a_capture(self):
        order_id = self.paid_order()
        self.assertEqual(self.post(f"/api/orders/{order_id}/refunds", {"amount": "1.00"}).status_code, 400)
        self.assertEqual(self.refund(order_id, "k1", "1.00").status_code, 409)

    def test_paypal_refusal_releases_the_reservation(self):
        order_id = self.fulfilled_order()
        self.transport.queue(error_response(422, "REFUND_AMOUNT_EXCEEDED"))
        self.assertEqual(self.refund(order_id, "k1", "5.00").status_code, 422)
        self.transport.queue(json_response(201, refund_body("R2", amount="20.00")))
        self.assertEqual(self.refund(order_id, "k2", "20.00").status_code, 201)


class SavedCardTests(PaymentsTestCase):
    def save(self, key="card-1"):
        self.transport.queue(json_response(200, payment_token_body()))
        return self.post("/api/payment-methods", {"card": CARD}, Idempotency_Key=key)

    def test_save_list_pay_and_delete(self):
        saved = self.save()
        self.assertEqual(saved.status_code, 201, saved.content)
        method_id = saved.json()["paymentMethodId"]
        self.assertEqual(saved.json()["lastDigits"], "1111")
        self.assertEqual(
            self.client.get("/api/payment-methods").json()["paymentMethods"][0]["paymentMethodId"], method_id
        )

        order_id = self.place_order()
        self.transport.queue(json_response(200, checkout_order_body(auth=authorization_body())))
        paid = self.post(f"/api/orders/{order_id}/pay", {"paymentMethodId": method_id})
        self.assertEqual(paid.status_code, 200, paid.content)
        body = self.transport.operation_requests()[-1].body.value
        self.assertEqual(body["payment_source"], {"card": {"vault_id": "TOK1"}})

        self.transport.queue(json_response(204, {}))
        deleted = self.client.delete(f"/api/payment-methods/{method_id}")
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(self.transport.operation_requests()[-1].url.endswith("/v3/vault/payment-tokens/TOK1"))
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        order2 = self.place_order()
        self.assertEqual(self.post(f"/api/orders/{order2}/pay", {"paymentMethodId": method_id}).status_code, 404)

    def test_saving_twice_with_one_key_vaults_once(self):
        first, second = self.save("k"), self.post("/api/payment-methods", {"card": CARD}, Idempotency_Key="k")
        self.assertEqual(first.json()["paymentMethodId"], second.json()["paymentMethodId"])
        self.assertEqual(len(self.transport.calls()), 1)

    def test_second_card_reuses_the_paypal_customer(self):
        self.save("k1")
        self.transport.queue(json_response(200, payment_token_body(token_id="TOK2")))
        self.post("/api/payment-methods", {"card": CARD}, Idempotency_Key="k2")
        body = self.transport.operation_requests()[-1].body.value
        self.assertEqual(body["customer"], {"id": "CUST1"})

    def test_cards_belong_to_their_shopper(self):
        method_id = self.save().json()["paymentMethodId"]
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        self.assertEqual(self.client.delete(f"/api/payment-methods/{method_id}").status_code, 404)
        order_id = self.place_order()
        self.assertEqual(self.post(f"/api/orders/{order_id}/pay", {"paymentMethodId": method_id}).status_code, 404)
        self.assertTrue(Bankcard.objects.filter(pk=method_id).exists())

    def test_full_card_details_never_reach_the_database_or_logs(self):
        with self.assertLogs("apps.payments", level="DEBUG") as logs:
            self.save()
            order_id = self.place_order()
            self.transport.queue(json_response(200, checkout_order_body(auth=authorization_body())))
            self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertNotIn(FULL_NUMBER, "\n".join(logs.output))
        self.assertNotIn("4111 1111", "\n".join(logs.output))
        card = Bankcard.objects.get()
        self.assertEqual(card.number, "XXXX-XXXX-XXXX-1111")
        dumped = json.dumps(
            [[op.ref, op.detail, op.fingerprint] for op in PaymentOperation.objects.all()]
            + [[p.card_last_digits, p.card_brand] for p in PayPalPayment.objects.all()]
        )
        self.assertNotIn(FULL_NUMBER, dumped)
        self.assertNotIn("4111 1111", dumped)

    def test_provider_delete_failure_still_removes_the_card_here(self):
        method_id = self.save().json()["paymentMethodId"]
        self.transport.queue(httpx.ConnectError("down"))
        response = self.client.delete(f"/api/payment-methods/{method_id}")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        op = PaymentOperation.objects.get(kind=PaymentOperation.VAULT_DELETE)
        self.assertEqual(op.outcome, PaymentOperation.FAILED)


class RefundRetryTests(PaymentsTestCase):
    def test_a_refused_key_retries_only_within_the_ceiling(self):
        order_id = self.fulfilled_order()
        self.transport.queue(error_response(422, "INTERNAL_REFUSAL"))
        refused = self.post(f"/api/orders/{order_id}/refunds", {"amount": "15.00"}, Idempotency_Key="k1")
        self.assertEqual(refused.status_code, 422)
        self.transport.queue(json_response(201, refund_body("R1", amount="10.00")))
        self.post(f"/api/orders/{order_id}/refunds", {"amount": "10.00"}, Idempotency_Key="k2")
        retry = self.post(f"/api/orders/{order_id}/refunds", {"amount": "15.00"}, Idempotency_Key="k1")
        self.assertEqual((retry.status_code, retry.json()["error"]["refundableAmount"]), (422, "10.00"))
        refunds = [r for r in self.transport.operation_requests() if r.url.endswith("/refund")]
        self.assertEqual(len(refunds), 2)  # the retry never reached PayPal


class PayRaceTests(PaymentsTestCase):
    def test_a_request_that_loses_the_mutex_never_starts_an_attempt(self):
        # Another request has just moved the order to "authorizing" but not yet written its claim.
        order_id = self.place_order()
        PayPalPayment.objects.filter(order__number=order_id).update(lifecycle=PayPalPayment.AUTHORIZING)
        response = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.transport.calls(), [])
        self.assertFalse(PaymentOperation.objects.filter(kind=PaymentOperation.CREATE_ORDER).exists())

    def test_an_orphaned_attempt_is_retaken_once_idle(self):
        order_id = self.place_order()
        PayPalPayment.objects.filter(order__number=order_id).update(
            lifecycle=PayPalPayment.AUTHORIZING, updated_at=timezone.now() - timedelta(minutes=10)
        )
        self.transport.queue(json_response(200, checkout_order_body(auth=authorization_body())))
        response = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(self.transport.calls()), 1)


class ReviewRegressionTests(PaymentsTestCase):
    def test_a_settled_refund_is_never_moved_back_or_applied_twice(self):
        order_id = self.fulfilled_order()
        self.transport.queue(json_response(201, refund_body("R1", amount="5.00")))
        self.post(f"/api/orders/{order_id}/refunds", {"amount": "5.00"}, Idempotency_Key="k1")
        payment = payment_for(order_id)
        op = PaymentOperation.objects.get(kind=PaymentOperation.REFUND)
        from apps.payments import services

        write = services._refund_write(payment, op.ref, amount=op.amount)
        # A concurrent re-check that could not confirm it must not overwrite PayPal's answer...
        self.assertEqual(safe_write.complete(write, PaymentOperation.UNKNOWN).outcome, PaymentOperation.DONE)
        # ...nor can a later "done" apply the refund a second time.
        safe_write.complete(write, PaymentOperation.DONE)
        payment.refresh_from_db()
        self.assertEqual(payment.refunded_amount, Decimal("5.00"))
        self.assertEqual(payment.source.amount_refunded, Decimal("5.00"))

    def test_refund_amount_spelling_does_not_change_the_request(self):
        order_id = self.fulfilled_order()
        self.transport.queue(json_response(201, refund_body("R1", amount="5.00")))
        first = self.post(f"/api/orders/{order_id}/refunds", {"amount": "5"}, Idempotency_Key="k1")
        second = self.post(f"/api/orders/{order_id}/refunds", {"amount": "5.00"}, Idempotency_Key="k1")
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(first.json()["refundId"], second.json()["refundId"])

    def test_an_order_cancelled_elsewhere_cannot_be_paid(self):
        order_id = self.place_order()
        Order.objects.filter(number=order_id).update(status="Cancelled")
        response = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (409, "order_not_payable"))
        self.assertEqual(self.transport.calls(), [])

    def test_an_unreadable_answer_is_recorded_unknown_not_left_sending(self):
        order_id = self.place_order()
        self.transport.queue(json_response(200, checkout_order_body(auth=authorization_body(amount="abc"))))
        response = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual((response.status_code, response.json()["error"]["outcomeUnknown"]), (504, True))
        self.assertEqual(PaymentOperation.objects.get(kind="create_order").outcome, PaymentOperation.UNKNOWN)

    def test_a_failed_second_step_lets_the_shopper_try_again(self):
        order_id = self.place_order()
        self.transport.queue(
            json_response(200, checkout_order_body(order_status="APPROVED")),
            error_response(422, "INSTRUMENT_DECLINED"),
        )
        self.assertEqual(self.post(f"/api/orders/{order_id}/pay", {"card": CARD}).status_code, 422)
        self.transport.queue(json_response(200, checkout_order_body(auth=authorization_body(auth_id="AUTH2"))))
        retry = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual(retry.status_code, 200, retry.content)
        self.assertEqual(payment_for(order_id).authorization_id, "AUTH2")

    def test_a_pending_void_is_refreshed_on_repeat(self):
        order_id = self.paid_order()
        self.as_user(self.operator)
        self.transport.queue(json_response(200, authorization_body(status="PENDING")))
        self.assertEqual(self.post(f"/api/orders/{order_id}/cancel").status_code, 202)
        self.transport.queue(json_response(200, authorization_body(status="VOIDED")))
        response = self.post(f"/api/orders/{order_id}/cancel")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.transport.calls()[-1], ("GET", "/v2/payments/authorizations/AUTH1"))

    def test_an_expired_hold_is_cancelled_without_a_void(self):
        order_id = self.paid_order()
        PayPalPayment.objects.filter(order__number=order_id).update(
            authorization_expires_at=timezone.now() - timedelta(days=1)
        )
        self.as_user(self.operator)
        response = self.post(f"/api/orders/{order_id}/cancel")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(self.transport.calls()), 1)
        self.assertEqual(Order.objects.get(number=order_id).status, "Cancelled")

    def test_an_unknown_renewal_holds_fulfilment_until_rechecked(self):
        order_id = self.paid_order()
        PayPalPayment.objects.filter(order__number=order_id).update(authorized_at=timezone.now() - timedelta(days=4))
        self.as_user(self.operator)
        self.transport.queue(httpx.ReadTimeout("no reply"), httpx.ReadTimeout("no reply"))
        self.assertEqual(self.post(f"/api/orders/{order_id}/fulfil").status_code, 504)
        self.assertEqual(payment_for(order_id).lifecycle, PayPalPayment.CAPTURING)  # never released while in doubt
        self.assertEqual(self.post(f"/api/orders/{order_id}/cancel").status_code, 409)

        PayPalPayment.objects.filter(order__number=order_id).update(updated_at=timezone.now() - timedelta(minutes=5))
        self.transport.queue(
            json_response(200, authorization_body(auth_id="AUTH1-R")), json_response(200, capture_body())
        )
        response = self.post(f"/api/orders/{order_id}/fulfil")
        self.assertEqual(response.status_code, 200, response.content)
        reauths = [r for r in self.transport.operation_requests() if r.url.endswith("/reauthorize")]
        self.assertEqual(len({r.headers["paypal-request-id"] for r in reauths}), 1)  # one key throughout

    def test_a_lookup_past_the_reporting_window_settles_a_never_sent_attempt(self):
        order_id = self.place_order()
        self.transport.queue(httpx.ReadTimeout("no reply"), json_response(200, {"transaction_details": []}))
        self.assertEqual(self.post(f"/api/orders/{order_id}/pay", {"card": CARD}).status_code, 504)
        PaymentOperation.objects.filter(kind="create_order").update(claimed_at=timezone.now() - timedelta(hours=7))
        refreshed = (timezone.now() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.transport.queue(json_response(200, {"transaction_details": [], "last_refreshed_datetime": refreshed}))
        response = self.post(f"/api/orders/{order_id}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(PaymentOperation.objects.get(kind="create_order").detail["error"], "not_found_at_paypal")
        self.assertEqual(payment_for(order_id).lifecycle, PayPalPayment.AWAITING_PAYMENT)
