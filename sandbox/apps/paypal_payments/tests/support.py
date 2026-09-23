import json
from contextlib import contextmanager
from unittest import mock

from paypal.core import HttpRequest, HttpResponse

from apps.paypal_payments import gateway as gw
from apps.paypal_payments import services

CONFIG = gw.PayPalConfig(client_id="test-client", client_secret="test-secret", environment="sandbox",
                         currency="USD")


class StubTransport:
    """The SDK's sync transport protocol. Answers queued responses in order; records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[HttpRequest] = []

    def queue(self, *responses):
        self.responses.extend(responses)

    def send(self, request):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected PayPal call: %s %s" % (request.method, request.url))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self):
        pass

    @property
    def api_requests(self):
        return [r for r in self.requests if not r.url.endswith("/v1/oauth2/token")]


def json_response(status, body):
    return HttpResponse(status_code=status, headers={"content-type": "application/json"},
                        content=json.dumps(body).encode())


def empty_response(status):
    return HttpResponse(status_code=status, headers={})


def token_response():
    return json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})


def paypal_error(status, issue, description="rejected"):
    return json_response(status, {"name": "UNPROCESSABLE_ENTITY", "message": "The requested action could not be "
                                  "performed.", "debug_id": "dbg1",
                                  "details": [{"issue": issue, "description": description}]})


def order_body(amount, auth_status="CREATED", order_status="COMPLETED", auth_id="AUTH1", currency="USD",
               created=None, expires=None):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    created = created or now.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires = expires or (now + timedelta(days=29)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": "PPORDER1", "status": order_status, "intent": "AUTHORIZE",
        "payment_source": {"card": {"brand": "VISA", "last_digits": "1111"}},
        "purchase_units": [{"payments": {"authorizations": [{
            "id": auth_id, "status": auth_status, "amount": {"currency_code": currency, "value": amount},
            "create_time": created, "expiration_time": expires}]}}],
    }


def capture_body(amount, status="COMPLETED", capture_id="CAP1", fee="0.75", net=None):
    from decimal import Decimal
    net = net or str(Decimal(amount) - Decimal(fee))
    return {"id": capture_id, "status": status, "amount": {"currency_code": "USD", "value": amount},
            "create_time": "2026-09-23T10:00:00Z",
            "seller_receivable_breakdown": {"gross_amount": {"currency_code": "USD", "value": amount},
                                            "paypal_fee": {"currency_code": "USD", "value": fee},
                                            "net_amount": {"currency_code": "USD", "value": net}}}


def refund_body(amount, status="COMPLETED", refund_id="REF1"):
    return {"id": refund_id, "status": status, "amount": {"currency_code": "USD", "value": amount},
            "create_time": "2026-09-23T11:00:00Z"}


def authorization_body(amount, status="CREATED", auth_id="AUTH2", expires="2026-10-25T10:00:00Z"):
    return {"id": auth_id, "status": status, "amount": {"currency_code": "USD", "value": amount},
            "create_time": "2026-09-23T10:00:00Z", "expiration_time": expires}


@contextmanager
def stub_paypal(*responses):
    """Route services' PayPal calls through a real SDK client over a stub transport."""
    transport = StubTransport(token_response(), *responses)
    client = gw.build_client(CONFIG, transport=transport)
    with mock.patch.object(services, "get_gateway", return_value=gw.PayPalGateway(client)):
        yield transport
