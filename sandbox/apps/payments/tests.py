"""
Tests for the payments API.

PayPal is faked at the SDK's transport seam (``custom_http_client``), so the
real SDK builds every request and decodes every response; nothing goes over
the network. Run from ``sandbox/``: ``python manage.py test apps.payments``.
"""
import json
import re
from datetime import timedelta
from decimal import Decimal

import httpx
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from oscar.test.factories import create_product
from paypal.core import HttpRequest, HttpResponse

from . import client as paypal_client
from .models import OrderPayment, PaymentState, ProviderWrite, SavedCard

CARD = {'number': '4111 1111 1111 1111', 'expiry': '2030-12', 'securityCode': '123',
        'name': 'Test Shopper'}


def json_response(status, body):
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


def token_response():
    return json_response(200, {'access_token': 't', 'token_type': 'Bearer', 'expires_in': 3600})


class StubTransport:
    """Answers by route; records every request. A queued exception is raised instead."""

    def __init__(self):
        self.routes = []  # [method, compiled path regex, list of answers]
        self.requests: list[HttpRequest] = []

    def on(self, method, path, *answers):
        self.routes.append([method, re.compile(path + '$'), list(answers)])
        return self

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = httpx.URL(request.url).path
        if path == '/v1/oauth2/token':
            return token_response()
        for method, pattern, answers in self.routes:
            if method == request.method and pattern.match(path) and answers:
                answer = answers.pop(0) if len(answers) > 1 else answers[0]
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError('unexpected PayPal call %s %s' % (request.method, path))

    def close(self):
        pass

    def calls(self, method, path):
        pattern = re.compile(path + '$')
        return [r for r in self.requests
                if r.method == method and pattern.match(httpx.URL(r.url).path)]


def iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def money(value):
    return {'currency_code': 'USD', 'value': value}


def order_body(auth_status='CREATED', amount='15.98', auth_id='AUTH1', created=None,
               order_status='COMPLETED'):
    created = created or timezone.now()
    body = {'id': 'PAYPALORDER1', 'status': order_status,
            'payment_source': {'card': {'last_digits': '1111', 'brand': 'VISA'}},
            'purchase_units': [{'amount': money(amount)}]}
    if order_status == 'COMPLETED':
        body['purchase_units'][0]['payments'] = {'authorizations': [{
            'id': auth_id, 'status': auth_status, 'amount': money(amount),
            'create_time': iso(created), 'expiration_time': iso(created + timedelta(days=29))}]}
    return body


def auth_body(auth_id='AUTH1', status='CREATED', created=None, amount='15.98'):
    return {'id': auth_id, 'status': status, 'amount': money(amount),
            'create_time': iso(created or timezone.now())}


def capture_body(capture_id='CAP1', status='COMPLETED', amount='15.98'):
    return {'id': capture_id, 'status': status, 'amount': money(amount),
            'create_time': iso(timezone.now()),
            'seller_receivable_breakdown': {'gross_amount': money(amount),
                                            'paypal_fee': money('0.84'),
                                            'net_amount': money('15.14')}}


def refund_body(refund_id, amount, status='COMPLETED'):
    return {'id': refund_id, 'status': status, 'amount': money(amount),
            'create_time': iso(timezone.now())}


def error_body(issue, status_name='UNPROCESSABLE_ENTITY'):
    return {'name': status_name, 'message': 'The requested action could not be performed.',
            'debug_id': 'dbg1', 'details': [{'issue': issue, 'description': issue}]}


@override_settings(PAYPAL_CLIENT_ID='test-id', PAYPAL_CLIENT_SECRET='test-secret',
                   PAYPAL_ENVIRONMENT='sandbox', PAYPAL_CURRENCY='USD', PAYPAL_BASE_URL='',
                   PAYPAL_REQUEST_PREFIX='test')
class PaymentsApiTestCase(TestCase):

    def setUp(self):
        self.transport = StubTransport()
        paypal_client.set_client(paypal_client.build_client(http_client=self.transport))
        self.product = create_product(price=Decimal('7.99'), num_in_stock=50)
        self.alice = User.objects.create_user('alice', 'alice@example.com', 'pw')
        self.bob = User.objects.create_user('bob', 'bob@example.com', 'pw')
        self.staff = User.objects.create_user('ops', 'ops@example.com', 'pw', is_staff=True)
        self.shopper = self.client_for(self.alice)
        self.operator = self.client_for(self.staff)

    def tearDown(self):
        paypal_client.set_client(None)

    def client_for(self, user):
        c = Client()
        c.force_login(user)
        return c

    def post(self, client, url, body=None, **headers):
        return client.post(url, data=json.dumps(body or {}), content_type='application/json',
                           headers=headers)

    def place_order(self, client=None, quantity=2):
        response = self.post(client or self.shopper, '/api/orders',
                             {'lines': [{'productId': self.product.pk, 'quantity': quantity}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()['orderId']

    def authorized_order(self, **kwargs):
        number = self.place_order()
        self.transport.on('POST', '/v2/checkout/orders', json_response(201, order_body(**kwargs)))
        response = self.post(self.shopper, '/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(response.status_code, 200, response.content)
        return number

    def captured_order(self):
        number = self.authorized_order()
        self.transport.on('GET', '/v2/payments/authorizations/AUTH1', json_response(200, auth_body()))
        self.transport.on('POST', '/v2/payments/authorizations/AUTH1/capture',
                          json_response(201, capture_body()))
        response = self.post(self.operator, '/api/orders/%s/fulfil' % number)
        self.assertEqual(response.status_code, 200, response.content)
        return number


class OrderAndPayTests(PaymentsApiTestCase):

    def test_order_starts_awaiting_payment_priced_from_catalogue(self):
        response = self.post(self.shopper, '/api/orders',
                             {'lines': [{'productId': self.product.pk, 'quantity': 2}]})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIsInstance(body['orderId'], str)
        self.assertEqual(body['status'], 'Awaiting payment')
        self.assertEqual(body['total'], '15.98')
        self.assertEqual(body['currency'], 'USD')
        self.assertEqual(body['payment']['state'], 'awaiting_payment')

    def test_requires_login(self):
        response = Client().post('/api/orders', data='{}', content_type='application/json')
        self.assertEqual(response.status_code, 401)

    def test_csrf_is_enforced_for_session_callers(self):
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.alice)
        response = c.post('/api/orders', data='{}', content_type='application/json')
        self.assertEqual(response.status_code, 403)

    def test_pay_holds_the_order_total_under_a_derived_reference(self):
        number = self.authorized_order()
        [call] = self.transport.calls('POST', '/v2/checkout/orders')
        self.assertEqual(call.headers['paypal-request-id'], 'test-ord-%s-a1-create' % number)
        self.assertEqual(call.headers['prefer'], 'return=representation')
        body = call.body.value
        self.assertEqual(body['intent'], 'AUTHORIZE')
        self.assertEqual(body['purchase_units'][0]['amount'], money('15.98'))
        self.assertEqual(body['purchase_units'][0]['custom_id'], 'test-%s' % number)
        self.assertEqual(body['payment_source']['card']['number'], '4111111111111111')
        payment = OrderPayment.objects.get(order__number=number)
        self.assertEqual(payment.state, PaymentState.AUTHORIZED)
        self.assertEqual(payment.authorization_id, 'AUTH1')
        self.assertEqual(payment.order.status, 'Payment authorised')
        assert payment.source is not None
        self.assertEqual(payment.source.amount_allocated, Decimal('15.98'))

    def test_the_same_pay_twice_makes_one_provider_call(self):
        number = self.authorized_order()
        second = self.post(self.shopper, '/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['payment']['state'], 'authorized')
        self.assertEqual(len(self.transport.calls('POST', '/v2/checkout/orders')), 1)

    def test_an_unlisted_status_is_unknown_and_settled_under_the_same_reference(self):
        number = self.place_order()
        self.transport.on('POST', '/v2/checkout/orders',
                          json_response(201, order_body(auth_status='SOMETHING_NEW')),
                          json_response(201, order_body()))
        first = self.post(self.shopper, '/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(first.status_code, 504)
        self.assertEqual(OrderPayment.objects.get(order__number=number).state,
                         PaymentState.AUTH_UNKNOWN)
        second = self.post(self.shopper, '/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(second.status_code, 200)
        refs = {c.headers['paypal-request-id'] for c in self.transport.calls('POST', '/v2/checkout/orders')}
        self.assertEqual(refs, {'test-ord-%s-a1-create' % number})

    def test_a_denied_authorization_is_failed_and_a_retry_is_a_new_attempt(self):
        number = self.place_order()
        self.transport.on('POST', '/v2/checkout/orders',
                          json_response(201, order_body(auth_status='DENIED')),
                          json_response(201, order_body(auth_id='AUTH2')))
        first = self.post(self.shopper, '/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(first.status_code, 402)
        self.assertEqual(first.json()['payment']['state'], 'auth_failed')
        self.assertEqual(first.json()['orderStatus'], 'Awaiting payment')
        second = self.post(self.shopper, '/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(second.status_code, 200)
        refs = [c.headers['paypal-request-id'] for c in self.transport.calls('POST', '/v2/checkout/orders')]
        self.assertEqual(refs, ['test-ord-%s-a1-create' % number, 'test-ord-%s-a2-create' % number])

    def test_a_hold_for_a_different_amount_is_flagged_not_accepted(self):
        number = self.place_order()
        self.transport.on('POST', '/v2/checkout/orders', json_response(201, order_body(amount='15.90')))
        response = self.post(self.shopper, '/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(response.status_code, 409)
        payment = OrderPayment.objects.get(order__number=number)
        self.assertEqual(payment.state, PaymentState.NEEDS_REVIEW)
        self.assertEqual(payment.order.status, 'Awaiting payment')

    def test_payer_action_required_is_reported_not_followed(self):
        number = self.place_order()
        self.transport.on('POST', '/v2/checkout/orders',
                          json_response(200, order_body(order_status='PAYER_ACTION_REQUIRED')))
        response = self.post(self.shopper, '/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(response.status_code, 402)
        self.assertIn('3-D Secure', response.json()['payment']['lastError'])

    def test_never_sent_is_a_known_failure_but_a_timeout_is_unknown(self):
        unsent_order, unknown_order = self.place_order(), self.place_order()
        self.transport.on('POST', '/v2/checkout/orders', httpx.ConnectError('refused'))
        unsent = self.post(self.shopper, '/api/orders/%s/pay' % unsent_order, {'card': CARD})
        self.transport.routes.clear()
        self.transport.on('POST', '/v2/checkout/orders', httpx.ReadTimeout('no reply'))
        unknown = self.post(self.shopper, '/api/orders/%s/pay' % unknown_order, {'card': CARD})

        self.assertEqual((unsent.status_code, unsent.json().get('outcomeUnknown', False)), (502, False))
        self.assertEqual((unknown.status_code, unknown.json().get('outcomeUnknown')), (504, True))
        self.assertEqual(OrderPayment.objects.get(order__number=unsent_order).state,
                         PaymentState.AUTH_FAILED)
        self.assertEqual(OrderPayment.objects.get(order__number=unknown_order).state,
                         PaymentState.AUTH_UNKNOWN)
        self.assertEqual(ProviderWrite.objects.get(ref='test-ord-%s-a1-create' % unknown_order).outcome,
                         'unknown')

    def test_rejected_credentials_are_a_configuration_error(self):
        class BadCredentials(StubTransport):
            def send(self, request):
                self.requests.append(request)
                return json_response(401, {'error': 'invalid_client'})
        paypal_client.set_client(paypal_client.build_client(http_client=BadCredentials()))
        number = self.place_order()
        response = self.post(self.shopper, '/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['error'], 'payment_provider_configuration')

    def test_card_details_are_not_stored(self):
        self.authorized_order()
        for write in ProviderWrite.objects.all():
            self.assertNotIn('4111111111111111', str(write.__dict__))
        for payment in OrderPayment.objects.all():
            self.assertNotIn('4111111111111111', str(payment.__dict__))


class OwnershipTests(PaymentsApiTestCase):

    def test_another_shopper_cannot_see_or_act_on_an_order(self):
        number = self.place_order()
        bob = self.client_for(self.bob)
        self.assertEqual(self.post(bob, '/api/orders/%s/pay' % number, {'card': CARD}).status_code, 404)
        self.assertEqual(self.post(bob, '/api/orders/%s/refunds' % number, {},
                                   **{'Idempotency-Key': 'k'}).status_code, 404)
        self.assertEqual(bob.get('/api/my-orders').json(), {'orders': []})
        self.assertEqual(len(self.shopper.get('/api/my-orders').json()['orders']), 1)

    def test_operator_routes_are_staff_only(self):
        number = self.place_order()
        for path in ('fulfil', 'cancel'):
            self.assertEqual(self.post(self.shopper, '/api/orders/%s/%s' % (number, path)).status_code, 403)
        self.assertEqual(self.shopper.get('/api/reconciliation', {'from': '2026-01-01T00:00:00Z',
                                                                 'to': '2026-01-02T00:00:00Z'}).status_code, 403)


class FulfilTests(PaymentsApiTestCase):

    def test_fulfil_captures_and_records_fee_and_net(self):
        number = self.captured_order()
        payment = OrderPayment.objects.get(order__number=number)
        self.assertEqual(payment.state, PaymentState.CAPTURED)
        self.assertEqual((payment.captured_amount, payment.paypal_fee, payment.net_amount),
                         (Decimal('15.98'), Decimal('0.84'), Decimal('15.14')))
        self.assertEqual(payment.order.status, 'Complete')
        [capture] = self.transport.calls('POST', '/v2/payments/authorizations/AUTH1/capture')
        self.assertEqual(capture.headers['paypal-request-id'], 'test-auth-AUTH1-capture')
        self.assertEqual(capture.body.value['amount'], money('15.98'))
        again = self.post(self.operator, '/api/orders/%s/fulfil' % number)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(self.transport.calls('POST', '/v2/payments/authorizations/AUTH1/capture')), 1)

    def test_a_pending_capture_is_not_fulfilled(self):
        number = self.authorized_order()
        self.transport.on('GET', '/v2/payments/authorizations/AUTH1', json_response(200, auth_body()))
        self.transport.on('POST', '/v2/payments/authorizations/AUTH1/capture',
                          json_response(201, capture_body(status='PENDING')))
        response = self.post(self.operator, '/api/orders/%s/fulfil' % number)
        self.assertEqual(response.status_code, 202)
        payment = OrderPayment.objects.get(order__number=number)
        self.assertEqual(payment.state, PaymentState.CAPTURE_PENDING)
        self.assertEqual(payment.order.status, 'Payment authorised')

    def test_a_stale_authorization_is_renewed_before_capture(self):
        old = timezone.now() - timedelta(days=5)
        number = self.authorized_order(created=old)
        self.transport.on('GET', '/v2/payments/authorizations/AUTH1',
                          json_response(200, auth_body(created=old)))
        self.transport.on('POST', '/v2/payments/authorizations/AUTH1/reauthorize',
                          json_response(201, auth_body(auth_id='AUTH9')))
        self.transport.on('POST', '/v2/payments/authorizations/AUTH9/capture',
                          json_response(201, capture_body()))
        response = self.post(self.operator, '/api/orders/%s/fulfil' % number)
        self.assertEqual(response.status_code, 200, response.content)
        payment = OrderPayment.objects.get(order__number=number)
        self.assertEqual((payment.state, payment.authorization_id), (PaymentState.CAPTURED, 'AUTH9'))
        [reauth] = self.transport.calls('POST', '/v2/payments/authorizations/AUTH1/reauthorize')
        self.assertEqual(reauth.headers['paypal-request-id'], 'test-auth-AUTH1-reauth')

    def test_an_authorization_past_the_renewal_limit_says_what_to_do(self):
        old = timezone.now() - timedelta(days=30)
        number = self.authorized_order(created=old)
        self.transport.on('GET', '/v2/payments/authorizations/AUTH1',
                          json_response(200, auth_body(created=old)))
        response = self.post(self.operator, '/api/orders/%s/fulfil' % number)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], 'authorization_expired')
        self.assertIn('pay for the order again', response.json()['message'])
        self.assertEqual(self.transport.calls('POST', r'/v2/payments/authorizations/.*/capture'), [])
        payment = OrderPayment.objects.get(order__number=number)
        self.assertEqual(payment.state, PaymentState.AUTHORIZATION_EXPIRED)
        self.assertEqual(payment.order.status, 'Awaiting payment')

    def test_a_refused_renewal_says_what_to_do(self):
        old = timezone.now() - timedelta(days=5)
        number = self.authorized_order(created=old)
        self.transport.on('GET', '/v2/payments/authorizations/AUTH1',
                          json_response(200, auth_body(created=old)))
        self.transport.on('POST', '/v2/payments/authorizations/AUTH1/reauthorize',
                          json_response(422, error_body('AUTHORIZATION_EXPIRED')))
        response = self.post(self.operator, '/api/orders/%s/fulfil' % number)
        self.assertEqual(response.status_code, 409)
        self.assertIn('AUTHORIZATION_EXPIRED', response.json()['message'])
        self.assertEqual(OrderPayment.objects.get(order__number=number).state,
                         PaymentState.AUTHORIZATION_EXPIRED)

    def test_cancel_releases_the_hold(self):
        number = self.authorized_order()
        self.transport.on('POST', '/v2/payments/authorizations/AUTH1/void',
                          json_response(200, auth_body(status='VOIDED')))
        response = self.post(self.operator, '/api/orders/%s/cancel' % number)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['payment']['state'], 'voided')
        self.assertEqual(response.json()['orderStatus'], 'Cancelled')
        again = self.post(self.operator, '/api/orders/%s/cancel' % number)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(self.transport.calls('POST', '/v2/payments/authorizations/AUTH1/void')), 1)

    def test_cancel_after_capture_points_to_refunds(self):
        number = self.captured_order()
        response = self.post(self.operator, '/api/orders/%s/cancel' % number)
        self.assertEqual(response.status_code, 409)


class RefundTests(PaymentsApiTestCase):

    def refund(self, number, amount, key):
        body = {} if amount is None else {'amount': amount}
        return self.post(self.shopper, '/api/orders/%s/refunds' % number, body, **{'Idempotency-Key': key})

    def test_partial_refunds_are_keyed_and_capped_at_the_capture(self):
        number = self.captured_order()
        self.transport.on('POST', '/v2/payments/captures/CAP1/refund',
                          json_response(201, refund_body('R1', '10.00')),
                          json_response(201, refund_body('R2', '5.98')))
        first = self.refund(number, '10.00', 'k1')
        self.assertEqual(first.status_code, 201, first.content)
        self.assertIn('refundId', first.json())
        repeat = self.refund(number, '10.00', 'k1')
        self.assertEqual(repeat.json()['refundId'], first.json()['refundId'])
        too_much = self.refund(number, '6.00', 'k2')
        self.assertEqual(too_much.status_code, 409)
        rest = self.refund(number, None, 'k3')
        self.assertEqual(rest.status_code, 201)
        self.assertEqual(rest.json()['refund']['amount'], '5.98')
        calls = self.transport.calls('POST', '/v2/payments/captures/CAP1/refund')
        self.assertEqual([c.headers['paypal-request-id'] for c in calls],
                         ['test-ord-%s-refund-k1' % number, 'test-ord-%s-refund-k3' % number])
        payment = OrderPayment.objects.get(order__number=number)
        self.assertEqual((payment.refunded_amount, payment.refund_reserved),
                         (Decimal('15.98'), Decimal('15.98')))
        self.assertEqual(self.refund(number, '0.01', 'k4').status_code, 409)

    def test_a_key_reused_for_another_amount_is_refused(self):
        number = self.captured_order()
        self.transport.on('POST', '/v2/payments/captures/CAP1/refund',
                          json_response(201, refund_body('R1', '1.00')))
        self.assertEqual(self.refund(number, '1.00', 'k1').status_code, 201)
        self.assertEqual(self.refund(number, '2.00', 'k1').status_code, 409)

    def test_a_refused_refund_releases_its_reservation(self):
        number = self.captured_order()
        self.transport.on('POST', '/v2/payments/captures/CAP1/refund',
                          json_response(422, error_body('REFUND_NOT_ALLOWED')))
        response = self.refund(number, '15.98', 'k1')
        self.assertEqual(response.status_code, 422)
        payment = OrderPayment.objects.get(order__number=number)
        self.assertEqual(payment.refund_reserved, Decimal('0.00'))

    def test_a_refund_with_unknown_outcome_keeps_its_reservation(self):
        number = self.captured_order()
        self.transport.on('POST', '/v2/payments/captures/CAP1/refund', httpx.ReadTimeout('no reply'))
        response = self.refund(number, '15.98', 'k1')
        self.assertEqual(response.status_code, 504)
        self.assertEqual(OrderPayment.objects.get(order__number=number).refund_reserved,
                         Decimal('15.98'))
        self.assertEqual(self.refund(number, '1.00', 'k2').status_code, 409)

    def test_only_a_captured_order_is_refundable(self):
        number = self.authorized_order()
        self.assertEqual(self.refund(number, '1.00', 'k1').status_code, 409)

    def test_refund_needs_an_idempotency_key(self):
        number = self.captured_order()
        response = self.post(self.shopper, '/api/orders/%s/refunds' % number, {'amount': '1.00'})
        self.assertEqual(response.status_code, 400)


class SavedCardTests(PaymentsApiTestCase):

    def token_body(self, token_id='TOK1'):
        return {'id': token_id, 'customer': {'id': 'CUST1'},
                'payment_source': {'card': {'last_digits': '1111', 'brand': 'VISA',
                                            'expiry': '2030-12', 'name': 'Test Shopper'}}}

    def save(self, client, key='c1'):
        return self.post(client, '/api/payment-methods', {'card': CARD}, **{'Idempotency-Key': key})

    def test_save_list_pay_and_delete(self):
        self.transport.on('POST', '/v3/vault/payment-tokens', json_response(201, self.token_body()))
        saved = self.save(self.shopper)
        self.assertEqual(saved.status_code, 201)
        body = saved.json()
        self.assertEqual((body['brand'], body['lastDigits']), ('VISA', '1111'))
        self.assertNotIn('number', body)
        method_id = body['paymentMethodId']
        self.assertEqual(self.save(self.shopper).json()['paymentMethodId'], method_id)
        self.assertEqual(len(self.transport.calls('POST', '/v3/vault/payment-tokens')), 1)
        listed = self.shopper.get('/api/payment-methods').json()['paymentMethods']
        self.assertEqual([m['paymentMethodId'] for m in listed], [method_id])
        self.assertEqual(SavedCard.objects.get().paypal_token_id, 'TOK1')

        number = self.place_order()
        self.transport.on('POST', '/v2/checkout/orders', json_response(201, order_body()))
        paid = self.post(self.shopper, '/api/orders/%s/pay' % number, {'paymentMethodId': method_id})
        self.assertEqual(paid.status_code, 200)
        [call] = self.transport.calls('POST', '/v2/checkout/orders')
        self.assertEqual(call.body.value['payment_source'], {'card': {'vault_id': 'TOK1'}})

        self.transport.on('DELETE', '/v3/vault/payment-tokens/TOK1', HttpResponse(status_code=204, headers={}))
        deleted = self.shopper.delete('/api/payment-methods/%s' % method_id)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.shopper.get('/api/payment-methods').json(), {'paymentMethods': []})
        another = self.place_order()
        refused = self.post(self.shopper, '/api/orders/%s/pay' % another, {'paymentMethodId': method_id})
        self.assertEqual(refused.status_code, 404)

    def test_cards_belong_to_their_shopper(self):
        self.transport.on('POST', '/v3/vault/payment-tokens', json_response(201, self.token_body()))
        method_id = self.save(self.shopper).json()['paymentMethodId']
        bob = self.client_for(self.bob)
        self.assertEqual(bob.get('/api/payment-methods').json(), {'paymentMethods': []})
        self.assertEqual(bob.delete('/api/payment-methods/%s' % method_id).status_code, 404)
        number = self.place_order(client=bob)
        response = self.post(bob, '/api/orders/%s/pay' % number, {'paymentMethodId': method_id})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.transport.calls('POST', '/v2/checkout/orders'), [])


class ReconciliationTests(PaymentsApiTestCase):

    def txn(self, txn_id, when, custom='', ref=''):
        return {'transaction_info': {
            'transaction_id': txn_id, 'paypal_reference_id': ref, 'transaction_event_code': 'T0006',
            'transaction_initiation_date': iso(when), 'transaction_amount': money('15.98'),
            'transaction_status': 'S', 'custom_field': custom}}

    def test_every_page_is_read_and_lined_up_with_orders(self):
        number = self.captured_order()
        now = timezone.now()
        page1 = {'transaction_details': [self.txn('CAP1', now), self.txn('FOREIGN1', now)],
                 'page': 1, 'total_pages': 2, 'last_refreshed_datetime': iso(now + timedelta(hours=1))}
        page2 = {'transaction_details': [self.txn('OTHER', now, custom='test-%s' % number),
                                         self.txn('OUTSIDE', now + timedelta(days=3))],
                 'page': 2, 'total_pages': 2, 'last_refreshed_datetime': iso(now + timedelta(hours=1))}
        self.transport.on('GET', '/v1/reporting/transactions',
                          json_response(200, page1), json_response(200, page2))
        start, end = now - timedelta(days=1), now + timedelta(days=1)
        response = self.operator.get('/api/reconciliation', {'from': iso(start), 'to': iso(end)})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual(report['pagesFetched'], 2)
        [matched] = report['matched']
        self.assertEqual(matched['orderId'], number)
        self.assertEqual({t['transactionId'] for t in matched['providerTransactions']}, {'CAP1', 'OTHER'})
        self.assertEqual([t['transactionId'] for t in report['providerOnly']], ['FOREIGN1'])
        # The authorization the app made is not in PayPal's report: local-only.
        self.assertEqual([e['paypalId'] for e in report['localOnly']], ['AUTH1'])
        calls = self.transport.calls('GET', '/v1/reporting/transactions')
        self.assertIn('page=2', calls[1].url)

    def test_a_long_range_is_split_into_31_day_windows(self):
        self.transport.on('GET', '/v1/reporting/transactions',
                          json_response(200, {'transaction_details': [], 'total_pages': 1}))
        response = self.operator.get('/api/reconciliation', {'from': '2026-01-01T00:00:00Z',
                                                            'to': '2026-03-15T00:00:00Z'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['windowsQueried'], 3)

    def test_bad_range_is_rejected(self):
        response = self.operator.get('/api/reconciliation', {'from': 'yesterday', 'to': 'today'})
        self.assertEqual(response.status_code, 400)


class ConfigurationTests(TestCase):

    @override_settings(PAYPAL_CLIENT_ID='id', PAYPAL_CLIENT_SECRET='secret',
                       PAYPAL_ENVIRONMENT='production', PAYPAL_BASE_URL='')
    def test_an_environment_without_a_known_host_needs_a_base_url(self):
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaises(ImproperlyConfigured):
            paypal_client.build_client(http_client=StubTransport())

    @override_settings(PAYPAL_CLIENT_ID='id', PAYPAL_CLIENT_SECRET='secret',
                       PAYPAL_ENVIRONMENT='sandbox', PAYPAL_BASE_URL='https://paypal.test.invalid')
    def test_base_url_override_is_used_for_every_call_including_the_token(self):
        transport = StubTransport().on('GET', '/v2/payments/authorizations/A1',
                                       json_response(200, auth_body(auth_id='A1')))
        client = paypal_client.build_client(http_client=transport)
        client.payments.get_authorized_payment('A1')
        self.assertEqual([httpx.URL(r.url).host for r in transport.requests],
                         ['paypal.test.invalid', 'paypal.test.invalid'])
        self.assertTrue(transport.requests[0].url.endswith('/v1/oauth2/token'))
