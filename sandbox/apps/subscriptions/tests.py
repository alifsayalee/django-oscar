"""Unit tests for the Maxio subscription service.

These exercise the real request-building pipeline of the SDK by injecting a
stub transport (the SDK's documented test seam), so no network calls happen.
We assert on our own boundary's behaviour: what we return, and how we translate
each SDK failure kind.
"""

from __future__ import annotations

import json

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase

from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import BasicAuthCredentials, HttpResponse

from .models import MaxioCustomer
from .services import (
    MaxioService,
    PlanNotFound,
    ProviderRejected,
    ProviderUnavailable,
    ProviderUnreadable,
)

User = get_user_model()


class StubTransport:
    """Satisfies the SDK's sync transport protocol: send() + close()."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return self._responses.pop(0)

    def close(self):
        pass

    @property
    def last_request(self):
        return self.requests[-1] if self.requests else None


class BoomTransport:
    """A transport that fails to connect."""

    def __init__(self):
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        raise httpx.ConnectError('refused')

    def close(self):
        pass


def json_response(status, body):
    return HttpResponse(
        status_code=status,
        headers={'content-type': 'application/json'},
        content=json.dumps(body).encode(),
    )


def _product(handle, name, price, family='eshop-subscribe'):
    return {
        'product': {
            'id': 100,
            'handle': handle,
            'name': name,
            'price_in_cents': price,
            'interval': 1,
            'interval_unit': 'month',
            'require_credit_card': False,
            'product_family': {'handle': family},
        }
    }


def _subscription(sub_id, state, handle, price=29900, customer_id=55):
    return {
        'subscription': {
            'id': sub_id,
            'state': state,
            'product_price_in_cents': price,
            'current_period_ends_at': '2026-10-22T12:00:00Z',
            'customer': {'id': customer_id},
            'product': {'handle': handle, 'name': 'Pro Plan'},
        }
    }


def make_service(*responses, family='eshop-subscribe'):
    transport = StubTransport(*responses)
    client = MaxioAdvancedBillingClient(
        custom_http_client=transport,
        basic_auth=BasicAuthCredentials(username='key', password='x'),
        environment='us',
        server_config={'production': {'us': {'site': 'test'}}},
    )
    return MaxioService(client=client, family_handle=family), transport


class ListPlansTests(TestCase):
    def test_filters_to_configured_family(self):
        service, transport = make_service(
            json_response(
                200,
                [
                    _product('eshop-pro', 'Pro Plan', 29900),
                    _product('basic-plan', 'Basic Plan', 2900),
                    _product('other', 'Other', 500, family='someone-else'),
                ],
            )
        )
        plans = service.list_plans()
        self.assertEqual({p['planHandle'] for p in plans}, {'eshop-pro', 'basic-plan'})
        pro = next(p for p in plans if p['planHandle'] == 'eshop-pro')
        self.assertEqual(pro['priceInCents'], 29900)
        self.assertFalse(pro['requiresPaymentMethod'])
        # One request, to the products endpoint.
        self.assertEqual(len(transport.requests), 1)
        self.assertIn('/products.json', transport.last_request.url)


class SubscribeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='shopper', email='shopper@example.com', password='pw-123456789'
        )

    def test_creates_customer_and_subscription(self):
        service, transport = make_service(
            json_response(200, [_product('eshop-pro', 'Pro Plan', 29900)]),  # list_plans
            json_response(404, {'errors': ['not found']}),  # read_customer_by_reference
            json_response(201, {'customer': {'id': 55, 'reference': 'oscar-user-1'}}),  # create_customer
            json_response(200, []),  # list_customer_subscriptions (idempotency)
            json_response(201, _subscription(9001, 'active', 'eshop-pro')),  # create_subscription
        )
        result = service.subscribe(self.user, 'eshop-pro')
        self.assertEqual(result['subscriptionId'], 9001)
        self.assertEqual(result['state'], 'active')
        self.assertEqual(result['planHandle'], 'eshop-pro')
        self.assertEqual(result['nextBillingDate'], '2026-10-22T12:00:00+00:00')
        self.assertFalse(result['reused'])
        # Local link persisted for idempotency.
        link = MaxioCustomer.objects.get(user=self.user)
        self.assertEqual(link.maxio_customer_id, 55)
        # The create request carried our plan handle and customer id.
        create_req = transport.requests[-1]
        self.assertEqual(create_req.method, 'POST')
        self.assertTrue(create_req.url.endswith('/subscriptions.json'))
        body = create_req.body.value['subscription']
        self.assertEqual(body['product_handle'], 'eshop-pro')
        self.assertEqual(body['customer_id'], 55)
        self.assertEqual(body['payment_collection_method'], 'remittance')

    def test_idempotent_when_live_subscription_exists(self):
        MaxioCustomer.objects.create(
            user=self.user, reference='oscar-user-1', maxio_customer_id=55
        )
        service, transport = make_service(
            json_response(200, [_product('eshop-pro', 'Pro Plan', 29900)]),  # list_plans
            json_response(200, [_subscription(7001, 'active', 'eshop-pro')]),  # existing
        )
        result = service.subscribe(self.user, 'eshop-pro')
        self.assertEqual(result['subscriptionId'], 7001)
        self.assertTrue(result['reused'])
        # No create call was made.
        self.assertEqual(len(transport.requests), 2)

    def test_unknown_plan_is_rejected(self):
        service, _ = make_service(
            json_response(200, [_product('eshop-pro', 'Pro Plan', 29900)])
        )
        with self.assertRaises(PlanNotFound):
            service.subscribe(self.user, 'does-not-exist')

    def test_provider_422_is_rejected(self):
        MaxioCustomer.objects.create(
            user=self.user, reference='oscar-user-1', maxio_customer_id=55
        )
        service, _ = make_service(
            json_response(200, [_product('eshop-pro', 'Pro Plan', 29900)]),
            json_response(200, []),  # no existing subs
            json_response(422, {'errors': ['Coupon code could not be applied']}),  # create
        )
        with self.assertRaises(ProviderRejected) as ctx:
            service.subscribe(self.user, 'eshop-pro')
        self.assertEqual(ctx.exception.http_status, 400)
        self.assertIn('Coupon code', '; '.join(ctx.exception.detail))

    def test_decode_failure_on_create_is_unreadable(self):
        MaxioCustomer.objects.create(
            user=self.user, reference='oscar-user-1', maxio_customer_id=55
        )
        service, _ = make_service(
            json_response(200, [_product('eshop-pro', 'Pro Plan', 29900)]),
            json_response(200, []),
            json_response(201, {'subscription': {'id': 'not-an-int'}}),  # type mismatch
        )
        with self.assertRaises(ProviderUnreadable):
            service.subscribe(self.user, 'eshop-pro')

    def test_transport_failure_is_unavailable(self):
        transport = BoomTransport()
        client = MaxioAdvancedBillingClient(
            custom_http_client=transport,
            basic_auth=BasicAuthCredentials(username='key', password='x'),
            environment='us',
            server_config={'production': {'us': {'site': 'test'}}},
        )
        service = MaxioService(client=client, family_handle='eshop-subscribe')
        with self.assertRaises(ProviderUnavailable):
            service.subscribe(self.user, 'eshop-pro')


class ListMySubscriptionsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='shopper2', email='s2@example.com', password='pw-123456789'
        )

    def test_empty_without_link(self):
        service, transport = make_service()  # no responses needed
        self.assertEqual(service.list_my_subscriptions(self.user), [])
        self.assertEqual(len(transport.requests), 0)

    def test_lists_subscriptions_for_linked_customer(self):
        MaxioCustomer.objects.create(
            user=self.user, reference='oscar-user-2', maxio_customer_id=55
        )
        service, _ = make_service(
            json_response(
                200,
                [
                    _subscription(9001, 'active', 'eshop-pro'),
                    _subscription(9002, 'canceled', 'basic-plan'),
                ],
            )
        )
        subs = service.list_my_subscriptions(self.user)
        self.assertEqual([s['subscriptionId'] for s in subs], [9001, 9002])
        self.assertEqual(subs[0]['state'], 'active')
