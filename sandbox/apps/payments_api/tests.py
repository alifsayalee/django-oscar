"""Unit tests for the PayPal integration.

The SDK's transport protocol is the seam: a stub transport is passed as ``custom_http_client``
so no real network calls happen, and we assert on *our* boundary's behaviour, not the SDK's.
"""
import json
from decimal import Decimal

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase

from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpRequest, HttpResponse

from . import paypal_client as pp
from . import services
from .models import OrderPayment, PaymentRefund
from .services import ServiceError

User = get_user_model()


# --- transport stubs (python-testing) ---------------------------------------
class StubTransport:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return self._responses.pop(0)

    def close(self):
        pass


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
    return HttpResponse(status_code=status,
                        headers={"content-type": "application/json"},
                        content=json.dumps(body).encode())


def token_response():
    return json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})


def client_with(transport):
    return PaypalClient(custom_http_client=transport,
                        oauth2=ClientCredentials(client_id="x", client_secret="y"))


# --- pure helpers -----------------------------------------------------------
class MoneyTests(TestCase):
    def test_usd_two_places(self):
        self.assertEqual(pp.format_amount(Decimal("10"), "USD"), "10.00")
        self.assertEqual(pp.format_amount(Decimal("1.005"), "USD"), "1.00")

    def test_zero_decimal_currency(self):
        self.assertEqual(pp.format_amount(Decimal("1000"), "JPY"), "1000")

    def test_three_decimal_currency(self):
        self.assertEqual(pp.format_amount(Decimal("1.2344"), "KWD"), "1.234")

    def test_amounts_equal_ignores_trailing_zeros(self):
        self.assertTrue(pp.amounts_equal("10.0", Decimal("10.00"), "USD"))
        self.assertFalse(pp.amounts_equal("10.01", Decimal("10.00"), "USD"))


class StatusMappingTests(TestCase):
    def test_authorization_members(self):
        self.assertEqual(pp.authorization_outcome("CREATED"), "authorized")
        self.assertEqual(pp.authorization_outcome("DENIED"), "failed")
        self.assertEqual(pp.authorization_outcome("VOIDED"), "voided")

    def test_unlisted_authorization_status_is_unknown_not_failed(self):
        self.assertEqual(pp.authorization_outcome("SOMETHING_NEW"), "unknown")

    def test_capture_members(self):
        self.assertEqual(pp.capture_outcome("COMPLETED"), "captured")
        self.assertEqual(pp.capture_outcome("DECLINED"), "failed")
        self.assertEqual(pp.capture_outcome("WEIRD"), "unknown")


# --- error boundary ---------------------------------------------------------
class ErrorBoundaryTests(TestCase):
    def test_refused_connection_is_known_never_sent(self):
        client = client_with(RaisingTransport(httpx.ConnectError("refused"), token_response()))
        with self.assertRaises(pp.ProviderError) as ctx:
            pp.call(client.orders.create_order, body={"intent": "AUTHORIZE",
                    "purchase_units": [{"amount": {"currency_code": "USD", "value": "1.00"}}]},
                    write=True)
        self.assertEqual((ctx.exception.status_code, ctx.exception.outcome_unknown), (502, False))

    def test_read_timeout_on_write_is_unknown(self):
        client = client_with(RaisingTransport(httpx.ReadTimeout("no reply"), token_response()))
        with self.assertRaises(pp.ProviderError) as ctx:
            pp.call(client.orders.create_order, body={"intent": "AUTHORIZE",
                    "purchase_units": [{"amount": {"currency_code": "USD", "value": "1.00"}}]},
                    write=True)
        self.assertEqual((ctx.exception.status_code, ctx.exception.outcome_unknown), (504, True))

    def test_401_becomes_502_not_the_callers_fault(self):
        client = client_with(StubTransport(token_response(),
                             json_response(401, {"name": "AUTHENTICATION_FAILURE",
                                                 "message": "no", "debug_id": "d"})))
        with self.assertRaises(pp.ProviderError) as ctx:
            pp.call(client.orders.get_order, "ORDER1")
        self.assertEqual(ctx.exception.status_code, 502)

    def test_422_typed_error_surfaces_message_and_issue(self):
        client = client_with(StubTransport(token_response(), json_response(422, {
            "name": "UNPROCESSABLE_ENTITY", "message": "bad", "debug_id": "d",
            "details": [{"issue": "INSTRUMENT_DECLINED"}]})))
        with self.assertRaises(pp.ProviderError) as ctx:
            pp.call(client.orders.create_order, body={"intent": "AUTHORIZE",
                    "purchase_units": [{"amount": {"currency_code": "USD", "value": "1.00"}}]},
                    write=True)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("INSTRUMENT_DECLINED", ctx.exception.issues)


# --- refund idempotency (service level) -------------------------------------
class RefundIdempotencyTests(TestCase):
    def setUp(self):
        from oscar.apps.order.models import Order

        self.user = User.objects.create_user(username="shopper", password="pw-123456789")
        self.order = Order.objects.create(
            number="TEST-1", total_incl_tax=Decimal("10.00"), total_excl_tax=Decimal("10.00"),
            user=self.user,
        )
        self.op = OrderPayment.objects.create(
            order=self.order, user=self.user, status=OrderPayment.CAPTURED,
            currency="USD", amount=Decimal("10.00"), invoice_id="OSC-TEST-1-abc",
            capture_id="CAP1", captured_value=Decimal("10.00"),
        )

    def tearDown(self):
        pp._client = None

    def _stub_refund(self):
        transport = StubTransport(
            token_response(),
            json_response(201, {"id": "REF1", "status": "COMPLETED",
                                "amount": {"currency_code": "USD", "value": "4.00"}}),
        )
        pp._client = client_with(transport)
        return transport

    def test_repeat_key_does_not_refund_twice(self):
        transport = self._stub_refund()
        r1 = services.refund(self.op, amount="4.00", idempotency_key="k1")
        calls_after_first = len(transport.requests)
        r2 = services.refund(self.op, amount="4.00", idempotency_key="k1")
        self.assertEqual(r1.id, r2.id)
        # No further PayPal request was made on the repeat.
        self.assertEqual(len(transport.requests), calls_after_first)
        self.assertEqual(PaymentRefund.objects.filter(order_payment=self.op).count(), 1)

    def test_two_distinct_keys_are_two_refunds(self):
        # First partial refund of 4.00.
        self._stub_refund()
        services.refund(self.op, amount="4.00", idempotency_key="k1")
        # Second partial refund of 3.00 under a different key.
        transport2 = StubTransport(
            token_response(),
            json_response(201, {"id": "REF2", "status": "COMPLETED",
                                "amount": {"currency_code": "USD", "value": "3.00"}}),
        )
        pp._client = client_with(transport2)
        self.op.refresh_from_db()
        services.refund(self.op, amount="3.00", idempotency_key="k2")
        self.assertEqual(PaymentRefund.objects.filter(order_payment=self.op).count(), 2)

    def test_refund_beyond_captured_is_rejected(self):
        self._stub_refund()
        with self.assertRaises(ServiceError) as ctx:
            services.refund(self.op, amount="99.00", idempotency_key="k1")
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(PaymentRefund.objects.filter(order_payment=self.op).count(), 0)


class StaleAuthorizationCaptureTests(TestCase):
    """Capturing an expired authorization renews it (reauthorize) then captures the new one."""

    def setUp(self):
        from oscar.apps.order.models import Order

        self.user = User.objects.create_user(username="s2", password="pw-123456789")
        self.order = Order.objects.create(
            number="TEST-2", total_incl_tax=Decimal("10.00"), total_excl_tax=Decimal("10.00"),
            user=self.user,
        )
        self.op = OrderPayment.objects.create(
            order=self.order, user=self.user, status=OrderPayment.AUTHORIZED,
            currency="USD", amount=Decimal("10.00"), invoice_id="OSC-TEST-2-abc",
            authorization_id="AUTH-OLD",
        )

    def tearDown(self):
        pp._client = None

    def test_expired_auth_is_renewed_then_captured(self):
        transport = StubTransport(
            token_response(),
            # capture the stale auth -> 422 AUTHORIZATION_EXPIRED
            json_response(422, {"name": "UNPROCESSABLE_ENTITY", "message": "expired",
                               "debug_id": "d", "details": [{"issue": "AUTHORIZATION_EXPIRED"}]}),
            # reauthorize -> new auth id
            json_response(201, {"id": "AUTH-NEW", "status": "CREATED",
                               "expiration_time": "2026-10-01T00:00:00Z",
                               "amount": {"currency_code": "USD", "value": "10.00"}}),
            # capture the renewed auth -> COMPLETED with fee/net
            json_response(201, {"id": "CAP-NEW", "status": "COMPLETED",
                               "amount": {"currency_code": "USD", "value": "10.00"},
                               "seller_receivable_breakdown": {
                                   "gross_amount": {"currency_code": "USD", "value": "10.00"},
                                   "paypal_fee": {"currency_code": "USD", "value": "0.59"},
                                   "net_amount": {"currency_code": "USD", "value": "9.41"}}}),
        )
        pp._client = client_with(transport)
        op = services.capture(self.op)
        self.assertEqual(op.status, OrderPayment.CAPTURED)
        self.assertEqual(op.authorization_id, "AUTH-NEW")  # renewed
        self.assertEqual(op.capture_id, "CAP-NEW")
        self.assertEqual(op.paypal_fee, Decimal("0.59"))
        self.assertEqual(op.net_amount, Decimal("9.41"))
        # token + capture + reauthorize + capture = 4 requests
        self.assertEqual(len(transport.requests), 4)

    def test_unrenewable_expired_auth_reports_to_operator(self):
        transport = StubTransport(
            token_response(),
            json_response(422, {"name": "UNPROCESSABLE_ENTITY", "message": "expired",
                               "debug_id": "d", "details": [{"issue": "AUTHORIZATION_EXPIRED"}]}),
            # reauthorize refused (e.g. beyond 29-day window)
            json_response(422, {"name": "UNPROCESSABLE_ENTITY", "message": "cannot reauthorize",
                               "debug_id": "d",
                               "details": [{"issue": "REAUTHORIZATION_TOO_LATE"}]}),
        )
        pp._client = client_with(transport)
        with self.assertRaises(ServiceError) as ctx:
            services.capture(self.op)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("no longer be renewed", ctx.exception.message)
        self.op.refresh_from_db()
        self.assertEqual(self.op.status, OrderPayment.NEEDS_REVIEW)


class ReconciliationTests(TestCase):
    """Reconciliation matches provider transactions to local orders by invoice, as a set."""

    def setUp(self):
        from oscar.apps.order.models import Order

        self.user = User.objects.create_user(username="s3", password="pw-123456789")
        self.order = Order.objects.create(
            number="TEST-3", total_incl_tax=Decimal("10.00"), total_excl_tax=Decimal("10.00"),
            user=self.user,
        )
        from django.utils.dateparse import parse_datetime

        self.op = OrderPayment.objects.create(
            order=self.order, user=self.user, status=OrderPayment.CAPTURED,
            currency="USD", amount=Decimal("10.00"), invoice_id="OSC-INV-X",
            capture_id="CAP1", captured_value=Decimal("10.00"),
            capture_time=parse_datetime("2025-09-10T10:00:00+00:00"),
        )
        # An authorized-but-not-captured order created in-window -> should be 'unsettled'.
        self.order2 = Order.objects.create(
            number="TEST-4", total_incl_tax=Decimal("5.00"), total_excl_tax=Decimal("5.00"),
            user=self.user,
        )
        self.op2 = OrderPayment.objects.create(
            order=self.order2, user=self.user, status=OrderPayment.AUTHORIZED,
            currency="USD", amount=Decimal("5.00"), invoice_id="OSC-INV-Y",
        )
        self.op2.created = parse_datetime("2025-09-11T10:00:00+00:00")
        self.op2.save(update_fields=["created"])

    def tearDown(self):
        pp._client = None

    def test_set_match_and_classification(self):
        transport = StubTransport(token_response(), json_response(200, {
            "transaction_details": [
                # matches local OSC-INV-X (the capture)
                {"transaction_info": {"transaction_id": "T1", "invoice_id": "OSC-INV-X",
                                      "transaction_amount": {"currency_code": "USD", "value": "10.00"},
                                      "transaction_initiation_date": "2025-09-10T10:00:00+0000",
                                      "transaction_event_code": "T0006"}},
                # a refund tx on the SAME order (set-match keeps it with the order)
                {"transaction_info": {"transaction_id": "T2", "invoice_id": "OSC-INV-X",
                                      "transaction_amount": {"currency_code": "USD", "value": "-1.00"},
                                      "transaction_initiation_date": "2025-09-10T12:00:00+0000",
                                      "transaction_event_code": "T1107"}},
                # a provider tx with no matching local order -> provider-only
                {"transaction_info": {"transaction_id": "T3", "invoice_id": "OSC-INV-ZZZ",
                                      "transaction_amount": {"currency_code": "USD", "value": "3.00"},
                                      "transaction_initiation_date": "2025-09-10T13:00:00+0000",
                                      "transaction_event_code": "T0006"}},
            ],
            "total_pages": 1, "total_items": 3, "page": 1,
        }))
        pp._client = client_with(transport)
        from django.utils.dateparse import parse_datetime

        report = services.reconcile(parse_datetime("2025-09-01T00:00:00+00:00"),
                                    parse_datetime("2025-09-20T00:00:00+00:00"))
        self.assertEqual(report["counts"]["matched"], 1)
        self.assertEqual(len(report["matched"][0]["providerTransactions"]), 2)  # capture + refund
        self.assertEqual(report["counts"]["providerOnly"], 1)
        self.assertEqual(report["counts"]["unsettled"], 1)  # the authorized-not-captured order
        self.assertEqual(report["counts"]["localOnly"], 0)

    def test_out_of_window_provider_tx_is_narrowed_out(self):
        transport = StubTransport(token_response(), json_response(200, {
            "transaction_details": [
                {"transaction_info": {"transaction_id": "T9", "invoice_id": "OSC-INV-X",
                                      "transaction_amount": {"currency_code": "USD", "value": "10.00"},
                                      # OUTSIDE the caller's window (widened query returns it)
                                      "transaction_initiation_date": "2025-09-25T10:00:00+0000",
                                      "transaction_event_code": "T0006"}},
            ],
            "total_pages": 1, "total_items": 1, "page": 1,
        }))
        pp._client = client_with(transport)
        from django.utils.dateparse import parse_datetime

        report = services.reconcile(parse_datetime("2025-09-01T00:00:00+00:00"),
                                    parse_datetime("2025-09-20T00:00:00+00:00"))
        self.assertEqual(report["counts"]["providerTransactions"], 0)  # narrowed out
        self.assertEqual(report["counts"]["localOnly"], 1)  # local capture has no provider match
