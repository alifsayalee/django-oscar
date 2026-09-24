import datetime
import json

import httpx
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from maxio_advanced_billing.core import HttpRequest, HttpResponse
from maxio_advanced_billing.models import (
    Customer, CustomerResponse, Product, ProductResponse, Subscription, SubscriptionResponse)

from . import maxio
from .models import BillingAccount, SubscriptionEnrollment

MAXIO_SETTINGS = dict(
    MAXIO_API_KEY='test-key',
    MAXIO_SITE_SUBDOMAIN='acme',
    MAXIO_ENVIRONMENT='US',
    MAXIO_DEFAULT_PRODUCT_FAMILY='eshop-subscribe',
    MAXIO_BASE_URL='',
    MAXIO_TIMEOUT=5.0,
    MAXIO_CUSTOMER_REFERENCE_PREFIX='test-customer-',
    MAXIO_PAYMENT_COLLECTION_METHOD='remittance',
)

NOW = datetime.datetime(2026, 9, 24, 10, 0, tzinfo=datetime.timezone.utc)


class StubTransport:
    """Satisfies the SDK's sync transport protocol; answers queued responses (or raises queued errors) in order."""

    def __init__(self):
        self.queue = []
        self.requests: list[HttpRequest] = []

    def add(self, *items):
        self.queue.extend(items)

    def send(self, request):
        self.requests.append(request)
        if not self.queue:
            raise AssertionError('unexpected request: %s %s' % (request.method, request.url))
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass

    def calls(self):
        return ['%s %s' % (r.method, r.url.split('.com', 1)[1].split('?', 1)[0]) for r in self.requests]


def respond(status, body=None):
    content = b'' if body is None else json.dumps(body).encode()
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'}, content=content)


def plans_response():
    return respond(200, [
        ProductResponse(product=Product(id=11, handle='basic-plan', name='Basic Plan', price_in_cents=2900,
                                        interval=1, interval_unit='month')).to_dict(),
        ProductResponse(product=Product(id=12, handle='eshop-pro', name='Pro Plan', price_in_cents=29900,
                                        interval=1, interval_unit='month')).to_dict(),
    ])


def customer_response(reference, customer_id=7):
    return respond(200, CustomerResponse(customer=Customer(
        id=customer_id, reference=reference, email='shopper@example.com')).to_dict())


def subscription_body(reference, customer_reference, subscription_id=501, state='active', handle='eshop-pro'):
    return SubscriptionResponse(subscription=Subscription(
        id=subscription_id,
        state=state,
        reference=reference,
        product_price_in_cents=29900,
        currency='USD',
        next_assessment_at=NOW + datetime.timedelta(days=30),
        current_period_ends_at=NOW + datetime.timedelta(days=30),
        created_at=NOW,
        product=Product(handle=handle, name='Pro Plan', interval=1, interval_unit='month'),
        customer=Customer(id=7, reference=customer_reference),
    )).to_dict()


@override_settings(**MAXIO_SETTINGS)
class SubscriptionApiTestCase(TestCase):

    def setUp(self):
        cache.clear()
        self.transport = StubTransport()
        maxio.set_client(maxio.build_client(http_client=self.transport))
        self.user = get_user_model().objects.create_user(
            username='shopper', email='shopper@example.com', password='not-a-real-password-1')
        self.client.force_login(self.user)

    def tearDown(self):
        maxio.set_client(None)

    def subscribe(self, plan_handle='eshop-pro'):
        return self.client.post('/api/subscriptions', data=json.dumps({'planHandle': plan_handle}),
                                content_type='application/json')

    def customer_reference(self):
        return BillingAccount.objects.get(user=self.user).reference

    def queue_new_customer(self):
        """Customer lookup misses, then the create succeeds (reference echoed back)."""
        self.transport.add(respond(404))
        self.transport.add(customer_response(reference=None))

    def last_body(self):
        return self.transport.requests[-1].body.value

    # --- plans -------------------------------------------------------------------------------------------

    def test_plans_carry_their_handles(self):
        self.transport.add(plans_response())

        response = self.client.get('/api/subscription-plans')

        self.assertEqual(response.status_code, 200)
        plans = response.json()['plans']
        self.assertEqual([p['planHandle'] for p in plans], ['basic-plan', 'eshop-pro'])
        self.assertEqual(plans[1]['price'], '299.00')
        request = self.transport.requests[0]
        self.assertEqual(request.method, 'GET')
        self.assertTrue(request.url.startswith('https://acme.chargify.com/product_families/'))
        self.assertIn('eshop-subscribe', request.url)
        self.assertEqual(request.headers['authorization'][:6], 'Basic ')

    def test_plans_are_cached(self):
        self.transport.add(plans_response())
        self.client.get('/api/subscription-plans')
        self.client.get('/api/subscription-plans')
        self.assertEqual(len(self.transport.requests), 1)

    def test_anonymous_callers_are_refused(self):
        response = Client().get('/api/subscription-plans')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.transport.requests, [])

    def test_posts_require_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post('/api/subscriptions', data='{"planHandle": "eshop-pro"}',
                               content_type='application/json')
        self.assertEqual(response.status_code, 403)

    # --- subscribing -------------------------------------------------------------------------------------

    def test_subscribe_creates_customer_and_subscription(self):
        self.transport.add(plans_response())
        self.queue_new_customer()
        self.transport.add(respond(201, subscription_body('placeholder', 'placeholder')))

        response = self.subscribe()

        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data['subscriptionId'], 501)
        self.assertTrue(data['created'])
        self.assertEqual(data['state'], 'active')
        self.assertEqual(data['planHandle'], 'eshop-pro')
        self.assertEqual(data['price'], '299.00')
        self.assertEqual(data['nextBillingAt'], '2026-10-24T10:00:00+00:00')

        self.assertEqual(self.transport.calls(), [
            'GET /product_families/handle%3Aeshop-subscribe/products.json',
            'GET /customers/lookup.json',
            'POST /customers.json',
            'POST /subscriptions.json',
        ])
        reference = self.customer_reference()
        self.assertTrue(reference.startswith('test-customer-'))
        customer_body = self.transport.requests[2].body.value['customer']
        self.assertEqual(customer_body['reference'], reference)
        self.assertEqual(customer_body['email'], 'shopper@example.com')
        enrollment = SubscriptionEnrollment.objects.get()
        self.assertEqual(self.last_body(), {'subscription': {
            'product_handle': 'eshop-pro', 'customer_reference': reference, 'reference': enrollment.reference,
            'payment_collection_method': 'remittance'}})
        self.assertEqual((enrollment.status, enrollment.maxio_subscription_id), ('active', 501))

    def test_repeat_subscribe_returns_the_same_subscription(self):
        self.transport.add(plans_response())
        self.queue_new_customer()
        self.transport.add(respond(201, subscription_body('r', 'c')))
        first = self.subscribe().json()

        self.transport.add(respond(200, subscription_body('r', 'c')))
        second = self.subscribe()

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['subscriptionId'], first['subscriptionId'])
        self.assertFalse(second.json()['created'])
        self.assertEqual(self.transport.calls()[-1], 'GET /subscriptions/501.json')
        self.assertEqual(self.transport.calls().count('POST /subscriptions.json'), 1)

    def test_existing_customer_is_reused(self):
        account = BillingAccount.objects.create(user=self.user)
        self.transport.add(plans_response())
        self.transport.add(customer_response(account.reference))
        self.transport.add(respond(201, subscription_body('r', account.reference)))

        self.assertEqual(self.subscribe().status_code, 201)
        self.assertNotIn('POST /customers.json', self.transport.calls())

    def test_resubscribe_allowed_after_subscription_ended(self):
        SubscriptionEnrollment.objects.create(user=self.user, plan_handle='eshop-pro', status='active',
                                              maxio_subscription_id=400)
        BillingAccount.objects.create(user=self.user)
        self.transport.add(plans_response())
        self.transport.add(respond(200, subscription_body('r', 'c', subscription_id=400, state='canceled')))
        self.transport.add(customer_response(self.customer_reference()))
        self.transport.add(respond(201, subscription_body('r2', 'c')))

        response = self.subscribe()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(SubscriptionEnrollment.objects.get(maxio_subscription_id=400).status, 'ended')

    def test_unknown_plan_is_rejected_without_writing(self):
        self.transport.add(plans_response())
        response = self.subscribe('no-such-plan')
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['code'], 'unknown_plan')
        self.assertEqual(len(self.transport.requests), 1)

    @override_settings(MAXIO_PAYMENT_COLLECTION_METHOD='cheque')
    def test_invalid_collection_method_is_a_configuration_error(self):
        self.transport.add(plans_response())
        self.queue_new_customer()

        response = self.subscribe()

        self.assertEqual(response.status_code, 503)
        self.assertNotIn('POST /subscriptions.json', self.transport.calls())
        self.assertEqual(SubscriptionEnrollment.objects.get().status, 'failed')

    def test_missing_plan_handle_is_a_bad_request(self):
        response = self.client.post('/api/subscriptions', data='{}', content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_provider_validation_error_is_the_callers(self):
        self.transport.add(plans_response())
        self.queue_new_customer()
        self.transport.add(respond(422, {'errors': ['Product must be active']}))

        response = self.subscribe()

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['details'], ['Product must be active'])
        self.assertEqual(SubscriptionEnrollment.objects.get().status, 'failed')

    def test_provider_auth_failure_is_ours_not_the_callers(self):
        self.transport.add(respond(401, {'errors': ['bad key']}))
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 502)

    def test_rate_limit_is_service_unavailable(self):
        self.transport.add(respond(429))
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response['Retry-After'], '5')

    def test_unsent_write_is_a_known_failure(self):
        self.transport.add(plans_response())
        self.queue_new_customer()
        self.transport.add(httpx.ConnectError('refused'))

        response = self.subscribe()

        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()['error']['outcomeUnknown'])
        self.assertEqual(SubscriptionEnrollment.objects.get().status, 'failed')
        # Nothing was sent, so there is nothing to look up.
        self.assertNotIn('GET /subscriptions/lookup.json', self.transport.calls())

    def test_unanswered_write_that_landed_is_recovered_by_reference(self):
        self.transport.add(plans_response())
        self.queue_new_customer()
        self.transport.add(httpx.ReadTimeout('no reply'))
        self.transport.add(respond(200, subscription_body('r', 'c', subscription_id=777)))

        response = self.subscribe()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['subscriptionId'], 777)
        enrollment = SubscriptionEnrollment.objects.get()
        lookup = self.transport.requests[-1]
        self.assertIn('reference=' + enrollment.reference, lookup.url)
        self.assertEqual((enrollment.status, enrollment.maxio_subscription_id), ('active', 777))

    def test_unanswered_write_not_found_stays_unknown_and_blocks_a_duplicate(self):
        self.transport.add(plans_response())
        self.queue_new_customer()
        self.transport.add(httpx.ReadTimeout('no reply'))
        self.transport.add(respond(404))

        response = self.subscribe()

        self.assertEqual(response.status_code, 504)
        self.assertTrue(response.json()['error']['outcomeUnknown'])
        self.assertEqual(SubscriptionEnrollment.objects.get().status, 'unknown')

        # A retry must not create a second subscription while the first may still land.
        self.transport.add(respond(404))
        retry = self.subscribe()
        self.assertEqual(retry.status_code, 409)
        self.assertTrue(retry.json()['error']['outcomeUnknown'])
        self.assertEqual(self.transport.calls().count('POST /subscriptions.json'), 1)

    def test_unknown_outcome_is_closed_after_the_reconcile_window(self):
        enrollment = SubscriptionEnrollment.objects.create(user=self.user, plan_handle='eshop-pro', status='unknown')
        SubscriptionEnrollment.objects.filter(pk=enrollment.pk).update(
            date_updated=timezone.now() - datetime.timedelta(hours=1))
        BillingAccount.objects.create(user=self.user)
        self.transport.add(plans_response())
        self.transport.add(respond(404))
        self.transport.add(customer_response(self.customer_reference()))
        self.transport.add(respond(201, subscription_body('r', 'c')))

        self.assertEqual(self.subscribe().status_code, 201)
        enrollment.refresh_from_db()
        self.assertEqual(enrollment.status, 'failed')

    def test_truncated_success_body_is_not_reported_as_success(self):
        self.transport.add(plans_response())
        self.queue_new_customer()
        self.transport.add(respond(201, {}))
        self.transport.add(respond(404))

        response = self.subscribe()

        self.assertEqual(response.status_code, 502)
        self.assertTrue(response.json()['error']['outcomeUnknown'])
        self.assertEqual(SubscriptionEnrollment.objects.get().status, 'unknown')

    def test_customer_create_race_converges_on_one_customer(self):
        self.transport.add(plans_response())
        self.transport.add(respond(404))
        self.transport.add(respond(422, {'errors': ['Reference must be unique']}))
        self.transport.add(customer_response('whatever', customer_id=9))
        self.transport.add(respond(201, subscription_body('r', 'c')))

        self.assertEqual(self.subscribe().status_code, 201)
        self.assertEqual(BillingAccount.objects.get().maxio_customer_id, 9)

    # --- billing customer --------------------------------------------------------------------------------

    def test_billing_customer_is_idempotent(self):
        self.queue_new_customer()
        first = self.client.post('/api/billing-customer')
        self.assertEqual(first.status_code, 201)

        self.transport.add(customer_response(self.customer_reference()))
        second = self.client.post('/api/billing-customer')
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['customerId'], first.json()['customerId'])
        self.assertEqual(BillingAccount.objects.count(), 1)

    # --- reading back ------------------------------------------------------------------------------------

    def test_my_subscriptions_is_empty_before_billing_starts(self):
        response = self.client.get('/api/my-subscriptions')
        self.assertEqual(response.json(), {'subscriptions': []})
        self.assertEqual(self.transport.requests, [])

    def test_my_subscriptions_lists_the_customers_subscriptions(self):
        BillingAccount.objects.create(user=self.user, maxio_customer_id=7)
        self.transport.add(respond(200, [subscription_body('r', 'c')]))

        response = self.client.get('/api/my-subscriptions')

        self.assertEqual(response.status_code, 200)
        self.assertEqual([s['subscriptionId'] for s in response.json()['subscriptions']], [501])
        self.assertEqual(self.transport.calls(), ['GET /customers/7/subscriptions.json'])

    def test_someone_elses_subscription_is_not_found(self):
        BillingAccount.objects.create(user=self.user)
        self.transport.add(respond(200, subscription_body('r', 'another-customer')))
        response = self.client.get('/api/my-subscriptions/501')
        self.assertEqual(response.status_code, 404)

    def test_own_subscription_is_returned(self):
        reference = BillingAccount.objects.create(user=self.user).reference
        self.transport.add(respond(200, subscription_body('r', reference)))
        response = self.client.get('/api/my-subscriptions/501')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['subscriptionId'], 501)

    def test_read_is_retried_once_when_never_sent(self):
        self.transport.add(httpx.ConnectError('refused'), plans_response())
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.transport.requests), 2)


class ClientConfigurationTestCase(TestCase):

    def base_url(self, **overrides):
        transport = StubTransport()
        transport.add(respond(404))
        with override_settings(**{**MAXIO_SETTINGS, **overrides}):
            client = maxio.build_client(http_client=transport)
        try:
            client.customers.with_raw_response.read_customer_by_reference('x')
        finally:
            client.close()
        return transport.requests[0].url.split('/customers', 1)[0]

    def test_base_url_is_derived_from_the_subdomain(self):
        self.assertEqual(self.base_url(), 'https://acme.chargify.com')

    def test_base_url_override_is_used_verbatim(self):
        self.assertEqual(self.base_url(MAXIO_BASE_URL='http://localhost:9999/maxio'), 'http://localhost:9999/maxio')

    def test_eu_environment(self):
        self.assertEqual(self.base_url(MAXIO_ENVIRONMENT='eu'), 'https://acme.ebilling.maxio.com')

    def test_missing_api_key_is_refused(self):
        from django.core.exceptions import ImproperlyConfigured
        with override_settings(**{**MAXIO_SETTINGS, 'MAXIO_API_KEY': ''}):
            with self.assertRaises(ImproperlyConfigured):
                maxio.build_client(http_client=StubTransport())

    def test_unknown_environment_is_refused(self):
        from django.core.exceptions import ImproperlyConfigured
        with override_settings(**{**MAXIO_SETTINGS, 'MAXIO_ENVIRONMENT': 'moon'}):
            with self.assertRaises(ImproperlyConfigured):
                maxio.build_client(http_client=StubTransport())
