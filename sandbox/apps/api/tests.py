"""Tests for the PayPal API app.

These stub the SDK's *transport* (its documented test seam) so the real request-building,
serialization and error-decoding pipeline runs with no network. They assert on this app's
behaviour — idempotency, the refund cap, ownership scoping, error translation, reconciliation
paging — not on the SDK's own tested behaviour. A separate live integration script exercises
the real sandbox end to end.
"""

from __future__ import annotations

import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from oscar.core.loading import get_model

from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpResponse

from . import gateway as gw
from .models import PayPalPayment, SavedCard

User = get_user_model()
StockRecord = get_model("partner", "StockRecord")
Product = get_model("catalogue", "Product")
Partner = get_model("partner", "Partner")
ProductClass = get_model("catalogue", "ProductClass")
Country = get_model("address", "Country")


def _ensure_country():
    Country.objects.get_or_create(
        iso_3166_1_a2="US",
        defaults={"printable_name": "United States", "name": "UNITED STATES",
                  "is_shipping_country": True})


# --- stub transport -------------------------------------------------------------------
class StubTransport:
    """Satisfies the SDK sync transport protocol; returns queued responses in order."""

    def __init__(self, *responses: HttpResponse):
        self._responses = list(responses)
        self.requests: list = []

    def send(self, request):
        self.requests.append(request)
        if not self._responses:
            raise AssertionError(f"unexpected PayPal call: {request.method} {request.url}")
        return self._responses.pop(0)

    def close(self):
        pass


def _resp(status: int, body: object) -> HttpResponse:
    return HttpResponse(status_code=status,
                        headers={"content-type": "application/json"},
                        content=json.dumps(body).encode())


def _token() -> HttpResponse:
    return _resp(200, {"access_token": "test-token", "token_type": "Bearer", "expires_in": 3600})


def _install_stub(*responses: HttpResponse) -> StubTransport:
    """Point the gateway singleton at a stub transport for the duration of a test."""
    transport = StubTransport(_token(), *responses)
    gw.gateway._client = PaypalClient(
        custom_http_client=transport,
        oauth2=ClientCredentials(client_id="test", client_secret="test"))
    return transport


# --- fixtures -------------------------------------------------------------------------
def _make_product(price="10.00", stock=10):
    pc, _ = ProductClass.objects.get_or_create(name="Books")
    partner, _ = Partner.objects.get_or_create(name="Test Partner")
    product = Product.objects.create(product_class=pc, title="Test Product", structure="standalone")
    StockRecord.objects.create(
        product=product, partner=partner, partner_sku=f"SKU-{product.pk}",
        price=Decimal(price), price_currency="USD", num_in_stock=stock)
    return product


# PayPal response fixtures ------------------------------------------------------------
def _order_authorized(order_id="PPO-1", auth_id="AUTH-1", status="CREATED"):
    return _resp(201, {
        "id": order_id, "status": "COMPLETED",
        "purchase_units": [{"payments": {"authorizations": [
            {"id": auth_id, "status": status, "amount": {"currency_code": "USD", "value": "20.00"},
             "expiration_time": "2030-01-01T00:00:00Z"}]}}],
    })


def _capture(cap_id="CAP-1", gross="20.00", fee="1.00", net="19.00"):
    return _resp(201, {
        "id": cap_id, "status": "COMPLETED",
        "amount": {"currency_code": "USD", "value": gross},
        "seller_receivable_breakdown": {
            "gross_amount": {"currency_code": "USD", "value": gross},
            "paypal_fee": {"currency_code": "USD", "value": fee},
            "net_amount": {"currency_code": "USD", "value": net}}})


def _authorization(status="CREATED"):
    return _resp(200, {"id": "AUTH-1", "status": status,
                       "amount": {"currency_code": "USD", "value": "20.00"},
                       "expiration_time": "2030-01-01T00:00:00Z"})


@override_settings(PAYPAL_CURRENCY="USD", PAYPAL_CLIENT_ID="test", PAYPAL_CLIENT_SECRET="test")
class PaymentFlowTests(TestCase):
    def setUp(self):
        self.shopper = User.objects.create_user("shopper", "s@example.com", "pw-123456789")
        self.other = User.objects.create_user("other", "o@example.com", "pw-123456789")
        self.staff = User.objects.create_user("staff", "st@example.com", "pw-123456789",
                                               is_staff=True)
        _ensure_country()
        self.product = _make_product(price="10.00", stock=10)
        self.sc = Client(); self.sc.force_login(self.shopper)
        self.oc = Client(); self.oc.force_login(self.staff)

    def tearDown(self):
        gw.gateway._client = None

    def _place_order(self, qty=2):
        r = self.sc.post("/api/orders",
                         data=json.dumps({"items": [{"productId": self.product.pk, "quantity": qty}]}),
                         content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["orderId"]

    def test_place_order_is_awaiting_payment(self):
        order_id = self._place_order()
        payment = PayPalPayment.objects.get(order__number=order_id)
        self.assertEqual(payment.state, PayPalPayment.PENDING)
        self.assertEqual(payment.amount, Decimal("20.00"))

    def test_pay_authorizes_exact_total_and_is_idempotent(self):
        order_id = self._place_order()
        transport = _install_stub(_order_authorized())
        r = self.sc.post(f"/api/orders/{order_id}/pay",
                         data=json.dumps({"card": {"number": "4111111111111111",
                                                   "expiry": "2030-01", "security_code": "123"}}),
                         content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["state"], "authorized")
        # The hold equals the order total to the cent.
        create_req = transport.requests[-1]
        sent = create_req.body.value
        self.assertEqual(sent["purchase_units"][0]["amount"]["value"], "20.00")
        self.assertEqual(sent["intent"], "AUTHORIZE")
        # A payment_source requires the PayPal-Request-Id idempotency header.
        self.assertIn("paypal-request-id", create_req.headers)

        # Second call (double-click) must NOT authorize again — no new PayPal request needed.
        transport2 = _install_stub()  # empty: any PayPal call would fail the stub
        r2 = self.sc.post(f"/api/orders/{order_id}/pay",
                          data=json.dumps({"card": {"number": "4111111111111111",
                                                    "expiry": "2030-01"}}),
                          content_type="application/json")
        self.assertEqual(r2.status_code, 200, r2.content)
        # Only the lazy token fetch may have happened; no order call.
        self.assertEqual(len(transport2.requests), 0)

    def test_fulfil_captures_and_records_fee_net(self):
        order_id = self._place_order()
        _install_stub(_order_authorized())
        self.sc.post(f"/api/orders/{order_id}/pay",
                     data=json.dumps({"card": {"number": "4111111111111111", "expiry": "2030-01"}}),
                     content_type="application/json")
        # fulfil: get_authorization (CREATED) then capture
        _install_stub(_authorization("CREATED"), _capture())
        r = self.oc.post(f"/api/orders/{order_id}/fulfil")
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()
        self.assertEqual(data["state"], "captured")
        self.assertEqual(data["paypalFee"], "1.00")
        self.assertEqual(data["netAmount"], "19.00")

    def test_shopper_cannot_fulfil(self):
        order_id = self._place_order()
        r = self.sc.post(f"/api/orders/{order_id}/fulfil")
        self.assertEqual(r.status_code, 403)

    def test_refund_is_capped_and_idempotent(self):
        order_id = self._place_order()
        _install_stub(_order_authorized())
        self.sc.post(f"/api/orders/{order_id}/pay",
                     data=json.dumps({"card": {"number": "4111111111111111", "expiry": "2030-01"}}),
                     content_type="application/json")
        _install_stub(_authorization("CREATED"), _capture())
        self.oc.post(f"/api/orders/{order_id}/fulfil")

        # partial refund
        _install_stub(_resp(201, {"id": "REF-1", "status": "COMPLETED",
                                  "amount": {"currency_code": "USD", "value": "5.00"}}))
        r = self.oc.post(f"/api/orders/{order_id}/refunds",
                         data=json.dumps({"amount": "5.00", "idempotencyKey": "k1"}),
                         content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["refundId"], "REF-1")

        # same key -> no new PayPal call, same refund id
        transport = _install_stub()
        r2 = self.oc.post(f"/api/orders/{order_id}/refunds",
                          data=json.dumps({"amount": "5.00", "idempotencyKey": "k1"}),
                          content_type="application/json")
        self.assertEqual(r2.json()["refundId"], "REF-1")
        self.assertEqual(len(transport.requests), 0)

        # over-refund (25 remaining) -> 422, no PayPal call
        transport = _install_stub()
        r3 = self.oc.post(f"/api/orders/{order_id}/refunds",
                          data=json.dumps({"amount": "9999", "idempotencyKey": "k2"}),
                          content_type="application/json")
        self.assertEqual(r3.status_code, 422)
        self.assertEqual(len(transport.requests), 0)

    def test_cancel_voids_before_capture(self):
        order_id = self._place_order()
        _install_stub(_order_authorized())
        self.sc.post(f"/api/orders/{order_id}/pay",
                     data=json.dumps({"card": {"number": "4111111111111111", "expiry": "2030-01"}}),
                     content_type="application/json")
        _install_stub(_resp(200, {"id": "AUTH-1", "status": "VOIDED"}))
        r = self.sc.post(f"/api/orders/{order_id}/cancel")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["state"], "voided")

    def test_fulfil_renews_expired_authorization(self):
        order_id = self._place_order()
        _install_stub(_order_authorized())
        self.sc.post(f"/api/orders/{order_id}/pay",
                     data=json.dumps({"card": {"number": "4111111111111111", "expiry": "2030-01"}}),
                     content_type="application/json")
        # get_authorization EXPIRED -> reauthorize (new id) -> capture
        reauth = _resp(201, {"id": "AUTH-2", "status": "CREATED",
                             "amount": {"currency_code": "USD", "value": "20.00"},
                             "expiration_time": "2030-02-01T00:00:00Z"})
        _install_stub(_authorization("EXPIRED"), reauth, _capture(cap_id="CAP-2"))
        r = self.oc.post(f"/api/orders/{order_id}/fulfil")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["state"], "captured")
        payment = PayPalPayment.objects.get(order__number=order_id)
        self.assertEqual(payment.authorization_id, "AUTH-2")

    def test_cross_shopper_cannot_act_on_anothers_order(self):
        order_id = self._place_order()
        oc = Client(); oc.force_login(self.other)
        r = oc.post(f"/api/orders/{order_id}/pay",
                    data=json.dumps({"card": {"number": "4111111111111111", "expiry": "2030-01"}}),
                    content_type="application/json")
        self.assertEqual(r.status_code, 404)

    def test_decode_failure_becomes_unreadable_error(self):
        order_id = self._place_order()
        # A 2xx whose authorizations list is the wrong type -> ValidationError inside SDK.
        bad = _resp(201, {"id": "PPO-1", "status": "COMPLETED",
                          "purchase_units": [{"payments": {"authorizations": "not-a-list"}}]})
        _install_stub(bad)
        r = self.sc.post(f"/api/orders/{order_id}/pay",
                         data=json.dumps({"card": {"number": "4111111111111111", "expiry": "2030-01"}}),
                         content_type="application/json")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["error"], "paypal_unreadable")

    def test_typed_rejection_is_surfaced(self):
        order_id = self._place_order()
        _install_stub(_resp(422, {"name": "UNPROCESSABLE_ENTITY", "message": "bad",
                                  "debug_id": "d1",
                                  "details": [{"issue": "INSTRUMENT_DECLINED",
                                               "description": "declined"}]}))
        r = self.sc.post(f"/api/orders/{order_id}/pay",
                         data=json.dumps({"card": {"number": "4111111111111111", "expiry": "2030-01"}}),
                         content_type="application/json")
        self.assertEqual(r.status_code, 422)
        self.assertIn("INSTRUMENT_DECLINED", r.json()["message"])


@override_settings(PAYPAL_CURRENCY="USD", PAYPAL_CLIENT_ID="test", PAYPAL_CLIENT_SECRET="test")
class SavedCardTests(TestCase):
    def setUp(self):
        self.shopper = User.objects.create_user("shopper", "s@example.com", "pw-123456789")
        self.other = User.objects.create_user("other", "o@example.com", "pw-123456789")
        self.sc = Client(); self.sc.force_login(self.shopper)

    def tearDown(self):
        gw.gateway._client = None

    def _token_response(self):
        return _resp(201, {"id": "TOK-1", "customer": {"id": "CUST-1"},
                           "payment_source": {"card": {"last_digits": "1111", "brand": "VISA",
                                                       "expiry": "2030-01", "name": "A B"}}})

    def test_save_card_stores_only_safe_descriptors(self):
        _install_stub(self._token_response())
        r = self.sc.post("/api/payment-methods",
                         data=json.dumps({"card": {"number": "4111111111111111",
                                                   "expiry": "2030-01", "security_code": "123"}}),
                         content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual(body["last4"], "1111")
        self.assertNotIn("4111111111111111", json.dumps(body))
        card = SavedCard.objects.get(pk=body["paymentMethodId"])
        self.assertEqual(card.user, self.shopper)
        self.assertEqual(card.last_digits, "1111")
        # nothing in the DB row resembles a full PAN
        self.assertNotIn("4111", card.vault_token_id)

    def test_delete_card_removes_and_scopes_to_owner(self):
        _install_stub(self._token_response())
        pm_id = self.sc.post("/api/payment-methods",
                             data=json.dumps({"card": {"number": "4111111111111111",
                                                       "expiry": "2030-01"}}),
                             content_type="application/json").json()["paymentMethodId"]
        # another shopper cannot delete it
        oc = Client(); oc.force_login(self.other)
        self.assertEqual(oc.delete(f"/api/payment-methods/{pm_id}").status_code, 404)
        # owner deletes it (stub returns 204 for the vault delete)
        _install_stub(HttpResponse(status_code=204, headers={}))
        self.assertEqual(self.sc.delete(f"/api/payment-methods/{pm_id}").status_code, 200)
        self.assertFalse(SavedCard.objects.filter(pk=pm_id).exists())

    def test_list_is_scoped_to_caller(self):
        SavedCard.objects.create(user=self.other, vault_token_id="T-OTHER", last_digits="9999")
        _install_stub(self._token_response())
        self.sc.post("/api/payment-methods",
                     data=json.dumps({"card": {"number": "4111111111111111", "expiry": "2030-01"}}),
                     content_type="application/json")
        r = self.sc.get("/api/payment-methods")
        cards = r.json()["paymentMethods"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["last4"], "1111")


@override_settings(PAYPAL_CURRENCY="USD", PAYPAL_CLIENT_ID="test", PAYPAL_CLIENT_SECRET="test")
class ReconciliationTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("staff", "st@example.com", "pw-123456789",
                                               is_staff=True)
        self.oc = Client(); self.oc.force_login(self.staff)

    def tearDown(self):
        gw.gateway._client = None

    def test_reconciliation_pages_through_all_results(self):
        def page(n, total, txn_id):
            return _resp(200, {"transaction_details": [
                {"transaction_info": {"transaction_id": txn_id, "invoice_id": "ORD-X",
                                      "transaction_amount": {"currency_code": "USD", "value": "5.00"},
                                      "transaction_status": "S"}}],
                "page": n, "total_pages": total})
        transport = _install_stub(page(1, 2, "T1"), page(2, 2, "T2"))
        r = self.oc.get("/api/reconciliation?from=2026-01-01T00:00:00Z&to=2026-01-15T00:00:00Z")
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()
        self.assertEqual(data["counts"]["paypalTransactions"], 2)
        # two search pages were fetched (token + 2 pages)
        self.assertEqual(len([q for q in transport.requests if "/v1/reporting/transactions" in q.url]), 2)

    def test_reconciliation_requires_staff(self):
        shopper = User.objects.create_user("shopper", "s@example.com", "pw-123456789")
        c = Client(); c.force_login(shopper)
        r = c.get("/api/reconciliation?from=2026-01-01T00:00:00Z&to=2026-01-15T00:00:00Z")
        self.assertEqual(r.status_code, 403)
