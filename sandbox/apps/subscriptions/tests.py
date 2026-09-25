"""
Tests for the subscription billing API. Maxio is faked at the SDK's transport
seam, so the real SDK builds every request and decodes every response.

Run: cd sandbox && python manage.py test apps.subscriptions
"""
import json
from datetime import timedelta
from typing import Any

import httpx
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from maxio_advanced_billing.core import HttpRequest, HttpResponse, JsonBody

from . import maxio
from .models import BillingCustomer, BillingSubscription, Outcome

PREFIX = 'test-install'
MAXIO_SETTINGS = {
    'MAXIO_API_KEY': 'test-key',
    'MAXIO_SITE_SUBDOMAIN': 'test-site',
    'MAXIO_ENVIRONMENT': 'US',
    'MAXIO_DEFAULT_PRODUCT_FAMILY': 'eshop-subscribe',
    'MAXIO_BASE_URL': '',
    'MAXIO_REFERENCE_PREFIX': PREFIX,
}


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


class StubTransport:
    """Satisfies the SDK's sync transport protocol; answers queued responses in order."""

    def __init__(self) -> None:
        self.responses: list[HttpResponse | Exception] = []
        self.requests: list[HttpRequest] = []

    def queue(self, *responses: HttpResponse | Exception) -> None:
        self.responses.extend(responses)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError(f'unexpected request {request.method} {request.url}')
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        pass

    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.split('?')[0].split('.com', 1)[1]) for r in self.requests]


def product(handle: str, cents: int, **extra: Any) -> dict[str, Any]:
    return {'product': {'id': cents, 'handle': handle, 'name': handle.title(), 'price_in_cents': cents,
                        'interval': 1, 'interval_unit': 'month', **extra}}


PLANS = [product('eshop-pro', 29900), product('basic-plan', 2900),
         product('old-plan', 100, archived_at='2025-01-01T00:00:00Z')]


def customer_body(customer_id: int = 501, reference: str = '') -> dict[str, Any]:
    return {'customer': {'id': customer_id, 'reference': reference, 'first_name': 'Sam',
                         'last_name': 'Shopper', 'email': 'sam@example.com',
                         'created_at': '2026-09-25T10:00:00Z'}}


def subscription_body(sub_id: int = 9001, state: str = 'active', reference: str = 'r',
                      customer_id: int = 501) -> dict[str, Any]:
    return {'subscription': {
        'id': sub_id, 'state': state, 'reference': reference,
        'product_price_in_cents': 29900,
        'next_assessment_at': '2026-10-25T10:00:00Z',
        'current_period_ends_at': '2026-10-25T10:00:00Z',
        'created_at': '2026-09-25T10:00:00Z',
        'customer': {'id': customer_id},
        'product': {'handle': 'eshop-pro', 'name': 'Pro Plan', 'price_in_cents': 29900,
                    'interval': 1, 'interval_unit': 'month'},
    }}


@override_settings(**MAXIO_SETTINGS)
class ApiTestCase(TestCase):

    def setUp(self) -> None:
        cache.clear()
        self.transport = StubTransport()
        maxio.set_client(maxio.build_client(custom_http_client=self.transport))
        self.user = get_user_model().objects.create_user(
            username='sam', email='sam@example.com', password='not-a-real-password-1',
            first_name='Sam', last_name='Shopper')
        self.client.force_login(self.user)

    def tearDown(self) -> None:
        maxio.set_client(None)

    @property
    def customer_ref(self) -> str:
        return f'{PREFIX}:customer:{self.user.pk}'

    def sub_ref(self, plan: str = 'eshop-pro', generation: int = 0) -> str:
        return f'{PREFIX}:subscription:{self.user.pk}:{plan}:{generation}'

    def subscribe(self, plan: str = 'eshop-pro') -> Any:
        return self.client.post('/api/subscriptions', data=json.dumps({'planHandle': plan}),
                                content_type='application/json')

    def body_of(self, index: int) -> Any:
        body = self.transport.requests[index].body
        assert isinstance(body, JsonBody)
        return body.value


class PlansTests(ApiTestCase):

    def test_lists_live_plans_of_the_configured_family_with_handles(self) -> None:
        self.transport.queue(json_response(200, PLANS))
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 200)
        plans = response.json()['plans']
        self.assertEqual([p['planHandle'] for p in plans], ['basic-plan', 'eshop-pro'])
        self.assertEqual(plans[1]['price'], '299.00')
        self.assertEqual(plans[1]['intervalUnit'], 'month')
        request = self.transport.requests[0]
        self.assertEqual(request.method, 'GET')
        self.assertIn('https://test-site.chargify.com/product_families/', request.url)
        self.assertIn('handle%3Aeshop-subscribe', request.url)
        self.assertEqual(request.headers['authorization'][:6], 'Basic ')

    def test_requires_login(self) -> None:
        self.client.logout()
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.transport.requests, [])

    @override_settings(MAXIO_API_KEY='')
    def test_missing_credentials_is_a_configuration_error(self) -> None:
        maxio.set_client(None)
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'billing_not_configured')

    @override_settings(MAXIO_BASE_URL='https://maxio.test.invalid')
    def test_base_url_override_is_used_verbatim(self) -> None:
        maxio.set_client(maxio.build_client(custom_http_client=self.transport))
        self.transport.queue(json_response(200, PLANS))
        self.client.get('/api/subscription-plans')
        self.assertTrue(self.transport.requests[0].url.startswith('https://maxio.test.invalid/product_families/'))


class ReferencePrefixTests(TestCase):

    @override_settings(MAXIO_REFERENCE_PREFIX='')
    def test_default_prefix_is_a_stable_random_id_per_database(self) -> None:
        first = maxio.reference_prefix()
        self.assertRegex(first, r'^oscar-[0-9a-f]{12}$')
        self.assertEqual(maxio.reference_prefix(), first)


class SubscribeTests(ApiTestCase):

    def test_subscribe_creates_customer_then_subscription_with_references(self) -> None:
        self.transport.queue(
            json_response(200, PLANS),
            json_response(201, customer_body(reference=self.customer_ref)),
            json_response(201, subscription_body(reference=self.sub_ref())),
        )
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data['subscriptionId'], 9001)
        self.assertEqual(data['state'], 'active')
        self.assertEqual(data['outcome'], 'done')
        self.assertEqual(data['price'], '299.00')
        self.assertEqual(data['nextBillingAt'], '2026-10-25T10:00:00+00:00')
        self.assertEqual([c[0] for c in self.transport.calls()], ['GET', 'POST', 'POST'])
        self.assertEqual(self.body_of(1)['customer']['reference'], self.customer_ref)
        self.assertEqual(self.body_of(1)['customer']['email'], 'sam@example.com')
        sub = self.body_of(2)['subscription']
        self.assertEqual(sub, {'product_handle': 'eshop-pro', 'customer_id': 501,
                               'payment_collection_method': 'remittance', 'reference': self.sub_ref()})
        record = BillingSubscription.objects.get()
        self.assertEqual((record.outcome, record.provider_id, record.provider_state), ('done', 9001, 'active'))

    def test_the_same_subscribe_twice_sends_one_create(self) -> None:
        self.transport.queue(
            json_response(200, PLANS),
            json_response(201, customer_body(reference=self.customer_ref)),
            json_response(201, subscription_body(reference=self.sub_ref())),
            json_response(200, subscription_body(reference=self.sub_ref())),   # re-read on the repeat
        )
        first = self.subscribe()
        second = self.subscribe()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['subscriptionId'], first.json()['subscriptionId'])
        self.assertEqual(self.transport.calls().count(('POST', '/subscriptions.json')), 1)
        self.assertEqual(self.transport.calls().count(('POST', '/customers.json')), 1)
        self.assertEqual(BillingSubscription.objects.count(), 1)

    def test_a_fresh_in_flight_claim_makes_no_provider_call(self) -> None:
        BillingCustomer.objects.create(user=self.user, reference=self.customer_ref, provider_id=501,
                                       outcome=Outcome.DONE, claimed_at=timezone.now())
        BillingSubscription.objects.create(user=self.user, plan_handle='eshop-pro', reference=self.sub_ref(),
                                           outcome=Outcome.SENDING, claimed_at=timezone.now())
        self.transport.queue(json_response(200, PLANS))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['outcome'], 'sending')
        self.assertEqual(self.transport.calls(), [('GET', '/product_families/handle%3Aeshop-subscribe/products.json')])

    def test_pending_state_is_accepted_not_done(self) -> None:
        self.transport.queue(json_response(200, PLANS), json_response(201, customer_body()),
                             json_response(201, subscription_body(state='awaiting_signup')))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['outcome'], 'pending')

    def test_a_problem_state_is_not_done(self) -> None:
        self.transport.queue(json_response(200, PLANS), json_response(201, customer_body()),
                             json_response(201, subscription_body(state='past_due')))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['outcome'], 'pending')

    def test_an_unlisted_state_is_unknown_not_done(self) -> None:
        self.transport.queue(json_response(200, PLANS), json_response(201, customer_body()),
                             json_response(201, subscription_body(state='something_new')))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['outcome'], 'unknown')

    def test_a_canceled_subscription_is_failed_and_the_next_subscribe_uses_a_new_reference(self) -> None:
        self.transport.queue(json_response(200, PLANS), json_response(201, customer_body()),
                             json_response(201, subscription_body(state='canceled')))
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['outcome'], 'failed')
        self.transport.queue(json_response(201, subscription_body(sub_id=9002)))
        again = self.subscribe()
        self.assertEqual(again.status_code, 201)
        self.assertEqual(self.body_of(-1)['subscription']['reference'], self.sub_ref(generation=1))

    def test_a_response_without_a_subscription_is_unknown(self) -> None:
        self.transport.queue(json_response(200, PLANS), json_response(201, customer_body()),
                             json_response(201, {}),
                             json_response(404, {}))       # the lookup by reference
        response = self.subscribe()
        self.assertEqual(response.status_code, 504)
        self.assertTrue(response.json()['error']['outcomeUnknown'])

    def test_unknown_plan_is_rejected_before_any_write(self) -> None:
        self.transport.queue(json_response(200, PLANS))
        response = self.subscribe('no-such-plan')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(self.transport.requests), 1)

    def test_missing_plan_handle_is_a_bad_request(self) -> None:
        response = self.client.post('/api/subscriptions', data='{}', content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_provider_validation_errors_reach_the_caller(self) -> None:
        self.transport.queue(json_response(200, PLANS), json_response(201, customer_body()),
                             json_response(422, {'errors': ['Product must be active']}))
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['details'], ['Product must be active'])
        record = BillingSubscription.objects.get()
        self.assertEqual((record.outcome, record.provider_id), ('failed', None))   # released

    def test_rejected_credentials_are_our_problem_not_the_callers(self) -> None:
        self.transport.queue(json_response(401, {}))
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 502)


class UnknownOutcomeTests(ApiTestCase):

    def test_refused_connection_is_known_and_releases_the_claim(self) -> None:
        self.transport.queue(json_response(200, PLANS), json_response(201, customer_body()),
                             httpx.ConnectError('refused'))
        response = self.subscribe()
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('outcomeUnknown', response.json()['error'])
        self.assertEqual(BillingSubscription.objects.get().outcome, 'failed')
        # the next request re-takes the released claim and sends under the SAME reference
        self.transport.queue(json_response(201, subscription_body()))
        self.assertEqual(self.subscribe().status_code, 201)
        self.assertEqual(self.body_of(-1)['subscription']['reference'], self.sub_ref())

    def test_read_timeout_is_unknown_and_is_only_ever_looked_up(self) -> None:
        self.transport.queue(json_response(200, PLANS), json_response(201, customer_body()),
                             httpx.ReadTimeout('no reply'),
                             json_response(404, {}))          # lookup: not there (yet)
        response = self.subscribe()
        self.assertEqual(response.status_code, 504)
        self.assertTrue(response.json()['error']['outcomeUnknown'])
        self.assertEqual(BillingSubscription.objects.get().outcome, 'unknown')
        lookup = self.transport.requests[-1]
        self.assertEqual(lookup.method, 'GET')
        self.assertIn('/subscriptions/lookup.json', lookup.url)
        self.assertIn('reference=' + self.sub_ref().replace(':', '%3A'), lookup.url)

        # The repeat checks by the same reference — it never sends a second create.
        self.transport.queue(json_response(200, subscription_body(reference=self.sub_ref())))
        repeat = self.subscribe()
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(repeat.json()['subscriptionId'], 9001)
        self.assertEqual(self.transport.calls().count(('POST', '/subscriptions.json')), 1)
        self.assertEqual(BillingSubscription.objects.get().outcome, 'done')

    def test_server_error_on_create_that_landed_is_found_by_reference(self) -> None:
        self.transport.queue(json_response(200, PLANS), json_response(201, customer_body()),
                             json_response(500, {}),
                             json_response(200, subscription_body()))
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(BillingSubscription.objects.get().provider_id, 9001)

    def test_customer_create_that_timed_out_is_resent_under_the_same_reference(self) -> None:
        self.transport.queue(json_response(200, PLANS), httpx.ReadTimeout('no reply'),
                             json_response(404, {}))           # lookup: not there yet
        response = self.subscribe()
        self.assertEqual(response.status_code, 504)
        self.assertEqual(BillingCustomer.objects.get().outcome, 'unknown')

        # Next attempt: the check is a same-reference resend; Maxio refuses the duplicate
        # reference with 422, and the lookup finds the customer.
        self.transport.queue(json_response(422, {'errors': ['Reference must be unique']}),
                             json_response(200, customer_body(reference=self.customer_ref)),
                             json_response(201, subscription_body()))
        again = self.subscribe()
        self.assertEqual(again.status_code, 201)
        customer_posts = [i for i, c in enumerate(self.transport.calls()) if c == ('POST', '/customers.json')]
        self.assertEqual(len(customer_posts), 2)
        self.assertEqual({self.body_of(i)['customer']['reference'] for i in customer_posts}, {self.customer_ref})
        self.assertEqual(BillingCustomer.objects.get().provider_id, 501)

    def test_a_stale_sending_claim_is_checked_not_resent(self) -> None:
        BillingCustomer.objects.create(user=self.user, reference=self.customer_ref, provider_id=501,
                                       outcome=Outcome.DONE, claimed_at=timezone.now())
        BillingSubscription.objects.create(user=self.user, plan_handle='eshop-pro', reference=self.sub_ref(),
                                           outcome=Outcome.SENDING,
                                           claimed_at=timezone.now() - timedelta(hours=1))
        self.transport.queue(json_response(200, PLANS), json_response(200, subscription_body()))
        response = self.subscribe()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(('POST', '/subscriptions.json'), self.transport.calls())


class ReadTests(ApiTestCase):

    def setUp(self) -> None:
        super().setUp()
        BillingCustomer.objects.create(user=self.user, reference=self.customer_ref, provider_id=501,
                                       outcome=Outcome.DONE, claimed_at=timezone.now())

    def test_my_subscriptions_reads_from_maxio(self) -> None:
        self.transport.queue(json_response(200, [subscription_body(), subscription_body(9002, 'canceled')]))
        response = self.client.get('/api/my-subscriptions')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual([s['subscriptionId'] for s in data['subscriptions']], [9001, 9002])
        self.assertEqual([s['outcome'] for s in data['subscriptions']], ['done', 'failed'])
        self.assertIn('/customers/501/subscriptions.json', self.transport.requests[0].url)

    def test_my_subscriptions_settles_an_unknown_claim_by_lookup(self) -> None:
        BillingSubscription.objects.create(user=self.user, plan_handle='eshop-pro', reference=self.sub_ref(),
                                           outcome=Outcome.UNKNOWN, claimed_at=timezone.now())
        self.transport.queue(json_response(200, subscription_body()),
                             json_response(200, [subscription_body()]))
        data = self.client.get('/api/my-subscriptions').json()
        self.assertEqual(data['unresolvedRequests'], [])
        self.assertEqual(BillingSubscription.objects.get().outcome, 'done')

    def test_subscription_detail_hides_other_customers_subscriptions(self) -> None:
        self.transport.queue(json_response(200, subscription_body(customer_id=777)))
        self.assertEqual(self.client.get('/api/subscriptions/9001').status_code, 404)

    def test_subscription_detail(self) -> None:
        self.transport.queue(json_response(200, subscription_body()))
        response = self.client.get('/api/subscriptions/9001')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['planHandle'], 'eshop-pro')

    def test_billing_customer_is_idempotent(self) -> None:
        response = self.client.post('/api/billing-customer')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['customerId'], 501)
        self.assertEqual(self.transport.requests, [])
