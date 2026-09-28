"""A stub PayPal: the SDK's transport protocol, answering by method and path."""
import json
from collections import defaultdict, deque
from typing import Any
from urllib.parse import parse_qs, urlsplit

from paypal.core import HttpRequest, HttpResponse

from apps.payments_api import paypal_client


def json_response(status: int, body: object = None) -> HttpResponse:
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=b'' if body is None else json.dumps(body).encode())


def token_response() -> HttpResponse:
    return json_response(200, {'access_token': 't', 'token_type': 'Bearer', 'expires_in': 3600})


def paypal_error(status: int, issue: str) -> HttpResponse:
    return json_response(status, {'name': 'UNPROCESSABLE_ENTITY', 'message': 'Refused.', 'debug_id': 'dbg',
                                  'details': [{'issue': issue, 'description': 'stub'}]})


class StubTransport:
    """Queues answers per (METHOD, path-prefix); an answer may be an exception to raise."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], deque[Any]] = defaultdict(deque)
        self.requests: list[HttpRequest] = []

    def on(self, method: str, path: str, *answers: Any) -> 'StubTransport':
        self.routes[(method, path)].extend(answers)
        return self

    def send(self, request: HttpRequest) -> HttpResponse:
        path = urlsplit(request.url).path
        if path == '/v1/oauth2/token':
            return token_response()
        self.requests.append(request)
        for (method, prefix), answers in self.routes.items():
            if method == request.method and path == prefix and answers:
                answer = answers.popleft()
                if isinstance(answer, BaseException):
                    raise answer
                return answer
        raise AssertionError('unexpected PayPal call %s %s' % (request.method, path))

    def close(self) -> None:
        pass

    def calls(self, method: str, path: str) -> list[HttpRequest]:
        return [r for r in self.requests if r.method == method and urlsplit(r.url).path == path]

    @staticmethod
    def query(request: HttpRequest) -> dict[str, list[str]]:
        return parse_qs(urlsplit(request.url).query)


def install(transport: StubTransport) -> None:
    paypal_client.set_client(paypal_client.build_client(transport))


# --- wire-shaped PayPal bodies

def authorization(auth_id: str = 'AUTH1', status: str = 'CREATED', value: str = '15.98',
                  created: str = '2026-09-28T10:00:00Z', expires: str = '2099-01-01T00:00:00Z',
                  custom_id: str = '') -> dict[str, Any]:
    return {'id': auth_id, 'status': status, 'amount': {'currency_code': 'USD', 'value': value},
            'create_time': created, 'expiration_time': expires, 'custom_id': custom_id}


def paypal_order(auth: dict[str, Any] | None = None, status: str = 'COMPLETED', value: str = '15.98') -> dict[str, Any]:
    unit: dict[str, Any] = {'reference_id': 'x', 'amount': {'currency_code': 'USD', 'value': value}}
    if auth is not None:
        unit['payments'] = {'authorizations': [auth]}
    return {'id': 'PPORDER1', 'status': status, 'create_time': '2026-09-28T10:00:00Z',
            'purchase_units': [unit],
            'payment_source': {'card': {'last_digits': '1111', 'brand': 'VISA'}}}


def capture(capture_id: str = 'CAP1', status: str = 'COMPLETED', value: str = '15.98') -> dict[str, Any]:
    return {'id': capture_id, 'status': status, 'amount': {'currency_code': 'USD', 'value': value},
            'create_time': '2026-09-28T11:00:00Z',
            'seller_receivable_breakdown': {'gross_amount': {'currency_code': 'USD', 'value': value},
                                            'paypal_fee': {'currency_code': 'USD', 'value': '0.95'},
                                            'net_amount': {'currency_code': 'USD', 'value': '15.03'}}}


def refund(refund_id: str = 'REF1', status: str = 'COMPLETED', value: str = '5.00') -> dict[str, Any]:
    return {'id': refund_id, 'status': status, 'amount': {'currency_code': 'USD', 'value': value},
            'create_time': '2026-09-28T12:00:00Z'}


def vault_token(token_id: str = 'TOK1') -> dict[str, Any]:
    return {'id': token_id, 'customer': {'id': 'C1'}, 'create_time': '2026-09-28T09:00:00Z',
            'payment_source': {'card': {'last_digits': '1111', 'brand': 'VISA', 'expiry': '2030-12'}}}


def search_page(rows: list[dict[str, Any]], page: int = 1, total_pages: int = 1) -> dict[str, Any]:
    return {'transaction_details': [{'transaction_info': r} for r in rows], 'page': page,
            'total_pages': total_pages, 'total_items': len(rows), 'last_refreshed_datetime': '2099-01-01T00:00:00Z'}
