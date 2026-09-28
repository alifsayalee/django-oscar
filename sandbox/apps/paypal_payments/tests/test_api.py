"""
API tests with PayPal faked at the SDK's transport seam (no network).

Run: cd sandbox && python manage.py test apps.paypal_payments
"""
import json
from datetime import timedelta
from decimal import Decimal as D
from urllib.parse import parse_qs, urlparse

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from oscar.core.loading import get_model
from oscar.test.factories import create_product
from paypal import PaypalClient
from paypal.core import HttpRequest, HttpResponse, OAuthToken

from apps.paypal_payments import gateway
from apps.paypal_payments.models import OrderPayment, PayPalRefund, PayPalWrite, SavedCard

Source = get_model('payment', 'Source')

CARD = {'number': '4111 1111 1111 1111', 'expiry': '2030-12', 'securityCode': '123', 'name': 'Test Shopper',
        'billingAddress': {'line1': '1 Main St', 'city': 'San Jose', 'state': 'CA', 'postalCode': '95131',
                           'countryCode': 'US'}}


# ---------------------------------------------------------------------------
# Fake PayPal
# ---------------------------------------------------------------------------

class StubTransport:
    """The SDK's sync transport protocol: queued answers (or exceptions), every request recorded."""

    def __init__(self):
        self.responses = []
        self.requests = []

    def queue(self, *responses):
        self.responses.extend(responses)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError('unexpected PayPal call: %s %s' % (request.method, request.url))
        answer = self.responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self):
        pass

    def paths(self):
        return ['%s %s' % (r.method, urlparse(r.url).path) for r in self.requests]


class StubTokenSource:
    def fetch(self, credentials):
        return OAuthToken(access_token='t', token_type='Bearer')


def json_response(status, body):
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


def paypal_error(status, issue, description='x'):
    return json_response(status, {'name': 'UNPROCESSABLE_ENTITY', 'message': 'failed', 'debug_id': 'd1',
                                  'details': [{'issue': issue, 'description': description}]})


def money(value):
    return {'currency_code': 'USD', 'value': value}


def order_body(auth_status='CREATED', value='20.00', order_status='COMPLETED', auth_id='AUTH1', created=None):
    created = created or timezone.now()
    return {'id': 'ORDER1', 'status': order_status,
            'payment_source': {'card': {'brand': 'VISA', 'last_digits': '1111'}},
            'purchase_units': [{'payments': {'authorizations': [{
                'id': auth_id, 'status': auth_status, 'amount': money(value),
                'create_time': created.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'expiration_time': (created + timedelta(days=29)).strftime('%Y-%m-%dT%H:%M:%SZ')}]}}]}


def auth_body(status, auth_id='AUTH1', value='20.00'):
    now = timezone.now()
    return {'id': auth_id, 'status': status, 'amount': money(value),
            'create_time': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'expiration_time': (now + timedelta(days=29)).strftime('%Y-%m-%dT%H:%M:%SZ')}


def capture_body(status='COMPLETED', value='20.00', fee='0.88', net='19.12', capture_id='CAP1'):
    return {'id': capture_id, 'status': status, 'amount': money(value),
            'create_time': timezone.now().strftime('%Y-%m-%dT%H:%M:%SZ'),
            'seller_receivable_breakdown': {'gross_amount': money(value), 'paypal_fee': money(fee),
                                            'net_amount': money(net)}}


def refund_body(value, status='COMPLETED', refund_id='REF1'):
    return {'id': refund_id, 'status': status, 'amount': money(value),
            'create_time': timezone.now().strftime('%Y-%m-%dT%H:%M:%SZ')}


def header(request, name):
    return request.headers.get(name.lower())


def body(request):
    return request.body.value


@override_settings(PAYPAL_CURRENCY='USD', PAYPAL_REFERENCE_PREFIX='t', PAYPAL_CLIENT_ID='id',
                   PAYPAL_CLIENT_SECRET='secret', PAYPAL_ENVIRONMENT='sandbox', PAYPAL_BASE_URL='')
class PayPalApiTestCase(TestCase):

    def setUp(self):
        self.transport = StubTransport()
        gateway.set_client(PaypalClient(
            custom_http_client=self.transport, oauth2={'client_id': 'id', 'client_secret': 'secret'},
            oauth2_token_source=StubTokenSource()))
        User = get_user_model()
        self.shopper = User.objects.create_user('shopper', 'shopper@example.com', 'pass12345!')
        self.other = User.objects.create_user('other', 'other@example.com', 'pass12345!')
        self.staff = User.objects.create_user('op', 'op@example.com', 'pass12345!', is_staff=True)
        self.product = create_product(price=D('10.00'), num_in_stock=100)
        self.client.force_login(self.shopper)

    def tearDown(self):
        gateway.set_client(None)

    def as_user(self, user):
        self.client.force_login(user)

    def post(self, url, data=None, **headers):
        return self.client.post(url, json.dumps(data or {}), content_type='application/json', headers=headers)

    def place(self, quantity=2):
        response = self.post('/api/orders', {'items': [{'productId': self.product.pk, 'quantity': quantity}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()['orderId']

    def authorized_order(self):
        order_id = self.place()
        self.transport.queue(json_response(201, order_body()))
        self.assertEqual(self.post('/api/orders/%s/pay' % order_id, {'card': CARD}).status_code, 200)
        return order_id

    def captured_order(self):
        order_id = self.authorized_order()
        self.as_user(self.staff)
        self.transport.queue(json_response(201, capture_body()))
        self.assertEqual(self.post('/api/orders/%s/fulfil' % order_id).status_code, 200)
        return order_id


class PlaceOrderTests(PayPalApiTestCase):

    def test_order_starts_awaiting_payment_with_catalogue_total(self):
        response = self.post('/api/orders', {'items': [{'productId': self.product.pk, 'quantity': 3}]})
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data['payment']['state'], 'awaiting_payment')
        self.assertEqual(data['total'], '30.00')
        self.assertEqual(data['status'], 'Pending')
        self.assertEqual(self.transport.requests, [])

    def test_anonymous_is_401(self):
        self.client.logout()
        self.assertEqual(self.post('/api/orders', {'items': []}).status_code, 401)

    def test_unknown_product_is_rejected(self):
        response = self.post('/api/orders', {'items': [{'productId': 999999, 'quantity': 1}]})
        self.assertEqual(response.status_code, 422)


class PayTests(PayPalApiTestCase):

    def test_authorizes_the_order_total_with_a_derived_request_id(self):
        order_id = self.place()
        self.transport.queue(json_response(201, order_body()))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['payment']['state'], 'authorized')
        req = self.transport.requests[-1]
        self.assertEqual(urlparse(req.url).path, '/v2/checkout/orders')
        number = OrderPayment.objects.get(order_id=order_id).order.number
        self.assertEqual(header(req, 'PayPal-Request-Id'), 't-auth-%s-0' % number)
        sent = body(req)
        self.assertEqual(sent['intent'], 'AUTHORIZE')
        self.assertEqual(sent['purchase_units'][0]['amount'], {'currency_code': 'USD', 'value': '20.00'})
        self.assertEqual(sent['payment_source']['card']['expiry'], '2030-12')
        self.assertEqual(Source.objects.get(order_id=order_id).amount_allocated, D('20.00'))

    def test_the_same_pay_twice_makes_one_provider_call(self):
        order_id = self.place()
        self.transport.queue(json_response(201, order_body()))
        first = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        second = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(first.json()['payment']['authorization']['id'], second.json()['payment']['authorization']['id'])
        self.assertEqual(second.status_code, 200)

    def test_a_pay_while_one_is_in_flight_makes_no_call(self):
        order_id = self.place()
        number = OrderPayment.objects.get(order_id=order_id).order.number
        PayPalWrite.objects.create(ref='t-auth-%s-0' % number, step='authorize')  # claimed, still sending
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.transport.requests, [])

    def test_a_denied_authorization_is_not_done_and_a_retry_is_a_new_attempt(self):
        order_id = self.place()
        self.transport.queue(json_response(201, order_body(auth_status='DENIED')))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()['payment']['state'], 'awaiting_payment')
        self.transport.queue(json_response(201, order_body()))
        self.assertEqual(self.post('/api/orders/%s/pay' % order_id, {'card': CARD}).status_code, 200)
        refs = [header(r, 'PayPal-Request-Id') for r in self.transport.requests]
        self.assertTrue(refs[0].endswith('-0') and refs[1].endswith('-1'), refs)

    def test_a_decline_error_is_402(self):
        order_id = self.place()
        self.transport.queue(paypal_error(422, 'TRANSACTION_REFUSED'))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()['error']['code'], 'payment_declined')

    def test_an_unlisted_status_is_unknown_not_done(self):
        order_id = self.place()
        self.transport.queue(json_response(201, order_body(auth_status='SOMETHING_NEW')))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 504)
        self.assertNotEqual(OrderPayment.objects.get(order_id=order_id).state, 'authorized')

    def test_a_pending_authorization_is_202(self):
        order_id = self.place()
        self.transport.queue(json_response(201, order_body(auth_status='PENDING')))
        self.assertEqual(self.post('/api/orders/%s/pay' % order_id, {'card': CARD}).status_code, 202)
        # a later request re-reads it by id and settles it
        self.transport.queue(json_response(200, auth_body('CREATED')))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.transport.paths()[-1], 'GET /v2/payments/authorizations/AUTH1')

    def test_payer_action_required_is_reported_not_done(self):
        order_id = self.place()
        self.transport.queue(json_response(200, {'id': 'ORDER1', 'status': 'PAYER_ACTION_REQUIRED'}))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()['code'], 'payer_action_required')

    def test_a_different_echoed_amount_needs_review(self):
        order_id = self.place()
        self.transport.queue(json_response(201, order_body(value='19.99')))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['payment']['state'], 'needs_review')

    def test_a_refused_connection_is_known_but_a_read_timeout_is_unknown(self):
        order_id = self.place()
        self.transport.queue(httpx.ConnectError('refused'))
        unsent = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual((unsent.status_code, unsent.json()['error']['outcomeUnknown']), (502, False))

        self.transport.queue(httpx.ReadTimeout('no reply'))
        unknown = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual((unknown.status_code, unknown.json()['error']['outcomeUnknown']), (504, True))

        # the repeat re-checks under the SAME reference, and PayPal's answer settles it
        self.transport.queue(json_response(200, order_body()))
        settled = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(settled.status_code, 200)
        refs = [header(r, 'PayPal-Request-Id') for r in self.transport.requests]
        self.assertEqual(refs[1], refs[2])
        self.assertNotEqual(refs[0], refs[1])  # the never-sent attempt failed definitively

    def test_bad_credentials_are_our_problem_not_the_callers(self):
        gateway.set_client(PaypalClient(custom_http_client=self.transport,
                                        oauth2={'client_id': 'id', 'client_secret': 'bad'}))
        self.transport.queue(json_response(401, {'error': 'invalid_client'}))
        order_id = self.place()
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 502)

    def test_another_shoppers_order_is_404(self):
        order_id = self.place()
        self.as_user(self.other)
        self.assertEqual(self.post('/api/orders/%s/pay' % order_id, {'card': CARD}).status_code, 404)
        self.assertEqual(self.transport.requests, [])

    def test_card_details_are_not_stored(self):
        order_id = self.place()
        self.transport.queue(json_response(201, order_body()))
        self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        for model in (PayPalWrite, OrderPayment):
            for row in model.objects.values():
                self.assertNotIn('4111111111111111', json.dumps(row, default=str))


class FulfilTests(PayPalApiTestCase):

    def test_capture_reports_amount_fee_and_net(self):
        order_id = self.authorized_order()
        self.as_user(self.staff)
        self.transport.queue(json_response(201, capture_body()))
        response = self.post('/api/orders/%s/fulfil' % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        capture = response.json()['payment']['capture']
        self.assertEqual((capture['capturedAmount'], capture['paypalFee'], capture['netAmount']),
                         ('20.00', '0.88', '19.12'))
        self.assertEqual(self.transport.paths()[-1], 'POST /v2/payments/authorizations/AUTH1/capture')
        payment = OrderPayment.objects.get(order_id=order_id)
        self.assertEqual(payment.order.status, 'Complete')
        # a double click on fulfil does not capture twice
        self.post('/api/orders/%s/fulfil' % order_id)
        self.assertEqual(len(self.transport.requests), 2)

    def test_shoppers_cannot_fulfil(self):
        order_id = self.authorized_order()
        self.assertEqual(self.post('/api/orders/%s/fulfil' % order_id).status_code, 403)

    def test_a_stale_authorization_is_renewed_then_captured(self):
        order_id = self.authorized_order()
        OrderPayment.objects.filter(order_id=order_id).update(
            authorized_at=timezone.now() - timedelta(days=5), original_authorized_at=timezone.now() - timedelta(days=5))
        self.as_user(self.staff)
        self.transport.queue(json_response(201, auth_body('CREATED', auth_id='AUTH2')),
                             json_response(201, capture_body()))
        response = self.post('/api/orders/%s/fulfil' % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.transport.paths()[-2:], ['POST /v2/payments/authorizations/AUTH1/reauthorize',
                                                       'POST /v2/payments/authorizations/AUTH2/capture'])

    def test_a_refused_renewal_tells_the_operator_what_to_do(self):
        order_id = self.authorized_order()
        OrderPayment.objects.filter(order_id=order_id).update(authorized_at=timezone.now() - timedelta(days=5))
        self.as_user(self.staff)
        self.transport.queue(paypal_error(422, 'AUTHORIZATION_EXPIRED'))
        response = self.post('/api/orders/%s/fulfil' % order_id)
        self.assertEqual(response.status_code, 409)
        self.assertIn('Cancel this order', response.json()['detail'])
        self.assertEqual(response.json()['code'], 'authorization_not_renewable')

    def test_a_hold_past_29_days_is_not_renewable(self):
        order_id = self.authorized_order()
        old = timezone.now() - timedelta(days=30)
        OrderPayment.objects.filter(order_id=order_id).update(
            authorized_at=old, original_authorized_at=old, authorization_expires_at=None)
        self.as_user(self.staff)
        calls = len(self.transport.requests)
        response = self.post('/api/orders/%s/fulfil' % order_id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'authorization_not_renewable')
        self.assertEqual(len(self.transport.requests), calls)

    def test_a_pending_capture_is_202_not_captured(self):
        order_id = self.authorized_order()
        self.as_user(self.staff)
        self.transport.queue(json_response(201, capture_body(status='PENDING')))
        response = self.post('/api/orders/%s/fulfil' % order_id)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['payment']['state'], 'capture_pending')


class CancelTests(PayPalApiTestCase):

    def test_cancel_voids_the_hold(self):
        order_id = self.authorized_order()
        self.as_user(self.staff)
        self.transport.queue(json_response(200, auth_body('VOIDED')))
        response = self.post('/api/orders/%s/cancel' % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['payment']['state'], 'cancelled')
        self.assertEqual(self.transport.paths()[-1], 'POST /v2/payments/authorizations/AUTH1/void')
        self.assertEqual(OrderPayment.objects.get(order_id=order_id).order.status, 'Cancelled')

    def test_a_refused_void_is_looked_up_and_reports_a_capture(self):
        order_id = self.authorized_order()
        self.as_user(self.staff)
        self.transport.queue(paypal_error(422, 'PREVIOUSLY_CAPTURED'), json_response(200, auth_body('CAPTURED')))
        response = self.post('/api/orders/%s/cancel' % order_id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'already_captured')

    def test_cancel_after_capture_is_refused_without_a_call(self):
        order_id = self.captured_order()
        calls = len(self.transport.requests)
        self.assertEqual(self.post('/api/orders/%s/cancel' % order_id).status_code, 409)
        self.assertEqual(len(self.transport.requests), calls)

    def test_cancel_before_payment_needs_no_paypal_call(self):
        order_id = self.place()
        self.as_user(self.staff)
        self.assertEqual(self.post('/api/orders/%s/cancel' % order_id).status_code, 200)
        self.assertEqual(self.transport.requests, [])


class RefundTests(PayPalApiTestCase):

    def test_partial_refunds_under_distinct_keys_and_a_repeat_under_one(self):
        order_id = self.captured_order()
        self.transport.queue(json_response(201, refund_body('5.00')))
        first = self.post('/api/orders/%s/refunds' % order_id, {'amount': '5.00'}, **{'Idempotency-Key': 'k1'})
        self.assertEqual(first.status_code, 201, first.content)
        self.assertIn('refundId', first.json())
        repeat = self.post('/api/orders/%s/refunds' % order_id, {'amount': '5.00'}, **{'Idempotency-Key': 'k1'})
        self.assertEqual(repeat.json()['refundId'], first.json()['refundId'])
        self.assertEqual(self.transport.paths().count('POST /v2/payments/captures/CAP1/refund'), 1)

        self.transport.queue(json_response(201, refund_body('3.00', refund_id='REF2')))
        second = self.post('/api/orders/%s/refunds' % order_id, {'amount': '3.00'}, **{'Idempotency-Key': 'k2'})
        self.assertEqual(second.status_code, 201)
        payment = second.json()['payment']
        self.assertEqual((payment['state'], payment['refundedAmount'], payment['refundableAmount']),
                         ('partially_refunded', '8.00', '12.00'))
        self.assertEqual(body(self.transport.requests[-1])['amount'], {'currency_code': 'USD', 'value': '3.00'})

    def test_never_beyond_what_was_captured(self):
        order_id = self.captured_order()
        self.transport.queue(json_response(201, refund_body('15.00')))
        self.post('/api/orders/%s/refunds' % order_id, {'amount': '15.00'}, **{'Idempotency-Key': 'a'})
        calls = len(self.transport.requests)
        response = self.post('/api/orders/%s/refunds' % order_id, {'amount': '5.01'}, **{'Idempotency-Key': 'b'})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['refundable'], '5.00')
        self.assertEqual(len(self.transport.requests), calls)

    def test_a_full_refund_by_default(self):
        order_id = self.captured_order()
        self.transport.queue(json_response(201, refund_body('20.00')))
        response = self.post('/api/orders/%s/refunds' % order_id, {}, **{'Idempotency-Key': 'full'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['payment']['state'], 'refunded')

    def test_an_idempotency_key_is_required(self):
        order_id = self.captured_order()
        self.assertEqual(self.post('/api/orders/%s/refunds' % order_id, {'amount': '1.00'}).status_code, 400)

    def test_a_failed_refund_releases_its_reservation(self):
        order_id = self.captured_order()
        self.transport.queue(json_response(201, refund_body('20.00', status='FAILED')))
        self.assertEqual(self.post('/api/orders/%s/refunds' % order_id, {}, **{'Idempotency-Key': 'x'}).status_code, 409)
        self.assertEqual(PayPalRefund.objects.get().outcome, 'failed')
        self.transport.queue(json_response(201, refund_body('20.00', refund_id='REF2')))
        self.assertEqual(self.post('/api/orders/%s/refunds' % order_id, {}, **{'Idempotency-Key': 'y'}).status_code, 201)

    def test_another_shopper_cannot_refund(self):
        order_id = self.captured_order()
        self.as_user(self.other)
        self.assertEqual(self.post('/api/orders/%s/refunds' % order_id, {}, **{'Idempotency-Key': 'z'}).status_code, 404)


class SavedCardTests(PayPalApiTestCase):

    def vault_body(self, token_id='TOK1'):
        return {'id': token_id, 'customer': {'id': 'CUST1'},
                'payment_source': {'card': {'brand': 'VISA', 'last_digits': '1111', 'expiry': '2030-12'}}}

    def save(self, token_id='TOK1'):
        self.transport.queue(json_response(201, self.vault_body(token_id)))
        response = self.post('/api/payment-methods', {'card': CARD})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()['paymentMethodId']

    def test_save_list_and_pay_with_a_saved_card(self):
        method_id = self.save()
        listed = self.client.get('/api/payment-methods').json()['paymentMethods']
        self.assertEqual(listed, [{'paymentMethodId': method_id, 'brand': 'VISA', 'lastDigits': '1111',
                                   'expiry': '2030-12', 'createdAt': listed[0]['createdAt']}])
        self.assertNotIn('4111111111111111', json.dumps(list(SavedCard.objects.values()), default=str))

        order_id = self.place()
        self.transport.queue(json_response(201, order_body()))
        response = self.post('/api/orders/%s/pay' % order_id, {'paymentMethodId': method_id})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(body(self.transport.requests[-1])['payment_source'], {'card': {'vault_id': 'TOK1'}})

    def test_saving_the_same_card_twice_is_one_vault_call(self):
        self.save()
        response = self.post('/api/payment-methods', {'card': CARD})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(self.transport.requests), 1)

    def test_other_shoppers_cannot_see_use_or_delete_it(self):
        method_id = self.save()
        self.as_user(self.other)
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])
        self.assertEqual(self.client.delete('/api/payment-methods/%s' % method_id).status_code, 404)
        order_id = self.place()
        self.assertEqual(self.post('/api/orders/%s/pay' % order_id, {'paymentMethodId': method_id}).status_code, 404)
        self.assertEqual(len(self.transport.requests), 1)

    def test_delete_removes_it_and_it_can_no_longer_pay(self):
        method_id = self.save()
        self.transport.queue(HttpResponse(status_code=204, headers={}))
        response = self.client.delete('/api/payment-methods/%s' % method_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.transport.paths()[-1], 'DELETE /v3/vault/payment-tokens/TOK1')
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])
        order_id = self.place()
        self.assertEqual(self.post('/api/orders/%s/pay' % order_id, {'paymentMethodId': method_id}).status_code, 404)

    def test_a_delete_with_unknown_outcome_keeps_the_card_unusable(self):
        method_id = self.save()
        self.transport.queue(httpx.ReadTimeout('no reply'))
        self.assertEqual(self.client.delete('/api/payment-methods/%s' % method_id).status_code, 504)
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])
        self.transport.queue(HttpResponse(status_code=204, headers={}))
        self.assertEqual(self.client.delete('/api/payment-methods/%s' % method_id).status_code, 200)

    def test_an_invalid_card_is_rejected_before_paypal(self):
        response = self.post('/api/payment-methods', {'card': dict(CARD, number='4111111111111112')})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.transport.requests, [])


class ReconciliationTests(PayPalApiTestCase):

    def search_page(self, txns, page, total_pages):
        return json_response(200, {'transaction_details': [{'transaction_info': t} for t in txns],
                                   'page': page, 'total_pages': total_pages})

    def test_every_window_and_page_is_read_and_lined_up(self):
        order_id = self.captured_order()
        payment = OrderPayment.objects.get(order_id=order_id)
        at = payment.captured_at.strftime('%Y-%m-%dT%H:%M:%SZ')
        start = timezone.now() - timedelta(days=40)
        end = timezone.now() + timedelta(hours=1)
        ours = {'transaction_id': 'CAP1', 'transaction_initiation_date': at, 'transaction_amount': money('20.00')}
        theirs = {'transaction_id': 'STRANGER', 'transaction_initiation_date': at,
                  'transaction_amount': money('7.00')}
        self.transport.queue(self.search_page([], 1, 0),                     # window 1 (31 days), empty
                             self.search_page([theirs], 1, 2),               # window 2, page 1 of 2
                             self.search_page([ours], 2, 2))                 # window 2, page 2 of 2
        self.as_user(self.staff)
        response = self.client.get('/api/reconciliation', {'from': start.isoformat(), 'to': end.isoformat()})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual([m['app']['paypalId'] for m in report['matched']], ['CAP1'])
        self.assertEqual([t['transactionId'] for t in report['paypalOnly']], ['STRANGER'])
        self.assertEqual(report['appOnly'], [])
        searches = [parse_qs(urlparse(r.url).query) for r in self.transport.requests
                    if urlparse(r.url).path == '/v1/reporting/transactions']
        self.assertEqual([s['page'] for s in searches], [['1'], ['1'], ['2']])
        self.assertEqual(searches[0]['end_date'], searches[1]['start_date'])

    def test_an_app_capture_paypal_does_not_list_is_app_only(self):
        self.captured_order()
        self.transport.queue(self.search_page([], 1, 0))
        self.as_user(self.staff)
        start = timezone.now() - timedelta(days=1)
        report = self.client.get('/api/reconciliation', {'from': start.isoformat(),
                                                         'to': (timezone.now() + timedelta(hours=1)).isoformat()}).json()
        self.assertEqual([r['paypalId'] for r in report['appOnly']], ['CAP1'])

    def test_shoppers_cannot_reconcile(self):
        self.assertEqual(self.client.get('/api/reconciliation', {'from': '2026-01-01T00:00:00Z',
                                                                 'to': '2026-01-02T00:00:00Z'}).status_code, 403)


class MyOrdersTests(PayPalApiTestCase):

    def test_lists_only_the_callers_orders_with_payment_state(self):
        order_id = self.authorized_order()
        self.as_user(self.other)
        self.place()
        self.as_user(self.shopper)
        orders = self.client.get('/api/my-orders').json()['orders']
        self.assertEqual([o['orderId'] for o in orders], [order_id])
        self.assertEqual(orders[0]['payment']['state'], 'authorized')
