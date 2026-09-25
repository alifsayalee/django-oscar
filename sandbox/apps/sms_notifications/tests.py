"""
Tests for the SMS notification API. The Twilio SDK client is real; its transport
is a stub, so the SDK's own request building runs and nothing leaves the process.

Run from sandbox/:  ../venv/Scripts/python manage.py test apps.sms_notifications
"""
import json
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import parse_qs, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from oscar.core.loading import get_model
from twilio_sdk import TwilioSdkClient
from twilio_sdk.core import HttpRequest, HttpResponse

from . import gateway
from .models import ContactNumber, SmsNotification
from .safe_write import ref_token

Order = get_model("order", "Order")
Product = get_model("catalogue", "Product")
Partner = get_model("partner", "Partner")
StockRecord = get_model("partner", "StockRecord")
ProductClass = get_model("catalogue", "ProductClass")

# Placeholder numbers: the transport is a stub, nothing is ever sent.
SHOPPER_NUMBER = "+10000000001"
OTHER_NUMBER = "+10000000002"
FROM_NUMBER = "+10000000009"
TWILIO = dict(
    TWILIO_ACCOUNT_SID="ACtest", TWILIO_AUTH_TOKEN="not-a-real-token", TWILIO_FROM_NUMBER=FROM_NUMBER,
    TWILIO_MESSAGING_SERVICE_SID="MGtest", TWILIO_BASE_URL="https://messaging.example.test",
    SMS_NOTIFICATIONS_INSTALL_ID="test-install", SMS_FOLLOWUP_DELAY_HOURS=72,
)
MESSAGES_URL = "https://messaging.example.test/2010-04-01/Accounts/ACtest/Messages"


def json_response(status, body):
    return HttpResponse(status_code=status, headers={"content-type": "application/json"},
                        content=json.dumps(body).encode())


def message(sid, status, body="text", to=SHOPPER_NUMBER, date_sent=None):
    return {"sid": sid, "status": status, "body": body, "to": to, "from": FROM_NUMBER,
            "date_sent": date_sent, "date_created": "Fri, 25 Sep 2026 08:57:51 +0000", "error_code": None}


class StubTransport:
    """Answers queued responses (or raises queued exceptions) in order; records every request."""

    def __init__(self):
        self.queue = []
        self.requests: list[HttpRequest] = []

    def add(self, *items):
        self.queue.extend(items)

    def send(self, request):
        self.requests.append(request)
        if not self.queue:
            raise AssertionError("unexpected provider call: %s %s" % (request.method, request.url))
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(request)
        return item

    def close(self):
        pass

    def calls(self, method, contains=""):
        return [r for r in self.requests if r.method == method and contains in r.url]


def form(request):
    return dict(request.body.fields)


@override_settings(**TWILIO)
class NotificationApiTestCase(TestCase):

    def setUp(self):
        self.transport = StubTransport()
        gateway.use_client(TwilioSdkClient(
            server_config={"default": {"base_url": TWILIO["TWILIO_BASE_URL"]}},
            custom_http_client=self.transport,
            account_sid_auth_token={"username": "ACtest", "password": "not-a-real-token"},
        ))
        self.addCleanup(gateway.use_client, None)
        User = get_user_model()
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "pass-word-123")
        self.other = User.objects.create_user("other", "other@example.com", "pass-word-123")
        self.staff = User.objects.create_user("op", "op@example.com", "pass-word-123", is_staff=True)
        product_class = ProductClass.objects.create(name="T-shirt", requires_shipping=False, track_stock=True)
        self.product = Product.objects.create(title="Shirt", product_class=product_class)
        StockRecord.objects.create(product=self.product, partner=Partner.objects.create(name="P"),
                                   partner_sku="S1", price=10, num_in_stock=100)
        self.client.force_login(self.shopper)

    # ---- helpers -------------------------------------------------------

    def post(self, url, body=None, **headers):
        return self.client.post(url, json.dumps(body or {}), content_type="application/json", **headers)

    def as_staff(self):
        self.client.force_login(self.staff)

    def as_shopper(self):
        self.client.force_login(self.shopper)

    def register(self, number=SHOPPER_NUMBER):
        self.transport.add(json_response(200, {"phone_number": number, "country_code": "US"}))
        return self.post("/api/contact-numbers", {"phoneNumber": number})

    def place(self, send_answer):
        self.transport.add(send_answer)
        response = self.post("/api/orders", {"items": [{"productId": self.product.pk, "quantity": 1}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    # ---- flow 1 --------------------------------------------------------

    def test_register_stores_provider_canonical_form(self):
        self.transport.add(json_response(200, {"phone_number": SHOPPER_NUMBER, "country_code": "US"}))
        response = self.post("/api/contact-numbers", {"phoneNumber": "(000) 000-0001", "countryCode": "us"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["phoneNumber"], SHOPPER_NUMBER)
        self.assertIn("contactNumberId", response.json())
        lookup = self.transport.requests[0]
        self.assertTrue(lookup.url.startswith("https://lookups.twilio.com/v1/PhoneNumbers/"))  # not TWILIO_BASE_URL
        self.assertIn("CountryCode=US", lookup.url)

    def test_register_rejects_number_provider_does_not_know(self):
        self.transport.add(json_response(404, {"code": 20404, "message": "not found"}))
        response = self.post("/api/contact-numbers", {"phoneNumber": "+1999"})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(ContactNumber.objects.exists())

    def test_lookup_credentials_refused_is_ours_not_the_callers(self):
        self.transport.add(json_response(401, {"code": 20003}))
        response = self.post("/api/contact-numbers", {"phoneNumber": "+10000000001"})
        self.assertEqual(response.status_code, 502)

    def test_numbers_are_private_to_their_owner(self):
        number_id = self.register().json()["contactNumberId"]
        self.client.force_login(self.other)
        self.assertEqual(self.client.get("/api/contact-numbers").json()["contactNumbers"], [])
        self.assertEqual(self.client.delete("/api/contact-numbers/%s" % number_id).status_code, 404)
        self.as_shopper()
        self.assertEqual(self.client.delete("/api/contact-numbers/%s" % number_id).status_code, 204)
        self.assertEqual(self.client.get("/api/contact-numbers").json()["contactNumbers"], [])

    def test_deleted_number_is_never_messaged(self):
        number_id = self.register().json()["contactNumberId"]
        self.client.delete("/api/contact-numbers/%s" % number_id)
        order = self.place_no_message()
        self.assertEqual(order["notifications"], [])
        self.assertEqual(self.transport.calls("POST", "/Messages"), [])

    def place_no_message(self):
        response = self.post("/api/orders", {"items": [{"productId": self.product.pk, "quantity": 1}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def test_anonymous_and_non_staff_are_refused(self):
        self.client.logout()
        self.assertEqual(self.client.get("/api/my-orders").status_code, 401)
        self.as_shopper()
        self.assertEqual(self.post("/api/orders/1/dispatch").status_code, 403)
        self.assertEqual(self.client.get("/api/notifications/reconciliation?from=a&to=b").status_code, 403)

    # ---- flow 2: the safe write ---------------------------------------

    def test_order_placed_sends_once_with_derived_reference(self):
        self.register()
        order = self.place(json_response(201, message("SM1", "queued")))
        sends = self.transport.calls("POST", "/Messages.json")
        self.assertEqual(len(sends), 1)
        self.assertTrue(sends[0].url.startswith(MESSAGES_URL))  # TWILIO_BASE_URL governs messaging
        reference = "test-install:order:%s:placed" % order["orderId"]
        self.assertIn("Ref " + ref_token(reference), form(sends[0])["Body"])
        self.assertEqual(form(sends[0])["From"], FROM_NUMBER)
        note = order["notifications"][0]
        self.assertEqual((note["outcome"], note["providerMessageSid"]), ("pending", "SM1"))

        # The same notification requested again makes no provider call.
        from . import services
        again = services.notify(Order.objects.get(pk=order["orderId"]), SmsNotification.KIND_PLACED)
        self.assertEqual(again.pk, note["notificationId"])
        self.assertEqual(len(self.transport.calls("POST", "/Messages.json")), 1)

    def test_refused_and_unlisted_statuses_are_not_done(self):
        self.register()
        self.assertEqual(self.place(json_response(201, message("SM1", "undelivered")))
                         ["notifications"][0]["outcome"], "failed")
        self.assertEqual(self.place(json_response(201, message("SM2", "something_new")))
                         ["notifications"][0]["outcome"], "unknown")

    def test_send_failure_never_fails_the_order(self):
        self.register()
        order = self.place(json_response(400, {"code": 21211, "message": "invalid To"}))
        self.assertEqual(order["status"], "Pending")
        self.assertEqual(order["notifications"][0]["outcome"], "failed")

    def test_unsent_is_not_the_same_failure_as_unknown(self):
        self.register()
        unsent = self.place(httpx.ConnectError("refused"))["notifications"][0]
        # A read timeout may have landed: the app looks it up by reference, finds nothing yet.
        self.transport.add(httpx.ReadTimeout("no reply"), json_response(200, {"messages": [], "next_page_uri": None}))
        unknown = self.place_raw()["notifications"][0]
        self.assertEqual((unsent["outcome"], unsent["providerMessageSid"]), ("failed", None))
        self.assertEqual(unknown["outcome"], "unknown")
        lookups = self.transport.calls("GET", "/Messages.json")
        self.assertEqual(len(lookups), 1)  # only the unknown one is looked up
        self.assertIn("From=%2B10000000009", lookups[0].url)

    def place_raw(self):
        response = self.post("/api/orders", {"items": [{"productId": self.product.pk, "quantity": 1}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def test_may_have_landed_send_is_settled_from_the_provider_by_reference(self):
        self.register()
        # 5xx on the send; the lookup finds the message carrying our reference token.
        def listing(request):  # the provider holds the message carrying the claimed row's token
            token = SmsNotification.objects.latest("pk").ref_token
            return json_response(200, {"messages": [message("SMX", "delivered", body="Hi Ref " + token)],
                                       "next_page_uri": None})
        self.transport.add(json_response(503, {"code": 20500}), listing)
        note = self.place_raw()["notifications"][0]
        self.assertEqual((note["outcome"], note["providerMessageSid"]), ("done", "SMX"))

    # ---- dispatch / follow-up / cancel --------------------------------

    def dispatched_order(self):
        self.register()
        order = self.place(json_response(201, message("SM1", "delivered")))
        self.as_staff()
        self.transport.add(json_response(201, message("SM2", "queued")),
                           json_response(201, message("SM3", "scheduled")))
        response = self.post("/api/orders/%s/dispatch" % order["orderId"])
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_dispatch_queues_the_follow_up_with_the_provider(self):
        order = self.dispatched_order()
        self.assertEqual(order["status"], "Dispatched")
        followup_send = self.transport.calls("POST", "/Messages.json")[-1]
        fields = form(followup_send)
        self.assertEqual(fields["ScheduleType"], "fixed")
        self.assertEqual(fields["MessagingServiceSid"], "MGtest")
        send_at = datetime.fromisoformat(fields["SendAt"].replace("Z", "+00:00"))
        self.assertGreater(send_at, datetime.now(dt_timezone.utc) + timedelta(hours=71))
        kinds = {n["kind"]: n for n in order["notifications"]}
        self.assertEqual(kinds["followup"]["outcome"], "pending")

    def test_cancel_calls_off_the_follow_up(self):
        order = self.dispatched_order()
        self.transport.add(json_response(200, message("SM3", "canceled")),
                           json_response(201, message("SM4", "queued")))
        response = self.post("/api/orders/%s/cancel" % order["orderId"])
        self.assertEqual(response.status_code, 200)
        update = self.transport.calls("POST", "/Messages/SM3.json")
        self.assertEqual(form(update[0]), {"Status": "canceled"})
        kinds = {n["kind"]: n for n in response.json()["notifications"]}
        self.assertEqual(kinds["followup"]["cancelState"], "done")
        self.assertEqual(kinds["cancelled"]["providerMessageSid"], "SM4")
        self.assertEqual(response.json()["status"], "Cancelled")

    def test_deleting_the_number_calls_off_its_scheduled_follow_up(self):
        self.dispatched_order()
        self.as_shopper()
        self.transport.add(json_response(200, message("SM3", "canceled")))
        number_id = ContactNumber.objects.get().pk
        self.assertEqual(self.client.delete("/api/contact-numbers/%s" % number_id).status_code, 204)
        self.assertEqual(form(self.transport.calls("POST", "/Messages/SM3.json")[0]), {"Status": "canceled"})
        self.assertEqual(SmsNotification.objects.get(provider_sid="SM3").cancel_state, "done")

    def test_follow_up_already_gone_is_reported_not_hidden(self):
        order = self.dispatched_order()
        self.transport.add(json_response(400, {"code": 21220}), json_response(200, message("SM3", "delivered")),
                           json_response(201, message("SM4", "queued")))
        response = self.post("/api/orders/%s/cancel" % order["orderId"])
        kinds = {n["kind"]: n for n in response.json()["notifications"]}
        self.assertEqual(kinds["followup"]["cancelState"], "failed")

    def test_orders_are_private_to_their_owner(self):
        self.register()
        order = self.place(json_response(201, message("SM1", "delivered")))
        self.client.force_login(self.other)
        self.assertEqual(self.client.get("/api/orders/%s/notifications" % order["orderId"]).status_code, 404)
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"], [])

    def test_pending_messages_are_refreshed_from_the_provider(self):
        self.register()
        order = self.place(json_response(201, message("SM1", "sent")))
        self.transport.add(json_response(200, message("SM1", "delivered", date_sent="Fri, 25 Sep 2026 09:00:00 +0000")))
        notes = self.client.get("/api/orders/%s/notifications" % order["orderId"]).json()["notifications"]
        self.assertEqual((notes[0]["outcome"], notes[0]["providerStatus"]), ("done", "delivered"))

    # ---- flow 3 ---------------------------------------------------------

    def failed_notification(self):
        self.register()
        order = self.place(json_response(201, message("SM1", "undelivered")))
        return order["notifications"][0]["notificationId"]

    def test_resend_is_idempotent_per_key(self):
        note_id = self.failed_notification()
        self.as_staff()
        self.transport.add(json_response(200, message("SM1", "undelivered")),  # refresh of the original
                           json_response(201, message("SM9", "queued")))
        first = self.post("/api/notifications/%s/resend" % note_id, HTTP_IDEMPOTENCY_KEY="k1")
        self.assertEqual(first.status_code, 201, first.content)
        self.transport.add(json_response(200, message("SM9", "queued")))  # the repeat refreshes its pending row
        repeat = self.post("/api/notifications/%s/resend" % note_id, HTTP_IDEMPOTENCY_KEY="k1")
        self.assertEqual(repeat.json()["notificationId"], first.json()["notificationId"])
        self.assertEqual(len(self.transport.calls("POST", "/Messages.json")), 2)  # original + one resend
        self.transport.add(json_response(200, message("SM1", "undelivered")),
                           json_response(201, message("SM10", "queued")))
        second = self.post("/api/notifications/%s/resend" % note_id, {"idempotencyKey": "k2"})
        self.assertNotEqual(second.json()["notificationId"], first.json()["notificationId"])
        self.assertEqual(len(self.transport.calls("POST", "/Messages.json")), 3)

    def test_resend_needs_a_key_and_a_failed_message(self):
        self.register()
        order = self.place(json_response(201, message("SM1", "delivered")))
        note_id = order["notifications"][0]["notificationId"]
        self.as_staff()
        self.assertEqual(self.post("/api/notifications/%s/resend" % note_id).status_code, 400)
        self.transport.add(json_response(200, message("SM1", "delivered")))  # the original, re-read first
        self.assertEqual(self.post("/api/notifications/%s/resend" % note_id, {"idempotencyKey": "k"}).status_code,
                         409)

    def test_content_disposal_erases_at_the_provider_and_keeps_the_outcome(self):
        note_id = self.failed_notification()
        self.as_staff()
        self.transport.add(json_response(200, message("SM1", "undelivered")),
                           json_response(200, message("SM1", "undelivered", body="")))
        response = self.client.delete("/api/notifications/%s/content" % note_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(form(self.transport.calls("POST", "/Messages/SM1.json")[0]), {"Body": ""})
        data = response.json()
        self.assertEqual((data["body"], data["outcome"], data["providerMessageSid"]), (None, "failed", "SM1"))
        self.assertEqual(SmsNotification.objects.get(pk=note_id).body, "")

    def test_content_kept_when_provider_does_not_confirm_erasure(self):
        note_id = self.failed_notification()
        self.as_staff()
        self.transport.add(json_response(200, message("SM1", "undelivered")),
                           json_response(200, message("SM1", "undelivered", body="still here")))
        self.assertEqual(self.client.delete("/api/notifications/%s/content" % note_id).status_code, 502)
        self.assertNotEqual(SmsNotification.objects.get(pk=note_id).body, "")

    def test_reconciliation_pages_through_our_number_and_classifies(self):
        self.register()
        self.place(json_response(201, message("SM1", "delivered", date_sent="Fri, 25 Sep 2026 10:00:00 +0000")))
        self.as_staff()
        page1 = {"messages": [message("SM1", "delivered", date_sent="Fri, 25 Sep 2026 10:00:00 +0000"),
                              message("SMother", "delivered", to=OTHER_NUMBER,
                                      date_sent="Fri, 25 Sep 2026 11:00:00 +0000")],
                 "next_page_uri": "/2010-04-01/Accounts/ACtest/Messages.json?PageSize=1000&Page=1&PageToken=PA1"}
        page2 = {"messages": [message("SMlate", "delivered", date_sent="Sun, 27 Sep 2026 10:00:00 +0000")],
                 "next_page_uri": None}
        self.transport.add(json_response(200, page1), json_response(200, page2))
        response = self.client.get("/api/notifications/reconciliation",
                                   {"from": "2026-09-25T00:00:00Z", "to": "2026-09-26T00:00:00Z"})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual(report["counts"]["matched"], 1)
        self.assertEqual([m["providerMessageSid"] for m in report["providerOnly"]], ["SMother"])  # SMlate: outside
        lists = self.transport.calls("GET", "/Messages.json")
        self.assertEqual(len(lists), 2)
        first = parse_qs(urlsplit(lists[0].url).query)
        self.assertEqual(first["From"], [FROM_NUMBER])  # asked the provider for our number only
        self.assertIn("DateSent>", first)
        self.assertEqual(parse_qs(urlsplit(lists[1].url).query)["PageToken"], ["PA1"])
