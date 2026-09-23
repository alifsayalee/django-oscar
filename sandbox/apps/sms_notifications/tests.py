"""
Tests for order SMS notifications.

Twilio is faked at the SDK's transport seam (``custom_http_client``), so the
real SDK builds and decodes every request; nothing touches the network.

Run from ``sandbox/``:  python manage.py test apps.sms_notifications
"""
import datetime as dt
import email.utils
import itertools
import json
from decimal import Decimal
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from oscar.core.loading import get_model
from oscar.test.factories import create_product
from twilio_sdk import TwilioSdkClient
from twilio_sdk.core import HttpRequest, HttpResponse

from . import gateway as gw
from .models import ContactNumber, Notification

Country = get_model("address", "Country")
Order = get_model("order", "Order")

ACCOUNT = "ACtest"
FROM = "+15550000001"
SHOPPER_NUMBER = "+15145550123"  # canonical form the fake lookup returns
OTHER_NUMBER = "+15145550999"
UNREACHABLE = "+15550004321"


def json_response(status, body):
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def rfc2822(value):
    return email.utils.format_datetime(value.astimezone(dt.timezone.utc))


class FakeTwilio:
    """
    A small in-memory Twilio: lookups, message create/fetch/update/list.
    ``fail`` queues an exception or a (status, body) answer for the next
    request of an operation, before the fake handles it.
    """

    def __init__(self):
        self.messages = {}
        self.requests = []
        self.failures = {}
        self.known_numbers = {
            SHOPPER_NUMBER: "CA",
            OTHER_NUMBER: "CA",
            UNREACHABLE: "US",
            "5145550123": SHOPPER_NUMBER,  # national format, resolved via CountryCode
        }
        self._ids = itertools.count(1)
        self.page_size_override = None

    def fail(self, operation, *answers):
        self.failures.setdefault(operation, []).extend(answers)

    def ops(self, operation):
        return [r for r in self.requests if self._operation(r) == operation]

    # -- transport protocol --
    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        operation = self._operation(request)
        queued = self.failures.get(operation)
        if queued:
            answer = queued.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return json_response(*answer)
        return getattr(self, "_" + operation)(request)

    def close(self):
        pass

    # -- routing --
    @staticmethod
    def _operation(request):
        path = urlsplit(request.url).path
        if path.startswith("/v1/PhoneNumbers/"):
            return "lookup"
        if path.endswith("/Messages.json"):
            return "create" if request.method == "POST" else "list"
        if "/Messages/" in path:
            return "update" if request.method == "POST" else "fetch"
        raise AssertionError("unexpected request %s %s" % (request.method, request.url))

    @staticmethod
    def _fields(request):
        return dict(request.body.fields) if request.body is not None else {}

    def _json(self, m):
        return {
            "sid": m["sid"], "status": m["status"], "to": m["to"], "from": m["from"],
            "body": m["body"], "direction": m.get("direction", "outbound-api"),
            "date_created": rfc2822(m["created"]),
            "date_sent": rfc2822(m["sent"]) if m.get("sent") else None,
            "error_code": m.get("error_code"), "account_sid": ACCOUNT,
            "messaging_service_sid": m.get("mss"),
        }

    def _lookup(self, request):
        raw = urlsplit(request.url).path.rsplit("/", 1)[1].replace("%2B", "+")
        known = self.known_numbers.get(raw)
        if known is None:
            return json_response(404, {"code": 20404, "message": "not found", "status": 404})
        number, country = (raw, known) if len(known) == 2 else (known, "CA")
        return json_response(200, {
            "phone_number": number, "country_code": country, "national_format": "x",
            "caller_name": None, "carrier": None, "add_ons": None, "url": "u",
        })

    def add_message(self, *, to, status, sent=None, body="hello", from_=FROM, direction="outbound-api"):
        sid = "SM%032d" % next(self._ids)
        now = timezone.now()
        self.messages[sid] = {
            "sid": sid, "to": to, "from": from_, "body": body, "status": status,
            "created": sent or now, "sent": sent, "direction": direction,
        }
        return sid

    def _create(self, request):
        f = self._fields(request)
        scheduled = f.get("ScheduleType") == "fixed"
        to = f["To"]
        now = timezone.now()
        if scheduled:
            status, sent = "scheduled", None
        elif to == UNREACHABLE:
            status, sent = "undelivered", now
        else:
            status, sent = "delivered", now
        sid = self.add_message(to=to, status=status, sent=sent, body=f.get("Body"), from_=f.get("From"))
        self.messages[sid]["mss"] = f.get("MessagingServiceSid")
        if to == UNREACHABLE:
            self.messages[sid]["error_code"] = 30034
        return json_response(201, self._json(self.messages[sid]))

    def _sid(self, request):
        return urlsplit(request.url).path.rsplit("/", 1)[1][: -len(".json")]

    def _fetch(self, request):
        m = self.messages.get(self._sid(request))
        if m is None:
            return json_response(404, {"code": 20404, "status": 404})
        return json_response(200, self._json(m))

    def _update(self, request):
        m = self.messages.get(self._sid(request))
        if m is None:
            return json_response(404, {"code": 20404, "status": 404})
        f = self._fields(request)
        if f.get("Status") == "canceled":
            if m["status"] != "scheduled":
                return json_response(409, {"code": 30409, "message": "not cancelable", "status": 409})
            m["status"] = "canceled"
        if "Body" in f:
            m["body"] = f["Body"]
        return json_response(200, self._json(m))

    def _list(self, request):
        q = {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}
        items = [m for m in self.messages.values() if m["from"] == q.get("From", m["from"])]
        if "To" in q:
            items = [m for m in items if m["to"] == q["To"]]
        if "DateSent>" in q:
            lo = dt.datetime.fromisoformat(q["DateSent>"]).date()
            hi = dt.datetime.fromisoformat(q["DateSent<"]).date()
            items = [m for m in items if m.get("sent") and lo <= m["sent"].date() <= hi]
        items.sort(key=lambda m: m["created"], reverse=True)
        size = self.page_size_override or int(q.get("PageSize", 50))
        page = int(q.get("Page", 0))
        chunk = items[page * size:(page + 1) * size]
        more = (page + 1) * size < len(items)
        return json_response(200, {
            "messages": [self._json(m) for m in chunk],
            "page": page, "page_size": size,
            "next_page_uri": "/2010-04-01/Accounts/%s/Messages.json?PageSize=%s&Page=%s&PageToken=PT%s"
            % (ACCOUNT, size, page + 1, page + 1) if more else None,
        })


def make_gateway(fake, *, messaging_service_sid="MGtest"):
    client = TwilioSdkClient(
        custom_http_client=fake, account_sid_auth_token={"username": ACCOUNT, "password": "secret"}
    )
    return gw.TwilioGateway(
        client, account_sid=ACCOUNT, from_number=FROM,
        messaging_service_sid=messaging_service_sid, sleep=lambda _s: None,
    )


class ApiTestCase(TestCase):
    def setUp(self):
        self.fake = FakeTwilio()
        gw.set_gateway(make_gateway(self.fake))
        self.addCleanup(gw.set_gateway, None)
        User = get_user_model()
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "pw-shopper-1")
        self.other = User.objects.create_user("other", "other@example.com", "pw-other-1")
        self.staff = User.objects.create_user("op", "op@example.com", "pw-op-1", is_staff=True)
        Country.objects.get_or_create(
            iso_3166_1_a2="GB", defaults={"name": "United Kingdom", "printable_name": "United Kingdom",
                                          "is_shipping_country": True})
        self.product = create_product(price=Decimal("10.00"), num_in_stock=50)
        self.as_shopper = Client()
        self.as_shopper.force_login(self.shopper)
        self.as_other = Client()
        self.as_other.force_login(self.other)
        self.as_staff = Client()
        self.as_staff.force_login(self.staff)

    def post(self, client, url, data=None, **extra):
        with self.captureOnCommitCallbacks(execute=True):
            return client.post(url, json.dumps(data or {}), content_type="application/json", **extra)

    def register(self, client=None, number=SHOPPER_NUMBER):
        response = self.post(client or self.as_shopper, "/api/contact-numbers", {"phoneNumber": number})
        self.assertIn(response.status_code, (200, 201), response.content)
        return response.json()["contactNumberId"]

    def place(self, client=None):
        """Place an order; ``notifications`` is read back after the commit hooks ran."""
        response = self.post(client or self.as_shopper, "/api/orders", {
            "lines": [{"productId": self.product.pk, "quantity": 2}],
            "shippingAddress": {"firstName": "Ada", "lastName": "Lovelace", "line1": "1 Main St",
                                "city": "London", "postcode": "N1 7GU", "country": "GB"},
        })
        self.assertEqual(response.status_code, 201, response.content)
        order = response.json()
        order["notifications"] = self.notifications(order["orderId"], client)
        return order

    def kinds(self, order_id):
        return {n["kind"]: n for n in self.notifications(order_id, self.as_staff)}

    def notifications(self, order_id, client=None):
        response = (client or self.as_shopper).get("/api/orders/%s/notifications" % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["notifications"]


class StatusMappingTests(TestCase):
    def test_every_listed_member_and_an_unlisted_value(self):
        self.assertEqual(gw.outcome_for("delivered"), gw.DELIVERED)
        self.assertEqual(gw.outcome_for("read"), gw.DELIVERED)
        for pending in ("queued", "accepted", "sending", "sent", "scheduled"):
            self.assertEqual(gw.outcome_for(pending), gw.PENDING, pending)
        for failed in ("failed", "undelivered", "partially_delivered"):
            self.assertEqual(gw.outcome_for(failed), gw.FAILED, failed)
        self.assertEqual(gw.outcome_for("canceled"), gw.CANCELED)
        self.assertEqual(gw.outcome_for("received"), gw.UNKNOWN)
        self.assertEqual(gw.outcome_for("something_new"), gw.UNKNOWN)
        self.assertEqual(gw.outcome_for(None), gw.UNKNOWN)


class GatewayErrorTests(TestCase):
    def setUp(self):
        self.fake = FakeTwilio()
        self.gateway = make_gateway(self.fake)

    def test_refused_connection_is_known_and_timeout_is_unknown(self):
        self.fake.fail("create", httpx.ConnectError("refused"))
        with self.assertRaises(gw.ProviderUnavailable) as unsent:
            self.gateway.send(SHOPPER_NUMBER, "hi")
        self.fake.fail("create", httpx.ReadTimeout("no reply"))
        with self.assertRaises(gw.ProviderUnavailable) as unknown:
            self.gateway.send(SHOPPER_NUMBER, "hi")
        self.assertEqual((unsent.exception.status_code, unsent.exception.outcome_unknown), (502, False))
        self.assertEqual((unknown.exception.status_code, unknown.exception.outcome_unknown), (504, True))

    def test_our_credentials_and_quota_are_not_the_callers_fault(self):
        self.fake.fail("create", (401, {"code": 20003}), (429, {"code": 20429}), (500, {}))
        with self.assertRaises(gw.ProviderConfigError) as auth:
            self.gateway.send(SHOPPER_NUMBER, "hi")
        with self.assertRaises(gw.ProviderUnavailable) as quota:
            self.gateway.send(SHOPPER_NUMBER, "hi")
        with self.assertRaises(gw.ProviderFailure) as down:
            self.gateway.send(SHOPPER_NUMBER, "hi")
        self.assertEqual(auth.exception.status_code, 502)
        self.assertEqual(quota.exception.status_code, 503)
        self.assertEqual((down.exception.status_code, down.exception.outcome_unknown), (502, True))

    def test_accepted_message_without_id_is_unreadable(self):
        self.fake.fail("create", (201, {"status": "queued"}))
        with self.assertRaises(gw.ProviderUnreadable):
            self.gateway.send(SHOPPER_NUMBER, "hi")

    def test_request_shape_for_schedule(self):
        send_at = dt.datetime(2030, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)
        self.gateway.schedule(SHOPPER_NUMBER, "later", send_at)
        request = self.fake.requests[-1]
        fields = request.body.fields
        self.assertEqual(request.method, "POST")
        self.assertTrue(request.url.endswith("/2010-04-01/Accounts/%s/Messages.json" % ACCOUNT))
        self.assertEqual(fields["From"], FROM)
        self.assertEqual(fields["MessagingServiceSid"], "MGtest")
        self.assertEqual(fields["ScheduleType"], "fixed")
        self.assertTrue(fields["SendAt"].startswith("2030-01-02T03:04:05"))
        self.assertTrue(request.headers["authorization"].startswith("Basic "))

    def test_cancel_retries_a_just_scheduled_not_found(self):
        sid = self.gateway.schedule(SHOPPER_NUMBER, "later", timezone.now() + dt.timedelta(days=3)).sid
        self.fake.fail("update", (404, {"code": 20404}), (404, {"code": 20404}))
        message = self.gateway.cancel(sid)
        self.assertEqual(message.outcome, gw.CANCELED)
        self.assertEqual(len(self.fake.ops("update")), 3)

    def test_cancel_after_it_went_out_reports_the_real_state(self):
        sid = self.fake.add_message(to=SHOPPER_NUMBER, status="delivered", sent=timezone.now())
        message = self.gateway.cancel(sid)
        self.assertEqual(message.outcome, gw.DELIVERED)

    @override_settings(TWILIO_ACCOUNT_SID=ACCOUNT, TWILIO_AUTH_TOKEN="secret", TWILIO_FROM_NUMBER=FROM,
                       TWILIO_BASE_URL="https://twilio-proxy.example.test")
    def test_base_url_override_applies_to_messaging_only(self):
        with mock.patch.object(gw, "HttpxClient", lambda **kw: self.fake):
            gateway = gw.build_gateway()
        gateway.send(SHOPPER_NUMBER, "hi")
        gateway.lookup(SHOPPER_NUMBER)
        self.assertTrue(self.fake.ops("create")[0].url.startswith("https://twilio-proxy.example.test/2010-04-01/"))
        self.assertTrue(self.fake.ops("lookup")[0].url.startswith("https://lookups.twilio.com/"))

    @override_settings(TWILIO_ACCOUNT_SID="", TWILIO_AUTH_TOKEN="", TWILIO_FROM_NUMBER="")
    def test_missing_credentials_refuse_to_build(self):
        with self.assertRaises(gw.ProviderConfigError) as e:
            gw.build_gateway()
        self.assertIn("TWILIO_AUTH_TOKEN", str(e.exception))


class AccessTests(ApiTestCase):
    def test_anonymous_is_rejected(self):
        self.assertEqual(Client().get("/api/contact-numbers").status_code, 401)
        self.assertEqual(Client().get("/api/my-orders").status_code, 401)

    def test_operator_actions_need_staff(self):
        order = self.place()
        for url in ("/api/orders/%s/dispatch" % order["orderId"], "/api/orders/%s/cancel" % order["orderId"]):
            self.assertEqual(self.post(self.as_shopper, url).status_code, 403)
        self.assertEqual(self.as_shopper.get("/api/notifications/reconciliation?from=2026-01-01T00:00:00Z"
                                             "&to=2026-01-02T00:00:00Z").status_code, 403)

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.shopper)
        response = client.post("/api/contact-numbers", json.dumps({"phoneNumber": SHOPPER_NUMBER}),
                               content_type="application/json")
        self.assertEqual(response.status_code, 403)
        token = client.get("/api/csrf").json()["csrfToken"]
        response = client.post("/api/contact-numbers", json.dumps({"phoneNumber": SHOPPER_NUMBER}),
                               content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 201)

    def test_session_login(self):
        client = Client()
        response = client.post("/api/session", json.dumps({"email": "shopper@example.com",
                                                            "password": "pw-shopper-1"}),
                               content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(client.get("/api/contact-numbers").status_code, 200)


class ContactNumberTests(ApiTestCase):
    def test_register_stores_the_providers_canonical_form(self):
        response = self.post(self.as_shopper, "/api/contact-numbers",
                             {"phoneNumber": "5145550123", "countryCode": "ca"})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["phoneNumber"], SHOPPER_NUMBER)
        self.assertIn("CountryCode=CA", self.fake.ops("lookup")[0].url)
        again = self.post(self.as_shopper, "/api/contact-numbers", {"phoneNumber": SHOPPER_NUMBER})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["contactNumberId"], response.json()["contactNumberId"])

    def test_unusable_number_is_rejected_at_registration(self):
        response = self.post(self.as_shopper, "/api/contact-numbers", {"phoneNumber": "12"})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(ContactNumber.objects.exists())

    def test_lookup_outage_is_not_blamed_on_the_caller(self):
        self.fake.fail("lookup", httpx.ConnectError("refused"), httpx.ConnectError("refused"))
        response = self.post(self.as_shopper, "/api/contact-numbers", {"phoneNumber": SHOPPER_NUMBER})
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()["outcomeUnknown"])

    def test_numbers_are_private_and_deletable_by_owner_only(self):
        mine = self.register()
        self.assertEqual(self.as_other.get("/api/contact-numbers").json()["contactNumbers"], [])
        self.assertEqual(self.as_other.delete("/api/contact-numbers/%s" % mine).status_code, 404)
        self.assertEqual(self.as_shopper.delete("/api/contact-numbers/%s" % mine).status_code, 204)
        self.assertEqual(self.as_shopper.get("/api/contact-numbers").json()["contactNumbers"], [])
        order = self.place()
        self.assertEqual(order["notifications"], [])  # nothing sent to a deleted number
        self.assertEqual(self.fake.ops("create"), [])

    def test_number_is_never_logged(self):
        with self.assertLogs("apps.sms_notifications", level="INFO") as logs:
            self.register()
            self.place()
        text = "\n".join(logs.output)
        self.assertNotIn(SHOPPER_NUMBER, text)
        self.assertNotIn(SHOPPER_NUMBER[2:], text)


class OrderFlowTests(ApiTestCase):
    def test_shopper_without_number_is_not_messaged(self):
        order = self.place()
        self.assertEqual(order["notifications"], [])
        self.assertEqual(Order.objects.get(pk=order["orderId"]).lines.first().quantity, 2)

    def test_placed_order_is_announced(self):
        self.register()
        order = self.place()
        self.assertEqual([n["kind"] for n in order["notifications"]], ["placed"])
        self.assertEqual(order["notifications"][0]["status"], "delivered")
        body = self.fake.ops("create")[0].body.fields["Body"]
        self.assertIn(order["number"], body)

    def test_send_failure_never_fails_the_order(self):
        self.register()
        self.fake.fail("create", (500, {"code": 20500}))
        self.fake.fail("list", httpx.ConnectError("refused"), httpx.ConnectError("refused"))
        order = self.place()
        self.assertEqual(order["notifications"][0]["status"], "unknown")
        self.fake.fail("create", (400, {"code": 21211}))
        self.post(self.as_staff, "/api/orders/%s/dispatch" % order["orderId"])
        statuses = {n["kind"]: n["status"] for n in self.notifications(order["orderId"])}
        self.assertEqual(statuses["dispatched"], "failed")
        self.assertEqual(Order.objects.get(pk=order["orderId"]).status, "Dispatched")

    def test_timed_out_send_is_found_by_reference(self):
        self.register()
        # The write landed although we never heard back.
        original_create = self.fake._create

        def landed_then_timeout(request):
            original_create(request)
            raise httpx.ReadTimeout("no reply")

        with mock.patch.object(self.fake, "_create", landed_then_timeout):
            order = self.place()
        n = order["notifications"][0]
        self.assertEqual(n["status"], "delivered")
        self.assertIsNotNone(n["providerMessageSid"])
        self.assertEqual(len(self.fake.messages), 1)  # looked up, not re-sent

    def test_orders_are_private(self):
        order = self.place()
        response = self.as_other.get("/api/orders/%s/notifications" % order["orderId"])
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.as_other.get("/api/my-orders").json()["orders"], [])
        self.assertEqual(len(self.as_shopper.get("/api/my-orders").json()["orders"]), 1)

    def test_dispatch_schedules_a_followup_with_the_provider_once(self):
        self.register()
        order = self.place()
        response = self.post(self.as_staff, "/api/orders/%s/dispatch" % order["orderId"])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["changed"])
        kinds = self.kinds(order["orderId"])
        self.assertEqual(kinds["dispatched"]["status"], "delivered")
        followup = kinds["followup"]
        self.assertEqual((followup["status"], followup["providerStatus"]), ("pending", "scheduled"))
        fields = self.fake.ops("create")[-1].body.fields
        self.assertEqual(fields["ScheduleType"], "fixed")
        send_at = dt.datetime.fromisoformat(fields["SendAt"].replace("Z", "+00:00"))
        self.assertAlmostEqual((send_at - timezone.now()).total_seconds(), 72 * 3600, delta=120)
        creates = len(self.fake.ops("create"))
        repeat = self.post(self.as_staff, "/api/orders/%s/dispatch" % order["orderId"])
        self.assertFalse(repeat.json()["changed"])
        self.assertEqual(len(self.fake.ops("create")), creates)

    def test_cancel_stops_the_followup_and_tells_the_shopper(self):
        self.register()
        order = self.place()
        self.post(self.as_staff, "/api/orders/%s/dispatch" % order["orderId"])
        self.fake.fail("update", (404, {"code": 20404}))  # just-scheduled propagation delay
        response = self.post(self.as_staff, "/api/orders/%s/cancel" % order["orderId"])
        self.assertEqual(response.status_code, 200, response.content)
        kinds = self.kinds(order["orderId"])
        self.assertEqual((kinds["followup"]["status"], kinds["followup"]["cancelState"]), ("canceled", "canceled"))
        self.assertEqual(kinds["cancelled"]["status"], "delivered")
        self.assertEqual(response.json()["status"], "Cancelled")
        followup_sid = kinds["followup"]["providerMessageSid"]
        self.assertEqual(self.fake.messages[followup_sid]["status"], "canceled")

    def test_unconfirmed_cancel_is_retried_on_the_next_read(self):
        self.register()
        order = self.place()
        self.post(self.as_staff, "/api/orders/%s/dispatch" % order["orderId"])
        self.fake.fail("update", *[httpx.ConnectError("refused")] * 4)
        response = self.post(self.as_staff, "/api/orders/%s/cancel" % order["orderId"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Notification.objects.get(kind="followup").cancel_state, "requested")
        followup = self.kinds(order["orderId"])["followup"]
        self.assertEqual((followup["status"], followup["cancelState"]), ("canceled", "canceled"))

    def test_deleting_the_number_cancels_its_queued_followup(self):
        contact = self.register()
        order = self.place()
        self.post(self.as_staff, "/api/orders/%s/dispatch" % order["orderId"])
        self.as_shopper.delete("/api/contact-numbers/%s" % contact)
        followup = Notification.objects.get(kind="followup")
        self.assertEqual(followup.status, "canceled")
        self.assertEqual(self.fake.messages[followup.provider_sid]["status"], "canceled")


class OperatorTests(ApiTestCase):
    def failed_notification(self):
        self.register(number=UNREACHABLE)
        order = self.place()
        n = order["notifications"][0]
        self.assertEqual(n["status"], "failed")
        return order, n

    def test_resend_is_idempotent_per_key(self):
        order, n = self.failed_notification()
        url = "/api/notifications/%s/resend" % n["notificationId"]
        first = self.post(self.as_staff, url, HTTP_IDEMPOTENCY_KEY="key-1")
        self.assertEqual(first.status_code, 201, first.content)
        again = self.post(self.as_staff, url, HTTP_IDEMPOTENCY_KEY="key-1")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["notificationId"], first.json()["notificationId"])
        self.assertTrue(again.json()["replayed"])
        self.assertEqual(len(self.fake.ops("create")), 2)  # original + one resend
        fresh = self.post(self.as_staff, url, {"idempotencyKey": "key-2"})
        self.assertEqual(fresh.status_code, 201)
        self.assertNotEqual(fresh.json()["notificationId"], first.json()["notificationId"])
        self.assertEqual(len(self.fake.ops("create")), 3)
        self.assertEqual(first.json()["resendOf"], n["notificationId"])

    def test_resend_refuses_a_delivered_message_and_needs_a_key(self):
        self.register()
        n = self.place()["notifications"][0]
        url = "/api/notifications/%s/resend" % n["notificationId"]
        self.assertEqual(self.post(self.as_staff, url).status_code, 400)
        self.assertEqual(self.post(self.as_staff, url, HTTP_IDEMPOTENCY_KEY="k").status_code, 409)
        self.assertEqual(len(self.fake.ops("create")), 1)

    def test_content_disposal_erases_it_at_the_provider_and_keeps_the_record(self):
        self.register()
        n = self.place()["notifications"][0]
        response = self.as_staff.delete("/api/notifications/%s/content" % n["notificationId"])
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["contentDisposed"])
        self.assertEqual(body["status"], "delivered")
        self.assertEqual(self.fake.messages[n["providerMessageSid"]]["body"], "")
        self.assertEqual(self.fake.ops("update")[-1].body.fields, {"Body": ""})
        self.assertEqual(self.as_staff.delete("/api/notifications/%s/content" % n["notificationId"]).status_code, 200)
        self.assertEqual(len(self.fake.ops("update")), 1)

    def test_disposing_a_scheduled_followup_cancels_it_first(self):
        self.register()
        order = self.place()
        self.post(self.as_staff, "/api/orders/%s/dispatch" % order["orderId"])
        followup = Notification.objects.get(kind="followup")
        response = self.as_staff.delete("/api/notifications/%s/content" % followup.pk)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.fake.messages[followup.provider_sid]["status"], "canceled")
        self.assertEqual(self.fake.messages[followup.provider_sid]["body"], "")

    def test_reconciliation_lines_up_both_sides(self):
        self.register()
        order = self.place()
        now = timezone.now()
        ours = Notification.objects.get(order_id=order["orderId"])
        # Provider knows it, we do not (sent from our number by something else).
        foreign = self.fake.add_message(to=OTHER_NUMBER, status="delivered", sent=now - dt.timedelta(minutes=5))
        # Same day but outside the requested instants: must be narrowed away.
        self.fake.add_message(to=OTHER_NUMBER, status="delivered", sent=now - dt.timedelta(hours=3))
        # Inbound copy and other senders' traffic are not ours to count.
        self.fake.add_message(to=FROM, status="received", sent=now, direction="inbound")
        self.fake.add_message(to=OTHER_NUMBER, status="delivered", sent=now, from_="+15550009999")
        # We believe we sent one the provider does not have.
        lost = Notification.objects.create(
            order=ours.order, contact_number=ours.contact_number, kind="dispatched",
            status="delivered", provider_status="delivered", provider_sid="SMlost",
            provider_date_sent=now - dt.timedelta(minutes=1),
        )
        self.fake.page_size_override = 1  # force paging
        start = (now - dt.timedelta(hours=1)).isoformat()
        end = (now + dt.timedelta(minutes=5)).isoformat()
        response = self.as_staff.get("/api/notifications/reconciliation", {"from": start, "to": end})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual([m["notificationId"] for m in report["matched"]], [ours.pk])
        self.assertEqual([m["providerMessageSid"] for m in report["providerOnly"]], [foreign])
        self.assertEqual([m["notificationId"] for m in report["localOnly"]], [lost.pk])
        self.assertFalse(report["truncated"])
        self.assertGreater(report["providerPagesRead"], 1)
        for request in self.fake.ops("list"):
            self.assertIn("From=%2B15550000001", request.url)
            self.assertIn("DateSent%3E=", request.url)

    def test_reconciliation_validates_the_range(self):
        response = self.as_staff.get("/api/notifications/reconciliation", {"from": "nope", "to": "2026-01-01"})
        self.assertEqual(response.status_code, 400)
        response = self.as_staff.get("/api/notifications/reconciliation",
                                     {"from": "2026-01-02T00:00:00Z", "to": "2026-01-01T00:00:00Z"})
        self.assertEqual(response.status_code, 422)
