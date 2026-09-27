"""
Tests for the subscription billing API. The Maxio client is real; only its HTTP
transport is replaced, so every request the SDK builds is asserted as sent.

Run: cd sandbox && python manage.py test apps.maxio_billing
"""
import json
from datetime import timedelta
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from maxio_advanced_billing.core import HttpRequest, HttpResponse, JsonBody

from .client import build_client
from .models import BillingWrite
from . import services
from .services import customer_reference, subscription_reference

MAXIO = dict(
    MAXIO_API_KEY='test-key', MAXIO_SITE_SUBDOMAIN='test-site', MAXIO_ENVIRONMENT='US',
    MAXIO_DEFAULT_PRODUCT_FAMILY='test-family', MAXIO_BASE_URL='', MAXIO_TIMEOUT=5.0,
    MAXIO_REFERENCE_PREFIX='test-install',
)

Reply = HttpResponse | Exception


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


class RouterTransport:
    """Answers per (method, path) from queued replies, recording every request."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[Reply]] = {}
        self.requests: list[HttpRequest] = []

    def on(self, method: str, path: str, *replies: Reply) -> 'RouterTransport':
        self.routes.setdefault((method, path), []).extend(replies)
        return self

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        key = (request.method, urlsplit(request.url).path)
        queue = self.routes.get(key)
        if not queue:
            raise AssertionError(f'unexpected Maxio call {key}')
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self) -> None:
        pass

    def calls(self, method: str, path: str) -> list[HttpRequest]:
        return [r for r in self.requests
                if r.method == method and urlsplit(r.url).path == path]


FAMILY_PATH = '/product_families/handle%3Atest-family/products.json'


def product(handle: str, price: int, pid: int, archived: bool = False) -> dict[str, Any]:
    return {'product': {
        'id': pid, 'handle': handle, 'name': handle.title(), 'description': 'desc',
        'price_in_cents': price, 'interval': 1, 'interval_unit': 'month',
        'require_credit_card': False,
        'archived_at': '2026-01-01T00:00:00Z' if archived else None,
    }}


PLANS = [product('eshop-pro', 29900, 11), product('basic-plan', 2900, 12),
         product('old-plan', 100, 13, archived=True)]


def customer(ref: str, cid: int = 501) -> dict[str, Any]:
    return {'customer': {'id': cid, 'reference': ref, 'email': 'shopper@example.com',
                         'first_name': 'Shopper', 'last_name': 'One',
                         'created_at': '2026-09-27T10:00:00Z'}}


def subscription(ref: str | None, sid: int = 9001, state: str = 'active', price: int = 29900,
                 handle: str = 'eshop-pro', cid: int = 501) -> dict[str, Any]:
    return {'subscription': {
        'id': sid, 'state': state, 'reference': ref, 'product_price_in_cents': price,
        'currency': 'USD', 'created_at': '2026-09-27T10:00:01Z',
        'activated_at': '2026-09-27T10:00:01Z',
        'current_period_ends_at': '2026-10-27T10:00:01Z',
        'next_assessment_at': '2026-10-27T10:00:01Z',
        'product': {'id': 11, 'handle': handle, 'name': 'Pro Plan', 'interval': 1,
                    'interval_unit': 'month'},
        'customer': {'id': cid, 'reference': 'x'},
    }}


@override_settings(**MAXIO)
class BillingApiTestCase(TestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(
            username='shopper', email='shopper@example.com', password='a-long-password-1',
            first_name='Shopper', last_name='One')
        self.http = Client()
        self.http.force_login(self.user)
        self.transport = RouterTransport()
        services._site_billing.clear()  # read once per process: every test starts cold
        self.transport.on('GET', '/site.json', json_response(200, {'site': {
            'id': 1, 'currency': 'USD', 'relationship_invoicing_enabled': True}}))
        self.sdk = build_client(self.transport)
        patcher = mock.patch('apps.maxio_billing.views.get_client', return_value=self.sdk)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.sdk.close)

    @property
    def cust_ref(self) -> str:
        return customer_reference(self.user)

    @property
    def sub_ref(self) -> str:
        return subscription_reference(self.user, 'eshop-pro', None)

    def post(self, path: str, body: dict[str, Any], **headers: str) -> Any:
        return self.http.post(path, data=json.dumps(body), content_type='application/json',
                              headers=headers)

    def plans_listed(self, times: int = 1) -> None:
        for _ in range(times):
            self.transport.on('GET', FAMILY_PATH, json_response(200, PLANS))

    def subscribe(self, **headers: str) -> Any:
        return self.post('/api/subscriptions', {'planHandle': 'eshop-pro'}, **headers)

    def body_of(self, request: HttpRequest) -> Any:
        assert isinstance(request.body, JsonBody)
        return request.body.value


class PlanTests(BillingApiTestCase):
    def test_lists_active_plans_with_handles(self) -> None:
        self.plans_listed()
        response = self.http.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 200)
        plans = response.json()['plans']
        self.assertEqual([p['planHandle'] for p in plans], ['eshop-pro', 'basic-plan'])
        self.assertEqual(plans[0]['price'], '299.00')
        self.assertEqual(plans[0]['intervalUnit'], 'month')
        self.assertEqual(plans[0]['currency'], 'USD')
        sent = self.transport.calls('GET', FAMILY_PATH)[0]
        self.assertTrue(sent.url.startswith('https://test-site.chargify.com/'))
        self.assertEqual(parse_qs(urlsplit(sent.url).query)['per_page'], ['200'])
        self.assertTrue(sent.headers['authorization'].startswith('Basic '))

    def test_requires_login(self) -> None:
        response = Client().get('/api/subscription-plans')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.transport.requests, [])

    def test_our_credentials_rejected_is_not_the_callers_401(self) -> None:
        self.transport.routes[('GET', '/site.json')] = [json_response(401, {})]
        response = self.http.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['error']['code'], 'provider_auth')

    def test_unreachable_provider_on_a_read(self) -> None:
        self.transport.routes[('GET', '/site.json')] = [httpx.ConnectError('refused')]
        self.assertEqual(self.http.get('/api/subscription-plans').status_code, 502)

    @override_settings(MAXIO_BASE_URL='https://mock.example.test')
    def test_base_url_override_is_used_verbatim(self) -> None:
        transport = RouterTransport().on('GET', FAMILY_PATH, json_response(200, PLANS))
        transport.on('GET', '/site.json', json_response(200, {'site': {
            'currency': 'USD', 'relationship_invoicing_enabled': True}}))
        services._site_billing.clear()
        with build_client(transport) as sdk, \
                mock.patch('apps.maxio_billing.views.get_client', return_value=sdk):
            self.assertEqual(self.http.get('/api/subscription-plans').status_code, 200)
        self.assertTrue(transport.requests[0].url.startswith('https://mock.example.test/'))


@override_settings(MAXIO_API_KEY='')
class NotConfiguredTests(TestCase):
    def test_missing_key_answers_503(self) -> None:
        user = get_user_model().objects.create_user(username='u', email='u@example.com')
        http = Client()
        http.force_login(user)
        response = http.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 503)


class SubscribeTests(BillingApiTestCase):
    def happy_path(self) -> None:
        self.plans_listed()
        self.transport.on('POST', '/customers.json', json_response(201, customer(self.cust_ref)))
        self.transport.on('POST', '/subscriptions.json',
                          json_response(201, subscription(self.sub_ref)))

    def test_subscribe_creates_customer_then_subscription(self) -> None:
        self.happy_path()
        response = self.subscribe()
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body['subscriptionId'], 9001)
        self.assertEqual(body['outcome'], 'done')
        self.assertEqual(body['subscription']['state'], 'active')
        self.assertEqual(body['subscription']['nextBillingAt'], '2026-10-27T10:00:01+00:00')
        self.assertEqual(body['subscription']['price'], '299.00')

        cust = self.body_of(self.transport.calls('POST', '/customers.json')[0])['customer']
        self.assertEqual(cust['reference'], self.cust_ref)
        self.assertEqual(cust['email'], 'shopper@example.com')
        sub = self.body_of(self.transport.calls('POST', '/subscriptions.json')[0])['subscription']
        self.assertEqual(sub, {'product_handle': 'eshop-pro', 'customer_id': 501,
                               'payment_collection_method': 'remittance',
                               'reference': self.sub_ref})
        self.assertEqual(BillingWrite.objects.get(reference=self.sub_ref).outcome, 'done')

    def test_double_submit_makes_one_customer_and_one_subscription(self) -> None:
        self.happy_path()
        first = self.subscribe()
        self.plans_listed()  # the second request re-reads plans, and writes nothing
        second = self.subscribe()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['subscriptionId'], first.json()['subscriptionId'])
        self.assertEqual(len(self.transport.calls('POST', '/customers.json')), 1)
        self.assertEqual(len(self.transport.calls('POST', '/subscriptions.json')), 1)

    def test_a_claim_in_flight_answers_in_progress_without_calling_maxio(self) -> None:
        self.happy_path()
        self.post('/api/billing-customer', {})
        BillingWrite.objects.create(reference=self.sub_ref, user=self.user, kind='subscription',
                                    outcome='sending', claimed_at=timezone.now())
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['outcome'], 'sending')
        self.assertEqual(self.transport.calls('POST', '/subscriptions.json'), [])

    def test_read_timeout_is_unknown_then_settled_by_lookup_never_recreated(self) -> None:
        self.plans_listed(2)
        self.transport.on('POST', '/customers.json', json_response(201, customer(self.cust_ref)))
        self.transport.on('POST', '/subscriptions.json', httpx.ReadTimeout('no reply'))
        self.transport.on('GET', '/subscriptions/lookup.json',
                          json_response(404, {}),                        # not visible yet
                          json_response(200, subscription(self.sub_ref)))  # landed after all
        first = self.subscribe()
        self.assertEqual(first.status_code, 504)
        self.assertTrue(first.json()['error']['outcomeUnknown'])
        self.assertEqual(BillingWrite.objects.get(reference=self.sub_ref).outcome, 'unknown')

        second = self.subscribe()
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(second.json()['subscriptionId'], 9001)
        self.assertEqual(len(self.transport.calls('POST', '/subscriptions.json')), 1)
        lookups = self.transport.calls('GET', '/subscriptions/lookup.json')
        self.assertEqual([parse_qs(urlsplit(r.url).query)['reference'] for r in lookups],
                         [[self.sub_ref], [self.sub_ref]])

    def test_refused_connection_is_known_and_releases_the_claim(self) -> None:
        self.plans_listed(2)
        self.transport.on('POST', '/customers.json', json_response(201, customer(self.cust_ref)))
        self.transport.on('POST', '/subscriptions.json', httpx.ConnectError('refused'),
                          json_response(201, subscription(self.sub_ref)))
        unsent = self.subscribe()
        self.assertEqual(unsent.status_code, 502)
        self.assertNotIn('outcomeUnknown', unsent.json()['error'])
        self.assertEqual(self.transport.calls('GET', '/subscriptions/lookup.json'), [])
        retried = self.subscribe()
        self.assertEqual(retried.status_code, 201)
        refs = [self.body_of(r)['subscription']['reference']
                for r in self.transport.calls('POST', '/subscriptions.json')]
        self.assertEqual(refs, [self.sub_ref, self.sub_ref])  # same reference, never a new one

    def test_server_error_on_create_is_checked_by_reference(self) -> None:
        self.happy_path()
        self.transport.routes[('POST', '/subscriptions.json')] = [json_response(500, {})]
        self.transport.on('GET', '/subscriptions/lookup.json',
                          json_response(200, subscription(self.sub_ref)))
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['subscriptionId'], 9001)

    def test_validation_rejection_passes_provider_messages(self) -> None:
        self.happy_path()
        self.transport.routes[('POST', '/subscriptions.json')] = [
            json_response(422, {'errors': ['Product must be active']})]
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['details'], ['Product must be active'])
        record = BillingWrite.objects.get(reference=self.sub_ref)
        self.assertEqual((record.outcome, record.provider_id), ('failed', ''))

    def test_an_id_with_a_problem_state_is_not_done(self) -> None:
        self.happy_path()
        self.transport.routes[('POST', '/subscriptions.json')] = [
            json_response(201, subscription(self.sub_ref, state='past_due'))]
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['outcome'], 'pending')

    def test_an_ended_state_is_failed(self) -> None:
        self.happy_path()
        self.transport.routes[('POST', '/subscriptions.json')] = [
            json_response(201, subscription(self.sub_ref, state='canceled'))]
        self.assertEqual(self.subscribe().status_code, 409)

    def test_an_unlisted_state_is_unknown_not_done(self) -> None:
        self.happy_path()
        self.transport.routes[('POST', '/subscriptions.json')] = [
            json_response(201, subscription(self.sub_ref, state='something_new'))]
        response = self.subscribe()
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()['outcome'], 'unknown')

    def test_a_different_price_than_shown_needs_review(self) -> None:
        self.happy_path()
        self.transport.routes[('POST', '/subscriptions.json')] = [
            json_response(201, subscription(self.sub_ref, price=39900))]
        response = self.subscribe()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['outcome'], 'needs_review')

    def test_a_2xx_without_a_subscription_is_unknown(self) -> None:
        self.happy_path()
        self.transport.routes[('POST', '/subscriptions.json')] = [json_response(201, {})]
        self.transport.on('GET', '/subscriptions/lookup.json', json_response(404, {}))
        response = self.subscribe()
        self.assertEqual(response.status_code, 504)
        self.assertEqual(BillingWrite.objects.get(reference=self.sub_ref).outcome, 'unknown')

    def test_idempotency_key_scopes_the_reference(self) -> None:
        self.plans_listed(2)
        self.transport.on('POST', '/customers.json', json_response(201, customer(self.cust_ref)))
        ref_a = subscription_reference(self.user, 'eshop-pro', 'key-a')
        ref_b = subscription_reference(self.user, 'eshop-pro', 'key-b')
        self.transport.on('POST', '/subscriptions.json',
                          json_response(201, subscription(ref_a, sid=1)),
                          json_response(201, subscription(ref_b, sid=2)))
        a = self.subscribe(**{'Idempotency-Key': 'key-a'})
        b = self.subscribe(**{'Idempotency-Key': 'key-b'})
        self.assertEqual((a.json()['subscriptionId'], b.json()['subscriptionId']), (1, 2))
        self.assertNotEqual(ref_a, ref_b)

    def test_unknown_plan_is_404_and_writes_nothing(self) -> None:
        self.plans_listed()
        response = self.post('/api/subscriptions', {'planHandle': 'old-plan'})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(BillingWrite.objects.count(), 0)

    def test_missing_plan_handle_is_400(self) -> None:
        self.assertEqual(self.post('/api/subscriptions', {}).status_code, 400)

    def test_csrf_is_enforced_for_session_callers(self) -> None:
        http = Client(enforce_csrf_checks=True)
        http.force_login(self.user)
        response = http.post('/api/subscriptions', data='{"planHandle": "eshop-pro"}',
                             content_type='application/json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.transport.requests, [])


class CustomerTests(BillingApiTestCase):
    def test_customer_step_is_separately_invocable_and_idempotent(self) -> None:
        self.transport.on('POST', '/customers.json', json_response(201, customer(self.cust_ref)))
        first = self.post('/api/billing-customer', {})
        second = self.post('/api/billing-customer', {})
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(second.json()['customerId'], 501)
        self.assertEqual(len(self.transport.calls('POST', '/customers.json')), 1)

    def test_customer_timeout_is_checked_by_reference(self) -> None:
        self.transport.on('POST', '/customers.json', httpx.ReadTimeout('no reply'))
        self.transport.on('GET', '/customers/lookup.json',
                          json_response(200, customer(self.cust_ref)))
        response = self.post('/api/billing-customer', {})
        self.assertEqual(response.status_code, 201)
        lookup = self.transport.calls('GET', '/customers/lookup.json')[0]
        self.assertEqual(parse_qs(urlsplit(lookup.url).query)['reference'], [self.cust_ref])

    def test_stale_sending_claim_is_looked_up_not_recreated(self) -> None:
        BillingWrite.objects.create(reference=self.cust_ref, user=self.user, kind='customer',
                                    outcome='sending',
                                    claimed_at=timezone.now() - timedelta(hours=1))
        self.transport.on('GET', '/customers/lookup.json', json_response(404, {}))
        response = self.post('/api/billing-customer', {})
        self.assertEqual(response.status_code, 504)
        self.assertEqual(self.transport.calls('POST', '/customers.json'), [])

    def test_user_without_email_is_rejected_before_any_call(self) -> None:
        self.user.email = ''
        self.user.save()
        self.assertEqual(self.post('/api/billing-customer', {}).status_code, 400)
        self.assertEqual(self.transport.requests, [])


class ReadBackTests(BillingApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        BillingWrite.objects.create(reference=self.cust_ref, user=self.user, kind='customer',
                                    outcome='done', provider_id='501',
                                    claimed_at=timezone.now())

    def test_my_subscriptions_reads_back_from_maxio(self) -> None:
        self.transport.on('GET', '/customers/501/subscriptions.json',
                          json_response(200, [subscription(self.sub_ref)]))
        response = self.http.get('/api/my-subscriptions')
        self.assertEqual(response.status_code, 200)
        subs = response.json()['subscriptions']
        self.assertEqual(subs[0]['subscriptionId'], 9001)
        self.assertEqual(subs[0]['plan']['planHandle'], 'eshop-pro')

    def test_my_subscriptions_without_a_customer_is_empty(self) -> None:
        other = get_user_model().objects.create_user(username='o', email='o@example.com')
        self.http.force_login(other)
        self.transport.on('GET', '/customers/lookup.json', json_response(404, {}))
        response = self.http.get('/api/my-subscriptions')
        self.assertEqual(response.json()['subscriptions'], [])
        self.assertIsNone(response.json()['customerId'])

    def test_subscription_of_another_customer_is_not_found(self) -> None:
        self.transport.on('GET', '/subscriptions/777.json',
                          json_response(200, subscription('x', sid=777, cid=999)))
        self.assertEqual(self.http.get('/api/subscriptions/777').status_code, 404)

    def test_own_subscription_detail(self) -> None:
        self.transport.on('GET', '/subscriptions/9001.json',
                          json_response(200, subscription(self.sub_ref)))
        response = self.http.get('/api/subscriptions/9001')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['state'], 'active')


class SessionTests(TestCase):
    def test_session_login_with_csrf(self) -> None:
        get_user_model().objects.create_user(
            username='s', email='s@example.com', password='a-long-password-1')
        http = Client(enforce_csrf_checks=True)
        token = http.get('/api/session').json()['csrfToken']
        response = http.post('/api/session', data=json.dumps(
            {'email': 's@example.com', 'password': 'a-long-password-1'}),
            content_type='application/json', headers={'X-CSRFToken': token})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['authenticated'])

    def test_bad_password(self) -> None:
        response = Client().post('/api/session', data='{"email": "x@example.com", '
                                 '"password": "nope"}', content_type='application/json')
        self.assertEqual(response.status_code, 401)


class SiteBillingTests(BillingApiTestCase):
    def test_legacy_statements_site_bills_by_invoice(self) -> None:
        self.transport.routes[('GET', '/site.json')] = [json_response(200, {'site': {
            'currency': 'USD', 'relationship_invoicing_enabled': False}})]
        self.assertEqual(services.site_billing(self.sdk).collection_method, 'invoice')

    def test_site_is_read_once(self) -> None:
        services.site_billing(self.sdk)
        services.site_billing(self.sdk)
        self.assertEqual(len(self.transport.calls('GET', '/site.json')), 1)
