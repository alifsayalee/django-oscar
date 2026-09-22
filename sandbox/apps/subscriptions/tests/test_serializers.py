"""Tests for the SDK-model -> JSON-dict serializers."""

from django.test import SimpleTestCase

from maxio_advanced_billing.models import Product, Subscription

from apps.subscriptions.serializers import serialize_plan, serialize_subscription


class SerializePlanTests(SimpleTestCase):
    def test_shapes_a_plan_with_handle_and_price(self):
        product = Product(id=1, name='Pro Plan', handle='eshop-pro',
                          price_in_cents=29900, interval=1, interval_unit='month',
                          require_credit_card=False)
        out = serialize_plan(product)
        self.assertEqual(out['planHandle'], 'eshop-pro')
        self.assertEqual(out['price'], '299.00')
        self.assertEqual(out['priceInCents'], 29900)
        self.assertEqual(out['intervalUnit'], 'month')
        self.assertFalse(out['paymentMethodRequired'])

    def test_unset_members_become_none_not_sentinel(self):
        # A product with only an id: everything else is UNSET and must not leak.
        out = serialize_plan(Product(id=9))
        self.assertIsNone(out['planHandle'])
        self.assertIsNone(out['price'])
        # Must be JSON-encodable (no UNSET sentinel anywhere).
        import json
        json.dumps(out)


class SerializeSubscriptionTests(SimpleTestCase):
    def test_shapes_subscription_fields(self):
        sub = Subscription(id=999, state='active',
                           current_billing_amount_in_cents=29900, currency='USD')
        out = serialize_subscription(sub)
        self.assertEqual(out['subscriptionId'], 999)
        self.assertEqual(out['state'], 'active')
        self.assertEqual(out['currentBillingAmount'], '299.00')
        import json
        json.dumps(out)
