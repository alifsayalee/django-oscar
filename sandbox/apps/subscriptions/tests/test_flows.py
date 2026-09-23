"""End-to-end flow tests through the real request-building pipeline (fake transport) and the HTTP
views, covering the hero flow and its idempotency guarantees."""

import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from .. import services
from ..client import reset_client
from ..models import ClaimStatus, MaxioCustomer, MaxioSubscription
from ..serializers import outcome_from_state
from . import fakes

User = get_user_model()

TEST_SETTINGS = dict(
    MAXIO_API_KEY="test-key",
    MAXIO_SITE_SUBDOMAIN="test-site",
    MAXIO_DEFAULT_PRODUCT_FAMILY=fakes.FAMILY_HANDLE,
    MAXIO_BASE_URL="",
)


@override_settings(**TEST_SETTINGS)
class SubscribeFlowTests(TestCase):
    def setUp(self):
        services._family_id_cache.clear()
        self.user = User.objects.create_user(username="alice", password="password12345")

    def tearDown(self):
        reset_client()
        services._family_id_cache.clear()

    def test_list_plans_returns_planhandles(self):
        fakes.install(fakes.FakeMaxio())
        plans = services.list_plans()
        handles = {p["planHandle"] for p in plans}
        self.assertEqual(handles, {"eshop-pro", "basic-plan"})

    def test_subscribe_creates_customer_and_subscription(self):
        fake = fakes.install(fakes.FakeMaxio(create_state="active"))
        claim, sub = services.subscribe(self.user, "eshop-pro")
        self.assertIsNotNone(sub)
        self.assertEqual(claim.status, ClaimStatus.DONE)
        # customer + subscription ledger rows exist and are linked to Maxio ids
        self.assertTrue(MaxioCustomer.objects.filter(user=self.user, customer_id=555).exists())
        self.assertEqual(claim.subscription_id, sub.id)
        self.assertEqual(len(fake.subscriptions), 1)

    def test_subscribe_is_idempotent(self):
        fake = fakes.install(fakes.FakeMaxio(create_state="active"))
        claim1, sub1 = services.subscribe(self.user, "eshop-pro")
        claim2, sub2 = services.subscribe(self.user, "eshop-pro")
        # No second subscription created at Maxio; same subscription returned.
        self.assertEqual(len(fake.subscriptions), 1)
        self.assertEqual(sub1.id, sub2.id)
        self.assertEqual(MaxioSubscription.objects.filter(user=self.user).count(), 1)

    def test_subscribe_uses_provider_side_guard_when_ledger_empty(self):
        # A subscription already live at Maxio for this plan (not via our ledger) is reused.
        fake = fakes.install(fakes.FakeMaxio(create_state="active", customer_exists=True))
        # ensure_customer will find customer 555; pre-seed a live subscription tagged with the ref.
        ref = f"oscar-sub-{self.user.pk}-eshop-pro"
        fake.subscriptions.append(fake._make_subscription(
            {"product_handle": "eshop-pro", "reference": ref}))
        _claim, sub = services.subscribe(self.user, "eshop-pro")
        self.assertEqual(len(fake.subscriptions), 1)  # nothing new created
        self.assertIsNotNone(sub)

    def test_failed_to_create_maps_to_failed_not_done(self):
        # An id comes back, but the state is a failure -> outcome must be 'failed', not 'done'.
        fakes.install(fakes.FakeMaxio(create_state="failed_to_create"))
        claim, sub = services.subscribe(self.user, "eshop-pro")
        self.assertIsNotNone(sub)  # a record exists
        self.assertEqual(claim.status, ClaimStatus.FAILED)
        self.assertEqual(outcome_from_state(sub.state), "failed")

    def test_unknown_state_maps_to_unknown(self):
        fakes.install(fakes.FakeMaxio(create_state="some_brand_new_state"))
        claim, sub = services.subscribe(self.user, "eshop-pro")
        self.assertEqual(claim.status, ClaimStatus.UNKNOWN)

    def test_unknown_plan_is_rejected(self):
        fakes.install(fakes.FakeMaxio())
        from ..exceptions import ProviderRejected
        with self.assertRaises(ProviderRejected) as ctx:
            services.subscribe(self.user, "no-such-plan")
        self.assertEqual(ctx.exception.status_code, 400)


@override_settings(**TEST_SETTINGS)
class ViewTests(TestCase):
    def setUp(self):
        services._family_id_cache.clear()
        self.user = User.objects.create_user(username="bob", password="password12345")
        self.client = Client()

    def tearDown(self):
        reset_client()
        services._family_id_cache.clear()

    def test_endpoints_require_authentication(self):
        for url in ("/api/subscription-plans", "/api/my-subscriptions"):
            self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.client.post("/api/subscriptions").status_code, 401)

    def test_plans_endpoint(self):
        fakes.install(fakes.FakeMaxio())
        self.client.force_login(self.user)
        resp = self.client.get("/api/subscription-plans")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(all("planHandle" in p for p in data["plans"]))

    def test_subscribe_endpoint_returns_subscription_id_top_level(self):
        fakes.install(fakes.FakeMaxio(create_state="active"))
        self.client.force_login(self.user)
        resp = self.client.post(
            "/api/subscriptions",
            data=json.dumps({"planHandle": "eshop-pro"}),
            content_type="application/json",
        )
        self.assertIn(resp.status_code, (200, 201))
        data = resp.json()
        self.assertIn("subscriptionId", data)  # top-level field, as required
        self.assertIsNotNone(data["subscriptionId"])
        self.assertEqual(data["subscription"]["planHandle"], "eshop-pro")

    def test_my_subscriptions_endpoint(self):
        fakes.install(fakes.FakeMaxio(create_state="active"))
        self.client.force_login(self.user)
        self.client.post(
            "/api/subscriptions",
            data=json.dumps({"planHandle": "eshop-pro"}),
            content_type="application/json",
        )
        resp = self.client.get("/api/my-subscriptions")
        self.assertEqual(resp.status_code, 200)
        subs = resp.json()["subscriptions"]
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["planHandle"], "eshop-pro")
