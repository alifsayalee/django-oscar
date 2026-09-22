"""Tests for the HTTP endpoints: auth gating and JSON shaping.

The service layer is stubbed here so these tests exercise routing, session-auth
gating and response shaping without touching the network (the service itself is
covered against the SDK seam in test_service).
"""

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from maxio_advanced_billing.models import Product, Subscription

from apps.subscriptions.service import MaxioServiceError


class _FakeService:
    """Stands in for MaxioSubscriptionService; records calls, returns canned data."""

    instances = []

    def __init__(self, *args, **kwargs):
        _FakeService.instances.append(self)
        self.subscribed = []

    def list_plans(self):
        return [Product(id=1, name='Pro Plan', handle='eshop-pro', price_in_cents=29900,
                        interval=1, interval_unit='month', require_credit_card=False)]

    def subscribe(self, user, plan_handle):
        self.subscribed.append((user.pk, plan_handle))
        return Subscription(id=999, state='active', product={'handle': plan_handle},
                            current_billing_amount_in_cents=29900, currency='USD'), True

    def list_my_subscriptions(self, user):
        return [Subscription(id=999, state='active', product={'handle': 'eshop-pro'})]


class ViewAuthTests(TestCase):
    def test_plans_requires_login(self):
        resp = self.client.get('/api/subscription-plans')
        self.assertEqual(resp.status_code, 401)

    def test_subscribe_requires_login(self):
        resp = self.client.post('/api/subscriptions', data='{}',
                                content_type='application/json')
        self.assertEqual(resp.status_code, 401)


class ViewFlowTests(TestCase):
    def setUp(self):
        _FakeService.instances = []
        User = get_user_model()
        self.user = User.objects.create_user('shopper', 'shopper@example.com', 'pw-secret-123')
        self.client.force_login(self.user)

    @mock.patch('apps.subscriptions.views.MaxioSubscriptionService', _FakeService)
    def test_list_plans_returns_handles(self):
        resp = self.client.get('/api/subscription-plans')
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data['plans'][0]['planHandle'], 'eshop-pro')

    @mock.patch('apps.subscriptions.views.MaxioSubscriptionService', _FakeService)
    def test_subscribe_returns_top_level_subscription_id(self):
        resp = self.client.post('/api/subscriptions',
                                data=json.dumps({'planHandle': 'eshop-pro'}),
                                content_type='application/json')
        self.assertEqual(resp.status_code, 201)
        data = json.loads(resp.content)
        self.assertEqual(data['subscriptionId'], 999)
        self.assertEqual(data['subscription']['planHandle'], 'eshop-pro')

    @mock.patch('apps.subscriptions.views.MaxioSubscriptionService', _FakeService)
    def test_subscribe_defaults_plan_when_omitted(self):
        resp = self.client.post('/api/subscriptions', data='{}',
                                content_type='application/json')
        self.assertEqual(resp.status_code, 201)
        # Default plan handle (settings.MAXIO_DEFAULT_PLAN_HANDLE) was used.
        self.assertEqual(_FakeService.instances[-1].subscribed[-1][1], 'eshop-pro')

    @mock.patch('apps.subscriptions.views.MaxioSubscriptionService', _FakeService)
    def test_my_subscriptions_lists(self):
        resp = self.client.get('/api/my-subscriptions')
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data['subscriptions'][0]['subscriptionId'], 999)

    def test_service_error_maps_to_status(self):
        class Failing(_FakeService):
            def list_plans(self):
                raise MaxioServiceError('boom', http_status=502)

        with mock.patch('apps.subscriptions.views.MaxioSubscriptionService', Failing):
            resp = self.client.get('/api/subscription-plans')
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(json.loads(resp.content)['error'], 'maxio_error')
