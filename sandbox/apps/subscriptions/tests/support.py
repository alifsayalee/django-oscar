"""Test support: a fake transport that satisfies the SDK's sync protocol.

Faking the transport (rather than mocking the client) exercises the real
request-building and response-decoding pipeline, which is the whole point of the
seam. Basic auth means there is no token fetch, so the stub sees only the
operation requests, in order.
"""

import json

from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import BasicAuthCredentials, HttpRequest, HttpResponse


class StubTransport:
    """Returns queued responses in order and records every request sent."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        if not self._responses:
            raise AssertionError('StubTransport received an unexpected request: {} {}'.format(
                request.method, request.url))
        return self._responses.pop(0)

    def close(self):
        pass

    @property
    def last_request(self):
        return self.requests[-1] if self.requests else None


def json_response(status, body):
    return HttpResponse(
        status_code=status,
        headers={'content-type': 'application/json'},
        content=json.dumps(body).encode(),
    )


def raising_transport(exc):
    class Boom:
        def __init__(self):
            self.requests = []

        def send(self, request):
            self.requests.append(request)
            raise exc

        def close(self):
            pass

    return Boom()


def make_client(transport):
    return MaxioAdvancedBillingClient(
        basic_auth=BasicAuthCredentials(username='k', password='x'),
        environment='us',
        server_config={'production': {'us': {'site': 'test-site'}}},
        custom_http_client=transport,
    )


# --- Wire-shaped response bodies -------------------------------------------

def product_body(product_id, handle, name, price_cents, family_handle='eshop-subscribe',
                 require_credit_card=False):
    return {
        'product': {
            'id': product_id,
            'name': name,
            'handle': handle,
            'price_in_cents': price_cents,
            'interval': 1,
            'interval_unit': 'month',
            'require_credit_card': require_credit_card,
            'product_family': {'id': 1, 'handle': family_handle, 'name': 'Eshop'},
        }
    }


def customer_body(customer_id, reference):
    return {'customer': {'id': customer_id, 'reference': reference,
                         'first_name': 'Ada', 'last_name': 'Lovelace',
                         'email': 'ada@example.com'}}


def subscription_body(sub_id, state, plan_handle, price_cents=29900):
    return {
        'subscription': {
            'id': sub_id,
            'state': state,
            'current_billing_amount_in_cents': price_cents,
            'product_price_in_cents': price_cents,
            'currency': 'USD',
            'next_assessment_at': '2026-10-22T12:00:00Z',
            'created_at': '2026-09-22T12:00:00Z',
            'product': {'id': 7130993, 'handle': plan_handle, 'name': 'Pro Plan'},
        }
    }
