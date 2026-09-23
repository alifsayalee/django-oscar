"""Orchestration + access-control tests. The PayPal gateway is faked, so these
exercise the app's own logic: idempotency, the payment state machine, the refund
guard, stale-authorization renewal, and per-shopper isolation.
"""

import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from oscar.core.loading import get_model

from apps.paypal_checkout import errors
from apps.paypal_checkout.models import PayPalPayment
from apps.paypal_checkout.services import cards, orders

Product = get_model("catalogue", "Product")
ProductClass = get_model("catalogue", "ProductClass")
Partner = get_model("partner", "Partner")
StockRecord = get_model("partner", "StockRecord")
Bankcard = get_model("payment", "Bankcard")
User = get_user_model()

CARD = {"number": "4111111111111111", "expiry": "2030-01", "security_code": "123", "name": "T"}


def make_product(price="15.00"):
    pc, _ = ProductClass.objects.get_or_create(name="Books", defaults={"track_stock": True})
    product = Product.objects.create(product_class=pc, title="A Book", structure=Product.STANDALONE)
    partner, _ = Partner.objects.get_or_create(name="Acme")
    StockRecord.objects.create(
        product=product,
        partner=partner,
        partner_sku="SKU-%s" % product.id,
        price_currency="GBP",
        price=Decimal(price),
        num_in_stock=100,
    )
    return product


def auth_ok(auth_id="AUTH1"):
    return {
        "paypal_order_id": "ORDER1",
        "authorization_id": auth_id,
        "authorization_status": "CREATED",
        "expiration_time": None,
    }


def capture_ok(amount=Decimal("15.00")):
    return {
        "capture_id": "CAP1",
        "status": "COMPLETED",
        "amount": amount,
        "paypal_fee": Decimal("0.88"),
        "net_amount": amount - Decimal("0.88"),
    }


class FlowTests(TestCase):
    def setUp(self):
        self.shopper = User.objects.create_user("shopper", "s@e.com", "pw-123456789")
        self.operator = User.objects.create_user("op", "o@e.com", "pw-123456789", is_staff=True)
        self.product = make_product()

    def _place(self, user=None):
        return orders.place_order(user or self.shopper, [{"product_id": self.product.id, "quantity": 1}])

    def test_pay_is_idempotent(self):
        payment = self._place()
        with mock.patch.object(orders.gateway, "authorize_with_card", return_value=auth_ok()) as m:
            orders.pay(self.shopper, payment.order, card=CARD)
            orders.pay(self.shopper, payment.order, card=CARD)
        self.assertEqual(m.call_count, 1)  # a double-click authorizes once
        payment.refresh_from_db()
        self.assertEqual(payment.state, PayPalPayment.AUTHORIZED)
        self.assertEqual(payment.authorization_id, "AUTH1")

    def test_full_capture_and_refund_states(self):
        payment = self._place()
        with mock.patch.object(orders.gateway, "authorize_with_card", return_value=auth_ok()):
            orders.pay(self.shopper, payment.order, card=CARD)
        with mock.patch.object(orders.gateway, "get_authorization", return_value={"status": "CREATED", "expiration_time": None}), \
             mock.patch.object(orders.gateway, "capture", return_value=capture_ok()):
            orders.fulfil(payment.order)
        payment.refresh_from_db()
        self.assertEqual(payment.state, PayPalPayment.CAPTURED)
        self.assertEqual(payment.paypal_fee, Decimal("0.88"))
        self.assertEqual(payment.net_amount, Decimal("14.12"))
        self.assertEqual(payment.captured_amount, Decimal("15.00"))

        # Partial refund -> PARTIALLY_REFUNDED
        with mock.patch.object(orders.gateway, "refund", return_value={"refund_id": "R1", "status": "COMPLETED", "amount": Decimal("5.00")}):
            orders.refund(payment.order, "key-1", amount="5.00")
        payment.refresh_from_db()
        self.assertEqual(payment.state, PayPalPayment.PARTIALLY_REFUNDED)
        self.assertEqual(payment.refundable_amount, Decimal("10.00"))

        # Remaining refund -> REFUNDED
        with mock.patch.object(orders.gateway, "refund", return_value={"refund_id": "R2", "status": "COMPLETED", "amount": Decimal("10.00")}):
            orders.refund(payment.order, "key-2", amount="10.00")
        payment.refresh_from_db()
        self.assertEqual(payment.state, PayPalPayment.REFUNDED)
        self.assertEqual(payment.refundable_amount, Decimal("0.00"))

    def test_refund_idempotency_and_overrefund_guard(self):
        payment = self._capture()
        with mock.patch.object(orders.gateway, "refund", return_value={"refund_id": "R1", "status": "COMPLETED", "amount": Decimal("5.00")}) as m:
            _, r1 = orders.refund(payment.order, "same-key", amount="5.00")
            _, r2 = orders.refund(payment.order, "same-key", amount="5.00")
        self.assertEqual(m.call_count, 1)          # same key never refunds twice
        self.assertEqual(r1.pk, r2.pk)

        # A distinct partial refund is legitimate...
        with mock.patch.object(orders.gateway, "refund", return_value={"refund_id": "R2", "status": "COMPLETED", "amount": Decimal("5.00")}):
            orders.refund(payment.order, "other-key", amount="5.00")
        # ...but never beyond what was captured.
        with self.assertRaises(errors.Conflict):
            orders.refund(payment.order, "third-key", amount="10.00")

    def test_cancel_voids_before_fulfilment(self):
        payment = self._place()
        with mock.patch.object(orders.gateway, "authorize_with_card", return_value=auth_ok()):
            orders.pay(self.shopper, payment.order, card=CARD)
        with mock.patch.object(orders.gateway, "void", return_value="VOIDED") as m:
            orders.cancel(payment.order)
        m.assert_called_once()
        payment.refresh_from_db()
        self.assertEqual(payment.state, PayPalPayment.CANCELLED)

    def test_cannot_cancel_after_capture(self):
        payment = self._capture()
        with self.assertRaises(errors.Conflict):
            orders.cancel(payment.order)

    def test_stale_authorization_is_renewed_at_fulfilment(self):
        payment = self._place()
        with mock.patch.object(orders.gateway, "authorize_with_card", return_value=auth_ok()):
            orders.pay(self.shopper, payment.order, card=CARD)
        with mock.patch.object(orders.gateway, "get_authorization", return_value={"status": "EXPIRED", "expiration_time": None}), \
             mock.patch.object(orders.gateway, "reauthorize", return_value={"id": "AUTH2", "status": "CREATED", "expiration_time": None}) as re, \
             mock.patch.object(orders.gateway, "capture", return_value=capture_ok()) as cap:
            orders.fulfil(payment.order)
        re.assert_called_once()
        cap.assert_called_once()
        payment.refresh_from_db()
        self.assertEqual(payment.authorization_id, "AUTH2")
        self.assertEqual(payment.state, PayPalPayment.CAPTURED)

    def test_unrenewable_authorization_reports_actionable_conflict(self):
        payment = self._place()
        with mock.patch.object(orders.gateway, "authorize_with_card", return_value=auth_ok()):
            orders.pay(self.shopper, payment.order, card=CARD)
        with mock.patch.object(orders.gateway, "get_authorization", return_value={"status": "EXPIRED", "expiration_time": None}), \
             mock.patch.object(orders.gateway, "reauthorize", side_effect=errors.PayPalRejected("cannot", status_code=422)):
            with self.assertRaises(errors.Conflict) as ctx:
                orders.fulfil(payment.order)
        self.assertIn("re-collect", ctx.exception.message)

    def _capture(self):
        payment = self._place()
        with mock.patch.object(orders.gateway, "authorize_with_card", return_value=auth_ok()):
            orders.pay(self.shopper, payment.order, card=CARD)
        with mock.patch.object(orders.gateway, "get_authorization", return_value={"status": "CREATED", "expiration_time": None}), \
             mock.patch.object(orders.gateway, "capture", return_value=capture_ok()):
            orders.fulfil(payment.order)
        payment.refresh_from_db()
        return payment


class SavedCardTests(TestCase):
    def setUp(self):
        self.a = User.objects.create_user("a", "a@e.com", "pw-123456789")
        self.b = User.objects.create_user("b", "b@e.com", "pw-123456789")

    def test_save_card_stores_no_pan(self):
        with mock.patch.object(cards.gateway, "create_vault_token", return_value={"token_id": "TOK1", "customer_id": "C1", "card": {"brand": "VISA", "last_digits": "1111", "expiry": "2030-01", "name": "T"}}):
            bc = cards.save_card(self.a, CARD)
        self.assertEqual(bc.partner_reference, "TOK1")
        self.assertTrue(bc.number.endswith("1111"))
        self.assertNotIn("4111", bc.number)
        self.assertEqual(bc.card_type, "VISA")

    def test_delete_card_scoped_to_owner(self):
        with mock.patch.object(cards.gateway, "create_vault_token", return_value={"token_id": "TOK1", "customer_id": "C1", "card": {"brand": "VISA", "last_digits": "1111", "expiry": "2030-01", "name": "T"}}):
            bc = cards.save_card(self.a, CARD)
        # Shopper B cannot delete shopper A's card.
        with self.assertRaises(errors.NotFound):
            cards.delete_card(self.b, bc.id)
        # Owner can.
        with mock.patch.object(cards.gateway, "delete_vault_token", return_value=None):
            cards.delete_card(self.a, bc.id)
        self.assertFalseIfExists(bc.id)

    def assertFalseIfExists(self, pk):
        self.assertFalse(Bankcard.objects.filter(id=pk).exists())


class AccessControlTests(TestCase):
    def setUp(self):
        self.a = User.objects.create_user("a", "a@e.com", "pw-123456789")
        self.b = User.objects.create_user("b", "b@e.com", "pw-123456789")
        self.operator = User.objects.create_user("op", "o@e.com", "pw-123456789", is_staff=True)
        self.product = make_product()
        self.payment_a = orders.place_order(self.a, [{"product_id": self.product.id, "quantity": 1}])

    def _client(self, user):
        c = Client(SERVER_NAME="localhost")
        c.force_login(user)
        return c

    def test_anonymous_gets_401(self):
        c = Client(SERVER_NAME="localhost")
        r = c.get("/api/my-orders")
        self.assertEqual(r.status_code, 401)

    def test_shopper_cannot_see_another_shoppers_order(self):
        c = self._client(self.b)
        r = c.post(f"/api/orders/{self.payment_a.order_id}/pay",
                   data=json.dumps({"card": CARD}), content_type="application/json")
        self.assertEqual(r.status_code, 404)

    def test_operator_endpoints_require_staff(self):
        c = self._client(self.a)
        r = c.post(f"/api/orders/{self.payment_a.order_id}/fulfil", data="{}", content_type="application/json")
        self.assertEqual(r.status_code, 403)
        r = c.get("/api/reconciliation?from=2020-01-01T00:00:00Z&to=2020-01-10T00:00:00Z")
        self.assertEqual(r.status_code, 403)

    def test_my_orders_lists_only_own(self):
        orders.place_order(self.b, [{"product_id": self.product.id, "quantity": 1}])
        c = self._client(self.a)
        data = c.get("/api/my-orders").json()
        self.assertEqual(len(data["orders"]), 1)
        self.assertEqual(data["orders"][0]["orderId"], self.payment_a.order_id)
