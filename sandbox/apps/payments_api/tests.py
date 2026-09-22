"""Unit tests for the PayPal gateway using the SDK's transport seam.

These fake the SDK transport (``custom_http_client``) so no network call is made
and the *real* request-building pipeline is exercised. They cover the parts a
live sandbox test cannot assert cheaply: error translation, that ``void`` asks
for a representation, and that idempotency keys reach the wire.

The full happy-path flows are verified end-to-end against the live sandbox by
``scripts/verify_paypal.py`` (see the app README).
"""
import json

from django.test import SimpleTestCase

from paypal import PaypalClient
from paypal.core import HttpResponse

from . import gateway
from .gateway import PayPalError


def _json_response(status, body):
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def _token_response():
    return _json_response(
        200, {"access_token": "test-token", "token_type": "Bearer", "expires_in": 3600}
    )


class StubTransport:
    """Satisfies the SDK sync transport protocol (send + close)."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        if isinstance(self._responses[0], Exception):
            raise self._responses.pop(0)
        return self._responses.pop(0)

    def close(self):
        pass

    @property
    def last_request(self):
        return self.requests[-1] if self.requests else None


class GatewayTestCase(SimpleTestCase):
    def setUp(self):
        self._saved_client = gateway._client

    def tearDown(self):
        gateway._client = self._saved_client

    def _install(self, *responses):
        transport = StubTransport(*responses)
        gateway._client = PaypalClient(
            custom_http_client=transport,
            oauth2={"client_id": "cid", "client_secret": "secret"},
        )
        return transport

    # ------------------------------------------------------------------ #
    def test_create_authorized_order_builds_correct_request(self):
        order_body = {
            "id": "ORDER123",
            "status": "COMPLETED",
            "purchase_units": [
                {"payments": {"authorizations": [{"id": "AUTH123", "status": "CREATED"}]}}
            ],
        }
        transport = self._install(_token_response(), _json_response(201, order_body))

        order = gateway.create_authorized_order(
            currency="USD",
            amount="24.99",
            order_number="100001",
            card={"number": "4111111111111111", "expiry": "2030-12",
                  "security_code": "123", "name": "Test Buyer"},
            request_id="auth-100001",
        )
        auth = gateway.extract_authorization(order)
        self.assertEqual(auth.id, "AUTH123")

        req = transport.last_request
        self.assertEqual(req.method, "POST")
        self.assertTrue(req.url.endswith("/v2/checkout/orders"))
        payload = req.body.value
        self.assertEqual(payload["intent"], "AUTHORIZE")
        self.assertEqual(payload["purchase_units"][0]["amount"]["value"], "24.99")
        self.assertEqual(payload["purchase_units"][0]["amount"]["currency_code"], "USD")
        self.assertEqual(
            payload["payment_source"]["card"]["number"], "4111111111111111"
        )
        # Idempotency key reaches the wire (lowercased header key).
        self.assertEqual(req.headers["paypal-request-id"], "auth-100001")

    def test_typed_error_becomes_paypal_error_with_issues(self):
        self._install(
            _token_response(),
            _json_response(
                422,
                {
                    "name": "UNPROCESSABLE_ENTITY",
                    "message": "Business validation failed.",
                    "debug_id": "abc",
                    "details": [{"issue": "TRANSACTION_REFUSED"}],
                },
            ),
        )
        with self.assertRaises(PayPalError) as ctx:
            gateway.create_authorized_order(
                currency="USD", amount="1.00", order_number="1",
                card={"number": "4000000000000002", "expiry": "2030-12"},
                request_id="x",
            )
        err = ctx.exception
        self.assertEqual(err.http_status, 422)
        self.assertIn("TRANSACTION_REFUSED", err.issues)

    def test_void_requests_a_representation(self):
        # The Prefer header must ask for a representation, else PayPal answers 204
        # (empty body) and the SDK cannot decode it.
        transport = self._install(
            _token_response(),
            _json_response(200, {"id": "AUTH1", "status": "VOIDED"}),
        )
        pa = gateway.void_authorization("AUTH1")
        self.assertEqual(str(pa.status), "VOIDED")
        self.assertEqual(
            transport.last_request.headers["prefer"], "return=representation"
        )

    def test_refund_sends_idempotency_key(self):
        transport = self._install(
            _token_response(),
            _json_response(201, {"id": "REF1", "status": "COMPLETED"}),
        )
        refund = gateway.refund_capture(
            "CAP1", currency="USD", amount="5.00", request_id="idem-key-1"
        )
        self.assertEqual(refund.id, "REF1")
        self.assertEqual(
            transport.last_request.headers["paypal-request-id"], "idem-key-1"
        )
        self.assertEqual(transport.last_request.body.value["amount"]["value"], "5.00")

    def test_bad_credentials_is_config_error(self):
        # Token endpoint rejects the credentials → configuration fault, not a
        # per-operation rejection.
        self._install(_json_response(401, {"error": "invalid_client"}))
        with self.assertRaises(PayPalError) as ctx:
            gateway.create_authorized_order(
                currency="USD", amount="1.00", order_number="1",
                card={"number": "4111111111111111", "expiry": "2030-12"},
                request_id="x",
            )
        self.assertTrue(ctx.exception.config)

    def test_transport_failure_is_unknown_outcome(self):
        import httpx

        self._install(_token_response(), httpx.ConnectError("refused"))
        with self.assertRaises(PayPalError) as ctx:
            gateway.capture_authorization("AUTH1", "cap-1")
        self.assertEqual(ctx.exception.http_status, 502)

    def test_delete_token_reads_204(self):
        transport = self._install(
            _token_response(), HttpResponse(status_code=204, headers={})
        )
        self.assertTrue(gateway.delete_vault_token("TOK1"))
        self.assertTrue(transport.last_request.url.endswith("/v3/vault/payment-tokens/TOK1"))
