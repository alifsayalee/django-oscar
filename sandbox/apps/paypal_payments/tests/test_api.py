"""
API and service tests. The gateway (the PayPal boundary, tested on its own in
test_gateway) is replaced with fakes so these tests exercise the HTTP
surface, ownership rules, the payment state machine and the claims.
"""
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone as dj_timezone
from oscar.test.factories import create_product

from apps.paypal_payments import gateway
from apps.paypal_payments.gateway import (
    AuthorizationResult, AuthorizationState, CaptureResult, ProviderError, RefundResult,
    TransactionReport, PayPalTransaction, VaultedCard)
from apps.paypal_payments.models import PayPalPayment, PayPalRefund, SavedCard

CARD = {'number': '4111 1111 1111 1111', 'expiry': '2030-12', 'securityCode': '123',
        'name': 'Test Shopper',
        'billingAddress': {'addressLine1': '1 Main St', 'adminArea2': 'San Jose',
                           'adminArea1': 'CA', 'postalCode': '95131', 'countryCode': 'US'}}


def authorization_result(amount, **overrides):
    values = dict(paypal_order_id='PPORDER', order_status='COMPLETED', authorization_id='AUTH-1',
                  authorization_status='CREATED', decline_reason=None, amount=amount,
                  currency='USD', created_at=dj_timezone.now(),
                  expires_at=dj_timezone.now() + timedelta(days=29),
                  card_brand='VISA', card_last_digits='1111')
    values.update(overrides)
    return AuthorizationResult(**values)


def capture_result(amount):
    return CaptureResult(capture_id='CAP-1', status='COMPLETED', amount=amount, gross_amount=amount,
                         paypal_fee=Decimal('0.99'), net_amount=amount - Decimal('0.99'),
                         created_at=dj_timezone.now())


class ApiTestCase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.shopper = User.objects.create_user('shopper', 'shopper@example.com', 'pass-12345')
        self.other = User.objects.create_user('other', 'other@example.com', 'pass-12345')
        self.staff = User.objects.create_user('operator', 'operator@example.com', 'pass-12345',
                                              is_staff=True)
        self.product = create_product(price=Decimal('10.00'), num_in_stock=50)
        self.second = create_product(price=Decimal('2.34'), num_in_stock=50)
        self.client = Client()
        self.client.force_login(self.shopper)
        self.staff_client = Client()
        self.staff_client.force_login(self.staff)
        self.other_client = Client()
        self.other_client.force_login(self.other)

    def post(self, client, url, body=None, **headers):
        return client.post(url, data=json.dumps(body or {}), content_type='application/json',
                           headers=headers)

    def place(self, client=None):
        response = self.post(client or self.client, '/api/orders', {'items': [
            {'productId': self.product.pk, 'quantity': 1},
            {'productId': self.second.pk, 'quantity': 1}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def pay(self, order_id, amount=Decimal('12.34'), client=None, **body):
        body = body or {'card': CARD}
        with mock.patch.object(gateway, 'authorize_order_total',
                               return_value=authorization_result(amount)) as fake:
            response = self.post(client or self.client, '/api/orders/%s/pay' % order_id, body)
        return response, fake

    def fulfil(self, order_id, **fakes):
        with mock.patch.object(gateway, 'get_authorization', return_value=fakes.get('state') or AuthorizationState(
                authorization_id='AUTH-1', status='CREATED', amount=Decimal('12.34'),
                created_at=dj_timezone.now(), expires_at=dj_timezone.now() + timedelta(days=29))), \
                mock.patch.object(gateway, 'capture_authorization',
                                  return_value=capture_result(Decimal('12.34'))) as capture, \
                mock.patch.object(gateway, 'reauthorize', **fakes.get('reauthorize', {})) as reauth:
            response = self.post(self.staff_client, '/api/orders/%s/fulfil' % order_id)
        return response, capture, reauth

    def paid_and_fulfilled(self):
        order_id = self.place()['orderId']
        self.assertEqual(self.pay(order_id)[0].status_code, 200)
        self.assertEqual(self.fulfil(order_id)[0].status_code, 200)
        return order_id


class OrderAndPayTests(ApiTestCase):
    def test_place_order_awaits_payment(self):
        order = self.place()
        self.assertEqual(order['status'], 'Awaiting payment')
        self.assertEqual(order['total'], '12.34')
        self.assertEqual(order['currency'], 'USD')
        self.assertEqual(order['payment']['status'], 'AWAITING_PAYMENT')
        self.assertEqual(len(order['lines']), 2)

    def test_unknown_product_is_rejected(self):
        response = self.post(self.client, '/api/orders', {'items': [{'productId': 999999, 'quantity': 1}]})
        self.assertEqual(response.status_code, 422)

    def test_authorize_holds_exactly_the_order_total(self):
        order_id = self.place()['orderId']
        response, fake = self.pay(order_id)
        self.assertEqual(response.status_code, 200, response.content)
        kwargs = fake.call_args.kwargs
        self.assertEqual((kwargs['amount'], kwargs['currency']), (Decimal('12.34'), 'USD'))
        self.assertEqual(kwargs['card'].number, '4111111111111111')
        data = response.json()
        self.assertEqual(data['status'], 'Payment authorized')
        self.assertEqual(data['payment']['status'], 'AUTHORIZED')
        self.assertEqual(data['payment']['authorization']['authorizationId'], 'AUTH-1')

    def test_second_pay_never_authorizes_twice(self):
        order_id = self.place()['orderId']
        self.pay(order_id)
        response, fake = self.pay(order_id)
        self.assertEqual(response.status_code, 409)
        fake.assert_not_called()

    def test_pay_while_another_attempt_holds_the_claim(self):
        order_id = self.place()['orderId']
        PayPalPayment.objects.filter(order__number=order_id).update(
            state=PayPalPayment.AUTHORIZING, claim_expires_at=dj_timezone.now() + timedelta(minutes=5))
        response, fake = self.pay(order_id)
        self.assertEqual(response.status_code, 409)
        fake.assert_not_called()

    def test_unknown_outcome_is_replayed_with_the_same_request_id(self):
        order_id = self.place()['orderId']
        with mock.patch.object(gateway, 'authorize_order_total', side_effect=ProviderError(
                504, 'paypal_timeout', 'late', outcome_unknown=True)) as first:
            response = self.post(self.client, '/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 504)
        self.assertTrue(response.json()['error']['outcomeUnknown'])
        response, second = self.pay(order_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(first.call_args.kwargs['request_id'], second.call_args.kwargs['request_id'])

    def test_decline_can_be_retried_with_a_new_request(self):
        order_id = self.place()['orderId']
        with mock.patch.object(gateway, 'authorize_order_total', return_value=authorization_result(
                Decimal('12.34'), authorization_status='DENIED')) as first:
            response = self.post(self.client, '/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 402)
        response, second = self.pay(order_id)
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(first.call_args.kwargs['request_id'], second.call_args.kwargs['request_id'])

    def test_mismatched_hold_is_voided(self):
        order_id = self.place()['orderId']
        with mock.patch.object(gateway, 'void_authorization', return_value='VOIDED') as void:
            response, _ = self.pay(order_id, amount=Decimal('12.00'))
        self.assertEqual(response.status_code, 502)
        void.assert_called_once()
        self.assertEqual(PayPalPayment.objects.get(order__number=order_id).state, PayPalPayment.FAILED)

    def test_other_shopper_cannot_pay_or_see_order(self):
        order_id = self.place()['orderId']
        response, fake = self.pay(order_id, client=self.other_client)
        self.assertEqual(response.status_code, 404)
        fake.assert_not_called()
        self.assertEqual(self.other_client.get('/api/my-orders').json()['orders'], [])
        self.assertEqual(len(self.client.get('/api/my-orders').json()['orders']), 1)

    def test_anonymous_caller_is_refused(self):
        self.assertEqual(Client().post('/api/orders', data='{}', content_type='application/json').status_code, 401)

    def test_csrf_is_enforced_for_session_callers(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.shopper)
        self.assertEqual(self.post(client, '/api/orders', {'items': []}).status_code, 403)

    def test_invalid_card_is_rejected_without_echoing_it(self):
        order_id = self.place()['orderId']
        response, fake = self.pay(order_id, card=dict(CARD, number='4111111111111112'))
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(b'4111', response.content)
        fake.assert_not_called()


class FulfilCancelTests(ApiTestCase):
    def test_fulfil_is_staff_only(self):
        order_id = self.place()['orderId']
        self.pay(order_id)
        response = self.post(self.client, '/api/orders/%s/fulfil' % order_id)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.post(self.client, '/api/orders/%s/cancel' % order_id).status_code, 403)
        response = self.client.get('/api/reconciliation',
                                   {'from': '2026-01-01T00:00:00Z', 'to': '2026-01-02T00:00:00Z'})
        self.assertEqual(response.status_code, 403)

    def test_fulfil_captures_and_reports_fee_and_net(self):
        order_id = self.place()['orderId']
        self.pay(order_id)
        response, capture, reauth = self.fulfil(order_id)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data['status'], 'Complete')
        self.assertEqual(data['payment']['capture'], {
            'captureId': 'CAP-1', 'status': 'COMPLETED', 'amount': '12.34', 'paypalFee': '0.99',
            'netAmount': '11.35', 'capturedAt': data['payment']['capture']['capturedAt']})
        reauth.assert_not_called()
        self.assertEqual(capture.call_args.kwargs['amount'], Decimal('12.34'))

    def test_repeated_fulfil_captures_once(self):
        order_id = self.place()['orderId']
        self.pay(order_id)
        self.fulfil(order_id)
        response, capture, _ = self.fulfil(order_id)
        self.assertEqual(response.status_code, 200)
        capture.assert_not_called()

    def test_stale_authorization_is_renewed_before_capture(self):
        order_id = self.place()['orderId']
        self.pay(order_id)
        PayPalPayment.objects.filter(order__number=order_id).update(
            authorized_at=dj_timezone.now() - timedelta(days=5))
        renewed = AuthorizationState(authorization_id='AUTH-2', status='CREATED', amount=Decimal('12.34'),
                                     created_at=dj_timezone.now(),
                                     expires_at=dj_timezone.now() + timedelta(days=24))
        response, capture, reauth = self.fulfil(order_id, reauthorize={'return_value': renewed})
        self.assertEqual(response.status_code, 200, response.content)
        reauth.assert_called_once()
        self.assertEqual(capture.call_args.args[0], 'AUTH-2')
        self.assertEqual(response.json()['payment']['authorization']['reauthorizations'], 1)

    def test_authorization_that_cannot_be_renewed_tells_the_operator_what_to_do(self):
        order_id = self.place()['orderId']
        self.pay(order_id)
        PayPalPayment.objects.filter(order__number=order_id).update(
            authorized_at=dj_timezone.now() - timedelta(days=5))
        refused = ProviderError(422, 'paypal_rejected', 'A reauthorization is only allowed once.',
                                issue='REAUTHORIZATION_TOO_SOON')
        response, capture, _ = self.fulfil(order_id, reauthorize={'side_effect': refused})
        self.assertEqual(response.status_code, 409)
        error = response.json()['error']
        self.assertEqual(error['code'], 'authorization_not_renewable')
        self.assertIn('/cancel', error['message'])
        capture.assert_not_called()
        self.assertEqual(PayPalPayment.objects.get(order__number=order_id).state, PayPalPayment.AUTHORIZED)

    def test_expired_authorization_is_not_renewable(self):
        order_id = self.place()['orderId']
        self.pay(order_id)
        expired = AuthorizationState(authorization_id='AUTH-1', status='CREATED', amount=Decimal('12.34'),
                                     created_at=dj_timezone.now() - timedelta(days=31),
                                     expires_at=dj_timezone.now() - timedelta(days=1))
        response, capture, reauth = self.fulfil(order_id, state=expired)
        self.assertEqual(response.status_code, 409)
        reauth.assert_not_called()
        capture.assert_not_called()

    def test_cancel_releases_the_hold(self):
        order_id = self.place()['orderId']
        self.pay(order_id)
        with mock.patch.object(gateway, 'void_authorization', return_value='VOIDED') as void:
            response = self.post(self.staff_client, '/api/orders/%s/cancel' % order_id)
            again = self.post(self.staff_client, '/api/orders/%s/cancel' % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(again.status_code, 200)
        void.assert_called_once()
        self.assertEqual(response.json()['status'], 'Cancelled')
        self.assertEqual(response.json()['payment']['status'], 'VOIDED')

    def test_cancel_after_fulfilment_is_refused(self):
        order_id = self.paid_and_fulfilled()
        with mock.patch.object(gateway, 'void_authorization') as void:
            response = self.post(self.staff_client, '/api/orders/%s/cancel' % order_id)
        self.assertEqual(response.status_code, 409)
        void.assert_not_called()


class RefundTests(ApiTestCase):
    def refund(self, order_id, key, amount=None, client=None, result_amount=None):
        body = {'amount': amount} if amount is not None else {}
        with mock.patch.object(gateway, 'refund_capture', side_effect=lambda capture_id, **kw: RefundResult(
                refund_id='REF-%s' % kw['request_id'][:8], status='COMPLETED', amount=kw['amount'])) as fake:
            response = self.post(client or self.client, '/api/orders/%s/refunds' % order_id, body,
                                 **{'Idempotency-Key': key})
        return response, fake

    def test_partial_refunds_add_up_and_never_exceed_the_capture(self):
        order_id = self.paid_and_fulfilled()
        first, _ = self.refund(order_id, 'r-1', '5.00')
        second, _ = self.refund(order_id, 'r-2', '5.00')
        too_much, fake = self.refund(order_id, 'r-3', '5.00')
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertIn('refundId', first.json())
        self.assertNotEqual(first.json()['refundId'], second.json()['refundId'])
        self.assertEqual(too_much.status_code, 422)
        fake.assert_not_called()
        rest, _ = self.refund(order_id, 'r-4')
        self.assertEqual(rest.json()['amount'], '2.34')
        self.assertEqual(rest.json()['payment']['status'], 'REFUNDED')
        self.assertEqual(rest.json()['payment']['refundableAmount'], '0.00')

    def test_same_key_does_not_refund_twice(self):
        order_id = self.paid_and_fulfilled()
        first, _ = self.refund(order_id, 'same', '5.00')
        repeat, fake = self.refund(order_id, 'same', '5.00')
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(repeat.json()['refundId'], first.json()['refundId'])
        fake.assert_not_called()
        self.assertEqual(PayPalRefund.objects.count(), 1)

    def test_same_key_with_other_amount_is_rejected(self):
        order_id = self.paid_and_fulfilled()
        self.refund(order_id, 'same', '5.00')
        response, fake = self.refund(order_id, 'same', '6.00')
        self.assertEqual(response.status_code, 422)
        fake.assert_not_called()

    def test_refund_needs_an_idempotency_key(self):
        order_id = self.paid_and_fulfilled()
        response = self.post(self.client, '/api/orders/%s/refunds' % order_id, {'amount': '1.00'})
        self.assertEqual(response.status_code, 422)

    def test_failed_refund_releases_its_reservation(self):
        order_id = self.paid_and_fulfilled()
        with mock.patch.object(gateway, 'refund_capture', side_effect=ProviderError(
                422, 'paypal_rejected', 'no', issue='REFUND_NOT_ALLOWED')):
            response = self.post(self.client, '/api/orders/%s/refunds' % order_id,
                                 {'amount': '12.34'}, **{'Idempotency-Key': 'k1'})
        self.assertEqual(response.status_code, 422)
        full, _ = self.refund(order_id, 'k2', '12.34')
        self.assertEqual(full.status_code, 201)

    def test_unknown_refund_outcome_is_replayed_not_duplicated(self):
        order_id = self.paid_and_fulfilled()
        with mock.patch.object(gateway, 'refund_capture', side_effect=ProviderError(
                504, 'paypal_timeout', 'late', outcome_unknown=True)) as first:
            response = self.post(self.client, '/api/orders/%s/refunds' % order_id,
                                 {'amount': '5.00'}, **{'Idempotency-Key': 'k'})
        self.assertEqual(response.status_code, 504)
        # the held reservation still counts against the capture
        self.assertEqual(PayPalPayment.objects.get(order__number=order_id).refund_reserved, Decimal('5.00'))
        retry, second = self.refund(order_id, 'k', '5.00')
        self.assertEqual(retry.status_code, 201)
        self.assertEqual(first.call_args.kwargs['request_id'], second.call_args.kwargs['request_id'])
        self.assertEqual(retry.json()['payment']['refundedAmount'], '5.00')

    def test_other_shopper_cannot_refund(self):
        order_id = self.paid_and_fulfilled()
        response, fake = self.refund(order_id, 'x', '1.00', client=self.other_client)
        self.assertEqual(response.status_code, 404)
        fake.assert_not_called()

    def test_refund_before_fulfilment_is_refused(self):
        order_id = self.place()['orderId']
        self.pay(order_id)
        response, fake = self.refund(order_id, 'x', '1.00')
        self.assertEqual(response.status_code, 409)
        fake.assert_not_called()


class SavedCardTests(ApiTestCase):
    def save(self, client=None, **headers):
        vaulted = VaultedCard(token_id='TOKEN-%d' % SavedCard.objects.count(), customer_id='CUST-1',
                              brand='VISA', last_digits='1111', expiry='2030-12')
        with mock.patch.object(gateway, 'vault_card', return_value=vaulted) as fake:
            response = self.post(client or self.client, '/api/payment-methods', {'card': CARD}, **headers)
        return response, fake

    def test_save_list_pay_and_delete(self):
        with self.assertLogs('apps.paypal_payments', level='INFO') as logs:
            response, fake = self.save()
        self.assertEqual(response.status_code, 201, response.content)
        saved = response.json()
        self.assertEqual((saved['brand'], saved['lastDigits'], saved['expiry']), ('VISA', '1111', '2030-12'))
        self.assertNotIn(b'4111111111111111', response.content)
        self.assertNotIn('4111111111111111', '\n'.join(logs.output))
        self.assertIsNone(fake.call_args.kwargs['customer_id'])
        method_id = saved['paymentMethodId']

        listed = self.client.get('/api/payment-methods').json()['paymentMethods']
        self.assertEqual([c['paymentMethodId'] for c in listed], [method_id])
        self.assertEqual(self.other_client.get('/api/payment-methods').json()['paymentMethods'], [])

        order_id = self.place()['orderId']
        response, pay = self.pay(order_id, paymentMethodId=method_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(pay.call_args.kwargs['vault_id'], 'TOKEN-0')
        self.assertIsNone(pay.call_args.kwargs['card'])

        with mock.patch.object(gateway, 'delete_vaulted_card') as delete:
            forbidden = self.other_client.delete('/api/payment-methods/%s' % method_id)
            removed = self.client.delete('/api/payment-methods/%s' % method_id)
        self.assertEqual(forbidden.status_code, 404)
        self.assertEqual(removed.status_code, 200)
        delete.assert_called_once_with('TOKEN-0')
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])

        second_order = self.place()['orderId']
        response, pay = self.pay(second_order, paymentMethodId=method_id)
        self.assertEqual(response.status_code, 404)
        pay.assert_not_called()

    def test_second_card_reuses_the_paypal_customer(self):
        self.save()
        _, fake = self.save()
        self.assertEqual(fake.call_args.kwargs['customer_id'], 'CUST-1')

    def test_card_number_is_never_stored(self):
        self.save()
        stored = json.dumps(list(SavedCard.objects.values()), default=str)
        self.assertNotIn('4111111111111111', stored)
        self.assertNotIn('123', json.dumps(list(SavedCard.objects.values('brand', 'last_digits', 'expiry'))))

    def test_idempotency_key_saves_once(self):
        first, _ = self.save(**{'Idempotency-Key': 'card-1'})
        again, fake = self.save(**{'Idempotency-Key': 'card-1'})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()['paymentMethodId'], first.json()['paymentMethodId'])
        fake.assert_not_called()

    def test_other_shopper_cannot_pay_with_my_card(self):
        method_id = self.save()[0].json()['paymentMethodId']
        order_id = self.place(client=self.other_client)['orderId']
        response, fake = self.pay(order_id, client=self.other_client, paymentMethodId=method_id)
        self.assertEqual(response.status_code, 404)
        fake.assert_not_called()


class ReconciliationTests(ApiTestCase):
    def test_report_lines_paypal_up_with_the_app(self):
        order_id = self.paid_and_fulfilled()
        now = dj_timezone.now()
        report = TransactionReport(transactions=[
            PayPalTransaction(transaction_id='CAP-1', reference_id=None, event_code='T0006', status='S',
                              initiated_at=now, amount=Decimal('12.34'), currency='USD',
                              fee=Decimal('-0.99'), custom_field=order_id, invoice_id=None),
            PayPalTransaction(transaction_id='STRANGER', reference_id=None, event_code='T0006', status='S',
                              initiated_at=now, amount=Decimal('9.99'), currency='USD', fee=None,
                              custom_field='elsewhere', invoice_id=None),
        ], last_refreshed_at=now + timedelta(minutes=1))
        with mock.patch.object(gateway, 'search_transactions', return_value=report) as search:
            response = self.staff_client.get('/api/reconciliation', {
                'from': (now - timedelta(days=1)).isoformat(), 'to': (now + timedelta(days=1)).isoformat()})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data['summary']['matched'], 1)
        self.assertEqual(data['summary']['unknownToApp'], 1)
        self.assertEqual(data['matched'][0]['orderId'], order_id)
        self.assertEqual(data['unknownToApp'][0]['paypal']['transactionId'], 'STRANGER')
        search.assert_called_once()

    def test_capture_paypal_does_not_report_is_flagged(self):
        order_id = self.paid_and_fulfilled()
        now = dj_timezone.now()
        empty = TransactionReport(transactions=[], last_refreshed_at=now + timedelta(minutes=1))
        with mock.patch.object(gateway, 'search_transactions', return_value=empty):
            data = self.staff_client.get('/api/reconciliation', {
                'from': (now - timedelta(days=1)).isoformat(), 'to': (now + timedelta(days=1)).isoformat()}).json()
        self.assertEqual([m['orderId'] for m in data['missingFromPayPal']], [order_id])

    def test_invalid_range(self):
        response = self.staff_client.get('/api/reconciliation', {'from': 'yesterday', 'to': 'now'})
        self.assertEqual(response.status_code, 422)
