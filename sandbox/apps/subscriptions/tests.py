"""
Tests for the subscription billing API. Maxio is faked at the SDK's transport
seam, so the real request-building and decoding pipeline runs.

Run with:  cd sandbox && python manage.py test apps.subscriptions
"""
import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from maxio_advanced_billing.core import HttpRequest, HttpResponse, JsonBody

from . import maxio, service
from .models import MaxioCustomer, Outcome, SubscriptionEnrollment

Handler = Callable[[HttpRequest], HttpResponse]

MAXIO_SETTINGS = dict(
    MAXIO_API_KEY='test-key',
    MAXIO_SITE_SUBDOMAIN='test-site',
    MAXIO_ENVIRONMENT='US',
    MAXIO_DEFAULT_PRODUCT_FAMILY='test-family',
    MAXIO_BASE_URL=None,
    MAXIO_REFERENCE_PREFIX='test',
)


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


def sent(request: HttpRequest, key: str) -> dict[str, Any]:
    """The JSON body the SDK built (wire names), under its top-level ``key``."""
    assert isinstance(request.body, JsonBody)
    value = request.body.value
    assert isinstance(value, dict)
    inner = value[key]
    assert isinstance(inner, dict)
    return inner


def empty(status: int) -> HttpResponse:
    return HttpResponse(status_code=status, headers={})


class RoutingTransport:
    """Answers by (method, path); a queued item may be an exception to raise."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[HttpResponse | Exception]] = {}
        self.requests: list[HttpRequest] = []

    def on(self, method: str, path: str, *answers: HttpResponse | Exception) -> None:
        self.routes.setdefault((method, path), []).extend(answers)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = httpx.URL(request.url).path
        queue = self.routes.get((request.method, path))
        if not queue:
            raise AssertionError(f'unexpected Maxio call {request.method} {request.url}')
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self) -> None:
        pass

    def calls(self, method: str, path: str) -> list[HttpRequest]:
        return [r for r in self.requests
                if r.method == method and httpx.URL(r.url).path == path]


def product(handle: str, cents: int, **extra: Any) -> dict[str, Any]:
    return {'product': {'id': abs(hash(handle)) % 10000, 'handle': handle, 'name': handle.title(),
                        'price_in_cents': cents, 'interval': 1, 'interval_unit': 'month',
                        'require_credit_card': False, **extra}}


def customer_body(cid: int = 11, reference: str = 'ref') -> dict[str, Any]:
    return {'customer': {'id': cid, 'reference': reference, 'first_name': 'A', 'last_name': 'B',
                         'email': 'shopper@example.com', 'created_at': '2026-09-24T10:00:00Z'}}


def subscription_body(sid: int = 501, state: str = 'active', cents: int = 29900,
                      customer_id: int = 11, reference: str = 'r') -> dict[str, Any]:
    return {'subscription': {
        'id': sid, 'state': state, 'product_price_in_cents': cents, 'currency': 'USD',
        'reference': reference, 'next_assessment_at': '2026-10-24T10:00:00Z',
        'current_period_ends_at': '2026-10-24T10:00:00Z', 'created_at': '2026-09-24T10:00:00Z',
        'customer': {'id': customer_id},
        'product': {'id': 1, 'handle': 'eshop-pro', 'name': 'Pro Plan', 'price_in_cents': cents,
                    'interval': 1, 'interval_unit': 'month'}}}


PLANS = '/product_families/handle:test-family/products.json'
CUSTOMERS = '/customers.json'
CUSTOMER_LOOKUP = '/customers/lookup.json'
SUBSCRIPTIONS = '/subscriptions.json'
SUBSCRIPTION_LOOKUP = '/subscriptions/lookup.json'


@override_settings(**MAXIO_SETTINGS)
class SubscriptionApiTests(TestCase):

    def setUp(self) -> None:
        self.transport = RoutingTransport()
        maxio.set_client(maxio.build_client(self.transport))
        self.addCleanup(maxio.set_client, None)
        service._collection_method = None
        self.transport.on('GET', '/site.json', json_response(200, {'site': {
            'id': 1, 'subdomain': 'test-site', 'relationship_invoicing_enabled': True}}))
        self.transport.on('GET', PLANS, json_response(200, [
            product('eshop-pro', 29900), product('basic-plan', 2900),
            product('old-plan', 100, archived_at='2026-01-01T00:00:00Z')]))
        self.user = get_user_model().objects.create_user(
            username='shopper', email='shopper@example.com', password='a-long-password-1')
        self.client.force_login(self.user)

    def subscribe(self, plan: str = 'eshop-pro') -> Any:
        return self.client.post('/api/subscriptions', data={'planHandle': plan},
                                content_type='application/json')

    def customer_reference(self) -> str:
        return f'test:user:{self.user.pk}:{int(self.user.date_joined.timestamp())}'

    def sub_reference(self, plan: str = 'eshop-pro', attempt: int = 1) -> str:
        return f'{self.customer_reference()}:sub:{plan}:{attempt}'

    def given_customer_created(self) -> None:
        self.transport.on('POST', CUSTOMERS, json_response(201, customer_body()))

    # ------------------------------------------------------------ auth

    def test_anonymous_caller_gets_401(self) -> None:
        self.client.logout()
        for path in ('/api/subscription-plans', '/api/my-subscriptions'):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.subscribe().status_code, 401)
        self.assertEqual(self.transport.requests, [])

    # ----------------------------------------------------------- plans

    def test_plans_are_listed_by_family_handle_with_plan_handle(self) -> None:
        response = self.client.get('/api/subscription-plans')

        self.assertEqual(response.status_code, 200)
        plans = response.json()['plans']
        self.assertEqual([p['planHandle'] for p in plans], ['eshop-pro', 'basic-plan'])
        self.assertEqual(plans[0]['price'], '299.00')
        request = self.transport.requests[0]
        self.assertTrue(request.url.startswith('https://test-site.chargify.com/'))
        self.assertTrue(request.headers['authorization'].startswith('Basic '))

    @override_settings(MAXIO_BASE_URL='https://billing.example.test')
    def test_base_url_override_is_used_verbatim(self) -> None:
        maxio.set_client(maxio.build_client(self.transport))
        self.client.get('/api/subscription-plans')
        self.assertTrue(self.transport.requests[0].url.startswith('https://billing.example.test/'))

    def test_maxio_auth_failure_is_our_502_not_the_callers_401(self) -> None:
        self.transport.routes[('GET', PLANS)] = [empty(401)]
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['outcomeUnknown'], False)

    # ------------------------------------------------------- subscribe

    def test_subscribe_creates_customer_then_subscription(self) -> None:
        self.given_customer_created()
        self.transport.on('POST', SUBSCRIPTIONS, json_response(201, subscription_body()))

        response = self.subscribe()

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['subscriptionId'], 501)
        self.assertEqual(body['outcome'], 'done')
        self.assertEqual(body['state'], 'active')
        self.assertEqual(body['price'], '299.00')
        self.assertEqual(body['nextBillingAt'], '2026-10-24T10:00:00+00:00')

        customer_req = self.transport.calls('POST', CUSTOMERS)[0]
        self.assertEqual(sent(customer_req, 'customer')['reference'], self.customer_reference())
        sub_req = self.transport.calls('POST', SUBSCRIPTIONS)[0]
        self.assertEqual(sent(sub_req, 'subscription'), {
            'product_handle': 'eshop-pro', 'customer_id': 11, 'reference': self.sub_reference(),
            'payment_collection_method': 'remittance'})
        self.assertEqual(sub_req.headers['idempotency-key'],
                         str(uuid.uuid5(uuid.NAMESPACE_URL, self.sub_reference())))

    def test_the_same_request_twice_sends_one_customer_and_one_subscription(self) -> None:
        self.given_customer_created()
        self.transport.on('POST', SUBSCRIPTIONS, json_response(201, subscription_body()))

        first = self.subscribe()
        second = self.subscribe()

        self.assertEqual(len(self.transport.calls('POST', CUSTOMERS)), 1)
        self.assertEqual(len(self.transport.calls('POST', SUBSCRIPTIONS)), 1)
        self.assertEqual(first.json()['subscriptionId'], second.json()['subscriptionId'])
        self.assertEqual(second.status_code, 201)
        self.assertEqual(SubscriptionEnrollment.objects.count(), 1)

    def test_a_request_finding_a_fresh_claim_answers_in_progress_without_calling(self) -> None:
        self.given_customer_created()
        self.transport.on('POST', SUBSCRIPTIONS, json_response(201, subscription_body()))
        self.client.post('/api/maxio-customer')
        SubscriptionEnrollment.objects.create(
            user=self.user, plan_handle='eshop-pro', attempt=1, reference=self.sub_reference(),
            expected_price_in_cents=29900, outcome=Outcome.SENDING,
            claimed_at=MaxioCustomer.objects.get().claimed_at)

        response = self.subscribe()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['outcome'], 'in_progress')
        self.assertEqual(self.transport.calls('POST', SUBSCRIPTIONS), [])

    def test_an_unlisted_state_is_unknown_not_done(self) -> None:
        self.given_customer_created()
        self.transport.on('POST', SUBSCRIPTIONS,
                          json_response(201, subscription_body(state='something_new')))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['outcome'], 'unknown')
        self.assertEqual(response.json()['subscriptionId'], 501)

    def test_a_pending_state_is_accepted_not_done(self) -> None:
        self.given_customer_created()
        self.transport.on('POST', SUBSCRIPTIONS, json_response(201, subscription_body(state='past_due')))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['outcome'], 'pending')

    def test_a_failed_state_is_not_done_and_a_later_request_starts_a_new_attempt(self) -> None:
        self.given_customer_created()
        self.transport.on('POST', SUBSCRIPTIONS,
                          json_response(201, subscription_body(sid=501, state='failed_to_create')),
                          json_response(201, subscription_body(sid=502)))
        first = self.subscribe()
        self.assertEqual(first.status_code, 409)
        self.assertEqual(first.json()['subscriptionId'], 501)

        second = self.subscribe()
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()['subscriptionId'], 502)
        second_req = self.transport.calls('POST', SUBSCRIPTIONS)[1]
        self.assertEqual(sent(second_req, 'subscription')['reference'], self.sub_reference(attempt=2))

    def test_a_price_other_than_the_catalogue_is_needs_review(self) -> None:
        self.given_customer_created()
        self.transport.on('POST', SUBSCRIPTIONS, json_response(201, subscription_body(cents=100)))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['outcome'], 'needs_review')

    def test_a_truncated_success_body_is_unknown_not_success(self) -> None:
        self.given_customer_created()
        self.transport.on('POST', SUBSCRIPTIONS, json_response(201, {}))
        self.transport.on('GET', SUBSCRIPTION_LOOKUP, empty(404))
        response = self.subscribe()
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()['outcomeUnknown'], True)
        self.assertEqual(len(self.transport.calls('GET', SUBSCRIPTION_LOOKUP)), 1)
        self.assertEqual(SubscriptionEnrollment.objects.get().outcome, Outcome.UNKNOWN)

    def test_a_plan_requiring_a_card_is_refused_before_anything_is_claimed(self) -> None:
        self.transport.routes[('GET', PLANS)] = [json_response(200, [
            product('eshop-pro', 29900, require_credit_card=True)])]
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(MaxioCustomer.objects.count(), 0)
        self.assertEqual(self.transport.calls('POST', CUSTOMERS), [])

    def test_unknown_plan_is_404_and_claims_nothing(self) -> None:
        response = self.subscribe('nope')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(MaxioCustomer.objects.count(), 0)

    def test_missing_plan_handle_is_400(self) -> None:
        response = self.client.post('/api/subscriptions', data={}, content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_a_provider_refusal_passes_through_and_releases_the_claim(self) -> None:
        self.given_customer_created()
        self.transport.on('POST', SUBSCRIPTIONS, json_response(422, {'errors': ['Bad product']}))
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['detail'], ['Bad product'])
        self.assertEqual(SubscriptionEnrollment.objects.count(), 0)

    # ------------------------------------------- transport failures

    def test_unsent_is_not_the_same_failure_as_unknown(self) -> None:
        self.given_customer_created()
        self.transport.on('POST', SUBSCRIPTIONS, httpx.ConnectError('refused'))
        unsent = self.subscribe()
        self.assertEqual((unsent.status_code, unsent.json()['outcomeUnknown']), (502, False))
        self.assertEqual(SubscriptionEnrollment.objects.count(), 0)   # claim released
        self.assertEqual(self.transport.calls('GET', SUBSCRIPTION_LOOKUP), [])

        self.transport.routes[('POST', SUBSCRIPTIONS)] = [httpx.ReadTimeout('no reply')]
        self.transport.on('GET', SUBSCRIPTION_LOOKUP, empty(404))
        unknown = self.subscribe()
        self.assertEqual((unknown.status_code, unknown.json()['outcomeUnknown']), (504, True))
        self.assertEqual(SubscriptionEnrollment.objects.get().outcome, Outcome.UNKNOWN)
        lookups = self.transport.calls('GET', SUBSCRIPTION_LOOKUP)
        self.assertEqual(len(lookups), 1)
        self.assertEqual(httpx.URL(lookups[0].url).params['reference'], self.sub_reference())

    def test_an_unknown_outcome_is_settled_by_lookup_never_by_a_new_create(self) -> None:
        self.given_customer_created()
        self.transport.on('POST', SUBSCRIPTIONS, httpx.ReadTimeout('no reply'))
        self.transport.on('GET', SUBSCRIPTION_LOOKUP, empty(404),
                          json_response(200, subscription_body(reference=self.sub_reference())))
        self.assertEqual(self.subscribe().status_code, 504)

        settled = self.subscribe()

        self.assertEqual(settled.status_code, 201)
        self.assertEqual(settled.json()['subscriptionId'], 501)
        self.assertEqual(len(self.transport.calls('POST', SUBSCRIPTIONS)), 1)

    def test_a_5xx_on_create_that_landed_is_found_by_reference(self) -> None:
        self.given_customer_created()
        self.transport.on('POST', SUBSCRIPTIONS, empty(502))
        self.transport.on('GET', SUBSCRIPTION_LOOKUP, json_response(200, subscription_body()))
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['subscriptionId'], 501)

    # ------------------------------------------------------ customer

    def test_customer_already_at_maxio_under_our_reference_is_adopted(self) -> None:
        self.transport.on('POST', CUSTOMERS, json_response(422, {'errors': {'reference': ['taken']}}))
        self.transport.on('GET', CUSTOMER_LOOKUP, json_response(200, customer_body(cid=77)))
        response = self.client.post('/api/maxio-customer')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['customerId'], 77)

    def test_customer_refused_by_maxio_releases_the_claim(self) -> None:
        self.transport.on('POST', CUSTOMERS, json_response(422, {'errors': {'email': ['bad']}}))
        self.transport.on('GET', CUSTOMER_LOOKUP, empty(404))
        response = self.client.post('/api/maxio-customer')
        self.assertEqual(response.status_code, 422)
        self.assertEqual(MaxioCustomer.objects.count(), 0)

    def test_unknown_customer_is_settled_by_same_reference_resend(self) -> None:
        self.transport.on('POST', CUSTOMERS, httpx.ReadTimeout('no reply'),
                          json_response(422, {'errors': {'reference': ['taken']}}))
        self.transport.on('GET', CUSTOMER_LOOKUP, empty(404), json_response(200, customer_body(cid=5)))
        self.assertEqual(self.client.post('/api/maxio-customer').status_code, 504)

        response = self.client.post('/api/maxio-customer')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['customerId'], 5)
        refs = {sent(r, 'customer')['reference'] for r in self.transport.calls('POST', CUSTOMERS)}
        self.assertEqual(refs, {self.customer_reference()})

    # ---------------------------------------------------- read back

    def test_my_subscriptions_reads_back_and_reconciles(self) -> None:
        self.given_customer_created()
        self.transport.on('POST', SUBSCRIPTIONS, json_response(201, subscription_body()))
        self.subscribe()
        self.transport.on('GET', '/customers/11/subscriptions.json', json_response(200, [
            subscription_body(state='canceled', reference=self.sub_reference())]))

        response = self.client.get('/api/my-subscriptions')

        self.assertEqual(response.status_code, 200)
        subs = response.json()['subscriptions']
        self.assertEqual([(s['subscriptionId'], s['state'], s['outcome']) for s in subs],
                         [(501, 'canceled', 'failed')])
        self.assertEqual(SubscriptionEnrollment.objects.get().outcome, Outcome.FAILED)

    def test_my_subscriptions_without_customer_is_empty_and_calls_nothing(self) -> None:
        response = self.client.get('/api/my-subscriptions')
        self.assertEqual(response.json()['subscriptions'], [])
        self.assertEqual(self.transport.requests, [])

    def test_subscription_detail_hides_other_customers(self) -> None:
        self.given_customer_created()
        self.client.post('/api/maxio-customer')
        self.transport.on('GET', '/subscriptions/900.json',
                          json_response(200, subscription_body(sid=900, customer_id=999)))
        self.assertEqual(self.client.get('/api/subscriptions/900').status_code, 404)
