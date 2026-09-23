"""Gateway tests: exercise the real SDK request pipeline through a stub transport."""
import httpx
from django.test import SimpleTestCase, override_settings

from apps.sms import provider

from .support import RaisingTransport, StubTransport, json_response, make_client

MSG_OK = {
    "sid": "SM123",
    "status": "queued",
    "from": "+15550000000",
    "to": "+15551111111",
    "date_created": "Tue, 23 Sep 2026 10:00:00 +0000",
    "date_sent": None,
}


@override_settings(
    TWILIO_ACCOUNT_SID="ACxxx",
    TWILIO_AUTH_TOKEN="secret",
    TWILIO_FROM_NUMBER="+15550000000",
    TWILIO_MESSAGING_SERVICE_SID="MGxxx",
)
class ProviderGatewayTests(SimpleTestCase):
    def tearDown(self):
        provider._client = None

    def _install(self, transport):
        provider._client = make_client(transport)
        return transport

    # --- status mapping -----------------------------------------------------------------
    def test_status_mapping_enumerated_and_unknown(self):
        from twilio_sdk.models.enums import MessageEnumStatus

        self.assertEqual(provider.outcome_from_status(MessageEnumStatus.DELIVERED), "delivered")
        self.assertEqual(provider.outcome_from_status(MessageEnumStatus.SENT), "sent")
        self.assertEqual(provider.outcome_from_status(MessageEnumStatus.QUEUED), "pending")
        self.assertEqual(provider.outcome_from_status(MessageEnumStatus.SCHEDULED), "scheduled")
        self.assertEqual(provider.outcome_from_status(MessageEnumStatus.FAILED), "failed")
        self.assertEqual(provider.outcome_from_status(MessageEnumStatus.CANCELED), "canceled")
        # A value newer than the SDK survives as a plain str -> unknown (not failed).
        self.assertEqual(provider.outcome_from_status("some_future_status"), "unknown")

    # --- send success -------------------------------------------------------------------
    def test_send_immediate_success_captures_sid_and_from(self):
        transport = self._install(StubTransport(json_response(201, MSG_OK)))
        result = provider.send_immediate("+15551111111", "hi")
        self.assertEqual(result.sid, "SM123")
        self.assertEqual(result.outcome, "pending")
        self.assertFalse(result.outcome_unknown)
        # From=FROM_NUMBER actually reached the wire (form body).
        req = transport.last_request
        self.assertEqual(req.method, "POST")
        self.assertTrue(req.url.endswith("/Messages.json"))
        self.assertEqual(req.body.fields["From"], "+15550000000")
        self.assertEqual(req.body.fields["To"], "+15551111111")

    def test_schedule_followup_sets_service_and_schedule_fields(self):
        from datetime import datetime, timezone

        scheduled = dict(MSG_OK, status="scheduled")
        transport = self._install(StubTransport(json_response(201, scheduled)))
        when = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)
        result = provider.schedule_followup("+15551111111", "how did it go?", when)
        self.assertEqual(result.outcome, "scheduled")
        fields = transport.last_request.body.fields
        self.assertEqual(fields["MessagingServiceSid"], "MGxxx")
        self.assertEqual(fields["ScheduleType"], "fixed")
        self.assertIn("SendAt", fields)
        self.assertNotIn("From", fields)  # From cannot be combined with scheduling

    # --- send failures never raise ------------------------------------------------------
    def test_api_error_is_recorded_failed_not_raised(self):
        self._install(StubTransport(json_response(400, {"code": 21211, "message": "bad To"})))
        result = provider.send_immediate("+1", "hi")
        self.assertIsNone(result.sid)
        self.assertEqual(result.outcome, "failed")
        self.assertFalse(result.outcome_unknown)

    def test_refused_connection_is_failed_known(self):
        self._install(RaisingTransport(httpx.ConnectError("refused")))
        result = provider.send_immediate("+1", "hi")
        self.assertEqual(result.outcome, "failed")
        self.assertFalse(result.outcome_unknown)  # never sent -> known

    def test_read_timeout_is_unknown(self):
        self._install(RaisingTransport(httpx.ReadTimeout("no reply")))
        result = provider.send_immediate("+1", "hi")
        self.assertEqual(result.outcome, "unknown")
        self.assertTrue(result.outcome_unknown)  # may have landed

    def test_decode_failure_on_success_is_unknown(self):
        # 2xx whose body has a type mismatch -> ValidationError, bypasses both modes.
        self._install(StubTransport(json_response(201, {"sid": 12345, "status": ["bad"]})))
        result = provider.send_immediate("+1", "hi")
        self.assertEqual(result.outcome, "unknown")
        self.assertTrue(result.outcome_unknown)

    def test_2xx_without_sid_is_unknown(self):
        self._install(StubTransport(json_response(201, {"status": "queued"})))
        result = provider.send_immediate("+1", "hi")
        self.assertIsNone(result.sid)
        self.assertTrue(result.outcome_unknown)

    # --- lookup -------------------------------------------------------------------------
    def test_lookup_ok_returns_canonical(self):
        self._install(StubTransport(json_response(200, {"phone_number": "+18254751588"})))
        self.assertEqual(provider.lookup_number("825 475 1588"), "+18254751588")

    def test_lookup_404_returns_none(self):
        self._install(StubTransport(json_response(404, {"code": 20404})))
        self.assertIsNone(provider.lookup_number("garbage"))

    def test_lookup_transport_failure_raises_provider_error(self):
        self._install(RaisingTransport(httpx.ConnectError("refused")))
        with self.assertRaises(provider.ProviderError):
            provider.lookup_number("+18254751588")

    # --- cancel / redact ----------------------------------------------------------------
    def test_cancel_scheduled_sends_status_canceled(self):
        canceled = dict(MSG_OK, status="canceled")
        transport = self._install(StubTransport(json_response(200, canceled)))
        result = provider.cancel_scheduled("SM123")
        self.assertEqual(result.outcome, "canceled")
        self.assertEqual(transport.last_request.body.fields["Status"], "canceled")

    def test_cancel_retries_transient_404_then_succeeds(self):
        # A scheduled message is briefly not updatable (404) right after creation.
        canceled = dict(MSG_OK, status="canceled")
        transport = self._install(
            StubTransport(
                json_response(404, {"code": 20404}),
                json_response(404, {"code": 20404}),
                json_response(200, canceled),
            )
        )
        result = provider.cancel_scheduled("SM123", _sleep=lambda _s: None)
        self.assertEqual(result.outcome, "canceled")
        self.assertEqual(len(transport.requests), 3)

    def test_redact_sends_empty_body(self):
        transport = self._install(StubTransport(json_response(200, dict(MSG_OK, body=""))))
        provider.redact_message("SM123")
        self.assertEqual(transport.last_request.body.fields["Body"], "")

    # --- reconciliation pagination ------------------------------------------------------
    def test_list_messages_paginates_and_bounds(self):
        page1 = {"messages": [dict(MSG_OK, sid="SM1")], "next_page_uri": "/x?Page=1&PageToken=TOK"}
        page2 = {"messages": [dict(MSG_OK, sid="SM2")], "next_page_uri": None}
        transport = self._install(StubTransport(json_response(200, page1), json_response(200, page2)))
        from datetime import datetime, timezone

        a = datetime(2026, 9, 20, tzinfo=timezone.utc)
        b = datetime(2026, 9, 25, tzinfo=timezone.utc)
        messages, truncated = provider.list_messages_from("+15550000000", a, b)
        self.assertEqual(len(messages), 2)
        self.assertFalse(truncated)
        self.assertEqual(len(transport.requests), 2)
