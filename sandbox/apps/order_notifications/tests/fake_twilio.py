"""
An in-memory stand-in for the Twilio endpoints this app uses, plugged in at the
SDK's transport seam so the real SDK builds and decodes every request.
"""

import itertools
import json
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from twilio_sdk import TwilioSdkClient
from twilio_sdk.core import FormBody, HttpRequest, HttpResponse

ACCOUNT_SID = "ACtest00000000000000000000000000"
FROM_NUMBER = "+15005550006"
SERVICE_SID = "MGtest00000000000000000000000000"

TEST_SETTINGS = dict(
    TWILIO_ACCOUNT_SID=ACCOUNT_SID,
    TWILIO_AUTH_TOKEN="test-token-not-a-secret",
    TWILIO_FROM_NUMBER=FROM_NUMBER,
    TWILIO_MESSAGING_SERVICE_SID=SERVICE_SID,
    TWILIO_BASE_URL="",
    ORDER_NOTIFICATIONS_FOLLOWUP_DELAY_HOURS=72,
)


def _json(status, body):
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def _rfc2822(dt):
    return format_datetime(dt.astimezone(timezone.utc), usegmt=True)


class FakeTwilio:
    """Implements the SDK's sync transport protocol (send + close)."""

    def __init__(self):
        self.requests: list[HttpRequest] = []
        self.messages: dict[str, dict] = {}
        self.numbers: dict[str, dict] = {}  # lookup input -> canonical record
        self.faults: list[tuple[str, object]] = []  # (operation, exception | HttpResponse)
        self.send_status = "queued"
        self.page_size_override = None
        self._ids = itertools.count(1)
        self.closed = False

    # -- test helpers -------------------------------------------------------

    def fail_next(self, operation, fault):
        """Make the next ``operation`` (send/fetch/update/list/lookup) fail with ``fault``."""
        self.faults.append((operation, fault))

    def add_number(self, raw, canonical, country="CA"):
        self.numbers[raw] = {
            "phone_number": canonical,
            "country_code": country,
            "national_format": raw,
            "caller_name": None,
            "carrier": None,
            "add_ons": None,
            "url": "https://lookups.twilio.com/v1/PhoneNumbers/" + canonical,
        }

    def add_foreign_message(self, to, status="delivered", created=None, from_=FROM_NUMBER):
        return self._store(to, "someone else's message", status, from_, None, created)

    def sent(self, operation="send"):
        return [r for r in self.requests if self._operation(r) == operation]

    # -- transport protocol ---------------------------------------------------

    def close(self):
        self.closed = True

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        operation = self._operation(request)
        for index, (op, fault) in enumerate(self.faults):
            if op == operation:
                del self.faults[index]
                if isinstance(fault, HttpResponse):
                    return fault
                if callable(fault) and not isinstance(fault, BaseException):
                    fault = fault()
                if isinstance(fault, BaseException):
                    raise fault
                return fault
        return getattr(self, "_" + operation)(request)

    # -- routing ------------------------------------------------------------

    @staticmethod
    def _operation(request):
        path = urlsplit(request.url).path
        if "/PhoneNumbers/" in path:
            return "lookup"
        if path.endswith("/Messages.json"):
            return "send" if request.method == "POST" else "list"
        if "/Messages/" in path:
            return "update" if request.method == "POST" else "fetch"
        return "unknown"

    def _fields(self, request):
        body = request.body
        assert isinstance(body, FormBody), body
        return {k: (v if isinstance(v, str) else v[0]) for k, v in body.fields.items()}

    def _store(self, to, body, status, from_, service_sid, created=None):
        sid = "SM%032d" % next(self._ids)
        now = created or datetime.now(timezone.utc)
        message = {
            "sid": sid,
            "account_sid": ACCOUNT_SID,
            "to": to,
            "from": from_,
            "body": body,
            "status": status,
            "messaging_service_sid": service_sid,
            "date_created": _rfc2822(now),
            "date_updated": _rfc2822(now),
            "date_sent": None if status in ("scheduled", "accepted", "queued") else _rfc2822(now),
            "error_code": 30032 if status == "undelivered" else None,
            "error_message": None,
            "num_segments": "1",
            "num_media": "0",
            "direction": "outbound-api",
            "api_version": "2010-04-01",
            "price": None,
            "price_unit": "USD",
            "uri": "/2010-04-01/Accounts/%s/Messages/%s.json" % (ACCOUNT_SID, sid),
            "subresource_uris": {},
        }
        self.messages[sid] = message
        return message

    def _lookup(self, request):
        raw = unquote(urlsplit(request.url).path.rsplit("/", 1)[1])
        record = self.numbers.get(raw)
        if record is None:
            return _json(404, {"code": 20404, "message": "The requested resource was not found"})
        return _json(200, record)

    def _send(self, request):
        fields = self._fields(request)
        status = "scheduled" if fields.get("ScheduleType") == "fixed" else self.send_status
        message = self._store(
            fields["To"],
            fields.get("Body"),
            status,
            fields.get("From"),
            fields.get("MessagingServiceSid"),
        )
        return _json(201, message)

    def _sid(self, request):
        return urlsplit(request.url).path.rsplit("/", 1)[1].removesuffix(".json")

    def _fetch(self, request):
        message = self.messages.get(self._sid(request))
        if message is None:
            return _json(404, {"code": 20404, "message": "not found"})
        return _json(200, message)

    def _update(self, request):
        message = self.messages.get(self._sid(request))
        if message is None:
            return _json(404, {"code": 20404, "message": "not found"})
        fields = self._fields(request)
        if fields.get("Status") == "canceled":
            if message["status"] != "scheduled":
                return _json(400, {"code": 30409, "message": "Message cannot be canceled"})
            message["status"] = "canceled"
        if "Body" in fields:
            if fields["Body"] != "":
                return _json(400, {"code": 21218, "message": "Invalid body"})
            message["body"] = ""
        return _json(200, message)

    def _list(self, request):
        query = parse_qs(urlsplit(request.url).query)
        items = sorted(self.messages.values(), key=lambda m: m["sid"], reverse=True)
        if "From" in query:
            items = [m for m in items if m["from"] == query["From"][0]]
        if "To" in query:
            items = [m for m in items if m["to"] == query["To"][0]]
        size = self.page_size_override or int(query.get("PageSize", ["50"])[0])
        start = int(query.get("PageToken", ["0"])[0] or 0)
        page_items = items[start : start + size]
        next_uri = None
        if start + size < len(items):
            params = {k: v[0] for k, v in query.items()}
            params.update(PageToken=str(start + size), Page=str(start // size + 1))
            next_uri = "/2010-04-01/Accounts/%s/Messages.json?%s" % (
                ACCOUNT_SID,
                "&".join("%s=%s" % (k, v) for k, v in params.items()),
            )
        return _json(
            200,
            {
                "messages": page_items,
                "next_page_uri": next_uri,
                "page": start // size,
                "page_size": size,
                "start": start,
                "end": start + len(page_items),
                "uri": "/x",
                "first_page_uri": "/x",
                "previous_page_uri": None,
            },
        )


def fake_client(fake):
    return TwilioSdkClient(
        account_sid_auth_token={"username": ACCOUNT_SID, "password": "test-token-not-a-secret"},
        custom_http_client=fake,
    )


NEVER_SENT = httpx.ConnectError("refused")
NO_REPLY = httpx.ReadTimeout("no reply")
