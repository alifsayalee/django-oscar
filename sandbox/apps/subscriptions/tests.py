"""
Tests for the Maxio subscription API.

The SDK's transport is the seam: a stub transport answers each request the
real SDK builds, so request building, decoding and error mapping all run.

Run from sandbox/:  manage.py test apps.subscriptions
"""
import json
from datetime import timedelta
from urllib.parse import unquote

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from maxio_advanced_billing.core import HttpResponse

from . import billing
from .maxio_client import build_client, use_client
from .models import MaxioWriteClaim

MAXIO_TEST_SETTINGS = dict(
    MAXIO_API_KEY='test-key',
    MAXIO_SITE_SUBDOMAIN='test-site',
    MAXIO_DEFAULT_PRODUCT_FAMILY='test-family',
    MAXIO_BASE_URL='',
    MAXIO_ENVIRONMENT='us',
    MAXIO_TIMEOUT=5.0,
    MAXIO_REFERENCE_PREFIX='test',
)


def json_response(status, body):
    return HttpResponse(
        status_code=status, headers={'content-type': 'application/json'}, content=json.dumps(body).encode())


class StubTransport:
    """The SDK's sync transport protocol. Each queued item is a response, an exception, or a callable(request)."""

    def __init__(self, *items):
        self._items = list(items)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        item = self._items.pop(0)
        if callable(item) and not isinstance(item, HttpResponse):
            item = item(request)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass

    def calls(self, method, path_fragment):
        return [r for r in self.requests if r.method == method and path_fragment in r.url]


PRODUCTS = [
    {'product': {'id': 11, 'handle': 'eshop-pro', 'name': 'Pro Plan', 'price_in_cents': 29900,
                 'interval': 1, 'interval_unit': 'month'}},
    {'product': {'id': 12, 'handle': 'basic-plan', 'name': 'Basic Plan', 'price_in_cents': 2900,
                 'interval': 1, 'interval_unit': 'month'}},
    {'product': {'id': 13, 'handle': 'old-plan', 'name': 'Old', 'price_in_cents': 100,
                 'interval': 1, 'interval_unit': 'month', 'archived_at': '2026-01-01T00:00:00Z'}},
]


def site_response(relationship_invoicing=True):
    return json_response(200, {'site': {
        'id': 1, 'subdomain': 'test-site', 'currency': 'USD', 'test': True,
        'relationship_invoicing_enabled': relationship_invoicing}})


def products_response():
    return json_response(200, PRODUCTS)


def customer_created(request):
    reference = request.body.value['customer']['reference']
    return json_response(201, {'customer': {
        'id': 501, 'reference': reference, 'email': 'shopper@example.com',
        'created_at': '2026-09-24T10:00:00Z'}})


def subscription_body(reference, state='active', handle='eshop-pro', subscription_id=9001):
    return {'subscription': {
        'id': subscription_id, 'state': state, 'reference': reference,
        'product': {'id': 11, 'handle': handle, 'name': 'Pro Plan'},
        'product_price_in_cents': 29900, 'currency': 'USD',
        'next_assessment_at': '2026-10-24T10:00:00Z', 'current_period_ends_at': '2026-10-24T10:00:00Z',
        'created_at': '2026-09-24T10:00:01Z'}}


def subscription_created(state='active', handle='eshop-pro'):
    def respond(request):
        reference = request.body.value['subscription']['reference']
        return json_response(201, subscription_body(reference, state=state, handle=handle))
    return respond


@override_settings(**MAXIO_TEST_SETTINGS)
class SubscriptionApiTestCase(TestCase):

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='shopper', email='shopper@example.com', password='a-long-password-1')
        self.client.force_login(self.user)

    def stub(self, *items):
        transport = StubTransport(*items)
        self.enterContext(use_client(build_client(transport)))
        return transport

    def subscribe(self, plan_handle='eshop-pro'):
        return self.client.post(
            reverse('subscriptions:subscriptions'), data=json.dumps({'planHandle': plan_handle}),
            content_type='application/json')


class PlanTests(SubscriptionApiTestCase):

    def test_lists_active_plans_of_the_configured_family(self):
        transport = self.stub(site_response(), products_response())

        response = self.client.get(reverse('subscriptions:subscription-plans'))

        self.assertEqual(response.status_code, 200)
        plans = response.json()['plans']
        self.assertEqual([p['planHandle'] for p in plans], ['eshop-pro', 'basic-plan'])
        self.assertEqual(plans[0]['price'], '299.00')
        self.assertEqual(plans[0]['currency'], 'USD')
        request = transport.calls('GET', '/products.json')[0]
        self.assertEqual(request.method, 'GET')
        self.assertIn('https://test-site.chargify.com/product_families/', request.url)
        self.assertIn('handle:test-family', unquote(request.url))
        self.assertIn('per_page=200', request.url)
        self.assertTrue(request.headers['authorization'].startswith('Basic '))

    def test_unknown_family_is_our_misconfiguration(self):
        self.stub(site_response(), json_response(404, 'Not found'))
        response = self.client.get(reverse('subscriptions:subscription-plans'))
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['error'], 'billing_misconfigured')

    def test_provider_refusing_our_credentials_is_not_the_callers_fault(self):
        self.stub(json_response(401, {'errors': ['bad key']}))
        response = self.client.get(reverse('subscriptions:subscription-plans'))
        self.assertEqual(response.status_code, 502)

    @override_settings(MAXIO_API_KEY='')
    def test_missing_api_key_is_not_configured(self):
        from . import maxio_client
        previous, maxio_client._client = maxio_client._client, None
        try:
            response = self.client.get(reverse('subscriptions:subscription-plans'))
        finally:
            maxio_client._client = previous
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error'], 'billing_not_configured')


class SubscribeTests(SubscriptionApiTestCase):

    def test_subscribes_and_confirms_plan_price_state_and_next_billing(self):
        transport = self.stub(site_response(), products_response(), customer_created, subscription_created())

        response = self.subscribe()

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['subscriptionId'], 9001)
        self.assertEqual(body['outcome'], 'done')
        self.assertEqual(body['state'], 'active')
        self.assertEqual(body['price'], '299.00')
        self.assertEqual(body['currency'], 'USD')
        self.assertTrue(body['nextBillingAt'].startswith('2026-10-24'))
        create = transport.calls('POST', '/subscriptions.json')[0]
        self.assertEqual(create.body.value['subscription']['product_handle'], 'eshop-pro')
        self.assertEqual(create.body.value['subscription']['customer_id'], 501)
        # No payment method is captured, so the subscription is invoice-collected.
        self.assertEqual(create.body.value['subscription']['payment_collection_method'], 'remittance')

    def test_a_legacy_statements_site_collects_by_invoice(self):
        transport = self.stub(site_response(relationship_invoicing=False), products_response(), customer_created,
                              subscription_created())
        self.assertEqual(self.subscribe().status_code, 201)
        create = transport.calls('POST', '/subscriptions.json')[0]
        self.assertEqual(create.body.value['subscription']['payment_collection_method'], 'invoice')

    def test_the_same_request_twice_creates_one_customer_and_one_subscription(self):
        transport = self.stub(
            site_response(), products_response(), customer_created, subscription_created(),
            products_response())                      # the second request only re-reads the plans

        first = self.subscribe()
        second = self.subscribe()

        self.assertEqual(len(transport.calls('POST', '/customers.json')), 1)
        self.assertEqual(len(transport.calls('POST', '/subscriptions.json')), 1)
        sent = transport.calls('POST', '/subscriptions.json')[0].body.value['subscription']['reference']
        self.assertEqual(sent, billing.subscription_reference(self.user, 'eshop-pro'))
        customer_ref = transport.calls('POST', '/customers.json')[0].body.value['customer']['reference']
        self.assertEqual(customer_ref, billing.customer_reference(self.user))
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['subscriptionId'], first.json()['subscriptionId'])
        self.assertTrue(second.json()['replayed'])

    def test_second_plan_reuses_the_existing_customer(self):
        transport = self.stub(
            site_response(), products_response(), customer_created, subscription_created(),
            products_response(), subscription_created(handle='basic-plan'))
        self.subscribe('eshop-pro')
        response = self.subscribe('basic-plan')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(transport.calls('POST', '/customers.json')), 1)

    def test_an_id_with_a_failed_state_is_not_done(self):
        self.stub(site_response(), products_response(), customer_created, subscription_created(state='failed_to_create'))
        response = self.subscribe()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['outcome'], 'failed')
        self.assertEqual(response.json()['subscriptionId'], 9001)

    def test_a_not_yet_state_is_accepted_not_done(self):
        self.stub(site_response(), products_response(), customer_created, subscription_created(state='past_due'))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['outcome'], 'pending')

    def test_an_unlisted_state_is_unknown_not_done(self):
        self.stub(site_response(), products_response(), customer_created, subscription_created(state='something_new'))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['outcome'], 'unknown')

    def test_a_different_plan_coming_back_needs_review(self):
        self.stub(site_response(), products_response(), customer_created, subscription_created(handle='basic-plan'))
        response = self.subscribe()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['outcome'], 'needs_review')

    def test_unknown_plan_is_rejected_before_any_write(self):
        transport = self.stub(site_response(), products_response())
        response = self.subscribe('no-such-plan')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'unknown_plan')
        self.assertIsNone(response.json()['subscriptionId'])
        self.assertEqual([r.method for r in transport.requests], ['GET', 'GET'])   # site, plans: no write

    def test_missing_plan_handle(self):
        response = self.client.post(reverse('subscriptions:subscriptions'), data='{}',
                                    content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('subscriptionId', response.json())

    def test_anonymous_caller_gets_401(self):
        self.client.logout()
        self.assertEqual(self.subscribe().status_code, 401)
        self.assertEqual(self.client.get(reverse('subscriptions:my-subscriptions')).status_code, 401)

    def test_a_request_in_flight_makes_no_provider_call(self):
        transport = self.stub(site_response(), products_response())
        # Another request holds the customer claim and is still waiting on Maxio.
        MaxioWriteClaim.objects.create(
            reference=billing.customer_reference(self.user), kind=MaxioWriteClaim.KIND_CUSTOMER,
            user=self.user, outcome=MaxioWriteClaim.SENDING, claimed_at=timezone.now())

        response = self.subscribe()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['outcome'], 'in_progress')
        self.assertEqual(transport.calls('POST', '/customers.json'), [])

    def test_a_422_is_a_rejection_when_nothing_carries_the_reference(self):
        transport = self.stub(
            site_response(), products_response(), customer_created,
            json_response(422, {'errors': ['Product is invalid']}),
            json_response(404, ''))                    # the lookup by reference
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['details'], ['Product is invalid'])
        self.assertEqual(len(transport.calls('GET', '/subscriptions/lookup.json')), 1)
        # Nothing exists at the provider: the claim is released for a later attempt.
        self.assertFalse(MaxioWriteClaim.objects.filter(kind=MaxioWriteClaim.KIND_SUBSCRIPTION).exists())

    def test_a_422_for_a_reference_that_already_landed_is_adopted(self):
        reference_holder = {}

        def lookup(request):
            return json_response(200, subscription_body(reference_holder['ref']))

        def refuse(request):
            reference_holder['ref'] = request.body.value['subscription']['reference']
            return json_response(422, {'errors': ['Reference has already been taken']})

        self.stub(site_response(), products_response(), customer_created, refuse, lookup)
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['subscriptionId'], 9001)


class TransportFailureTests(SubscriptionApiTestCase):

    def test_unsent_is_not_the_same_failure_as_unknown(self):
        self.stub(site_response(), products_response(), customer_created, httpx.ConnectError('refused'))
        unsent = self.subscribe()
        self.assertEqual((unsent.status_code, unsent.json()['outcomeUnknown']), (502, False))
        # Never sent: the claim is released, nothing to look up.
        self.assertFalse(MaxioWriteClaim.objects.filter(kind=MaxioWriteClaim.KIND_SUBSCRIPTION).exists())

        transport = self.stub(site_response(), products_response(), httpx.ReadTimeout('no reply'), json_response(404, ''))
        unknown = self.subscribe()
        self.assertEqual((unknown.status_code, unknown.json()['outcomeUnknown']), (504, True))
        lookups = transport.calls('GET', '/subscriptions/lookup.json')
        self.assertEqual(len(lookups), 1)
        self.assertIn('reference=' + billing.subscription_reference(self.user, 'eshop-pro'), unquote(lookups[0].url))
        record = MaxioWriteClaim.objects.get(kind=MaxioWriteClaim.KIND_SUBSCRIPTION)
        self.assertEqual(record.outcome, MaxioWriteClaim.UNKNOWN)

    def test_a_timed_out_write_that_landed_is_found_by_its_reference(self):
        def lookup(request):
            return json_response(200, subscription_body(billing.subscription_reference(self.user, 'eshop-pro')))

        self.stub(site_response(), products_response(), customer_created, httpx.ReadTimeout('no reply'), lookup)
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['subscriptionId'], 9001)

    def test_an_unknown_write_is_only_ever_looked_up_never_resent(self):
        self.stub(site_response(), products_response(), customer_created, httpx.ReadTimeout('no reply'), json_response(404, ''))
        self.assertEqual(self.subscribe().status_code, 504)

        def lookup(request):
            return json_response(200, subscription_body(billing.subscription_reference(self.user, 'eshop-pro')))

        transport = self.stub(site_response(), products_response(), lookup)
        retry = self.subscribe()

        self.assertEqual(retry.status_code, 200)             # the earlier request's write, now confirmed
        self.assertEqual(retry.json()['outcome'], 'done')
        self.assertEqual(transport.calls('POST', '/subscriptions.json'), [])

    def test_an_unreadable_success_body_is_looked_up_not_reported_failed(self):
        self.stub(site_response(), products_response(), customer_created, json_response(201, {'subscription': {'state': 'active'}}),
                  json_response(404, ''))
        response = self.subscribe()
        self.assertEqual(response.status_code, 504)
        self.assertTrue(response.json()['outcomeUnknown'])

    def test_a_stale_sending_claim_is_checked_by_lookup(self):
        self.stub(site_response(), products_response(), customer_created, subscription_created())
        self.subscribe()
        record = MaxioWriteClaim.objects.get(kind=MaxioWriteClaim.KIND_SUBSCRIPTION)
        MaxioWriteClaim.objects.filter(pk=record.pk).update(
            outcome=MaxioWriteClaim.SENDING, claimed_at=timezone.now() - timedelta(hours=1))

        def lookup(request):
            return json_response(200, subscription_body(record.reference))

        transport = self.stub(site_response(), products_response(), lookup)
        response = self.subscribe()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(transport.calls('POST', '/subscriptions.json'), [])


class MySubscriptionsTests(SubscriptionApiTestCase):

    def test_reads_back_from_maxio_and_settles_unknown_records(self):
        self.stub(site_response(), products_response(), customer_created, httpx.ReadTimeout('no reply'), json_response(404, ''))
        self.subscribe()
        reference = billing.subscription_reference(self.user, 'eshop-pro')

        transport = self.stub(json_response(200, [subscription_body(reference)]))
        response = self.client.get(reverse('subscriptions:my-subscriptions'))

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['customerId'], 501)
        self.assertEqual(body['subscriptions'][0]['subscriptionId'], 9001)
        self.assertEqual(body['subscriptions'][0]['planHandle'], 'eshop-pro')
        self.assertEqual(body['subscriptions'][0]['state'], 'active')
        self.assertEqual(body['unsettled'], [])
        self.assertIn('/customers/501/subscriptions.json', transport.requests[0].url)
        self.assertEqual(MaxioWriteClaim.objects.get(reference=reference).outcome, MaxioWriteClaim.DONE)

    def test_no_customer_yet_is_an_empty_list_without_a_provider_call(self):
        transport = self.stub()
        response = self.client.get(reverse('subscriptions:my-subscriptions'))
        self.assertEqual(response.json(), {'customerId': None, 'subscriptions': [], 'unsettled': []})
        self.assertEqual(transport.requests, [])


class StatusMappingTests(TestCase):

    def test_every_listed_state_has_an_outcome_and_unlisted_is_unknown(self):
        from maxio_advanced_billing.models.enums import SubscriptionState
        for state in SubscriptionState:
            self.assertNotEqual(billing.status_from_provider(state), MaxioWriteClaim.UNKNOWN, state)
        self.assertEqual(billing.status_from_provider('brand_new_state'), MaxioWriteClaim.UNKNOWN)
        self.assertEqual(billing.status_from_provider(None), MaxioWriteClaim.UNKNOWN)
        self.assertEqual(billing.status_from_provider(SubscriptionState.CANCELED), MaxioWriteClaim.FAILED)
