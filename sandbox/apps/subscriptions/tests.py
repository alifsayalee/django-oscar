"""
Tests for the subscriptions API. Maxio is faked at the SDK's transport seam,
so the real request-building and decoding pipeline runs on every call.

Run with:  sandbox/manage.py test apps.subscriptions
"""

import json
from datetime import timedelta

import httpx
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from maxio_advanced_billing.core import HttpRequest, HttpResponse

from . import gateway
from .models import BillingCustomer, SubscriptionEnrollment

User = get_user_model()

MAXIO_SETTINGS = {
    "MAXIO_API_KEY": "test-key",
    "MAXIO_SITE_SUBDOMAIN": "example-site",
    "MAXIO_DEFAULT_PRODUCT_FAMILY": "eshop-subscribe",
    "MAXIO_BASE_URL": "",
    "MAXIO_ENVIRONMENT": "us",
}


def json_response(status, body):
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


class StubTransport:
    """Answers queued responses in order; a queued exception is raised; a callable gets the request."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def queue(self, *responses):
        self._responses.extend(responses)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError(f"unexpected request {request.method} {request.url}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(request) if callable(item) else item

    def close(self):
        pass

    def calls(self, method, path_part):
        return [r for r in self.requests if r.method == method and path_part in r.url]


def product(handle, price, **extra):
    return {"product": {
        "id": abs(hash(handle)) % 10000, "handle": handle, "name": handle.title(),
        "price_in_cents": price, "interval": 1, "interval_unit": "month", **extra,
    }}


PLANS = json_response(200, [
    product("eshop-pro", 29900),
    product("basic-plan", 2900),
    product("old-plan", 100, archived_at="2025-01-01T00:00:00Z"),
])


SITE = json_response(200, {"site": {"id": 1, "subdomain": "example-site", "relationship_invoicing_enabled": True}})


def echo_customer(customer_id=501):
    def respond(request):
        sent = request.body.value["customer"]
        return json_response(201, {"customer": {
            "id": customer_id, "reference": sent["reference"], "email": sent["email"],
        }})
    return respond


def subscription_body(sub_id=9001, state="active", handle="eshop-pro", price=29900,
                      customer_id=501, reference=None):
    return {"subscription": {
        "id": sub_id,
        "state": state,
        "product_price_in_cents": price,
        "product": {"handle": handle, "name": "Pro Plan", "interval": 1, "interval_unit": "month"},
        "customer": {"id": customer_id},
        "next_assessment_at": "2026-10-24T10:00:00-04:00",
        "current_period_ends_at": "2026-10-24T10:00:00-04:00",
        "created_at": "2026-09-24T10:00:00-04:00",
        "reference": reference,
    }}


def echo_subscription(**kwargs):
    def respond(request):
        sent = request.body.value["subscription"]
        return json_response(201, subscription_body(reference=sent["reference"], **kwargs))
    return respond


@override_settings(**MAXIO_SETTINGS)
class SubscriptionApiTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.transport = StubTransport()
        self.previous = gateway.use_client(gateway.build_client(transport=self.transport))
        self.user = User.objects.create_user("shopper", "shopper@example.com", "correct-horse-battery")
        self.client.force_login(self.user)

    def tearDown(self):
        gateway.use_client(self.previous)

    def subscribe(self, plan="eshop-pro"):
        return self.client.post(
            "/api/subscriptions", data=json.dumps({"planHandle": plan}), content_type="application/json",
        )


class PlansTests(SubscriptionApiTestCase):
    def test_lists_live_plans_with_plan_handles(self):
        self.transport.queue(PLANS)
        self.client.logout()  # the plan catalogue is public

        response = self.client.get("/api/subscription-plans")

        self.assertEqual(response.status_code, 200)
        plans = response.json()["plans"]
        self.assertEqual([p["planHandle"] for p in plans], ["eshop-pro", "basic-plan"])
        self.assertEqual(plans[0]["price"], "299.00")
        req = self.transport.requests[0]
        self.assertEqual(req.method, "GET")
        self.assertTrue(req.url.startswith("https://example-site.chargify.com/product_families/"))
        self.assertIn("eshop-subscribe", req.url)
        self.assertEqual(req.headers["authorization"][:6], "Basic ")

    def test_base_url_override_is_used_verbatim(self):
        with override_settings(MAXIO_BASE_URL="http://billing.test:9999"):
            transport = StubTransport(PLANS)
            gateway.use_client(gateway.build_client(transport=transport))
            self.client.get("/api/subscription-plans")
        self.assertTrue(transport.requests[0].url.startswith("http://billing.test:9999/product_families/"))

    def test_transient_read_failure_is_retried(self):
        self.transport.queue(httpx.ConnectError("refused"), PLANS)
        response = self.client.get("/api/subscription-plans")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.transport.requests), 2)

    def test_missing_configuration_answers_503(self):
        gateway.use_client(None)
        with override_settings(MAXIO_API_KEY=""):
            response = self.client.get("/api/subscription-plans")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "billing_not_configured")

    def test_unknown_environment_is_refused(self):
        gateway.use_client(None)
        with override_settings(MAXIO_ENVIRONMENT="moon"):
            response = self.client.get("/api/subscription-plans")
        self.assertEqual(response.status_code, 503)


class AuthTests(SubscriptionApiTestCase):
    def test_session_login_is_required(self):
        self.client.logout()
        self.assertEqual(self.subscribe().status_code, 401)
        self.assertEqual(self.client.get("/api/my-subscriptions").status_code, 401)
        self.assertEqual(self.client.post("/api/billing-customer").status_code, 401)
        self.assertEqual(self.transport.requests, [])


class SubscribeTests(SubscriptionApiTestCase):
    def test_subscribe_creates_customer_and_subscription(self):
        self.transport.queue(PLANS, echo_customer(), SITE, echo_subscription())

        response = self.subscribe()

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["subscriptionId"], 9001)
        self.assertEqual(body["planHandle"], "eshop-pro")
        self.assertEqual(body["price"], "299.00")
        self.assertEqual(body["state"], "active")
        self.assertEqual(body["status"], "done")
        self.assertEqual(body["nextBillingAt"], "2026-10-24T10:00:00-04:00")

        customer_req = self.transport.calls("POST", "/customers.json")[0]
        row = BillingCustomer.objects.get(user=self.user)
        self.assertEqual(customer_req.body.value["customer"]["reference"], row.reference)
        self.assertEqual(customer_req.body.value["customer"]["email"], "shopper@example.com")
        sub_req = self.transport.calls("POST", "/subscriptions.json")[0]
        sent = sub_req.body.value["subscription"]
        enrollment = SubscriptionEnrollment.objects.get(user=self.user)
        self.assertEqual(sent, {
            "product_handle": "eshop-pro", "customer_id": 501, "reference": enrollment.reference,
            # no card is captured, so subscriptions are billed by invoice
            "payment_collection_method": "remittance",
        })
        self.assertEqual(enrollment.status, SubscriptionEnrollment.DONE)
        self.assertEqual(enrollment.maxio_subscription_id, 9001)

    def test_legacy_statements_site_bills_by_invoice(self):
        legacy = json_response(200, {"site": {"id": 1, "relationship_invoicing_enabled": False}})
        self.transport.queue(PLANS, echo_customer(), legacy, echo_subscription())
        self.assertEqual(self.subscribe().status_code, 201)
        sent = self.transport.calls("POST", "/subscriptions.json")[0].body.value["subscription"]
        self.assertEqual(sent["payment_collection_method"], "invoice")

    def test_double_submit_does_not_create_twice(self):
        self.transport.queue(PLANS, echo_customer(), SITE, echo_subscription())
        self.assertEqual(self.subscribe().status_code, 201)
        enrollment = SubscriptionEnrollment.objects.get()
        self.transport.queue(json_response(200, subscription_body(reference=enrollment.reference)))

        again = self.subscribe()

        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["subscriptionId"], 9001)
        self.assertEqual(len(self.transport.calls("POST", "/subscriptions.json")), 1)
        self.assertEqual(len(self.transport.calls("POST", "/customers.json")), 1)
        self.assertEqual(SubscriptionEnrollment.objects.count(), 1)

    def test_request_in_flight_answers_409_without_calling_maxio(self):
        self.transport.queue(PLANS)
        customer = BillingCustomer.objects.create(
            user=self.user, reference="ref-c", maxio_customer_id=501, status=BillingCustomer.DONE)
        SubscriptionEnrollment.objects.create(
            user=self.user, billing_customer=customer, plan_handle="eshop-pro",
            expected_price_in_cents=29900, reference="ref-s", status=SubscriptionEnrollment.SENDING)

        response = self.subscribe()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.transport.requests), 1)  # only the plan list

    def test_stale_claim_is_reconciled_by_reference_not_recreated(self):
        self.transport.queue(PLANS)
        customer = BillingCustomer.objects.create(
            user=self.user, reference="ref-c", maxio_customer_id=501, status=BillingCustomer.DONE)
        row = SubscriptionEnrollment.objects.create(
            user=self.user, billing_customer=customer, plan_handle="eshop-pro",
            expected_price_in_cents=29900, reference="ref-s", status=SubscriptionEnrollment.SENDING)
        SubscriptionEnrollment.objects.filter(pk=row.pk).update(
            date_updated=timezone.now() - timedelta(minutes=5))
        self.transport.queue(json_response(200, subscription_body(reference="ref-s")))

        response = self.subscribe()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.transport.calls("POST", "/subscriptions.json"), [])
        self.assertIn("reference=ref-s", self.transport.calls("GET", "/subscriptions/lookup.json")[0].url)
        row.refresh_from_db()
        self.assertEqual(row.status, SubscriptionEnrollment.DONE)

    def test_unknown_plan_is_404_and_never_creates(self):
        self.transport.queue(PLANS)
        response = self.subscribe("no-such-plan")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.transport.calls("POST", "/customers.json"), [])

    def test_archived_plan_is_not_subscribable(self):
        self.transport.queue(PLANS)
        self.assertEqual(self.subscribe("old-plan").status_code, 404)

    def test_missing_plan_handle_is_400(self):
        response = self.client.post("/api/subscriptions", data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 400)


class SubscriptionOutcomeTests(SubscriptionApiTestCase):
    def setUp(self):
        super().setUp()
        self.transport.queue(PLANS, echo_customer(), SITE)

    def enrollment(self):
        return SubscriptionEnrollment.objects.get(user=self.user)

    def test_rejection_is_the_callers_and_releases_the_claim(self):
        self.transport.queue(
            json_response(422, {"errors": ["Customer is invalid"]}),
            HttpResponse(status_code=404, headers={}),  # lookup: nothing landed
        )
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["details"], ["Customer is invalid"])
        self.assertEqual(self.enrollment().status, SubscriptionEnrollment.FAILED)

    def test_rejection_for_a_reference_that_already_landed_keeps_it(self):
        def found(request):
            ref = request.url.split("reference=")[1]
            return json_response(200, subscription_body(reference=ref))
        self.transport.queue(json_response(422, {"errors": ["Reference has already been taken"]}), found)
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.enrollment().status, SubscriptionEnrollment.DONE)

    def test_refused_connection_is_known_and_timeout_is_unknown(self):
        self.transport.queue(httpx.ConnectError("refused"))
        unsent = self.subscribe()
        self.assertEqual(unsent.status_code, 502)
        self.assertEqual(self.enrollment().status, SubscriptionEnrollment.FAILED)
        self.assertEqual(self.transport.calls("GET", "/subscriptions/lookup.json"), [])

        self.transport.queue(PLANS, httpx.ReadTimeout("no reply"), HttpResponse(status_code=404, headers={}))
        cache.clear()
        unknown = self.subscribe()
        self.assertEqual(unknown.status_code, 504)
        self.assertNotEqual(unsent.status_code, unknown.status_code)
        latest = SubscriptionEnrollment.objects.exclude(status=SubscriptionEnrollment.FAILED).get()
        self.assertEqual(latest.status, SubscriptionEnrollment.UNKNOWN)
        lookups = self.transport.calls("GET", "/subscriptions/lookup.json")
        self.assertEqual(len(lookups), 1)
        self.assertIn(f"reference={latest.reference}", lookups[0].url)

    def test_timeout_that_landed_is_found_by_reference(self):
        def found(request):
            ref = request.url.split("reference=")[1]
            return json_response(200, subscription_body(reference=ref))
        self.transport.queue(httpx.ReadTimeout("no reply"), found)
        response = self.subscribe()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["subscriptionId"], 9001)

    def test_truncated_success_body_is_not_success(self):
        self.transport.queue(json_response(201, {}), HttpResponse(status_code=404, headers={}))
        response = self.subscribe()
        self.assertEqual(response.status_code, 504)
        self.assertEqual(self.enrollment().status, SubscriptionEnrollment.UNKNOWN)

    def test_problem_state_is_accepted_not_done(self):
        self.transport.queue(echo_subscription(state="past_due"))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "needs_attention")

    def test_unlisted_state_is_unknown_not_done(self):
        self.transport.queue(echo_subscription(state="something_new"))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.enrollment().status, SubscriptionEnrollment.UNKNOWN)

    def test_echoed_price_mismatch_needs_review(self):
        self.transport.queue(echo_subscription(price=100))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.enrollment().status, SubscriptionEnrollment.NEEDS_REVIEW)

    def test_provider_auth_failure_is_ours_not_the_callers(self):
        self.transport.queue(HttpResponse(status_code=401, headers={}, content=b"HTTP Basic: Access denied."))
        response = self.subscribe()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.enrollment().status, SubscriptionEnrollment.FAILED)


class CustomerTests(SubscriptionApiTestCase):
    def test_ensure_customer_is_idempotent(self):
        self.transport.queue(echo_customer())
        first = self.client.post("/api/billing-customer")
        second = self.client.post("/api/billing-customer")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.json()["customerId"], 501)
        self.assertEqual(len(self.transport.calls("POST", "/customers.json")), 1)

    def test_unknown_customer_outcome_is_resent_under_the_same_reference(self):
        self.transport.queue(httpx.ReadTimeout("no reply"), HttpResponse(status_code=404, headers={}))
        self.assertEqual(self.client.post("/api/billing-customer").status_code, 504)
        row = BillingCustomer.objects.get()
        self.assertEqual(row.status, BillingCustomer.UNKNOWN)

        self.transport.queue(HttpResponse(status_code=404, headers={}), echo_customer())
        self.assertEqual(self.client.post("/api/billing-customer").status_code, 200)
        refs = {r.body.value["customer"]["reference"] for r in self.transport.calls("POST", "/customers.json")}
        self.assertEqual(refs, {row.reference})

    def test_user_without_email_is_422(self):
        self.user.email = ""
        self.user.save()
        self.assertEqual(self.client.post("/api/billing-customer").status_code, 422)
        self.assertEqual(self.transport.requests, [])


class ReadTests(SubscriptionApiTestCase):
    def setUp(self):
        super().setUp()
        self.customer = BillingCustomer.objects.create(
            user=self.user, reference="ref-c", maxio_customer_id=501, status=BillingCustomer.DONE)

    def test_my_subscriptions_reads_live_state_and_settles_unknown_claims(self):
        row = SubscriptionEnrollment.objects.create(
            user=self.user, billing_customer=self.customer, plan_handle="eshop-pro",
            expected_price_in_cents=29900, reference="ref-s", status=SubscriptionEnrollment.UNKNOWN)
        self.transport.queue(json_response(200, [subscription_body(reference="ref-s")]))

        response = self.client.get("/api/my-subscriptions")

        self.assertEqual(response.status_code, 200)
        subs = response.json()["subscriptions"]
        self.assertEqual(subs[0]["subscriptionId"], 9001)
        self.assertEqual(subs[0]["status"], "done")
        self.assertEqual(response.json()["pending"], [])
        self.assertIn("/customers/501/subscriptions.json", self.transport.requests[0].url)
        row.refresh_from_db()
        self.assertEqual(row.status, SubscriptionEnrollment.DONE)

    def test_my_subscriptions_without_customer_is_empty(self):
        self.customer.delete()
        response = self.client.get("/api/my-subscriptions")
        self.assertEqual(response.json(), {"subscriptions": [], "pending": []})
        self.assertEqual(self.transport.requests, [])

    def test_cannot_read_someone_elses_subscription(self):
        self.transport.queue(json_response(200, subscription_body(customer_id=777)))
        self.assertEqual(self.client.get("/api/subscriptions/9001").status_code, 404)

    def test_read_own_subscription(self):
        self.transport.queue(json_response(200, subscription_body()))
        response = self.client.get("/api/subscriptions/9001")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["planHandle"], "eshop-pro")

    def test_provider_outage_on_read_is_not_reported_as_empty(self):
        self.transport.queue(*[json_response(503, {}) for _ in range(gateway.READ_ATTEMPTS)])
        response = self.client.get("/api/my-subscriptions")
        self.assertEqual(response.status_code, 502)
