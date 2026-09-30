"""
Tests for SMS order notifications.

The real TwilioSdkClient is used throughout; only its transport is replaced
(``gateway.reset_client(transport)``), so request building, auth, decoding and
error mapping are all exercised. No network traffic, no real numbers.
"""
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal as D
from email.utils import format_datetime
from urllib.parse import parse_qs, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from oscar.test.factories import create_product
from twilio_sdk.core import FormBody, HttpRequest

from . import gateway
from .models import ContactNumber, Notification

User = get_user_model()

FROM = "+15555550199"
SHOPPER_NUMBER = "+15555550123"
OTHER_NUMBER = "+15555550124"
TWILIO_SETTINGS = dict(
    TWILIO_ACCOUNT_SID="ACtest",
    TWILIO_AUTH_TOKEN="test-token",
    TWILIO_FROM_NUMBER=FROM,
    TWILIO_MESSAGING_SERVICE_SID="MGtest",
    TWILIO_BASE_URL="",
    SMS_FOLLOWUP_DELAY_HOURS=72,
)


# ---------------------------------------------------------------------------
# Stub transport
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


def json_response(status, body):
    return StubResponse(status_code=status, headers={"content-type": "application/json"},
                        chunks=[json.dumps(body).encode()])


class StubTwilio:
    """
    A transport that behaves like a tiny Twilio: it keeps the messages it
    "created", answers fetch/list/update from them, and lets a test inject a
    failure for the next call matching a predicate.
    """

    def __init__(self):
        self.requests: list[HttpRequest] = []
        self.messages: dict[str, dict] = {}
        self.lookup_answers: dict[str, dict | int] = {}
        self.failures: list[tuple[Callable[[HttpRequest], bool], object]] = []
        self.list_pages: list[dict] | None = None
        self._seq = 0

    # -- helpers for tests ---------------------------------------------
    def fail_next(self, predicate, outcome):
        self.failures.append((predicate, outcome))

    def calls(self, method, path_part):
        return [r for r in self.requests if r.method == method and path_part in urlsplit(r.url).path]

    def creates(self):
        return [r for r in self.calls("POST", "/Messages.json")]

    # -- HttpClient protocol -------------------------------------------
    def send(self, request: HttpRequest):
        self.requests.append(request)
        for i, (predicate, outcome) in enumerate(self.failures):
            if predicate(request):
                del self.failures[i]
                if isinstance(outcome, BaseException):
                    raise outcome
                if outcome == "create-then-timeout":
                    self._route(request)
                    raise httpx.ReadTimeout("no reply")
                return outcome
        return self._route(request)

    def close(self) -> None:
        pass

    # -- fake provider -------------------------------------------------
    def _route(self, request):
        parts = urlsplit(request.url)
        path = parts.path
        if path.startswith("/v2/PhoneNumbers/"):
            number = path.rsplit("/", 1)[1].replace("%2B", "+")
            answer = self.lookup_answers.get(number, {"valid": True, "phone_number": number,
                                                     "country_code": "US", "national_format": number[2:]})
            if isinstance(answer, int):
                return json_response(answer, {"code": 20404, "message": "not found"})
            return json_response(200, answer)
        if path.endswith("/Messages.json") and request.method == "POST":
            fields = request.body.fields
            self._seq += 1
            sid = "SM%032d" % self._seq
            message = {
                "sid": sid, "to": fields["To"], "from": fields.get("From"), "body": fields.get("Body"),
                "status": "scheduled" if "SendAt" in fields else "queued",
                "date_created": format_datetime(timezone.now()), "date_sent": None,
                "error_code": None, "messaging_service_sid": fields.get("MessagingServiceSid"),
            }
            self.messages[sid] = message
            return json_response(201, message)
        if path.endswith("/Messages.json") and request.method == "GET":
            if self.list_pages is not None:
                query = parse_qs(parts.query)
                index = int(query.get("Page", ["0"])[0])
                return json_response(200, self.list_pages[index])
            query = parse_qs(parts.query)
            msgs = [m for m in reversed(list(self.messages.values()))
                    if ("To" not in query or m["to"] == query["To"][0]) and m["from"] == query["From"][0]]
            return json_response(200, {"messages": msgs, "next_page_uri": None, "page": 0, "page_size": 50})
        if "/Messages/" in path:
            sid = path.rsplit("/", 1)[1].removesuffix(".json")
            message = self.messages.get(sid)
            if message is None:
                return json_response(404, {"code": 20404})
            if request.method == "POST":
                fields = request.body.fields
                if fields.get("Status") == "canceled":
                    if message["status"] != "scheduled":
                        return json_response(400, {"code": 30409})
                    message["status"] = "canceled"
                if "Body" in fields:
                    message["body"] = fields["Body"]
            return json_response(200, message)
        return json_response(404, {"code": 20404})


def is_create(request):
    return request.method == "POST" and urlsplit(request.url).path.endswith("/Messages.json")


def is_update(request):
    return request.method == "POST" and "/Messages/" in urlsplit(request.url).path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@override_settings(**TWILIO_SETTINGS)
class ApiTestCase(TestCase):
    def setUp(self):
        self.twilio = StubTwilio()
        gateway.reset_client(self.twilio)
        self.addCleanup(gateway.reset_client)
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "pw-shopper-123")
        self.other = User.objects.create_user("other", "other@example.com", "pw-other-123")
        self.staff = User.objects.create_user("operator", "op@example.com", "pw-staff-123", is_staff=True)
        self.product = create_product(price=D("12.00"), num_in_stock=50)
        self.shopper_client = self._client_for(self.shopper)
        self.other_client = self._client_for(self.other)
        self.staff_client = self._client_for(self.staff)

    def _client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def post(self, client, url, payload=None, **extra):
        return client.post(url, json.dumps(payload or {}), content_type="application/json", **extra)

    def register(self, client=None, number=SHOPPER_NUMBER):
        response = self.post(client or self.shopper_client, "/api/contact-numbers", {"phoneNumber": number})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["contactNumberId"]

    def place_order(self, client=None):
        response = self.post(client or self.shopper_client, "/api/orders",
                             {"lines": [{"productId": self.product.pk, "quantity": 2}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["orderId"]

    def notifications(self, order_id, client=None):
        response = (client or self.shopper_client).get("/api/orders/%s/notifications" % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        return {n["kind"]: n for n in response.json()["notifications"] if n["resendOf"] is None}


# ---------------------------------------------------------------------------
# Flow 1
# ---------------------------------------------------------------------------

class ContactNumberTests(ApiTestCase):
    def test_stores_provider_canonical_form(self):
        self.twilio.lookup_answers["15555550123"] = {"valid": True, "phone_number": SHOPPER_NUMBER,
                                                     "country_code": "US"}
        response = self.post(self.shopper_client, "/api/contact-numbers", {"phoneNumber": "1 (555) 555-0123"})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["phoneNumber"], SHOPPER_NUMBER)
        self.assertIn("contactNumberId", response.json())
        lookup = self.twilio.calls("GET", "/v2/PhoneNumbers/")[0]
        self.assertTrue(lookup.url.startswith("https://lookups.twilio.com/"))
        self.assertEqual(lookup.headers["authorization"][:6], "Basic ")

    def test_invalid_number_rejected_at_registration(self):
        self.twilio.lookup_answers["+15555550999"] = {"valid": False, "phone_number": "+15555550999"}
        response = self.post(self.shopper_client, "/api/contact-numbers", {"phoneNumber": "+15555550999"})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(ContactNumber.objects.exists())

    def test_provider_404_is_an_unusable_number(self):
        self.twilio.lookup_answers["+15555550998"] = 404
        response = self.post(self.shopper_client, "/api/contact-numbers", {"phoneNumber": "+15555550998"})
        self.assertEqual(response.status_code, 422)

    def test_our_credentials_refused_is_502_not_401(self):
        self.twilio.fail_next(lambda r: "/v2/PhoneNumbers/" in r.url,
                              json_response(401, {"code": 20003, "message": "auth"}))
        response = self.post(self.shopper_client, "/api/contact-numbers", {"phoneNumber": SHOPPER_NUMBER})
        self.assertEqual(response.status_code, 502)

    def test_garbage_is_400_without_calling_provider(self):
        response = self.post(self.shopper_client, "/api/contact-numbers", {"phoneNumber": "call me maybe"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.twilio.requests, [])

    def test_duplicate_is_409(self):
        self.register()
        response = self.post(self.shopper_client, "/api/contact-numbers", {"phoneNumber": SHOPPER_NUMBER})
        self.assertEqual(response.status_code, 409)

    def test_shoppers_cannot_see_or_delete_each_others_numbers(self):
        contact_id = self.register()
        self.assertEqual(self.other_client.get("/api/contact-numbers").json()["contactNumbers"], [])
        self.assertEqual(self.other_client.delete("/api/contact-numbers/%s" % contact_id).status_code, 404)
        self.assertEqual(self.shopper_client.delete("/api/contact-numbers/%s" % contact_id).status_code, 204)
        self.assertEqual(self.shopper_client.get("/api/contact-numbers").json()["contactNumbers"], [])

    def test_anonymous_is_401(self):
        self.assertEqual(Client().get("/api/contact-numbers").status_code, 401)

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.shopper)
        response = client.post("/api/contact-numbers", json.dumps({"phoneNumber": SHOPPER_NUMBER}),
                               content_type="application/json")
        self.assertEqual(response.status_code, 403)

    def test_number_never_logged(self):
        with self.assertLogs("apps.sms_notifications", level="DEBUG") as logs:
            self.register()
            self.place_order()
        self.assertNotIn(SHOPPER_NUMBER, "\n".join(logs.output))
        self.assertNotIn(SHOPPER_NUMBER[1:], "\n".join(logs.output))


# ---------------------------------------------------------------------------
# Flow 2
# ---------------------------------------------------------------------------

class PlaceOrderTests(ApiTestCase):
    def test_order_placed_message_sent_to_canonical_number(self):
        self.register()
        order_id = self.place_order()
        create = self.twilio.creates()[0]
        self.assertIsInstance(create.body, FormBody)
        self.assertEqual(create.body.fields["To"], SHOPPER_NUMBER)
        self.assertEqual(create.body.fields["From"], FROM)
        self.assertIn("thanks for your order", create.body.fields["Body"])
        self.assertIn("/2010-04-01/Accounts/ACtest/Messages.json", create.url)
        placed = self.notifications(order_id)["order_placed"]
        self.assertEqual(placed["state"], "submitted")
        self.assertTrue(placed["providerSid"].startswith("SM"))
        self.assertEqual(placed["providerStatus"], "queued")

    def test_reuses_oscar_order_model(self):
        from oscar.apps.order.models import Order
        self.register()
        order = Order.objects.get(pk=self.place_order())
        self.assertEqual(order.user, self.shopper)
        self.assertEqual(order.lines.get().quantity, 2)
        self.assertEqual(order.status, "Pending")

    def test_no_number_on_file_is_simply_not_messaged(self):
        order_id = self.place_order()
        self.assertEqual(self.twilio.requests, [])
        self.assertEqual(self.notifications(order_id)["order_placed"]["deliveryOutcome"], "not_sent")

    def test_provider_rejection_does_not_fail_the_order(self):
        self.register()
        self.twilio.fail_next(is_create, json_response(400, {"code": 21211, "message": "invalid To"}))
        order_id = self.place_order()
        placed = self.notifications(order_id)["order_placed"]
        self.assertEqual(placed["state"], "send_failed")
        self.assertEqual(placed["providerErrorCode"], 21211)

    def test_unknown_product_is_400(self):
        response = self.post(self.shopper_client, "/api/orders", {"lines": [{"productId": 999999, "quantity": 1}]})
        self.assertEqual(response.status_code, 400)

    def test_shoppers_cannot_see_each_others_orders(self):
        order_id = self.place_order()
        self.assertEqual(self.other_client.get("/api/orders/%s/notifications" % order_id).status_code, 404)
        self.assertEqual(self.other_client.get("/api/my-orders").json()["orders"], [])
        self.assertEqual(len(self.shopper_client.get("/api/my-orders").json()["orders"]), 1)


class SendOutcomeTests(ApiTestCase):
    """The unsent / unknown split for a create, and settling an unknown one."""

    def test_refused_connection_is_known_not_sent(self):
        self.register()
        self.twilio.fail_next(is_create, httpx.ConnectError("refused"))
        order_id = self.place_order()
        n = Notification.objects.get(order_id=order_id)
        self.assertEqual(n.state, "send_failed")
        self.assertEqual(self.twilio.calls("GET", "/Messages.json"), [])   # nothing to reconcile

    def test_read_timeout_on_create_is_settled_by_rereading(self):
        self.register()
        self.twilio.fail_next(is_create, "create-then-timeout")
        order_id = self.place_order()   # still succeeds
        n = Notification.objects.get(order_id=order_id)
        self.assertEqual(n.state, "submitted")
        self.assertEqual(n.provider_sid, next(iter(self.twilio.messages)))
        listing = self.twilio.calls("GET", "/Messages.json")[0]
        query = parse_qs(urlsplit(listing.url).query)
        self.assertEqual(query["To"], [SHOPPER_NUMBER])
        self.assertEqual(query["From"], [FROM])
        self.assertEqual(len(self.twilio.creates()), 1)   # never re-sent

    def test_read_timeout_unmatched_stays_unknown_then_settles_later(self):
        self.register()
        self.twilio.fail_next(is_create, httpx.ReadTimeout("no reply"))
        order_id = self.place_order()
        n = Notification.objects.get(order_id=order_id)
        self.assertEqual(n.state, "unknown")
        # The message turns up at the provider later; the next read settles it.
        self.twilio._route(type("R", (), {"url": "https://api.twilio.com/2010-04-01/Accounts/ACtest/Messages.json",
                                          "method": "POST",
                                          "body": FormBody(fields={"To": SHOPPER_NUMBER, "From": FROM,
                                                                   "Body": n.body})})())
        self.assertEqual(self.notifications(order_id)["order_placed"]["state"], "submitted")

    def test_5xx_is_unknown_and_unreadable_2xx_is_unknown(self):
        self.register()
        self.twilio.fail_next(is_create, json_response(503, {"code": 20500}))
        self.twilio.fail_next(lambda r: r.method == "GET", httpx.ConnectError("down"))
        order_id = self.place_order()
        self.assertEqual(Notification.objects.get(order_id=order_id).state, "unknown")

    def test_error_mapping_distinguishes_unsent_from_unknown(self):
        def failure(exc):
            self.twilio.fail_next(lambda r: True, exc)
            try:
                gateway.fetch_message("SMx")
            except gateway.ProviderError as e:
                return e
            self.fail("no error")
        unsent, unknown = failure(httpx.ConnectError("refused")), failure(httpx.ReadTimeout("no reply"))
        self.assertEqual((unsent.status_code, unsent.outcome_unknown), (502, False))
        self.assertEqual((unknown.status_code, unknown.outcome_unknown), (504, True))


class DispatchAndCancelTests(ApiTestCase):
    def dispatch(self, order_id, client=None):
        return self.post(client or self.staff_client, "/api/orders/%s/dispatch" % order_id)

    def cancel(self, order_id):
        return self.post(self.staff_client, "/api/orders/%s/cancel" % order_id)

    def test_dispatch_is_staff_only(self):
        order_id = self.place_order()
        self.assertEqual(self.dispatch(order_id, self.shopper_client).status_code, 403)

    def test_dispatch_sends_and_queues_followup_with_provider(self):
        self.register()
        order_id = self.place_order()
        before = timezone.now()
        response = self.dispatch(order_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "Dispatched")
        scheduled = [r for r in self.twilio.creates() if "SendAt" in r.body.fields]
        self.assertEqual(len(scheduled), 1)
        fields = scheduled[0].body.fields
        self.assertEqual(fields["ScheduleType"], "fixed")
        self.assertEqual(fields["MessagingServiceSid"], "MGtest")
        self.assertEqual(fields["From"], FROM)
        send_at = gateway.parse_provider_time(fields["SendAt"])
        self.assertGreater(send_at, before + timedelta(hours=71))
        n = self.notifications(order_id)
        self.assertEqual(n["dispatched"]["providerStatus"], "queued")
        self.assertEqual(n["delivery_followup"]["providerStatus"], "scheduled")
        self.assertEqual(n["delivery_followup"]["deliveryOutcome"], "scheduled")

    def test_second_dispatch_is_409_and_sends_nothing(self):
        self.register()
        order_id = self.place_order()
        self.dispatch(order_id)
        count = len(self.twilio.creates())
        self.assertEqual(self.dispatch(order_id).status_code, 409)
        self.assertEqual(len(self.twilio.creates()), count)

    def test_cancel_calls_off_the_followup_before_it_goes_out(self):
        self.register()
        order_id = self.place_order()
        self.dispatch(order_id)
        response = self.cancel(order_id)
        self.assertEqual(response.status_code, 200, response.content)
        n = self.notifications(order_id)
        followup = n["delivery_followup"]
        self.assertEqual(followup["providerStatus"], "canceled")
        self.assertEqual(followup["cancelState"], "done")
        self.assertEqual(self.twilio.messages[followup["providerSid"]]["status"], "canceled")
        update = self.twilio.calls("POST", "/Messages/%s" % followup["providerSid"])[0]
        self.assertEqual(update.body.fields, {"Status": "canceled"})
        self.assertEqual(n["cancelled"]["state"], "submitted")
        # Follow-up cancel went out before the cancellation notice.
        order = [r for r in self.twilio.requests if r.method == "POST"]
        self.assertLess(order.index(update), order.index(self.twilio.creates()[-1]))

    def test_cancel_timeout_rechecked_and_retried(self):
        self.register()
        order_id = self.place_order()
        self.dispatch(order_id)
        self.twilio.fail_next(is_update, httpx.ReadTimeout("no reply"))
        self.twilio.fail_next(lambda r: r.method == "GET", httpx.ConnectError("down"))
        self.cancel(order_id)
        followup = Notification.objects.get(order_id=order_id, kind="delivery_followup")
        self.assertEqual(followup.cancel_state, "unknown")
        self.assertEqual(self.twilio.messages[followup.provider_sid]["status"], "scheduled")
        # Any later read of the order retries the cancellation.
        self.assertEqual(self.notifications(order_id)["delivery_followup"]["cancelState"], "done")
        self.assertEqual(self.twilio.messages[followup.provider_sid]["status"], "canceled")

    def test_repeat_cancel_is_idempotent(self):
        self.register()
        order_id = self.place_order()
        self.cancel(order_id)
        creates = len(self.twilio.creates())
        response = self.cancel(order_id)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["alreadyCancelled"])
        self.assertEqual(len(self.twilio.creates()), creates)

    def test_cannot_dispatch_cancelled_order(self):
        order_id = self.place_order()
        self.cancel(order_id)
        self.assertEqual(self.dispatch(order_id).status_code, 409)

    def test_deleting_the_number_calls_off_queued_messages(self):
        contact_id = self.register()
        order_id = self.place_order()
        self.dispatch(order_id)
        self.shopper_client.delete("/api/contact-numbers/%s" % contact_id)
        followup = Notification.objects.get(order_id=order_id, kind="delivery_followup")
        self.assertEqual(self.twilio.messages[followup.provider_sid]["status"], "canceled")


# ---------------------------------------------------------------------------
# Flow 3
# ---------------------------------------------------------------------------

class ResendTests(ApiTestCase):
    def failed_notification(self):
        self.register()
        order_id = self.place_order()
        n = Notification.objects.get(order_id=order_id)
        self.twilio.messages[n.provider_sid]["status"] = "undelivered"
        self.twilio.messages[n.provider_sid]["error_code"] = 30007
        return n

    def resend(self, notification_id, key, client=None):
        return self.post(client or self.staff_client, "/api/notifications/%s/resend" % notification_id,
                         {"idempotencyKey": key})

    def test_same_key_sends_once_fresh_key_sends_again(self):
        n = self.failed_notification()
        first = self.resend(n.pk, "k-1")
        self.assertEqual(first.status_code, 201, first.content)
        resend_id = first.json()["notificationId"]
        self.assertNotEqual(resend_id, n.pk)
        again = self.resend(n.pk, "k-1")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["notificationId"], resend_id)
        self.assertEqual(len(self.twilio.creates()), 2)   # original + one resend
        fresh = self.resend(n.pk, "k-2")
        self.assertEqual(fresh.status_code, 201)
        self.assertEqual(len(self.twilio.creates()), 3)

    def test_resend_is_staff_only(self):
        n = self.failed_notification()
        self.assertEqual(self.resend(n.pk, "k", self.shopper_client).status_code, 403)

    def test_delivered_message_is_not_resent(self):
        self.register()
        n = Notification.objects.get(order_id=self.place_order())
        self.twilio.messages[n.provider_sid]["status"] = "delivered"
        self.assertEqual(self.resend(n.pk, "k").status_code, 409)

    def test_key_reused_for_other_notification_is_409(self):
        n = self.failed_notification()
        self.resend(n.pk, "shared")
        other = Notification.objects.create(order=n.order, kind="cancelled", state="send_failed", body="x")
        self.assertEqual(self.resend(other.pk, "shared").status_code, 409)

    def test_key_required(self):
        n = self.failed_notification()
        self.assertEqual(self.post(self.staff_client, "/api/notifications/%s/resend" % n.pk).status_code, 400)


class DisposalTests(ApiTestCase):
    def sent_notification(self):
        self.register()
        n = Notification.objects.get(order_id=self.place_order())
        self.twilio.messages[n.provider_sid]["status"] = "delivered"
        return n

    def test_content_redacted_at_provider_record_survives(self):
        n = self.sent_notification()
        response = self.staff_client.delete("/api/notifications/%s/content" % n.pk)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertTrue(data["contentDisposed"])
        self.assertIsNone(data["body"])
        self.assertEqual(data["providerSid"], n.provider_sid)
        self.assertEqual(data["providerStatus"], "delivered")
        self.assertEqual(self.twilio.messages[n.provider_sid]["body"], "")
        update = self.twilio.calls("POST", "/Messages/%s" % n.provider_sid)[0]
        self.assertEqual(update.body.fields, {"Body": ""})
        n.refresh_from_db()
        self.assertEqual(n.body, "")

    def test_redact_timeout_rechecked(self):
        n = self.sent_notification()
        self.twilio.fail_next(is_update, "create-then-timeout")   # applied, reply lost
        response = self.staff_client.delete("/api/notifications/%s/content" % n.pk)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["contentDisposed"])

    def test_disposal_is_staff_only(self):
        n = self.sent_notification()
        self.assertEqual(self.shopper_client.delete("/api/notifications/%s/content" % n.pk).status_code, 403)


class ReconciliationTests(ApiTestCase):
    def test_lines_up_provider_and_app_over_all_pages(self):
        self.register()
        n = Notification.objects.get(order_id=self.place_order())
        sent = timezone.now()
        n.provider_date_sent = sent
        n.provider_status = "sent"
        n.save()
        ghost = Notification.objects.create(order=n.order, kind="dispatched", state="submitted",
                                            provider_sid="SMghost", provider_status="delivered",
                                            provider_date_sent=sent, body="x")
        stamp = format_datetime(sent)
        page0 = {"messages": [{"sid": n.provider_sid, "status": "delivered", "date_sent": stamp,
                               "to": SHOPPER_NUMBER, "from": FROM}],
                 "next_page_uri": "/2010-04-01/Accounts/ACtest/Messages.json?From=x&PageSize=1000&Page=1&PageToken=PT1"}
        page1 = {"messages": [{"sid": "SMprovideronly", "status": "delivered", "date_sent": stamp,
                               "to": OTHER_NUMBER, "from": FROM},
                              {"sid": "SMoutofrange", "status": "delivered",
                               "date_sent": format_datetime(sent - timedelta(days=1)),
                               "to": OTHER_NUMBER, "from": FROM}],
                 "next_page_uri": None}
        self.twilio.list_pages = [page0, page1]
        start = (sent - timedelta(hours=1)).isoformat()
        end = (sent + timedelta(hours=1)).isoformat()
        response = self.staff_client.get("/api/notifications/reconciliation", {"from": start, "to": end})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual([m["providerSid"] for m in report["matched"]], [n.provider_sid])
        self.assertFalse(report["matched"][0]["statusAgreed"])
        self.assertEqual([m["providerSid"] for m in report["providerOnly"]], ["SMprovideronly"])
        self.assertEqual([m["notificationId"] for m in report["appOnly"]], [ghost.pk])
        self.assertNotIn(OTHER_NUMBER, response.content.decode())
        lists = self.twilio.calls("GET", "/Messages.json")
        first, second = (parse_qs(urlsplit(r.url).query) for r in lists[-2:])
        self.assertEqual(first["From"], [FROM])            # asked for our number only
        self.assertIn("DateSent>", first)
        self.assertIn("DateSent<", first)
        self.assertEqual(second["PageToken"], ["PT1"])
        n.refresh_from_db()
        self.assertEqual(n.provider_status, "delivered")

    def test_requires_offset_and_staff(self):
        self.assertEqual(self.staff_client.get("/api/notifications/reconciliation",
                                               {"from": "2026-01-01T00:00:00", "to": "2026-01-02T00:00:00Z"}
                                               ).status_code, 400)
        self.assertEqual(self.shopper_client.get("/api/notifications/reconciliation",
                                                 {"from": "2026-01-01T00:00:00Z", "to": "2026-01-02T00:00:00Z"}
                                                 ).status_code, 403)


class ConfigurationTests(ApiTestCase):
    @override_settings(TWILIO_BASE_URL="https://messaging-proxy.test")
    def test_base_url_governs_messaging_only(self):
        gateway.reset_client(self.twilio)
        self.register()
        self.place_order()
        self.assertTrue(self.twilio.creates()[0].url.startswith("https://messaging-proxy.test/2010-04-01/"))
        self.assertTrue(self.twilio.calls("GET", "/v2/PhoneNumbers/")[0].url.startswith(
            "https://lookups.twilio.com/"))

    @override_settings(TWILIO_AUTH_TOKEN="")
    def test_missing_credentials_never_sends_unauthenticated(self):
        gateway.reset_client(self.twilio)
        ContactNumber.objects.create(user=self.shopper, phone_number=SHOPPER_NUMBER)
        order_id = self.place_order()
        self.assertEqual(self.twilio.requests, [])
        self.assertEqual(Notification.objects.get(order_id=order_id).state, "send_failed")
