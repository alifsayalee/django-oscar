"""
Tests for the Maxio subscription API. Maxio is replaced at the SDK's transport seam, so the real
request-building and decoding pipeline runs; nothing leaves the process.

Run from sandbox/:  python manage.py test apps.subscriptions
"""
import json
from typing import Any, Optional
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client as DjangoClient
from django.test import TestCase, override_settings
from django.urls import resolve
from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import HttpRequest, HttpResponse, JsonBody

from . import maxio
from .models import BillingCustomer, SubscriptionRequest

FAMILY = 'test-family'
MAXIO_SETTINGS = dict(MAXIO_API_KEY='test-key', MAXIO_SITE_SUBDOMAIN='test-site',
                      MAXIO_DEFAULT_PRODUCT_FAMILY=FAMILY, MAXIO_BASE_URL='', MAXIO_ENVIRONMENT='US')

Step = tuple[str, str, Any]   # (method, path fragment, HttpResponse or exception to raise)


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


def empty_response(status: int) -> HttpResponse:
    return HttpResponse(status_code=status, headers={})


class ScriptedTransport:
    """The SDK's sync transport protocol; answers each request from a script, checking method and path."""

    def __init__(self) -> None:
        self.steps: list[Step] = []
        self.requests: list[HttpRequest] = []

    def expect(self, method: str, path: str, outcome: Any) -> None:
        self.steps.append((method, path, outcome))

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        assert self.steps, 'unexpected request %s %s' % (request.method, request.url)
        method, path, outcome = self.steps.pop(0)
        assert request.method == method and path in request.url, (
            'expected %s %s, got %s %s' % (method, path, request.method, request.url))
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, HttpResponse)
        return outcome

    def close(self) -> None:
        pass

    def bodies(self, path: str) -> list[Any]:
        return [r.body.value for r in self.requests if path in r.url and isinstance(r.body, JsonBody)]

    def calls(self, method: str, path: str) -> int:
        return sum(1 for r in self.requests if r.method == method and path in r.url)


def product(handle: str, price: int, **extra: Any) -> dict[str, Any]:
    return {'product': {'id': hash(handle) % 10000, 'handle': handle, 'name': handle.title(),
                        'price_in_cents': price, 'interval': 1, 'interval_unit': 'month', **extra}}


def subscription(sub_id: int, state: str = 'active', *, handle: str = 'eshop-pro', price: int = 29900,
                 customer_id: int = 501, reference: Optional[str] = None) -> dict[str, Any]:
    return {'subscription': {
        'id': sub_id, 'state': state, 'product_price_in_cents': price,
        'current_period_ends_at': '2026-10-23T10:00:00-04:00', 'next_assessment_at': '2026-10-23T10:00:00-04:00',
        'activated_at': '2026-09-23T10:00:00-04:00', 'created_at': '2026-09-23T10:00:00-04:00',
        'reference': reference,
        'customer': {'id': customer_id, 'email': 'shopper@example.com'},
        'product': {'id': 1, 'handle': handle, 'name': handle.title(), 'price_in_cents': price,
                    'interval': 1, 'interval_unit': 'month'},
    }}


PLANS = json_response(200, [product('eshop-pro', 29900), product('basic-plan', 2900),
                            product('old-plan', 100, archived_at='2025-01-01T00:00:00Z')])
PRODUCTS_PATH = '/products.json'


@override_settings(**MAXIO_SETTINGS)
class SubscriptionApiTestCase(TestCase):

    def setUp(self) -> None:
        cache.clear()
        self.transport = ScriptedTransport()
        maxio.set_client(MaxioAdvancedBillingClient(
            environment='us', server_config={'production': {'us': {'site': 'test-site'}}},
            basic_auth={'username': 'test-key', 'password': 'x'}, custom_http_client=self.transport))
        self.addCleanup(maxio.set_client, None)
        patcher = mock.patch.object(maxio, 'READ_BACKOFF_SECONDS', 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = get_user_model().objects.create_user(
            username='shopper', email='shopper@example.com', password='not-a-real-password-1',
            first_name='Sam', last_name='Shopper')
        self.client.force_login(self.user)

    # -- helpers --

    def subscribe(self, plan: str = 'eshop-pro', client: Optional[DjangoClient] = None) -> Any:
        return (client or self.client).post('/api/subscriptions', data=json.dumps({'planHandle': plan}),
                                            content_type='application/json')

    def expect_new_customer(self, customer_id: int = 501) -> None:
        self.transport.expect('GET', '/customers/lookup.json', empty_response(404))
        self.transport.expect('POST', '/customers.json', json_response(201, {'customer': {'id': customer_id}}))

    def request_row(self) -> SubscriptionRequest:
        return SubscriptionRequest.objects.get(user=self.user)

    # -- plans --

    def test_plans_list_offers_handles_and_skips_archived(self) -> None:
        self.transport.expect('GET', PRODUCTS_PATH, PLANS)
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual([p['planHandle'] for p in data['plans']], ['eshop-pro', 'basic-plan'])
        self.assertEqual(data['plans'][0]['price'], '299.00')
        self.assertFalse(data['truncated'])
        request = self.transport.requests[0]
        self.assertTrue(request.url.startswith('https://test-site.chargify.com/'))
        self.assertIn(FAMILY, request.url)
        self.assertTrue(request.headers['authorization'].startswith('Basic '))

    def test_plans_read_is_retried_on_transient_failure(self) -> None:
        self.transport.expect('GET', PRODUCTS_PATH, httpx.ConnectError('refused'))
        self.transport.expect('GET', PRODUCTS_PATH, json_response(503, {}))
        self.transport.expect('GET', PRODUCTS_PATH, PLANS)
        self.assertEqual(self.client.get('/api/subscription-plans').status_code, 200)

    def test_unauthenticated_caller_gets_401(self) -> None:
        response = DjangoClient().get('/api/subscription-plans')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.transport.requests, [])

    def test_post_requires_csrf_token_for_session_callers(self) -> None:
        csrf_client = DjangoClient(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(self.subscribe(client=csrf_client).status_code, 403)
        self.assertEqual(self.transport.requests, [])

    def test_api_views_opt_out_of_atomic_requests(self) -> None:
        # Claims must commit before Maxio is called and never roll back after it acted.
        for path in ('/api/subscription-plans', '/api/subscriptions', '/api/subscriptions/1',
                     '/api/my-subscriptions'):
            self.assertIn('default', getattr(resolve(path).func, '_non_atomic_requests', set()), path)

    # -- subscribe: happy path and idempotency --

    def test_subscribe_creates_customer_and_subscription(self) -> None:
        self.transport.expect('GET', PRODUCTS_PATH, PLANS)
        self.expect_new_customer()
        self.transport.expect('POST', '/subscriptions.json', json_response(201, subscription(9001)))

        response = self.subscribe()

        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data['subscriptionId'], 9001)
        self.assertEqual(data['status'], 'active')
        self.assertEqual(data['subscription']['planHandle'], 'eshop-pro')
        self.assertEqual(data['subscription']['price'], '299.00')
        self.assertEqual(data['subscription']['state'], 'active')
        self.assertIsNotNone(data['subscription']['nextBillingAt'])

        customer_body = self.transport.bodies('/customers.json')[0]['customer']
        link = BillingCustomer.objects.get(user=self.user)
        self.assertEqual(customer_body['reference'], link.reference)
        self.assertEqual((customer_body['email'], customer_body['first_name']), ('shopper@example.com', 'Sam'))
        self.assertEqual(link.maxio_customer_id, 501)

        sub_body = self.transport.bodies('/subscriptions.json')[0]['subscription']
        row = self.request_row()
        self.assertEqual(sub_body, {'product_handle': 'eshop-pro', 'customer_id': 501, 'reference': row.reference,
                                    'payment_collection_method': 'remittance'})
        self.assertEqual((row.status, row.maxio_subscription_id), ('active', 9001))

    def test_double_click_does_not_create_a_second_subscription(self) -> None:
        self.transport.expect('GET', PRODUCTS_PATH, PLANS)
        self.expect_new_customer()
        self.transport.expect('POST', '/subscriptions.json', json_response(201, subscription(9001)))
        self.assertEqual(self.subscribe().status_code, 201)

        self.transport.expect('GET', '/subscriptions/9001.json', json_response(200, subscription(9001)))
        second = self.subscribe()

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['subscriptionId'], 9001)
        self.assertFalse(second.json()['created'])
        self.assertEqual(self.transport.calls('POST', '/subscriptions.json'), 1)
        self.assertEqual(self.transport.calls('POST', '/customers.json'), 1)

    def test_in_flight_claim_answers_without_calling_maxio_create(self) -> None:
        SubscriptionRequest.objects.create(user=self.user, plan_handle='eshop-pro', reference='sub-inflight',
                                           status=SubscriptionRequest.SENDING)
        self.transport.expect('GET', PRODUCTS_PATH, PLANS)
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertIsNone(response.json()['subscriptionId'])
        self.assertEqual(self.transport.calls('POST', '/subscriptions.json'), 0)

    def test_existing_customer_is_reused_by_reference(self) -> None:
        self.transport.expect('GET', PRODUCTS_PATH, PLANS)
        self.transport.expect('GET', '/customers/lookup.json', json_response(200, {'customer': {'id': 777}}))
        self.transport.expect('POST', '/subscriptions.json',
                              json_response(201, subscription(9002, customer_id=777)))
        self.assertEqual(self.subscribe().status_code, 201)
        self.assertEqual(self.transport.calls('POST', '/customers.json'), 0)

    def test_customer_reference_taken_is_looked_up_not_recreated(self) -> None:
        self.transport.expect('GET', PRODUCTS_PATH, PLANS)
        self.transport.expect('GET', '/customers/lookup.json', empty_response(404))
        self.transport.expect('POST', '/customers.json',
                              json_response(422, {'errors': ['Reference must be unique']}))
        self.transport.expect('GET', '/customers/lookup.json', json_response(200, {'customer': {'id': 888}}))
        self.transport.expect('POST', '/subscriptions.json',
                              json_response(201, subscription(9003, customer_id=888)))
        self.assertEqual(self.subscribe().status_code, 201)
        self.assertEqual(BillingCustomer.objects.get(user=self.user).maxio_customer_id, 888)

    # -- the plan handle must be one the plan list offers --

    def test_unknown_plan_is_rejected_before_any_write(self) -> None:
        self.transport.expect('GET', PRODUCTS_PATH, PLANS)
        self.transport.expect('GET', PRODUCTS_PATH, PLANS)   # a miss re-checks a fresh list
        response = self.subscribe('no-such-plan')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.transport.calls('POST', '/subscriptions.json'), 0)
        self.assertFalse(SubscriptionRequest.objects.exists())

    def test_archived_plan_is_not_subscribable(self) -> None:
        self.transport.expect('GET', PRODUCTS_PATH, PLANS)
        self.transport.expect('GET', PRODUCTS_PATH, PLANS)
        self.assertEqual(self.subscribe('old-plan').status_code, 400)

    def test_missing_plan_handle_is_400(self) -> None:
        response = self.client.post('/api/subscriptions', data='{}', content_type='application/json')
        self.assertEqual(response.status_code, 400)

    # -- outcomes from the state Maxio reports --

    def test_pending_state_is_accepted_not_confirmed(self) -> None:
        self.transport.expect('GET', PRODUCTS_PATH, PLANS)
        self.expect_new_customer()
        self.transport.expect('POST', '/subscriptions.json', json_response(201, subscription(9004, 'pending')))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['status'], 'pending')

    def test_unlisted_state_is_unknown_not_active(self) -> None:
        self.transport.expect('GET', PRODUCTS_PATH, PLANS)
        self.expect_new_customer()
        self.transport.expect('POST', '/subscriptions.json',
                              json_response(201, subscription(9005, 'something_new')))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['status'], 'unknown')

    def test_failed_to_create_releases_the_claim(self) -> None:
        self.transport.expect('GET', PRODUCTS_PATH, PLANS)
        self.expect_new_customer()
        self.transport.expect('POST', '/subscriptions.json',
                              json_response(201, subscription(9006, 'failed_to_create')))
        self.assertEqual(self.subscribe().status_code, 502)
        self.assertEqual(self.request_row().status, 'failed')

    def test_echoed_price_mismatch_needs_review(self) -> None:
        self.transport.expect('GET', PRODUCTS_PATH, PLANS)
        self.expect_new_customer()
        self.transport.expect('POST', '/subscriptions.json', json_response(201, subscription(9007, price=100)))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.request_row().status, 'needs_review')

    # -- failures --

    def test_validation_rejection_passes_through_and_releases_the_claim(self) -> None:
        self.transport.expect('GET', PRODUCTS_PATH, PLANS)
        self.expect_new_customer()
        self.transport.expect('POST', '/subscriptions.json',
                              json_response(422, {'errors': ['Product must be active']}))
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['details'], ['Product must be active'])
        self.assertEqual(self.request_row().status, 'failed')
        self.assertEqual(self.transport.calls('GET', '/subscriptions/lookup.json'), 0)

    def test_provider_auth_failure_is_ours_not_the_callers(self) -> None:
        self.transport.expect('GET', PRODUCTS_PATH, empty_response(401))
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 502)

    def test_unsent_and_unknown_transport_failures_are_told_apart(self) -> None:
        # Never sent: a known outcome, the claim is released, nothing is looked up.
        self.transport.expect('GET', PRODUCTS_PATH, PLANS)
        self.expect_new_customer()
        self.transport.expect('POST', '/subscriptions.json', httpx.ConnectError('refused'))
        unsent = self.subscribe()
        self.assertEqual((unsent.status_code, unsent.json()['outcomeUnknown']), (502, False))
        self.assertEqual(self.request_row().status, 'failed')
        self.assertEqual(self.transport.calls('GET', '/subscriptions/lookup.json'), 0)

        # No reply: may have landed. Looked up by the SAME reference; not found stays unknown (504).
        self.transport.expect('POST', '/subscriptions.json', httpx.ReadTimeout('no reply'))
        self.transport.expect('GET', '/subscriptions/lookup.json', empty_response(404))
        unknown = self.subscribe()
        self.assertEqual(unknown.status_code, 504)
        self.assertNotEqual(unsent.status_code, unknown.status_code)
        row = SubscriptionRequest.objects.get(user=self.user, status='unknown')
        self.assertIn('reference=' + row.reference, self.transport.requests[-1].url)

        # The retry reconciles under that reference instead of creating a second subscription.
        self.transport.expect('GET', '/subscriptions/lookup.json',
                              json_response(200, subscription(9008, reference=row.reference)))
        settled = self.subscribe()
        self.assertEqual(settled.status_code, 200)
        self.assertEqual(settled.json()['subscriptionId'], 9008)
        self.assertEqual(self.transport.calls('POST', '/subscriptions.json'), 2)   # the two above, no third

    def test_unreadable_success_body_is_reconciled(self) -> None:
        self.transport.expect('GET', PRODUCTS_PATH, PLANS)
        self.expect_new_customer()
        self.transport.expect('POST', '/subscriptions.json', json_response(201, {}))   # no subscription in it
        self.transport.expect('GET', '/subscriptions/lookup.json', json_response(200, subscription(9009)))
        response = self.subscribe()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['subscriptionId'], 9009)

    def test_missing_configuration_is_503(self) -> None:
        maxio.set_client(None)
        with override_settings(MAXIO_API_KEY=''):
            response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 503)

    # -- reading back --

    def test_my_subscriptions_reads_from_maxio(self) -> None:
        BillingCustomer.objects.create(user=self.user, reference='oscar-user-x', maxio_customer_id=501)
        self.transport.expect('GET', '/customers/501/subscriptions.json',
                              json_response(200, [subscription(9001), subscription(9010, 'canceled')]))
        response = self.client.get('/api/my-subscriptions')
        self.assertEqual(response.status_code, 200)
        subs = response.json()['subscriptions']
        self.assertEqual([(s['subscriptionId'], s['status']) for s in subs], [(9001, 'active'), (9010, 'ended')])

    def test_my_subscriptions_without_customer_is_empty(self) -> None:
        response = self.client.get('/api/my-subscriptions')
        self.assertEqual(response.json(), {'subscriptions': [], 'unconfirmedRequests': []})
        self.assertEqual(self.transport.requests, [])

    def test_subscription_detail_hides_other_customers_subscriptions(self) -> None:
        BillingCustomer.objects.create(user=self.user, reference='oscar-user-x', maxio_customer_id=501)
        self.transport.expect('GET', '/subscriptions/9999.json', json_response(200, subscription(9999, customer_id=2)))
        self.assertEqual(self.client.get('/api/subscriptions/9999').status_code, 404)
        self.transport.expect('GET', '/subscriptions/9001.json', json_response(200, subscription(9001)))
        response = self.client.get('/api/subscriptions/9001')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['subscriptionId'], 9001)
