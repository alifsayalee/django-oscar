import json
from datetime import timedelta
from typing import Any

import httpx
from django.test import override_settings
from paypal.core import HttpRequest, JsonBody
from paypal.models.enums import AuthorizationStatus, CaptureStatus, OrderStatus, RefundStatus

from apps.payments import outcomes
from apps.payments.models import PaypalPayment, ProviderWrite, SavedCard
from apps.payments.paypal_client import build_client, set_client

from .helpers import (
    CARD,
    TEST_PAYPAL,
    ApiTestCase,
    StubTransport,
    authorization,
    capture_body,
    json_response,
    now,
    order_body,
    paypal_error,
    refund_body,
)


def body_of(request: HttpRequest) -> dict[str, Any]:
    assert isinstance(request.body, JsonBody)
    value = request.body.value
    assert isinstance(value, dict)
    return value


class OrderPlacementTests(ApiTestCase):
    def test_places_order_from_catalogue_prices_awaiting_payment(self) -> None:
        response = self.post("/api/orders", {"items": [{"productId": self.product.pk, "quantity": 3}]})
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertIn("orderId", data)
        self.assertEqual(data["status"], "Awaiting payment")
        self.assertEqual(data["total"], "30.00")
        self.assertEqual(data["currency"], "USD")
        self.assertEqual(data["payment"]["state"], "awaiting_payment")
        self.assertEqual(self.transport.api_requests, [])

    def test_rejects_unknown_product_and_bad_quantity(self) -> None:
        self.assertEqual(self.post("/api/orders", {"items": [{"productId": 999999, "quantity": 1}]}).status_code, 400)
        self.assertEqual(
            self.post("/api/orders", {"items": [{"productId": self.product.pk, "quantity": 0}]}).status_code, 400
        )

    def test_requires_login(self) -> None:
        self.client.logout()
        self.assertEqual(self.post("/api/orders", {"items": []}).status_code, 401)
        self.assertEqual(self.client.get("/api/my-orders").status_code, 401)


class PayTests(ApiTestCase):
    def test_authorizes_exact_total_and_never_stores_card(self) -> None:
        number = self.place_order()
        self.transport.add(json_response(201, order_body(auth=authorization())))
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})

        self.assertEqual(response.status_code, 200, response.content)
        payment = response.json()["payment"]
        self.assertEqual(payment["state"], "authorized")
        self.assertEqual(payment["authorization"]["id"], "AUTH1")
        self.assertEqual(response.json()["status"], "Payment authorized")

        request = self.transport.api_requests[0]
        self.assertTrue(request.url.endswith("/v2/checkout/orders"))
        sent = body_of(request)
        self.assertEqual(sent["intent"], "AUTHORIZE")
        self.assertEqual(sent["purchase_units"][0]["amount"], {"currency_code": "USD", "value": "20.00"})
        self.assertEqual(sent["payment_source"]["card"]["number"], "4111111111111111")
        order_pk = PaypalPayment.objects.get().order_id
        self.assertEqual(request.headers["paypal-request-id"], f"test:o{order_pk}:authorize:1")

        stored = json.dumps(
            list(ProviderWrite.objects.values()) + list(PaypalPayment.objects.values()), default=str
        )
        self.assertNotIn("4111111111111111", stored)
        self.assertNotIn("2030-12", stored)

    def test_double_click_authorizes_once(self) -> None:
        number = self.paid_order()
        calls = len(self.transport.api_requests)
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.transport.api_requests), calls)

    def test_declined_card_can_be_retried_under_a_new_reference(self) -> None:
        number = self.place_order()
        self.transport.add(paypal_error(422))
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()["error"], "payment_declined")

        self.transport.add(json_response(201, order_body(auth=authorization())))
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 200)
        ids = [r.headers["paypal-request-id"] for r in self.transport.api_requests]
        self.assertTrue(ids[0].endswith(":authorize:1") and ids[1].endswith(":authorize:2"))

    def test_denied_authorization_is_not_success(self) -> None:
        number = self.place_order()
        self.transport.add(json_response(201, order_body(auth=authorization(status="DENIED"))))
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(PaypalPayment.objects.get().state, "awaiting_payment")

    def test_unlisted_status_is_unknown_not_paid(self) -> None:
        number = self.place_order()
        self.transport.add(json_response(201, order_body(auth=authorization(status="SOMETHING_NEW"))))
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "Awaiting payment")
        self.assertEqual(ProviderWrite.objects.get().outcome, "unknown")

    def test_payer_action_required_is_reported_not_built(self) -> None:
        number = self.place_order()
        self.transport.add(json_response(201, order_body(status="PAYER_ACTION_REQUIRED")))
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()["error"], "payer_action_required")

    def test_read_timeout_is_settled_by_same_reference_resend(self) -> None:
        number = self.place_order()
        self.transport.add(httpx.ReadTimeout("no reply"), json_response(200, order_body(auth=authorization())))
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 200)
        ids = {r.headers["paypal-request-id"] for r in self.transport.api_requests}
        self.assertEqual(len(ids), 1)  # the check re-sent under the SAME reference

    def test_unsent_and_unknown_failures_are_different(self) -> None:
        number = self.place_order()
        self.transport.add(httpx.ConnectError("refused"))
        unsent = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual((unsent.status_code, unsent.json().get("outcomeUnknown", False)), (502, False))
        self.assertFalse(ProviderWrite.objects.exists())  # nothing left: nothing happened

        self.transport.add(httpx.ReadTimeout("no reply"), httpx.ReadTimeout("still nothing"))
        unknown = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual((unknown.status_code, unknown.json()["outcomeUnknown"]), (504, True))
        self.assertEqual(ProviderWrite.objects.get().outcome, "unknown")

        # Repeating the request settles it under the same reference - no new authorization.
        self.transport.add(json_response(200, order_body(auth=authorization())))
        settled = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(settled.status_code, 200)
        refs = {r.headers["paypal-request-id"] for r in self.transport.api_requests}
        self.assertEqual(len(refs), 1)

    def test_amount_mismatch_is_flagged_for_review(self) -> None:
        number = self.place_order()
        self.transport.add(json_response(201, order_body(auth=authorization(value="19.99"))))
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(ProviderWrite.objects.get().outcome, "needs_review")

    def test_bad_credentials_are_a_configuration_error(self) -> None:
        transport = StubTransport()
        transport.queue = [json_response(401, {"error": "invalid_client", "error_description": "bad"})]
        set_client(build_client(transport))
        number = self.place_order()
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "paypal_configuration")
        self.assertFalse(ProviderWrite.objects.exists())

    def test_invalid_card_is_rejected_before_paypal(self) -> None:
        number = self.place_order()
        response = self.post(f"/api/orders/{number}/pay", {"card": {**CARD, "number": "4111111111111112"}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.transport.api_requests, [])


class OwnershipTests(ApiTestCase):
    def test_other_shoppers_cannot_see_or_act_on_an_order(self) -> None:
        number = self.captured_order()
        self.as_user(self.other)
        self.assertEqual(self.client.get(f"/api/orders/{number}").status_code, 404)
        self.assertEqual(self.post(f"/api/orders/{number}/pay", {"card": CARD}).status_code, 404)
        self.assertEqual(
            self.post(f"/api/orders/{number}/refunds", {"idempotencyKey": "k", "amount": "1.00"}).status_code, 404
        )
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"], [])

    def test_operator_actions_require_staff(self) -> None:
        number = self.paid_order()
        self.assertEqual(self.post(f"/api/orders/{number}/fulfil").status_code, 403)
        self.assertEqual(self.post(f"/api/orders/{number}/cancel").status_code, 403)
        self.assertEqual(self.client.get("/api/reconciliation?from=2026-01-01T00:00:00Z&to=2026-01-02T00:00:00Z").status_code, 403)


class FulfilTests(ApiTestCase):
    def test_capture_records_amount_fee_and_net(self) -> None:
        number = self.captured_order()
        payment = self.client.get(f"/api/orders/{number}").json()["payment"]
        self.assertEqual(payment["state"], "captured")
        self.assertEqual(payment["capture"]["amount"], "20.00")
        self.assertEqual(payment["capture"]["paypalFee"], "1.07")
        self.assertEqual(payment["capture"]["netAmount"], "18.93")
        capture_request = self.transport.api_requests[-1]
        self.assertTrue(capture_request.url.endswith("/v2/payments/authorizations/AUTH1/capture"))
        self.assertEqual(body_of(capture_request)["amount"], {"currency_code": "USD", "value": "20.00"})

    def test_fulfil_twice_captures_once(self) -> None:
        number = self.captured_order()
        calls = len(self.transport.api_requests)
        self.as_user(self.operator)
        self.assertEqual(self.post(f"/api/orders/{number}/fulfil").status_code, 200)
        self.assertEqual(len(self.transport.api_requests), calls)

    def test_stale_authorization_is_renewed_then_captured(self) -> None:
        created = now() - timedelta(days=5)
        number = self.paid_order(auth=authorization(created=created))
        self.as_user(self.operator)
        self.transport.add(
            json_response(200, authorization(created=created)),
            json_response(201, authorization(auth_id="AUTH2")),
            json_response(201, capture_body()),
        )
        response = self.post(f"/api/orders/{number}/fulfil")
        self.assertEqual(response.status_code, 200, response.content)
        urls = [r.url for r in self.transport.api_requests[-3:]]
        self.assertTrue(urls[1].endswith("/authorizations/AUTH1/reauthorize"))
        self.assertTrue(urls[2].endswith("/authorizations/AUTH2/capture"))
        auth = response.json()["payment"]["authorization"]
        self.assertEqual((auth["id"], auth["originalId"]), ("AUTH2", "AUTH1"))

    def test_authorization_too_old_to_renew_says_what_to_do(self) -> None:
        created = now() - timedelta(days=29, hours=1)
        stale = authorization(created=created)
        stale.pop("expiration_time")
        number = self.paid_order(auth=stale)
        self.as_user(self.operator)
        self.transport.add(json_response(200, stale))
        response = self.post(f"/api/orders/{number}/fulfil")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "authorization_not_renewable")
        self.assertIn("Cancel order", response.json()["message"])

    def test_refused_renewal_says_what_to_do(self) -> None:
        created = now() - timedelta(days=5)
        number = self.paid_order(auth=authorization(created=created))
        self.as_user(self.operator)
        self.transport.add(json_response(200, authorization(created=created)), paypal_error(422, issue="REAUTH_REFUSED"))
        response = self.post(f"/api/orders/{number}/fulfil")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "authorization_not_renewable")

    def test_pending_capture_is_not_complete(self) -> None:
        number = self.paid_order()
        self.as_user(self.operator)
        self.transport.add(json_response(200, authorization()), json_response(201, capture_body(status="PENDING")))
        response = self.post(f"/api/orders/{number}/fulfil")
        self.assertEqual(response.status_code, 202)
        self.assertNotEqual(response.json()["status"], "Complete")


class CancelTests(ApiTestCase):
    def test_cancel_voids_the_hold(self) -> None:
        number = self.paid_order()
        self.as_user(self.operator)
        self.transport.add(json_response(200, authorization(status="VOIDED")))
        response = self.post(f"/api/orders/{number}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "Cancelled")
        self.assertEqual(response.json()["payment"]["state"], "voided")
        self.assertTrue(self.transport.api_requests[-1].url.endswith("/authorizations/AUTH1/void"))

    def test_cancel_unpaid_order_calls_nothing(self) -> None:
        number = self.place_order()
        self.as_user(self.operator)
        self.assertEqual(self.post(f"/api/orders/{number}/cancel").status_code, 200)
        self.assertEqual(self.transport.api_requests, [])

    def test_cannot_cancel_after_capture(self) -> None:
        number = self.captured_order()
        self.as_user(self.operator)
        self.assertEqual(self.post(f"/api/orders/{number}/cancel").status_code, 409)


class RefundTests(ApiTestCase):
    def test_partial_refunds_never_exceed_the_capture(self) -> None:
        number = self.captured_order()
        self.transport.add(json_response(201, refund_body("R1", "5.00")))
        first = self.post(f"/api/orders/{number}/refunds", {"idempotencyKey": "a", "amount": "5.00"})
        self.assertEqual(first.status_code, 201, first.content)
        self.assertIn("refundId", first.json())

        self.transport.add(json_response(201, refund_body("R2", "10.00")))
        second = self.post(f"/api/orders/{number}/refunds", {"idempotencyKey": "b", "amount": "10.00"})
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()["order"]["payment"]["state"], "partially_refunded")
        self.assertEqual(second.json()["order"]["payment"]["refundableAmount"], "5.00")

        calls = len(self.transport.api_requests)
        too_much = self.post(f"/api/orders/{number}/refunds", {"idempotencyKey": "c", "amount": "5.01"})
        self.assertEqual(too_much.status_code, 422)
        self.assertEqual(len(self.transport.api_requests), calls)

        self.transport.add(json_response(201, refund_body("R3", "5.00")))
        rest = self.post(f"/api/orders/{number}/refunds", {"idempotencyKey": "d"})
        self.assertEqual(rest.status_code, 201)
        self.assertEqual(rest.json()["order"]["payment"]["state"], "refunded")

    def test_same_key_refunds_once(self) -> None:
        number = self.captured_order()
        self.transport.add(json_response(201, refund_body("R1", "5.00")))
        first = self.post(f"/api/orders/{number}/refunds", {"amount": "5.00"}, HTTP_IDEMPOTENCY_KEY="key-1")
        calls = len(self.transport.api_requests)
        again = self.post(f"/api/orders/{number}/refunds", {"amount": "5.00"}, HTTP_IDEMPOTENCY_KEY="key-1")
        self.assertEqual(again.status_code, 201)
        self.assertEqual(again.json()["refundId"], first.json()["refundId"])
        self.assertEqual(len(self.transport.api_requests), calls)
        mismatch = self.post(f"/api/orders/{number}/refunds", {"amount": "6.00"}, HTTP_IDEMPOTENCY_KEY="key-1")
        self.assertEqual(mismatch.status_code, 422)

    def test_refund_requires_key_and_capture(self) -> None:
        number = self.paid_order()
        self.assertEqual(self.post(f"/api/orders/{number}/refunds", {"amount": "1.00"}).status_code, 400)
        self.assertEqual(
            self.post(f"/api/orders/{number}/refunds", {"idempotencyKey": "x", "amount": "1.00"}).status_code, 409
        )


class SavedCardTests(ApiTestCase):
    def save(self) -> dict[str, Any]:
        self.transport.add(
            json_response(
                201,
                {"id": "TOKEN1", "customer": {"id": "CUST1"},
                 "payment_source": {"card": {"last_digits": "1111", "brand": "VISA", "expiry": "2030-12"}}},
            )
        )
        response = self.post("/api/payment-methods", {"card": CARD})
        self.assertEqual(response.status_code, 201, response.content)
        data: dict[str, Any] = response.json()
        return data

    def test_save_list_pay_delete(self) -> None:
        saved = self.save()
        self.assertEqual((saved["brand"], saved["lastDigits"]), ("VISA", "1111"))
        self.assertNotIn("number", json.dumps(saved))
        method_id = saved["paymentMethodId"]
        self.assertEqual(
            [c["paymentMethodId"] for c in self.client.get("/api/payment-methods").json()["paymentMethods"]],
            [method_id],
        )
        self.assertNotIn("4111111111111111", json.dumps(list(SavedCard.objects.values()), default=str))

        number = self.place_order()
        self.transport.add(json_response(201, order_body(auth=authorization())))
        paid = self.post(f"/api/orders/{number}/pay", {"paymentMethodId": method_id})
        self.assertEqual(paid.status_code, 200)
        self.assertEqual(body_of(self.transport.api_requests[-1])["payment_source"], {"card": {"vault_id": "TOKEN1"}})

        self.transport.add(json_response(204, {}))
        self.assertEqual(self.client.delete(f"/api/payment-methods/{method_id}").status_code, 204)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        second = self.place_order()
        self.assertEqual(self.post(f"/api/orders/{second}/pay", {"paymentMethodId": method_id}).status_code, 404)

    def test_saving_the_same_card_twice_vaults_once(self) -> None:
        first = self.save()
        calls = len(self.transport.api_requests)
        again = self.post("/api/payment-methods", {"card": CARD})
        self.assertEqual(again.json()["paymentMethodId"], first["paymentMethodId"])
        self.assertEqual(len(self.transport.api_requests), calls)

    def test_other_shoppers_cannot_see_use_or_delete_a_card(self) -> None:
        method_id = self.save()["paymentMethodId"]
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        self.assertEqual(self.client.delete(f"/api/payment-methods/{method_id}").status_code, 404)
        number = self.place_order()
        self.assertEqual(self.post(f"/api/orders/{number}/pay", {"paymentMethodId": method_id}).status_code, 404)

    def test_unconfirmed_vault_delete_still_removes_the_card(self) -> None:
        method_id = self.save()["paymentMethodId"]
        self.transport.add(httpx.ConnectError("refused"))
        response = self.client.delete(f"/api/payment-methods/{method_id}")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        self.transport.add(json_response(204, {}))
        self.assertEqual(self.client.delete(f"/api/payment-methods/{method_id}").status_code, 204)


class ReconciliationTests(ApiTestCase):
    def test_walks_every_page_and_lines_up_both_sides(self) -> None:
        number = self.captured_order()
        start = (now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = (now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def txn(txn_id: str, value: str, custom: str = "") -> dict[str, Any]:
            return {"transaction_info": {
                "transaction_id": txn_id, "transaction_event_code": "T0006",
                "transaction_initiation_date": now().strftime("%Y-%m-%dT%H:%M:%S+0000"),
                "transaction_amount": {"currency_code": "USD", "value": value},
                "transaction_status": "S", "custom_field": custom}}

        self.transport.add(
            json_response(200, {"transaction_details": [txn("CAP1", "20.00")], "page": 1, "total_pages": 2}),
            json_response(200, {"transaction_details": [txn("STRANGER", "9.99", "elsewhere")], "page": 2, "total_pages": 2}),
        )
        self.as_user(self.operator)
        response = self.client.get("/api/reconciliation", {"from": start, "to": end})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual(report["paypal"]["pagesFetched"], 2)
        self.assertEqual([m["paypal"]["transactionId"] for m in report["matched"]], ["CAP1"])
        self.assertTrue(report["matched"][0]["amountMatches"])
        self.assertEqual([t["transactionId"] for t in report["paypalOnly"]], ["STRANGER"])
        self.assertEqual([w["paypalId"] for w in report["localOnly"]], ["AUTH1"])
        self.assertEqual(report["localOnly"][0]["orderId"], number)
        pages = [r.url for r in self.transport.api_requests[-2:]]
        self.assertIn("page=1", pages[0])
        self.assertIn("page=2", pages[1])

    def test_long_range_is_split_into_31_day_windows(self) -> None:
        self.transport.add(*[json_response(200, {"transaction_details": [], "total_pages": 0})] * 3)
        self.as_user(self.operator)
        response = self.client.get(
            "/api/reconciliation", {"from": "2026-01-01T00:00:00Z", "to": "2026-03-15T00:00:00Z"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["paypal"]["pagesFetched"], 3)

    def test_rejects_bad_range(self) -> None:
        self.as_user(self.operator)
        self.assertEqual(self.client.get("/api/reconciliation", {"from": "x", "to": "y"}).status_code, 400)


class ConfigurationTests(ApiTestCase):
    @override_settings(**{**TEST_PAYPAL, "PAYPAL_BASE_URL": "https://paypal.example.test"})
    def test_base_url_override_is_used_for_every_call_including_token(self) -> None:
        transport = StubTransport()
        set_client(build_client(transport))
        number = self.place_order()
        transport.add(json_response(201, order_body(auth=authorization())))
        self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertTrue(transport.requests)
        self.assertTrue(all(r.url.startswith("https://paypal.example.test/") for r in transport.requests))
        self.assertTrue(transport.requests[0].url.endswith("/v1/oauth2/token"))


    @override_settings(**{**TEST_PAYPAL, "PAYPAL_ENVIRONMENT": "live"})
    def test_unknown_environment_without_base_url_is_not_configured(self) -> None:
        set_client(None)
        number = self.place_order()
        response = self.post(f"/api/orders/{number}/pay", {"card": CARD})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "not_configured")

    @override_settings(**{**TEST_PAYPAL, "PAYPAL_CURRENCY": ""})
    def test_missing_currency_is_not_configured(self) -> None:
        response = self.post("/api/orders", {"items": [{"productId": self.product.pk, "quantity": 1}]})
        self.assertEqual(response.status_code, 503)


class OutcomeMappingTests(ApiTestCase):
    def test_every_status_member_is_mapped(self) -> None:
        self.assertEqual(outcomes.authorization_outcome(AuthorizationStatus.CREATED), "done")
        self.assertEqual(outcomes.authorization_outcome(AuthorizationStatus.PENDING), "pending")
        self.assertEqual(outcomes.authorization_outcome(AuthorizationStatus.DENIED), "failed")
        self.assertEqual(outcomes.authorization_outcome("NEW_VALUE"), "unknown")
        self.assertEqual(outcomes.authorization_outcome(None), "unknown")
        self.assertEqual(outcomes.order_outcome(OrderStatus.APPROVED, None), "pending")
        self.assertEqual(outcomes.void_outcome(AuthorizationStatus.VOIDED), "done")
        self.assertEqual(outcomes.void_outcome(AuthorizationStatus.CAPTURED), "failed")
        self.assertEqual(outcomes.capture_outcome(CaptureStatus.REFUNDED), "failed")
        self.assertEqual(outcomes.capture_outcome(CaptureStatus.PENDING), "pending")
        self.assertEqual(outcomes.refund_outcome(RefundStatus.CANCELLED), "failed")
        self.assertEqual(outcomes.refund_outcome(RefundStatus.COMPLETED), "done")
