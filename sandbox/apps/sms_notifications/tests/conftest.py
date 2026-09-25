"""
Test doubles at the SDK's transport seam: the real ``TwilioSdkClient`` builds
every request, and ``FakeTwilio`` answers it like the provider would.
"""
import itertools
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from twilio_sdk import TwilioSdkClient
from twilio_sdk.core import FormBody, HttpRequest, HttpResponse

from apps.sms_notifications import gateway as gateway_module
from apps.sms_notifications.gateway import TwilioGateway

ACCOUNT_SID = 'AC' + '0' * 32
FROM_NUMBER = '+15005550006'
SERVICE_SID = 'MG' + '1' * 32
SHOPPER_NUMBER = '+18255550100'


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


class FakeTwilio:
    """
    Answers the Messages and Lookup routes from in-memory state. ``fail_next``
    queues a one-shot override (an exception to raise or a response to return)
    for the next request whose method and path match.
    """

    def __init__(self) -> None:
        self.messages: dict[str, dict[str, Any]] = {}
        self.requests: list[HttpRequest] = []
        self.overrides: list[tuple[str, str, Callable[[HttpRequest], HttpResponse]]] = []
        self.invalid_numbers: set[str] = set()
        self._sids = itertools.count(1)
        self.create_status = 'queued'
        self.foreign: list[dict[str, Any]] = []

    # -- scripting ----------------------------------------------------------------------
    def fail_next(self, method: str, path_suffix: str, outcome: Exception | HttpResponse,
                  *, land: bool = False) -> None:
        """Make the next matching request fail. ``land=True``: the provider acts first, then fails."""
        def respond(request: HttpRequest) -> HttpResponse:
            if land:
                self._route(request)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        self.overrides.append((method, path_suffix, respond))

    def creates(self) -> list[HttpRequest]:
        return [r for r in self.requests if r.method == 'POST' and urlsplit(r.url).path.endswith('/Messages.json')]

    def updates(self) -> list[HttpRequest]:
        return [r for r in self.requests if r.method == 'POST' and not urlsplit(r.url).path.endswith(
            '/Messages.json')]

    # -- transport protocol ---------------------------------------------------------------
    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = urlsplit(request.url).path
        for i, (method, suffix, respond) in enumerate(self.overrides):
            if request.method == method and path.endswith(suffix):
                del self.overrides[i]
                return respond(request)
        return self._route(request)

    def close(self) -> None:
        pass

    # -- provider behaviour ---------------------------------------------------------------
    def _route(self, request: HttpRequest) -> HttpResponse:
        parts = urlsplit(request.url)
        path = unquote(parts.path)
        if path.startswith('/v2/PhoneNumbers/'):
            return self._lookup(path.rsplit('/', 1)[1])
        if path.endswith('/Messages.json') and request.method == 'POST':
            return self._create(_fields(request))
        if path.endswith('/Messages.json') and request.method == 'GET':
            return self._list(parse_qs(parts.query))
        sid = path.rsplit('/', 1)[1].removesuffix('.json')
        message = self.messages.get(sid)
        if message is None:
            return json_response(404, {'code': 20404, 'message': 'not found', 'status': 404})
        if request.method == 'GET':
            return json_response(200, message)
        fields = _fields(request)
        if fields.get('Status') == 'canceled':
            if message['status'] not in ('scheduled', 'accepted'):
                return json_response(400, {'code': 30409, 'message': 'cannot cancel', 'status': 400})
            message['status'] = 'canceled'
        if 'Body' in fields:
            message['body'] = fields['Body']
        return json_response(200, message)

    def _lookup(self, number: str) -> HttpResponse:
        digits = ''.join(ch for ch in number if ch.isdigit())
        canonical = '+' + (digits if len(digits) > 10 else '1' + digits)
        valid = canonical not in self.invalid_numbers and len(digits) >= 10
        return json_response(200, {'phone_number': canonical, 'valid': valid, 'country_code': 'CA',
                                   'validation_errors': [] if valid else ['TOO_SHORT']})

    def _create(self, fields: dict[str, str]) -> HttpResponse:
        sid = 'SM%032d' % next(self._sids)
        scheduled = fields.get('ScheduleType') == 'fixed'
        message = {
            'sid': sid, 'account_sid': ACCOUNT_SID, 'to': fields['To'], 'from': fields.get('From'),
            'body': fields.get('Body', ''), 'status': 'scheduled' if scheduled else self.create_status,
            'messaging_service_sid': fields.get('MessagingServiceSid'),
            'date_created': 'Thu, 25 Sep 2026 10:00:00 +0000', 'date_sent': None, 'error_code': None,
            'subresource_uris': {'media': '/x'},
        }
        self.messages[sid] = message
        return json_response(201, message)

    def deliver(self, sid: str, status: str = 'delivered', date_sent: str = 'Thu, 25 Sep 2026 10:00:05 +0000',
                error_code: int | None = None) -> None:
        self.messages[sid].update(status=status, date_sent=date_sent, error_code=error_code)

    def _list(self, query: dict[str, list[str]]) -> HttpResponse:
        rows = list(self.messages.values()) + self.foreign
        if 'To' in query:
            rows = [m for m in rows if m['to'] == query['To'][0]]
        if 'From' in query:
            rows = [m for m in rows if m['from'] == query['From'][0]]
        page_size = int(query.get('PageSize', ['50'])[0])
        page = int(query.get('Page', ['0'])[0])
        chunk = rows[page * page_size:(page + 1) * page_size]
        more = (page + 1) * page_size < len(rows)
        next_uri = ('/2010-04-01/Accounts/%s/Messages.json?PageSize=%d&Page=%d&PageToken=PA%d'
                    % (ACCOUNT_SID, page_size, page + 1, page + 1)) if more else None
        return json_response(200, {'messages': chunk, 'next_page_uri': next_uri, 'page': page,
                                   'page_size': page_size})


def _fields(request: HttpRequest) -> dict[str, str]:
    assert isinstance(request.body, FormBody)
    return {k: v if isinstance(v, str) else v[0] for k, v in request.body.fields.items()}


@pytest.fixture
def fake_twilio(settings: Any) -> Any:
    settings.TWILIO_ACCOUNT_SID = ACCOUNT_SID
    settings.TWILIO_AUTH_TOKEN = 'test-token'
    settings.TWILIO_FROM_NUMBER = FROM_NUMBER
    settings.TWILIO_MESSAGING_SERVICE_SID = SERVICE_SID
    settings.TWILIO_BASE_URL = None
    fake = FakeTwilio()
    client = TwilioSdkClient(custom_http_client=fake,
                             account_sid_auth_token={'username': ACCOUNT_SID, 'password': 'test-token'})
    gateway_module.set_gateway(TwilioGateway(client, account_sid=ACCOUNT_SID, from_number=FROM_NUMBER,
                                             messaging_service_sid=SERVICE_SID))
    yield fake
    gateway_module.set_gateway(None)


@pytest.fixture
def shopper(db: Any) -> Any:
    return get_user_model().objects.create_user(username='shopper', email='shopper@example.com',
                                                password='pw-shopper-1')


@pytest.fixture
def other_shopper(db: Any) -> Any:
    return get_user_model().objects.create_user(username='other', email='other@example.com',
                                                password='pw-other-1')


@pytest.fixture
def operator(db: Any) -> Any:
    return get_user_model().objects.create_user(username='operator', email='op@example.com',
                                                password='pw-op-1', is_staff=True)


class Api:
    """A JSON client for /api/ that logs in with Django's session login."""

    def __init__(self, user: Any) -> None:
        self.client = Client()
        self.client.force_login(user)

    def get(self, path: str, data: Any = None, **kw: Any) -> Any:
        return self.client.get('/api/' + path, data=data, **kw)

    def post(self, path: str, data: Any = None, **kw: Any) -> Any:
        return self.client.post('/api/' + path, data=json.dumps(data or {}), content_type='application/json',
                                **kw)

    def delete(self, path: str) -> Any:
        return self.client.delete('/api/' + path)


@pytest.fixture
def api_as() -> Callable[[Any], Api]:
    return Api


@pytest.fixture
def product(db: Any) -> Any:
    from oscar.test.factories import create_product
    return create_product(price=10, num_in_stock=100)
