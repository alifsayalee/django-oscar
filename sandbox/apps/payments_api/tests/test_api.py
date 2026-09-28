import json
from datetime import timedelta
from decimal import Decimal

import httpx
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from oscar.test.factories import create_product

from apps.payments_api import paypal_client
from apps.payments_api.models import PayPalPayment, ProviderWrite
from apps.payments_api.outcomes import answer, capture_outcome, refund_outcome

from . import stubs
from .stubs import StubTransport, json_response, paypal_error

CARD = {'number': '4111111111111111', 'expiry': '2030-12', 'securityCode': '123', 'name': 'Test Shopper'}
PAYPAL_SETTINGS = dict(
    PAYPAL_CLIENT_ID='test-client', PAYPAL_CLIENT_SECRET='test-secret', PAYPAL_ENVIRONMENT='sandbox',
    PAYPAL_CURRENCY='USD', PAYPAL_BASE_URL='', PAYPAL_REFERENCE_PREFIX='test')
CREATE = ('POST', '/v2/checkout/orders')
SEARCH = ('GET', '/v1/reporting/transactions')


def auth_path(auth_id: str, action: str = '') -> str:
    return '/v2/payments/authorizations/%s%s' % (auth_id, '/' + action if action else '')


@override_settings(**PAYPAL_SETTINGS)
class ApiTestCase(TestCase):

    def setUp(self):
        self.stub = StubTransport()
        stubs.install(self.stub)
        self.addCleanup(paypal_client.set_client, None)
        self.shopper = User.objects.create_user('alice', 'alice@example.com', 'pw-alice-123')
        self.other = User.objects.create_user('bob', 'bob@example.com', 'pw-bob-123')
        self.staff = User.objects.create_user('op', 'op@example.com', 'pw-op-123', is_staff=True)
        self.product = create_product(price=Decimal('7.99'), num_in_stock=50)
        self.client.force_login(self.shopper)

    # --- helpers
    def post(self, url, body=None, *, as_user=None, key=None):
        if as_user is not None:
            self.client.force_login(as_user)
        headers = {'Idempotency-Key': key} if key else {}
        response = self.client.post(url, data=json.dumps(body or {}), content_type='application/json',
                                    headers=headers)
        if as_user is not None:
            self.client.force_login(self.shopper)
        return response

    def place(self, quantity=2):
        response = self.post('/api/orders', {'items': [{'productId': self.product.pk, 'quantity': quantity}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()['orderId']

    def authorized_order(self):
        number = self.place()
        self.stub.on(*CREATE, json_response(201, stubs.paypal_order(stubs.authorization())))
        self.assertEqual(self.post('/api/orders/%s/pay' % number, {'card': CARD}).status_code, 200)
        return number

    def captured_order(self):
        number = self.authorized_order()
        self.stub.on('GET', auth_path('AUTH1'), json_response(200, stubs.authorization()))
        self.stub.on('POST', auth_path('AUTH1', 'capture'), json_response(201, stubs.capture()))
        self.assertEqual(self.post('/api/orders/%s/fulfil' % number, as_user=self.staff).status_code, 200)
        return number

    def payment(self, number):
        return PayPalPayment.objects.get(order__number=number)


class PlaceOrderTests(ApiTestCase):

    def test_order_is_priced_from_the_catalogue_in_the_configured_currency(self):
        response = self.post('/api/orders', {'items': [{'productId': self.product.pk, 'quantity': 3}]})
        body = response.json()
        self.assertEqual(response.status_code, 201)
        self.assertEqual((body['total'], body['currency']), ('23.97', 'USD'))
        self.assertEqual(body['payment']['state'], 'awaiting_payment')
        self.assertEqual(self.stub.requests, [])

    def test_unknown_product_and_bad_quantity_are_rejected(self):
        self.assertEqual(self.post('/api/orders', {'items': [{'productId': 999999, 'quantity': 1}]}).status_code, 422)
        self.assertEqual(self.post('/api/orders', {'items': [{'productId': self.product.pk, 'quantity': 0}]}).status_code, 422)

    def test_unauthenticated_callers_are_refused(self):
        self.client.logout()
        self.assertEqual(self.post('/api/orders', {'items': []}).status_code, 401)
        self.assertEqual(self.client.get('/api/my-orders').status_code, 401)


class AuthorizeTests(ApiTestCase):

    def test_the_same_pay_twice_sends_one_create_order_carrying_the_derived_reference(self):
        number = self.place()
        self.stub.on(*CREATE, json_response(201, stubs.paypal_order(stubs.authorization())))
        first = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        second = self.post('/api/orders/%s/pay' % number, {'card': CARD})

        self.assertEqual((first.status_code, second.status_code), (200, 200))
        calls = self.stub.calls(*CREATE)
        self.assertEqual(len(calls), 1)
        ref = 'test:order:%s:authorize:0' % number
        self.assertEqual(calls[0].headers['paypal-request-id'], ref)
        sent = calls[0].body.value
        self.assertEqual(sent['purchase_units'][0]['custom_id'], ref)
        self.assertEqual(sent['purchase_units'][0]['amount'], {'currency_code': 'USD', 'value': '15.98'})
        self.assertEqual(sent['intent'], 'AUTHORIZE')
        self.assertEqual(second.json()['payment']['authorization']['id'], 'AUTH1')

    def test_card_details_are_not_stored(self):
        number = self.place()
        self.stub.on(*CREATE, json_response(201, stubs.paypal_order(stubs.authorization())))
        self.post('/api/orders/%s/pay' % number, {'card': CARD})
        stored = json.dumps(list(ProviderWrite.objects.values()), default=str) + json.dumps(
            list(PayPalPayment.objects.values()), default=str)
        self.assertNotIn('4111111111111111', stored)
        self.assertNotIn('"123"', stored)

    def test_a_refused_card_leaves_the_order_payable_and_the_next_attempt_uses_a_new_reference(self):
        number = self.place()
        self.stub.on(*CREATE, paypal_error(422, 'TRANSACTION_REFUSED'),
                     json_response(201, stubs.paypal_order(stubs.authorization())))
        declined = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(declined.status_code, 402)
        self.assertEqual(declined.json()['error']['code'], 'payment_declined')
        self.assertEqual(self.payment(number).state, PayPalPayment.AWAITING_PAYMENT)

        retried = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(retried.status_code, 200)
        refs = [c.headers['paypal-request-id'] for c in self.stub.calls(*CREATE)]
        self.assertEqual(refs, ['test:order:%s:authorize:0' % number, 'test:order:%s:authorize:1' % number])

    def test_a_denied_authorization_is_not_done(self):
        number = self.place()
        self.stub.on(*CREATE, json_response(201, stubs.paypal_order(stubs.authorization(status='DENIED'))))
        response = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()['outcome'], 'failed')
        self.assertEqual(self.payment(number).state, PayPalPayment.AWAITING_PAYMENT)

    def test_an_unlisted_status_is_unknown_not_done(self):
        number = self.place()
        self.stub.on(*CREATE, json_response(201, stubs.paypal_order(stubs.authorization(status='SOMETHING_NEW'))))
        response = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()['outcome'], 'unknown')

    def test_a_browser_challenge_is_reported_not_followed(self):
        number = self.place()
        self.stub.on(*CREATE, json_response(201, stubs.paypal_order(None, status='PAYER_ACTION_REQUIRED')))
        response = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(response.status_code, 402)
        self.assertIn('3-D Secure', self.payment(number).last_error)

    def test_an_echoed_amount_that_differs_needs_review(self):
        number = self.place()
        self.stub.on(*CREATE, json_response(201, stubs.paypal_order(stubs.authorization(value='1.00'))))
        response = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'needs_review')
        self.assertEqual(self.payment(number).state, PayPalPayment.NEEDS_REVIEW)

    def test_a_read_timeout_is_unknown_and_is_settled_by_lookup_never_by_a_second_create(self):
        number = self.place()
        self.stub.on(*CREATE, httpx.ReadTimeout('no reply'))
        self.stub.on(*SEARCH, json_response(200, stubs.search_page([])))  # not reported yet
        first = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(first.status_code, 504)
        self.assertTrue(first.json()['error']['outcomeUnknown'])
        self.assertEqual(self.payment(number).state, PayPalPayment.AUTHORIZATION_UNKNOWN)

        ref = 'test:order:%s:authorize:0' % number
        self.stub.on(*SEARCH, json_response(200, stubs.search_page([
            {'transaction_id': 'AUTH9', 'custom_field': ref, 'transaction_initiation_date': '2026-09-28T10:00:00Z'}])))
        self.stub.on('GET', auth_path('AUTH9'), json_response(200, stubs.authorization('AUTH9', custom_id=ref)))
        second = self.post('/api/orders/%s/pay' % number, {'card': CARD})

        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(len(self.stub.calls(*CREATE)), 1)
        self.assertEqual(self.payment(number).authorization_id, 'AUTH9')

    def test_a_refused_connection_is_known_nothing_happened(self):
        number = self.place()
        self.stub.on(*CREATE, httpx.ConnectError('refused'))
        response = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()['error']['outcomeUnknown'])
        self.assertEqual(self.payment(number).state, PayPalPayment.AWAITING_PAYMENT)

    def test_rejected_credentials_are_our_problem_not_the_callers(self):
        number = self.place()
        stub = StubTransport()
        stub.send = lambda request: json_response(401, {'error': 'invalid_client'})  # the token request fails
        stubs.install(stub)
        response = self.post('/api/orders/%s/pay' % number, {'card': CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['error']['code'], 'paypal_credentials_rejected')

    def test_another_shoppers_order_is_not_found(self):
        number = self.place()
        response = self.post('/api/orders/%s/pay' % number, {'card': CARD}, as_user=self.other)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.stub.requests, [])


class FulfilTests(ApiTestCase):

    def test_fulfil_captures_and_records_fee_and_net(self):
        number = self.captured_order()
        payment = self.payment(number)
        self.assertEqual(payment.state, PayPalPayment.CAPTURED)
        self.assertEqual((payment.captured_amount, payment.paypal_fee, payment.net_amount),
                         (Decimal('15.98'), Decimal('0.95'), Decimal('15.03')))
        self.assertEqual(payment.order.status, 'Complete')
        capture_call = self.stub.calls('POST', auth_path('AUTH1', 'capture'))[0]
        self.assertEqual(capture_call.body.value['amount'], {'currency_code': 'USD', 'value': '15.98'})

    def test_only_staff_can_fulfil(self):
        number = self.authorized_order()
        self.assertEqual(self.post('/api/orders/%s/fulfil' % number).status_code, 403)

    def test_a_pending_capture_is_accepted_not_done(self):
        number = self.authorized_order()
        self.stub.on('GET', auth_path('AUTH1'), json_response(200, stubs.authorization()))
        self.stub.on('POST', auth_path('AUTH1', 'capture'), json_response(201, stubs.capture(status='PENDING')))
        response = self.post('/api/orders/%s/fulfil' % number, as_user=self.staff)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.payment(number).state, PayPalPayment.CAPTURE_PENDING)

    def test_a_capture_timeout_is_checked_by_resending_the_same_reference(self):
        number = self.authorized_order()
        self.stub.on('GET', auth_path('AUTH1'), json_response(200, stubs.authorization()))
        self.stub.on('POST', auth_path('AUTH1', 'capture'), httpx.ReadTimeout('no reply'),
                     json_response(201, stubs.capture()))
        response = self.post('/api/orders/%s/fulfil' % number, as_user=self.staff)
        self.assertEqual(response.status_code, 200)
        keys = {c.headers['paypal-request-id'] for c in self.stub.calls('POST', auth_path('AUTH1', 'capture'))}
        self.assertEqual(keys, {'test:order:%s:capture:AUTH1' % number})

    def test_a_stale_authorization_is_renewed_before_capture(self):
        number = self.authorized_order()
        PayPalPayment.objects.filter(order__number=number).update(
            authorization_created_at=timezone.now() - timedelta(days=5),
            original_authorization_at=timezone.now() - timedelta(days=5))
        self.stub.on('GET', auth_path('AUTH1'), json_response(200, stubs.authorization()))
        self.stub.on('POST', auth_path('AUTH1', 'reauthorize'), json_response(201, stubs.authorization('AUTH2')))
        self.stub.on('POST', auth_path('AUTH2', 'capture'), json_response(201, stubs.capture()))
        response = self.post('/api/orders/%s/fulfil' % number, as_user=self.staff)
        self.assertEqual(response.status_code, 200, response.content)
        payment = self.payment(number)
        self.assertEqual((payment.authorization_id, payment.reauthorized, payment.state),
                         ('AUTH2', True, PayPalPayment.CAPTURED))

    def test_a_hold_that_cannot_be_renewed_says_what_to_do(self):
        number = self.authorized_order()
        PayPalPayment.objects.filter(order__number=number).update(
            authorization_created_at=timezone.now() - timedelta(days=5))
        self.stub.on('GET', auth_path('AUTH1'), json_response(200, stubs.authorization()))
        self.stub.on('POST', auth_path('AUTH1', 'reauthorize'), paypal_error(422, 'REAUTHORIZATION_NOT_ALLOWED'))
        response = self.post('/api/orders/%s/fulfil' % number, as_user=self.staff)
        self.assertEqual(response.status_code, 409)
        message = response.json()['error']['message']
        self.assertIn('REAUTHORIZATION_NOT_ALLOWED', message)
        self.assertIn('/pay', message)
        self.assertEqual(self.stub.calls('POST', auth_path('AUTH1', 'capture')), [])
        self.assertEqual(self.payment(number).state, PayPalPayment.AUTHORIZATION_EXPIRED)

    def test_a_hold_older_than_29_days_is_not_reauthorized(self):
        number = self.authorized_order()
        PayPalPayment.objects.filter(order__number=number).update(
            authorization_created_at=timezone.now() - timedelta(days=30),
            original_authorization_at=timezone.now() - timedelta(days=30))
        self.stub.on('GET', auth_path('AUTH1'), json_response(200, stubs.authorization()))
        response = self.post('/api/orders/%s/fulfil' % number, as_user=self.staff)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'authorization_expired')
        self.assertEqual(self.stub.calls('POST', auth_path('AUTH1', 'reauthorize')), [])

    def test_an_expired_hold_reopens_payment(self):
        number = self.authorized_order()
        self.stub.on('GET', auth_path('AUTH1'), json_response(200, stubs.authorization(expires='2020-01-01T00:00:00Z')))
        response = self.post('/api/orders/%s/fulfil' % number, as_user=self.staff)
        self.assertEqual(response.status_code, 409)
        self.stub.on(*CREATE, json_response(201, stubs.paypal_order(stubs.authorization('AUTH3'))))
        self.assertEqual(self.post('/api/orders/%s/pay' % number, {'card': CARD}).status_code, 200)
        self.assertEqual(self.payment(number).authorization_id, 'AUTH3')


class CancelTests(ApiTestCase):

    def test_cancel_voids_the_hold(self):
        number = self.authorized_order()
        self.stub.on('POST', auth_path('AUTH1', 'void'), json_response(200, stubs.authorization(status='VOIDED')))
        response = self.post('/api/orders/%s/cancel' % number, as_user=self.staff)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((self.payment(number).state, self.payment(number).order.status),
                         (PayPalPayment.VOIDED, 'Cancelled'))

    def test_previously_voided_is_a_landing_confirmed_by_lookup(self):
        number = self.authorized_order()
        self.stub.on('POST', auth_path('AUTH1', 'void'), paypal_error(422, 'PREVIOUSLY_VOIDED'))
        self.stub.on('GET', auth_path('AUTH1'), json_response(200, stubs.authorization(status='VOIDED')))
        response = self.post('/api/orders/%s/cancel' % number, as_user=self.staff)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.payment(number).state, PayPalPayment.VOIDED)

    def test_cancel_after_fulfilment_is_refused(self):
        number = self.captured_order()
        self.assertEqual(self.post('/api/orders/%s/cancel' % number, as_user=self.staff).status_code, 409)

    def test_only_staff_can_cancel(self):
        number = self.authorized_order()
        self.assertEqual(self.post('/api/orders/%s/cancel' % number).status_code, 403)


class RefundTests(ApiTestCase):
    REFUND_PATH = ('POST', '/v2/payments/captures/CAP1/refund')

    def test_the_same_key_never_refunds_twice(self):
        number = self.captured_order()
        self.stub.on(*self.REFUND_PATH, json_response(201, stubs.refund()))
        first = self.post('/api/orders/%s/refunds' % number, {'amount': '5.00'}, key='k1')
        second = self.post('/api/orders/%s/refunds' % number, {'amount': '5.00'}, key='k1')
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertEqual(first.json()['refundId'], second.json()['refundId'])
        self.assertEqual(len(self.stub.calls(*self.REFUND_PATH)), 1)

    def test_distinct_partial_refunds_are_allowed_but_never_beyond_the_capture(self):
        number = self.captured_order()
        self.stub.on(*self.REFUND_PATH, json_response(201, stubs.refund('R1', value='10.00')),
                     json_response(201, stubs.refund('R2', value='5.98')))
        self.assertEqual(self.post('/api/orders/%s/refunds' % number, {'amount': '10.00'}, key='a').status_code, 201)
        over = self.post('/api/orders/%s/refunds' % number, {'amount': '6.00'}, key='b')
        self.assertEqual(over.status_code, 409)
        self.assertEqual(over.json()['error']['refundableAmount'], '5.98')
        rest = self.post('/api/orders/%s/refunds' % number, {}, key='c')
        self.assertEqual(rest.status_code, 201)
        self.assertEqual(rest.json()['amount'], '5.98')
        self.assertEqual(len(self.stub.calls(*self.REFUND_PATH)), 2)
        self.assertEqual(self.payment(number).state, PayPalPayment.REFUNDED)

    def test_an_unsettled_refund_keeps_its_amount_reserved(self):
        number = self.captured_order()
        self.stub.on(*self.REFUND_PATH, httpx.ReadTimeout('no reply'), httpx.ReadTimeout('no reply'))
        unknown = self.post('/api/orders/%s/refunds' % number, {'amount': '15.00'}, key='a')
        self.assertEqual(unknown.status_code, 504)
        self.assertEqual(self.post('/api/orders/%s/refunds' % number, {'amount': '1.00'}, key='b').status_code, 409)

    def test_a_key_reused_for_a_different_amount_is_refused(self):
        number = self.captured_order()
        self.stub.on(*self.REFUND_PATH, json_response(201, stubs.refund()))
        self.post('/api/orders/%s/refunds' % number, {'amount': '5.00'}, key='k')
        again = self.post('/api/orders/%s/refunds' % number, {'amount': '6.00'}, key='k')
        self.assertEqual(again.status_code, 422)

    def test_a_key_is_required_and_refunds_need_a_capture(self):
        number = self.authorized_order()
        self.assertEqual(self.post('/api/orders/%s/refunds' % number, {'amount': '1.00'}).status_code, 400)
        self.assertEqual(self.post('/api/orders/%s/refunds' % number, {'amount': '1.00'}, key='x').status_code, 409)

    def test_a_pending_refund_is_accepted_not_done(self):
        number = self.captured_order()
        self.stub.on(*self.REFUND_PATH, json_response(201, stubs.refund(status='PENDING')))
        self.assertEqual(self.post('/api/orders/%s/refunds' % number, {'amount': '5.00'}, key='p').status_code, 202)


class SavedCardTests(ApiTestCase):
    TOKENS = ('POST', '/v3/vault/payment-tokens')

    def save(self, key='save-1'):
        self.stub.on(*self.TOKENS, json_response(201, stubs.vault_token()))
        return self.post('/api/payment-methods', {'card': CARD}, key=key)

    def test_save_describes_the_card_safely_and_stores_no_card_number(self):
        response = self.save()
        body = response.json()
        self.assertEqual(response.status_code, 201)
        self.assertEqual((body['brand'], body['last4'], body['expiry']), ('VISA', '1111', '2030-12'))
        self.assertNotIn('4111111111111111', json.dumps(body))
        from oscar.core.loading import get_model
        card = get_model('payment', 'Bankcard').objects.get(pk=body['paymentMethodId'])
        self.assertEqual((card.number, card.partner_reference), ('XXXX-XXXX-XXXX-1111', 'TOK1'))

    def test_saving_twice_with_one_key_vaults_once(self):
        first, second = self.save('k'), self.save('k')
        self.assertEqual(first.json()['paymentMethodId'], second.json()['paymentMethodId'])
        self.assertEqual(len(self.stub.calls(*self.TOKENS)), 1)

    def test_a_saved_card_pays_by_vault_id_and_belongs_to_its_owner(self):
        pm = self.save().json()['paymentMethodId']
        self.client.force_login(self.other)
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])
        self.assertEqual(self.client.delete('/api/payment-methods/%s' % pm).status_code, 404)
        self.client.force_login(self.shopper)
        number = self.place()
        self.stub.on(*CREATE, json_response(201, stubs.paypal_order(stubs.authorization())))
        self.assertEqual(self.post('/api/orders/%s/pay' % number, {'paymentMethodId': pm}).status_code, 200)
        self.assertEqual(self.stub.calls(*CREATE)[0].body.value['payment_source'], {'card': {'vault_id': 'TOK1'}})

    def test_delete_removes_the_card_everywhere(self):
        pm = self.save().json()['paymentMethodId']
        self.stub.on('DELETE', '/v3/vault/payment-tokens/TOK1', json_response(204))
        response = self.client.delete('/api/payment-methods/%s' % pm)
        self.assertEqual(response.json()['vaultDeletion'], 'done')
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])
        number = self.place()
        self.assertEqual(self.post('/api/orders/%s/pay' % number, {'paymentMethodId': pm}).status_code, 404)

    def test_a_failed_vault_deletion_still_removes_the_card_here(self):
        pm = self.save().json()['paymentMethodId']
        self.stub.on('DELETE', '/v3/vault/payment-tokens/TOK1', httpx.ConnectError('refused'))
        response = self.client.delete('/api/payment-methods/%s' % pm)
        self.assertEqual(response.json()['vaultDeletion'], 'failed')
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])

    def test_invalid_card_numbers_are_rejected_without_echoing_them(self):
        response = self.post('/api/payment-methods', {'card': {**CARD, 'number': '4111111111111112'}})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('4111111111111112', response.content.decode())
        self.assertEqual(self.stub.requests, [])


class ReconciliationTests(ApiTestCase):

    def get(self, params):
        self.client.force_login(self.staff)
        return self.client.get('/api/reconciliation', params)

    def test_every_page_is_read_and_both_sides_are_lined_up(self):
        number = self.captured_order()
        mine = {'transaction_id': 'CAP1', 'custom_field': 'test:order:%s' % number,
                'transaction_initiation_date': '2026-09-28T11:00:00Z',
                'transaction_amount': {'currency_code': 'USD', 'value': '15.98'}}
        foreign = {'transaction_id': 'ELSEWHERE', 'transaction_initiation_date': '2026-09-28T11:30:00Z',
                   'transaction_amount': {'currency_code': 'USD', 'value': '3.00'}}
        self.stub.on(*SEARCH, json_response(200, stubs.search_page([mine], 1, 2)),
                     json_response(200, stubs.search_page([foreign], 2, 2)))
        response = self.get({'from': '2026-09-28T00:00:00Z', 'to': '2026-09-29T00:00:00Z'})
        body = response.json()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([self.stub.query(r)['page'] for r in self.stub.calls(*SEARCH)], [['1'], ['2']])
        self.assertEqual([m['paypalId'] for m in body['matched']], ['CAP1'])
        self.assertEqual([r['transactionId'] for r in body['paypalOnly']], ['ELSEWHERE'])
        # The authorization happened at PayPal but is not in PayPal's report: visible, not dropped.
        self.assertEqual([r['paypalId'] for r in body['appOnly']], ['AUTH1'])

    def test_a_long_range_is_split_into_31_day_windows(self):
        self.stub.on(*SEARCH, *[json_response(200, stubs.search_page([])) for _ in range(3)])
        response = self.get({'from': '2026-07-01T00:00:00Z', 'to': '2026-09-15T00:00:00Z'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.stub.calls(*SEARCH)), 3)

    def test_staff_only_and_valid_dates(self):
        self.assertEqual(self.client.get('/api/reconciliation', {'from': 'x', 'to': 'y'}).status_code, 403)
        self.assertEqual(self.get({'from': 'yesterday', 'to': '2026-09-29T00:00:00Z'}).status_code, 422)


class ConfigurationTests(TestCase):

    @override_settings(**{**PAYPAL_SETTINGS, 'PAYPAL_BASE_URL': 'https://paypal-proxy.example/base'})
    def test_the_base_url_override_carries_every_call_including_the_token_request(self):
        urls = []

        class Recorder(StubTransport):
            def send(self, request):
                urls.append(request.url)
                return super().send(request)

        stub = Recorder().on('GET', '/base/v2/payments/authorizations/A1', json_response(200, stubs.authorization('A1')))
        client = paypal_client.build_client(stub)
        # The stub answers the token by path; here the path carries the override's prefix.
        stub.routes[('POST', '/base/v1/oauth2/token')].append(stubs.token_response())
        client.payments.get_authorized_payment('A1')
        self.assertEqual(len(urls), 2)
        self.assertTrue(all(u.startswith('https://paypal-proxy.example/base/') for u in urls), urls)

    @override_settings(**{**PAYPAL_SETTINGS, 'PAYPAL_ENVIRONMENT': 'live'})
    def test_an_environment_without_a_known_host_must_name_its_base_url(self):
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaises(ImproperlyConfigured):
            paypal_client.base_url()


class OutcomeTests(TestCase):

    def test_undone_and_unlisted_statuses_are_never_done(self):
        self.assertEqual(capture_outcome('REFUNDED'), 'failed')
        self.assertEqual(capture_outcome('BRAND_NEW'), 'unknown')
        self.assertEqual(refund_outcome(None), 'unknown')

    def test_a_not_done_outcome_never_answers_success(self):
        for outcome in ('pending', 'sending', 'failed', 'needs_review', 'unknown'):
            self.assertNotIn(answer(outcome, {}).status_code, (200, 201, 204))
