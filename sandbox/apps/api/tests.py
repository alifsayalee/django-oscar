"""Unit tests for the checkout API.

Two layers, neither touching the network:

* Gateway tests fake the SDK's *transport* (the seam ``python-testing`` prescribes)
  so the real request-building and error-translation run against canned responses.
* Service tests fake the gateway functions and exercise the idempotency, over-refund
  and scoping logic against a real (sqlite) database.
"""

import json
from decimal import Decimal
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase

from paypal import PaypalClient
from paypal.core import HttpRequest, HttpResponse

from . import errors, paypal_gateway
from .models import PayPalPayment, PayPalRefund

User = get_user_model()


# ---------------------------------------------------------------------------
# Transport doubles
# ---------------------------------------------------------------------------


class StubTransport:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
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


def _json_response(status, body):
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def _token_response():
    return _json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})


def _client_with(transport):
    return PaypalClient(
        base_url="https://api-m.sandbox.paypal.com",
        oauth2={"client_id": "id", "client_secret": "secret"},
        custom_http_client=transport,
    )


class _GatewayClientMixin:
    """Point the gateway singleton at a stub client for the duration of a test."""

    def use_client(self, transport):
        client = _client_with(transport)
        patcher = mock.patch.object(paypal_gateway, "_client", client)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client


# ---------------------------------------------------------------------------
# Gateway: money formatting
# ---------------------------------------------------------------------------


class MoneyFormattingTests(TestCase):
    def test_two_decimal_currency(self):
        self.assertEqual(paypal_gateway.format_amount(Decimal("10"), "USD"), "10.00")

    def test_zero_decimal_currency(self):
        self.assertEqual(paypal_gateway.format_amount(Decimal("1000"), "JPY"), "1000")

    def test_three_decimal_currency(self):
        self.assertEqual(paypal_gateway.format_amount(Decimal("1.2344"), "KWD"), "1.234")


# ---------------------------------------------------------------------------
# Gateway: request building
# ---------------------------------------------------------------------------


class CreateOrderRequestTests(_GatewayClientMixin, TestCase):
    def test_builds_authorize_order_with_idempotency_and_amount(self):
        transport = StubTransport(
            _token_response(),
            _json_response(201, {"id": "PP-ORDER-1", "status": "CREATED"}),
        )
        self.use_client(transport)

        order_id = paypal_gateway.create_paypal_order("100001", "100001-abcdef", Decimal("20"), "USD")

        self.assertEqual(order_id, "PP-ORDER-1")
        req = transport.last_request
        self.assertEqual(req.method, "POST")
        self.assertTrue(req.url.endswith("/v2/checkout/orders"))
        # Idempotency header is sent (lowercased key).
        self.assertEqual(req.headers["paypal-request-id"], "ord-100001-create")
        body = req.body.value
        unit = body["purchase_units"][0]
        self.assertEqual(unit["amount"], {"currency_code": "USD", "value": "20.00"})
        self.assertEqual(unit["custom_id"], "100001")
        self.assertEqual(unit["invoice_id"], "100001-abcdef")
        self.assertEqual(body["intent"], "AUTHORIZE")


# ---------------------------------------------------------------------------
# Gateway: error translation (the ladder from python-error-handling)
# ---------------------------------------------------------------------------


class ErrorTranslationTests(_GatewayClientMixin, TestCase):
    def test_refused_connection_is_known_outcome_502(self):
        self.use_client(RaisingTransport(httpx.ConnectError("refused"), _token_response()))
        with self.assertRaises(errors.ProviderUnavailable) as ctx:
            paypal_gateway.create_paypal_order("100001", "ref", Decimal("5"), "USD")
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertFalse(ctx.exception.outcome_unknown)

    def test_read_timeout_is_unknown_outcome_504(self):
        self.use_client(RaisingTransport(httpx.ReadTimeout("no reply"), _token_response()))
        with self.assertRaises(errors.ProviderUnavailable) as ctx:
            paypal_gateway.create_paypal_order("100001", "ref", Decimal("5"), "USD")
        self.assertEqual(ctx.exception.status_code, 504)
        self.assertTrue(ctx.exception.outcome_unknown)

    def test_two_transport_failures_are_distinct(self):
        # The two must not collapse to the same answer.
        self.use_client(RaisingTransport(httpx.ConnectError("refused"), _token_response()))
        with self.assertRaises(errors.ProviderUnavailable) as unsent:
            paypal_gateway.create_paypal_order("1", "r", Decimal("5"), "USD")
        self.use_client(RaisingTransport(httpx.ReadTimeout("no reply"), _token_response()))
        with self.assertRaises(errors.ProviderUnavailable) as unknown:
            paypal_gateway.create_paypal_order("1", "r", Decimal("5"), "USD")
        self.assertNotEqual(unsent.exception.status_code, unknown.exception.status_code)

    def test_bad_credentials_is_config_error(self):
        # A 401 on the token fetch -> config error, surfaced from the operation call.
        self.use_client(StubTransport(_json_response(401, {"error": "invalid_client"})))
        with self.assertRaises(errors.ProviderConfigError):
            paypal_gateway.create_paypal_order("1", "r", Decimal("5"), "USD")

    def test_typed_422_is_surfaced_as_rejection(self):
        transport = StubTransport(
            _token_response(),
            _json_response(
                422,
                {
                    "name": "UNPROCESSABLE_ENTITY",
                    "message": "bad",
                    "debug_id": "d",
                    "details": [{"issue": "DUPLICATE_INVOICE_ID"}],
                },
            ),
        )
        self.use_client(transport)
        with self.assertRaises(errors.ApiError) as ctx:
            paypal_gateway.create_paypal_order("1", "r", Decimal("5"), "USD")
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("DUPLICATE_INVOICE_ID", ctx.exception.message)


# ---------------------------------------------------------------------------
# Services: refund idempotency, over-refund guard, scoping
# ---------------------------------------------------------------------------


def _make_order(user, number):
    from oscar.test.factories import create_order

    return create_order(number=number, user=user)


class RefundServiceTests(TestCase):
    def setUp(self):
        from . import services

        self.services = services
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "pw-123456789")
        self.other = User.objects.create_user("other", "other@example.com", "pw-123456789")
        self.order = _make_order(self.shopper, "100777")
        self.payment = PayPalPayment.objects.create(
            order=self.order,
            currency="USD",
            amount=Decimal("20.00"),
            state=PayPalPayment.CAPTURED,
            capture_id="CAP-1",
            gross_amount=Decimal("20.00"),
        )

    def test_partial_refund_and_idempotent_repeat(self):
        calls = []

        def fake_refund(capture_id, amount, currency, key):
            calls.append(key)
            return {"refund_id": "RF-1", "status": "COMPLETED", "amount": amount}

        with mock.patch.object(self.services.paypal_gateway, "refund", side_effect=fake_refund):
            r1 = self.services.refund_order(self.shopper, "100777", "5.00", "key-a")
            r2 = self.services.refund_order(self.shopper, "100777", "5.00", "key-a")

        self.assertEqual(r1.refund_id, "RF-1")
        self.assertEqual(r2.pk, r1.pk)
        self.assertEqual(len(calls), 1)  # PayPal called once, not twice
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.state, PayPalPayment.PARTIALLY_REFUNDED)

    def test_two_distinct_keys_are_two_refunds(self):
        def fake_refund(capture_id, amount, currency, key):
            return {"refund_id": "RF-" + key, "status": "COMPLETED", "amount": amount}

        with mock.patch.object(self.services.paypal_gateway, "refund", side_effect=fake_refund):
            self.services.refund_order(self.shopper, "100777", "5.00", "key-a")
            self.services.refund_order(self.shopper, "100777", "7.00", "key-b")
        self.assertEqual(PayPalRefund.objects.filter(payment=self.payment).count(), 2)
        self.assertEqual(self.payment.refunds.count(), 2)

    def test_over_refund_is_rejected(self):
        def fake_refund(*a, **k):  # should never be called
            raise AssertionError("PayPal must not be called for an over-refund")

        with mock.patch.object(self.services.paypal_gateway, "refund", side_effect=fake_refund):
            with self.assertRaises(errors.Unprocessable):
                self.services.refund_order(self.shopper, "100777", "25.00", "key-x")

    def test_cumulative_over_refund_is_rejected(self):
        def fake_refund(capture_id, amount, currency, key):
            return {"refund_id": "RF-" + key, "status": "COMPLETED", "amount": amount}

        with mock.patch.object(self.services.paypal_gateway, "refund", side_effect=fake_refund):
            self.services.refund_order(self.shopper, "100777", "15.00", "key-a")
            with self.assertRaises(errors.Unprocessable):
                self.services.refund_order(self.shopper, "100777", "10.00", "key-b")

    def test_another_shopper_cannot_refund(self):
        with self.assertRaises(errors.NotFound):
            self.services.refund_order(self.other, "100777", "5.00", "key-a")

    def test_refund_requires_idempotency_key(self):
        with self.assertRaises(errors.BadRequest):
            self.services.refund_order(self.shopper, "100777", "5.00", "")
