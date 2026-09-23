"""Unit tests for the Twilio SMS-notification integration.

These fake the SDK's TRANSPORT (its documented test seam) rather than the client
or the gateway, so the real request-building and response-decoding pipeline runs.
The assertions are about OUR boundary's behaviour -- the status mapping, the
error translation, and the idempotent resend -- not about the SDK itself.
"""

import json

import httpx
from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model

from twilio_sdk import Client
from twilio_sdk.core import BasicAuthCredentials, HttpRequest, HttpResponse

from oscar.test.factories import create_order

from . import exceptions, gateway, services
from .models import (
    ContactNumber,
    Notification,
    NotificationCategory,
    NotificationStatus,
)
from .status import map_status

TWILIO_TEST_SETTINGS = dict(
    TWILIO_ACCOUNT_SID="AC" + "0" * 32,
    TWILIO_AUTH_TOKEN="x" * 32,
    TWILIO_FROM_NUMBER="+15550009999",
    TWILIO_MESSAGING_SERVICE_SID="MG" + "0" * 32,
    TWILIO_BASE_URL="",
)


class StubTransport:
    """Sync transport protocol: send() + close(). Returns queued responses in order."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("stub transport ran out of responses")
        return self._responses.pop(0)

    def close(self):
        pass

    @property
    def last_request(self):
        return self.requests[-1] if self.requests else None


class RaisingTransport(StubTransport):
    def __init__(self, error, *responses):
        super().__init__(*responses)
        self._error = error

    def send(self, request):
        if self._responses:
            return super().send(request)
        self.requests.append(request)
        raise self._error


def json_response(status, body):
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def install_transport(transport):
    client = Client(
        account_sid_auth_token=BasicAuthCredentials(username="AC0", password="tok"),
        custom_http_client=transport,
    )
    gateway.reset_client_for_tests(client)
    return transport


def tearDownModule():
    gateway.reset_client_for_tests(None)


class StatusMappingTests(TestCase):
    def test_delivered_family(self):
        for raw in ("delivered", "received", "read"):
            self.assertEqual(map_status(raw), NotificationStatus.DELIVERED)

    def test_pending_family(self):
        for raw in ("queued", "sending", "accepted", "scheduled"):
            self.assertEqual(map_status(raw), NotificationStatus.PENDING)

    def test_failed_family(self):
        for raw in ("failed", "undelivered"):
            self.assertEqual(map_status(raw), NotificationStatus.FAILED)

    def test_canceled_and_partial(self):
        self.assertEqual(map_status("canceled"), NotificationStatus.CANCELED)
        self.assertEqual(map_status("partially_delivered"), NotificationStatus.PARTIAL)

    def test_unknown_default_arm(self):
        # A value newer than the SDK, or None, is unknown -- never delivered/failed.
        self.assertEqual(map_status("something_new"), NotificationStatus.UNKNOWN)
        self.assertEqual(map_status(None), NotificationStatus.UNKNOWN)


@override_settings(**TWILIO_TEST_SETTINGS)
class GatewayBoundaryTests(TestCase):
    def tearDown(self):
        gateway.reset_client_for_tests(None)

    def test_send_success_builds_request_and_maps_status(self):
        t = install_transport(
            StubTransport(json_response(201, {"sid": "SM123", "status": "queued"}))
        )
        result = gateway.send_sms("+15551234567", "hello")
        self.assertEqual(result.sid, "SM123")
        self.assertEqual(result.status, NotificationStatus.PENDING)
        req = t.last_request
        self.assertEqual(req.method, "POST")
        self.assertIn("/Messages.json", req.url)

    def test_send_sid_with_failed_status_is_not_delivered(self):
        # A returned SID means accepted, not delivered.
        install_transport(
            StubTransport(json_response(201, {"sid": "SM1", "status": "undelivered", "error_code": 30006}))
        )
        result = gateway.send_sms("+15551234567", "hi")
        self.assertEqual(result.sid, "SM1")
        self.assertEqual(result.status, NotificationStatus.FAILED)
        self.assertEqual(result.error_code, 30006)

    def test_send_5xx_is_unavailable_outcome_unknown(self):
        install_transport(StubTransport(json_response(500, {"message": "boom"})))
        with self.assertRaises(exceptions.ProviderUnavailable) as cm:
            gateway.send_sms("+15551234567", "hi")
        self.assertTrue(cm.exception.outcome_unknown)  # a write 5xx may have landed

    def test_send_connect_error_is_known_not_sent(self):
        install_transport(RaisingTransport(httpx.ConnectError("refused")))
        with self.assertRaises(exceptions.ProviderUnavailable) as cm:
            gateway.send_sms("+15551234567", "hi")
        self.assertEqual(cm.exception.status_code, 502)
        self.assertFalse(cm.exception.outcome_unknown)  # never left the process

    def test_send_read_timeout_is_unknown(self):
        install_transport(RaisingTransport(httpx.ReadTimeout("no reply")))
        with self.assertRaises(exceptions.ProviderUnavailable) as cm:
            gateway.send_sms("+15551234567", "hi")
        self.assertEqual(cm.exception.status_code, 504)
        self.assertTrue(cm.exception.outcome_unknown)

    def test_lookup_200_returns_canonical(self):
        # Transport is stubbed, so these are fictional example values, not the
        # real test destination -- no real number is ever written to source.
        install_transport(
            StubTransport(json_response(200, {"phone_number": "+12025550123", "country_code": "US"}))
        )
        self.assertEqual(gateway.validate_number("(202) 555-0123", country_code="US"), "+12025550123")

    def test_lookup_404_is_rejected(self):
        # A read retries transient failures, so queue a 404 for each attempt.
        install_transport(StubTransport(json_response(404, {"code": 20404})))
        with self.assertRaises(exceptions.ProviderRejected) as cm:
            gateway.validate_number("12345")
        self.assertEqual(cm.exception.status_code, 404)

    def test_lookup_401_is_config_error(self):
        install_transport(StubTransport(json_response(401, {"code": 20003})))
        with self.assertRaises(exceptions.ProviderConfigError):
            gateway.validate_number("+15551234567")

    def test_cancel_retries_transient_404(self):
        # A just-scheduled message answers 404 on update for a few seconds; the
        # gateway retries and succeeds once the message becomes updatable.
        gateway._UPDATE_404_BACKOFF = 0  # no real sleep in tests
        install_transport(
            StubTransport(
                json_response(404, {"code": 20404, "message": "not found yet"}),
                json_response(404, {"code": 20404, "message": "not found yet"}),
                json_response(200, {"sid": "SMx", "status": "canceled"}),
            )
        )
        result = gateway.cancel_scheduled("SMx")
        self.assertEqual(result.status, NotificationStatus.CANCELED)

    def test_cancel_persistent_404_raises(self):
        gateway._UPDATE_404_BACKOFF = 0
        install_transport(
            StubTransport(*[json_response(404, {"code": 20404}) for _ in range(20)])
        )
        with self.assertRaises(exceptions.ProviderRejected):
            gateway.cancel_scheduled("SMmissing")

    def test_redact_sends_empty_body(self):
        t = install_transport(
            StubTransport(json_response(200, {"sid": "SMr", "status": "delivered", "body": ""}))
        )
        result = gateway.redact_content("SMr")
        self.assertEqual(result.status, NotificationStatus.DELIVERED)
        # The update carried an empty Body form field.
        req = t.last_request
        self.assertEqual(req.method, "POST")
        self.assertIn("Body", req.body.fields)
        self.assertEqual(req.body.fields["Body"], "")

    def test_list_paginates_and_stops(self):
        page1 = {
            "messages": [{"sid": "SM1", "status": "delivered", "from": "+15550009999", "to": "+1555", "date_sent": "x"}],
            "next_page_uri": "/2010-04-01/Accounts/AC/Messages.json?PageToken=PA2&Page=1",
        }
        page2 = {
            "messages": [{"sid": "SM2", "status": "sent", "from": "+15550009999", "to": "+1555", "date_sent": "y"}],
            "next_page_uri": None,
        }
        install_transport(StubTransport(json_response(200, page1), json_response(200, page2)))
        import datetime
        msgs, truncated = gateway.list_sent_messages(
            "+15550009999",
            datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc),
            datetime.datetime(2026, 9, 30, tzinfo=datetime.timezone.utc),
        )
        self.assertEqual([m.sid for m in msgs], ["SM1", "SM2"])
        self.assertFalse(truncated)


@override_settings(**TWILIO_TEST_SETTINGS)
class ResendIdempotencyTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("resender", "resender@example.com", "pw-123456789")
        self.order = create_order(user=self.user)
        # A resend is only allowed to a still-registered number.
        ContactNumber.objects.create(owner=self.user, phone_number="+15551234567")
        self.original = Notification.objects.create(
            order=self.order,
            category=NotificationCategory.DISPATCHED,
            recipient="+15551234567",
            body="on its way",
            status=NotificationStatus.FAILED,
            provider_sid="SMold",
        )

    def tearDown(self):
        gateway.reset_client_for_tests(None)

    def test_same_key_sends_once(self):
        install_transport(StubTransport(json_response(201, {"sid": "SMnew", "status": "queued"})))
        first, created1 = services.resend_notification(self.original, "key-abc")
        self.assertTrue(created1)
        self.assertEqual(first.provider_sid, "SMnew")

        # Second call under the SAME key must NOT send again (no response queued;
        # if it tried to send, the stub would raise).
        second, created2 = services.resend_notification(self.original, "key-abc")
        self.assertFalse(created2)
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(Notification.objects.filter(category=NotificationCategory.RESEND).count(), 1)

    def test_resend_to_deleted_number_is_refused(self):
        # Remove the registered number -> a resend must not reach it again.
        ContactNumber.objects.filter(owner=self.user).delete()
        install_transport(StubTransport())  # stub would raise if a send were attempted
        with self.assertRaises(services.RecipientNotRegistered):
            services.resend_notification(self.original, "key-x")
        self.assertEqual(Notification.objects.filter(category=NotificationCategory.RESEND).count(), 0)

    def test_fresh_key_sends_again(self):
        install_transport(
            StubTransport(
                json_response(201, {"sid": "SMnew1", "status": "queued"}),
                json_response(201, {"sid": "SMnew2", "status": "queued"}),
            )
        )
        services.resend_notification(self.original, "key-1")
        services.resend_notification(self.original, "key-2")
        self.assertEqual(Notification.objects.filter(category=NotificationCategory.RESEND).count(), 2)


@override_settings(**TWILIO_TEST_SETTINGS)
class NotifyBestEffortTests(TestCase):
    def tearDown(self):
        gateway.reset_client_for_tests(None)

    def test_send_failure_does_not_raise_and_records_failed(self):
        User = get_user_model()
        user = User.objects.create_user("shopper", "shopper@example.com", "pw-123456789")
        order = create_order(user=user)
        # give the order's user a contact number
        ContactNumber.objects.create(owner=order.user, phone_number="+15551234567")
        install_transport(StubTransport(json_response(500, {"message": "down"})))
        # Must not raise even though the provider errored.
        notifs = services.notify_order(order, NotificationCategory.PLACED)
        self.assertEqual(len(notifs), 1)
        notifs[0].refresh_from_db()
        # 5xx on a write -> outcome unknown, recorded as UNKNOWN (not FAILED).
        self.assertEqual(notifs[0].status, NotificationStatus.UNKNOWN)
