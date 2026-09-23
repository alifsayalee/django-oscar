"""Shared test doubles: a stub transport satisfying the SDK's sync transport protocol.

Auth is HTTP Basic (no token fetch), so the first request the stub sees is the operation itself.
"""
import json

from twilio_sdk import TwilioSdkClient
from twilio_sdk.core import BasicAuthCredentials, HttpRequest, HttpResponse


class StubTransport:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self._responses.pop(0)

    def close(self):  # pragma: no cover - no-op
        pass

    @property
    def last_request(self):
        return self.requests[-1] if self.requests else None


class RaisingTransport(StubTransport):
    def __init__(self, error, *responses):
        super().__init__(*responses)
        self._error = error

    def send(self, request):
        if self._responses:
            return super().send(request)
        self.requests.append(request)
        raise self._error


def json_response(status, body):
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def make_client(transport):
    return TwilioSdkClient(
        account_sid_auth_token=BasicAuthCredentials(username="AC_test", password="secret"),
        custom_http_client=transport,
        timeout=5.0,
    )
