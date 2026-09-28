import json
import re
from urllib.parse import unquote, urlsplit

import pytest
from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import HttpRequest, HttpResponse

from apps.subscriptions import maxio, service


def json_response(status, body):
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


def empty_response(status):
    return HttpResponse(status_code=status, headers={}, content=b'')


class RoutingTransport:
    """
    Satisfies the SDK's sync transport protocol. Answers each request from a
    queue keyed by "METHOD /path" (numeric ids become {id}); a queued
    exception is raised instead of answered.
    """

    def __init__(self):
        self.routes = {}
        self.requests = []

    def on(self, route, *answers):
        self.routes.setdefault(route, []).extend(answers)
        return self

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = re.sub(r'/\d+', '/{id}', unquote(urlsplit(request.url).path))
        route = f'{request.method} {path}'
        queue = self.routes.get(route)
        if not queue:
            raise AssertionError(f'unexpected Maxio call: {route}')
        answer = queue.pop(0) if len(queue) > 1 else queue[0]  # the last answer repeats
        if isinstance(answer, BaseException):
            raise answer
        assert isinstance(answer, HttpResponse)
        return answer

    def close(self):
        pass

    def calls(self, route):
        method, path = route.split(' ', 1)
        return [r for r in self.requests if r.method == method
                and re.sub(r'/\d+', '/{id}', unquote(urlsplit(r.url).path)) == path]


FAMILY_ROUTE = 'GET /product_families/handle:test-family/products.json'


def product(handle, price, **extra):
    return {'product': {'id': 1, 'name': handle.title(), 'handle': handle, 'price_in_cents': price,
                        'interval': 1, 'interval_unit': 'month', 'require_credit_card': False,
                        'archived_at': None, **extra}}


def customer(ref, customer_id=501):
    return {'customer': {'id': customer_id, 'first_name': 'Sam', 'last_name': 'Shopper',
                         'email': 'sam@example.com', 'reference': ref,
                         'created_at': '2026-09-28T10:00:00Z'}}


def subscription(state='active', subscription_id=9001, handle='pro', price=29900, customer_id=501,
                 reference=None):
    return {'subscription': {
        'id': subscription_id, 'state': state, 'product_price_in_cents': price, 'currency': 'USD',
        'reference': reference, 'created_at': '2026-09-28T10:00:01Z',
        'next_assessment_at': '2026-10-28T10:00:01Z',
        'current_period_ends_at': '2026-10-28T10:00:01Z',
        'product': {'id': 1, 'handle': handle, 'name': handle.title(), 'price_in_cents': price,
                    'interval': 1, 'interval_unit': 'month'},
        'customer': {'id': customer_id, 'reference': 'x'},
    }}


@pytest.fixture
def transport(settings):
    settings.MAXIO_DEFAULT_PRODUCT_FAMILY = 'test-family'
    settings.MAXIO_REFERENCE_PREFIX = 'test'
    stub = RoutingTransport()
    stub.on('GET /site.json', json_response(200, {'site': {'id': 1, 'currency': 'USD', 'relationship_invoicing_enabled': True}}))
    stub.on(FAMILY_ROUTE, json_response(200, [product('pro', 29900), product('basic', 2900)]))
    client = MaxioAdvancedBillingClient(
        environment='us', server_config={'production': {'us': {'site': 'test'}}},
        basic_auth={'username': 'key', 'password': 'x'}, custom_http_client=stub)
    maxio.set_client(client)
    service._site_info = None
    yield stub
    maxio.set_client(None)
    service._site_info = None


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(
        username='sam', email='sam@example.com', password='pw', first_name='Sam', last_name='Shopper')


@pytest.fixture
def api(client, user):
    client.force_login(user)
    return client
