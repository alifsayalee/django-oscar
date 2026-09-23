"""Pure serialization / outcome-mapping tests — no SDK client needed."""

from django.test import SimpleTestCase

from ..serializers import (
    is_live,
    outcome_from_state,
    serialize_plan,
    serialize_subscription,
)
from .fakes import PLANS


class _Obj:
    """Minimal stand-in for an SDK model (attribute access)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class OutcomeMappingTests(SimpleTestCase):
    def test_active_is_done_and_live(self):
        self.assertEqual(outcome_from_state("active"), "done")
        self.assertTrue(is_live("active"))

    def test_pending_is_pending_and_live(self):
        self.assertEqual(outcome_from_state("pending"), "pending")
        self.assertTrue(is_live("pending"))

    def test_failed_to_create_is_failed_not_live(self):
        self.assertEqual(outcome_from_state("failed_to_create"), "failed")
        self.assertFalse(is_live("failed_to_create"))

    def test_canceled_is_not_live(self):
        self.assertEqual(outcome_from_state("canceled"), "failed")
        self.assertFalse(is_live("canceled"))

    def test_unlisted_state_is_unknown_not_done(self):
        # A state Maxio adds later must NOT be silently classified as done or failed.
        self.assertEqual(outcome_from_state("some_new_future_state"), "unknown")
        self.assertFalse(is_live("some_new_future_state"))

    def test_missing_state_is_unknown(self):
        self.assertEqual(outcome_from_state(None), "unknown")


class SerializePlanTests(SimpleTestCase):
    def test_plan_carries_planhandle_and_price(self):
        product = _Obj(
            id=PLANS[0]["id"], name="Pro Plan", handle="eshop-pro", description="d",
            price_in_cents=29900, interval=1, interval_unit="month", archived_at=None,
        )
        out = serialize_plan(product)
        self.assertEqual(out["planHandle"], "eshop-pro")
        self.assertEqual(out["price"]["amountInCents"], 29900)
        self.assertEqual(out["price"]["amount"], "299.00")
        self.assertEqual(out["price"]["currency"], "USD")
        self.assertEqual(out["intervalUnit"], "month")


class SerializeSubscriptionTests(SimpleTestCase):
    def test_next_billing_and_state(self):
        product = _Obj(id=1, handle="eshop-pro", name="Pro Plan")
        sub = _Obj(
            id=99001, state="active", product=product,
            current_billing_amount_in_cents=29900,
            current_period_ends_at=None, next_assessment_at=None,
            current_period_started_at=None, created_at=None,
        )
        out = serialize_subscription(sub)
        self.assertEqual(out["subscriptionId"], 99001)
        self.assertEqual(out["state"], "active")
        self.assertEqual(out["outcome"], "done")
        self.assertEqual(out["planHandle"], "eshop-pro")
