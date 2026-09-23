"""
Tests for the order SMS notification API.

The Twilio SDK client is real; only its transport is replaced by a stub, so the
SDK's own request building (paths, form fields, auth) runs for every call.
"""

import json
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal as D
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from oscar.test.factories import create_product
from twilio_sdk import TwilioSdkClient
from twilio_sdk.core import FormBody, HttpRequest, HttpResponse

from . import provider
from .models import ContactNumber, Notification
from .twilio_client import redact_url, set_client_for_tests

ACCOUNT = "AC" + "0" * 32
FROM = "+15550000001"
CA_NUMBER = "+16045550123"
US_NUMBER = "+15005550006"

TWILIO_TEST_SETTINGS = dict(
    TWILIO_ACCOUNT_SID=ACCOUNT,
    TWILIO_AUTH_TOKEN="test-token",
    TWILIO_FROM_NUMBER=FROM,
    TWILIO_MESSAGING_SERVICE_SID="MG" + "0" * 32,
    TWILIO_BASE_URL="",
    TWILIO_FOLLOWUP_DELAY_SECONDS=3 * 24 * 3600,
)


def json_response(status, body):
    return HttpResponse(status_code=status, headers={"content-type": "application/json"},
                        content=json.dumps(body).encode())


def message_json(sid, status, *, to=CA_NUMBER, body="hi", date_sent=None, direction="outbound-api",
                 error_code=None):
    return {
        "sid": sid, "status": status, "to": to, "from": FROM, "body": body, "direction": direction,
        "date_sent": date_sent, "date_created": "Wed, 23 Sep 2026 08:00:00 +0000", "error_code": error_code,
        "error_message": None, "account_sid": ACCOUNT,
    }


class StubTransport:
    """Satisfies the SDK's sync transport protocol; answers via ``handler``."""

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[HttpRequest] = []

    def send(self, request):
        self.requests.append(request)
        result = self.handler(request)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        pass

    def calls(self, method, path_fragment):
        return [r for r in self.requests if r.method == method and path_fragment in urlsplit(r.url).path]


def form(request):
    assert isinstance(request.body, FormBody)
    return request.body.fields


class TwilioStubMixin:
    """Routes: lookup, create, fetch, update, list. Override per test."""

    def setUp(self):
        super().setUp()
        self.created = []
        self.sid_counter = 0
        self.create_status = "queued"
        self.create_error = None
        self.update_handler = None
        self.fetch_status = {}
        self.list_pages = None
        self.transport = StubTransport(self.route)
        set_client_for_tests(TwilioSdkClient(
            account_sid_auth_token={"username": ACCOUNT, "password": "test-token"},
            custom_http_client=self.transport))
        self.addCleanup(set_client_for_tests, None)

    def route(self, request):
        path = urlsplit(request.url).path
        if "/v1/PhoneNumbers/" in path:
            number = unquote(path.rsplit("/", 1)[-1])
            if number in (CA_NUMBER, US_NUMBER) or number.replace(" ", "") in (CA_NUMBER, US_NUMBER):
                canonical = CA_NUMBER if "604" in number else US_NUMBER
                return json_response(200, {"phone_number": canonical,
                                           "country_code": "CA" if canonical == CA_NUMBER else "US"})
            return json_response(404, {"code": 20404, "message": "not found", "status": 404})
        if request.method == "POST" and path.endswith("/Messages.json"):
            if self.create_error is not None:
                return self.create_error
            self.sid_counter += 1
            sid = f"SM{self.sid_counter:032d}"
            fields = form(request)
            status = "scheduled" if "SendAt" in fields else self.create_status
            self.created.append((sid, dict(fields)))
            return json_response(201, message_json(sid, status, to=fields["To"], body=fields.get("Body", "")))
        if request.method == "POST" and "/Messages/" in path:
            sid = path.rsplit("/", 1)[-1].removesuffix(".json")
            if self.update_handler:
                return self.update_handler(sid, form(request))
            fields = form(request)
            if fields.get("Status") == "canceled":
                return json_response(200, message_json(sid, "canceled"))
            if fields.get("Body") == "":
                return json_response(200, message_json(sid, self.fetch_status.get(sid, "delivered"), body=""))
        if request.method == "GET" and "/Messages/" in path:
            sid = path.rsplit("/", 1)[-1].removesuffix(".json")
            return json_response(200, message_json(sid, self.fetch_status.get(sid, "delivered"),
                                                   date_sent="Wed, 23 Sep 2026 09:00:00 +0000"))
        if request.method == "GET" and path.endswith("/Messages.json"):
            page = int(parse_qs(urlsplit(request.url).query).get("Page", ["0"])[0])
            pages = self.list_pages or [[]]
            nxt = None
            if page + 1 < len(pages):
                nxt = f"/2010-04-01/Accounts/{ACCOUNT}/Messages.json?PageToken=PA{page + 1}&Page={page + 1}"
            return json_response(200, {"messages": pages[page], "next_page_uri": nxt})
        raise AssertionError(f"unexpected request {request.method} {path}")


@override_settings(**TWILIO_TEST_SETTINGS)
class ApiTestCase(TwilioStubMixin, TestCase):

    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "pass-word-123")
        self.other = User.objects.create_user("other", "other@example.com", "pass-word-123")
        self.staff = User.objects.create_user("op", "op@example.com", "pass-word-123", is_staff=True)
        self.product = create_product(price=D("12.00"), num_in_stock=50)

    # helpers ------------------------------------------------------------
    def as_user(self, user):
        self.client.force_login(user)

    def post(self, url, data=None):
        return self.client.post(url, data=json.dumps(data or {}), content_type="application/json")

    def register(self, number=CA_NUMBER):
        self.as_user(self.shopper)
        return self.post("/api/contact-numbers", {"phoneNumber": number})

    def place(self):
        self.as_user(self.shopper)
        return self.post("/api/orders", {"items": [{"productId": self.product.pk, "quantity": 2}]})

    def dispatch(self, order_id):
        self.as_user(self.staff)
        return self.post(f"/api/orders/{order_id}/dispatch")


class ContactNumberTests(ApiTestCase):

    def test_registers_provider_canonical_form(self):
        with self.assertLogs("apps.order_notifications", level="INFO") as logs:
            response = self.register("+1 604 555 0123")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["phoneNumber"], CA_NUMBER)
        self.assertIn("contactNumberId", response.json())
        self.assertEqual(ContactNumber.objects.get().e164, CA_NUMBER)
        # The number never reaches the logs, neither raw nor canonical.
        joined = "\n".join(logs.output)
        self.assertNotIn("604", joined)

    def test_unusable_number_rejected_at_registration(self):
        response = self.register("12345")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "number_not_usable")
        self.assertFalse(ContactNumber.objects.exists())

    def test_numbers_are_private_to_their_owner(self):
        number_id = self.register().json()["contactNumberId"]
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/contact-numbers").json()["contactNumbers"], [])
        self.assertEqual(self.client.delete(f"/api/contact-numbers/{number_id}").status_code, 404)
        self.assertTrue(ContactNumber.objects.filter(pk=number_id).exists())

    def test_delete_removes_number_and_cancels_queued_followup(self):
        number_id = self.register().json()["contactNumberId"]
        order_id = self.place().json()["orderId"]
        self.dispatch(order_id)
        followup = Notification.objects.get(kind=Notification.KIND_FOLLOWUP)
        self.assertEqual(followup.status, "scheduled")

        self.as_user(self.shopper)
        self.assertEqual(self.client.delete(f"/api/contact-numbers/{number_id}").status_code, 204)
        self.assertEqual(self.client.get("/api/contact-numbers").json()["contactNumbers"], [])
        followup.refresh_from_db()
        self.assertEqual(followup.status, "canceled")
        cancels = [r for r in self.transport.calls("POST", followup.provider_sid)]
        self.assertEqual(form(cancels[0])["Status"], "canceled")

    def test_anonymous_is_401(self):
        self.assertEqual(self.client.get("/api/contact-numbers").status_code, 401)


class OrderFlowTests(ApiTestCase):

    def test_place_order_messages_shopper(self):
        self.register()
        response = self.place()
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIn("orderId", body)
        self.assertEqual(body["lines"][0]["quantity"], 2)
        (sid, fields), = self.created
        self.assertEqual(fields["To"], CA_NUMBER)
        self.assertEqual(fields["From"], FROM)
        self.assertNotIn("SendAt", fields)
        n = Notification.objects.get()
        self.assertEqual((n.kind, n.provider_sid, n.status), ("placed", sid, "pending"))

    def test_no_number_means_no_message(self):
        response = self.place()
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.json()["notified"])
        self.assertEqual(self.transport.requests, [])

    def test_provider_outage_does_not_fail_the_order(self):
        self.register()
        self.create_error = json_response(500, {"code": 20500})
        self.transport.requests.clear()
        response = self.place()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Notification.objects.get().status, "unknown")

    def test_refused_connection_is_not_sent_but_timeout_is_unknown(self):
        self.register()
        self.create_error = httpx.ConnectError("refused")
        self.assertEqual(self.place().status_code, 201)
        unsent = Notification.objects.get()
        self.create_error = httpx.ReadTimeout("no reply")
        self.assertEqual(self.place().status_code, 201)
        timed_out = Notification.objects.exclude(pk=unsent.pk).get()
        self.assertEqual((unsent.status, timed_out.status), ("not_sent", "unknown"))
        # Only the unknown outcome was looked up at the provider.
        self.assertEqual(len(self.transport.calls("GET", "/Messages.json")), 1)

    def test_truncated_create_answer_is_unknown_not_success(self):
        self.register()
        self.create_error = json_response(201, {"status": "queued"})  # no sid
        self.place()
        self.assertEqual(Notification.objects.get().status, "unknown")

    def test_credentials_refused_is_not_sent(self):
        self.register()
        self.create_error = json_response(401, {"code": 20003})
        self.place()
        n = Notification.objects.get()
        self.assertEqual((n.status, n.error_code), ("not_sent", 20003))

    def test_dispatch_sends_now_and_queues_followup_with_provider(self):
        self.register()
        order_id = self.place().json()["orderId"]
        before = datetime.now(dt_timezone.utc)
        response = self.dispatch(order_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "Dispatched")
        _, now_fields, followup_fields = [f for _, f in self.created]
        self.assertNotIn("SendAt", now_fields)
        self.assertEqual(followup_fields["ScheduleType"], "fixed")
        self.assertEqual(followup_fields["MessagingServiceSid"], TWILIO_TEST_SETTINGS["TWILIO_MESSAGING_SERVICE_SID"])
        self.assertEqual(followup_fields["From"], FROM)
        send_at = datetime.fromisoformat(followup_fields["SendAt"].replace("Z", "+00:00"))
        self.assertGreater(send_at, before + timedelta(days=2, hours=23))

    def test_dispatch_is_staff_only(self):
        self.register()
        order_id = self.place().json()["orderId"]
        self.as_user(self.shopper)
        self.assertEqual(self.post(f"/api/orders/{order_id}/dispatch").status_code, 403)

    def test_repeated_dispatch_sends_nothing_more(self):
        self.register()
        order_id = self.place().json()["orderId"]
        self.dispatch(order_id)
        response = self.dispatch(order_id)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["changed"])
        self.assertEqual(len(self.created), 3)

    def test_cancel_calls_off_followup_and_tells_shopper(self):
        self.register()
        order_id = self.place().json()["orderId"]
        self.dispatch(order_id)
        self.as_user(self.staff)
        response = self.post(f"/api/orders/{order_id}/cancel")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "Cancelled")
        self.assertEqual(body["followupCancellations"][0]["settled"], True)
        followup = Notification.objects.get(kind="followup")
        self.assertEqual(followup.status, "canceled")
        self.assertTrue(Notification.objects.filter(kind="cancelled").exists())
        # Cancelling again is a no-op: no further messages.
        count = len(self.created)
        self.assertFalse(self.post(f"/api/orders/{order_id}/cancel").json()["changed"])
        self.assertEqual(len(self.created), count)

    def test_cancel_reports_followup_that_already_went_out(self):
        self.register()
        order_id = self.place().json()["orderId"]
        self.dispatch(order_id)
        followup = Notification.objects.get(kind="followup")
        self.update_handler = lambda sid, fields: json_response(400, {"code": 30409})
        self.fetch_status[followup.provider_sid] = "delivered"
        self.as_user(self.staff)
        body = self.post(f"/api/orders/{order_id}/cancel").json()
        self.assertEqual(body["status"], "Cancelled")
        self.assertTrue(body["followupCancellations"][0]["alreadySent"])

    def test_orders_are_private(self):
        self.register()
        order_id = self.place().json()["orderId"]
        self.as_user(self.other)
        self.assertEqual(self.client.get(f"/api/orders/{order_id}/notifications").status_code, 404)
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"], [])

    def test_my_orders_refreshes_outcome_from_provider(self):
        self.register()
        self.place()
        sid = Notification.objects.get().provider_sid
        self.fetch_status[sid] = "undelivered"
        orders = self.client.get("/api/my-orders").json()["orders"]
        self.assertEqual(orders[0]["notifications"][0]["status"], "failed")
        self.assertEqual(orders[0]["notifications"][0]["providerStatus"], "undelivered")

    def test_unknown_product_404(self):
        self.as_user(self.shopper)
        response = self.post("/api/orders", {"items": [{"productId": 999999, "quantity": 1}]})
        self.assertEqual(response.status_code, 404)


class OperatorTests(ApiTestCase):

    def failed_notification(self):
        self.register()
        self.create_status = "failed"
        self.place()
        self.create_status = "queued"
        return Notification.objects.get()

    def test_resend_is_idempotent_per_key(self):
        source = self.failed_notification()
        self.as_user(self.staff)
        url = f"/api/notifications/{source.pk}/resend"
        first = self.post(url, {"idempotencyKey": "k-1"})
        again = self.post(url, {"idempotencyKey": "k-1"})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(first.json()["notificationId"], again.json()["notificationId"])
        self.assertEqual(len(self.created), 2)  # original + one resend
        fresh = self.post(url, {"idempotencyKey": "k-2"})
        self.assertEqual(fresh.status_code, 201)
        self.assertNotEqual(fresh.json()["notificationId"], first.json()["notificationId"])
        self.assertEqual(len(self.created), 3)

    def test_resend_refuses_delivered_and_key_reuse(self):
        source = self.failed_notification()
        self.as_user(self.staff)
        self.post(f"/api/notifications/{source.pk}/resend", {"idempotencyKey": "k-1"})
        delivered = Notification.objects.exclude(pk=source.pk).get()
        delivered.status = "delivered"
        delivered.save()
        self.assertEqual(self.post(f"/api/notifications/{delivered.pk}/resend",
                                   {"idempotencyKey": "k-9"}).status_code, 409)
        other = self.post(f"/api/notifications/{delivered.pk}/resend", {"idempotencyKey": "k-1"})
        self.assertEqual(other.status_code, 409)

    def test_resend_requires_key_and_staff(self):
        source = self.failed_notification()
        self.as_user(self.staff)
        self.assertEqual(self.post(f"/api/notifications/{source.pk}/resend").status_code, 400)
        self.as_user(self.shopper)
        self.assertEqual(self.post(f"/api/notifications/{source.pk}/resend",
                                   {"idempotencyKey": "x"}).status_code, 403)

    def test_dispose_content_redacts_at_provider_and_keeps_outcome(self):
        self.register()
        self.place()
        n = Notification.objects.get()
        self.fetch_status[n.provider_sid] = "delivered"
        self.as_user(self.staff)
        response = self.client.delete(f"/api/notifications/{n.pk}/content")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["contentDisposed"], body["body"], body["status"]), (True, None, "delivered"))
        redact = [r for r in self.transport.calls("POST", n.provider_sid) if "Body" in form(r)]
        self.assertEqual(form(redact[0])["Body"], "")
        n.refresh_from_db()
        self.assertEqual(n.body, "")

    def test_dispose_refuses_unconfirmed_redaction(self):
        self.register()
        self.place()
        n = Notification.objects.get()
        self.fetch_status[n.provider_sid] = "delivered"
        self.update_handler = lambda sid, fields: json_response(200, message_json(sid, "delivered", body="still here"))
        self.as_user(self.staff)
        self.assertEqual(self.client.delete(f"/api/notifications/{n.pk}/content").status_code, 502)

    def test_dispose_refuses_queued_message(self):
        self.register()
        order_id = self.place().json()["orderId"]
        self.dispatch(order_id)
        followup = Notification.objects.get(kind="followup")
        self.fetch_status[followup.provider_sid] = "scheduled"
        self.assertEqual(self.client.delete(f"/api/notifications/{followup.pk}/content").status_code, 409)


class ReconciliationTests(ApiTestCase):

    def test_report_lines_up_both_sides_and_follows_pages(self):
        self.register()
        self.place()
        ours = Notification.objects.get()
        self.list_pages = [
            [message_json(ours.provider_sid, "delivered", date_sent="Wed, 23 Sep 2026 10:00:00 +0000"),
             message_json("SM" + "9" * 32, "delivered", date_sent="Wed, 23 Sep 2026 11:00:00 +0000",
                          direction="inbound")],
            [message_json("SM" + "8" * 32, "undelivered", date_sent="Wed, 23 Sep 2026 12:00:00 +0000"),
             message_json("SM" + "7" * 32, "delivered", date_sent="Fri, 25 Sep 2026 12:00:00 +0000")],
        ]
        self.as_user(self.staff)
        response = self.client.get("/api/notifications/reconciliation",
                                   {"from": "2026-09-23T00:00:00Z", "to": "2026-09-24T00:00:00Z"})
        self.assertEqual(response.status_code, 200)
        report = response.json()
        self.assertEqual(report["counts"]["matched"], 1)
        self.assertEqual(report["counts"]["providerOnly"], 1)           # the undelivered one; 25th is out of window
        self.assertEqual(report["counts"]["excludedInboundLegs"], 1)
        self.assertFalse(report["truncated"])
        lists = self.transport.calls("GET", "/Messages.json")
        self.assertEqual(len(lists), 2)
        query = parse_qs(urlsplit(lists[0].url).query)
        self.assertEqual(query["From"], [FROM])                          # asked the provider, not filtered after
        self.assertIn("DateSent>", query)
        self.assertIn("DateSent<", query)
        self.assertEqual(parse_qs(urlsplit(lists[1].url).query)["PageToken"], ["PA1"])

    def test_local_send_missing_at_provider_is_local_only(self):
        self.register()
        self.place()
        ours = Notification.objects.get()
        ours.provider_date_sent = datetime(2026, 9, 23, 10, tzinfo=dt_timezone.utc)
        ours.save()
        self.list_pages = [[]]
        self.as_user(self.staff)
        report = self.client.get("/api/notifications/reconciliation",
                                 {"from": "2026-09-23T00:00:00Z", "to": "2026-09-24T00:00:00Z"}).json()
        self.assertEqual(report["counts"]["localOnly"], 1)

    def test_requires_offset_aware_range(self):
        self.as_user(self.staff)
        self.assertEqual(self.client.get("/api/notifications/reconciliation",
                                         {"from": "2026-09-23T00:00:00", "to": "2026-09-24T00:00:00Z"}).status_code,
                         400)


class UnitTests(TestCase):

    def test_status_map_is_explicit(self):
        cases = {"delivered": "delivered", "read": "delivered", "sent": "sent", "queued": "pending",
                 "sending": "pending", "accepted": "pending", "scheduled": "scheduled", "failed": "failed",
                 "undelivered": "failed", "canceled": "canceled", "partially_delivered": "unknown",
                 "received": "unknown", "something_new": "unknown"}
        for status, expected in cases.items():
            self.assertEqual(provider.outcome_from_status(status), expected, status)

    def test_log_urls_are_redacted(self):
        url = f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Messages.json?To=%2B16045550123"
        self.assertEqual(redact_url(url), "api.twilio.com/2010-04-01/Accounts/AC***/Messages.json")
        self.assertEqual(redact_url("https://lookups.twilio.com/v1/PhoneNumbers/%2B16045550123"),
                         "lookups.twilio.com/v1/PhoneNumbers/***")

    @override_settings(**{**TWILIO_TEST_SETTINGS, "TWILIO_BASE_URL": "http://127.0.0.1:9/mock"})
    def test_base_url_override_governs_messaging_only(self):
        from .twilio_client import build_client
        stub = StubTransport(lambda r: json_response(200, message_json("SM1", "sent"))
                             if "/Messages/" in r.url else json_response(200, {"phone_number": CA_NUMBER}))
        with build_client(inner=stub) as client:
            client.api20100401_message.fetch_message(ACCOUNT, "SM1")
            client.lookups_v1_phone_number_api.fetch_phone_number2(CA_NUMBER)
        self.assertTrue(stub.requests[0].url.startswith("http://127.0.0.1:9/mock/2010-04-01/"))
        self.assertTrue(stub.requests[1].url.startswith("https://lookups.twilio.com/"))
        self.assertTrue(stub.requests[0].headers["authorization"].startswith("Basic "))

    @override_settings(**{**TWILIO_TEST_SETTINGS, "TWILIO_AUTH_TOKEN": ""})
    def test_missing_credentials_refused_at_build(self):
        from .twilio_client import TwilioNotConfigured, build_client
        with self.assertRaises(TwilioNotConfigured):
            build_client()
