"""The error-translation boundary: every Maxio failure kind -> one ProviderError with the right
status and outcome flag (``python-error-handling``).  Tests assert on OUR boundary's output."""

import httpx
from django.test import SimpleTestCase

from ..client import get_client, reset_client, translate_errors
from ..exceptions import (
    ProviderConfigError,
    ProviderRejected,
    ProviderUnavailable,
    ProviderUnreadable,
)
from .fakes import QueueTransport, RaisingTransport, install, json_response


class ApiErrorLadderTests(SimpleTestCase):
    def tearDown(self):
        reset_client()

    def _call_with(self, response):
        install(QueueTransport(response))
        with translate_errors(writes=False):
            get_client().product_families.list_product_families()

    def test_500_is_502_unavailable(self):
        with self.assertRaises(ProviderUnavailable) as ctx:
            self._call_with(json_response(500, {"error": "boom"}))
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertFalse(ctx.exception.outcome_unknown)

    def test_401_is_our_config_error_502(self):
        with self.assertRaises(ProviderConfigError) as ctx:
            self._call_with(json_response(401, {"error": "unauthorized"}))
        self.assertEqual(ctx.exception.status_code, 502)

    def test_429_is_503(self):
        with self.assertRaises(ProviderUnavailable) as ctx:
            self._call_with(json_response(429, {"error": "slow down"}))
        self.assertEqual(ctx.exception.status_code, 503)

    def test_422_is_caller_fault_passed_through(self):
        with self.assertRaises(ProviderRejected) as ctx:
            self._call_with(json_response(422, {"errors": ["bad input"]}))
        self.assertEqual(ctx.exception.status_code, 422)

    def test_decode_failure_on_read_is_unreadable_502(self):
        # 200 but the body is not the expected list -> ValidationError, not ApiError.
        with self.assertRaises(ProviderUnreadable) as ctx:
            self._call_with(json_response(200, {"not": "a list"}))
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertFalse(ctx.exception.outcome_unknown)


class TransportFailureTests(SimpleTestCase):
    """The two transport failures are different facts and must not collapse to one outcome."""

    def tearDown(self):
        reset_client()

    def _write_call_raising(self, error):
        install(RaisingTransport(error))
        with translate_errors(writes=True):
            get_client().product_families.list_product_families()

    def test_refused_connection_is_known_never_sent_502(self):
        with self.assertRaises(ProviderUnavailable) as ctx:
            self._write_call_raising(httpx.ConnectError("refused"))
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertFalse(ctx.exception.outcome_unknown)

    def test_read_timeout_on_write_is_unknown_504(self):
        with self.assertRaises(ProviderUnavailable) as ctx:
            self._write_call_raising(httpx.ReadTimeout("no reply"))
        self.assertEqual(ctx.exception.status_code, 504)
        self.assertTrue(ctx.exception.outcome_unknown)

    def test_the_two_transport_failures_differ(self):
        # This is the assertion that proves the split is real, not decorative.
        install(RaisingTransport(httpx.ConnectError("refused")))
        try:
            with translate_errors(writes=True):
                get_client().product_families.list_product_families()
        except ProviderUnavailable as e:
            unsent = (e.status_code, e.outcome_unknown)
        reset_client()
        install(RaisingTransport(httpx.ReadTimeout("no reply")))
        try:
            with translate_errors(writes=True):
                get_client().product_families.list_product_families()
        except ProviderUnavailable as e:
            unknown = (e.status_code, e.outcome_unknown)
        self.assertEqual(unsent, (502, False))
        self.assertEqual(unknown, (504, True))
        self.assertNotEqual(unsent[0], unknown[0])
