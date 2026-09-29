"""
Tests for the subscription billing API.

Maxio is faked at the SDK's transport seam (``custom_http_client``), so the
real request-building and decoding pipeline runs and no network is used.

Run with: ``sandbox/manage.py test apps.subscriptions`` (from ``sandbox/``).
"""
import json
from dataclasses import dataclass, field
from datetime import timedelta
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings
from django.utils import timezone
from maxio_advanced_billing.core import JsonBody

from . import maxio
from .models import MaxioSubscriptionEnrollment as Enrollment

FAMILY = 'test-family'
MAXIO_SETTINGS = dict(
    MAXIO_API_KEY='test-key',
    MAXIO_SITE_SUBDOMAIN='test-site',
    MAXIO_DEFAULT_PRODUCT_FAMILY=FAMILY,
    MAXIO_BASE_URL='',
    MAXIO_ENVIRONMENT='us',
    MAXIO_CUSTOMER_REFERENCE_PREFIX='test-user-',
)


@dataclass
class StubResponse:
    status_code: int = 200
    headers: dict = field(default_factory=dict)
    chunks: list = field(default_factory=list)
    url: str = 'https://test-site.chargify.com/'
    closed: bool = False

    def iter_bytes(self, chunk_size):
        yield from self.chunks
        self.close()

    def read(self):
        self.close()
        return b''.join(self.chunks)

    def close(self):
        self.closed = True


def respond(status, body=None):
    chunks = [] if body is None else [json.dumps(body).encode()]
    return StubResponse(status_code=status, headers={'content-type': 'application/json'}, chunks=chunks)


class StubTransport:
    """Answers requests from (method, path fragment) routes, in order per route."""

    def __init__(self):
        self.routes = []
        self.requests = []

    def on(self, method, fragment, response):
        self.routes.append((method, fragment, response))
        return self

    def send(self, request):
        self.requests.append(request)
        for i, (method, fragment, response) in enumerate(self.routes):
            if method == request.method and fragment in request.url:
                del self.routes[i]
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError('Unexpected request %s %s' % (request.method, request.url))

    def close(self):
        pass

    def sent(self, method, fragment):
        return [r for r in self.requests if r.method == method and fragment in r.url]


def product(handle, price=2900, family=FAMILY, archived=False, pid=11):
    body = {
        'id': pid, 'name': handle.title(), 'handle': handle, 'description': 'desc',
        'price_in_cents': price, 'interval': 1, 'interval_unit': 'month',
        'require_credit_card': False, 'product_family': {'id': 1, 'handle': family},
    }
    if archived:
        body['archived_at'] = '2026-01-01T00:00:00Z'
    return {'product': body}


def customer(cid=500, reference='test-user-1'):
    return {'customer': {'id': cid, 'reference': reference, 'email': 'a@example.com',
                         'first_name': 'Ann', 'last_name': 'Buyer'}}


def subscription(sid=900, handle='pro', state='active', reference='test-user-1', cid=500):
    return {'subscription': {
        'id': sid, 'state': state, 'product_price_in_cents': 29900, 'currency': 'USD',
        'next_assessment_at': '2026-10-29T12:00:00Z', 'current_period_ends_at': '2026-10-29T12:00:00Z',
        'activated_at': '2026-09-29T12:00:00Z', 'created_at': '2026-09-29T12:00:00Z',
        'product': product(handle, price=29900)['product'],
        'customer': customer(cid, reference)['customer'],
    }}


@override_settings(**MAXIO_SETTINGS)
class ApiTestCase(TestCase):

    def setUp(self):
        self.user = User.objects.create_user('ann', 'a@example.com', 'secret-pass-123')
        self.user_reference = 'test-user-%s' % self.user.pk
        self.transport = StubTransport()
        # retry_options=0 so a stubbed failure is seen exactly once.
        client = maxio.build_client(custom_http_client=self.transport, retry_options=0)
        self.client_context = maxio.use_client(client)
        self.client_context.__enter__()
        self.addCleanup(self.client_context.__exit__, None, None, None)
        self.client.force_login(self.user)

    def subscribe(self, handle='pro'):
        return self.client.post('/api/subscriptions', data=json.dumps({'planHandle': handle}),
                                content_type='application/json')


class AuthTests(ApiTestCase):

    def test_endpoints_require_session_login(self):
        self.client.logout()
        for method, url in [('get', '/api/subscription-plans'), ('post', '/api/subscriptions'),
                            ('get', '/api/my-subscriptions'), ('get', '/api/billing-customer')]:
            response = getattr(self.client, method)(url)
            self.assertEqual(response.status_code, 401, url)
        self.assertEqual(self.transport.requests, [])

    def test_request_carries_basic_auth_and_site_host(self):
        self.transport.on('GET', '/product_families/', respond(200, []))
        self.client.get('/api/subscription-plans')
        request = self.transport.requests[0]
        self.assertTrue(request.url.startswith('https://test-site.chargify.com/'))
        self.assertTrue(request.headers['authorization'].startswith('Basic '))


class PlanTests(ApiTestCase):

    def test_lists_active_plans_of_the_configured_family(self):
        self.transport.on('GET', '/product_families/', respond(200, [
            product('basic', 2900), product('pro', 29900, pid=12), product('old', archived=True, pid=13)]))

        response = self.client.get('/api/subscription-plans')

        self.assertEqual(response.status_code, 200)
        plans = response.json()['plans']
        self.assertEqual([p['planHandle'] for p in plans], ['basic', 'pro'])
        self.assertEqual(plans[1]['price'], '299.00')
        self.assertEqual(plans[1]['intervalUnit'], 'month')
        self.assertIn('handle%3Atest-family', self.transport.requests[0].url.replace(':', '%3A'))

    def test_maxio_credentials_rejected_is_our_502(self):
        self.transport.on('GET', '/product_families/', respond(401, {'errors': ['bad key']}))
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 502)


class BillingCustomerTests(ApiTestCase):

    def test_ensure_creates_customer_with_deterministic_reference(self):
        self.transport.on('GET', '/customers/lookup.json', respond(404))
        self.transport.on('POST', '/customers.json', respond(201, customer(reference=self.user_reference)))

        response = self.client.post('/api/billing-customer')

        self.assertEqual(response.status_code, 201)
        body = self.transport.sent('POST', '/customers.json')[0].body
        self.assertIsInstance(body, JsonBody)
        self.assertEqual(body.value['customer']['reference'], self.user_reference)
        self.assertEqual(body.value['customer']['email'], 'a@example.com')

    def test_ensure_is_idempotent_when_customer_exists(self):
        self.transport.on('GET', '/customers/lookup.json', respond(200, customer(reference=self.user_reference)))
        response = self.client.post('/api/billing-customer')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['created'])
        self.assertEqual(self.transport.sent('POST', '/customers.json'), [])

    def test_create_race_rereads_by_reference(self):
        self.transport.on('GET', '/customers/lookup.json', respond(404))
        self.transport.on('POST', '/customers.json', respond(422, {'errors': {'reference': ['must be unique']}}))
        self.transport.on('GET', '/customers/lookup.json', respond(200, customer(reference=self.user_reference)))
        response = self.client.post('/api/billing-customer')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['customer']['customerId'], 500)


class SubscribeTests(ApiTestCase):

    def stub_new_customer(self):
        self.transport.on('GET', '/products/handle/pro.json', respond(200, product('pro', 29900)))
        self.transport.on('GET', '/customers/lookup.json', respond(404))
        self.transport.on('POST', '/customers.json', respond(201, customer(reference=self.user_reference)))
        self.transport.on('GET', '/customers/500/subscriptions.json', respond(200, []))

    def test_subscribe_creates_customer_and_subscription(self):
        self.stub_new_customer()
        self.transport.on('POST', '/subscriptions.json', respond(201, subscription(reference=self.user_reference)))

        response = self.subscribe()

        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data['subscriptionId'], 900)
        self.assertEqual(data['subscription']['state'], 'active')
        self.assertEqual(data['subscription']['planHandle'], 'pro')
        self.assertEqual(data['subscription']['price'], '299.00')
        self.assertEqual(data['subscription']['nextBillingAt'], '2026-10-29T12:00:00+00:00')
        sent = self.transport.sent('POST', '/subscriptions.json')[0].body.value['subscription']
        self.assertEqual(sent['product_handle'], 'pro')
        self.assertEqual(sent['customer_id'], 500)
        # No card is captured, so Maxio must not try to charge one at signup.
        self.assertEqual(sent['payment_collection_method'], 'remittance')
        enrollment = Enrollment.objects.get()
        self.assertEqual((enrollment.status, enrollment.maxio_subscription_id), (Enrollment.ACTIVE, 900))

    def test_repeat_subscribe_returns_existing_subscription(self):
        Enrollment.objects.create(user=self.user, plan_handle='pro', status=Enrollment.ACTIVE,
                                  maxio_subscription_id=900, maxio_customer_id=500)
        self.transport.on('GET', '/products/handle/pro.json', respond(200, product('pro', 29900)))
        self.transport.on('GET', '/subscriptions/900.json', respond(200, subscription(reference=self.user_reference)))

        response = self.subscribe()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['subscriptionId'], 900)
        self.assertFalse(response.json()['created'])
        self.assertEqual(self.transport.sent('POST', '/subscriptions.json'), [])

    def test_in_flight_subscribe_is_a_conflict(self):
        Enrollment.objects.create(user=self.user, plan_handle='pro', status=Enrollment.PENDING)
        self.transport.on('GET', '/products/handle/pro.json', respond(200, product('pro', 29900)))
        response = self.subscribe()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.transport.sent('POST', '/subscriptions.json'), [])

    def test_stale_pending_claim_is_reconciled_not_duplicated(self):
        stale = Enrollment.objects.create(user=self.user, plan_handle='pro', status=Enrollment.PENDING)
        Enrollment.objects.filter(pk=stale.pk).update(date_updated=timezone.now() - timedelta(hours=1))
        self.transport.on('GET', '/products/handle/pro.json', respond(200, product('pro', 29900)))
        self.transport.on('GET', '/customers/lookup.json', respond(200, customer(reference=self.user_reference)))
        self.transport.on('GET', '/customers/500/subscriptions.json',
                          respond(200, [subscription(reference=self.user_reference)]))

        response = self.subscribe()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['subscriptionId'], 900)
        self.assertEqual(self.transport.sent('POST', '/subscriptions.json'), [])
        self.assertEqual(Enrollment.objects.get(pk=stale.pk).status, Enrollment.FAILED)

    def test_ended_subscription_allows_resubscribe(self):
        Enrollment.objects.create(user=self.user, plan_handle='pro', status=Enrollment.ACTIVE,
                                  maxio_subscription_id=800)
        self.transport.on('GET', '/subscriptions/800.json',
                          respond(200, subscription(sid=800, state='canceled', reference=self.user_reference)))
        self.stub_new_customer()
        self.transport.on('POST', '/subscriptions.json', respond(201, subscription(reference=self.user_reference)))

        response = self.subscribe()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Enrollment.objects.get(maxio_subscription_id=800).status, Enrollment.ENDED)

    def test_unknown_plan_is_404(self):
        self.transport.on('GET', '/products/handle/nope.json', respond(404))
        self.assertEqual(self.subscribe('nope').status_code, 404)

    def test_plan_from_another_family_is_404(self):
        self.transport.on('GET', '/products/handle/pro.json', respond(200, product('pro', family='other')))
        self.assertEqual(self.subscribe().status_code, 404)

    def test_missing_plan_handle_is_400(self):
        response = self.client.post('/api/subscriptions', data='{}', content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_maxio_rejection_passes_status_and_releases_claim(self):
        self.stub_new_customer()
        self.transport.on('POST', '/subscriptions.json', respond(422, {'errors': ['Product is archived']}))

        response = self.subscribe()

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error'], 'Product is archived')
        self.assertEqual(Enrollment.objects.get().status, Enrollment.FAILED)

    def test_truncated_create_response_is_outcome_unknown(self):
        self.stub_new_customer()
        self.transport.on('POST', '/subscriptions.json', respond(201, {}))
        response = self.subscribe()
        self.assertEqual(response.status_code, 502)
        self.assertTrue(response.json()['outcomeUnknown'])

    def test_unsent_and_unanswered_writes_are_different_failures(self):
        self.stub_new_customer()
        self.transport.on('POST', '/subscriptions.json', httpx.ConnectError('refused'))
        unsent = self.subscribe()

        self.stub_new_customer()
        self.transport.routes = [r for r in self.transport.routes if r[1] != '/customers/lookup.json']
        self.transport.on('GET', '/customers/lookup.json', respond(200, customer(reference=self.user_reference)))
        self.transport.on('POST', '/subscriptions.json', httpx.ReadTimeout('no reply'))
        unknown = self.subscribe()

        self.assertEqual((unsent.status_code, unsent.json()['outcomeUnknown']), (502, False))
        self.assertEqual((unknown.status_code, unknown.json()['outcomeUnknown']), (504, True))


class MySubscriptionsTests(ApiTestCase):

    def test_no_customer_means_no_subscriptions(self):
        self.transport.on('GET', '/customers/lookup.json', respond(404))
        response = self.client.get('/api/my-subscriptions')
        self.assertEqual(response.json(), {'subscriptions': []})

    def test_lists_subscriptions_from_maxio(self):
        self.transport.on('GET', '/customers/lookup.json', respond(200, customer(reference=self.user_reference)))
        self.transport.on('GET', '/customers/500/subscriptions.json',
                          respond(200, [subscription(reference=self.user_reference)]))
        subs = self.client.get('/api/my-subscriptions').json()['subscriptions']
        self.assertEqual([(s['subscriptionId'], s['planHandle'], s['state']) for s in subs], [(900, 'pro', 'active')])

    def test_lookup_failure_is_not_reported_as_no_subscriptions(self):
        self.transport.on('GET', '/customers/lookup.json', respond(500))
        response = self.client.get('/api/my-subscriptions')
        self.assertEqual(response.status_code, 502)

    def test_someone_elses_subscription_is_404(self):
        self.transport.on('GET', '/subscriptions/900.json', respond(200, subscription(reference='someone-else')))
        self.assertEqual(self.client.get('/api/subscriptions/900').status_code, 404)

    def test_own_subscription_detail(self):
        self.transport.on('GET', '/subscriptions/900.json', respond(200, subscription(reference=self.user_reference)))
        response = self.client.get('/api/subscriptions/900')
        self.assertEqual(response.json()['subscription']['subscriptionId'], 900)


class ConfigurationTests(TestCase):

    @override_settings(**dict(MAXIO_SETTINGS, MAXIO_BASE_URL='http://localhost:9999/maxio'))
    def test_base_url_override_is_used_verbatim(self):
        self.assertEqual(maxio.server_config(), {'production': {'us': {'base_url': 'http://localhost:9999/maxio'}}})

    @override_settings(**dict(MAXIO_SETTINGS, MAXIO_ENVIRONMENT='EU'))
    def test_environment_is_case_insensitive(self):
        self.assertEqual(maxio.server_config(), {'production': {'eu': {'site': 'test-site'}}})

    @override_settings(**dict(MAXIO_SETTINGS, MAXIO_ENVIRONMENT='mars'))
    def test_unknown_environment_fails_fast(self):
        with self.assertRaises(ImproperlyConfigured):
            maxio.server_config()

    @override_settings(**dict(MAXIO_SETTINGS, MAXIO_PAYMENT_COLLECTION_METHOD='cash'))
    def test_unknown_collection_method_fails_fast(self):
        from .services import collection_method
        with self.assertRaises(ImproperlyConfigured):
            collection_method()

    @override_settings(**dict(MAXIO_SETTINGS, MAXIO_API_KEY=''))
    def test_missing_api_key_fails_fast(self):
        with self.assertRaises(ImproperlyConfigured):
            maxio.build_client()

    @override_settings(**MAXIO_SETTINGS)
    def test_client_is_built_once_and_reused(self):
        with mock.patch.object(maxio, '_client', None):
            self.assertIs(maxio.get_client(), maxio.get_client())
            maxio.close_client()
