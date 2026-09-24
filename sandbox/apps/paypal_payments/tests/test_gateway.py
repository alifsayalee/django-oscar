from decimal import Decimal

import httpx
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from paypal.core import ApiError, RawError

from apps.paypal_payments import gateway, money, statuses

from . import stubs


def api_error(status: int) -> ApiError[RawError]:
    response = stubs.json_response(status, {"name": "X"})
    return ApiError(RawError(response), response)


class BaseUrlTests(SimpleTestCase):
    @override_settings(PAYPAL_BASE_URL=None, PAYPAL_ENVIRONMENT="sandbox")
    def test_sandbox(self) -> None:
        self.assertEqual(gateway.resolve_base_url(), "https://api-m.sandbox.paypal.com")

    @override_settings(PAYPAL_BASE_URL="http://127.0.0.1:9999", PAYPAL_ENVIRONMENT="sandbox")
    def test_override_is_used_verbatim(self) -> None:
        self.assertEqual(gateway.resolve_base_url(), "http://127.0.0.1:9999")

    @override_settings(PAYPAL_BASE_URL=None, PAYPAL_ENVIRONMENT="live")
    def test_other_environments_must_name_their_host(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            gateway.resolve_base_url()

    @override_settings(PAYPAL_CLIENT_ID="", PAYPAL_CLIENT_SECRET="")
    def test_missing_credentials_refuse_to_build_a_client(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            gateway.build_client()


class TranslateTests(SimpleTestCase):
    def test_never_sent_and_no_reply_are_different_outcomes(self) -> None:
        unsent = gateway.translate(httpx.ConnectError("refused"), write=True)
        unknown = gateway.translate(httpx.ReadTimeout("no reply"), write=True)
        self.assertEqual((unsent.status_code, unsent.outcome_unknown), (502, False))
        self.assertEqual((unknown.status_code, unknown.outcome_unknown), (504, True))

    def test_our_credentials_and_quota_are_not_the_callers_fault(self) -> None:
        self.assertEqual(gateway.translate(api_error(401), write=True).status_code, 502)
        self.assertEqual(gateway.translate(api_error(403), write=True).status_code, 502)
        self.assertEqual(gateway.translate(api_error(429), write=True).status_code, 503)
        self.assertEqual(gateway.translate(api_error(404), write=True).status_code, 502)

    def test_validation_passes_through_and_5xx_on_a_write_may_have_landed(self) -> None:
        self.assertEqual(gateway.translate(api_error(422), write=True).status_code, 422)
        server = gateway.translate(api_error(500), write=True)
        self.assertEqual((server.status_code, server.outcome_unknown), (502, True))


class StatusMapTests(SimpleTestCase):
    def test_every_capture_status(self) -> None:
        expected = {"COMPLETED": "done", "PARTIALLY_REFUNDED": "done", "PENDING": "pending", "DECLINED": "failed",
                    "FAILED": "failed", "REFUNDED": "failed", "NEW_THING": "unknown"}
        for status, outcome in expected.items():
            self.assertEqual(statuses.capture_outcome(status), outcome, status)

    def test_every_authorization_status(self) -> None:
        expected = {"CREATED": "done", "CAPTURED": "done", "PARTIALLY_CAPTURED": "done", "PENDING": "pending",
                    "DENIED": "failed", "VOIDED": "failed", "EXPIRED": "unknown"}
        for status, outcome in expected.items():
            self.assertEqual(statuses.authorization_outcome(status), outcome, status)

    def test_every_refund_status(self) -> None:
        expected = {"COMPLETED": "done", "PENDING": "pending", "FAILED": "failed", "CANCELLED": "failed",
                    "": "unknown"}
        for status, outcome in expected.items():
            self.assertEqual(statuses.refund_outcome(status), outcome, status)


class MoneyTests(SimpleTestCase):
    def test_currency_exponents(self) -> None:
        self.assertEqual(money.to_wire(Decimal("10"), "USD"), "10.00")
        self.assertEqual(money.to_wire(Decimal("1000"), "JPY"), "1000")
        self.assertEqual(money.to_wire(Decimal("1.234"), "KWD"), "1.234")

    def test_parse_amount(self) -> None:
        self.assertEqual(money.parse_amount("5.5", "USD"), Decimal("5.50"))
        for bad in ("0", "-1", "1.001", "abc", 1.5, True, "NaN"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                money.parse_amount(bad, "USD")
