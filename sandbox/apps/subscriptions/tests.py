"""
Tests for the subscription billing API. The Maxio SDK is exercised for real
down to its transport; the transport is a stub, so nothing leaves the process.

Run with:  sandbox/manage.py test apps.subscriptions
"""
import json
from datetime import datetime, timezone as dt_timezone
from typing import Any

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from maxio_advanced_billing.core import HttpRequest, HttpResponse, JsonBody
from maxio_advanced_billing.models import (
    Customer, CustomerResponse, Product, ProductResponse, Subscription, SubscriptionResponse)
from maxio_advanced_billing.models.enums import IntervalUnit, SubscriptionState

from .maxio import build_client, set_client, subscription_outcome
from .models import ProviderWrite
from .services import customer_reference, subscription_reference

MAXIO_SETTINGS = dict(
    MAXIO_API_KEY='test-key', MAXIO_SITE_SUBDOMAIN='test-site', MAXIO_DEFAULT_PRODUCT_FAMILY='eshop-subscribe',
    MAXIO_BASE_URL='', MAXIO_ENVIRONMENT='US', MAXIO_TIMEOUT=5.0, MAXIO_REFERENCE_PREFIX='test',
    MAXIO_PAYMENT_COLLECTION_METHOD='remittance')

CREATED = datetime(2026, 9, 25, 12, 0, tzinfo=dt_timezone.utc)
NEXT_BILLING = datetime(2026, 10, 25, 12, 0, tzinfo=dt_timezone.utc)


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


class StubTransport:
    """Answers queued responses in order (or raises a queued exception) and records every request."""

    def __init__(self, *responses: HttpResponse | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        answer = self.responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self) -> None:
        pass

    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.split('?')[0].split('.com', 1)[1]) for r in self.requests]


def plans_body() -> list[dict[str, Any]]:
    return [
        ProductResponse(product=Product(
            id=1, handle='eshop-pro', name='Pro Plan', price_in_cents=29900, interval=1,
            interval_unit=IntervalUnit.MONTH, require_credit_card=False)).to_dict(),
        ProductResponse(product=Product(
            id=2, handle='basic-plan', name='Basic Plan', price_in_cents=2900, interval=1,
            interval_unit=IntervalUnit.MONTH, require_credit_card=False)).to_dict(),
        ProductResponse(product=Product(
            id=3, handle='old-plan', name='Old', price_in_cents=100, archived_at=CREATED)).to_dict(),
    ]


def customer_body(customer_id: int = 500, reference: str = '') -> dict[str, Any]:
    return CustomerResponse(customer=Customer(
        id=customer_id, reference=reference, email='shopper@example.com', created_at=CREATED)).to_dict()


def subscription_body(sub_id: int = 900, state: str = 'active', handle: str = 'eshop-pro',
                      reference: str = '') -> dict[str, Any]:
    return SubscriptionResponse(subscription=Subscription(
        id=sub_id, state=state, reference=reference, product_price_in_cents=29900,
        next_assessment_at=NEXT_BILLING, current_period_ends_at=NEXT_BILLING, created_at=CREATED,
        product=Product(handle=handle, name='Pro Plan', price_in_cents=29900))).to_dict()


def sent_json(request: HttpRequest) -> dict[str, Any]:
    """The serialized JSON body the SDK handed the transport (wire names)."""
    body = request.body
    assert isinstance(body, JsonBody)
    assert isinstance(body.value, dict)
    return body.value


def not_found() -> HttpResponse:
    return HttpResponse(status_code=404, headers={}, content=b'')


@override_settings(**MAXIO_SETTINGS)
class ApiTestCase(TestCase):

    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(
            username='shopper', email='shopper@example.com', password='a-long-password',
            first_name='Sam', last_name='Shopper')
        self.client.force_login(self.user)

    def tearDown(self) -> None:
        set_client(None)

    def stub(self, *responses: HttpResponse | Exception) -> StubTransport:
        transport = StubTransport(*responses)
        set_client(build_client(transport))
        return transport

    def subscribe(self, plan: str = 'eshop-pro', idempotency_key: str | None = None) -> Any:
        headers = {'Idempotency-Key': idempotency_key} if idempotency_key else {}
        return self.client.post('/api/subscriptions', data=json.dumps({'planHandle': plan}),
                                content_type='application/json', headers=headers)

    @property
    def sub_ref(self) -> str:
        return subscription_reference(self.user, 'eshop-pro', 'default')


class PlansTests(ApiTestCase):

    def test_lists_unarchived_plans_of_the_configured_family(self) -> None:
        transport = self.stub(json_response(200, plans_body()))
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 200)
        plans = response.json()['plans']
        self.assertEqual([p['planHandle'] for p in plans], ['eshop-pro', 'basic-plan'])
        self.assertEqual(plans[0]['price'], '299.00')
        self.assertEqual(plans[0]['intervalUnit'], 'month')
        request = transport.requests[0]
        self.assertEqual(request.method, 'GET')
        self.assertTrue(request.url.startswith(
            'https://test-site.chargify.com/product_families/handle%3Aeshop-subscribe/products.json'), request.url)
        self.assertTrue(request.headers['authorization'].startswith('Basic '))

    def test_base_url_override_is_used_verbatim(self) -> None:
        with self.settings(MAXIO_BASE_URL='http://billing.internal:8080'):
            transport = self.stub(json_response(200, []))
            self.client.get('/api/subscription-plans')
        self.assertTrue(transport.requests[0].url.startswith('http://billing.internal:8080/product_families/'))

    def test_provider_outage_on_read(self) -> None:
        self.stub(httpx.ConnectError('refused'))
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['error'], 'billing_unreachable')

    def test_unconfigured_is_503(self) -> None:
        set_client(None)
        with self.settings(MAXIO_API_KEY=''):
            response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 503)


class SubscribeTests(ApiTestCase):

    def test_requires_login(self) -> None:
        self.client.logout()
        self.assertEqual(self.subscribe().status_code, 401)

    def test_subscribe_creates_customer_then_subscription(self) -> None:
        transport = self.stub(
            json_response(200, plans_body()),
            json_response(201, customer_body()),
            json_response(201, subscription_body(reference=self.sub_ref)),
        )
        response = self.subscribe()
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body['subscriptionId'], 900)
        self.assertEqual(body['outcome'], 'done')
        self.assertEqual(body['state'], 'active')
        self.assertEqual(body['price'], '299.00')
        self.assertEqual(body['nextBillingAt'], NEXT_BILLING.isoformat())
        self.assertEqual(transport.calls(), [
            ('GET', '/product_families/handle%3Aeshop-subscribe/products.json'),
            ('POST', '/customers.json'),
            ('POST', '/subscriptions.json'),
        ])
        customer_sent = sent_json(transport.requests[1])
        self.assertEqual(customer_sent['customer']['reference'], customer_reference(self.user))
        self.assertEqual(customer_sent['customer']['first_name'], 'Sam')
        self.assertEqual(sent_json(transport.requests[2])['subscription'], {
            'product_handle': 'eshop-pro', 'customer_id': 500, 'reference': self.sub_ref,
            'payment_collection_method': 'remittance'})
        record = ProviderWrite.objects.get(reference=self.sub_ref)
        self.assertEqual((record.outcome, record.provider_id), ('done', '900'))

    def test_the_same_request_twice_makes_one_create(self) -> None:
        transport = self.stub(
            json_response(200, plans_body()),
            json_response(201, customer_body()),
            json_response(201, subscription_body(reference=self.sub_ref)),
            json_response(200, plans_body()),  # the repeat only re-reads plans
        )
        first = self.subscribe()
        second = self.subscribe()
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()['subscriptionId'], first.json()['subscriptionId'])
        posts = [c for c in transport.calls() if c[0] == 'POST']
        self.assertEqual(posts, [('POST', '/customers.json'), ('POST', '/subscriptions.json')])

    def test_a_new_idempotency_key_is_a_new_subscription(self) -> None:
        self.stub(
            json_response(200, plans_body()),
            json_response(201, customer_body()),
            json_response(201, subscription_body(sub_id=1)),
            json_response(200, plans_body()),
            json_response(201, subscription_body(sub_id=2)),
        )
        self.assertEqual(self.subscribe(idempotency_key='a').json()['subscriptionId'], 1)
        self.assertEqual(self.subscribe(idempotency_key='b').json()['subscriptionId'], 2)

    def test_in_flight_claim_answers_in_progress_without_a_provider_call(self) -> None:
        ProviderWrite.objects.create(
            reference=customer_reference(self.user), kind='customer', user=self.user, provider_id='500',
            outcome='done', claimed_at=CREATED)
        ProviderWrite.objects.create(
            reference=self.sub_ref, kind='subscription', user=self.user, plan_handle='eshop-pro',
            outcome='sending', claimed_at=datetime.now(dt_timezone.utc))
        transport = self.stub(json_response(200, plans_body()))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['outcome'], 'in_progress')
        self.assertIsNone(response.json()['subscriptionId'])
        self.assertEqual(len(transport.requests), 1)

    def test_unknown_plan_is_rejected_before_any_write(self) -> None:
        transport = self.stub(json_response(200, plans_body()))
        response = self.subscribe('nope')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(transport.requests), 1)

    def test_pending_state_is_accepted_not_done(self) -> None:
        self.stub(json_response(200, plans_body()), json_response(201, customer_body()),
                  json_response(201, subscription_body(state='pending')))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['outcome'], 'pending')

    def test_an_id_with_a_failed_state_is_not_done(self) -> None:
        self.stub(json_response(200, plans_body()), json_response(201, customer_body()),
                  json_response(201, subscription_body(state='failed_to_create')))
        response = self.subscribe()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['outcome'], 'failed')

    def test_an_unlisted_state_is_unknown_not_done(self) -> None:
        self.stub(json_response(200, plans_body()), json_response(201, customer_body()),
                  json_response(201, subscription_body(state='something_new')))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['outcome'], 'unknown')

    def test_provider_applied_a_different_plan_needs_review(self) -> None:
        self.stub(json_response(200, plans_body()), json_response(201, customer_body()),
                  json_response(201, subscription_body(handle='basic-plan')))
        response = self.subscribe()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(ProviderWrite.objects.get(reference=self.sub_ref).outcome, 'needs_review')

    def test_validation_rejection_releases_the_claim(self) -> None:
        self.stub(json_response(200, plans_body()), json_response(201, customer_body()),
                  json_response(422, {'errors': ['Product must be active']}))
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['details'], ['Product must be active'])
        self.assertFalse(ProviderWrite.objects.filter(reference=self.sub_ref).exists())

    def test_unsent_is_not_the_same_failure_as_unknown(self) -> None:
        self.stub(json_response(200, plans_body()), json_response(201, customer_body()),
                  httpx.ConnectError('refused'))
        unsent = self.subscribe()
        self.assertEqual((unsent.status_code, unsent.json()['outcomeUnknown']), (502, False))
        self.assertFalse(ProviderWrite.objects.filter(reference=self.sub_ref).exists())

        transport = self.stub(json_response(200, plans_body()), httpx.ReadTimeout('no reply'), not_found())
        unknown = self.subscribe()
        self.assertEqual((unknown.status_code, unknown.json()['outcomeUnknown']), (504, True))
        self.assertEqual(ProviderWrite.objects.get(reference=self.sub_ref).outcome, 'unknown')
        lookup = transport.requests[-1]
        self.assertEqual(lookup.method, 'GET')
        self.assertIn('/subscriptions/lookup.json', lookup.url)
        self.assertIn('reference=' + self.sub_ref, lookup.url)

    def test_an_unknown_outcome_is_settled_by_lookup_never_by_a_second_create(self) -> None:
        self.stub(json_response(200, plans_body()), json_response(201, customer_body()),
                  httpx.ReadTimeout('no reply'), not_found())
        self.assertEqual(self.subscribe().status_code, 504)

        transport = self.stub(json_response(200, plans_body()),
                              json_response(200, subscription_body(reference=self.sub_ref)))
        retry = self.subscribe()
        self.assertEqual(retry.status_code, 201)
        self.assertEqual(retry.json()['subscriptionId'], 900)
        self.assertEqual([c[0] for c in transport.calls()], ['GET', 'GET'])  # plans, lookup: no POST

    def test_a_5xx_on_create_is_checked_by_lookup(self) -> None:
        self.stub(json_response(200, plans_body()), json_response(201, customer_body()),
                  json_response(502, {}), json_response(200, subscription_body(reference=self.sub_ref)))
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)

    def test_customer_reference_taken_is_a_landing(self) -> None:
        transport = self.stub(
            json_response(200, plans_body()),
            json_response(422, {'errors': ['Reference: must be unique - that value has been taken.']}),
            json_response(200, customer_body(customer_id=777)),
            json_response(201, subscription_body()),
        )
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        self.assertIn('/customers/lookup.json', transport.requests[2].url)
        self.assertEqual(sent_json(transport.requests[3])['subscription']['customer_id'], 777)


class MySubscriptionsTests(ApiTestCase):

    def test_lists_provider_subscriptions_and_unsettled_claims(self) -> None:
        ProviderWrite.objects.create(
            reference=customer_reference(self.user), kind='customer', user=self.user, provider_id='500',
            outcome='done', claimed_at=CREATED)
        ProviderWrite.objects.create(
            reference=self.sub_ref, kind='subscription', user=self.user, plan_handle='eshop-pro',
            outcome='pending', claimed_at=CREATED)
        ProviderWrite.objects.create(
            reference='test-u1-sub-basic-plan-x', kind='subscription', user=self.user, plan_handle='basic-plan',
            outcome='unknown', claimed_at=CREATED)
        transport = self.stub(
            json_response(200, [subscription_body(reference=self.sub_ref)]),
            not_found(),  # the unknown claim is still not found
        )
        response = self.client.get('/api/my-subscriptions')
        self.assertEqual(response.status_code, 200)
        entries = response.json()['subscriptions']
        self.assertEqual([(e['subscriptionId'], e['outcome']) for e in entries], [(900, 'done'), (None, 'unknown')])
        self.assertEqual(ProviderWrite.objects.get(reference=self.sub_ref).outcome, 'done')
        self.assertIn('/customers/500/subscriptions.json', transport.requests[0].url)

    def test_no_customer_yet_is_an_empty_list(self) -> None:
        transport = self.stub()
        response = self.client.get('/api/my-subscriptions')
        self.assertEqual(response.json(), {'subscriptions': []})
        self.assertEqual(transport.requests, [])


class SessionTests(TestCase):

    def test_login_with_email_and_password(self) -> None:
        get_user_model().objects.create_user(username='u', email='u@example.com', password='a-long-password')
        response = self.client.post('/api/session', data=json.dumps(
            {'email': 'u@example.com', 'password': 'a-long-password'}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['authenticated'])
        self.assertTrue(response.json()['csrfToken'])

    def test_bad_password(self) -> None:
        response = self.client.post('/api/session', data=json.dumps(
            {'email': 'x@example.com', 'password': 'nope'}), content_type='application/json')
        self.assertEqual(response.status_code, 401)


class OutcomeMapTests(TestCase):

    def test_every_state_is_mapped(self) -> None:
        expected = {
            'active': 'done', 'trialing': 'done',
            'pending': 'pending', 'assessing': 'pending', 'awaiting_signup': 'pending',
            'past_due': 'pending', 'soft_failure': 'pending', 'unpaid': 'pending', 'paused': 'pending',
            'on_hold': 'pending', 'suspended': 'pending',
            'failed_to_create': 'failed', 'canceled': 'failed', 'expired': 'failed', 'trial_ended': 'failed',
        }
        self.assertEqual({s.value for s in SubscriptionState}, set(expected))
        for state in SubscriptionState:
            self.assertEqual(subscription_outcome(state), expected[state.value], state)
        self.assertEqual(subscription_outcome('brand_new_state'), 'unknown')
        self.assertEqual(subscription_outcome(None), 'unknown')
