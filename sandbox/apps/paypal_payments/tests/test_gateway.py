"""
Gateway tests: a real SDK client over a stub transport, so the SDK builds
and decodes real requests and responses without any network.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from pay_pal_server_sdk import PayPalServerSdkClient
from pay_pal_server_sdk.core import HttpRequest

from apps.paypal_payments import gateway
from apps.paypal_payments.gateway import CardDetails, ProviderError

BASE = 'https://paypal.test'


@dataclass
class StubResponse:
    status_code: int = 200
    headers: dict = field(default_factory=dict)
    chunks: list = field(default_factory=list)
    url: str = BASE
    closed: bool = False

    def iter_bytes(self, chunk_size):
        yield from self.chunks
        self.close()

    def read(self):
        self.close()
        return b''.join(self.chunks)

    def close(self):
        self.closed = True


def json_response(status, body):
    return StubResponse(status_code=status, headers={'content-type': 'application/json'},
                        chunks=[json.dumps(body).encode()])


def token_response():
    return json_response(200, {'access_token': 't', 'token_type': 'Bearer', 'expires_in': 3600})


class StubTransport:
    def __init__(self, *responses, error=None):
        self._responses = list(responses)
        self._error = error
        self.requests: list[HttpRequest] = []

    def send(self, request):
        self.requests.append(request)
        if not self._responses and self._error is not None:
            raise self._error
        return self._responses.pop(0)

    def close(self):
        pass


def card():
    return CardDetails(number='4111111111111111', expiry='2030-12', security_code='123',
                       name='Test Shopper')


def authorization(status='CREATED', value='12.34'):
    return {'id': 'AUTH-1', 'status': status, 'amount': {'currency_code': 'USD', 'value': value},
            'create_time': '2026-09-29T17:50:51Z', 'expiration_time': '2026-10-28T17:50:51Z'}


class GatewayTestCase(SimpleTestCase):
    def use(self, *responses, error=None):
        transport = StubTransport(token_response(), *responses, error=error)
        client = PayPalServerSdkClient(
            base_url=BASE, custom_http_client=transport, retry_options=0,
            oauth2={'client_id': 'id', 'client_secret': 'secret'})
        previous = gateway.install_client(client)
        self.addCleanup(lambda: gateway.install_client(previous))
        self.addCleanup(client.close)
        return transport


class AuthorizeTests(GatewayTestCase):
    def test_single_step_card_authorization(self):
        transport = self.use(json_response(201, {
            'id': 'ORDER-1', 'status': 'COMPLETED',
            'payment_source': {'card': {'last_digits': '1111', 'brand': 'VISA'}},
            'purchase_units': [{'payments': {'authorizations': [authorization()]}}]}))

        result = gateway.authorize_order_total(
            reference='100001', description='Order 100001', amount=Decimal('12.34'),
            currency='USD', request_id='req-1', card=card())

        self.assertEqual(result.authorization_id, 'AUTH-1')
        self.assertEqual(result.amount, Decimal('12.34'))
        self.assertEqual((result.card_brand, result.card_last_digits), ('VISA', '1111'))
        self.assertEqual(result.expires_at, datetime(2026, 10, 28, 17, 50, 51, tzinfo=timezone.utc))
        request = transport.requests[-1]
        self.assertEqual(request.method, 'POST')
        self.assertTrue(request.url.endswith('/v2/checkout/orders'))
        self.assertEqual(request.headers['paypal-request-id'], 'req-1')
        self.assertEqual(request.headers['prefer'], 'return=representation')
        body = request.body.value
        self.assertEqual(body['intent'], 'AUTHORIZE')
        self.assertEqual(body['purchase_units'][0]['amount'], {'currency_code': 'USD', 'value': '12.34'})
        self.assertEqual(body['purchase_units'][0]['custom_id'], '100001')
        self.assertEqual(body['payment_source']['card']['number'], '4111111111111111')

    def test_token_request_uses_the_configured_base_url(self):
        transport = self.use(json_response(201, {
            'id': 'ORDER-1', 'status': 'COMPLETED',
            'purchase_units': [{'payments': {'authorizations': [authorization()]}}]}))
        gateway.authorize_order_total(reference='1', description='x', amount=Decimal('12.34'),
                                      currency='USD', request_id='r', card=card())
        self.assertEqual(transport.requests[0].url, BASE + '/v1/oauth2/token')

    def test_vaulted_card_sends_vault_id_and_stored_credential(self):
        transport = self.use(json_response(201, {
            'id': 'ORDER-1', 'status': 'COMPLETED',
            'purchase_units': [{'payments': {'authorizations': [authorization()]}}]}))
        gateway.authorize_order_total(reference='1', description='x', amount=Decimal('12.34'),
                                      currency='USD', request_id='r', vault_id='TOKEN-1')
        source = transport.requests[-1].body.value['payment_source']['card']
        self.assertEqual(source['vault_id'], 'TOKEN-1')
        self.assertEqual(source['stored_credential']['payment_initiator'], 'CUSTOMER')
        self.assertNotIn('number', source)

    def test_approved_order_is_authorized_in_a_second_call(self):
        transport = self.use(
            json_response(201, {'id': 'ORDER-1', 'status': 'APPROVED'}),
            json_response(201, {'id': 'ORDER-1', 'status': 'COMPLETED',
                                'purchase_units': [{'payments': {'authorizations': [authorization()]}}]}))
        result = gateway.authorize_order_total(reference='1', description='x', amount=Decimal('12.34'),
                                               currency='USD', request_id='r', card=card())
        self.assertEqual(result.authorization_id, 'AUTH-1')
        self.assertTrue(transport.requests[-1].url.endswith('/v2/checkout/orders/ORDER-1/authorize'))

    def test_payer_action_required_is_refused(self):
        self.use(json_response(201, {'id': 'ORDER-1', 'status': 'PAYER_ACTION_REQUIRED'}))
        with self.assertRaises(ProviderError) as ctx:
            gateway.authorize_order_total(reference='1', description='x', amount=Decimal('1.00'),
                                          currency='USD', request_id='r', card=card())
        self.assertEqual((ctx.exception.status_code, ctx.exception.code), (422, 'payer_action_required'))

    def test_paypal_rejection_carries_the_issue(self):
        self.use(json_response(422, {
            'name': 'UNPROCESSABLE_ENTITY', 'message': 'failed', 'debug_id': 'dbg',
            'details': [{'issue': 'CARD_EXPIRED', 'description': 'The card is expired.'}]}))
        with self.assertRaises(ProviderError) as ctx:
            gateway.authorize_order_total(reference='1', description='x', amount=Decimal('1.00'),
                                          currency='USD', request_id='r', card=card())
        error = ctx.exception
        self.assertEqual((error.status_code, error.issue, error.debug_id), (422, 'CARD_EXPIRED', 'dbg'))
        self.assertFalse(error.outcome_unknown)
        self.assertNotIn('4111', error.message)


class FailureKindTests(GatewayTestCase):
    def capture(self):
        return gateway.capture_authorization('AUTH-1', amount=Decimal('1.00'), currency='USD',
                                             request_id='r')

    def test_unsent_is_not_the_same_failure_as_unknown(self):
        self.use(error=httpx.ConnectError('refused'))
        with self.assertRaises(ProviderError) as unsent:
            self.capture()
        self.use(error=httpx.ReadTimeout('no reply'))
        with self.assertRaises(ProviderError) as unknown:
            self.capture()
        self.assertEqual((unsent.exception.status_code, unsent.exception.outcome_unknown), (502, False))
        self.assertEqual((unknown.exception.status_code, unknown.exception.outcome_unknown), (504, True))

    def test_unreadable_success_body_is_an_unknown_outcome(self):
        self.use(StubResponse(status_code=200, headers={'content-type': 'application/json'},
                              chunks=[b'<html>']))
        with self.assertRaises(ProviderError) as ctx:
            self.capture()
        self.assertTrue(ctx.exception.outcome_unknown)

    def test_truncated_success_body_is_an_unknown_outcome(self):
        self.use(json_response(201, {}))
        with self.assertRaises(ProviderError) as ctx:
            self.capture()
        self.assertEqual(ctx.exception.code, 'paypal_incomplete_response')
        self.assertTrue(ctx.exception.outcome_unknown)

    def test_server_error_on_a_write_is_an_unknown_outcome(self):
        self.use(json_response(500, {'name': 'INTERNAL_SERVER_ERROR', 'message': 'x', 'debug_id': 'd'}))
        with self.assertRaises(ProviderError) as ctx:
            self.capture()
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertTrue(ctx.exception.outcome_unknown)

    def test_bad_credentials_is_a_configuration_error(self):
        transport = StubTransport(json_response(401, {'error': 'invalid_client'}))
        client = PayPalServerSdkClient(base_url=BASE, custom_http_client=transport, retry_options=0,
                                       oauth2={'client_id': 'id', 'client_secret': 'bad'})
        previous = gateway.install_client(client)
        self.addCleanup(lambda: gateway.install_client(previous))
        with self.assertRaises(ProviderError) as ctx:
            self.capture()
        self.assertEqual((ctx.exception.status_code, ctx.exception.code),
                         (502, 'paypal_credentials_rejected'))


class CaptureRefundVaultTests(GatewayTestCase):
    def test_capture_reports_fee_and_net(self):
        transport = self.use(json_response(201, {
            'id': 'CAP-1', 'status': 'COMPLETED', 'amount': {'currency_code': 'USD', 'value': '12.34'},
            'seller_receivable_breakdown': {
                'gross_amount': {'currency_code': 'USD', 'value': '12.34'},
                'paypal_fee': {'currency_code': 'USD', 'value': '0.92'},
                'net_amount': {'currency_code': 'USD', 'value': '11.42'}}}))
        result = gateway.capture_authorization('AUTH-1', amount=Decimal('12.34'), currency='USD',
                                               request_id='cap-req')
        self.assertEqual((result.capture_id, result.paypal_fee, result.net_amount),
                         ('CAP-1', Decimal('0.92'), Decimal('11.42')))
        request = transport.requests[-1]
        self.assertTrue(request.url.endswith('/v2/payments/authorizations/AUTH-1/capture'))
        self.assertEqual(request.headers['paypal-request-id'], 'cap-req')
        self.assertEqual(request.body.value['amount'], {'currency_code': 'USD', 'value': '12.34'})
        self.assertTrue(request.body.value['final_capture'])

    def test_refund_sends_explicit_amount(self):
        transport = self.use(json_response(201, {
            'id': 'REF-1', 'status': 'COMPLETED', 'amount': {'currency_code': 'USD', 'value': '5.00'}}))
        result = gateway.refund_capture('CAP-1', amount=Decimal('5'), currency='USD',
                                        request_id='ref-req', custom_id='100001')
        self.assertEqual((result.refund_id, result.status), ('REF-1', 'COMPLETED'))
        body = transport.requests[-1].body.value
        self.assertEqual(body['amount'], {'currency_code': 'USD', 'value': '5.00'})
        self.assertEqual(transport.requests[-1].headers['paypal-request-id'], 'ref-req')

    def test_vault_card_returns_safe_description(self):
        transport = self.use(json_response(200, {
            'id': 'TOKEN-1', 'customer': {'id': 'CUST-1'},
            'payment_source': {'card': {'brand': 'VISA', 'last_digits': '1111', 'expiry': '2030-12'}}}))
        vaulted = gateway.vault_card(card(), customer_id='CUST-1', request_id='v')
        self.assertEqual((vaulted.token_id, vaulted.customer_id, vaulted.last_digits),
                         ('TOKEN-1', 'CUST-1', '1111'))
        self.assertEqual(transport.requests[-1].body.value['customer'], {'id': 'CUST-1'})
        self.assertNotIn('4111111111111111', repr(card()))

    def test_delete_treats_missing_token_as_removed(self):
        self.use(StubResponse(status_code=204))
        gateway.delete_vaulted_card('TOKEN-1')
        self.use(json_response(404, {'name': 'RESOURCE_NOT_FOUND', 'message': 'x', 'debug_id': 'd'}))
        gateway.delete_vaulted_card('TOKEN-1')

    def test_delete_failure_is_reported(self):
        self.use(json_response(403, {'name': 'NOT_AUTHORIZED', 'message': 'x', 'debug_id': 'd'}))
        with self.assertRaises(ProviderError):
            gateway.delete_vaulted_card('TOKEN-1')


class SearchTests(GatewayTestCase):
    def page(self, ids, total_pages, page):
        return json_response(200, {
            'transaction_details': [{'transaction_info': {
                'transaction_id': i, 'transaction_event_code': 'T0006',
                'transaction_amount': {'currency_code': 'USD', 'value': '1.00'}}} for i in ids],
            'total_pages': total_pages, 'page': page,
            'last_refreshed_datetime': '2026-09-29T10:00:00Z'})

    def test_whole_range_is_read_across_windows_and_pages(self):
        transport = self.use(self.page(['A'], 2, 1), self.page(['B'], 2, 2), self.page(['C'], 1, 1))
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        end = datetime(2026, 9, 10, tzinfo=timezone.utc)      # 40 days: two windows

        report = gateway.search_transactions(start, end)

        self.assertEqual([t.transaction_id for t in report.transactions], ['A', 'B', 'C'])
        urls = [r.url for r in transport.requests[1:]]
        self.assertEqual(len(urls), 3)
        self.assertIn('page=2', urls[1])
        self.assertIn('start_date=2026-09-01T00%3A00%3A00Z', urls[2])
        self.assertIn('end_date=2026-09-10T00%3A00%3A00Z', urls[2])


class ConfigurationTests(SimpleTestCase):
    def test_base_url_override_is_used_verbatim(self):
        self.assertEqual(gateway.resolve_base_url('sandbox', 'https://proxy.example/paypal'),
                         'https://proxy.example/paypal')

    def test_sandbox_environment(self):
        self.assertEqual(gateway.resolve_base_url('sandbox', ''), gateway.SANDBOX_BASE_URL)

    def test_other_environment_needs_an_explicit_base_url(self):
        with self.assertRaises(ImproperlyConfigured):
            gateway.resolve_base_url('live', '')

    @override_settings(PAYPAL_CLIENT_ID='', PAYPAL_CLIENT_SECRET='')
    def test_missing_credentials_fail_fast(self):
        with self.assertRaises(ImproperlyConfigured):
            gateway.build_client()
