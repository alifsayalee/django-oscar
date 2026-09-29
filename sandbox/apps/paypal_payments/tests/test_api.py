import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from oscar.test.factories import create_product

from apps.paypal_payments import client as paypal_client
from apps.paypal_payments import gateway
from apps.paypal_payments.models import OrderPayment, PaymentRefund, SavedCard
from apps.paypal_payments.services import answer_status

from .stubs import (
    StubResponse, StubTransport, authorization_body, capture_body, json_response, order_body, paypal_error, refund_body,
    token_body)

PAYPAL = dict(PAYPAL_CLIENT_ID='test-id', PAYPAL_CLIENT_SECRET='test-secret', PAYPAL_ENVIRONMENT='sandbox',
              PAYPAL_CURRENCY='USD', PAYPAL_BASE_URL='', PAYPAL_TIMEOUT=5.0, PAYPAL_REFERENCE_PREFIX='test')
CARD = {'number': '4111111111111111', 'expiry': '2030-12', 'securityCode': '123', 'name': 'T Shopper'}


@override_settings(**PAYPAL)
class ApiTestCase(TestCase):

    def setUp(self):
        User = get_user_model()
        self.shopper = User.objects.create_user('shopper', 'shopper@example.com', 'pw-123456789')
        self.other = User.objects.create_user('other', 'other@example.com', 'pw-123456789')
        self.staff = User.objects.create_user('ops', 'ops@example.com', 'pw-123456789', is_staff=True)
        self.product = create_product(price=Decimal('10.00'), num_in_stock=50)
        self.transport = StubTransport()
        self.previous = paypal_client.install_client(
            paypal_client.build_client(paypal_client.paypal_config(), transport=self.transport))

    def tearDown(self):
        paypal_client.install_client(self.previous)

    def as_user(self, user):
        self.client.force_login(user)

    def post(self, url, body=None, **headers):
        return self.client.post(url, data=json.dumps(body or {}), content_type='application/json',
                                headers=headers)

    def place_order(self, quantity=2):
        self.as_user(self.shopper)
        response = self.post('/api/orders', {'items': [{'productId': self.product.pk, 'quantity': quantity}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()['orderId']

    def paid_order(self, **order_kwargs):
        order_id = self.place_order()
        payment = OrderPayment.objects.get(order__number=order_id)
        self.transport.on('POST', r'/v2/checkout/orders$',
                          json_response(200, order_body(invoice=payment.reference + '-1', **order_kwargs)))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 200, response.content)
        return order_id


class PayTests(ApiTestCase):

    def test_order_starts_awaiting_payment_with_catalogue_total(self):
        order_id = self.place_order(quantity=2)
        payment = OrderPayment.objects.get(order__number=order_id)
        self.assertEqual(payment.state, OrderPayment.AWAITING_PAYMENT)
        self.assertEqual(payment.amount, Decimal('20.00'))
        self.assertEqual(payment.currency, 'USD')

    def test_pay_authorizes_the_exact_total_under_a_derived_reference(self):
        order_id = self.paid_order()
        payment = OrderPayment.objects.get(order__number=order_id)
        [call] = self.transport.calls('POST', r'/v2/checkout/orders$')
        body = call.body.value
        self.assertEqual(body['intent'], 'AUTHORIZE')
        self.assertEqual(body['purchase_units'][0]['amount'], {'currency_code': 'USD', 'value': '20.00'})
        self.assertEqual(body['purchase_units'][0]['invoice_id'], payment.attempt_reference)
        self.assertEqual(call.headers['paypal-request-id'], payment.attempt_reference)
        self.assertEqual(payment.state, OrderPayment.AUTHORIZED)
        self.assertEqual(payment.authorization_id, 'AUTH1')
        self.assertEqual(payment.card_last_digits, '1111')

    def test_paying_twice_makes_one_provider_call(self):
        order_id = self.paid_order()
        second = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['payment']['authorization']['id'], 'AUTH1')
        self.assertEqual(len(self.transport.calls('POST', r'/v2/checkout/orders$')), 1)

    def test_declined_authorization_is_not_done_and_order_stays_payable(self):
        order_id = self.place_order()
        self.transport.on('POST', r'/v2/checkout/orders$', json_response(200, order_body(auth_status='DENIED')))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(OrderPayment.objects.get(order__number=order_id).state, OrderPayment.AUTHORIZATION_FAILED)

    def test_unlisted_status_is_unknown_not_done(self):
        order_id = self.place_order()
        self.transport.on('POST', r'/v2/checkout/orders$', json_response(200, order_body(auth_status='SOMETHING_NEW')))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 504)
        self.assertEqual(OrderPayment.objects.get(order__number=order_id).state,
                         OrderPayment.AUTHORIZATION_UNKNOWN)

    def test_amount_mismatch_is_flagged_not_done(self):
        order_id = self.place_order()
        self.transport.on('POST', r'/v2/checkout/orders$', json_response(200, order_body(amount='19.99')))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(OrderPayment.objects.get(order__number=order_id).state, OrderPayment.NEEDS_REVIEW)

    def test_card_refusal_is_reported_and_retry_uses_a_new_attempt(self):
        order_id = self.place_order()
        self.transport.on('POST', r'/v2/checkout/orders$', paypal_error(422, 'TRANSACTION_REFUSED'),
                          json_response(200, order_body()))
        refused = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(refused.json()['error']['code'], 'transaction_refused')
        again = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(again.status_code, 200)
        first, second = self.transport.calls('POST', r'/v2/checkout/orders$')
        self.assertNotEqual(first.headers['paypal-request-id'], second.headers['paypal-request-id'])

    def test_lost_answer_is_unknown_then_resolved_under_the_same_reference(self):
        order_id = self.place_order()
        payment = OrderPayment.objects.get(order__number=order_id)
        self.transport.on('POST', r'/v2/checkout/orders$', httpx.ReadTimeout('no reply'),
                          paypal_error(422, 'DUPLICATE_INVOICE_ID'))
        self.transport.on('GET', r'/v1/reporting/transactions$', json_response(200, {
            'transaction_details': [{'transaction_info': {'transaction_id': 'AUTH1',
                                                          'invoice_id': payment.reference + '-1'}}],
            'page': 1, 'total_pages': 1}), repeat=False)
        self.transport.on('GET', r'/v2/payments/authorizations/AUTH1$', json_response(200, authorization_body()))
        self.transport.on('GET', r'/v1/reporting/transactions$', json_response(200, {'page': 1, 'total_pages': 1}))

        first = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(first.status_code, 504)
        self.assertTrue(first.json()['error']['outcomeUnknown'])
        self.assertEqual(OrderPayment.objects.get(pk=payment.pk).state, OrderPayment.AUTHORIZATION_UNKNOWN)

        second = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(second.status_code, 200, second.content)
        refs = {c.headers['paypal-request-id'] for c in self.transport.calls('POST', r'/v2/checkout/orders$')}
        self.assertEqual(refs, {payment.reference + '-1'})
        self.assertEqual(OrderPayment.objects.get(pk=payment.pk).authorization_id, 'AUTH1')

    def test_refused_connection_is_known_failure_not_unknown(self):
        order_id = self.place_order()
        self.transport.on('POST', r'/v2/checkout/orders$', httpx.ConnectError('refused'))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()['error']['outcomeUnknown'])
        self.assertEqual(OrderPayment.objects.get(order__number=order_id).state, OrderPayment.AUTHORIZATION_FAILED)

    def test_other_shopper_cannot_see_or_pay_my_order(self):
        order_id = self.place_order()
        self.as_user(self.other)
        self.assertEqual(self.post('/api/orders/%s/pay' % order_id, {'card': CARD}).status_code, 404)
        self.assertEqual(self.client.get('/api/my-orders').json()['orders'], [])

    def test_bad_credentials_are_a_configuration_error(self):
        order_id = self.place_order()
        self.transport.on('POST', r'/v1/oauth2/token$', json_response(401, {'error': 'invalid_client'}))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['error']['code'], 'paypal_auth_failed')
        self.assertEqual(self.transport.calls('POST', r'/v2/checkout/orders$'), [])

    def test_card_number_never_stored(self):
        self.paid_order()
        for payment in OrderPayment.objects.all():
            self.assertNotIn('4111111111111111', json.dumps({f.name: str(getattr(payment, f.name))
                                                             for f in payment._meta.fields}))


class FulfilTests(ApiTestCase):

    def fulfil(self, order_id):
        self.as_user(self.staff)
        return self.post('/api/orders/%s/fulfil' % order_id)

    def test_only_staff_can_fulfil(self):
        order_id = self.paid_order()
        self.assertEqual(self.post('/api/orders/%s/fulfil' % order_id).status_code, 403)

    def test_capture_records_fee_and_net(self):
        order_id = self.paid_order(created=timezone.now().strftime('%Y-%m-%dT%H:%M:%SZ'),
                                   expires=(timezone.now() + timedelta(days=29)).strftime('%Y-%m-%dT%H:%M:%SZ'))
        self.transport.on('POST', r'/authorizations/AUTH1/capture$', json_response(200, capture_body()))
        response = self.fulfil(order_id)
        self.assertEqual(response.status_code, 200, response.content)
        capture = response.json()['payment']['capture']
        self.assertEqual((capture['amount'], capture['paypalFee'], capture['netAmount']), ('20.00', '0.88', '19.12'))
        [call] = self.transport.calls('POST', r'/capture$')
        self.assertEqual(call.body.value['amount'], {'currency_code': 'USD', 'value': '20.00'})
        self.assertTrue(call.body.value['final_capture'])
        self.assertEqual(self.fulfil(order_id).status_code, 200)
        self.assertEqual(len(self.transport.calls('POST', r'/capture$')), 1)

    def _fresh(self):
        now = timezone.now()
        return dict(created=now.strftime('%Y-%m-%dT%H:%M:%SZ'),
                    expires=(now + timedelta(days=29)).strftime('%Y-%m-%dT%H:%M:%SZ'))

    def test_capture_timeout_is_unknown_and_repeat_resends_same_reference(self):
        order_id = self.paid_order(**self._fresh())
        # The write times out and so does the immediate same-reference check.
        self.transport.on('POST', r'/authorizations/AUTH1/capture$', httpx.ReadTimeout('no reply'),
                          httpx.ReadTimeout('no reply'), json_response(200, capture_body()))
        first = self.fulfil(order_id)
        self.assertEqual(first.status_code, 504)
        self.assertEqual(OrderPayment.objects.get(order__number=order_id).state, OrderPayment.CAPTURE_UNKNOWN)
        second = self.fulfil(order_id)
        self.assertEqual(second.status_code, 200)
        refs = {c.headers['paypal-request-id'] for c in self.transport.calls('POST', r'/capture$')}
        self.assertEqual(len(self.transport.calls('POST', r'/capture$')), 3)
        self.assertEqual(len(refs), 1)

    def test_stale_authorization_is_renewed_before_capture(self):
        old = timezone.now() - timedelta(days=5)
        order_id = self.paid_order(created=old.strftime('%Y-%m-%dT%H:%M:%SZ'),
                                   expires=(old + timedelta(days=29)).strftime('%Y-%m-%dT%H:%M:%SZ'))
        self.transport.on('POST', r'/authorizations/AUTH1/reauthorize$',
                          json_response(200, authorization_body(auth_id='AUTH2')))
        self.transport.on('POST', r'/authorizations/AUTH2/capture$', json_response(200, capture_body()))
        response = self.fulfil(order_id)
        self.assertEqual(response.status_code, 200, response.content)
        payment = OrderPayment.objects.get(order__number=order_id)
        self.assertEqual((payment.authorization_id, payment.original_authorization_id), ('AUTH2', 'AUTH1'))
        self.assertIsNotNone(payment.reauthorized_at)

    def test_expired_authorization_explains_what_to_do(self):
        old = timezone.now() - timedelta(days=31)
        order_id = self.paid_order(created=old.strftime('%Y-%m-%dT%H:%M:%SZ'),
                                   expires=(old + timedelta(days=29)).strftime('%Y-%m-%dT%H:%M:%SZ'))
        response = self.fulfil(order_id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'authorization_expired')
        self.assertIn('pay again', response.json()['error']['message'])
        self.assertEqual(self.transport.calls('POST', r'/capture$|/reauthorize$'), [])
        # The shopper may now pay again.
        self.assertEqual(OrderPayment.objects.get(order__number=order_id).state,
                         OrderPayment.AUTHORIZATION_EXPIRED)

    def test_refused_renewal_still_tries_the_original_hold(self):
        old = timezone.now() - timedelta(days=5)
        order_id = self.paid_order(created=old.strftime('%Y-%m-%dT%H:%M:%SZ'),
                                   expires=(old + timedelta(days=29)).strftime('%Y-%m-%dT%H:%M:%SZ'))
        self.transport.on('POST', r'/reauthorize$', paypal_error(422, 'REAUTHORIZATION_TOO_SOON'))
        self.transport.on('POST', r'/authorizations/AUTH1/capture$', json_response(200, capture_body()))
        self.assertEqual(self.fulfil(order_id).status_code, 200)


class CancelTests(ApiTestCase):

    def test_cancel_voids_the_hold(self):
        order_id = self.paid_order()
        self.transport.on('POST', r'/authorizations/AUTH1/void$',
                          json_response(200, authorization_body(status='VOIDED')))
        self.as_user(self.staff)
        response = self.post('/api/orders/%s/cancel' % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['payment']['state'], 'voided')
        self.assertEqual(response.json()['orderStatus'], 'Cancelled')
        self.assertEqual(self.post('/api/orders/%s/cancel' % order_id).status_code, 200)
        self.assertEqual(len(self.transport.calls('POST', r'/void$')), 1)

    def test_void_of_captured_is_not_done(self):
        order_id = self.paid_order()
        self.transport.on('POST', r'/void$', paypal_error(422, 'PREVIOUSLY_CAPTURED'))
        self.as_user(self.staff)
        response = self.post('/api/orders/%s/cancel' % order_id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'already_captured')

    def test_unpaid_order_cancels_without_paypal(self):
        order_id = self.place_order()
        self.as_user(self.staff)
        self.assertEqual(self.post('/api/orders/%s/cancel' % order_id).status_code, 200)
        self.assertEqual(self.transport.calls('POST', r'/void$'), [])


class RefundTests(ApiTestCase):

    def captured_order(self):
        now = timezone.now()
        order_id = self.paid_order(created=now.strftime('%Y-%m-%dT%H:%M:%SZ'),
                                   expires=(now + timedelta(days=29)).strftime('%Y-%m-%dT%H:%M:%SZ'))
        self.transport.on('POST', r'/capture$', json_response(200, capture_body()))
        self.as_user(self.staff)
        self.assertEqual(self.post('/api/orders/%s/fulfil' % order_id).status_code, 200)
        self.as_user(self.shopper)
        return order_id

    def test_same_key_refunds_once_distinct_keys_twice(self):
        order_id = self.captured_order()
        self.transport.on('POST', r'/captures/CAP1/refund$', json_response(200, refund_body(refund_id='R1')),
                          json_response(200, refund_body(refund_id='R2')))
        url = '/api/orders/%s/refunds' % order_id
        first = self.post(url, {'amount': '5.00'}, **{'Idempotency-Key': 'k1'})
        self.assertEqual(first.status_code, 201, first.content)
        self.assertIn('refundId', first.json())
        self.assertEqual(self.post(url, {'amount': '5.00'}, **{'Idempotency-Key': 'k1'}).json()['refundId'],
                         first.json()['refundId'])
        self.assertEqual(self.post(url, {'amount': '5.00'}, **{'Idempotency-Key': 'k2'}).status_code, 201)
        calls = self.transport.calls('POST', r'/refund$')
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0].headers['paypal-request-id'], calls[1].headers['paypal-request-id'])

    def test_cannot_refund_beyond_capture(self):
        order_id = self.captured_order()
        self.transport.on('POST', r'/refund$', json_response(200, refund_body(amount='15.00')))
        url = '/api/orders/%s/refunds' % order_id
        self.assertEqual(self.post(url, {'amount': '15.00'}, **{'Idempotency-Key': 'a'}).status_code, 201)
        over = self.post(url, {'amount': '5.01'}, **{'Idempotency-Key': 'b'})
        self.assertEqual(over.status_code, 409)
        self.assertEqual(over.json()['error']['code'], 'refund_exceeds_captured')
        self.assertEqual(len(self.transport.calls('POST', r'/refund$')), 1)

    def test_failed_refund_releases_its_reservation(self):
        order_id = self.captured_order()
        self.transport.on('POST', r'/refund$', json_response(200, refund_body(status='FAILED', amount='20.00')))
        url = '/api/orders/%s/refunds' % order_id
        self.assertEqual(self.post(url, {}, **{'Idempotency-Key': 'a'}).status_code, 409)
        self.assertEqual(OrderPayment.objects.get(order__number=order_id).refund_reserved, Decimal('0.00'))

    def test_key_reused_with_other_amount_is_rejected(self):
        order_id = self.captured_order()
        self.transport.on('POST', r'/refund$', json_response(200, refund_body()))
        url = '/api/orders/%s/refunds' % order_id
        self.post(url, {'amount': '5.00'}, **{'Idempotency-Key': 'a'})
        self.assertEqual(self.post(url, {'amount': '6.00'}, **{'Idempotency-Key': 'a'}).status_code, 409)

    def test_refund_requires_idempotency_key(self):
        order_id = self.captured_order()
        self.assertEqual(self.post('/api/orders/%s/refunds' % order_id, {'amount': '1.00'}).status_code, 400)
        self.assertEqual(PaymentRefund.objects.count(), 0)


class SavedCardTests(ApiTestCase):

    def save(self, **headers):
        self.as_user(self.shopper)
        return self.post('/api/payment-methods', {'card': CARD}, **headers)

    def test_save_list_pay_delete(self):
        self.transport.on('POST', r'/v3/vault/payment-tokens$', json_response(200, token_body()))
        saved = self.save(**{'Idempotency-Key': 's1'})
        self.assertEqual(saved.status_code, 201, saved.content)
        card_id = saved.json()['paymentMethodId']
        self.assertEqual(saved.json()['lastDigits'], '1111')
        self.assertNotIn('number', json.dumps(saved.json()))
        self.assertEqual([c['paymentMethodId'] for c in self.client.get('/api/payment-methods').json()
                          ['paymentMethods']], [card_id])

        order_id = self.place_order()
        payment = OrderPayment.objects.get(order__number=order_id)
        self.transport.on('POST', r'/v2/checkout/orders$',
                          json_response(200, order_body(invoice=payment.reference + '-1')))
        self.assertEqual(self.post('/api/orders/%s/pay' % order_id, {'paymentMethodId': card_id}).status_code, 200)
        [call] = self.transport.calls('POST', r'/v2/checkout/orders$')
        self.assertEqual(call.body.value['payment_source'], {'card': {'vault_id': 'TOK1'}})

        self.transport.on('DELETE', r'/v3/vault/payment-tokens/TOK1$', lambda: StubResponse(status_code=204))
        self.assertEqual(self.client.delete('/api/payment-methods/%s' % card_id).status_code, 200)
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])
        second_order = self.place_order()
        self.assertEqual(self.post('/api/orders/%s/pay' % second_order,
                                   {'paymentMethodId': card_id}).status_code, 404)

    def test_same_key_vaults_once(self):
        self.transport.on('POST', r'/v3/vault/payment-tokens$', json_response(200, token_body()))
        first = self.save(**{'Idempotency-Key': 's1'})
        second = self.save(**{'Idempotency-Key': 's1'})
        self.assertEqual(first.json()['paymentMethodId'], second.json()['paymentMethodId'])
        self.assertEqual(len(self.transport.calls('POST', r'/payment-tokens$')), 1)

    def test_other_shopper_cannot_see_use_or_delete(self):
        self.transport.on('POST', r'/v3/vault/payment-tokens$', json_response(200, token_body()))
        card_id = self.save().json()['paymentMethodId']
        self.as_user(self.other)
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])
        self.assertEqual(self.client.delete('/api/payment-methods/%s' % card_id).status_code, 404)
        response = self.post('/api/orders', {'items': [{'productId': self.product.pk, 'quantity': 1}]})
        pay = self.post('/api/orders/%s/pay' % response.json()['orderId'], {'paymentMethodId': card_id})
        self.assertEqual(pay.status_code, 404)
        self.assertEqual(SavedCard.objects.get(pk=card_id).state, SavedCard.ACTIVE)

    def test_delete_with_lost_answer_hides_card_and_repeat_settles_it(self):
        self.transport.on('POST', r'/v3/vault/payment-tokens$', json_response(200, token_body()))
        card_id = self.save().json()['paymentMethodId']
        self.transport.on('DELETE', r'/payment-tokens/TOK1$', httpx.ReadTimeout('x'), httpx.ReadTimeout('x'),
                          StubResponse(status_code=204))
        self.assertEqual(self.client.delete('/api/payment-methods/%s' % card_id).status_code, 504)
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])
        self.assertEqual(self.client.delete('/api/payment-methods/%s' % card_id).status_code, 200)


class ReconciliationTests(ApiTestCase):

    def test_whole_range_is_paged_and_matched(self):
        order_id = self.paid_order()
        payment = OrderPayment.objects.get(order__number=order_id)
        page1 = {'transaction_details': [{'transaction_info': {
            'transaction_id': 'AUTH1', 'invoice_id': payment.reference + '-1',
            'transaction_amount': {'currency_code': 'USD', 'value': '20.00'}}}],
            'page': 1, 'total_pages': 2, 'last_refreshed_datetime': '2026-09-29T00:00:00Z'}
        page2 = {'transaction_details': [{'transaction_info': {
            'transaction_id': 'FOREIGN', 'invoice_id': 'someone-else',
            'transaction_amount': {'currency_code': 'USD', 'value': '1.00'}}}],
            'page': 2, 'total_pages': 2, 'last_refreshed_datetime': '2026-09-29T00:00:00Z'}
        self.transport.on('GET', r'/v1/reporting/transactions$', json_response(200, page1),
                          json_response(200, page2))
        self.as_user(self.staff)
        response = self.client.get('/api/reconciliation', {'from': '2026-08-31T00:00:00Z',
                                                          'to': '2026-09-20T00:00:00Z'})
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body['paypal']['pagesFetched'], 2)
        self.assertEqual([m['orderId'] for m in body['matched']], [order_id])
        self.assertEqual([t['transactionId'] for t in body['paypalOnly']], ['FOREIGN'])
        self.assertEqual(body['appOnly'], [])
        pages = [c.url for c in self.transport.calls('GET', r'/v1/reporting/transactions$')]
        self.assertIn('page=2', pages[1])

    def test_range_longer_than_31_days_is_split(self):
        empty = lambda: json_response(200, {'page': 1, 'total_pages': 1})  # noqa: E731
        self.transport.on('GET', r'/v1/reporting/transactions$', empty, repeat=True)
        self.as_user(self.staff)
        response = self.client.get('/api/reconciliation', {'from': '2026-06-01T00:00:00Z',
                                                          'to': '2026-09-01T00:00:00Z'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.transport.calls('GET', r'/v1/reporting/transactions$')), 3)

    def test_shoppers_cannot_reconcile(self):
        self.as_user(self.shopper)
        self.assertEqual(self.client.get('/api/reconciliation', {'from': '2026-09-01T00:00:00Z',
                                                                'to': '2026-09-02T00:00:00Z'}).status_code, 403)


class OutcomeMappingTests(TestCase):

    def test_not_done_outcomes_never_answer_success(self):
        for outcome in ('pending', 'sending', 'failed', 'needs_review', 'unknown'):
            self.assertNotIn(answer_status(outcome), (200, 201, 204))

    def test_unlisted_statuses_are_unknown(self):
        for mapper in (gateway.authorization_outcome, gateway.capture_outcome, gateway.cancel_outcome,
                       gateway.refund_outcome, gateway.vault_outcome):
            self.assertEqual(mapper('SOMETHING_NEW'), gateway.UNKNOWN)
            self.assertEqual(mapper(None), gateway.UNKNOWN)

    def test_undone_capture_is_not_done(self):
        self.assertEqual(gateway.capture_outcome(gateway.CaptureStatus.REFUNDED), gateway.FAILED)

    def test_unsent_is_not_the_same_failure_as_unknown(self):
        unsent = gateway.translate(httpx.ConnectError('refused'))
        unknown = gateway.translate(httpx.ReadTimeout('no reply'))
        self.assertEqual((unsent.status_code, unsent.outcome_unknown), (502, False))
        self.assertEqual((unknown.status_code, unknown.outcome_unknown), (504, True))

    @override_settings(PAYPAL_CLIENT_ID='x', PAYPAL_CLIENT_SECRET='y', PAYPAL_ENVIRONMENT='live',
                       PAYPAL_CURRENCY='USD', PAYPAL_BASE_URL='')
    def test_unknown_environment_without_base_url_is_refused(self):
        with self.assertRaises(paypal_client.PayPalNotConfigured):
            paypal_client.paypal_config()

    @override_settings(PAYPAL_CLIENT_ID='x', PAYPAL_CLIENT_SECRET='y', PAYPAL_ENVIRONMENT='live',
                       PAYPAL_CURRENCY='USD', PAYPAL_BASE_URL='https://paypal.example.test')
    def test_base_url_override_is_used_for_every_call_including_token(self):
        transport = StubTransport()
        transport.on('GET', r'/v2/payments/captures/C1$', json_response(200, capture_body()))
        client = paypal_client.build_client(paypal_client.paypal_config(), transport=transport)
        with mock.patch.object(paypal_client, '_client', client):
            gateway.get_capture(paypal_client.get_client(), 'C1')
        self.assertTrue(all(r.url.startswith('https://paypal.example.test/') for r in transport.requests))
        self.assertEqual(len(transport.requests), 2)
