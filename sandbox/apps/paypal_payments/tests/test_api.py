"""
Tests for the PayPal payments API.

PayPal is faked at the SDK's transport seam (``custom_http_client``), so the
real SDK builds every request and decodes every answer; the tests assert on
the requests it actually sent. Run with:

    cd sandbox && python manage.py test apps.paypal_payments
"""
import json
from datetime import timedelta
from decimal import Decimal as D

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from oscar.test.factories import create_product
from paypal.core import HttpRequest, HttpResponse

from apps.paypal_payments import gateway, services
from apps.paypal_payments.models import OrderPayment, PayPalOperation, PayPalRefund, SavedCard

User = get_user_model()

CARD = {'number': '4111 1111 1111 1111', 'expiry': '2030-12', 'securityCode': '123', 'name': 'Test Shopper',
        'billingAddress': {'addressLine1': '1 Main St', 'city': 'San Jose', 'state': 'CA',
                           'postalCode': '95131', 'countryCode': 'US'}}


# --------------------------------------------------------------------------
# Transport stub
# --------------------------------------------------------------------------

class StubTransport:
    """Satisfies the SDK's sync transport protocol; answers (or raises) from a queue."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests: list[HttpRequest] = []

    def queue(self, *responses):
        self._responses.extend(responses)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError('Unexpected PayPal call: %s %s' % (request.method, request.url))
        answer = self._responses.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def close(self) -> None:
        pass

    def api_requests(self):
        return [r for r in self.requests if not r.url.endswith('/v1/oauth2/token')]


def json_response(status, body):
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


def token_response():
    return json_response(200, {'access_token': 't', 'token_type': 'Bearer', 'expires_in': 3600})


def iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def money(value, currency='USD'):
    return {'currency_code': currency, 'value': value}


def paypal_error(issue, description='refused'):
    return {'name': 'UNPROCESSABLE_ENTITY', 'message': 'semantically incorrect', 'debug_id': 'dbg1',
            'details': [{'issue': issue, 'description': description}]}


def order_body(value='20.00', auth_status='CREATED', order_status='COMPLETED', auth_id='AUTH1', created=None):
    created = created or timezone.now()
    return {
        'id': 'PAYPALORDER1', 'status': order_status,
        'payment_source': {'card': {'brand': 'VISA', 'last_digits': '1111'}},
        'purchase_units': [{'payments': {'authorizations': [{
            'id': auth_id, 'status': auth_status, 'amount': money(value),
            'create_time': iso(created), 'expiration_time': iso(created + timedelta(days=29))}]}}],
    }


def authorization_body(auth_id='AUTH1', status='CREATED', created=None, expires=None, value='20.00'):
    created = created or timezone.now()
    expires = expires or created + timedelta(days=29)
    return {'id': auth_id, 'status': status, 'amount': money(value),
            'create_time': iso(created), 'update_time': iso(timezone.now()), 'expiration_time': iso(expires)}


def capture_body(capture_id='CAP1', status='COMPLETED', value='20.00', fee='0.88', net='19.12'):
    return {'id': capture_id, 'status': status, 'amount': money(value), 'create_time': iso(timezone.now()),
            'seller_receivable_breakdown': {'gross_amount': money(value), 'paypal_fee': money(fee),
                                            'net_amount': money(net)}}


def refund_body(refund_id='RF1', status='COMPLETED', value='5.00'):
    return {'id': refund_id, 'status': status, 'amount': money(value), 'create_time': iso(timezone.now())}


@override_settings(PAYPAL_CLIENT_ID='test-client', PAYPAL_CLIENT_SECRET='test-secret', PAYPAL_CURRENCY='USD',
                   PAYPAL_ENVIRONMENT='sandbox', PAYPAL_BASE_URL=None, PAYPAL_REFERENCE_PREFIX='test',
                   PAYPAL_AUTH_HONOR_PERIOD_DAYS=3)
class ApiTestCase(TestCase):

    def setUp(self):
        self.transport = StubTransport(token_response())
        gateway.set_client(gateway.build_client(custom_http_client=self.transport))
        self.shopper = User.objects.create_user('shopper', 'shopper@example.com', 'x-password-123')
        self.other = User.objects.create_user('other', 'other@example.com', 'x-password-123')
        self.staff = User.objects.create_user('op', 'op@example.com', 'x-password-123', is_staff=True)
        self.product = create_product(price=D('10.00'), num_in_stock=50)

    def tearDown(self):
        gateway.set_client(None)

    # helpers ---------------------------------------------------------------

    def as_user(self, user):
        self.client.force_login(user)

    def post(self, url, body=None, **headers):
        return self.client.post(url, data=json.dumps(body or {}), content_type='application/json',
                                headers=headers)

    def place_order(self, quantity=2):
        self.as_user(self.shopper)
        response = self.post('/api/orders', {'lines': [{'productId': self.product.pk, 'quantity': quantity}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()['orderId']

    def paid_order(self):
        order_id = self.place_order()
        self.transport.queue(json_response(201, order_body()))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 200, response.content)
        return order_id

    def fulfilled_order(self):
        order_id = self.paid_order()
        self.transport.queue(json_response(200, authorization_body()), json_response(201, capture_body()))
        self.as_user(self.staff)
        response = self.post('/api/orders/%s/fulfil' % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.as_user(self.shopper)
        return order_id

    def payment(self, order_id):
        return OrderPayment.objects.get(order__number=order_id)


class OrderAndPayTests(ApiTestCase):

    def test_order_total_comes_from_the_catalogue_in_the_configured_currency(self):
        order_id = self.place_order(quantity=3)
        payment = self.payment(order_id)
        self.assertEqual((payment.amount, payment.currency, payment.state), (D('30.00'), 'USD', 'awaiting_payment'))
        self.assertEqual(payment.order.lines.get().quantity, 3)

    def test_pay_authorizes_exactly_the_order_total_under_a_derived_reference(self):
        order_id = self.paid_order()
        request = self.transport.api_requests()[0]
        self.assertTrue(request.url.endswith('/v2/checkout/orders'))
        body = request.body.value
        self.assertEqual(body['intent'], 'AUTHORIZE')
        self.assertEqual(body['purchase_units'][0]['amount'], {'currency_code': 'USD', 'value': '20.00'})
        self.assertEqual(request.headers['paypal-request-id'], 'test-%s-auth-1' % order_id)
        self.assertEqual(request.headers['prefer'], 'return=representation')
        payment = self.payment(order_id)
        self.assertEqual((payment.state, payment.authorization_id, payment.card_last_digits),
                         ('authorized', 'AUTH1', '1111'))
        # Oscar's own payment models carry the hold.
        source = payment.order.sources.get()
        self.assertEqual(source.amount_allocated, D('20.00'))

    def test_the_same_payment_twice_makes_one_paypal_call(self):
        order_id = self.paid_order()
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.transport.api_requests()), 1)
        self.assertEqual(PayPalOperation.objects.filter(kind='authorize').count(), 1)

    def test_a_denied_authorization_is_not_done_and_the_next_attempt_uses_a_new_reference(self):
        order_id = self.place_order()
        self.transport.queue(json_response(201, order_body(auth_status='DENIED')))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 402)
        payment = self.payment(order_id)
        self.assertEqual((payment.state, payment.attempt), ('awaiting_payment', 2))
        self.transport.queue(json_response(201, order_body()))
        self.assertEqual(self.post('/api/orders/%s/pay' % order_id, {'card': CARD}).status_code, 200)
        self.assertEqual(self.transport.api_requests()[-1].headers['paypal-request-id'],
                         'test-%s-auth-2' % order_id)

    def test_an_unlisted_status_is_unknown_not_done(self):
        order_id = self.place_order()
        self.transport.queue(json_response(201, order_body(auth_status='SOMETHING_NEW')))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.payment(order_id).state, 'authorizing')
        self.assertEqual(PayPalOperation.objects.get().outcome, 'unknown')

    def test_an_echoed_amount_that_differs_needs_review(self):
        order_id = self.place_order()
        self.transport.queue(json_response(201, order_body(value='19.00')))
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'amount_mismatch')
        self.assertEqual(self.payment(order_id).state, 'needs_review')

    def test_a_refused_connection_is_a_known_failure_but_a_read_timeout_is_checked(self):
        order_id = self.place_order()
        self.transport.queue(httpx.ConnectError('refused'))
        unsent = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual((unsent.status_code, unsent.json()['error']['outcome_unknown']), (502, False))
        self.assertFalse(PayPalOperation.objects.exists())  # claim released: nothing happened

        # The write may have landed: the check is a resend under the SAME reference.
        self.transport.queue(httpx.ReadTimeout('no reply'), httpx.ReadTimeout('no reply again'))
        unknown = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual((unknown.status_code, unknown.json()['error']['outcome_unknown']), (504, True))
        sent = [r.headers['paypal-request-id'] for r in self.transport.api_requests()[-2:]]
        self.assertEqual(sent, ['test-%s-auth-2' % order_id] * 2)
        self.assertEqual(PayPalOperation.objects.get().outcome, 'unknown')

        # Repeating the request settles it under the same reference, never a new one.
        self.transport.queue(json_response(200, order_body()))
        settled = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual(settled.status_code, 200)
        self.assertEqual(self.transport.api_requests()[-1].headers['paypal-request-id'],
                         'test-%s-auth-2' % order_id)
        self.assertEqual(self.payment(order_id).state, 'authorized')

    def test_a_card_the_caller_gets_wrong_is_rejected_before_paypal(self):
        order_id = self.place_order()
        response = self.post('/api/orders/%s/pay' % order_id, {'card': dict(CARD, number='1234')})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('1234', response.content.decode())
        self.assertEqual(self.transport.api_requests(), [])

    def test_bad_credentials_are_a_configuration_error(self):
        self.transport = StubTransport(json_response(401, {'error': 'invalid_client'}))
        gateway.set_client(gateway.build_client(custom_http_client=self.transport))
        order_id = self.place_order()
        response = self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual((response.status_code, response.json()['error']['code']), (502, 'paypal_configuration'))
        self.assertEqual(self.payment(order_id).state, 'awaiting_payment')


class AccessTests(ApiTestCase):

    def test_anonymous_callers_are_refused(self):
        self.assertEqual(self.client.get('/api/my-orders').status_code, 401)

    def test_one_shopper_cannot_see_or_act_on_another_shoppers_order(self):
        order_id = self.place_order()
        self.as_user(self.other)
        self.assertEqual(self.post('/api/orders/%s/pay' % order_id, {'card': CARD}).status_code, 404)
        self.assertEqual(self.post('/api/orders/%s/refunds' % order_id, {'amount': '1.00'},
                                   HTTP_IDEMPOTENCY_KEY='k').status_code, 404)
        self.assertEqual(self.client.get('/api/my-orders').json()['orders'], [])

    def test_operator_actions_need_staff(self):
        order_id = self.place_order()
        self.assertEqual(self.post('/api/orders/%s/fulfil' % order_id).status_code, 403)
        self.assertEqual(self.post('/api/orders/%s/cancel' % order_id).status_code, 403)
        self.assertEqual(self.client.get('/api/reconciliation?from=2026-01-01&to=2026-01-02').status_code, 403)


class FulfilTests(ApiTestCase):

    def test_fulfil_captures_and_shows_what_paypal_reported(self):
        order_id = self.fulfilled_order()
        capture_request = self.transport.api_requests()[-1]
        self.assertTrue(capture_request.url.endswith('/v2/payments/authorizations/AUTH1/capture'))
        self.assertEqual(capture_request.body.value['amount'], {'currency_code': 'USD', 'value': '20.00'})
        payment = self.payment(order_id)
        self.assertEqual((payment.state, payment.captured_amount, payment.paypal_fee, payment.net_amount),
                         ('captured', D('20.00'), D('0.88'), D('19.12')))
        self.assertEqual(payment.order.status, 'Complete')
        self.assertEqual(payment.order.sources.get().amount_debited, D('20.00'))

    def test_a_pending_capture_is_settled_by_reading_it_not_by_capturing_again(self):
        order_id = self.paid_order()
        self.transport.queue(json_response(200, authorization_body()),
                             json_response(201, capture_body(status='PENDING')))
        self.as_user(self.staff)
        self.assertEqual(self.post('/api/orders/%s/fulfil' % order_id).status_code, 202)
        self.assertEqual(self.payment(order_id).state, 'capturing')
        # Cancel cannot race it while PayPal is still capturing.
        self.assertEqual(self.post('/api/orders/%s/cancel' % order_id).status_code, 409)
        self.transport.queue(json_response(200, authorization_body(status='CAPTURED')),
                             json_response(200, capture_body()))
        self.assertEqual(self.post('/api/orders/%s/fulfil' % order_id).status_code, 200)
        last = self.transport.api_requests()[-1]
        self.assertEqual((last.method, last.url.endswith('/v2/payments/captures/CAP1')), ('GET', True))
        self.assertEqual(len([r for r in self.transport.api_requests() if r.url.endswith('/capture')]), 1)
        self.assertEqual(self.payment(order_id).state, 'captured')

    def test_a_stale_authorization_is_renewed_before_capture(self):
        order_id = self.paid_order()
        old = timezone.now() - timedelta(days=5)
        OrderPayment.objects.filter(order__number=order_id).update(authorized_at=old)
        self.transport.queue(
            json_response(200, authorization_body(created=old)),
            json_response(201, authorization_body(auth_id='AUTH2')),
            json_response(201, capture_body()))
        self.as_user(self.staff)
        response = self.post('/api/orders/%s/fulfil' % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        urls = [r.url for r in self.transport.api_requests()[-2:]]
        self.assertTrue(urls[0].endswith('/authorizations/AUTH1/reauthorize'))
        self.assertTrue(urls[1].endswith('/authorizations/AUTH2/capture'))
        self.assertEqual(self.payment(order_id).authorization_id, 'AUTH2')

    def test_a_capture_refused_as_stale_is_renewed_then_captured(self):
        order_id = self.paid_order()
        self.transport.queue(
            json_response(200, authorization_body()),
            json_response(422, paypal_error('AUTHORIZATION_EXPIRED')),
            json_response(201, authorization_body(auth_id='AUTH2')),
            json_response(201, capture_body()))
        self.as_user(self.staff)
        self.assertEqual(self.post('/api/orders/%s/fulfil' % order_id).status_code, 200)
        self.assertEqual(self.payment(order_id).state, 'captured')

    def test_an_authorization_that_cannot_be_renewed_says_what_to_do(self):
        order_id = self.paid_order()
        old = timezone.now() - timedelta(days=10)
        OrderPayment.objects.filter(order__number=order_id).update(authorized_at=old)
        self.transport.queue(
            json_response(200, authorization_body(created=old)),
            json_response(422, paypal_error('REAUTHORIZATION_NOT_ALLOWED', 'Reauthorization is not allowed.')))
        self.as_user(self.staff)
        response = self.post('/api/orders/%s/fulfil' % order_id)
        self.assertEqual(response.status_code, 409)
        error = response.json()['error']
        self.assertEqual((error['code'], error['action']),
                         ('authorization_not_renewable', 'cancel_order_and_request_new_payment'))
        self.assertIn('Cancel this order', error['message'])
        self.assertEqual(self.payment(order_id).state, 'authorized')

    def test_an_expired_authorization_is_reported_without_a_write(self):
        order_id = self.paid_order()
        expired = timezone.now() - timedelta(days=1)
        self.transport.queue(json_response(200, authorization_body(created=expired - timedelta(days=29),
                                                                   expires=expired)))
        self.as_user(self.staff)
        response = self.post('/api/orders/%s/fulfil' % order_id)
        self.assertEqual(response.json()['error']['code'], 'authorization_not_renewable')
        self.assertEqual(self.transport.api_requests()[-1].method, 'GET')

    def test_cancel_before_fulfilment_releases_the_hold(self):
        order_id = self.paid_order()
        self.transport.queue(json_response(200, authorization_body(status='VOIDED')))
        self.as_user(self.staff)
        response = self.post('/api/orders/%s/cancel' % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(self.transport.api_requests()[-1].url.endswith('/authorizations/AUTH1/void'))
        payment = self.payment(order_id)
        self.assertEqual((payment.state, payment.order.status), ('voided', 'Cancelled'))
        self.assertEqual(self.post('/api/orders/%s/fulfil' % order_id).status_code, 409)


class RefundTests(ApiTestCase):

    def refund(self, order_id, amount, key):
        body = {} if amount is None else {'amount': amount}
        return self.post('/api/orders/%s/refunds' % order_id, body, **{'Idempotency-Key': key})

    def test_partial_refunds_are_idempotent_per_key_and_capped_at_the_capture(self):
        order_id = self.fulfilled_order()
        self.transport.queue(json_response(201, refund_body('RF1', value='5.00')))
        first = self.refund(order_id, '5.00', 'key-1')
        self.assertEqual(first.status_code, 201, first.content)
        self.assertIn('refundId', first.json())
        # Same key again: the same refund, no second PayPal call.
        calls = len(self.transport.api_requests())
        again = self.refund(order_id, '5.00', 'key-1')
        self.assertEqual((again.status_code, again.json()['refundId']), (200, first.json()['refundId']))
        self.assertEqual(len(self.transport.api_requests()), calls)
        # Same key, different amount: refused.
        self.assertEqual(self.refund(order_id, '6.00', 'key-1').status_code, 422)
        # A second, distinct partial refund is fine; beyond what was captured is not.
        self.assertEqual(self.refund(order_id, '15.01', 'key-2').status_code, 422)
        self.transport.queue(json_response(201, refund_body('RF2', value='15.00')))
        self.assertEqual(self.refund(order_id, '15.00', 'key-3').status_code, 201)
        payment = self.payment(order_id)
        self.assertEqual((payment.state, payment.refunded_amount), ('refunded', D('20.00')))
        self.assertEqual(self.refund(order_id, '0.01', 'key-4').status_code, 409)
        refund_requests = [r for r in self.transport.api_requests() if r.url.endswith('/refund')]
        self.assertEqual(len({r.headers['paypal-request-id'] for r in refund_requests}), 2)

    def test_an_unknown_refund_keeps_its_amount_reserved(self):
        order_id = self.fulfilled_order()
        self.transport.queue(httpx.ReadTimeout('no reply'), httpx.ReadTimeout('no reply'))
        response = self.refund(order_id, '15.00', 'key-1')
        self.assertEqual(response.status_code, 504)
        self.assertEqual(PayPalRefund.objects.get().outcome, 'unknown')
        # 15.00 may have been refunded, so only 5.00 remains refundable.
        self.assertEqual(self.refund(order_id, '6.00', 'key-2').status_code, 422)

    def test_a_refund_without_a_key_is_refused(self):
        order_id = self.fulfilled_order()
        self.assertEqual(self.post('/api/orders/%s/refunds' % order_id, {'amount': '1.00'}).status_code, 400)

    def test_a_refund_before_fulfilment_is_refused(self):
        order_id = self.paid_order()
        self.assertEqual(self.refund(order_id, '1.00', 'k').status_code, 409)


class SavedCardTests(ApiTestCase):

    def token_body(self, token_id='TOK1'):
        return {'id': token_id, 'customer': {'id': 'CUST1'},
                'payment_source': {'card': {'brand': 'VISA', 'last_digits': '1111', 'expiry': '2030-12'}}}

    def test_save_list_pay_and_delete(self):
        self.as_user(self.shopper)
        self.transport.queue(json_response(201, self.token_body()))
        saved = self.post('/api/payment-methods', {'card': CARD}, **{'Idempotency-Key': 'save-1'})
        self.assertEqual(saved.status_code, 201, saved.content)
        method_id = saved.json()['paymentMethodId']
        self.assertEqual((saved.json()['brand'], saved.json()['lastDigits']), ('VISA', '1111'))
        self.assertNotIn('4111111111111111', saved.content.decode())
        # A repeated save under the same key is the same card, with no second PayPal call.
        again = self.post('/api/payment-methods', {'card': CARD}, **{'Idempotency-Key': 'save-1'})
        self.assertEqual((again.status_code, again.json()['paymentMethodId']), (200, method_id))
        self.assertEqual(len(self.transport.api_requests()), 1)
        self.assertEqual(SavedCard.objects.get().paypal_token_id, 'TOK1')

        listed = self.client.get('/api/payment-methods').json()['paymentMethods']
        self.assertEqual([m['paymentMethodId'] for m in listed], [method_id])

        order_id = self.place_order()
        self.transport.queue(json_response(201, order_body()))
        paid = self.post('/api/orders/%s/pay' % order_id, {'paymentMethodId': method_id})
        self.assertEqual(paid.status_code, 200)
        sent = self.transport.api_requests()[-1].body.value['payment_source']['card']
        self.assertEqual(sent, {'vault_id': 'TOK1'})

        # Another shopper can neither see, use nor delete it.
        self.as_user(self.other)
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])
        self.assertEqual(self.client.delete('/api/payment-methods/%s' % method_id).status_code, 404)
        other_order = self.post('/api/orders', {'lines': [{'productId': self.product.pk, 'quantity': 1}]})
        self.assertEqual(self.post('/api/orders/%s/pay' % other_order.json()['orderId'],
                                   {'paymentMethodId': method_id}).status_code, 404)

        self.as_user(self.shopper)
        self.transport.queue(HttpResponse(status_code=204, headers={}))
        self.assertEqual(self.client.delete('/api/payment-methods/%s' % method_id).status_code, 204)
        self.assertTrue(self.transport.api_requests()[-1].url.endswith('/v3/vault/payment-tokens/TOK1'))
        self.assertEqual(self.client.get('/api/payment-methods').json()['paymentMethods'], [])
        next_order = self.place_order()
        self.assertEqual(self.post('/api/orders/%s/pay' % next_order,
                                   {'paymentMethodId': method_id}).status_code, 404)

    def test_card_details_are_never_stored(self):
        self.as_user(self.shopper)
        self.transport.queue(json_response(201, self.token_body()))
        self.post('/api/payment-methods', {'card': CARD})
        card = SavedCard.objects.get()
        stored = ' '.join(str(getattr(card, f.attname)) for f in SavedCard._meta.concrete_fields)
        self.assertNotIn('4111111111111111', stored)
        self.assertNotIn('123', card.last_digits + card.expiry)


class ConfigurationTests(ApiTestCase):

    def test_base_url_override_is_used_for_the_token_and_every_call(self):
        transport = StubTransport(token_response(), json_response(201, order_body()))
        with self.settings(PAYPAL_BASE_URL='https://paypal-proxy.example.test'):
            gateway.set_client(gateway.build_client(custom_http_client=transport))
            order_id = self.place_order()
            self.post('/api/orders/%s/pay' % order_id, {'card': CARD})
        self.assertEqual([r.url.split('/v')[0] for r in transport.requests],
                         ['https://paypal-proxy.example.test'] * 2)

    def test_an_unknown_environment_without_a_base_url_fails_loudly(self):
        from django.core.exceptions import ImproperlyConfigured
        with self.settings(PAYPAL_ENVIRONMENT='production', PAYPAL_BASE_URL=None):
            with self.assertRaises(ImproperlyConfigured):
                gateway.resolve_base_url()

    def test_money_uses_the_currency_exponent(self):
        self.assertEqual(gateway.format_amount(D('10'), 'USD'), '10.00')
        self.assertEqual(gateway.format_amount(D('1000'), 'JPY'), '1000')
        self.assertFalse(gateway.is_representable(D('7.99'), 'JPY'))


class ReconciliationTests(ApiTestCase):

    def search_page(self, records, page, total_pages):
        return json_response(200, {
            'transaction_details': [{'transaction_info': r} for r in records],
            'page': page, 'total_pages': total_pages, 'last_refreshed_datetime': iso(timezone.now())})

    def test_covers_every_page_and_chunk_and_lines_up_both_sides(self):
        order_id = self.fulfilled_order()
        capture_op = PayPalOperation.objects.get(kind='capture')
        when = capture_op.provider_time
        # A local write PayPal does not list, inside the window.
        PayPalOperation.objects.create(
            reference='test-x-refund-1', kind='refund', order_payment=capture_op.order_payment,
            outcome='done', amount=D('2.00'), currency='USD', provider_id='RFMISSING',
            provider_time=when - timedelta(days=2), claimed_at=when - timedelta(days=2))
        ours = {'transaction_id': 'CAP1', 'transaction_event_code': 'T0006',
                'transaction_initiation_date': iso(when), 'transaction_amount': money('20.00'),
                'transaction_status': 'S', 'custom_field': 'test:%s' % order_id}
        theirs = {'transaction_id': 'STRANGER1', 'transaction_event_code': 'T0006',
                  'transaction_initiation_date': iso(when - timedelta(days=1)),
                  'transaction_amount': money('9.99'), 'transaction_status': 'S'}
        outside = {'transaction_id': 'OUTSIDE1', 'transaction_initiation_date': iso(when + timedelta(days=3)),
                   'transaction_amount': money('1.00')}
        start, end = when - timedelta(days=40), when + timedelta(hours=1)
        self.transport.queue(
            self.search_page([theirs], 1, 1),               # first 31-day chunk
            self.search_page([ours], 1, 2),                 # second chunk, page 1 of 2
            self.search_page([outside], 2, 2))              # second chunk, page 2
        self.as_user(self.staff)
        response = self.client.get('/api/reconciliation', {'from': iso(start), 'to': iso(end)})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual(report['paypal']['chunks'], 2)
        self.assertEqual(report['paypal']['pagesFetched'], 3)
        self.assertEqual([m['transactionId'] for m in report['matched']], ['CAP1'])
        self.assertTrue(report['matched'][0]['amountMatches'])
        self.assertEqual([p['transactionId'] for p in report['paypalOnly']], ['STRANGER1'])
        self.assertEqual([p['transactionId'] for p in report['localOnly']], ['RFMISSING'])
        searches = [r for r in self.transport.api_requests() if '/v1/reporting/transactions' in r.url]
        self.assertIn('page=2', searches[-1].url)

    def test_bad_dates_are_refused(self):
        self.as_user(self.staff)
        self.assertEqual(self.client.get('/api/reconciliation', {'from': 'yesterday', 'to': 'x'}).status_code, 400)
        self.assertEqual(self.client.get('/api/reconciliation',
                                         {'from': '2026-02-01T00:00:00Z', 'to': '2026-01-01T00:00:00Z'}).status_code,
                         400)


class ServicesTests(ApiTestCase):

    def test_parse_instant_accepts_an_unencoded_plus(self):
        parsed = services.parse_instant('2026-09-01T00:00:00 00:00', 'from')
        self.assertEqual(parsed.utcoffset(), timedelta(0))
