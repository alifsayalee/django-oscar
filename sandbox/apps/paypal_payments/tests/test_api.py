"""
API tests with PayPal faked at the SDK's transport seam.

The real SDK client builds every request (paths, headers, bodies); only the
HTTP exchange is replaced, so these tests see exactly what would go on the
wire. Run with ``sandbox/manage.py test apps.paypal_payments``.
"""
import json
import re
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from oscar.test.factories import create_product
from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpRequest, HttpResponse

from apps.paypal_payments import gateway
from apps.paypal_payments.models import OrderPayment, ProviderWrite

BASE = 'https://paypal.test'
CARD = {'number': '4111 1111 1111 1111', 'expiry': '2030-12', 'securityCode': '123', 'name': 'T Shopper'}


def iso(moment):
    return moment.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def json_response(status, body):
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


def paypal_error(status, issue, name='UNPROCESSABLE_ENTITY'):
    return json_response(status, {'name': name, 'message': 'Rejected.', 'debug_id': 'dbg1',
                                  'details': [{'issue': issue, 'description': issue.lower()}]})


def completed_order(auth_id='AUTH1', amount='10.00', status='CREATED', created=None, expires=None):
    created = created or datetime.now(dt_timezone.utc)
    expires = expires or created + timedelta(days=29)
    return json_response(201, {
        'id': 'ORDER1', 'status': 'COMPLETED', 'intent': 'AUTHORIZE',
        'create_time': iso(created),
        'purchase_units': [{'amount': {'currency_code': 'USD', 'value': amount}, 'payments': {
            'authorizations': [{'id': auth_id, 'status': status,
                                'amount': {'currency_code': 'USD', 'value': amount},
                                'create_time': iso(created), 'expiration_time': iso(expires)}]}}]})


def authorization(auth_id='AUTH1', status='CREATED', created=None, expires=None, amount='10.00'):
    created = created or datetime.now(dt_timezone.utc)
    expires = expires or created + timedelta(days=29)
    return json_response(200, {'id': auth_id, 'status': status,
                               'amount': {'currency_code': 'USD', 'value': amount},
                               'create_time': iso(created), 'update_time': iso(created),
                               'expiration_time': iso(expires)})


def capture(capture_id='CAP1', status='COMPLETED', amount='10.00'):
    return json_response(201, {
        'id': capture_id, 'status': status, 'amount': {'currency_code': 'USD', 'value': amount},
        'create_time': iso(datetime.now(dt_timezone.utc)), 'final_capture': True,
        'seller_receivable_breakdown': {
            'gross_amount': {'currency_code': 'USD', 'value': amount},
            'paypal_fee': {'currency_code': 'USD', 'value': '0.84'},
            'net_amount': {'currency_code': 'USD', 'value': str(Decimal(amount) - Decimal('0.84'))}}})


def refund_response(refund_id, amount, status='COMPLETED'):
    return json_response(201, {'id': refund_id, 'status': status,
                               'amount': {'currency_code': 'USD', 'value': amount},
                               'create_time': iso(datetime.now(dt_timezone.utc))})


class FakePayPal:
    """A transport answering queued responses per (method, URL pattern)."""

    def __init__(self):
        self.requests = []
        self.routes = []

    def on(self, method, pattern, *answers):
        self.routes.append((method, re.compile(pattern), list(answers)))
        return self

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if request.url.endswith('/v1/oauth2/token'):
            return json_response(200, {'access_token': 't', 'token_type': 'Bearer', 'expires_in': 3600})
        for method, pattern, answers in self.routes:
            if method == request.method and pattern.search(request.url):
                if not answers:
                    raise AssertionError('no answer left for %s %s' % (request.method, request.url))
                answer = answers.pop(0) if len(answers) > 1 else answers[0]
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError('unexpected %s %s' % (request.method, request.url))

    def close(self):
        pass

    def calls(self, method, pattern):
        return [r for r in self.requests if r.method == method and re.search(pattern, r.url)]


@override_settings(PAYPAL_CLIENT_ID='test-client', PAYPAL_CLIENT_SECRET='test-secret',
                   PAYPAL_ENVIRONMENT='sandbox', PAYPAL_CURRENCY='USD', PAYPAL_BASE_URL='',
                   PAYPAL_REFERENCE_PREFIX='test')
class PayPalApiTestCase(TestCase):

    def setUp(self):
        self.fake = FakePayPal()
        previous = gateway.install_client(PaypalClient(
            base_url=BASE, custom_http_client=self.fake,
            oauth2=ClientCredentials(client_id='test-client', client_secret='test-secret')))
        self.addCleanup(gateway.install_client, previous)
        sleeper = mock.patch('apps.paypal_payments.gateway.time.sleep')
        sleeper.start()
        self.addCleanup(sleeper.stop)
        User = get_user_model()
        self.shopper = User.objects.create_user('shopper', 'shopper@example.com', 'pw-123456789')
        self.other = User.objects.create_user('other', 'other@example.com', 'pw-123456789')
        self.staff = User.objects.create_user('op', 'op@example.com', 'pw-123456789', is_staff=True)
        self.product = create_product(price=Decimal('10.00'), num_in_stock=50)

    # helpers -------------------------------------------------------------

    def post(self, user, path, body=None, **headers):
        self.client.force_login(user)
        return self.client.post('/api' + path, data=json.dumps(body or {}),
                                content_type='application/json', headers=headers)

    def get(self, user, path):
        self.client.force_login(user)
        return self.client.get('/api' + path)

    def place(self, user=None, quantity=1):
        response = self.post(user or self.shopper, '/orders',
                             {'lines': [{'productId': self.product.pk, 'quantity': quantity}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()['orderId']

    def paid_order(self, **auth):
        self.fake.on('POST', r'/v2/checkout/orders$', completed_order(**auth))
        order_id = self.place()
        response = self.post(self.shopper, '/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 200, response.content)
        return order_id

    def captured_order(self):
        order_id = self.paid_order()
        self.fake.on('GET', r'/v2/payments/authorizations/AUTH1$', authorization())
        self.fake.on('POST', r'/authorizations/AUTH1/capture$', capture())
        self.assertEqual(self.post(self.staff, '/orders/%s/fulfil' % order_id).status_code, 200)
        return order_id


class OrderAndPayTests(PayPalApiTestCase):

    def test_place_order_returns_order_id_awaiting_payment(self):
        response = self.post(self.shopper, '/orders', {'lines': [{'productId': self.product.pk, 'quantity': 2}]})
        body = response.json()
        self.assertEqual(response.status_code, 201)
        self.assertTrue(body['orderId'])
        self.assertEqual(body['total'], '20.00')
        self.assertEqual(body['currency'], 'USD')
        self.assertEqual(body['payment']['state'], 'awaiting_payment')

    def test_order_idempotency_key_returns_the_same_order(self):
        body = {'lines': [{'productId': self.product.pk, 'quantity': 1}]}
        first = self.post(self.shopper, '/orders', body, **{'Idempotency-Key': 'k1'}).json()
        second = self.post(self.shopper, '/orders', body, **{'Idempotency-Key': 'k1'}).json()
        self.assertEqual(first['orderId'], second['orderId'])
        self.assertEqual(OrderPayment.objects.count(), 1)

    def test_pay_authorizes_exact_total_and_sends_card_only_to_paypal(self):
        order_id = self.paid_order()
        (call,) = self.fake.calls('POST', r'/v2/checkout/orders$')
        sent = call.body.value
        self.assertEqual(sent['intent'], 'AUTHORIZE')
        self.assertEqual(sent['purchase_units'][0]['amount'], {'currency_code': 'USD', 'value': '10.00'})
        self.assertEqual(sent['payment_source']['card']['number'], '4111111111111111')
        self.assertEqual(call.headers['prefer'], 'return=representation')
        write = ProviderWrite.objects.get(kind=ProviderWrite.CREATE_ORDER)
        self.assertEqual(call.headers['paypal-request-id'], write.ref)
        self.assertNotIn('4111', json.dumps(write.details) + write.error_message)
        op = OrderPayment.objects.get(order__number=order_id)
        self.assertEqual((op.state, op.authorization_id, op.order.status),
                         ('authorized', 'AUTH1', 'Being processed'))
        self.assertEqual(op.source.amount_allocated, Decimal('10.00'))

    def test_double_click_pay_makes_one_paypal_call(self):
        order_id = self.paid_order()
        again = self.post(self.shopper, '/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(self.fake.calls('POST', r'/v2/checkout/orders$')), 1)

    def test_repeat_while_claim_in_flight_makes_no_call(self):
        order_id = self.place()
        op = OrderPayment.objects.get(order__number=order_id)
        ProviderWrite.objects.create(ref=gateway.reference('o%d' % op.pk, 'pay1'),
                                     base_ref=gateway.reference('o%d' % op.pk, 'pay1'),
                                     kind=ProviderWrite.CREATE_ORDER, order_payment=op)
        response = self.post(self.shopper, '/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.fake.calls('POST', r'/v2/checkout/orders$'), [])

    def test_denied_authorization_is_declined_not_success(self):
        self.fake.on('POST', r'/v2/checkout/orders$', completed_order(status='DENIED'))
        order_id = self.place()
        response = self.post(self.shopper, '/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()['error']['code'], 'payment_declined')
        self.assertEqual(OrderPayment.objects.get(order__number=order_id).state, 'awaiting_payment')

    def test_a_declined_attempt_can_be_retried_under_a_new_reference(self):
        self.fake.on('POST', r'/v2/checkout/orders$', completed_order(status='DENIED'),
                     completed_order(auth_id='AUTH2'))
        order_id = self.place()
        self.assertEqual(self.post(self.shopper, '/orders/%s/pay' % order_id, {'card': CARD}).status_code, 402)
        self.assertEqual(self.post(self.shopper, '/orders/%s/pay' % order_id, {'card': CARD}).status_code, 200)
        first, second = self.fake.calls('POST', r'/v2/checkout/orders$')
        self.assertNotEqual(first.headers['paypal-request-id'], second.headers['paypal-request-id'])

    def test_unlisted_status_is_unknown_not_success(self):
        self.fake.on('POST', r'/v2/checkout/orders$', completed_order(status='SOMETHING_NEW'))
        order_id = self.place()
        response = self.post(self.shopper, '/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 504)
        self.assertEqual(OrderPayment.objects.get(order__number=order_id).state, 'awaiting_payment')

    def test_payer_action_required_is_reported(self):
        self.fake.on('POST', r'/v2/checkout/orders$', json_response(
            200, {'id': 'ORDER1', 'status': 'PAYER_ACTION_REQUIRED',
                  'purchase_units': [{'amount': {'currency_code': 'USD', 'value': '10.00'}}]}))
        order_id = self.place()
        response = self.post(self.shopper, '/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual((response.status_code, response.json()['error']['code']),
                         (402, 'payer_action_required'))

    def test_amount_mismatch_needs_review(self):
        self.fake.on('POST', r'/v2/checkout/orders$', completed_order(amount='9.00'))
        order_id = self.place()
        response = self.post(self.shopper, '/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(ProviderWrite.objects.get().outcome, ProviderWrite.NEEDS_REVIEW)

    def test_unsent_and_unknown_are_different_failures(self):
        self.fake.on('POST', r'/v2/checkout/orders$', httpx.ConnectError('refused'))
        order_id = self.place()
        unsent = self.post(self.shopper, '/orders/%s/pay' % order_id, {'card': CARD}).json()['error']
        self.assertEqual((unsent['code'], unsent.get('outcomeUnknown', False)), ('paypal_unreachable', False))

        self.fake.routes.clear()
        self.fake.on('POST', r'/v2/checkout/orders$', httpx.ReadTimeout('no reply'), httpx.ReadTimeout('no reply'))
        order_id2 = self.place()
        response = self.post(self.shopper, '/orders/%s/pay' % order_id2, {'card': CARD})
        self.assertEqual(response.status_code, 504)
        self.assertTrue(response.json()['error']['outcomeUnknown'])
        write = ProviderWrite.objects.filter(order_payment__order__number=order_id2).get()
        self.assertEqual(write.outcome, ProviderWrite.UNKNOWN)
        # The check is a resend under the SAME reference, never a new write.
        self.fake.routes.clear()
        self.fake.on('POST', r'/v2/checkout/orders$', completed_order())
        self.assertEqual(self.post(self.shopper, '/orders/%s/pay' % order_id2, {'card': CARD}).status_code, 200)
        refs = {r.headers['paypal-request-id'] for r in self.fake.calls('POST', r'/v2/checkout/orders$')
                if r.headers['paypal-request-id'].startswith(write.base_ref)}
        self.assertEqual(refs, {write.ref})

    def test_bad_credentials_are_a_configuration_error(self):
        class BadToken(FakePayPal):
            def send(self, request):
                self.requests.append(request)
                return json_response(401, {'error': 'invalid_client'})
        gateway.install_client(PaypalClient(base_url=BASE, custom_http_client=BadToken(),
                                            oauth2=ClientCredentials(client_id='x', client_secret='y')))
        order_id = self.place()
        response = self.post(self.shopper, '/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual((response.status_code, response.json()['error']['code']),
                         (502, 'paypal_credentials_rejected'))

    def test_invalid_card_is_rejected_before_paypal(self):
        order_id = self.place()
        response = self.post(self.shopper, '/orders/%s/pay' % order_id,
                             {'card': {**CARD, 'number': '4111111111111112'}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.fake.requests, [])


class FulfilCancelTests(PayPalApiTestCase):

    def test_fulfil_captures_and_reports_fee_and_net(self):
        order_id = self.captured_order()
        body = self.get(self.shopper, '/my-orders').json()['orders'][0]
        self.assertEqual(body['status'], 'Complete')
        self.assertEqual(body['payment']['capture'],
                         {'id': 'CAP1', 'status': 'COMPLETED', 'amount': '10.00',
                          'paypalFee': '0.84', 'netAmount': '9.16'})
        (call,) = self.fake.calls('POST', r'/capture$')
        self.assertEqual(call.body.value['amount'], {'currency_code': 'USD', 'value': '10.00'})
        # repeat: answered from the record, no second capture
        self.assertEqual(self.post(self.staff, '/orders/%s/fulfil' % order_id).status_code, 200)
        self.assertEqual(len(self.fake.calls('POST', r'/capture$')), 1)

    def test_unlisted_capture_status_is_unknown_not_done(self):
        order_id = self.paid_order()
        self.fake.on('GET', r'/v2/payments/authorizations/AUTH1$', authorization())
        self.fake.on('POST', r'/authorizations/AUTH1/capture$', capture(status='SOMETHING_NEW'))
        response = self.post(self.staff, '/orders/%s/fulfil' % order_id)
        self.assertEqual((response.status_code, response.json()['error']['code']), (504, 'outcome_unknown'))
        self.assertEqual(OrderPayment.objects.get(order__number=order_id).state, 'authorized')

    def test_pending_capture_is_accepted_not_done_and_settles_on_repeat(self):
        order_id = self.paid_order()
        self.fake.on('GET', r'/v2/payments/authorizations/AUTH1$', authorization())
        self.fake.on('POST', r'/authorizations/AUTH1/capture$', capture(status='PENDING'))
        self.fake.on('GET', r'/v2/payments/captures/CAP1$', capture(status='COMPLETED'))
        first = self.post(self.staff, '/orders/%s/fulfil' % order_id)
        self.assertEqual((first.status_code, first.json()['payment']['state']), (202, 'authorized'))
        second = self.post(self.staff, '/orders/%s/fulfil' % order_id)
        self.assertEqual((second.status_code, second.json()['payment']['state']), (200, 'captured'))
        self.assertEqual(len(self.fake.calls('POST', r'/capture$')), 1)

    def test_fulfil_and_cancel_are_staff_only(self):
        order_id = self.paid_order()
        self.assertEqual(self.post(self.shopper, '/orders/%s/fulfil' % order_id).status_code, 403)
        self.assertEqual(self.post(self.shopper, '/orders/%s/cancel' % order_id).status_code, 403)
        self.assertEqual(self.get(self.shopper, '/reconciliation?from=2026-01-01T00:00:00Z&to=2026-01-02T00:00:00Z').status_code, 403)

    def test_stale_authorization_is_renewed_before_capture(self):
        created = datetime.now(dt_timezone.utc) - timedelta(days=5)
        order_id = self.paid_order(created=created)
        self.fake.on('GET', r'/v2/payments/authorizations/AUTH1$', authorization(created=created))
        self.fake.on('POST', r'/authorizations/AUTH1/reauthorize$', authorization(auth_id='AUTH2'))
        self.fake.on('POST', r'/authorizations/AUTH2/capture$', capture())
        response = self.post(self.staff, '/orders/%s/fulfil' % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['payment']['authorization']['id'], 'AUTH2')
        self.assertEqual(self.fake.calls('POST', r'/AUTH1/capture$'), [])

    def test_unrenewable_authorization_says_what_to_do(self):
        created = datetime.now(dt_timezone.utc) - timedelta(days=5)
        order_id = self.paid_order(created=created)
        self.fake.on('GET', r'/v2/payments/authorizations/AUTH1$', authorization(created=created))
        self.fake.on('POST', r'/reauthorize$', paypal_error(422, 'REAUTHORIZATION_NOT_ALLOWED'))
        response = self.post(self.staff, '/orders/%s/fulfil' % order_id)
        error = response.json()['error']
        self.assertEqual((response.status_code, error['code']), (409, 'authorization_renewal_failed'))
        self.assertIn('ask the shopper to pay again', error['message'])

    def test_expired_authorization_is_not_captured(self):
        created = datetime.now(dt_timezone.utc) - timedelta(days=31)
        order_id = self.paid_order(created=created, expires=created + timedelta(days=29))
        self.fake.on('GET', r'/v2/payments/authorizations/AUTH1$',
                     authorization(created=created, expires=created + timedelta(days=29)))
        response = self.post(self.staff, '/orders/%s/fulfil' % order_id)
        self.assertEqual(response.json()['error']['code'], 'authorization_expired')
        self.assertEqual(self.fake.calls('POST', r'/capture$'), [])

    def test_cancel_voids_the_hold(self):
        order_id = self.paid_order()
        self.fake.on('POST', r'/authorizations/AUTH1/void$', authorization(status='VOIDED'))
        response = self.post(self.staff, '/orders/%s/cancel' % order_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()['payment']['state'], response.json()['status']), ('voided', 'Cancelled'))
        self.assertEqual(self.post(self.staff, '/orders/%s/cancel' % order_id).status_code, 200)
        self.assertEqual(len(self.fake.calls('POST', r'/void$')), 1)

    def test_previously_voided_is_a_landing_not_a_failure(self):
        order_id = self.paid_order()
        self.fake.on('POST', r'/void$', paypal_error(422, 'PREVIOUSLY_VOIDED'))
        self.fake.on('GET', r'/v2/payments/authorizations/AUTH1$', authorization(status='VOIDED'))
        self.assertEqual(self.post(self.staff, '/orders/%s/cancel' % order_id).status_code, 200)

    def test_cancel_after_capture_is_refused(self):
        order_id = self.captured_order()
        self.assertEqual(self.post(self.staff, '/orders/%s/cancel' % order_id).json()['error']['code'],
                         'already_fulfilled')


class RefundTests(PayPalApiTestCase):

    def test_partial_refunds_and_the_idempotency_key(self):
        order_id = self.captured_order()
        self.fake.on('POST', r'/captures/CAP1/refund$', refund_response('R1', '4.00'), refund_response('R2', '5.00'))
        first = self.post(self.shopper, '/orders/%s/refunds' % order_id, {'amount': '4.00'}, **{'Idempotency-Key': 'a'})
        repeat = self.post(self.shopper, '/orders/%s/refunds' % order_id, {'amount': '4.00'}, **{'Idempotency-Key': 'a'})
        second = self.post(self.shopper, '/orders/%s/refunds' % order_id, {'amount': '5.00'}, **{'Idempotency-Key': 'b'})
        self.assertEqual((first.status_code, repeat.status_code, second.status_code), (201, 201, 201))
        self.assertEqual(first.json()['refundId'], repeat.json()['refundId'])
        self.assertNotEqual(first.json()['refundId'], second.json()['refundId'])
        self.assertEqual(len(self.fake.calls('POST', r'/refund$')), 2)
        payment = second.json()['order']['payment']
        self.assertEqual((payment['state'], payment['refundedAmount'], payment['refundableAmount']),
                         ('partially_refunded', '9.00', '1.00'))
        over = self.post(self.shopper, '/orders/%s/refunds' % order_id, {'amount': '2.00'}, **{'Idempotency-Key': 'c'})
        self.assertEqual((over.status_code, over.json()['error']['code']), (409, 'refund_exceeds_captured'))
        self.assertEqual(len(self.fake.calls('POST', r'/refund$')), 2)

    def test_refund_requires_a_key_and_a_capture(self):
        order_id = self.paid_order()
        self.assertEqual(self.post(self.shopper, '/orders/%s/refunds' % order_id, {}).status_code, 400)
        response = self.post(self.shopper, '/orders/%s/refunds' % order_id, {}, **{'Idempotency-Key': 'k'})
        self.assertEqual(response.json()['error']['code'], 'not_captured')

    def test_failed_refund_releases_its_reservation(self):
        order_id = self.captured_order()
        self.fake.on('POST', r'/refund$', paypal_error(422, 'REFUND_NOT_ALLOWED'), refund_response('R1', '10.00'))
        self.assertEqual(self.post(self.shopper, '/orders/%s/refunds' % order_id, {}, **{'Idempotency-Key': 'x'}).status_code, 422)
        op = OrderPayment.objects.get(order__number=order_id)
        self.assertEqual(op.refund_reserved, Decimal('0.00'))
        full = self.post(self.shopper, '/orders/%s/refunds' % order_id, {}, **{'Idempotency-Key': 'y'})
        self.assertEqual(full.status_code, 201)
        self.assertEqual(full.json()['order']['payment']['state'], 'refunded')

    def test_refund_timeout_is_checked_by_same_reference_resend(self):
        order_id = self.captured_order()
        self.fake.on('POST', r'/refund$', httpx.ReadTimeout('slow'), httpx.ReadTimeout('slow'))
        unknown = self.post(self.shopper, '/orders/%s/refunds' % order_id, {'amount': '3.00'}, **{'Idempotency-Key': 't'})
        self.assertEqual(unknown.status_code, 504)
        # The reservation is kept while the outcome is unknown.
        self.assertEqual(OrderPayment.objects.get(order__number=order_id).refund_reserved, Decimal('3.00'))
        self.fake.routes.clear()
        self.fake.on('POST', r'/refund$', refund_response('R1', '3.00'))
        settled = self.post(self.shopper, '/orders/%s/refunds' % order_id, {'amount': '3.00'}, **{'Idempotency-Key': 't'})
        self.assertEqual(settled.status_code, 201)
        self.assertEqual(len({r.headers['paypal-request-id'] for r in self.fake.calls('POST', r'/refund$')}), 1)

    def test_other_shopper_cannot_see_or_refund(self):
        order_id = self.captured_order()
        response = self.post(self.other, '/orders/%s/refunds' % order_id, {}, **{'Idempotency-Key': 'z'})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.get(self.other, '/my-orders').json()['orders'], [])
        self.assertEqual(self.post(self.other, '/orders/%s/pay' % order_id, {'card': CARD}).status_code, 404)


class SavedCardTests(PayPalApiTestCase):

    def token(self, token_id='TOK1'):
        return json_response(201, {'id': token_id, 'customer': {'id': 'CUST1'},
                                   'payment_source': {'card': {'brand': 'VISA', 'last_digits': '1111',
                                                               'expiry': '2030-12'}}})

    def test_save_list_pay_delete(self):
        self.fake.on('POST', r'/v3/vault/payment-tokens$', self.token())
        saved = self.post(self.shopper, '/payment-methods', {'card': CARD})
        self.assertEqual(saved.status_code, 201)
        method_id = saved.json()['paymentMethodId']
        self.assertEqual(saved.json()['last4'], '1111')
        self.assertNotIn('4111111111111111', saved.content.decode())
        listed = self.get(self.shopper, '/payment-methods').json()['paymentMethods']
        self.assertEqual([m['paymentMethodId'] for m in listed], [method_id])
        self.assertEqual(self.get(self.other, '/payment-methods').json()['paymentMethods'], [])

        self.fake.on('POST', r'/v2/checkout/orders$', completed_order())
        order_id = self.place()
        paid = self.post(self.shopper, '/orders/%s/pay' % order_id, {'paymentMethodId': method_id})
        self.assertEqual(paid.status_code, 200)
        (call,) = self.fake.calls('POST', r'/v2/checkout/orders$')
        self.assertEqual(call.body.value['payment_source'], {'card': {'vault_id': 'TOK1'}})

        other_order = self.place(self.other)
        self.assertEqual(self.post(self.other, '/orders/%s/pay' % other_order,
                                   {'paymentMethodId': method_id}).status_code, 404)
        self.client.force_login(self.other)
        self.assertEqual(self.client.delete('/api/payment-methods/%s' % method_id).status_code, 404)

        self.fake.on('DELETE', r'/v3/vault/payment-tokens/TOK1$', HttpResponse(status_code=204, headers={}))
        self.client.force_login(self.shopper)
        self.assertEqual(self.client.delete('/api/payment-methods/%s' % method_id).status_code, 204)
        self.assertEqual(self.get(self.shopper, '/payment-methods').json()['paymentMethods'], [])
        next_order = self.place()
        self.assertEqual(self.post(self.shopper, '/orders/%s/pay' % next_order,
                                   {'paymentMethodId': method_id}).status_code, 404)

    def test_saving_the_same_card_twice_makes_one_token(self):
        self.fake.on('POST', r'/v3/vault/payment-tokens$', self.token(), self.token('TOK2'))
        first = self.post(self.shopper, '/payment-methods', {'card': CARD}).json()
        second = self.post(self.shopper, '/payment-methods', {'card': CARD}).json()
        self.assertEqual(first['paymentMethodId'], second['paymentMethodId'])
        self.assertEqual(len(self.fake.calls('POST', r'/payment-tokens$')), 1)


class ReconciliationTests(PayPalApiTestCase):

    def txn(self, transaction_id, when, amount, custom=''):
        return {'transaction_info': {
            'transaction_id': transaction_id, 'transaction_event_code': 'T0006',
            'transaction_initiation_date': iso(when), 'custom_field': custom,
            'transaction_amount': {'currency_code': 'USD', 'value': amount},
            'transaction_status': 'S'}}

    def test_pages_are_followed_and_both_sides_are_reported(self):
        self.captured_order()
        capture_time = ProviderWrite.objects.get(kind=ProviderWrite.CAPTURE).provider_time
        start = capture_time - timedelta(days=40)
        end = capture_time + timedelta(minutes=1)
        prefix = gateway.custom_id_prefix()
        page1 = json_response(200, {'transaction_details': [self.txn('OTHER1', capture_time, '5.00')],
                                    'page': 1, 'total_pages': 2})
        page2 = json_response(200, {'transaction_details': [
            self.txn('CAP1', capture_time, '10.00', prefix + '100001'),
            self.txn('ORPHAN', capture_time, '7.00', prefix + '999'),
            self.txn('OUTSIDE', end + timedelta(hours=1), '1.00')], 'page': 2, 'total_pages': 2})
        empty = json_response(200, {'transaction_details': [], 'page': 1, 'total_pages': 0})
        self.fake.on('GET', r'/v1/reporting/transactions\?.*page=1(&|$)', empty)
        self.fake.on('GET', r'/v1/reporting/transactions\?.*page=2(&|$)', page2)
        # the chunk holding the capture answers with two pages
        self.fake.routes.insert(0, ('GET', re.compile(
            r'/v1/reporting/transactions\?start_date=%s.*page=1(&|$)' % re.escape(
                iso(start + timedelta(days=31)).replace(':', '%3A'))), [page1]))
        response = self.get(self.staff, '/reconciliation?from=%s&to=%s' % (iso(start), iso(end)))
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual([m['transactionId'] for m in body['matched']], ['CAP1'])
        self.assertEqual([r['transactionId'] for r in body['paypalOnly']['thisSite']], ['ORPHAN'])
        self.assertEqual([r['transactionId'] for r in body['paypalOnly']['other']], ['OTHER1'])
        self.assertEqual(body['localOnly'], [])
        searches = self.fake.calls('GET', r'/v1/reporting/transactions')
        self.assertEqual(len(searches), 3)  # chunk 1: one page; chunk 2: two pages

    def test_a_local_capture_missing_at_paypal_is_local_only(self):
        self.captured_order()
        self.fake.on('GET', r'/v1/reporting/transactions', json_response(
            200, {'transaction_details': [], 'page': 1, 'total_pages': 1}))
        now = datetime.now(dt_timezone.utc)
        body = self.get(self.staff, '/reconciliation?from=%s&to=%s' % (
            iso(now - timedelta(days=1)), iso(now + timedelta(minutes=1)))).json()
        self.assertEqual([r['transactionId'] for r in body['localOnly']], ['CAP1'])
        self.assertTrue(body['localOnly'][0]['withinReportingLag'])

    def test_dates_must_carry_a_timezone(self):
        response = self.get(self.staff, '/reconciliation?from=2026-01-01T00:00:00&to=2026-01-02T00:00:00Z')
        self.assertEqual(response.status_code, 400)


class ConfigurationTests(TestCase):

    @override_settings(PAYPAL_BASE_URL='https://proxy.example/paypal', PAYPAL_ENVIRONMENT='live')
    def test_base_url_override_is_used_verbatim(self):
        self.assertEqual(gateway.resolve_base_url(), 'https://proxy.example/paypal')

    @override_settings(PAYPAL_BASE_URL='', PAYPAL_ENVIRONMENT='sandbox')
    def test_sandbox_host(self):
        self.assertEqual(gateway.resolve_base_url(), 'https://api-m.sandbox.paypal.com')

    @override_settings(PAYPAL_BASE_URL='', PAYPAL_ENVIRONMENT='elsewhere')
    def test_unknown_environment_without_override_fails_loudly(self):
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaises(ImproperlyConfigured):
            gateway.resolve_base_url()

    @override_settings(PAYPAL_BASE_URL='https://proxy.example/pp', PAYPAL_CLIENT_ID='a', PAYPAL_CLIENT_SECRET='b')
    def test_token_request_uses_the_override_too(self):
        fake = FakePayPal().on('GET', r'/v3/vault/payment-tokens/T$', json_response(200, {'id': 'T'}))
        with mock.patch('apps.paypal_payments.gateway.HttpxClient', return_value=fake):
            client = gateway.build_client()
        client.vault.get_payment_token('T')
        self.assertTrue(all(r.url.startswith('https://proxy.example/pp/') for r in fake.requests))
        self.assertTrue(fake.requests[0].url.endswith('/v1/oauth2/token'))
