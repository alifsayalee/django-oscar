import json
from datetime import datetime
from datetime import timezone as dt_timezone
from typing import Any

import httpx
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings
from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import (
    BasicAuthCredentials, HttpRequest, HttpResponse, JsonBody)
from maxio_advanced_billing.models import (
    Customer, CustomerResponse, Product, ProductFamily, ProductResponse,
    Subscription, SubscriptionResponse)
from maxio_advanced_billing.models.enums import SubscriptionState

from . import maxio_client
from .billing import reset_site_settings, status_from_provider
from .models import BillingCustomer, Outcome, SubscriptionClaim

MAXIO_TEST_SETTINGS = {
    'MAXIO_API_KEY': 'test-key',
    'MAXIO_SITE_SUBDOMAIN': 'test-site',
    'MAXIO_ENVIRONMENT': 'US',
    'MAXIO_DEFAULT_PRODUCT_FAMILY': 'test-family',
    'MAXIO_BASE_URL': '',
    'MAXIO_REFERENCE_PREFIX': 'test',
}

CUSTOMER_REF = 'test-customer-u{pk}'
NEXT_BILLING = datetime(2026, 10, 25, 12, 0, tzinfo=dt_timezone.utc)


class StubTransport:
    """The SDK's sync transport protocol; answers queued responses (or raises queued errors) in order."""

    def __init__(self) -> None:
        self.queue: list[HttpResponse | Exception] = []
        self.requests: list[HttpRequest] = []

    def add(self, *items: HttpResponse | Exception) -> None:
        self.queue.extend(items)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if request.url.endswith('/site.json'):
            return site()                 # the site is read once per process, whenever first needed
        if not self.queue:
            raise AssertionError(f'unexpected Maxio call: {request.method} {request.url}')
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        pass

    def calls(self, method: str, path: str) -> list[HttpRequest]:
        return [r for r in self.requests if r.method == method and path in r.url]


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


def product(handle: str = 'pro', price: int = 29900, family: str = 'test-family') -> HttpResponse:
    return json_response(200, ProductResponse(product=Product(
        id=11, name=f'{handle.title()} Plan', handle=handle, price_in_cents=price, interval=1,
        interval_unit='month', require_credit_card=False,
        product_family=ProductFamily(handle=family))).to_dict())


def customer(pk: int, customer_id: int = 501) -> HttpResponse:
    return json_response(201, CustomerResponse(customer=Customer(
        id=customer_id, reference=CUSTOMER_REF.format(pk=pk), email='shopper@example.com',
        created_at=datetime(2026, 9, 25, tzinfo=dt_timezone.utc))).to_dict())


def subscription(reference: str, state: str = 'active', subscription_id: int = 9001,
                 handle: str = 'pro', status: int = 201) -> HttpResponse:
    return json_response(status, SubscriptionResponse(subscription=Subscription(
        id=subscription_id, state=state, reference=reference, product_price_in_cents=29900,
        currency='USD', next_assessment_at=NEXT_BILLING,
        product=Product(handle=handle, name='Pro Plan', interval=1, interval_unit='month'))).to_dict())


def sent_json(request: HttpRequest) -> Any:
    assert isinstance(request.body, JsonBody)
    return request.body.value


def site(relationship_invoicing: bool = True) -> HttpResponse:
    return json_response(200, {'site': {'id': 1, 'currency': 'USD',
                                        'relationship_invoicing_enabled': relationship_invoicing}})


def not_found() -> HttpResponse:
    return HttpResponse(status_code=404, headers={})


@override_settings(**MAXIO_TEST_SETTINGS)
class SubscriptionApiTestCase(TestCase):

    def setUp(self) -> None:
        self.transport = StubTransport()
        maxio_client.set_client(MaxioAdvancedBillingClient(
            environment='us',
            server_config=maxio_client.server_config('us'),
            custom_http_client=self.transport,
            basic_auth=BasicAuthCredentials(username='test-key', password='x'),
        ))
        self.user = get_user_model().objects.create_user(
            username='shopper', email='shopper@example.com', password='a-long-password',
            first_name='Sam', last_name='Shopper')
        self.client.force_login(self.user)
        self.sub_ref = f'test-sub-u{self.user.pk}-pro-1'
        reset_site_settings()

    def tearDown(self) -> None:
        maxio_client.set_client(None)
        reset_site_settings()

    def subscribe(self, plan: str = 'pro') -> Any:
        return self.client.post('/api/subscriptions', data=json.dumps({'planHandle': plan}),
                                content_type='application/json')


class PlansTests(SubscriptionApiTestCase):

    def test_lists_plans_of_the_configured_family_by_handle(self) -> None:
        self.transport.add(json_response(200, [
            ProductResponse(product=Product(handle='basic', name='Basic', price_in_cents=2900, interval=1,
                                            interval_unit='month')).to_dict(),
            ProductResponse(product=Product(handle='old', name='Old', price_in_cents=100,
                                            archived_at=NEXT_BILLING)).to_dict(),
        ]))

        response = self.client.get('/api/subscription-plans')

        self.assertEqual(response.status_code, 200)
        plans = response.json()['plans']
        self.assertEqual([p['planHandle'] for p in plans], ['basic'])     # archived plan hidden
        self.assertEqual(plans[0]['price'], {'amountInCents': 2900, 'amount': '29.00', 'currency': 'USD'})
        [request] = self.transport.calls('GET', '/product_families/')
        self.assertEqual(request.method, 'GET')
        self.assertTrue(request.url.startswith('https://test-site.chargify.com/product_families/'))
        self.assertIn('handle%3Atest-family', request.url)
        self.assertTrue(request.headers['authorization'].startswith('Basic '))

    def test_anonymous_caller_is_rejected(self) -> None:
        self.client.logout()
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.transport.requests, [])

    def test_our_credentials_refused_is_a_502_not_a_401(self) -> None:
        self.transport.add(HttpResponse(status_code=401, headers={}, content=b'HTTP Basic: Access denied.'))
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['error']['code'], 'provider_auth')


class SubscribeTests(SubscriptionApiTestCase):

    def test_subscribe_creates_customer_then_subscription(self) -> None:
        self.transport.add(product(), customer(self.user.pk), subscription(self.sub_ref))

        response = self.subscribe()

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['subscriptionId'], 9001)
        self.assertEqual(body['outcome'], 'done')
        self.assertEqual(body['state'], 'active')
        self.assertEqual(body['price']['amount'], '299.00')
        self.assertEqual(body['nextBillingAt'], NEXT_BILLING.isoformat())

        [create_customer] = self.transport.calls('POST', '/customers.json')
        self.assertEqual(sent_json(create_customer)['customer']['reference'], CUSTOMER_REF.format(pk=self.user.pk))
        [create_subscription] = self.transport.calls('POST', '/subscriptions.json')
        sent = sent_json(create_subscription)['subscription']
        self.assertEqual(sent, {'product_handle': 'pro', 'customer_id': 501, 'reference': self.sub_ref,
                                'payment_collection_method': 'remittance'})

    @override_settings(MAXIO_PAYMENT_COLLECTION_METHOD='automatic')
    def test_collection_method_setting_overrides_the_site_default(self) -> None:
        self.transport.add(product(), customer(self.user.pk), subscription(self.sub_ref))
        self.subscribe()
        [create_subscription] = self.transport.calls('POST', '/subscriptions.json')
        self.assertEqual(sent_json(create_subscription)['subscription']['payment_collection_method'], 'automatic')

    def test_the_same_subscribe_twice_makes_one_customer_and_one_subscription(self) -> None:
        self.transport.add(product(), customer(self.user.pk), subscription(self.sub_ref))
        first = self.subscribe()
        self.transport.add(product())            # the repeat only re-validates the plan
        second = self.subscribe()

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['subscriptionId'], first.json()['subscriptionId'])
        self.assertTrue(second.json()['repeat'])
        self.assertEqual(len(self.transport.calls('POST', '/customers.json')), 1)
        self.assertEqual(len(self.transport.calls('POST', '/subscriptions.json')), 1)
        self.assertEqual(BillingCustomer.objects.count(), 1)
        self.assertEqual(SubscriptionClaim.objects.count(), 1)

    def test_a_fresh_in_flight_claim_answers_in_progress_without_calling_maxio(self) -> None:
        self.transport.add(product(), customer(self.user.pk), subscription(self.sub_ref))
        self.subscribe()
        SubscriptionClaim.objects.update(outcome=Outcome.SENDING, maxio_subscription_id=None)
        self.transport.add(product())

        response = self.subscribe()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['outcome'], 'sending')
        self.assertEqual(len(self.transport.calls('POST', '/subscriptions.json')), 1)

    def test_pending_state_is_accepted_not_created(self) -> None:
        self.transport.add(product(), customer(self.user.pk), subscription(self.sub_ref, state='pending'))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['outcome'], 'pending')
        self.assertEqual(response.json()['subscriptionId'], 9001)

    def test_an_unlisted_state_is_unknown_not_done(self) -> None:
        self.transport.add(product(), customer(self.user.pk),
                           subscription(self.sub_ref, state='something_new'))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['outcome'], 'unknown')

    def test_failed_to_create_is_a_failure_and_frees_the_plan(self) -> None:
        self.transport.add(product(), customer(self.user.pk),
                           subscription(self.sub_ref, state='failed_to_create'))
        response = self.subscribe()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['outcome'], 'failed')
        self.assertFalse(SubscriptionClaim.objects.get().slot_open)

    def test_a_different_plan_echoed_back_needs_review(self) -> None:
        self.transport.add(product(), customer(self.user.pk), subscription(self.sub_ref, handle='other'))
        response = self.subscribe()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['outcome'], 'needs_review')

    def test_refused_connection_is_known_and_releases_the_claim(self) -> None:
        self.transport.add(product(), customer(self.user.pk), httpx.ConnectError('refused'))

        response = self.subscribe()

        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()['error']['outcomeUnknown'])
        self.assertEqual(self.transport.calls('GET', '/subscriptions/lookup.json'), [])   # nothing to look up
        claim = SubscriptionClaim.objects.get()
        self.assertEqual((claim.outcome, claim.slot_open), (Outcome.FAILED, False))

    def test_read_timeout_is_unknown_and_is_settled_by_lookup_never_by_a_second_create(self) -> None:
        self.transport.add(product(), customer(self.user.pk), httpx.ReadTimeout('no reply'), not_found())

        response = self.subscribe()

        self.assertEqual(response.status_code, 504)
        self.assertTrue(response.json()['error']['outcomeUnknown'])
        self.assertEqual(response.json()['error']['reference'], self.sub_ref)
        self.assertEqual(SubscriptionClaim.objects.get().outcome, Outcome.UNKNOWN)
        [lookup] = self.transport.calls('GET', '/subscriptions/lookup.json')
        self.assertIn(f'reference={self.sub_ref}', lookup.url)

        # The repeat looks the SAME reference up; it landed after all.
        self.transport.add(product(), subscription(self.sub_ref, status=200))
        repeat = self.subscribe()

        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(repeat.json()['subscriptionId'], 9001)
        self.assertEqual(len(self.transport.calls('POST', '/subscriptions.json')), 1)

    def test_unsent_and_unknown_failures_answer_differently(self) -> None:
        self.transport.add(product(), customer(self.user.pk), httpx.ConnectError('refused'))
        unsent = self.subscribe()
        self.transport.add(product(), httpx.ReadTimeout('no reply'), not_found())
        unknown = self.subscribe()

        self.assertEqual((unsent.status_code, unsent.json()['error']['outcomeUnknown']), (502, False))
        self.assertEqual((unknown.status_code, unknown.json()['error']['outcomeUnknown']), (504, True))

    def test_provider_rejection_is_passed_back_with_its_messages(self) -> None:
        self.transport.add(product(), customer(self.user.pk),
                           json_response(422, {'errors': ['Product is not available.']}))
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['details'], ['Product is not available.'])
        self.assertEqual(SubscriptionClaim.objects.get().outcome, Outcome.FAILED)

    def test_unknown_or_foreign_plan_is_404(self) -> None:
        self.transport.add(not_found())
        self.assertEqual(self.subscribe('nope').status_code, 404)
        self.transport.add(product(family='another-family'))
        self.assertEqual(self.subscribe('pro').status_code, 404)
        self.assertEqual(self.transport.calls('POST', '/customers.json'), [])

    def test_plan_handle_is_required(self) -> None:
        response = self.client.post('/api/subscriptions', data='{}', content_type='application/json')
        self.assertEqual(response.status_code, 400)


class CustomerTests(SubscriptionApiTestCase):

    def test_duplicate_reference_rejection_adopts_the_customer_that_landed(self) -> None:
        self.transport.add(json_response(422, {'errors': ['Reference must be unique.']}),
                           customer(self.user.pk, customer_id=777))

        response = self.client.post('/api/billing-customer')

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['customerId'], 777)
        [lookup] = self.transport.calls('GET', '/customers/lookup.json')
        self.assertIn(f'reference={CUSTOMER_REF.format(pk=self.user.pk)}', lookup.url)

    def test_ensure_customer_is_idempotent(self) -> None:
        self.transport.add(customer(self.user.pk))
        first = self.client.post('/api/billing-customer')
        second = self.client.post('/api/billing-customer')
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(len(self.transport.requests), 1)

    def test_customer_rejection_releases_the_claim(self) -> None:
        self.transport.add(json_response(422, {'errors': {'customer': 'Email is invalid.'}}), not_found())
        response = self.client.post('/api/billing-customer')
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['details'], ['Email is invalid.'])
        self.assertFalse(BillingCustomer.objects.exists())


class MySubscriptionsTests(SubscriptionApiTestCase):

    def test_reads_subscriptions_live_and_refreshes_claims(self) -> None:
        self.transport.add(product(), customer(self.user.pk), subscription(self.sub_ref))
        self.subscribe()
        self.transport.add(json_response(200, [
            SubscriptionResponse(subscription=Subscription(
                id=9001, state='canceled', reference=self.sub_ref,
                product=Product(handle='pro', name='Pro Plan'))).to_dict(),
        ]))

        response = self.client.get('/api/my-subscriptions')

        self.assertEqual(response.status_code, 200)
        [entry] = response.json()['subscriptions']
        self.assertEqual((entry['subscriptionId'], entry['state'], entry['outcome']), (9001, 'canceled', 'failed'))
        self.assertTrue(entry['managedByThisSite'])
        self.assertIn('/customers/501/subscriptions.json', self.transport.requests[-1].url)
        claim = SubscriptionClaim.objects.get()
        self.assertEqual((claim.state, claim.slot_open), ('canceled', False))   # the plan can be taken again

    def test_no_customer_yet_means_no_subscriptions_and_no_calls(self) -> None:
        response = self.client.get('/api/my-subscriptions')
        self.assertEqual(response.json(), {'customer': None, 'subscriptions': [], 'unconfirmed': []})
        self.assertEqual(self.transport.requests, [])


class StatusMappingTests(TestCase):

    def test_every_subscription_state_maps_to_an_outcome(self) -> None:
        expected = {
            'active': 'done', 'trialing': 'done',
            'pending': 'pending', 'assessing': 'pending', 'awaiting_signup': 'pending',
            'past_due': 'pending', 'soft_failure': 'pending', 'unpaid': 'pending',
            'on_hold': 'pending', 'paused': 'pending', 'suspended': 'pending',
            'failed_to_create': 'failed', 'canceled': 'failed', 'expired': 'failed', 'trial_ended': 'failed',
        }
        self.assertEqual({s.value: status_from_provider(s) for s in SubscriptionState}, expected)
        self.assertEqual(status_from_provider('brand_new_state'), 'unknown')
        self.assertEqual(status_from_provider(None), 'unknown')


class ClientConfigurationTests(TestCase):

    @override_settings(**{**MAXIO_TEST_SETTINGS, 'MAXIO_BASE_URL': 'http://localhost:9999/maxio'})
    def test_base_url_override_is_used_verbatim(self) -> None:
        config = maxio_client.server_config('us')
        self.assertEqual(config.production.us.base_url, 'http://localhost:9999/maxio')

    @override_settings(**{**MAXIO_TEST_SETTINGS, 'MAXIO_ENVIRONMENT': 'mars'})
    def test_unknown_environment_is_a_configuration_error(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            maxio_client.build_client()

    @override_settings(**{**MAXIO_TEST_SETTINGS, 'MAXIO_API_KEY': ''})
    def test_missing_api_key_is_a_configuration_error(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            maxio_client.build_client()
