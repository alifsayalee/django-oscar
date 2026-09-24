"""
Tests for the PayPal payments API.

PayPal is faked at the SDK's transport seam (``custom_http_client``), so every
test runs the real SDK request pipeline: serialisation, headers, decoding and
error mapping. No network access is needed.

Run with: ``python sandbox/manage.py test apps.payments_api``
"""

import itertools
import json
from unittest import mock
from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from oscar.core.loading import get_model
from oscar.test.factories import create_product
from paypal.core import HttpRequest, HttpResponse

from . import gateway as gw
from .models import PayPalPayment, PayPalRefund

Bankcard = get_model('payment', 'Bankcard')
Source = get_model('payment', 'Source')

VISA = {'number': '4111 1111 1111 1111', 'expiry': '2030-12', 'securityCode': '123', 'name': 'Test Shopper',
        'billingAddress': {'addressLine1': '1 Main St', 'city': 'San Jose', 'state': 'CA', 'postalCode': '95131',
                           'countryCode': 'US'}}
BASE = 'https://paypal.test'


def _json(status, body, headers=None):
    all_headers = {'content-type': 'application/json', 'paypal-debug-id': 'dbg123'}
    all_headers.update(headers or {})
    return HttpResponse(status_code=status, headers=all_headers, content=json.dumps(body).encode())


class FakePayPal:
    """A tiny in-memory PayPal behind the SDK transport protocol."""

    def __init__(self):
        self.requests = []
        self.ids = itertools.count(1)
        self.by_request_id = {}
        self.authorizations = {}
        self.captures = {}
        self.tokens = {}
        self.overrides = []  # [(method, path_fragment, response_or_exception)]
        self.auth_created = timezone.now()
        self.transactions = []

    # -- helpers used by tests
    def fail_next(self, method, fragment, outcome):
        self.overrides.append((method, fragment, outcome))

    def calls(self, method, fragment):
        return [r for r in self.requests if r.method == method and fragment in r.url]

    # -- transport protocol
    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = urlsplit(request.url).path
        for index, (method, fragment, outcome) in enumerate(self.overrides):
            if request.method == method and fragment in path:
                del self.overrides[index]
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        if path == '/v1/oauth2/token':
            return _json(200, {'access_token': 'token', 'token_type': 'Bearer', 'expires_in': 32400})
        key = (request.method, path, request.headers.get('paypal-request-id'))
        if key[2] and key in self.by_request_id:
            return self.by_request_id[key]  # PayPal de-duplicates on PayPal-Request-Id
        response = self._route(request, path)
        if key[2]:
            self.by_request_id[key] = response
        return response

    def close(self):
        pass

    def _new_id(self, prefix):
        return '%s%015d' % (prefix, next(self.ids))

    def _route(self, request, path):
        body = request.body.value if request.body is not None else {}
        if request.method == 'POST' and path == '/v2/checkout/orders':
            unit = body['purchase_units'][0]
            card = body['payment_source']['card']
            if card.get('vault_id') and card['vault_id'] not in self.tokens:
                return _json(422, {'name': 'UNPROCESSABLE_ENTITY', 'message': 'Invalid vault id',
                                   'debug_id': 'd1', 'details': [{'issue': 'INVALID_VAULT_ID'}]})
            auth_id = self._new_id('AUTH')
            self.authorizations[auth_id] = {'status': 'CREATED', 'amount': unit['amount'],
                                            'created': self.auth_created}
            last = (card.get('number') or '4111111111111111')[-4:]
            return _json(201, {
                'id': self._new_id('ORDER'), 'status': 'COMPLETED', 'intent': 'AUTHORIZE',
                'payment_source': {'card': {'last_digits': last, 'brand': 'VISA', 'type': 'CREDIT'}},
                'purchase_units': [{'reference_id': unit['reference_id'], 'payments': {'authorizations': [
                    self._auth_json(auth_id)]}}]})
        if path.startswith('/v2/payments/authorizations/'):
            parts = path.split('/')
            auth_id, action = parts[4], parts[5] if len(parts) > 5 else None
            auth = self.authorizations.get(auth_id)
            if auth is None:
                return _json(404, {'name': 'RESOURCE_NOT_FOUND', 'message': 'nope', 'debug_id': 'd'})
            if action is None:
                return _json(200, self._auth_json(auth_id))
            if action == 'capture':
                auth['status'] = 'CAPTURED'
                capture_id = self._new_id('CAP')
                value = body['amount']['value']
                fee = (Decimal(value) * Decimal('0.0349') + Decimal('0.49')).quantize(Decimal('0.01'))
                self.captures[capture_id] = {'value': Decimal(value), 'refunded': Decimal('0')}
                return _json(201, {
                    'id': capture_id, 'status': 'COMPLETED', 'amount': body['amount'], 'final_capture': True,
                    'seller_receivable_breakdown': {
                        'gross_amount': body['amount'],
                        'paypal_fee': {'currency_code': 'USD', 'value': str(fee)},
                        'net_amount': {'currency_code': 'USD', 'value': str(Decimal(value) - fee)}}})
            if action == 'void':
                auth['status'] = 'VOIDED'
                return _json(200, self._auth_json(auth_id))
            if action == 'reauthorize':
                auth['status'] = 'VOIDED'
                new_id = self._new_id('REAUTH')
                self.authorizations[new_id] = {'status': 'CREATED', 'amount': auth['amount'],
                                               'created': timezone.now()}
                return _json(201, self._auth_json(new_id))
        if path.startswith('/v2/payments/captures/') and path.endswith('/refund'):
            capture = self.captures[path.split('/')[4]]
            value = Decimal(body['amount']['value'])
            if capture['refunded'] + value > capture['value']:
                return _json(422, {'name': 'UNPROCESSABLE_ENTITY', 'message': 'Refund exceeds capture',
                                   'debug_id': 'd', 'details': [{'issue': 'REFUND_AMOUNT_EXCEEDED'}]})
            capture['refunded'] += value
            return _json(201, {'id': self._new_id('REF'), 'status': 'COMPLETED', 'amount': body['amount'],
                               'seller_payable_breakdown': {'net_amount': body['amount']}})
        if request.method == 'POST' and path == '/v3/vault/payment-tokens':
            card = body['payment_source']['card']
            token_id = self._new_id('TOK')
            self.tokens[token_id] = card['number'][-4:]
            customer = (body.get('customer') or {}).get('id') or 'CUST1'
            return _json(201, {'id': token_id, 'customer': {'id': customer}, 'payment_source': {'card': {
                'last_digits': card['number'][-4:], 'brand': 'VISA', 'expiry': card['expiry']}}})
        if request.method == 'DELETE' and path.startswith('/v3/vault/payment-tokens/'):
            if self.tokens.pop(path.rsplit('/', 1)[1], None) is None:
                return HttpResponse(status_code=404, headers={}, content=b'')
            return HttpResponse(status_code=204, headers={}, content=b'')
        if path == '/v1/reporting/transactions':
            query = parse_qs(urlsplit(request.url).query)
            page = int(query['page'][0])
            size = int(query['page_size'][0])
            rows = self.transactions[(page - 1) * size: page * size]
            pages = max(1, -(-len(self.transactions) // size))
            return _json(200, {'transaction_details': [{'transaction_info': row} for row in rows],
                               'page': page, 'total_pages': pages, 'total_items': len(self.transactions),
                               'last_refreshed_datetime': '2026-09-24T00:00:00+0000'})
        return _json(418, {'unexpected': path})

    def _auth_json(self, auth_id):
        auth = self.authorizations[auth_id]
        return {'id': auth_id, 'status': auth['status'], 'amount': auth['amount'],
                'create_time': auth['created'].strftime('%Y-%m-%dT%H:%M:%SZ'),
                'expiration_time': (auth['created'] + timedelta(days=29)).strftime('%Y-%m-%dT%H:%M:%SZ')}


def make_gateway(fake):
    config = gw.PayPalConfig(client_id='id', client_secret='secret', base_url=BASE, currency='USD',
                             timeout=5.0, reference_prefix='test')
    return gw.Gateway(gw.build_client(config, transport=fake), config)


class GatewayTests(TestCase):
    """The SDK boundary: request shape and the error ladder."""

    def setUp(self):
        self.fake = FakePayPal()
        self.gateway = make_gateway(self.fake)
        patcher = mock.patch.object(gw, '_sleep', lambda seconds: None)  # no real backoff in tests
        patcher.start()
        self.addCleanup(patcher.stop)

    def authorize(self, request_id='rid-1'):
        return self.gateway.authorize(
            request_id=request_id, reference='100001', invoice_id='inv-1', description='Order 100001',
            amount=Decimal('25.50'), currency='USD',
            items=[gw.LineItem(name='Book', sku='SKU1', unit_amount=Decimal('12.75'), quantity=2)],
            card=gw.CardDetails(number='4111111111111111', expiry='2030-12', security_code='123'))

    def test_authorize_builds_single_step_authorize_order(self):
        result = self.authorize()
        self.assertEqual(result.order_status, 'COMPLETED')
        self.assertEqual(result.authorization.amount, gw.Amount(Decimal('25.50'), 'USD'))
        request = self.fake.calls('POST', '/v2/checkout/orders')[0]
        self.assertEqual(request.headers['paypal-request-id'], 'rid-1')
        self.assertEqual(request.headers['prefer'], 'return=representation')
        self.assertEqual(request.headers['authorization'], 'Bearer token')
        body = request.body.value
        self.assertEqual(body['intent'], 'AUTHORIZE')
        self.assertEqual(body['purchase_units'][0]['amount'],
                         {'currency_code': 'USD', 'value': '25.50',
                          'breakdown': {'item_total': {'currency_code': 'USD', 'value': '25.50'}}})
        self.assertEqual(body['purchase_units'][0]['items'][0]['quantity'], '2')
        self.assertEqual(body['payment_source']['card']['expiry'], '2030-12')

    def test_typed_error_becomes_rejection_with_issues(self):
        self.fake.fail_next('POST', '/v2/checkout/orders', _json(422, {
            'name': 'UNPROCESSABLE_ENTITY', 'message': 'declined', 'debug_id': 'abc',
            'details': [{'issue': 'INSTRUMENT_DECLINED'}]}))
        with self.assertRaises(gw.PayPalRejected) as ctx:
            self.authorize()
        self.assertEqual((ctx.exception.status_code, ctx.exception.issues), (422, ('INSTRUMENT_DECLINED',)))
        self.assertFalse(ctx.exception.outcome_unknown)

    def test_unmapped_status_is_not_passed_through(self):
        self.fake.fail_next('POST', '/v2/checkout/orders', _json(418, {'whatever': 1}))
        with self.assertRaises(gw.PayPalUnavailable) as ctx:
            self.authorize()
        self.assertEqual((ctx.exception.status_code, ctx.exception.outcome_unknown), (502, False))

    def test_provider_401_is_our_problem_not_the_callers(self):
        self.fake.fail_next('POST', '/v2/checkout/orders', _json(401, {
            'name': 'AUTHENTICATION_FAILURE', 'message': 'no', 'debug_id': 'x'}))
        with self.assertRaises(gw.PayPalConfigError) as ctx:
            self.authorize()
        self.assertEqual(ctx.exception.status_code, 502)

    def test_bad_credentials_is_a_configuration_error(self):
        self.fake.fail_next('POST', '/v1/oauth2/token', _json(401, {'error': 'invalid_client'}))
        with self.assertRaises(gw.PayPalConfigError) as ctx:
            self.authorize()
        self.assertEqual(ctx.exception.code, 'PAYPAL_AUTH_FAILED')
        self.assertEqual(self.fake.calls('POST', '/v2/checkout/orders'), [])

    def test_unsent_is_not_the_same_failure_as_unknown(self):
        for _ in range(2):
            self.fake.fail_next('POST', '/v2/checkout/orders', httpx.ConnectError('refused'))
        with self.assertRaises(gw.PayPalUnavailable) as unsent:
            self.authorize('rid-unsent')
        for _ in range(2):
            self.fake.fail_next('POST', '/v2/checkout/orders', httpx.ReadTimeout('no reply'))
        with self.assertRaises(gw.PayPalUnavailable) as unknown:
            self.authorize('rid-unknown')
        self.assertEqual((unsent.exception.status_code, unsent.exception.outcome_unknown), (502, False))
        self.assertEqual((unknown.exception.status_code, unknown.exception.outcome_unknown), (504, True))
        # Each write was retried once, and always under the same request id.
        sent = [r.headers['paypal-request-id'] for r in self.fake.calls('POST', '/v2/checkout/orders')]
        self.assertEqual(sent, ['rid-unsent', 'rid-unsent', 'rid-unknown', 'rid-unknown'])

    def test_retry_after_timeout_reuses_the_key_and_succeeds(self):
        self.fake.fail_next('POST', '/v2/checkout/orders', httpx.ReadTimeout('no reply'))
        result = self.authorize('rid-retry')
        self.assertEqual(result.order_status, 'COMPLETED')
        self.assertEqual(len(self.fake.calls('POST', '/v2/checkout/orders')), 2)

    def test_unreadable_success_body_is_an_unknown_outcome(self):
        self.fake.fail_next('POST', '/v2/checkout/orders', _json(201, {'id': 'X', 'purchase_units': 'nope'}))
        with self.assertRaises(gw.PayPalUnavailable) as ctx:
            self.authorize()
        self.assertEqual((ctx.exception.code, ctx.exception.outcome_unknown), ('PAYPAL_UNREADABLE_RESPONSE', True))

    def test_truncated_success_body_is_not_reported_as_success(self):
        self.fake.fail_next('POST', '/v2/checkout/orders', _json(201, {}))
        with self.assertRaises(gw.PayPalUnavailable) as ctx:
            self.authorize()
        self.assertTrue(ctx.exception.outcome_unknown)

    def test_search_covers_every_page_of_every_window(self):
        self.fake.transactions = [{'transaction_id': 'T%03d' % i, 'transaction_amount': {
            'currency_code': 'USD', 'value': '1.00'}} for i in range(250)]
        start = timezone.now() - timedelta(days=40)
        result = self.gateway.search_transactions(start, timezone.now())
        self.assertEqual(result.windows, 2)
        # 3 pages in each of the 2 windows (the fake ignores the dates).
        self.assertEqual(result.pages, 6)
        self.assertEqual(len(result.transactions), 500)
        first = self.fake.calls('GET', '/v1/reporting/transactions')[0]
        query = parse_qs(urlsplit(first.url).query)
        self.assertEqual(query['page'], ['1'])
        self.assertIn('T', query['start_date'][0])

    def test_search_window_without_report_data_is_empty_not_an_error(self):
        self.fake.fail_next('GET', '/v1/reporting/transactions', _json(404, {
            'name': 'INVALID_REQUEST', 'message': 'Data for the given start date is not available.',
            'debug_id': 'lag'}))
        start = timezone.now() - timedelta(hours=1)
        result = self.gateway.search_transactions(start, timezone.now())
        self.assertEqual((result.transactions, result.pages), ([], 0))
        self.assertEqual(len(result.unavailable_windows), 1)

    def test_other_search_errors_still_fail(self):
        self.fake.fail_next('GET', '/v1/reporting/transactions', _json(400, {
            'name': 'INVALID_REQUEST', 'message': 'bad', 'debug_id': 'x'}))
        with self.assertRaises(gw.PayPalRejected):
            self.gateway.search_transactions(timezone.now() - timedelta(hours=1), timezone.now())

    def test_money_uses_the_currency_exponent(self):
        self.assertEqual(gw.format_amount(Decimal('10'), 'USD'), '10.00')
        self.assertEqual(gw.format_amount(Decimal('1000'), 'JPY'), '1000')
        self.assertEqual(gw.format_amount(Decimal('1.2344'), 'KWD'), '1.234')

    def test_base_url_override_and_environment(self):
        self.assertEqual(gw.resolve_base_url('sandbox', ''), gw.SANDBOX_BASE_URL)
        self.assertEqual(gw.resolve_base_url('sandbox', 'https://mock.local'), 'https://mock.local')
        self.assertEqual(gw.resolve_base_url('live', 'https://api.example'), 'https://api.example')
        with self.assertRaises(gw.PayPalConfigError):
            gw.resolve_base_url('live', '')


@override_settings(PAYPAL_CURRENCY='USD', PAYPAL_REFERENCE_PREFIX='test')
class ApiTests(TestCase):
    """The HTTP API end to end, over the fake PayPal."""

    def setUp(self):
        patcher = mock.patch.object(gw, '_sleep', lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fake = FakePayPal()
        self.gateway_ctx = gw.use_gateway(make_gateway(self.fake))
        self.gateway_ctx.__enter__()
        User = get_user_model()
        self.shopper = User.objects.create_user('shopper', 'shopper@example.com', 'pw-shopper-123')
        self.other = User.objects.create_user('other', 'other@example.com', 'pw-other-123')
        self.staff = User.objects.create_user('ops', 'ops@example.com', 'pw-ops-123', is_staff=True)
        self.product = create_product(price=Decimal('12.50'), num_in_stock=50)
        self.client.force_login(self.shopper)

    def tearDown(self):
        self.gateway_ctx.__exit__(None, None, None)

    def post(self, url, body=None, **headers):
        return self.client.post(url, json.dumps(body or {}), content_type='application/json', headers=headers)

    def as_user(self, user):
        self.client.force_login(user)

    def place(self, quantity=2):
        response = self.post('/api/orders', {'lines': [{'productId': self.product.pk, 'quantity': quantity}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()['orderId']

    def pay(self, order_id, **body):
        return self.post('/api/orders/%s/pay' % order_id, body or {'card': VISA})

    def paid_and_fulfilled(self):
        order_id = self.place()
        self.assertEqual(self.pay(order_id).status_code, 200)
        self.as_user(self.staff)
        self.assertEqual(self.post('/api/orders/%s/fulfil' % order_id).status_code, 200)
        self.as_user(self.shopper)
        return order_id

    def test_order_starts_awaiting_payment_with_catalogue_amounts(self):
        response = self.post('/api/orders', {'lines': [{'productId': self.product.pk, 'quantity': 2}]})
        body = response.json()
        self.assertEqual((body['status'], body['total'], body['currency']), ('Pending', '25.00', 'USD'))
        self.assertEqual(body['payment']['state'], 'unpaid')

    def test_pay_authorizes_the_exact_total_once(self):
        order_id = self.place()
        first = self.pay(order_id)
        second = self.pay(order_id)  # a double-click
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200)
        payment = first.json()['payment']
        self.assertEqual(payment['state'], 'authorized')
        self.assertEqual(payment['authorization']['amount'], '25.00')
        self.assertEqual(first.json()['status'], 'Being processed')
        self.assertEqual(len(self.fake.calls('POST', '/v2/checkout/orders')), 1)
        sent = self.fake.calls('POST', '/v2/checkout/orders')[0].body.value
        self.assertEqual(sent['purchase_units'][0]['amount']['value'], '25.00')
        source = Source.objects.get(order__number=order_id)
        self.assertEqual(source.amount_allocated, Decimal('25.00'))

    def test_card_data_is_never_stored(self):
        order_id = self.place()
        self.pay(order_id)
        payment = PayPalPayment.objects.get(order__number=order_id)
        stored = json.dumps({f.name: str(getattr(payment, f.attname)) for f in payment._meta.fields})
        self.assertNotIn('4111111111111111', stored)
        self.assertEqual(payment.card_last_digits, '1111')

    def test_unknown_outcome_is_resent_under_the_same_key(self):
        order_id = self.place()
        for _ in range(2):
            self.fake.fail_next('POST', '/v2/checkout/orders', httpx.ReadTimeout('slow'))
        response = self.pay(order_id)
        self.assertEqual(response.status_code, 504)
        self.assertTrue(response.json()['error']['outcomeUnknown'])
        response = self.pay(order_id)
        self.assertEqual(response.json()['payment']['state'], 'authorized')
        keys = {r.headers['paypal-request-id'] for r in self.fake.calls('POST', '/v2/checkout/orders')}
        self.assertEqual(len(keys), 1)

    def test_decline_allows_a_new_attempt(self):
        order_id = self.place()
        self.fake.fail_next('POST', '/v2/checkout/orders', _json(422, {
            'name': 'UNPROCESSABLE_ENTITY', 'message': 'Declined', 'debug_id': 'z',
            'details': [{'issue': 'INSTRUMENT_DECLINED'}]}))
        declined = self.pay(order_id)
        self.assertEqual(declined.status_code, 422)
        self.assertEqual(declined.json()['error']['paypalIssues'], ['INSTRUMENT_DECLINED'])
        accepted = self.pay(order_id)
        self.assertEqual(accepted.json()['payment']['state'], 'authorized')
        keys = [r.headers['paypal-request-id'] for r in self.fake.calls('POST', '/v2/checkout/orders')]
        self.assertEqual(len(set(keys)), 2)

    def test_invalid_card_is_rejected_before_paypal(self):
        order_id = self.place()
        response = self.pay(order_id, card=dict(VISA, number='4111111111111112'))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.fake.calls('POST', '/v2/checkout/orders'), [])

    def test_fulfil_captures_and_reports_fee_and_net(self):
        order_id = self.place()
        self.pay(order_id)
        self.as_user(self.shopper)
        self.assertEqual(self.post('/api/orders/%s/fulfil' % order_id).status_code, 403)
        self.as_user(self.staff)
        response = self.post('/api/orders/%s/fulfil' % order_id)
        again = self.post('/api/orders/%s/fulfil' % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        capture = response.json()['payment']['capture']
        self.assertEqual((capture['amount'], capture['paypalFee'], capture['netAmount']), ('25.00', '1.36', '23.64'))
        self.assertEqual(response.json()['status'], 'Complete')
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(self.fake.calls('POST', '/capture')), 1)

    def test_stale_authorization_is_renewed_before_capture(self):
        self.fake.auth_created = timezone.now() - timedelta(days=5)
        order_id = self.place()
        self.pay(order_id)
        self.as_user(self.staff)
        response = self.post('/api/orders/%s/fulfil' % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        auth = response.json()['payment']['authorization']
        self.assertTrue(auth['id'].startswith('REAUTH'))
        self.assertTrue(auth['originalAuthorizationId'].startswith('AUTH'))
        self.assertIn(auth['id'], self.fake.calls('POST', '/capture')[0].url)

    def test_expired_authorization_is_reported_actionably(self):
        self.fake.auth_created = timezone.now() - timedelta(days=31)
        order_id = self.place()
        self.pay(order_id)
        self.as_user(self.staff)
        response = self.post('/api/orders/%s/fulfil' % order_id)
        self.assertEqual(response.status_code, 409)
        error = response.json()['error']
        self.assertEqual(error['code'], 'AUTHORIZATION_EXPIRED')
        self.assertIn('Cancel this order', error['message'])
        self.assertEqual(PayPalPayment.objects.get(order__number=order_id).state, 'authorized')
        self.assertEqual(self.fake.calls('POST', '/capture'), [])

    def test_refused_renewal_is_reported_actionably(self):
        self.fake.auth_created = timezone.now() - timedelta(days=5)
        order_id = self.place()
        self.pay(order_id)
        self.fake.fail_next('POST', '/reauthorize', _json(422, {
            'name': 'UNPROCESSABLE_ENTITY', 'message': 'Cannot reauthorize', 'debug_id': 'q',
            'details': [{'issue': 'REAUTHORIZATION_NOT_SUPPORTED'}]}))
        self.as_user(self.staff)
        response = self.post('/api/orders/%s/fulfil' % order_id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'AUTHORIZATION_RENEWAL_REFUSED')
        self.assertEqual(response.json()['error']['paypalIssues'], ['REAUTHORIZATION_NOT_SUPPORTED'])

    def test_cancel_releases_the_hold(self):
        order_id = self.place()
        self.pay(order_id)
        self.assertEqual(self.post('/api/orders/%s/cancel' % order_id).status_code, 403)
        self.as_user(self.staff)
        response = self.post('/api/orders/%s/cancel' % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.json()['status'], response.json()['payment']['state']), ('Cancelled', 'voided'))
        self.assertEqual(len(self.fake.calls('POST', '/void')), 1)
        self.assertEqual(self.post('/api/orders/%s/fulfil' % order_id).status_code, 409)
        self.assertEqual(self.post('/api/orders/%s/cancel' % order_id).status_code, 200)
        self.assertEqual(len(self.fake.calls('POST', '/void')), 1)

    def test_cancel_after_fulfilment_is_refused(self):
        order_id = self.paid_and_fulfilled()
        self.as_user(self.staff)
        self.assertEqual(self.post('/api/orders/%s/cancel' % order_id).status_code, 409)

    def test_partial_refunds_never_exceed_the_capture(self):
        order_id = self.paid_and_fulfilled()
        url = '/api/orders/%s/refunds' % order_id
        first = self.post(url, {'amount': '10.00'}, **{'Idempotency-Key': 'r1'})
        repeat = self.post(url, {'amount': '10.00'}, **{'Idempotency-Key': 'r1'})
        second = self.post(url, {'amount': '10.00'}, **{'Idempotency-Key': 'r2'})
        too_much = self.post(url, {'amount': '5.01'}, **{'Idempotency-Key': 'r3'})
        rest = self.post(url, {}, **{'Idempotency-Key': 'r4'})
        self.assertEqual((first.status_code, repeat.status_code, second.status_code), (201, 200, 201))
        self.assertEqual(first.json()['refundId'], repeat.json()['refundId'])
        self.assertNotEqual(first.json()['refundId'], second.json()['refundId'])
        self.assertEqual(too_much.status_code, 422)
        self.assertEqual(too_much.json()['error']['code'], 'REFUND_EXCEEDS_CAPTURED')
        self.assertEqual((rest.status_code, rest.json()['amount']), (201, '5.00'))
        self.assertEqual(rest.json()['payment']['refundableAmount'], '0.00')
        self.assertEqual(len(self.fake.calls('POST', '/refund')), 3)
        self.assertEqual(Source.objects.get(order__number=order_id).amount_refunded, Decimal('25.00'))

    def test_refundable_amount_is_exact_after_a_full_refund(self):
        self.product = create_product(price=Decimal('7.99'), num_in_stock=50)
        order_id = self.place(quantity=3)
        self.pay(order_id)
        self.as_user(self.staff)
        self.post('/api/orders/%s/fulfil' % order_id)
        self.as_user(self.shopper)
        url = '/api/orders/%s/refunds' % order_id
        self.post(url, {'amount': '0.01'}, **{'Idempotency-Key': 'a'})
        rest = self.post(url, {}, **{'Idempotency-Key': 'b'})
        self.assertEqual((rest.status_code, rest.json()['amount']), (201, '23.96'))
        self.assertEqual(rest.json()['payment']['refundableAmount'], '0.00')
        self.assertEqual(rest.json()['payment']['refundedAmount'], '23.97')

    def test_refund_requires_key_and_capture(self):
        order_id = self.place()
        url = '/api/orders/%s/refunds' % order_id
        self.assertEqual(self.post(url, {'amount': '1.00'}).status_code, 400)
        self.assertEqual(self.post(url, {'amount': '1.00'}, **{'Idempotency-Key': 'k'}).status_code, 409)

    def test_refund_reusing_a_key_with_another_amount_is_refused(self):
        order_id = self.paid_and_fulfilled()
        url = '/api/orders/%s/refunds' % order_id
        self.post(url, {'amount': '1.00'}, **{'Idempotency-Key': 'same'})
        self.assertEqual(self.post(url, {'amount': '2.00'}, **{'Idempotency-Key': 'same'}).status_code, 422)

    def test_refund_with_unknown_outcome_keeps_its_reservation(self):
        order_id = self.paid_and_fulfilled()
        url = '/api/orders/%s/refunds' % order_id
        for _ in range(2):
            self.fake.fail_next('POST', '/refund', httpx.ReadTimeout('slow'))
        pending = self.post(url, {'amount': '20.00'}, **{'Idempotency-Key': 'slow'})
        self.assertEqual(pending.status_code, 504)
        self.assertEqual(self.post(url, {'amount': '10.00'}, **{'Idempotency-Key': 'other'}).status_code, 422)
        settled = self.post(url, {'amount': '20.00'}, **{'Idempotency-Key': 'slow'})
        self.assertEqual((settled.status_code, settled.json()['state']), (201, 'completed'))
        self.assertEqual(PayPalRefund.objects.filter(state='completed').count(), 1)

    def test_shoppers_cannot_see_or_touch_each_others_orders(self):
        order_id = self.place()
        self.as_user(self.other)
        self.assertEqual(self.client.get('/api/orders/%s' % order_id).status_code, 404)
        self.assertEqual(self.pay(order_id).status_code, 404)
        self.assertEqual(self.post('/api/orders/%s/refunds' % order_id, {}, **{'Idempotency-Key': 'x'}).status_code,
                         404)
        self.assertEqual(self.client.get('/api/my-orders').json()['orders'], [])

    def test_anonymous_callers_are_refused(self):
        self.client.logout()
        self.assertEqual(self.client.get('/api/my-orders').status_code, 401)
        self.assertEqual(self.post('/api/orders', {'lines': []}).status_code, 401)

    def test_my_orders_lists_payment_state(self):
        order_id = self.place()
        self.pay(order_id)
        orders = self.client.get('/api/my-orders').json()['orders']
        self.assertEqual([(o['orderId'], o['payment']['state']) for o in orders], [(order_id, 'authorized')])

    def test_saved_card_lifecycle_and_reuse(self):
        created = self.post('/api/payment-methods', {'card': VISA})
        self.assertEqual(created.status_code, 201, created.content)
        card = created.json()
        self.assertEqual((card['brand'], card['lastDigits'], card['expiry']), ('VISA', '1111', '2030-12'))
        self.assertNotIn('4111111111111111', created.content.decode())
        stored = Bankcard.objects.get(pk=card['paymentMethodId'])
        self.assertEqual(stored.number, 'XXXX-XXXX-XXXX-1111')

        listed = self.client.get('/api/payment-methods').json()['paymentMethods']
        self.assertEqual([c['paymentMethodId'] for c in listed], [card['paymentMethodId']])

        order_id = self.place()
        paid = self.pay(order_id, paymentMethodId=card['paymentMethodId'])
        self.assertEqual(paid.json()['payment']['state'], 'authorized')
        sent = self.fake.calls('POST', '/v2/checkout/orders')[-1].body.value
        self.assertEqual(sent['payment_source'], {'card': {'vault_id': stored.partner_reference}})

        # Another shopper can neither see, use nor delete it.
        self.as_user(self.other)
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])
        other_order = self.place()
        self.assertEqual(self.pay(other_order, paymentMethodId=card['paymentMethodId']).status_code, 404)
        self.assertEqual(self.client.delete('/api/payment-methods/%s' % card['paymentMethodId']).status_code, 404)

        self.as_user(self.shopper)
        deleted = self.client.delete('/api/payment-methods/%s' % card['paymentMethodId'])
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])
        another = self.place()
        self.assertEqual(self.pay(another, paymentMethodId=card['paymentMethodId']).status_code, 404)
        self.assertEqual(len(self.fake.calls('DELETE', '/v3/vault/payment-tokens/')), 1)

    def test_second_saved_card_joins_the_same_paypal_customer(self):
        self.post('/api/payment-methods', {'card': VISA})
        self.post('/api/payment-methods', {'card': dict(VISA, number='5555555555554444')})
        second = self.fake.calls('POST', '/v3/vault/payment-tokens')[1].body.value
        self.assertEqual(second['customer'], {'id': 'CUST1'})

    def test_reconciliation_lines_up_both_sides(self):
        order_id = self.paid_and_fulfilled()
        self.post('/api/orders/%s/refunds' % order_id, {'amount': '5.00'}, **{'Idempotency-Key': 'a'})
        payment = PayPalPayment.objects.get(order__number=order_id)
        refund = payment.refunds.get()
        self.fake.transactions = [
            {'transaction_id': payment.capture_id, 'transaction_amount': {'currency_code': 'USD', 'value': '25.00'}},
            {'transaction_id': refund.paypal_refund_id, 'paypal_reference_id': payment.capture_id,
             'transaction_amount': {'currency_code': 'USD', 'value': '-5.00'}},
            {'transaction_id': 'STRANGER', 'transaction_amount': {'currency_code': 'USD', 'value': '9.99'}},
        ]
        start = (timezone.now() - timedelta(days=1)).isoformat()
        end = (timezone.now() + timedelta(minutes=1)).isoformat()
        self.assertEqual(self.client.get('/api/reconciliation', {'from': start, 'to': end}).status_code, 403)
        self.as_user(self.staff)
        report = self.client.get('/api/reconciliation', {'from': start, 'to': end}).json()
        self.assertEqual(report['summary'], {'paypalTransactions': 3, 'matched': 2, 'paypalOnly': 1, 'appOnly': 0,
                                             'amountMismatches': 0})
        self.assertEqual(report['paypalOnly'][0]['paypalTransactionId'], 'STRANGER')

        self.fake.transactions = []
        report = self.client.get('/api/reconciliation', {'from': start, 'to': end}).json()
        self.assertEqual(sorted(i['kind'] for i in report['appOnly']), ['capture', 'refund'])

    def test_reconciliation_validates_dates(self):
        self.as_user(self.staff)
        self.assertEqual(self.client.get('/api/reconciliation', {'from': 'yesterday', 'to': 'now'}).status_code, 400)

    def test_session_login_with_csrf(self):
        client = self.client_class(enforce_csrf_checks=True)
        token = client.get('/api/csrf').json()['csrfToken']
        refused = client.post('/api/session', json.dumps({'username': 'shopper', 'password': 'pw-shopper-123'}),
                              content_type='application/json')
        self.assertEqual(refused.status_code, 403)
        response = client.post('/api/session', json.dumps({'username': 'shopper', 'password': 'pw-shopper-123'}),
                               content_type='application/json', headers={'X-CSRFToken': token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get('/api/my-orders').status_code, 200)
