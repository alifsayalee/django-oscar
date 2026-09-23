"""A stub PayPal behind the SDK's transport seam (``custom_http_client``).

The real SDK client builds, authenticates and decodes every request; only the
network is replaced. Routes answer in PayPal's wire shape.
"""
import json
import re

from paypal.core import HttpRequest, HttpResponse

from apps.paypal_payments import gateway


def json_response(status, body=None):
    return HttpResponse(
        status_code=status,
        headers={'content-type': 'application/json', 'paypal-debug-id': 'dbg-test'},
        content=b'' if body is None else json.dumps(body).encode(),
    )


def paypal_error(status, issue, description='refused', name='UNPROCESSABLE_ENTITY'):
    return json_response(status, {
        'name': name, 'message': 'The requested action could not be performed.',
        'debug_id': 'dbg-%s' % issue.lower(),
        'details': [{'issue': issue, 'description': description}],
    })


class StubTransport:
    def __init__(self):
        self.routes = []
        self.requests: list[HttpRequest] = []
        self.route('POST', r'/v1/oauth2/token$', lambda req, m: json_response(200, {
            'access_token': 'test-token', 'token_type': 'Bearer', 'expires_in': 3600}))

    def route(self, method, pattern, handler):
        """Register ``handler(request, match) -> HttpResponse``; later routes win."""
        self.routes.insert(0, (method, re.compile(pattern), handler))

    def send(self, request):
        self.requests.append(request)
        path = request.url.split('?', 1)[0]
        for method, pattern, handler in self.routes:
            match = pattern.search(path)
            if method == request.method and match:
                result = handler(request, match)
                if isinstance(result, Exception):
                    raise result
                return result
        return json_response(404, {'name': 'RESOURCE_NOT_FOUND', 'message': 'no route',
                                   'debug_id': 'x'})

    def close(self):
        pass

    def calls(self, method, pattern):
        rx = re.compile(pattern)
        return [r for r in self.requests
                if r.method == method and rx.search(r.url.split('?', 1)[0])]


def body_of(request):
    return request.body.value if request.body is not None else None


def install(transport):
    client = gateway.build_client(http_client=transport)
    gateway.set_client(client)
    return client


# ---- canned PayPal resources -------------------------------------------

def authorization(auth_id, amount, currency='USD', status='CREATED',
                  create_time='2026-09-24T10:00:00Z', expiration_time='2026-10-23T10:00:00Z'):
    return {'id': auth_id, 'status': status,
            'amount': {'currency_code': currency, 'value': amount},
            'create_time': create_time, 'expiration_time': expiration_time}


def completed_order(order_id, auth, *, status='COMPLETED', last_digits='1111', brand='VISA'):
    body = {'id': order_id, 'status': status, 'intent': 'AUTHORIZE',
            'payment_source': {'card': {'last_digits': last_digits, 'brand': brand}},
            'purchase_units': [{'reference_id': 'x'}]}
    if auth is not None:
        body['purchase_units'][0]['payments'] = {'authorizations': [auth]}
    return body


def capture(capture_id, amount, fee='0.81', net=None, status='COMPLETED'):
    net = net or '%.2f' % (float(amount) - float(fee))
    return {'id': capture_id, 'status': status, 'final_capture': True,
            'amount': {'currency_code': 'USD', 'value': amount},
            'seller_receivable_breakdown': {
                'gross_amount': {'currency_code': 'USD', 'value': amount},
                'paypal_fee': {'currency_code': 'USD', 'value': fee},
                'net_amount': {'currency_code': 'USD', 'value': net}},
            'create_time': '2026-09-24T11:00:00Z'}
