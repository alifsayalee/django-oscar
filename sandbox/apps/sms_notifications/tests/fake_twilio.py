"""
An in-memory stand-in for the parts of Twilio this app uses, at the SDK's transport seam.

It satisfies ``twilio_sdk.core.HttpClient`` (``send`` + ``close``), so the real client, request building, auth and
decoding all run; only the network is replaced. It keeps message state so writes, lookups, call-offs, redactions and
paged listings behave like a provider would, and it can be told to fail the next write in each of the ways that
matter (never sent, refused, landed-but-no-answer, not-landed-and-no-answer).
"""
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import httpx
from twilio_sdk.core import FormBody, HttpRequest, HttpResponse

CANADIAN_AREA_CODES = ("416", "647", "604")
FINAL = {"delivered", "undelivered", "failed", "canceled", "sent", "read"}


def rfc2822(when):
    return format_datetime(when.astimezone(timezone.utc), usegmt=True)


class FakeTwilio:
    def __init__(self, account_sid="AC" + "0" * 32):
        self.account_sid = account_sid
        self.messages = {}  # sid -> dict, insertion ordered (oldest first)
        self.requests = []
        self.failures = []  # queued failure modes for the next create/update
        self.undeliverable_prefixes = ("+1",)  # the account cannot deliver to US numbers
        self.auth_fails = False

    # -- helpers for tests --
    def fail_next(self, mode):
        self.failures.append(mode)

    def add_foreign(self, to, from_, status="delivered", sent_at=None, body="not ours"):
        """A message on the account that this app did not send."""
        sid = "SM" + secrets.token_hex(16)
        when = sent_at or datetime.now(timezone.utc)
        self.messages[sid] = self._record(sid, to, from_, body, status, when, when)
        return sid

    def deliver(self, sid, status=None, sent_at=None):
        msg = self.messages[sid]
        to = msg["to"]
        us = to.startswith(self.undeliverable_prefixes) and to[2:5] not in CANADIAN_AREA_CODES
        msg["status"] = status or ("undelivered" if us else "delivered")
        if msg["status"] == "undelivered":
            msg["error_code"] = 30032
        when = sent_at or datetime.now(timezone.utc)
        msg["date_sent"] = rfc2822(when)
        msg["date_updated"] = rfc2822(when)

    def creates(self):
        return [r for r in self.requests if r.method == "POST" and r.url.split("?")[0].endswith("/Messages.json")]

    def updates(self):
        return [r for r in self.requests if r.method == "POST" and re.search(r"/Messages/SM\w+\.json$", r.url)]

    # -- HttpClient protocol --
    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if self.auth_fails:
            return self._json(401, {"code": 20003, "message": "Authenticate", "status": 401})
        parts = urlsplit(request.url)
        path = unquote(parts.path)
        query = parse_qs(parts.query)
        fields = dict(request.body.fields) if isinstance(request.body, FormBody) else {}

        if path.startswith("/v2/PhoneNumbers/"):
            return self._lookup(path[len("/v2/PhoneNumbers/"):])
        base = "/2010-04-01/Accounts/%s/Messages" % self.account_sid
        if path == base + ".json" and request.method == "POST":
            return self._create(fields)
        if path == base + ".json" and request.method == "GET":
            return self._list(query)
        match = re.fullmatch(re.escape(base) + r"/(SM\w+)\.json", path)
        if match and request.method == "GET":
            msg = self.messages.get(match.group(1))
            return self._json(200, msg) if msg else self._not_found()
        if match and request.method == "POST":
            return self._update(match.group(1), fields)
        return self._not_found()

    def close(self):
        pass

    # -- endpoints --
    def _lookup(self, raw):
        digits = re.sub(r"\D", "", raw)
        if raw.strip().startswith("+") and 8 <= len(digits) <= 15:
            canonical = "+" + digits
            country = "CA" if canonical.startswith("+1") and canonical[2:5] in CANADIAN_AREA_CODES else (
                "US" if canonical.startswith("+1") else None)
            return self._json(200, {"phone_number": canonical, "valid": True, "validation_errors": [],
                                    "country_code": country, "calling_country_code": canonical[1:2]})
        return self._json(200, {"phone_number": raw, "valid": False,
                                "validation_errors": ["TOO_SHORT" if len(digits) < 8 else "INVALID_COUNTRY_CODE"]})

    def _take_failure(self):
        return self.failures.pop(0) if self.failures else None

    def _create(self, fields):
        failure = self._take_failure()
        if failure == "connect_error":
            raise httpx.ConnectError("refused")
        if failure == "timeout_not_landed":
            raise httpx.ReadTimeout("no reply")
        if failure == "http_400":
            return self._json(400, {"code": 21211, "message": "Invalid 'To' Phone Number", "status": 400})
        if failure == "http_500":
            return self._json(500, {"code": 20500, "message": "Internal error", "status": 500})
        now = datetime.now(timezone.utc)
        sid = "SM" + secrets.token_hex(16)
        scheduled = fields.get("ScheduleType") == "fixed"
        msg = self._record(sid, fields["To"], fields.get("From"), fields.get("Body", ""),
                           "scheduled" if scheduled else "queued", now, None)
        msg["messaging_service_sid"] = fields.get("MessagingServiceSid")
        msg["send_at"] = fields.get("SendAt")
        self.messages[sid] = msg
        if failure == "timeout_landed":
            raise httpx.ReadTimeout("no reply")
        if failure == "http_500_landed":
            return self._json(500, {"code": 20500, "message": "Internal error", "status": 500})
        if failure == "truncated":
            return self._json(201, {})
        return self._json(201, msg)

    def _update(self, sid, fields):
        msg = self.messages.get(sid)
        failure = self._take_failure()
        if failure == "timeout_landed" and msg is not None:
            self._apply_update(msg, fields)
            raise httpx.ReadTimeout("no reply")
        if msg is None:
            return self._not_found()
        return self._apply_update(msg, fields)

    def _apply_update(self, msg, fields):
        now = rfc2822(datetime.now(timezone.utc))
        if fields.get("Status") == "canceled":
            if msg["status"] != "scheduled" and msg["status"] != "canceled":
                return self._json(400, {"code": 30409, "message": "Message cannot be canceled", "status": 400})
            msg["status"] = "canceled"
        if "Body" in fields:
            if fields["Body"] != "":
                return self._json(400, {"code": 20001, "message": "Body can only be redacted", "status": 400})
            if msg["status"] not in FINAL:
                return self._json(400, {"code": 20009, "message": "Cannot redact a message in progress",
                                        "status": 400})
            msg["body"] = ""
        msg["date_updated"] = now
        return self._json(200, msg)

    def _list(self, query):
        def one(name):
            return query.get(name, [None])[0]

        to, from_ = one("To"), one("From")
        after, before = one("DateSent>"), one("DateSent<")
        page_size = int(one("PageSize") or 50)
        offset = int(one("PageToken") or 0)
        rows = list(reversed(self.messages.values()))  # newest first
        if to:
            rows = [m for m in rows if m["to"] == to]
        if from_:
            rows = [m for m in rows if m["from"] == from_]
        if after or before:
            rows = [m for m in rows if m["date_sent"]]
            if after:
                lo = datetime.fromisoformat(after.replace("Z", "+00:00"))
                rows = [m for m in rows if self._when(m["date_sent"]) >= lo]
            if before:
                hi = datetime.fromisoformat(before.replace("Z", "+00:00"))
                rows = [m for m in rows if self._when(m["date_sent"]) <= hi]
        chunk = rows[offset:offset + page_size]
        page = offset // page_size
        next_uri = None
        if offset + page_size < len(rows):
            q = {k: v[0] for k, v in query.items()}
            q.update({"Page": str(page + 1), "PageToken": str(offset + page_size)})
            next_uri = "/2010-04-01/Accounts/%s/Messages.json?%s" % (self.account_sid, urlencode(q))
        return self._json(200, {"messages": chunk, "next_page_uri": next_uri, "page": page, "page_size": page_size})

    # -- plumbing --
    @staticmethod
    def _when(value):
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(value)

    def _record(self, sid, to, from_, body, status, created, sent):
        return {
            "sid": sid, "account_sid": self.account_sid, "to": to, "from": from_, "body": body, "status": status,
            "direction": "outbound-api", "date_created": rfc2822(created), "date_updated": rfc2822(created),
            "date_sent": rfc2822(sent) if sent else None, "error_code": None, "error_message": None,
            "num_segments": "1",
        }

    @staticmethod
    def _json(status, body):
        return HttpResponse(status_code=status, headers={"content-type": "application/json"},
                            content=json.dumps(body).encode())

    def _not_found(self):
        return self._json(404, {"code": 20404, "message": "The requested resource was not found", "status": 404})


def in_days(days):
    return datetime.now(timezone.utc) + timedelta(days=days)
