"""
Tests for the PayPal payments API, with PayPal replaced at the SDK's
transport seam: the real SDK builds and decodes every request, a stub
answers it. Run from ``sandbox/``:  python manage.py test apps.paypal_payments
"""
import json
import re
from datetime import timedelta
from decimal import Decimal

import httpx
from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from oscar.test.factories import create_product
from paypal import PaypalClient
from typing import Any

from paypal.core import HttpRequest, HttpResponse, JsonBody, OAuthToken

from . import client as client_module
from .models import Outcome, PaymentState, PayPalOperation, PayPalPayment
from .writes import invoice_prefix, make_ref, request_id_for

CARD = {
    'number': '4111 1111 1111 1111', 'expiry': '2030-12', 'securityCode': '123', 'name': 'Test Shopper',
    'billingAddress': {'addressLine1': '1 Main St', 'city': 'San Jose', 'state': 'CA',
                       'postalCode': '95131', 'countryCode': 'US'},
}
PAN = '4111111111111111'


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


def sent_json(request: HttpRequest) -> Any:
    """The JSON body the SDK built, as it went on the wire (wire aliases)."""
    assert isinstance(request.body, JsonBody)
    return request.body.value


def paypal_error(status: int, issue: str, description: str = '') -> HttpResponse:
    return json_response(status, {
        'name': 'UNPROCESSABLE_ENTITY', 'message': 'failed business validation', 'debug_id': 'dbg1',
        'details': [{'issue': issue, 'description': description}]})


class StubTransport:
    """Answers queued responses per route; raises queued exceptions."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], list[object]]] = []
        self.requests: list[HttpRequest] = []

    def on(self, method: str, pattern: str, *answers: object) -> None:
        self.routes.append((method, re.compile(pattern), list(answers)))

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = request.url.split('?', 1)[0]
        for method, pattern, answers in self.routes:
            if method == request.method and pattern.search(path) and answers:
                answer = answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                assert isinstance(answer, HttpResponse)
                return answer
        raise AssertionError('unexpected request %s %s' % (request.method, request.url))

    def close(self) -> None:
        pass

    def calls(self, method: str, pattern: str) -> list[HttpRequest]:
        return [r for r in self.requests
                if r.method == method and re.search(pattern, r.url.split('?', 1)[0])]


class StubTokenSource:
    def fetch(self, credentials: object) -> OAuthToken:
        return OAuthToken(access_token='t', token_type='Bearer')


def authorization(auth_id='AUTH1', status='CREATED', value='15.00', created=None, expires=None):
    created = created or timezone.now()
    return {
        'id': auth_id, 'status': status, 'amount': {'currency_code': 'USD', 'value': value},
        'create_time': created.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'expiration_time': (expires or created + timedelta(days=29)).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }


def order_body(status='COMPLETED', auth=None, value='15.00'):
    unit = {'reference_id': 'x', 'amount': {'currency_code': 'USD', 'value': value}}
    if auth is not None:
        unit['payments'] = {'authorizations': [auth]}
    return {'id': 'PPORDER1', 'status': status, 'create_time': '2026-09-25T09:49:26Z', 'purchase_units': [unit]}


def capture_body(capture_id='CAP1', status='COMPLETED', value='15.00', fee='0.97', net='14.03'):
    return {
        'id': capture_id, 'status': status, 'amount': {'currency_code': 'USD', 'value': value},
        'create_time': timezone.now().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'seller_receivable_breakdown': {
            'gross_amount': {'currency_code': 'USD', 'value': value},
            'paypal_fee': {'currency_code': 'USD', 'value': fee},
            'net_amount': {'currency_code': 'USD', 'value': net}},
    }


def refund_body(refund_id='REF1', status='COMPLETED', value='5.00'):
    return {'id': refund_id, 'status': status, 'amount': {'currency_code': 'USD', 'value': value},
            'create_time': timezone.now().strftime('%Y-%m-%dT%H:%M:%SZ')}


CREATE_ORDER = r'/v2/checkout/orders$'
CAPTURE = r'/v2/payments/authorizations/[^/]+/capture$'
REAUTHORIZE = r'/v2/payments/authorizations/[^/]+/reauthorize$'
VOID = r'/v2/payments/authorizations/[^/]+/void$'
REFUND = r'/v2/payments/captures/[^/]+/refund$'
SETUP_TOKEN = r'/v3/vault/setup-tokens$'
PAYMENT_TOKEN = r'/v3/vault/payment-tokens$'
SEARCH = r'/v1/reporting/transactions$'


@override_settings(PAYPAL_CLIENT_ID='id', PAYPAL_CLIENT_SECRET='secret', PAYPAL_ENVIRONMENT='sandbox',
                   PAYPAL_CURRENCY='USD', PAYPAL_BASE_URL='', PAYPAL_REFERENCE_PREFIX='test',
                   PAYPAL_TIMEOUT=5.0, PAYPAL_AUTH_HONOR_PERIOD_DAYS=3)
class PayPalAPITestCase(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        getattr(apps.get_app_config('haystack'), 'signal_processor').teardown()  # keep the search index out of tests

    @classmethod
    def tearDownClass(cls):
        getattr(apps.get_app_config('haystack'), 'signal_processor').setup()
        super().tearDownClass()

    def setUp(self):
        User = get_user_model()
        self.shopper = User.objects.create_user('shopper', 'shopper@example.com', 'pass-word-1')
        self.other = User.objects.create_user('other', 'other@example.com', 'pass-word-1')
        self.staff = User.objects.create_user('ops', 'ops@example.com', 'pass-word-1', is_staff=True)
        self.product = create_product(price=Decimal('15.00'), num_in_stock=10)
        self.transport = StubTransport()
        client_module.set_client(PaypalClient(
            base_url='https://paypal.test', custom_http_client=self.transport,
            oauth2={'client_id': 'id', 'client_secret': 'secret'}, oauth2_token_source=StubTokenSource()))
        self.addCleanup(client_module.set_client, None)

    # -- helpers

    def as_user(self, user):
        self.client.force_login(user)

    def post(self, url, body=None, **headers):
        return self.client.post(url, data=json.dumps(body or {}), content_type='application/json',
                                headers=headers)

    def place_order(self, quantity=1):
        self.as_user(self.shopper)
        response = self.post('/api/orders', {'items': [{'productId': self.product.pk, 'quantity': quantity}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()['orderId']

    def paid_order(self):
        order_id = self.place_order()
        self.transport.on('POST', CREATE_ORDER, json_response(201, order_body(auth=authorization())))
        self.assertEqual(self.post('/api/orders/%s/pay' % order_id, {'card': CARD}).status_code, 200)
        return order_id

    def fulfilled_order(self):
        order_id = self.paid_order()
        self.transport.on('POST', CAPTURE, json_response(201, capture_body()))
        self.as_user(self.staff)
        self.assertEqual(self.post('/api/orders/%s/fulfil' % order_id).status_code, 200)
        return order_id


class OrderTests(PayPalAPITestCase):

    def test_order_starts_awaiting_payment_with_catalogue_amount(self):
        order_id = self.place_order(quantity=2)
        payment = PayPalPayment.objects.get(order__number=order_id)
        self.assertEqual(payment.state, PaymentState.AWAITING_PAYMENT)
        self.assertEqual(payment.amount, Decimal('30.00'))
        self.assertEqual(payment.order.status, 'Pending')
        self.assertEqual(payment.order.lines.get().quantity, 2)

    def test_requires_login(self):
        response = self.post('/api/orders', {'items': [{'productId': self.product.pk}]})
        self.assertEqual(response.status_code, 401)

    def test_unknown_product(self):
        self.as_user(self.shopper)
        response = self.post('/api/orders', {'items': [{'productId': 999999, 'quantity': 1}]})
        self.assertEqual(response.status_code, 400)

    def test_my_orders_are_scoped_to_the_caller(self):
        order_id = self.place_order()
        self.as_user(self.other)
        self.assertEqual(self.client.get('/api/my-orders').json()['orders'], [])
        self.as_user(self.shopper)
        self.assertEqual([o['orderId'] for o in self.client.get('/api/my-orders').json()['orders']], [order_id])


class PayTests(PayPalAPITestCase):

    def test_card_payment_authorizes_the_order_total(self):
        order_id = self.place_order()
        self.transport.on('POST', CREATE_ORDER, json_response(201, order_body(auth=authorization())))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 200, response.content)
        payment = response.json()['payment']
        self.assertEqual(payment['state'], 'authorized')
        self.assertEqual(payment['authorization']['id'], 'AUTH1')

        sent = self.transport.calls('POST', CREATE_ORDER)[0]
        body = sent_json(sent)
        self.assertEqual(body['intent'], 'AUTHORIZE')
        self.assertEqual(body['purchase_units'][0]['amount'], {'currency_code': 'USD', 'value': '15.00'})
        self.assertEqual(body['payment_source']['card']['number'], PAN)
        self.assertEqual(sent.headers['paypal-request-id'], request_id_for(make_ref('pay', order_id, 1)))
        self.assertEqual(sent.headers['prefer'], 'return=representation')
        order = PayPalPayment.objects.get(order__number=order_id).order
        self.assertEqual(order.status, 'Being processed')
        self.assertEqual(order.sources.get().amount_allocated, Decimal('15.00'))

    def test_card_number_is_never_stored(self):
        order_id = self.place_order()
        self.transport.on('POST', CREATE_ORDER, json_response(201, order_body(auth=authorization())))
        self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        for op in PayPalOperation.objects.all():
            self.assertNotIn(PAN, json.dumps(op.inputs) + op.detail + op.ref)

    def test_double_click_authorizes_once(self):
        order_id = self.place_order()
        self.transport.on('POST', CREATE_ORDER, json_response(201, order_body(auth=authorization())))
        first = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        second = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(len(self.transport.calls('POST', CREATE_ORDER)), 1)

    def test_decline_then_retry_uses_a_new_reference(self):
        order_id = self.place_order()
        self.transport.on('POST', CREATE_ORDER, paypal_error(422, 'INSTRUMENT_DECLINED', 'declined'),
                          json_response(201, order_body(auth=authorization())))
        declined = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(declined.status_code, 422)
        self.assertEqual(declined.json()['error']['paypal']['issues'][0]['issue'], 'INSTRUMENT_DECLINED')
        self.assertEqual(PayPalPayment.objects.get(order__number=order_id).state, PaymentState.AUTHORIZATION_FAILED)
        retried = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(retried.status_code, 200)
        ids = [r.headers['paypal-request-id'] for r in self.transport.calls('POST', CREATE_ORDER)]
        self.assertEqual(len(set(ids)), 2)

    def test_timeout_is_checked_by_resending_the_same_reference(self):
        order_id = self.place_order()
        self.transport.on('POST', CREATE_ORDER, httpx.ReadTimeout('no reply'),
                          json_response(201, order_body(auth=authorization())))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 200, response.content)
        ids = [r.headers['paypal-request-id'] for r in self.transport.calls('POST', CREATE_ORDER)]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(PayPalOperation.objects.filter(kind='create_order').count(), 1)

    def test_unsent_is_not_the_same_failure_as_unknown(self):
        order_id = self.place_order()
        self.transport.on('POST', CREATE_ORDER, httpx.ConnectError('refused'))
        unsent = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(unsent.status_code, 502)
        self.assertNotIn('outcomeUnknown', unsent.json()['error'])

        other_id = self.place_order()
        self.transport.on('POST', CREATE_ORDER, httpx.ReadTimeout('no reply'), httpx.ReadTimeout('no reply'))
        unknown = self.post('/api/orders/%s/pay' % other_id, {'card': CARD})
        self.assertEqual(unknown.status_code, 504)
        self.assertTrue(unknown.json()['error']['outcomeUnknown'])
        op = PayPalOperation.objects.get(payment__order__number=other_id)
        self.assertEqual(op.outcome, Outcome.UNKNOWN)

        # The caller's repeat checks under the SAME reference, never a new one.
        self.transport.on('POST', CREATE_ORDER, json_response(201, order_body(auth=authorization())))
        settled = self.post('/api/orders/%s/pay' % other_id, {'card': CARD})
        self.assertEqual(settled.status_code, 200)
        ids = {r.headers['paypal-request-id'] for r in self.transport.calls('POST', CREATE_ORDER)[1:]}
        self.assertEqual(ids, {op.request_id})

    def test_pending_hold_is_not_success(self):
        order_id = self.place_order()
        self.transport.on('POST', CREATE_ORDER,
                          json_response(201, order_body(auth=authorization(status='PENDING'))))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['payment']['state'], 'authorizing')

    def test_unlisted_status_is_unknown_not_done(self):
        order_id = self.place_order()
        self.transport.on('POST', CREATE_ORDER,
                          json_response(201, order_body(auth=authorization(status='SOMETHING_NEW'))))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(PayPalOperation.objects.get().outcome, Outcome.UNKNOWN)

    def test_denied_hold_is_a_failure(self):
        order_id = self.place_order()
        self.transport.on('POST', CREATE_ORDER, json_response(201, order_body(auth=authorization(status='DENIED'))))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(PayPalPayment.objects.get().state, PaymentState.AUTHORIZATION_FAILED)

    def test_amount_mismatch_needs_review(self):
        order_id = self.place_order()
        self.transport.on('POST', CREATE_ORDER,
                          json_response(201, order_body(auth=authorization(value='14.00'))))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(PayPalPayment.objects.get().state, PaymentState.NEEDS_REVIEW)

    def test_approved_order_runs_the_authorize_step(self):
        order_id = self.place_order()
        self.transport.on('POST', CREATE_ORDER, json_response(201, order_body(status='APPROVED')))
        self.transport.on('POST', r'/authorize$', json_response(201, order_body(auth=authorization())))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['payment']['state'], 'authorized')

    def test_payer_action_required_is_reported_not_approved(self):
        order_id = self.place_order()
        self.transport.on('POST', CREATE_ORDER, json_response(201, order_body(status='PAYER_ACTION_REQUIRED')))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['code'], 'payer_action_required')

    def test_another_shopper_cannot_pay_my_order(self):
        order_id = self.place_order()
        self.as_user(self.other)
        self.assertEqual(self.post('/api/orders/%s/pay' % order_id, {'card': CARD}).status_code, 404)
        self.assertEqual(self.transport.requests, [])

    def test_bad_credentials_are_a_configuration_error(self):
        order_id = self.place_order()
        client_module.set_client(PaypalClient(
            base_url='https://paypal.test', custom_http_client=self.transport,
            oauth2={'client_id': 'id', 'client_secret': 'wrong'}))
        self.transport.on('POST', r'/v1/oauth2/token$', json_response(401, {'error': 'invalid_client'}))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['error']['code'], 'paypal_configuration_error')
        self.assertEqual(self.transport.calls('POST', CREATE_ORDER), [])


class FulfilCancelTests(PayPalAPITestCase):

    def test_shopper_cannot_fulfil(self):
        order_id = self.paid_order()
        self.assertEqual(self.post('/api/orders/%s/fulfil' % order_id).status_code, 403)

    def test_fulfil_captures_and_records_fee_and_net(self):
        order_id = self.paid_order()
        self.transport.on('POST', CAPTURE, json_response(201, capture_body()))
        self.as_user(self.staff)
        response = self.post('/api/orders/%s/fulfil' % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        capture = response.json()['payment']['capture']
        self.assertEqual((capture['amount'], capture['paypalFee'], capture['netAmount']), ('15.00', '0.97', '14.03'))
        self.assertEqual(response.json()['status'], 'Complete')
        self.assertIn('/authorizations/AUTH1/capture', self.transport.calls('POST', CAPTURE)[0].url)
        again = self.post('/api/orders/%s/fulfil' % order_id)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(self.transport.calls('POST', CAPTURE)), 1)

    def test_stale_authorization_is_renewed_before_capture(self):
        order_id = self.paid_order()
        PayPalPayment.objects.update(authorized_at=timezone.now() - timedelta(days=5))
        self.transport.on('POST', REAUTHORIZE, json_response(201, authorization(auth_id='AUTH2')))
        self.transport.on('POST', CAPTURE, json_response(201, capture_body()))
        self.as_user(self.staff)
        response = self.post('/api/orders/%s/fulfil' % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn('/authorizations/AUTH1/reauthorize', self.transport.calls('POST', REAUTHORIZE)[0].url)
        self.assertIn('/authorizations/AUTH2/capture', self.transport.calls('POST', CAPTURE)[0].url)
        self.assertEqual(response.json()['payment']['authorization']['reauthorizations'], 1)

    def test_unrenewable_authorization_tells_the_operator_what_to_do(self):
        order_id = self.paid_order()
        PayPalPayment.objects.update(authorized_at=timezone.now() - timedelta(days=5))
        self.transport.on('POST', REAUTHORIZE, paypal_error(422, 'REAUTHORIZATION_NOT_ALLOWED', 'not allowed'))
        self.as_user(self.staff)
        response = self.post('/api/orders/%s/fulfil' % order_id)
        self.assertEqual(response.status_code, 409)
        error = response.json()['error']
        self.assertEqual(error['code'], 'authorization_not_renewable')
        self.assertIn('cancel this order', error['message'])
        self.assertEqual(self.transport.calls('POST', CAPTURE), [])

    def test_too_soon_to_reauthorize_captures_the_original(self):
        order_id = self.paid_order()
        PayPalPayment.objects.update(authorized_at=timezone.now() - timedelta(days=5))
        self.transport.on('POST', REAUTHORIZE, paypal_error(422, 'REAUTHORIZATION_TOO_SOON'))
        self.transport.on('POST', CAPTURE, json_response(201, capture_body()))
        self.as_user(self.staff)
        self.assertEqual(self.post('/api/orders/%s/fulfil' % order_id).status_code, 200)
        self.assertIn('/authorizations/AUTH1/capture', self.transport.calls('POST', CAPTURE)[0].url)

    def test_expired_authorization_is_not_captured(self):
        order_id = self.paid_order()
        PayPalPayment.objects.update(authorization_expires_at=timezone.now() - timedelta(minutes=1))
        self.as_user(self.staff)
        response = self.post('/api/orders/%s/fulfil' % order_id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'authorization_expired')

    def test_pending_capture_does_not_complete_the_order(self):
        order_id = self.paid_order()
        self.transport.on('POST', CAPTURE, json_response(201, capture_body(status='PENDING')))
        self.as_user(self.staff)
        response = self.post('/api/orders/%s/fulfil' % order_id)
        self.assertEqual(response.status_code, 202)
        self.assertNotEqual(response.json()['status'], 'Complete')

    def test_cancel_voids_the_hold(self):
        order_id = self.paid_order()
        self.transport.on('POST', VOID, json_response(200, authorization(status='VOIDED')))
        self.as_user(self.staff)
        response = self.post('/api/orders/%s/cancel' % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['payment']['state'], 'voided')
        self.assertEqual(response.json()['status'], 'Cancelled')
        self.assertEqual(self.post('/api/orders/%s/cancel' % order_id).status_code, 200)
        self.assertEqual(len(self.transport.calls('POST', VOID)), 1)

    def test_cannot_cancel_after_fulfilment(self):
        order_id = self.fulfilled_order()
        self.assertEqual(self.post('/api/orders/%s/cancel' % order_id).status_code, 409)


class RefundTests(PayPalAPITestCase):

    def test_refund_requires_an_idempotency_key(self):
        order_id = self.fulfilled_order()
        self.assertEqual(self.post('/api/orders/%s/refunds' % order_id, {'amount': '5.00'}).status_code, 400)

    def test_same_key_refunds_once_and_distinct_keys_refund_twice(self):
        order_id = self.fulfilled_order()
        self.transport.on('POST', REFUND, json_response(201, refund_body('REF1')),
                          json_response(201, refund_body('REF2')))
        url = '/api/orders/%s/refunds' % order_id
        first = self.post(url, {'amount': '5.00'}, **{'Idempotency-Key': 'k1'})
        repeat = self.post(url, {'amount': '5.00'}, **{'Idempotency-Key': 'k1'})
        second = self.post(url, {'amount': '5.00'}, **{'Idempotency-Key': 'k2'})
        self.assertEqual((first.status_code, repeat.status_code, second.status_code), (201, 201, 201))
        self.assertEqual(first.json()['refundId'], repeat.json()['refundId'])
        self.assertNotEqual(first.json()['refundId'], second.json()['refundId'])
        self.assertEqual(len(self.transport.calls('POST', REFUND)), 2)
        payment = second.json()['order']['payment']
        self.assertEqual((payment['refundedAmount'], payment['refundableAmount'], payment['state']),
                         ('10.00', '5.00', 'partially_refunded'))

    def test_refund_never_exceeds_the_capture(self):
        order_id = self.fulfilled_order()
        self.transport.on('POST', REFUND, json_response(201, refund_body('REF1', value='10.00')))
        url = '/api/orders/%s/refunds' % order_id
        self.assertEqual(self.post(url, {'amount': '10.00'}, **{'Idempotency-Key': 'a'}).status_code, 201)
        over = self.post(url, {'amount': '5.01'}, **{'Idempotency-Key': 'b'})
        self.assertEqual(over.status_code, 422)
        self.assertEqual(over.json()['error']['code'], 'refund_exceeds_captured')
        self.assertEqual(len(self.transport.calls('POST', REFUND)), 1)

    def test_key_reused_for_a_different_amount_is_rejected(self):
        order_id = self.fulfilled_order()
        self.transport.on('POST', REFUND, json_response(201, refund_body('REF1')))
        url = '/api/orders/%s/refunds' % order_id
        self.post(url, {'amount': '5.00'}, **{'Idempotency-Key': 'k'})
        self.assertEqual(self.post(url, {'amount': '6.00'}, **{'Idempotency-Key': 'k'}).status_code, 409)

    def test_full_refund_by_default(self):
        order_id = self.fulfilled_order()
        self.transport.on('POST', REFUND, json_response(201, refund_body('REF1', value='15.00')))
        response = self.post('/api/orders/%s/refunds' % order_id, {}, **{'Idempotency-Key': 'full'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['order']['payment']['state'], 'refunded')

    def test_failed_refund_releases_its_reservation(self):
        order_id = self.fulfilled_order()
        self.transport.on('POST', REFUND, json_response(201, refund_body('REF1', status='FAILED', value='15.00')),
                          json_response(201, refund_body('REF2', value='15.00')))
        url = '/api/orders/%s/refunds' % order_id
        self.assertEqual(self.post(url, {}, **{'Idempotency-Key': 'x'}).status_code, 422)
        self.assertEqual(self.post(url, {}, **{'Idempotency-Key': 'y'}).status_code, 201)


class SavedCardTests(PayPalAPITestCase):

    def save_card(self, user, key='card-1'):
        self.as_user(user)
        self.transport.on('POST', SETUP_TOKEN, json_response(201, {
            'id': 'SETUP1', 'status': 'APPROVED', 'customer': {'id': 'CUST1'}}))
        self.transport.on('POST', PAYMENT_TOKEN, json_response(201, {
            'id': 'TOKEN1', 'customer': {'id': 'CUST1'},
            'payment_source': {'card': {'name': 'Test Shopper', 'last_digits': '1111', 'brand': 'VISA',
                                        'expiry': '2030-12'}}}))
        return self.post('/api/payment-methods', {'card': CARD}, **{'Idempotency-Key': key})

    def test_save_card_returns_a_safe_description(self):
        response = self.save_card(self.shopper)
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertIn('paymentMethodId', body)
        self.assertEqual((body['brand'], body['last4'], body['expiry']), ('VISA', '1111', '2030-12'))
        self.assertNotIn(PAN, json.dumps(body))
        token_request = sent_json(self.transport.calls('POST', PAYMENT_TOKEN)[0])
        self.assertEqual(token_request['payment_source']['token'], {'id': 'SETUP1', 'type': 'SETUP_TOKEN'})
        listed = self.client.get('/api/payment-methods').json()['paymentMethods']
        self.assertEqual([c['paymentMethodId'] for c in listed], [body['paymentMethodId']])

    def test_saving_twice_with_one_key_vaults_once(self):
        first = self.save_card(self.shopper)
        second = self.post('/api/payment-methods', {'card': CARD}, **{'Idempotency-Key': 'card-1'})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()['paymentMethodId'], second.json()['paymentMethodId'])
        self.assertEqual(len(self.transport.calls('POST', SETUP_TOKEN)), 1)

    def test_saved_card_pays_a_later_order(self):
        card_id = self.save_card(self.shopper).json()['paymentMethodId']
        order_id = self.place_order()
        self.transport.on('POST', CREATE_ORDER, json_response(201, order_body(auth=authorization())))
        response = self.post('/api/orders/%s/pay' % order_id, {'paymentMethodId': card_id})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(sent_json(self.transport.calls('POST', CREATE_ORDER)[0])['payment_source'],
                         {'card': {'vault_id': 'TOKEN1'}})

    def test_cards_are_private_to_their_owner(self):
        card_id = self.save_card(self.shopper).json()['paymentMethodId']
        self.as_user(self.other)
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])
        self.assertEqual(self.client.delete('/api/payment-methods/%s' % card_id).status_code, 404)
        self.as_user(self.other)
        response = self.post('/api/orders', {'items': [{'productId': self.product.pk}]})
        order_id = response.json()['orderId']
        self.assertEqual(self.post('/api/orders/%s/pay' % order_id, {'paymentMethodId': card_id}).status_code, 404)

    def test_deleted_card_is_gone_and_cannot_pay(self):
        card_id = self.save_card(self.shopper).json()['paymentMethodId']
        self.transport.on('DELETE', r'/v3/vault/payment-tokens/TOKEN1$', HttpResponse(status_code=204, headers={}))
        self.assertEqual(self.client.delete('/api/payment-methods/%s' % card_id).status_code, 204)
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])
        order_id = self.place_order()
        self.assertEqual(self.post('/api/orders/%s/pay' % order_id, {'paymentMethodId': card_id}).status_code, 404)

    def test_card_is_hidden_even_when_paypal_delete_fails(self):
        card_id = self.save_card(self.shopper).json()['paymentMethodId']
        self.transport.on('DELETE', r'/v3/vault/payment-tokens/TOKEN1$', httpx.ReadTimeout('no reply'),
                          HttpResponse(status_code=204, headers={}))
        self.assertEqual(self.client.delete('/api/payment-methods/%s' % card_id).status_code, 202)
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])
        self.assertEqual(self.client.delete('/api/payment-methods/%s' % card_id).status_code, 204)


class ReconciliationTests(PayPalAPITestCase):

    def captured_at(self):
        when = PayPalOperation.objects.get(kind='capture').provider_time
        assert when is not None
        return when

    def txn(self, txn_id, value, when, invoice=None):
        info = {
            'transaction_id': txn_id, 'transaction_event_code': 'T0006',
            'transaction_initiation_date': when.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'transaction_amount': {'currency_code': 'USD', 'value': value}, 'transaction_status': 'S'}
        if invoice:
            info['invoice_id'] = invoice
        return {'transaction_info': info}

    def test_staff_only(self):
        self.as_user(self.shopper)
        self.assertEqual(self.client.get('/api/reconciliation?from=2026-01-01T00:00:00Z&to=2026-01-02T00:00:00Z')
                         .status_code, 403)

    def test_reads_every_page_of_every_window_and_lines_up_both_sides(self):
        self.fulfilled_order()
        captured_at = self.captured_at()
        start, end = captured_at - timedelta(days=40), captured_at + timedelta(hours=1)
        page1 = {'transaction_details': [self.txn('OTHER1', '9.99', start + timedelta(days=1))],
                 'total_pages': 1, 'page': 1}
        page2a = {'transaction_details': [self.txn('CAP1', '15.00', captured_at)], 'total_pages': 2, 'page': 1}
        page2b = {'transaction_details': [self.txn('FOREIGN', '1.00', captured_at, invoice=invoice_prefix() + '9-1')],
                  'total_pages': 2, 'page': 2}
        self.transport.on('GET', SEARCH, json_response(200, page1), json_response(200, page2a),
                          json_response(200, page2b))
        self.as_user(self.staff)
        response = self.client.get('/api/reconciliation', {'from': start.isoformat(), 'to': end.isoformat()})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual(len(self.transport.calls('GET', SEARCH)), 3)  # two windows, three pages
        self.assertEqual([m['paypal']['transactionId'] for m in report['matched']], ['CAP1'])
        self.assertEqual(sorted(r['transactionId'] for r in report['paypalOnly']), ['FOREIGN', 'OTHER1'])
        self.assertEqual([r['ours'] for r in report['paypalOnly'] if r['transactionId'] == 'FOREIGN'], [True])
        self.assertEqual(report['appOnly'], [])

    def test_app_only_capture_is_visible(self):
        self.fulfilled_order()
        captured_at = self.captured_at()
        self.transport.on('GET', SEARCH, json_response(200, {'transaction_details': [], 'total_pages': 0}))
        self.as_user(self.staff)
        report = self.client.get('/api/reconciliation', {
            'from': (captured_at - timedelta(hours=1)).isoformat(),
            'to': (captured_at + timedelta(hours=1)).isoformat()}).json()
        self.assertEqual([r['transactionId'] for r in report['appOnly']], ['CAP1'])
        self.assertEqual(report['appOnly'][0]['reason'], 'not_yet_reported')

    def test_window_paypal_has_not_reported_yet_is_not_an_error(self):
        self.fulfilled_order()
        captured_at = self.captured_at()
        self.transport.on('GET', SEARCH, json_response(404, {
            'name': 'INVALID_REQUEST', 'message': 'Data for the given start date is not available.',
            'debug_id': 'd', 'details': [], 'links': []}))
        self.as_user(self.staff)
        response = self.client.get('/api/reconciliation', {
            'from': (captured_at - timedelta(hours=1)).isoformat(),
            'to': (captured_at + timedelta(hours=1)).isoformat()})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual(len(report['paypalNotYetAvailable']), 1)
        self.assertEqual([r['reason'] for r in report['appOnly']], ['not_yet_reported'])

    def test_other_search_failures_are_reported(self):
        self.transport.on('GET', SEARCH, json_response(400, {'name': 'INVALID_REQUEST', 'message': 'bad'}))
        self.as_user(self.staff)
        response = self.client.get('/api/reconciliation?from=2026-01-01T00:00:00Z&to=2026-01-02T00:00:00Z')
        self.assertEqual(response.status_code, 422)

    def test_invalid_range(self):
        self.as_user(self.staff)
        self.assertEqual(self.client.get('/api/reconciliation?from=nope&to=2026-01-01T00:00:00Z').status_code, 400)
