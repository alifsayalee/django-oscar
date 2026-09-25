"""
Tests for the order SMS API. The Twilio SDK is exercised for real, against a
stub transport (``custom_http_client``), so every request the SDK builds is
asserted on and no network call is made.

Run from ``sandbox/``:  python manage.py test apps.order_sms
"""

import json
from unittest import mock
from datetime import timedelta
from decimal import Decimal as D
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from oscar.test.factories import create_product, create_stockrecord
from twilio_sdk import TwilioSdkClient
from twilio_sdk.core import BasicAuthCredentials, HttpRequest, HttpResponse

from . import provider, services
from .models import ContactNumber, Notification

User = get_user_model()

SHOPPER_NUMBER = "+15005550006"
OTHER_NUMBER = "+15005550009"
FROM_NUMBER = "+15005550001"

TWILIO_SETTINGS = dict(
    TWILIO_ACCOUNT_SID="ACtest",
    TWILIO_AUTH_TOKEN="token-for-tests",
    TWILIO_FROM_NUMBER=FROM_NUMBER,
    TWILIO_MESSAGING_SERVICE_SID="MGtest",
    TWILIO_BASE_URL=None,
    ORDER_SMS_REFERENCE_PREFIX="test-install",
    ORDER_SMS_FOLLOWUP_DELAY_HOURS=72,
)


def json_response(status, body):
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def message_body(sid, status, *, to=SHOPPER_NUMBER, body="text", date_sent=None,
                 date_created="Fri, 25 Sep 2026 10:00:00 +0000", direction="outbound-api",
                 error_code=None):
    return {
        "sid": sid, "status": status, "to": to, "from": FROM_NUMBER, "body": body,
        "direction": direction, "date_sent": date_sent, "date_created": date_created,
        "error_code": error_code, "error_message": None,
    }


class StubProvider:
    """
    A fake Twilio behind the SDK's transport seam: satisfies the transport
    protocol (``send`` + ``close``) and answers by route. ``failures`` lets a
    test make the next create raise or answer an error.
    """

    def __init__(self):
        self.requests: list[HttpRequest] = []
        self.messages: dict[str, dict] = {}
        self.create_failures: list = []
        self.lookup_invalid: set[str] = set()
        self.next_sid = 1

    # transport protocol
    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        parts = urlsplit(request.url)
        path = parts.path
        if "/v1/PhoneNumbers/" in path:
            number = unquote(path.rsplit("/", 1)[1])
            if number in self.lookup_invalid:
                return json_response(404, {"code": 20404, "status": 404})
            digits = "".join(ch for ch in number if ch.isdigit())
            return json_response(200, {"phone_number": "+" + digits, "country_code": "US"})
        if path.endswith("/Messages.json") and request.method == "POST":
            return self._create(request)
        if path.endswith("/Messages.json") and request.method == "GET":
            query = parse_qs(parts.query)
            items = list(self.messages.values())
            if "To" in query:
                items = [m for m in items if m["to"] == query["To"][0]]
            if "From" in query:
                items = [m for m in items if m["from"] == query["From"][0]]
            return json_response(200, {"messages": list(reversed(items)), "next_page_uri": None})
        if "/Messages/" in path:
            sid = path.rsplit("/", 1)[1].removesuffix(".json")
            if sid not in self.messages:
                return json_response(404, {"code": 20404, "status": 404})
            if request.method == "POST":
                fields = request.body.fields
                if fields.get("Status") == "canceled":
                    if self.messages[sid]["status"] != "scheduled":
                        return json_response(400, {"code": 30409, "status": 400})
                    self.messages[sid]["status"] = "canceled"
                if fields.get("Body") == "":
                    self.messages[sid]["body"] = ""
            return json_response(200, self.messages[sid])
        raise AssertionError("unexpected request %s %s" % (request.method, request.url))

    def close(self) -> None:
        pass

    def _create(self, request):
        fields = request.body.fields
        if self.create_failures:
            failure = self.create_failures.pop(0)
            if isinstance(failure, Exception):
                if getattr(failure, "landed", False):
                    self._store(fields, "queued")
                raise failure
            return failure
        status = "scheduled" if fields.get("ScheduleType") == "fixed" else "queued"
        return json_response(201, self._store(fields, status))

    def _store(self, fields, status):
        sid = "SM%030d" % self.next_sid
        self.next_sid += 1
        self.messages[sid] = message_body(sid, status, to=fields["To"], body=fields["Body"])
        return self.messages[sid]

    # assertions
    def creates(self):
        return [r for r in self.requests
                if r.method == "POST" and urlsplit(r.url).path.endswith("/Messages.json")]


def landed(exc):
    """Mark a transport exception as one where the provider still acted."""
    exc.landed = True
    return exc


@override_settings(**TWILIO_SETTINGS)
class OrderSmsTestCase(TestCase):

    def setUp(self):
        self.stub = StubProvider()
        provider.set_client(TwilioSdkClient(
            custom_http_client=self.stub,
            account_sid_auth_token=BasicAuthCredentials(username="ACtest", password="token-for-tests"),
        ))
        self.addCleanup(provider.set_client, None)
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "pw-123456789")
        self.other = User.objects.create_user("other", "other@example.com", "pw-123456789")
        self.staff = User.objects.create_user("op", "op@example.com", "pw-123456789", is_staff=True)
        self.product = create_product(title="Widget")
        create_stockrecord(self.product, num_in_stock=50, price=D("10.00"))

    def post(self, url, data=None, **extra):
        return self.client.post(url, json.dumps(data or {}), content_type="application/json", **extra)

    def register(self, user, number=SHOPPER_NUMBER):
        self.client.force_login(user)
        return self.post("/api/contact-numbers", {"number": number})

    def place(self, user):
        self.client.force_login(user)
        return self.post("/api/orders", {"lines": [{"productId": self.product.pk, "quantity": 2}]})


class ContactNumberTests(OrderSmsTestCase):

    def test_stores_the_providers_canonical_form(self):
        response = self.register(self.shopper, "+1 (500) 555-0006")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["number"], SHOPPER_NUMBER)
        self.assertIn("contactNumberId", response.json())

    def test_rejects_a_number_the_provider_does_not_accept(self):
        self.stub.lookup_invalid.add("12")
        response = self.register(self.shopper, "12")
        self.assertEqual(response.status_code, 422)
        self.assertFalse(ContactNumber.objects.exists())

    def test_base_url_override_moves_messaging_but_not_lookup(self):
        # The real factory, with only the innermost HTTP transport stubbed.
        with self.settings(TWILIO_BASE_URL="https://mock.example.test"),                 mock.patch.object(provider, "HttpxClient", lambda timeout: self.stub):
            provider.set_client(None)
            self.register(self.shopper)
            self.place(self.shopper)
        hosts = {urlsplit(r.url).netloc for r in self.stub.requests}
        self.assertEqual(hosts, {"lookups.twilio.com", "mock.example.test"})
        self.assertTrue(all(r.headers["authorization"].startswith("Basic ")
                            for r in self.stub.requests))

    def test_a_shopper_never_sees_or_deletes_anothers_number(self):
        contact_id = self.register(self.shopper).json()["contactNumberId"]
        self.client.force_login(self.other)
        self.assertEqual(self.client.get("/api/contact-numbers").json()["contactNumbers"], [])
        self.assertEqual(self.client.delete("/api/contact-numbers/%s" % contact_id).status_code, 404)

    def test_deleted_number_is_not_listed_and_not_messaged(self):
        contact_id = self.register(self.shopper).json()["contactNumberId"]
        self.assertEqual(self.client.delete("/api/contact-numbers/%s" % contact_id).status_code, 200)
        self.assertEqual(self.client.get("/api/contact-numbers").json()["contactNumbers"], [])
        self.place(self.shopper)
        self.assertEqual(self.stub.creates(), [])

    def test_requires_sign_in(self):
        self.assertEqual(self.client.get("/api/contact-numbers").status_code, 401)


class OrderEventTests(OrderSmsTestCase):

    def test_placing_an_order_texts_the_shopper(self):
        self.register(self.shopper)
        response = self.place(self.shopper)
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIn("orderId", body)
        [create] = self.stub.creates()
        fields = create.body.fields
        self.assertEqual(fields["To"], SHOPPER_NUMBER)
        self.assertEqual(fields["From"], FROM_NUMBER)
        notification = Notification.objects.get()
        self.assertIn("Ref %s" % provider.reference_token(notification.reference), fields["Body"])
        self.assertEqual(notification.reference, "test-install:order:%s:placed" % body["number"])
        self.assertEqual(notification.outcome, Notification.OUTCOME_PENDING)  # queued is not done
        self.assertTrue(notification.provider_sid)

    def test_no_number_on_file_means_no_message(self):
        self.assertEqual(self.place(self.shopper).status_code, 201)
        self.assertEqual(self.stub.creates(), [])

    def test_the_same_event_twice_sends_one_message(self):
        self.register(self.shopper)
        order_id = self.place(self.shopper).json()["orderId"]
        order = services.Order.objects.get(pk=order_id)
        first = services.notify_order_event(order, Notification.KIND_PLACED)
        second = services.notify_order_event(order, Notification.KIND_PLACED)
        self.assertEqual(len(self.stub.creates()), 1)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.outcome, first.outcome)

    def test_a_send_that_fails_never_fails_the_order(self):
        self.register(self.shopper)
        self.stub.create_failures.append(json_response(500, {"status": 500}))
        response = self.place(self.shopper)
        self.assertEqual(response.status_code, 201)
        notification = Notification.objects.get()
        # A 5xx on a create may have landed: looked up, not found yet => unknown.
        self.assertEqual(notification.outcome, Notification.OUTCOME_UNKNOWN)

    def test_unsent_is_not_the_same_failure_as_unknown(self):
        self.register(self.shopper)
        self.stub.create_failures.append(httpx.ConnectError("refused"))
        self.place(self.shopper)
        unsent = Notification.objects.get()
        lookups_after_unsent = [r for r in self.stub.requests if r.method == "GET" and "Messages.json" in r.url]

        self.stub.create_failures.append(httpx.ReadTimeout("no reply"))
        self.place(self.shopper)
        unknown = Notification.objects.exclude(pk=unsent.pk).get()
        lookups = [r for r in self.stub.requests if r.method == "GET" and "Messages.json" in r.url]

        self.assertEqual((unsent.outcome, unsent.provider_sid), (Notification.OUTCOME_FAILED, None))
        self.assertEqual(unknown.outcome, Notification.OUTCOME_UNKNOWN)
        self.assertEqual(lookups_after_unsent, [])   # never sent: nothing to look up
        self.assertEqual(len(lookups), 1)            # may have landed: looked up by reference
        query = parse_qs(urlsplit(lookups[0].url).query)
        self.assertEqual(query["To"], [SHOPPER_NUMBER])
        self.assertEqual(query["From"], [FROM_NUMBER])

    def test_a_timed_out_send_that_landed_is_found_by_its_reference(self):
        self.register(self.shopper)
        self.stub.create_failures.append(landed(httpx.ReadTimeout("no reply")))
        self.place(self.shopper)
        notification = Notification.objects.get()
        self.assertEqual(notification.outcome, Notification.OUTCOME_PENDING)
        self.assertEqual(notification.provider_sid, next(iter(self.stub.messages)))
        self.assertEqual(len(self.stub.creates()), 1)

    def test_an_unreadable_2xx_is_unknown_not_success(self):
        self.register(self.shopper)
        self.stub.create_failures.append(json_response(201, {"status": "queued"}))  # no sid
        self.place(self.shopper)
        self.assertEqual(Notification.objects.get().outcome, Notification.OUTCOME_UNKNOWN)

    def test_a_released_claim_is_retaken_on_the_next_attempt(self):
        self.register(self.shopper)
        self.stub.create_failures.append(httpx.ConnectError("refused"))
        order = services.Order.objects.get(pk=self.place(self.shopper).json()["orderId"])
        again = services.notify_order_event(order, Notification.KIND_PLACED)
        self.assertEqual(len(self.stub.creates()), 2)
        self.assertEqual(again.outcome, Notification.OUTCOME_PENDING)


class DispatchAndCancelTests(OrderSmsTestCase):

    def setUp(self):
        super().setUp()
        self.register(self.shopper)
        self.order_id = self.place(self.shopper).json()["orderId"]
        self.client.force_login(self.staff)

    def test_dispatch_tells_the_shopper_and_queues_the_follow_up_with_the_provider(self):
        response = self.post("/api/orders/%s/dispatch" % self.order_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "Dispatched")
        dispatched, followup = self.stub.creates()[1:]
        self.assertNotIn("ScheduleType", dispatched.body.fields)
        fields = followup.body.fields
        self.assertEqual(fields["ScheduleType"], "fixed")
        self.assertEqual(fields["MessagingServiceSid"], "MGtest")
        self.assertEqual(fields["From"], FROM_NUMBER)
        send_at = fields["SendAt"]
        self.assertTrue(send_at.startswith(str((timezone.now() + timedelta(hours=72)).date())))
        note = Notification.objects.get(kind=Notification.KIND_FOLLOWUP)
        self.assertEqual(note.provider_status, "scheduled")
        self.assertEqual(note.outcome, Notification.OUTCOME_PENDING)

    def test_cancel_calls_off_the_follow_up_before_it_goes_out(self):
        self.post("/api/orders/%s/dispatch" % self.order_id)
        response = self.post("/api/orders/%s/cancel" % self.order_id)
        self.assertEqual(response.status_code, 200)
        followup = Notification.objects.get(kind=Notification.KIND_FOLLOWUP)
        self.assertEqual(followup.cancel_state, Notification.CANCEL_DONE)
        self.assertEqual(self.stub.messages[followup.provider_sid]["status"], "canceled")
        cancel_calls = [r for r in self.stub.requests
                        if r.method == "POST" and followup.provider_sid in r.url]
        self.assertEqual(cancel_calls[0].body.fields, {"Status": "canceled"})
        kinds = list(Notification.objects.values_list("kind", flat=True))
        self.assertIn(Notification.KIND_CANCELLED, kinds)

    def test_a_follow_up_that_already_went_out_is_reported_too_late(self):
        self.post("/api/orders/%s/dispatch" % self.order_id)
        followup = Notification.objects.get(kind=Notification.KIND_FOLLOWUP)
        self.stub.messages[followup.provider_sid]["status"] = "delivered"
        self.post("/api/orders/%s/cancel" % self.order_id)
        followup.refresh_from_db()
        self.assertEqual(followup.cancel_state, Notification.CANCEL_FAILED)

    def test_operator_actions_need_staff(self):
        self.client.force_login(self.shopper)
        self.assertEqual(self.post("/api/orders/%s/dispatch" % self.order_id).status_code, 403)
        self.assertEqual(self.post("/api/orders/%s/cancel" % self.order_id).status_code, 403)
        note = Notification.objects.first()
        self.assertEqual(self.post("/api/notifications/%s/resend" % note.pk,
                                   HTTP_IDEMPOTENCY_KEY="k").status_code, 403)
        self.assertEqual(self.client.delete("/api/notifications/%s/content" % note.pk).status_code, 403)
        self.assertEqual(self.client.get("/api/notifications/reconciliation").status_code, 403)

    def test_a_shopper_cannot_read_anothers_order(self):
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.get("/api/orders/%s/notifications" % self.order_id).status_code, 404)
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"], [])

    def test_my_orders_reports_where_each_message_got_to(self):
        note = Notification.objects.get()
        self.stub.messages[note.provider_sid]["status"] = "delivered"
        Notification.objects.filter(pk=note.pk).update(last_checked_at=None)
        self.client.force_login(self.shopper)
        [order] = self.client.get("/api/my-orders").json()["orders"]
        self.assertEqual(order["notifications"][0]["outcome"], "done")
        self.assertEqual(order["notifications"][0]["providerStatus"], "delivered")


class OperatorToolTests(OrderSmsTestCase):

    def setUp(self):
        super().setUp()
        self.register(self.shopper)
        self.place(self.shopper)
        self.note = Notification.objects.get()
        self.stub.messages[self.note.provider_sid]["status"] = "undelivered"
        self.stub.messages[self.note.provider_sid]["error_code"] = 30034
        Notification.objects.filter(pk=self.note.pk).update(last_checked_at=None)
        self.client.force_login(self.staff)

    def resend(self, key):
        return self.post("/api/notifications/%s/resend" % self.note.pk, HTTP_IDEMPOTENCY_KEY=key)

    def test_same_key_sends_once_new_key_sends_again(self):
        first = self.resend("key-1")
        repeat = self.resend("key-1")
        fresh = self.resend("key-2")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(repeat.json()["notificationId"], first.json()["notificationId"])
        self.assertEqual(fresh.status_code, 201)
        self.assertNotEqual(fresh.json()["notificationId"], first.json()["notificationId"])
        self.assertEqual(len(self.stub.creates()), 3)  # original + two resends

    def test_a_delivered_message_is_not_resent(self):
        self.stub.messages[self.note.provider_sid]["status"] = "delivered"
        self.assertEqual(self.resend("k").status_code, 409)

    def test_resend_needs_a_key(self):
        response = self.post("/api/notifications/%s/resend" % self.note.pk)
        self.assertEqual(response.status_code, 400)

    def test_content_disposal_erases_the_text_at_the_provider_and_keeps_the_record(self):
        response = self.client.delete("/api/notifications/%s/content" % self.note.pk)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["contentDisposed"])
        self.assertIsNone(data["body"])
        self.assertEqual(data["providerStatus"], "undelivered")
        self.assertEqual(self.stub.messages[self.note.provider_sid]["body"], "")
        redact = [r for r in self.stub.requests if r.method == "POST" and self.note.provider_sid in r.url]
        self.assertEqual(redact[0].body.fields, {"Body": ""})

    def test_reconciliation_lines_up_both_sides_and_asks_only_for_our_number(self):
        sent = "Fri, 25 Sep 2026 10:00:00 +0000"
        self.stub.messages[self.note.provider_sid]["date_sent"] = sent
        services.refresh(self.note, force=True)
        self.stub.messages["SMforeign"] = message_body("SMforeign", "delivered", date_sent=sent)
        response = self.client.get(
            "/api/notifications/reconciliation",
            {"from": "2026-09-25T00:00:00Z", "to": "2026-09-26T00:00:00Z"})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual(report["summary"]["matched"], 1)
        self.assertEqual(report["summary"]["providerOnly"], 1)
        self.assertEqual(report["providerOnly"][0]["providerMessageSid"], "SMforeign")
        listing = [r for r in self.stub.requests if r.method == "GET" and "Messages.json" in r.url][-1]
        query = parse_qs(urlsplit(listing.url).query)
        self.assertEqual(query["From"], [FROM_NUMBER])
        self.assertIn("DateSent>", query)
        self.assertIn("DateSent<", query)


class StatusMappingTests(TestCase):

    def test_statuses(self):
        self.assertEqual(provider.status_from_provider("delivered"), provider.DONE)
        self.assertEqual(provider.status_from_provider("sent"), provider.PENDING)
        self.assertEqual(provider.status_from_provider("scheduled"), provider.PENDING)
        self.assertEqual(provider.status_from_provider("undelivered"), provider.FAILED)
        self.assertEqual(provider.status_from_provider("canceled"), provider.FAILED)
        self.assertEqual(provider.status_from_provider("something_new"), provider.UNKNOWN)
        self.assertEqual(provider.status_from_provider(""), provider.UNKNOWN)
        self.assertEqual(provider.cancel_outcome("canceled"), provider.DONE)
        self.assertEqual(provider.cancel_outcome("sent"), provider.FAILED)
        self.assertEqual(provider.cancel_outcome("something_new"), provider.UNKNOWN)

    def test_log_lines_never_carry_a_phone_number(self):
        url = "https://lookups.twilio.com/v1/PhoneNumbers/%2B15005550006?CountryCode=US"
        self.assertEqual(provider.redact_url(url), "lookups.twilio.com/v1/PhoneNumbers/***")
        listing = "https://api.twilio.com/2010-04-01/Accounts/AC1/Messages.json?To=%2B15005550006"
        self.assertNotIn("5550006", provider.redact_url(listing))
