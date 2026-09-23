"""
A fake PayPal behind the SDK's transport protocol.

The real ``PaypalClient`` builds and decodes every request, so tests exercise
the SDK's request pipeline; only the network is replaced. Responses are queued
per ``(METHOD, path-prefix)`` and every request is recorded.
"""

import json
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import httpx
from paypal.core import HttpRequest, HttpResponse

from apps.paypal_payments import gateway

TOKEN_PATH = "/v1/oauth2/token"


def json_response(status, body=None, headers=None):
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json", **(headers or {})},
        content=b"" if body is None else json.dumps(body).encode(),
    )


def token_response():
    return json_response(
        200, {"access_token": "test-token", "token_type": "Bearer", "expires_in": 3600}
    )


class FakePayPal:
    """Satisfies the sync transport protocol: ``send()`` + ``close()``."""

    def __init__(self):
        self.routes = defaultdict(deque)
        self.requests: list[HttpRequest] = []
        self.token = token_response()

    # -- setup
    def on(self, method, path, *responses):
        """Queue responses (``HttpResponse`` or an exception to raise) for a route."""
        self.routes[(method, path)].extend(responses)
        return self

    # -- protocol
    def send(self, request):
        self.requests.append(request)
        path = httpx.URL(request.url).path
        if path == TOKEN_PATH:
            if isinstance(self.token, Exception):
                raise self.token
            return self.token
        for (method, prefix), queue in self.routes.items():
            if method == request.method and path.startswith(prefix) and queue:
                item = queue.popleft()
                if isinstance(item, Exception):
                    raise item
                return item
        raise AssertionError(
            "Unexpected PayPal call: %s %s" % (request.method, request.url)
        )

    def close(self):
        pass

    # -- inspection
    def calls(self, method=None, path=None):
        out = []
        for r in self.requests:
            p = httpx.URL(r.url).path
            if p == TOKEN_PATH:
                continue
            if method and r.method != method:
                continue
            if path and not p.startswith(path):
                continue
            out.append(r)
        return out

    @staticmethod
    def body(request):
        return request.body.value if request.body is not None else None


def install(fake):
    gateway.set_client(gateway.build_client(transport=fake))
    return fake


def uninstall():
    gateway.set_client(None)


# ---------------------------------------------------------------- canned bodies


def ts(days_ago=0.0):
    """An RFC3339 timestamp ``days_ago`` before now, as PayPal would send it."""
    value = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return value.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def order_completed(
    order_id="PPORDER1",
    auth_id="AUTH1",
    amount="15.00",
    currency="USD",
    auth_status="CREATED",
    order_status="COMPLETED",
    create_time=None,
):
    return {
        "id": order_id,
        "status": order_status,
        "intent": "AUTHORIZE",
        "payment_source": {
            "card": {"last_digits": "1111", "brand": "VISA", "type": "CREDIT"}
        },
        "purchase_units": [
            {
                "reference_id": "x",
                "payments": {
                    "authorizations": [
                        {
                            "id": auth_id,
                            "status": auth_status,
                            "amount": {"currency_code": currency, "value": amount},
                            "create_time": create_time or ts(0),
                            "expiration_time": ts(-29),
                        }
                    ]
                },
            }
        ],
    }


def authorization(
    auth_id="AUTH1",
    status="CREATED",
    amount="15.00",
    currency="USD",
    create_time=None,
    expiration_time=None,
):
    return {
        "id": auth_id,
        "status": status,
        "amount": {"currency_code": currency, "value": amount},
        "create_time": create_time or ts(0),
        "expiration_time": expiration_time or ts(-29),
    }


def capture(
    capture_id="CAP1",
    status="COMPLETED",
    amount="15.00",
    fee="0.74",
    net="14.26",
    currency="USD",
):
    return {
        "id": capture_id,
        "status": status,
        "final_capture": True,
        "amount": {"currency_code": currency, "value": amount},
        "seller_receivable_breakdown": {
            "gross_amount": {"currency_code": currency, "value": amount},
            "paypal_fee": {"currency_code": currency, "value": fee},
            "net_amount": {"currency_code": currency, "value": net},
        },
        "create_time": ts(0),
    }


def refund(refund_id="REF1", status="COMPLETED", amount="5.00", currency="USD"):
    return {
        "id": refund_id,
        "status": status,
        "amount": {"currency_code": currency, "value": amount},
        "create_time": ts(0),
    }


def payment_token(token_id="TOKEN1", customer_id="CUST1", verification_status=None):
    card = {
        "last_digits": "1111",
        "brand": "VISA",
        "expiry": "2030-12",
        "name": "Alice Shopper",
    }
    if verification_status:
        card["verification_status"] = verification_status
    return {
        "id": token_id,
        "customer": {"id": customer_id},
        "payment_source": {"card": card},
    }


def paypal_error(
    name="UNPROCESSABLE_ENTITY",
    message="The requested action could not be performed.",
    issue="X",
):
    return {
        "name": name,
        "message": message,
        "debug_id": "dbg123",
        "details": [{"issue": issue, "description": "desc of %s" % issue}],
    }
