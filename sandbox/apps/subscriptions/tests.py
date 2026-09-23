"""
Tests for the Maxio subscription billing API.

The SDK client is real; only its transport is faked (``custom_http_client``),
so every test exercises the real request building and response decoding.

Run from sandbox/:  python manage.py test apps.subscriptions
"""

import json
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from maxio_advanced_billing.core import HttpRequest, HttpResponse

from apps.subscriptions import gateway, maxio
from apps.subscriptions.models import BillingCustomer, ClaimStatus, SubscriptionEnrollment

FAMILY = "test-family"
MAXIO_SETTINGS = {
    "MAXIO_API_KEY": "test-key-placeholder",
    "MAXIO_SITE_SUBDOMAIN": "test-site",
    "MAXIO_DEFAULT_PRODUCT_FAMILY": FAMILY,
    "MAXIO_BASE_URL": "",
    "MAXIO_ENVIRONMENT": "us",
    "MAXIO_REFERENCE_PREFIX": "test",
}


def json_response(status, body):
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def product(handle, price, archived=False, product_id=1):
    return {
        "product": {
            "id": product_id,
            "handle": handle,
            "name": handle.title(),
            "price_in_cents": price,
            "interval": 1,
            "interval_unit": "month",
            "require_credit_card": False,
            "archived_at": "2026-01-01T00:00:00Z" if archived else None,
            "product_family": {"id": 9, "handle": FAMILY},
        }
    }


def subscription(sub_id, reference, customer_id=500, handle="pro", price=29900, state="active"):
    return {
        "subscription": {
            "id": sub_id,
            "state": state,
            "reference": reference,
            "product_price_in_cents": price,
            "current_billing_amount_in_cents": price,
            "currency": "USD",
            "payment_collection_method": "remittance",
            "next_assessment_at": "2026-10-23T12:00:00Z",
            "created_at": "2026-09-23T12:00:00Z",
            "product": {"id": 1, "handle": handle, "name": "Pro", "interval": 1, "interval_unit": "month"},
            "customer": {"id": customer_id, "reference": "test-user-x"},
        }
    }


class FakeMaxio:
    """
    A transport satisfying the SDK's HttpClient protocol. Routes by method and
    path to handlers, records every request, and holds a tiny in-memory
    account so lookups see what creates made.
    """

    def __init__(self):
        self.requests = []
        self.products = [product("pro", 29900, product_id=1), product("basic", 2900, product_id=2),
                         product("old", 100, archived=True, product_id=3)]
        self.customers = {}  # reference -> body
        self.subscriptions = {}  # reference -> body
        self.next_id = 500
        # Optional overrides: (method, path) -> callable(request) -> HttpResponse | raises
        self.overrides = {}

    # -- protocol --
    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        parts = urlsplit(request.url)
        key = (request.method, unquote(parts.path))
        if key in self.overrides:
            handler = self.overrides[key]
            if isinstance(handler, list):
                handler = handler.pop(0) if len(handler) > 1 else handler[0]
            result = handler(request)
            if result is not None:
                return result
        return self._default(request, parts)

    def close(self):
        pass

    # -- helpers --
    def calls(self, method, path):
        return [r for r in self.requests
                if r.method == method and unquote(urlsplit(r.url).path) == path]

    def _default(self, request, parts):
        query = parse_qs(parts.query)
        parts = parts._replace(path=unquote(parts.path))
        if request.method == "GET" and parts.path == f"/product_families/handle:{FAMILY}/products.json":
            return json_response(200, self.products)
        if request.method == "POST" and parts.path == "/customers.json":
            body = request.body.value["customer"]
            if body["reference"] in self.customers:
                return json_response(422, {"errors": ["Reference: must be unique - that value has been taken."]})
            self.next_id += 1
            customer = {"customer": {"id": self.next_id, **body}}
            self.customers[body["reference"]] = customer
            return json_response(201, customer)
        if request.method == "GET" and parts.path == "/customers/lookup.json":
            found = self.customers.get(query["reference"][0])
            return json_response(200, found) if found else HttpResponse(status_code=404, headers={})
        if request.method == "POST" and parts.path == "/subscriptions.json":
            body = request.body.value["subscription"]
            self.next_id += 1
            price = next(p["product"]["price_in_cents"] for p in self.products
                         if p["product"]["handle"] == body["product_handle"])
            sub = subscription(self.next_id, body["reference"], body["customer_id"],
                               body["product_handle"], price)
            self.subscriptions[body["reference"]] = sub
            return json_response(201, sub)
        if request.method == "GET" and parts.path == "/subscriptions/lookup.json":
            found = self.subscriptions.get(query["reference"][0])
            return json_response(200, found) if found else HttpResponse(status_code=404, headers={})
        if request.method == "GET" and parts.path.startswith("/subscriptions/"):
            sub_id = int(parts.path.rsplit("/", 1)[1].split(".")[0])
            for sub in self.subscriptions.values():
                if sub["subscription"]["id"] == sub_id:
                    return json_response(200, sub)
            return HttpResponse(status_code=404, headers={})
        if request.method == "GET" and parts.path.startswith("/customers/") and parts.path.endswith("/subscriptions.json"):
            customer_id = int(parts.path.split("/")[2])
            return json_response(200, [s for s in self.subscriptions.values()
                                       if s["subscription"]["customer"]["id"] == customer_id])
        raise AssertionError(f"unexpected request {request.method} {request.url}")


def raising(exc):
    def handler(request):
        raise exc
    return handler


@override_settings(**MAXIO_SETTINGS)
class SubscriptionApiTestCase(TestCase):
    def setUp(self):
        self.fake = FakeMaxio()
        maxio.set_client(maxio.build_client(transport=self.fake))
        self.addCleanup(maxio.set_client, None)
        self.sleeps = []
        original_sleep = gateway._sleep
        gateway._sleep = self.sleeps.append
        self.addCleanup(setattr, gateway, "_sleep", original_sleep)
        self.user = get_user_model().objects.create_user(
            username="shopper", email="shopper@example.com", password="not-a-real-pass-123"
        )
        self.client.force_login(self.user)

    def subscribe(self, plan="pro"):
        return self.client.post("/api/subscriptions", data=json.dumps({"planHandle": plan}),
                                content_type="application/json")


class PlanListingTests(SubscriptionApiTestCase):
    def test_lists_plans_with_handles_and_skips_archived(self):
        response = self.client.get("/api/subscription-plans")
        self.assertEqual(response.status_code, 200)
        plans = response.json()["plans"]
        self.assertEqual([p["planHandle"] for p in plans], ["pro", "basic"])
        self.assertEqual(plans[0]["priceInCents"], 29900)
        self.assertEqual(plans[0]["price"], "299.00")
        self.assertEqual(plans[0]["intervalUnit"], "month")
        self.assertFalse(response.json()["truncated"])

    def test_request_reaches_the_configured_site_and_family_with_basic_auth(self):
        self.client.get("/api/subscription-plans")
        request = self.fake.requests[-1]
        self.assertTrue(request.url.startswith(
            f"https://test-site.chargify.com/product_families/handle%3A{FAMILY}/products.json"))
        self.assertIn("per_page=200", request.url)
        self.assertTrue(request.headers["authorization"].startswith("Basic "))

    @override_settings(MAXIO_BASE_URL="http://maxio.local:9999/")
    def test_base_url_override_is_used_verbatim(self):
        maxio.set_client(maxio.build_client(transport=self.fake))
        self.client.get("/api/subscription-plans")
        self.assertTrue(self.fake.requests[-1].url.startswith("http://maxio.local:9999/product_families/"))

    def test_anonymous_caller_gets_401(self):
        self.client.logout()
        self.assertEqual(self.client.get("/api/subscription-plans").status_code, 401)
        self.assertEqual(self.subscribe().status_code, 401)
        self.assertEqual(self.fake.requests, [])

    @override_settings(MAXIO_API_KEY="")
    def test_missing_credentials_are_a_configuration_error(self):
        maxio.set_client(None)
        response = self.client.get("/api/subscription-plans")
        self.assertEqual(response.status_code, 503)

    def test_transient_read_failure_is_retried(self):
        path = ("GET", f"/product_families/handle:{FAMILY}/products.json")
        self.fake.overrides[path] = [lambda r: json_response(503, {}), lambda r: None]
        response = self.client.get("/api/subscription-plans")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.fake.calls(*path)), 2)
        self.assertEqual(len(self.sleeps), 1)

    def test_provider_401_is_our_problem_not_the_callers(self):
        path = ("GET", f"/product_families/handle:{FAMILY}/products.json")
        self.fake.overrides[path] = lambda r: HttpResponse(status_code=401, headers={})
        response = self.client.get("/api/subscription-plans")
        self.assertEqual(response.status_code, 502)


class SubscribeTests(SubscriptionApiTestCase):
    def test_subscribe_creates_customer_and_subscription(self):
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIsInstance(body["subscriptionId"], int)
        self.assertEqual(body["status"], "active")
        self.assertEqual(body["subscription"]["planHandle"], "pro")
        self.assertEqual(body["subscription"]["price"], "299.00")
        self.assertEqual(body["subscription"]["state"], "active")
        self.assertEqual(body["subscription"]["nextBillingAt"], "2026-10-23T12:00:00+00:00")

        [customer_call] = self.fake.calls("POST", "/customers.json")
        sent = customer_call.body.value["customer"]
        self.assertEqual(sent["reference"], f"test-user-{self.user.pk}")
        self.assertEqual(sent["email"], "shopper@example.com")
        self.assertTrue(sent["first_name"] and sent["last_name"])

        [sub_call] = self.fake.calls("POST", "/subscriptions.json")
        sent = sub_call.body.value["subscription"]
        row = SubscriptionEnrollment.objects.get()
        self.assertEqual(sent, {"product_handle": "pro", "customer_id": 501, "reference": row.reference,
                                "payment_collection_method": "remittance"})
        self.assertEqual(row.maxio_subscription_id, body["subscriptionId"])
        self.assertEqual(row.status, ClaimStatus.ACTIVE)

    def test_double_click_returns_the_same_subscription(self):
        first = self.subscribe().json()
        second = self.subscribe()
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["subscriptionId"], first["subscriptionId"])
        self.assertFalse(second.json()["created"])
        self.assertEqual(len(self.fake.calls("POST", "/subscriptions.json")), 1)
        self.assertEqual(len(self.fake.calls("POST", "/customers.json")), 1)

    def test_in_flight_claim_is_not_sent_twice(self):
        customer = BillingCustomer.objects.create(
            user=self.user, reference=f"test-user-{self.user.pk}",
            maxio_customer_id=77, status=ClaimStatus.ACTIVE)
        SubscriptionEnrollment.objects.create(
            user=self.user, billing_customer=customer, plan_handle="pro",
            reference="test-sub-inflight", status=ClaimStatus.SENDING, expected_price_in_cents=29900)
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertIsNone(response.json()["subscriptionId"])
        self.assertEqual(self.fake.calls("POST", "/subscriptions.json"), [])

    def test_different_plan_is_a_separate_subscription(self):
        self.subscribe("pro")
        response = self.subscribe("basic")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(self.fake.calls("POST", "/subscriptions.json")), 2)

    def test_unknown_or_archived_plan_is_rejected_before_any_write(self):
        for plan in ("nope", "old"):
            response = self.subscribe(plan)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.fake.calls("POST", "/customers.json"), [])
        self.assertEqual(self.fake.calls("POST", "/subscriptions.json"), [])

    def test_missing_plan_handle_is_400(self):
        response = self.client.post("/api/subscriptions", data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_existing_customer_reference_is_adopted_not_duplicated(self):
        reference = f"test-user-{self.user.pk}"
        self.fake.customers[reference] = {"customer": {
            "id": 42, "reference": reference, "email": "shopper@example.com",
            "first_name": "S", "last_name": "H"}}
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(BillingCustomer.objects.get().maxio_customer_id, 42)
        self.assertEqual(self.fake.calls("POST", "/subscriptions.json")[0].body.value["subscription"]["customer_id"], 42)

    def test_customer_found_by_reference_with_other_email_needs_review(self):
        reference = f"test-user-{self.user.pk}"
        self.fake.customers[reference] = {"customer": {
            "id": 42, "reference": reference, "email": "someone-else@example.com"}}
        response = self.subscribe()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(BillingCustomer.objects.get().status, ClaimStatus.NEEDS_REVIEW)
        self.assertEqual(self.fake.calls("POST", "/subscriptions.json"), [])

    def test_provider_validation_error_is_passed_to_caller_and_releases_claim(self):
        self.fake.overrides[("POST", "/subscriptions.json")] = [
            lambda r: json_response(422, {"errors": ["Credit card: cannot be blank."]}),
            lambda r: None,
        ]
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["details"], ["Credit card: cannot be blank."])
        self.assertEqual(SubscriptionEnrollment.objects.get().status, ClaimStatus.FAILED)
        # The claim was released: trying again creates.
        self.assertEqual(self.subscribe().status_code, 201)

    def test_connection_refused_is_known_and_releases_claim(self):
        self.fake.overrides[("POST", "/subscriptions.json")] = raising(httpx.ConnectError("refused"))
        response = self.subscribe()
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()["error"]["outcomeUnknown"])
        self.assertEqual(SubscriptionEnrollment.objects.get().status, ClaimStatus.FAILED)
        self.assertEqual(self.fake.calls("GET", "/subscriptions/lookup.json"), [])

    def test_read_timeout_that_landed_is_reconciled_by_reference(self):
        def land_then_time_out(request):
            self.fake._default(request, urlsplit(request.url))
            raise httpx.ReadTimeout("no reply")
        self.fake.overrides[("POST", "/subscriptions.json")] = land_then_time_out
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        row = SubscriptionEnrollment.objects.get()
        self.assertEqual(row.status, ClaimStatus.ACTIVE)
        self.assertEqual(response.json()["subscriptionId"], row.maxio_subscription_id)
        [lookup] = self.fake.calls("GET", "/subscriptions/lookup.json")
        self.assertIn(f"reference={row.reference}", lookup.url)

    def test_read_timeout_not_found_is_unknown_and_never_resent(self):
        self.fake.overrides[("POST", "/subscriptions.json")] = raising(httpx.ReadTimeout("no reply"))
        response = self.subscribe()
        self.assertEqual(response.status_code, 504)
        self.assertTrue(response.json()["error"]["outcomeUnknown"])
        self.assertEqual(SubscriptionEnrollment.objects.get().status, ClaimStatus.UNKNOWN)
        # A retry looks the reference up again; it never creates a second one.
        retry = self.subscribe()
        self.assertEqual(retry.status_code, 202)
        self.assertEqual(retry.json()["status"], ClaimStatus.UNKNOWN)
        self.assertEqual(len(self.fake.calls("POST", "/subscriptions.json")), 1)
        self.assertEqual(len(self.fake.calls("GET", "/subscriptions/lookup.json")), 2)

    def test_unknown_and_refused_outcomes_are_distinguishable(self):
        self.fake.overrides[("POST", "/subscriptions.json")] = raising(httpx.ConnectError("refused"))
        unsent = self.subscribe().json()["error"]
        SubscriptionEnrollment.objects.all().delete()
        self.fake.overrides[("POST", "/subscriptions.json")] = raising(httpx.ReadTimeout("slow"))
        unknown = self.subscribe()
        self.assertEqual((502, False), (502, unsent["outcomeUnknown"]))
        self.assertEqual((unknown.status_code, unknown.json()["error"]["outcomeUnknown"]), (504, True))

    def test_truncated_success_body_is_reconciled_not_trusted(self):
        def empty_then_land(request):
            self.fake._default(request, urlsplit(request.url))
            return json_response(201, {})
        self.fake.overrides[("POST", "/subscriptions.json")] = empty_then_land
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(self.fake.calls("GET", "/subscriptions/lookup.json")), 1)

    def test_refused_state_with_an_id_is_not_success(self):
        self.fake.overrides[("POST", "/subscriptions.json")] = lambda r: json_response(
            201, subscription(900, r.body.value["subscription"]["reference"], 501, state="failed_to_create"))
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["status"], ClaimStatus.FAILED)
        self.assertEqual(SubscriptionEnrollment.objects.get().status, ClaimStatus.FAILED)

    def test_unlisted_state_is_unknown_not_done(self):
        self.fake.overrides[("POST", "/subscriptions.json")] = lambda r: json_response(
            201, subscription(901, r.body.value["subscription"]["reference"], 501, state="something_new"))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], ClaimStatus.UNKNOWN)

    def test_echoed_price_mismatch_needs_review(self):
        self.fake.overrides[("POST", "/subscriptions.json")] = lambda r: json_response(
            201, subscription(902, r.body.value["subscription"]["reference"], 501, price=100))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(SubscriptionEnrollment.objects.get().status, ClaimStatus.NEEDS_REVIEW)

    def test_user_without_email_cannot_subscribe(self):
        self.user.email = ""
        self.user.save()
        self.assertEqual(self.subscribe().status_code, 422)
        self.assertEqual(self.fake.calls("POST", "/customers.json"), [])


class BillingCustomerTests(SubscriptionApiTestCase):
    def test_ensure_customer_is_idempotent(self):
        first = self.client.post("/api/billing-customer")
        second = self.client.post("/api/billing-customer")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(len(self.fake.calls("POST", "/customers.json")), 1)

    def test_unknown_customer_create_is_resent_under_the_same_reference(self):
        self.fake.overrides[("POST", "/customers.json")] = [raising(httpx.ReadTimeout("slow")), lambda r: None]
        self.assertEqual(self.client.post("/api/billing-customer").status_code, 504)
        self.assertEqual(BillingCustomer.objects.get().status, ClaimStatus.UNKNOWN)
        response = self.client.post("/api/billing-customer")
        self.assertEqual(response.status_code, 200)
        refs = {c.body.value["customer"]["reference"] for c in self.fake.calls("POST", "/customers.json")}
        self.assertEqual(refs, {f"test-user-{self.user.pk}"})


class MySubscriptionsTests(SubscriptionApiTestCase):
    def test_lists_nothing_before_subscribing(self):
        response = self.client.get("/api/my-subscriptions")
        self.assertEqual(response.json(), {"subscriptions": [], "pendingEnrollments": []})
        self.assertEqual(self.fake.requests, [])

    def test_reads_subscriptions_back_from_maxio(self):
        sub_id = self.subscribe().json()["subscriptionId"]
        response = self.client.get("/api/my-subscriptions")
        self.assertEqual(response.status_code, 200)
        [sub] = response.json()["subscriptions"]
        self.assertEqual(sub["subscriptionId"], sub_id)
        self.assertEqual(sub["planHandle"], "pro")
        self.assertEqual(sub["state"], "active")
        self.assertEqual(self.fake.calls("GET", "/customers/501/subscriptions.json")[0].method, "GET")

    def test_cancellation_at_maxio_releases_the_plan_claim(self):
        self.subscribe()
        sub = next(iter(self.fake.subscriptions.values()))
        sub["subscription"]["state"] = "canceled"
        self.client.get("/api/my-subscriptions")
        self.assertEqual(SubscriptionEnrollment.objects.get().status, ClaimStatus.ENDED)
        self.assertEqual(self.subscribe().status_code, 201)

    def test_detail_is_only_visible_to_its_owner(self):
        sub_id = self.subscribe().json()["subscriptionId"]
        self.assertEqual(self.client.get(f"/api/subscriptions/{sub_id}").status_code, 200)
        other = get_user_model().objects.create_user(
            username="other", email="other@example.com", password="not-a-real-pass-123")
        self.client.force_login(other)
        self.client.post("/api/billing-customer")
        self.assertEqual(self.client.get(f"/api/subscriptions/{sub_id}").status_code, 404)
