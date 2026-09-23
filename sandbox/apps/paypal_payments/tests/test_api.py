import json
from datetime import timedelta
from decimal import Decimal as D

import httpx
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from oscar.test.factories import create_product

from apps.paypal_payments import gateway
from apps.paypal_payments.models import PayPalPayment, PayPalRefund

from . import fakes
from .fakes import body_of, json_response, paypal_error

CARD = {
    'number': '4111 1111 1111 1111', 'expiry': '2031-12', 'securityCode': '123',
    'name': 'Test Shopper',
    'billingAddress': {'addressLine1': '1 Main St', 'adminArea2': 'San Jose',
                       'adminArea1': 'CA', 'postalCode': '95131', 'countryCode': 'US'},
}

PAYPAL_SETTINGS = dict(
    PAYPAL_CLIENT_ID='test-client', PAYPAL_CLIENT_SECRET='test-secret',
    PAYPAL_ENVIRONMENT='sandbox', PAYPAL_CURRENCY='USD', PAYPAL_BASE_URL=None,
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])


@override_settings(**PAYPAL_SETTINGS)
class ApiTestCase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.shopper = User.objects.create_user('shopper', 'shopper@example.com', 'pw-123456789')
        self.other = User.objects.create_user('other', 'other@example.com', 'pw-123456789')
        self.staff = User.objects.create_user('op', 'op@example.com', 'pw-123456789',
                                              is_staff=True)
        self.product = create_product(price=D('12.34'), num_in_stock=100)
        self.product2 = create_product(price=D('5.00'), num_in_stock=100)
        self.client = self.login(self.shopper)
        self.staff_client = self.login(self.staff)
        self.paypal = fakes.StubTransport()
        fakes.install(self.paypal)
        self.addCleanup(gateway.set_client, None)
        self.next_id = 0

    def login(self, user):
        client = Client()
        client.force_login(user)
        return client

    def post(self, client, url, data=None, **headers):
        return client.post(url, json.dumps(data or {}), content_type='application/json',
                           headers=headers)

    def place(self, client=None, items=None):
        response = self.post(client or self.client, '/api/orders', {
            'items': items or [{'productId': self.product.pk, 'quantity': 2},
                               {'productId': self.product2.pk, 'quantity': 1}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def new_id(self, prefix):
        self.next_id += 1
        return '%s%d' % (prefix, self.next_id)

    def paypal_authorizes(self, *, status='CREATED', amount=None, order_status='COMPLETED'):
        def handler(request, match):
            value = amount or body_of(request)['purchase_units'][0]['amount']['value']
            auth = fakes.authorization(self.new_id('AUTH'), value, status=status)
            return json_response(201, fakes.completed_order(self.new_id('PPORDER'), auth,
                                                            status=order_status))
        self.paypal.route('POST', r'/v2/checkout/orders$', handler)

    def paypal_captures(self, fee='0.81'):
        def handler(request, match):
            return json_response(201, fakes.capture(self.new_id('CAP'),
                                                    body_of(request)['amount']['value'], fee=fee))
        self.paypal.route('POST', r'/v2/payments/authorizations/(\w+)/capture$', handler)

    def paypal_auth_lookup(self, status='CREATED', expiration_time='2026-12-31T00:00:00Z'):
        self.paypal.route('GET', r'/v2/payments/authorizations/(\w+)$', lambda req, m: json_response(
            200, fakes.authorization(m.group(1), '29.68', status=status,
                                     expiration_time=expiration_time)))

    def paid_order(self):
        order = self.place()
        self.paypal_authorizes()
        response = self.post(self.client, '/api/orders/%s/pay' % order['orderId'], {'card': CARD})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def fulfilled_order(self):
        order = self.paid_order()
        self.paypal_auth_lookup()
        self.paypal_captures()
        response = self.post(self.staff_client, '/api/orders/%s/fulfil' % order['orderId'])
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()


class PlaceOrderTests(ApiTestCase):
    def test_order_uses_catalogue_prices_and_awaits_payment(self):
        order = self.place()
        self.assertIn('orderId', order)
        self.assertEqual(order['status'], 'Awaiting payment')
        self.assertEqual(order['total'], '29.68')
        self.assertEqual(order['currency'], 'USD')
        self.assertEqual(order['payment']['state'], 'NEW')
        self.assertEqual(len(order['lines']), 2)

    def test_rejects_unknown_products_and_bad_quantities(self):
        for items in ([{'productId': 999999, 'quantity': 1}],
                      [{'productId': self.product.pk, 'quantity': 0}],
                      [{'productId': 'x'}], []):
            response = self.post(self.client, '/api/orders', {'items': items})
            self.assertEqual(response.status_code, 400, items)

    def test_requires_authentication(self):
        response = Client().post('/api/orders', '{}', content_type='application/json')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error']['code'], 'not_authenticated')

    def test_csrf_failure_is_json(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.shopper)
        response = client.post('/api/orders', '{}', content_type='application/json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'csrf_failed')


class PayTests(ApiTestCase):
    def test_authorizes_the_order_total_with_a_card(self):
        order = self.place()
        self.paypal_authorizes()
        with self.assertLogs('apps.paypal_payments', level='INFO') as logs:
            response = self.post(self.client, '/api/orders/%s/pay' % order['orderId'],
                                 {'card': CARD})
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body['status'], 'Payment authorised')
        self.assertEqual(body['payment']['state'], 'AUTHORIZED')
        self.assertEqual(body['payment']['card'], {'brand': 'VISA', 'lastDigits': '1111',
                                                   'paymentMethodId': None})

        (create,) = self.paypal.calls('POST', r'/v2/checkout/orders$')
        sent = body_of(create)
        self.assertEqual(sent['intent'], 'AUTHORIZE')
        self.assertEqual(sent['purchase_units'][0]['amount'],
                         {'currency_code': 'USD', 'value': '29.68'})
        self.assertEqual(sent['payment_source']['card']['number'], '4111111111111111')
        self.assertTrue(create.headers['paypal-request-id'])
        self.assertEqual(create.headers['prefer'], 'return=representation')
        self.assertEqual(create.headers['authorization'], 'Bearer test-token')

        # The card number is never persisted and never logged.
        self.assertFalse(any('4111111111111111' in line for line in logs.output))
        self.assertNotIn('4111111111111111', self._dump_database())

    def _dump_database(self):
        dump = []
        with connection.cursor() as cursor:
            for table in connection.introspection.table_names():
                cursor.execute('SELECT * FROM "%s"' % table)
                dump.append(repr(cursor.fetchall()))
        return '\n'.join(dump)

    def test_double_click_does_not_authorize_twice(self):
        order = self.place()
        self.paypal_authorizes()
        url = '/api/orders/%s/pay' % order['orderId']
        first = self.post(self.client, url, {'card': CARD})
        second = self.post(self.client, url, {'card': CARD})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(self.paypal.calls('POST', r'/v2/checkout/orders$')), 1)

    def test_payment_in_flight_blocks_a_concurrent_attempt(self):
        order = self.place()
        PayPalPayment.objects.filter(order_id=order['orderId']).update(
            state=PayPalPayment.AUTHORIZING, lock_until=timezone.now() + timedelta(minutes=1))
        response = self.post(self.client, '/api/orders/%s/pay' % order['orderId'], {'card': CARD})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'payment_in_progress')
        self.assertFalse(self.paypal.calls('POST', r'/v2/checkout/orders$'))

    def test_declined_card_then_retry_with_a_new_request_id(self):
        order = self.place()
        self.paypal.route('POST', r'/v2/checkout/orders$',
                          lambda req, m: paypal_error(422, 'INSTRUMENT_DECLINED'))
        url = '/api/orders/%s/pay' % order['orderId']
        declined = self.post(self.client, url, {'card': CARD})
        self.assertEqual(declined.status_code, 402)
        self.assertEqual(declined.json()['error']['paypalIssue'], 'INSTRUMENT_DECLINED')
        self.assertEqual(PayPalPayment.objects.get(order_id=order['orderId']).state, 'FAILED')

        self.paypal_authorizes()
        self.assertEqual(self.post(self.client, url, {'card': CARD}).status_code, 200)
        ids = [r.headers['paypal-request-id'] for r in
               self.paypal.calls('POST', r'/v2/checkout/orders$')]
        self.assertEqual(len(set(ids)), 2)

    def test_unknown_outcome_is_resumed_with_the_same_request_id(self):
        order = self.place()
        self.paypal.route('POST', r'/v2/checkout/orders$',
                          lambda req, m: httpx.ReadTimeout('timed out'))
        url = '/api/orders/%s/pay' % order['orderId']
        response = self.post(self.client, url, {'card': CARD})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(PayPalPayment.objects.get(order_id=order['orderId']).state, 'AUTHORIZING')

        self.paypal_authorizes()
        self.assertEqual(self.post(self.client, url, {'card': CARD}).status_code, 200)
        ids = {r.headers['paypal-request-id'] for r in
               self.paypal.calls('POST', r'/v2/checkout/orders$')}
        self.assertEqual(len(ids), 1)   # every attempt (incl. the SDK-level resend) reused it

    def test_three_d_secure_challenge_is_refused(self):
        order = self.place()
        self.paypal_authorizes(order_status='PAYER_ACTION_REQUIRED')
        self.paypal.route('POST', r'/v2/checkout/orders$', lambda req, m: json_response(
            200, fakes.completed_order('PP1', None, status='PAYER_ACTION_REQUIRED')))
        response = self.post(self.client, '/api/orders/%s/pay' % order['orderId'], {'card': CARD})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()['error']['code'], 'payer_action_required')

    def test_approved_order_is_authorized_separately(self):
        order = self.place()
        self.paypal.route('POST', r'/v2/checkout/orders$', lambda req, m: json_response(
            200, fakes.completed_order('PPA', None, status='APPROVED')))
        self.paypal.route('POST', r'/v2/checkout/orders/PPA/authorize$', lambda req, m: json_response(
            201, fakes.completed_order('PPA', fakes.authorization('AUTHX', '29.68'))))
        response = self.post(self.client, '/api/orders/%s/pay' % order['orderId'], {'card': CARD})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['payment']['authorization']['id'], 'AUTHX')

    def test_a_hold_for_the_wrong_amount_is_voided(self):
        order = self.place()
        self.paypal_authorizes(amount='29.67')
        self.paypal.route('POST', r'/v2/payments/authorizations/(\w+)/void$',
                          lambda req, m: json_response(200, fakes.authorization(
                              m.group(1), '29.67', status='VOIDED')))
        response = self.post(self.client, '/api/orders/%s/pay' % order['orderId'], {'card': CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(self.paypal.calls('POST', r'/void$')), 1)

    def test_cannot_pay_someone_elses_order(self):
        order = self.place()
        response = self.post(self.login(self.other), '/api/orders/%s/pay' % order['orderId'],
                             {'card': CARD})
        self.assertEqual(response.status_code, 404)

    def test_invalid_card_is_rejected_locally(self):
        order = self.place()
        for card in ({**CARD, 'number': '4111111111111112'}, {**CARD, 'expiry': '2020-01'},
                     {**CARD, 'securityCode': '12a'}):
            response = self.post(self.client, '/api/orders/%s/pay' % order['orderId'],
                                 {'card': card})
            self.assertEqual(response.status_code, 400)
        self.assertFalse(self.paypal.calls('POST', r'/v2/checkout/orders$'))

    def test_rejected_credentials_are_a_configuration_error(self):
        order = self.place()
        self.paypal.route('POST', r'/v1/oauth2/token$', lambda req, m: json_response(
            401, {'error': 'invalid_client', 'error_description': 'Client Authentication failed'}))
        response = self.post(self.client, '/api/orders/%s/pay' % order['orderId'], {'card': CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['error']['code'], 'payment_provider_misconfigured')


class FulfilTests(ApiTestCase):
    def test_capture_records_amount_fee_and_net(self):
        body = self.fulfilled_order()
        self.assertEqual(body['status'], 'Complete')
        capture = body['payment']['capture']
        self.assertEqual(capture['amount'], '29.68')
        self.assertEqual(capture['paypalFee'], '0.81')
        self.assertEqual(capture['netAmount'], '28.87')
        self.assertEqual(body['payment']['state'], 'CAPTURED')
        (call,) = self.paypal.calls('POST', r'/capture$')
        self.assertEqual(body_of(call)['amount'], {'currency_code': 'USD', 'value': '29.68'})

    def test_fulfil_is_staff_only_and_idempotent(self):
        order = self.paid_order()
        self.assertEqual(self.post(self.client, '/api/orders/%s/fulfil' % order['orderId'])
                         .status_code, 403)
        self.paypal_auth_lookup()
        self.paypal_captures()
        url = '/api/orders/%s/fulfil' % order['orderId']
        self.assertEqual(self.post(self.staff_client, url).status_code, 200)
        self.assertEqual(self.post(self.staff_client, url).status_code, 200)
        self.assertEqual(len(self.paypal.calls('POST', r'/capture$')), 1)

    def test_stale_hold_is_reauthorized_before_capture(self):
        order = self.paid_order()
        PayPalPayment.objects.filter(order_id=order['orderId']).update(
            authorized_at=timezone.now() - timedelta(days=5))
        self.paypal_auth_lookup()
        self.paypal.route('POST', r'/v2/payments/authorizations/(\w+)/reauthorize$',
                          lambda req, m: json_response(201, fakes.authorization(
                              'REAUTH1', '29.68', create_time=timezone.now().isoformat())))
        self.paypal_captures()
        response = self.post(self.staff_client, '/api/orders/%s/fulfil' % order['orderId'])
        self.assertEqual(response.status_code, 200, response.content)
        (capture_call,) = self.paypal.calls('POST', r'/capture$')
        self.assertIn('/authorizations/REAUTH1/capture', capture_call.url)
        auth = response.json()['payment']['authorization']
        self.assertEqual(auth['id'], 'REAUTH1')
        self.assertIsNotNone(auth['reauthorizedAt'])

    def test_refused_capture_of_stale_hold_renews_it_once(self):
        order = self.paid_order()
        self.paypal_auth_lookup()
        attempts = []

        def capture(request, match):
            attempts.append(match.group(1))
            if len(attempts) == 1:
                return paypal_error(422, 'AUTHORIZATION_EXPIRED')
            return json_response(201, fakes.capture('CAPX', '29.68'))
        self.paypal.route('POST', r'/v2/payments/authorizations/(\w+)/capture$', capture)
        self.paypal.route('POST', r'/v2/payments/authorizations/(\w+)/reauthorize$',
                          lambda req, m: json_response(201, fakes.authorization('RE2', '29.68')))
        response = self.post(self.staff_client, '/api/orders/%s/fulfil' % order['orderId'])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(attempts[1], 'RE2')

    def test_hold_that_cannot_be_renewed_is_reported_actionably(self):
        order = self.paid_order()
        self.paypal_auth_lookup(expiration_time='2020-01-01T00:00:00Z')
        response = self.post(self.staff_client, '/api/orders/%s/fulfil' % order['orderId'])
        self.assertEqual(response.status_code, 409)
        error = response.json()['error']
        self.assertEqual(error['code'], 'authorization_expired')
        self.assertIn('pay again', error['message'])
        self.assertFalse(self.paypal.calls('POST', r'/capture$'))
        # ... and the shopper can pay again.
        self.paypal_authorizes()
        repay = self.post(self.client, '/api/orders/%s/pay' % order['orderId'], {'card': CARD})
        self.assertEqual(repay.status_code, 200, repay.content)

    def test_refused_capture_after_renewal_is_reported(self):
        order = self.paid_order()
        self.paypal_auth_lookup()
        self.paypal.route('POST', r'/capture$', lambda req, m: paypal_error(422, 'CARD_CLOSED'))
        self.paypal.route('POST', r'/reauthorize$',
                          lambda req, m: paypal_error(422, 'REAUTHORIZATION_TOO_SOON'))
        response = self.post(self.staff_client, '/api/orders/%s/fulfil' % order['orderId'])
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['paypalIssue'], 'CARD_CLOSED')
        self.assertEqual(PayPalPayment.objects.get(order_id=order['orderId']).state, 'AUTHORIZED')


class CancelTests(ApiTestCase):
    def test_cancel_releases_the_hold(self):
        order = self.paid_order()
        self.paypal.route('POST', r'/v2/payments/authorizations/(\w+)/void$',
                          lambda req, m: json_response(200, fakes.authorization(
                              m.group(1), '29.68', status='VOIDED')))
        response = self.post(self.staff_client, '/api/orders/%s/cancel' % order['orderId'])
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body['status'], 'Cancelled')
        self.assertEqual(body['payment']['state'], 'VOIDED')
        self.assertFalse(self.paypal.calls('POST', r'/capture$'))

    def test_cannot_cancel_after_fulfilment(self):
        order = self.fulfilled_order()
        response = self.post(self.staff_client, '/api/orders/%s/cancel' % order['orderId'])
        self.assertEqual(response.status_code, 409)

    def test_cancel_is_staff_only(self):
        order = self.paid_order()
        response = self.post(self.client, '/api/orders/%s/cancel' % order['orderId'])
        self.assertEqual(response.status_code, 403)


class RefundTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.paypal.route('POST', r'/v2/payments/captures/(\w+)/refund$', lambda req, m: json_response(
            201, {'id': self.new_id('REF'), 'status': 'COMPLETED',
                  'amount': body_of(req)['amount']}))

    def refund(self, order_id, key, amount=None, client=None):
        data = {} if amount is None else {'amount': amount}
        headers = {'Idempotency-Key': key} if key else {}
        return self.post(client or self.staff_client, '/api/orders/%s/refunds' % order_id,
                         data, **headers)

    def test_partial_refunds_and_idempotency(self):
        order_id = self.fulfilled_order()['orderId']
        first = self.refund(order_id, 'k1', '10.00')
        self.assertEqual(first.status_code, 201, first.content)
        self.assertIn('refundId', first.json())
        repeat = self.refund(order_id, 'k1', '10.00')
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(repeat.json()['refundId'], first.json()['refundId'])
        second = self.refund(order_id, 'k2', '10.00')
        self.assertEqual(second.status_code, 201)
        self.assertEqual(len(self.paypal.calls('POST', r'/refund$')), 2)
        payment = second.json()['order']['payment']
        self.assertEqual(payment['state'], 'PARTIALLY_REFUNDED')
        self.assertEqual(payment['refundableAmount'], '9.68')

    def test_never_refunds_beyond_the_capture(self):
        order_id = self.fulfilled_order()['orderId']
        self.assertEqual(self.refund(order_id, 'a', '20.00').status_code, 201)
        over = self.refund(order_id, 'b', '10.00')
        self.assertEqual(over.status_code, 422)
        self.assertEqual(over.json()['error']['code'], 'refund_exceeds_captured')
        rest = self.refund(order_id, 'c')     # no amount: the remainder
        self.assertEqual(rest.status_code, 201)
        self.assertEqual(rest.json()['amount'], '9.68')
        self.assertEqual(rest.json()['order']['payment']['state'], 'REFUNDED')
        self.assertEqual(self.refund(order_id, 'd').status_code, 409)
        self.assertEqual(len(self.paypal.calls('POST', r'/refund$')), 2)

    def test_same_key_different_amount_is_rejected(self):
        order_id = self.fulfilled_order()['orderId']
        self.refund(order_id, 'k', '1.00')
        self.assertEqual(self.refund(order_id, 'k', '2.00').status_code, 422)

    def test_key_is_required_and_order_must_be_fulfilled(self):
        order_id = self.fulfilled_order()['orderId']
        self.assertEqual(self.refund(order_id, None, '1.00').status_code, 400)
        unpaid = self.paid_order()['orderId']
        self.assertEqual(self.refund(unpaid, 'x', '1.00').status_code, 409)

    def test_refund_unknown_outcome_resubmits_same_request(self):
        order_id = self.fulfilled_order()['orderId']
        self.paypal.route('POST', r'/refund$', lambda req, m: httpx.ConnectError('down'))
        self.assertEqual(self.refund(order_id, 'k', '5.00').status_code, 503)
        self.assertEqual(PayPalRefund.objects.get().status, 'SUBMITTING')
        self.paypal.route('POST', r'/v2/payments/captures/(\w+)/refund$', lambda req, m: json_response(
            201, {'id': 'REFZ', 'status': 'COMPLETED', 'amount': body_of(req)['amount']}))
        retry = self.refund(order_id, 'k', '5.00')
        self.assertEqual(retry.status_code, 201, retry.content)
        ids = {r.headers['paypal-request-id'] for r in self.paypal.calls('POST', r'/refund$')}
        self.assertEqual(len(ids), 1)

    def test_other_shopper_cannot_refund(self):
        order_id = self.fulfilled_order()['orderId']
        self.assertEqual(self.refund(order_id, 'k', '1.00', client=self.login(self.other))
                         .status_code, 404)


class SavedCardTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.paypal.route('POST', r'/v3/vault/payment-tokens$', lambda req, m: json_response(201, {
            'id': self.new_id('TOK'), 'customer': {'id': 'CUST1'},
            'payment_source': {'card': {'last_digits': '1111', 'brand': 'VISA',
                                        'expiry': '2031-12', 'name': 'Test Shopper'}}}))
        self.paypal.route('DELETE', r'/v3/vault/payment-tokens/(\w+)$',
                          lambda req, m: fakes.HttpResponse(status_code=204, headers={}))

    def save(self, client=None):
        response = self.post(client or self.client, '/api/payment-methods', {'card': CARD})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def test_save_list_and_describe_safely(self):
        saved = self.save()
        self.assertEqual(saved['brand'], 'VISA')
        self.assertEqual(saved['lastDigits'], '1111')
        self.assertNotIn('4111111111111111', json.dumps(saved))
        listed = self.client.get('/api/payment-methods').json()['paymentMethods']
        self.assertEqual([c['paymentMethodId'] for c in listed], [saved['paymentMethodId']])
        # second save reuses the PayPal vault customer
        self.save()
        second = self.paypal.calls('POST', r'/v3/vault/payment-tokens$')[1]
        self.assertEqual(body_of(second)['customer'], {'id': 'CUST1'})

    def test_pay_with_saved_card(self):
        saved = self.save()
        order = self.place()
        self.paypal_authorizes()
        response = self.post(self.client, '/api/orders/%s/pay' % order['orderId'],
                             {'paymentMethodId': saved['paymentMethodId']})
        self.assertEqual(response.status_code, 200, response.content)
        (create,) = self.paypal.calls('POST', r'/v2/checkout/orders$')
        self.assertEqual(body_of(create)['payment_source'], {'card': {'vault_id': 'TOK1'}})

    def test_cards_are_private_to_their_owner(self):
        saved = self.save()
        other = self.login(self.other)
        self.assertEqual(other.get('/api/payment-methods').json()['paymentMethods'], [])
        self.assertEqual(other.delete('/api/payment-methods/%s' % saved['paymentMethodId'])
                         .status_code, 404)
        order = self.place(client=other)
        response = self.post(other, '/api/orders/%s/pay' % order['orderId'],
                             {'paymentMethodId': saved['paymentMethodId']})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.paypal.calls('DELETE', r'/payment-tokens/'))

    def test_deleted_card_is_gone_and_unusable(self):
        saved = self.save()
        response = self.client.delete('/api/payment-methods/%s' % saved['paymentMethodId'])
        self.assertEqual(response.status_code, 204)
        self.assertEqual(len(self.paypal.calls('DELETE', r'/v3/vault/payment-tokens/TOK1$')), 1)
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])
        order = self.place()
        response = self.post(self.client, '/api/orders/%s/pay' % order['orderId'],
                             {'paymentMethodId': saved['paymentMethodId']})
        self.assertEqual(response.status_code, 404)


class MyOrdersTests(ApiTestCase):
    def test_lists_only_the_callers_orders(self):
        mine = self.paid_order()
        self.place(client=self.login(self.other))
        orders = self.client.get('/api/my-orders').json()['orders']
        self.assertEqual([o['orderId'] for o in orders], [mine['orderId']])
        self.assertEqual(orders[0]['payment']['state'], 'AUTHORIZED')


class ReconciliationTests(ApiTestCase):
    def test_pages_through_every_window_and_lines_up_records(self):
        order = self.fulfilled_order()
        payment = PayPalPayment.objects.get(order_id=order['orderId'])
        now = timezone.now()
        PayPalPayment.objects.filter(pk=payment.pk).update(captured_at=now - timedelta(days=35))
        pages = []

        def search(request, match):
            query = dict(httpx.URL(request.url).params)
            pages.append((query['start_date'], query['page']))
            page = int(query['page'])
            txns = []
            if len(pages) == 1:
                txns = [{'transaction_info': {
                    'transaction_id': payment.capture_id, 'transaction_event_code': 'T0006',
                    'transaction_amount': {'currency_code': 'USD', 'value': '29.68'},
                    'fee_amount': {'currency_code': 'USD', 'value': '-0.81'},
                    'invoice_id': payment.invoice_id, 'transaction_status': 'S'}}]
            elif len(pages) == 2:
                txns = [{'transaction_info': {
                    'transaction_id': 'STRANGER', 'transaction_event_code': 'T0006',
                    'transaction_amount': {'currency_code': 'USD', 'value': '3.00'}}}]
            return json_response(200, {
                'transaction_details': txns, 'page': page,
                'total_pages': 2 if len(pages) <= 2 else 1,
                'last_refreshed_datetime': now.isoformat()})
        self.paypal.route('GET', r'/v1/reporting/transactions$', search)
        start = (now - timedelta(days=40)).strftime('%Y-%m-%dT%H:%M:%SZ')
        end = now.strftime('%Y-%m-%dT%H:%M:%SZ')

        self.assertEqual(self.client.get('/api/reconciliation', {'from': start, 'to': end})
                         .status_code, 403)
        response = self.staff_client.get('/api/reconciliation', {'from': start, 'to': end})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual(len(pages), 3)                  # 2 pages of window 1 + window 2
        self.assertEqual(len({p[0] for p in pages}), 2)  # two ≤31-day windows
        self.assertEqual(report['summary']['matched'], 1)
        self.assertTrue(report['matched'][0]['amountMatches'])
        self.assertEqual(report['paypalOnly'][0]['transactionId'], 'STRANGER')
        self.assertEqual(report['summary']['appOnly'], 0)

    def test_requires_a_valid_range(self):
        response = self.staff_client.get('/api/reconciliation', {'from': 'yesterday'})
        self.assertEqual(response.status_code, 400)


class ConfigurationTests(ApiTestCase):
    @override_settings(PAYPAL_BASE_URL='https://paypal.example.test')
    def test_base_url_override_is_used_for_every_call(self):
        transport = fakes.StubTransport()
        fakes.install(transport)
        self.paypal = transport
        self.paid_order()
        self.assertTrue(transport.requests)
        self.assertTrue(all(r.url.startswith('https://paypal.example.test/')
                            for r in transport.requests))
        self.assertIn('/v1/oauth2/token', transport.requests[0].url)

    @override_settings(PAYPAL_ENVIRONMENT='live', PAYPAL_BASE_URL=None)
    def test_unknown_environment_without_base_url_fails_fast(self):
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaises(ImproperlyConfigured):
            gateway.resolve_base_url()
