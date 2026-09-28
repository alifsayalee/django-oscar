"""
Tests for the PayPal payments API. PayPal is faked at the SDK's transport seam (a stub
``HttpClient`` passed as ``custom_http_client``), so the real SDK builds and decodes every request.

Run: venv\\Scripts\\python sandbox\\manage.py test apps.paypal_payments
"""
import json
import re
from datetime import timedelta
from decimal import Decimal as D

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from oscar.test.factories import create_product
from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpRequest, HttpResponse

from . import gateway
from .models import PayPalPayment, PayPalRefund, ProviderWrite, SavedCard

User = get_user_model()

CARD = {'number': '4111111111111111', 'expiry': '2030-12', 'securityCode': '123', 'name': 'Test Shopper',
        'billingAddress': {'addressLine1': '1 Main St', 'city': 'San Jose', 'state': 'CA',
                           'postalCode': '95131', 'countryCode': 'US'}}


def json_response(status, body=None):
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=b'' if body is None else json.dumps(body).encode())


def token_response():
    return json_response(200, {'access_token': 't', 'token_type': 'Bearer', 'expires_in': 3600})


class StubTransport:
    """Routes by method + path; each route answers from a queue (the last entry repeats).
    An Exception in the queue is raised instead of answered."""

    def __init__(self):
        self.routes = []
        self.requests = []

    def on(self, method, pattern, *responses):
        self.routes.append((method, re.compile(pattern), list(responses)))
        return self

    def send(self, request: HttpRequest) -> HttpResponse:
        path = httpx.URL(request.url).path
        if path == '/v1/oauth2/token':
            return token_response()
        self.requests.append(request)
        for method, pattern, queue in reversed(self.routes):
            if method == request.method and pattern.fullmatch(path):
                item = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(item, Exception):
                    raise item
                return item(request) if callable(item) else item
        raise AssertionError('Unexpected PayPal call %s %s' % (request.method, path))

    def close(self):
        pass

    def calls(self, method, pattern):
        rx = re.compile(pattern)
        return [r for r in self.requests if r.method == method and rx.fullmatch(httpx.URL(r.url).path)]


def order_body(amount='20.00', status='COMPLETED', auth_status='CREATED', auth_id='AUTH1'):
    now = timezone.now().replace(microsecond=0)
    return {
        'id': 'PAYPALORDER1', 'status': status,
        'payment_source': {'card': {'last_digits': '1111', 'brand': 'VISA', 'expiry': '2030-12'}},
        'purchase_units': [{'payments': {'authorizations': [{
            'id': auth_id, 'status': auth_status, 'amount': {'currency_code': 'USD', 'value': amount},
            'create_time': now.isoformat().replace('+00:00', 'Z'),
            'expiration_time': (now + timedelta(days=29)).isoformat().replace('+00:00', 'Z')}]}}]}


def capture_body(amount='20.00', status='COMPLETED'):
    return {'id': 'CAP1', 'status': status, 'amount': {'currency_code': 'USD', 'value': amount},
            'create_time': '2026-09-01T10:00:00Z',
            'seller_receivable_breakdown': {'gross_amount': {'currency_code': 'USD', 'value': amount},
                                            'paypal_fee': {'currency_code': 'USD', 'value': '0.88'},
                                            'net_amount': {'currency_code': 'USD', 'value': '19.12'}}}


def refund_body(amount, status='COMPLETED', refund_id='REF1'):
    return {'id': refund_id, 'status': status, 'amount': {'currency_code': 'USD', 'value': amount},
            'create_time': '2026-09-01T11:00:00Z'}


def authorization_body(status, auth_id='AUTH1'):
    now = timezone.now().replace(microsecond=0)
    return {'id': auth_id, 'status': status, 'amount': {'currency_code': 'USD', 'value': '20.00'},
            'create_time': now.isoformat().replace('+00:00', 'Z'),
            'expiration_time': (now + timedelta(days=29)).isoformat().replace('+00:00', 'Z')}


def vault_body(token_id='TOK1'):
    return {'id': token_id, 'customer': {'id': 'CUST1'},
            'payment_source': {'card': {'last_digits': '1111', 'brand': 'VISA', 'expiry': '2030-12',
                                        'name': 'Test Shopper'}}}


def header(request, name):
    return request.headers.get(name.lower())


@override_settings(PAYPAL_CLIENT_ID='test-id', PAYPAL_CLIENT_SECRET='test-secret', PAYPAL_CURRENCY='USD',
                   PAYPAL_ENVIRONMENT='sandbox', PAYPAL_BASE_URL='', PAYPAL_REFERENCE_PREFIX='test',
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class PayPalApiTestCase(TestCase):

    def setUp(self):
        self.transport = StubTransport()
        gateway.set_client(PaypalClient(
            base_url='https://paypal.test', custom_http_client=gateway.LoggingTransport(self.transport),
            oauth2=ClientCredentials(client_id='test-id', client_secret='test-secret')))
        self.shopper = User.objects.create_user('shopper', 'shopper@example.com', 'password-123')
        self.other = User.objects.create_user('other', 'other@example.com', 'password-123')
        self.operator = User.objects.create_user('op', 'op@example.com', 'password-123', is_staff=True)
        self.product = create_product(price=D('10.00'), num_in_stock=50)
        self.client.force_login(self.shopper)

    def tearDown(self):
        gateway.set_client(None)

    # helpers -------------------------------------------------------------------------------
    def as_user(self, user):
        self.client.logout()
        self.client.force_login(user)

    def post(self, url, body=None, **headers):
        return self.client.post(url, data=json.dumps(body or {}), content_type='application/json',
                                headers=headers)

    def place_order(self, quantity=2):
        response = self.post('/api/orders', {'items': [{'productId': self.product.pk, 'quantity': quantity}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()['orderId']

    def authorized_order(self):
        self.transport.on('POST', '/v2/checkout/orders', json_response(201, order_body()))
        number = self.place_order()
        response = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(response.status_code, 200, response.content)
        return number

    def captured_order(self):
        number = self.authorized_order()
        self.transport.on('POST', r'/v2/payments/authorizations/AUTH1/capture', json_response(201, capture_body()))
        self.as_user(self.operator)
        response = self.post('/api/orders/%s/fulfil' % number)
        self.assertEqual(response.status_code, 200, response.content)
        return number


class OrderAndPayTests(PayPalApiTestCase):

    def test_place_order_uses_catalogue_prices_and_configured_currency(self):
        number = self.place_order(quantity=2)
        payment = PayPalPayment.objects.get(order__number=number)
        self.assertEqual((payment.amount, payment.currency, payment.state), (D('20.00'), 'USD', 'awaiting_payment'))
        self.assertEqual(payment.order.status, 'Awaiting payment')
        self.assertEqual(payment.order.lines.get().quantity, 2)

    def test_pay_authorizes_order_total_and_never_stores_card(self):
        number = self.authorized_order()
        request = self.transport.calls('POST', '/v2/checkout/orders')[0]
        body = request.body.value
        self.assertEqual(body['intent'], 'AUTHORIZE')
        self.assertEqual(body['purchase_units'][0]['amount'], {'currency_code': 'USD', 'value': '20.00'})
        self.assertEqual(header(request, 'PayPal-Request-Id'), 'test:order:%s:authorize:1' % number)
        payment = PayPalPayment.objects.get(order__number=number)
        self.assertEqual((payment.state, payment.authorization_id), ('authorized', 'AUTH1'))
        self.assertEqual(payment.order.status, 'Payment authorized')
        for write in ProviderWrite.objects.all():
            self.assertNotIn('4111111111111111', json.dumps([write.data, write.error, write.ref]))

    def test_double_submit_sends_one_authorization(self):
        self.transport.on('POST', '/v2/checkout/orders', json_response(201, order_body()))
        number = self.place_order()
        first = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        second = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(len(self.transport.calls('POST', '/v2/checkout/orders')), 1)
        self.assertEqual(second.json()['write']['paypalId'], 'AUTH1')

    def test_in_flight_claim_answers_in_progress_without_a_provider_call(self):
        number = self.place_order()
        ProviderWrite.objects.create(ref='test:order:%s:authorize:1' % number, kind='authorize',
                                     claimed_at=timezone.now())
        response = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.transport.requests, [])

    def test_declined_authorization_is_not_success_and_next_attempt_gets_new_reference(self):
        self.transport.on('POST', '/v2/checkout/orders',
                          json_response(201, order_body(auth_status='DENIED')),
                          json_response(201, order_body(auth_id='AUTH2')))
        number = self.place_order()
        declined = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(declined.status_code, 409)
        self.assertEqual(PayPalPayment.objects.get(order__number=number).state, 'awaiting_payment')
        retried = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(retried.status_code, 200)
        refs = [header(r, 'PayPal-Request-Id') for r in self.transport.calls('POST', '/v2/checkout/orders')]
        self.assertEqual(refs, ['test:order:%s:authorize:1' % number, 'test:order:%s:authorize:2' % number])

    def test_unlisted_status_is_unknown_not_done(self):
        self.transport.on('POST', '/v2/checkout/orders',
                          json_response(201, order_body(auth_status='SOMETHING_NEW')))
        number = self.place_order()
        response = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(response.status_code, 504)
        self.assertTrue(response.json()['outcomeUnknown'])
        self.assertEqual(PayPalPayment.objects.get(order__number=number).state, 'authorizing')

    def test_three_d_secure_challenge_is_reported_not_followed(self):
        body = order_body()
        body.update(status='PAYER_ACTION_REQUIRED', purchase_units=[{}])
        self.transport.on('POST', '/v2/checkout/orders', json_response(200, body))
        number = self.place_order()
        response = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['order']['payment']['lastError']['code'], 'payer_action_required')

    def test_amount_echo_mismatch_needs_review(self):
        self.transport.on('POST', '/v2/checkout/orders', json_response(201, order_body(amount='19.99')))
        number = self.place_order()
        response = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['outcome'], 'needs_review')
        self.assertEqual(PayPalPayment.objects.get(order__number=number).state, 'needs_review')

    def test_read_timeout_is_checked_by_resending_the_same_reference(self):
        self.transport.on('POST', '/v2/checkout/orders', httpx.ReadTimeout('no reply'),
                          json_response(200, order_body()))
        number = self.place_order()
        response = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(response.status_code, 200)
        refs = {header(r, 'PayPal-Request-Id') for r in self.transport.calls('POST', '/v2/checkout/orders')}
        self.assertEqual(refs, {'test:order:%s:authorize:1' % number})

    def test_unsent_is_not_the_same_failure_as_unknown(self):
        number = self.place_order()
        self.transport.on('POST', '/v2/checkout/orders', httpx.ConnectError('refused'))
        unsent = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual((unsent.status_code, unsent.json()['error']['outcomeUnknown']), (502, False))
        self.assertFalse(ProviderWrite.objects.exists())          # claim released: nothing happened

        self.transport.on('POST', '/v2/checkout/orders', httpx.ReadTimeout('no reply'))
        unknown = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual((unknown.status_code, unknown.json()['outcomeUnknown']), (504, True))
        self.assertEqual(ProviderWrite.objects.get().outcome, 'unknown')

    def test_refused_request_with_unreadable_error_body_is_a_rejection(self):
        self.transport.on('POST', '/v2/checkout/orders',
                          json_response(422, {'name': 'X', 'message': 'no', 'debug_id': 'd',
                                              'links': [{'href': 'https://x'}]}))   # links[].rel missing
        number = self.place_order()
        response = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(response.json()['error']['outcomeUnknown'])

    def test_bad_credentials_are_our_problem_not_the_callers(self):
        class BadToken(StubTransport):
            def send(self, request):
                if request.url.endswith('/v1/oauth2/token'):
                    return json_response(401, {'error': 'invalid_client'})
                return super().send(request)
        gateway.set_client(PaypalClient(base_url='https://paypal.test',
                                        custom_http_client=gateway.LoggingTransport(BadToken()),
                                        oauth2=ClientCredentials(client_id='x', client_secret='y')))
        number = self.place_order()
        response = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['error']['code'], 'paypal_credentials_rejected')

    def test_orders_are_private_to_their_shopper(self):
        number = self.authorized_order()
        self.as_user(self.other)
        self.assertEqual(self.post('/api/orders/%s/pay' % number, {'card': CARD}).status_code, 404)
        self.assertEqual(self.client.get('/api/my-orders').json()['orders'], [])

    def test_operator_actions_require_staff(self):
        number = self.authorized_order()
        for action in ('fulfil', 'cancel'):
            self.assertEqual(self.post('/api/orders/%s/%s' % (number, action)).status_code, 403)
        self.assertEqual(self.client.get('/api/reconciliation?from=2026-01-01T00:00:00Z&to=2026-01-02T00:00:00Z')
                         .status_code, 403)

    def test_anonymous_is_rejected(self):
        self.client.logout()
        self.assertEqual(self.post('/api/orders', {'items': []}).status_code, 401)


class FulfilCancelTests(PayPalApiTestCase):

    def test_fulfil_captures_and_records_fee_and_net(self):
        number = self.captured_order()
        payment = PayPalPayment.objects.get(order__number=number)
        self.assertEqual((payment.state, payment.captured_amount, payment.paypal_fee, payment.net_amount),
                         ('captured', D('20.00'), D('0.88'), D('19.12')))
        self.assertEqual(payment.order.status, 'Complete')
        capture = self.transport.calls('POST', r'/v2/payments/authorizations/AUTH1/capture')
        self.assertEqual(len(capture), 1)
        self.assertEqual(capture[0].body.value['amount'], {'currency_code': 'USD', 'value': '20.00'})
        again = self.post('/api/orders/%s/fulfil' % number)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(self.transport.calls('POST', r'.*/capture')), 1)

    def test_stale_authorization_is_renewed_before_capture(self):
        number = self.authorized_order()
        PayPalPayment.objects.filter(order__number=number).update(
            authorized_at=timezone.now() - timedelta(days=5),
            original_authorized_at=timezone.now() - timedelta(days=5))
        self.transport.on('POST', r'/v2/payments/authorizations/AUTH1/reauthorize',
                          json_response(201, authorization_body('CREATED', auth_id='AUTH2')))
        self.transport.on('POST', r'/v2/payments/authorizations/AUTH2/capture', json_response(201, capture_body()))
        self.as_user(self.operator)
        response = self.post('/api/orders/%s/fulfil' % number)
        self.assertEqual(response.status_code, 200, response.content)
        payment = PayPalPayment.objects.get(order__number=number)
        self.assertEqual((payment.authorization_id, payment.reauthorization_count, payment.state),
                         ('AUTH2', 1, 'captured'))

    def test_authorization_too_old_to_renew_says_what_to_do(self):
        number = self.authorized_order()
        PayPalPayment.objects.filter(order__number=number).update(
            authorized_at=timezone.now() - timedelta(days=30),
            original_authorized_at=timezone.now() - timedelta(days=30))
        self.as_user(self.operator)
        response = self.post('/api/orders/%s/fulfil' % number)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'authorization_expired')
        self.assertIn('Cancel this order', response.json()['error']['message'])
        self.assertEqual(self.transport.calls('POST', r'.*/(reauthorize|capture)'), [])
        self.assertEqual(PayPalPayment.objects.get(order__number=number).state, 'authorized')

    def test_refused_renewal_is_reported_for_the_operator(self):
        number = self.authorized_order()
        PayPalPayment.objects.filter(order__number=number).update(
            authorized_at=timezone.now() - timedelta(days=5))
        self.transport.on('POST', r'/v2/payments/authorizations/AUTH1/reauthorize', json_response(422, {
            'name': 'UNPROCESSABLE_ENTITY', 'message': 'The requested action could not be performed.',
            'debug_id': 'abc', 'details': [{'issue': 'REAUTHORIZATION_NOT_ALLOWED'}]}))
        self.as_user(self.operator)
        response = self.post('/api/orders/%s/fulfil' % number)
        self.assertEqual(response.status_code, 422)
        error = response.json()['error']
        self.assertEqual(error['code'], 'authorization_renewal_failed')
        self.assertIn('REAUTHORIZATION_NOT_ALLOWED', error['message'])

    def test_cancel_before_fulfilment_voids_the_hold(self):
        number = self.authorized_order()
        self.transport.on('POST', r'/v2/payments/authorizations/AUTH1/void',
                          json_response(200, authorization_body('VOIDED')))
        self.as_user(self.operator)
        response = self.post('/api/orders/%s/cancel' % number)
        self.assertEqual(response.status_code, 200)
        payment = PayPalPayment.objects.get(order__number=number)
        self.assertEqual((payment.state, payment.order.status), ('voided', 'Cancelled'))
        self.assertEqual(self.post('/api/orders/%s/fulfil' % number).status_code, 409)

    def test_cancel_after_fulfilment_is_refused(self):
        number = self.captured_order()
        self.assertEqual(self.post('/api/orders/%s/cancel' % number).status_code, 409)


class RefundTests(PayPalApiTestCase):

    def test_partial_refunds_never_exceed_capture_and_keys_are_idempotent(self):
        number = self.captured_order()
        self.transport.on('POST', r'/v2/payments/captures/CAP1/refund',
                          lambda req: json_response(201, refund_body(req.body.value['amount']['value'],
                                                                     refund_id=header(req, 'PayPal-Request-Id')[-6:])))
        url = '/api/orders/%s/refunds' % number
        first = self.post(url, {'amount': '7.50'}, **{'Idempotency-Key': 'a'})
        self.assertEqual(first.status_code, 201, first.content)
        self.assertIn('refundId', first.json())
        repeat = self.post(url, {'amount': '7.50'}, **{'Idempotency-Key': 'a'})
        self.assertEqual(repeat.json()['refundId'], first.json()['refundId'])
        second = self.post(url, {'amount': '7.50'}, **{'Idempotency-Key': 'b'})
        self.assertEqual(second.status_code, 201)
        too_much = self.post(url, {'amount': '5.01'}, **{'Idempotency-Key': 'c'})
        self.assertEqual(too_much.status_code, 409)
        rest = self.post(url, {}, **{'Idempotency-Key': 'd'})
        self.assertEqual(rest.json()['refund']['amount'], '5.00')
        self.assertEqual(len(self.transport.calls('POST', r'.*/refund')), 3)
        payment = PayPalPayment.objects.get(order__number=number)
        self.assertEqual((payment.refunded_amount, payment.state), (D('20.00'), 'refunded'))
        self.assertEqual(self.post(url, {'amount': '1.00'}, **{'Idempotency-Key': 'e'}).status_code, 409)

    def test_reused_key_with_different_amount_is_rejected(self):
        number = self.captured_order()
        self.transport.on('POST', r'/v2/payments/captures/CAP1/refund', json_response(201, refund_body('3.00')))
        url = '/api/orders/%s/refunds' % number
        self.post(url, {'amount': '3.00'}, **{'Idempotency-Key': 'k'})
        self.assertEqual(self.post(url, {'amount': '4.00'}, **{'Idempotency-Key': 'k'}).status_code, 422)

    def test_failed_refund_releases_its_reservation(self):
        number = self.captured_order()
        self.transport.on('POST', r'/v2/payments/captures/CAP1/refund',
                          json_response(201, refund_body('20.00', status='FAILED')))
        response = self.post('/api/orders/%s/refunds' % number, {}, **{'Idempotency-Key': 'f'})
        self.assertEqual(response.status_code, 409)
        payment = PayPalPayment.objects.get(order__number=number)
        self.assertEqual((payment.refund_reserved, payment.refunded_amount), (D('0.00'), D('0.00')))

    def test_refund_requires_idempotency_key(self):
        number = self.captured_order()
        self.assertEqual(self.post('/api/orders/%s/refunds' % number, {'amount': '1.00'}).status_code, 400)


class SavedCardTests(PayPalApiTestCase):

    def save_card(self):
        self.transport.on('POST', '/v3/vault/payment-tokens', json_response(200, vault_body()))
        response = self.post('/api/payment-methods', {'card': CARD})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()['paymentMethodId']

    def test_save_list_pay_delete(self):
        pm = self.save_card()
        listed = self.client.get('/api/payment-methods').json()['paymentMethods']
        self.assertEqual([(c['paymentMethodId'], c['lastDigits']) for c in listed], [(pm, '1111')])
        self.assertNotIn('4111111111111111', json.dumps(listed))
        self.assertFalse(SavedCard.objects.filter(last_digits='4111111111111111').exists())

        self.transport.on('POST', '/v2/checkout/orders', json_response(201, order_body()))
        number = self.place_order()
        paid = self.post('/api/orders/%s/pay' % number, {'paymentMethodId': pm})
        self.assertEqual(paid.status_code, 200)
        source = self.transport.calls('POST', '/v2/checkout/orders')[-1].body.value['payment_source']
        self.assertEqual(source, {'card': {'vault_id': 'TOK1'}})

        self.transport.on('DELETE', '/v3/vault/payment-tokens/TOK1', HttpResponse(status_code=204, headers={}))
        self.assertEqual(self.client.delete('/api/payment-methods/%s' % pm).status_code, 200)
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])
        number2 = self.place_order()
        self.assertEqual(self.post('/api/orders/%s/pay' % number2, {'paymentMethodId': pm}).status_code, 404)

    def test_same_card_saved_twice_is_one_write(self):
        first = self.save_card()
        second = self.post('/api/payment-methods', {'card': CARD})
        self.assertEqual(second.json()['paymentMethodId'], first)
        self.assertEqual(len(self.transport.calls('POST', '/v3/vault/payment-tokens')), 1)

    def test_cards_are_private_to_their_shopper(self):
        pm = self.save_card()
        self.as_user(self.other)
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])
        self.assertEqual(self.client.delete('/api/payment-methods/%s' % pm).status_code, 404)
        self.transport.on('POST', '/v2/checkout/orders', json_response(201, order_body()))
        number = self.place_order()
        self.assertEqual(self.post('/api/orders/%s/pay' % number, {'paymentMethodId': pm}).status_code, 404)
        self.assertEqual(self.transport.calls('POST', '/v2/checkout/orders'), [])

    def test_delete_with_unknown_outcome_keeps_card_unusable_and_resends(self):
        pm = self.save_card()
        self.transport.on('DELETE', '/v3/vault/payment-tokens/TOK1', httpx.ReadTimeout('x'),
                          httpx.ReadTimeout('x'), json_response(404, {}))
        first = self.client.delete('/api/payment-methods/%s' % pm)
        self.assertEqual(first.status_code, 504)
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])
        second = self.client.delete('/api/payment-methods/%s' % pm)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(SavedCard.objects.get().state, 'deleted')

    def test_invalid_card_is_rejected_before_paypal(self):
        response = self.post('/api/payment-methods', {'card': dict(CARD, number='4111111111111112')})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.transport.requests, [])


class ReconciliationTests(PayPalApiTestCase):

    def txn(self, tid, amount, when, custom=None):
        info = {'transaction_id': tid, 'transaction_event_code': 'T0006',
                'transaction_initiation_date': when, 'transaction_status': 'S',
                'transaction_amount': {'currency_code': 'USD', 'value': amount}}
        if custom:
            info['custom_field'] = custom
        return {'transaction_info': info}

    def test_reports_whole_range_and_lines_up_both_sides(self):
        number = self.captured_order()
        ProviderWrite.objects.filter(kind='authorize').update(provider_time='2026-09-01T09:59:00Z')
        pages = {
            '1': {'transaction_details': [self.txn('CAP1', '20.00', '2026-09-01T10:00:00Z')],
                  'total_pages': 2, 'page': 1},
            '2': {'transaction_details': [self.txn('FOREIGN1', '5.00', '2026-09-01T12:00:00Z'),
                                          self.txn('OURS9', '9.00', '2026-09-01T12:00:00Z', 'test:999')],
                  'total_pages': 2, 'page': 2},
        }
        self.transport.on('GET', '/v1/reporting/transactions',
                          lambda req: json_response(200, pages[httpx.URL(req.url).params['page']]))
        response = self.client.get('/api/reconciliation', {'from': '2026-09-01T00:00:00Z',
                                                          'to': '2026-09-02T00:00:00Z'})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual(len(self.transport.calls('GET', '/v1/reporting/transactions')), 2)
        self.assertEqual([m['paypal']['transactionId'] for m in report['matched']], ['CAP1'])
        self.assertTrue(report['matched'][0]['amountMatches'])
        self.assertEqual(report['matched'][0]['orderId'], number)
        self.assertEqual(sorted((p['transactionId'], p['source']) for p in report['paypalOnly']),
                         [('FOREIGN1', 'other'), ('OURS9', 'this_app')])
        self.assertEqual([a['kind'] for a in report['appOnly']], ['authorize'])

    def test_long_range_is_split_into_31_day_windows(self):
        self.transport.on('GET', '/v1/reporting/transactions',
                          json_response(200, {'transaction_details': [], 'total_pages': 0, 'page': 1}))
        self.as_user(self.operator)
        response = self.client.get('/api/reconciliation', {'from': '2026-01-01T00:00:00Z',
                                                          'to': '2026-03-15T00:00:00Z'})
        self.assertEqual(response.status_code, 200)
        windows = [(httpx.URL(r.url).params['start_date'], httpx.URL(r.url).params['end_date'])
                   for r in self.transport.calls('GET', '/v1/reporting/transactions')]
        self.assertEqual(windows, [('2026-01-01T00:00:00Z', '2026-02-01T00:00:00Z'),
                                   ('2026-02-01T00:00:00Z', '2026-03-04T00:00:00Z'),
                                   ('2026-03-04T00:00:00Z', '2026-03-15T00:00:00Z')])

    def test_report_failure_is_not_an_empty_report(self):
        self.transport.on('GET', '/v1/reporting/transactions', json_response(500, {}))
        self.as_user(self.operator)
        response = self.client.get('/api/reconciliation', {'from': '2026-09-01T00:00:00Z',
                                                          'to': '2026-09-02T00:00:00Z'})
        self.assertEqual(response.status_code, 502)


class ConfigTests(TestCase):

    def test_base_url_override_is_used_verbatim(self):
        config = gateway.PayPalConfig('i', 's', 'live', 'USD', 'https://proxy.example/paypal')
        self.assertEqual(config.resolved_base_url(), 'https://proxy.example/paypal')

    def test_unknown_environment_without_base_url_fails(self):
        with self.assertRaises(gateway.ConfigurationError):
            gateway.PayPalConfig('i', 's', 'live', 'USD', None).validate()

    def test_missing_credentials_fail_fast(self):
        with self.assertRaises(gateway.ConfigurationError):
            gateway.PayPalConfig('', 's', 'sandbox', 'USD', None).validate()

    def test_money_is_scaled_to_the_currency(self):
        self.assertEqual(gateway.money_str(D('10'), 'USD'), '10.00')
        self.assertEqual(gateway.money_str(D('1000'), 'JPY'), '1000')
