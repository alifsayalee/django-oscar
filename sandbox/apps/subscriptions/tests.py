"""
Tests for the Maxio subscriptions API.

Maxio is faked at the SDK's transport seam: a real SDK client is built with a stub
transport, so request building, auth and decoding run for real with no network.
Run from ``sandbox/``: ``python manage.py test apps.subscriptions``.
"""

import base64
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from maxio_advanced_billing.core import HttpRequest, HttpResponse, JsonBody

from . import maxio
from .models import BillingCustomer, Subscription

MAXIO_SETTINGS: dict[str, Any] = {
    "MAXIO_API_KEY": "test-api-key",
    "MAXIO_SITE_SUBDOMAIN": "test-site",
    "MAXIO_ENVIRONMENT": "US",
    "MAXIO_DEFAULT_PRODUCT_FAMILY": "test-family",
    "MAXIO_BASE_URL": None,
    "MAXIO_TIMEOUT": 5.0,
}


@dataclass
class StubResponse:
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    chunks: list[bytes] = field(default_factory=list)
    url: str = "https://test-site.chargify.com/"
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
    """Answers queued responses in order; an Exception in the queue is raised instead."""

    def __init__(self, *responses: StubResponse | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected Maxio request: %s %s" % (request.method, request.url))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        pass

    def calls(self) -> list[tuple[str, str]]:
        return [(str(r.method), httpx.URL(r.url).path) for r in self.requests]


def json_response(status: int, body: object) -> StubResponse:
    return StubResponse(status_code=status, headers={"content-type": "application/json"},
                        chunks=[json.dumps(body).encode()])


def empty_response(status: int) -> StubResponse:
    return StubResponse(status_code=status)


def product(handle: str, name: str, cents: int, **extra: Any) -> dict[str, Any]:
    return {"product": {"id": 700 + cents % 97, "handle": handle, "name": name, "price_in_cents": cents,
                        "interval": 1, "interval_unit": "month", "require_credit_card": False,
                        "product_family": {"handle": "test-family"}, **extra}}


PLANS = [product("eshop-pro", "Pro Plan", 29900), product("basic-plan", "Basic Plan", 2900)]


def customer_body(customer_id: int, reference: str) -> dict[str, Any]:
    return {"customer": {"id": customer_id, "reference": reference, "first_name": "Ada",
                         "last_name": "Lovelace", "email": "ada@example.com"}}


def subscription_body(sub_id: int, reference: str, *, handle: str = "eshop-pro", state: str = "active",
                      customer_id: int = 55) -> dict[str, Any]:
    plan = next(p["product"] for p in PLANS if p["product"]["handle"] == handle)
    return {"subscription": {
        "id": sub_id, "state": state, "reference": reference, "currency": "USD",
        "product_price_in_cents": plan["price_in_cents"],
        "current_period_ends_at": "2026-10-29T12:00:00-04:00",
        "activated_at": "2026-09-29T12:00:00-04:00",
        "customer": {"id": customer_id}, "product": plan,
    }}


def body_of(request: HttpRequest) -> Any:
    assert isinstance(request.body, JsonBody)
    return request.body.value


@override_settings(**MAXIO_SETTINGS)
class SubscriptionsApiTestCase(TestCase):

    def setUp(self) -> None:
        cache.clear()
        self.user = get_user_model().objects.create_user(
            username="ada", email="ada@example.com", password="correct horse battery"
        )
        self.client.force_login(self.user)

    def use_maxio(self, *responses: StubResponse | Exception) -> StubTransport:
        transport = StubTransport(*responses)
        client = maxio.build_client(maxio.MaxioConfig.from_settings(), transport=transport)
        context = maxio.override_client(client)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return transport

    def subscribe(self, plan: str = "eshop-pro") -> Any:
        return self.client.post("/api/subscriptions", data=json.dumps({"planHandle": plan}),
                                content_type="application/json")

    customer_ref = "oscar-cus-test"

    # -- plans ---------------------------------------------------------------

    def test_lists_plans_from_the_configured_family(self) -> None:
        archived = product("old-plan", "Old", 100, archived_at="2025-01-01T00:00:00Z")
        transport = self.use_maxio(json_response(200, PLANS + [archived]))

        response = self.client.get("/api/subscription-plans")

        self.assertEqual(response.status_code, 200)
        plans = response.json()["plans"]
        self.assertEqual([p["planHandle"] for p in plans], ["eshop-pro", "basic-plan"])
        self.assertEqual(plans[0]["price"], {"amountInCents": 29900, "amount": "299.00"})
        self.assertEqual(plans[0]["intervalUnit"], "month")
        request = transport.requests[0]
        self.assertEqual(request.method, "GET")
        self.assertTrue(request.url.startswith("https://test-site.chargify.com/product_families/"))
        self.assertIn("handle:test-family", httpx.URL(request.url).path)
        expected = base64.b64encode(b"test-api-key:x").decode()
        self.assertEqual(request.headers["authorization"], f"Basic {expected}")
        self.assertIn("csrftoken", response.cookies)

    def test_plans_are_cached(self) -> None:
        transport = self.use_maxio(json_response(200, PLANS))
        self.client.get("/api/subscription-plans")
        self.client.get("/api/subscription-plans")
        self.assertEqual(len(transport.requests), 1)

    def test_requires_login(self) -> None:
        anonymous = Client()
        for method, url in (("get", "/api/subscription-plans"), ("post", "/api/subscriptions"),
                            ("get", "/api/my-subscriptions"), ("post", "/api/billing-customer")):
            response = getattr(anonymous, method)(url)
            self.assertEqual(response.status_code, 401, url)
            self.assertEqual(response.json()["error"]["code"], "not_authenticated")

    def test_post_requires_csrf_token(self) -> None:
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        response = strict.post("/api/subscriptions", data=json.dumps({"planHandle": "eshop-pro"}),
                               content_type="application/json")
        self.assertEqual(response.status_code, 403)

    # -- subscribe -------------------------------------------------------------

    def test_subscribe_creates_customer_then_subscription(self) -> None:
        transport = self.use_maxio(
            json_response(200, PLANS),
            empty_response(404),                                    # customer lookup: none yet
            json_response(201, customer_body(55, self.customer_ref)),
            json_response(201, subscription_body(101, "set-below")),
        )

        response = self.subscribe()

        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["subscriptionId"], 101)
        self.assertEqual(body["planHandle"], "eshop-pro")
        self.assertEqual(body["state"], "active")
        self.assertEqual(body["price"], {"amountInCents": 29900, "amount": "299.00", "currency": "USD"})
        self.assertEqual(body["nextBillingAt"], "2026-10-29T16:00:00+00:00")
        self.assertTrue(body["created"])

        self.assertEqual(transport.calls(), [
            ("GET", "/product_families/handle:test-family/products.json"),
            ("GET", "/customers/lookup.json"),
            ("POST", "/customers.json"),
            ("POST", "/subscriptions.json"),
        ])
        reference = BillingCustomer.objects.get().reference
        self.assertRegex(reference, r"^oscar-cus-[0-9a-f]{32}$")
        self.assertIn(f"reference={reference}", transport.requests[1].url)
        customer = body_of(transport.requests[2])["customer"]
        self.assertEqual(customer["email"], "ada@example.com")
        self.assertEqual(customer["reference"], reference)
        self.assertEqual(customer["first_name"], "ada")
        row = Subscription.objects.get()
        sent = body_of(transport.requests[3])["subscription"]
        self.assertEqual(
            {key: sent[key] for key in ("product_handle", "customer_id", "reference", "payment_collection_method")},
            {"product_handle": "eshop-pro", "customer_id": 55, "reference": row.reference,
             "payment_collection_method": "remittance"},
        )
        self.assertNotIn("payment_profile_attributes", sent)
        self.assertEqual((row.claim_state, row.is_live, row.maxio_subscription_id),
                         (Subscription.CONFIRMED, True, 101))
        self.assertEqual(BillingCustomer.objects.get().maxio_customer_id, 55)

    def test_existing_maxio_customer_is_adopted_by_reference(self) -> None:
        transport = self.use_maxio(
            json_response(200, PLANS),
            json_response(200, customer_body(77, self.customer_ref)),
            json_response(201, subscription_body(102, "x", customer_id=77)),
        )
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        self.assertNotIn(("POST", "/customers.json"), transport.calls())
        self.assertEqual(body_of(transport.requests[-1])["subscription"]["customer_id"], 77)

    def test_repeated_subscribe_returns_existing_without_calling_maxio_again(self) -> None:
        transport = self.use_maxio(
            json_response(200, PLANS),
            empty_response(404),
            json_response(201, customer_body(55, self.customer_ref)),
            json_response(201, subscription_body(101, "x")),
        )
        first = self.subscribe()
        second = self.subscribe()

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["subscriptionId"], 101)
        self.assertFalse(second.json()["created"])
        self.assertEqual(len(transport.requests), 4)
        self.assertEqual(Subscription.objects.count(), 1)
        self.assertEqual(BillingCustomer.objects.count(), 1)

    def test_in_flight_claim_refuses_a_concurrent_subscribe(self) -> None:
        BillingCustomer.objects.create(user=self.user, reference=self.customer_ref, maxio_customer_id=55,
                                       status=BillingCustomer.READY, claimed_at=timezone.now())
        Subscription.objects.create(user=self.user, product_family="test-family", plan_handle="eshop-pro",
                                    claimed_at=timezone.now())
        transport = self.use_maxio(json_response(200, PLANS))

        response = self.subscribe()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "subscription_in_progress")
        self.assertNotIn(("POST", "/subscriptions.json"), transport.calls())

    def test_in_flight_customer_claim_refuses_a_concurrent_request(self) -> None:
        BillingCustomer.objects.create(user=self.user, reference=self.customer_ref, claimed_at=timezone.now())
        transport = self.use_maxio(json_response(200, PLANS))
        response = self.subscribe()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "customer_setup_in_progress")
        self.assertEqual(len(transport.requests), 1)

    def test_subscribing_to_a_different_plan_while_active_is_a_conflict(self) -> None:
        transport = self.use_maxio(
            json_response(200, PLANS),
            empty_response(404),
            json_response(201, customer_body(55, self.customer_ref)),
            json_response(201, subscription_body(101, "x")),
            json_response(200, subscription_body(101, "x")),       # re-read: still active
        )
        self.subscribe()
        response = self.subscribe("basic-plan")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "already_subscribed")
        self.assertEqual(transport.calls()[-1], ("GET", "/subscriptions/101.json"))

    def test_canceled_subscription_no_longer_blocks_a_new_one(self) -> None:
        self.use_maxio(
            json_response(200, PLANS),
            empty_response(404),
            json_response(201, customer_body(55, self.customer_ref)),
            json_response(201, subscription_body(101, "x")),
            json_response(200, subscription_body(101, "x", state="canceled")),
            json_response(201, subscription_body(102, "y", handle="basic-plan")),
        )
        self.subscribe()
        response = self.subscribe("basic-plan")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["subscriptionId"], 102)
        self.assertEqual(Subscription.objects.filter(is_live=True).get().maxio_subscription_id, 102)

    def test_unknown_plan_is_rejected_before_any_write(self) -> None:
        transport = self.use_maxio(json_response(200, PLANS), json_response(200, PLANS))
        response = self.subscribe("no-such-plan")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "unknown_plan")
        self.assertTrue(all(method == "GET" for method, _ in transport.calls()))

    def test_plan_requiring_a_payment_method_is_refused_before_any_write(self) -> None:
        card_plan = product("card-plan", "Card Plan", 1000, require_credit_card=True)
        transport = self.use_maxio(json_response(200, PLANS + [card_plan]))
        response = self.subscribe("card-plan")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "payment_method_required")
        self.assertEqual(len(transport.requests), 1)
        self.assertFalse(BillingCustomer.objects.exists())

    def test_missing_plan_handle(self) -> None:
        response = self.client.post("/api/subscriptions", data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/subscriptions", data="not json", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    # -- failure kinds -----------------------------------------------------------

    def _ready_customer(self) -> None:
        BillingCustomer.objects.create(user=self.user, reference=self.customer_ref, maxio_customer_id=55,
                                       status=BillingCustomer.READY, claimed_at=timezone.now())

    def test_read_timeout_is_unknown_and_the_retry_reconciles_instead_of_recreating(self) -> None:
        self._ready_customer()
        transport = self.use_maxio(json_response(200, PLANS), httpx.ReadTimeout("no reply"))

        first = self.subscribe()

        self.assertEqual(first.status_code, 504)
        self.assertTrue(first.json()["error"]["retryable"])
        row = Subscription.objects.get()
        self.assertEqual((row.claim_state, row.is_live, row.outcome_unknown), (Subscription.PENDING, True, True))

        # It did land in Maxio: the retry finds it by reference and does not create another.
        transport.responses.append(json_response(200, [subscription_body(103, row.reference)]))
        second = self.subscribe()

        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(second.json()["subscriptionId"], 103)
        self.assertEqual([c for c in transport.calls() if c[0] == "POST"], [("POST", "/subscriptions.json")])
        self.assertEqual(transport.calls()[-1], ("GET", "/customers/55/subscriptions.json"))

    def test_unknown_outcome_that_did_not_land_is_released_and_recreated(self) -> None:
        self._ready_customer()
        transport = self.use_maxio(json_response(200, PLANS), httpx.ReadTimeout("no reply"))
        self.subscribe()
        transport.responses += [json_response(200, []), json_response(201, subscription_body(104, "z"))]

        response = self.subscribe()

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Subscription.objects.filter(claim_state=Subscription.RELEASED).count(), 1)
        self.assertEqual(Subscription.objects.get(is_live=True).maxio_subscription_id, 104)

    def test_refused_connection_is_a_known_failure_and_releases_the_claim(self) -> None:
        self._ready_customer()
        self.use_maxio(json_response(200, PLANS), httpx.ConnectError("refused"))

        response = self.subscribe()

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "billing_unreachable")
        row = Subscription.objects.get()
        self.assertEqual((row.claim_state, row.is_live, row.outcome_unknown),
                         (Subscription.RELEASED, False, False))

    def test_unsent_and_unknown_failures_are_told_apart(self) -> None:
        self._ready_customer()
        self.use_maxio(json_response(200, PLANS), httpx.ConnectError("refused"))
        unsent = self.subscribe()
        cache.clear()
        self.use_maxio(json_response(200, PLANS), httpx.ReadTimeout("no reply"))
        unknown = self.subscribe()
        self.assertEqual(unsent.status_code, 502)
        self.assertEqual(unknown.status_code, 504)
        self.assertEqual(Subscription.objects.filter(outcome_unknown=True).count(), 1)

    def test_maxio_rejection_is_reported_with_details_and_releases_the_claim(self) -> None:
        self._ready_customer()
        self.use_maxio(json_response(200, PLANS),
                       json_response(422, {"errors": ["Product must be active"]}))

        response = self.subscribe()

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["details"], ["Product must be active"])
        self.assertFalse(Subscription.objects.get().is_live)

    def test_truncated_success_body_is_not_reported_as_success(self) -> None:
        self._ready_customer()
        self.use_maxio(json_response(200, PLANS), json_response(201, {}))
        response = self.subscribe()
        self.assertEqual(response.status_code, 502)
        self.assertTrue(Subscription.objects.get().outcome_unknown)

    def test_unreadable_success_body_is_an_unknown_outcome(self) -> None:
        self._ready_customer()
        self.use_maxio(json_response(200, PLANS), json_response(201, {"subscription": {"id": "not-an-int"}}))
        response = self.subscribe()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "billing_unreadable")
        self.assertTrue(Subscription.objects.get().outcome_unknown)

    def test_our_bad_credentials_are_never_the_callers_401(self) -> None:
        self.use_maxio(json_response(401, {"errors": ["access denied"]}))
        response = self.client.get("/api/subscription-plans")
        self.assertEqual(response.status_code, 502)

    def test_customer_create_rejection_releases_the_customer_claim(self) -> None:
        self.use_maxio(
            json_response(200, PLANS),
            empty_response(404),
            json_response(422, {"errors": ["Email address: is invalid"]}),   # does not fit the typed arm
            empty_response(404),                                              # verify: nothing was created
        )
        response = self.subscribe()
        self.assertEqual(response.status_code, 502)
        self.assertFalse(BillingCustomer.objects.exists())   # released: a retry starts cleanly

    def test_account_without_email_is_rejected(self) -> None:
        self.user.email = ""
        self.user.save()
        self.use_maxio(json_response(200, PLANS))
        response = self.subscribe()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "email_required")

    def test_stale_customer_claim_is_taken_over(self) -> None:
        BillingCustomer.objects.create(user=self.user, reference=self.customer_ref,
                                       claimed_at=timezone.now() - timedelta(minutes=5))
        self.use_maxio(json_response(200, customer_body(55, self.customer_ref)))
        response = self.client.post("/api/billing-customer")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["customerId"], 55)

    # -- read-back -----------------------------------------------------------------

    def test_my_subscriptions_reads_through_from_maxio(self) -> None:
        transport = self.use_maxio(
            json_response(200, PLANS),
            empty_response(404),
            json_response(201, customer_body(55, self.customer_ref)),
            json_response(201, subscription_body(101, "x")),
            json_response(200, [subscription_body(101, "x", state="past_due")]),
        )
        self.subscribe()

        response = self.client.get("/api/my-subscriptions")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["source"], "maxio")
        self.assertEqual([(s["subscriptionId"], s["state"]) for s in body["subscriptions"]], [(101, "past_due")])
        self.assertEqual(Subscription.objects.get().state, "past_due")
        self.assertEqual(transport.calls()[-1], ("GET", "/customers/55/subscriptions.json"))

    def test_my_subscriptions_falls_back_to_the_snapshot_when_maxio_is_down(self) -> None:
        self.use_maxio(
            json_response(200, PLANS),
            empty_response(404),
            json_response(201, customer_body(55, self.customer_ref)),
            json_response(201, subscription_body(101, "x")),
            httpx.ConnectError("refused"),
        )
        self.subscribe()
        body = self.client.get("/api/my-subscriptions").json()
        self.assertEqual(body["source"], "cache")
        self.assertEqual(body["subscriptions"][0]["subscriptionId"], 101)

    def test_my_subscriptions_is_empty_for_a_new_user(self) -> None:
        response = self.client.get("/api/my-subscriptions")
        self.assertEqual(response.json(), {"subscriptions": [], "source": "maxio"})

    def test_subscription_detail_hides_other_customers_subscriptions(self) -> None:
        self._ready_customer()
        self.use_maxio(json_response(200, subscription_body(900, "other", customer_id=999)))
        response = self.client.get("/api/subscriptions/900")
        self.assertEqual(response.status_code, 404)

    def test_subscription_detail_reads_from_maxio(self) -> None:
        self._ready_customer()
        self.use_maxio(json_response(200, subscription_body(101, "x")))
        response = self.client.get("/api/subscriptions/101")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["planName"], "Pro Plan")


class MaxioConfigTestCase(TestCase):

    @override_settings(**{**MAXIO_SETTINGS, "MAXIO_BASE_URL": "http://127.0.0.1:9999/maxio"})
    def test_base_url_override_is_used_verbatim(self) -> None:
        transport = StubTransport(json_response(200, PLANS))
        client = maxio.build_client(maxio.MaxioConfig.from_settings(), transport=transport)
        with client:
            client.product_families.list_products_for_product_family("handle:test-family")
        self.assertTrue(transport.requests[0].url.startswith("http://127.0.0.1:9999/maxio/product_families/"))

    @override_settings(**{**MAXIO_SETTINGS, "MAXIO_ENVIRONMENT": "eu"})
    def test_eu_environment_uses_the_eu_host(self) -> None:
        transport = StubTransport(json_response(200, PLANS))
        client = maxio.build_client(maxio.MaxioConfig.from_settings(), transport=transport)
        with client:
            client.product_families.list_products_for_product_family("handle:test-family")
        self.assertTrue(transport.requests[0].url.startswith("https://test-site.ebilling.maxio.com/"))

    @override_settings(**{**MAXIO_SETTINGS, "MAXIO_API_KEY": ""})
    def test_missing_credentials_answer_503(self) -> None:
        user = get_user_model().objects.create_user(username="u", email="u@example.com", password="x" * 12)
        self.client.force_login(user)
        response = self.client.get("/api/subscription-plans")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "billing_not_configured")

    @override_settings(**{**MAXIO_SETTINGS, "MAXIO_ENVIRONMENT": "mars"})
    def test_unknown_environment_is_refused(self) -> None:
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaises(ImproperlyConfigured):
            maxio.MaxioConfig.from_settings()
