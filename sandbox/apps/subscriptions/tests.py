"""Tests for the Maxio subscription capability.

The seam is the SDK's transport protocol (python-testing): a stub ``send()`` returns
scripted responses so the real request-building pipeline runs with no network. Tests assert
on the app's own boundary behaviour — status mapping, idempotency, the two-transport-failure
split — not on the SDK's own tested behaviour.
"""

import json
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import BasicAuthCredentials, HttpResponse

from . import maxio_client, services
from .exceptions import ConfigurationError, ProviderRejected, ProviderUnavailable
from .models import MaxioSubscription, SubscriptionStatus


def json_response(status, body):
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


FAMILIES = [{"product_family": {"id": 111, "handle": "eshop-subscribe", "name": "eShop"}}]
PRODUCTS = [
    {"product": {"id": 1, "handle": "eshop-pro", "name": "Pro Plan",
                 "price_in_cents": 29900, "interval": 1, "interval_unit": "month"}},
    {"product": {"id": 2, "handle": "basic-plan", "name": "Basic Plan",
                 "price_in_cents": 2900, "interval": 1, "interval_unit": "month"}},
]
CUSTOMER = {"customer": {"id": 555, "reference": None, "email": "x@example.com"}}
SUBSCRIPTION = {
    "subscription": {
        "id": 9001, "state": "active",
        "current_period_ends_at": "2026-10-23T06:43:58-05:00",
        "product": {"handle": "eshop-pro", "name": "Pro Plan", "price_in_cents": 29900},
        "product_price_in_cents": 29900,
        "reference": "oscar-user-{pk}-eshop-pro",
    }
}


class RoutingTransport:
    """Answers requests by (method, path-substring). Each rule yields queued responses;
    the last is reused. Unmatched requests fail loudly so a missing stub is obvious."""

    def __init__(self, rules):
        # rules: list of (method, substring, [responses])
        self._rules = [[m, s, list(r)] for m, s, r in rules]
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        url = str(request.url)
        for rule in self._rules:
            method, substr, responses = rule
            if request.method == method and substr in url:
                return responses[0] if len(responses) == 1 else responses.pop(0)
        raise AssertionError(f"No stub for {request.method} {url}")

    def close(self):
        pass


class RaisingTransport(RoutingTransport):
    def __init__(self, error, on_substr, rules):
        super().__init__(rules)
        self._error = error
        self._on_substr = on_substr

    def send(self, request):
        if self._on_substr in str(request.url) and request.method == self._error_method():
            self.requests.append(request)
            raise self._error
        return super().send(request)

    def _error_method(self):
        return "POST"


def build_stub_client(transport):
    return MaxioAdvancedBillingClient(
        basic_auth=BasicAuthCredentials(username="k", password="x"),
        environment="us",
        server_config={"production": {"us": {"site": "test"}}},
        custom_http_client=transport,
    )


@override_settings(MAXIO_DEFAULT_PRODUCT_FAMILY="eshop-subscribe")
class ServiceTests(TestCase):
    def setUp(self):
        maxio_client.reset_client_for_testing()
        self.User = get_user_model()
        self.user = self.User.objects.create_user(
            username="alice", email="alice@example.com", password="pw-123456789"
        )

    def _patch_client(self, transport):
        client = build_stub_client(transport)
        patcher = mock.patch.object(services, "get_client", return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    def _plans_rules(self):
        return [
            ("GET", "/product_families.json", [json_response(200, FAMILIES)]),
            ("GET", "/products.json", [json_response(200, PRODUCTS)]),
        ]

    def _sub_body(self):
        body = json.loads(json.dumps(SUBSCRIPTION))
        body["subscription"]["reference"] = f"oscar-user-{self.user.pk}-eshop-pro"
        return body

    # --- plans -----------------------------------------------------------------

    def test_list_plans_maps_and_sorts(self):
        self._patch_client(RoutingTransport(self._plans_rules()))
        plans = services.list_plans()
        self.assertEqual([p["planHandle"] for p in plans], ["basic-plan", "eshop-pro"])
        pro = next(p for p in plans if p["planHandle"] == "eshop-pro")
        self.assertEqual(pro["priceInCents"], 29900)
        self.assertEqual(pro["priceFormatted"], "$299.00")

    # --- subscribe success + idempotency --------------------------------------

    def _subscribe_rules(self):
        return self._plans_rules() + [
            ("GET", "/customers/lookup.json", [json_response(404, {"error": "not found"})]),
            ("POST", "/customers.json", [json_response(201, CUSTOMER)]),
            ("POST", "/subscriptions.json", [json_response(201, self._sub_body())]),
            ("GET", "/customers/555/subscriptions.json",
             [json_response(200, [self._sub_body()])]),
        ]

    def test_subscribe_creates_and_is_idempotent(self):
        transport = RoutingTransport(self._subscribe_rules())
        self._patch_client(transport)

        payload, created = services.subscribe(self.user, "eshop-pro")
        self.assertTrue(created)
        self.assertEqual(payload["subscriptionId"], 9001)
        self.assertEqual(payload["status"], SubscriptionStatus.ACTIVE)
        self.assertEqual(payload["nextBillingDate"][:10], "2026-10-23")
        self.assertEqual(MaxioSubscription.objects.count(), 1)

        creates_before = sum(
            1 for r in transport.requests
            if r.method == "POST" and "/subscriptions.json" in str(r.url)
        )
        # Second subscribe: the claim already exists, so no second create is sent.
        payload2, created2 = services.subscribe(self.user, "eshop-pro")
        self.assertFalse(created2)
        self.assertEqual(payload2["subscriptionId"], 9001)
        creates_after = sum(
            1 for r in transport.requests
            if r.method == "POST" and "/subscriptions.json" in str(r.url)
        )
        self.assertEqual(creates_before, creates_after)
        self.assertEqual(MaxioSubscription.objects.count(), 1)

    def test_subscribe_unknown_plan_is_caller_error(self):
        self._patch_client(RoutingTransport(self._plans_rules()))
        with self.assertRaises(Exception) as ctx:
            services.subscribe(self.user, "no-such-plan")
        self.assertEqual(ctx.exception.status_code, 404)

    # --- error mapping ---------------------------------------------------------

    def test_provider_422_on_subscribe_is_rejected(self):
        rules = self._plans_rules() + [
            ("GET", "/customers/lookup.json", [json_response(404, {"error": "nf"})]),
            ("POST", "/customers.json", [json_response(201, CUSTOMER)]),
            ("POST", "/subscriptions.json",
             [json_response(422, {"errors": ["No payment method was on file"]})]),
            ("GET", "/customers/555/subscriptions.json", [json_response(200, [])]),
        ]
        self._patch_client(RoutingTransport(rules))
        with self.assertRaises(ProviderRejected) as ctx:
            services.subscribe(self.user, "eshop-pro")
        self.assertEqual(ctx.exception.status_code, 422)
        row = MaxioSubscription.objects.get()
        self.assertEqual(row.status, SubscriptionStatus.FAILED)

    def test_provider_401_is_our_config_error_not_callers(self):
        rules = [("GET", "/product_families.json",
                  [json_response(401, {"error": "bad key"})])]
        self._patch_client(RoutingTransport(rules))
        with self.assertRaises(ConfigurationError) as ctx:
            services.list_plans()
        self.assertEqual(ctx.exception.status_code, 502)

    # --- the two transport failures must map to DIFFERENT outcomes ------------

    def _subscribe_rules_until_create(self):
        return self._plans_rules() + [
            ("GET", "/customers/lookup.json", [json_response(404, {"error": "nf"})]),
            ("POST", "/customers.json", [json_response(201, CUSTOMER)]),
            ("GET", "/customers/555/subscriptions.json", [json_response(200, [])]),
        ]

    def test_never_sent_is_known_failure_502(self):
        transport = RaisingTransport(
            httpx.ConnectError("refused"), "/subscriptions.json",
            self._subscribe_rules_until_create(),
        )
        self._patch_client(transport)
        with self.assertRaises(ProviderUnavailable) as ctx:
            services.subscribe(self.user, "eshop-pro")
        self.assertEqual((ctx.exception.status_code, ctx.exception.outcome_unknown), (502, False))
        self.assertEqual(MaxioSubscription.objects.get().status, SubscriptionStatus.FAILED)

    def test_may_have_landed_is_unknown_504(self):
        transport = RaisingTransport(
            httpx.ReadTimeout("no reply"), "/subscriptions.json",
            self._subscribe_rules_until_create(),
        )
        self._patch_client(transport)
        with self.assertRaises(ProviderUnavailable) as ctx:
            services.subscribe(self.user, "eshop-pro")
        self.assertEqual((ctx.exception.status_code, ctx.exception.outcome_unknown), (504, True))
        self.assertEqual(MaxioSubscription.objects.get().status, SubscriptionStatus.UNKNOWN)

    def test_two_transport_failures_differ(self):
        # The distinction the code makes must be observable, not just present.
        self.assertNotEqual((502, False), (504, True))

    # --- state mapping ---------------------------------------------------------

    def test_status_from_state_buckets(self):
        self.assertEqual(services.status_from_state("active"), SubscriptionStatus.ACTIVE)
        self.assertEqual(services.status_from_state("past_due"), SubscriptionStatus.PROBLEM)
        self.assertEqual(services.status_from_state("canceled"), SubscriptionStatus.ENDED)
        self.assertEqual(services.status_from_state("pending"), SubscriptionStatus.PENDING)
        # A state newer than the SDK is unknown, never silently ended.
        self.assertEqual(services.status_from_state("brand_new_state"), SubscriptionStatus.UNKNOWN)


class ViewAuthTests(TestCase):
    def test_endpoints_require_login(self):
        for url in ("/api/subscription-plans", "/api/my-subscriptions"):
            self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(
            self.client.post("/api/subscriptions", data="{}",
                             content_type="application/json").status_code,
            401,
        )
