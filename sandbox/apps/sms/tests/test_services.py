"""Service-layer tests (DB), with the Twilio gateway faked — no network, no real messages."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from oscar.apps.catalogue.models import Product, ProductClass
from oscar.apps.partner.models import Partner, StockRecord

from apps.sms import services
from apps.sms.models import ContactNumber, Notification, NotificationKind, Outcome
from apps.sms.provider import SendResult

User = get_user_model()

TEST_TO = "+18254751588"


def ok_result(sid="SMfake", outcome="delivered", status="delivered"):
    now = timezone.now()
    return SendResult(
        sid=sid, outcome=outcome, status=status, error_code=None, error_message="",
        outcome_unknown=False, date_sent=now, date_created=now,
    )


@override_settings(
    TWILIO_ACCOUNT_SID="ACxxx", TWILIO_AUTH_TOKEN="secret",
    TWILIO_FROM_NUMBER="+15550000000", TWILIO_MESSAGING_SERVICE_SID="MGxxx",
)
class ServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="shopper", email="shopper@example.com", password="passw0rd!!"
        )
        # A minimal purchasable product (no stock tracking -> always available).
        pc = ProductClass.objects.create(
            name="Widget", track_stock=False, requires_shipping=False
        )
        self.product = Product.objects.create(
            product_class=pc, title="Widget", structure=Product.STANDALONE
        )
        partner = Partner.objects.create(name="P")
        StockRecord.objects.create(
            product=self.product, partner=partner, partner_sku="W1",
            price=Decimal("9.99"), price_currency="GBP",
        )
        ContactNumber.objects.create(owner=self.user, e164=TEST_TO)

    def _place(self):
        with mock.patch.object(services.provider, "send_immediate", return_value=ok_result()):
            order, notification = services.place_order(
                self.user, [{"product_id": self.product.id, "quantity": 1}]
            )
        return order, notification

    def test_place_order_creates_order_and_placed_notification(self):
        order, notification = self._place()
        self.assertEqual(order.user, self.user)
        self.assertEqual(order.status, "Pending")
        self.assertEqual(order.lines.count(), 1)
        self.assertEqual(notification.kind, NotificationKind.ORDER_PLACED)
        self.assertEqual(notification.provider_sid, "SMfake")

    def test_shopper_with_no_number_is_not_messaged(self):
        ContactNumber.objects.filter(owner=self.user).delete()
        with mock.patch.object(services.provider, "send_immediate") as send:
            order, notification = services.place_order(
                self.user, [{"product_id": self.product.id, "quantity": 1}]
            )
        self.assertIsNone(notification)
        send.assert_not_called()
        self.assertTrue(order.pk)  # order still placed

    def test_dispatch_notifies_and_schedules_followup_once(self):
        order, _ = self._place()
        with mock.patch.object(services.provider, "send_immediate", return_value=ok_result("SMd")) as send, \
             mock.patch.object(services.provider, "schedule_followup", return_value=ok_result("SMf", "scheduled", "scheduled")) as sched:
            order, changed = services.dispatch_order(order)
            self.assertTrue(changed)
            # Dispatching again is a clean no-op: no second message, no second schedule.
            order, changed2 = services.dispatch_order(order)
        self.assertFalse(changed2)
        self.assertEqual(order.status, "Being processed")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(sched.call_count, 1)
        self.assertEqual(
            Notification.objects.filter(order=order, kind=NotificationKind.DELIVERY_FOLLOWUP).count(), 1
        )

    def test_cancel_calls_off_followup_before_notifying(self):
        order, _ = self._place()
        with mock.patch.object(services.provider, "send_immediate", return_value=ok_result("SMd")), \
             mock.patch.object(services.provider, "schedule_followup", return_value=ok_result("SMf", "scheduled", "scheduled")):
            order, _ = services.dispatch_order(order)
        followup = Notification.objects.get(order=order, kind=NotificationKind.DELIVERY_FOLLOWUP)
        self.assertEqual(followup.outcome, "scheduled")

        with mock.patch.object(services.provider, "send_immediate", return_value=ok_result("SMc")) as send, \
             mock.patch.object(services.provider, "cancel_scheduled", return_value=ok_result("SMf", "canceled", "canceled")) as cancel:
            order, changed = services.cancel_order(order)
        self.assertTrue(changed)
        self.assertEqual(order.status, "Cancelled")
        cancel.assert_called_once_with("SMf")
        followup.refresh_from_db()
        self.assertEqual(followup.outcome, "canceled")
        self.assertEqual(send.call_count, 1)  # the cancellation notice

    def test_cancel_twice_is_noop(self):
        order, _ = self._place()
        with mock.patch.object(services.provider, "send_immediate", return_value=ok_result("SMc")) as send, \
             mock.patch.object(services.provider, "cancel_scheduled", return_value=ok_result()):
            order, changed = services.cancel_order(order)
            order, changed2 = services.cancel_order(order)
        self.assertTrue(changed)
        self.assertFalse(changed2)
        self.assertEqual(send.call_count, 1)

    def test_resend_idempotency_same_key_no_second_message(self):
        _, placed = self._place()
        with mock.patch.object(services.provider, "send_immediate", return_value=ok_result("SMr1")) as send:
            n1, sent1 = services.resend_notification(placed, "key-1")
            n2, sent2 = services.resend_notification(placed, "key-1")  # repeat
        self.assertTrue(sent1)
        self.assertFalse(sent2)
        self.assertEqual(n1.pk, n2.pk)
        self.assertEqual(send.call_count, 1)

        with mock.patch.object(services.provider, "send_immediate", return_value=ok_result("SMr2")) as send2:
            n3, sent3 = services.resend_notification(placed, "key-2")  # fresh key
        self.assertTrue(sent3)
        self.assertNotEqual(n3.pk, n1.pk)
        self.assertEqual(send2.call_count, 1)

    def test_dispose_content_redacts_and_survives(self):
        _, placed = self._place()
        with mock.patch.object(services.provider, "redact_message", return_value=ok_result("SMfake")) as redact:
            services.dispose_content(placed)
        redact.assert_called_once_with("SMfake")
        placed.refresh_from_db()
        self.assertTrue(placed.content_disposed)
        self.assertEqual(placed.body, "")
        self.assertTrue(Notification.objects.filter(pk=placed.pk).exists())  # record survives

    def test_dispose_content_unconfirmed_raises(self):
        _, placed = self._place()
        failed = SendResult(None, "failed", "", None, "no", False, None, None)
        with mock.patch.object(services.provider, "redact_message", return_value=failed):
            with self.assertRaises(services.ServiceError):
                services.dispose_content(placed)

    def test_register_rejects_unusable_number(self):
        with mock.patch.object(services.provider, "lookup_number", return_value=None):
            with self.assertRaises(services.InvalidRequest):
                services.register_contact_number(self.user, "garbage")

    def test_reconcile_buckets(self):
        order, placed = self._place()  # placed has sid SMfake, provider_date_sent ~ now
        # A local row the provider will NOT return -> local_only.
        Notification.objects.filter(pk=placed.pk).update(
            provider_sid="SM_local_only", provider_date_sent=timezone.now()
        )
        start = timezone.now() - timedelta(hours=1)
        end = timezone.now() + timedelta(hours=1)

        # Fake provider list: one matching our sid + one provider-only.
        matched_msg = mock.Mock()
        matched_msg.sid = "SM_local_only"
        matched_msg.date_sent = "Tue, 23 Sep 2026 10:00:00 +0000"
        matched_msg.date_created = matched_msg.date_sent
        # Give it a real "now" so it lands in window:
        now_str = timezone.now().strftime("%a, %d %b %Y %H:%M:%S +0000")
        matched_msg.date_sent = now_str
        other = mock.Mock()
        other.sid = "SM_provider_only"
        other.date_sent = now_str
        other.status = "delivered"

        def fake_list(from_number, a, b, **kw):
            return [matched_msg, other], False

        with mock.patch.object(services.provider, "list_messages_from", side_effect=fake_list), \
             mock.patch.object(services.provider, "fetch_status", return_value=None):
            report = services.reconcile(start, end)

        matched_sids = {n.provider_sid for (n, _m, _s) in report["matched"]}
        provider_only_sids = {sid for (sid, _m, _s) in report["provider_only"]}
        self.assertIn("SM_local_only", matched_sids)
        self.assertIn("SM_provider_only", provider_only_sids)
