"""
Tests for the Maxio subscription API.

Maxio is faked at the SDK's transport seam, so the SDK really builds and
decodes every request; the tests assert on what reached the "wire".
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from unittest import mock
from urllib.parse import unquote

import httpx
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from maxio_advanced_billing.core import HttpRequest, HttpResponse, JsonBody
from maxio_advanced_billing.models.enums import SubscriptionState

from apps.subscriptions import maxio, services
from apps.subscriptions.models import BillingCustomer, Outcome, SubscriptionEnrollment

PREFIX = 'test-install'
MAXIO_SETTINGS = dict(
    MAXIO_API_KEY='test-api-key',
    MAXIO_SITE_SUBDOMAIN='test-site',
    MAXIO_ENVIRONMENT='US',
    MAXIO_DEFAULT_PRODUCT_FAMILY='eshop-subscribe',
    MAXIO_BASE_URL='',
    MAXIO_TIMEOUT=5.0,
    MAXIO_REFERENCE_PREFIX=PREFIX,
    MAXIO_PAYMENT_COLLECTION_METHOD='',
)


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


class StubTransport:
    """Satisfies the SDK's sync transport protocol; answers per (method, path) route, in order."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, list[HttpResponse | Exception]]] = []
        self.requests: list[HttpRequest] = []

    def on(self, method: str, path: str, *answers: HttpResponse | Exception) -> StubTransport:
        self.routes.append((method, path, list(answers)))
        return self

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = unquote(httpx.URL(request.url).path)
        for method, route_path, answers in self.routes:
            if method == request.method and path == route_path and answers:
                answer = answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f'Unexpected request {request.method} {request.url}')

    def close(self) -> None:
        pass

    def sent(self, method: str, path: str) -> list[HttpRequest]:
        return [r for r in self.requests
                if r.method == method and unquote(httpx.URL(r.url).path) == path]


def body_of(request: HttpRequest) -> Any:
    assert isinstance(request.body, JsonBody)
    return request.body.value


def product(handle: str, price: int, name: str) -> dict[str, Any]:
    return {'id': hash(handle) % 10000, 'handle': handle, 'name': name, 'price_in_cents': price,
            'interval': 1, 'interval_unit': 'month', 'description': f'{name} description'}


PLANS = [{'product': product('basic-plan', 2900, 'Basic Plan')},
         {'product': product('eshop-pro', 29900, 'Pro Plan')}]
SITE = {'site': {'id': 1, 'subdomain': 'test-site', 'currency': 'USD', 'relationship_invoicing_enabled': True}}
CUSTOMER_ID = 5001


def customer_body(reference: str, customer_id: int = CUSTOMER_ID) -> dict[str, Any]:
    return {'customer': {'id': customer_id, 'reference': reference, 'first_name': 'Shop',
                         'last_name': 'Per', 'email': 'shopper@example.com'}}


def subscription_body(subscription_id: int, state: str, reference: str | None = None,
                      handle: str = 'eshop-pro', customer_id: int = CUSTOMER_ID) -> dict[str, Any]:
    return {'subscription': {
        'id': subscription_id, 'state': state, 'reference': reference,
        'product': product(handle, 29900, 'Pro Plan'),
        'customer': {'id': customer_id},
        'product_price_in_cents': 29900, 'currency': 'USD',
        'next_assessment_at': '2026-10-25T12:00:00Z',
        'current_period_ends_at': '2026-10-25T12:00:00Z',
        'created_at': '2026-09-25T12:00:00Z', 'updated_at': '2026-09-25T12:00:00Z',
    }}


@override_settings(**MAXIO_SETTINGS)
class MaxioApiTestCase(TestCase):

    def setUp(self) -> None:
        cache.clear()
        self.transport = StubTransport()
        self.transport.on('GET', '/site.json', *[json_response(200, SITE)] * 5)
        self.transport.on('GET', '/product_families/handle:eshop-subscribe/products.json',
                          *[json_response(200, PLANS)] * 5)
        client_ctx = maxio.use_client(maxio.build_client(transport=self.transport))
        client_ctx.__enter__()
        self.addCleanup(client_ctx.__exit__, None, None, None)

        self.user = get_user_model().objects.create_user(
            username='shopper', email='shopper@example.com', password='a-strong-password-1')
        self.client = Client()
        self.client.force_login(self.user)

    @property
    def customer_ref(self) -> str:
        return f'{PREFIX}-cust-u{self.user.pk}'

    def sub_ref(self, n: int = 0, plan: str = 'eshop-pro') -> str:
        return f'{PREFIX}-sub-u{self.user.pk}-{plan}-{n}'

    def expect_new_customer(self) -> None:
        self.transport.on('GET', '/customers/lookup.json', json_response(404, {}))
        self.transport.on('POST', '/customers.json', json_response(201, customer_body(self.customer_ref)))

    def subscribe(self, plan: str = 'eshop-pro', **headers: str) -> Any:
        return self.client.post('/api/subscriptions', data=json.dumps({'planHandle': plan}),
                                content_type='application/json', headers=headers)


class TestAuthentication(MaxioApiTestCase):

    def test_endpoints_require_a_session(self) -> None:
        anonymous = Client()
        for path in ('/api/subscription-plans', '/api/my-subscriptions', '/api/billing-customer'):
            self.assertEqual(anonymous.get(path).status_code, 401)
        response = anonymous.post('/api/subscriptions', data='{}', content_type='application/json')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.transport.requests, [])

    def test_session_login_and_csrf_are_enforced(self) -> None:
        browser = Client(enforce_csrf_checks=True)
        token = browser.get('/api/session').json()['csrfToken']
        rejected = browser.post('/api/session', data=json.dumps(
            {'username': 'shopper@example.com', 'password': 'a-strong-password-1'}),
            content_type='application/json')
        self.assertEqual(rejected.status_code, 403)
        response = browser.post('/api/session', data=json.dumps(
            {'username': 'shopper@example.com', 'password': 'a-strong-password-1'}),
            content_type='application/json', headers={'X-CSRFToken': token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['user']['email'], 'shopper@example.com')
        # Without the (rotated) token an authenticated POST is still refused
        self.assertEqual(browser.post('/api/subscriptions', data='{}',
                                      content_type='application/json').status_code, 403)

    def test_bad_password_is_rejected(self) -> None:
        response = Client().post('/api/session', data=json.dumps(
            {'username': 'shopper', 'password': 'wrong'}), content_type='application/json')
        self.assertEqual(response.status_code, 400)


class TestPlans(MaxioApiTestCase):

    def test_lists_plans_with_handles(self) -> None:
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 200)
        plans = {p['planHandle']: p for p in response.json()['plans']}
        self.assertEqual(set(plans), {'basic-plan', 'eshop-pro'})
        self.assertEqual(plans['eshop-pro']['priceInCents'], 29900)
        self.assertEqual(plans['eshop-pro']['price'], '299.00')
        self.assertEqual(plans['eshop-pro']['currency'], 'USD')
        self.assertEqual(plans['eshop-pro']['intervalUnit'], 'month')

        request = self.transport.sent('GET', '/product_families/handle:eshop-subscribe/products.json')[0]
        self.assertTrue(request.url.startswith('https://test-site.chargify.com/'))
        self.assertIn('per_page=200', request.url)
        self.assertTrue(request.headers['authorization'].startswith('Basic '))

    def test_base_url_override_is_used_verbatim(self) -> None:
        transport = StubTransport()
        transport.on('GET', '/proxy/site.json', json_response(200, SITE))
        transport.on('GET', '/proxy/product_families/handle:eshop-subscribe/products.json',
                     json_response(200, PLANS))
        with override_settings(MAXIO_BASE_URL='https://billing.example.test/proxy'):
            client = maxio.build_client(transport=transport)
        with maxio.use_client(client):
            response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(r.url.startswith('https://billing.example.test/proxy/') for r in transport.requests))

    def test_maxio_rejecting_our_credentials_is_a_502(self) -> None:
        self.transport.routes.clear()
        self.transport.on('GET', '/site.json', json_response(401, {'errors': ['bad key']}))
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['error']['code'], 'billing_provider_auth')

    def test_reads_are_retried_on_transient_failures(self) -> None:
        self.transport.routes.clear()
        self.transport.on('GET', '/site.json', httpx.ConnectError('refused'), json_response(200, SITE))
        self.transport.on('GET', '/product_families/handle:eshop-subscribe/products.json',
                          json_response(503, {}), json_response(200, PLANS))
        with mock.patch.object(maxio, 'READ_BACKOFF_SECONDS', (0.0, 0.0)):
            response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 200)

    def test_missing_configuration_is_a_503(self) -> None:
        with override_settings(MAXIO_DEFAULT_PRODUCT_FAMILY=''):
            response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 503)


class TestBillingCustomer(MaxioApiTestCase):

    def test_ensure_customer_creates_once(self) -> None:
        self.transport.on('POST', '/customers.json', json_response(201, customer_body(self.customer_ref)))
        first = self.client.post('/api/billing-customer')
        second = self.client.post('/api/billing-customer')
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()['customerId'], CUSTOMER_ID)
        creates = self.transport.sent('POST', '/customers.json')
        self.assertEqual(len(creates), 1)
        sent = body_of(creates[0])['customer']
        self.assertEqual(sent['reference'], self.customer_ref)
        self.assertEqual(sent['email'], 'shopper@example.com')

    def test_duplicate_reference_rejection_adopts_the_existing_customer(self) -> None:
        self.transport.on('POST', '/customers.json',
                          json_response(422, {'errors': ['Reference must be unique']}))
        self.transport.on('GET', '/customers/lookup.json', json_response(200, customer_body(self.customer_ref)))
        response = self.client.post('/api/billing-customer')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(BillingCustomer.objects.get(user=self.user).outcome, Outcome.DONE)

    def test_timeout_then_lookup_miss_is_unknown_then_same_reference_resend(self) -> None:
        self.transport.on('POST', '/customers.json', httpx.ReadTimeout('no reply'),
                          json_response(201, customer_body(self.customer_ref)))
        self.transport.on('GET', '/customers/lookup.json', json_response(404, {}), json_response(404, {}))
        first = self.client.post('/api/billing-customer')
        self.assertEqual(first.status_code, 504)
        self.assertTrue(first.json()['error']['outcomeUnknown'])
        self.assertEqual(BillingCustomer.objects.get(user=self.user).outcome, Outcome.UNKNOWN)

        second = self.client.post('/api/billing-customer')
        self.assertEqual(second.status_code, 201)
        creates = self.transport.sent('POST', '/customers.json')
        self.assertEqual([body_of(r)['customer']['reference'] for r in creates], [self.customer_ref] * 2)


class TestSubscribe(MaxioApiTestCase):

    def test_subscribe_returns_subscription_id_and_confirms_details(self) -> None:
        self.expect_new_customer()
        self.transport.on('POST', '/subscriptions.json',
                          json_response(201, subscription_body(777, 'active', self.sub_ref())))
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data['subscriptionId'], 777)
        self.assertEqual(data['planHandle'], 'eshop-pro')
        self.assertEqual(data['state'], 'active')
        self.assertEqual(data['outcome'], 'done')
        self.assertEqual(data['price'], '299.00')
        self.assertEqual(data['nextBillingAt'], '2026-10-25T12:00:00+00:00')

        sent = body_of(self.transport.sent('POST', '/subscriptions.json')[0])['subscription']
        self.assertEqual(sent, {'product_handle': 'eshop-pro', 'customer_id': CUSTOMER_ID,
                                'reference': self.sub_ref(), 'payment_collection_method': 'remittance'})

    def test_collection_method_can_be_configured(self) -> None:
        self.expect_new_customer()
        self.transport.on('POST', '/subscriptions.json',
                          json_response(201, subscription_body(776, 'active', self.sub_ref())))
        with override_settings(MAXIO_PAYMENT_COLLECTION_METHOD='Automatic'):
            self.assertEqual(self.subscribe().status_code, 201)
        sent = body_of(self.transport.sent('POST', '/subscriptions.json')[0])['subscription']
        self.assertEqual(sent['payment_collection_method'], 'automatic')

    def test_the_same_request_twice_sends_one_create(self) -> None:
        self.expect_new_customer()
        self.transport.on('POST', '/subscriptions.json',
                          json_response(201, subscription_body(777, 'active', self.sub_ref())))
        first = self.subscribe()
        second = self.subscribe()
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(second.json()['subscriptionId'], first.json()['subscriptionId'])
        self.assertEqual(len(self.transport.sent('POST', '/subscriptions.json')), 1)
        self.assertEqual(len(self.transport.sent('POST', '/customers.json')), 1)

    def test_a_request_while_one_is_in_flight_makes_no_provider_call(self) -> None:
        customer = BillingCustomer.objects.create(
            user=self.user, reference=self.customer_ref, maxio_customer_id=CUSTOMER_ID,
            outcome=Outcome.DONE, claimed_at=timezone.now())
        SubscriptionEnrollment.objects.create(
            user=self.user, customer=customer, plan_handle='eshop-pro', reference=self.sub_ref(),
            outcome=Outcome.SENDING, claimed_at=timezone.now())
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['error']['code'], 'in_progress')
        self.assertEqual(self.transport.sent('POST', '/subscriptions.json'), [])

    def test_the_database_rejects_a_second_live_enrollment(self) -> None:
        customer = BillingCustomer.objects.create(
            user=self.user, reference=self.customer_ref, outcome=Outcome.DONE, claimed_at=timezone.now())
        SubscriptionEnrollment.objects.create(
            user=self.user, customer=customer, plan_handle='eshop-pro', reference='a',
            outcome=Outcome.PENDING, claimed_at=timezone.now())
        with self.assertRaises(IntegrityError), transaction.atomic():
            SubscriptionEnrollment.objects.create(
                user=self.user, customer=customer, plan_handle='eshop-pro', reference='b',
                outcome=Outcome.SENDING, claimed_at=timezone.now())

    def test_idempotency_key_derives_the_reference(self) -> None:
        self.expect_new_customer()
        self.transport.on('POST', '/subscriptions.json', json_response(201, subscription_body(778, 'active')))
        first = self.subscribe(**{'Idempotency-Key': 'order-42'})
        again = self.subscribe(**{'Idempotency-Key': 'order-42'})
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        reference = body_of(self.transport.sent('POST', '/subscriptions.json')[0])['subscription']['reference']
        self.assertTrue(reference.startswith(f'{PREFIX}-sub-u{self.user.pk}-k'))
        other_plan = self.subscribe('basic-plan', **{'Idempotency-Key': 'order-42'})
        self.assertEqual(other_plan.status_code, 422)

    def test_unknown_plan_is_rejected_before_any_write(self) -> None:
        response = self.subscribe('no-such-plan')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.transport.sent('POST', '/customers.json'), [])

    def test_pending_state_is_accepted_not_done(self) -> None:
        self.expect_new_customer()
        self.transport.on('POST', '/subscriptions.json', json_response(201, subscription_body(779, 'pending')))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['subscriptionId'], 779)
        self.assertEqual(response.json()['outcome'], 'pending')

    def test_an_id_with_a_failed_state_is_not_done(self) -> None:
        self.expect_new_customer()
        self.transport.on('POST', '/subscriptions.json',
                          json_response(201, subscription_body(780, 'failed_to_create')))
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['outcome'], 'failed')

    def test_an_unlisted_state_is_unknown_not_done(self) -> None:
        self.expect_new_customer()
        self.transport.on('POST', '/subscriptions.json',
                          json_response(201, subscription_body(781, 'something_new')))
        response = self.subscribe()
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()['outcome'], 'unknown')

    def test_a_different_plan_back_needs_review(self) -> None:
        self.expect_new_customer()
        self.transport.on('POST', '/subscriptions.json',
                          json_response(201, subscription_body(782, 'active', handle='basic-plan')))
        response = self.subscribe()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(SubscriptionEnrollment.objects.get().outcome, Outcome.NEEDS_REVIEW)

    def test_refused_connection_is_known_and_released(self) -> None:
        self.expect_new_customer()
        self.transport.on('POST', '/subscriptions.json', httpx.ConnectError('refused'),
                          json_response(201, subscription_body(783, 'active', self.sub_ref())))
        unsent = self.subscribe()
        self.assertEqual(unsent.status_code, 502)
        self.assertFalse(unsent.json()['error']['outcomeUnknown'])
        self.assertEqual(self.transport.sent('GET', '/subscriptions/lookup.json'), [])

        retried = self.subscribe()
        self.assertEqual(retried.status_code, 201)
        references = [body_of(r)['subscription']['reference']
                      for r in self.transport.sent('POST', '/subscriptions.json')]
        self.assertEqual(references, [self.sub_ref(), self.sub_ref()])

    def test_read_timeout_is_looked_up_by_reference(self) -> None:
        self.expect_new_customer()
        self.transport.on('POST', '/subscriptions.json', httpx.ReadTimeout('no reply'))
        self.transport.on('GET', '/subscriptions/lookup.json',
                          json_response(200, subscription_body(784, 'active', self.sub_ref())))
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['subscriptionId'], 784)
        lookup = self.transport.sent('GET', '/subscriptions/lookup.json')[0]
        self.assertIn(f'reference={self.sub_ref()}', unquote(lookup.url))

    def test_unconfirmed_create_stays_unknown_and_is_never_resent(self) -> None:
        self.expect_new_customer()
        self.transport.on('POST', '/subscriptions.json', httpx.ReadTimeout('no reply'))
        self.transport.on('GET', '/subscriptions/lookup.json', json_response(404, {}),
                          json_response(200, subscription_body(785, 'active', self.sub_ref())))
        unknown = self.subscribe()
        self.assertEqual(unknown.status_code, 504)
        self.assertTrue(unknown.json()['error']['outcomeUnknown'])
        self.assertEqual(SubscriptionEnrollment.objects.get().outcome, Outcome.UNKNOWN)

        settled = self.subscribe()
        self.assertEqual(settled.status_code, 200)
        self.assertEqual(settled.json()['subscriptionId'], 785)
        self.assertEqual(len(self.transport.sent('POST', '/subscriptions.json')), 1)

    def test_unsent_and_unknown_are_different_failures(self) -> None:
        self.expect_new_customer()
        self.transport.on('POST', '/subscriptions.json', httpx.ConnectError('refused'), httpx.ReadTimeout('late'))
        self.transport.on('GET', '/subscriptions/lookup.json', json_response(404, {}))
        unsent = self.subscribe().json()
        unknown = self.subscribe().json()
        self.assertEqual((unsent['error']['code'], unsent['error']['outcomeUnknown']),
                         ('billing_provider_unreachable', False))
        self.assertEqual((unknown['error']['code'], unknown['error']['outcomeUnknown']),
                         ('billing_outcome_unknown', True))
        self.assertEqual(len(self.transport.sent('GET', '/subscriptions/lookup.json')), 1)

    def test_validation_rejection_is_returned_and_released(self) -> None:
        self.expect_new_customer()
        self.transport.on('POST', '/subscriptions.json',
                          json_response(422, {'errors': ['Product must be active']}))
        self.transport.on('GET', '/subscriptions/lookup.json', json_response(404, {}))
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['details'], ['Product must be active'])
        enrollment = SubscriptionEnrollment.objects.get()
        self.assertEqual((enrollment.outcome, enrollment.maxio_subscription_id), (Outcome.FAILED, None))

    def test_stale_sending_claim_is_checked_not_recreated(self) -> None:
        customer = BillingCustomer.objects.create(
            user=self.user, reference=self.customer_ref, maxio_customer_id=CUSTOMER_ID,
            outcome=Outcome.DONE, claimed_at=timezone.now())
        SubscriptionEnrollment.objects.create(
            user=self.user, customer=customer, plan_handle='eshop-pro', reference=self.sub_ref(),
            outcome=Outcome.SENDING, claimed_at=timezone.now() - timedelta(hours=1))
        self.transport.on('GET', '/subscriptions/lookup.json',
                          json_response(200, subscription_body(786, 'active', self.sub_ref())))
        response = self.subscribe()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.transport.sent('POST', '/subscriptions.json'), [])


class TestReadingBack(MaxioApiTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.customer = BillingCustomer.objects.create(
            user=self.user, reference=self.customer_ref, maxio_customer_id=CUSTOMER_ID,
            outcome=Outcome.DONE, claimed_at=timezone.now())

    def test_my_subscriptions_reads_maxio_and_releases_ended_plans(self) -> None:
        SubscriptionEnrollment.objects.create(
            user=self.user, customer=self.customer, plan_handle='eshop-pro', reference=self.sub_ref(),
            maxio_subscription_id=790, outcome=Outcome.DONE, claimed_at=timezone.now())
        self.transport.on('GET', f'/customers/{CUSTOMER_ID}/subscriptions.json',
                          json_response(200, [subscription_body(790, 'canceled', self.sub_ref())]))
        response = self.client.get('/api/my-subscriptions')
        self.assertEqual(response.status_code, 200)
        [listed] = response.json()['subscriptions']
        self.assertEqual((listed['subscriptionId'], listed['state'], listed['outcome']),
                         (790, 'canceled', 'failed'))
        self.assertEqual(SubscriptionEnrollment.objects.get().outcome, Outcome.FAILED)
        # The plan is free again, under a new reference
        self.assertEqual(services.subscription_reference(self.user, 'eshop-pro', ''), self.sub_ref(1))

    def test_my_subscriptions_reconciles_unknown_creates(self) -> None:
        SubscriptionEnrollment.objects.create(
            user=self.user, customer=self.customer, plan_handle='eshop-pro', reference=self.sub_ref(),
            outcome=Outcome.UNKNOWN, claimed_at=timezone.now())
        self.transport.on('GET', '/subscriptions/lookup.json',
                          json_response(200, subscription_body(791, 'active', self.sub_ref())))
        self.transport.on('GET', f'/customers/{CUSTOMER_ID}/subscriptions.json',
                          json_response(200, [subscription_body(791, 'active', self.sub_ref())]))
        data = self.client.get('/api/my-subscriptions').json()
        self.assertEqual([s['subscriptionId'] for s in data['subscriptions']], [791])
        self.assertEqual(data['unconfirmed'], [])
        self.assertEqual(SubscriptionEnrollment.objects.get().outcome, Outcome.DONE)

    def test_subscription_detail_hides_other_customers(self) -> None:
        self.transport.on('GET', '/subscriptions/792.json',
                          json_response(200, subscription_body(792, 'active', customer_id=9999)))
        self.assertEqual(self.client.get('/api/subscriptions/792').status_code, 404)

    def test_subscription_detail(self) -> None:
        self.transport.on('GET', '/subscriptions/793.json', json_response(200, subscription_body(793, 'active')))
        response = self.client.get('/api/subscriptions/793')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['subscriptionId'], 793)


class TestStatusFromProvider(TestCase):

    def test_every_state_is_mapped_deliberately(self) -> None:
        expected = {
            SubscriptionState.ACTIVE: Outcome.DONE, SubscriptionState.TRIALING: Outcome.DONE,
            SubscriptionState.PENDING: Outcome.PENDING, SubscriptionState.ASSESSING: Outcome.PENDING,
            SubscriptionState.AWAITING_SIGNUP: Outcome.PENDING, SubscriptionState.PAUSED: Outcome.PENDING,
            SubscriptionState.SOFT_FAILURE: Outcome.PENDING, SubscriptionState.PAST_DUE: Outcome.PENDING,
            SubscriptionState.UNPAID: Outcome.PENDING, SubscriptionState.ON_HOLD: Outcome.PENDING,
            SubscriptionState.SUSPENDED: Outcome.PENDING,
            SubscriptionState.FAILED_TO_CREATE: Outcome.FAILED, SubscriptionState.CANCELED: Outcome.FAILED,
            SubscriptionState.EXPIRED: Outcome.FAILED, SubscriptionState.TRIAL_ENDED: Outcome.FAILED,
        }
        self.assertEqual(set(expected), set(SubscriptionState))
        for state, outcome in expected.items():
            self.assertEqual(services.status_from_provider(state), outcome, state)
            self.assertEqual(services.status_from_provider(state.value), outcome, state)
        self.assertEqual(services.status_from_provider('brand_new_state'), Outcome.UNKNOWN)
        self.assertEqual(services.status_from_provider(None), Outcome.UNKNOWN)
