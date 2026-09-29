import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings
from django.utils import timezone
from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import HttpRequest, HttpResponse, JsonBody

from . import maxio
from .models import BillingClaim
from .views import answer

PLANS_PATH = '/product_families/handle:eshop-subscribe/products.json'
CUSTOMERS_PATH = '/customers.json'
CUSTOMER_LOOKUP_PATH = '/customers/lookup.json'
SUBSCRIPTIONS_PATH = '/subscriptions.json'
SUBSCRIPTION_LOOKUP_PATH = '/subscriptions/lookup.json'


@dataclass
class StubResponse:
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    chunks: list[bytes] = field(default_factory=list)
    url: str = 'https://cp-test.chargify.com/'
    closed: bool = False

    def iter_bytes(self, chunk_size: int | None) -> Iterator[bytes]:
        yield from self.chunks
        self.close()

    def read(self) -> bytes:
        self.close()
        return b''.join(self.chunks)

    def close(self) -> None:
        self.closed = True


def body_of(request: HttpRequest) -> Any:
    assert isinstance(request.body, JsonBody)
    return request.body.value


def json_response(status: int, body: object) -> StubResponse:
    return StubResponse(status_code=status, headers={'content-type': 'application/json'},
                        chunks=[json.dumps(body).encode()])


Reply = StubResponse | Exception | Callable[[HttpRequest], StubResponse]


class RoutingTransport:
    """The SDK's sync transport protocol, answering by method and path."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[Reply]] = {}
        self.requests: list[HttpRequest] = []

    def on(self, method: str, path: str, *replies: Reply) -> None:
        self.routes.setdefault((method, path), []).extend(replies)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = httpx.URL(request.url).path
        queue = self.routes.get((request.method, path))
        if not queue:
            raise AssertionError('unexpected request %s %s' % (request.method, request.url))
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(request)
        return reply

    def close(self) -> None:
        pass

    def sent(self, method: str, path: str) -> list[HttpRequest]:
        return [r for r in self.requests if r.method == method and httpx.URL(r.url).path == path]


def product(handle: str, price: int, **extra: Any) -> dict[str, Any]:
    return {'product': {'id': 700 + price % 97, 'handle': handle, 'name': handle.title(),
                        'price_in_cents': price, 'interval': 1, 'interval_unit': 'month', **extra}}


def customer_echo(request: HttpRequest) -> StubResponse:
    sent = body_of(request)['customer']
    return json_response(201, {'customer': {'id': 9001, 'reference': sent['reference'],
                                            'email': sent['email'],
                                            'created_at': '2026-09-29T10:00:00-04:00'}})


def subscription_body(reference: str, *, state: str = 'active', price: int = 29900,
                      subscription_id: int = 5501) -> dict[str, Any]:
    return {'subscription': {
        'id': subscription_id, 'state': state, 'reference': reference,
        'product_price_in_cents': price, 'currency': 'USD',
        'current_period_ends_at': '2026-10-29T10:00:00-04:00',
        'created_at': '2026-09-29T10:00:00-04:00',
        'product': {'id': 1, 'handle': 'eshop-pro', 'name': 'Pro Plan', 'price_in_cents': 29900},
        'customer': {'id': 9001},
    }}


def subscription_echo(**kwargs: Any) -> Callable[[HttpRequest], StubResponse]:
    def reply(request: HttpRequest) -> StubResponse:
        reference = body_of(request)['subscription']['reference']
        return json_response(201, subscription_body(reference, **kwargs))
    return reply


@override_settings(
    MAXIO_API_KEY='test-key', MAXIO_SITE_SUBDOMAIN='cp-test', MAXIO_ENVIRONMENT='US',
    MAXIO_DEFAULT_PRODUCT_FAMILY='eshop-subscribe', MAXIO_BASE_URL='',
    MAXIO_REFERENCE_PREFIX='test')
class SubscriptionApiTests(TestCase):

    def setUp(self) -> None:
        self.transport = RoutingTransport()
        self.transport.on('GET', PLANS_PATH, json_response(200, [
            product('basic-plan', 2900),
            product('eshop-pro', 29900),
            product('old-plan', 100, archived_at='2025-01-01T00:00:00Z'),
        ]))
        self.transport.on('GET', '/site.json', json_response(200, {'site': {
            'id': 1, 'subdomain': 'cp-test', 'currency': 'USD', 'relationship_invoicing_enabled': True}}))
        self.client_sdk = MaxioAdvancedBillingClient(
            environment='us', server_config={'production': {'us': {'site': 'cp-test'}}},
            custom_http_client=self.transport, basic_auth={'username': 'test-key', 'password': 'x'})
        patcher = mock.patch.object(maxio, 'get_client', return_value=self.client_sdk)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = get_user_model().objects.create_user(
            username='shopper', email='shopper@example.com', password='a-long-password')
        self.client.force_login(self.user)

    def subscribe(self, plan: str = 'eshop-pro', idempotency_key: str | None = None) -> Any:
        headers = {'Idempotency-Key': idempotency_key} if idempotency_key else {}
        return self.client.post('/api/subscriptions', data=json.dumps({'planHandle': plan}),
                                content_type='application/json', headers=headers)

    # Plans

    def test_plans_list_live_products_of_the_family_with_their_handles(self) -> None:
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 200)
        plans = response.json()
        self.assertEqual([p['planHandle'] for p in plans], ['basic-plan', 'eshop-pro'])
        self.assertEqual(plans[1]['priceInCents'], 29900)
        self.assertEqual(plans[1]['price'], '299.00')
        self.assertEqual(plans[1]['intervalUnit'], 'month')
        request = self.transport.requests[0]
        self.assertTrue(request.url.startswith('https://cp-test.chargify.com/'))
        self.assertEqual(request.headers['authorization'][:6], 'Basic ')

    def test_our_credentials_refused_is_not_the_callers_401(self) -> None:
        self.transport.routes[('GET', PLANS_PATH)] = [json_response(401, {'errors': ['bad key']})]
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 502)

    # Subscribe

    def test_subscribe_creates_customer_then_subscription_and_returns_subscription_id(self) -> None:
        self.transport.on('POST', CUSTOMERS_PATH, customer_echo)
        self.transport.on('POST', SUBSCRIPTIONS_PATH, subscription_echo())

        response = self.subscribe()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['subscriptionId'], 5501)
        self.assertEqual(body['status'], 'done')
        self.assertEqual(body['state'], 'active')
        self.assertEqual(body['planHandle'], 'eshop-pro')
        self.assertEqual(body['price'], '299.00')
        self.assertEqual(body['nextBillingAt'], '2026-10-29T10:00:00-04:00')

        customer_body = body_of(self.transport.sent('POST', CUSTOMERS_PATH)[0])
        customer_claim = BillingClaim.objects.get(key='customer:%s' % self.user.pk)
        self.assertEqual(customer_body['customer']['reference'], customer_claim.reference)
        self.assertEqual(customer_body['customer']['email'], 'shopper@example.com')
        sub_body = body_of(self.transport.sent('POST', SUBSCRIPTIONS_PATH)[0])
        self.assertEqual(sub_body['subscription']['product_handle'], 'eshop-pro')
        self.assertEqual(sub_body['subscription']['customer_id'], 9001)
        self.assertEqual(sub_body['subscription']['payment_collection_method'], 'remittance')
        sub_claim = BillingClaim.objects.get(kind=BillingClaim.SUBSCRIPTION)
        self.assertEqual(sub_body['subscription']['reference'], sub_claim.reference)
        self.assertEqual(sub_claim.outcome, BillingClaim.DONE)
        self.assertEqual(sub_claim.provider_id, '5501')

    def test_a_legacy_invoicing_site_is_invoiced(self) -> None:
        self.transport.routes[('GET', '/site.json')] = [json_response(200, {'site': {
            'id': 1, 'relationship_invoicing_enabled': False}})]
        self.transport.on('POST', CUSTOMERS_PATH, customer_echo)
        self.transport.on('POST', SUBSCRIPTIONS_PATH, subscription_echo())
        self.assertEqual(self.subscribe().status_code, 200)
        sub_body = body_of(self.transport.sent('POST', SUBSCRIPTIONS_PATH)[0])
        self.assertEqual(sub_body['subscription']['payment_collection_method'], 'invoice')

    def test_the_same_subscribe_twice_makes_one_customer_and_one_subscription(self) -> None:
        self.transport.on('POST', CUSTOMERS_PATH, customer_echo)
        self.transport.on('POST', SUBSCRIPTIONS_PATH, subscription_echo())

        first = self.subscribe()
        second = self.subscribe()

        self.assertEqual(len(self.transport.sent('POST', CUSTOMERS_PATH)), 1)
        self.assertEqual(len(self.transport.sent('POST', SUBSCRIPTIONS_PATH)), 1)
        self.assertEqual(second.status_code, first.status_code)
        self.assertEqual(second.json()['subscriptionId'], first.json()['subscriptionId'])

    def test_a_new_idempotency_key_is_a_new_subscription_for_the_same_customer(self) -> None:
        self.transport.on('POST', CUSTOMERS_PATH, customer_echo)
        self.transport.on('POST', SUBSCRIPTIONS_PATH, subscription_echo())

        self.subscribe(idempotency_key='a')
        self.subscribe(idempotency_key='b')

        self.assertEqual(len(self.transport.sent('POST', CUSTOMERS_PATH)), 1)
        references = [body_of(r)['subscription']['reference']
                      for r in self.transport.sent('POST', SUBSCRIPTIONS_PATH)]
        self.assertEqual(len(set(references)), 2)

    def test_a_claim_in_flight_answers_in_progress_without_a_provider_write(self) -> None:
        BillingClaim.objects.create(
            key='customer:%s' % self.user.pk, reference='test-customer-inflight',
            kind=BillingClaim.CUSTOMER, user=self.user, claimed_at=timezone.now())

        response = self.subscribe()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['status'], 'sending')
        self.assertEqual(self.transport.sent('POST', CUSTOMERS_PATH), [])

    def test_an_id_with_a_canceled_state_is_not_done(self) -> None:
        self.transport.on('POST', CUSTOMERS_PATH, customer_echo)
        self.transport.on('POST', SUBSCRIPTIONS_PATH, subscription_echo(state='canceled'))
        response = self.subscribe()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['status'], 'failed')

    def test_an_unlisted_state_is_unknown_not_done(self) -> None:
        self.transport.on('POST', CUSTOMERS_PATH, customer_echo)
        self.transport.on('POST', SUBSCRIPTIONS_PATH, subscription_echo(state='something_new'))
        response = self.subscribe()
        self.assertEqual(response.status_code, 504)
        self.assertTrue(response.json()['outcomeUnknown'])

    def test_a_pending_state_is_accepted_not_done(self) -> None:
        self.transport.on('POST', CUSTOMERS_PATH, customer_echo)
        self.transport.on('POST', SUBSCRIPTIONS_PATH, subscription_echo(state='pending'))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['subscriptionId'], 5501)

    def test_a_not_done_outcome_never_answers_success(self) -> None:
        for outcome in ('pending', 'sending', 'failed', 'needs_review', 'unknown', 'new'):
            claim = BillingClaim(kind=BillingClaim.SUBSCRIPTION, outcome=outcome, reference='r')
            self.assertNotIn(answer(claim).status_code, (200, 201, 204))

    def test_a_price_other_than_the_plans_needs_review(self) -> None:
        self.transport.on('POST', CUSTOMERS_PATH, customer_echo)
        self.transport.on('POST', SUBSCRIPTIONS_PATH, subscription_echo(price=100))
        response = self.subscribe()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['status'], 'needs_review')

    def test_a_truncated_success_body_is_unknown(self) -> None:
        self.transport.on('POST', CUSTOMERS_PATH, customer_echo)
        self.transport.on('POST', SUBSCRIPTIONS_PATH, json_response(201, {}))
        self.transport.on('GET', SUBSCRIPTION_LOOKUP_PATH, json_response(404, {}))
        response = self.subscribe()
        self.assertEqual(response.status_code, 504)
        self.assertTrue(response.json()['outcomeUnknown'])

    def test_a_timed_out_subscribe_is_looked_up_by_its_reference_not_resent(self) -> None:
        self.transport.on('POST', CUSTOMERS_PATH, customer_echo)
        self.transport.on('POST', SUBSCRIPTIONS_PATH, httpx.ReadTimeout('no reply'))

        def found(request: HttpRequest) -> StubResponse:
            reference = httpx.URL(request.url).params['reference']
            return json_response(200, subscription_body(reference))
        self.transport.on('GET', SUBSCRIPTION_LOOKUP_PATH, found)

        response = self.subscribe()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['subscriptionId'], 5501)
        self.assertEqual(len(self.transport.sent('POST', SUBSCRIPTIONS_PATH)), 1)
        claim = BillingClaim.objects.get(kind=BillingClaim.SUBSCRIPTION)
        lookup = self.transport.sent('GET', SUBSCRIPTION_LOOKUP_PATH)[0]
        self.assertEqual(httpx.URL(lookup.url).params['reference'], claim.reference)

    def test_an_unknown_outcome_is_settled_by_the_next_request_without_a_second_create(self) -> None:
        self.transport.on('POST', CUSTOMERS_PATH, customer_echo)
        self.transport.on('POST', SUBSCRIPTIONS_PATH, httpx.ReadTimeout('no reply'))
        self.transport.on('GET', SUBSCRIPTION_LOOKUP_PATH, json_response(404, {}))

        first = self.subscribe()
        self.assertEqual(first.status_code, 504)
        self.assertTrue(first.json()['outcomeUnknown'])
        claim = BillingClaim.objects.get(kind=BillingClaim.SUBSCRIPTION)
        self.assertEqual(claim.outcome, BillingClaim.UNKNOWN)

        self.transport.routes[('GET', SUBSCRIPTION_LOOKUP_PATH)] = [
            json_response(200, subscription_body(claim.reference))]
        second = self.subscribe()

        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(self.transport.sent('POST', SUBSCRIPTIONS_PATH)), 1)

    def test_unsent_is_not_the_same_failure_as_unknown(self) -> None:
        self.transport.on('POST', CUSTOMERS_PATH, httpx.ConnectError('refused'))
        unsent = self.subscribe()
        self.assertEqual((unsent.status_code, unsent.json()['outcomeUnknown']), (502, False))
        claim = BillingClaim.objects.get(kind=BillingClaim.CUSTOMER)
        self.assertEqual(claim.outcome, BillingClaim.FAILED)  # released
        self.assertEqual(self.transport.sent('GET', CUSTOMER_LOOKUP_PATH), [])

        self.transport.routes[('POST', CUSTOMERS_PATH)] = [httpx.ReadTimeout('no reply')]
        self.transport.on('GET', CUSTOMER_LOOKUP_PATH, json_response(404, {}))
        unknown = self.subscribe()
        self.assertEqual((unknown.status_code, unknown.json()['outcomeUnknown']), (504, True))
        lookup = self.transport.sent('GET', CUSTOMER_LOOKUP_PATH)[0]
        self.assertEqual(httpx.URL(lookup.url).params['reference'], claim.reference)

    def test_a_released_claim_is_resent_under_the_same_reference(self) -> None:
        self.transport.on('POST', CUSTOMERS_PATH, httpx.ConnectError('refused'), customer_echo)
        self.transport.on('POST', SUBSCRIPTIONS_PATH, subscription_echo())
        self.assertEqual(self.subscribe().status_code, 502)
        self.assertEqual(self.subscribe().status_code, 200)
        references = [body_of(r)['customer']['reference']
                      for r in self.transport.sent('POST', CUSTOMERS_PATH)]
        self.assertEqual(len(references), 2)
        self.assertEqual(references[0], references[1])

    def test_a_provider_validation_error_reaches_the_caller(self) -> None:
        self.transport.on('POST', CUSTOMERS_PATH, customer_echo)
        self.transport.on('POST', SUBSCRIPTIONS_PATH,
                          json_response(422, {'errors': ['Product must be active']}))
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['details'], ['Product must be active'])
        self.assertEqual(BillingClaim.objects.get(kind=BillingClaim.SUBSCRIPTION).outcome,
                         BillingClaim.FAILED)

    def test_an_unknown_plan_is_404_and_writes_nothing(self) -> None:
        response = self.subscribe(plan='no-such-plan')
        self.assertEqual(response.status_code, 404)
        self.assertFalse(BillingClaim.objects.exists())

    def test_missing_plan_handle_is_400(self) -> None:
        response = self.client.post('/api/subscriptions', data='{}', content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_anonymous_callers_are_401(self) -> None:
        self.client.logout()
        self.assertEqual(self.subscribe().status_code, 401)
        self.assertEqual(self.client.get('/api/my-subscriptions').status_code, 401)

    # Reading back

    def test_my_subscriptions_reads_back_from_maxio(self) -> None:
        self.transport.on('POST', CUSTOMERS_PATH, customer_echo)
        self.transport.on('POST', SUBSCRIPTIONS_PATH, subscription_echo())
        self.subscribe()
        claim = BillingClaim.objects.get(kind=BillingClaim.SUBSCRIPTION)
        self.transport.on('GET', '/customers/9001/subscriptions.json',
                          json_response(200, [subscription_body(claim.reference)]))

        response = self.client.get('/api/my-subscriptions')

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['customerId'], 9001)
        self.assertEqual(body['subscriptions'][0]['subscriptionId'], 5501)
        self.assertEqual(body['subscriptions'][0]['state'], 'active')
        self.assertEqual(body['unsettled'], [])

    def test_my_subscriptions_shows_requests_maxio_has_not_settled(self) -> None:
        BillingClaim.objects.create(
            key='customer:%s' % self.user.pk, reference='test-customer-old', kind=BillingClaim.CUSTOMER,
            user=self.user, outcome=BillingClaim.UNKNOWN, claimed_at=timezone.now() - timedelta(hours=1))
        body = self.client.get('/api/my-subscriptions').json()
        self.assertEqual(body['subscriptions'], [])
        self.assertEqual(body['unsettled'][0]['status'], 'unknown')


class ClientConfigurationTests(TestCase):

    @override_settings(MAXIO_API_KEY='k', MAXIO_SITE_SUBDOMAIN='acme', MAXIO_ENVIRONMENT='EU', MAXIO_BASE_URL='')
    def test_the_region_and_site_come_from_settings(self) -> None:
        self.assertEqual(maxio._server_config(), ('eu', {'production': {'eu': {'site': 'acme'}}}))

    @override_settings(MAXIO_API_KEY='k', MAXIO_SITE_SUBDOMAIN='acme', MAXIO_ENVIRONMENT='us',
                       MAXIO_BASE_URL='http://localhost:9999/mock')
    def test_the_base_url_override_is_used_verbatim(self) -> None:
        environment, server_config = maxio._server_config()
        self.assertEqual(server_config, {'production': {'us': {'base_url': 'http://localhost:9999/mock'}}})
        transport = RoutingTransport()
        transport.on('GET', '/mock/customers/lookup.json', json_response(404, {}))
        sdk = MaxioAdvancedBillingClient(environment=environment, server_config=server_config,
                                         custom_http_client=transport, basic_auth={'username': 'k', 'password': 'x'})
        with self.assertRaises(Exception):
            sdk.customers.read_customer_by_reference('r')
        self.assertTrue(transport.requests[0].url.startswith('http://localhost:9999/mock/customers/lookup.json'))

    @override_settings(MAXIO_API_KEY='k', MAXIO_SITE_SUBDOMAIN='acme', MAXIO_ENVIRONMENT='mars')
    def test_an_unknown_region_fails_instead_of_defaulting(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            maxio.build_client()

    @override_settings(MAXIO_API_KEY='', MAXIO_SITE_SUBDOMAIN='acme', MAXIO_ENVIRONMENT='us')
    def test_a_missing_api_key_fails_instead_of_sending_unauthenticated(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            maxio.build_client()
