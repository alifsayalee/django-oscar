import json
from datetime import timedelta
from decimal import Decimal as D
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from oscar.test.factories import create_product

from apps.paypal_payments import services
from apps.paypal_payments.gateway import PayPalGateway
from apps.paypal_payments.models import PayPalPayment

from .fake_paypal import FakePayPal, json_response, paypal_error

VISA = {'number': '4111 1111 1111 1111', 'expiry': '2031-12', 'securityCode': '123', 'name': 'Ann Shopper'}

User = get_user_model()


@override_settings(PAYPAL_CURRENCY='USD', PAYPAL_ENVIRONMENT='sandbox',
                   PAYPAL_CLIENT_ID='test-id', PAYPAL_CLIENT_SECRET='test-secret')
class PayPalApiTestCase(TestCase):

    def setUp(self):
        self.fake = FakePayPal()
        patcher = mock.patch.object(services, 'paypal', PayPalGateway(self.fake.client()))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.shopper = User.objects.create_user('ann', 'ann@example.com', 'secret-pass-1')
        self.other = User.objects.create_user('bob', 'bob@example.com', 'secret-pass-2')
        self.staff = User.objects.create_user('op', 'op@example.com', 'secret-pass-3', is_staff=True)
        self.book = create_product(price=D('12.50'), num_in_stock=20)
        self.pen = create_product(price=D('3.25'), num_in_stock=20)
        self.client = self.login(self.shopper)
        self.operator = self.login(self.staff)

    def login(self, user):
        client = Client()
        client.force_login(user)
        return client

    def post(self, client, url, body=None, **headers):
        return client.post(url, json.dumps(body or {}), content_type='application/json', headers=headers)

    def place_order(self, client=None):
        response = self.post(client or self.client, '/api/orders', {'items': [
            {'productId': self.book.pk, 'quantity': 2}, {'productId': self.pen.pk, 'quantity': 1}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def paid_order(self):
        order = self.place_order()
        response = self.post(self.client, '/api/orders/%s/pay' % order['orderId'], {'card': VISA})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def fulfilled_order(self):
        order = self.paid_order()
        response = self.post(self.operator, '/api/orders/%s/fulfil' % order['orderId'])
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()


class OrderAndPaymentTests(PayPalApiTestCase):

    def test_order_starts_awaiting_payment_with_catalogue_total(self):
        order = self.place_order()
        self.assertIn('orderId', order)
        self.assertEqual(order['total'], '28.25')
        self.assertEqual(order['currency'], 'USD')
        self.assertEqual(order['payment']['state'], 'awaiting_payment')
        self.assertEqual(order['status'], 'Pending')

    def test_unknown_product_is_rejected(self):
        response = self.post(self.client, '/api/orders', {'items': [{'productId': 999999, 'quantity': 1}]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['code'], 'unknown_product')

    def test_pay_authorizes_exactly_the_order_total(self):
        order = self.paid_order()
        self.assertEqual(order['payment']['state'], 'authorized')
        self.assertEqual(order['payment']['authorization']['amount'], '28.25')
        self.assertEqual(order['payment']['card'], 'VISA ending 1111')
        self.assertEqual(order['status'], 'Being processed')
        sent = self.fake.calls('create_order')[0]
        self.assertEqual(sent.body.value['intent'], 'AUTHORIZE')
        self.assertEqual(sent.body.value['purchase_units'][0]['amount'],
                         {'currency_code': 'USD', 'value': '28.25'})
        self.assertEqual(sent.headers['prefer'], 'return=representation')
        self.assertTrue(sent.headers['paypal-request-id'])

    def test_double_click_does_not_authorize_twice(self):
        order = self.paid_order()
        again = self.post(self.client, '/api/orders/%s/pay' % order['orderId'], {'card': VISA})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()['payment']['authorization']['id'],
                         order['payment']['authorization']['id'])
        self.assertEqual(len(self.fake.calls('create_order')), 1)

    def test_declined_card_leaves_order_payable_with_a_new_request_id(self):
        order = self.place_order()
        url = '/api/orders/%s/pay' % order['orderId']
        response = self.post(self.client, url, {'card': dict(VISA, expiry='2020-01')})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['details']['paypalIssue'], 'CARD_EXPIRED')
        self.assertEqual(self.post(self.client, url, {'card': VISA}).status_code, 200)
        first, second = self.fake.calls('create_order')
        self.assertNotEqual(first.headers['paypal-request-id'], second.headers['paypal-request-id'])

    def test_card_number_is_never_stored(self):
        self.paid_order()
        self.post(self.client, '/api/payment-methods', {'card': VISA})
        with connection.cursor() as cursor:
            for table in connection.introspection.table_names(cursor):
                cursor.execute('SELECT * FROM "%s"' % table)
                for row in cursor.fetchall():
                    self.assertNotIn('4111111111111111', ' '.join(map(str, row)), table)

    def test_fulfil_captures_and_reports_fee_and_net(self):
        order = self.fulfilled_order()
        capture = order['payment']['capture']
        self.assertEqual(order['payment']['state'], 'captured')
        self.assertEqual(capture['amount'], '28.25')
        self.assertEqual(capture['paypalFee'], '1.48')
        self.assertEqual(capture['netAmount'], '26.77')
        self.assertEqual(order['status'], 'Complete')
        again = self.post(self.operator, '/api/orders/%s/fulfil' % order['orderId'])
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(self.fake.calls('capture')), 1)

    def test_cancel_before_fulfilment_voids_the_hold(self):
        order = self.paid_order()
        response = self.post(self.operator, '/api/orders/%s/cancel' % order['orderId'])
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['payment']['state'], 'cancelled')
        self.assertEqual(body['status'], 'Cancelled')
        self.assertEqual(len(self.fake.calls('void')), 1)
        self.assertEqual(len(self.fake.calls('capture')), 0)

    def test_cancel_after_fulfilment_is_refused(self):
        order = self.fulfilled_order()
        response = self.post(self.operator, '/api/orders/%s/cancel' % order['orderId'])
        self.assertEqual(response.status_code, 409)

    def test_my_orders_lists_only_the_callers_orders(self):
        mine = self.place_order()
        self.place_order(self.login(self.other))
        orders = self.client.get('/api/my-orders').json()['orders']
        self.assertEqual([o['orderId'] for o in orders], [mine['orderId']])


class StaleAuthorizationTests(PayPalApiTestCase):

    def age_authorization(self, order, days):
        payment = PayPalPayment.objects.get(order_id=order['orderId'])
        auth = self.fake.authorizations[payment.authorization_id]
        created = timezone.now() - timedelta(days=days)
        auth['create_time'] = created.strftime('%Y-%m-%dT%H:%M:%SZ')
        auth['expiration_time'] = (created + timedelta(days=29)).strftime('%Y-%m-%dT%H:%M:%SZ')
        payment.authorized_at = created
        payment.authorization_expires_at = created + timedelta(days=29)
        payment.save()

    def test_stale_authorization_is_renewed_then_captured(self):
        order = self.paid_order()
        original = order['payment']['authorization']['id']
        self.age_authorization(order, days=5)
        response = self.post(self.operator, '/api/orders/%s/fulfil' % order['orderId'])
        self.assertEqual(response.status_code, 200, response.content)
        payment = response.json()['payment']
        self.assertEqual(payment['state'], 'captured')
        self.assertEqual(payment['authorization']['reauthorizations'], 1)
        self.assertNotEqual(payment['authorization']['id'], original)
        self.assertEqual(len(self.fake.calls('reauthorize')), 1)
        self.assertIn(payment['authorization']['id'], self.fake.calls('capture')[0].url)

    def test_expired_authorization_is_reported_in_operator_terms(self):
        order = self.paid_order()
        self.age_authorization(order, days=30)
        response = self.post(self.operator, '/api/orders/%s/fulfil' % order['orderId'])
        self.assertEqual(response.status_code, 409)
        error = response.json()['error']
        self.assertEqual(error['code'], 'authorization_expired')
        self.assertIn('Cancel the order and ask the shopper to pay again', error['message'])
        self.assertEqual(self.fake.calls('capture'), [])
        self.assertEqual(PayPalPayment.objects.get(order_id=order['orderId']).state, 'authorized')

    def test_refused_renewal_still_tries_the_original_hold(self):
        order = self.paid_order()
        self.age_authorization(order, days=5)
        self.fake.reauthorize_response = paypal_error(422, 'REAUTHORIZATION_NOT_SUPPORTED')
        response = self.post(self.operator, '/api/orders/%s/fulfil' % order['orderId'])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn(order['payment']['authorization']['id'], self.fake.calls('capture')[0].url)


class RefundTests(PayPalApiTestCase):

    def refund(self, order, key, amount=None, client=None):
        body = {} if amount is None else {'amount': amount}
        return self.post(client or self.client, '/api/orders/%s/refunds' % order['orderId'], body,
                         **{'Idempotency-Key': key})

    def test_partial_refunds_cannot_exceed_the_capture(self):
        order = self.fulfilled_order()
        first = self.refund(order, 'r-1', '10.00')
        self.assertEqual(first.status_code, 201, first.content)
        self.assertIn('refundId', first.json())
        second = self.refund(order, 'r-2', '10.00')
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()['order']['payment']['state'], 'partially_refunded')
        too_much = self.refund(order, 'r-3', '8.26')
        self.assertEqual(too_much.status_code, 422)
        self.assertEqual(too_much.json()['error']['details']['refundable'], '8.25')
        rest = self.refund(order, 'r-4')
        self.assertEqual(rest.json()['amount'], '8.25')
        self.assertEqual(rest.json()['order']['payment']['state'], 'refunded')
        self.assertEqual(len(self.fake.calls('refund')), 3)

    def test_same_key_does_not_refund_twice(self):
        order = self.fulfilled_order()
        first = self.refund(order, 'same', '5.00')
        again = self.refund(order, 'same', '5.00')
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()['refundId'], first.json()['refundId'])
        self.assertEqual(len(self.fake.calls('refund')), 1)
        reused = self.refund(order, 'same', '6.00')
        self.assertEqual(reused.status_code, 422)

    def test_refund_requires_a_key_and_a_fulfilled_order(self):
        order = self.paid_order()
        no_key = self.post(self.client, '/api/orders/%s/refunds' % order['orderId'], {})
        self.assertEqual(no_key.status_code, 400)
        self.assertEqual(self.refund(order, 'k').status_code, 409)

    def test_another_shopper_cannot_refund(self):
        order = self.fulfilled_order()
        response = self.refund(order, 'x', client=self.login(self.other))
        self.assertEqual(response.status_code, 404)


class SavedCardTests(PayPalApiTestCase):

    def save_card(self, client=None):
        response = self.post(client or self.client, '/api/payment-methods', {'card': VISA})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def test_saved_card_is_described_safely_and_reused(self):
        card = self.save_card()
        self.assertEqual(card['last4'], '1111')
        self.assertEqual(card['brand'], 'VISA')
        self.assertNotIn('number', card)
        listed = self.client.get('/api/payment-methods').json()['paymentMethods']
        self.assertEqual([c['paymentMethodId'] for c in listed], [card['paymentMethodId']])

        order = self.place_order()
        response = self.post(self.client, '/api/orders/%s/pay' % order['orderId'],
                             {'paymentMethodId': card['paymentMethodId']})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['payment']['paymentMethodId'], card['paymentMethodId'])
        sent = self.fake.calls('create_order')[0].body.value['payment_source']['card']
        self.assertEqual(set(sent), {'vault_id'})

    def test_second_card_reuses_the_paypal_customer(self):
        self.save_card()
        self.save_card()
        first, second = self.fake.calls('vault')
        self.assertNotIn('customer', first.body.value)
        self.assertTrue(second.body.value['customer']['id'])

    def test_deleted_card_is_gone_and_unusable(self):
        card = self.save_card()
        url = '/api/payment-methods/%s' % card['paymentMethodId']
        self.assertEqual(self.client.delete(url).status_code, 200)
        self.assertEqual(len(self.fake.calls('delete_token')), 1)
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])
        order = self.place_order()
        response = self.post(self.client, '/api/orders/%s/pay' % order['orderId'],
                             {'paymentMethodId': card['paymentMethodId']})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.fake.calls('create_order'), [])

    def test_other_shoppers_cannot_see_use_or_delete_a_card(self):
        card = self.save_card()
        bob = self.login(self.other)
        self.assertEqual(bob.get('/api/payment-methods').json()['paymentMethods'], [])
        self.assertEqual(bob.delete('/api/payment-methods/%s' % card['paymentMethodId']).status_code, 404)
        order = self.place_order(bob)
        response = self.post(bob, '/api/orders/%s/pay' % order['orderId'],
                             {'paymentMethodId': card['paymentMethodId']})
        self.assertEqual(response.status_code, 404)


class AccessTests(PayPalApiTestCase):

    def test_anonymous_callers_are_rejected(self):
        self.assertEqual(Client().get('/api/my-orders').status_code, 401)

    def test_operator_actions_need_staff(self):
        order = self.paid_order()
        for action in ('fulfil', 'cancel'):
            response = self.post(self.client, '/api/orders/%s/%s' % (order['orderId'], action))
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get('/api/reconciliation?from=2026-01-01T00:00:00Z'
                                         '&to=2026-01-02T00:00:00Z').status_code, 403)

    def test_shopper_cannot_touch_another_shoppers_order(self):
        order = self.place_order()
        bob = self.login(self.other)
        response = self.post(bob, '/api/orders/%s/pay' % order['orderId'], {'card': VISA})
        self.assertEqual(response.status_code, 404)

    def test_csrf_is_enforced_for_session_callers(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.shopper)
        self.assertEqual(self.post(client, '/api/orders', {'items': []}).status_code, 403)
        token = client.get('/api/auth/csrf').json()['csrfToken']
        response = self.post(client, '/api/orders', {'items': [{'productId': self.pen.pk}]},
                             **{'X-CSRFToken': token})
        self.assertEqual(response.status_code, 201)

    def test_session_login(self):
        client = Client()
        response = self.post(client, '/api/auth/login', {'username': 'ann', 'password': 'secret-pass-1'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get('/api/my-orders').status_code, 200)


class FailureBoundaryTests(PayPalApiTestCase):

    def test_unsent_request_is_a_known_outcome(self):
        order = self.place_order()
        self.fake.fail_next['create_order'] = httpx.ConnectError('refused')
        response = self.post(self.client, '/api/orders/%s/pay' % order['orderId'], {'card': VISA})
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()['error']['details']['outcomeUnknown'])
        self.assertEqual(PayPalPayment.objects.get(order_id=order['orderId']).state, 'awaiting_payment')

    def test_lost_reply_is_an_unknown_outcome_resumed_with_the_same_request_id(self):
        order = self.place_order()
        url = '/api/orders/%s/pay' % order['orderId']
        self.fake.fail_next['create_order'] = httpx.ReadTimeout('no reply')
        response = self.post(self.client, url, {'card': VISA})
        self.assertEqual(response.status_code, 504)
        self.assertTrue(response.json()['error']['details']['outcomeUnknown'])
        payment = PayPalPayment.objects.get(order_id=order['orderId'])
        self.assertEqual(payment.state, 'authorizing')
        self.assertEqual(self.post(self.client, url, {'card': VISA}).status_code, 409)  # lease still held
        payment.claimed_at = timezone.now() - services.CLAIM_LEASE * 2
        payment.save()
        self.assertEqual(self.post(self.client, url, {'card': VISA}).status_code, 200)
        first, second = self.fake.calls('create_order')
        self.assertEqual(first.headers['paypal-request-id'], second.headers['paypal-request-id'])

    def test_rejected_credentials_are_a_configuration_fault(self):
        fake = FakePayPal()
        original = fake.send

        def reject_token(request):
            if request.url.endswith('/v1/oauth2/token'):
                fake.requests.append(request)
                return json_response(401, {'error': 'invalid_client', 'error_description': 'bad'})
            return original(request)

        fake.send = reject_token
        with mock.patch.object(services, 'paypal', PayPalGateway(fake.client())):
            order = self.place_order()
            response = self.post(self.client, '/api/orders/%s/pay' % order['orderId'], {'card': VISA})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['error']['code'], 'payment_provider_misconfigured')

    def test_unreadable_error_body_is_still_classified_by_status(self):
        card = self.post(self.client, '/api/payment-methods', {'card': VISA}).json()
        # A vault 404 body that does not match the SDK's error model (seen in the sandbox).
        self.fake.fail_next['delete_token'] = json_response(404, {
            'name': 'RESOURCE_NOT_FOUND', 'message': 'Not found.', 'debug_id': 'dbg',
            'details': [{'issue': 'INVALID_RESOURCE_ID'}],
            'links': [{'href': 'https://developer.paypal.com/docs', 'method': 'GET'}]})
        response = self.client.delete('/api/payment-methods/%s' % card['paymentMethodId'])
        self.assertEqual(response.status_code, 200)


    def test_undecodable_rejection_is_not_reported_as_an_unknown_outcome(self):
        order = self.place_order()
        # 422 is a typed arm of create_order; a body missing links[].rel fails to decode.
        self.fake.fail_next['create_order'] = json_response(422, {
            'name': 'UNPROCESSABLE_ENTITY', 'message': 'No.', 'debug_id': 'dbg',
            'links': [{'href': 'https://developer.paypal.com/docs', 'method': 'GET'}]})
        response = self.post(self.client, '/api/orders/%s/pay' % order['orderId'], {'card': VISA})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(response.json()['error']['details']['outcomeUnknown'])
        self.assertEqual(PayPalPayment.objects.get(order_id=order['orderId']).state, 'awaiting_payment')

    def test_unreadable_success_is_an_unknown_outcome(self):
        order = self.place_order()
        self.fake.fail_next['create_order'] = json_response(201, {'id': 42})
        response = self.post(self.client, '/api/orders/%s/pay' % order['orderId'], {'card': VISA})
        self.assertEqual(response.status_code, 502)
        self.assertTrue(response.json()['error']['details']['outcomeUnknown'])
        self.assertEqual(PayPalPayment.objects.get(order_id=order['orderId']).state, 'authorizing')


@mock.patch('apps.paypal_payments.gateway.SEARCH_PAGE_SIZE', 100)
@mock.patch('apps.paypal_payments.gateway.READ_BACKOFF_SECONDS', 0)
class ReconciliationTests(PayPalApiTestCase):

    def test_transient_paypal_failure_during_a_read_is_retried(self):
        self.fake.fail_next['search'] = json_response(503, {'name': 'SERVICE_UNAVAILABLE'})
        start = (timezone.now() - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        end = timezone.now().strftime('%Y-%m-%dT%H:%M:%SZ')
        response = self.operator.get('/api/reconciliation', {'from': start, 'to': end})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(self.fake.calls('search')), 2)

    def test_report_pages_through_paypal_and_lines_up_both_sides(self):
        order = self.fulfilled_order()
        capture_id = order['payment']['capture']['id']
        unknown = [{'transaction_id': 'STRANGER%05d' % i,
                    'transaction_amount': {'currency_code': 'USD', 'value': '1.00'}} for i in range(150)]
        self.fake.transactions = unknown + [
            {'transaction_id': capture_id, 'transaction_amount': {'currency_code': 'USD', 'value': '28.25'}}]
        start = (timezone.now() - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        end = (timezone.now() + timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
        response = self.operator.get('/api/reconciliation', {'from': start, 'to': end})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual(len(self.fake.calls('search')), 2)  # 151 records over pages of 100
        self.assertEqual(report['summary']['paypalOnly'], 150)
        self.assertEqual([m['paypalTransactionId'] for m in report['matched']], [capture_id])
        # The authorization is in the app but not in PayPal's report (reporting lag).
        self.assertEqual([a['kind'] for a in report['appOnly']], ['authorization'])
        self.assertTrue(report['appOnly'][0]['possiblyReportingLag'])

    def test_range_is_validated(self):
        response = self.operator.get('/api/reconciliation', {'from': 'yesterday', 'to': 'today'})
        self.assertEqual(response.status_code, 400)
