"""Gateway tests: the request the SDK builds, and the error boundary. No network."""

from decimal import Decimal
from unittest import mock

import httpx
from django.test import SimpleTestCase

from apps.paypal_checkout import errors
from apps.paypal_checkout.services import gateway

from .stub import client_with, json_response

CARD = {"number": "4111111111111111", "expiry": "2030-01", "security_code": "123"}

ORDER_OK = {
    "id": "ORDER1",
    "status": "COMPLETED",
    "purchase_units": [
        {
            "payments": {
                "authorizations": [
                    {
                        "id": "AUTH1",
                        "status": "CREATED",
                        "expiration_time": "2030-01-01T00:00:00Z",
                        "amount": {"currency_code": "USD", "value": "15.00"},
                    }
                ]
            }
        }
    ],
}

CAPTURE_OK = {
    "id": "CAP1",
    "status": "COMPLETED",
    "amount": {"currency_code": "USD", "value": "15.00"},
    "seller_receivable_breakdown": {
        "gross_amount": {"currency_code": "USD", "value": "15.00"},
        "paypal_fee": {"currency_code": "USD", "value": "0.88"},
        "net_amount": {"currency_code": "USD", "value": "14.12"},
    },
}

ERROR_422 = {
    "name": "UNPROCESSABLE_ENTITY",
    "message": "Business validation failed.",
    "debug_id": "dbg",
    "details": [{"issue": "DUPLICATE_INVOICE_ID", "description": "already used"}],
}


class GatewayTests(SimpleTestCase):
    def _patch(self, client):
        p = mock.patch.object(gateway, "get_client", return_value=client)
        p.start()
        self.addCleanup(p.stop)

    def test_authorize_builds_intent_authorize_and_returns_ids(self):
        client, transport = client_with(json_response(201, ORDER_OK))
        self._patch(client)

        result = gateway.authorize_with_card(
            CARD, Decimal("15.00"), "USD", "INV-1", "ORDER-1", "auth-1"
        )

        self.assertEqual(result["authorization_id"], "AUTH1")
        self.assertEqual(result["authorization_status"], "CREATED")
        # The request the SDK actually built:
        req = transport.last_request
        self.assertEqual(req.method, "POST")
        self.assertTrue(req.url.endswith("/v2/checkout/orders"))
        self.assertEqual(req.body.value["intent"], "AUTHORIZE")
        self.assertEqual(
            req.body.value["purchase_units"][0]["amount"]["value"], "15.00"
        )
        self.assertEqual(req.headers["paypal-request-id"], "auth-1")

    def test_capture_reports_fee_and_net(self):
        client, _ = client_with(json_response(201, CAPTURE_OK))
        self._patch(client)

        result = gateway.capture("AUTH1", "cap-1")

        self.assertEqual(result["capture_id"], "CAP1")
        self.assertEqual(result["paypal_fee"], Decimal("0.88"))
        self.assertEqual(result["net_amount"], Decimal("14.12"))
        self.assertEqual(result["amount"], Decimal("15.00"))

    def test_void_uses_return_representation(self):
        client, transport = client_with(
            json_response(200, {"id": "AUTH1", "status": "VOIDED"})
        )
        self._patch(client)

        status = gateway.void("AUTH1")

        self.assertEqual(status, "VOIDED")
        self.assertEqual(transport.last_request.headers["prefer"], "return=representation")

    def test_422_becomes_rejected_with_message(self):
        client, _ = client_with(json_response(422, ERROR_422))
        self._patch(client)

        with self.assertRaises(errors.PayPalRejected) as ctx:
            gateway.authorize_with_card(CARD, Decimal("1.00"), "USD", "i", "c", "r")

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("DUPLICATE_INVOICE_ID", ctx.exception.message)

    def test_401_is_config_error_not_caller_fault(self):
        client, _ = client_with(json_response(401, {"name": "X", "message": "no", "debug_id": "d"}))
        self._patch(client)

        with self.assertRaises(errors.PayPalConfigError) as ctx:
            gateway.capture("AUTH1", "cap-1")
        self.assertEqual(ctx.exception.status_code, 502)

    def test_decode_failure_is_unreadable_outcome_unknown(self):
        # A 2xx whose body is the wrong type for a required member.
        bad = {"id": "CAP1", "amount": {"currency_code": "USD", "value": "15.00"},
               "seller_receivable_breakdown": {"gross_amount": "not-an-object"}}
        client, _ = client_with(json_response(201, bad))
        self._patch(client)

        with self.assertRaises(errors.PayPalUnreadable) as ctx:
            gateway.capture("AUTH1", "cap-1")
        self.assertTrue(ctx.exception.outcome_unknown)

    def test_connection_error_is_never_sent(self):
        client, _ = client_with(httpx.ConnectError("refused"))
        self._patch(client)

        with self.assertRaises(errors.PayPalUnavailable) as ctx:
            gateway.capture("AUTH1", "cap-1")
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertFalse(ctx.exception.outcome_unknown)

    def test_read_timeout_is_unknown_outcome(self):
        client, _ = client_with(httpx.ReadTimeout("no reply"))
        self._patch(client)

        with self.assertRaises(errors.PayPalUnavailable) as ctx:
            gateway.capture("AUTH1", "cap-1")
        self.assertEqual(ctx.exception.status_code, 504)
        self.assertTrue(ctx.exception.outcome_unknown)
