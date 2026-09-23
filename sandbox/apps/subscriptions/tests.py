"""Tests for the Maxio subscriptions integration.

They fake the SDK's transport (the supported seam, per python-testing) so no network call
happens, and exercise the real request-building pipeline. A routing transport matches on
(method, path) rather than a strict response queue, keeping the multi-call subscribe flow
readable.
"""

import json

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import HttpResponse

from . import services
from .exceptions import (
    MaxioConfigError,
    MaxioUnavailable,
    OutcomeUnknown,
    PlanNotFound,
)
from .models import SubscriptionIntent


def json_response(status, body):
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


PRODUCTS_BODY = [
    {
        "product": {
            "id": 1,
            "handle": "eshop-pro",
            "name": "Pro Plan",
            "price_in_cents": 29900,
            "interval": 1,
            "interval_unit": "month",
        }
    },
    {
        "product": {
            "id": 2,
            "handle": "basic-plan",
            "name": "Basic Plan",
            "price_in_cents": 2900,
            "interval": 1,
            "interval_unit": "month",
        }
    },
]

SUBSCRIPTION_BODY = {
    "subscription": {
        "id": 12345,
        "state": "active",
        "next_assessment_at": "2026-10-23T00:00:00Z",
        "current_period_ends_at": "2026-10-23T00:00:00Z",
        "product": {"handle": "eshop-pro", "name": "Pro Plan", "price_in_cents": 29900},
    }
}


class RoutingTransport:
    """Sync transport whose responses are chosen by (method, path substring)."""

    def __init__(self, routes):
        # routes: list of (method, url_substring, HttpResponse | Exception | callable)
        self.routes = routes
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        for method, substring, action in self.routes:
            if request.method == method and substring in str(request.url):
                if isinstance(action, Exception):
                    raise action
                return action(request) if callable(action) else action
        raise AssertionError(f"no stub route for {request.method} {request.url}")

    def close(self):
        pass


@override_settings(MAXIO_DEFAULT_PRODUCT_FAMILY="123")
class SubscriptionServiceTests(TestCase):
    def setUp(self):
        services._family_id_cache.clear()
        self.user = get_user_model().objects.create_user(
            username="alice", email="alice@example.com", password="password12345"
        )

    def _use(self, *routes):
        transport = RoutingTransport(list(routes))
        client = MaxioAdvancedBillingClient(
            custom_http_client=transport,
            basic_auth={"username": "k", "password": "x"},
            environment="us",
            server_config={"production": {"us": {"site": "test"}}},
        )
        services.get_client = lambda: client  # type: ignore[assignment]
        return transport

    # ------------------------------------------------------------------ plans

    def test_list_plans_carries_plan_handle(self):
        self._use(("GET", "/products.json", json_response(200, PRODUCTS_BODY)))
        plans = services.list_plans()
        self.assertEqual(len(plans), 2)
        handles = {p["planHandle"] for p in plans}
        self.assertEqual(handles, {"eshop-pro", "basic-plan"})
        pro = next(p for p in plans if p["planHandle"] == "eshop-pro")
        self.assertEqual(pro["priceInCents"], 29900)
        self.assertEqual(pro["priceFormatted"], "$299.00")

    # ------------------------------------------------------------------ subscribe

    def _happy_routes(self):
        return [
            ("GET", "/products.json", json_response(200, PRODUCTS_BODY)),
            ("GET", "/customers/lookup.json", json_response(404, {"error": "not found"})),
            ("POST", "/customers.json", json_response(201, {"customer": {"id": 555}})),
            ("GET", "/customers/555/subscriptions.json", json_response(200, [])),
            ("POST", "/subscriptions.json", json_response(201, SUBSCRIPTION_BODY)),
        ]

    def test_subscribe_happy_path(self):
        self._use(*self._happy_routes())
        result = services.subscribe(self.user, "eshop-pro")
        self.assertEqual(result["subscriptionId"], 12345)
        self.assertEqual(result["state"], "active")
        self.assertEqual(result["planHandle"], "eshop-pro")
        self.assertEqual(result["priceFormatted"], "$299.00")
        self.assertFalse(result["idempotent"])
        self.assertTrue(result["nextBillingAt"].startswith("2026-10-23"))

        intent = SubscriptionIntent.objects.get(user=self.user, plan_handle="eshop-pro")
        self.assertEqual(intent.status, SubscriptionIntent.STATUS_DONE)
        self.assertEqual(intent.maxio_subscription_id, 12345)
        self.assertEqual(intent.maxio_customer_id, 555)

    def test_subscribe_is_idempotent_no_duplicate(self):
        transport = self._use(*self._happy_routes())
        first = services.subscribe(self.user, "eshop-pro")
        self.assertFalse(first["idempotent"])

        # Second attempt: only a read of the existing subscription should occur.
        self._use(
            ("GET", "/products.json", json_response(200, PRODUCTS_BODY)),
            ("GET", "/subscriptions/12345.json", json_response(200, SUBSCRIPTION_BODY)),
        )
        second = services.subscribe(self.user, "eshop-pro")
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["subscriptionId"], 12345)
        # Exactly one intent row, still done: no duplicate customer/subscription.
        self.assertEqual(
            SubscriptionIntent.objects.filter(user=self.user, plan_handle="eshop-pro").count(),
            1,
        )

    def test_subscribe_adopts_existing_maxio_subscription(self):
        # No local intent, but Maxio already has a live subscription for this product:
        # it must be adopted, not duplicated.
        self._use(
            ("GET", "/products.json", json_response(200, PRODUCTS_BODY)),
            ("GET", "/customers/lookup.json", json_response(200, {"customer": {"id": 555}})),
            ("GET", "/customers/555/subscriptions.json", json_response(200, [SUBSCRIPTION_BODY])),
        )
        result = services.subscribe(self.user, "eshop-pro")
        self.assertEqual(result["subscriptionId"], 12345)
        self.assertEqual(result["state"], "active")
        # No create_subscription call was routed; adoption succeeded.

    def test_subscribe_unknown_plan(self):
        self._use(("GET", "/products.json", json_response(200, PRODUCTS_BODY)))
        with self.assertRaises(PlanNotFound):
            services.subscribe(self.user, "no-such-plan")
        self.assertEqual(SubscriptionIntent.objects.count(), 0)

    def test_subscribe_read_timeout_is_unknown_outcome(self):
        self._use(
            ("GET", "/products.json", json_response(200, PRODUCTS_BODY)),
            ("GET", "/customers/lookup.json", json_response(404, {"error": "x"})),
            ("POST", "/customers.json", json_response(201, {"customer": {"id": 555}})),
            ("GET", "/customers/555/subscriptions.json", json_response(200, [])),
            ("POST", "/subscriptions.json", httpx.ReadTimeout("no reply")),
            ("GET", "/subscriptions/lookup.json", json_response(404, {"error": "x"})),
        )
        with self.assertRaises(OutcomeUnknown) as ctx:
            services.subscribe(self.user, "eshop-pro")
        self.assertEqual(ctx.exception.status_code, 504)
        self.assertTrue(ctx.exception.outcome_unknown)
        intent = SubscriptionIntent.objects.get(user=self.user, plan_handle="eshop-pro")
        self.assertEqual(intent.status, SubscriptionIntent.STATUS_UNKNOWN)

    # ------------------------------------------------------------------ errors

    def test_credentials_rejected_is_config_error(self):
        self._use(("GET", "/products.json", json_response(401, {"error": "unauthorized"})))
        with self.assertRaises(MaxioConfigError) as ctx:
            services.list_plans()
        self.assertEqual(ctx.exception.status_code, 502)

    def test_connect_error_is_known_unavailable(self):
        self._use(("GET", "/products.json", httpx.ConnectError("refused")))
        with self.assertRaises(MaxioUnavailable) as ctx:
            services.list_plans()
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertFalse(ctx.exception.outcome_unknown)

    # ------------------------------------------------------------------ my subs

    def test_my_subscriptions_empty_when_no_customer(self):
        self._use(("GET", "/customers/lookup.json", json_response(404, {"error": "x"})))
        self.assertEqual(services.list_my_subscriptions(self.user), [])

    def test_my_subscriptions_lists_customer_subscriptions(self):
        self._use(
            ("GET", "/customers/lookup.json", json_response(200, {"customer": {"id": 555}})),
            ("GET", "/customers/555/subscriptions.json", json_response(200, [SUBSCRIPTION_BODY])),
        )
        subs = services.list_my_subscriptions(self.user)
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["subscriptionId"], 12345)
        self.assertEqual(subs[0]["planHandle"], "eshop-pro")


class SubscriptionViewAuthTests(TestCase):
    def test_endpoints_require_login(self):
        for path in ("/api/subscription-plans", "/api/my-subscriptions"):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(
            self.client.post(
                "/api/subscriptions",
                data=json.dumps({"planHandle": "eshop-pro"}),
                content_type="application/json",
            ).status_code,
            401,
        )
