"""A fake transport satisfying the SDK's sync transport protocol, so tests build
a real PaypalClient with no network. The token request is the first one the
transport sees (the SDK fetches it lazily on the first authenticated call).
"""

import json

from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpResponse


class StubTransport:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("StubTransport ran out of queued responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass

    @property
    def last_request(self):
        return self.requests[-1] if self.requests else None


def json_response(status, body):
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def token_response():
    return json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})


def client_with(*responses):
    transport = StubTransport(token_response(), *responses)
    client = PaypalClient(
        custom_http_client=transport,
        oauth2=ClientCredentials(client_id="id", client_secret="secret"),
    )
    return client, transport
