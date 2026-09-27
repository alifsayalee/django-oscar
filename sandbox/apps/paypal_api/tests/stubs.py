"""A fake PayPal at the SDK's transport seam: routes requests to canned responses."""

import json
import re
from collections.abc import Callable
from typing import Any

from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpRequest, HttpResponse

from apps.paypal_api.gateway import LoggingTransport

Responder = HttpResponse | Exception | Callable[[HttpRequest], HttpResponse]


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(status_code=status, headers={"content-type": "application/json"},
                        content=json.dumps(body).encode())


def paypal_error(status: int, issue: str, description: str = "") -> HttpResponse:
    return json_response(status, {
        "name": "UNPROCESSABLE_ENTITY", "message": "The requested action could not be performed.",
        "debug_id": "dbg123", "details": [{"issue": issue, "description": description or issue}],
    })


class RouterTransport:
    """Answers the OAuth token request itself; everything else must match a route."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], list[Responder]]] = []
        self.requests: list[HttpRequest] = []
        self.token_response: HttpResponse = json_response(
            200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})

    def on(self, method: str, path: str, *responses: Responder) -> None:
        """Queue responses for a route; the last one repeats."""
        self.routes.append((method, re.compile(path + r"(\?|$)"), list(responses)))

    def send(self, request: HttpRequest) -> HttpResponse:
        if request.url.endswith("/v1/oauth2/token"):
            return self.token_response
        self.requests.append(request)
        for method, pattern, responses in reversed(self.routes):
            if method == request.method and pattern.search(request.url):
                responder = responses.pop(0) if len(responses) > 1 else responses[0]
                if isinstance(responder, Exception):
                    raise responder
                if callable(responder):
                    return responder(request)
                return responder
        raise AssertionError(f"Unexpected PayPal request {request.method} {request.url}")

    def close(self) -> None:
        pass

    def calls(self, method: str, path: str) -> list[HttpRequest]:
        pattern = re.compile(path + r"(\?|$)")
        return [r for r in self.requests if r.method == method and pattern.search(r.url)]


def client_over(transport: RouterTransport) -> PaypalClient:
    return PaypalClient(
        base_url="https://paypal.test",
        custom_http_client=LoggingTransport(transport),  # type: ignore[arg-type]
        oauth2=ClientCredentials(client_id="id", client_secret="secret"),
    )


def body_of(request: HttpRequest) -> Any:
    return getattr(request.body, "value", None)


# --- canned PayPal bodies (wire shape) ------------------------------------------------


def authorization(auth_id: str = "AUTH1", status: str = "CREATED", value: str = "20.00",
                  created: str = "2026-09-27T10:00:00Z", expires: str = "2026-10-26T10:00:00Z",
                  order_id: str = "PPORDER1") -> dict[str, Any]:
    return {"id": auth_id, "status": status, "amount": {"currency_code": "USD", "value": value},
            "create_time": created, "expiration_time": expires,
            "supplementary_data": {"related_ids": {"order_id": order_id}}}


def created_order(auth: dict[str, Any] | None = None, status: str = "COMPLETED",
                  order_id: str = "PPORDER1", value: str = "20.00") -> dict[str, Any]:
    unit: dict[str, Any] = {"amount": {"currency_code": "USD", "value": value}}
    if auth is not None:
        unit["payments"] = {"authorizations": [auth]}
    return {"id": order_id, "status": status, "purchase_units": [unit],
            "payment_source": {"card": {"brand": "VISA", "last_digits": "1111"}}}


def capture(capture_id: str = "CAP1", status: str = "COMPLETED", value: str = "20.00") -> dict[str, Any]:
    return {"id": capture_id, "status": status, "amount": {"currency_code": "USD", "value": value},
            "create_time": "2026-09-27T11:00:00Z",
            "seller_receivable_breakdown": {"gross_amount": {"currency_code": "USD", "value": value},
                                            "paypal_fee": {"currency_code": "USD", "value": "0.88"},
                                            "net_amount": {"currency_code": "USD", "value": "19.12"}}}


def refund(refund_id: str = "REF1", status: str = "COMPLETED", value: str = "5.00") -> dict[str, Any]:
    return {"id": refund_id, "status": status, "amount": {"currency_code": "USD", "value": value},
            "create_time": "2026-09-27T12:00:00Z"}


def token(token_id: str = "TOK1", customer_id: str = "CUST1") -> dict[str, Any]:
    return {"id": token_id, "customer": {"id": customer_id},
            "payment_source": {"card": {"brand": "VISA", "last_digits": "1111", "expiry": "2030-12"}}}
