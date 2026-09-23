"""Unit tests for the SMS order-notifications app.

The Twilio SDK is exercised through its real request-building pipeline: we inject
a fake transport (the SDK's documented test seam) rather than mocking the client.
Twilio uses HTTP Basic auth, so there is no token request before an operation —
the first request the stub sees is the operation itself.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal as D

import httpx
from django.contrib.auth import get_user_model
from django.test import Client as DjangoClient, TestCase, override_settings
from django.utils import timezone
from oscar.core.loading import get_model

from twilio_sdk import Client
from twilio_sdk.core import BasicAuthCredentials, HttpRequest, HttpResponse

from . import services, twilio_gateway as gw
from .models import ContactNumber, OrderNotification

Order = get_model("order", "Order")
User = get_user_model()

TWILIO_SETTINGS = dict(
    TWILIO_ACCOUNT_SID="ACtest",
    TWILIO_AUTH_TOKEN="secrettoken",
    TWILIO_FROM_NUMBER="+15550000000",
    TWILIO_MESSAGING_SERVICE_SID="MGtest",
    TWILIO_BASE_URL="",
)


class StubTransport:
    """Satisfies the SDK's sync transport protocol: send() + close()."""

    def __init__(self, *responses: HttpResponse):
        self._responses = list(responses)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self._responses.pop(0)

    def close(self) -> None:  # pragma: no cover
        pass

    @property
    def last_request(self) -> HttpRequest | None:
        return self.requests[-1] if self.requests else None


class RaisingTransport(StubTransport):
    def __init__(self, error: Exception, *responses: HttpResponse):
        super().__init__(*responses)
        self._error = error

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        raise self._error


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def install(transport) -> StubTransport:
    """Point the gateway's global client at a stub transport."""
    gw._reset_client_for_tests()
    gw._client = Client(
        custom_http_client=transport,
        account_sid_auth_token=BasicAuthCredentials(username="ACtest", password="secrettoken"),
    )
    return transport


def message_body(sid="SM1", status="queued", **extra) -> dict:
    body = {"sid": sid, "status": status, "from": "+15550000000", "to": "+15551112222"}
    body.update(extra)
    return body


@override_settings(**TWILIO_SETTINGS)
class GatewayTests(TestCase):
    def tearDown(self):
        gw._reset_client_for_tests()

    def test_canonicalize_returns_provider_form(self):
        install(StubTransport(json_response(200, {"phone_number": "+15551112222", "country_code": "US"})))
        self.assertEqual(gw.canonicalize_number("(555) 111-2222"), "+15551112222")

    def test_canonicalize_rejects_unusable_number(self):
        install(StubTransport(json_response(404, {"code": 20404})))
        with self.assertRaises(gw.InvalidPhoneNumber):
            gw.canonicalize_number("+1550001")

    def test_lookup_uses_v1_and_hits_lookups_host(self):
        t = install(StubTransport(json_response(200, {"phone_number": "+15551112222"})))
        gw.canonicalize_number("+15551112222")
        req = t.last_request
        self.assertIn("/v1/PhoneNumbers/", req.url)
        self.assertIn("lookups.twilio.com", req.url)

    def test_send_sets_from_number_and_returns_sid(self):
        t = install(StubTransport(json_response(201, message_body(sid="SMabc", status="queued"))))
        sent = gw.send_sms("+15551112222", "hi")
        self.assertEqual(sent.sid, "SMabc")
        req = t.last_request
        self.assertEqual(req.method, "POST")
        self.assertTrue(req.url.endswith("/Messages.json"))
        self.assertEqual(req.body.fields["From"], "+15550000000")
        self.assertEqual(req.body.fields["To"], "+15551112222")
        self.assertEqual(req.body.fields["Body"], "hi")

    def test_schedule_followup_uses_messaging_service_and_fixed(self):
        t = install(StubTransport(json_response(201, message_body(sid="SMsched", status="scheduled"))))
        when = timezone.now() + dt.timedelta(days=3)
        gw.schedule_followup("+15551112222", "survey", when)
        body = t.last_request.body.fields
        self.assertEqual(body["MessagingServiceSid"], "MGtest")
        self.assertEqual(body["ScheduleType"], "fixed")
        self.assertIn("SendAt", body)
        self.assertNotIn("From", body)

    def test_cancel_scheduled_posts_canceled_status(self):
        t = install(StubTransport(json_response(200, message_body(sid="SMsched", status="canceled"))))
        self.assertEqual(gw.cancel_scheduled("SMsched"), "canceled")
        self.assertEqual(t.last_request.body.fields["Status"], "canceled")

    def test_redact_content_posts_empty_body(self):
        t = install(StubTransport(json_response(200, message_body(sid="SM1", status="delivered"))))
        gw.redact_content("SM1")
        self.assertEqual(t.last_request.body.fields["Body"], "")

    def test_refused_connection_is_known_outcome(self):
        install(RaisingTransport(httpx.ConnectError("refused")))
        with self.assertRaises(gw.ProviderUnavailable) as ctx:
            gw.send_sms("+15551112222", "hi")
        self.assertFalse(ctx.exception.outcome_unknown)

    def test_read_timeout_is_unknown_outcome(self):
        install(RaisingTransport(httpx.ReadTimeout("no reply")))
        with self.assertRaises(gw.ProviderUnavailable) as ctx:
            gw.send_sms("+15551112222", "hi")
        self.assertTrue(ctx.exception.outcome_unknown)

    def test_credentials_rejected_is_config_error(self):
        install(StubTransport(json_response(401, {"code": 20003})))
        with self.assertRaises(gw.ConfigurationError):
            gw.send_sms("+15551112222", "hi")

    def test_reconcile_filters_by_from_number(self):
        t = install(StubTransport(json_response(200, {"messages": [], "next_page_uri": None})))
        gw.list_from_number(
            timezone.now() - dt.timedelta(days=1), timezone.now()
        )
        self.assertIn("From=%2B15550000000", t.last_request.url)


@override_settings(**TWILIO_SETTINGS)
class ServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("shopper", "shopper@example.com", "password123")
        ContactNumber.objects.create(user=self.user, e164="+15551112222")

    def tearDown(self):
        gw._reset_client_for_tests()

    def _order(self, status="Pending", user=None):
        return Order.objects.create(
            number="ORD-%s" % Order.objects.count(),
            user=user if user is not None else self.user,
            currency="GBP",
            total_incl_tax=D("10.00"),
            total_excl_tax=D("10.00"),
            status=status,
        )

    def test_send_failure_does_not_raise_and_records_row(self):
        install(RaisingTransport(httpx.ReadTimeout("no reply")))
        order = self._order()
        notif = services._record_and_send(order, OrderNotification.KIND_PLACED, "hello")
        self.assertEqual(notif.local_outcome, OrderNotification.OUTCOME_UNKNOWN)
        self.assertEqual(order.sms_notifications.count(), 1)

    def test_no_contact_number_is_not_messaged(self):
        other = User.objects.create_user("nonum", "n@example.com", "password123")
        order = self._order(user=other)
        install(StubTransport())  # no responses: a send would blow up if attempted
        notif = services._record_and_send(order, OrderNotification.KIND_PLACED, "hello")
        self.assertIsNone(notif)
        self.assertEqual(order.sms_notifications.count(), 0)

    def test_resend_is_idempotent_on_key(self):
        order = self._order()
        source = OrderNotification.objects.create(
            order=order, kind=OrderNotification.KIND_DISPATCHED, to_number="+15551112222",
            provider_sid="SMorig", provider_status="undelivered",
            local_outcome=OrderNotification.OUTCOME_SENT,
        )
        install(StubTransport(json_response(201, message_body(sid="SMresend1", status="queued"))))
        first, created1 = services.resend_notification(source, "key-1")
        self.assertTrue(created1)
        self.assertEqual(first.provider_sid, "SMresend1")
        # Same key again: no second send (stub has no more responses; must not be called).
        second, created2 = services.resend_notification(source, "key-1")
        self.assertFalse(created2)
        self.assertEqual(second.pk, first.pk)

    def test_resend_fresh_key_sends_again(self):
        order = self._order()
        source = OrderNotification.objects.create(
            order=order, kind=OrderNotification.KIND_DISPATCHED, to_number="+15551112222",
            provider_sid="SMorig", local_outcome=OrderNotification.OUTCOME_SENT,
        )
        install(StubTransport(
            json_response(201, message_body(sid="SMr1")),
            json_response(201, message_body(sid="SMr2")),
        ))
        services.resend_notification(source, "key-1")
        second, created = services.resend_notification(source, "key-2")
        self.assertTrue(created)
        self.assertEqual(second.provider_sid, "SMr2")

    def test_dispatch_schedules_followup_then_cancel_calls_it_off(self):
        order = self._order()
        # dispatched send + scheduled followup send
        install(StubTransport(
            json_response(201, message_body(sid="SMdisp", status="queued")),
            json_response(201, message_body(sid="SMsurvey", status="scheduled")),
        ))
        order, changed = services.dispatch_order(order)
        self.assertTrue(changed)
        self.assertEqual(order.status, "Being processed")
        followup = order.sms_notifications.get(is_followup=True)
        self.assertEqual(followup.provider_sid, "SMsurvey")

        # Cancel: cancels the scheduled followup, then sends cancellation notice.
        install(StubTransport(
            json_response(200, message_body(sid="SMsurvey", status="canceled")),
            json_response(201, message_body(sid="SMcancel", status="queued")),
        ))
        order, changed = services.cancel_order(order)
        self.assertTrue(changed)
        followup.refresh_from_db()
        self.assertTrue(followup.followup_cancelled)
        self.assertEqual(followup.provider_status, "canceled")

    def test_repeat_dispatch_is_noop_and_does_not_remessage(self):
        order = self._order(status="Being processed")
        install(StubTransport())  # any send would fail (no responses)
        order, changed = services.dispatch_order(order)
        self.assertFalse(changed)
        self.assertEqual(order.sms_notifications.count(), 0)

    def test_dispose_content_redacts_and_keeps_record(self):
        order = self._order()
        notif = OrderNotification.objects.create(
            order=order, kind=OrderNotification.KIND_PLACED, to_number="+15551112222",
            provider_sid="SMdisp", provider_status="delivered",
            local_outcome=OrderNotification.OUTCOME_SENT,
        )
        install(StubTransport(json_response(200, message_body(sid="SMdisp", status="delivered"))))
        notif = services.dispose_content(notif)
        self.assertTrue(notif.content_disposed)
        self.assertTrue(OrderNotification.objects.filter(pk=notif.pk).exists())

    def test_reconcile_matches_app_and_provider(self):
        order = self._order()
        now = timezone.now()
        OrderNotification.objects.create(
            order=order, kind=OrderNotification.KIND_PLACED, to_number="+15551112222",
            provider_sid="SMmatch", provider_status="delivered",
            provider_date_sent=now, local_outcome=OrderNotification.OUTCOME_SENT,
        )
        OrderNotification.objects.create(
            order=order, kind=OrderNotification.KIND_DISPATCHED, to_number="+15551112222",
            provider_sid="SMapponly", provider_status="queued",
            provider_date_sent=now, local_outcome=OrderNotification.OUTCOME_SENT,
        )
        provider_list = {
            "messages": [
                {"sid": "SMmatch", "status": "delivered", "from": "+15550000000",
                 "to": "+15551112222", "date_sent": now.strftime("%a, %d %b %Y %H:%M:%S +0000")},
                {"sid": "SMprovideronly", "status": "sent", "from": "+15550000000",
                 "to": "+15551112222", "date_sent": now.strftime("%a, %d %b %Y %H:%M:%S +0000")},
            ],
            "next_page_uri": None,
        }
        install(StubTransport(json_response(200, provider_list)))
        report = services.reconcile(now - dt.timedelta(hours=1), now + dt.timedelta(hours=1))
        self.assertEqual(report["counts"]["matched"], 1)
        self.assertEqual(report["counts"]["appOnly"], 1)
        self.assertEqual(report["counts"]["providerOnly"], 1)


@override_settings(**TWILIO_SETTINGS)
class ApiAuthTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "password123")
        self.bob = User.objects.create_user("bob", "bob@example.com", "password123")
        self.staff = User.objects.create_user("op", "op@example.com", "password123", is_staff=True)

    def tearDown(self):
        gw._reset_client_for_tests()

    def test_anonymous_gets_401(self):
        c = DjangoClient()
        self.assertEqual(c.get("/api/contact-numbers").status_code, 401)

    def test_shopper_cannot_dispatch(self):
        c = DjangoClient()
        c.force_login(self.alice)
        order = Order.objects.create(number="O1", user=self.alice, currency="GBP",
                                     total_incl_tax=D("1"), total_excl_tax=D("1"))
        self.assertEqual(c.post("/api/orders/%s/dispatch" % order.pk).status_code, 403)

    def test_shopper_cannot_see_others_numbers(self):
        ContactNumber.objects.create(user=self.bob, e164="+15559998888")
        c = DjangoClient()
        c.force_login(self.alice)
        data = c.get("/api/contact-numbers").json()
        self.assertEqual(data["contactNumbers"], [])

    def test_shopper_cannot_delete_others_number(self):
        num = ContactNumber.objects.create(user=self.bob, e164="+15559998888")
        c = DjangoClient()
        c.force_login(self.alice)
        self.assertEqual(c.delete("/api/contact-numbers/%s" % num.pk).status_code, 404)
        self.assertTrue(ContactNumber.objects.filter(pk=num.pk).exists())

    def test_shopper_cannot_view_others_order_notifications(self):
        order = Order.objects.create(number="O2", user=self.bob, currency="GBP",
                                     total_incl_tax=D("1"), total_excl_tax=D("1"))
        c = DjangoClient()
        c.force_login(self.alice)
        self.assertEqual(c.get("/api/orders/%s/notifications" % order.pk).status_code, 404)

    def test_register_number_returns_id(self):
        install(StubTransport(json_response(200, {"phone_number": "+15551112222"})))
        c = DjangoClient()
        c.force_login(self.alice)
        resp = c.post("/api/contact-numbers", data=json.dumps({"number": "555-111-2222"}),
                      content_type="application/json")
        self.assertEqual(resp.status_code, 201)
        self.assertIn("contactNumberId", resp.json())
        self.assertEqual(resp.json()["number"], "+15551112222")
