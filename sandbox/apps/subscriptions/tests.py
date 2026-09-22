# mypy: disable-error-code="misc, union-attr, index"
# Test module: subclasses Django's untyped TestCase (misc), introspects the SDK's
# request-body union `req.body.value` (union-attr) and the untyped test client's
# `.json()` (index). These are test-harness ergonomics, not contract facts; the
# production modules type-check clean under mypy --strict.
"""Tests for the Maxio subscriptions app.

Service tests fake the SDK's transport seam (``custom_http_client``) so the real
request-building/decoding pipeline runs with no network — asserting on our own
boundary behaviour, not the SDK's. View tests drive the endpoints through Django's
test client with the service layer stubbed, so auth/routing/shape are covered
without touching Maxio. Basic auth means no token request precedes an operation:
the first request the stub sees is the operation itself.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase

from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import HttpRequest, HttpResponse

from . import services
from .errors import MaxioServiceError, PlanNotFound


# --------------------------------------------------------------------------- #
# Transport doubles
# --------------------------------------------------------------------------- #
class StubTransport:
    """Satisfies the SDK's sync transport protocol: send() + close()."""

    def __init__(self, *responses: HttpResponse) -> None:
        self._responses = list(responses)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self._responses.pop(0)

    def close(self) -> None:  # pragma: no cover - trivial
        ...

    @property
    def last_request(self) -> HttpRequest:
        return self.requests[-1]


class RaisingTransport(StubTransport):
    """Answers queued responses, then raises ``error`` on the next send."""

    def __init__(self, error: Exception, *responses: HttpResponse) -> None:
        super().__init__(*responses)
        self._error = error

    def send(self, request: HttpRequest) -> HttpResponse:
        if self._responses:
            return super().send(request)
        self.requests.append(request)
        raise self._error


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def make_client(transport: StubTransport) -> MaxioAdvancedBillingClient:
    return MaxioAdvancedBillingClient(
        basic_auth={"username": "k", "password": "x"},
        environment="us",
        server_config={"production": {"us": {"site": "test-site"}}},
        custom_http_client=transport,
    )


# Canonical response bodies -------------------------------------------------- #
FAMILIES_BODY = [
    {"product_family": {"id": 100, "handle": "eshop-subscribe", "name": "eShop"}}
]
PRODUCTS_BODY = [
    {
        "product": {
            "id": 1,
            "handle": "eshop-pro",
            "name": "Pro Plan",
            "price_in_cents": 29900,
            "interval": 1,
            "interval_unit": "month",
            "require_credit_card": False,
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
            "require_credit_card": False,
        }
    },
]
CUSTOMER_BODY = {"customer": {"id": 555, "reference": "eshop-user-1"}}
ACTIVE_SUB_BODY = {
    "subscription": {
        "id": 9001,
        "state": "active",
        "product": {"handle": "eshop-pro", "name": "Pro Plan"},
        "product_price_in_cents": 29900,
        "current_period_ends_at": "2026-10-23T00:00:00Z",
    }
}


def a_user(pk: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        pk=pk, first_name="Ada", last_name="Lovelace",
        email="ada@example.com", username="ada",
    )


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #
class PlanServiceTests(TestCase):
    def test_list_plans_exposes_plan_handle(self) -> None:
        transport = StubTransport(
            json_response(200, FAMILIES_BODY),
            json_response(200, PRODUCTS_BODY),
        )
        plans = services.list_plans(make_client(transport))

        self.assertEqual([p["planHandle"] for p in plans], ["eshop-pro", "basic-plan"])
        self.assertEqual(plans[0]["priceInCents"], 29900)
        self.assertEqual(plans[0]["intervalUnit"], "month")
        # Family resolved by handle, then products fetched for its id.
        self.assertIn("/product_families.json", transport.requests[0].url)
        self.assertIn("/product_families/100/products.json", transport.requests[1].url)

    def test_missing_family_is_a_502(self) -> None:
        transport = StubTransport(json_response(200, []))  # no matching family
        with self.assertRaises(MaxioServiceError) as ctx:
            services.list_plans(make_client(transport))
        self.assertEqual(ctx.exception.status_code, 502)


# --------------------------------------------------------------------------- #
# Customer identity (idempotency)
# --------------------------------------------------------------------------- #
class EnsureCustomerTests(TestCase):
    def test_reuses_existing_customer(self) -> None:
        transport = StubTransport(json_response(200, CUSTOMER_BODY))
        cid = services.ensure_customer(make_client(transport), a_user())
        self.assertEqual(cid, 555)
        self.assertEqual(len(transport.requests), 1)  # no create attempted

    def test_creates_customer_when_missing(self) -> None:
        transport = StubTransport(
            json_response(404, {"error": "not found"}),
            json_response(201, {"customer": {"id": 777, "reference": "eshop-user-1"}}),
        )
        cid = services.ensure_customer(make_client(transport), a_user())
        self.assertEqual(cid, 777)
        create = transport.last_request
        self.assertEqual(create.method, "POST")
        self.assertEqual(create.body.value["customer"]["reference"], "eshop-user-1")
        self.assertEqual(create.body.value["customer"]["email"], "ada@example.com")

    def test_recovers_from_create_race(self) -> None:
        # lookup misses, create loses the race (422), re-lookup finds the winner.
        transport = StubTransport(
            json_response(404, {"error": "not found"}),
            json_response(422, {"errors": ["reference: already taken"]}),
            json_response(200, {"customer": {"id": 888, "reference": "eshop-user-1"}}),
        )
        cid = services.ensure_customer(make_client(transport), a_user())
        self.assertEqual(cid, 888)


# --------------------------------------------------------------------------- #
# Subscribe
# --------------------------------------------------------------------------- #
class SubscribeTests(TestCase):
    def test_creates_subscription(self) -> None:
        transport = StubTransport(
            json_response(200, FAMILIES_BODY),
            json_response(200, PRODUCTS_BODY),
            json_response(200, CUSTOMER_BODY),
            json_response(200, []),  # no existing subscriptions
            json_response(201, ACTIVE_SUB_BODY),
        )
        result, created = services.subscribe(a_user(), "eshop-pro", make_client(transport))
        self.assertTrue(created)
        self.assertEqual(result["subscriptionId"], 9001)
        self.assertEqual(result["state"], "active")
        self.assertEqual(result["planHandle"], "eshop-pro")
        self.assertEqual(result["nextBillingDate"], "2026-10-23T00:00:00+00:00")
        create = transport.last_request
        self.assertEqual(create.method, "POST")
        self.assertEqual(create.body.value["subscription"]["product_handle"], "eshop-pro")
        self.assertEqual(create.body.value["subscription"]["customer_id"], 555)

    def test_double_submit_reuses_active_subscription(self) -> None:
        transport = StubTransport(
            json_response(200, FAMILIES_BODY),
            json_response(200, PRODUCTS_BODY),
            json_response(200, CUSTOMER_BODY),
            json_response(200, [ACTIVE_SUB_BODY]),  # already subscribed
        )
        result, created = services.subscribe(a_user(), "eshop-pro", make_client(transport))
        self.assertFalse(created)
        self.assertEqual(result["subscriptionId"], 9001)
        # No create_subscription request was made (4 requests, not 5).
        self.assertEqual(len(transport.requests), 4)

    def test_terminal_subscription_does_not_block_new(self) -> None:
        canceled = {
            "subscription": {
                "id": 1, "state": "canceled",
                "product": {"handle": "eshop-pro", "name": "Pro Plan"},
            }
        }
        transport = StubTransport(
            json_response(200, FAMILIES_BODY),
            json_response(200, PRODUCTS_BODY),
            json_response(200, CUSTOMER_BODY),
            json_response(200, [canceled]),
            json_response(201, ACTIVE_SUB_BODY),
        )
        _result, created = services.subscribe(a_user(), "eshop-pro", make_client(transport))
        self.assertTrue(created)

    def test_unknown_plan_raises_plan_not_found(self) -> None:
        transport = StubTransport(
            json_response(200, FAMILIES_BODY),
            json_response(200, PRODUCTS_BODY),
        )
        with self.assertRaises(PlanNotFound) as ctx:
            services.subscribe(a_user(), "no-such-plan", make_client(transport))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_defaults_to_pro_plan(self) -> None:
        transport = StubTransport(
            json_response(200, FAMILIES_BODY),
            json_response(200, PRODUCTS_BODY),
            json_response(200, CUSTOMER_BODY),
            json_response(200, []),
            json_response(201, ACTIVE_SUB_BODY),
        )
        services.subscribe(a_user(), None, make_client(transport))
        create = transport.last_request
        self.assertEqual(create.body.value["subscription"]["product_handle"], "eshop-pro")


# --------------------------------------------------------------------------- #
# my-subscriptions
# --------------------------------------------------------------------------- #
class MySubscriptionsTests(TestCase):
    def test_returns_empty_when_no_customer(self) -> None:
        transport = StubTransport(json_response(404, {"error": "not found"}))
        subs = services.list_my_subscriptions(a_user(), make_client(transport))
        self.assertEqual(subs, [])

    def test_maps_subscriptions(self) -> None:
        transport = StubTransport(
            json_response(200, CUSTOMER_BODY),
            json_response(200, [ACTIVE_SUB_BODY]),
        )
        subs = services.list_my_subscriptions(a_user(), make_client(transport))
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["subscriptionId"], 9001)
        self.assertEqual(subs[0]["planHandle"], "eshop-pro")


# --------------------------------------------------------------------------- #
# Error boundary
# --------------------------------------------------------------------------- #
class ErrorBoundaryTests(TestCase):
    def test_provider_401_becomes_our_502(self) -> None:
        transport = StubTransport(json_response(401, {"error": "bad key"}))
        with self.assertRaises(MaxioServiceError) as ctx:
            services.list_my_subscriptions(a_user(), make_client(transport))
        self.assertEqual(ctx.exception.status_code, 502)

    def test_refused_connection_is_known_outcome(self) -> None:
        transport = RaisingTransport(httpx.ConnectError("refused"))
        with self.assertRaises(MaxioServiceError) as ctx:
            services.list_my_subscriptions(a_user(), make_client(transport))
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertFalse(ctx.exception.outcome_unknown)

    def test_read_timeout_is_unknown_outcome_on_write(self) -> None:
        # customer exists, then create_subscription read-times-out: may have landed.
        transport = RaisingTransport(
            httpx.ReadTimeout("no reply"),
            json_response(200, FAMILIES_BODY),
            json_response(200, PRODUCTS_BODY),
            json_response(200, CUSTOMER_BODY),
            json_response(200, []),
        )
        with self.assertRaises(MaxioServiceError) as ctx:
            services.subscribe(a_user(), "eshop-pro", make_client(transport))
        self.assertEqual(ctx.exception.status_code, 504)
        self.assertTrue(ctx.exception.outcome_unknown)

    def test_undecodable_success_body_is_reported(self) -> None:
        # A 200 whose id is the wrong type fails to decode (not an ApiError).
        transport = StubTransport(json_response(200, {"customer": {"id": "not-an-int"}}))
        with self.assertRaises(MaxioServiceError) as ctx:
            services.list_my_subscriptions(a_user(), make_client(transport))
        self.assertEqual(ctx.exception.status_code, 502)


# --------------------------------------------------------------------------- #
# HTTP endpoints (auth, routing, response shape) with services stubbed
# --------------------------------------------------------------------------- #
class EndpointTests(TestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(
            username="shopper", email="shopper@example.com", password="pw12345!"
        )

    def test_plans_requires_authentication(self) -> None:
        response = self.client.get("/api/subscription-plans")
        self.assertEqual(response.status_code, 401)

    def test_my_subscriptions_requires_authentication(self) -> None:
        response = self.client.get("/api/my-subscriptions")
        self.assertEqual(response.status_code, 401)

    def test_plans_endpoint(self) -> None:
        self.client.force_login(self.user)
        with mock.patch.object(
            services, "list_plans", return_value=[{"planHandle": "eshop-pro"}]
        ):
            response = self.client.get("/api/subscription-plans")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["plans"][0]["planHandle"], "eshop-pro")

    def test_wrong_method_is_405(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get("/api/subscriptions")  # POST-only
        self.assertEqual(response.status_code, 405)

    def test_subscribe_returns_subscription_id_top_level(self) -> None:
        self.client.force_login(self.user)
        payload: dict[str, Any] = {"subscriptionId": 9001, "state": "active"}
        with mock.patch.object(services, "subscribe", return_value=(payload, True)) as sub:
            response = self.client.post(
                "/api/subscriptions",
                data=json.dumps({"planHandle": "eshop-pro"}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["subscriptionId"], 9001)
        self.assertTrue(response.json()["created"])
        # planHandle from the body reached the service.
        _args, kwargs = sub.call_args
        self.assertIn("eshop-pro", sub.call_args.args + tuple(kwargs.values()))

    def test_subscribe_reuse_returns_200(self) -> None:
        self.client.force_login(self.user)
        payload = {"subscriptionId": 9001}
        with mock.patch.object(services, "subscribe", return_value=(payload, False)):
            response = self.client.post("/api/subscriptions", data="{}",
                                        content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["created"])

    def test_service_error_maps_to_status(self) -> None:
        self.client.force_login(self.user)
        err = MaxioServiceError("boom", status_code=504, outcome_unknown=True)
        with mock.patch.object(services, "subscribe", side_effect=err):
            response = self.client.post("/api/subscriptions", data="{}",
                                        content_type="application/json")
        self.assertEqual(response.status_code, 504)
        self.assertTrue(response.json()["outcomeUnknown"])
