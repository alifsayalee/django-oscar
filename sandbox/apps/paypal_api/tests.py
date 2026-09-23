"""Offline tests for the PayPal gateway boundary.

These fake the SDK's transport (its documented test seam), so they exercise the
real request-building and error-mapping pipeline without any network. They
target the tricky SDK-facing behaviour -- the empty-body void, the
error-translation ladder, and response extraction -- rather than re-testing the
SDK itself. The live end-to-end flows are verified separately against the
sandbox.
"""
import json

import httpx
from django.test import SimpleTestCase

from paypal import PaypalClient
from paypal.core import HttpResponse

from .errors import (
    PayPalConfigError,
    PaymentChallengeRequired,
    PayPalRejected,
    PayPalUnavailable,
    PayPalUnreadable,
)
from .gateway import PayPalGateway


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


class RaisingTransport(StubTransport):
    def __init__(self, error, *responses):
        super().__init__(*responses)
        self._error = error

    def send(self, request):
        if self._responses:
            return super().send(request)
        self.requests.append(request)
        raise self._error


def gateway_with(*responses):
    transport = StubTransport(*responses)
    client = PaypalClient(
        base_url="https://api-m.sandbox.paypal.com",
        custom_http_client=transport,
        oauth2={"client_id": "id", "client_secret": "secret"},
    )
    return PayPalGateway(client=client), transport


def gateway_raising(error, *responses):
    transport = RaisingTransport(error, token_response(), *responses)
    client = PaypalClient(
        base_url="https://api-m.sandbox.paypal.com",
        custom_http_client=transport,
        oauth2={"client_id": "id", "client_secret": "secret"},
    )
    return PayPalGateway(client=client)


CARD = {"number": "4111111111111111", "expiry": "2030-01", "security_code": "123"}
ERROR_422 = {
    "name": "UNPROCESSABLE_ENTITY",
    "message": "failed business validation",
    "debug_id": "abc123",
    "details": [{"issue": "INSTRUMENT_DECLINED", "description": "The card was declined"}],
}


def _order_with_authorization(status="COMPLETED", auth_status="CREATED"):
    return {
        "id": "ORDER123",
        "status": status,
        "purchase_units": [
            {
                "payments": {
                    "authorizations": [
                        {
                            "id": "AUTH123",
                            "status": auth_status,
                            "amount": {"currency_code": "USD", "value": "24.99"},
                            "expiration_time": "2026-10-22T07:59:50Z",
                        }
                    ]
                }
            }
        ],
    }


class VoidPaymentTests(SimpleTestCase):
    def test_empty_204_body_is_success(self):
        # A successful void answers 204 with an empty body the SDK cannot decode.
        gateway, _ = gateway_with(token_response(), empty_response(204))
        self.assertIsNone(gateway.void("AUTH123"))  # no exception == voided

    def test_failure_is_translated(self):
        gateway, _ = gateway_with(token_response(), json_response(422, ERROR_422))
        with self.assertRaises(PayPalRejected) as ctx:
            gateway.void("AUTH123")
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("INSTRUMENT_DECLINED", ctx.exception.message)


class ErrorLadderTests(SimpleTestCase):
    def test_401_is_config_error_not_caller_fault(self):
        gateway, _ = gateway_with(token_response(), json_response(401, ERROR_422))
        with self.assertRaises(PayPalConfigError):
            gateway.authorize_order(amount_value="1.00", currency="USD", card=CARD)

    def test_422_is_caller_rejection_with_issue(self):
        gateway, _ = gateway_with(token_response(), json_response(422, ERROR_422))
        with self.assertRaises(PayPalRejected) as ctx:
            gateway.authorize_order(amount_value="1.00", currency="USD", card=CARD)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("INSTRUMENT_DECLINED", ctx.exception.message)

    def test_bad_credentials_is_config_error(self):
        # The first request is the token fetch; a 401 there is a config fault.
        gateway, _ = gateway_with(json_response(401, {"error": "invalid_client"}))
        with self.assertRaises(PayPalConfigError):
            gateway.authorize_order(amount_value="1.00", currency="USD", card=CARD)

    def test_decode_failure_is_unreadable(self):
        # A 2xx whose body does not match the schema -> outcome unknown.
        gateway, _ = gateway_with(
            token_response(), json_response(200, {"purchase_units": "not-a-list"})
        )
        with self.assertRaises(PayPalUnreadable):
            gateway.authorize_order(amount_value="1.00", currency="USD", card=CARD)

    def test_connect_error_is_known_outcome(self):
        gateway = gateway_raising(httpx.ConnectError("refused"))
        with self.assertRaises(PayPalUnavailable) as ctx:
            gateway.authorize_order(amount_value="1.00", currency="USD", card=CARD)
        self.assertFalse(ctx.exception.outcome_unknown)  # never sent
        self.assertEqual(ctx.exception.http_status, 502)

    def test_read_timeout_is_unknown_outcome(self):
        gateway = gateway_raising(httpx.ReadTimeout("no reply"))
        with self.assertRaises(PayPalUnavailable) as ctx:
            gateway.authorize_order(amount_value="1.00", currency="USD", card=CARD)
        self.assertTrue(ctx.exception.outcome_unknown)  # may have landed
        self.assertEqual(ctx.exception.http_status, 504)


class ExtractionTests(SimpleTestCase):
    def test_authorization_extracted_from_create_response(self):
        gateway, transport = gateway_with(
            token_response(), json_response(201, _order_with_authorization())
        )
        result = gateway.authorize_order(
            amount_value="24.99", currency="USD", card=CARD, custom_id="100001"
        )
        self.assertEqual(result["paypal_order_id"], "ORDER123")
        self.assertEqual(result["authorization"]["id"], "AUTH123")
        self.assertEqual(result["authorization"]["status"], "CREATED")
        self.assertEqual(result["authorization"]["amount_value"], "24.99")
        # No fallback authorize_order call was needed: one API call after the token.
        self.assertEqual(len(transport.requests), 2)

    def test_payer_action_required_is_challenge_stop(self):
        gateway, _ = gateway_with(
            token_response(),
            json_response(201, _order_with_authorization(status="PAYER_ACTION_REQUIRED")),
        )
        with self.assertRaises(PaymentChallengeRequired):
            gateway.authorize_order(amount_value="24.99", currency="USD", card=CARD)

    def test_capture_reports_fee_and_net(self):
        body = {
            "id": "CAP123",
            "status": "COMPLETED",
            "amount": {"currency_code": "USD", "value": "24.99"},
            "seller_receivable_breakdown": {
                "gross_amount": {"currency_code": "USD", "value": "24.99"},
                "paypal_fee": {"currency_code": "USD", "value": "1.14"},
                "net_amount": {"currency_code": "USD", "value": "23.85"},
            },
            "update_time": "2026-09-23T08:15:09Z",
        }
        gateway, _ = gateway_with(token_response(), json_response(201, body))
        result = gateway.capture("AUTH123")
        self.assertEqual(result["capture_id"], "CAP123")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["fee"], "1.14")
        self.assertEqual(result["net"], "23.85")

    def test_vault_returns_safe_display_only(self):
        body = {
            "id": "VAULT123",
            "customer": {"id": "CUST123"},
            "payment_source": {
                "card": {
                    "brand": "VISA",
                    "last_digits": "1111",
                    "expiry": "2030-01",
                    "name": "John Doe",
                }
            },
        }
        gateway, _ = gateway_with(token_response(), json_response(201, body))
        result = gateway.vault_card(card=CARD)
        self.assertEqual(result["vault_id"], "VAULT123")
        self.assertEqual(result["customer_id"], "CUST123")
        self.assertEqual(result["last_digits"], "1111")
        self.assertEqual(result["brand"], "VISA")
        # The full PAN never appears in what we persist/return.
        self.assertNotIn("4111111111111111", json.dumps(result))


class RefundTests(SimpleTestCase):
    def test_refund_returns_id_and_status(self):
        body = {"id": "REF123", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "5.00"}}
        gateway, _ = gateway_with(token_response(), json_response(201, body))
        result = gateway.refund("CAP123", amount_value="5.00", currency="USD", request_id="rf-x")
        self.assertEqual(result["refund_id"], "REF123")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["amount_value"], "5.00")
