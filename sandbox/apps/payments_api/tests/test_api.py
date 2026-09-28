import json
from datetime import timedelta
from decimal import Decimal as D
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from oscar.core.loading import get_model
from oscar.test.factories import create_product

from apps.payments_api.models import PayPalPayment, PayPalRefund, ProviderWrite, SavedCard
from apps.payments_api.paypal_gateway.client import set_client

from . import stubs
from .stubs import RoutingTransport, json_response, paypal_error

Country = get_model("address", "Country")

CARD = {"number": "4111 1111 1111 1111", "expiry": "2030-12", "securityCode": "123", "name": "Alice Shopper",
        "billingAddress": {"addressLine1": "1 Main St", "city": "San Jose", "state": "CA", "postalCode": "95131",
                           "countryCode": "US"}}
SHIP = {"firstName": "Alice", "lastName": "Shopper", "line1": "1 Main St", "city": "London", "postcode": "N1 1AA",
        "countryCode": "GB"}

ORDERS = r"/v2/checkout/orders$"
AUTH_GET = r"/v2/payments/authorizations/[^/]+$"
CAPTURE = r"/v2/payments/authorizations/[^/]+/capture$"
REAUTH = r"/v2/payments/authorizations/[^/]+/reauthorize$"
VOID = r"/v2/payments/authorizations/[^/]+/void$"
REFUND = r"/v2/payments/captures/[^/]+/refund$"
VAULT = r"/v3/vault/payment-tokens$"
VAULT_ONE = r"/v3/vault/payment-tokens/[^/]+$"
SEARCH = r"/v1/reporting/transactions$"


@override_settings(PAYPAL_CLIENT_ID="test-id", PAYPAL_CLIENT_SECRET="test-secret", PAYPAL_ENVIRONMENT="sandbox",
                   PAYPAL_CURRENCY="USD", PAYPAL_BASE_URL="", PAYPAL_REFERENCE_PREFIX="t")
class ApiTestCase(TestCase):
    def setUp(self):
        self.pp = RoutingTransport()
        set_client(stubs.stub_client(self.pp))
        self.addCleanup(set_client, None)
        sleeper = mock.patch("time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.product = create_product(price=D("20.00"), num_in_stock=50)
        Country.objects.get_or_create(iso_3166_1_a2="GB", defaults={"name": "United Kingdom",
                                                                    "is_shipping_country": True})
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw-123456789")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw-123456789")
        self.staff = User.objects.create_user("op", "op@example.com", "pw-123456789", is_staff=True)

    def call(self, user, method, url, body=None, **headers):
        client = Client()
        if user is not None:
            client.force_login(user)
        kwargs = {"headers": headers} if headers else {}
        if method == "get":
            return client.get(url, **kwargs)
        if method == "delete":
            return client.delete(url, **kwargs)
        return client.post(url, data=json.dumps(body or {}), content_type="application/json", **kwargs)

    def place(self, user=None, quantity=1):
        r = self.call(user or self.alice, "post", "/api/orders",
                      {"items": [{"productId": self.product.pk, "quantity": quantity}], "shippingAddress": SHIP})
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["orderId"]

    def pay(self, number, body=None, user=None):
        return self.call(user or self.alice, "post", f"/api/orders/{number}/pay", body or {"card": CARD})

    def authorized_order(self):
        self.pp.on("POST", ORDERS, json_response(201, stubs.paypal_order(stubs.authorization())))
        number = self.place()
        self.assertEqual(self.pay(number).status_code, 200)
        return number

    def captured_order(self):
        number = self.authorized_order()
        self.pp.on("GET", AUTH_GET, json_response(200, stubs.authorization()))
        self.pp.on("POST", CAPTURE, json_response(201, stubs.capture()))
        r = self.call(self.staff, "post", f"/api/orders/{number}/fulfil")
        self.assertEqual(r.status_code, 200, r.content)
        return number


class OrderAndPayTests(ApiTestCase):
    def test_order_uses_oscar_models_and_awaits_payment(self):
        number = self.place(quantity=2)
        order = get_model("order", "Order").objects.get(number=number)
        self.assertEqual(order.lines.get().quantity, 2)
        self.assertEqual(order.total_incl_tax, D("40.00"))
        self.assertEqual(order.currency, "USD")
        self.assertEqual(order.paypal_payment.status, PayPalPayment.AWAITING)

    def test_pay_puts_a_hold_for_the_exact_total_under_a_derived_reference(self):
        self.pp.on("POST", ORDERS, json_response(201, stubs.paypal_order(stubs.authorization())))
        number = self.place()
        r = self.pay(number)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["payment"]["status"], "authorized")
        self.assertEqual(r.json()["payment"]["card"]["lastDigits"], "1111")

        [req] = self.pp.calls("POST", ORDERS)
        body = req.body.value
        self.assertEqual(body["intent"], "AUTHORIZE")
        self.assertEqual(body["purchase_units"][0]["amount"], {"currency_code": "USD", "value": "20.00"})
        self.assertEqual(req.headers["paypal-request-id"], f"t:o{number}:pay:0")
        self.assertEqual(body["purchase_units"][0]["custom_id"], f"t-{number}")
        self.assertEqual(req.headers["prefer"], "return=representation")
        # The card went to PayPal and nowhere else.
        self.assertEqual(body["payment_source"]["card"]["number"], "4111111111111111")
        for row in ProviderWrite.objects.values_list("reference", "detail"):
            self.assertNotIn("4111", " ".join(row))

    def test_the_same_pay_twice_makes_one_provider_call(self):
        self.pp.on("POST", ORDERS, json_response(201, stubs.paypal_order(stubs.authorization())))
        number = self.place()
        first, second = self.pay(number), self.pay(number)
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(len(self.pp.calls("POST", ORDERS)), 1)

    def test_a_hold_in_flight_elsewhere_is_answered_in_progress_without_a_call(self):
        number = self.place()
        ProviderWrite.objects.create(reference=f"t:o{number}:pay:0", operation="pay",
                                     claimed_at=stubs.now())
        r = self.pay(number)
        self.assertEqual(r.status_code, 202)
        self.assertEqual(self.pp.calls("POST", ORDERS), [])

    def test_a_denied_hold_is_not_done_and_the_next_attempt_gets_a_new_reference(self):
        self.pp.on("POST", ORDERS,
                   json_response(201, stubs.paypal_order(stubs.authorization(status="DENIED"))),
                   json_response(201, stubs.paypal_order(stubs.authorization(auth_id="AUTH2"))))
        number = self.place()
        r = self.pay(number)
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.json()["error"]["code"], "payment_declined")
        self.assertEqual(PayPalPayment.objects.get(order__number=number).status, PayPalPayment.AWAITING)
        self.assertEqual(self.pay(number).status_code, 200)
        refs = [c.headers["paypal-request-id"] for c in self.pp.calls("POST", ORDERS)]
        self.assertEqual(refs, [f"t:o{number}:pay:0", f"t:o{number}:pay:1"])

    def test_an_unlisted_status_is_unknown_and_a_repeat_settles_it_under_the_same_reference(self):
        self.pp.on("POST", ORDERS,
                   json_response(201, stubs.paypal_order(stubs.authorization(status="SOMETHING_NEW"))),
                   json_response(201, stubs.paypal_order(stubs.authorization())))
        number = self.place()
        r = self.pay(number)
        self.assertEqual(r.status_code, 504)
        self.assertTrue(r.json()["error"]["outcomeUnknown"])
        self.assertEqual(self.pay(number).status_code, 200)
        refs = {c.headers["paypal-request-id"] for c in self.pp.calls("POST", ORDERS)}
        self.assertEqual(refs, {f"t:o{number}:pay:0"})

    def test_payer_action_required_is_reported_not_built(self):
        self.pp.on("POST", ORDERS, json_response(200, stubs.paypal_order(None, status="PAYER_ACTION_REQUIRED")))
        r = self.pay(self.place())
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.json()["error"]["code"], "payer_action_required")

    def test_an_echoed_amount_that_differs_needs_review(self):
        self.pp.on("POST", ORDERS, json_response(201, stubs.paypal_order(stubs.authorization(value="2.00"))))
        number = self.place()
        r = self.pay(number)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "needs_review")
        self.assertEqual(PayPalPayment.objects.get(order__number=number).status, PayPalPayment.NEEDS_REVIEW)

    def test_unsent_is_not_the_same_failure_as_unknown(self):
        self.pp.on("POST", ORDERS, httpx.ConnectError("refused"))
        unsent = self.pay(self.place())
        self.pp = RoutingTransport()
        set_client(stubs.stub_client(self.pp))
        self.pp.on("POST", ORDERS, httpx.ReadTimeout("no reply"))
        unknown = self.pay(self.place())
        self.assertEqual((unsent.status_code, unsent.json()["error"]["outcomeUnknown"]), (502, False))
        self.assertEqual((unknown.status_code, unknown.json()["error"]["outcomeUnknown"]), (504, True))
        outcomes = dict(ProviderWrite.objects.values_list("operation", "outcome"))
        self.assertEqual(outcomes, {"pay": "unknown"})  # the unsent claim was released; the unknown one kept

    def test_bad_credentials_are_our_configuration_problem(self):
        self.pp.routes.clear()
        self.pp.on("POST", r"/v1/oauth2/token$", json_response(401, {"error": "invalid_client"}))
        r = self.pay(self.place())
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["error"]["code"], "paypal_auth_failed")

    def test_a_decline_from_paypal_is_a_402(self):
        self.pp.on("POST", ORDERS, paypal_error(422, "INSTRUMENT_DECLINED"))
        r = self.pay(self.place())
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.json()["error"]["issues"], ["INSTRUMENT_DECLINED"])

    def test_invalid_card_input_is_rejected_before_paypal(self):
        r = self.pay(self.place(), {"card": dict(CARD, number="4111111111111112")})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn(b"4111111111111112", r.content)
        self.assertEqual(self.pp.requests, [])


class AccessTests(ApiTestCase):
    def test_anonymous_callers_must_sign_in(self):
        self.assertEqual(self.call(None, "get", "/api/my-orders").status_code, 401)

    def test_a_shopper_cannot_see_or_act_on_another_shoppers_order(self):
        number = self.place()
        self.assertEqual(self.pay(number, user=self.bob).status_code, 404)
        self.assertEqual(self.call(self.bob, "post", f"/api/orders/{number}/refunds",
                                   {"idempotencyKey": "k"}).status_code, 404)
        self.assertEqual(self.call(self.bob, "get", "/api/my-orders").json()["orders"], [])

    def test_operator_actions_need_staff(self):
        number = self.place()
        for action in ("fulfil", "cancel"):
            self.assertEqual(self.call(self.alice, "post", f"/api/orders/{number}/{action}").status_code, 403)
        self.assertEqual(self.call(self.alice, "get", "/api/reconciliation?from=2026-01-01T00:00:00Z"
                                                      "&to=2026-01-02T00:00:00Z").status_code, 403)

    def test_state_changing_calls_must_be_json(self):
        client = Client()
        client.force_login(self.alice)
        self.assertEqual(client.post("/api/orders", {"items": "x"}).status_code, 415)


class FulfilCancelTests(ApiTestCase):
    def test_fulfil_captures_and_records_what_paypal_reported(self):
        number = self.captured_order()
        payment = PayPalPayment.objects.get(order__number=number)
        self.assertEqual(payment.status, PayPalPayment.CAPTURED)
        self.assertEqual((payment.captured_amount, payment.paypal_fee, payment.net_amount),
                         (D("20.00"), D("0.88"), D("19.12")))
        self.assertEqual(payment.order.status, "Complete")
        self.assertEqual(payment.source.amount_debited, D("20.00"))
        [cap] = self.pp.calls("POST", CAPTURE)
        self.assertEqual(cap.body.value["amount"], {"currency_code": "USD", "value": "20.00"})
        # Fulfilling again answers from the record.
        self.assertEqual(self.call(self.staff, "post", f"/api/orders/{number}/fulfil").status_code, 200)
        self.assertEqual(len(self.pp.calls("POST", CAPTURE)), 1)

    def test_a_pending_capture_is_accepted_not_done(self):
        number = self.authorized_order()
        self.pp.on("GET", AUTH_GET, json_response(200, stubs.authorization()))
        self.pp.on("POST", CAPTURE, json_response(201, stubs.capture(status="PENDING")))
        r = self.call(self.staff, "post", f"/api/orders/{number}/fulfil")
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json()["payment"]["status"], "capture_pending")
        self.assertNotEqual(r.json()["status"], "Complete")

    def test_a_stale_hold_is_renewed_before_capture(self):
        old = stubs.now() - timedelta(days=5)
        self.pp.on("POST", ORDERS, json_response(201, stubs.paypal_order(stubs.authorization(created=old))))
        number = self.place()
        self.pay(number)
        self.pp.on("GET", AUTH_GET, json_response(200, stubs.authorization(created=old)))
        self.pp.on("POST", REAUTH, json_response(201, stubs.authorization(auth_id="AUTH2")))
        self.pp.on("POST", CAPTURE, json_response(201, stubs.capture()))
        r = self.call(self.staff, "post", f"/api/orders/{number}/fulfil")
        self.assertEqual(r.status_code, 200, r.content)
        [cap] = self.pp.calls("POST", CAPTURE)
        self.assertIn("/authorizations/AUTH2/capture", cap.url)

    def test_a_hold_that_can_no_longer_be_renewed_says_what_to_do(self):
        old = stubs.now() - timedelta(days=5)
        self.pp.on("POST", ORDERS, json_response(201, stubs.paypal_order(stubs.authorization(created=old))))
        number = self.place()
        self.pay(number)
        self.pp.on("GET", AUTH_GET, json_response(200, stubs.authorization(created=old)))
        self.pp.on("POST", REAUTH, paypal_error(422, "REAUTHORIZATION_NOT_ALLOWED"))
        r = self.call(self.staff, "post", f"/api/orders/{number}/fulfil")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "authorization_expired")
        self.assertIn(f"/api/orders/{number}/pay", r.json()["error"]["message"])
        self.assertEqual(self.pp.calls("POST", CAPTURE), [])
        self.assertEqual(PayPalPayment.objects.get(order__number=number).status, PayPalPayment.AWAITING)

    def test_a_hold_older_than_29_days_is_not_renewed(self):
        old = stubs.now() - timedelta(days=30)
        self.pp.on("POST", ORDERS, json_response(201, stubs.paypal_order(stubs.authorization(created=old))))
        number = self.place()
        self.pay(number)
        self.pp.on("GET", AUTH_GET, json_response(200, stubs.authorization(created=old)))
        r = self.call(self.staff, "post", f"/api/orders/{number}/fulfil")
        self.assertEqual(r.json()["error"]["code"], "authorization_expired")
        self.assertEqual(self.pp.calls("POST", REAUTH), [])

    def test_cancel_releases_the_hold(self):
        number = self.authorized_order()
        self.pp.on("POST", VOID, json_response(200, stubs.authorization(status="VOIDED")))
        r = self.call(self.staff, "post", f"/api/orders/{number}/cancel")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["status"], r.json()["payment"]["status"]), ("Cancelled", "voided"))

    def test_cancel_after_capture_points_to_refunds(self):
        number = self.captured_order()
        r = self.call(self.staff, "post", f"/api/orders/{number}/cancel")
        self.assertEqual(r.json()["error"]["code"], "already_fulfilled")
        self.assertEqual(self.pp.calls("POST", VOID), [])

    def test_cancel_of_an_unpaid_order_moves_no_money(self):
        number = self.place()
        r = self.call(self.staff, "post", f"/api/orders/{number}/cancel")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.pp.requests, [])


class RefundTests(ApiTestCase):
    def refund(self, number, body, user=None):
        return self.call(user or self.staff, "post", f"/api/orders/{number}/refunds", body)

    def test_partial_refunds_and_the_same_key_twice(self):
        number = self.captured_order()
        self.pp.on("POST", REFUND, json_response(201, stubs.refund("R1", value="5.00")),
                   json_response(201, stubs.refund("R2", value="3.00")))
        first = self.refund(number, {"amount": "5.00", "idempotencyKey": "k1"})
        again = self.refund(number, {"amount": "5.00", "idempotencyKey": "k1"})
        other = self.refund(number, {"amount": "3.00", "idempotencyKey": "k2"}, user=self.alice)
        self.assertEqual((first.status_code, again.status_code, other.status_code), (201, 201, 201))
        self.assertEqual(first.json()["refundId"], again.json()["refundId"])
        self.assertEqual(len(self.pp.calls("POST", REFUND)), 2)
        payment = PayPalPayment.objects.get(order__number=number)
        self.assertEqual((payment.refunded_amount, payment.source.amount_refunded), (D("8.00"), D("8.00")))
        self.assertEqual(other.json()["payment"]["status"], "partially_refunded")

    def test_never_refundable_beyond_what_was_captured(self):
        number = self.captured_order()
        self.pp.on("POST", REFUND, json_response(201, stubs.refund(value="15.00")))
        self.assertEqual(self.refund(number, {"amount": "15.00", "idempotencyKey": "a"}).status_code, 201)
        over = self.refund(number, {"amount": "5.01", "idempotencyKey": "b"})
        self.assertEqual(over.status_code, 409)
        self.assertEqual(over.json()["error"]["code"], "exceeds_refundable")
        self.assertEqual(len(self.pp.calls("POST", REFUND)), 1)

    def test_a_reserved_but_unsettled_refund_still_counts(self):
        number = self.captured_order()
        self.pp.on("POST", REFUND, httpx.ReadTimeout("no reply"))
        self.assertEqual(self.refund(number, {"amount": "15.00", "idempotencyKey": "a"}).status_code, 504)
        self.assertEqual(self.refund(number, {"amount": "10.00", "idempotencyKey": "b"}).status_code, 409)

    def test_a_failed_refund_gives_the_amount_back(self):
        number = self.captured_order()
        self.pp.on("POST", REFUND, json_response(201, stubs.refund(status="FAILED", value="20.00")),
                   json_response(201, stubs.refund("R2", value="20.00")))
        self.assertEqual(self.refund(number, {"idempotencyKey": "a"}).status_code, 409)
        full = self.refund(number, {"idempotencyKey": "b"})
        self.assertEqual(full.status_code, 201)
        self.assertEqual(full.json()["payment"]["status"], "refunded")

    def test_a_conflict_is_rechecked_under_the_same_reference(self):
        number = self.captured_order()
        self.pp.on("POST", REFUND, paypal_error(409, "RESOURCE_CONFLICT", name="RESOURCE_CONFLICT"),
                   json_response(201, stubs.refund(value="5.00")))
        r = self.refund(number, {"amount": "5.00", "idempotencyKey": "k"})
        self.assertEqual(r.status_code, 201, r.content)
        refs = {c.headers["paypal-request-id"] for c in self.pp.calls("POST", REFUND)}
        self.assertEqual(len(refs), 1)

    def test_a_key_reused_for_another_amount_is_refused(self):
        number = self.captured_order()
        self.pp.on("POST", REFUND, json_response(201, stubs.refund(value="5.00")))
        self.refund(number, {"amount": "5.00", "idempotencyKey": "k"})
        self.assertEqual(self.refund(number, {"amount": "6.00", "idempotencyKey": "k"}).status_code, 422)

    def test_refund_needs_a_capture_and_a_key(self):
        number = self.authorized_order()
        self.assertEqual(self.refund(number, {"idempotencyKey": "k"}).status_code, 409)
        self.assertEqual(self.refund(number, {}).status_code, 400)
        self.assertEqual(PayPalRefund.objects.count(), 0)


class SavedCardTests(ApiTestCase):
    def test_save_list_use_and_delete(self):
        self.pp.on("POST", VAULT, json_response(201, stubs.vault_token()))
        r = self.call(self.alice, "post", "/api/payment-methods", {"card": CARD})
        self.assertEqual(r.status_code, 201, r.content)
        pm = r.json()["paymentMethodId"]
        self.assertEqual((r.json()["brand"], r.json()["lastDigits"]), ("VISA", "1111"))
        self.assertNotIn("number", r.json())
        # Double submit: the same card, one vault call.
        self.assertEqual(self.call(self.alice, "post", "/api/payment-methods", {"card": CARD}).status_code, 200)
        self.assertEqual(len(self.pp.calls("POST", VAULT)), 1)
        self.assertEqual([c["paymentMethodId"] for c in
                          self.call(self.alice, "get", "/api/payment-methods").json()["paymentMethods"]], [pm])
        # Nothing card-like was stored.
        card = SavedCard.objects.get()
        self.assertNotIn("4111111111111111", json.dumps([str(v) for v in card.__dict__.values()]))

        # Another shopper can neither see, use nor delete it.
        self.assertEqual(self.call(self.bob, "get", "/api/payment-methods").json()["paymentMethods"], [])
        self.assertEqual(self.pay(self.place(self.bob), {"paymentMethodId": pm}, user=self.bob).status_code, 404)
        self.assertEqual(self.call(self.bob, "delete", f"/api/payment-methods/{pm}").status_code, 404)

        # Paying with it sends the vault id, not a card.
        self.pp.on("POST", ORDERS, json_response(201, stubs.paypal_order(stubs.authorization())))
        self.assertEqual(self.pay(self.place(), {"paymentMethodId": pm}).status_code, 200)
        self.assertEqual(self.pp.calls("POST", ORDERS)[-1].body.value["payment_source"], {"card": {"vault_id": "TOK1"}})

        self.pp.on("DELETE", VAULT_ONE, stubs.HttpResponse(status_code=204, headers={}))
        self.assertEqual(self.call(self.alice, "delete", f"/api/payment-methods/{pm}").status_code, 204)
        self.assertEqual(self.call(self.alice, "get", "/api/payment-methods").json()["paymentMethods"], [])
        self.assertEqual(self.pay(self.place(), {"paymentMethodId": pm}).status_code, 404)

    def test_a_refused_delete_keeps_the_card(self):
        self.pp.on("POST", VAULT, json_response(201, stubs.vault_token()))
        pm = self.call(self.alice, "post", "/api/payment-methods", {"card": CARD}).json()["paymentMethodId"]
        self.pp.on("DELETE", VAULT_ONE, paypal_error(400, "INVALID_REQUEST"))
        self.assertEqual(self.call(self.alice, "delete", f"/api/payment-methods/{pm}").status_code, 422)
        self.assertEqual(len(self.call(self.alice, "get", "/api/payment-methods").json()["paymentMethods"]), 1)

    def test_a_vault_answer_without_a_card_is_not_done(self):
        self.pp.on("POST", VAULT, json_response(201, {"id": "TOK1"}))
        r = self.call(self.alice, "post", "/api/payment-methods", {"card": CARD})
        self.assertEqual(r.status_code, 504)
        self.assertEqual(SavedCard.objects.count(), 0)


class ReconciliationTests(ApiTestCase):
    def info(self, tid, value, custom=None, code="T0006"):
        return {"transaction_info": {"transaction_id": tid, "transaction_event_code": code,
                                     "transaction_status": "S", "transaction_initiation_date": stubs.iso(stubs.now()),
                                     "transaction_amount": {"currency_code": "USD", "value": value},
                                     **({"custom_field": custom} if custom else {})}}

    def page(self, details, page, total_pages):
        return json_response(200, {"transaction_details": details, "page": page, "total_pages": total_pages,
                                   "last_refreshed_datetime": stubs.iso(stubs.now() + timedelta(hours=1))})

    def test_covers_every_window_and_page_and_lines_both_sides_up(self):
        number = self.captured_order()
        self.pp.on("GET", SEARCH,
                   self.page([self.info("CAP1", "20.00")], 1, 2),
                   self.page([self.info("STRANGER", "9.99", custom=f"t-999999")], 2, 2),
                   self.page([self.info("OTHERSHOP", "1.00", custom="elsewhere-1")], 1, 1))
        start = stubs.now() - timedelta(days=40)
        end = stubs.now() + timedelta(minutes=5)
        r = self.call(self.staff, "get", f"/api/reconciliation?from={stubs.iso(start)}&to={stubs.iso(end)}")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["paypalPagesRead"], 3)
        pages = [c.url for c in self.pp.calls("GET", SEARCH)]
        self.assertIn("page=2", pages[1])
        self.assertEqual([m["orderId"] for m in body["matched"]], [number])
        self.assertTrue(body["matched"][0]["amountMatches"])
        self.assertEqual({p["transactionId"]: p["attributedToThisShop"] for p in body["providerOnly"]},
                         {"STRANGER": True, "OTHERSHOP": False})
        self.assertEqual(body["localOnly"], [])

    def test_a_local_capture_paypal_does_not_report_is_visible(self):
        self.captured_order()
        self.pp.on("GET", SEARCH, json_response(200, {"transaction_details": [], "total_pages": 1,
                                                      "last_refreshed_datetime": stubs.iso(stubs.now()
                                                                                           + timedelta(hours=1))}))
        start, end = stubs.now() - timedelta(days=1), stubs.now() + timedelta(minutes=5)
        body = self.call(self.staff, "get", f"/api/reconciliation?from={stubs.iso(start)}&to={stubs.iso(end)}").json()
        self.assertEqual([x["transactionId"] for x in body["localOnly"]], ["CAP1"])
