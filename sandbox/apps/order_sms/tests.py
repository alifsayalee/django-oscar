"""
Tests for order SMS notifications.

The Twilio SDK client is real; only its transport is faked (the SDK's own test
seam), so request building, decoding and error mapping all run for real.

Run with:  cd sandbox && ../venv/Scripts/python manage.py test apps.order_sms
"""
import json
import logging
from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from oscar.test.factories import create_product
from twilio_sdk.core import FormBody, HttpRequest, HttpResponse

from . import services
from . import twilio_gateway as tg
from .models import ContactNumber, Installation, Notification

User = get_user_model()

ACCOUNT = "AC00000000000000000000000000000000"
FROM = "+15550000001"
SHOPPER_NUMBER = "+16045550123"
OTHER_NUMBER = "+16045550199"

TEST_SETTINGS = dict(
    TWILIO_ACCOUNT_SID=ACCOUNT, TWILIO_AUTH_TOKEN="test-token", TWILIO_FROM_NUMBER=FROM,
    TWILIO_MESSAGING_SERVICE_SID="MG00000000000000000000000000000000", TWILIO_BASE_URL="",
    ORDER_SMS_INSTALL_ID="test-install", ORDER_SMS_FOLLOWUP_DELAY_HOURS=72,
)


def json_response(status, body):
    return HttpResponse(status_code=status, headers={"content-type": "application/json"},
                        content=json.dumps(body).encode())


def form(request):
    assert isinstance(request.body, FormBody)
    return request.body.fields


def rfc1123(dt):
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


class FakeTwilio:
    """A transport that answers like the provider from a small in-memory message store."""

    def __init__(self):
        self.requests = []
        self.messages = {}
        self.fail_next = []  # exceptions or HttpResponses to use for the next Messages.json POSTs
        self.create_status = "queued"
        self.lookup_status = 200
        self._seq = 0

    # helpers --------------------------------------------------------------
    def creates(self):
        return [r for r in self.requests if r.method == "POST" and r.url.split("?")[0].endswith("/Messages.json")]

    def updates(self):
        return [r for r in self.requests if r.method == "POST" and "/Messages/SM" in r.url]

    def add_message(self, to, body, status="delivered", from_=FROM, when=None, direction="outbound-api"):
        self._seq += 1
        sid = "SM%032d" % self._seq
        when = when or timezone.now()
        self.messages[sid] = {"sid": sid, "to": to, "from": from_, "body": body, "status": status,
                              "direction": direction,
                              "date_created": rfc1123(when), "date_sent": rfc1123(when),
                              "error_code": None, "error_message": None, "account_sid": ACCOUNT}
        return self.messages[sid]

    # transport protocol ---------------------------------------------------
    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        parts = urlsplit(request.url)
        path = parts.path
        if parts.netloc == "lookups.twilio.com":
            if self.lookup_status != 200:
                return json_response(self.lookup_status, {"code": 20404, "message": "not found"})
            number = unquote(path.rsplit("/", 1)[1])
            canonical = number if number.startswith("+") else "+1" + number
            return json_response(200, {"phone_number": canonical, "country_code": "CA",
                                       "national_format": number, "caller_name": None, "carrier": None,
                                       "add_ons": None, "url": "https://lookups.twilio.com" + path})
        if path.endswith("/Messages.json") and request.method == "POST":
            fields = form(request)
            if self.fail_next:
                outcome = self.fail_next.pop(0)
                if isinstance(outcome, BaseException):
                    if isinstance(outcome, httpx.ReadTimeout):  # the provider acted, the reply was lost
                        self.add_message(fields["To"], fields.get("Body"), status=self.create_status)
                        # ...and the destination routed an inbound copy back into the account, newer
                        self.add_message(fields["To"], fields.get("Body"), status="received", direction="inbound")
                    raise outcome
                return outcome
            status = "scheduled" if fields.get("ScheduleType") == "fixed" else self.create_status
            message = self.add_message(fields["To"], fields.get("Body"), status=status)
            if status == "scheduled":
                message["date_sent"] = None
            return json_response(201, message)
        if path.endswith("/Messages.json") and request.method == "GET":
            query = parse_qs(parts.query)
            items = [m for m in self.messages.values()
                     if m["from"] == query.get("From", [None])[0]
                     and ("To" not in query or m["to"] == query["To"][0])]
            items.reverse()
            return json_response(200, {"messages": items, "next_page_uri": None, "page": 0,
                                       "page_size": 50, "start": 0, "end": len(items)})
        if "/Messages/SM" in path:
            sid = path.rsplit("/", 1)[1].replace(".json", "")
            message = self.messages.get(sid)
            if message is None:
                return json_response(404, {"code": 20404, "message": "not found"})
            if request.method == "POST":
                fields = form(request)
                if fields.get("Status") == "canceled":
                    if message["status"] != "scheduled":
                        return json_response(400, {"code": 30409, "message": "cannot cancel"})
                    message["status"] = "canceled"
                if "Body" in fields:
                    message["body"] = fields["Body"]
            return json_response(200, message)
        return json_response(404, {"message": "unrouted"})

    def close(self):
        pass


@override_settings(**TEST_SETTINGS)
class OrderSmsTestCase(TestCase):
    def setUp(self):
        self.fake = FakeTwilio()
        config = services.gateway_config()
        services.set_gateway(tg.Gateway(tg.build_client(config, transport=self.fake), config))
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "pass-word-123")
        self.other = User.objects.create_user("other", "other@example.com", "pass-word-123")
        self.staff = User.objects.create_user("operator", "op@example.com", "pass-word-123", is_staff=True)
        self.product = create_product(price=Decimal("10.00"), num_in_stock=50)

    def tearDown(self):
        services.set_gateway(None)

    # helpers --------------------------------------------------------------
    def as_user(self, user):
        self.client.force_login(user)

    def post(self, url, data=None, **headers):
        return self.client.post(url, json.dumps(data or {}), content_type="application/json", headers=headers)

    def register(self, user=None, number=SHOPPER_NUMBER):
        self.as_user(user or self.shopper)
        response = self.post("/api/contact-numbers", {"phoneNumber": number})
        self.assertIn(response.status_code, (200, 201), response.content)
        return response.json()["contactNumberId"]

    def place(self, user=None):
        self.as_user(user or self.shopper)
        response = self.post("/api/orders", {"lines": [{"productId": self.product.pk, "quantity": 2}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()


class ContactNumberTests(OrderSmsTestCase):
    def test_stores_the_providers_canonical_form(self):
        self.as_user(self.shopper)
        response = self.post("/api/contact-numbers", {"phoneNumber": "6045550123", "countryCode": "CA"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["phoneNumber"], "+16045550123")
        self.assertIn("CountryCode=CA", self.fake.requests[-1].url)

    def test_number_the_provider_does_not_recognise_is_rejected_up_front(self):
        self.fake.lookup_status = 404
        self.as_user(self.shopper)
        response = self.post("/api/contact-numbers", {"phoneNumber": "12345"})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(ContactNumber.objects.exists())

    def test_numbers_are_private_to_their_owner(self):
        contact_id = self.register()
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/contact-numbers").json()["contactNumbers"], [])
        self.assertEqual(self.client.delete("/api/contact-numbers/%s" % contact_id).status_code, 404)
        self.assertTrue(ContactNumber.objects.get(pk=contact_id).removed_at is None)

    def test_removed_number_is_not_listed_and_not_messaged(self):
        contact_id = self.register()
        self.assertEqual(self.client.delete("/api/contact-numbers/%s" % contact_id).status_code, 204)
        self.assertEqual(self.client.get("/api/contact-numbers").json()["contactNumbers"], [])
        order = self.place()
        self.assertIsNone(order["notification"])
        self.assertEqual(self.fake.creates(), [])

    def test_removing_a_number_calls_off_its_queued_follow_up(self):
        contact_id = self.register()
        order = self.place()
        self.as_user(self.staff)
        followup = self.post("/api/orders/%s/dispatch" % order["orderId"]).json()["followUp"]
        self.as_user(self.shopper)
        self.assertEqual(self.client.delete("/api/contact-numbers/%s" % contact_id).status_code, 204)
        self.assertEqual(self.fake.messages[followup["providerSid"]]["status"], "canceled")

    def test_unauthenticated_is_401(self):
        self.assertEqual(self.client.get("/api/contact-numbers").status_code, 401)


class OrderNotificationTests(OrderSmsTestCase):
    def test_placing_an_order_texts_the_shopper(self):
        self.register()
        order = self.place()
        self.assertIn("orderId", order)
        [create] = self.fake.creates()
        fields = form(create)
        self.assertEqual((fields["To"], fields["From"]), (SHOPPER_NUMBER, FROM))
        n = Notification.objects.get(pk=order["notification"]["notificationId"])
        self.assertIn("Ref %s" % n.ref_token, fields["Body"])
        self.assertEqual(n.reference, "test-install:order_placed:%s" % order["orderId"])
        self.assertEqual(create.headers["idempotency-key"], n.ref_token)
        self.assertEqual((n.outcome, n.provider_status), ("pending", "queued"))

    @override_settings(ORDER_SMS_INSTALL_ID="")
    def test_references_use_a_generated_install_id_when_none_is_configured(self):
        self.register()
        order = self.place()
        n = Notification.objects.get(pk=order["notification"]["notificationId"])
        install = Installation.objects.get()
        self.assertEqual(n.reference, "%s:order_placed:%s" % (install.install_id, order["orderId"]))
        self.assertEqual(len(install.install_id), 32)

    def test_no_number_on_file_is_simply_not_messaged(self):
        order = self.place()
        self.assertIsNone(order["notification"])
        self.assertEqual(self.fake.requests, [])

    def test_provider_refusal_never_fails_the_order(self):
        self.register()
        self.fake.fail_next = [json_response(400, {"code": 21211, "message": "Invalid 'To'"})]
        order = self.place()
        n = order["notification"]
        self.assertEqual((n["outcome"], n["errorCode"]), ("failed", 21211))

    def test_unsent_is_not_the_same_failure_as_unknown(self):
        self.register()
        self.fake.fail_next = [httpx.ConnectError("refused")]
        unsent = self.place()["notification"]
        self.fake.fail_next = [httpx.ReadTimeout("no reply")]
        self.fake.create_status = "sent"
        landed = self.place()["notification"]
        self.fake.fail_next = [json_response(503, {"message": "down"})]
        unknown = self.place()["notification"]

        self.assertEqual((unsent["outcome"], unsent["providerSid"]), ("failed", None))
        # the timed-out write had landed: found by its reference and settled from the provider's status
        self.assertEqual((landed["outcome"], landed["providerStatus"]), ("pending", "sent"))
        self.assertIsNotNone(landed["providerSid"])
        # a 5xx that did not land: the lookup is empty, so it stays unknown - never "failed"
        self.assertEqual((unknown["outcome"], unknown["providerSid"]), ("unknown", None))
        lookups = [r for r in self.fake.requests if r.method == "GET" and "Messages.json" in r.url]
        self.assertEqual(len(lookups), 2)  # only the two may-have-landed writes were looked up

    def test_status_mapping_reads_the_status_not_the_id(self):
        self.assertEqual(tg.status_from_provider("undelivered"), "failed")
        self.assertEqual(tg.status_from_provider("delivered"), "done")
        self.assertEqual(tg.status_from_provider("sent"), "pending")
        self.assertEqual(tg.status_from_provider("canceled"), "failed")
        self.assertEqual(tg.status_from_provider("SOMETHING_NEW"), "unknown")

    def test_dispatch_schedules_a_follow_up_with_the_provider(self):
        self.register()
        order = self.place()
        self.as_user(self.shopper)
        self.assertEqual(self.post("/api/orders/%s/dispatch" % order["orderId"]).status_code, 403)
        self.as_user(self.staff)
        response = self.post("/api/orders/%s/dispatch" % order["orderId"])
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["status"], "Dispatched")
        scheduled = form(self.fake.creates()[-1])
        self.assertEqual(scheduled["ScheduleType"], "fixed")
        self.assertEqual(scheduled["MessagingServiceSid"], TEST_SETTINGS["TWILIO_MESSAGING_SERVICE_SID"])
        self.assertEqual(scheduled["From"], FROM)
        self.assertTrue(scheduled["SendAt"].startswith(str((timezone.now() + timedelta(hours=72)).date())))
        self.assertEqual(body["followUp"]["providerStatus"], "scheduled")
        # dispatching twice is refused, and sends nothing more
        count = len(self.fake.creates())
        self.assertEqual(self.post("/api/orders/%s/dispatch" % order["orderId"]).status_code, 409)
        self.assertEqual(len(self.fake.creates()), count)

    def test_cancel_calls_off_the_queued_follow_up(self):
        self.register()
        order = self.place()
        self.as_user(self.staff)
        followup = self.post("/api/orders/%s/dispatch" % order["orderId"]).json()["followUp"]
        response = self.post("/api/orders/%s/cancel" % order["orderId"])
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["status"], "Cancelled")
        self.assertEqual(body["followUp"]["cancellation"], "done")
        self.assertEqual(self.fake.messages[followup["providerSid"]]["status"], "canceled")
        [cancel] = [r for r in self.fake.updates() if form(r).get("Status") == "canceled"]
        self.assertIn(followup["providerSid"], cancel.url)
        self.assertIn("cancelled", body["notification"]["text"])

    def test_orders_and_notifications_are_private(self):
        self.register()
        order = self.place()
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/orders/%s/notifications" % order["orderId"]).status_code, 404)
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"], [])
        self.as_user(self.shopper)
        notes = self.client.get("/api/orders/%s/notifications" % order["orderId"]).json()["notifications"]
        self.assertEqual([n["kind"] for n in notes], ["order_placed"])
        self.assertIn("notificationId", notes[0])

    def test_reading_notifications_refreshes_the_delivery_outcome(self):
        self.register()
        order = self.place()
        n = Notification.objects.get(pk=order["notification"]["notificationId"])
        self.fake.messages[n.provider_sid]["status"] = "undelivered"
        self.fake.messages[n.provider_sid]["error_code"] = 30034
        Notification.objects.filter(pk=n.pk).update(last_checked_at=None)
        mine = self.client.get("/api/my-orders").json()["orders"][0]["notifications"][0]
        self.assertEqual((mine["outcome"], mine["errorCode"]), ("failed", 30034))


class OperatorTests(OrderSmsTestCase):
    def failed_notification(self):
        self.register()
        self.fake.create_status = "undelivered"
        order = self.place()
        self.fake.create_status = "queued"
        return order["notification"]["notificationId"]

    def test_resend_is_idempotent_per_key(self):
        notification_id = self.failed_notification()
        self.as_user(self.staff)
        before = len(self.fake.creates())
        first = self.post("/api/notifications/%s/resend" % notification_id, **{"Idempotency-Key": "k1"})
        again = self.post("/api/notifications/%s/resend" % notification_id, **{"Idempotency-Key": "k1"})
        fresh = self.post("/api/notifications/%s/resend" % notification_id, **{"Idempotency-Key": "k2"})
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(first.json()["notificationId"], again.json()["notificationId"])
        self.assertNotEqual(first.json()["notificationId"], fresh.json()["notificationId"])
        self.assertEqual(len(self.fake.creates()), before + 2)  # the repeated key sent nothing
        n = Notification.objects.get(pk=first.json()["notificationId"])
        self.assertEqual(n.resend_of_id, notification_id)
        self.assertIn("Ref %s" % n.ref_token, form(self.fake.creates()[before])["Body"])

    def test_resend_requires_a_key_staff_and_a_failed_message(self):
        notification_id = self.failed_notification()
        self.as_user(self.shopper)
        self.assertEqual(self.post("/api/notifications/%s/resend" % notification_id,
                                   **{"Idempotency-Key": "k"}).status_code, 403)
        self.as_user(self.staff)
        self.assertEqual(self.post("/api/notifications/%s/resend" % notification_id).status_code, 400)
        self.as_user(self.shopper)
        ok = self.place()["notification"]["notificationId"]  # queued, not failed
        self.as_user(self.staff)
        self.assertEqual(self.post("/api/notifications/%s/resend" % ok, **{"Idempotency-Key": "k"}).status_code, 409)

    def test_a_called_off_follow_up_is_never_resent(self):
        self.register()
        order = self.place()
        self.as_user(self.staff)
        followup = self.post("/api/orders/%s/dispatch" % order["orderId"]).json()["followUp"]
        self.post("/api/orders/%s/cancel" % order["orderId"])
        response = self.post("/api/notifications/%s/resend" % followup["notificationId"],
                             **{"Idempotency-Key": "oops"})
        self.assertEqual(response.status_code, 409)

    def test_content_disposal_redacts_at_the_provider(self):
        self.register()
        n = self.place()["notification"]
        self.as_user(self.staff)
        response = self.client.delete("/api/notifications/%s/content" % n["notificationId"])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.fake.messages[n["providerSid"]]["body"], "")
        redact = self.fake.updates()[-1]
        self.assertEqual(form(redact), {"Body": ""})
        body = response.json()
        self.assertTrue(body["contentDisposed"])
        self.assertIsNone(body["text"])
        self.assertEqual(body["providerSid"], n["providerSid"])  # the fact it was sent survives

    def test_reconciliation_lines_up_both_sides_for_our_number_only(self):
        self.register()
        order = self.place()
        mine = Notification.objects.get(pk=order["notification"]["notificationId"])
        stranger = self.fake.add_message(OTHER_NUMBER, "not ours")  # from our number, unknown to the app
        self.fake.add_message(OTHER_NUMBER, "an inbound copy", status="received", direction="inbound")
        self.fake.add_message(OTHER_NUMBER, "other traffic", from_="+15559999999")
        lost = self.place()["notification"]
        del self.fake.messages[lost["providerSid"]]  # the app believes it sent this one
        self.as_user(self.staff)
        start = (timezone.now() - timedelta(hours=1)).isoformat()
        end = (timezone.now() + timedelta(hours=1)).isoformat()
        response = self.client.get("/api/notifications/reconciliation", {"from": start, "to": end})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual([m["providerSid"] for m in report["matched"]], [mine.provider_sid])
        self.assertEqual([p["providerSid"] for p in report["providerOnly"]], [stranger["sid"]])
        self.assertEqual([m["notificationId"] for m in report["localOnly"]], [lost["notificationId"]])
        self.assertEqual(report["summary"]["inboundRecordsIgnored"], 1)
        listing = [r for r in self.fake.requests if r.method == "GET" and "Messages.json" in r.url][-1]
        query = parse_qs(urlsplit(listing.url).query)
        self.assertEqual(query["From"], [FROM])
        self.assertIn("DateSent>", query)
        self.assertIn("DateSent<", query)

    def test_reconciliation_is_staff_only_and_validates_range(self):
        self.as_user(self.shopper)
        self.assertEqual(self.client.get("/api/notifications/reconciliation").status_code, 403)
        self.as_user(self.staff)
        self.assertEqual(self.client.get("/api/notifications/reconciliation", {"from": "x", "to": "y"}).status_code,
                         400)


class LoggingTransportTests(TestCase):
    def test_numbers_never_reach_the_log(self):
        class Inner:
            def send(self, request):
                return HttpResponse(status_code=200, headers={})

            def close(self):
                pass

        transport = tg.RedactingLogTransport(Inner())
        with self.assertLogs("apps.order_sms.twilio_gateway", level=logging.INFO) as logs:
            transport.send(HttpRequest(method="GET", url="https://lookups.twilio.com/v1/PhoneNumbers/%2B16045550123",
                                       headers={"authorization": "Basic c2VjcmV0"}))
            transport.send(HttpRequest(method="GET", url="https://api.twilio.com/x/Messages.json?To=%2B16045550123",
                                       headers={}))
        text = "\n".join(logs.output)
        self.assertNotIn("6045550123", text)
        self.assertNotIn("c2VjcmV0", text)
