"""Unit tests for the SMS notification app.

The Twilio SDK is exercised through its transport seam -- a fake transport passed as
``custom_http_client`` -- so no real network call happens and the real request-building pipeline is
still tested. No credentials, ids or timestamps from a live provider appear here.
"""

from __future__ import annotations

import json

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from oscar.core.loading import get_model

from twilio_sdk import TwilioSdkClient
from twilio_sdk.core import BasicAuthCredentials, HttpRequest, HttpResponse

from . import services
from . import twilio_gateway as tw
from .models import ContactNumber, OrderNotification, OrderTransition

StockRecord = get_model("partner", "StockRecord")

TWILIO_TEST_SETTINGS = dict(
    TWILIO_ACCOUNT_SID="ACtest",
    TWILIO_AUTH_TOKEN="secrettoken",
    TWILIO_FROM_NUMBER="+15551230000",
    TWILIO_MESSAGING_SERVICE_SID="MGtest",
    TWILIO_BASE_URL="",
)

CANONICAL = "+15558675309"


def _json(status, body):
    return HttpResponse(status_code=status, headers={"content-type": "application/json"},
                        content=json.dumps(body).encode())


class RoutingStub:
    """Answers by inspecting the request, so a multi-call flow needs no fixed queue.

    Configure per-category responses; a raiser can be set to simulate a transport failure.
    """

    def __init__(self):
        self.requests: list[HttpRequest] = []
        self.lookup_valid = True
        self.create_status = "queued"
        self.create_error_code = None
        self.create_raises: Exception | None = None
        self.create_http_error: int | None = None
        self.fetch_status = "delivered"
        self.list_messages: list[dict] = []
        self._sid_counter = 0

    def _next_sid(self):
        self._sid_counter += 1
        return f"SM{self._sid_counter:032d}"

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        url, method = request.url, request.method
        if "/v1/PhoneNumbers/" in url:
            if self.lookup_valid:
                return _json(200, {"phone_number": CANONICAL, "country_code": "US",
                                   "national_format": "(555) 867-5309", "caller_name": None,
                                   "carrier": None, "add_ons": None, "url": url})
            return _json(404, {"code": 20404, "message": "not found", "status": 404})
        if "/Messages/" in url and url.rstrip("/").endswith(".json"):
            # single-message update (POST) or fetch (GET)
            if method == "POST":
                return _json(200, {"sid": "SMupd", "status": "canceled", "error_code": None,
                                   "date_sent": "Wed, 23 Sep 2026 00:29:41 +0000"})
            return _json(200, {"sid": url.split("/Messages/")[1].split(".json")[0],
                               "status": self.fetch_status, "error_code": None,
                               "date_sent": "Wed, 23 Sep 2026 00:29:41 +0000"})
        if url.rstrip("/").endswith("/Messages.json") or "/Messages.json?" in url:
            if method == "POST":
                if self.create_raises is not None:
                    raise self.create_raises
                if self.create_http_error is not None:
                    return _json(self.create_http_error, {"code": 1, "message": "no"})
                return _json(201, {"sid": self._next_sid(), "status": self.create_status,
                                   "error_code": self.create_error_code, "from": "+15551230000",
                                   "date_sent": "Wed, 23 Sep 2026 00:29:41 +0000"})
            return _json(200, {"messages": self.list_messages, "next_page_uri": None})
        raise AssertionError(f"unexpected request: {method} {url}")

    def close(self):
        pass


class GatewayTestBase(TestCase):
    def setUp(self):
        self.stub = RoutingStub()
        tw._client = TwilioSdkClient(
            custom_http_client=self.stub,
            account_sid_auth_token=BasicAuthCredentials(username="ACtest", password="secrettoken"),
        )

    def tearDown(self):
        tw._client = None


@override_settings(**TWILIO_TEST_SETTINGS)
class StatusMappingTests(TestCase):
    def test_status_maps_by_name_default_unknown(self):
        from twilio_sdk.models.enums.message_enum_status import MessageEnumStatus
        self.assertEqual(tw.status_to_outcome(MessageEnumStatus.DELIVERED), tw.DONE)
        self.assertEqual(tw.status_to_outcome(MessageEnumStatus.SENT), tw.DONE)
        self.assertEqual(tw.status_to_outcome(MessageEnumStatus.QUEUED), tw.PENDING)
        self.assertEqual(tw.status_to_outcome(MessageEnumStatus.SCHEDULED), tw.PENDING)
        self.assertEqual(tw.status_to_outcome(MessageEnumStatus.UNDELIVERED), tw.FAILED)
        self.assertEqual(tw.status_to_outcome(MessageEnumStatus.CANCELED), tw.FAILED)
        self.assertEqual(tw.status_to_outcome(MessageEnumStatus.PARTIALLY_DELIVERED), tw.PARTIAL)
        # A value newer than the SDK survives as a plain string -> unknown, never failed.
        self.assertEqual(tw.status_to_outcome("brand_new_status"), tw.UNKNOWN)
        self.assertEqual(tw.status_to_outcome(None), tw.UNKNOWN)


@override_settings(**TWILIO_TEST_SETTINGS)
class LookupTests(GatewayTestBase):
    def test_valid_number_returns_canonical(self):
        self.assertEqual(tw.validate_and_canonicalize("(555) 867-5309"), CANONICAL)
        req = self.stub.requests[-1]
        self.assertEqual(req.method, "GET")
        self.assertIn("/v1/PhoneNumbers/", req.url)

    def test_unusable_number_rejected(self):
        self.stub.lookup_valid = False
        with self.assertRaises(tw.NotAUsableDestination):
            tw.validate_and_canonicalize("+15550001111")


@override_settings(**TWILIO_TEST_SETTINGS)
class SendBoundaryTests(GatewayTestBase):
    def test_send_immediate_builds_request_and_maps_status(self):
        result = tw.send_immediate(CANONICAL, "hi")
        self.assertTrue(result.sid)
        self.assertEqual(result.outcome, tw.PENDING)  # queued
        req = self.stub.requests[-1]
        self.assertEqual(req.method, "POST")
        self.assertTrue(req.url.endswith("/Messages.json"))
        self.assertEqual(req.body.fields["To"], CANONICAL)
        self.assertEqual(req.body.fields["From"], "+15551230000")
        self.assertEqual(req.body.fields["Body"], "hi")

    def test_provider_rejection_is_failed(self):
        self.stub.create_http_error = 400
        with self.assertRaises(tw.TwilioGatewayError) as e:
            tw.send_immediate(CANONICAL, "hi")
        self.assertEqual(e.exception.outcome, tw.FAILED)

    def test_connect_error_is_failed_never_sent(self):
        self.stub.create_raises = httpx.ConnectError("refused")
        with self.assertRaises(tw.TwilioGatewayError) as e:
            tw.send_immediate(CANONICAL, "hi")
        self.assertEqual(e.exception.outcome, tw.FAILED)  # known: nothing happened

    def test_read_timeout_is_unknown_may_have_landed(self):
        self.stub.create_raises = httpx.ReadTimeout("no reply")
        with self.assertRaises(tw.TwilioGatewayError) as e:
            tw.send_immediate(CANONICAL, "hi")
        self.assertEqual(e.exception.outcome, tw.UNKNOWN)  # not the same fact as never-sent


@override_settings(**TWILIO_TEST_SETTINGS)
class OrderFlowTests(GatewayTestBase):
    def setUp(self):
        super().setUp()
        from decimal import Decimal as D
        from oscar.test.factories import create_product
        User = get_user_model()
        self.shopper = User.objects.create_user(username="shopper1", password="pw")
        self.product = create_product(price=D("10.00"), num_in_stock=5)
        ContactNumber.objects.create(user=self.shopper, canonical_number=CANONICAL)

    def _place(self):
        return services.place_order(self.shopper, [{"productId": self.product.id, "quantity": 1}])

    def test_place_order_sends_and_records(self):
        order = self._place()
        note = order.sms_notifications.get(kind=OrderNotification.KIND_PLACED)
        self.assertTrue(note.provider_sid)
        self.assertEqual(note.to_number, CANONICAL)

    def test_send_failure_never_fails_the_order(self):
        self.stub.create_raises = httpx.ReadTimeout("no reply")
        order = self._place()  # must not raise
        note = order.sms_notifications.get(kind=OrderNotification.KIND_PLACED)
        self.assertEqual(note.outcome, tw.UNKNOWN)
        self.assertEqual(order.lines.count(), 1)  # order still placed

    def test_no_number_on_file_is_skipped_not_failed(self):
        ContactNumber.objects.filter(user=self.shopper).delete()
        order = self._place()
        note = order.sms_notifications.get(kind=OrderNotification.KIND_PLACED)
        self.assertEqual(note.outcome, OrderNotification.OUTCOME_SKIPPED)

    def test_dispatch_is_idempotent(self):
        order = self._place()
        self.assertTrue(services.dispatch_order(order, self.shopper))
        self.assertFalse(services.dispatch_order(order, self.shopper))  # no second run
        self.assertEqual(order.sms_notifications.filter(kind=OrderNotification.KIND_DISPATCHED).count(), 1)
        self.assertEqual(
            order.sms_notifications.filter(kind=OrderNotification.KIND_DISPATCHED_FOLLOWUP).count(), 1
        )
        self.assertEqual(OrderTransition.objects.filter(order=order).count(), 1)

    def test_cancel_calls_off_the_followup(self):
        order = self._place()
        self.stub.create_status = "scheduled"
        services.dispatch_order(order, self.shopper)
        followup = order.sms_notifications.get(kind=OrderNotification.KIND_DISPATCHED_FOLLOWUP)
        self.assertFalse(followup.canceled)
        services.cancel_order(order, self.shopper)
        followup.refresh_from_db()
        self.assertTrue(followup.canceled)  # follow-up called off before it could go out

    def test_resend_idempotency(self):
        order = self._place()
        original = order.sms_notifications.get(kind=OrderNotification.KIND_PLACED)
        first = services.resend(original, "key-abc", self.shopper)
        again = services.resend(original, "key-abc", self.shopper)  # same key -> no second send
        self.assertEqual(first.id, again.id)
        fresh = services.resend(original, "key-def", self.shopper)  # fresh key -> new attempt
        self.assertNotEqual(first.id, fresh.id)
        self.assertEqual(
            OrderNotification.objects.filter(kind=OrderNotification.KIND_RESEND).count(), 2
        )


@override_settings(**TWILIO_TEST_SETTINGS)
class ScopingTests(GatewayTestBase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.a = User.objects.create_user(username="a", password="pw")
        self.b = User.objects.create_user(username="b", password="pw")

    def test_a_cannot_see_b_contact_number(self):
        ContactNumber.objects.create(user=self.b, canonical_number=CANONICAL)
        self.client.force_login(self.a)
        resp = self.client.get("/api/contact-numbers")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["contactNumbers"], [])

    def test_operator_endpoint_forbidden_for_shopper(self):
        self.client.force_login(self.a)
        resp = self.client.get("/api/notifications/reconciliation?from=2026-01-01T00:00:00Z&to=2026-12-31T00:00:00Z")
        self.assertEqual(resp.status_code, 403)

    def test_unauthenticated_rejected(self):
        resp = self.client.get("/api/contact-numbers")
        self.assertEqual(resp.status_code, 401)
