"""Unit tests for MaxioSubscriptionService against a faked SDK transport."""

import httpx
from django.test import SimpleTestCase

from apps.subscriptions.service import MaxioServiceError, MaxioSubscriptionService

from .support import (
    StubTransport,
    customer_body,
    json_response,
    make_client,
    product_body,
    raising_transport,
    subscription_body,
)


class _User:
    def __init__(self, pk=42, first_name='Ada', last_name='Lovelace',
                 email='ada@example.com', username='ada'):
        self.pk = pk
        self.first_name = first_name
        self.last_name = last_name
        self.email = email
        self.username = username


def _service(transport, family='eshop-subscribe'):
    return MaxioSubscriptionService(client=make_client(transport),
                                    product_family_handle=family)


class ListPlansTests(SimpleTestCase):
    def test_filters_to_configured_family_and_exposes_handle(self):
        transport = StubTransport(json_response(200, [
            product_body(1, 'eshop-pro', 'Pro Plan', 29900),
            product_body(2, 'basic-plan', 'Basic Plan', 2900),
            product_body(3, 'other-plan', 'Other', 100, family_handle='something-else'),
        ]))
        plans = _service(transport).list_plans()
        handles = [p.handle for p in plans]
        self.assertEqual(handles, ['eshop-pro', 'basic-plan'])

    def test_transport_failure_becomes_service_error(self):
        transport = raising_transport(httpx.ConnectError('refused'))
        with self.assertRaises(MaxioServiceError) as ctx:
            _service(transport).list_plans()
        self.assertEqual(ctx.exception.http_status, 502)


class SubscribeTests(SimpleTestCase):
    def test_creates_customer_then_subscription_when_none_exist(self):
        transport = StubTransport(
            json_response(200, [product_body(1, 'eshop-pro', 'Pro Plan', 29900)]),  # list_products
            json_response(404, {'errors': ['not found']}),                          # lookup -> absent
            json_response(201, customer_body(555, 'oscar-user-42')),                # create_customer
            json_response(200, []),                                                 # existing subs
            json_response(201, subscription_body(999, 'active', 'eshop-pro')),      # create_subscription
        )
        subscription, created = _service(transport).subscribe(_User(), 'eshop-pro')
        self.assertTrue(created)
        self.assertEqual(subscription.id, 999)
        self.assertEqual(len(transport.requests), 5)
        # The create carried the plan handle and the resolved customer id.
        create_req = transport.last_request
        self.assertEqual(create_req.method, 'POST')
        self.assertTrue(create_req.url.endswith('/subscriptions.json'))
        self.assertEqual(create_req.body.value['subscription']['product_handle'], 'eshop-pro')
        self.assertEqual(create_req.body.value['subscription']['customer_id'], 555)

    def test_reuses_existing_customer(self):
        transport = StubTransport(
            json_response(200, [product_body(1, 'eshop-pro', 'Pro Plan', 29900)]),  # list_products
            json_response(200, customer_body(555, 'oscar-user-42')),                # lookup -> found
            json_response(200, []),                                                 # existing subs
            json_response(201, subscription_body(999, 'active', 'eshop-pro')),      # create_subscription
        )
        _service(transport).subscribe(_User(), 'eshop-pro')
        # No create_customer call: 4 requests, not 5.
        self.assertEqual(len(transport.requests), 4)

    def test_idempotent_when_active_subscription_exists(self):
        transport = StubTransport(
            json_response(200, [product_body(1, 'eshop-pro', 'Pro Plan', 29900)]),  # list_products
            json_response(200, customer_body(555, 'oscar-user-42')),                # lookup -> found
            json_response(200, [subscription_body(777, 'active', 'eshop-pro')]),    # existing active
        )
        subscription, created = _service(transport).subscribe(_User(), 'eshop-pro')
        self.assertFalse(created)
        self.assertEqual(subscription.id, 777)
        # No create_subscription call was made.
        self.assertEqual(len(transport.requests), 3)

    def test_canceled_subscription_does_not_block_resubscribe(self):
        transport = StubTransport(
            json_response(200, [product_body(1, 'eshop-pro', 'Pro Plan', 29900)]),
            json_response(200, customer_body(555, 'oscar-user-42')),
            json_response(200, [subscription_body(777, 'canceled', 'eshop-pro')]),  # terminal
            json_response(201, subscription_body(888, 'active', 'eshop-pro')),      # new one created
        )
        subscription, created = _service(transport).subscribe(_User(), 'eshop-pro')
        self.assertTrue(created)
        self.assertEqual(subscription.id, 888)

    def test_unknown_plan_is_404_without_calling_create(self):
        transport = StubTransport(
            json_response(200, [product_body(1, 'eshop-pro', 'Pro Plan', 29900)]),
        )
        with self.assertRaises(MaxioServiceError) as ctx:
            _service(transport).subscribe(_User(), 'no-such-plan')
        self.assertEqual(ctx.exception.http_status, 404)
        self.assertEqual(len(transport.requests), 1)

    def test_422_on_create_is_translated_with_detail(self):
        transport = StubTransport(
            json_response(200, [product_body(1, 'eshop-pro', 'Pro Plan', 29900)]),
            json_response(200, customer_body(555, 'oscar-user-42')),
            json_response(200, []),
            json_response(422, {'errors': ['Product handle is invalid']}),
        )
        with self.assertRaises(MaxioServiceError) as ctx:
            _service(transport).subscribe(_User(), 'eshop-pro')
        self.assertEqual(ctx.exception.http_status, 422)
        self.assertIn('Product handle is invalid', ctx.exception.detail)

    def test_truncated_create_body_is_unknown_outcome(self):
        transport = StubTransport(
            json_response(200, [product_body(1, 'eshop-pro', 'Pro Plan', 29900)]),
            json_response(200, customer_body(555, 'oscar-user-42')),
            json_response(200, []),
            json_response(201, {'subscription': {}}),  # no id
        )
        with self.assertRaises(MaxioServiceError) as ctx:
            _service(transport).subscribe(_User(), 'eshop-pro')
        self.assertEqual(ctx.exception.http_status, 502)


class ListMySubscriptionsTests(SimpleTestCase):
    def test_no_customer_yields_empty_list(self):
        transport = StubTransport(json_response(404, {'errors': ['not found']}))
        self.assertEqual(_service(transport).list_my_subscriptions(_User()), [])

    def test_returns_customer_subscriptions(self):
        transport = StubTransport(
            json_response(200, customer_body(555, 'oscar-user-42')),
            json_response(200, [subscription_body(777, 'active', 'eshop-pro')]),
        )
        subs = _service(transport).list_my_subscriptions(_User())
        self.assertEqual([s.id for s in subs], [777])
