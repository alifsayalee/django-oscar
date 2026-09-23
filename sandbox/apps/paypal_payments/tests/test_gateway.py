"""The PayPal boundary: error ladder, status mapping, configuration, pagination."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from django.test import SimpleTestCase

from apps.paypal_payments import gateway as gw

from .support import (CONFIG, StubTransport, json_response, order_body, paypal_error,
                      token_response)

CARD = gw.CardInput(number="4111111111111111", expiry="2030-12", security_code="123", name="T")


def gateway_with(*responses, config=CONFIG):
    transport = StubTransport(token_response(), *responses)
    return gw.PayPalGateway(gw.build_client(config, transport=transport)), transport


def authorize(gateway):
    return gateway.authorize(amount=Decimal("10.00"), currency="USD", custom_id="1:" + "a" * 32,
                             request_id="req-1", card=CARD)


class ErrorLadderTests(SimpleTestCase):
    def test_refused_connection_is_known_not_sent(self):
        gateway, _ = gateway_with(httpx.ConnectError("refused"))
        with self.assertRaises(gw.ProviderError) as ctx:
            authorize(gateway)
        self.assertEqual((ctx.exception.http_status, ctx.exception.outcome_unknown), (502, False))

    def test_read_timeout_is_unknown_outcome(self):
        gateway, _ = gateway_with(httpx.ReadTimeout("no reply"))
        with self.assertRaises(gw.ProviderError) as ctx:
            authorize(gateway)
        self.assertEqual((ctx.exception.http_status, ctx.exception.outcome_unknown), (504, True))

    def test_unreadable_success_body_is_unknown(self):
        gateway, _ = gateway_with(json_response(200, {"id": 123, "status": ["not", "a", "status"]}))
        with self.assertRaises(gw.ProviderError) as ctx:
            authorize(gateway)
        self.assertTrue(ctx.exception.outcome_unknown)

    def test_truncated_success_body_is_unknown(self):
        gateway, _ = gateway_with(json_response(200, {"id": "PPORDER1", "status": "COMPLETED"}))
        with self.assertRaises(gw.ProviderUnreadable):
            authorize(gateway)

    def test_rejection_carries_paypal_issue(self):
        gateway, _ = gateway_with(paypal_error(422, "INSTRUMENT_DECLINED", "The card was declined."))
        with self.assertRaises(gw.ProviderError) as ctx:
            authorize(gateway)
        exc = ctx.exception
        self.assertEqual((exc.http_status, exc.outcome_unknown, exc.issues), (422, False, ("INSTRUMENT_DECLINED",)))
        self.assertIn("declined", exc.message)

    def test_our_auth_problem_is_not_the_callers(self):
        gateway, _ = gateway_with(paypal_error(403, "PERMISSION_DENIED"))
        with self.assertRaises(gw.ProviderError) as ctx:
            authorize(gateway)
        self.assertEqual(ctx.exception.http_status, 502)

    def test_rejected_credentials_are_a_configuration_error(self):
        transport = StubTransport(json_response(401, {"error": "invalid_client"}))
        gateway = gw.PayPalGateway(gw.build_client(CONFIG, transport=transport))
        with self.assertRaises(gw.ProviderError) as ctx:
            authorize(gateway)
        self.assertEqual((ctx.exception.http_status, ctx.exception.outcome_unknown), (502, False))
        self.assertEqual(len(transport.requests), 1)  # the operation was never sent

    def test_server_error_may_have_landed(self):
        gateway, _ = gateway_with(json_response(500, {"oops": True}))
        with self.assertRaises(gw.ProviderError) as ctx:
            authorize(gateway)
        self.assertTrue(ctx.exception.outcome_unknown)


class AuthorizeTests(SimpleTestCase):
    def test_request_shape(self):
        gateway, transport = gateway_with(json_response(201, order_body("10.00")))
        result = authorize(gateway)
        req = transport.api_requests[0]
        self.assertTrue(req.url.endswith("/v2/checkout/orders"))
        self.assertEqual(req.headers["paypal-request-id"], "req-1")
        self.assertEqual(req.headers["prefer"], "return=representation")
        body = req.body.value
        self.assertEqual(body["intent"], "AUTHORIZE")
        self.assertEqual(body["purchase_units"][0]["amount"], {"currency_code": "USD", "value": "10.00"})
        self.assertEqual(body["payment_source"]["card"]["number"], "4111111111111111")
        self.assertEqual((result.state, result.authorization_id, result.amount),
                         ("authorized", "AUTH1", Decimal("10.00")))

    def test_saved_card_sends_only_vault_id(self):
        gateway, transport = gateway_with(json_response(201, order_body("10.00")))
        gateway.authorize(amount=Decimal("10"), currency="USD", custom_id="x", request_id="r", vault_id="tok1")
        self.assertEqual(transport.api_requests[0].body.value["payment_source"], {"card": {"vault_id": "tok1"}})

    def test_denied_is_failed_and_unlisted_status_is_unknown(self):
        for status, state in (("DENIED", "failed"), ("PENDING", "pending"), ("SOMETHING_NEW", "unknown")):
            gateway, _ = gateway_with(json_response(201, order_body("10.00", auth_status=status)))
            self.assertEqual(authorize(gateway).state, state)

    def test_payer_action_required_stops(self):
        body = order_body("10.00", order_status="PAYER_ACTION_REQUIRED")
        gateway, _ = gateway_with(json_response(200, body))
        with self.assertRaises(gw.PayerActionRequired):
            authorize(gateway)


class ConfigTests(SimpleTestCase):
    def test_base_url_override_moves_every_call_including_token(self):
        config = gw.PayPalConfig(client_id="a", client_secret="b", environment="live", currency="USD",
                                 base_url="https://paypal.test.invalid")
        gateway, transport = gateway_with(json_response(201, order_body("10.00")), config=config)
        authorize(gateway)
        self.assertTrue(all(r.url.startswith("https://paypal.test.invalid/") for r in transport.requests))
        self.assertTrue(transport.requests[0].url.endswith("/v1/oauth2/token"))

    def test_sandbox_environment_uses_sandbox_host(self):
        self.assertEqual(CONFIG.resolved_base_url(), "https://api-m.sandbox.paypal.com")

    def test_unknown_environment_without_base_url_refuses(self):
        config = gw.PayPalConfig(client_id="a", client_secret="b", environment="live", currency="USD")
        with self.assertRaises(gw.ConfigurationError):
            config.validate()

    def test_missing_credentials_refuse_to_build(self):
        with self.assertRaises(gw.ConfigurationError) as ctx:
            gw.build_client(gw.PayPalConfig(client_id="", client_secret="", environment="sandbox", currency="USD"))
        self.assertIn("PAYPAL_CLIENT_ID", str(ctx.exception))

    def test_secret_not_in_repr(self):
        self.assertNotIn("test-secret", repr(CONFIG))

    def test_currency_exponent(self):
        self.assertEqual(gw.format_amount(Decimal("10"), "USD"), "10.00")
        self.assertEqual(gw.format_amount(Decimal("1000"), "JPY"), "1000")
        self.assertEqual(gw.format_amount(Decimal("1.234"), "KWD"), "1.234")


class SearchTests(SimpleTestCase):
    def txn(self, txn_id, when):
        return {"transaction_info": {"transaction_id": txn_id, "transaction_initiation_date": when,
                                     "transaction_amount": {"currency_code": "USD", "value": "5.00"}}}

    def test_walks_every_page_and_splits_long_ranges(self):
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        end = start + timedelta(days=40)
        gateway, transport = gateway_with(
            json_response(200, {"transaction_details": [self.txn("A", "2026-07-02T00:00:00Z")],
                                "page": 1, "total_pages": 2}),
            json_response(200, {"transaction_details": [self.txn("B", "2026-07-03T00:00:00Z")],
                                "page": 2, "total_pages": 2}),
            json_response(200, {"transaction_details": [self.txn("C", "2026-08-09T00:00:00Z"),
                                                        self.txn("OUT", "2026-08-11T00:00:01Z")],
                                "page": 1, "total_pages": 1}),
        )
        result = gateway.search_transactions(start, end)
        self.assertEqual([t.transaction_id for t in result.transactions], ["A", "B", "C"])  # narrowed to range
        self.assertFalse(result.truncated)
        urls = [r.url for r in transport.api_requests]
        self.assertEqual(len(urls), 3)
        self.assertIn("page=2", urls[1])
        self.assertIn("start_date=2026-08-01T00%3A00%3A00Z", urls[2])

    def test_page_cap_is_reported(self):
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        pages = [json_response(200, {"transaction_details": [self.txn("X%d" % i, "2026-07-02T00:00:00Z")],
                                     "page": i + 1, "total_pages": 999}) for i in range(gw.SEARCH_MAX_PAGES)]
        gateway, _ = gateway_with(*pages)
        result = gateway.search_transactions(start, start + timedelta(days=2))
        self.assertTrue(result.truncated)
