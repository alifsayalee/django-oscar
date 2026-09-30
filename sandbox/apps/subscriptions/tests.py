"""
Tests for the subscription API. Maxio is replaced at the SDK's transport seam,
so the real client builds and decodes every request.

Run from sandbox/:  python manage.py test apps.subscriptions
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

import httpx
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from maxio_advanced_billing.core import HttpRequest, HttpResponse, JsonBody
from maxio_advanced_billing.models import (
    Customer, CustomerResponse, ErrorListResponse1, Product, ProductFamily, ProductResponse, Subscription,
    SubscriptionResponse,
)
from maxio_advanced_billing.models.enums import IntervalUnit, SubscriptionState

from . import maxio
from .models import MaxioCustomer, MaxioSubscription

FAMILY = "test-family"
MAXIO_SETTINGS = {
    "MAXIO_API_KEY": "test-key",
    "MAXIO_SITE_SUBDOMAIN": "acme",
    "MAXIO_ENVIRONMENT": "US",
    "MAXIO_DEFAULT_PRODUCT_FAMILY": FAMILY,
    "MAXIO_BASE_URL": None,
    "MAXIO_CUSTOMER_REFERENCE_PREFIX": "test-user-",
    "MAXIO_PAYMENT_COLLECTION_METHOD": "remittance",
}


# The transport seam
# ==================

@dataclass
class StubResponse:
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    chunks: list[bytes] = field(default_factory=list)
    url: str = "https://acme.chargify.com/"
    closed: bool = False

    def iter_bytes(self, chunk_size: int | None) -> Iterator[bytes]:
        yield from self.chunks
        self.close()

    def read(self) -> bytes:
        self.close()
        return b"".join(self.chunks)

    def close(self) -> None:
        self.closed = True


class StubTransport:
    """Answers queued responses in order; a queued exception is raised instead."""

    def __init__(self, *responses: StubResponse | Exception) -> None:
        self._responses = list(responses)
        self.requests: list[HttpRequest] = []

    def queue(self, *responses: StubResponse | Exception) -> None:
        self._responses.extend(responses)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        pass

    def body(self, index: int) -> Any:
        body = self.requests[index].body
        assert isinstance(body, JsonBody)
        return body.value


def json_response(status: int, body: object) -> StubResponse:
    return StubResponse(status_code=status, headers={"content-type": "application/json"},
                        chunks=[json.dumps(body).encode()])


def empty_response(status: int) -> StubResponse:
    return StubResponse(status_code=status)


# Fixtures built from the SDK's own models
# ========================================

def product(handle: str = "pro", *, family: str = FAMILY, price: int = 29900, archived: bool = False) -> Product:
    return Product(
        id=100, name=f"{handle.title()} Plan", handle=handle, description="", price_in_cents=price, interval=1,
        interval_unit=IntervalUnit.MONTH, require_credit_card=False, product_family=ProductFamily(handle=family),
        archived_at=datetime(2026, 1, 1, tzinfo=dt_timezone.utc) if archived else None,
    )


def product_response(handle: str = "pro", **kwargs: Any) -> StubResponse:
    return json_response(200, ProductResponse(product=product(handle, **kwargs)).to_dict())


def customer_response(customer_id: int = 7, reference: str = "test-user-1") -> StubResponse:
    return json_response(200, CustomerResponse(customer=Customer(id=customer_id, reference=reference)).to_dict())


def subscription(subscription_id: int = 555, *, handle: str = "pro", state: SubscriptionState = SubscriptionState.ACTIVE,
                 reference: str | None = None) -> Subscription:
    return Subscription(
        id=subscription_id, state=state, reference=reference, product=product(handle), product_price_in_cents=29900,
        currency="USD", next_assessment_at=datetime(2026, 10, 30, 12, 0, tzinfo=dt_timezone.utc),
    )


def subscription_response(status: int = 201, **kwargs: Any) -> StubResponse:
    return json_response(status, SubscriptionResponse(subscription=subscription(**kwargs)).to_dict())


# Tests
# =====

@override_settings(**MAXIO_SETTINGS)
class SubscriptionApiTestCase(TestCase):

    def setUp(self) -> None:
        cache.clear()
        self.transport = StubTransport()
        self.previous = maxio.install_client(maxio.build_client(custom_http_client=self.transport))
        self.user = get_user_model().objects.create_user(
            username="shopper", email="shopper@example.com", password="pw", first_name="Sam", last_name="Shopper")
        self.client.force_login(self.user)

    def tearDown(self) -> None:
        installed = maxio.install_client(self.previous)
        if installed is not None:
            installed.close()

    def subscribe(self, plan_handle: str = "pro") -> Any:
        return self.client.post("/api/subscriptions", data=json.dumps({"planHandle": plan_handle}),
                                content_type="application/json")

    def existing_customer(self, customer_id: int = 7) -> MaxioCustomer:
        return MaxioCustomer.objects.create(user=self.user, reference=f"test-user-{self.user.pk}",
                                            maxio_customer_id=customer_id, status=MaxioCustomer.CREATED,
                                            lease_at=timezone.now())


class PlanTests(SubscriptionApiTestCase):

    def test_lists_live_plans_of_the_family(self) -> None:
        self.transport.queue(json_response(200, [
            ProductResponse(product=product("pro")).to_dict(),
            ProductResponse(product=product("basic", price=2900)).to_dict(),
            ProductResponse(product=product("old", archived=True)).to_dict(),
        ]))
        response = self.client.get("/api/subscription-plans")
        self.assertEqual(response.status_code, 200)
        plans = response.json()["plans"]
        self.assertEqual([p["planHandle"] for p in plans], ["pro", "basic"])
        self.assertEqual(plans[1]["price"], "29.00")
        self.assertEqual(plans[0]["intervalUnit"], "month")
        request = self.transport.requests[0]
        self.assertEqual(request.method, "GET")
        self.assertTrue(request.url.startswith("https://acme.chargify.com/product_families/handle"), request.url)
        self.assertIn(FAMILY, request.url)
        self.assertTrue(request.headers["authorization"].startswith("Basic "))

    def test_plans_are_cached(self) -> None:
        self.transport.queue(json_response(200, [ProductResponse(product=product()).to_dict()]))
        self.client.get("/api/subscription-plans")
        self.assertEqual(self.client.get("/api/subscription-plans").status_code, 200)
        self.assertEqual(len(self.transport.requests), 1)

    def test_our_credentials_refused_is_a_502(self) -> None:
        self.transport.queue(empty_response(401))
        response = self.client.get("/api/subscription-plans")
        self.assertEqual(response.status_code, 502)

    @override_settings(MAXIO_BASE_URL="http://localhost:9999/maxio")
    def test_base_url_override_is_used_verbatim(self) -> None:
        client = maxio.build_client(custom_http_client=self.transport)
        maxio.install_client(client).close()  # type: ignore[union-attr]
        self.transport.queue(json_response(200, []))
        self.client.get("/api/subscription-plans")
        self.assertTrue(self.transport.requests[0].url.startswith("http://localhost:9999/maxio/product_families/"))


class SubscribeTests(SubscriptionApiTestCase):

    def test_requires_login(self) -> None:
        self.client.logout()
        self.assertEqual(self.subscribe().status_code, 401)
        self.assertEqual(self.client.get("/api/my-subscriptions").status_code, 401)

    def test_requires_plan_handle(self) -> None:
        response = self.client.post("/api/subscriptions", data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_unknown_plan_is_404(self) -> None:
        self.transport.queue(empty_response(404))
        self.assertEqual(self.subscribe("nope").status_code, 404)

    def test_plan_from_another_family_is_404(self) -> None:
        self.transport.queue(product_response("pro", family="other"))
        self.assertEqual(self.subscribe().status_code, 404)
        self.assertEqual(len(self.transport.requests), 1)

    def test_creates_customer_and_subscription(self) -> None:
        self.transport.queue(product_response(), customer_response(7), subscription_response())
        response = self.subscribe()

        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["subscriptionId"], 555)
        self.assertEqual(data["subscription"]["state"], "active")
        self.assertEqual(data["subscription"]["enrollment"], "active")
        self.assertEqual(data["subscription"]["planHandle"], "pro")
        self.assertEqual(data["subscription"]["price"], "299.00")
        self.assertEqual(data["subscription"]["nextBillingAt"], "2026-10-30T12:00:00+00:00")

        customer_body = self.transport.body(1)["customer"]
        self.assertEqual(customer_body["reference"], f"test-user-{self.user.pk}")
        self.assertEqual(customer_body["email"], "shopper@example.com")
        self.assertEqual(customer_body["first_name"], "Sam")
        subscription_body = self.transport.body(2)["subscription"]
        self.assertEqual(subscription_body["product_handle"], "pro")
        self.assertEqual(subscription_body["customer_id"], 7)
        self.assertEqual(subscription_body["payment_collection_method"], "remittance")

        record = MaxioSubscription.objects.get()
        self.assertEqual(subscription_body["reference"], record.reference)
        self.assertEqual((record.status, record.maxio_subscription_id), (MaxioSubscription.LIVE, 555))
        self.assertEqual(MaxioCustomer.objects.get().maxio_customer_id, 7)

    def test_double_submit_does_not_create_twice(self) -> None:
        self.transport.queue(product_response(), customer_response(7), subscription_response())
        self.assertEqual(self.subscribe().status_code, 201)

        self.transport.queue(product_response())
        response = self.subscribe()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["subscriptionId"], 555)
        self.assertFalse(response.json()["created"])
        self.assertEqual(response.json()["subscription"]["enrollment"], "active")
        # Only the plan read went out the second time: no customer, no subscription.
        self.assertEqual(len(self.transport.requests), 4)
        self.assertEqual(MaxioSubscription.objects.count(), 1)

    def test_in_flight_claim_answers_409(self) -> None:
        self.existing_customer()
        MaxioSubscription.objects.create(user=self.user, plan_handle="pro", reference="r-1",
                                         status=MaxioSubscription.PENDING, lease_at=timezone.now())
        self.transport.queue(product_response())
        self.assertEqual(self.subscribe().status_code, 409)
        self.assertEqual(len(self.transport.requests), 1)

    def test_customer_setup_in_flight_answers_409(self) -> None:
        MaxioCustomer.objects.create(user=self.user, reference=f"test-user-{self.user.pk}", lease_at=timezone.now())
        self.transport.queue(product_response())
        self.assertEqual(self.subscribe().status_code, 409)
        self.assertEqual(len(self.transport.requests), 1)

    def test_stale_customer_claim_is_settled_by_reference(self) -> None:
        MaxioCustomer.objects.create(user=self.user, reference=f"test-user-{self.user.pk}",
                                     lease_at=timezone.now() - timedelta(minutes=10))
        self.transport.queue(product_response(), customer_response(9), subscription_response())
        self.assertEqual(self.subscribe().status_code, 201)
        self.assertEqual(self.transport.requests[1].method, "GET")  # lookup, not a second create
        self.assertEqual(self.transport.body(2)["subscription"]["customer_id"], 9)

    def test_customer_create_timeout_is_settled_by_reference(self) -> None:
        self.transport.queue(product_response(), httpx.ReadTimeout("no reply"), customer_response(8),
                             subscription_response())
        self.assertEqual(self.subscribe().status_code, 201)
        self.assertEqual(MaxioCustomer.objects.get().maxio_customer_id, 8)

    def test_provider_rejection_is_passed_back_and_releases_the_claim(self) -> None:
        self.existing_customer()
        self.transport.queue(product_response(),
                             json_response(422, ErrorListResponse1(errors=["Product is not available"]).to_dict()))
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["details"], ["Product is not available"])
        self.assertEqual(MaxioSubscription.objects.get().status, MaxioSubscription.REJECTED)

        # The claim is released: a new attempt goes through.
        self.transport.queue(product_response(), subscription_response())
        self.assertEqual(self.subscribe().status_code, 201)

    def test_refused_connection_is_known_but_read_timeout_is_unknown(self) -> None:
        self.existing_customer()
        # Never sent: nothing to reconcile, no lookup.
        self.transport.queue(product_response(), httpx.ConnectError("refused"))
        unsent = self.subscribe()
        self.assertEqual(unsent.status_code, 502)
        self.assertNotIn("outcomeUnknown", unsent.json())
        self.assertEqual(len(self.transport.requests), 2)
        self.assertEqual(MaxioSubscription.objects.get().status, MaxioSubscription.REJECTED)

        # May have landed: looked up by reference, and found.
        self.transport.queue(product_response(), httpx.ReadTimeout("no reply"), subscription_response(200))
        landed = self.subscribe()
        self.assertEqual(landed.status_code, 201)
        self.assertEqual(landed.json()["subscriptionId"], 555)
        lookup = self.transport.requests[-1]
        self.assertEqual(lookup.method, "GET")
        record = MaxioSubscription.objects.get(status=MaxioSubscription.LIVE)
        self.assertIn(record.reference, lookup.url)

    def test_unknown_outcome_not_found_releases_the_claim(self) -> None:
        self.existing_customer()
        self.transport.queue(product_response(), empty_response(503), empty_response(404))
        response = self.subscribe()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(MaxioSubscription.objects.get().status, MaxioSubscription.REJECTED)

    def test_outcome_stays_unknown_until_a_later_read_settles_it(self) -> None:
        self.existing_customer()
        self.transport.queue(product_response(), httpx.ReadTimeout("no reply"), httpx.ConnectError("down"))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["outcomeUnknown"])
        self.assertIsNone(response.json()["subscriptionId"])
        record = MaxioSubscription.objects.get()
        self.assertEqual(record.status, MaxioSubscription.UNKNOWN)

        # The next read of the user's subscriptions settles it by reference.
        self.transport.queue(subscription_response(200, reference=record.reference),
                             json_response(200, [SubscriptionResponse(subscription=subscription()).to_dict()]))
        listing = self.client.get("/api/my-subscriptions")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual([s["subscriptionId"] for s in listing.json()["subscriptions"]], [555])
        record.refresh_from_db()
        self.assertEqual((record.status, record.maxio_subscription_id), (MaxioSubscription.LIVE, 555))

    def test_truncated_success_is_not_reported_as_success(self) -> None:
        self.existing_customer()
        self.transport.queue(product_response(), json_response(201, {}), subscription_response(200))
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.transport.requests[-1].method, "GET")  # settled by lookup

    def test_unreadable_success_is_an_unknown_outcome(self) -> None:
        self.existing_customer()
        self.transport.queue(product_response(), json_response(201, {"subscription": {"id": "not-a-number"}}),
                             httpx.ConnectError("down"))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(MaxioSubscription.objects.get().status, MaxioSubscription.UNKNOWN)

    def test_not_done_yet_state_is_reported_as_pending(self) -> None:
        self.existing_customer()
        self.transport.queue(product_response(), subscription_response(state=SubscriptionState.AWAITING_SIGNUP))
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["subscription"]["enrollment"], "pending")

    def test_failed_to_create_releases_the_plan(self) -> None:
        self.existing_customer()
        self.transport.queue(product_response(), subscription_response(state=SubscriptionState.FAILED_TO_CREATE))
        response = self.subscribe()
        self.assertEqual(response.json()["subscription"]["enrollment"], "failed")
        self.assertEqual(MaxioSubscription.objects.get().status, MaxioSubscription.ENDED)


class MySubscriptionsTests(SubscriptionApiTestCase):

    def test_no_customer_yet_is_empty(self) -> None:
        response = self.client.get("/api/my-subscriptions")
        self.assertEqual(response.json(), {"subscriptions": []})
        self.assertEqual(self.transport.requests, [])

    def test_reads_from_maxio_and_refreshes_local_state(self) -> None:
        self.existing_customer(7)
        record = MaxioSubscription.objects.create(user=self.user, plan_handle="pro", reference="r-1",
                                                  maxio_subscription_id=555, status=MaxioSubscription.LIVE,
                                                  state="active", lease_at=timezone.now())
        self.transport.queue(json_response(200, [
            SubscriptionResponse(subscription=subscription(555, state=SubscriptionState.CANCELED)).to_dict(),
            SubscriptionResponse(subscription=subscription(556, handle="basic")).to_dict(),
        ]))
        response = self.client.get("/api/my-subscriptions")
        self.assertEqual(response.status_code, 200)
        subs = response.json()["subscriptions"]
        self.assertEqual([(s["subscriptionId"], s["state"]) for s in subs], [(555, "canceled"), (556, "active")])
        self.assertTrue(self.transport.requests[0].url.endswith("/customers/7/subscriptions.json"))
        record.refresh_from_db()
        self.assertEqual(record.status, MaxioSubscription.ENDED)
