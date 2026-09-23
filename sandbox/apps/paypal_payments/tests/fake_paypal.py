"""
An in-memory PayPal behind the SDK's transport seam.

The real ``PaypalClient`` builds and sends every request through this stub, so
tests exercise request building, serialization and decoding; only the network
is replaced. Responses follow the shapes observed in the PayPal sandbox,
including replaying a write whose PayPal-Request-Id was seen before.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from paypal.core import HttpRequest, HttpResponse, JsonBody

from apps.paypal_payments.gateway import build_client


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _ts(moment):
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


def json_response(status, body=None):
    content = b'' if body is None else json.dumps(body).encode()
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'}, content=content)


def paypal_error(status, issue, name='UNPROCESSABLE_ENTITY'):
    return json_response(status, {
        'name': name, 'message': 'The requested action could not be performed.',
        'debug_id': 'dbg123', 'details': [{'issue': issue, 'description': 'Issue %s.' % issue}],
    })


class FakePayPal:
    """Satisfies the SDK's sync transport protocol: ``send`` and ``close``."""

    def __init__(self):
        self.requests = []
        self.replays = {}
        self.authorizations = {}
        self.captures = {}
        self.tokens = {}
        self.counter = 0
        self.fail_next = {}  # route name -> HttpResponse or exception
        self.reauthorize_response = None
        self.transactions = []  # reporting records

    # -- helpers -------------------------------------------------------------

    def client(self):
        """The production client construction, with this stub as the HTTP layer."""
        return build_client(self)

    def _id(self, prefix):
        self.counter += 1
        return '%s%05d' % (prefix, self.counter)

    def calls(self, route):
        return [r for r in self.requests if self._route(r)[0] == route]

    @staticmethod
    def _route(request):
        path = re.sub(r'^https?://[^/]+', '', request.url).split('?')[0]
        patterns = [
            ('token', 'POST', r'^/v1/oauth2/token$'),
            ('create_order', 'POST', r'^/v2/checkout/orders$'),
            ('get_authorization', 'GET', r'^/v2/payments/authorizations/([^/]+)$'),
            ('reauthorize', 'POST', r'^/v2/payments/authorizations/([^/]+)/reauthorize$'),
            ('capture', 'POST', r'^/v2/payments/authorizations/([^/]+)/capture$'),
            ('void', 'POST', r'^/v2/payments/authorizations/([^/]+)/void$'),
            ('get_capture', 'GET', r'^/v2/payments/captures/([^/]+)$'),
            ('refund', 'POST', r'^/v2/payments/captures/([^/]+)/refund$'),
            ('vault', 'POST', r'^/v3/vault/payment-tokens$'),
            ('delete_token', 'DELETE', r'^/v3/vault/payment-tokens/([^/]+)$'),
            ('search', 'GET', r'^/v1/reporting/transactions$'),
        ]
        for name, method, pattern in patterns:
            match = re.match(pattern, path)
            if match and request.method == method:
                return name, match.groups()
        raise AssertionError('Unexpected PayPal request %s %s' % (request.method, path))

    # -- transport protocol ----------------------------------------------------

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        route, args = self._route(request)
        if route == 'token':
            return json_response(200, {'access_token': 't', 'token_type': 'Bearer', 'expires_in': 3600})
        failure = self.fail_next.pop(route, None)
        if isinstance(failure, Exception):
            raise failure
        if failure is not None:
            return failure
        request_id = request.headers.get('paypal-request-id')
        key = (route, request_id)
        if request_id and key in self.replays:
            return self.replays[key]
        body = request.body.value if isinstance(request.body, JsonBody) else None
        response = getattr(self, '_' + route)(body, *args, query=request.url)
        if request_id and 200 <= response.status_code < 300:
            self.replays[key] = response
        return response

    def close(self):
        pass

    # -- routes --------------------------------------------------------------

    def _authorization(self, auth_id, amount, created):
        auth = {
            'id': auth_id, 'status': 'CREATED', 'amount': amount,
            'create_time': _ts(created), 'expiration_time': _ts(created + timedelta(days=29)),
        }
        self.authorizations[auth_id] = auth
        return auth

    def _create_order(self, body, query):
        unit = body['purchase_units'][0]
        card = body['payment_source']['card']
        if 'vault_id' in card:
            if card['vault_id'] not in self.tokens:
                return paypal_error(403, 'PERMISSION_DENIED', name='NOT_AUTHORIZED')
            last = self.tokens[card['vault_id']]['last_digits']
        else:
            if card['expiry'] < _now().strftime('%Y-%m'):
                return paypal_error(422, 'CARD_EXPIRED')
            last = card['number'][-4:]
        auth = self._authorization(self._id('AUTH'), unit['amount'], _now())
        return json_response(201, {
            'id': self._id('ORDER'), 'status': 'COMPLETED', 'intent': 'AUTHORIZE',
            'payment_source': {'card': {'last_digits': last, 'brand': 'VISA'}},
            'purchase_units': [{'amount': unit['amount'], 'custom_id': unit.get('custom_id'),
                                'payments': {'authorizations': [dict(auth)]}}],
        })

    def _get_authorization(self, body, auth_id, query):
        return json_response(200, self.authorizations[auth_id])

    def _reauthorize(self, body, auth_id, query):
        if self.reauthorize_response is not None:
            return self.reauthorize_response
        old = self.authorizations[auth_id]
        old['status'] = 'VOIDED'
        created = datetime.fromisoformat(old['create_time'].replace('Z', '+00:00'))
        auth = self._authorization(self._id('AUTH'), body['amount'], _now())
        auth['expiration_time'] = _ts(created + timedelta(days=29))
        return json_response(201, auth)

    def _capture(self, body, auth_id, query):
        auth = self.authorizations[auth_id]
        if auth['status'] == 'VOIDED':
            return paypal_error(422, 'AUTHORIZATION_VOIDED')
        auth['status'] = 'CAPTURED'
        gross = Decimal(body['amount']['value'])
        fee = (gross * Decimal('0.0349') + Decimal('0.49')).quantize(Decimal('0.01'))
        capture = {
            'id': self._id('CAP'), 'status': 'COMPLETED', 'amount': body['amount'], 'final_capture': True,
            'create_time': _ts(_now()),
            'seller_receivable_breakdown': {
                'gross_amount': body['amount'],
                'paypal_fee': {'currency_code': body['amount']['currency_code'], 'value': str(fee)},
                'net_amount': {'currency_code': body['amount']['currency_code'], 'value': str(gross - fee)},
            },
        }
        capture['refunded'] = '0.00'
        self.captures[capture['id']] = capture
        return json_response(201, {k: v for k, v in capture.items() if k != 'refunded'})

    def _void(self, body, auth_id, query):
        auth = self.authorizations[auth_id]
        if auth['status'] == 'VOIDED':
            return paypal_error(422, 'PREVIOUSLY_VOIDED')
        auth['status'] = 'VOIDED'
        return json_response(200, auth)

    def _get_capture(self, body, capture_id, query):
        return json_response(200, {k: v for k, v in self.captures[capture_id].items() if k != 'refunded'})

    def _refund(self, body, capture_id, query):
        capture = self.captures[capture_id]
        amount = Decimal(body['amount']['value'])
        refunded = Decimal(capture['refunded'])
        if refunded + amount > Decimal(capture['amount']['value']):
            return paypal_error(422, 'REFUND_AMOUNT_EXCEEDED')
        capture['refunded'] = str(refunded + amount)
        return json_response(201, {
            'id': self._id('REF'), 'status': 'COMPLETED', 'amount': body['amount'],
            'create_time': _ts(_now()),
            'seller_payable_breakdown': {'gross_amount': body['amount'], 'net_amount': body['amount']},
        })

    def _vault(self, body, query):
        card = body['payment_source']['card']
        customer = (body.get('customer') or {}).get('id') or self._id('CUST')
        token_id = self._id('TOKEN')
        self.tokens[token_id] = {'last_digits': card['number'][-4:]}
        return json_response(201, {
            'id': token_id, 'customer': {'id': customer},
            'payment_source': {'card': {'last_digits': card['number'][-4:], 'brand': 'VISA',
                                        'expiry': card['expiry'], 'name': card.get('name')}},
        })

    def _delete_token(self, body, token_id, query):
        self.tokens.pop(token_id, None)
        return HttpResponse(status_code=204, headers={})

    def _search(self, body, query):
        page = int(re.search(r'[?&]page=(\d+)', query).group(1))
        size = int(re.search(r'[?&]page_size=(\d+)', query).group(1))
        pages = max(1, -(-len(self.transactions) // size))
        chunk = self.transactions[(page - 1) * size:page * size]
        return json_response(200, {
            'transaction_details': [{'transaction_info': t} for t in chunk],
            'page': page, 'total_items': len(self.transactions), 'total_pages': pages,
        })
