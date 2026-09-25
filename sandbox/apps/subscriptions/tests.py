"""
Tests for the subscription billing API.

The Maxio client is real; only its transport is faked (the SDK's HttpClient protocol), so the SDK's
own request building, decoding and error mapping run exactly as in production.

Run: cd sandbox && ../venv/Scripts/python manage.py test apps.subscriptions
"""
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import HttpRequest, HttpResponse, JsonBody

from . import maxio
from .billing import status_from_provider
from .models import MaxioClaim

Handler = Callable[[HttpRequest], HttpResponse]


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(status_code=status, headers={"content-type": "application/json"},
                        content=json.dumps(body).encode())


def empty_response(status: int) -> HttpResponse:
    return HttpResponse(status_code=status, headers={})


class RoutingTransport:
    """Satisfies the SDK's sync transport protocol. Routes by (method, path) to queued handlers."""

    def __init__(self) -> None:
        self.requests: list[HttpRequest] = []
        self.routes: dict[tuple[str, str], list[Handler | HttpResponse | Exception]] = {}

    def on(self, method: str, path: str, *answers: Handler | HttpResponse | Exception) -> None:
        self.routes.setdefault((method, path), []).extend(answers)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = httpx.URL(request.url).path
        queue = self.routes.get((request.method, path))
        if not queue:
            raise AssertionError(f"unexpected request {request.method} {request.url}")
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, HttpResponse):
            return answer
        return answer(request)

    def close(self) -> None:
        pass

    def calls(self, method: str, path: str) -> list[HttpRequest]:
        return [r for r in self.requests if r.method == method and httpx.URL(r.url).path == path]


PLANS_PATH = "/product_families/handle:eshop-subscribe/products.json"
CUSTOMERS_PATH = "/customers.json"
CUSTOMER_LOOKUP_PATH = "/customers/lookup.json"
SUBSCRIPTIONS_PATH = "/subscriptions.json"
SUBSCRIPTION_LOOKUP_PATH = "/subscriptions/lookup.json"

PLANS_BODY = [
    {"product": {"id": 11, "handle": "eshop-pro", "name": "Pro Plan", "price_in_cents": 29900,
                 "interval": 1, "interval_unit": "month"}},
    {"product": {"id": 12, "handle": "basic-plan", "name": "Basic Plan", "price_in_cents": 2900,
                 "interval": 1, "interval_unit": "month"}},
]


def body_of(request: HttpRequest) -> Any:
    assert isinstance(request.body, JsonBody)
    return request.body.value


def customer_created(request: HttpRequest) -> HttpResponse:
    sent = body_of(request)["customer"]
    return json_response(201, {"customer": {"id": 501, "reference": sent["reference"], "email": sent["email"],
                                            "created_at": "2026-09-25T10:00:00Z"}})


def subscription_body(sub_id: int, reference: str, state: str = "active", plan: str = "eshop-pro") -> dict[str, Any]:
    return {"subscription": {
        "id": sub_id, "state": state, "reference": reference, "product_price_in_cents": 29900, "currency": "USD",
        "current_period_ends_at": "2026-10-25T10:00:00Z", "next_assessment_at": "2026-10-25T10:00:00Z",
        "created_at": "2026-09-25T10:00:00Z",
        "product": {"id": 11, "handle": plan, "name": "Pro Plan", "interval": 1, "interval_unit": "month"},
    }}


def subscription_created(state: str = "active", sub_id: int = 9001, plan: str = "eshop-pro") -> Handler:
    def handler(request: HttpRequest) -> HttpResponse:
        return json_response(201, subscription_body(sub_id, body_of(request)["subscription"]["reference"], state,
                                                    plan))
    return handler


def lookup_by_reference(sub_id: int = 9001, state: str = "active") -> Handler:
    def handler(request: HttpRequest) -> HttpResponse:
        return json_response(200, subscription_body(sub_id, httpx.URL(request.url).params["reference"], state))
    return handler


REFERENCE_TAKEN = json_response(422, {"errors": ["Reference: must be unique - that value has been taken."]})


@override_settings(MAXIO_REFERENCE_PREFIX="test", MAXIO_DEFAULT_PRODUCT_FAMILY="eshop-subscribe",
                   MAXIO_API_KEY="unit-test-key", MAXIO_SITE_SUBDOMAIN="unit-test-site", MAXIO_BASE_URL="")
class SubscriptionApiTestCase(TestCase):

    def setUp(self) -> None:
        self.transport = RoutingTransport()
        self.transport.on("GET", PLANS_PATH, json_response(200, PLANS_BODY))
        self.transport.on("POST", CUSTOMERS_PATH, customer_created)
        maxio.set_client(MaxioAdvancedBillingClient(
            environment="us", custom_http_client=self.transport,
            server_config={"production": {"us": {"site": "unit-test-site"}}},
            basic_auth={"username": "unit-test-key", "password": "x"}))
        self.user = get_user_model().objects.create_user(
            username="shopper", email="shopper@example.com", password="correct-horse-battery", first_name="Sam",
            last_name="Shopper")
        self.client.force_login(self.user)

    def tearDown(self) -> None:
        maxio.set_client(None)

    def subscribe(self, plan: str = "eshop-pro", **headers: str) -> Any:
        return self.client.post("/api/subscriptions", data=json.dumps({"planHandle": plan}),
                                content_type="application/json", headers=headers)

    # --- plans ---------------------------------------------------------------------------------

    def test_plans_carry_plan_handle(self) -> None:
        response = self.client.get("/api/subscription-plans")
        self.assertEqual(response.status_code, 200)
        plans = response.json()["plans"]
        self.assertEqual([p["planHandle"] for p in plans], ["eshop-pro", "basic-plan"])
        self.assertEqual(plans[0]["price"], "299.00")
        request = self.transport.requests[0]
        self.assertTrue(request.url.startswith("https://unit-test-site.chargify.com/"))
        self.assertTrue(request.headers["authorization"].startswith("Basic "))

    def test_unknown_family_is_a_configuration_failure_not_an_empty_list(self) -> None:
        self.transport.routes[("GET", PLANS_PATH)] = [empty_response(404)]   # Maxio's real answer: no body
        response = self.client.get("/api/subscription-plans")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "plan_family_unavailable")

    def test_anonymous_caller_is_refused(self) -> None:
        self.client.logout()
        self.assertEqual(self.client.get("/api/subscription-plans").status_code, 401)
        self.assertEqual(self.subscribe().status_code, 401)
        self.assertEqual(self.client.get("/api/my-subscriptions").status_code, 401)

    # --- subscribe: the hero flow --------------------------------------------------------------

    def test_subscribe_creates_customer_then_subscription(self) -> None:
        self.transport.on("POST", SUBSCRIPTIONS_PATH, subscription_created())
        response = self.subscribe()

        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["subscriptionId"], 9001)
        self.assertEqual(data["outcome"], "done")
        self.assertEqual(data["subscription"]["state"], "active")
        self.assertEqual(data["subscription"]["price"], "299.00")
        self.assertEqual(data["subscription"]["nextBillingAt"], "2026-10-25T10:00:00+00:00")

        customer_body = body_of(self.transport.calls("POST", CUSTOMERS_PATH)[0])["customer"]
        self.assertEqual(customer_body["reference"], f"test:u{self.user.pk}:customer")
        self.assertEqual(customer_body["email"], "shopper@example.com")
        sub_body = body_of(self.transport.calls("POST", SUBSCRIPTIONS_PATH)[0])["subscription"]
        self.assertEqual(sub_body, {
            "product_handle": "eshop-pro",
            "customer_reference": f"test:u{self.user.pk}:customer",
            "reference": f"test:u{self.user.pk}:sub:eshop-pro:1",
            "payment_collection_method": "remittance",
        })
        claim = MaxioClaim.objects.get(kind=MaxioClaim.KIND_SUBSCRIPTION)
        self.assertEqual((claim.outcome, claim.provider_id, claim.provider_state), ("done", 9001, "active"))

    def test_the_same_subscribe_twice_makes_one_create(self) -> None:
        self.transport.on("POST", SUBSCRIPTIONS_PATH, subscription_created())
        self.transport.on("GET", "/subscriptions/9001.json",
                          json_response(200, subscription_body(9001, f"test:u{self.user.pk}:sub:eshop-pro:1")))
        first = self.subscribe()
        second = self.subscribe()

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["subscriptionId"], first.json()["subscriptionId"])
        self.assertTrue(second.json()["repeat"])
        self.assertEqual(len(self.transport.calls("POST", SUBSCRIPTIONS_PATH)), 1)
        self.assertEqual(len(self.transport.calls("POST", CUSTOMERS_PATH)), 1)

    def test_request_in_flight_is_answered_without_a_provider_call(self) -> None:
        MaxioClaim.objects.create(reference="test:u%d:customer" % self.user.pk, kind="customer", user=self.user,
                                  outcome="done", provider_id=501, claimed_at=timezone.now())
        MaxioClaim.objects.create(reference="test:u%d:sub:eshop-pro:1" % self.user.pk, kind="subscription",
                                  user=self.user, plan_handle="eshop-pro", outcome="sending",
                                  claimed_at=timezone.now())
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["outcome"], "sending")
        self.assertIsNone(response.json()["subscriptionId"])
        self.assertEqual(self.transport.calls("POST", SUBSCRIPTIONS_PATH), [])

    def test_idempotency_key_same_key_is_one_subscription_new_key_is_another(self) -> None:
        self.transport.on("POST", SUBSCRIPTIONS_PATH, subscription_created(sub_id=1), subscription_created(sub_id=2))
        self.transport.on("GET", "/subscriptions/1.json",
                          json_response(200, subscription_body(1, f"test:u{self.user.pk}:sub:key:k-1")))
        a = self.subscribe(**{"Idempotency-Key": "k-1"})
        b = self.subscribe(**{"Idempotency-Key": "k-1"})
        c = self.subscribe(**{"Idempotency-Key": "k-2"})
        self.assertEqual((a.status_code, b.status_code, c.status_code), (201, 200, 201))
        self.assertEqual((a.json()["subscriptionId"], b.json()["subscriptionId"], c.json()["subscriptionId"]),
                         (1, 1, 2))
        refs = [body_of(r)["subscription"]["reference"] for r in self.transport.calls("POST", SUBSCRIPTIONS_PATH)]
        self.assertEqual(refs, [f"test:u{self.user.pk}:sub:key:k-1", f"test:u{self.user.pk}:sub:key:k-2"])

    def test_ended_subscription_lets_the_user_subscribe_again(self) -> None:
        self.transport.on("POST", SUBSCRIPTIONS_PATH, subscription_created(sub_id=1), subscription_created(sub_id=2))
        self.transport.on("GET", "/subscriptions/1.json", json_response(
            200, subscription_body(1, f"test:u{self.user.pk}:sub:eshop-pro:1", state="canceled")))
        self.assertEqual(self.subscribe().status_code, 201)
        again = self.subscribe()
        self.assertEqual(again.status_code, 201)
        self.assertEqual(again.json()["subscriptionId"], 2)
        self.assertEqual(again.json()["reference"], f"test:u{self.user.pk}:sub:eshop-pro:2")

    # --- status is read, not the id ------------------------------------------------------------

    def test_an_id_with_a_not_yet_state_is_accepted_not_created(self) -> None:
        self.transport.on("POST", SUBSCRIPTIONS_PATH, subscription_created(state="past_due"))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual((response.json()["subscriptionId"], response.json()["outcome"]), (9001, "pending"))

    def test_an_unlisted_state_is_unknown_not_done(self) -> None:
        self.transport.on("POST", SUBSCRIPTIONS_PATH, subscription_created(state="something_new"))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["outcome"], "unknown")

    def test_failed_to_create_is_not_success(self) -> None:
        self.transport.on("POST", SUBSCRIPTIONS_PATH, subscription_created(state="failed_to_create"))
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["outcome"], "failed")

    def test_status_mapping_covers_every_group(self) -> None:
        self.assertEqual(status_from_provider("active"), "done")      # open enum: plain str compares equal
        for state in ("pending", "assessing", "awaiting_signup", "past_due", "soft_failure", "unpaid", "paused",
                      "on_hold", "suspended"):
            self.assertEqual(status_from_provider(state), "pending", state)
        for state in ("failed_to_create", "canceled", "expired", "trial_ended"):
            self.assertEqual(status_from_provider(state), "failed", state)
        self.assertEqual(status_from_provider(None), "unknown")
        self.assertEqual(status_from_provider("brand_new"), "unknown")

    # --- the four failure kinds of a write -----------------------------------------------------

    def test_refused_connection_is_known_and_releases_the_claim(self) -> None:
        self.transport.on("POST", SUBSCRIPTIONS_PATH, httpx.ConnectError("refused"))
        response = self.subscribe()
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()["error"]["outcomeUnknown"])
        self.assertFalse(MaxioClaim.objects.filter(kind=MaxioClaim.KIND_SUBSCRIPTION).exists())
        self.assertEqual(self.transport.calls("GET", SUBSCRIPTION_LOOKUP_PATH), [])

    def test_read_timeout_not_found_is_unknown_and_kept(self) -> None:
        self.transport.on("POST", SUBSCRIPTIONS_PATH, httpx.ReadTimeout("no reply"))
        self.transport.on("GET", SUBSCRIPTION_LOOKUP_PATH, empty_response(404))
        response = self.subscribe()
        self.assertEqual(response.status_code, 504)
        self.assertTrue(response.json()["error"]["outcomeUnknown"])
        reference = f"test:u{self.user.pk}:sub:eshop-pro:1"
        self.assertEqual(response.json()["error"]["reference"], reference)
        self.assertEqual(MaxioClaim.objects.get(reference=reference).outcome, "unknown")
        lookup = self.transport.calls("GET", SUBSCRIPTION_LOOKUP_PATH)[0]
        self.assertEqual(httpx.URL(lookup.url).params["reference"], reference)

    def test_read_timeout_that_landed_is_found_by_reference(self) -> None:
        self.transport.on("POST", SUBSCRIPTIONS_PATH, httpx.ReadTimeout("no reply"))
        self.transport.on("GET", SUBSCRIPTION_LOOKUP_PATH, lookup_by_reference())
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["subscriptionId"], 9001)

    def test_unknown_is_settled_by_a_same_reference_resend(self) -> None:
        self.transport.on("POST", SUBSCRIPTIONS_PATH, httpx.ReadTimeout("no reply"), REFERENCE_TAKEN)
        self.transport.on("GET", SUBSCRIPTION_LOOKUP_PATH, empty_response(404), lookup_by_reference())
        self.assertEqual(self.subscribe().status_code, 504)
        retry = self.subscribe()
        self.assertEqual(retry.status_code, 201)
        refs = {body_of(r)["subscription"]["reference"] for r in self.transport.calls("POST", SUBSCRIPTIONS_PATH)}
        self.assertEqual(refs, {f"test:u{self.user.pk}:sub:eshop-pro:1"})   # never a new reference

    def test_provider_5xx_on_create_is_looked_up(self) -> None:
        self.transport.on("POST", SUBSCRIPTIONS_PATH, json_response(502, {"errors": ["bad gateway"]}))
        self.transport.on("GET", SUBSCRIPTION_LOOKUP_PATH, lookup_by_reference())
        self.assertEqual(self.subscribe().status_code, 201)

    def test_validation_rejection_is_the_callers_422_and_releases_the_claim(self) -> None:
        self.transport.on("POST", SUBSCRIPTIONS_PATH,
                          json_response(422, {"errors": ["No payment method was on file for the $29.00 balance"]}))
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["providerErrors"],
                         ["No payment method was on file for the $29.00 balance"])
        self.assertFalse(MaxioClaim.objects.filter(kind=MaxioClaim.KIND_SUBSCRIPTION).exists())

    def test_our_credentials_refused_is_a_502_not_the_callers_401(self) -> None:
        self.transport.routes[("POST", CUSTOMERS_PATH)] = [empty_response(401)]
        response = self.subscribe()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "billing_provider_config")

    def test_echo_mismatch_needs_review(self) -> None:
        self.transport.on("POST", SUBSCRIPTIONS_PATH, subscription_created(plan="basic-plan"))
        response = self.subscribe()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "needs_review")
        self.assertEqual(MaxioClaim.objects.get(kind=MaxioClaim.KIND_SUBSCRIPTION).outcome, "needs_review")

    def test_stale_sending_claim_is_checked_not_recreated_blind(self) -> None:
        reference = f"test:u{self.user.pk}:sub:eshop-pro:1"
        MaxioClaim.objects.create(reference=f"test:u{self.user.pk}:customer", kind="customer", user=self.user,
                                  outcome="done", provider_id=501, claimed_at=timezone.now())
        MaxioClaim.objects.create(reference=reference, kind="subscription", user=self.user, plan_handle="eshop-pro",
                                  outcome="sending", claimed_at=timezone.now() - timedelta(minutes=5))
        self.transport.on("POST", SUBSCRIPTIONS_PATH, REFERENCE_TAKEN)
        self.transport.on("GET", SUBSCRIPTION_LOOKUP_PATH, lookup_by_reference())
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(MaxioClaim.objects.get(reference=reference).outcome, "done")

    # --- customer ------------------------------------------------------------------------------

    def test_customer_already_at_maxio_is_linked_not_duplicated(self) -> None:
        self.transport.routes[("POST", CUSTOMERS_PATH)] = [REFERENCE_TAKEN]
        self.transport.on("GET", CUSTOMER_LOOKUP_PATH, json_response(200, {"customer": {
            "id": 777, "reference": f"test:u{self.user.pk}:customer"}}))
        response = self.client.post("/api/billing-customer")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["customerId"], 777)
        again = self.client.post("/api/billing-customer")
        self.assertEqual(again.json()["customerId"], 777)
        self.assertEqual(len(self.transport.calls("POST", CUSTOMERS_PATH)), 1)

    def test_user_without_email_cannot_subscribe(self) -> None:
        self.user.email = ""
        self.user.save()
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "email_required")

    def test_unknown_plan_is_404(self) -> None:
        response = self.subscribe(plan="no-such-plan")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.transport.calls("POST", CUSTOMERS_PATH), [])

    # --- account view ----------------------------------------------------------------------------

    def test_my_subscriptions_reads_back_from_maxio(self) -> None:
        self.assertEqual(self.client.get("/api/my-subscriptions").json()["subscriptions"], [])
        self.transport.on("POST", SUBSCRIPTIONS_PATH, subscription_created())
        self.subscribe()
        self.transport.on("GET", "/customers/501/subscriptions.json", json_response(200, [
            subscription_body(9001, f"test:u{self.user.pk}:sub:eshop-pro:1")]))
        data = self.client.get("/api/my-subscriptions").json()
        self.assertEqual([(s["subscriptionId"], s["planHandle"], s["state"]) for s in data["subscriptions"]],
                         [(9001, "eshop-pro", "active")])
        self.assertEqual(data["unsettledRequests"], [])


class SessionApiTestCase(TestCase):

    def test_login_with_email_and_logout(self) -> None:
        get_user_model().objects.create_user(username="u1", email="u1@example.com", password="correct-horse-1")
        client = self.client_class(enforce_csrf_checks=True)
        token = client.get("/api/session").json()["csrfToken"]
        credentials = json.dumps({"email": "u1@example.com", "password": "correct-horse-1"})
        response = client.post("/api/session", data=credentials, content_type="application/json",
                               headers={"X-CSRFToken": token})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(client.get("/api/session").json()["authenticated"])
        token = client.cookies["csrftoken"].value
        self.assertEqual(client.delete("/api/session", headers={"X-CSRFToken": token}).status_code, 204)
        self.assertFalse(client.get("/api/session").json()["authenticated"])

    def test_bad_password_is_401(self) -> None:
        get_user_model().objects.create_user(username="u2", email="u2@example.com", password="correct-horse-2")
        response = self.client.post("/api/session", data=json.dumps({"username": "u2", "password": "nope"}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 401)
