"""Unit tests for the PayPal checkout app.

The SDK is exercised through its **transport seam** -- a stub transport passed to
a real ``PaypalClient`` -- so no network is touched and the real
request-building/response-decoding pipeline runs. A stub token source bypasses
the lazy OAuth fetch (except where we deliberately test a token failure).

These assert on *our boundary's* behaviour: that a PayPal 4xx becomes the right
domain exception, a decode failure is reported as "unknown", a challenge is
surfaced, and the service layer's idempotency/refund guards hold.
"""
import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from paypal import PaypalClient
from paypal.core import HttpResponse, OAuthToken

from . import exceptions as exc
from . import gateway, services
from .models import PayPalPayment, PayPalRefund, SavedCard

User = get_user_model()


# --------------------------------------------------------------------------- #
# SDK transport seam
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


class RaisingTransport:
    def __init__(self, error):
        self._error = error
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        raise self._error

    def close(self):
        pass


class StubTokenSource:
    def fetch(self, credentials):
        return OAuthToken(access_token="test-token", token_type="Bearer")


def json_response(status, body):
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def raw_response(status, text):
    return HttpResponse(status_code=status, headers={"content-type": "text/plain"},
                        content=text.encode())


def stub_client(*responses, transport=None):
    transport = transport or StubTransport(*responses)
    client = PaypalClient(
        oauth2={"client_id": "id", "client_secret": "secret"},
        oauth2_token_source=StubTokenSource(),
        custom_http_client=transport,
    )
    gateway._client = client
    return client, transport


AUTH_OK = {
    "status": "COMPLETED",
    "purchase_units": [{
        "payments": {"authorizations": [{
            "id": "AUTH-1", "status": "CREATED",
            "amount": {"currency_code": "USD", "value": "20.00"},
        }]}
    }],
}


class GatewayErrorTranslationTests(SimpleTestCase):
    def tearDown(self):
        gateway._client = None

    def test_authorize_success_extracts_authorization(self):
        stub_client(json_response(200, AUTH_OK))
        result = gateway.authorize_order(
            paypal_order_id="O-1", card_source={"vault_id": "V-1"}, request_id="r1"
        )
        self.assertEqual(result["authorization_id"], "AUTH-1")
        self.assertEqual(result["amount"], Decimal("20.00"))
        self.assertEqual(result["status"], "CREATED")

    def test_typed_422_becomes_provider_rejected(self):
        body = {"name": "UNPROCESSABLE_ENTITY", "message": "Business validation failed.",
                "debug_id": "d1", "details": [{"issue": "DUPLICATE_INVOICE_ID"}]}
        stub_client(json_response(422, body))
        with self.assertRaises(exc.ProviderRejected) as ctx:
            gateway.authorize_order(paypal_order_id="O", card_source={}, request_id="r")
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("Business validation", ctx.exception.message)

    def test_400_becomes_bad_request(self):
        body = {"name": "INVALID_REQUEST", "message": "Malformed.", "debug_id": "d"}
        stub_client(json_response(400, body))
        with self.assertRaises(exc.BadRequest):
            gateway.create_order(currency="USD", value=Decimal("5"), invoice_id="i",
                                 custom_id="c", request_id="r")

    def test_401_becomes_config_error_not_caller_fault(self):
        stub_client(json_response(401, {"name": "AUTHENTICATION_FAILURE",
                                        "message": "no", "debug_id": "d"}))
        with self.assertRaises(exc.ProviderConfigError):
            gateway.create_order(currency="USD", value=Decimal("5"), invoice_id="i",
                                 custom_id="c", request_id="r")

    def test_500_becomes_provider_unavailable(self):
        stub_client(raw_response(500, "boom"))
        with self.assertRaises(exc.ProviderUnavailable):
            gateway.create_order(currency="USD", value=Decimal("5"), invoice_id="i",
                                 custom_id="c", request_id="r")

    def test_decode_failure_on_success_is_unreadable(self):
        # 200 with a type mismatch (authorizations should be a list).
        bad = {"status": "COMPLETED", "purchase_units": "not-a-list"}
        stub_client(json_response(200, bad))
        with self.assertRaises(exc.ProviderUnreadable):
            gateway.authorize_order(paypal_order_id="O", card_source={}, request_id="r")

    def test_truncated_success_without_authorization_is_unreadable(self):
        stub_client(json_response(200, {"status": "COMPLETED", "purchase_units": []}))
        with self.assertRaises(exc.ProviderUnreadable):
            gateway.authorize_order(paypal_order_id="O", card_source={}, request_id="r")

    def test_payer_action_required_is_reported_not_worked_around(self):
        stub_client(json_response(200, {"status": "PAYER_ACTION_REQUIRED", "purchase_units": []}))
        with self.assertRaises(exc.PaymentActionRequired):
            gateway.authorize_order(paypal_order_id="O", card_source={}, request_id="r")

    def test_transport_failure_becomes_provider_unavailable(self):
        import httpx
        stub_client(transport=RaisingTransport(httpx.ConnectError("refused")))
        with self.assertRaises(exc.ProviderUnavailable):
            gateway.create_order(currency="USD", value=Decimal("5"), invoice_id="i",
                                 custom_id="c", request_id="r")

    def test_bad_credentials_is_config_error(self):
        # No token source: the token request itself fails with an OAuth error body.
        transport = StubTransport(json_response(401, {"error": "invalid_client"}))
        client = PaypalClient(oauth2={"client_id": "x", "client_secret": "y"},
                              custom_http_client=transport)
        gateway._client = client
        with self.assertRaises(exc.ProviderConfigError):
            gateway.create_order(currency="USD", value=Decimal("5"), invoice_id="i",
                                 custom_id="c", request_id="r")

    def test_capture_extracts_fee_and_net(self):
        body = {"id": "CAP-1", "status": "COMPLETED",
                "amount": {"currency_code": "USD", "value": "20.00"},
                "seller_receivable_breakdown": {
                    "gross_amount": {"currency_code": "USD", "value": "20.00"},
                    "paypal_fee": {"currency_code": "USD", "value": "1.01"},
                    "net_amount": {"currency_code": "USD", "value": "18.99"}}}
        stub_client(json_response(201, body))
        result = gateway.capture_authorization(authorization_id="A", request_id="r")
        self.assertEqual(result["captured"], Decimal("20.00"))
        self.assertEqual(result["fee"], Decimal("1.01"))
        self.assertEqual(result["net"], Decimal("18.99"))

    def test_money_str_formats_to_cents(self):
        self.assertEqual(gateway.money_str(Decimal("5")), "5.00")
        self.assertEqual(gateway.money_str(Decimal("10.1")), "10.10")

    def test_authorize_sends_card_in_body(self):
        _, transport = stub_client(json_response(200, AUTH_OK))
        gateway.authorize_order(paypal_order_id="O-9",
                                card_source={"vault_id": "V-2"}, request_id="rid")
        req = transport.requests[-1]
        self.assertTrue(req.url.endswith("/v2/checkout/orders/O-9/authorize"))
        self.assertEqual(req.body.value["payment_source"]["card"]["vault_id"], "V-2")
        self.assertEqual(req.headers["paypal-request-id"], "rid")


class ServiceGuardTests(TestCase):
    """Service-layer logic with the gateway stubbed (no network)."""

    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="pw")
        self._orig = {}

    def tearDown(self):
        for name, fn in self._orig.items():
            setattr(gateway, name, fn)
        gateway._client = None

    def _patch_gateway(self, **fakes):
        for name, fn in fakes.items():
            self._orig[name] = getattr(gateway, name)
            setattr(gateway, name, fn)

    def _place(self, total="20.00"):
        payment = PayPalPayment.objects.create(
            order=_ensure_order(self.user, Decimal(total)),
            status=PayPalPayment.CAPTURED, currency="USD",
            order_total=Decimal(total), captured_amount=Decimal(total),
            capture_id="CAP-X", request_id_base="base",
        )
        return payment

    def test_refund_idempotency_same_key_returns_existing(self):
        payment = self._place("20.00")
        calls = {"n": 0}

        def fake_refund(**kw):
            calls["n"] += 1
            return {"refund_id": f"R{calls['n']}", "status": "COMPLETED",
                    "amount": Decimal("5.00"), "currency": "USD"}

        self._patch_gateway(refund_capture=fake_refund)
        _, r1 = services.create_refund(order=payment.order, amount="5.00", idempotency_key="k1")
        _, r2 = services.create_refund(order=payment.order, amount="5.00", idempotency_key="k1")
        self.assertEqual(r1.id, r2.id)
        self.assertEqual(calls["n"], 1)  # PayPal called once
        self.assertEqual(PayPalRefund.objects.filter(payment=payment).count(), 1)

    def test_two_distinct_partial_refunds_allowed(self):
        payment = self._place("20.00")

        def fake_refund(**kw):
            return {"refund_id": "R", "status": "COMPLETED",
                    "amount": Decimal("5.00"), "currency": "USD"}

        self._patch_gateway(refund_capture=fake_refund)
        services.create_refund(order=payment.order, amount="5.00", idempotency_key="a")
        services.create_refund(order=payment.order, amount="5.00", idempotency_key="b")
        payment.refresh_from_db()
        self.assertEqual(payment.amount_refunded, Decimal("10.00"))
        self.assertEqual(payment.status, PayPalPayment.PARTIALLY_REFUNDED)

    def test_over_refund_is_rejected(self):
        payment = self._place("20.00")

        def fake_refund(**kw):
            return {"refund_id": "R", "status": "COMPLETED",
                    "amount": Decimal("25.00"), "currency": "USD"}

        self._patch_gateway(refund_capture=fake_refund)
        with self.assertRaises(exc.Conflict):
            services.create_refund(order=payment.order, amount="25.00", idempotency_key="c")


# Minimal helper to build a real Oscar order for the service tests.
def _ensure_order(user, total):
    import uuid

    from oscar.core.loading import get_model
    Order = get_model("order", "Order")
    number = "T" + uuid.uuid4().hex[:10]
    return Order.objects.create(
        number=number, user=user, currency="USD",
        total_incl_tax=total, total_excl_tax=total,
    )
