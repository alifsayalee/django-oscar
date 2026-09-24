"""
Tests for the Maxio subscription API.

The seam is the SDK's transport protocol: a stub transport stands in for the
network, so the real SDK builds and decodes every request. Run with:

    cd sandbox && python manage.py test apps.subscriptions
"""

import json
from datetime import timedelta
from typing import Any

import httpx
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from maxio_advanced_billing.core import HttpRequest, HttpResponse, JsonBody

from . import maxio
from .models import MaxioWrite
from .outcomes import status_from_provider
from .services import customer_reference, idempotency_key, subscription_reference

MAXIO_SETTINGS = dict(
    MAXIO_API_KEY="test-key",
    MAXIO_SITE_SUBDOMAIN="test-site",
    MAXIO_ENVIRONMENT="US",
    MAXIO_DEFAULT_PRODUCT_FAMILY="eshop-subscribe",
    MAXIO_BASE_URL=None,
    MAXIO_TIMEOUT=5.0,
    MAXIO_REFERENCE_PREFIX="test",
    MAXIO_PAYMENT_COLLECTION_METHOD="remittance",
)


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def empty_response(status: int) -> HttpResponse:
    return HttpResponse(status_code=status, headers={}, content=b"")


class StubTransport:
    """Satisfies the SDK's sync transport protocol. Answers queued (method, path, reply) in order."""

    def __init__(self) -> None:
        self.queue: list[tuple[str, str, HttpResponse | Exception]] = []
        self.requests: list[HttpRequest] = []

    def expect(self, method: str, path: str, reply: HttpResponse | Exception) -> "StubTransport":
        self.queue.append((method, path, reply))
        return self

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self.queue:
            raise AssertionError(f"unexpected request {request.method} {request.url}")
        method, path, reply = self.queue.pop(0)
        url_path = httpx.URL(request.url).path
        if request.method != method or url_path != path:
            raise AssertionError(f"expected {method} {path}, got {request.method} {url_path}")
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self) -> None:
        pass

    def posts(self) -> list[HttpRequest]:
        return [r for r in self.requests if r.method == "POST"]


def product(handle: str, price: int, **extra: Any) -> dict[str, Any]:
    return {
        "product": {
            "id": abs(hash(handle)) % 100000,
            "name": handle.title(),
            "handle": handle,
            "price_in_cents": price,
            "interval": 1,
            "interval_unit": "month",
            "require_credit_card": False,
            **extra,
        }
    }


def subscription(sub_id: int, state: str, reference: str, *, plan: str = "eshop-pro",
                 customer_id: int = 501) -> dict[str, Any]:
    return {
        "subscription": {
            "id": sub_id,
            "state": state,
            "reference": reference,
            "product_price_in_cents": 29900,
            "currency": "USD",
            "next_assessment_at": "2026-10-24T12:00:00-04:00",
            "current_period_ends_at": "2026-10-24T12:00:00-04:00",
            "created_at": "2026-09-24T12:00:00-04:00",
            "product": {"handle": plan, "name": "Pro Plan", "interval": 1, "interval_unit": "month"},
            "customer": {"id": customer_id, "reference": "x"},
        }
    }


FAMILY_PATH = "/product_families/handle:eshop-subscribe/products.json"


@override_settings(**MAXIO_SETTINGS)
class SubscriptionApiTestCase(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.transport = StubTransport()
        maxio.set_client(maxio.build_client(self.transport))
        self.user = get_user_model().objects.create_user(
            username="shopper", email="shopper@example.com", password="correct-horse-battery"
        )
        self.client.force_login(self.user)

    def tearDown(self) -> None:
        maxio.set_client(None)

    # helpers

    def expect_plans(self) -> None:
        self.transport.expect("GET", FAMILY_PATH, json_response(200, [
            product("eshop-pro", 29900),
            product("basic-plan", 2900),
            product("old-plan", 100, archived_at="2025-01-01T00:00:00Z"),
        ]))

    def expect_new_customer(self) -> None:
        self.transport.expect("GET", "/customers/lookup.json", empty_response(404))
        self.transport.expect("POST", "/customers.json", json_response(201, {
            "customer": {"id": 501, "reference": customer_reference(self.user), "email": "shopper@example.com"}
        }))

    def sub_ref(self, plan: str = "eshop-pro", generation: int = 1) -> str:
        return subscription_reference(self.user, plan, generation)

    def post_subscribe(self, plan: str = "eshop-pro") -> Any:
        return self.client.post("/api/subscriptions", {"planHandle": plan}, content_type="application/json")

    # plans

    def test_plans_list_active_family_products_with_plan_handle(self) -> None:
        self.expect_plans()
        response = self.client.get("/api/subscription-plans")
        self.assertEqual(response.status_code, 200)
        plans = response.json()["plans"]
        self.assertEqual([p["planHandle"] for p in plans], ["eshop-pro", "basic-plan"])
        self.assertEqual(plans[0]["price"], "299.00")
        url = self.transport.requests[0].url
        self.assertTrue(url.startswith("https://test-site.chargify.com/product_families/handle%3Aeshop-subscribe/"))
        self.assertIn("per_page=200", url)
        self.assertEqual(self.transport.requests[0].headers["authorization"][:6], "Basic ")

    @override_settings(MAXIO_BASE_URL="http://billing.internal:8080")
    def test_base_url_override_is_used_verbatim(self) -> None:
        maxio.set_client(maxio.build_client(self.transport))
        self.expect_plans()
        self.client.get("/api/subscription-plans")
        self.assertTrue(self.transport.requests[0].url.startswith("http://billing.internal:8080/product_families/"))

    # the hero flow

    def test_subscribe_creates_customer_then_subscription(self) -> None:
        ref = self.sub_ref()
        self.expect_plans()
        self.expect_new_customer()
        self.transport.expect("GET", "/subscriptions/lookup.json", empty_response(404))
        self.transport.expect("POST", "/subscriptions.json", json_response(201, subscription(9001, "active", ref)))
        self.transport.expect("GET", "/subscriptions/9001.json", json_response(200, subscription(9001, "active", ref)))

        response = self.post_subscribe()

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["subscriptionId"], 9001)
        self.assertEqual(body["outcome"], "done")
        self.assertEqual(body["state"], "active")
        self.assertEqual(body["planHandle"], "eshop-pro")
        self.assertEqual(body["price"], "299.00")
        self.assertEqual(body["nextBillingAt"], "2026-10-24T12:00:00-04:00")

        customer_post, sub_post = self.transport.posts()
        assert isinstance(customer_post.body, JsonBody) and isinstance(sub_post.body, JsonBody)
        customer_body: Any = customer_post.body.value
        sub_body: Any = sub_post.body.value
        self.assertEqual(customer_body["customer"]["reference"], customer_reference(self.user))
        self.assertEqual(customer_body["customer"]["email"], "shopper@example.com")
        self.assertEqual(sub_body["subscription"], {
            "product_handle": "eshop-pro", "customer_id": 501, "reference": ref,
            "payment_collection_method": "remittance",
        })
        self.assertEqual(sub_post.headers["idempotency-key"], idempotency_key(ref))

    def test_the_same_subscribe_twice_makes_one_provider_write(self) -> None:
        ref = self.sub_ref()
        self.expect_plans()
        self.expect_new_customer()
        self.transport.expect("GET", "/subscriptions/lookup.json", empty_response(404))
        self.transport.expect("POST", "/subscriptions.json", json_response(201, subscription(9001, "active", ref)))
        self.transport.expect("GET", "/subscriptions/9001.json", json_response(200, subscription(9001, "active", ref)))
        # The double-click: only the live re-read, no write.
        self.transport.expect("GET", "/subscriptions/9001.json", json_response(200, subscription(9001, "active", ref)))

        first = self.post_subscribe()
        second = self.post_subscribe()

        self.assertEqual(len(self.transport.posts()), 2)  # one customer create + one subscription create
        self.assertEqual(second.status_code, first.status_code)
        self.assertEqual(second.json()["subscriptionId"], first.json()["subscriptionId"])
        self.assertEqual(MaxioWrite.objects.filter(kind=MaxioWrite.KIND_SUBSCRIPTION).count(), 1)
        self.assertEqual(MaxioWrite.objects.filter(kind=MaxioWrite.KIND_CUSTOMER).count(), 1)

    def test_a_claim_in_flight_answers_in_progress_without_calling_maxio(self) -> None:
        self.expect_plans()
        MaxioWrite.objects.create(reference=customer_reference(self.user), kind=MaxioWrite.KIND_CUSTOMER,
                                  user=self.user, outcome=MaxioWrite.SENDING, claimed_at=timezone.now())
        response = self.post_subscribe()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "request_in_progress")
        self.assertEqual(self.transport.posts(), [])

    def test_existing_customer_at_maxio_is_adopted_not_duplicated(self) -> None:
        ref = self.sub_ref()
        self.expect_plans()
        self.transport.expect("GET", "/customers/lookup.json", json_response(200, {"customer": {"id": 501}}))
        self.transport.expect("GET", "/subscriptions/lookup.json", empty_response(404))
        self.transport.expect("POST", "/subscriptions.json", json_response(201, subscription(9001, "active", ref)))
        self.transport.expect("GET", "/subscriptions/9001.json", json_response(200, subscription(9001, "active", ref)))
        self.assertEqual(self.post_subscribe().status_code, 201)
        self.assertEqual([r.url.split("?")[0].rsplit("/", 1)[-1] for r in self.transport.posts()],
                         ["subscriptions.json"])

    # statuses decide the outcome, not the id

    def test_an_id_with_a_canceled_state_is_not_done(self) -> None:
        ref = self.sub_ref()
        self.expect_plans()
        self.expect_new_customer()
        self.transport.expect("GET", "/subscriptions/lookup.json", empty_response(404))
        self.transport.expect("POST", "/subscriptions.json", json_response(201, subscription(9002, "canceled", ref)))
        self.transport.expect("GET", "/subscriptions/9002.json",
                              json_response(200, subscription(9002, "canceled", ref)))
        response = self.post_subscribe()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["outcome"], "failed")
        self.assertEqual(response.json()["subscriptionId"], 9002)

    def test_an_unlisted_state_is_unknown_not_done(self) -> None:
        ref = self.sub_ref()
        self.expect_plans()
        self.expect_new_customer()
        self.transport.expect("GET", "/subscriptions/lookup.json", empty_response(404))
        self.transport.expect("POST", "/subscriptions.json",
                              json_response(201, subscription(9003, "something_new", ref)))
        self.transport.expect("GET", "/subscriptions/9003.json",
                              json_response(200, subscription(9003, "something_new", ref)))
        response = self.post_subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["outcome"], "unknown")

    def test_status_mapping(self) -> None:
        self.assertEqual(status_from_provider("active"), "done")
        self.assertEqual(status_from_provider("trialing"), "done")
        for pending in ("pending", "assessing", "awaiting_signup", "soft_failure", "past_due",
                        "unpaid", "paused", "on_hold", "suspended"):
            self.assertEqual(status_from_provider(pending), "pending", pending)
        for failed in ("failed_to_create", "canceled", "expired", "trial_ended"):
            self.assertEqual(status_from_provider(failed), "failed", failed)
        self.assertEqual(status_from_provider("brand_new_state"), "unknown")

    def test_a_different_plan_coming_back_needs_review(self) -> None:
        ref = self.sub_ref()
        self.expect_plans()
        self.expect_new_customer()
        self.transport.expect("GET", "/subscriptions/lookup.json", empty_response(404))
        self.transport.expect("POST", "/subscriptions.json",
                              json_response(201, subscription(9004, "active", ref, plan="basic-plan")))
        response = self.post_subscribe()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["outcome"], "needs_review")

    # failures: never sent vs may have landed

    def test_refused_connection_is_known_and_releases_the_claim(self) -> None:
        self.expect_plans()
        self.expect_new_customer()
        self.transport.expect("GET", "/subscriptions/lookup.json", empty_response(404))
        self.transport.expect("POST", "/subscriptions.json", httpx.ConnectError("refused"))
        response = self.post_subscribe()
        self.assertEqual((response.status_code, response.json()["outcomeUnknown"]), (502, False))
        self.assertFalse(MaxioWrite.objects.filter(reference=self.sub_ref()).exists())

    def test_read_timeout_is_unknown_and_checked_by_reference(self) -> None:
        ref = self.sub_ref()
        self.expect_plans()
        self.expect_new_customer()
        self.transport.expect("GET", "/subscriptions/lookup.json", empty_response(404))
        self.transport.expect("POST", "/subscriptions.json", httpx.ReadTimeout("no reply"))
        self.transport.expect("GET", "/subscriptions/lookup.json", empty_response(404))  # the check
        response = self.post_subscribe()
        self.assertEqual((response.status_code, response.json()["outcomeUnknown"]), (504, True))
        self.assertEqual(MaxioWrite.objects.get(reference=ref).outcome, MaxioWrite.UNKNOWN)
        lookup = self.transport.requests[-1]
        self.assertIn(f"reference={ref}", lookup.url)

    def test_a_repeat_after_unknown_only_looks_up_under_the_same_reference(self) -> None:
        ref = self.sub_ref()
        MaxioWrite.objects.create(reference=customer_reference(self.user), kind=MaxioWrite.KIND_CUSTOMER,
                                  user=self.user, outcome=MaxioWrite.DONE, provider_id=501,
                                  claimed_at=timezone.now())
        MaxioWrite.objects.create(reference=ref, kind=MaxioWrite.KIND_SUBSCRIPTION, user=self.user,
                                  plan_handle="eshop-pro", outcome=MaxioWrite.UNKNOWN,
                                  claimed_at=timezone.now() - timedelta(minutes=10))
        self.expect_plans()
        self.transport.expect("GET", "/subscriptions/lookup.json", json_response(200, subscription(9005, "active", ref)))
        self.transport.expect("GET", "/subscriptions/9005.json", json_response(200, subscription(9005, "active", ref)))
        response = self.post_subscribe()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["subscriptionId"], 9005)
        self.assertEqual(self.transport.posts(), [])  # the check never creates

    def test_truncated_success_body_is_not_success(self) -> None:
        self.expect_plans()
        self.expect_new_customer()
        self.transport.expect("GET", "/subscriptions/lookup.json", empty_response(404))
        self.transport.expect("POST", "/subscriptions.json", json_response(201, {}))
        response = self.post_subscribe()
        self.assertEqual((response.status_code, response.json()["outcomeUnknown"]), (504, True))

    def test_a_validation_rejection_is_the_callers_and_releases_the_claim(self) -> None:
        self.expect_plans()
        self.expect_new_customer()
        self.transport.expect("GET", "/subscriptions/lookup.json", empty_response(404))
        self.transport.expect("POST", "/subscriptions.json", json_response(422, {"errors": ["Product is invalid"]}))
        response = self.post_subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["details"], ["Product is invalid"])
        self.assertFalse(MaxioWrite.objects.filter(reference=self.sub_ref()).exists())

    def test_provider_auth_failure_is_ours_not_the_callers(self) -> None:
        self.transport.expect("GET", FAMILY_PATH, empty_response(401))
        response = self.client.get("/api/subscription-plans")
        self.assertEqual(response.status_code, 502)

    # access

    def test_unknown_plan_is_404_and_writes_nothing(self) -> None:
        self.expect_plans()
        response = self.post_subscribe("no-such-plan")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.transport.posts(), [])

    def test_anonymous_callers_cannot_subscribe(self) -> None:
        self.client.logout()
        self.assertEqual(self.post_subscribe().status_code, 401)
        self.assertEqual(self.client.get("/api/my-subscriptions").status_code, 401)

    def test_subscribe_requires_csrf_token_with_session_auth(self) -> None:
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        response = strict.post("/api/subscriptions", {"planHandle": "eshop-pro"}, content_type="application/json")
        self.assertEqual(response.status_code, 403)

    def test_session_login_via_api(self) -> None:
        self.client.logout()
        response = self.client.post("/api/session", {"username": "shopper", "password": "correct-horse-battery"},
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["authenticated"])
        bad = self.client.post("/api/session", {"username": "shopper", "password": "nope"},
                               content_type="application/json")
        self.assertEqual(bad.status_code, 401)

    @override_settings(MAXIO_API_KEY="")
    def test_unconfigured_billing_is_503(self) -> None:
        maxio.set_client(None)
        response = self.client.get("/api/subscription-plans")
        self.assertEqual(response.status_code, 503)

    # reading back

    def test_my_subscriptions_reads_live_from_maxio(self) -> None:
        ref = self.sub_ref()
        MaxioWrite.objects.create(reference=customer_reference(self.user), kind=MaxioWrite.KIND_CUSTOMER,
                                  user=self.user, outcome=MaxioWrite.DONE, provider_id=501,
                                  claimed_at=timezone.now())
        self.transport.expect("GET", "/customers/501/subscriptions.json",
                              json_response(200, [subscription(9001, "active", ref)]))
        response = self.client.get("/api/my-subscriptions")
        self.assertEqual(response.status_code, 200)
        subs = response.json()["subscriptions"]
        self.assertEqual([(s["subscriptionId"], s["state"], s["planHandle"]) for s in subs],
                         [(9001, "active", "eshop-pro")])

    def test_someone_elses_subscription_is_not_found(self) -> None:
        MaxioWrite.objects.create(reference=customer_reference(self.user), kind=MaxioWrite.KIND_CUSTOMER,
                                  user=self.user, outcome=MaxioWrite.DONE, provider_id=501,
                                  claimed_at=timezone.now())
        self.transport.expect("GET", "/subscriptions/7777.json",
                              json_response(200, subscription(7777, "active", "other", customer_id=999)))
        self.assertEqual(self.client.get("/api/subscriptions/7777").status_code, 404)
