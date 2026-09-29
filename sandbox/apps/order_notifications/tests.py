"""Tests for order SMS notifications.

The Twilio SDK is exercised for real; only its transport is replaced (the
SDK's own test seam), by a fake that answers like the provider and records
every request. Phone numbers here are fictional and never leave the process.

Run: python sandbox/manage.py test apps.order_notifications
"""

import json
import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase, override_settings

from oscar.test.factories import create_product

from twilio_sdk.core import FormBody, HttpRequest, HttpResponse
from twilio_sdk.models.enums import MessageEnumStatus

from . import provider
from .models import ContactNumber, Notification, Outcome, ProviderWrite
from .outcomes import GONE, answer_status, cancel_outcome, redact_outcome, status_from_provider

SHOPPER_NUMBER = "+16135550142"  # fictional (555-01xx)
OTHER_NUMBER = "+16135550199"
FROM_NUMBER = "+16135550100"
ACCOUNT = "ACtest"

TWILIO_SETTINGS = dict(
    TWILIO_ACCOUNT_SID=ACCOUNT,
    TWILIO_AUTH_TOKEN="test-token",
    TWILIO_FROM_NUMBER=FROM_NUMBER,
    TWILIO_MESSAGING_SERVICE_SID="MGtest",
    TWILIO_BASE_URL=None,
    TWILIO_LOOKUPS_BASE_URL=None,
    ORDER_SMS_REFERENCE_PREFIX="test-install",
)


# ---------------------------------------------------------------------------
# A fake provider behind the SDK's transport protocol
# ---------------------------------------------------------------------------


@dataclass
class StubResponse:
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    chunks: list[bytes] = field(default_factory=list)
    url: str = "https://api.test/"
    closed: bool = False

    def iter_bytes(self, chunk_size: int | None) -> Iterator[bytes]:
        yield from self.chunks
        self.close()

    def read(self) -> bytes:
        self.close()
        return b"".join(self.chunks)

    def close(self) -> None:
        self.closed = True


def json_response(status: int, body: object) -> StubResponse:
    return StubResponse(status_code=status, headers={"content-type": "application/json"},
                        chunks=[json.dumps(body).encode()])


def twilio_error(status: int, code: int, message: str) -> StubResponse:
    return json_response(status, {"code": code, "message": message, "status": status})


@dataclass
class Injection:
    method: str
    path: str  # regex matched against the URL path
    action: Any  # a StubResponse to return, or an exception to raise
    land_first: bool = False  # process the request normally, then raise (the write landed)


class FakeTwilio:
    """Answers lookups and the Messages API like the provider, keeping state."""

    def __init__(self) -> None:
        self.requests: list[HttpRequest] = []
        self.messages: dict[str, dict[str, Any]] = {}
        self.injections: list[Injection] = []
        self.valid_numbers: dict[str, str] = {SHOPPER_NUMBER: SHOPPER_NUMBER, OTHER_NUMBER: OTHER_NUMBER,
                                              "613-555-0142": SHOPPER_NUMBER}
        self.create_status = "queued"
        self.redaction_keeps_text = False
        self._seq = 0

    # -- protocol
    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = urlsplit(request.url).path
        for inj in list(self.injections):
            if inj.method == request.method and re.search(inj.path, path):
                self.injections.remove(inj)
                if isinstance(inj.action, StubResponse):
                    return inj.action
                if inj.land_first:
                    self._route(request)
                raise inj.action
        return self._route(request)

    def close(self) -> None:
        pass

    # -- helpers for tests
    def inject(self, method: str, path: str, action: Any, *, land_first: bool = False) -> None:
        self.injections.append(Injection(method, path, action, land_first))

    def calls(self, method: str, path: str) -> list[HttpRequest]:
        return [r for r in self.requests if r.method == method and re.search(path, urlsplit(r.url).path)]

    def creates(self) -> list[HttpRequest]:
        return self.calls("POST", r"/Messages\.json$")

    @staticmethod
    def form(request: HttpRequest) -> Mapping[str, Any]:
        assert isinstance(request.body, FormBody)
        return request.body.fields

    # -- routing
    def _route(self, request: HttpRequest) -> StubResponse:
        parts = urlsplit(request.url)
        path, query = parts.path, parse_qs(parts.query)
        m = re.fullmatch(r"/v2/PhoneNumbers/(.+)", path)
        if m and request.method == "GET":
            from urllib.parse import unquote
            raw = unquote(m.group(1))
            canonical = self.valid_numbers.get(raw)
            return json_response(200, {
                "valid": canonical is not None,
                "phone_number": canonical or raw,
                "country_code": "CA" if canonical else None,
                "national_format": "(613) 555-0142" if canonical else None,
                "validation_errors": [] if canonical else ["TOO_SHORT"],
            })
        if path == "/2010-04-01/Accounts/%s/Messages.json" % ACCOUNT:
            if request.method == "POST":
                return self._create(self.form(request))
            return self._list(query)
        m = re.fullmatch(r"/2010-04-01/Accounts/%s/Messages/(\w+)\.json" % ACCOUNT, path)
        if m:
            message = self.messages.get(m.group(1))
            if message is None:
                return twilio_error(404, 20404, "The requested resource was not found")
            if request.method == "GET":
                return json_response(200, message)
            fields = self.form(request)
            if fields.get("Status") == "canceled":
                if message["status"] not in ("scheduled", "accepted"):
                    return twilio_error(400, 30409, "Message cannot be canceled in its current state")
                message["status"] = "canceled"
            if "Body" in fields:
                if not self.redaction_keeps_text:
                    message["body"] = fields["Body"]
            return json_response(200, message)
        return twilio_error(404, 20404, "unknown route")

    def _create(self, fields: Mapping[str, Any]) -> StubResponse:
        self._seq += 1
        sid = "SM%032d" % self._seq
        now = datetime.now(timezone.utc)
        scheduled = fields.get("ScheduleType") == "fixed"
        message = {
            "sid": sid,
            "account_sid": ACCOUNT,
            "to": fields["To"],
            "from": fields.get("From"),
            "body": fields.get("Body"),
            "status": "scheduled" if scheduled else self.create_status,
            "messaging_service_sid": fields.get("MessagingServiceSid"),
            "date_created": format_datetime(now),
            "date_sent": None if scheduled else format_datetime(now),
            "error_code": None,
            "error_message": None,
        }
        self.messages[sid] = message
        return json_response(201, message)

    def _list(self, query: dict[str, list[str]]) -> StubResponse:
        found = list(self.messages.values())
        if "To" in query:
            found = [m for m in found if m["to"] == query["To"][0]]
        if "From" in query:
            found = [m for m in found if m["from"] == query["From"][0]]
        return json_response(200, {"messages": found, "next_page_uri": None, "page": 0, "page_size": 50})

    def deliver(self, sid: str, status: str = "delivered") -> None:
        self.messages[sid]["status"] = status


# ---------------------------------------------------------------------------
# Pure mappers
# ---------------------------------------------------------------------------


class OutcomeMappingTests(TransactionTestCase):
    def test_every_status_member_is_mapped_deliberately(self) -> None:
        expected = {
            "delivered": "done", "read": "done",
            "queued": "pending", "sending": "pending", "sent": "pending",
            "accepted": "pending", "scheduled": "pending",
            "failed": "failed", "undelivered": "failed", "canceled": "failed",
            "partially_delivered": "failed",
            "receiving": "unknown", "received": "unknown",
        }
        self.assertEqual({m.value for m in MessageEnumStatus}, set(expected))
        for member in MessageEnumStatus:
            self.assertEqual(status_from_provider(member), expected[member.value], member)

    def test_an_unlisted_or_absent_status_is_unknown_not_done(self) -> None:
        from twilio_sdk.core import UNSET
        self.assertEqual(status_from_provider("something_new"), Outcome.UNKNOWN)
        self.assertEqual(status_from_provider(UNSET), Outcome.UNKNOWN)

    def test_call_off_done_only_when_canceled_or_gone(self) -> None:
        self.assertEqual(cancel_outcome(MessageEnumStatus.CANCELED), Outcome.DONE)
        self.assertEqual(cancel_outcome(GONE), Outcome.DONE)
        self.assertEqual(cancel_outcome(MessageEnumStatus.SCHEDULED), Outcome.PENDING)
        self.assertEqual(cancel_outcome(MessageEnumStatus.SENT), Outcome.FAILED)
        self.assertEqual(cancel_outcome("brand_new"), Outcome.UNKNOWN)

    def test_redaction_done_only_when_text_is_gone(self) -> None:
        from twilio_sdk.core import UNSET
        self.assertEqual(redact_outcome(""), Outcome.DONE)
        self.assertEqual(redact_outcome(None), Outcome.DONE)
        self.assertEqual(redact_outcome("still here"), Outcome.FAILED)
        self.assertEqual(redact_outcome(UNSET), Outcome.UNKNOWN)

    def test_a_not_done_outcome_never_answers_success(self) -> None:
        self.assertEqual(answer_status(Outcome.DONE), 200)
        for outcome in ("pending", "sending", "failed", "needs_review", "unknown", "???"):
            self.assertNotIn(answer_status(outcome), (200, 201, 204), outcome)


# ---------------------------------------------------------------------------
# API flows
# ---------------------------------------------------------------------------


@override_settings(**TWILIO_SETTINGS)
class ApiTestCase(TransactionTestCase):
    def setUp(self) -> None:
        self.fake = FakeTwilio()
        provider.set_client(provider.build_client(self.fake))
        User = get_user_model()
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "pw-shopper-123")
        self.other = User.objects.create_user("other", "other@example.com", "pw-other-123")
        self.staff = User.objects.create_user("operator", "op@example.com", "pw-staff-123", is_staff=True)
        self.product = create_product(price=10, num_in_stock=100)

    def tearDown(self) -> None:
        provider.set_client(None)

    def as_user(self, user: Any) -> None:
        self.client.force_login(user)

    def post(self, url: str, data: Any = None, **headers: Any) -> Any:
        return self.client.post(url, data=json.dumps(data or {}), content_type="application/json", **headers)

    def register(self, number: str = SHOPPER_NUMBER) -> int:
        self.as_user(self.shopper)
        response = self.post("/api/contact-numbers", {"phoneNumber": number})
        self.assertIn(response.status_code, (200, 201), response.content)
        return int(response.json()["contactNumberId"])

    def place(self) -> dict[str, Any]:
        self.as_user(self.shopper)
        response = self.post("/api/orders", {"items": [{"productId": self.product.pk, "quantity": 2}]})
        self.assertEqual(response.status_code, 201, response.content)
        return dict(response.json())

    def dispatch(self, order_id: int) -> Any:
        self.as_user(self.staff)
        return self.post("/api/orders/%s/dispatch" % order_id)

    def cancel(self, order_id: int) -> Any:
        self.as_user(self.staff)
        return self.post("/api/orders/%s/cancel" % order_id)


class ContactNumberTests(ApiTestCase):
    def test_registers_the_providers_canonical_form(self) -> None:
        self.as_user(self.shopper)
        response = self.post("/api/contact-numbers", {"phoneNumber": "613-555-0142", "countryCode": "CA"})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["phoneNumber"], SHOPPER_NUMBER)
        self.assertIn("contactNumberId", body)
        lookup = self.fake.calls("GET", r"^/v2/PhoneNumbers/")[0]
        self.assertIn("CountryCode=CA", lookup.url)

    def test_rejects_a_number_the_provider_says_is_unusable(self) -> None:
        self.as_user(self.shopper)
        response = self.post("/api/contact-numbers", {"phoneNumber": "+1555"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["validationErrors"], ["TOO_SHORT"])
        self.assertFalse(ContactNumber.objects.exists())

    def test_provider_refusing_our_credentials_is_our_502_not_the_callers_401(self) -> None:
        self.fake.inject("GET", r"^/v2/PhoneNumbers/", twilio_error(401, 20003, "Authenticate"))
        self.as_user(self.shopper)
        response = self.post("/api/contact-numbers", {"phoneNumber": SHOPPER_NUMBER})
        self.assertEqual(response.status_code, 502)

    def test_never_sent_and_may_have_landed_reads_answer_differently(self) -> None:
        self.as_user(self.shopper)
        self.fake.inject("GET", r"^/v2/PhoneNumbers/", httpx.ConnectError("refused"))
        refused = self.post("/api/contact-numbers", {"phoneNumber": SHOPPER_NUMBER})
        self.fake.inject("GET", r"^/v2/PhoneNumbers/", httpx.ReadTimeout("no reply"))
        timed_out = self.post("/api/contact-numbers", {"phoneNumber": SHOPPER_NUMBER})
        self.assertEqual(refused.status_code, 502)
        self.assertEqual(timed_out.status_code, 504)

    def test_one_shopper_cannot_see_or_delete_anothers_number(self) -> None:
        number_id = self.register()
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/contact-numbers").json()["contactNumbers"], [])
        self.assertEqual(self.client.delete("/api/contact-numbers/%s" % number_id).status_code, 404)
        self.assertTrue(ContactNumber.objects.filter(pk=number_id).exists())

    def test_the_number_is_never_logged(self) -> None:
        with self.assertLogs("apps.order_notifications", level="INFO") as logs:
            self.register()
            self.post("/api/contact-numbers", {"phoneNumber": "613-555-0142", "countryCode": "CA"})
            self.place()
        output = "\n".join(logs.output)
        self.assertNotIn("555-0142", output)
        self.assertNotIn("6135550142", output)
        self.assertNotIn("test-token", output)

    def test_unauthenticated_and_non_staff_callers_are_refused(self) -> None:
        self.assertEqual(self.client.get("/api/contact-numbers").status_code, 401)
        self.as_user(self.shopper)
        self.assertEqual(self.post("/api/orders/1/dispatch").status_code, 403)
        self.assertEqual(self.post("/api/orders/1/cancel").status_code, 403)
        self.assertEqual(self.post("/api/notifications/1/resend").status_code, 403)
        self.assertEqual(self.client.delete("/api/notifications/1/content").status_code, 403)
        self.assertEqual(self.client.get("/api/notifications/reconciliation?from=a&to=b").status_code, 403)


class OrderFlowTests(ApiTestCase):
    def test_placing_an_order_uses_oscars_order_and_tells_the_shopper(self) -> None:
        self.register()
        body = self.place()
        from oscar.core.loading import get_model
        order = get_model("order", "Order").objects.get(pk=body["orderId"])
        self.assertEqual(order.user, self.shopper)
        self.assertEqual(order.lines.get().quantity, 2)
        [notification] = body["notifications"]
        self.assertEqual(notification["kind"], "placed")
        self.assertEqual(notification["outcome"], "pending")  # queued is accepted, not delivered
        [create] = self.fake.creates()
        fields = self.fake.form(create)
        self.assertEqual(fields["To"], SHOPPER_NUMBER)
        self.assertEqual(fields["From"], FROM_NUMBER)
        n = Notification.objects.get()
        self.assertIn("Ref %s" % n.reference_token, fields["Body"])

    def test_a_provider_refusal_never_fails_the_order(self) -> None:
        self.register()
        self.fake.inject("POST", r"/Messages\.json$", twilio_error(401, 20003, "Authenticate"))
        body = self.place()
        self.assertEqual(body["notifications"][0]["outcome"], "failed")
        self.assertEqual(body["notifications"][0]["errorCode"], 20003)
        self.assertFalse(ProviderWrite.objects.exists())  # nothing landed: the claim was released

    def test_a_shopper_with_no_number_is_simply_not_messaged(self) -> None:
        body = self.place()
        self.assertEqual(body["notifications"], [])
        self.assertEqual(self.fake.creates(), [])

    def test_a_lost_answer_is_checked_by_reference_at_once(self) -> None:
        self.register()
        self.fake.inject("POST", r"/Messages\.json$", httpx.ReadTimeout("no reply"), land_first=True)
        body = self.place()
        self.assertEqual(body["notifications"][0]["outcome"], "pending")  # found by its reference
        self.assertIsNotNone(body["notifications"][0]["messageSid"])
        self.assertEqual(len(self.fake.creates()), 1)

    def test_unknown_send_is_found_by_its_reference_never_sent_twice(self) -> None:
        self.register()
        self.fake.inject("POST", r"/Messages\.json$", httpx.ReadTimeout("no reply"), land_first=True)
        self.fake.inject("GET", r"/Messages\.json$", httpx.ReadTimeout("lookup failed too"))
        body = self.place()
        self.assertEqual(body["notifications"][0]["outcome"], "unknown")  # never reported as failed
        self.assertEqual(len(self.fake.messages), 1)  # it did land
        # A later read settles it by looking the reference up - not by sending again.
        self.as_user(self.shopper)
        listing = self.client.get("/api/orders/%s/notifications" % body["orderId"]).json()
        self.assertEqual(listing["notifications"][0]["outcome"], "pending")
        self.assertIsNotNone(listing["notifications"][0]["messageSid"])
        self.assertEqual(len(self.fake.creates()), 1)

    def test_refused_connection_is_known_failed_and_read_timeout_is_unknown(self) -> None:
        self.register()
        self.fake.inject("POST", r"/Messages\.json$", httpx.ConnectError("refused"))
        refused = self.place()["notifications"][0]
        self.fake.inject("POST", r"/Messages\.json$", httpx.ReadTimeout("no reply"))
        timed_out = self.place()["notifications"][0]
        self.assertEqual(refused["outcome"], "failed")
        self.assertEqual(timed_out["outcome"], "unknown")

    def test_dispatch_tells_the_shopper_and_queues_the_follow_up_with_the_provider(self) -> None:
        self.register()
        order_id = self.place()["orderId"]
        response = self.dispatch(order_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "Dispatched")
        kinds = {n["kind"]: n for n in response.json()["notifications"]}
        self.assertEqual(kinds["dispatched"]["outcome"], "pending")
        self.assertEqual(kinds["follow_up"]["providerStatus"], "scheduled")
        follow_up = self.fake.form(self.fake.creates()[-1])
        self.assertEqual(follow_up["ScheduleType"], "fixed")
        self.assertEqual(follow_up["MessagingServiceSid"], "MGtest")
        self.assertEqual(follow_up["From"], FROM_NUMBER)
        send_at = datetime.fromisoformat(str(follow_up["SendAt"]).replace("Z", "+00:00"))
        self.assertGreater(send_at, datetime.now(timezone.utc) + timedelta(hours=71))

    def test_the_same_dispatch_twice_makes_no_second_provider_write(self) -> None:
        self.register()
        order_id = self.place()["orderId"]
        self.dispatch(order_id)
        writes_after_first = len(self.fake.creates())
        second = self.dispatch(order_id)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(self.fake.creates()), writes_after_first)
        self.assertEqual(Notification.objects.filter(kind=Notification.FOLLOW_UP).count(), 1)

    def test_cancel_calls_off_the_follow_up_before_telling_the_shopper(self) -> None:
        self.register()
        order_id = self.place()["orderId"]
        self.dispatch(order_id)
        follow_up = Notification.objects.get(kind=Notification.FOLLOW_UP)
        response = self.cancel(order_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.fake.messages[follow_up.message_sid]["status"], "canceled")
        [call_off] = self.fake.calls("POST", r"/Messages/\w+\.json$")
        self.assertEqual(self.fake.form(call_off)["Status"], "canceled")
        kinds = {n["kind"]: n for n in response.json()["notifications"]}
        self.assertEqual(kinds["follow_up"]["callOffOutcome"], "done")
        self.assertEqual(kinds["cancelled"]["outcome"], "pending")
        # The cancellation message went out after the call-off.
        self.assertLess(self.fake.requests.index(call_off), self.fake.requests.index(self.fake.creates()[-1]))

    def test_a_follow_up_that_already_left_is_reported_not_claimed_as_called_off(self) -> None:
        self.register()
        order_id = self.place()["orderId"]
        self.dispatch(order_id)
        follow_up = Notification.objects.get(kind=Notification.FOLLOW_UP)
        self.fake.deliver(follow_up.message_sid, "sent")
        response = self.cancel(order_id)
        kinds = {n["kind"]: n for n in response.json()["notifications"]}
        self.assertEqual(kinds["follow_up"]["callOffOutcome"], "failed")

    def test_removing_a_number_calls_off_its_queued_follow_up_and_erases_it(self) -> None:
        number_id = self.register()
        order_id = self.place()["orderId"]
        self.dispatch(order_id)
        follow_up = Notification.objects.get(kind=Notification.FOLLOW_UP)
        self.as_user(self.shopper)
        response = self.client.delete("/api/contact-numbers/%s" % number_id)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["erased"])
        self.assertEqual(self.fake.messages[follow_up.message_sid]["status"], "canceled")
        self.assertEqual(self.client.get("/api/contact-numbers").json()["contactNumbers"], [])
        # Nothing is sent to it again.
        before = len(self.fake.creates())
        self.cancel(order_id)
        self.assertEqual(len(self.fake.creates()), before)

    def test_orders_are_shopper_scoped(self) -> None:
        self.register()
        order_id = self.place()["orderId"]
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/orders/%s/notifications" % order_id).status_code, 404)
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"], [])
        self.as_user(self.shopper)
        mine = self.client.get("/api/my-orders").json()["orders"]
        self.assertEqual([o["orderId"] for o in mine], [order_id])


class OperatorActionTests(ApiTestCase):
    def failed_notification(self) -> Notification:
        self.register()
        order_id = self.place()["orderId"]
        n = Notification.objects.get(order_id=order_id)
        self.fake.deliver(n.message_sid, "undelivered")
        return n

    def resend(self, notification_id: int, key: str) -> Any:
        self.as_user(self.staff)
        return self.post("/api/notifications/%s/resend" % notification_id, HTTP_IDEMPOTENCY_KEY=key)

    def test_resend_under_the_same_key_sends_once_and_a_fresh_key_sends_again(self) -> None:
        n = self.failed_notification()
        first = self.resend(n.pk, "key-1")
        self.assertEqual(first.status_code, 202)  # queued, not yet delivered
        repeat = self.resend(n.pk, "key-1")
        self.assertEqual(repeat.json()["notificationId"], first.json()["notificationId"])
        self.assertEqual(len(self.fake.creates()), 2)  # original + one resend
        second = self.resend(n.pk, "key-2")
        self.assertNotEqual(second.json()["notificationId"], first.json()["notificationId"])
        self.assertEqual(len(self.fake.creates()), 3)

    def test_a_delivered_message_is_not_resent(self) -> None:
        self.register()
        order_id = self.place()["orderId"]
        n = Notification.objects.get(order_id=order_id)
        self.fake.deliver(n.message_sid, "delivered")
        response = self.resend(n.pk, "key-1")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.fake.creates()), 1)

    def test_resend_requires_an_idempotency_key(self) -> None:
        n = self.failed_notification()
        self.as_user(self.staff)
        self.assertEqual(self.post("/api/notifications/%s/resend" % n.pk).status_code, 400)

    def test_disposal_erases_the_text_at_the_provider_and_keeps_the_record(self) -> None:
        self.register()
        order_id = self.place()["orderId"]
        n = Notification.objects.get(order_id=order_id)
        self.fake.deliver(n.message_sid)
        self.as_user(self.staff)
        response = self.client.delete("/api/notifications/%s/content" % n.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.fake.messages[n.message_sid]["body"], "")
        [redact] = self.fake.calls("POST", r"/Messages/\w+\.json$")
        self.assertEqual(self.fake.form(redact)["Body"], "")
        body = response.json()["notification"]
        self.assertIsNone(body["body"])
        self.assertTrue(body["contentDisposed"])
        self.assertEqual(body["messageSid"], n.message_sid)
        self.assertEqual(body["outcome"], "done")  # what became of it survives

    def test_disposal_the_provider_did_not_honour_is_not_reported_done(self) -> None:
        self.register()
        order_id = self.place()["orderId"]
        n = Notification.objects.get(order_id=order_id)
        self.fake.redaction_keeps_text = True
        self.as_user(self.staff)
        response = self.client.delete("/api/notifications/%s/content" % n.pk)
        self.assertEqual(response.status_code, 409)
        n.refresh_from_db()
        self.assertIsNone(n.content_disposed_at)

    def test_reconciliation_asks_for_our_number_and_lines_both_sides_up(self) -> None:
        self.register()
        self.place()
        # A message the provider knows about that this app never sent.
        self.fake._create({"To": OTHER_NUMBER, "From": FROM_NUMBER, "Body": "manual"})
        # Traffic from a different number on the same account.
        self.fake._create({"To": OTHER_NUMBER, "From": "+16135550111", "Body": "not ours"})
        now = datetime.now(timezone.utc)
        self.as_user(self.staff)
        response = self.client.get("/api/notifications/reconciliation", {
            "from": (now - timedelta(hours=1)).isoformat(), "to": (now + timedelta(hours=1)).isoformat()})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        listing = self.fake.calls("GET", r"/Messages\.json$")[-1]
        query = parse_qs(urlsplit(listing.url).query)
        self.assertEqual(query["From"], [FROM_NUMBER])
        self.assertIn("DateSent>", query)
        self.assertIn("DateSent<", query)
        self.assertEqual(report["counts"]["matched"], 1)
        self.assertEqual(report["counts"]["providerOnly"], 1)
        self.assertEqual(report["counts"]["localOnly"], 0)
