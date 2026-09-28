"""A routing stub for the SDK's transport seam: the real SDK builds and parses every request, no network."""

import json
import re
from datetime import datetime, timedelta, timezone

from paypal.core import HttpRequest, HttpResponse

from apps.payments_api.paypal_gateway.client import PayPalConfig, build_client


def json_response(status, body):
    return HttpResponse(status_code=status, headers={"content-type": "application/json"},
                        content=json.dumps(body).encode())


def token_response():
    return json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})


class RoutingTransport:
    """Answers each (method, path regex) route from its own queue, in order; the last answer repeats. An
    Exception in a queue is raised instead of answering. Every request is recorded (the token fetch too)."""

    def __init__(self):
        self.routes = []
        self.requests = []
        self.on("POST", r"/v1/oauth2/token$", token_response())

    def on(self, method, pattern, *answers):
        for route in self.routes:
            if route[0] == method and route[1].pattern == pattern:
                route[2].extend(answers)
                return self
        self.routes.append((method, re.compile(pattern), list(answers)))
        return self

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = request.url.split("?")[0]
        for method, pattern, answers in self.routes:
            if method == request.method and pattern.search(path):
                if not answers:
                    raise AssertionError(f"no answer queued for {request.method} {path}")
                # The last queued answer repeats, the way PayPal repeats its answer for a request id.
                answer = answers.pop(0) if len(answers) > 1 else answers[0]
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"unexpected request {request.method} {path}")

    def close(self):
        pass

    def calls(self, method, pattern):
        rx = re.compile(pattern)
        return [r for r in self.requests if r.method == method and rx.search(r.url.split("?")[0])]


def stub_client(transport):
    return build_client(PayPalConfig(client_id="test-id", client_secret="test-secret", environment="sandbox",
                                     currency="USD", base_url=None), transport=transport)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def authorization(auth_id="AUTH1", status="CREATED", value="20.00", currency="USD", created=None):
    created = created or now()
    return {"id": auth_id, "status": status, "amount": {"currency_code": currency, "value": value},
            "create_time": iso(created), "expiration_time": iso(created + timedelta(days=29))}


def paypal_order(auth=None, status="COMPLETED", order_id="PPORDER1", value="20.00"):
    unit = {"reference_id": "x", "amount": {"currency_code": "USD", "value": value}}
    if auth is not None:
        unit["payments"] = {"authorizations": [auth]}
    return {"id": order_id, "status": status, "create_time": iso(now()),
            "payment_source": {"card": {"last_digits": "1111", "brand": "VISA"}},
            "purchase_units": [unit]}


def capture(capture_id="CAP1", status="COMPLETED", value="20.00", fee="0.88", net="19.12"):
    return {"id": capture_id, "status": status, "amount": {"currency_code": "USD", "value": value},
            "final_capture": True, "create_time": iso(now()),
            "seller_receivable_breakdown": {"gross_amount": {"currency_code": "USD", "value": value},
                                            "paypal_fee": {"currency_code": "USD", "value": fee},
                                            "net_amount": {"currency_code": "USD", "value": net}}}


def refund(refund_id="REF1", status="COMPLETED", value="5.00"):
    return {"id": refund_id, "status": status, "amount": {"currency_code": "USD", "value": value},
            "create_time": iso(now())}


def vault_token(token_id="TOK1", customer_id="CUST1"):
    return {"id": token_id, "customer": {"id": customer_id},
            "payment_source": {"card": {"last_digits": "1111", "brand": "VISA", "expiry": "2030-12"}}}


def paypal_error(status, issue, name="UNPROCESSABLE_ENTITY"):
    return json_response(status, {"name": name, "message": "The requested action could not be performed.",
                                  "debug_id": "dbg1", "details": [{"issue": issue, "description": issue.lower()}]})
