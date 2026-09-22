"""Tests for the PayPal integration.

The gateway is exercised through the SDK's transport seam (a stub satisfying
the transport protocol), so the real request-building/decoding pipeline runs
without any network — covering the four SDK failure kinds. Service-level
idempotency/ownership is tested with a fake gateway over real Oscar orders.
"""
from __future__ import annotations

import json
from decimal import Decimal

import httpx
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from paypal import Client
from paypal.core import HttpResponse

from . import money, services
from .exceptions import (
    PaymentConfigError,
    PaymentConflict,
    PaymentError,
    PaymentProviderError,
    PaymentRejected,
    PaymentUnavailable,
    PaymentUnreadable,
    ReauthorizationNeeded,
)
from .gateway import PayPalGateway
from .models import PayPalPayment, PayPalRefund, SavedCard


# --------------------------------------------------------------------------
# transport stub
# --------------------------------------------------------------------------
class StubTransport:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("no queued response")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def close(self):
        pass


class RaisingTransport:
    def __init__(self, exc):
        self._exc = exc
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        raise self._exc

    def close(self):
        pass


def json_response(status, body):
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def raw_response(status, text, content_type="text/html"):
    return HttpResponse(
        status_code=status, headers={"content-type": content_type}, content=text.encode()
    )


def token_response():
    return json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})


def gateway_with(*responses, transport=None):
    transport = transport or StubTransport(token_response(), *responses)
    client = Client(
        base_url="https://api-m.sandbox.paypal.com",
        custom_http_client=transport,
        oauth2={"client_id": "id", "client_secret": "secret"},
    )
    return PayPalGateway(client=client), transport


ERROR_BODY = {
    "name": "UNPROCESSABLE_ENTITY",
    "message": "The request failed business validation.",
    "debug_id": "abc123",
    "details": [{"issue": "TRANSACTION_REFUSED", "description": "The request was refused"}],
}


# --------------------------------------------------------------------------
# money
# --------------------------------------------------------------------------
class MoneyTests(SimpleTestCase):
    def test_usd_two_decimals(self):
        self.assertEqual(money.format_amount(Decimal("15"), "USD"), "15.00")
        self.assertEqual(money.format_amount(Decimal("15.005"), "USD"), "15.01")

    def test_zero_decimal_currency(self):
        self.assertEqual(money.format_amount(Decimal("1000"), "JPY"), "1000")

    def test_three_decimal_currency(self):
        self.assertEqual(money.format_amount(Decimal("1.2344"), "KWD"), "1.234")
        self.assertEqual(money.format_amount(Decimal("1.2346"), "KWD"), "1.235")

    def test_parse_roundtrip(self):
        self.assertEqual(money.parse_amount("15.00", "USD"), Decimal("15.00"))


# --------------------------------------------------------------------------
# gateway error boundary
# --------------------------------------------------------------------------
class GatewayErrorTests(SimpleTestCase):
    def _authorize(self, gw):
        return gw.authorize(
            amount=Decimal("15.00"), currency="USD", order_number="100001",
            request_id="auth-100001-0",
            card={"number": "4111111111111111", "expiry": "2030-01", "security_code": "123"},
        )

    def test_typed_error_becomes_payment_rejected(self):
        gw, t = gateway_with(json_response(422, ERROR_BODY))
        with self.assertRaises(PaymentRejected) as ctx:
            self._authorize(gw)
        self.assertEqual(ctx.exception.http_status, 422)
        self.assertIn("TRANSACTION_REFUSED", ctx.exception.message)
        # token request first, then the operation
        self.assertEqual(len(t.requests), 2)

    def test_conflict_409_becomes_payment_conflict(self):
        # 409 is a mapped typed-error status on capture (not on create_order).
        gw, _ = gateway_with(json_response(409, ERROR_BODY))
        with self.assertRaises(PaymentConflict):
            gw.capture("AUTH1", request_id="capture-AUTH1")

    def test_raw_error_becomes_provider_error(self):
        gw, _ = gateway_with(raw_response(503, "<html>upstream</html>"))
        with self.assertRaises(PaymentProviderError) as ctx:
            self._authorize(gw)
        self.assertEqual(ctx.exception.http_status, 502)

    def test_transport_failure_becomes_unavailable(self):
        gw, _ = gateway_with(transport=RaisingTransport(httpx.ConnectError("refused")))
        with self.assertRaises(PaymentUnavailable):
            self._authorize(gw)

    def test_bad_credentials_becomes_config_error(self):
        # No token_response queued: the token endpoint answers 401.
        gw, _ = gateway_with(transport=StubTransport(json_response(401, {"error": "invalid_client"})))
        with self.assertRaises(PaymentConfigError):
            self._authorize(gw)

    def test_missing_order_id_is_unreadable(self):
        gw, _ = gateway_with(json_response(201, {"status": "COMPLETED"}))  # no id
        with self.assertRaises(PaymentUnreadable):
            self._authorize(gw)

    def test_authorization_extracted_from_create_response(self):
        body = {
            "id": "ORDER1", "status": "COMPLETED",
            "purchase_units": [
                {"payments": {"authorizations": [{"id": "AUTH1", "status": "CREATED"}]}}
            ],
        }
        gw, _ = gateway_with(json_response(201, body))
        result = self._authorize(gw)
        self.assertEqual(result["authorization_id"], "AUTH1")
        self.assertEqual(result["authorization_status"], "CREATED")

    def test_capture_reads_fee_and_net(self):
        body = {
            "id": "CAP1", "status": "COMPLETED",
            "seller_receivable_breakdown": {
                "gross_amount": {"currency_code": "USD", "value": "15.00"},
                "paypal_fee": {"currency_code": "USD", "value": "0.88"},
                "net_amount": {"currency_code": "USD", "value": "14.12"},
            },
        }
        gw, _ = gateway_with(json_response(201, body))
        result = gw.capture("AUTH1", request_id="capture-AUTH1")
        self.assertEqual(result["capture_id"], "CAP1")
        self.assertEqual(result["paypal_fee"], Decimal("0.88"))
        self.assertEqual(result["net_amount"], Decimal("14.12"))

    def test_capture_expired_signals_reauthorization(self):
        body = {
            "name": "UNPROCESSABLE_ENTITY", "message": "expired", "debug_id": "d",
            "details": [{"issue": "AUTHORIZATION_EXPIRED", "description": "expired"}],
        }
        gw, _ = gateway_with(json_response(422, body))
        with self.assertRaises(ReauthorizationNeeded):
            gw.capture("AUTH1", request_id="capture-AUTH1")

    def test_void_returns_status(self):
        gw, _ = gateway_with(json_response(200, {"id": "AUTH1", "status": "VOIDED"}))
        self.assertEqual(gw.void("AUTH1", request_id="void-AUTH1"), "VOIDED")

    def test_search_paginates_all_pages(self):
        def page(n, total):
            return json_response(200, {
                "total_pages": total, "page": n,
                "transaction_details": [
                    {"transaction_info": {
                        "transaction_id": f"T{n}",
                        "transaction_amount": {"currency_code": "USD", "value": "15.00"},
                        "transaction_status": "S", "invoice_id": "100001"}}
                ],
            })
        gw, t = gateway_with(page(1, 2), page(2, 2))
        result = gw.search_transactions("2026-01-01T00:00:00-0000", "2026-02-01T00:00:00-0000")
        self.assertEqual(len(result["transactions"]), 2)
        self.assertFalse(result["truncated"])
        # token + 2 pages
        self.assertEqual(len(t.requests), 3)


# --------------------------------------------------------------------------
# service idempotency / ownership (fake gateway, real Oscar orders)
# --------------------------------------------------------------------------
class FakeGateway:
    def __init__(self):
        self.calls = []
        self.authorize_error = None
        self.capture_error = None

    def authorize(self, **kw):
        self.calls.append(("authorize", kw))
        if self.authorize_error:
            raise self.authorize_error
        return {"paypal_order_id": "PPO", "authorization_id": "AUTH1", "authorization_status": "CREATED"}

    def capture(self, authorization_id, **kw):
        self.calls.append(("capture", authorization_id))
        if self.capture_error:
            raise self.capture_error
        return {"capture_id": "CAP1", "status": "COMPLETED",
                "gross_amount": Decimal("15.00"), "paypal_fee": Decimal("0.88"),
                "net_amount": Decimal("14.12")}

    def reauthorize(self, authorization_id, **kw):
        self.calls.append(("reauthorize", authorization_id))
        return {"authorization_id": "AUTH2", "status": "CREATED"}

    def void(self, authorization_id, **kw):
        self.calls.append(("void", authorization_id))
        return "VOIDED"

    def refund(self, capture_id, **kw):
        self.calls.append(("refund", capture_id, kw.get("amount")))
        return {"refund_id": f"REF{len(self.calls)}", "status": "COMPLETED"}


class ServiceFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from oscar.test.factories import create_order  # local import: test-only dep
        cls.create_order = staticmethod(create_order)

    def setUp(self):
        User = get_user_model()
        self.shopper = User.objects.create_user("shopper", "s@example.com", "pw")
        self.other = User.objects.create_user("other", "o@example.com", "pw")
        self.gw = FakeGateway()

    def _payment(self, user=None):
        from oscar.test.factories import create_order
        order = create_order(user=user or self.shopper)
        return PayPalPayment.objects.create(
            order=order, status=PayPalPayment.AWAITING_PAYMENT,
            currency="USD", amount=order.total_incl_tax,
        )

    def test_pay_then_double_pay_is_idempotent(self):
        p = self._payment()
        card = {"number": "4111111111111111", "expiry": "2030-01"}
        services.pay(p.id, user=self.shopper, card=card, gateway=self.gw)
        services.pay(p.id, user=self.shopper, card=card, gateway=self.gw)
        p.refresh_from_db()
        self.assertEqual(p.status, PayPalPayment.AUTHORIZED)
        # only one authorize call reached the gateway
        self.assertEqual(sum(1 for c in self.gw.calls if c[0] == "authorize"), 1)

    def test_pay_failure_bumps_attempt_for_retry(self):
        p = self._payment()
        self.gw.authorize_error = PaymentRejected("TRANSACTION_REFUSED", provider_status=422)
        with self.assertRaises(PaymentRejected):
            services.pay(p.id, user=self.shopper, card={"number": "4", "expiry": "2030-01"}, gateway=self.gw)
        p.refresh_from_db()
        self.assertEqual(p.pay_attempts, 1)
        self.assertEqual(p.status, PayPalPayment.AWAITING_PAYMENT)

    def test_cannot_pay_another_users_order(self):
        p = self._payment(user=self.other)
        with self.assertRaises(PaymentError) as ctx:
            services.pay(p.id, user=self.shopper, card={"number": "4", "expiry": "2030-01"}, gateway=self.gw)
        self.assertEqual(ctx.exception.http_status, 404)

    def test_fulfil_captures_and_is_idempotent(self):
        p = self._payment()
        services.pay(p.id, user=self.shopper, card={"number": "4", "expiry": "2030-01"}, gateway=self.gw)
        services.fulfil(p.id, gateway=self.gw)
        services.fulfil(p.id, gateway=self.gw)
        p.refresh_from_db()
        self.assertEqual(p.status, PayPalPayment.CAPTURED)
        self.assertEqual(p.paypal_fee, Decimal("0.88"))
        self.assertEqual(sum(1 for c in self.gw.calls if c[0] == "capture"), 1)

    def test_fulfil_renews_stale_authorization(self):
        class ReauthGateway(FakeGateway):
            def __init__(self):
                super().__init__()
                self._captures = 0

            def capture(self, authorization_id, **kw):
                self._captures += 1
                self.calls.append(("capture", authorization_id))
                if self._captures == 1:
                    raise ReauthorizationNeeded()  # stale on first try
                return {"capture_id": "CAP1", "status": "COMPLETED",
                        "gross_amount": Decimal("15.00"), "paypal_fee": Decimal("0.88"),
                        "net_amount": Decimal("14.12")}

        gw = ReauthGateway()
        p = self._payment()
        services.pay(p.id, user=self.shopper, card={"number": "4", "expiry": "2030-01"}, gateway=gw)
        services.fulfil(p.id, gateway=gw)
        p.refresh_from_db()
        self.assertEqual(p.status, PayPalPayment.CAPTURED)
        self.assertEqual(p.authorization_id, "AUTH2")  # renewed auth captured
        self.assertTrue(any(c[0] == "reauthorize" for c in gw.calls))

    def test_cancel_voids_and_is_idempotent(self):
        p = self._payment()
        services.pay(p.id, user=self.shopper, card={"number": "4", "expiry": "2030-01"}, gateway=self.gw)
        services.cancel(p.id, gateway=self.gw)
        services.cancel(p.id, gateway=self.gw)
        p.refresh_from_db()
        self.assertEqual(p.status, PayPalPayment.CANCELLED)
        self.assertEqual(sum(1 for c in self.gw.calls if c[0] == "void"), 1)

    def test_refund_idempotency_and_cap(self):
        p = self._payment()
        services.pay(p.id, user=self.shopper, card={"number": "4", "expiry": "2030-01"}, gateway=self.gw)
        services.fulfil(p.id, gateway=self.gw)
        # first partial refund
        _, r1 = services.refund(p.id, user=self.shopper, amount="5.00", idempotency_key="k1", gateway=self.gw)
        # repeat same key -> same refund, no second gateway refund
        _, r1b = services.refund(p.id, user=self.shopper, amount="5.00", idempotency_key="k1", gateway=self.gw)
        self.assertEqual(r1.id, r1b.id)
        self.assertEqual(PayPalRefund.objects.filter(payment=p).count(), 1)
        # over-refund rejected (never beyond captured)
        with self.assertRaises(PaymentError):
            services.refund(p.id, user=self.shopper, amount="100.00", idempotency_key="k2", gateway=self.gw)
        p.refresh_from_db()
        self.assertEqual(p.status, PayPalPayment.PARTIALLY_REFUNDED)

    def test_cannot_refund_unfulfilled_order(self):
        p = self._payment()
        services.pay(p.id, user=self.shopper, card={"number": "4", "expiry": "2030-01"}, gateway=self.gw)
        with self.assertRaises(PaymentConflict):
            services.refund(p.id, user=self.shopper, amount="1.00", idempotency_key="k", gateway=self.gw)


class SavedCardTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.shopper = User.objects.create_user("shopper", "s@example.com", "pw")
        self.other = User.objects.create_user("other", "o@example.com", "pw")

    def test_save_and_delete_card(self):
        class FakeVault:
            def vault_card(self, **kw):
                return {"vault_id": "V1", "brand": "VISA", "last4": "1111", "expiry": "2030-01"}

            def delete_vault_card(self, vault_id):
                return True

        gw = FakeVault()
        card = services.save_card(self.shopper, card={"number": "4"}, gateway=gw)
        self.assertEqual(card.last4, "1111")
        self.assertEqual(SavedCard.objects.filter(user=self.shopper).count(), 1)
        services.delete_saved_card(card.id, user=self.shopper, gateway=gw)
        self.assertEqual(SavedCard.objects.filter(user=self.shopper).count(), 0)

    def test_cannot_delete_another_users_card(self):
        card = SavedCard.objects.create(user=self.other, vault_id="V9", brand="VISA", last4="1111")

        class FakeVault:
            def delete_vault_card(self, vault_id):
                raise AssertionError("should not be called")

        with self.assertRaises(PaymentError) as ctx:
            services.delete_saved_card(card.id, user=self.shopper, gateway=FakeVault())
        self.assertEqual(ctx.exception.http_status, 404)
