"""
API tests for PayPal payments and saved cards.

PayPal is faked at the SDK's transport seam: a real ``PaypalClient`` builds and
serializes every request, and ``RoutedTransport`` answers it, so the tests see
exactly what would go over the wire. Run with::

    cd sandbox && python manage.py test apps.payments_api
"""
import json
import re
from datetime import timedelta
from decimal import Decimal

import httpx
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from oscar.test.factories import create_product

from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpRequest, HttpResponse, OAuthToken

from apps.payments_api import gateway
from apps.payments_api.models import PaypalPayment, PaypalRefund, SavedCard

BASE = 'https://paypal.test'
CARD = {
    'number': '4111 1111 1111 1111',
    'expiry': '2030-12',
    'securityCode': '123',
    'name': 'Test Shopper',
    'billingAddress': {'addressLine1': '1 Main St', 'adminArea2': 'San Jose', 'adminArea1': 'CA',
                       'postalCode': '95131', 'countryCode': 'US'},
}
PAN = '4111111111111111'


def json_response(status, body=None):
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=b'' if body is None else json.dumps(body).encode())


def paypal_error(status, issue, description='rejected'):
    return json_response(status, {'name': 'UNPROCESSABLE_ENTITY', 'message': 'failed', 'debug_id': 'dbg1',
                                  'details': [{'issue': issue, 'description': description}]})


class RoutedTransport:
    """The SDK's sync transport protocol, answering by method + path."""

    def __init__(self):
        self.routes = []
        self.requests = []

    def on(self, method, path_regex, *answers):
        """Queue answers (HttpResponse, exception, or callable(request)) for a route."""
        self.routes.append([method, re.compile(path_regex + r'$'), list(answers)])

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = request.url.split('?', 1)[0].removeprefix(BASE)
        for method, pattern, answers in self.routes:
            if method == request.method and pattern.match(path) and answers:
                answer = answers.pop(0) if len(answers) > 1 else answers[0]
                if isinstance(answer, BaseException):
                    raise answer
                return answer(request) if callable(answer) else answer
        raise AssertionError(f'unexpected PayPal call {request.method} {path}')

    def close(self):
        pass

    def calls(self, method, path_regex):
        pattern = re.compile(path_regex + r'$')
        return [r for r in self.requests
                if r.method == method and pattern.match(r.url.split('?', 1)[0].removeprefix(BASE))]


class StubTokenSource:
    def fetch(self, credentials):
        return OAuthToken(access_token='test-token', token_type='Bearer')


def order_response(request, *, auth_status='CREATED', order_status='COMPLETED', auth_id='AUTH-1'):
    body = request.body.value
    unit = body['purchase_units'][0]
    return json_response(201, {
        'id': 'PPORDER-1',
        'status': order_status,
        'payment_source': {'card': {'brand': 'VISA', 'last_digits': '1111'}},
        'purchase_units': [{
            'reference_id': unit.get('reference_id'),
            'custom_id': unit.get('custom_id'),
            'payments': {'authorizations': [{
                'id': auth_id, 'status': auth_status, 'amount': unit['amount'],
                'create_time': timezone.now().strftime('%Y-%m-%dT%H:%M:%SZ'),
                'expiration_time': (timezone.now() + timedelta(days=29)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            }]},
        }],
    })


def capture_response(request, *, capture_id='CAP-1', status='COMPLETED'):
    amount = request.body.value['amount']
    fee = '0.75'
    net = str(Decimal(amount['value']) - Decimal(fee))
    return json_response(201, {
        'id': capture_id, 'status': status, 'amount': amount,
        'seller_receivable_breakdown': {
            'gross_amount': amount,
            'paypal_fee': {'currency_code': amount['currency_code'], 'value': fee},
            'net_amount': {'currency_code': amount['currency_code'], 'value': net},
        },
        'create_time': timezone.now().strftime('%Y-%m-%dT%H:%M:%SZ'),
    })


def refund_response(refund_id, status='COMPLETED'):
    def answer(request):
        return json_response(201, {'id': refund_id, 'status': status, 'amount': request.body.value['amount']})
    return answer


@override_settings(PAYPAL_CLIENT_ID='test-id', PAYPAL_CLIENT_SECRET='test-secret',
                   PAYPAL_ENVIRONMENT='sandbox', PAYPAL_CURRENCY='USD', PAYPAL_BASE_URL='')
class PaymentsApiTestCase(TestCase):

    def setUp(self):
        self.transport = RoutedTransport()
        self.previous_client = gateway.use_client(PaypalClient(
            base_url=BASE, custom_http_client=self.transport,
            oauth2=ClientCredentials(client_id='test-id', client_secret='test-secret'),
            oauth2_token_source=StubTokenSource()))
        User = get_user_model()
        self.shopper = User.objects.create_user('shopper', 'shopper@example.com', 'pass-12345')
        self.other = User.objects.create_user('other', 'other@example.com', 'pass-12345')
        self.operator = User.objects.create_user('operator', 'op@example.com', 'pass-12345', is_staff=True)
        self.product = create_product(price=Decimal('10.00'), num_in_stock=100)
        self.product2 = create_product(price=Decimal('2.50'), num_in_stock=100)
        self.client = self.client_for(self.shopper)
        self.staff = self.client_for(self.operator)

    def tearDown(self):
        gateway.use_client(self.previous_client)

    def client_for(self, user):
        c = Client()
        c.force_login(user)
        return c

    def post(self, client, url, body=None, **headers):
        return client.post(url, data=json.dumps(body or {}), content_type='application/json',
                           headers=headers)

    def place_order(self, client=None):
        response = self.post(client or self.client, '/api/orders', {'items': [
            {'productId': self.product.pk, 'quantity': 2},
            {'productId': self.product2.pk, 'quantity': 1},
        ]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()['orderId']

    def paid_order(self):
        self.transport.on('POST', '/v2/checkout/orders', order_response)
        order_id = self.place_order()
        response = self.post(self.client, f'/api/orders/{order_id}/pay', {'card': CARD})
        self.assertEqual(response.status_code, 201, response.content)
        return order_id

    def fulfilled_order(self):
        order_id = self.paid_order()
        self.transport.on('POST', '/v2/payments/authorizations/AUTH-1/capture', capture_response)
        response = self.post(self.staff, f'/api/orders/{order_id}/fulfil')
        self.assertEqual(response.status_code, 201, response.content)
        return order_id


class OrderAndPayTests(PaymentsApiTestCase):

    def test_place_order_uses_catalogue_prices_and_configured_currency(self):
        order_id = self.place_order()
        body = self.client.get(f'/api/orders/{order_id}').json()
        self.assertEqual(body['total'], '22.50')
        self.assertEqual(body['currency'], 'USD')
        self.assertEqual(body['payment']['status'], 'awaiting_payment')
        self.assertEqual(len(body['lines']), 2)

    def test_pay_with_card_authorizes_the_order_total(self):
        self.transport.on('POST', '/v2/checkout/orders', order_response)
        order_id = self.place_order()
        with self.assertLogs('apps.payments_api', level='INFO') as logs:
            response = self.post(self.client, f'/api/orders/{order_id}/pay', {'card': CARD})
        self.assertEqual(response.status_code, 201, response.content)
        payment = response.json()['payment']
        self.assertEqual(payment['status'], 'authorized')
        self.assertEqual(payment['authorization']['id'], 'AUTH-1')
        self.assertEqual(payment['authorization']['amount'], '22.50')
        self.assertEqual(payment['card'], {'brand': 'VISA', 'lastDigits': '1111'})

        [call] = self.transport.calls('POST', '/v2/checkout/orders')
        sent = call.body.value
        record = PaypalPayment.objects.get(order__number=order_id)
        self.assertEqual(sent['intent'], 'AUTHORIZE')
        self.assertEqual(sent['purchase_units'][0]['amount'], {'currency_code': 'USD', 'value': '22.50'})
        self.assertEqual(sent['purchase_units'][0]['custom_id'], record.reference)
        self.assertEqual(sent['payment_source']['card']['number'], PAN)
        self.assertEqual(call.headers['paypal-request-id'], f'{record.reference}-authorize-1')
        self.assertEqual(call.headers['prefer'], 'return=representation')
        self.assertEqual(call.headers['authorization'], 'Bearer test-token')

        # Oscar's order and payment ledger reflect the hold.
        self.assertEqual(record.order.status, 'Being processed')
        source = record.order.sources.get()
        self.assertEqual(source.amount_allocated, Decimal('22.50'))
        # The card number is neither stored nor logged.
        self.assertNotIn(PAN, json.dumps(list(PaypalPayment.objects.values()), default=str))
        self.assertNotIn(PAN, '\n'.join(logs.output))

    def test_double_click_never_authorizes_twice(self):
        order_id = self.paid_order()
        response = self.post(self.client, f'/api/orders/{order_id}/pay', {'card': CARD})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['payment']['status'], 'authorized')
        self.assertEqual(len(self.transport.calls('POST', '/v2/checkout/orders')), 1)

    def test_declined_card_fails_and_a_retry_is_a_new_attempt(self):
        self.transport.on('POST', '/v2/checkout/orders',
                          paypal_error(422, 'INSTRUMENT_DECLINED', 'The instrument was declined.'),
                          order_response)
        order_id = self.place_order()
        response = self.post(self.client, f'/api/orders/{order_id}/pay', {'card': CARD})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['code'], 'INSTRUMENT_DECLINED')
        self.assertEqual(response.json()['order']['payment']['status'], 'failed')

        response = self.post(self.client, f'/api/orders/{order_id}/pay', {'card': CARD})
        self.assertEqual(response.status_code, 201)
        ids = [c.headers['paypal-request-id'] for c in self.transport.calls('POST', '/v2/checkout/orders')]
        self.assertEqual([i.rsplit('-', 1)[1] for i in ids], ['1', '2'])

    def test_denied_authorization_is_declined_not_paid(self):
        self.transport.on('POST', '/v2/checkout/orders', lambda r: order_response(r, auth_status='DENIED'))
        order_id = self.place_order()
        response = self.post(self.client, f'/api/orders/{order_id}/pay', {'card': CARD})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()['order']['payment']['status'], 'failed')

    def test_unlisted_authorization_status_is_unknown_not_paid(self):
        self.transport.on('POST', '/v2/checkout/orders', lambda r: order_response(r, auth_status='SOMETHING_NEW'))
        order_id = self.place_order()
        response = self.post(self.client, f'/api/orders/{order_id}/pay', {'card': CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['order']['payment']['status'], 'unknown')

    def test_three_d_secure_challenge_is_reported_not_attempted(self):
        self.transport.on('POST', '/v2/checkout/orders',
                          json_response(201, {'id': 'PP1', 'status': 'PAYER_ACTION_REQUIRED'}))
        order_id = self.place_order()
        response = self.post(self.client, f'/api/orders/{order_id}/pay', {'card': CARD})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['code'], 'PAYER_ACTION_REQUIRED')

    def test_no_reply_is_unknown_and_a_repeat_resends_under_the_same_request_id(self):
        self.transport.on('POST', '/v2/checkout/orders', httpx.ReadTimeout('no reply'), order_response)
        order_id = self.place_order()
        response = self.post(self.client, f'/api/orders/{order_id}/pay', {'card': CARD})
        self.assertEqual((response.status_code, response.json()['error']['code']), (504, 'outcome_unknown'))
        self.assertEqual(response.json()['order']['payment']['status'], 'unknown')

        response = self.post(self.client, f'/api/orders/{order_id}/pay', {'card': CARD})
        self.assertEqual(response.status_code, 201)
        first, second = self.transport.calls('POST', '/v2/checkout/orders')
        self.assertEqual(first.headers['paypal-request-id'], second.headers['paypal-request-id'])

    def test_refused_connection_is_a_known_failure(self):
        self.transport.on('POST', '/v2/checkout/orders', httpx.ConnectError('refused'))
        order_id = self.place_order()
        response = self.post(self.client, f'/api/orders/{order_id}/pay', {'card': CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['order']['payment']['status'], 'failed')

    def test_invalid_card_is_rejected_before_paypal(self):
        order_id = self.place_order()
        response = self.post(self.client, f'/api/orders/{order_id}/pay',
                             {'card': {**CARD, 'number': '4111111111111112'}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.transport.requests, [])

    def test_rejected_credentials_are_a_server_configuration_error(self):
        transport = RoutedTransport()
        transport.on('POST', '/v1/oauth2/token', json_response(401, {'error': 'invalid_client'}))
        gateway.use_client(PaypalClient(base_url=BASE, custom_http_client=transport,
                                        oauth2=ClientCredentials(client_id='x', client_secret='y')))
        order_id = self.place_order()
        response = self.post(self.client, f'/api/orders/{order_id}/pay', {'card': CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['error']['code'], 'PAYPAL_CREDENTIALS')
        self.assertEqual(response.json()['order']['payment']['status'], 'failed')


class FulfilCancelTests(PaymentsApiTestCase):

    def test_fulfil_captures_and_records_fee_and_net(self):
        order_id = self.fulfilled_order()
        payment = self.client.get(f'/api/orders/{order_id}').json()['payment']
        self.assertEqual(payment['status'], 'captured')
        self.assertEqual(payment['capture'], {**payment['capture'], 'id': 'CAP-1', 'amount': '22.50',
                                              'paypalFee': '0.75', 'netAmount': '21.75'})
        [call] = self.transport.calls('POST', '/v2/payments/authorizations/AUTH-1/capture')
        record = PaypalPayment.objects.get(order__number=order_id)
        self.assertEqual(call.body.value['amount'], {'currency_code': 'USD', 'value': '22.50'})
        self.assertEqual(call.headers['paypal-request-id'], f'{record.reference}-capture-AUTH-1')
        self.assertEqual(record.order.status, 'Complete')
        self.assertEqual(record.order.sources.get().amount_debited, Decimal('22.50'))

        # Fulfilling again never takes the money twice.
        self.assertEqual(self.post(self.staff, f'/api/orders/{order_id}/fulfil').status_code, 200)
        self.assertEqual(len(self.transport.calls('POST', '/v2/payments/authorizations/AUTH-1/capture')), 1)

    def test_operator_actions_require_staff(self):
        order_id = self.paid_order()
        for action in ('fulfil', 'cancel'):
            self.assertEqual(self.post(self.client, f'/api/orders/{order_id}/{action}').status_code, 403)
        self.assertEqual(self.client.get('/api/reconciliation?from=2026-01-01T00:00:00Z&to=2026-01-02T00:00:00Z')
                         .status_code, 403)

    def test_stale_authorization_is_renewed_before_capture(self):
        order_id = self.paid_order()
        PaypalPayment.objects.filter(order__number=order_id).update(
            authorization_created_at=timezone.now() - timedelta(days=5))
        self.transport.on('POST', '/v2/payments/authorizations/AUTH-1/reauthorize', json_response(201, {
            'id': 'AUTH-2', 'status': 'CREATED', 'amount': {'currency_code': 'USD', 'value': '22.50'},
            'expiration_time': (timezone.now() + timedelta(days=24)).strftime('%Y-%m-%dT%H:%M:%SZ')}))
        self.transport.on('POST', '/v2/payments/authorizations/AUTH-2/capture', capture_response)
        response = self.post(self.staff, f'/api/orders/{order_id}/fulfil')
        self.assertEqual(response.status_code, 201, response.content)
        authorization = response.json()['payment']['authorization']
        self.assertEqual((authorization['id'], authorization['originalAuthorizationId']), ('AUTH-2', 'AUTH-1'))
        self.assertEqual(self.transport.calls('POST', '/v2/payments/authorizations/AUTH-1/capture'), [])

    def test_expired_authorization_tells_the_operator_what_to_do(self):
        order_id = self.paid_order()
        PaypalPayment.objects.filter(order__number=order_id).update(
            authorization_created_at=timezone.now() - timedelta(days=31),
            authorization_expires_at=timezone.now() - timedelta(days=1))
        response = self.post(self.staff, f'/api/orders/{order_id}/fulfil')
        self.assertEqual(response.status_code, 409)
        error = response.json()['error']
        self.assertEqual(error['code'], 'AUTHORIZATION_EXPIRED')
        self.assertIn('Cancel this order', error['message'])
        self.assertEqual(response.json()['order']['payment']['status'], 'authorized')
        self.assertEqual(len(self.transport.requests), 1)   # only the original authorization

    def test_renewal_refused_and_capture_refused_reports_both(self):
        order_id = self.paid_order()
        PaypalPayment.objects.filter(order__number=order_id).update(
            authorization_created_at=timezone.now() - timedelta(days=5))
        self.transport.on('POST', '/v2/payments/authorizations/AUTH-1/reauthorize',
                          paypal_error(422, 'REAUTHORIZATION_NOT_ALLOWED', 'Not allowed for this payment.'))
        self.transport.on('POST', '/v2/payments/authorizations/AUTH-1/capture',
                          paypal_error(422, 'AUTHORIZATION_EXPIRED', 'Authorization has expired.'))
        response = self.post(self.staff, f'/api/orders/{order_id}/fulfil')
        self.assertEqual(response.status_code, 409)
        message = response.json()['error']['message']
        self.assertIn('would not renew', message)
        self.assertIn('AUTHORIZATION_EXPIRED', message)
        self.assertIn('ask the shopper to pay again', message)

    def test_cancel_before_fulfilment_releases_the_hold(self):
        order_id = self.paid_order()
        self.transport.on('POST', '/v2/payments/authorizations/AUTH-1/void',
                          json_response(200, {'id': 'AUTH-1', 'status': 'VOIDED'}))
        response = self.post(self.staff, f'/api/orders/{order_id}/cancel')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['payment']['status'], 'voided')
        self.assertEqual(response.json()['status'], 'Cancelled')
        # Cancelled orders cannot be paid or fulfilled any more.
        self.assertEqual(self.post(self.client, f'/api/orders/{order_id}/pay', {'card': CARD}).status_code, 409)
        self.assertEqual(self.post(self.staff, f'/api/orders/{order_id}/fulfil').status_code, 409)

    def test_cancel_after_fulfilment_is_refused(self):
        order_id = self.fulfilled_order()
        response = self.post(self.staff, f'/api/orders/{order_id}/cancel')
        self.assertEqual((response.status_code, response.json()['error']['code']), (409, 'already_fulfilled'))


class RefundTests(PaymentsApiTestCase):

    def refund(self, order_id, key, amount=None, client=None):
        return self.post(client or self.client, f'/api/orders/{order_id}/refunds',
                         {} if amount is None else {'amount': amount}, **{'Idempotency-Key': key})

    def test_partial_refunds_are_idempotent_per_key_and_bounded_by_the_capture(self):
        order_id = self.fulfilled_order()
        self.transport.on('POST', '/v2/payments/captures/CAP-1/refund',
                          refund_response('R-1'), refund_response('R-2'))

        first = self.refund(order_id, 'key-a', '3.00')
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(first.json()['status'], 'completed')
        again = self.refund(order_id, 'key-a', '3.00')
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()['refundId'], first.json()['refundId'])

        second = self.refund(order_id, 'key-b', '2.00')
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(second.json()['refundId'], first.json()['refundId'])
        self.assertEqual(second.json()['order']['payment']['refundableAmount'], '17.50')

        too_much = self.refund(order_id, 'key-c', '17.51')
        self.assertEqual((too_much.status_code, too_much.json()['error']['code']),
                         (422, 'refund_exceeds_captured'))
        reused = self.refund(order_id, 'key-a', '4.00')
        self.assertEqual((reused.status_code, reused.json()['error']['code']), (422, 'idempotency_key_reused'))

        calls = self.transport.calls('POST', '/v2/payments/captures/CAP-1/refund')
        self.assertEqual([c.body.value['amount']['value'] for c in calls], ['3.00', '2.00'])
        self.assertNotEqual(calls[0].headers['paypal-request-id'], calls[1].headers['paypal-request-id'])
        self.assertEqual(PaypalPayment.objects.get(order__number=order_id).status, 'partially_refunded')

    def test_full_refund_without_amount_refunds_the_remainder(self):
        order_id = self.fulfilled_order()
        self.transport.on('POST', '/v2/payments/captures/CAP-1/refund', refund_response('R-1'))
        response = self.refund(order_id, 'all')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['amount'], '22.50')
        self.assertEqual(response.json()['order']['payment']['status'], 'refunded')
        self.assertEqual(self.refund(order_id, 'more', '0.01').status_code, 409)

    def test_unknown_refund_is_resent_under_the_same_request_id(self):
        order_id = self.fulfilled_order()
        self.transport.on('POST', '/v2/payments/captures/CAP-1/refund',
                          httpx.ReadTimeout('no reply'), refund_response('R-1'))
        response = self.refund(order_id, 'key-a', '5.00')
        self.assertEqual((response.status_code, response.json()['error']['code']), (504, 'outcome_unknown'))
        self.assertEqual(response.json()['refund']['status'], 'unknown')
        response = self.refund(order_id, 'key-a', '5.00')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['status'], 'completed')
        first, second = self.transport.calls('POST', '/v2/payments/captures/CAP-1/refund')
        self.assertEqual(first.headers['paypal-request-id'], second.headers['paypal-request-id'])
        self.assertEqual(PaypalRefund.objects.count(), 1)

    def test_refund_requires_idempotency_key_and_a_capture(self):
        order_id = self.paid_order()
        response = self.post(self.client, f'/api/orders/{order_id}/refunds', {'amount': '1.00'})
        self.assertEqual(response.status_code, 400)
        response = self.refund(order_id, 'k', '1.00')
        self.assertEqual((response.status_code, response.json()['error']['code']), (409, 'not_refundable'))


class OwnershipTests(PaymentsApiTestCase):

    def test_one_shopper_cannot_see_or_act_on_anothers_order_or_card(self):
        order_id = self.fulfilled_order()
        other = self.client_for(self.other)
        self.assertEqual(other.get(f'/api/orders/{order_id}').status_code, 404)
        self.assertEqual(self.post(other, f'/api/orders/{order_id}/pay', {'card': CARD}).status_code, 404)
        self.assertEqual(self.post(other, f'/api/orders/{order_id}/refunds', {'amount': '1.00'},
                                   **{'Idempotency-Key': 'x'}).status_code, 404)
        self.assertEqual(other.get('/api/my-orders').json()['orders'], [])
        self.assertEqual([o['orderId'] for o in self.client.get('/api/my-orders').json()['orders']], [order_id])

        card = SavedCard.objects.create(user=self.shopper, paypal_token_id='TOK-9', paypal_customer_id='C-1',
                                        brand='VISA', last_digits='1111', expiry='2030-12')
        self.assertEqual(other.get('/api/payment-methods').json()['paymentMethods'], [])
        self.assertEqual(other.delete(f'/api/payment-methods/{card.public_id}').status_code, 404)
        other_order = self.place_order(other)
        response = self.post(other, f'/api/orders/{other_order}/pay', {'paymentMethodId': str(card.public_id)})
        self.assertEqual(response.status_code, 404)

    def test_unauthenticated_callers_are_refused(self):
        anonymous = Client()
        self.assertEqual(anonymous.get('/api/my-orders').status_code, 401)
        self.assertEqual(self.post(anonymous, '/api/orders', {'items': []}).status_code, 401)

    def test_session_login_with_csrf(self):
        browser = Client(enforce_csrf_checks=True)
        token = browser.get('/api/auth/csrf').json()['csrfToken']
        refused = self.post(browser, '/api/auth/login', {'username': 'shopper', 'password': 'pass-12345'})
        self.assertEqual(refused.status_code, 403)
        response = self.post(browser, '/api/auth/login', {'username': 'shopper', 'password': 'pass-12345'},
                             **{'X-CSRFToken': token})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(browser.get('/api/my-orders').status_code, 200)


class SavedCardTests(PaymentsApiTestCase):

    def vault_response(self, token_id, customer_id='CUST-1'):
        return json_response(201, {
            'id': token_id, 'customer': {'id': customer_id},
            'payment_source': {'card': {'brand': 'VISA', 'last_digits': '1111', 'expiry': '2030-12'}}})

    def test_save_list_pay_with_and_remove_a_card(self):
        self.transport.on('POST', '/v3/vault/payment-tokens', self.vault_response('TOK-1'),
                          self.vault_response('TOK-2'))
        response = self.post(self.client, '/api/payment-methods', {'card': CARD})
        self.assertEqual(response.status_code, 201, response.content)
        card = response.json()
        self.assertEqual({k: card[k] for k in ('brand', 'lastDigits', 'expiry')},
                         {'brand': 'VISA', 'lastDigits': '1111', 'expiry': '2030-12'})
        self.assertNotIn(PAN, response.content.decode())
        self.assertNotIn(PAN, json.dumps(list(SavedCard.objects.values()), default=str))
        first_vault = self.transport.calls('POST', '/v3/vault/payment-tokens')[0].body.value
        self.assertNotIn('customer', first_vault)

        # A second card joins the same PayPal customer.
        self.post(self.client, '/api/payment-methods', {'card': CARD})
        second_vault = self.transport.calls('POST', '/v3/vault/payment-tokens')[1].body.value
        self.assertEqual(second_vault['customer'], {'id': 'CUST-1'})
        listed = self.client.get('/api/payment-methods').json()['paymentMethods']
        self.assertEqual(len(listed), 2)

        # Pay a new order with the saved card: only the vault id reaches PayPal.
        self.transport.on('POST', '/v2/checkout/orders', order_response)
        order_id = self.place_order()
        response = self.post(self.client, f'/api/orders/{order_id}/pay',
                             {'paymentMethodId': card['paymentMethodId']})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['payment']['paymentMethodId'], card['paymentMethodId'])
        sent = self.transport.calls('POST', '/v2/checkout/orders')[0].body.value
        self.assertEqual(sent['payment_source'], {'card': {'vault_id': 'TOK-1'}})

        # Remove it: gone from the list and unusable.
        self.transport.on('DELETE', '/v3/vault/payment-tokens/TOK-1', HttpResponse(status_code=204, headers={}))
        response = self.client.delete(f'/api/payment-methods/{card["paymentMethodId"]}')
        self.assertEqual(response.status_code, 200, response.content)
        listed = self.client.get('/api/payment-methods').json()['paymentMethods']
        self.assertNotIn(card['paymentMethodId'], [c['paymentMethodId'] for c in listed])
        next_order = self.place_order()
        response = self.post(self.client, f'/api/orders/{next_order}/pay',
                             {'paymentMethodId': card['paymentMethodId']})
        self.assertEqual(response.status_code, 404)

    def test_removal_is_immediate_even_if_paypal_is_unreachable(self):
        card = SavedCard.objects.create(user=self.shopper, paypal_token_id='TOK-5', paypal_customer_id='C',
                                        brand='VISA', last_digits='1111')
        self.transport.on('DELETE', '/v3/vault/payment-tokens/TOK-5', httpx.ConnectError('refused'),
                          HttpResponse(status_code=204, headers={}))
        response = self.client.delete(f'/api/payment-methods/{card.public_id}')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])
        # Repeating the DELETE retries the PayPal deletion.
        response = self.client.delete(f'/api/payment-methods/{card.public_id}')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SavedCard.objects.get(pk=card.pk).paypal_delete_pending)


class ReconciliationTests(PaymentsApiTestCase):

    def test_covers_the_whole_range_across_windows_and_pages(self):
        order_id = self.fulfilled_order()
        record = PaypalPayment.objects.get(order__number=order_id)
        now = timezone.now()

        def txn(tid, value, custom=''):
            return {'transaction_info': {
                'transaction_id': tid, 'transaction_event_code': 'T0006', 'transaction_status': 'S',
                'transaction_initiation_date': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'transaction_amount': {'currency_code': 'USD', 'value': value}, 'custom_field': custom}}

        pages = {
            1: {'transaction_details': [txn('CAP-1', '22.50', record.reference)], 'total_pages': 2, 'page': 1,
                'last_refreshed_datetime': now.strftime('%Y-%m-%dT%H:%M:%SZ')},
            2: {'transaction_details': [txn('STRANGER-1', '9.99')], 'total_pages': 2, 'page': 2},
        }

        def search(request):
            params = dict(p.split('=', 1) for p in request.url.split('?', 1)[1].split('&'))
            self.assertLessEqual(int(params['page_size']), 500)
            self.assertEqual(params['balance_affecting_records_only'], 'N')
            if params['end_date'].startswith(now.strftime('%Y-%m-%d')):
                return json_response(200, pages[int(params['page'])])
            return json_response(200, {'transaction_details': [], 'total_pages': 1, 'page': 1})

        self.transport.on('GET', '/v1/reporting/transactions', search)
        start = (now - timedelta(days=45)).strftime('%Y-%m-%dT%H:%M:%SZ')
        end = (now + timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        response = self.staff.get(f'/api/reconciliation?from={start}&to={end}')
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual(report['pagesFetched'], 3)   # two windows, the second of two pages
        self.assertEqual([m['transactionId'] for m in report['matched']], ['CAP-1'])
        self.assertEqual(report['matched'][0]['orderId'], order_id)
        self.assertEqual([t['transactionId'] for t in report['paypalOnly']], ['STRANGER-1'])
        self.assertEqual(report['appOnly'], [])
        self.assertEqual(report['amountMismatches'], [])

    def test_app_payment_paypal_does_not_report_is_listed(self):
        order_id = self.fulfilled_order()
        self.transport.on('GET', '/v1/reporting/transactions',
                          json_response(200, {'transaction_details': [], 'total_pages': 1}))
        now = timezone.now()
        response = self.staff.get('/api/reconciliation', {
            'from': (now - timedelta(days=1)).isoformat(), 'to': (now + timedelta(minutes=1)).isoformat()})
        self.assertEqual(response.status_code, 200, response.content)
        kinds = {(r['orderId'], r['kind']) for r in response.json()['appOnly']}
        self.assertEqual(kinds, {(order_id, 'authorization'), (order_id, 'capture')})

    def test_rejects_a_bad_range(self):
        response = self.staff.get('/api/reconciliation?from=2026-02-01T00:00:00Z&to=2026-01-01T00:00:00Z')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.staff.get('/api/reconciliation?from=yesterday&to=today').status_code, 400)


class ConfigurationTests(TestCase):

    @override_settings(PAYPAL_BASE_URL='https://proxy.example.test', PAYPAL_ENVIRONMENT='whatever')
    def test_base_url_override_is_used_verbatim(self):
        self.assertEqual(gateway.base_url(), 'https://proxy.example.test')

    @override_settings(PAYPAL_BASE_URL='', PAYPAL_ENVIRONMENT='sandbox')
    def test_sandbox_environment_selects_the_sandbox_host(self):
        self.assertEqual(gateway.base_url(), gateway.SANDBOX_BASE_URL)

    @override_settings(PAYPAL_BASE_URL='', PAYPAL_ENVIRONMENT='live')
    def test_unknown_environment_without_override_is_a_configuration_error(self):
        with self.assertRaises(ImproperlyConfigured):
            gateway.base_url()
