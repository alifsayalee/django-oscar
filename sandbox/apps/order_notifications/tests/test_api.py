import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from oscar.core.loading import get_model
from oscar.test.factories import create_product
from twilio_sdk.models.enums import MessageEnumStatus

from apps.order_notifications import services
from apps.order_notifications import status as st
from apps.order_notifications import twilio_gateway as gateway
from apps.order_notifications.models import ContactNumber, Notification

from .fake_twilio import FROM_NUMBER, SERVICE_SID, TEST_SETTINGS, FakeTwilio, _json, fake_client

Order = get_model("order", "Order")
User = get_user_model()

CA_NUMBER = "+18255550199"
US_NUMBER = "+12025550143"


@override_settings(**TEST_SETTINGS)
class ApiTestCase(TestCase):
    def setUp(self):
        self.fake = FakeTwilio()
        self.fake.add_number("825 555 0199", CA_NUMBER)
        self.fake.add_number(CA_NUMBER, CA_NUMBER)
        self.fake.add_number(US_NUMBER, US_NUMBER, "US")
        swap = gateway.use_client(fake_client(self.fake))
        swap.__enter__()
        self.addCleanup(swap.__exit__, None, None, None)
        sleep = mock.patch.object(services.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "pw-123456789")
        self.other = User.objects.create_user("other", "other@example.com", "pw-123456789")
        self.operator = User.objects.create_user(
            "operator", "op@example.com", "pw-123456789", is_staff=True
        )
        self.product = create_product(price=10, num_in_stock=50)

    # -- helpers --------------------------------------------------------------

    def as_user(self, user):
        self.client.force_login(user)

    def post(self, url, data=None, **extra):
        return self.client.post(
            url, json.dumps(data or {}), content_type="application/json", **extra
        )

    def register(self, number=CA_NUMBER, user=None):
        self.as_user(user or self.shopper)
        response = self.post("/api/contact-numbers", {"phoneNumber": number})
        self.assertIn(response.status_code, (200, 201), response.content)
        return response.json()["contactNumberId"]

    def place(self, user=None):
        self.as_user(user or self.shopper)
        response = self.post(
            "/api/orders", {"lines": [{"productId": self.product.pk, "quantity": 2}]}
        )
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["orderId"]

    def notifications(self, order_id, user=None):
        self.as_user(user or self.shopper)
        response = self.client.get("/api/orders/%s/notifications" % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["notifications"]

    def fields(self, request):
        return {k: (v if isinstance(v, str) else v[0]) for k, v in request.body.fields.items()}


class ContactNumberTests(ApiTestCase):
    def test_stores_the_providers_canonical_form(self):
        self.as_user(self.shopper)
        response = self.post("/api/contact-numbers", {"phoneNumber": "825 555 0199"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["phoneNumber"], CA_NUMBER)
        self.assertEqual(ContactNumber.objects.get().phone_number, CA_NUMBER)

    def test_number_the_provider_does_not_recognise_is_rejected(self):
        self.as_user(self.shopper)
        response = self.post("/api/contact-numbers", {"phoneNumber": "12345"})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(ContactNumber.objects.exists())

    def test_lookup_outage_is_not_reported_as_a_bad_number(self):
        self.fake.fail_next("lookup", lambda: httpx.ConnectError("refused"))
        self.as_user(self.shopper)
        response = self.post("/api/contact-numbers", {"phoneNumber": CA_NUMBER})
        self.assertEqual(response.status_code, 502)

    def test_credentials_refused_is_ours_not_the_callers(self):
        self.fake.fail_next("lookup", _json(401, {"code": 20003, "message": "Authenticate"}))
        self.as_user(self.shopper)
        response = self.post("/api/contact-numbers", {"phoneNumber": CA_NUMBER})
        self.assertEqual(response.status_code, 502)

    def test_registering_twice_returns_the_same_number(self):
        first = self.register()
        self.assertEqual(self.register(), first)
        self.assertEqual(ContactNumber.objects.count(), 1)

    def test_numbers_are_private_to_their_owner(self):
        number_id = self.register()
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/contact-numbers").json()["contactNumbers"], [])
        self.assertEqual(self.client.delete("/api/contact-numbers/%s" % number_id).status_code, 404)
        self.assertTrue(ContactNumber.objects.get(pk=number_id).is_active)

    def test_removed_number_is_unlisted_and_never_messaged(self):
        number_id = self.register()
        self.assertEqual(self.client.delete("/api/contact-numbers/%s" % number_id).status_code, 204)
        self.assertEqual(self.client.get("/api/contact-numbers").json()["contactNumbers"], [])
        order_id = self.place()
        self.assertEqual(self.notifications(order_id), [])
        self.assertEqual(self.fake.sent(), [])

    def test_removing_a_number_calls_off_its_queued_follow_up(self):
        number_id = self.register()
        order_id = self.place()
        self.as_user(self.operator)
        self.post("/api/orders/%s/dispatch" % order_id)
        self.as_user(self.shopper)
        self.client.delete("/api/contact-numbers/%s" % number_id)
        follow_up = Notification.objects.get(kind=Notification.FOLLOW_UP)
        self.assertEqual(follow_up.outcome, st.CANCELED)
        self.assertEqual(self.fake.messages[follow_up.provider_sid]["status"], "canceled")

    def test_phone_numbers_never_reach_the_logs(self):
        with self.assertLogs("order_notifications", level="DEBUG") as logs:
            self.register()
            order_id = self.place()
            self.as_user(self.operator)
            self.post("/api/orders/%s/dispatch" % order_id)
        joined = "\n".join(logs.output)
        self.assertNotIn(CA_NUMBER, joined)
        self.assertNotIn(CA_NUMBER.replace("+", "%2B"), joined)
        self.assertNotIn("8255550199", joined)

    def test_requires_login(self):
        self.assertEqual(self.client.get("/api/contact-numbers").status_code, 401)


class OrderFlowTests(ApiTestCase):
    def test_placing_an_order_reuses_oscar_orders_and_messages_the_shopper(self):
        self.register()
        order_id = self.place()
        order = Order.objects.get(pk=order_id)
        self.assertEqual(order.user, self.shopper)
        self.assertEqual(order.lines.get().quantity, 2)
        (sent,) = self.fake.sent()
        fields = self.fields(sent)
        self.assertEqual(fields["To"], CA_NUMBER)
        self.assertEqual(fields["From"], FROM_NUMBER)
        self.assertIn(order.number, fields["Body"])
        (n,) = self.notifications(order_id)
        self.assertEqual((n["kind"], n["outcome"]), ("placed", st.PENDING))
        self.assertTrue(n["providerSid"].startswith("SM"))

    def test_shopper_without_a_number_is_not_messaged(self):
        order_id = self.place()
        self.assertEqual(self.notifications(order_id), [])
        self.assertEqual(self.fake.requests, [])

    def test_unsent_message_never_fails_the_order(self):
        self.register()
        self.fake.fail_next("send", lambda: httpx.ConnectError("refused"))
        order_id = self.place()
        (n,) = self.notifications(order_id)
        self.assertEqual(n["outcome"], st.FAILED)

    def test_send_timeout_is_looked_up_by_reference_not_assumed_failed(self):
        self.register()

        def lands_then_times_out():
            # The provider processed the request, the reply never arrived.
            self.fake._send(self.fake.requests[-1])
            return httpx.ReadTimeout("no reply")

        self.fake.fail_next("send", lands_then_times_out)
        order_id = self.place()
        (n,) = self.notifications(order_id)
        self.assertEqual(n["outcome"], st.PENDING)
        self.assertIsNotNone(n["providerSid"])
        self.assertEqual(len(self.fake.messages), 1)

    def test_send_timeout_with_nothing_found_stays_unknown(self):
        self.register()
        self.fake.fail_next("send", lambda: httpx.ReadTimeout("no reply"))
        order_id = self.place()
        notification = Notification.objects.get(order_id=order_id)
        self.assertEqual(notification.outcome, st.UNKNOWN)
        self.assertEqual(len(self.fake.sent("list")), 1)

    def test_rejected_destination_is_recorded_as_failed(self):
        self.register()
        self.fake.fail_next(
            "send", _json(400, {"code": 21211, "message": "Invalid 'To' Phone Number: %s" % CA_NUMBER})
        )
        order_id = self.place()
        notification = Notification.objects.get(order_id=order_id)
        self.assertEqual(notification.outcome, st.FAILED)
        self.assertNotIn(CA_NUMBER, notification.error_message)

    def test_orders_are_private(self):
        self.register()
        order_id = self.place()
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/orders/%s/notifications" % order_id).status_code, 404)
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"], [])

    def test_my_orders_reports_each_orders_notifications(self):
        self.register()
        order_id = self.place()
        sid = Notification.objects.get().provider_sid
        self.fake.messages[sid]["status"] = "delivered"
        self.as_user(self.shopper)
        (order,) = self.client.get("/api/my-orders").json()["orders"]
        self.assertEqual(order["orderId"], order_id)
        self.assertEqual(order["notificationSummary"], {"placed": st.DELIVERED})

    def test_unknown_product_is_rejected(self):
        self.as_user(self.shopper)
        response = self.post("/api/orders", {"lines": [{"productId": 999999, "quantity": 1}]})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Order.objects.exists())


class DispatchCancelTests(ApiTestCase):
    def dispatch(self, order_id):
        self.as_user(self.operator)
        response = self.post("/api/orders/%s/dispatch" % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def cancel(self, order_id):
        self.as_user(self.operator)
        response = self.post("/api/orders/%s/cancel" % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_dispatch_is_operator_only(self):
        self.register()
        order_id = self.place()
        self.as_user(self.shopper)
        self.assertEqual(self.post("/api/orders/%s/dispatch" % order_id).status_code, 403)
        self.assertEqual(self.post("/api/orders/%s/cancel" % order_id).status_code, 403)

    def test_dispatch_queues_the_follow_up_with_the_provider(self):
        self.register()
        order_id = self.place()
        body = self.dispatch(order_id)
        self.assertEqual(body["status"], "Dispatched")
        sends = self.fake.sent()
        self.assertEqual(len(sends), 3)
        follow_up = self.fields(sends[2])
        self.assertEqual(follow_up["ScheduleType"], "fixed")
        self.assertEqual(follow_up["MessagingServiceSid"], SERVICE_SID)
        self.assertEqual(follow_up["From"], FROM_NUMBER)
        send_at = datetime.fromisoformat(follow_up["SendAt"].replace("Z", "+00:00"))
        self.assertAlmostEqual(
            (send_at - datetime.now(timezone.utc)).total_seconds(), 72 * 3600, delta=120
        )
        kinds = {n["kind"]: n["outcome"] for n in body["notifications"]}
        self.assertEqual(kinds["follow_up"], st.SCHEDULED)

    def test_repeated_dispatch_sends_nothing_more(self):
        self.register()
        order_id = self.place()
        self.dispatch(order_id)
        body = self.dispatch(order_id)
        self.assertFalse(body["changed"])
        self.assertEqual(len(self.fake.sent()), 3)

    def test_cancel_calls_off_the_queued_follow_up(self):
        self.register()
        order_id = self.place()
        self.dispatch(order_id)
        body = self.cancel(order_id)
        self.assertEqual(body["status"], "Cancelled")
        follow_up = Notification.objects.get(kind=Notification.FOLLOW_UP)
        self.assertEqual(follow_up.outcome, st.CANCELED)
        self.assertEqual(self.fake.messages[follow_up.provider_sid]["status"], "canceled")
        (update,) = self.fake.sent("update")
        self.assertEqual(self.fields(update), {"Status": "canceled"})
        self.assertTrue(Notification.objects.filter(kind=Notification.CANCELLED).exists())

    def test_cancel_retries_a_transient_failure(self):
        self.register()
        order_id = self.place()
        self.dispatch(order_id)
        self.fake.fail_next("update", _json(503, {"code": 20503, "message": "unavailable"}))
        self.cancel(order_id)
        follow_up = Notification.objects.get(kind=Notification.FOLLOW_UP)
        self.assertEqual(follow_up.outcome, st.CANCELED)
        self.assertEqual(len(self.fake.sent("update")), 2)

    def test_uncancelled_follow_up_is_retried_by_later_reads(self):
        self.register()
        order_id = self.place()
        self.dispatch(order_id)
        for _ in range(services.CANCEL_ATTEMPTS):
            self.fake.fail_next("update", lambda: httpx.ConnectError("refused"))
        self.cancel(order_id)
        follow_up = Notification.objects.get(kind=Notification.FOLLOW_UP)
        self.assertEqual(follow_up.outcome, st.SCHEDULED)
        self.notifications(order_id)  # any later read retries the cancellation
        follow_up.refresh_from_db()
        self.assertEqual(follow_up.outcome, st.CANCELED)

    def test_cancel_during_follow_up_send_still_calls_it_off(self):
        self.register()
        order_id = self.place()
        order = Order.objects.get(pk=order_id)

        def cancel_while_in_flight():
            # The operator cancels while the follow-up's create is on the wire.
            in_flight = self.fake.requests[-1]
            services.cancel(Order.objects.get(pk=order_id), self.operator)
            return self.fake._send(in_flight)

        # dispatched message goes through, the follow-up races the cancel.
        original_send = self.fake._send
        calls = {"n": 0}

        def send(request):
            calls["n"] += 1
            if calls["n"] == 2:
                self.fake._send = original_send
                return cancel_while_in_flight()
            return original_send(request)

        self.fake._send = send
        services.dispatch(order, self.operator)
        follow_up = Notification.objects.get(kind=Notification.FOLLOW_UP)
        self.assertEqual(follow_up.outcome, st.CANCELED)
        self.assertEqual(self.fake.messages[follow_up.provider_sid]["status"], "canceled")

    def test_cancel_a_placed_order_without_dispatch(self):
        self.register()
        order_id = self.place()
        body = self.cancel(order_id)
        self.assertEqual(body["status"], "Cancelled")
        self.assertEqual(self.fake.sent("update"), [])

    def test_dispatch_after_cancel_is_refused(self):
        self.register()
        order_id = self.place()
        self.cancel(order_id)
        self.as_user(self.operator)
        self.assertEqual(self.post("/api/orders/%s/dispatch" % order_id).status_code, 409)


class OperatorActionTests(ApiTestCase):
    def failed_notification(self):
        self.register()
        self.fake.send_status = "undelivered"
        order_id = self.place()
        self.fake.send_status = "queued"
        return Notification.objects.get(order_id=order_id)

    def resend(self, notification_id, key):
        self.as_user(self.operator)
        return self.post(
            "/api/notifications/%s/resend" % notification_id, {"idempotencyKey": key}
        )

    def test_resend_is_idempotent_per_key(self):
        original = self.failed_notification()
        first = self.resend(original.pk, "key-1")
        self.assertEqual(first.status_code, 201, first.content)
        again = self.resend(original.pk, "key-1")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["notificationId"], first.json()["notificationId"])
        self.assertEqual(len(self.fake.sent()), 2)  # the original + one resend

        # A fresh key is a genuine second attempt.
        Notification.objects.filter(pk=first.json()["notificationId"]).update(outcome=st.FAILED)
        second = self.resend(original.pk, "key-2")
        self.assertEqual(second.status_code, 201)
        self.assertEqual(len(self.fake.sent()), 3)

    def test_resend_carries_a_new_reference(self):
        original = self.failed_notification()
        new_id = self.resend(original.pk, "key-1").json()["notificationId"]
        resent = Notification.objects.get(pk=new_id)
        self.assertEqual(resent.resend_of, original)
        self.assertNotEqual(resent.ref, original.ref)
        self.assertIn(resent.ref, self.fields(self.fake.sent()[-1])["Body"])

    def test_resend_of_a_delivered_message_is_refused(self):
        self.register()
        order_id = self.place()
        notification = Notification.objects.get(order_id=order_id)
        self.fake.messages[notification.provider_sid]["status"] = "delivered"
        self.assertEqual(self.resend(notification.pk, "k").status_code, 409)
        self.assertEqual(len(self.fake.sent()), 1)

    def test_key_reused_for_another_notification_is_rejected(self):
        original = self.failed_notification()
        self.resend(original.pk, "key-1")
        other_order = self.place()
        other = Notification.objects.get(order_id=other_order)
        self.assertEqual(self.resend(other.pk, "key-1").status_code, 422)

    def test_resend_requires_a_key_and_an_operator(self):
        original = self.failed_notification()
        self.assertEqual(self.resend(original.pk, "").status_code, 400)
        self.as_user(self.shopper)
        response = self.post("/api/notifications/%s/resend" % original.pk, {"idempotencyKey": "k"})
        self.assertEqual(response.status_code, 403)

    def test_dispose_content_erases_it_at_the_provider(self):
        self.register()
        order_id = self.place()
        notification = Notification.objects.get(order_id=order_id)
        self.fake.messages[notification.provider_sid]["status"] = "delivered"
        self.as_user(self.operator)
        response = self.client.delete("/api/notifications/%s/content" % notification.pk)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertIsNone(body["body"])
        self.assertEqual(body["outcome"], st.DELIVERED)
        self.assertEqual(self.fields(self.fake.sent("update")[0]), {"Body": ""})
        self.assertEqual(self.fake.messages[notification.provider_sid]["body"], "")
        notification.refresh_from_db()
        self.assertEqual(notification.body, "")

    def test_reconciliation_lines_up_both_sides(self):
        self.register()
        self.place()
        self.fake.add_foreign_message("+15550001111")
        # Someone else's traffic from another sender must not be asked for.
        self.fake.add_foreign_message("+15550002222", from_="+15550009999")
        now = datetime.now(timezone.utc)
        self.as_user(self.operator)
        response = self.client.get(
            "/api/notifications/reconciliation",
            {"from": (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
             "to": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")},
        )
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual(report["counts"]["matched"], 1)
        self.assertEqual(report["counts"]["providerOnly"], 1)
        self.assertEqual(report["counts"]["appOnly"], 0)
        (listed,) = self.fake.sent("list")
        self.assertIn("From=%2B15005550006", listed.url)

    def test_reconciliation_walks_every_page(self):
        self.register()
        self.place()
        for i in range(4):
            self.fake.add_foreign_message("+1555000111%s" % i)
        self.fake.page_size_override = 2
        now = datetime.now(timezone.utc)
        report = services.reconcile(now - timedelta(hours=1), now + timedelta(hours=1))
        self.assertEqual(report["counts"]["provider"], 5)
        self.assertEqual(len(self.fake.sent("list")), 3)
        self.assertFalse(report["providerTruncated"])

    def test_reconciliation_reports_what_the_provider_does_not_list(self):
        self.register()
        order_id = self.place()
        sid = Notification.objects.get(order_id=order_id).provider_sid
        del self.fake.messages[sid]
        now = datetime.now(timezone.utc)
        report = services.reconcile(now - timedelta(hours=1), now + timedelta(hours=1))
        self.assertEqual(report["counts"]["appOnly"], 1)
        self.assertEqual(report["appOnly"][0]["providerLookup"], "not_found")

    def test_reconciliation_validates_its_range(self):
        self.as_user(self.operator)
        url = "/api/notifications/reconciliation"
        self.assertEqual(self.client.get(url).status_code, 400)
        self.assertEqual(
            self.client.get(url, {"from": "2026-01-02T00:00:00Z", "to": "2026-01-01T00:00:00Z"}).status_code,
            400,
        )
        self.assertEqual(
            self.client.get(url, {"from": "2026-01-01T00:00:00", "to": "2026-01-02T00:00:00"}).status_code,
            400,
        )


class GatewayTests(TestCase):
    def test_every_status_member_is_mapped_deliberately(self):
        expected = {
            "delivered": st.DELIVERED, "read": st.DELIVERED, "sent": st.SENT,
            "accepted": st.PENDING, "queued": st.PENDING, "sending": st.PENDING,
            "scheduled": st.SCHEDULED, "canceled": st.CANCELED,
            "failed": st.FAILED, "undelivered": st.FAILED,
            "partially_delivered": st.UNKNOWN, "receiving": st.UNKNOWN, "received": st.UNKNOWN,
        }
        self.assertEqual({m.value for m in MessageEnumStatus}, set(expected))
        for member in MessageEnumStatus:
            self.assertEqual(st.outcome_from_provider(member), expected[member.value])
        self.assertEqual(st.outcome_from_provider("brand_new_status"), st.UNKNOWN)

    def test_never_sent_and_no_reply_are_different_outcomes(self):
        def failing(error):
            def fn():
                raise error
            return fn

        with self.assertRaises(gateway.ProviderUnavailable) as unsent:
            gateway._call("send", failing(httpx.ConnectError("refused")))
        with self.assertRaises(gateway.ProviderUnavailable) as unknown:
            gateway._call("send", failing(httpx.ReadTimeout("no reply")))
        self.assertEqual((unsent.exception.status_code, unsent.exception.outcome_unknown), (502, False))
        self.assertEqual((unknown.exception.status_code, unknown.exception.outcome_unknown), (504, True))

    @override_settings(**dict(TEST_SETTINGS, TWILIO_BASE_URL="https://proxy.example.test"))
    def test_base_url_override_governs_messaging_only(self):
        fake = FakeTwilio()
        fake.add_number(CA_NUMBER, CA_NUMBER)
        with gateway.use_client(gateway.build_client(transport=fake)):
            gateway.send_message(CA_NUMBER, "hi")
            gateway.lookup_number(CA_NUMBER)
        self.assertTrue(fake.requests[0].url.startswith("https://proxy.example.test/2010-04-01/"))
        self.assertTrue(fake.requests[1].url.startswith("https://lookups.twilio.com/"))

    @override_settings(**dict(TEST_SETTINGS, TWILIO_AUTH_TOKEN=""))
    def test_refuses_to_build_without_credentials(self):
        with self.assertRaises(gateway.ProviderConfigError):
            gateway.build_client(transport=FakeTwilio())
