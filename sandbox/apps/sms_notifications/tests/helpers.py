"""
A fake transport for the Twilio SDK client: every test runs the SDK's real
request pipeline and no request leaves the process.
"""
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from django.test import override_settings
from twilio_sdk.core import HttpRequest, HttpResponse

from apps.sms_notifications import gateway

TEST_SETTINGS = dict(
    TWILIO_ACCOUNT_SID='AC00000000000000000000000000000000',
    TWILIO_AUTH_TOKEN='test-token',
    TWILIO_FROM_NUMBER='+15555550100',
    TWILIO_MESSAGING_SERVICE_SID='MG00000000000000000000000000000000',
    TWILIO_BASE_URL=None,
    TWILIO_LOOKUPS_BASE_URL=None,
    SMS_REFERENCE_PREFIX='test-install',
)

SHOPPER_NUMBER = '+15555550142'


@dataclass
class StubResponse:
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    chunks: list[bytes] = field(default_factory=list)
    url: str = 'https://api.test/resource'
    closed: bool = False

    def iter_bytes(self, chunk_size: int | None) -> Iterator[bytes]:
        yield from self.chunks
        self.close()

    def read(self) -> bytes:
        self.close()
        return b''.join(self.chunks)

    def close(self) -> None:
        self.closed = True


class StubTransport:
    """Answers queued responses in order; a queued exception is raised instead."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests: list[HttpRequest] = []

    def queue(self, *responses):
        self._responses.extend(responses)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError('Unexpected provider call: %s %s' % (request.method, request.url))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        pass

    @property
    def pending(self):
        return len(self._responses)


def json_response(status, body):
    return StubResponse(status_code=status, headers={'content-type': 'application/json'},
                        chunks=[json.dumps(body).encode()])


def message(sid='SM00000000000000000000000000000001', status='queued', body='x', to=SHOPPER_NUMBER,
            date_sent=None, error_code=None, error_message=None):
    return {
        'sid': sid, 'status': status, 'body': body, 'to': to, 'from': TEST_SETTINGS['TWILIO_FROM_NUMBER'],
        'account_sid': TEST_SETTINGS['TWILIO_ACCOUNT_SID'],
        'date_created': 'Tue, 29 Sep 2026 10:00:00 +0000', 'date_updated': 'Tue, 29 Sep 2026 10:00:00 +0000',
        'date_sent': date_sent, 'error_code': error_code, 'error_message': error_message,
        'direction': 'outbound-api', 'messaging_service_sid': TEST_SETTINGS['TWILIO_MESSAGING_SERVICE_SID'],
    }


def message_response(status_code=201, **kwargs):
    return json_response(status_code, message(**kwargs))


def lookup_response(phone_number=SHOPPER_NUMBER, valid=True, errors=()):
    return json_response(200, {
        'phone_number': phone_number, 'valid': valid, 'validation_errors': list(errors),
        'country_code': 'US', 'calling_country_code': '1', 'national_format': '(555) 555-0142',
    })


def error_response(status, code, message_text='error'):
    return json_response(status, {'code': code, 'message': message_text, 'status': status})


def form_fields(request):
    return dict(request.body.fields)


def query(request):
    return {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}


class ProviderTestMixin:
    """Installs a client over a stub transport for the duration of each test."""

    def setUp(self):
        super().setUp()
        overrider = override_settings(**TEST_SETTINGS)
        overrider.enable()
        self.addCleanup(overrider.disable)
        self.transport = StubTransport()
        gateway.set_client(gateway.build_client(transport=self.transport))
        self.addCleanup(gateway.set_client, None)

    def provider_calls(self, method=None, path=None):
        return [r for r in self.transport.requests
                if (method is None or r.method == method) and (path is None or path in r.url)]
