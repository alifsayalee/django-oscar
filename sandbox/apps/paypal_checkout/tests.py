"""Unit tests for the PayPal checkout integration.

The SDK is faked at its transport seam (``custom_http_client``) — a real
``PaypalClient`` with no network — so the real request-building and error pipeline
run. We assert on our own boundary's behaviour (which ``ProviderError`` each
failure kind becomes) and on the request the SDK actually built, per
python-testing.
"""
import json
from decimal import Decimal

import httpx
from django.test import SimpleTestCase, TestCase

from paypal import PaypalClient
from paypal.core import HttpRequest, HttpResponse

from . import money
from .exceptions import (
    ApiClientError,
    ProviderConfigError,
    ProviderRejected,
    ProviderUnavailable,
    ProviderUnreadable,
)
from .gateway import PayPalGateway
from .models import PayPalPayment, PayPalRefund, SavedCard


# --- test doubles ----------------------------------------------------------

def json_response(status, body):
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def empty_response(status):
    return HttpResponse(status_code=status, headers={})


def token_response():
    return json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})


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


def gateway_with(*responses):
    transport = StubTransport(*responses)
    client = PaypalClient(oauth2={"client_id": "id", "client_secret": "sec"},
                          base_url="https://api-m.sandbox.paypal.com",
                          custom_http_client=transport)
    return PayPalGateway(client=client), transport


def gateway_raising(error):
    transport = RaisingTransport(error, token_response())
    client = PaypalClient(oauth2={"client_id": "id", "client_secret": "sec"},
                          base_url="https://api-m.sandbox.paypal.com",
                          custom_http_client=transport)
    return PayPalGateway(client=client)


# --- money -----------------------------------------------------------------

class MoneyTests(SimpleTestCase):
    def test_usd_two_places(self):
        self.assertEqual(money.format_amount(Decimal("10"), "USD"), "10.00")

    def test_jpy_zero_places(self):
        self.assertEqual(money.format_amount(Decimal("1000"), "JPY"), "1000")

    def test_kwd_three_places(self):
        self.assertEqual(money.format_amount(Decimal("1.2344"), "KWD"), "1.234")

    def test_amounts_equal_across_scale(self):
        self.assertTrue(money.amounts_equal("10.0", Decimal("10.00"), "USD"))
        self.assertFalse(money.amounts_equal("10.01", Decimal("10.00"), "USD"))


# --- gateway error boundary ------------------------------------------------

class GatewayErrorTests(SimpleTestCase):
    def test_typed_422_becomes_provider_rejected(self):
        gw, _ = gateway_with(token_response(), json_response(
            422, {"name": "UNPROCESSABLE_ENTITY", "message": "Card was declined",
                  "debug_id": "abc123"}))
        with self.assertRaises(ProviderRejected) as ctx:
            gw.capture("AUTH1", request_id="r")
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("declined", ctx.exception.message)
        self.assertEqual(ctx.exception.debug_id, "abc123")

    def test_401_is_our_config_error(self):
        gw, _ = gateway_with(token_response(), json_response(
            401, {"name": "AUTHENTICATION_FAILURE", "message": "no", "debug_id": "d1"}))
        with self.assertRaises(ProviderConfigError):
            gw.capture("AUTH1", request_id="r")

    def test_429_is_service_unavailable(self):
        gw, _ = gateway_with(token_response(), json_response(
            429, {"name": "RATE_LIMIT", "message": "slow down"}))
        with self.assertRaises(ProviderUnavailable) as ctx:
            gw.capture("AUTH1", request_id="r")
        self.assertEqual(ctx.exception.status_code, 503)

    def test_500_raw_error_is_our_fault(self):
        gw, _ = gateway_with(token_response(), json_response(500, {"oops": True}))
        with self.assertRaises(Exception) as ctx:
            gw.capture("AUTH1", request_id="r")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 502)

    def test_never_sent_is_known_outcome(self):
        gw = gateway_raising(httpx.ConnectError("refused"))
        with self.assertRaises(ProviderUnavailable) as ctx:
            gw.capture("AUTH1", request_id="r")
        self.assertEqual((ctx.exception.status_code, ctx.exception.outcome_unknown),
                         (502, False))

    def test_read_timeout_is_unknown_outcome(self):
        gw = gateway_raising(httpx.ReadTimeout("no reply"))
        with self.assertRaises(ProviderUnavailable) as ctx:
            gw.capture("AUTH1", request_id="r")
        self.assertEqual((ctx.exception.status_code, ctx.exception.outcome_unknown),
                         (504, True))

    def test_unsent_and_unknown_are_distinguishable(self):
        unsent = gateway_raising(httpx.ConnectError("refused"))
        unknown = gateway_raising(httpx.ReadTimeout("no reply"))
        with self.assertRaises(ProviderUnavailable) as a:
            unsent.capture("A", request_id="r")
        with self.assertRaises(ProviderUnavailable) as b:
            unknown.capture("A", request_id="r")
        self.assertNotEqual(a.exception.status_code, b.exception.status_code)

    def test_bad_credentials_is_config_error(self):
        # No token_response queued: the token fetch itself gets the 401 body.
        gw, _ = gateway_with(json_response(401, {"error": "invalid_client"}))
        with self.assertRaises(ProviderConfigError):
            gw.capture("AUTH1", request_id="r")

    def test_unreadable_success_body_is_not_success(self):
        # 200 with a type mismatch where a list is expected -> decode failure.
        gw, _ = gateway_with(token_response(), json_response(
            200, {"purchase_units": "not-a-list"}))
        with self.assertRaises(ProviderUnreadable):
            gw.authorize_order("ORDER1", card_request=_card(), request_id="r")

    def test_void_empty_body_is_success(self):
        # void returns 204 empty; the SDK success decoder would raise ValueError,
        # which for void means success.
        gw, _ = gateway_with(token_response(), empty_response(204))
        self.assertIsNone(gw.void("AUTH1", request_id="r"))


class GatewayRequestTests(SimpleTestCase):
    def test_create_order_builds_authorize_body(self):
        gw, transport = gateway_with(token_response(), json_response(
            201, {"id": "PPORDER1", "status": "CREATED"}))
        gw.create_order(currency="USD", value="15.00", invoice_id="INV-100042",
                        custom_id="100042", request_id="100042-create")
        req = transport.last_request
        self.assertEqual(req.method, "POST")
        self.assertTrue(req.url.endswith("/v2/checkout/orders"))
        body = req.body.value
        self.assertEqual(body["intent"], "AUTHORIZE")
        self.assertEqual(body["purchase_units"][0]["amount"]["currency_code"], "USD")
        self.assertEqual(body["purchase_units"][0]["amount"]["value"], "15.00")
        self.assertEqual(body["purchase_units"][0]["invoice_id"], "INV-100042")
        self.assertEqual(body["purchase_units"][0]["custom_id"], "100042")
        self.assertEqual(req.headers["paypal-request-id"], "100042-create")

    def test_vault_retries_on_500_then_succeeds(self):
        gw, transport = gateway_with(
            token_response(),
            json_response(500, {"name": "INTERNAL_SERVER_ERROR",
                                "message": "An internal server error has occurred.",
                                "debug_id": "d500"}),
            json_response(201, {"id": "TOK1", "customer": {"id": "oscar-cust-1"},
                                "payment_source": {"card": {"last_digits": "1111",
                                                            "brand": "VISA"}}}),
        )
        token = gw.create_payment_token(customer_id="oscar-cust-1", card_request=_vault_card(),
                                        request_id="vault-1")
        self.assertEqual(token.id, "TOK1")
        # token fetch + failed create + retried create
        self.assertEqual(len(transport.requests), 3)
        # same request id reused across the retry (idempotency)
        self.assertEqual(transport.requests[1].headers["paypal-request-id"], "vault-1")
        self.assertEqual(transport.requests[2].headers["paypal-request-id"], "vault-1")


def _card():
    from paypal.models import CardRequest
    return CardRequest(number="4111111111111111", expiry="2030-01", security_code="123")


def _vault_card():
    from paypal.models import PaymentTokenRequestCard
    return PaymentTokenRequestCard(number="4111111111111111", expiry="2030-01",
                                   security_code="123")


# --- service-level idempotency / guards (DB, gateway faked) -----------------

class _FakeRefundGateway:
    """A gateway stand-in whose refund echoes a COMPLETED refund."""

    def __init__(self):
        self.calls = []

    def refund(self, capture_id, *, currency, value, request_id, full=False):
        self.calls.append((capture_id, value, request_id))

        class _R:
            id = "REF-" + request_id
            status = "COMPLETED"
        return _R()


class RefundServiceTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from oscar.test.factories import create_order
        self.user = get_user_model().objects.create_user(
            username="buyer", password="password123", email="b@example.com")
        self.order = create_order(user=self.user)
        self.payment = PayPalPayment.objects.create(
            order=self.order, currency="USD", amount=Decimal("20.00"),
            status=PayPalPayment.CAPTURED, capture_id="CAP1",
            captured_value=Decimal("20.00"), authorization_id="AUTH1",
        )

    def _patch_gateway(self):
        from . import services
        fake = _FakeRefundGateway()
        services.PayPalGateway = lambda *a, **k: fake
        return fake

    def tearDown(self):
        from . import services
        from .gateway import PayPalGateway as RealGateway
        services.PayPalGateway = RealGateway

    def test_partial_then_remaining_refund(self):
        from . import services
        self._patch_gateway()
        _, r1 = services.refund_order(self.user, self.order.number,
                                      amount="5.00", idempotency_key="k1")
        self.assertEqual(r1.amount, Decimal("5.00"))
        payment, r2 = services.refund_order(self.user, self.order.number,
                                            amount="15.00", idempotency_key="k2")
        self.assertEqual(payment.status, PayPalPayment.REFUNDED)
        self.assertEqual(payment.refunded_total, Decimal("20.00"))

    def test_over_refund_is_rejected(self):
        from . import services
        self._patch_gateway()
        with self.assertRaises(ApiClientError):
            services.refund_order(self.user, self.order.number,
                                  amount="25.00", idempotency_key="k1")

    def test_repeated_key_does_not_refund_twice(self):
        from . import services
        fake = self._patch_gateway()
        services.refund_order(self.user, self.order.number, amount="5.00", idempotency_key="k1")
        services.refund_order(self.user, self.order.number, amount="5.00", idempotency_key="k1")
        # Only one actual PayPal refund call was made for the repeated key.
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(PayPalRefund.objects.filter(payment=self.payment).count(), 1)

    def test_two_distinct_keys_are_two_refunds(self):
        from . import services
        fake = self._patch_gateway()
        services.refund_order(self.user, self.order.number, amount="5.00", idempotency_key="k1")
        services.refund_order(self.user, self.order.number, amount="5.00", idempotency_key="k2")
        self.assertEqual(len(fake.calls), 2)

    def test_cannot_see_another_users_order(self):
        from django.contrib.auth import get_user_model
        from . import services
        self._patch_gateway()
        other = get_user_model().objects.create_user(
            username="other", password="password123", email="o@example.com")
        with self.assertRaises(ApiClientError) as ctx:
            services.refund_order(other, self.order.number, amount="5.00", idempotency_key="k1")
        self.assertEqual(ctx.exception.status_code, 404)


class _FakeSearchGateway:
    """Returns a fixed SearchResponse from search_transactions."""

    def __init__(self, response):
        self._response = response

    def search_transactions(self, *, start_date, end_date, page, page_size=500):
        return self._response


class ReconciliationTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from oscar.test.factories import create_order
        self.user = get_user_model().objects.create_user(
            "recon", "recon@example.com", "password123")
        self.order = create_order(user=self.user)
        self.payment = PayPalPayment.objects.create(
            order=self.order, currency="USD", amount=Decimal("20.00"),
            status=PayPalPayment.CAPTURED, capture_id="CAP1",
            captured_value=Decimal("20.00"), paypal_order_id="PPO1",
            paypal_invoice_id="INV-MATCH",
            provider_time=None,
        )

    def _search_response(self, *infos):
        from paypal.models import (SearchResponse, TransactionDetails,
                                   TransactionInformation, Money)
        details = []
        for inv, txn, amt in infos:
            details.append(TransactionDetails(transaction_info=TransactionInformation(
                transaction_id=txn, invoice_id=inv,
                transaction_amount=Money(currency_code="USD", value=amt),
                transaction_status="S")))
        return SearchResponse(transaction_details=details, total_pages=1, page=1)

    def _patch(self, response):
        from . import services
        services.PayPalGateway = lambda *a, **k: _FakeSearchGateway(response)

    def tearDown(self):
        from . import services
        from .gateway import PayPalGateway as RealGateway
        services.PayPalGateway = RealGateway

    def test_matches_provider_transaction_to_local_order(self):
        from django.utils import timezone
        from datetime import timedelta
        from . import services
        # Put the payment's provider clock inside the window.
        now = timezone.now()
        self.payment.provider_time = now - timedelta(minutes=5)
        self.payment.save()
        self._patch(self._search_response(
            ("INV-MATCH", "TXN1", "20.00"),      # matches our order
            ("INV-OTHER", "TXN2", "99.00"),      # provider-only
        ))
        report = services.reconcile(now - timedelta(hours=1), now + timedelta(hours=1))
        self.assertEqual(report["counts"]["matched"], 1)
        self.assertEqual(report["counts"]["providerOnly"], 1)
        self.assertEqual(report["matched"][0]["orderId"], str(self.order.number))
        self.assertEqual(report["providerOnly"][0]["invoiceId"], "INV-OTHER")

    def test_local_order_without_provider_record_is_app_only(self):
        from django.utils import timezone
        from datetime import timedelta
        from . import services
        now = timezone.now()
        self.payment.provider_time = now - timedelta(minutes=5)
        self.payment.save()
        self._patch(self._search_response())  # provider reports nothing (lag)
        report = services.reconcile(now - timedelta(hours=1), now + timedelta(hours=1))
        self.assertEqual(report["counts"]["appOnly"], 1)
        self.assertEqual(report["counts"]["matched"], 0)


class SavedCardOwnershipTests(TestCase):
    def test_delete_foreign_card_is_not_found(self):
        from django.contrib.auth import get_user_model
        from . import services
        u1 = get_user_model().objects.create_user("u1", "u1@example.com", "password123")
        u2 = get_user_model().objects.create_user("u2", "u2@example.com", "password123")
        card = SavedCard.objects.create(user=u1, paypal_token_id="TOK", last_digits="1111")
        with self.assertRaises(ApiClientError) as ctx:
            services.delete_saved_card(u2, card.id)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertTrue(SavedCard.objects.filter(pk=card.id).exists())
