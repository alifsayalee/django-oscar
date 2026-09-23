"""
Tests for the Maxio subscription API.

Maxio is faked at the SDK's transport seam (``custom_http_client``), so the real
SDK builds every request and decodes every response. Run with:

    cd sandbox && python manage.py test apps.subscriptions
"""

import json
import re
from datetime import timedelta
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from maxio_advanced_billing.core import HttpResponse
from maxio_advanced_billing.models import (
    Customer, CustomerResponse, Product, ProductFamily, ProductResponse, Subscription, SubscriptionResponse)

from . import provider
from .maxio.client import MaxioConfig, MaxioConfigurationError, build_client
from .maxio.gateway import MaxioGateway
from .models import MaxioCustomer, SubscriptionRequest

FAMILY = 'eshop-subscribe'
DUPLICATE = 'Reference: must be unique - that value has been taken.'
PLANS = {
    'eshop-pro': ('Pro Plan', 29900),
    'basic-plan': ('Basic Plan', 2900),
}


def json_response(status, body):
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


def product(handle, family=FAMILY, archived=False):
    name, price = PLANS[handle]
    return Product(
        id=100 + len(handle), handle=handle, name=name, price_in_cents=price, interval=1, interval_unit='month',
        product_family=ProductFamily(handle=family), archived_at='2026-01-01T00:00:00Z' if archived else None)


class FakeMaxio:
    """A stateful stand-in for the Maxio site, speaking the wire format."""

    def __init__(self, prefix=''):
        self.prefix = prefix  # path prefix of a MAXIO_BASE_URL override
        self.requests = []
        self.customers = {}  # reference -> id
        self.subscriptions = {}  # reference -> dict
        self.failures = []  # (method, path regex, HttpResponse | Exception, lands)
        self.state = 'active'
        self.echo_price = None
        self.next_id = 5000

    def fail(self, method, pattern, outcome, lands=False):
        """Make the next matching request fail; with lands=True the write still takes effect."""
        self.failures.append((method, re.compile(pattern), outcome, lands))

    def calls(self, method, pattern):
        return [r for r in self.requests if r.method == method and re.search(pattern, urlsplit(r.url).path)]

    def send(self, request):
        self.requests.append(request)
        path = unquote(urlsplit(request.url).path).removeprefix(self.prefix)
        for i, (method, pattern, outcome, lands) in enumerate(self.failures):
            if method == request.method and pattern.search(path):
                del self.failures[i]
                if lands:
                    self._route(request, path)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        return self._route(request, path)

    def close(self):
        pass

    def _sub_body(self, sub):
        return SubscriptionResponse(subscription=Subscription(**sub)).to_dict()

    def _route(self, request, path):
        routes = (
            ('GET', rf'/product_families/handle:{FAMILY}/products\.json', self._list_products),
            ('GET', r'/products/handle/(.+)\.json', self._read_product),
            ('GET', r'/customers/lookup\.json', self._lookup_customer),
            ('POST', r'/customers\.json', self._create_customer),
            ('POST', r'/subscriptions\.json', self._create_subscription),
            ('GET', r'/subscriptions/lookup\.json', self._lookup_subscription),
            ('GET', r'/customers/(\d+)/subscriptions\.json', self._customer_subscriptions),
        )
        for method, pattern, handler in routes:
            if method == request.method and (match := re.fullmatch(pattern, path)):
                return handler(request, *match.groups())
        return json_response(599, {'unrouted': path})

    @staticmethod
    def _reference(request):
        return parse_qs(urlsplit(request.url).query)['reference'][0]

    def _list_products(self, request):
        return json_response(200, [ProductResponse(product=product(h)).to_dict() for h in PLANS])

    def _read_product(self, request, handle):
        if handle not in PLANS:
            return HttpResponse(status_code=404, headers={})
        return json_response(200, ProductResponse(product=product(handle)).to_dict())

    def _customer_body(self, ref):
        return CustomerResponse(customer=Customer(id=self.customers[ref], reference=ref)).to_dict()

    def _lookup_customer(self, request):
        ref = self._reference(request)
        if ref not in self.customers:
            return HttpResponse(status_code=404, headers={})
        return json_response(200, self._customer_body(ref))

    def _create_customer(self, request):
        ref = request.body.value['customer']['reference']
        if ref in self.customers:
            return json_response(422, {'errors': [DUPLICATE]})
        self.next_id += 1
        self.customers[ref] = self.next_id
        return json_response(201, self._customer_body(ref))

    def _create_subscription(self, request):
        body = request.body.value['subscription']
        ref = body['reference']
        if ref in self.subscriptions:
            return json_response(422, {'errors': [DUPLICATE]})
        self.next_id += 1
        sub = dict(id=self.next_id, state=self.state, reference=ref, currency='USD',
                   product_price_in_cents=self.echo_price or PLANS[body['product_handle']][1],
                   next_assessment_at='2026-10-23T12:00:00Z', customer_id_=body['customer_id'],
                   product=product(body['product_handle']))
        self.subscriptions[ref] = sub
        return json_response(201, self._sub_body(self._public(sub)))

    def _lookup_subscription(self, request):
        ref = self._reference(request)
        if ref not in self.subscriptions:
            return HttpResponse(status_code=404, headers={})
        return json_response(200, self._sub_body(self._public(self.subscriptions[ref])))

    def _customer_subscriptions(self, request, customer_id):
        subs = [self._public(s) for s in self.subscriptions.values() if s['customer_id_'] == int(customer_id)]
        return json_response(200, [self._sub_body(s) for s in subs])

    @staticmethod
    def _public(sub):
        return {k: v for k, v in sub.items() if not k.endswith('_')}


class SubscriptionApiTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.maxio = FakeMaxio()
        config = MaxioConfig(api_key='test-key', site_subdomain='test-site', product_family=FAMILY)
        gateway = MaxioGateway(build_client(config, transport=self.maxio), FAMILY, sleep=lambda s: None)
        previous = provider.set_gateway(gateway)
        self.addCleanup(provider.set_gateway, previous)
        self.user = get_user_model().objects.create_user(
            username='shopper', email='shopper@example.com', password='a-long-password-123')
        self.client.force_login(self.user)

    def subscribe(self, plan='eshop-pro'):
        return self.client.post('/api/subscriptions', data=json.dumps({'planHandle': plan}),
                                content_type='application/json')


class PlanTests(SubscriptionApiTestCase):
    def test_lists_family_plans_with_handles(self):
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 200)
        plans = {p['planHandle']: p for p in response.json()['plans']}
        self.assertEqual(set(plans), {'eshop-pro', 'basic-plan'})
        self.assertEqual(plans['eshop-pro']['price'], {'amountInCents': 29900, 'amount': '299.00', 'currency': None})
        self.assertFalse(response.json()['truncated'])
        request = self.maxio.requests[0]
        self.assertEqual(request.headers['authorization'][:6], 'Basic ')
        self.assertTrue(request.url.startswith('https://test-site.chargify.com/'))

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get('/api/subscription-plans').status_code, 401)
        self.assertEqual(self.subscribe().status_code, 401)
        self.assertEqual(self.client.get('/api/my-subscriptions').status_code, 401)
        self.assertEqual(self.maxio.requests, [])

    def test_provider_auth_failure_is_ours_not_the_callers(self):
        self.maxio.fail('GET', r'/products\.json$', json_response(401, {'errors': ['bad key']}))
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 502)


class SubscribeTests(SubscriptionApiTestCase):
    def test_subscribe_creates_customer_and_subscription(self):
        response = self.subscribe()
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertIsInstance(body['subscriptionId'], int)
        self.assertEqual(body['status'], 'done')
        self.assertEqual(body['subscription']['state'], 'active')
        self.assertEqual(body['subscription']['planHandle'], 'eshop-pro')
        self.assertEqual(body['subscription']['nextBillingAt'], '2026-10-23T12:00:00+00:00')

        create = self.maxio.calls('POST', r'^/subscriptions\.json$')[0].body.value['subscription']
        customer = MaxioCustomer.objects.get(user=self.user)
        self.assertEqual(create['product_handle'], 'eshop-pro')
        self.assertEqual(create['customer_id'], customer.maxio_customer_id)
        self.assertEqual(create['payment_collection_method'], 'remittance')
        self.assertEqual(create['reference'], SubscriptionRequest.objects.get().reference)
        sent_customer = self.maxio.calls('POST', r'^/customers\.json$')[0].body.value['customer']
        self.assertEqual(sent_customer['email'], 'shopper@example.com')
        self.assertEqual(sent_customer['reference'], customer.reference)

    def test_double_submit_creates_one_customer_and_one_subscription(self):
        first = self.subscribe()
        second = self.subscribe()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()['subscriptionId'], second.json()['subscriptionId'])
        self.assertEqual(len(self.maxio.calls('POST', r'^/customers\.json$')), 1)
        self.assertEqual(len(self.maxio.calls('POST', r'^/subscriptions\.json$')), 1)

    def test_request_in_flight_is_not_sent_twice(self):
        customer = MaxioCustomer.objects.create(user=self.user, reference='c-ref', status='done', maxio_customer_id=1)
        SubscriptionRequest.objects.create(user=self.user, plan_handle='eshop-pro', reference='in-flight',
                                           status='sending', sent_at=timezone.now())
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertIsNone(response.json()['subscriptionId'])
        self.assertEqual(self.maxio.calls('POST', r'^/subscriptions\.json$'), [])
        self.assertTrue(customer.pk)

    def test_second_plan_is_a_separate_subscription_for_the_same_customer(self):
        self.subscribe('eshop-pro')
        response = self.subscribe('basic-plan')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(self.maxio.calls('POST', r'^/customers\.json$')), 1)

    def test_unknown_plan_is_rejected_before_anything_is_created(self):
        response = self.subscribe('no-such-plan')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['code'], 'plan_not_available')
        self.assertEqual(self.maxio.calls('POST', r'.*'), [])

    def test_missing_plan_handle(self):
        response = self.client.post('/api/subscriptions', data='{}', content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_provider_rejection_passes_through_and_releases_the_claim(self):
        self.maxio.fail('POST', r'^/subscriptions\.json$',
                        json_response(422, {'errors': ['No payment method was on file']}))
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['details'], ['No payment method was on file'])
        self.assertEqual(SubscriptionRequest.objects.get().status, 'failed')
        self.assertEqual(self.subscribe().status_code, 201)  # the plan is free to try again

    def test_unsent_is_not_the_same_failure_as_unknown(self):
        self.maxio.fail('POST', r'^/subscriptions\.json$', httpx.ConnectError('refused'))
        unsent = self.subscribe()
        self.assertEqual(unsent.status_code, 502)
        self.assertFalse(unsent.json()['error']['outcomeUnknown'])
        self.assertEqual(SubscriptionRequest.objects.get().status, 'failed')
        self.assertEqual(self.maxio.calls('GET', r'^/subscriptions/lookup\.json$'), [])

        self.maxio.fail('POST', r'^/subscriptions\.json$', httpx.ReadTimeout('no reply'))
        unknown = self.subscribe()
        self.assertEqual(unknown.status_code, 504)
        self.assertEqual(unknown.json()['status'], 'unknown')
        self.assertEqual(len(self.maxio.calls('GET', r'^/subscriptions/lookup\.json$')), 1)

    def test_timeout_that_landed_is_reconciled_by_reference(self):
        self.maxio.fail('POST', r'^/subscriptions\.json$', httpx.ReadTimeout('no reply'), lands=True)
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['subscriptionId'], list(self.maxio.subscriptions.values())[0]['id'])
        self.assertEqual(len(self.maxio.calls('POST', r'^/subscriptions\.json$')), 1)

    def test_unknown_outcome_is_resolved_on_the_next_attempt_under_the_same_reference(self):
        self.maxio.fail('POST', r'^/subscriptions\.json$', httpx.ReadTimeout('no reply'))
        self.assertEqual(self.subscribe().status_code, 504)
        reference = SubscriptionRequest.objects.get().reference
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        creates = self.maxio.calls('POST', r'^/subscriptions\.json$')
        self.assertEqual([c.body.value['subscription']['reference'] for c in creates], [reference, reference])
        self.assertEqual(SubscriptionRequest.objects.count(), 1)

    def test_duplicate_reference_answer_is_a_landing(self):
        self.maxio.fail('POST', r'^/subscriptions\.json$',
                        json_response(422, {'errors': [DUPLICATE]}),
                        lands=True)
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(SubscriptionRequest.objects.get().status, 'done')

    def test_truncated_success_body_is_not_reported_as_success(self):
        self.maxio.fail('POST', r'^/subscriptions\.json$', json_response(201, {}))
        response = self.subscribe()
        self.assertEqual(response.status_code, 504)
        self.assertEqual(SubscriptionRequest.objects.get().status, 'unknown')

    def test_undecodable_success_body_is_an_unknown_outcome(self):
        self.maxio.fail('POST', r'^/subscriptions\.json$', json_response(201, {'subscription': {'id': 'nope'}}))
        self.assertEqual(self.subscribe().status_code, 504)

    def test_pending_state_is_accepted_not_done(self):
        self.maxio.state = 'pending'
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['status'], 'pending')

    def test_failed_to_create_with_an_id_is_not_done(self):
        self.maxio.state = 'failed_to_create'
        response = self.subscribe()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(SubscriptionRequest.objects.get().status, 'failed')

    def test_an_unlisted_state_is_unknown_not_done(self):
        self.maxio.state = 'something_new'
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['status'], 'needs_review')

    def test_echoed_price_mismatch_needs_review(self):
        self.maxio.echo_price = 100
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['status'], 'needs_review')

    def test_customer_create_timeout_that_landed_is_reused(self):
        self.maxio.fail('POST', r'^/customers\.json$', httpx.ReadTimeout('no reply'), lands=True)
        self.assertEqual(self.subscribe().status_code, 201)
        self.assertEqual(len(self.maxio.customers), 1)
        self.assertEqual(MaxioCustomer.objects.get().status, 'done')

    def test_customer_create_unknown_blocks_then_resolves(self):
        self.maxio.fail('POST', r'^/customers\.json$', httpx.ReadTimeout('no reply'))
        self.assertEqual(self.subscribe().status_code, 504)
        self.assertEqual(MaxioCustomer.objects.get().status, 'unknown')
        self.assertEqual(self.subscribe().status_code, 201)
        self.assertEqual(len(self.maxio.customers), 1)

    def test_stale_sending_customer_claim_is_taken_over(self):
        row = MaxioCustomer.objects.create(user=self.user, reference='stale-ref', status='sending')
        MaxioCustomer.objects.filter(pk=row.pk).update(updated_at=timezone.now() - timedelta(minutes=5))
        self.assertEqual(self.subscribe().status_code, 201)
        self.assertIn('stale-ref', self.maxio.customers)


class MySubscriptionTests(SubscriptionApiTestCase):
    def test_reads_back_from_maxio(self):
        subscription_id = self.subscribe().json()['subscriptionId']
        response = self.client.get('/api/my-subscriptions')
        self.assertEqual(response.status_code, 200)
        [entry] = response.json()['subscriptions']
        self.assertEqual(entry['subscriptionId'], subscription_id)
        self.assertEqual(entry['status'], 'active')
        self.assertEqual(entry['planHandle'], 'eshop-pro')
        self.assertEqual(entry['price']['amount'], '299.00')

    def test_no_customer_yet_is_empty(self):
        response = self.client.get('/api/my-subscriptions')
        self.assertEqual(response.json(), {'subscriptions': []})
        self.assertEqual(self.maxio.requests, [])

    def test_ended_subscription_frees_the_plan(self):
        self.subscribe()
        list(self.maxio.subscriptions.values())[0]['state'] = 'canceled'
        self.assertEqual(self.client.get('/api/my-subscriptions').json()['subscriptions'][0]['status'], 'ended')
        self.assertEqual(SubscriptionRequest.objects.get().status, 'ended')
        self.assertEqual(self.subscribe().status_code, 201)

    def test_provider_outage_is_an_error_not_an_empty_list(self):
        self.subscribe()
        self.maxio.fail('GET', r'/subscriptions\.json$', httpx.ConnectError('refused'))
        self.maxio.fail('GET', r'/subscriptions\.json$', httpx.ConnectError('refused'))
        response = self.client.get('/api/my-subscriptions')
        self.assertEqual(response.status_code, 502)


class ConfigTests(TestCase):
    def test_base_url_is_used_verbatim(self):
        maxio = FakeMaxio(prefix='/maxio')
        config = MaxioConfig(api_key='k', site_subdomain='', product_family=FAMILY,
                             base_url='http://localhost:9999/maxio')
        MaxioGateway(build_client(config, transport=maxio), FAMILY).list_plans()
        self.assertTrue(maxio.requests[0].url.startswith('http://localhost:9999/maxio/product_families/'))

    def test_unknown_environment_fails_loudly(self):
        with self.assertRaises(MaxioConfigurationError):
            build_client(MaxioConfig(api_key='k', site_subdomain='s', product_family=FAMILY, environment='mars'))

    def test_missing_credentials_fail_loudly(self):
        with self.assertRaises(MaxioConfigurationError):
            build_client(MaxioConfig(api_key='', site_subdomain='s', product_family=FAMILY))

    @override_settings(MAXIO_API_KEY='')
    def test_unconfigured_site_answers_503(self):
        previous = provider.set_gateway(None)
        self.addCleanup(provider.set_gateway, previous)
        user = get_user_model().objects.create_user(username='u', email='u@example.com', password='x' * 12)
        self.client.force_login(user)
        self.assertEqual(self.client.get('/api/subscription-plans').status_code, 503)
