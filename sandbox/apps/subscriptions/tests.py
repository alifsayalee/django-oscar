"""Tests for the Maxio subscription capability.

These exercise the real request-building pipeline of the Maxio SDK by faking its *transport*
(the SDK's documented test seam) rather than mocking the client or the service. A stub transport
returns canned HTTP responses in order, so the views -> services -> SDK stack runs end to end with
no network. Assertions target our own boundary: response shapes, idempotency, and the mapping of
each provider failure kind onto an HTTP status.

Run with the sandbox settings, e.g. ``manage.py test apps.subscriptions``.
"""

import json
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase

from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import BasicAuthCredentials, HttpResponse
from maxio_advanced_billing.models import (
    Customer,
    CustomerResponse,
    Product,
    ProductFamily,
    ProductFamilyResponse,
    ProductResponse,
    Subscription,
    SubscriptionResponse,
)

from apps.subscriptions import client as maxio_client


# ---------------------------------------------------------------------------
# Transport seam
# ---------------------------------------------------------------------------

class StubTransport:
    """Satisfies the SDK's sync transport protocol: send() + close()."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        if not self._responses:
            raise AssertionError(f'unexpected extra request: {request.method} {request.url}')
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass

    @property
    def last_request(self):
        return self.requests[-1] if self.requests else None


def json_response(status, body):
    return HttpResponse(
        status_code=status,
        headers={'content-type': 'application/json'},
        content=json.dumps(body).encode(),
    )


def _client_with(*responses):
    transport = StubTransport(*responses)
    client = MaxioAdvancedBillingClient(
        custom_http_client=transport,
        basic_auth=BasicAuthCredentials(username='k', password='x'),
        environment='us',
        server_config={'production': {'us': {'site': 'test-site'}}},
    )
    return client, transport


# Canned wire bodies built from models (a member rename fails the fixture, not a stale string).
FAMILIES = [ProductFamilyResponse(
    product_family=ProductFamily(id=999, handle='eshop-subscribe', name='eShop')).to_dict()]

PRODUCTS = [
    ProductResponse(product=Product(
        id=1, handle='eshop-pro', name='Pro Plan', price_in_cents=29900,
        interval=1, interval_unit='month')).to_dict(),
    ProductResponse(product=Product(
        id=2, handle='basic-plan', name='Basic Plan', price_in_cents=2900,
        interval=1, interval_unit='month')).to_dict(),
]

CUSTOMER = CustomerResponse(
    customer=Customer(id=555, reference='oscar-user-1', first_name='S', last_name='U',
                      email='s@example.com')).to_dict()

SUB_PRO = SubscriptionResponse(subscription=Subscription(
    id=777, state='active', product=Product(handle='eshop-pro', name='Pro Plan',
                                             price_in_cents=29900))).to_dict()


class ServiceTestBase(TestCase):
    def _install(self, client):
        """Point the app's client singleton at a stubbed client for the duration of a test."""
        patcher = mock.patch.object(maxio_client, 'get_client', return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)


class ListPlansTests(ServiceTestBase):
    def test_returns_plans_with_handles(self):
        client, _ = _client_with(json_response(200, FAMILIES), json_response(200, PRODUCTS))
        self._install(client)
        from apps.subscriptions import services

        plans = services.list_plans()
        handles = {p['planHandle'] for p in plans}
        self.assertEqual(handles, {'eshop-pro', 'basic-plan'})
        pro = next(p for p in plans if p['planHandle'] == 'eshop-pro')
        self.assertEqual(pro['priceInCents'], 29900)
        self.assertEqual(pro['priceFormatted'], '299.00')
        self.assertEqual(pro['intervalUnit'], 'month')


class SubscribeTests(ServiceTestBase):
    def test_creates_new_subscription(self):
        client, transport = _client_with(
            json_response(200, FAMILIES),          # get_plan -> list_plans -> resolve family
            json_response(200, PRODUCTS),          # list products
            json_response(404, {'error': 'nf'}),   # read_customer_by_reference -> not found
            json_response(201, CUSTOMER),          # create_customer
            json_response(200, []),                # list_customer_subscriptions -> none
            json_response(201, SUB_PRO),           # create_subscription
        )
        self._install(client)
        from apps.subscriptions import services

        User = get_user_model()
        user = User(pk=1, username='s', email='s@example.com')
        summary, created = services.subscribe(user, 'eshop-pro')

        self.assertTrue(created)
        self.assertEqual(summary['subscriptionId'], 777)
        self.assertEqual(summary['planHandle'], 'eshop-pro')
        self.assertEqual(summary['state'], 'active')
        # The create carried the expected body to the wire (wire aliases, dumped payload).
        create = transport.requests[-1]
        self.assertEqual(create.method, 'POST')
        self.assertTrue(create.url.endswith('/subscriptions.json'))
        body = create.body.value['subscription']
        self.assertEqual(body['product_handle'], 'eshop-pro')
        self.assertEqual(body['customer_id'], 555)
        self.assertEqual(body['payment_collection_method'], 'remittance')

    def test_idempotent_when_live_subscription_exists(self):
        client, transport = _client_with(
            json_response(200, FAMILIES),
            json_response(200, PRODUCTS),
            json_response(200, CUSTOMER),   # customer already exists
            json_response(200, [SUB_PRO]),  # already subscribed to eshop-pro (active)
        )
        self._install(client)
        from apps.subscriptions import services

        User = get_user_model()
        user = User(pk=1, username='s', email='s@example.com')
        summary, created = services.subscribe(user, 'eshop-pro')

        self.assertFalse(created)
        self.assertEqual(summary['subscriptionId'], 777)
        # No create_subscription was sent (last call was the subscriptions list).
        self.assertTrue(transport.last_request.url.endswith('/subscriptions.json'))
        self.assertEqual(transport.last_request.method, 'GET')

    def test_unknown_plan_raises_plan_not_found(self):
        client, _ = _client_with(json_response(200, FAMILIES), json_response(200, PRODUCTS))
        self._install(client)
        from apps.subscriptions import services
        from apps.subscriptions.exceptions import PlanNotFound

        User = get_user_model()
        user = User(pk=1, username='s')
        with self.assertRaises(PlanNotFound):
            services.subscribe(user, 'no-such-plan')


class ErrorBoundaryTests(ServiceTestBase):
    def test_provider_rejection_maps_to_422(self):
        client, _ = _client_with(
            json_response(200, FAMILIES),
            json_response(200, PRODUCTS),
            json_response(404, {'error': 'nf'}),
            json_response(201, CUSTOMER),
            json_response(200, []),
            json_response(422, {'errors': ['bad']}),   # create_subscription rejected
        )
        self._install(client)
        from apps.subscriptions import services
        from apps.subscriptions.exceptions import ProviderRejected

        User = get_user_model()
        user = User(pk=1, username='s', email='s@example.com')
        with self.assertRaises(ProviderRejected) as ctx:
            services.subscribe(user, 'eshop-pro')
        self.assertEqual(ctx.exception.status_code, 422)

    def test_transport_failure_maps_to_502(self):
        client, _ = _client_with(httpx.ConnectError('refused'))
        self._install(client)
        from apps.subscriptions import services
        from apps.subscriptions.exceptions import ProviderUnavailable

        with self.assertRaises(ProviderUnavailable):
            services.list_plans()

    def test_truncated_customer_response_is_unknown_outcome(self):
        # create_customer returns 200 but with no customer id -> outcome unknown.
        client, _ = _client_with(
            json_response(200, FAMILIES),
            json_response(200, PRODUCTS),
            json_response(404, {'error': 'nf'}),
            json_response(201, {'customer': {}}),   # no id
        )
        self._install(client)
        from apps.subscriptions import services
        from apps.subscriptions.exceptions import ProviderUnavailable

        User = get_user_model()
        user = User(pk=1, username='s', email='s@example.com')
        with self.assertRaises(ProviderUnavailable):
            services.subscribe(user, 'eshop-pro')


class ViewAuthTests(TestCase):
    def test_plans_requires_login(self):
        resp = self.client.get('/api/subscription-plans')
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()['error'], 'authentication_required')

    def test_subscribe_requires_login(self):
        resp = self.client.post('/api/subscriptions', data='{}',
                                content_type='application/json')
        self.assertEqual(resp.status_code, 401)

    def test_my_subscriptions_requires_login(self):
        resp = self.client.get('/api/my-subscriptions')
        self.assertEqual(resp.status_code, 401)


class ViewFlowTests(ServiceTestBase):
    def test_subscribe_endpoint_returns_subscription_id(self):
        client, _ = _client_with(
            json_response(200, FAMILIES),
            json_response(200, PRODUCTS),
            json_response(404, {'error': 'nf'}),
            json_response(201, CUSTOMER),
            json_response(200, []),
            json_response(201, SUB_PRO),
        )
        self._install(client)

        User = get_user_model()
        user = User.objects.create_user(username='flowuser', password='pw-123456789',
                                        email='flow@example.com')
        self.client.force_login(user)
        # A freshly created user gets pk != 1; the reference is derived from pk so the stub's
        # canned bodies still apply (they don't assert the reference).
        resp = self.client.post('/api/subscriptions',
                                data=json.dumps({'planHandle': 'eshop-pro'}),
                                content_type='application/json')
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data['subscriptionId'], 777)
        self.assertTrue(data['created'])
