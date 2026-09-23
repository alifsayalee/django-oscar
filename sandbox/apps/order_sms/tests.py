"""
Tests for order SMS notifications. Twilio is faked at the SDK's transport
seam, so the real request-building pipeline runs and no network is used.

Run: python sandbox/manage.py test apps.order_sms
"""
import itertools
import json
import re
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from email.utils import format_datetime
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from oscar.test.factories import create_product
from twilio_sdk.core import HttpRequest, HttpResponse

from . import services
from . import twilio_gateway as gateway
from .models import ContactNumber, Notification

User = get_user_model()

FROM = "+15550000001"
SHOPPER_NUMBER = "+18255550123"

TWILIO_SETTINGS = dict(
    TWILIO_ACCOUNT_SID="AC_test",
    TWILIO_AUTH_TOKEN="token_test",
    TWILIO_FROM_NUMBER=FROM,
    TWILIO_MESSAGING_SERVICE_SID="MG_test",
    TWILIO_BASE_URL="",
    ORDER_SMS_FOLLOWUP_DELAY_SECONDS=3 * 24 * 3600,
)


def json_response(status, body):
    return HttpResponse(
        status_code=status, headers={"content-type": "application/json"}, content=json.dumps(body).encode()
    )


def rfc1123(dt):
    return format_datetime(dt, usegmt=True)


class FakeTwilio:
    """An in-memory Twilio behind the SDK's transport protocol (send + close)."""

    def __init__(self):
        self.requests: list[HttpRequest] = []
        self.messages: dict[str, dict] = {}
        self.overrides: list[tuple[str, re.Pattern, object]] = []
        self._sids = itertools.count(1)

    # -- test controls -------------------------------------------------------
    def fail_next(self, method, pattern, outcome):
        """``outcome`` is an exception to raise or an HttpResponse to return, once."""
        self.overrides.append((method, re.compile(pattern), outcome))

    def add_message(self, **fields):
        sid = fields.pop("sid", None) or f"SM{next(self._sids):032d}"
        now = datetime.now(dt_timezone.utc)
        message = {
            "sid": sid,
            "status": "delivered",
            "body": "x",
            "to": "+18255550000",
            "from": FROM,
            "direction": "outbound-api",
            "date_created": rfc1123(now),
            "date_sent": rfc1123(now),
            "error_code": None,
            "error_message": None,
        }
        message.update(fields)
        self.messages[sid] = message
        return message

    def sent(self, method="POST", path_suffix="/Messages.json"):
        return [r for r in self.requests if r.method == method and urlsplit(r.url).path.endswith(path_suffix)]

    # -- transport protocol --------------------------------------------------
    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = urlsplit(request.url).path
        for i, (method, pattern, outcome) in enumerate(self.overrides):
            if method == request.method and pattern.search(path):
                del self.overrides[i]
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        if request.method == "GET" and path.startswith("/v1/PhoneNumbers/"):
            return self._lookup(unquote(path.rsplit("/", 1)[1]))
        if path.endswith("/Messages.json"):
            return self._create(request) if request.method == "POST" else self._list(request)
        match = re.search(r"/Messages/(SM\w+)\.json$", path)
        if match:
            message = self.messages.get(match.group(1))
            if message is None:
                return json_response(404, {"code": 20404, "message": "not found"})
            if request.method == "POST":
                return self._update(message, request.body.fields)
            return json_response(200, message)
        raise AssertionError(f"unexpected request {request.method} {path}")

    def close(self) -> None:
        pass

    # -- behaviour -----------------------------------------------------------
    def _lookup(self, number):
        digits = re.sub(r"\D", "", number)
        if len(digits) == 10:
            digits = "1" + digits
        if len(digits) != 11:
            return json_response(404, {"code": 20404, "message": "not found", "status": 404})
        return json_response(200, {"phone_number": f"+{digits}", "country_code": "CA", "national_format": "x"})

    def _create(self, request):
        fields = request.body.fields
        scheduled = fields.get("ScheduleType") == "fixed"
        return json_response(
            201,
            self.add_message(
                status="scheduled" if scheduled else "queued",
                body=fields["Body"],
                to=fields["To"],
                **{"from": fields.get("From")},
                date_sent=None,
            ),
        )

    def _update(self, message, fields):
        if fields.get("Status") == "canceled":
            if message["status"] != "scheduled":
                return json_response(400, {"code": 30409, "message": "cannot cancel"})
            message["status"] = "canceled"
        if fields.get("Body") == "":
            message["body"] = ""
        return json_response(200, message)

    def _list(self, request):
        query = parse_qs(urlsplit(request.url).query)
        rows = [
            m
            for m in self.messages.values()
            if m["from"] == query.get("From", [None])[0]
            and ("To" not in query or m["to"] == query["To"][0])
        ]
        return json_response(200, {"messages": rows, "next_page_uri": None, "page": 0, "page_size": 50})


@override_settings(**TWILIO_SETTINGS)
class OrderSmsTestCase(TestCase):
    def setUp(self):
        self.twilio = FakeTwilio()
        gateway.set_client(gateway.build_client(http_client=self.twilio))
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "pw-shopper-1")
        self.other = User.objects.create_user("other", "other@example.com", "pw-other-1")
        self.operator = User.objects.create_user("operator", "operator@example.com", "pw-operator-1")
        self.operator.is_staff = True
        self.operator.save()
        self.product = create_product(price=Decimal("10.00"), num_in_stock=100)

    def tearDown(self):
        gateway.set_client(None)

    # -- helpers -------------------------------------------------------------
    def as_user(self, user):
        self.client.force_login(user)

    def post(self, url, body=None, **headers):
        return self.client.post(url, json.dumps(body or {}), content_type="application/json", headers=headers)

    def register(self, number=SHOPPER_NUMBER):
        self.as_user(self.shopper)
        response = self.post("/api/contact-numbers", {"phoneNumber": number})
        self.assertIn(response.status_code, (200, 201), response.content)
        return response.json()

    def place(self):
        self.as_user(self.shopper)
        response = self.post("/api/orders", {"lines": [{"productId": self.product.pk, "quantity": 2}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def form(self, request):
        return request.body.fields


class ContactNumberTests(OrderSmsTestCase):
    def test_stores_twilio_canonical_form_not_the_input(self):
        data = self.register("(825) 555-0123")
        self.assertEqual(data["phoneNumber"], SHOPPER_NUMBER)
        self.assertIn("contactNumberId", data)
        lookup = self.twilio.requests[-1]
        self.assertEqual(urlsplit(lookup.url).netloc, "lookups.twilio.com")

    def test_unusable_number_is_rejected_at_registration(self):
        self.as_user(self.shopper)
        response = self.post("/api/contact-numbers", {"phoneNumber": "12345"})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(ContactNumber.objects.exists())

    def test_twilio_refusing_our_credentials_is_not_the_callers_fault(self):
        self.twilio.fail_next("GET", r"/v1/PhoneNumbers/", json_response(401, {"code": 20003}))
        self.as_user(self.shopper)
        response = self.post("/api/contact-numbers", {"phoneNumber": SHOPPER_NUMBER})
        self.assertEqual(response.status_code, 502)

    def test_numbers_are_private_to_their_owner(self):
        contact_id = self.register()["contactNumberId"]
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/contact-numbers").json()["contactNumbers"], [])
        self.assertEqual(self.client.delete(f"/api/contact-numbers/{contact_id}").status_code, 404)
        self.assertTrue(ContactNumber.objects.filter(pk=contact_id).exists())

    def test_unauthenticated_is_401(self):
        self.assertEqual(self.client.get("/api/contact-numbers").status_code, 401)

    def test_deleted_number_is_gone_and_never_messaged(self):
        contact_id = self.register()["contactNumberId"]
        self.assertEqual(self.client.delete(f"/api/contact-numbers/{contact_id}").status_code, 204)
        self.assertEqual(self.client.get("/api/contact-numbers").json()["contactNumbers"], [])
        order = self.place()
        self.assertFalse(order["notified"])
        self.assertEqual(self.twilio.sent(), [])


class OrderFlowTests(OrderSmsTestCase):
    def test_order_placed_uses_oscar_models_and_texts_the_shopper(self):
        self.register()
        order = self.place()
        self.assertIn("orderId", order)
        self.assertEqual(order["lines"][0]["quantity"], 2)
        (create,) = self.twilio.sent()
        fields = self.form(create)
        self.assertEqual(fields["To"], SHOPPER_NUMBER)
        self.assertEqual(fields["From"], FROM)
        self.assertIn("placed", fields["Body"])
        self.assertNotIn("ScheduleType", fields)
        self.assertEqual(order["notifications"][0]["outcome"], "pending")

    def test_no_number_on_file_means_no_message(self):
        order = self.place()
        self.assertFalse(order["notified"])
        self.assertEqual(self.twilio.sent(), [])

    def test_unsent_is_a_known_failure_and_the_order_still_succeeds(self):
        self.register()
        self.twilio.fail_next("POST", r"/Messages\.json$", httpx.ConnectError("refused"))
        order = self.place()
        (n,) = order["notifications"]
        self.assertEqual((n["status"], n["outcome"]), ("rejected", "failed"))
        self.assertEqual(self.twilio.sent("GET"), [])  # nothing to look up: it never left

    def test_timeout_that_did_not_land_is_unknown_not_failed(self):
        self.register()
        self.twilio.fail_next("POST", r"/Messages\.json$", httpx.ReadTimeout("no reply"))
        order = self.place()
        (n,) = order["notifications"]
        self.assertEqual((n["status"], n["outcome"]), ("unknown", "unknown"))
        self.assertEqual(len(self.twilio.sent("GET")), 1)  # looked up by reference

    def test_timeout_that_landed_is_found_by_reference(self):
        self.register()
        self.place()  # a first message, which must not be mistaken for the second
        order = Notification.objects.get().order
        notification = Notification(order=order, user=self.shopper, contact=ContactNumber.objects.get(),
                                    kind=Notification.ORDER_CANCELLED)
        notification.body = f"cancelled {notification.ref_tag}"
        notification.save()
        landed = self.twilio.add_message(to=SHOPPER_NUMBER, body=notification.body, status="sent")
        self.twilio.fail_next("POST", r"/Messages\.json$", httpx.ReadTimeout("no reply"))
        services._deliver(notification)
        notification.refresh_from_db()
        self.assertEqual((notification.provider_sid, notification.status), (landed["sid"], "sent"))

    def test_truncated_success_body_is_unknown(self):
        self.register()
        self.twilio.fail_next("POST", r"/Messages\.json$", json_response(201, {}))
        (n,) = self.place()["notifications"]
        self.assertEqual(n["outcome"], "unknown")

    def test_dispatch_texts_and_queues_the_followup_at_twilio(self):
        self.register()
        order_id = self.place()["orderId"]
        self.as_user(self.operator)
        response = self.post(f"/api/orders/{order_id}/dispatch")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "Dispatched")
        _, dispatched, followup = self.twilio.sent()
        self.assertNotIn("ScheduleType", self.form(dispatched))
        fields = self.form(followup)
        self.assertEqual(fields["ScheduleType"], "fixed")
        self.assertEqual(fields["MessagingServiceSid"], "MG_test")
        self.assertEqual(fields["From"], FROM)
        send_at = datetime.fromisoformat(fields["SendAt"].replace("Z", "+00:00"))
        self.assertGreater(send_at, datetime.now(dt_timezone.utc) + timedelta(days=2, hours=23))
        kinds = {n["kind"]: n["outcome"] for n in response.json()["notifications"]}
        self.assertEqual(kinds[Notification.DELIVERY_FOLLOWUP], "scheduled")

    def test_repeated_dispatch_sends_nothing_more(self):
        self.register()
        order_id = self.place()["orderId"]
        self.as_user(self.operator)
        self.post(f"/api/orders/{order_id}/dispatch")
        response = self.post(f"/api/orders/{order_id}/dispatch")
        self.assertTrue(response.json()["alreadyApplied"])
        self.assertEqual(len(self.twilio.sent()), 3)

    def test_shoppers_cannot_dispatch(self):
        self.register()
        order_id = self.place()["orderId"]
        self.assertEqual(self.post(f"/api/orders/{order_id}/dispatch").status_code, 403)

    def test_cancel_calls_off_the_followup_before_it_goes_out(self):
        self.register()
        order_id = self.place()["orderId"]
        self.as_user(self.operator)
        self.post(f"/api/orders/{order_id}/dispatch")
        followup = Notification.objects.get(kind=Notification.DELIVERY_FOLLOWUP)
        response = self.post(f"/api/orders/{order_id}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "Cancelled")
        (cancel,) = self.twilio.sent("POST", f"/Messages/{followup.provider_sid}.json")
        self.assertEqual(self.form(cancel), {"Status": "canceled"})
        followup.refresh_from_db()
        self.assertEqual(followup.status, "canceled")
        self.assertEqual(self.twilio.messages[followup.provider_sid]["status"], "canceled")
        self.assertEqual(
            Notification.objects.filter(kind=Notification.ORDER_CANCELLED).count(), 1
        )

    def test_a_failed_cancel_is_retried_on_the_next_read(self):
        self.register()
        order_id = self.place()["orderId"]
        self.as_user(self.operator)
        self.post(f"/api/orders/{order_id}/dispatch")
        followup = Notification.objects.get(kind=Notification.DELIVERY_FOLLOWUP)
        self.twilio.fail_next("POST", r"/Messages/SM\w+\.json$", httpx.ReadTimeout("no reply"))
        self.post(f"/api/orders/{order_id}/cancel")
        followup.refresh_from_db()
        self.assertEqual(followup.status, "scheduled")
        self.assertIsNotNone(followup.cancel_requested_at)
        self.as_user(self.shopper)
        notifications = self.client.get(f"/api/orders/{order_id}/notifications").json()["notifications"]
        by_kind = {n["kind"]: n for n in notifications}
        self.assertEqual(by_kind[Notification.DELIVERY_FOLLOWUP]["status"], "canceled")

    def test_followup_claimed_before_cancel_is_cancelled_when_it_settles(self):
        self.register()
        order_id = self.place()["orderId"]
        order = Notification.objects.get().order
        followup = Notification(order=order, user=self.shopper, contact=ContactNumber.objects.get(),
                                kind=Notification.DELIVERY_FOLLOWUP,
                                send_at=datetime.now(dt_timezone.utc) + timedelta(days=3))
        followup.body = "follow-up"
        followup.save()
        services.request_followup_cancel(Notification.objects.filter(order=order))  # cancel lands mid-send
        services._deliver(followup)
        followup.refresh_from_db()
        self.assertEqual(followup.status, "canceled")
        self.assertEqual(order_id, order.pk)

    def test_deleting_the_number_calls_off_its_followup(self):
        contact_id = self.register()["contactNumberId"]
        order_id = self.place()["orderId"]
        self.as_user(self.operator)
        self.post(f"/api/orders/{order_id}/dispatch")
        followup = Notification.objects.get(kind=Notification.DELIVERY_FOLLOWUP)
        self.as_user(self.shopper)
        self.client.delete(f"/api/contact-numbers/{contact_id}")
        self.assertEqual(self.twilio.messages[followup.provider_sid]["status"], "canceled")

    def test_orders_are_private_to_their_owner(self):
        self.register()
        order_id = self.place()["orderId"]
        self.as_user(self.other)
        self.assertEqual(self.client.get(f"/api/orders/{order_id}/notifications").status_code, 404)
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"], [])

    def test_my_orders_refreshes_delivery_state_from_twilio(self):
        self.register()
        self.place()
        sid = Notification.objects.get().provider_sid
        self.twilio.messages[sid]["status"] = "delivered"
        (order,) = self.client.get("/api/my-orders").json()["orders"]
        self.assertEqual(order["notifications"][0]["outcome"], "pending")  # checked moments ago
        Notification.objects.update(last_synced_at=datetime.now(dt_timezone.utc) - timedelta(minutes=1))
        (order,) = self.client.get("/api/my-orders").json()["orders"]
        self.assertEqual(order["notifications"][0]["outcome"], "delivered")

    def test_unknown_twilio_status_is_neither_delivered_nor_failed(self):
        self.assertEqual(gateway.outcome_for("something_new"), "unknown")
        self.assertEqual(gateway.outcome_for("undelivered"), "failed")
        self.assertEqual(gateway.outcome_for("sent"), "pending")


class OperatorTests(OrderSmsTestCase):
    def failed_notification(self):
        self.register()
        self.place()
        notification = Notification.objects.get()
        self.twilio.messages[notification.provider_sid].update(status="undelivered", error_code=30003)
        return notification

    def test_resend_is_idempotent_per_key(self):
        source = self.failed_notification()
        self.as_user(self.operator)
        url = f"/api/notifications/{source.pk}/resend"
        first = self.post(url, {"idempotencyKey": "k-1"})
        self.assertEqual(first.status_code, 201, first.content)
        again = self.post(url, {"idempotencyKey": "k-1"})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["notificationId"], first.json()["notificationId"])
        self.assertEqual(len(self.twilio.sent()), 2)  # original + one resend
        fresh = self.post(url, {}, **{"Idempotency-Key": "k-2"})
        self.assertEqual(fresh.status_code, 201)
        self.assertNotEqual(fresh.json()["notificationId"], first.json()["notificationId"])
        self.assertEqual(len(self.twilio.sent()), 3)

    def test_resend_needs_a_key_and_a_failed_message(self):
        self.register()
        self.place()
        delivered = Notification.objects.get()
        self.twilio.messages[delivered.provider_sid]["status"] = "delivered"
        self.as_user(self.operator)
        url = f"/api/notifications/{delivered.pk}/resend"
        self.assertEqual(self.post(url, {}).status_code, 400)
        self.assertEqual(self.post(url, {"idempotencyKey": "k"}).status_code, 409)

    def test_dispose_content_blanks_it_at_twilio_and_keeps_the_record(self):
        source = self.failed_notification()
        self.as_user(self.operator)
        response = self.client.delete(f"/api/notifications/{source.pk}/content")
        self.assertEqual(response.status_code, 200, response.content)
        (redact,) = self.twilio.sent("POST", f"/Messages/{source.provider_sid}.json")
        self.assertEqual(self.form(redact), {"Body": ""})
        data = response.json()
        self.assertEqual((data["body"], data["contentDisposed"], data["status"]), (None, True, "undelivered"))
        source.refresh_from_db()
        self.assertEqual(source.body, "")

    def test_scheduled_message_content_cannot_be_disposed_yet(self):
        self.register()
        order_id = self.place()["orderId"]
        self.as_user(self.operator)
        self.post(f"/api/orders/{order_id}/dispatch")
        followup = Notification.objects.get(kind=Notification.DELIVERY_FOLLOWUP)
        self.assertEqual(self.client.delete(f"/api/notifications/{followup.pk}/content").status_code, 409)

    def test_reconciliation_asks_for_our_number_and_classifies(self):
        self.register()
        self.place()
        ours = Notification.objects.get()
        stranger = self.twilio.add_message(body="not ours")
        inbound = self.twilio.add_message(direction="inbound")
        gone = Notification.objects.create(
            order=ours.order, user=self.shopper, kind=Notification.ORDER_CANCELLED,
            provider_sid="SM" + "9" * 32, status="delivered",
            provider_date_created=datetime.now(dt_timezone.utc),
        )
        self.as_user(self.operator)
        start = (datetime.now(dt_timezone.utc) - timedelta(hours=1)).isoformat()
        end = (datetime.now(dt_timezone.utc) + timedelta(hours=1)).isoformat()
        response = self.client.get("/api/notifications/reconciliation", {"from": start, "to": end})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        (listing,) = [r for r in self.twilio.sent("GET") if "From=" in r.url]
        query = parse_qs(urlsplit(listing.url).query)
        self.assertEqual(query["From"], [FROM])
        self.assertIn("DateSent>", query)
        self.assertIn("DateSent<", query)
        self.assertEqual([m["notificationId"] for m in report["matched"]], [ours.pk])
        self.assertEqual([m["sid"] for m in report["providerOnly"]], [stranger["sid"]])
        self.assertEqual([m["notificationId"] for m in report["appOnly"]], [gone.pk])
        self.assertEqual(report["summary"]["inboundExcluded"], 1)
        self.assertNotIn(inbound["sid"], json.dumps(report))
        self.assertTrue(report["complete"])

    def test_operator_endpoints_are_staff_only(self):
        source = self.failed_notification()
        self.as_user(self.shopper)
        self.assertEqual(self.client.delete(f"/api/notifications/{source.pk}/content").status_code, 403)
        self.assertEqual(
            self.client.get("/api/notifications/reconciliation", {"from": "2026-01-01T00:00:00Z",
                                                                  "to": "2026-01-02T00:00:00Z"}).status_code,
            403,
        )


class ConfigurationTests(TestCase):
    @override_settings(TWILIO_ACCOUNT_SID="", TWILIO_AUTH_TOKEN="", TWILIO_FROM_NUMBER="")
    def test_missing_credentials_refuse_to_build_a_client(self):
        with self.assertRaises(gateway.TwilioNotConfigured):
            gateway.build_client()

    @override_settings(**{**TWILIO_SETTINGS, "TWILIO_BASE_URL": "http://mock.local:9999"})
    def test_base_url_governs_messaging_calls(self):
        fake = FakeTwilio()
        gateway.set_client(gateway.build_client(http_client=fake))
        try:
            gateway.send_message(SHOPPER_NUMBER, "hi", idempotency_key="k")
        finally:
            gateway.set_client(None)
        request = fake.requests[-1]
        self.assertTrue(request.url.startswith("http://mock.local:9999/2010-04-01/Accounts/AC_test/"))
        self.assertEqual(request.headers["idempotency-key"], "k")
        self.assertTrue(request.headers["authorization"].startswith("Basic "))

    def test_mask_hides_numbers(self):
        self.assertEqual(gateway.mask("/v1/PhoneNumbers/%2B18255550123"), "/v1/PhoneNumbers/***")
