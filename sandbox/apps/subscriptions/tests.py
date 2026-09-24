"""
Tests for the subscription billing API.

Maxio is faked at the SDK's transport seam: a real client builds real requests, and the stub answers
them in order, asserting each one's method and path. Run from ``sandbox/``:

    python manage.py test apps.subscriptions
"""

import json
from datetime import datetime, timezone
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from maxio_advanced_billing.core import HttpRequest, HttpResponse
from maxio_advanced_billing.models import (
    Customer,
    CustomerResponse,
    Product,
    ProductResponse,
    Subscription,
    SubscriptionResponse,
)
from maxio_advanced_billing.models.enums import SubscriptionState

from . import maxio
from .models import BillingCustomer, SubscriptionAttempt, WriteStatus

CUSTOMER_ID = 501
PLAN = 'eshop-pro'


def json_response(status, body):
    return HttpResponse(
        status_code=status, headers={'content-type': 'application/json'}, content=json.dumps(body).encode())


def empty_response(status):
    return HttpResponse(status_code=status, headers={})


def product(handle, name, price, archived=False):
    return ProductResponse(product=Product(
        id=hash(handle) % 10000, handle=handle, name=name, price_in_cents=price, interval=1,
        interval_unit='month', require_credit_card=False,
        archived_at=datetime(2026, 1, 1, tzinfo=timezone.utc) if archived else None,
    )).to_dict()


PLANS_BODY = [
    product(PLAN, 'Pro Plan', 29900),
    product('basic-plan', 'Basic Plan', 2900),
    product('old-plan', 'Old Plan', 100, archived=True),
]


def customer_body(customer_id=CUSTOMER_ID):
    return CustomerResponse(customer=Customer(id=customer_id, email='shopper@example.com')).to_dict()


def subscription_body(subscription_id=9001, state=SubscriptionState.ACTIVE, reference=None,
                      customer_id=CUSTOMER_ID, handle=PLAN):
    return SubscriptionResponse(subscription=Subscription(
        id=subscription_id, state=state, reference=reference,
        customer=Customer(id=customer_id), product=Product(handle=handle, name='Pro Plan', interval=1),
        product_price_in_cents=29900, currency='USD',
        current_period_ends_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
    )).to_dict()


class StubTransport:
    """Answers queued (method, path fragment, response-or-exception) steps, in order."""

    def __init__(self):
        self.steps = []
        self.requests: list[HttpRequest] = []

    def expect(self, method, path, answer):
        self.steps.append((method, path, answer))

    def send(self, request):
        self.requests.append(request)
        if not self.steps:
            raise AssertionError('Unexpected request %s %s' % (request.method, request.url))
        method, path, answer = self.steps.pop(0)
        assert request.method == method, (request.method, request.url, method, path)
        assert path in request.url, (request.url, path)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self):
        pass


@override_settings(
    MAXIO_API_KEY='test-key', MAXIO_SITE_SUBDOMAIN='example', MAXIO_ENVIRONMENT='US', MAXIO_BASE_URL='',
    MAXIO_DEFAULT_PRODUCT_FAMILY='eshop-subscribe', MAXIO_REFERENCE_PREFIX='test',
)
class SubscriptionApiTestCase(TestCase):

    def setUp(self):
        cache.clear()
        self.transport = StubTransport()
        maxio.set_client(maxio.build_client(http_client=self.transport))
        self.addCleanup(maxio.set_client, None)
        sleep = mock.patch('apps.subscriptions.maxio.time.sleep')
        sleep.start()
        self.addCleanup(sleep.stop)
        self.user = get_user_model().objects.create_user(
            username='shopper', email='shopper@example.com', password='not-a-real-password')
        self.client.force_login(self.user)

    # helpers

    def expect_plans(self):
        self.transport.expect('GET', '/product_families/handle%3Aeshop-subscribe/products.json',
                              json_response(200, PLANS_BODY))

    def expect_new_customer(self):
        self.transport.expect('GET', '/customers/lookup.json', empty_response(404))
        self.transport.expect('POST', '/customers.json', json_response(201, customer_body()))

    def subscribe(self, plan=PLAN):
        return self.client.post('/api/subscriptions', data=json.dumps({'planHandle': plan}),
                                content_type='application/json')

    def assert_all_answered(self):
        self.assertEqual(self.transport.steps, [], 'queued responses were not consumed')

    @property
    def reference(self):
        return 'test-sub-%s-%s-1' % (self.user.pk, PLAN)

    # plans

    def test_plans_list_active_plans_with_handles(self):
        self.expect_plans()
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 200)
        plans = response.json()['plans']
        self.assertEqual([p['planHandle'] for p in plans], [PLAN, 'basic-plan'])
        self.assertEqual(plans[0]['priceInCents'], 29900)
        self.assertIn('per_page=200', self.transport.requests[0].url)
        self.assertEqual(self.transport.requests[0].headers['authorization'][:6], 'Basic ')

    def test_anonymous_callers_get_401(self):
        self.client.logout()
        self.assertEqual(self.client.get('/api/subscription-plans').status_code, 401)
        self.assertEqual(self.subscribe().status_code, 401)
        self.assertEqual(self.transport.requests, [])

    # subscribe

    def test_subscribe_creates_customer_and_subscription(self):
        self.expect_plans()
        self.expect_new_customer()
        self.transport.expect('POST', '/subscriptions.json', json_response(201, subscription_body()))

        response = self.subscribe()

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['subscriptionId'], 9001)
        self.assertEqual(body['state'], 'active')
        self.assertEqual(body['status'], 'done')
        self.assertEqual(body['plan']['planHandle'], PLAN)
        self.assertEqual(body['nextBillingAt'], '2026-10-24T00:00:00+00:00')
        customer_request = self.transport.requests[2].body.value['customer']
        self.assertEqual(customer_request['reference'], 'test-cust-%s' % self.user.pk)
        self.assertEqual(customer_request['email'], 'shopper@example.com')
        subscription_request = self.transport.requests[3].body.value['subscription']
        self.assertEqual(subscription_request, {
            'product_handle': PLAN, 'customer_id': CUSTOMER_ID, 'reference': self.reference,
            'payment_collection_method': 'remittance'})
        attempt = SubscriptionAttempt.objects.get()
        self.assertEqual((attempt.status, attempt.maxio_subscription_id), (WriteStatus.DONE, 9001))
        self.assert_all_answered()

    def test_double_submit_returns_the_same_subscription(self):
        self.expect_plans()
        self.expect_new_customer()
        self.transport.expect('POST', '/subscriptions.json', json_response(201, subscription_body()))
        self.assertEqual(self.subscribe().status_code, 201)

        # Second click: plans are cached, the customer is known; only a read, never a create.
        self.transport.expect('GET', '/subscriptions/9001.json', json_response(200, subscription_body()))
        response = self.subscribe()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['subscriptionId'], 9001)
        self.assertEqual(SubscriptionAttempt.objects.count(), 1)
        self.assertEqual(BillingCustomer.objects.count(), 1)
        self.assert_all_answered()

    def test_existing_customer_is_found_by_reference_not_recreated(self):
        self.expect_plans()
        self.transport.expect('GET', '/customers/lookup.json', json_response(200, customer_body()))
        self.transport.expect('POST', '/subscriptions.json', json_response(201, subscription_body()))
        self.assertEqual(self.subscribe().status_code, 201)
        self.assertIn('reference=test-cust-%s' % self.user.pk, self.transport.requests[1].url)
        self.assert_all_answered()

    def test_subscribing_again_after_the_subscription_ended_creates_a_new_one(self):
        self.expect_plans()
        self.expect_new_customer()
        self.transport.expect('POST', '/subscriptions.json', json_response(201, subscription_body()))
        self.subscribe()
        self.transport.expect('GET', '/subscriptions/9001.json',
                              json_response(200, subscription_body(state=SubscriptionState.CANCELED)))
        self.transport.expect('POST', '/subscriptions.json',
                              json_response(201, subscription_body(subscription_id=9002)))

        response = self.subscribe()

        self.assertEqual((response.status_code, response.json()['subscriptionId']), (201, 9002))
        self.assertTrue(self.transport.requests[-1].body.value['subscription']['reference'].endswith('-2'))

    def test_unknown_plan_is_rejected_without_writing(self):
        self.expect_plans()
        response = self.subscribe('no-such-plan')
        self.assertEqual(response.status_code, 422)
        self.assertEqual(len(self.transport.requests), 1)

    def test_missing_plan_handle_is_a_400(self):
        response = self.client.post('/api/subscriptions', data='{}', content_type='application/json')
        self.assertEqual(response.status_code, 400)

    # provider states

    def test_an_id_with_a_not_done_state_is_accepted_not_created(self):
        self.expect_plans()
        self.expect_new_customer()
        self.transport.expect('POST', '/subscriptions.json',
                              json_response(201, subscription_body(state=SubscriptionState.PAST_DUE)))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['status'], 'attention')

    def test_an_unlisted_state_is_unknown_not_done(self):
        self.expect_plans()
        self.expect_new_customer()
        self.transport.expect('POST', '/subscriptions.json',
                              json_response(201, subscription_body(state='something_new')))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['status'], 'unknown')

    def test_state_mapping(self):
        self.assertEqual(maxio.status_from_provider(SubscriptionState.ACTIVE), maxio.DONE)
        self.assertEqual(maxio.status_from_provider('active'), maxio.DONE)
        self.assertEqual(maxio.status_from_provider(SubscriptionState.AWAITING_SIGNUP), maxio.PENDING)
        self.assertEqual(maxio.status_from_provider(SubscriptionState.CANCELED), maxio.ENDED)
        self.assertEqual(maxio.status_from_provider('brand_new_state'), maxio.UNKNOWN)

    # failures of the create

    def test_validation_errors_are_passed_through(self):
        self.expect_plans()
        self.expect_new_customer()
        self.transport.expect('POST', '/subscriptions.json',
                              json_response(422, {'errors': ['Product must be active.']}))
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['details'], ['Product must be active.'])
        self.assertEqual(SubscriptionAttempt.objects.get().status, WriteStatus.FAILED)

    def test_provider_auth_failure_is_our_502_not_the_callers_401(self):
        self.transport.expect('GET', '/product_families/', empty_response(401))
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 502)

    def test_refused_connection_is_known_and_not_looked_up(self):
        self.expect_plans()
        self.expect_new_customer()
        self.transport.expect('POST', '/subscriptions.json', httpx.ConnectError('refused'))
        response = self.subscribe()
        self.assertEqual((response.status_code, response.json()['outcomeUnknown']), (502, False))
        self.assertEqual(SubscriptionAttempt.objects.get().status, WriteStatus.FAILED)
        self.assert_all_answered()  # no lookup was queued, and none was made

    def test_read_timeout_is_reconciled_by_reference(self):
        self.expect_plans()
        self.expect_new_customer()
        self.transport.expect('POST', '/subscriptions.json', httpx.ReadTimeout('no reply'))
        self.transport.expect('GET', '/subscriptions/lookup.json',
                              json_response(200, subscription_body(reference=self.reference)))
        response = self.subscribe()
        self.assertEqual((response.status_code, response.json()['subscriptionId']), (201, 9001))
        self.assertIn('reference=' + self.reference, self.transport.requests[-1].url)
        self.assertEqual(SubscriptionAttempt.objects.get().status, WriteStatus.DONE)

    def test_read_timeout_not_found_stays_unknown_and_is_never_recreated(self):
        self.expect_plans()
        self.expect_new_customer()
        self.transport.expect('POST', '/subscriptions.json', httpx.ReadTimeout('no reply'))
        self.transport.expect('GET', '/subscriptions/lookup.json', empty_response(404))
        response = self.subscribe()
        self.assertEqual((response.status_code, response.json()['outcomeUnknown']), (504, True))
        self.assertEqual(SubscriptionAttempt.objects.get().status, WriteStatus.UNKNOWN)

        # The user tries again: the earlier attempt is looked up, and it landed after all.
        self.transport.expect('GET', '/subscriptions/lookup.json',
                              json_response(200, subscription_body(reference=self.reference)))
        response = self.subscribe()
        self.assertEqual((response.status_code, response.json()['subscriptionId']), (200, 9001))
        self.assertEqual(
            [r.method for r in self.transport.requests].count('POST'), 2)  # customer + one subscription
        self.assertEqual(SubscriptionAttempt.objects.get().status, WriteStatus.DONE)

    def test_unsent_and_unknown_failures_are_told_apart(self):
        outcomes = []
        for error in (httpx.ConnectError('refused'), httpx.ReadTimeout('no reply')):
            failure = maxio.translate(error, write=True)
            outcomes.append((failure.status_code, failure.outcome_unknown))
        self.assertEqual(outcomes, [(502, False), (504, True)])

    def test_a_2xx_without_the_subscription_is_unreadable_and_reconciled(self):
        self.expect_plans()
        self.expect_new_customer()
        self.transport.expect('POST', '/subscriptions.json', json_response(201, {}))
        self.transport.expect('GET', '/subscriptions/lookup.json', empty_response(404))
        response = self.subscribe()
        self.assertEqual(response.status_code, 502)
        self.assertTrue(response.json()['outcomeUnknown'])
        self.assertEqual(SubscriptionAttempt.objects.get().status, WriteStatus.UNKNOWN)

    def test_duplicate_customer_reference_is_a_landing_not_a_failure(self):
        self.expect_plans()
        self.transport.expect('GET', '/customers/lookup.json', empty_response(404))
        self.transport.expect('POST', '/customers.json',
                              json_response(422, {'errors': ['Reference must be unique.']}))
        self.transport.expect('GET', '/customers/lookup.json', json_response(200, customer_body()))
        self.transport.expect('POST', '/subscriptions.json', json_response(201, subscription_body()))
        self.assertEqual(self.subscribe().status_code, 201)
        self.assertEqual(BillingCustomer.objects.get().maxio_customer_id, CUSTOMER_ID)

    def test_reads_are_retried_on_transient_failures(self):
        self.transport.expect('GET', '/product_families/', httpx.ConnectError('refused'))
        self.transport.expect('GET', '/product_families/', empty_response(503))
        self.expect_plans()
        self.assertEqual(self.client.get('/api/subscription-plans').status_code, 200)
        self.assert_all_answered()

    # reading back

    def test_my_subscriptions_lists_and_reconciles_unknown_attempts(self):
        BillingCustomer.objects.create(user=self.user, reference='test-cust-%s' % self.user.pk,
                                       maxio_customer_id=CUSTOMER_ID, status=WriteStatus.DONE)
        SubscriptionAttempt.objects.create(user=self.user, plan_handle=PLAN, sequence=1,
                                           reference=self.reference, status=WriteStatus.UNKNOWN)
        self.transport.expect('GET', '/customers/%s/subscriptions.json' % CUSTOMER_ID,
                              json_response(200, [subscription_body(reference=self.reference)]))

        response = self.client.get('/api/my-subscriptions')

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([s['subscriptionId'] for s in body['subscriptions']], [9001])
        self.assertEqual(body['unresolvedRequests'], [])
        self.assertEqual(SubscriptionAttempt.objects.get().status, WriteStatus.DONE)

    def test_my_subscriptions_without_customer_is_empty(self):
        response = self.client.get('/api/my-subscriptions')
        self.assertEqual(response.json(), {'subscriptions': [], 'unresolvedRequests': []})
        self.assertEqual(self.transport.requests, [])

    def test_subscription_detail_hides_other_customers_subscriptions(self):
        BillingCustomer.objects.create(user=self.user, reference='test-cust-%s' % self.user.pk,
                                       maxio_customer_id=CUSTOMER_ID, status=WriteStatus.DONE)
        self.transport.expect('GET', '/subscriptions/777.json',
                              json_response(200, subscription_body(subscription_id=777, customer_id=999)))
        self.assertEqual(self.client.get('/api/subscriptions/777').status_code, 404)
        self.transport.expect('GET', '/subscriptions/9001.json', json_response(200, subscription_body()))
        self.assertEqual(self.client.get('/api/subscriptions/9001').json()['subscriptionId'], 9001)

    def test_billing_customer_endpoint_is_idempotent(self):
        self.expect_new_customer()
        first = self.client.post('/api/billing-customer')
        second = self.client.post('/api/billing-customer')
        self.assertEqual(first.json()['customerId'], CUSTOMER_ID)
        self.assertEqual(second.json(), first.json())
        self.assert_all_answered()


class ConfigurationTestCase(TestCase):

    @override_settings(MAXIO_API_KEY='k', MAXIO_SITE_SUBDOMAIN='acme', MAXIO_ENVIRONMENT='us',
                       MAXIO_BASE_URL='http://127.0.0.1:9/api')
    def test_base_url_override_is_used_verbatim(self):
        transport = StubTransport()
        transport.expect('GET', '/customers/lookup.json', empty_response(404))
        with maxio.build_client(http_client=transport) as client:
            maxio.read_or_none(lambda: client.customers.read_customer_by_reference('r'))
        self.assertTrue(transport.requests[0].url.startswith('http://127.0.0.1:9/api/customers/lookup.json'))

    @override_settings(MAXIO_API_KEY='k', MAXIO_SITE_SUBDOMAIN='acme', MAXIO_ENVIRONMENT='us', MAXIO_BASE_URL='')
    def test_subdomain_selects_the_site(self):
        transport = StubTransport()
        transport.expect('GET', '/customers/lookup.json', empty_response(404))
        with maxio.build_client(http_client=transport) as client:
            maxio.read_or_none(lambda: client.customers.read_customer_by_reference('r'))
        self.assertTrue(transport.requests[0].url.startswith('https://acme.chargify.com/'))

    @override_settings(MAXIO_API_KEY='', MAXIO_SITE_SUBDOMAIN='acme')
    def test_missing_api_key_fails_fast(self):
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaises(ImproperlyConfigured):
            maxio.build_client(http_client=StubTransport())

    @override_settings(MAXIO_API_KEY='k', MAXIO_SITE_SUBDOMAIN='acme', MAXIO_ENVIRONMENT='mars')
    def test_unknown_environment_fails_fast(self):
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaises(ImproperlyConfigured):
            maxio.build_client(http_client=StubTransport())
