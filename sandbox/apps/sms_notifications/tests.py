"""
Tests for order SMS notifications.

The Twilio SDK is exercised for real; only its transport is replaced
(``custom_http_client``), so every request the SDK builds is captured and
asserted on. Run with:  python sandbox/manage.py test apps.sms_notifications
"""

import json
import re
from decimal import Decimal as D
from urllib.parse import parse_qs, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from oscar.test.factories import create_product
from twilio_sdk.core import HttpRequest, HttpResponse

from . import twilio_gateway as gw
from .models import ContactNumber, Notification, ProviderWrite
from .services import reference, reference_tag

User = get_user_model()

ACCOUNT = "AC00000000000000000000000000000000"
FROM = "+15550000001"
TEST_SETTINGS = dict(
    TWILIO_ACCOUNT_SID=ACCOUNT,
    TWILIO_AUTH_TOKEN="test-token",
    TWILIO_FROM_NUMBER=FROM,
    TWILIO_MESSAGING_SERVICE_SID="MG00000000000000000000000000000000",
    TWILIO_BASE_URL=None,
    SMS_REFERENCE_PREFIX="test",
)
CANONICAL = "+15557654321"


def json_response(status, body):
    return HttpResponse(
        status_code=status, headers={"content-type": "application/json"}, content=json.dumps(body).encode()
    )


def message_body(sid, status, body="hello", date_sent=None, to=CANONICAL):
    return {
        "sid": sid,
        "status": status,
        "body": body,
        "to": to,
        "from": FROM,
        "date_sent": date_sent,
        "date_created": "Mon, 28 Sep 2026 10:00:00 +0000",
        "error_code": None,
    }


class FakeTwilio:
    """
    A transport that answers like Twilio from in-memory state, and records
    every request the SDK built. ``fail_next`` injects one failure.
    """

    def __init__(self):
        self.requests = []
        self.messages = {}
        self.counter = 0
        self.lookup_valid = True
        self.fail_next = {}  # operation -> exception or HttpResponse
        self.create_status = "queued"
        self.cancel_status = "canceled"

    # transport protocol
    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        op = self.operation(request)
        injected = self.fail_next.pop(op, None)
        if isinstance(injected, Exception):
            raise injected
        if isinstance(injected, HttpResponse):
            return injected
        return getattr(self, "_" + op)(request)

    def close(self):
        pass

    @staticmethod
    def operation(request):
        path = urlsplit(request.url).path
        if "/PhoneNumbers/" in path:
            return "lookup"
        if path.endswith("/Messages.json"):
            return "create" if request.method == "POST" else "list"
        if request.method == "POST":
            return "update"
        return "fetch"

    def by_op(self, op):
        return [r for r in self.requests if self.operation(r) == op]

    @staticmethod
    def form(request):
        return dict(request.body.fields)

    def _lookup(self, request):
        if not self.lookup_valid:
            return json_response(200, {"phone_number": "+1555", "valid": False, "validation_errors": ["TOO_SHORT"]})
        return json_response(200, {"phone_number": CANONICAL, "valid": True, "country_code": "US"})

    def _create(self, request):
        fields = self.form(request)
        self.counter += 1
        sid = "SM%032d" % self.counter
        status = "scheduled" if fields.get("SendAt") else self.create_status
        self.messages[sid] = message_body(sid, status, fields["Body"], to=fields["To"])
        return json_response(201, self.messages[sid])

    def _fetch(self, request):
        sid = urlsplit(request.url).path.rsplit("/", 1)[1].removesuffix(".json")
        if sid not in self.messages:
            return json_response(404, {"code": 20404, "message": "not found"})
        return json_response(200, self.messages[sid])

    def _update(self, request):
        sid = urlsplit(request.url).path.rsplit("/", 1)[1].removesuffix(".json")
        fields = self.form(request)
        message = self.messages[sid]
        if fields.get("Status") == "canceled":
            if message["status"] != "scheduled":
                return json_response(400, {"code": 21660, "message": "cannot cancel"})
            message["status"] = self.cancel_status
        if "Body" in fields:
            message["body"] = fields["Body"]
        return json_response(200, message)

    def _list(self, request):
        query = parse_qs(urlsplit(request.url).query)
        messages = list(self.messages.values())
        if "To" in query:
            messages = [m for m in messages if m["to"] == query["To"][0]]
        return json_response(200, {"messages": messages, "next_page_uri": None})


@override_settings(**TEST_SETTINGS)
class SmsTestCase(TestCase):
    def setUp(self):
        self.fake = FakeTwilio()
        config = gw.TwilioConfig.from_settings()
        gw.set_client_for_tests(gw.build_client(config, transport=gw.LoggingTransport(self.fake)), config)
        self.addCleanup(gw.set_client_for_tests, None, None)
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "pw-shopper-123")
        self.other = User.objects.create_user("other", "other@example.com", "pw-other-123")
        self.operator = User.objects.create_user("operator", "op@example.com", "pw-op-123", is_staff=True)
        self.product = create_product(price=D("12.00"), num_in_stock=100)

    def as_user(self, user):
        self.client.force_login(user)

    def post(self, url, data=None, **extra):
        return self.client.post(url, json.dumps(data or {}), content_type="application/json", **extra)

    def register_number(self, user=None):
        self.as_user(user or self.shopper)
        response = self.post("/api/contact-numbers", {"phoneNumber": "(555) 765-4321"})
        self.assertIn(response.status_code, (200, 201), response.content)
        return response.json()["contactNumberId"]

    def place_order(self, user=None):
        self.as_user(user or self.shopper)
        response = self.post("/api/orders", {"lines": [{"productId": self.product.pk, "quantity": 2}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()


class ContactNumberTests(SmsTestCase):
    def test_stores_the_providers_canonical_form(self):
        self.register_number()
        self.assertEqual(ContactNumber.objects.get().phone_number, CANONICAL)
        self.assertEqual(self.client.get("/api/contact-numbers").json()["contactNumbers"][0]["phoneNumber"], CANONICAL)

    def test_rejects_a_number_the_provider_says_is_not_usable(self):
        self.fake.lookup_valid = False
        self.as_user(self.shopper)
        response = self.post("/api/contact-numbers", {"phoneNumber": "12"})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(ContactNumber.objects.exists())

    def test_provider_refusing_our_credentials_is_not_the_callers_401(self):
        self.fake.fail_next["lookup"] = json_response(401, {"code": 20003, "message": "Authenticate"})
        self.as_user(self.shopper)
        response = self.post("/api/contact-numbers", {"phoneNumber": "5557654321"})
        self.assertEqual(response.status_code, 502)

    def test_another_shopper_can_neither_see_nor_delete_it(self):
        contact_id = self.register_number()
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/contact-numbers").json()["contactNumbers"], [])
        self.assertEqual(self.client.delete("/api/contact-numbers/%s" % contact_id).status_code, 404)
        self.assertTrue(ContactNumber.objects.get(pk=contact_id).is_active)

    def test_deleted_number_is_gone_and_never_messaged(self):
        contact_id = self.register_number()
        self.assertEqual(self.client.delete("/api/contact-numbers/%s" % contact_id).status_code, 200)
        self.assertEqual(self.client.get("/api/contact-numbers").json()["contactNumbers"], [])
        self.place_order()
        self.assertEqual(self.fake.by_op("create"), [])

    def test_deleting_a_number_calls_off_its_scheduled_follow_up(self):
        contact_id = self.register_number()
        order = self.place_order()
        self.as_user(self.operator)
        self.post("/api/orders/%s/dispatch" % order["orderId"])
        self.as_user(self.shopper)
        response = self.client.delete("/api/contact-numbers/%s" % contact_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["followUpCallOffs"][0]["outcome"], "done")
        self.assertEqual(self.form_of_last("update")["Status"], "canceled")

    def test_unauthenticated_is_401(self):
        self.assertEqual(self.client.get("/api/contact-numbers").status_code, 401)

    def form_of_last(self, op):
        return self.fake.form(self.fake.by_op(op)[-1])

    def test_phone_numbers_are_not_logged(self):
        with self.assertLogs("apps.sms_notifications", level="INFO") as logs:
            self.register_number()
            self.place_order()
        output = "\n".join(logs.output)
        self.assertNotIn("7654321", output)
        self.assertNotIn("test-token", output)


class OrderFlowTests(SmsTestCase):
    def test_placing_an_order_texts_the_shopper(self):
        self.register_number()
        order = self.place_order()
        self.assertIn("orderId", order)
        [request] = self.fake.by_op("create")
        fields = self.fake.form(request)
        self.assertEqual(fields["To"], CANONICAL)
        self.assertEqual(fields["From"], FROM)
        ref = reference("order", order["orderId"], "placed")
        self.assertIn("Ref %s" % reference_tag(ref), fields["Body"])
        self.assertTrue(request.url.startswith("https://api.twilio.com/2010-04-01/Accounts/%s/" % ACCOUNT))
        [notification] = order["notifications"]
        self.assertEqual(notification["outcome"], "pending")  # queued is not delivered
        self.assertEqual(ProviderWrite.objects.get().reference, ref)

    def test_order_reuses_oscar_models(self):
        self.register_number()
        order = self.place_order()
        from oscar.core.loading import get_model

        oscar_order = get_model("order", "Order").objects.get(pk=order["orderId"])
        self.assertEqual(oscar_order.user, self.shopper)
        self.assertEqual(oscar_order.lines.get().quantity, 2)

    def test_no_number_on_file_means_no_message(self):
        order = self.place_order()
        self.assertEqual(order["notifications"], [])
        self.assertEqual(self.fake.requests, [])

    def test_a_refused_send_does_not_fail_the_order(self):
        self.register_number()
        self.fake.fail_next["create"] = json_response(400, {"code": 21211, "message": "invalid To"})
        order = self.place_order()
        self.assertEqual(order["notifications"][0]["outcome"], "failed")

    def test_a_lost_answer_is_looked_up_by_reference_not_resent(self):
        self.register_number()
        self.fake.fail_next["create"] = httpx.ReadTimeout("no reply")
        order = self.place_order()
        # Timed out: the order stands, the outcome is unknown, and the only
        # other request is the lookup - no second create.
        self.assertEqual(order["notifications"][0]["outcome"], "unknown")
        self.assertEqual(len(self.fake.by_op("create")), 1)
        self.assertEqual(len(self.fake.by_op("list")), 1)

    def test_a_lost_answer_that_landed_is_found_by_its_tag(self):
        self.register_number()
        self.fake.fail_next["create"] = json_response(503, {"message": "busy"})

        # The provider made the message but the answer was a 5xx: model that
        # by making it before answering.
        original = self.fake._create

        def create_then_fail(request):
            original(request)
            return json_response(503, {"message": "busy"})

        self.fake.fail_next.pop("create")
        self.fake._create = create_then_fail
        order = self.place_order()
        [sid] = self.fake.messages
        notification = Notification.objects.get(pk=order["notifications"][0]["notificationId"])
        self.assertEqual(notification.provider_sid, sid)
        self.assertEqual(notification.writes.get().outcome, "pending")

    def test_never_sent_and_may_have_landed_are_different_outcomes(self):
        self.register_number()
        self.fake.fail_next["create"] = httpx.ConnectError("refused")
        unsent = self.place_order()["notifications"][0]
        self.fake.fail_next["create"] = httpx.ReadTimeout("no reply")
        unknown = self.place_order()["notifications"][0]
        self.assertEqual((unsent["outcome"], unknown["outcome"]), ("failed", "unknown"))
        # Only the timed-out one was looked up.
        self.assertEqual(len(self.fake.by_op("list")), 1)

    def test_an_unlisted_status_is_unknown_and_undelivered_is_failed(self):
        self.register_number()
        self.fake.create_status = "something_new"
        self.assertEqual(self.place_order()["notifications"][0]["outcome"], "unknown")
        self.fake.create_status = "undelivered"
        self.assertEqual(self.place_order()["notifications"][0]["outcome"], "failed")

    def test_delivery_status_is_refreshed_from_the_provider(self):
        self.register_number()
        order = self.place_order()
        sid = next(iter(self.fake.messages))
        self.fake.messages[sid]["status"] = "delivered"
        self.fake.messages[sid]["date_sent"] = "Mon, 28 Sep 2026 10:00:05 +0000"
        body = self.client.get("/api/orders/%s/notifications" % order["orderId"]).json()
        self.assertEqual(body["notifications"][0]["outcome"], "done")
        self.assertEqual(body["notifications"][0]["providerStatus"], "delivered")
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"][0]["notifications"][0]["outcome"], "done")

    def test_another_shoppers_order_is_not_visible(self):
        self.register_number()
        order = self.place_order()
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/orders/%s/notifications" % order["orderId"]).status_code, 404)
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"], [])

    def test_dispatch_and_cancel_are_staff_only(self):
        order = self.place_order()
        self.assertEqual(self.post("/api/orders/%s/dispatch" % order["orderId"]).status_code, 403)
        self.assertEqual(self.post("/api/orders/%s/cancel" % order["orderId"]).status_code, 403)

    def test_dispatch_texts_now_and_queues_the_follow_up_with_the_provider(self):
        self.register_number()
        order = self.place_order()
        self.as_user(self.operator)
        response = self.post("/api/orders/%s/dispatch" % order["orderId"])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "Dispatched")
        creates = self.fake.by_op("create")
        self.assertEqual(len(creates), 3)
        follow_up = self.fake.form(creates[-1])
        self.assertEqual(follow_up["ScheduleType"], "fixed")
        self.assertEqual(follow_up["MessagingServiceSid"], TEST_SETTINGS["TWILIO_MESSAGING_SERVICE_SID"])
        self.assertEqual(follow_up["From"], FROM)
        self.assertIn("SendAt", follow_up)
        kinds = {n["kind"]: n["outcome"] for n in response.json()["notifications"]}
        self.assertEqual(kinds, {"dispatched": "pending", "follow_up": "pending"})

    def test_dispatching_twice_sends_nothing_twice(self):
        self.register_number()
        order = self.place_order()
        self.as_user(self.operator)
        self.post("/api/orders/%s/dispatch" % order["orderId"])
        second = self.post("/api/orders/%s/dispatch" % order["orderId"])
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(self.fake.by_op("create")), 3)

    def test_cancel_calls_off_the_follow_up_and_tells_the_shopper(self):
        self.register_number()
        order = self.place_order()
        self.as_user(self.operator)
        self.post("/api/orders/%s/dispatch" % order["orderId"])
        response = self.post("/api/orders/%s/cancel" % order["orderId"])
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["status"], "Cancelled")
        self.assertEqual(body["followUpCallOffs"][0]["outcome"], "done")
        self.assertEqual(body["followUpCallOffs"][0]["providerStatus"], "canceled")
        [update] = self.fake.by_op("update")
        self.assertEqual(self.fake.form(update), {"Status": "canceled"})
        self.assertEqual(body["notifications"][0]["kind"], "cancelled")
        # Cancelling again makes no further provider writes.
        writes = len(self.fake.by_op("create")) + len(self.fake.by_op("update"))
        self.post("/api/orders/%s/cancel" % order["orderId"])
        self.assertEqual(len(self.fake.by_op("create")) + len(self.fake.by_op("update")), writes)

    def test_a_follow_up_that_already_went_out_is_reported_too_late(self):
        self.register_number()
        order = self.place_order()
        self.as_user(self.operator)
        self.post("/api/orders/%s/dispatch" % order["orderId"])
        follow_up_sid = list(self.fake.messages)[-1]
        self.fake.messages[follow_up_sid]["status"] = "delivered"
        body = self.post("/api/orders/%s/cancel" % order["orderId"]).json()
        self.assertEqual(body["followUpCallOffs"][0]["outcome"], "failed")

    def test_a_call_off_the_provider_does_not_confirm_is_not_done(self):
        self.register_number()
        order = self.place_order()
        self.as_user(self.operator)
        self.post("/api/orders/%s/dispatch" % order["orderId"])
        self.fake.cancel_status = "scheduled"  # the provider still says scheduled
        body = self.post("/api/orders/%s/cancel" % order["orderId"]).json()
        self.assertEqual(body["followUpCallOffs"][0]["outcome"], "unknown")


class OperatorActionTests(SmsTestCase):
    def failed_notification(self):
        self.register_number()
        self.fake.create_status = "undelivered"
        order = self.place_order()
        self.fake.create_status = "queued"
        return order["notifications"][0]["notificationId"]

    def test_resend_under_one_key_sends_once(self):
        notification_id = self.failed_notification()
        self.as_user(self.operator)
        url = "/api/notifications/%s/resend" % notification_id
        first = self.post(url, HTTP_IDEMPOTENCY_KEY="key-1")
        second = self.post(url, HTTP_IDEMPOTENCY_KEY="key-1")
        self.assertEqual(first.status_code, 202, first.content)  # accepted, not yet delivered
        self.assertEqual(first.json()["notificationId"], second.json()["notificationId"])
        self.assertNotEqual(first.json()["notificationId"], notification_id)
        self.assertEqual(len(self.fake.by_op("create")), 2)  # the original + one resend
        third = self.post(url, {"idempotencyKey": "key-2"})
        self.assertEqual(third.status_code, 202)
        self.assertNotEqual(third.json()["notificationId"], first.json()["notificationId"])
        self.assertEqual(len(self.fake.by_op("create")), 3)
        ref = reference("notification", notification_id, "resend", ProviderWrite.objects.last().idempotency_key_hash[:32])
        self.assertIn(reference_tag(ref), self.fake.form(self.fake.by_op("create")[-1])["Body"])

    def test_resend_requires_a_key_and_staff(self):
        notification_id = self.failed_notification()
        url = "/api/notifications/%s/resend" % notification_id
        self.assertEqual(self.post(url, HTTP_IDEMPOTENCY_KEY="k").status_code, 403)
        self.as_user(self.operator)
        self.assertEqual(self.post(url).status_code, 400)

    def test_a_delivered_message_is_not_resent(self):
        self.register_number()
        order = self.place_order()
        self.fake.messages[next(iter(self.fake.messages))]["status"] = "delivered"
        self.as_user(self.operator)
        response = self.post(
            "/api/notifications/%s/resend" % order["notifications"][0]["notificationId"], HTTP_IDEMPOTENCY_KEY="k"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.fake.by_op("create")), 1)

    def test_disposing_content_redacts_it_at_the_provider(self):
        self.register_number()
        order = self.place_order()
        notification_id = order["notifications"][0]["notificationId"]
        self.as_user(self.operator)
        response = self.client.delete("/api/notifications/%s/content" % notification_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.fake.form(self.fake.by_op("update")[-1]), {"Body": ""})
        notification = Notification.objects.get(pk=notification_id)
        self.assertEqual(notification.body, "")
        self.assertTrue(notification.provider_sid)  # that it was sent survives
        self.assertTrue(response.json()["contentDisposed"])

    def test_content_the_provider_still_holds_is_not_reported_disposed(self):
        self.register_number()
        order = self.place_order()
        self.fake._update = lambda request: json_response(200, self.fake.messages[next(iter(self.fake.messages))])
        self.as_user(self.operator)
        response = self.client.delete("/api/notifications/%s/content" % order["notifications"][0]["notificationId"])
        self.assertEqual(response.status_code, 409)
        self.assertTrue(Notification.objects.get().body)

    def test_reconciliation_asks_for_our_number_and_lines_both_sides_up(self):
        self.register_number()
        order = self.place_order()
        ours = next(iter(self.fake.messages))
        self.fake.messages[ours].update(status="delivered", date_sent="Mon, 28 Sep 2026 10:00:05 +0000")
        self.fake.messages["SMforeign"] = message_body(
            "SMforeign", "delivered", date_sent="Mon, 28 Sep 2026 11:00:00 +0000", to="+15559999999"
        )
        self.fake.messages["SMoutside"] = message_body(
            "SMoutside", "delivered", date_sent="Sun, 27 Sep 2026 23:00:00 +0000"
        )
        self.as_user(self.operator)
        response = self.client.get(
            "/api/notifications/reconciliation",
            {"from": "2026-09-28T00:00:00Z", "to": "2026-09-29T00:00:00Z"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        [request] = self.fake.by_op("list")
        query = parse_qs(urlsplit(request.url).query)
        self.assertEqual(query["From"], [FROM])
        self.assertIn("DateSent>", query)
        self.assertIn("DateSent<", query)
        self.assertEqual([m["providerMessageSid"] for m in body["matched"]], [ours])
        self.assertEqual(body["matched"][0]["notificationId"], order["notifications"][0]["notificationId"])
        self.assertEqual([m["providerMessageSid"] for m in body["providerOnly"]], ["SMforeign"])
        self.assertEqual(body["summary"]["localOnly"], 0)

    def test_reconciliation_is_staff_only(self):
        self.as_user(self.shopper)
        response = self.client.get(
            "/api/notifications/reconciliation", {"from": "2026-09-28T00:00:00Z", "to": "2026-09-29T00:00:00Z"}
        )
        self.assertEqual(response.status_code, 403)


@override_settings(**dict(TEST_SETTINGS, TWILIO_BASE_URL="https://messaging-proxy.example.test"))
class BaseUrlTests(SmsTestCase):
    def test_base_url_governs_messaging_calls_only(self):
        self.register_number()
        self.place_order()
        [lookup] = self.fake.by_op("lookup")
        [create] = self.fake.by_op("create")
        self.assertTrue(lookup.url.startswith("https://lookups.twilio.com/"))
        self.assertTrue(create.url.startswith("https://messaging-proxy.example.test/2010-04-01/"))


class StatusMappingTests(TestCase):
    def test_send_outcomes(self):
        S = gw.MessageEnumStatus
        self.assertEqual(gw.status_from_provider(S.DELIVERED), "done")
        for pending in (S.QUEUED, S.SENDING, S.SENT, S.ACCEPTED, S.SCHEDULED):
            self.assertEqual(gw.status_from_provider(pending), "pending")
        for failed in (S.FAILED, S.UNDELIVERED, S.CANCELED):
            self.assertEqual(gw.status_from_provider(failed), "failed")
        self.assertEqual(gw.status_from_provider("brand_new"), "unknown")
        self.assertEqual(gw.status_from_provider(None), "unknown")

    def test_cancel_outcomes(self):
        S = gw.MessageEnumStatus
        self.assertEqual(gw.cancel_outcome(S.CANCELED), "done")
        self.assertEqual(gw.cancel_outcome(S.DELIVERED), "failed")
        self.assertEqual(gw.cancel_outcome(S.SCHEDULED), "unknown")
        self.assertEqual(gw.cancel_outcome("brand_new"), "unknown")

    def test_no_not_done_outcome_answers_success(self):
        from .safe_write import answer_status

        for outcome in ("pending", "sending", "failed", "needs_review", "unknown", "other"):
            self.assertNotIn(answer_status(outcome), (200, 201, 204))

    def test_log_masking(self):
        self.assertNotRegex(gw.mask_numbers("/v2/PhoneNumbers/%2B15557654321"), re.compile(r"\d{6}"))
