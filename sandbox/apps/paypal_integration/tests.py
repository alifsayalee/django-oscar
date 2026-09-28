"""
API tests with PayPal faked at the SDK's transport seam: the real SDK builds
and decodes every request, only the network is replaced.

Run from ``sandbox/``:
    pytest apps/paypal_integration/tests.py --ds=settings --import-mode=importlib
"""
import json
import re
from datetime import timedelta
from decimal import Decimal as D

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone
from oscar.test.factories import create_product

from paypal import PaypalClient
from paypal.core import HttpResponse

from apps.paypal_integration import gateway
from apps.paypal_integration.models import PayPalPayment, ProviderWrite

pytestmark = pytest.mark.django_db(transaction=True)

CARD = {'number': '4111 1111 1111 1111', 'expiry': '2030-12', 'securityCode': '123', 'name': 'Test Shopper'}
PAN = '4111111111111111'


def json_response(status, body):
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


class FakePayPal:
    """Routes (method, path regex) to queued answers; records every request."""

    def __init__(self):
        self.routes = []
        self.requests = []

    def on(self, method, path, *answers):
        self.routes.append((method, re.compile(path + '$'), list(answers)))
        return self

    def send(self, request):
        self.requests.append(request)
        path = httpx.URL(request.url).path
        if request.method == 'POST' and path == '/v1/oauth2/token':
            return json_response(200, {'access_token': 't', 'token_type': 'Bearer', 'expires_in': 3600})
        for method, pattern, answers in self.routes:
            if method == request.method and pattern.match(path):
                answer = answers.pop(0) if len(answers) > 1 else answers[0]
                if isinstance(answer, BaseException):
                    raise answer
                return answer(request) if callable(answer) else answer
        raise AssertionError('unexpected PayPal call %s %s' % (request.method, path))

    def close(self):
        pass

    def calls(self, method, path):
        pattern = re.compile(path + '$')
        return [r for r in self.requests if r.method == method and pattern.match(httpx.URL(r.url).path)]


@pytest.fixture
def paypal(settings):
    settings.PAYPAL_REFERENCE_PREFIX = 'test'
    settings.PAYPAL_CURRENCY = 'USD'
    fake = FakePayPal()
    gateway.set_client(PaypalClient(
        base_url='https://paypal.test', custom_http_client=gateway.ObservingTransport(fake),
        oauth2={'client_id': 'id', 'client_secret': 'secret'}))
    yield fake
    gateway.set_client(None)


def make_user(name, staff=False):
    return get_user_model().objects.create_user(name, '%s@example.com' % name, 'pw-%s-123456' % name, is_staff=staff)


@pytest.fixture
def shopper():
    client = Client()
    client.force_login(make_user('shopper'))
    return client


@pytest.fixture
def operator():
    client = Client()
    client.force_login(make_user('operator', staff=True))
    return client


@pytest.fixture
def product():
    return create_product(price=D('12.50'), num_in_stock=100)


def post(client, url, body=None, **headers):
    return client.post(url, json.dumps(body or {}), content_type='application/json', headers=headers)


def place(client, product, quantity=2):
    response = post(client, '/api/orders', {'lines': [{'productId': product.pk, 'quantity': quantity}]})
    assert response.status_code == 201, response.content
    return response.json()['orderId']


# ---- PayPal bodies ---------------------------------------------------------

def order_body(amount='25.00', auth_status='CREATED', auth_id='AUTH1', created=None, order_status='COMPLETED'):
    created = (created or timezone.now()).strftime('%Y-%m-%dT%H:%M:%SZ')
    return {
        'id': 'PPORDER1', 'status': order_status, 'intent': 'AUTHORIZE',
        'payment_source': {'card': {'last_digits': '1111', 'brand': 'VISA'}},
        'purchase_units': [{'payments': {'authorizations': [{
            'id': auth_id, 'status': auth_status, 'amount': {'currency_code': 'USD', 'value': amount},
            'create_time': created, 'expiration_time': '2099-01-01T00:00:00Z'}]}}],
    }


def authorization_body(auth_id='AUTH1', status='CREATED', created=None, amount='25.00'):
    return {'id': auth_id, 'status': status, 'amount': {'currency_code': 'USD', 'value': amount},
            'create_time': (created or timezone.now()).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'expiration_time': '2099-01-01T00:00:00Z'}


def capture_body(amount='25.00', status='COMPLETED'):
    return {'id': 'CAP1', 'status': status, 'amount': {'currency_code': 'USD', 'value': amount},
            'create_time': '2026-09-28T10:00:00Z',
            'seller_receivable_breakdown': {'gross_amount': {'currency_code': 'USD', 'value': amount},
                                            'paypal_fee': {'currency_code': 'USD', 'value': '1.22'},
                                            'net_amount': {'currency_code': 'USD', 'value': '23.78'}}}


def refund_answer(request):
    value = request.body.value['amount']['value']
    # PayPal returns the original refund for a repeated PayPal-Request-Id
    refund_id = 'REF-%s' % request.headers['paypal-request-id'][-8:]
    return json_response(201, {'id': refund_id, 'status': 'COMPLETED',
                               'amount': {'currency_code': 'USD', 'value': value},
                               'create_time': '2026-09-28T11:00:00Z'})


ORDERS = r'/v2/checkout/orders'
CAPTURE = r'/v2/payments/authorizations/[^/]+/capture'


def authorized_order(paypal, shopper, product):
    paypal.on('POST', ORDERS, json_response(201, order_body()))
    number = place(shopper, product)
    assert post(shopper, '/api/orders/%s/pay' % number, {'card': CARD}).status_code == 200
    return number


def fulfilled_order(paypal, shopper, operator, product):
    number = authorized_order(paypal, shopper, product)
    paypal.on('GET', r'/v2/payments/authorizations/AUTH1', json_response(200, authorization_body()))
    paypal.on('POST', CAPTURE, json_response(201, capture_body()))
    assert post(operator, '/api/orders/%s/fulfil' % number).status_code == 200
    return number


# ---- Flow 1 ----------------------------------------------------------------

def test_pay_authorizes_the_order_total_once_under_a_derived_reference(paypal, shopper, product):
    paypal.on('POST', ORDERS, json_response(201, order_body()))
    number = place(shopper, product)

    first = post(shopper, '/api/orders/%s/pay' % number, {'card': CARD})
    second = post(shopper, '/api/orders/%s/pay' % number, {'card': CARD})     # double-click

    assert (first.status_code, second.status_code) == (200, 200)
    calls = paypal.calls('POST', ORDERS)
    assert len(calls) == 1
    sent = calls[0]
    assert sent.headers['paypal-request-id'] == 'test:ord:%s:auth:1' % number
    assert sent.body.value['intent'] == 'AUTHORIZE'
    assert sent.body.value['purchase_units'][0]['amount'] == {'currency_code': 'USD', 'value': '25.00'}
    body = first.json()
    assert body['status'] == 'Being processed'
    assert body['payment']['state'] == 'authorized'
    assert body['payment']['authorization']['id'] == 'AUTH1'
    assert body['payment']['amountAuthorized'] == '25.00'


def test_a_declined_authorization_is_not_done_and_a_retry_uses_a_new_reference(paypal, shopper, product):
    paypal.on('POST', ORDERS, json_response(201, order_body(auth_status='DENIED')),
              json_response(201, order_body(auth_id='AUTH2')))
    number = place(shopper, product)

    declined = post(shopper, '/api/orders/%s/pay' % number, {'card': CARD})
    assert declined.status_code == 409
    assert declined.json()['outcome'] == 'failed'
    assert declined.json()['status'] == 'Pending'

    retried = post(shopper, '/api/orders/%s/pay' % number, {'card': CARD})
    assert retried.status_code == 200
    refs = [r.headers['paypal-request-id'] for r in paypal.calls('POST', ORDERS)]
    assert refs == ['test:ord:%s:auth:1' % number, 'test:ord:%s:auth:2' % number]


def test_an_unlisted_status_is_unknown_not_done(paypal, shopper, product):
    paypal.on('POST', ORDERS, json_response(201, order_body(auth_status='SOMETHING_NEW')))
    number = place(shopper, product)
    response = post(shopper, '/api/orders/%s/pay' % number, {'card': CARD})
    assert response.status_code == 504
    assert PayPalPayment.objects.get().state == 'awaiting_payment'


def test_a_3ds_challenge_is_refused_not_round_tripped(paypal, shopper, product):
    paypal.on('POST', ORDERS, json_response(201, {'id': 'X', 'status': 'PAYER_ACTION_REQUIRED'}))
    number = place(shopper, product)
    response = post(shopper, '/api/orders/%s/pay' % number, {'card': CARD})
    assert response.status_code == 409
    assert '3-D Secure' in response.json()['message']


def test_an_authorized_amount_that_differs_is_flagged_for_review(paypal, shopper, product):
    paypal.on('POST', ORDERS, json_response(201, order_body(amount='24.99')))
    number = place(shopper, product)
    response = post(shopper, '/api/orders/%s/pay' % number, {'card': CARD})
    assert response.status_code == 409
    assert response.json()['error'] == 'needs_review'
    assert ProviderWrite.objects.get(kind='authorize').outcome == 'needs_review'


def test_unsent_is_not_the_same_failure_as_unknown(paypal, shopper, product):
    paypal.on('POST', ORDERS, httpx.ConnectError('refused'))
    unsent_order = place(shopper, product)
    unsent = post(shopper, '/api/orders/%s/pay' % unsent_order, {'card': CARD})

    paypal.routes.clear()
    paypal.on('POST', ORDERS, httpx.ReadTimeout('no reply'))
    unknown_order = place(shopper, product)
    unknown = post(shopper, '/api/orders/%s/pay' % unknown_order, {'card': CARD})

    assert (unsent.status_code, unsent.json()['outcomeUnknown']) == (502, False)
    assert (unknown.status_code, unknown.json()['outcomeUnknown']) == (504, True)
    # the unknown one was checked by resending under the SAME reference, never a new one
    refs = {r.headers['paypal-request-id'] for r in paypal.calls('POST', ORDERS)
            if unknown_order in r.headers['paypal-request-id']}
    assert refs == {'test:ord:%s:auth:1' % unknown_order}
    assert ProviderWrite.objects.get(ref='test:ord:%s:auth:1' % unknown_order).outcome == 'unknown'


def test_an_unknown_outcome_is_settled_by_the_next_request_under_the_same_reference(paypal, shopper, product):
    paypal.on('POST', ORDERS, httpx.ReadTimeout('no reply'), httpx.ReadTimeout('no reply'),
              json_response(201, order_body()))
    number = place(shopper, product)
    assert post(shopper, '/api/orders/%s/pay' % number, {'card': CARD}).status_code == 504
    settled = post(shopper, '/api/orders/%s/pay' % number, {'card': CARD})
    assert settled.status_code == 200
    assert {r.headers['paypal-request-id'] for r in paypal.calls('POST', ORDERS)} == {'test:ord:%s:auth:1' % number}


def test_an_unreadable_success_body_is_unknown(paypal, shopper, product):
    paypal.on('POST', ORDERS, json_response(201, {'id': 'X', 'purchase_units': 'not-a-list'}))
    number = place(shopper, product)
    assert post(shopper, '/api/orders/%s/pay' % number, {'card': CARD}).status_code == 504


def test_bad_credentials_are_our_configuration_error(paypal, shopper, product):
    fake = FakePayPal()
    fake.send = lambda request: json_response(401, {'error': 'invalid_client'})
    gateway.set_client(PaypalClient(base_url='https://paypal.test', custom_http_client=fake,
                                    oauth2={'client_id': 'id', 'client_secret': 'bad'}))
    number = place(shopper, product)
    response = post(shopper, '/api/orders/%s/pay' % number, {'card': CARD})
    assert response.status_code == 502
    assert ProviderWrite.objects.get().outcome == 'failed'


def test_a_card_rejection_is_the_callers_to_fix(paypal, shopper, product):
    paypal.on('POST', ORDERS, json_response(422, {
        'name': 'UNPROCESSABLE_ENTITY', 'message': 'The requested action could not be performed.',
        'debug_id': 'd1', 'details': [{'issue': 'CARD_EXPIRED', 'description': 'The card is expired.'}]}))
    number = place(shopper, product)
    response = post(shopper, '/api/orders/%s/pay' % number, {'card': CARD})
    assert response.status_code == 422
    assert response.json()['issues'] == ['CARD_EXPIRED']


def test_fulfil_captures_and_records_paypals_fee_and_net(paypal, shopper, operator, product):
    number = fulfilled_order(paypal, shopper, operator, product)
    again = post(operator, '/api/orders/%s/fulfil' % number)
    assert again.status_code == 200
    assert len(paypal.calls('POST', CAPTURE)) == 1
    capture = again.json()['payment']['capture']
    assert capture == {'id': 'CAP1', 'status': 'COMPLETED', 'amount': '25.00',
                       'paypalFee': '1.22', 'netAmount': '23.78'}
    assert again.json()['status'] == 'Complete'
    assert paypal.calls('POST', CAPTURE)[0].headers['paypal-request-id'] == 'test:ord:%s:capture:AUTH1' % number


def test_fulfil_is_staff_only(paypal, shopper, product):
    number = authorized_order(paypal, shopper, product)
    assert post(shopper, '/api/orders/%s/fulfil' % number).status_code == 403


def test_a_stale_authorization_is_renewed_before_capture(paypal, shopper, operator, product):
    number = authorized_order(paypal, shopper, product)
    stale = timezone.now() - timedelta(days=5)
    paypal.on('GET', r'/v2/payments/authorizations/AUTH1', json_response(200, authorization_body(created=stale)))
    paypal.on('POST', r'/v2/payments/authorizations/AUTH1/reauthorize',
              json_response(201, authorization_body(auth_id='AUTH9')))
    paypal.on('POST', CAPTURE, json_response(201, capture_body()))
    response = post(operator, '/api/orders/%s/fulfil' % number)
    assert response.status_code == 200
    assert httpx.URL(paypal.calls('POST', CAPTURE)[0].url).path == '/v2/payments/authorizations/AUTH9/capture'


def test_an_authorization_past_renewal_says_what_to_do(paypal, shopper, operator, product):
    number = authorized_order(paypal, shopper, product)
    paypal.on('GET', r'/v2/payments/authorizations/AUTH1',
              json_response(200, authorization_body(created=timezone.now() - timedelta(days=30))))
    response = post(operator, '/api/orders/%s/fulfil' % number)
    assert response.status_code == 409
    assert response.json()['error'] == 'authorization_expired'
    assert 'cancel' in response.json()['message'].lower()
    assert paypal.calls('POST', CAPTURE) == []


def test_cancel_before_fulfilment_voids_the_hold(paypal, shopper, operator, product):
    number = authorized_order(paypal, shopper, product)
    paypal.on('POST', r'/v2/payments/authorizations/AUTH1/void',
              json_response(200, authorization_body(status='VOIDED')))
    response = post(operator, '/api/orders/%s/cancel' % number)
    assert response.status_code == 200
    assert response.json()['status'] == 'Cancelled'
    assert response.json()['payment']['state'] == 'voided'
    assert post(operator, '/api/orders/%s/cancel' % number).status_code == 200
    assert len(paypal.calls('POST', r'/v2/payments/authorizations/AUTH1/void')) == 1
    assert post(operator, '/api/orders/%s/fulfil' % number).status_code == 409


def test_cancel_after_fulfilment_is_refused(paypal, shopper, operator, product):
    number = fulfilled_order(paypal, shopper, operator, product)
    assert post(operator, '/api/orders/%s/cancel' % number).status_code == 409


def test_refunds_are_idempotent_per_key_and_never_exceed_the_capture(paypal, shopper, operator, product):
    number = fulfilled_order(paypal, shopper, operator, product)
    paypal.on('POST', r'/v2/payments/captures/CAP1/refund', refund_answer)
    url = '/api/orders/%s/refunds' % number

    first = post(shopper, url, {'amount': '10.00'}, **{'Idempotency-Key': 'k1'})
    repeat = post(shopper, url, {'amount': '10.00'}, **{'Idempotency-Key': 'k1'})
    second = post(shopper, url, {'amount': '10.00'}, **{'Idempotency-Key': 'k2'})
    too_much = post(shopper, url, {'amount': '5.01'}, **{'Idempotency-Key': 'k3'})
    rest = post(shopper, url, {}, **{'Idempotency-Key': 'k4'})

    assert first.status_code == 201 and repeat.status_code == 201 and second.status_code == 201
    assert first.json()['refundId'] == repeat.json()['refundId'] != second.json()['refundId']
    assert too_much.status_code == 422
    assert rest.status_code == 201 and rest.json()['amount'] == '5.00'
    assert len(paypal.calls('POST', r'/v2/payments/captures/CAP1/refund')) == 3
    assert rest.json()['payment']['state'] == 'refunded'
    assert rest.json()['payment']['amountRefunded'] == '25.00'
    assert post(shopper, url, {'amount': '1.00'}, **{'Idempotency-Key': 'k5'}).status_code == 409
    assert post(shopper, url, {'amount': '9.00'}, **{'Idempotency-Key': 'k1'}).status_code == 422


def test_refund_requires_an_idempotency_key(paypal, shopper, operator, product):
    number = fulfilled_order(paypal, shopper, operator, product)
    assert post(shopper, '/api/orders/%s/refunds' % number, {'amount': '1.00'}).status_code == 400


def test_one_shopper_never_sees_or_acts_on_anothers_order(paypal, shopper, product):
    number = authorized_order(paypal, shopper, product)
    other = Client()
    other.force_login(make_user('other'))
    assert other.get('/api/my-orders').json()['orders'] == []
    assert post(other, '/api/orders/%s/pay' % number, {'card': CARD}).status_code == 404
    assert post(other, '/api/orders/%s/refunds' % number, {}, **{'Idempotency-Key': 'x'}).status_code == 404
    assert [o['orderId'] for o in shopper.get('/api/my-orders').json()['orders']] == [number]


def test_anonymous_callers_are_rejected(paypal):
    assert Client().get('/api/my-orders').status_code == 401


# ---- Flow 2 ----------------------------------------------------------------

TOKEN = {'id': 'TOK1', 'customer': {'id': 'CUST1'},
         'payment_source': {'card': {'brand': 'VISA', 'last_digits': '1111', 'expiry': '2030-12'}}}


def test_save_list_pay_with_and_delete_a_card(paypal, shopper, product):
    paypal.on('POST', r'/v3/vault/payment-tokens', json_response(200, TOKEN))
    saved = post(shopper, '/api/payment-methods', {'card': CARD}, **{'Idempotency-Key': 'save-1'})
    again = post(shopper, '/api/payment-methods', {'card': CARD}, **{'Idempotency-Key': 'save-1'})
    assert saved.status_code == 201 and again.json()['paymentMethodId'] == saved.json()['paymentMethodId']
    assert len(paypal.calls('POST', r'/v3/vault/payment-tokens')) == 1
    method_id = saved.json()['paymentMethodId']
    assert saved.json()['lastDigits'] == '1111' and 'number' not in saved.json()
    assert [c['paymentMethodId'] for c in shopper.get('/api/payment-methods').json()['paymentMethods']] == [method_id]

    paypal.on('POST', ORDERS, json_response(201, order_body()))
    number = place(shopper, product)
    assert post(shopper, '/api/orders/%s/pay' % number, {'paymentMethodId': method_id}).status_code == 200
    assert paypal.calls('POST', ORDERS)[0].body.value['payment_source'] == {'card': {'vault_id': 'TOK1'}}

    paypal.on('DELETE', r'/v3/vault/payment-tokens/TOK1', HttpResponse(status_code=204, headers={}))
    assert shopper.delete('/api/payment-methods/%s' % method_id).status_code == 204
    assert shopper.get('/api/payment-methods').json()['paymentMethods'] == []
    later = place(shopper, product)
    assert post(shopper, '/api/orders/%s/pay' % later, {'paymentMethodId': method_id}).status_code == 404


def test_a_card_belongs_to_the_shopper_who_saved_it(paypal, shopper, product):
    paypal.on('POST', r'/v3/vault/payment-tokens', json_response(200, TOKEN))
    method_id = post(shopper, '/api/payment-methods', {'card': CARD}).json()['paymentMethodId']
    other = Client()
    other.force_login(make_user('other'))
    assert other.get('/api/payment-methods').json()['paymentMethods'] == []
    assert other.delete('/api/payment-methods/%s' % method_id).status_code == 404
    order = place(other, product)
    assert post(other, '/api/orders/%s/pay' % order, {'paymentMethodId': method_id}).status_code == 404


def test_card_details_are_never_stored_or_logged(paypal, shopper, product, caplog):
    caplog.set_level('DEBUG')
    paypal.on('POST', r'/v3/vault/payment-tokens', json_response(200, TOKEN))
    paypal.on('POST', ORDERS, json_response(201, order_body()))
    post(shopper, '/api/payment-methods', {'card': CARD})
    post(shopper, '/api/orders/%s/pay' % place(shopper, product), {'card': CARD})
    from django.db import connection
    with connection.cursor() as cursor:
        for table in connection.introspection.table_names():
            cursor.execute('SELECT * FROM "%s"' % table)
            assert PAN not in repr(cursor.fetchall()), table
    assert PAN not in caplog.text and '123' not in [r.getMessage() for r in caplog.records]


# ---- Reconciliation ---------------------------------------------------------

def test_reconciliation_covers_every_page_and_lines_both_sides_up(paypal, shopper, operator, product):
    fulfilled_order(paypal, shopper, operator, product)

    def page(request):
        params = httpx.URL(request.url).params
        n = int(params['page'])
        rows = {1: [{'transaction_info': {'transaction_id': 'CAP1', 'transaction_event_code': 'T0006',
                                          'transaction_initiation_date': '2026-09-28T10:00:00+0000',
                                          'transaction_amount': {'currency_code': 'USD', 'value': '25.00'},
                                          'transaction_status': 'S'}}],
                2: [{'transaction_info': {'transaction_id': 'STRANGER', 'transaction_event_code': 'T0006',
                                          'transaction_initiation_date': '2026-09-28T12:00:00+0000',
                                          'transaction_amount': {'currency_code': 'USD', 'value': '3.00'},
                                          'transaction_status': 'S'}}]}
        return json_response(200, {'transaction_details': rows[n], 'page': n, 'total_pages': 2})

    paypal.on('GET', r'/v1/reporting/transactions', page)
    response = operator.get('/api/reconciliation', {'from': '2026-09-27T00:00:00Z', 'to': '2026-09-29T00:00:00Z'})
    assert response.status_code == 200, response.content
    report = response.json()
    assert report['summary'] == {'paypalTransactions': 2, 'matched': 1, 'appOnly': 0, 'paypalOnly': 1, 'unsettled': 0}
    assert report['paypalOnly'][0]['transactionId'] == 'STRANGER'
    assert report['matched'][0]['amountsAgree'] is True
    assert shopper.get('/api/reconciliation', {'from': '2026-09-27T00:00:00Z',
                                               'to': '2026-09-29T00:00:00Z'}).status_code == 403


def test_reconciliation_splits_ranges_longer_than_31_days(paypal, operator):
    paypal.on('GET', r'/v1/reporting/transactions', json_response(200, {'transaction_details': [], 'total_pages': 1}))
    response = operator.get('/api/reconciliation', {'from': '2026-01-01T00:00:00Z', 'to': '2026-03-15T00:00:00Z'})
    assert response.status_code == 200
    windows = [(httpx.URL(r.url).params['start_date'], httpx.URL(r.url).params['end_date'])
               for r in paypal.calls('GET', r'/v1/reporting/transactions')]
    assert windows == [('2026-01-01T00:00:00Z', '2026-02-01T00:00:00Z'),
                       ('2026-02-01T00:00:00Z', '2026-03-04T00:00:00Z'),
                       ('2026-03-04T00:00:00Z', '2026-03-15T00:00:00Z')]

