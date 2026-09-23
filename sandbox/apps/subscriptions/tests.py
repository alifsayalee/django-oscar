"""Tests for the Maxio subscription integration.

The SDK's transport protocol is the seam (python-testing): a routing stub
answers by (method, URL) so the multi-call flows read naturally, and the real
request-building/decoding pipeline is exercised. Maxio uses HTTP Basic auth, so
there is no token request to queue -- the stub only ever sees our operations.
"""

import json
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase

from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import HttpResponse

from . import services
from .errors import MaxioError
from .models import MaxioSubscription


def json_response(status, body):
    return HttpResponse(
        status_code=status,
        headers={'content-type': 'application/json'},
        content=json.dumps(body).encode(),
    )


class RouteStub:
    """Satisfies the sync transport protocol; routes by (method, url substring)."""

    def __init__(self):
        self._routes = []
        self.requests = []

    def route(self, method, substring, response):
        # `response` may be an HttpResponse, an Exception to raise, or a
        # zero-arg callable returning one (so a route can vary per hit).
        self._routes.append((method, substring, response))
        return self

    def send(self, request):
        self.requests.append(request)
        url = str(request.url)
        for method, substring, response in self._routes:
            if request.method == method and substring in url:
                if callable(response) and not isinstance(response, HttpResponse):
                    response = response()
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f'no stub route for {request.method} {url}')

    def close(self):
        pass

    def count(self, method, substring):
        return sum(
            1 for r in self.requests
            if r.method == method and substring in str(r.url)
        )


def make_client(stub):
    return MaxioAdvancedBillingClient(
        custom_http_client=stub,
        basic_auth={'username': 'k', 'password': 'x'},
        environment='us',
        server_config={'production': {'us': {'site': 'test'}}},
    )


PLAN_BODY = [
    {'product': {'id': 10, 'handle': 'eshop-pro', 'name': 'Pro Plan',
                 'description': 'Pro', 'price_in_cents': 29900,
                 'interval': 1, 'interval_unit': 'month'}},
    {'product': {'id': 11, 'handle': 'basic-plan', 'name': 'Basic Plan',
                 'price_in_cents': 2900, 'interval': 1, 'interval_unit': 'month'}},
]
FAMILY_BODY = [{'product_family': {'id': 500, 'handle': 'eshop-subscribe', 'name': 'eShop'}}]


def happy_stub():
    """A stub wired for the full happy subscribe/list flow."""
    stub = RouteStub()
    stub.route('GET', '/product_families.json', json_response(200, FAMILY_BODY))
    stub.route('GET', '/product_families/500/products.json', json_response(200, PLAN_BODY))
    # Customer: first lookup misses (404), then create succeeds.
    lookups = iter([json_response(404, {'errors': ['not found']}),
                    json_response(200, {'customer': {'id': 700, 'reference': 'oscar-cust-u1'}})])
    stub.route('GET', '/customers/lookup.json', lambda: next(lookups))
    stub.route('POST', '/customers.json',
               json_response(201, {'customer': {'id': 700, 'reference': 'oscar-cust-u1'}}))
    stub.route('POST', '/subscriptions.json',
               json_response(201, {'subscription': {
                   'id': 9001, 'state': 'active', 'product_price_in_cents': 29900,
                   'current_period_ends_at': '2026-10-23T00:00:00Z',
                   'product': {'id': 10, 'handle': 'eshop-pro', 'name': 'Pro Plan'},
                   'customer': {'id': 700}}}))
    stub.route('GET', '/subscriptions/lookup.json',
               json_response(200, {'subscription': {
                   'id': 9001, 'state': 'active', 'product_price_in_cents': 29900,
                   'current_period_ends_at': '2026-10-23T00:00:00Z',
                   'product': {'id': 10, 'handle': 'eshop-pro', 'name': 'Pro Plan'},
                   'customer': {'id': 700}}}))
    return stub


class ServiceTestBase(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='shopper', email='shopper@example.com', password='pw-123456789',
            first_name='Sam', last_name='Shopper')

    def patch_client(self, stub):
        patcher = mock.patch.object(services, 'get_client', return_value=make_client(stub))
        patcher.start()
        self.addCleanup(patcher.stop)
        return stub


class ListPlansTests(ServiceTestBase):
    def test_lists_plans_with_handles(self):
        self.patch_client(happy_stub())
        plans = services.list_plans()
        handles = [p['planHandle'] for p in plans]
        self.assertEqual(handles, ['eshop-pro', 'basic-plan'])
        self.assertEqual(plans[0]['priceInCents'], 29900)
        self.assertEqual(plans[0]['intervalUnit'], 'month')


class EnsureCustomerTests(ServiceTestBase):
    def test_reuses_existing_customer_without_creating(self):
        stub = RouteStub()
        stub.route('GET', '/customers/lookup.json',
                   json_response(200, {'customer': {'id': 700, 'reference': 'oscar-cust-u1'}}))
        self.patch_client(stub)
        self.assertEqual(services.ensure_customer(self.user), 700)
        self.assertEqual(stub.count('POST', '/customers.json'), 0)

    def test_creates_customer_when_absent(self):
        stub = RouteStub()
        stub.route('GET', '/customers/lookup.json', json_response(404, {'errors': ['x']}))
        stub.route('POST', '/customers.json',
                   json_response(201, {'customer': {'id': 701}}))
        self.patch_client(stub)
        self.assertEqual(services.ensure_customer(self.user), 701)
        self.assertEqual(stub.count('POST', '/customers.json'), 1)


class SubscribeTests(ServiceTestBase):
    def test_creates_subscription_and_returns_id_top_level(self):
        self.patch_client(happy_stub())
        payload, status = services.subscribe(self.user, 'eshop-pro')
        self.assertEqual(status, 201)
        self.assertEqual(payload['subscriptionId'], 9001)
        self.assertEqual(payload['state'], 'active')
        self.assertEqual(payload['planHandle'], 'eshop-pro')
        self.assertEqual(payload['status'], 'done')
        self.assertEqual(payload['nextBillingDate'], '2026-10-23T00:00:00+00:00')
        row = MaxioSubscription.objects.get(reference='oscar-sub-u1-eshop-pro')
        self.assertEqual(row.maxio_subscription_id, 9001)
        self.assertEqual(row.status, 'done')

    def test_second_subscribe_is_idempotent(self):
        stub = self.patch_client(happy_stub())
        services.subscribe(self.user, 'eshop-pro')
        payload, status = services.subscribe(self.user, 'eshop-pro')
        self.assertEqual(status, 200)
        self.assertEqual(payload['subscriptionId'], 9001)
        # The provider create was called exactly once across both attempts.
        self.assertEqual(stub.count('POST', '/subscriptions.json'), 1)
        self.assertEqual(MaxioSubscription.objects.filter(user=self.user).count(), 1)

    def test_in_flight_claim_returns_202(self):
        self.patch_client(happy_stub())
        MaxioSubscription.objects.create(
            user=self.user, plan_handle='eshop-pro',
            reference='oscar-sub-u1-eshop-pro', status='sending')
        payload, status = services.subscribe(self.user, 'eshop-pro')
        self.assertEqual(status, 202)
        self.assertEqual(payload['status'], 'sending')

    def test_unknown_plan_is_rejected(self):
        self.patch_client(happy_stub())
        with self.assertRaises(MaxioError) as ctx:
            services.subscribe(self.user, 'no-such-plan')
        self.assertEqual(ctx.exception.status_code, 404)


class ErrorBoundaryTests(ServiceTestBase):
    def _plans_stub_with_family_error(self, response):
        stub = RouteStub()
        stub.route('GET', '/product_families.json', response)
        return stub

    def test_credentials_failure_becomes_502(self):
        self.patch_client(self._plans_stub_with_family_error(
            json_response(401, {'error': 'unauthorized'})))
        with self.assertRaises(MaxioError) as ctx:
            services.list_plans()
        self.assertEqual(ctx.exception.status_code, 502)

    def test_rate_limit_becomes_503(self):
        self.patch_client(self._plans_stub_with_family_error(
            json_response(429, {'error': 'slow down'})))
        with self.assertRaises(MaxioError) as ctx:
            services.list_plans()
        self.assertEqual(ctx.exception.status_code, 503)

    def test_refused_connection_is_502_known(self):
        self.patch_client(self._plans_stub_with_family_error(
            httpx.ConnectError('refused')))
        with self.assertRaises(MaxioError) as ctx:
            services.list_plans()
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertFalse(ctx.exception.outcome_unknown)

    def test_read_timeout_on_write_is_504_unknown(self):
        # Customer lookup 404, then create times out mid-flight: may have landed.
        stub = RouteStub()
        stub.route('GET', '/customers/lookup.json', json_response(404, {'errors': ['x']}))
        stub.route('POST', '/customers.json', httpx.ReadTimeout('no reply'))
        self.patch_client(stub)
        with self.assertRaises(MaxioError) as ctx:
            services.ensure_customer(self.user)
        self.assertEqual(ctx.exception.status_code, 504)
        self.assertTrue(ctx.exception.outcome_unknown)


class ViewTests(ServiceTestBase):
    def test_endpoints_require_authentication(self):
        for url in ('/api/subscription-plans', '/api/my-subscriptions'):
            self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.client.post('/api/subscriptions').status_code, 401)

    def test_plans_endpoint_returns_json(self):
        self.patch_client(happy_stub())
        self.client.force_login(self.user)
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['plans']), 2)

    def test_subscribe_endpoint_returns_subscription_id(self):
        self.patch_client(happy_stub())
        self.client.force_login(self.user)
        response = self.client.post(
            '/api/subscriptions', data=json.dumps({'planHandle': 'eshop-pro'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['subscriptionId'], 9001)
