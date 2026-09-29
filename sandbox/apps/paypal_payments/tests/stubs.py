"""A fake PayPal behind the SDK's transport seam: no network, real request building."""
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

from pay_pal_server_sdk.core import HttpRequest


@dataclass
class StubResponse:
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    chunks: list = field(default_factory=list)
    url: str = 'https://paypal.test/'
    closed: bool = False

    def iter_bytes(self, chunk_size=None) -> Iterator[bytes]:
        yield from self.chunks
        self.close()

    def read(self) -> bytes:
        self.close()
        return b''.join(self.chunks)

    def close(self) -> None:
        self.closed = True


def json_response(status, body):
    return StubResponse(status_code=status, headers={'content-type': 'application/json'},
                        chunks=[json.dumps(body).encode()])


def token_response():
    return json_response(200, {'access_token': 't', 'token_type': 'Bearer', 'expires_in': 3600})


def paypal_error(status, issue, description='refused'):
    return json_response(status, {'name': 'UNPROCESSABLE_ENTITY', 'message': 'refused', 'debug_id': 'dbg1',
                                  'details': [{'issue': issue, 'description': description}]})


class StubTransport:
    """Answers by (method, path regex); each route holds a queue of responses or exceptions."""

    def __init__(self):
        self.routes = []
        self.requests: list[HttpRequest] = []
        self.on('POST', r'/v1/oauth2/token$', token_response, repeat=True)

    def on(self, method, pattern, *responses, repeat=False):
        self.routes.insert(0, [method, re.compile(pattern), list(responses), repeat])
        return self

    def send(self, request: HttpRequest):
        self.requests.append(request)
        path = request.url.split('?', 1)[0]
        for method, pattern, queue, repeat in self.routes:
            if method == request.method and pattern.search(path) and queue:
                item = queue[0] if repeat else queue.pop(0)
                if isinstance(item, BaseException):
                    raise item
                return item() if callable(item) else item
        raise AssertionError('unexpected PayPal call %s %s' % (request.method, path))

    def close(self):
        pass

    def calls(self, method, pattern):
        rx = re.compile(pattern)
        return [r for r in self.requests if r.method == method and rx.search(r.url.split('?', 1)[0])]


# --- canned PayPal bodies -------------------------------------------------

def order_body(auth_status='CREATED', amount='20.00', auth_id='AUTH1', order_id='ORD1', created='2026-09-01T10:00:00Z',
               expires='2026-09-30T10:00:00Z', order_status='COMPLETED', invoice='x'):
    return {
        'id': order_id, 'status': order_status, 'intent': 'AUTHORIZE',
        'payment_source': {'card': {'last_digits': '1111', 'brand': 'VISA'}},
        'purchase_units': [{'invoice_id': invoice, 'payments': {'authorizations': [{
            'id': auth_id, 'status': auth_status, 'amount': {'currency_code': 'USD', 'value': amount},
            'create_time': created, 'expiration_time': expires}]}}],
    }


def authorization_body(status='CREATED', auth_id='AUTH1', amount='20.00', created='2026-09-01T10:00:00Z',
                       expires='2026-09-30T10:00:00Z'):
    return {'id': auth_id, 'status': status, 'amount': {'currency_code': 'USD', 'value': amount},
            'create_time': created, 'expiration_time': expires, 'update_time': created}


def capture_body(status='COMPLETED', capture_id='CAP1', amount='20.00', fee='0.88', net='19.12'):
    return {'id': capture_id, 'status': status, 'amount': {'currency_code': 'USD', 'value': amount},
            'create_time': '2026-09-02T10:00:00Z',
            'seller_receivable_breakdown': {'gross_amount': {'currency_code': 'USD', 'value': amount},
                                            'paypal_fee': {'currency_code': 'USD', 'value': fee},
                                            'net_amount': {'currency_code': 'USD', 'value': net}}}


def refund_body(status='COMPLETED', refund_id='REF1', amount='5.00'):
    return {'id': refund_id, 'status': status, 'amount': {'currency_code': 'USD', 'value': amount},
            'create_time': '2026-09-03T10:00:00Z'}


def token_body(token_id='TOK1', customer='CUST1', verification=None):
    card = {'last_digits': '1111', 'brand': 'VISA', 'expiry': '2030-12'}
    if verification:
        card['verification_status'] = verification
    return {'id': token_id, 'customer': {'id': customer}, 'payment_source': {'card': card}}
