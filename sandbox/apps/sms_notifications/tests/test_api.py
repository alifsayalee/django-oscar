"""
End-to-end tests of the /api/ endpoints against a fake Twilio at the SDK's transport seam.

The real SDK client builds, authenticates and decodes every request; only the network is replaced.
"""
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from oscar.core.loading import get_model
from oscar.test.factories import create_product

from apps.sms_notifications import provider, services
from apps.sms_notifications.models import ContactNumber, Notification, ProviderAction
from apps.sms_notifications.outcomes import answer_status, cancel_outcome, redaction_outcome, status_from_provider

from .fake_twilio import FakeTwilio

Order = get_model("order", "Order")

CA_NUMBER = "+14165550123"  # fictional (555) numbers: nothing here ever reaches a network
US_NUMBER = "+12025550199"
FROM_NUMBER = "+15005550006"

TWILIO_SETTINGS = dict(
    TWILIO_ACCOUNT_SID="AC" + "0" * 32,
    TWILIO_AUTH_TOKEN="test-token-not-a-secret",
    TWILIO_FROM_NUMBER=FROM_NUMBER,
    TWILIO_MESSAGING_SERVICE_SID="MG" + "0" * 32,
    TWILIO_BASE_URL="",
    SMS_NOTIFICATIONS_REFERENCE_PREFIX="test-install",
)


@override_settings(**TWILIO_SETTINGS)
class ApiTestCase(TestCase):
    def setUp(self):
        self.fake = FakeTwilio()
        config = provider.get_config()
        provider.set_client_for_tests(provider.build_client(config, transport=self.fake), config)
        User = get_user_model()
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "pw-shopper")
        self.other = User.objects.create_user("other", "other@example.com", "pw-other")
        self.staff = User.objects.create_user("operator", "op@example.com", "pw-staff", is_staff=True)
        self.product = create_product(price="12.50", num_in_stock=50)

    def tearDown(self):
        provider.set_client_for_tests(None)

    # -- helpers --
    def as_user(self, user):
        self.client.force_login(user)

    def post(self, url, data=None, **headers):
        return self.client.post(url, data=json.dumps(data or {}), content_type="application/json", **headers)

    def register(self, user, number=CA_NUMBER):
        self.as_user(user)
        return self.post("/api/contact-numbers", {"phoneNumber": number})

    def place(self, user, quantity=1):
        self.as_user(user)
        response = self.post("/api/orders", {"lines": [{"productId": self.product.pk, "quantity": quantity}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def dispatch(self, order_id):
        self.as_user(self.staff)
        return self.post("/api/orders/%s/dispatch" % order_id)

    def cancel(self, order_id):
        self.as_user(self.staff)
        return self.post("/api/orders/%s/cancel" % order_id)


class ContactNumberTests(ApiTestCase):
    def test_registration_stores_the_providers_canonical_form(self):
        response = self.register(self.shopper, " +1 416-555-0123 ")
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIn("contactNumberId", body)
        self.assertEqual(body["phoneNumber"], CA_NUMBER)
        self.assertEqual(ContactNumber.objects.get(pk=body["contactNumberId"]).phone_number, CA_NUMBER)

    def test_a_number_the_provider_calls_invalid_is_rejected_at_registration(self):
        response = self.register(self.shopper, "12345")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["validationErrors"], ["TOO_SHORT"])
        self.assertFalse(ContactNumber.objects.exists())

    def test_registering_twice_returns_the_same_number(self):
        first = self.register(self.shopper).json()
        second = self.register(self.shopper)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["contactNumberId"], first["contactNumberId"])

    def test_a_shopper_never_sees_or_deletes_anothers_number(self):
        mine = self.register(self.shopper).json()["contactNumberId"]
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/contact-numbers").json(), {"contactNumbers": []})
        self.assertEqual(self.client.delete("/api/contact-numbers/%s" % mine).status_code, 404)
        self.assertTrue(ContactNumber.objects.filter(pk=mine).exists())

    def test_delete_removes_the_number(self):
        mine = self.register(self.shopper).json()["contactNumberId"]
        response = self.client.delete("/api/contact-numbers/%s" % mine)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/contact-numbers").json(), {"contactNumbers": []})

    def test_anonymous_callers_get_401_not_a_redirect(self):
        self.assertEqual(self.client.get("/api/contact-numbers").status_code, 401)

    def test_provider_refusing_our_credentials_is_not_the_callers_fault(self):
        self.fake.auth_fails = True
        self.assertEqual(self.register(self.shopper).status_code, 502)


class OrderFlowTests(ApiTestCase):
    def test_placing_an_order_reuses_oscars_order_and_tells_the_shopper(self):
        self.register(self.shopper)
        body = self.place(self.shopper, quantity=2)
        order = Order.objects.get(pk=body["orderId"])
        self.assertEqual(order.user, self.shopper)
        self.assertEqual(order.lines.get().quantity, 2)
        [note] = body["notifications"]
        self.assertEqual(note["kind"], "order_placed")
        self.assertEqual(note["outcome"], "pending")  # queued at the provider: accepted, not delivered
        [create] = self.fake.creates()
        self.assertEqual(create.body.fields["To"], CA_NUMBER)
        self.assertEqual(create.body.fields["From"], FROM_NUMBER)

    def test_a_shopper_with_no_number_is_simply_not_messaged(self):
        body = self.place(self.shopper)
        self.assertEqual(body["notifications"][0]["outcome"], "skipped")
        self.assertEqual(self.fake.creates(), [])

    def test_a_message_that_cannot_be_sent_never_fails_the_order(self):
        self.register(self.shopper)
        self.fake.fail_next("http_400")
        body = self.place(self.shopper)
        self.assertEqual(body["notifications"][0]["outcome"], "failed")
        self.assertTrue(Order.objects.filter(pk=body["orderId"]).exists())

    def test_dispatch_is_staff_only(self):
        self.register(self.shopper)
        order_id = self.place(self.shopper)["orderId"]
        self.as_user(self.shopper)
        self.assertEqual(self.post("/api/orders/%s/dispatch" % order_id).status_code, 403)

    def test_dispatch_queues_the_follow_up_with_the_provider(self):
        self.register(self.shopper)
        order_id = self.place(self.shopper)["orderId"]
        response = self.dispatch(order_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "Dispatched")
        notice, follow_up = response.json()["notifications"]
        self.assertEqual((notice["kind"], follow_up["kind"]), ("order_dispatched", "delivery_follow_up"))
        self.assertEqual(follow_up["providerStatus"], "scheduled")
        request = self.fake.creates()[-1]
        self.assertEqual(request.body.fields["ScheduleType"], "fixed")
        self.assertEqual(request.body.fields["MessagingServiceSid"], TWILIO_SETTINGS["TWILIO_MESSAGING_SERVICE_SID"])
        send_at = datetime.fromisoformat(request.body.fields["SendAt"].replace("Z", "+00:00"))
        self.assertGreater(send_at, datetime.now(timezone.utc) + timedelta(days=2, hours=23))

    def test_repeating_a_dispatch_sends_nothing_new(self):
        self.register(self.shopper)
        order_id = self.place(self.shopper)["orderId"]
        self.dispatch(order_id)
        first = len(self.fake.creates())
        self.dispatch(order_id)
        self.assertEqual(len(self.fake.creates()), first)

    def test_cancel_calls_off_the_queued_follow_up(self):
        self.register(self.shopper)
        order_id = self.place(self.shopper)["orderId"]
        follow_up_id = self.dispatch(order_id).json()["notifications"][1]["notificationId"]
        response = self.cancel(order_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "Cancelled")
        self.assertEqual(response.json()["followUpCallOff"]["outcome"], "done")
        follow_up = Notification.objects.get(pk=follow_up_id)
        self.assertEqual(self.fake.messages[follow_up.provider_sid]["status"], "canceled")
        self.assertEqual(follow_up.provider_status, "canceled")
        self.assertEqual(follow_up.outcome, "failed")  # it never reached the shopper, by design
        self.assertEqual(response.json()["notifications"][0]["kind"], "order_cancelled")

    def test_cancel_before_dispatch_blocks_any_later_follow_up(self):
        self.register(self.shopper)
        order_id = self.place(self.shopper)["orderId"]
        self.cancel(order_id)
        order = Order.objects.get(pk=order_id)
        # Even if a dispatch raced the cancel and reached the messaging step, the follow-up claim is taken.
        services.dispatch_notifications(order)
        self.assertFalse(any(r.body.fields.get("ScheduleType") for r in self.fake.creates()))

    def test_a_follow_up_whose_send_is_in_doubt_is_still_called_off(self):
        self.register(self.shopper)
        order_id = self.place(self.shopper)["orderId"]
        self.fake.fail_next(None)  # dispatch notice ok
        self.fake.fail_next("timeout_landed")  # follow-up lands, answer lost
        follow_up = self.dispatch(order_id).json()["notifications"][1]
        self.assertEqual(follow_up["outcome"], "pending")  # found by its reference token: no second create
        self.assertEqual(sum(1 for r in self.fake.creates() if r.body.fields.get("ScheduleType")), 1)
        self.assertEqual(self.cancel(order_id).json()["followUpCallOff"]["outcome"], "done")

    def test_a_shopper_never_sees_anothers_order(self):
        self.register(self.shopper)
        order_id = self.place(self.shopper)["orderId"]
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/orders/%s/notifications" % order_id).status_code, 404)
        self.assertEqual(self.client.get("/api/my-orders").json(), {"orders": []})

    def test_my_orders_reports_where_notifications_got_to(self):
        self.register(self.shopper)
        order_id = self.place(self.shopper)["orderId"]
        self.fake.deliver(Notification.objects.get(order_id=order_id).provider_sid)
        self.as_user(self.shopper)
        [order] = self.client.get("/api/my-orders").json()["orders"]
        self.assertEqual(order["notifications"][0]["outcome"], "done")
        self.assertEqual(order["notifications"][0]["providerStatus"], "delivered")

    def test_removing_a_number_calls_off_follow_ups_queued_for_it(self):
        contact_id = self.register(self.shopper).json()["contactNumberId"]
        order_id = self.place(self.shopper)["orderId"]
        self.dispatch(order_id)
        self.as_user(self.shopper)
        response = self.client.delete("/api/contact-numbers/%s" % contact_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([c["outcome"] for c in response.json()["followUpCallOffs"]], ["done"])
        follow_up = Notification.objects.get(order_id=order_id, kind="delivery_follow_up")
        self.assertEqual(self.fake.messages[follow_up.provider_sid]["status"], "canceled")


class WriteOutcomeTests(ApiTestCase):
    """The failure kinds of a send, told apart."""

    def notify(self):
        self.register(self.shopper)
        return self.place(self.shopper)["notifications"][0]

    def test_unsent_is_not_the_same_failure_as_unknown(self):
        self.fake.fail_next("connect_error")
        unsent = self.notify()
        self.fake.fail_next("timeout_not_landed")
        self.as_user(self.shopper)
        order_id = self.post("/api/orders", {"lines": [{"productId": self.product.pk, "quantity": 1}]}).json()
        unknown = order_id["notifications"][0]
        self.assertEqual((unsent["outcome"], unknown["outcome"]), ("failed", "unknown"))
        # Only the unknown one was looked up at the provider (a GET on the message list), by its reference.
        lookups = [r for r in self.fake.requests if r.method == "GET" and "/Messages.json" in r.url]
        self.assertEqual(len(lookups), 1)

    def test_a_write_that_landed_without_an_answer_is_found_not_resent(self):
        self.fake.fail_next("timeout_landed")
        note = self.notify()
        self.assertEqual(note["outcome"], "pending")
        self.assertEqual(len(self.fake.creates()), 1)
        self.assertEqual(len(self.fake.messages), 1)

    def test_a_5xx_that_landed_is_found_not_resent(self):
        self.fake.fail_next("http_500_landed")
        self.assertEqual(self.notify()["outcome"], "pending")
        self.assertEqual(len(self.fake.messages), 1)

    def test_a_truncated_success_is_unknown_until_found(self):
        self.fake.fail_next("truncated")
        self.assertEqual(self.notify()["outcome"], "pending")  # found by token on the lookup

    def test_the_reference_travels_in_the_body(self):
        note = self.notify()
        record = Notification.objects.get(pk=note["notificationId"])
        expected = hashlib.sha256(record.reference.encode()).hexdigest()[:10]
        self.assertIn(expected, self.fake.creates()[0].body.fields["Body"])
        self.assertTrue(record.reference.startswith("test-install:order:"))


class ResendTests(ApiTestCase):
    def undelivered_notification(self):
        self.register(self.shopper, US_NUMBER)
        note = self.place(self.shopper)["notifications"][0]
        self.fake.deliver(Notification.objects.get(pk=note["notificationId"]).provider_sid)  # US: undelivered
        return note["notificationId"]

    def resend(self, notification_id, key):
        self.as_user(self.staff)
        headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        return self.post("/api/notifications/%s/resend" % notification_id, **headers)

    def test_the_same_operation_twice_sends_one_message(self):
        original = self.undelivered_notification()
        first = self.resend(original, "key-1")
        self.assertEqual(first.status_code, 202)  # accepted by the provider, not yet delivered
        second = self.resend(original, "key-1")
        self.assertEqual(second.json()["notificationId"], first.json()["notificationId"])
        self.assertEqual(len(self.fake.creates()), 2)  # the original + one resend
        expected = "test-install:resend:%s:%s" % (original, hashlib.sha256(b"key-1").hexdigest()[:32])
        self.assertEqual(Notification.objects.get(pk=first.json()["notificationId"]).reference, expected)

    def test_a_fresh_key_is_a_genuine_second_attempt(self):
        original = self.undelivered_notification()
        a = self.resend(original, "key-1").json()["notificationId"]
        b = self.resend(original, "key-2").json()["notificationId"]
        self.assertNotEqual(a, b)
        self.assertEqual(len(self.fake.creates()), 3)

    def test_a_key_is_required(self):
        self.assertEqual(self.resend(self.undelivered_notification(), None).status_code, 400)

    def test_a_delivered_message_is_not_resent(self):
        self.register(self.shopper)
        note = self.place(self.shopper)["notifications"][0]
        self.fake.deliver(Notification.objects.get(pk=note["notificationId"]).provider_sid)
        self.assertEqual(self.resend(note["notificationId"], "k").status_code, 409)

    def test_a_called_off_follow_up_is_never_resent(self):
        self.register(self.shopper)
        order_id = self.place(self.shopper)["orderId"]
        follow_up = self.dispatch(order_id).json()["notifications"][1]["notificationId"]
        self.cancel(order_id)
        self.assertEqual(self.resend(follow_up, "k").status_code, 409)

    def test_shoppers_cannot_resend(self):
        original = self.undelivered_notification()
        self.as_user(self.shopper)
        response = self.post("/api/notifications/%s/resend" % original, HTTP_IDEMPOTENCY_KEY="k")
        self.assertEqual(response.status_code, 403)

    def test_a_refused_resend_answers_409_with_the_new_id(self):
        original = self.undelivered_notification()
        self.fake.fail_next("http_400")
        response = self.resend(original, "key-x")
        self.assertEqual(response.status_code, 409)
        self.assertIn("notificationId", response.json())


class ContentDisposalTests(ApiTestCase):
    def test_disposal_redacts_at_the_provider_and_keeps_the_record(self):
        self.register(self.shopper)
        note = self.place(self.shopper)["notifications"][0]
        record = Notification.objects.get(pk=note["notificationId"])
        self.fake.deliver(record.provider_sid)
        self.as_user(self.staff)
        response = self.client.delete("/api/notifications/%s/content" % record.pk)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.fake.messages[record.provider_sid]["body"], "")
        record.refresh_from_db()
        self.assertEqual(record.body, "")
        self.assertIsNotNone(record.content_disposed_at)
        self.assertEqual((record.outcome, record.provider_status), ("done", "delivered"))
        # Repeating it makes no further provider write.
        updates = len(self.fake.updates())
        self.assertEqual(self.client.delete("/api/notifications/%s/content" % record.pk).status_code, 200)
        self.assertEqual(len(self.fake.updates()), updates)

    def test_the_provider_refusing_to_redact_is_reported_not_hidden(self):
        self.register(self.shopper)
        note = self.place(self.shopper)["notifications"][0]  # still queued: cannot be redacted yet
        self.as_user(self.staff)
        response = self.client.delete("/api/notifications/%s/content" % note["notificationId"])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(ProviderAction.objects.get().outcome, "failed")


class ReconciliationTests(ApiTestCase):
    def test_report_lines_up_both_sides_on_the_providers_clock(self):
        self.register(self.shopper)
        start = datetime.now(timezone.utc) - timedelta(minutes=5)
        ours = Notification.objects.get(pk=self.place(self.shopper)["notifications"][0]["notificationId"])
        self.fake.deliver(ours.provider_sid)
        foreign = self.fake.add_foreign(CA_NUMBER, FROM_NUMBER)  # the provider knows it; we don't
        self.fake.add_foreign(CA_NUMBER, "+15005550009")  # another sender's traffic: excluded by the query
        end = datetime.now(timezone.utc) + timedelta(minutes=5)

        self.as_user(self.staff)
        response = self.client.get("/api/notifications/reconciliation", {"from": start.isoformat(),
                                                                         "to": end.isoformat()})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual([m["notificationId"] for m in report["matched"]], [ours.pk])
        self.assertEqual([m["providerSid"] for m in report["providerOnly"]], [foreign])
        self.assertEqual(report["localOnly"], [])
        [listing] = [r for r in self.fake.requests if r.method == "GET" and "DateSent" in r.url]
        self.assertIn("From=%2B15005550006", listing.url)  # asked for our number's messages only

    def test_follows_every_page(self):
        start = datetime.now(timezone.utc) - timedelta(minutes=5)
        for _ in range(3):
            self.fake.add_foreign(CA_NUMBER, FROM_NUMBER)
        provider.RECONCILE_PAGE_SIZE, saved = 2, provider.RECONCILE_PAGE_SIZE
        try:
            self.as_user(self.staff)
            report = self.client.get("/api/notifications/reconciliation", {
                "from": start.isoformat(), "to": (start + timedelta(minutes=10)).isoformat()}).json()
        finally:
            provider.RECONCILE_PAGE_SIZE = saved
        self.assertEqual(report["providerCount"], 3)
        self.assertFalse(report["truncated"])

    def test_bad_dates_are_rejected(self):
        self.as_user(self.staff)
        self.assertEqual(self.client.get("/api/notifications/reconciliation", {"from": "x", "to": "y"}).status_code,
                         400)

    def test_is_staff_only(self):
        self.as_user(self.shopper)
        self.assertEqual(self.client.get("/api/notifications/reconciliation").status_code, 403)


class PrivacyTests(ApiTestCase):
    def test_numbers_and_token_never_reach_the_logs(self):
        with self.assertLogs("apps.sms_notifications", level=logging.INFO) as logs:
            self.register(self.shopper)
            order_id = self.place(self.shopper)["orderId"]
            self.dispatch(order_id)
            self.cancel(order_id)
        text = "\n".join(logs.output)
        for secret in (CA_NUMBER, CA_NUMBER[1:], "%2B" + CA_NUMBER[1:], TWILIO_SETTINGS["TWILIO_AUTH_TOKEN"]):
            self.assertNotIn(secret, text)
        self.assertIn("twilio POST", text)


class OutcomeMappingTests(TestCase):
    def test_statuses_are_mapped_by_name_and_unknowns_are_not_done(self):
        from twilio_sdk.models.enums import MessageEnumStatus as S

        self.assertEqual(status_from_provider(S.DELIVERED), "done")
        self.assertEqual(status_from_provider(S.SENT), "pending")
        self.assertEqual(status_from_provider(S.SCHEDULED), "pending")
        self.assertEqual(status_from_provider(S.UNDELIVERED), "failed")
        self.assertEqual(status_from_provider(S.CANCELED), "failed")
        self.assertEqual(status_from_provider("something_new"), "unknown")
        self.assertEqual(cancel_outcome(S.CANCELED), "done")
        self.assertEqual(cancel_outcome(S.DELIVERED), "failed")
        self.assertEqual(cancel_outcome("something_new"), "unknown")
        self.assertEqual(redaction_outcome(""), "done")
        self.assertEqual(redaction_outcome("still here"), "failed")

    def test_a_not_done_outcome_never_answers_success(self):
        for outcome in ("pending", "sending", "failed", "needs_review", "unknown", "skipped"):
            self.assertNotIn(answer_status(outcome), (200, 201, 204))
