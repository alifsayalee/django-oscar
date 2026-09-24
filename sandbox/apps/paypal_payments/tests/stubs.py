"""A stub transport at the SDK's transport seam, and PayPal response bodies.

The real ``PaypalClient`` runs against it, so every request the tests assert
on was built by the SDK itself."""
import json
from typing import Any
from urllib.parse import urlsplit

from paypal import PaypalClient
from paypal.core import HttpRequest, HttpResponse

from apps.paypal_payments import gateway

Reply = HttpResponse | BaseException


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def token_response() -> HttpResponse:
    return json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})


class StubTransport:
    """Answers queued replies in order; an exception in the queue is raised.
    The OAuth token request is answered automatically and not counted."""

    def __init__(self, *replies: Reply) -> None:
        self.replies: list[Reply] = list(replies)
        self.requests: list[HttpRequest] = []

    def queue(self, *replies: Reply) -> None:
        self.replies.extend(replies)

    def send(self, request: HttpRequest) -> HttpResponse:
        if request.url.endswith("/v1/oauth2/token"):
            return token_response()
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("unexpected PayPal call: %s %s" % (request.method, request.url))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def close(self) -> None:
        pass

    # helpers for assertions
    def paths(self) -> list[str]:
        return ["%s %s" % (r.method, urlsplit(r.url).path) for r in self.requests]

    @staticmethod
    def body(request: HttpRequest) -> Any:
        return getattr(request.body, "value", None)


class PayPalStub:
    """Installs a real PaypalClient over a StubTransport for one test."""

    def __init__(self) -> None:
        self.transport = StubTransport()
        self.client = PaypalClient(
            base_url="https://paypal.test", custom_http_client=self.transport,
            oauth2={"client_id": "id", "client_secret": "secret"},
        )
        self.previous = gateway.install_client(self.client)

    def restore(self) -> None:
        gateway.install_client(self.previous)


# ---------------------------------------------------------------------------
# PayPal bodies, in wire shape
# ---------------------------------------------------------------------------


def money(value: str, currency: str = "USD") -> dict[str, str]:
    return {"currency_code": currency, "value": value}


def authorization(auth_id: str, value: str, status: str = "CREATED", *, created: str = "2026-09-24T10:00:00Z",
                  expires: str = "2026-10-23T10:00:00Z") -> dict[str, Any]:
    return {"id": auth_id, "status": status, "amount": money(value), "create_time": created,
            "update_time": created, "expiration_time": expires}


def order(order_id: str, value: str, *, auth: dict[str, Any] | None = None, status: str = "COMPLETED") -> dict[str, Any]:
    unit: dict[str, Any] = {"reference_id": "default", "amount": money(value)}
    if auth is not None:
        unit["payments"] = {"authorizations": [auth]}
    return {
        "id": order_id, "status": status, "intent": "AUTHORIZE",
        "create_time": "2026-09-24T10:00:00Z", "update_time": "2026-09-24T10:00:00Z",
        "payment_source": {"card": {"brand": "VISA", "last_digits": "1111", "expiry": "2030-12"}},
        "purchase_units": [unit],
    }


def capture(capture_id: str, value: str, status: str = "COMPLETED", fee: str = "0.81", net: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": capture_id, "status": status, "amount": money(value), "final_capture": True,
        "create_time": "2026-09-24T11:00:00Z", "update_time": "2026-09-24T11:00:00Z",
    }
    if status != "PENDING":
        from decimal import Decimal
        body["seller_receivable_breakdown"] = {
            "gross_amount": money(value), "paypal_fee": money(fee),
            "net_amount": money(net or str(Decimal(value) - Decimal(fee))),
        }
    return body


def refund(refund_id: str, value: str, status: str = "COMPLETED") -> dict[str, Any]:
    return {"id": refund_id, "status": status, "amount": money(value),
            "create_time": "2026-09-24T12:00:00Z", "update_time": "2026-09-24T12:00:00Z"}


def payment_token(token_id: str, customer_id: str = "CUST1") -> dict[str, Any]:
    return {
        "id": token_id, "customer": {"id": customer_id},
        "payment_source": {"card": {"brand": "VISA", "last_digits": "1111", "expiry": "2030-12"}},
        "create_time": "2026-09-24T09:00:00Z",
    }


def paypal_error(status: int, issue: str, name: str = "UNPROCESSABLE_ENTITY") -> HttpResponse:
    return json_response(status, {
        "name": name, "message": "The requested action could not be performed.", "debug_id": "dbg",
        "details": [{"issue": issue, "description": "%s description" % issue}],
    })


def search_page(rows: list[dict[str, Any]], page: int = 1, total_pages: int = 1) -> dict[str, Any]:
    return {
        "transaction_details": [{"transaction_info": row} for row in rows],
        "page": page, "total_items": len(rows), "total_pages": total_pages,
    }


def transaction_row(transaction_id: str, initiated: str, value: str = "10.00", *, invoice_id: str = "",
                    custom: str = "", code: str = "T0006") -> dict[str, Any]:
    row: dict[str, Any] = {
        "transaction_id": transaction_id, "transaction_event_code": code,
        "transaction_initiation_date": initiated, "transaction_updated_date": initiated,
        "transaction_amount": money(value), "transaction_status": "S",
    }
    if invoice_id:
        row["invoice_id"] = invoice_id
    if custom:
        row["custom_field"] = custom
    return row
