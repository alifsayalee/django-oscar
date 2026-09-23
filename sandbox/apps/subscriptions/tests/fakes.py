"""Test doubles for the Maxio transport seam (``python-testing``).

The seam is the SDK's transport protocol (``send`` + ``close``), passed as ``custom_http_client`` —
we never patch the client's private attributes.  Maxio uses Basic auth, so there is no lazy token
fetch: the first request the transport sees is the operation itself.

``FakeMaxio`` is a small stateful router keyed on method+path, enough to drive the whole subscribe
flow (family resolution -> customer -> guard -> create) and its idempotent second run.
"""

import json
from urllib.parse import parse_qs, urlparse

from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import BasicAuthCredentials, HttpResponse

from .. import client as client_module

FAMILY_HANDLE = "eshop-subscribe"
FAMILY_ID = 3026728
PLANS = [
    {"id": 7130993, "handle": "eshop-pro", "name": "Pro Plan", "price_in_cents": 29900,
     "interval": 1, "interval_unit": "month"},
    {"id": 7130994, "handle": "basic-plan", "name": "Basic Plan", "price_in_cents": 2900,
     "interval": 1, "interval_unit": "month"},
]


def _json(status, body):
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


class FakeMaxio:
    """Routing, stateful fake transport."""

    def __init__(self, *, create_state="active", customer_exists=False):
        self.create_state = create_state
        self.customer_id = 555 if customer_exists else None
        self.subscriptions = []  # list of subscription dicts
        self.requests = []
        self._next_sub_id = 99001

    # -- transport protocol --
    def send(self, request):
        self.requests.append(request)
        parsed = urlparse(request.url)
        path = parsed.path
        qs = parse_qs(parsed.query)
        method = request.method.upper()

        if method == "GET" and path.endswith("/product_families.json"):
            return _json(200, [{"product_family": {
                "id": FAMILY_ID, "handle": FAMILY_HANDLE, "name": "eShopSubscribe"}}])

        if method == "GET" and path.endswith(f"/product_families/{FAMILY_ID}/products.json"):
            return _json(200, [{"product": p} for p in PLANS])

        if method == "GET" and path.endswith("/customers/lookup.json"):
            if self.customer_id is not None:
                return _json(200, {"customer": self._customer(qs.get("reference", [""])[0])})
            return _json(404, {"error": "not found"})

        if method == "POST" and path.endswith("/customers.json"):
            body = request.body.value["customer"]
            self.customer_id = 555
            return _json(201, {"customer": self._customer(body.get("reference"))})

        if method == "GET" and path.endswith("/subscriptions/lookup.json"):
            ref = qs.get("reference", [""])[0]
            for s in self.subscriptions:
                if s.get("reference") == ref:
                    return _json(200, {"subscription": s})
            return _json(404, {"error": "not found"})

        if method == "GET" and path.endswith(f"/customers/{self.customer_id}/subscriptions.json"):
            return _json(200, [{"subscription": s} for s in self.subscriptions])

        if method == "GET" and "/subscriptions/" in path and path.endswith(".json"):
            sub_id = int(path.rsplit("/", 1)[-1][:-len(".json")])
            for s in self.subscriptions:
                if s.get("id") == sub_id:
                    return _json(200, {"subscription": s})
            return _json(404, {"error": "not found"})

        if method == "POST" and path.endswith("/subscriptions.json"):
            body = request.body.value["subscription"]
            sub = self._make_subscription(body)
            self.subscriptions.append(sub)
            return _json(201, {"subscription": sub})

        raise AssertionError(f"unexpected request: {method} {path}")

    def close(self):
        pass

    # -- helpers --
    def _customer(self, reference):
        return {"id": self.customer_id, "reference": reference,
                "first_name": "Oscar", "last_name": "Shopper", "email": "s@example.com"}

    def _make_subscription(self, body):
        handle = body.get("product_handle")
        plan = next((p for p in PLANS if p["handle"] == handle), PLANS[0])
        sub_id = self._next_sub_id
        self._next_sub_id += 1
        return {
            "id": sub_id,
            "state": self.create_state,
            "reference": body.get("reference"),
            "current_period_ends_at": "2026-10-23T10:00:00-05:00",
            "next_assessment_at": "2026-10-23T10:00:00-05:00",
            "current_billing_amount_in_cents": plan["price_in_cents"],
            "product": {"id": plan["id"], "handle": plan["handle"], "name": plan["name"]},
            "customer": {"id": self.customer_id},
        }


class RaisingTransport:
    """Raises ``error`` on the first send (Maxio has no token pre-fetch to answer)."""

    def __init__(self, error):
        self._error = error
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        raise self._error

    def close(self):
        pass


class QueueTransport:
    """Returns queued responses in order."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return self._responses.pop(0)

    def close(self):
        pass


def install(transport):
    """Point the module-scoped client at ``transport`` and return the transport."""
    client_module.reset_client()
    client_module._client = MaxioAdvancedBillingClient(
        custom_http_client=transport,
        basic_auth=BasicAuthCredentials(username="test-key", password="x"),
        environment="us",
        server_config={"production": {"us": {"site": "test-site"}}},
    )
    return transport


def json_response(status, body):
    return _json(status, body)
