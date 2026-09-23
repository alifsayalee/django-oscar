"""Tests for the PayPal payments + saved-cards API.

Service-layer tests use the SDK's transport seam (a fake ``custom_http_client``)
so the real request-building/decoding pipeline runs with no network. View-level
tests replace the service with an in-memory fake to exercise auth, shopper
scoping, idempotency, refund caps and status transitions deterministically.
"""

import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from paypal import PaypalClient
from paypal.core import ApiError, HttpResponse, RawError
from paypal.models import Error

from . import views
from .exceptions import (
    PayPalConfigError,
    PayPalFailure,
    PayPalRejected,
    PayPalUnreadable,
)
from .models import PayPalPayment, RefundRecord, SavedPaymentMethod
from .paypal_service import PayPalService

User = get_user_model()


# ---------------------------------------------------------------------------
# SDK transport stub (per python-testing)
# ---------------------------------------------------------------------------

class StubTransport:
    """Satisfies the SDK's sync transport protocol: send() + close()."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return self._responses.pop(0)

    def close(self):
        pass

    @property
    def last_request(self):
        return self.requests[-1] if self.requests else None


def json_response(status, body):
    return HttpResponse(
        status_code=status,
        headers={'content-type': 'application/json'},
        content=json.dumps(body).encode(),
    )


def token_response():
    return json_response(200, {
        'access_token': 't', 'token_type': 'Bearer', 'expires_in': 3600})


def service_with(*responses):
    transport = StubTransport(*responses)
    client = PaypalClient(
        custom_http_client=transport,
        oauth2={'client_id': 'id', 'client_secret': 'secret'},
        base_url='https://api-m.sandbox.paypal.com',
    )
    return PayPalService(client=client), transport


class ServiceCreateOrderTests(TestCase):
    def test_create_order_returns_id_and_sends_authorize_intent(self):
        service, transport = service_with(
            token_response(),
            json_response(201, {'id': 'ORDER-1', 'status': 'CREATED'}),
        )
        order_id = service.create_order(
            amount=Decimal('42.00'), currency='USD',
            invoice_id='100001', description='x', request_id='req-1')
        self.assertEqual(order_id, 'ORDER-1')
        req = transport.last_request
        self.assertEqual(req.method, 'POST')
        self.assertTrue(req.url.endswith('/v2/checkout/orders'))
        body = req.body.value
        self.assertEqual(body['intent'], 'AUTHORIZE')
        self.assertEqual(body['purchase_units'][0]['amount']['value'], '42.00')
        self.assertEqual(body['purchase_units'][0]['amount']['currency_code'], 'USD')
        self.assertEqual(body['purchase_units'][0]['invoice_id'], '100001')
        self.assertEqual(req.headers['paypal-request-id'], 'req-1')

    def test_missing_id_on_success_is_unreadable(self):
        service, _ = service_with(token_response(), json_response(201, {}))
        with self.assertRaises(PayPalUnreadable):
            service.create_order(
                amount=Decimal('1.00'), currency='USD',
                invoice_id='1', description='', request_id='r')

    def test_typed_error_becomes_rejected(self):
        service, _ = service_with(
            token_response(),
            json_response(422, {
                'name': 'UNPROCESSABLE_ENTITY',
                'message': 'bad amount',
                'debug_id': 'dbg-1'}),
        )
        with self.assertRaises(PayPalRejected) as ctx:
            service.create_order(
                amount=Decimal('1.00'), currency='USD',
                invoice_id='1', description='', request_id='r')
        self.assertEqual(ctx.exception.http_status, 422)
        self.assertEqual(ctx.exception.debug_id, 'dbg-1')

    def test_unmapped_status_becomes_failure(self):
        service, _ = service_with(
            token_response(), json_response(418, {'weird': 1}))
        with self.assertRaises(PayPalFailure):
            service.create_order(
                amount=Decimal('1.00'), currency='USD',
                invoice_id='1', description='', request_id='r')

    def test_bad_credentials_is_config_error(self):
        # No token_response(): the token fetch itself returns 401.
        service, _ = service_with(json_response(401, {'error': 'invalid_client'}))
        with self.assertRaises(PayPalConfigError):
            service.create_order(
                amount=Decimal('1.00'), currency='USD',
                invoice_id='1', description='', request_id='r')


class ServiceAuthorizeCaptureRefundTests(TestCase):
    def test_authorize_extracts_authorization(self):
        service, transport = service_with(
            token_response(),
            json_response(201, {
                'id': 'ORDER-1',
                'status': 'COMPLETED',
                'purchase_units': [{
                    'payments': {
                        'authorizations': [{
                            'id': 'AUTH-1',
                            'status': 'CREATED',
                            'expiration_time': '2030-01-01T00:00:00Z',
                        }],
                    },
                }],
            }),
        )
        result = service.authorize_order(
            paypal_order_id='ORDER-1',
            payment_source={'card': {'vault_id': 'tok'}},
            request_id='req-2')
        self.assertEqual(result['authorization_id'], 'AUTH-1')
        self.assertEqual(result['status'], 'CREATED')
        self.assertIsNotNone(result['expiry'])
        self.assertTrue(transport.last_request.url.endswith(
            '/v2/checkout/orders/ORDER-1/authorize'))

    def test_capture_extracts_fee_breakdown(self):
        service, _ = service_with(
            token_response(),
            json_response(201, {
                'id': 'CAP-1',
                'status': 'COMPLETED',
                'final_capture': True,
                'seller_receivable_breakdown': {
                    'gross_amount': {'currency_code': 'USD', 'value': '42.00'},
                    'paypal_fee': {'currency_code': 'USD', 'value': '1.62'},
                    'net_amount': {'currency_code': 'USD', 'value': '40.38'},
                },
            }),
        )
        result = service.capture(
            authorization_id='AUTH-1', currency='USD', request_id='cap-req')
        self.assertEqual(result['capture_id'], 'CAP-1')
        self.assertEqual(result['gross_amount'], Decimal('42.00'))
        self.assertEqual(result['paypal_fee'], Decimal('1.62'))
        self.assertEqual(result['net_amount'], Decimal('40.38'))

    def test_refund_partial_sends_amount(self):
        service, transport = service_with(
            token_response(),
            json_response(201, {
                'id': 'REF-1', 'status': 'COMPLETED',
                'amount': {'currency_code': 'USD', 'value': '10.00'}}),
        )
        result = service.refund(
            capture_id='CAP-1', amount=Decimal('10.00'),
            currency='USD', request_id='key-1')
        self.assertEqual(result['refund_id'], 'REF-1')
        self.assertEqual(result['amount'], Decimal('10.00'))
        body = transport.last_request.body.value
        self.assertEqual(body['amount']['value'], '10.00')
        self.assertEqual(transport.last_request.headers['paypal-request-id'], 'key-1')

    def test_delete_vault_token_success(self):
        service, _ = service_with(
            token_response(), HttpResponse(status_code=204, headers={}))
        self.assertTrue(service.delete_vault_token('tok-1'))

    def test_delete_vault_token_forbidden_rejected(self):
        # 403 is a mapped (typed) status for delete_payment_token; it must
        # surface as a typed rejection rather than an opaque failure.
        service, _ = service_with(
            token_response(),
            json_response(403, {
                'name': 'NOT_AUTHORIZED', 'message': 'no', 'debug_id': 'd'}),
        )
        with self.assertRaises(PayPalRejected):
            service.delete_vault_token('missing')


class ServiceReconciliationTests(TestCase):
    def test_search_walks_all_pages(self):
        page1 = json_response(200, {
            'page': 1, 'total_pages': 2, 'total_items': 2,
            'transaction_details': [{
                'transaction_info': {
                    'transaction_id': 'T1', 'invoice_id': '100001',
                    'transaction_status': 'S',
                    'transaction_amount': {'currency_code': 'USD', 'value': '42.00'},
                }}]})
        page2 = json_response(200, {
            'page': 2, 'total_pages': 2, 'total_items': 2,
            'transaction_details': [{
                'transaction_info': {
                    'transaction_id': 'T2', 'invoice_id': '100002',
                    'transaction_status': 'S',
                    'transaction_amount': {'currency_code': 'USD', 'value': '7.00'},
                }}]})
        service, transport = service_with(token_response(), page1, page2)
        records = service.search_transactions(
            start_date='2024-01-01T00:00:00-0000',
            end_date='2024-01-15T00:00:00-0000')
        self.assertEqual([r['transaction_id'] for r in records], ['T1', 'T2'])
        # token + 2 pages
        self.assertEqual(len(transport.requests), 3)


# ---------------------------------------------------------------------------
# View-level tests with a fake service
# ---------------------------------------------------------------------------

class FakePayPalService:
    """In-memory stand-in for PayPalService used in view tests."""

    def __init__(self, client=None):
        self.calls = []

    def create_order(self, **kw):
        self.calls.append(('create_order', kw))
        return 'PP-ORDER'

    def authorize_order(self, **kw):
        self.calls.append(('authorize_order', kw))
        return {'authorization_id': 'PP-AUTH', 'status': 'CREATED', 'expiry': None}

    def create_authorized_order(self, **kw):
        self.calls.append(('create_authorized_order', kw))
        return {'paypal_order_id': 'PP-ORDER', 'order_status': 'COMPLETED',
                'authorization_id': 'PP-AUTH', 'status': 'CREATED', 'expiry': None}

    def capture(self, **kw):
        self.calls.append(('capture', kw))
        return {
            'capture_id': 'PP-CAP', 'status': 'COMPLETED',
            'gross_amount': Decimal('42.00'), 'paypal_fee': Decimal('1.62'),
            'net_amount': Decimal('40.38'), 'currency': 'USD'}

    def void(self, **kw):
        self.calls.append(('void', kw))
        return {'status': 'VOIDED'}

    def refund(self, **kw):
        self.calls.append(('refund', kw))
        return {'refund_id': 'PP-REF-%d' % len(self.calls),
                'status': 'COMPLETED', 'amount': kw['amount']}

    reauth_fails = False

    def reauthorize(self, **kw):
        self.calls.append(('reauthorize', kw))
        if self.reauth_fails:
            raise PayPalFailure('authorization can no longer be renewed')
        return {'authorization_id': 'PP-AUTH2', 'status': 'CREATED', 'expiry': None}

    def get_authorization(self, authorization_id):
        return {'authorization_id': authorization_id, 'status': 'CREATED', 'expiry': None}

    def vault_card(self, **kw):
        self.calls.append(('vault_card', kw))
        return {'token_id': 'TOK-1', 'customer_id': 'CUST-1',
                'brand': 'VISA', 'last_digits': '1111', 'expiry': '2030-01'}

    def delete_vault_token(self, token_id):
        self.calls.append(('delete_vault_token', token_id))
        return True


def _seed_products():
    """Return two catalogue product ids that can be ordered, creating them if
    the fixtures did not."""
    from oscar.core.loading import get_model
    Product = get_model('catalogue', 'Product')
    ProductClass = get_model('catalogue', 'ProductClass')
    Partner = get_model('partner', 'Partner')
    StockRecord = get_model('partner', 'StockRecord')

    pc, _ = ProductClass.objects.get_or_create(
        name='Test goods', defaults={'track_stock': False, 'requires_shipping': False})
    pc.track_stock = False
    pc.save()
    partner, _ = Partner.objects.get_or_create(name='Test partner')
    ids = []
    for i, price in enumerate([Decimal('42.00'), Decimal('10.00')]):
        product = Product.objects.create(
            title='Test product %d' % i, product_class=pc, structure='standalone')
        StockRecord.objects.create(
            product=product, partner=partner,
            partner_sku='SKU-%d' % i, price_currency='USD', price=price)
        ids.append(product.id)
    return ids


@override_settings(PAYPAL_CURRENCY='USD')
class ViewFlowTests(TestCase):
    def setUp(self):
        self._orig_service = views.PayPalService
        self.fake = FakePayPalService()
        views.PayPalService = lambda client=None: self.fake
        self.shopper = User.objects.create_user(
            username='shopper', email='shopper@example.com', password='pw-123456789')
        self.other = User.objects.create_user(
            username='other', email='other@example.com', password='pw-123456789')
        self.staff = User.objects.create_user(
            username='op', email='op@example.com', password='pw-123456789',
            is_staff=True)
        self.product_ids = _seed_products()

    def tearDown(self):
        views.PayPalService = self._orig_service

    def _login(self, user):
        self.client.force_login(user)

    def _create_order(self, user=None):
        self._login(user or self.shopper)
        resp = self.client.post(
            reverse('api:create-order'),
            data=json.dumps({'items': [{'id': self.product_ids[0], 'quantity': 1}]}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def test_create_requires_login(self):
        resp = self.client.post(
            reverse('api:create-order'),
            data=json.dumps({'items': [{'id': self.product_ids[0], 'quantity': 1}]}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 401)

    def test_full_pay_fulfil_flow(self):
        created = self._create_order()
        order_id = created['orderId']
        self.assertEqual(created['total'], '42.00')

        # pay (authorize)
        resp = self.client.post(
            reverse('api:pay-order', args=[order_id]),
            data=json.dumps({'card': {'number': '4111111111111111', 'expiry': '2030-01'}}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['payment']['state'], 'authorized')

        # double-click pay -> idempotent, still one authorize call
        resp2 = self.client.post(
            reverse('api:pay-order', args=[order_id]),
            data=json.dumps({'card': {'number': '4111111111111111', 'expiry': '2030-01'}}),
            content_type='application/json')
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(
            sum(1 for c in self.fake.calls if c[0] == 'create_authorized_order'), 1)

        # fulfil (capture) as staff
        self._login(self.staff)
        resp = self.client.post(reverse('api:fulfil-order', args=[order_id]),
                                content_type='application/json')
        self.assertEqual(resp.status_code, 200, resp.content)
        payment = resp.json()['payment']
        self.assertEqual(payment['state'], 'captured')
        self.assertEqual(payment['paypalFee'], '1.62')
        self.assertEqual(payment['netAmount'], '40.38')

        # double fulfil -> idempotent, one capture
        self.client.post(reverse('api:fulfil-order', args=[order_id]),
                         content_type='application/json')
        self.assertEqual(sum(1 for c in self.fake.calls if c[0] == 'capture'), 1)

    def test_fulfil_requires_staff(self):
        created = self._create_order()
        self._authorize(created['orderId'])
        self._login(self.shopper)
        resp = self.client.post(
            reverse('api:fulfil-order', args=[created['orderId']]),
            content_type='application/json')
        self.assertEqual(resp.status_code, 403)

    def test_cancel_before_fulfil_voids(self):
        created = self._create_order()
        self._authorize(created['orderId'])
        self._login(self.staff)
        resp = self.client.post(
            reverse('api:cancel-order', args=[created['orderId']]),
            content_type='application/json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['payment']['state'], 'voided')

    def test_refund_idempotency_and_cap(self):
        created = self._create_order()
        order_id = created['orderId']
        self._authorize(order_id)
        self._login(self.staff)
        self.client.post(reverse('api:fulfil-order', args=[order_id]),
                         content_type='application/json')

        # partial refund
        resp = self.client.post(
            reverse('api:refund-order', args=[order_id]),
            data=json.dumps({'amount': '10.00', 'idempotencyKey': 'k1'}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 201, resp.content)
        first_refund_id = resp.json()['refundId']

        # same key again -> no second refund
        resp = self.client.post(
            reverse('api:refund-order', args=[order_id]),
            data=json.dumps({'amount': '10.00', 'idempotencyKey': 'k1'}),
            content_type='application/json')
        self.assertEqual(resp.json()['refundId'], first_refund_id)
        self.assertEqual(RefundRecord.objects.count(), 1)
        self.assertEqual(sum(1 for c in self.fake.calls if c[0] == 'refund'), 1)

        # over-refund the remaining 32.00 -> 409
        resp = self.client.post(
            reverse('api:refund-order', args=[order_id]),
            data=json.dumps({'amount': '999.00', 'idempotencyKey': 'k2'}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 409)

        # a distinct legitimate partial refund proceeds
        resp = self.client.post(
            reverse('api:refund-order', args=[order_id]),
            data=json.dumps({'amount': '5.00', 'idempotencyKey': 'k3'}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 201)
        payment = PayPalPayment.objects.get(order_id=order_id)
        self.assertEqual(payment.amount_refunded, Decimal('15.00'))
        self.assertEqual(payment.state, PayPalPayment.PARTIALLY_REFUNDED)

    def test_shopper_cannot_see_or_pay_others_order(self):
        created = self._create_order(self.shopper)
        order_id = created['orderId']
        self._login(self.other)
        resp = self.client.post(
            reverse('api:pay-order', args=[order_id]),
            data=json.dumps({'card': {'number': '4111111111111111', 'expiry': '2030-01'}}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        # my-orders is scoped
        resp = self.client.get(reverse('api:my-orders'))
        self.assertEqual(resp.json()['orders'], [])

    def test_stale_authorization_is_renewed_before_capture(self):
        from django.utils import timezone
        created = self._create_order()
        order_id = created['orderId']
        self._authorize(order_id)
        # Force the hold to look expired.
        payment = PayPalPayment.objects.get(order_id=order_id)
        payment.authorization_expiry = timezone.now() - timezone.timedelta(hours=1)
        payment.save(update_fields=['authorization_expiry'])

        self._login(self.staff)
        resp = self.client.post(reverse('api:fulfil-order', args=[order_id]),
                                content_type='application/json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['payment']['state'], 'captured')
        self.assertEqual(sum(1 for c in self.fake.calls if c[0] == 'reauthorize'), 1)

    def test_unrenewable_authorization_is_operator_actionable_conflict(self):
        from django.utils import timezone
        created = self._create_order()
        order_id = created['orderId']
        self._authorize(order_id)
        payment = PayPalPayment.objects.get(order_id=order_id)
        payment.authorization_expiry = timezone.now() - timezone.timedelta(hours=1)
        payment.save(update_fields=['authorization_expiry'])
        self.fake.reauth_fails = True

        self._login(self.staff)
        resp = self.client.post(reverse('api:fulfil-order', args=[order_id]),
                                content_type='application/json')
        self.assertEqual(resp.status_code, 409)
        self.assertIn('renew', resp.json()['error'].lower())

    def _authorize(self, order_id):
        self._login(self.shopper)
        resp = self.client.post(
            reverse('api:pay-order', args=[order_id]),
            data=json.dumps({'card': {'number': '4111111111111111', 'expiry': '2030-01'}}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 200, resp.content)


@override_settings(PAYPAL_CURRENCY='USD')
class SavedCardViewTests(TestCase):
    def setUp(self):
        self._orig_service = views.PayPalService
        self.fake = FakePayPalService()
        views.PayPalService = lambda client=None: self.fake
        self.shopper = User.objects.create_user(
            username='s', email='s@example.com', password='pw-123456789')
        self.other = User.objects.create_user(
            username='o', email='o@example.com', password='pw-123456789')

    def tearDown(self):
        views.PayPalService = self._orig_service

    def test_save_list_delete_scoped(self):
        self.client.force_login(self.shopper)
        resp = self.client.post(
            reverse('api:payment-methods'),
            data=json.dumps({'card': {'number': '4111111111111111', 'expiry': '2030-01'}}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 201, resp.content)
        pm_id = resp.json()['paymentMethodId']
        self.assertEqual(resp.json()['lastDigits'], '1111')
        self.assertNotIn('number', json.dumps(resp.json()))

        # list shows it
        resp = self.client.get(reverse('api:payment-methods'))
        self.assertEqual(len(resp.json()['paymentMethods']), 1)

        # other shopper cannot see it
        self.client.force_login(self.other)
        resp = self.client.get(reverse('api:payment-methods'))
        self.assertEqual(resp.json()['paymentMethods'], [])
        # ... nor delete it
        resp = self.client.delete(
            reverse('api:delete-payment-method', args=[pm_id]))
        self.assertEqual(resp.status_code, 404)

        # owner deletes it; then it is gone
        self.client.force_login(self.shopper)
        resp = self.client.delete(
            reverse('api:delete-payment-method', args=[pm_id]))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(SavedPaymentMethod.objects.filter(id=pm_id).exists())
