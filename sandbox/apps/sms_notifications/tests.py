"""Tests for SMS order notifications.

The SDK client is real; only its transport is faked (``custom_http_client``), so every request the
SDK builds -- path, form fields, query string -- is what these tests assert on.
"""

import json
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from oscar.core.loading import get_model
from twilio_sdk.core import FormBody, HttpRequest, HttpResponse

from . import provider, services
from .models import ContactNumber, Notification, NotificationAction

Order = get_model("order", "Order")
Product = get_model("catalogue", "Product")
Partner = get_model("partner", "Partner")
StockRecord = get_model("partner", "StockRecord")
ProductClass = get_model("catalogue", "ProductClass")

CANADA = "+15145550123"  # fixtures only; nothing here reaches a network
US = "+15005550006"
FROM = "+15550009999"
SID = "AC" + "0" * 32


def json_response(status, body):
    return HttpResponse(status_code=status, headers={"content-type": "application/json"}, content=json.dumps(body).encode())


def message(sid, status, *, to=CANADA, body="hello", date_sent=None, from_=FROM):
    return {
        "sid": sid,
        "status": status,
        "to": to,
        "from": from_,
        "body": body,
        "date_created": "Sat, 27 Sep 2026 10:00:00 +0000",
        "date_sent": date_sent,
        "error_code": None,
        "error_message": None,
    }


class FakeTwilio:
    """A transport that answers like the Twilio endpoints this app uses, and records every request."""

    def __init__(self):
        self.requests: list[HttpRequest] = []
        self.messages: dict[str, dict] = {}
        self.create_status = "queued"
        self.fail_next_create: Exception | HttpResponse | None = None
        self.fail_next_update: HttpResponse | None = None
        self.lookup_valid = True
        self.list_pages: list[list[dict]] | None = None
        self._n = 0

    # the transport protocol
    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = unquote(urlsplit(request.url).path)
        fields = request.body.fields if isinstance(request.body, FormBody) else {}
        if "/v2/PhoneNumbers/" in path:
            number = path.rsplit("/", 1)[1]
            return json_response(200, {"phone_number": number.replace(" ", ""), "valid": self.lookup_valid,
                                       "country_code": "CA", "validation_errors": [] if self.lookup_valid else ["TOO_SHORT"]})
        match = re.search(r"/Messages/(SM\w+)\.json$", path)
        if request.method == "POST" and path.endswith("/Messages.json"):
            failure, self.fail_next_create = self.fail_next_create, None
            self._n += 1
            sid = f"SM{self._n:032d}"
            if isinstance(failure, Exception):
                if isinstance(failure, httpx.ReadTimeout):  # it landed, the answer was lost
                    self.messages[sid] = message(sid, self.create_status, to=fields["To"], body=fields["Body"])
                raise failure
            if isinstance(failure, HttpResponse):
                return failure
            status = "scheduled" if fields.get("ScheduleType") == "fixed" else self.create_status
            self.messages[sid] = message(sid, status, to=fields["To"], body=fields["Body"])
            return json_response(201, self.messages[sid])
        if match and request.method == "POST":
            failure, self.fail_next_update = self.fail_next_update, None
            if failure is not None:
                return failure
            msg = self.messages[match.group(1)]
            if fields.get("Status") == "canceled":
                msg["status"] = "canceled"
            if fields.get("Body") == "":
                msg["body"] = ""
            return json_response(200, msg)
        if match and request.method == "GET":
            return json_response(200, self.messages[match.group(1)])
        if request.method == "GET" and path.endswith("/Messages.json"):
            query = parse_qs(urlsplit(request.url).query)
            if self.list_pages is not None:
                page = int(query.get("Page", ["0"])[0])
                nxt = None if page + 1 >= len(self.list_pages) else f"/x/Messages.json?Page={page + 1}&PageToken=PT{page + 1}"
                return json_response(200, {"messages": self.list_pages[page], "next_page_uri": nxt})
            to = query.get("To", [None])[0]
            found = [m for m in reversed(list(self.messages.values())) if to is None or m["to"] == to]
            return json_response(200, {"messages": found, "next_page_uri": None})
        return json_response(404, {"code": 20404, "message": "not found"})

    def close(self) -> None:
        pass

    # helpers for assertions
    def writes(self):
        return [r for r in self.requests if r.method == "POST" and "/Messages" in r.url]

    def fields(self, request):
        return request.body.fields


@override_settings(
    TWILIO_ACCOUNT_SID=SID,
    TWILIO_AUTH_TOKEN="test-token",
    TWILIO_FROM_NUMBER=FROM,
    TWILIO_MESSAGING_SERVICE_SID="MG" + "0" * 32,
    TWILIO_BASE_URL=None,
    SMS_INSTALL_ID="test",
    SMS_FOLLOWUP_DELAY_MINUTES=60,
)
class SmsApiTestCase(TestCase):
    def setUp(self):
        self.fake = FakeTwilio()
        config = services.twilio_config()
        self.previous = services.set_messaging(
            provider.Messaging(provider.build_client(config, transport=self.fake), config)
        )
        User = get_user_model()
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "pw-shopper-123")
        self.other = User.objects.create_user("other", "other@example.com", "pw-other-12345")
        self.staff = User.objects.create_user("op", "op@example.com", "pw-staff-12345", is_staff=True)
        self.product = self._product()

    def tearDown(self):
        services.set_messaging(self.previous)

    def _product(self):
        pclass = ProductClass.objects.create(name="Books", requires_shipping=False, track_stock=True)
        product = Product.objects.create(title="A book", product_class=pclass, structure="standalone")
        partner = Partner.objects.create(name="P")
        StockRecord.objects.create(product=product, partner=partner, partner_sku="sku1", price=10, num_in_stock=100)
        return product

    def as_user(self, user):
        self.client.force_login(user)

    def post(self, url, data=None, **extra):
        return self.client.post(url, json.dumps(data or {}), content_type="application/json", **extra)

    def register(self, number=CANADA):
        return self.post("/api/contact-numbers", {"phoneNumber": number})

    def place(self):
        response = self.post("/api/orders", {"lines": [{"productId": self.product.pk, "quantity": 1}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()


class ContactNumberTests(SmsApiTestCase):
    def test_registers_the_providers_canonical_form(self):
        self.as_user(self.shopper)
        response = self.register("+1 514 555 0123")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["phoneNumber"], CANADA)
        self.assertIn("contactNumberId", response.json())
        lookup = self.fake.requests[-1]
        self.assertTrue(lookup.url.startswith("https://lookups.twilio.com/v2/PhoneNumbers/"))
        self.assertTrue(lookup.headers["authorization"].startswith("Basic "))

    def test_rejects_a_number_the_provider_considers_invalid(self):
        self.as_user(self.shopper)
        self.fake.lookup_valid = False
        response = self.register("+1555")
        self.assertEqual(response.status_code, 422)
        self.assertFalse(ContactNumber.objects.exists())

    def test_numbers_are_private_to_their_owner(self):
        self.as_user(self.shopper)
        number_id = self.register().json()["contactNumberId"]
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/contact-numbers").json(), {"contactNumbers": []})
        self.assertEqual(self.client.delete(f"/api/contact-numbers/{number_id}").status_code, 404)
        self.as_user(self.shopper)
        self.assertEqual(self.client.delete(f"/api/contact-numbers/{number_id}").status_code, 204)
        self.assertEqual(self.client.get("/api/contact-numbers").json(), {"contactNumbers": []})

    def test_requires_login(self):
        self.assertEqual(self.client.get("/api/contact-numbers").status_code, 401)

    def test_the_number_is_never_logged(self):
        self.as_user(self.shopper)
        self.fake.fail_next_create = httpx.ConnectError("refused")
        with self.assertLogs("apps.sms_notifications", level="DEBUG") as logs:
            self.register()
            self.place()
        self.assertTrue(logs.output)
        self.assertFalse(any(CANADA in line or CANADA[2:] in line for line in logs.output))


class OrderFlowTests(SmsApiTestCase):
    def test_placing_an_order_texts_the_shopper(self):
        self.as_user(self.shopper)
        self.register()
        order = self.place()
        self.assertIn("orderId", order)
        [sent] = self.fake.writes()
        fields = self.fake.fields(sent)
        self.assertEqual((fields["To"], fields["From"]), (CANADA, FROM))
        self.assertIn(order["number"], fields["Body"])
        [n] = order["notifications"]
        self.assertEqual((n["kind"], n["outcome"], n["providerStatus"]), ("placed", "pending", "queued"))
        self.assertEqual(Order.objects.get(pk=order["orderId"]).user, self.shopper)

    def test_no_number_on_file_means_no_message(self):
        self.as_user(self.shopper)
        order = self.place()
        self.assertEqual(order["notifications"], [])
        self.assertEqual(self.fake.writes(), [])

    def test_a_refused_message_never_fails_the_order(self):
        self.as_user(self.shopper)
        self.register()
        self.fake.fail_next_create = json_response(400, {"code": 21211, "message": "Invalid 'To'"})
        order = self.place()
        [n] = order["notifications"]
        self.assertEqual((n["outcome"], n["errorCode"]), ("failed", 21211))

    def test_unsent_and_unknown_are_different_outcomes(self):
        self.as_user(self.shopper)
        self.register()
        self.fake.fail_next_create = httpx.ConnectError("refused")
        refused = self.place()["notifications"][0]
        self.fake.fail_next_create = httpx.ReadTimeout("no reply")
        lost = self.place()["notifications"][0]
        self.assertEqual(refused["outcome"], "failed")  # never left: nothing happened
        self.assertEqual(lost["outcome"], "pending")  # may have landed: the lookup by reference found it
        self.assertIsNotNone(lost["providerSid"])

    def test_an_unfound_write_stays_unknown_and_is_settled_later(self):
        self.as_user(self.shopper)
        self.register()
        self.fake.fail_next_create = httpx.ReadTimeout("no reply")
        real_find = services.get_messaging().find_by_reference
        services.get_messaging().find_by_reference = lambda to, ref: None  # the provider has not indexed it yet
        try:
            order = self.place()
        finally:
            del services.get_messaging().find_by_reference
        self.assertEqual(order["notifications"][0]["outcome"], "unknown")
        self.assertEqual(len(self.fake.writes()), 1)
        listed = self.client.get(f"/api/orders/{order['orderId']}/notifications").json()["notifications"]
        self.assertEqual(listed[0]["outcome"], "pending")
        self.assertEqual(len(self.fake.writes()), 1)  # settled by lookup, never by a second send
        self.assertIsNotNone(real_find)

    def test_orders_are_private_to_their_owner(self):
        self.as_user(self.shopper)
        order = self.place()
        self.as_user(self.other)
        self.assertEqual(self.client.get(f"/api/orders/{order['orderId']}/notifications").status_code, 404)
        self.assertEqual(self.client.get("/api/my-orders").json(), {"orders": []})

    def test_operator_actions_need_staff(self):
        self.as_user(self.shopper)
        order = self.place()
        self.assertEqual(self.post(f"/api/orders/{order['orderId']}/dispatch").status_code, 403)
        self.assertEqual(self.post(f"/api/orders/{order['orderId']}/cancel").status_code, 403)
        self.assertEqual(self.client.get("/api/notifications/reconciliation?from=2026-01-01T00:00:00Z&to=2026-01-02T00:00:00Z").status_code, 403)

    def dispatched(self):
        self.as_user(self.shopper)
        self.register()
        order = self.place()
        self.as_user(self.staff)
        response = self.post(f"/api/orders/{order['orderId']}/dispatch")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_dispatch_queues_the_follow_up_with_the_provider(self):
        order = self.dispatched()
        self.assertTrue(order["dispatched"])
        kinds = {n["kind"]: n for n in order["notifications"]}
        self.assertEqual(kinds["dispatched"]["outcome"], "pending")
        self.assertEqual((kinds["delivery_follow_up"]["outcome"], kinds["delivery_follow_up"]["providerStatus"]), ("pending", "scheduled"))
        scheduled = self.fake.fields(self.fake.writes()[-1])
        self.assertEqual(scheduled["ScheduleType"], "fixed")
        self.assertEqual(scheduled["MessagingServiceSid"], "MG" + "0" * 32)
        send_at = datetime.fromisoformat(scheduled["SendAt"].replace("Z", "+00:00"))
        self.assertGreater(send_at, datetime.now(dt_timezone.utc) + timedelta(minutes=55))

    def test_cancel_calls_off_the_follow_up_once(self):
        order = self.dispatched()
        follow_up = next(n for n in order["notifications"] if n["kind"] == "delivery_follow_up")
        response = self.post(f"/api/orders/{order['orderId']}/cancel")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "Cancelled")
        self.assertEqual(body["followUpCancellation"]["outcome"], "done")
        self.assertEqual(self.fake.messages[follow_up["providerSid"]]["status"], "canceled")
        calls = [r for r in self.fake.writes() if follow_up["providerSid"] in r.url]
        self.assertEqual([self.fake.fields(r) for r in calls], [{"Status": "canceled"}])
        self.assertIn("cancelled", {n["kind"] for n in body["notifications"]})
        writes = len(self.fake.writes())
        again = self.post(f"/api/orders/{order['orderId']}/cancel").json()  # repeat: nothing new is sent
        self.assertEqual(len(self.fake.writes()), writes)
        self.assertEqual(again["followUpCancellation"]["outcome"], "done")

    def test_a_follow_up_for_a_cancelled_order_cannot_be_resent(self):
        order = self.dispatched()
        follow_up = next(n for n in order["notifications"] if n["kind"] == "delivery_follow_up")
        self.post(f"/api/orders/{order['orderId']}/cancel")
        response = self.post(f"/api/notifications/{follow_up['notificationId']}/resend", {"idempotencyKey": "k1"})
        self.assertEqual(response.status_code, 409)

    def test_a_deleted_number_is_never_messaged_again(self):
        self.as_user(self.shopper)
        number_id = self.register().json()["contactNumberId"]
        self.fake.fail_next_create = json_response(400, {"code": 30003})
        order = self.place()
        self.client.delete(f"/api/contact-numbers/{number_id}")
        self.as_user(self.staff)
        failed = order["notifications"][0]["notificationId"]
        self.assertEqual(self.post(f"/api/notifications/{failed}/resend", {"idempotencyKey": "k"}).status_code, 409)
        dispatched = self.post(f"/api/orders/{order['orderId']}/dispatch")
        self.assertEqual(dispatched.status_code, 200)
        self.assertEqual([n["kind"] for n in dispatched.json()["notifications"]], ["placed"])
        self.assertEqual(len(self.fake.writes()), 1)  # only the original, refused attempt


class OperatorTests(SmsApiTestCase):
    def failed_notification(self):
        self.as_user(self.shopper)
        self.register()
        self.fake.create_status = "undelivered"
        order = self.place()
        self.fake.create_status = "queued"
        self.as_user(self.staff)
        return order["notifications"][0]

    def test_resend_is_once_per_key(self):
        failed = self.failed_notification()
        self.assertEqual(failed["outcome"], "failed")
        url = f"/api/notifications/{failed['notificationId']}/resend"
        first = self.post(url, HTTP_IDEMPOTENCY_KEY="key-1")
        second = self.post(url, HTTP_IDEMPOTENCY_KEY="key-1")
        self.assertEqual(first.status_code, 202)  # accepted by the provider, not yet delivered
        self.assertEqual(first.json()["notificationId"], second.json()["notificationId"])
        self.assertEqual(len(self.fake.writes()), 2)  # the original + ONE resend
        third = self.post(url, HTTP_IDEMPOTENCY_KEY="key-2")
        self.assertNotEqual(third.json()["notificationId"], first.json()["notificationId"])
        self.assertEqual(len(self.fake.writes()), 3)
        refs = [re.search(r"Ref (\w+)$", self.fake.fields(r)["Body"]).group(1) for r in self.fake.writes()]
        self.assertEqual(len(set(refs)), 3)

    def test_resend_requires_a_key_and_a_failed_message(self):
        failed = self.failed_notification()
        self.assertEqual(self.post(f"/api/notifications/{failed['notificationId']}/resend").status_code, 400)
        ok = self.post(f"/api/notifications/{failed['notificationId']}/resend", {"idempotencyKey": "a"}).json()
        response = self.post(f"/api/notifications/{ok['notificationId']}/resend", {"idempotencyKey": "b"})
        self.assertEqual(response.status_code, 409)  # the resend is still pending: nothing to re-send

    def test_disposing_content_redacts_it_at_the_provider(self):
        failed = self.failed_notification()
        url = f"/api/notifications/{failed['notificationId']}/content"
        response = self.client.delete(url)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.fake.messages[failed["providerSid"]]["body"], "")
        self.assertEqual(self.fake.fields(self.fake.writes()[-1]), {"Body": ""})
        n = response.json()["notification"]
        self.assertIsNone(n["content"])
        self.assertEqual((n["outcome"], n["providerStatus"]), ("failed", "undelivered"))  # the facts survive
        writes = len(self.fake.writes())
        self.assertEqual(self.client.delete(url).status_code, 200)
        self.assertEqual(len(self.fake.writes()), writes)
        self.assertEqual(Notification.objects.get(pk=failed["notificationId"]).body, "")

    def test_disposal_refused_by_the_provider_is_not_success(self):
        failed = self.failed_notification()
        self.fake.fail_next_update = json_response(400, {"code": 20009})
        response = self.client.delete(f"/api/notifications/{failed['notificationId']}/content")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(NotificationAction.objects.get().outcome, "failed")
        self.assertEqual(self.client.delete(f"/api/notifications/{failed['notificationId']}/content").status_code, 200)


class ReconciliationTests(SmsApiTestCase):
    def test_lines_up_our_numbers_messages_across_pages(self):
        self.as_user(self.shopper)
        self.register()
        order = self.place()
        ours = order["notifications"][0]["providerSid"]
        self.fake.list_pages = [
            [message(ours, "delivered", date_sent="Sat, 27 Sep 2026 10:05:00 +0000")],
            [
                message("SMforeign", "sent", date_sent="Sat, 27 Sep 2026 11:00:00 +0000"),
                message("SMlate", "sent", date_sent="Sun, 28 Sep 2026 09:00:00 +0000"),  # outside the window
            ],
        ]
        Notification.objects.create(
            reference="test:x", order_id=order["orderId"], user=self.shopper, kind="placed", outcome="pending",
            provider_sid="SMmissing", date_sent=datetime(2026, 9, 27, 12, tzinfo=dt_timezone.utc),
        )
        self.as_user(self.staff)
        response = self.client.get(
            "/api/notifications/reconciliation", {"from": "2026-09-27T00:00:00Z", "to": "2026-09-28T00:00:00Z"}
        )
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual(report["counts"]["matched"], 1)
        self.assertEqual([m["providerSid"] for m in report["providerOnly"]], ["SMforeign"])
        self.assertEqual([m["providerSid"] for m in report["localOnly"]], ["SMmissing"])
        self.assertTrue(report["complete"])
        lists = [r for r in self.fake.requests if r.method == "GET" and r.url.split("?")[0].endswith("/Messages.json")]
        self.assertEqual(len(lists), 2)
        query = parse_qs(urlsplit(lists[0].url).query)
        self.assertEqual(query["From"], [FROM])  # asked for our number's messages only
        self.assertIn("DateSent>", query)
        self.assertEqual(parse_qs(urlsplit(lists[1].url).query)["PageToken"], ["PT1"])
        self.assertEqual(Notification.objects.get(provider_sid=ours).outcome, "done")

    def test_rejects_a_bad_range(self):
        self.as_user(self.staff)
        self.assertEqual(self.client.get("/api/notifications/reconciliation?from=nope&to=x").status_code, 400)


class MapperTests(TestCase):
    def test_statuses_that_are_not_done_never_answer_success(self):
        from twilio_sdk.models.enums import MessageEnumStatus as S

        self.assertEqual(provider.status_from_provider(S.DELIVERED), "done")
        for status in (S.QUEUED, S.SENT, S.SCHEDULED, S.ACCEPTED):
            self.assertEqual(provider.status_from_provider(status), "pending")
        for status in (S.FAILED, S.UNDELIVERED, S.CANCELED):
            self.assertEqual(provider.status_from_provider(status), "failed")
        self.assertEqual(provider.status_from_provider("something_new"), "unknown")
        self.assertEqual(provider.cancel_outcome(S.CANCELED), "done")
        self.assertEqual(provider.cancel_outcome(S.DELIVERED), "failed")
        for outcome in ("pending", "sending", "failed", "needs_review", "unknown"):
            self.assertNotIn(provider.answer_status(outcome), (200, 201, 204))
