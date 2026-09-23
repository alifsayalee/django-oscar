"""Tests for the PayPal integration.

The SDK is exercised through its real request-building pipeline by injecting a
fake transport (the SDK's documented test seam) — never by mocking the client.
The first request any stub sees is the lazy OAuth token fetch, so a token
response is queued ahead of each operation's.
"""
import json
from decimal import Decimal

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from oscar.core.loading import get_model

from paypal import PaypalClient
from paypal.core import ApiError, HttpResponse, OAuthProviderError, RawError

from . import errors, money, paypal_client, services
from .models import PaymentStatus, PayPalCustomer, PayPalPayment, PayPalRefund, SavedPaymentMethod

User = get_user_model()
Order = get_model("order", "Order")


# --------------------------------------------------------------------------- #
# Transport stubs (the SDK test seam)
# --------------------------------------------------------------------------- #
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
        if self._responses:  # let the token request through first
            return super().send(request)
        self.requests.append(request)
        raise self._error


def json_response(status, body):
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def token_response():
    return json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})


def install_client(transport):
    client = PaypalClient(
        base_url="https://api-m.sandbox.paypal.com",
        custom_http_client=transport,
        oauth2={"client_id": "x", "client_secret": "y"},
    )
    paypal_client._client = client
    return client


class StubClientMixin:
    def tearDown(self):
        paypal_client._client = None
        super().tearDown()

    def stub(self, *responses):
        transport = StubTransport(*responses)
        install_client(transport)
        return transport

    def stub_raising(self, error):
        transport = RaisingTransport(error, token_response())
        install_client(transport)
        return transport


# --------------------------------------------------------------------------- #
# Pure unit tests — no DB, no network
# --------------------------------------------------------------------------- #
class MoneyTests(TestCase):
    def test_currency_exponents(self):
        self.assertEqual(money.to_wire(Decimal("10"), "USD"), "10.00")
        self.assertEqual(money.to_wire(Decimal("1000"), "JPY"), "1000")
        self.assertEqual(money.to_wire(Decimal("1.2344"), "KWD"), "1.234")


class ErrorTranslateTests(TestCase):
    def _response(self, status, body=b""):
        return HttpResponse(status_code=status, headers={}, content=body)

    def _api_error(self, status, error):
        return ApiError(error=error, response=self._response(status))

    def _raw_error(self, status):
        return RawError(response=self._response(status))

    def test_oauth_failure_is_config_error(self):
        exc = self._api_error(401, OAuthProviderError(error="invalid_client"))
        out = errors.translate(exc, operation="op")
        self.assertIsInstance(out, errors.PayPalConfigError)
        self.assertEqual(out.status_code, 502)

    def test_401_is_ours_not_the_caller(self):
        out = errors.translate(self._api_error(401, self._raw_error(401)), operation="op")
        self.assertIsInstance(out, errors.PayPalConfigError)
        self.assertEqual(out.status_code, 502)

    def test_429_is_service_unavailable(self):
        out = errors.translate(self._api_error(429, self._raw_error(429)), operation="op")
        self.assertEqual(out.status_code, 503)

    def test_422_typed_is_caller_error_with_issues(self):
        from paypal.models import Error, ErrorDetails

        err = Error(
            name="UNPROCESSABLE_ENTITY",
            message="bad",
            debug_id="d1",
            details=[ErrorDetails(issue="CARD_EXPIRED")],
        )
        out = errors.translate(self._api_error(422, err), operation="op")  # noqa: E501
        self.assertIsInstance(out, errors.PayPalCallerError)
        self.assertEqual(out.status_code, 422)
        self.assertIn("CARD_EXPIRED", out.issues)

    def test_decode_failure_is_outcome_unknown(self):
        out = errors.translate(ValueError("bad json"), operation="op")
        self.assertTrue(out.outcome_unknown)

    def test_refused_connection_is_known_not_sent(self):
        out = errors.translate(httpx.ConnectError("refused"), operation="op")
        self.assertEqual((out.status_code, out.outcome_unknown), (502, False))

    def test_read_timeout_is_unknown(self):
        out = errors.translate(httpx.ReadTimeout("no reply"), operation="op")
        self.assertEqual((out.status_code, out.outcome_unknown), (504, True))

    def test_refused_and_timeout_are_distinguishable(self):
        refused = errors.translate(httpx.ConnectError("x"), operation="op")
        timeout = errors.translate(httpx.ReadTimeout("y"), operation="op")
        self.assertNotEqual(refused.status_code, timeout.status_code)
        self.assertNotEqual(refused.outcome_unknown, timeout.outcome_unknown)


# --------------------------------------------------------------------------- #
# Saved cards
# --------------------------------------------------------------------------- #
@override_settings(PAYPAL_CURRENCY="USD")
class SavedCardTests(StubClientMixin, TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="shopper", password="pw-123456789")
        self.other = User.objects.create_user(username="other", password="pw-123456789")

    def _vault_responses(self):
        return (
            token_response(),
            json_response(200, {"id": "ST-1", "status": "APPROVED", "customer": {"id": "CUST-1"}}),
            json_response(
                201,
                {
                    "id": "TOK-1",
                    "customer": {"id": "CUST-1"},
                    "payment_source": {"card": {"brand": "VISA", "last_digits": "1111", "expiry": "2030-01"}},
                },
            ),
        )

    def test_save_card_stores_safe_description_only(self):
        transport = self.stub(*self._vault_responses())
        saved = services.save_card(self.user, {"number": "4111111111111111", "expiry": "2030-01", "security_code": "123"})

        self.assertEqual(saved.payment_token_id, "TOK-1")
        self.assertEqual(saved.brand, "VISA")
        self.assertEqual(saved.last_digits, "1111")
        # PayPal customer recorded for reuse.
        self.assertTrue(PayPalCustomer.objects.filter(user=self.user, customer_id="CUST-1").exists())

        # The PAN reached PayPal but is never persisted or echoed back.
        setup_req = transport.requests[1]
        self.assertEqual(setup_req.body.value["payment_source"]["card"]["number"], "4111111111111111")
        for field in vars(saved).values():
            self.assertNotIn("4111111111111111", str(field))
        # Second (payment-token) request references the setup token, typed correctly.
        token_req = transport.requests[2]
        self.assertEqual(token_req.body.value["payment_source"]["token"]["id"], "ST-1")
        self.assertEqual(token_req.body.value["payment_source"]["token"]["type"], "SETUP_TOKEN")

    def test_saved_card_is_scoped_to_owner(self):
        self.stub(*self._vault_responses())
        services.save_card(self.user, {"number": "4111111111111111", "expiry": "2030-01", "security_code": "123"})
        self.assertEqual(len(services.list_saved_cards(self.user)), 1)
        self.assertEqual(len(services.list_saved_cards(self.other)), 0)

    def test_delete_removes_card_and_calls_paypal(self):
        SavedPaymentMethod.objects.create(
            user=self.user, payment_token_id="TOK-9", paypal_customer_id="CUST-1", brand="VISA", last_digits="1111"
        )
        transport = self.stub(token_response(), HttpResponse(status_code=204, headers={}))
        removed = services.delete_saved_card(self.user, "TOK-9")
        self.assertTrue(removed)
        self.assertFalse(SavedPaymentMethod.objects.filter(payment_token_id="TOK-9").exists())
        self.assertTrue(transport.last_request.url.endswith("/v3/vault/payment-tokens/TOK-9"))

    def test_delete_other_users_card_is_not_found(self):
        SavedPaymentMethod.objects.create(
            user=self.other, payment_token_id="TOK-X", paypal_customer_id="C", brand="VISA", last_digits="1111"
        )
        # No SDK call should happen; no stub installed.
        self.assertFalse(services.delete_saved_card(self.user, "TOK-X"))

    def test_bad_credentials_surface_as_config_error(self):
        # Token endpoint rejects -> config error, no operation ever sent.
        self.stub(json_response(401, {"error": "invalid_client"}))
        with self.assertRaises(errors.PayPalConfigError):
            services.save_card(self.user, {"number": "4111111111111111", "expiry": "2030-01"})

    def test_transport_failure_is_translated(self):
        self.stub_raising(httpx.ReadTimeout("no reply"))
        with self.assertRaises(errors.PayPalUnavailable) as ctx:
            services.save_card(self.user, {"number": "4111111111111111", "expiry": "2030-01"})
        self.assertTrue(ctx.exception.outcome_unknown)


# --------------------------------------------------------------------------- #
# Refund idempotency and cap (uses a directly-built captured payment)
# --------------------------------------------------------------------------- #
@override_settings(PAYPAL_CURRENCY="USD")
class RefundTests(StubClientMixin, TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="shopper", password="pw-123456789")
        order = Order.objects.create(
            number="TEST-1",
            site_id=1,
            user=self.user,
            currency="USD",
            total_incl_tax=Decimal("24.99"),
            total_excl_tax=Decimal("24.99"),
            shipping_incl_tax=Decimal("0.00"),
            shipping_excl_tax=Decimal("0.00"),
            status="Being processed",
            date_placed=timezone.now(),
        )
        self.payment = PayPalPayment.objects.create(
            order=order,
            user=self.user,
            status=PaymentStatus.CAPTURED,
            currency="USD",
            order_total=Decimal("24.99"),
            captured_amount=Decimal("24.99"),
            capture_id="CAP-1",
        )

    def test_partial_refund_then_same_key_is_idempotent(self):
        transport = self.stub(
            token_response(),
            json_response(201, {"id": "RF-1", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "10.00"}}),
        )
        payment, refund = services.refund_payment(self.payment, Decimal("10.00"), "key-1")
        self.assertEqual(refund.refund_id, "RF-1")
        self.assertEqual(payment.status, PaymentStatus.PARTIALLY_REFUNDED)
        self.assertEqual(payment.refunded_amount, Decimal("10.00"))
        calls_after_first = len(transport.requests)

        # Same key again: returns the same refund without a second PayPal call.
        payment2, refund2 = services.refund_payment(self.payment, Decimal("10.00"), "key-1")
        self.assertEqual(refund2.pk, refund.pk)
        self.assertEqual(len(transport.requests), calls_after_first)
        self.assertEqual(PayPalRefund.objects.filter(payment=self.payment).count(), 1)

    def test_two_distinct_partial_refunds_are_allowed(self):
        self.stub(
            token_response(),
            json_response(201, {"id": "RF-1", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "10.00"}}),
        )
        services.refund_payment(self.payment, Decimal("10.00"), "key-1")
        self.stub(
            token_response(),
            json_response(201, {"id": "RF-2", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "5.00"}}),
        )
        payment, refund = services.refund_payment(self.payment, Decimal("5.00"), "key-2")
        self.assertEqual(refund.refund_id, "RF-2")
        self.assertEqual(payment.refunded_amount, Decimal("15.00"))
        self.assertEqual(payment.status, PaymentStatus.PARTIALLY_REFUNDED)

    def test_refund_cannot_exceed_capture(self):
        with self.assertRaises(services.OrderBuildError):
            services.refund_payment(self.payment, Decimal("30.00"), "key-big")
        # No refund recorded, nothing sent.
        self.assertEqual(PayPalRefund.objects.filter(payment=self.payment).count(), 0)

    def test_full_refund_marks_refunded(self):
        self.stub(
            token_response(),
            json_response(201, {"id": "RF-1", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "24.99"}}),
        )
        payment, _ = services.refund_payment(self.payment, None, "key-full")
        self.assertEqual(payment.status, PaymentStatus.REFUNDED)
        self.assertEqual(payment.refunded_amount, Decimal("24.99"))
