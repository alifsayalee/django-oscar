"""
Tests for SMS order notifications.

The Twilio SDK client is real; only its transport is replaced by ``FakeTwilio``,
which answers the handful of routes this app calls. Run from ``sandbox/``:

    ../venv/Scripts/python -m pytest apps/sms_notifications/tests.py --ds=settings
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from email.utils import format_datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.test.utils import override_settings
from oscar.test.factories import create_product
from twilio_sdk.core import FormBody, HttpRequest, HttpResponse

from apps.sms_notifications import services
from apps.sms_notifications.models import ContactNumber, Notification, Outcome, ProviderAction
from apps.sms_notifications.safe_write import (
    answer_status,
    cancel_outcome,
    delivery_outcome,
    schedule_outcome,
)
from apps.sms_notifications.twilio_client import build_client, mask_url, use_client

ACCOUNT = "ACtest00000000000000000000000000"
FROM = "+15005550006"
SERVICE = "MGtest00000000000000000000000000"
CANADA = "+14165550199"
UNREACHABLE = "+12025550143"

TWILIO_SETTINGS = dict(
    TWILIO_ACCOUNT_SID=ACCOUNT,
    TWILIO_AUTH_TOKEN="test-token",
    TWILIO_FROM_NUMBER=FROM,
    TWILIO_MESSAGING_SERVICE_SID=SERVICE,
    TWILIO_BASE_URL=None,
    SMS_REFERENCE_PREFIX="test-install",
)


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


class FakeTwilio:
    """A tiny in-memory Twilio: messages and lookups, plus injectable failures."""

    def __init__(self) -> None:
        self.requests: list[HttpRequest] = []
        self.messages: dict[str, dict[str, Any]] = {}
        self.valid_numbers = {CANADA: "CA", UNREACHABLE: "US"}
        # Called before a route is served; may raise or return a response.
        self.interceptors: list[Callable[[HttpRequest], HttpResponse | None]] = []
        self.create_status = "queued"
        self._next = 1

    # transport protocol
    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        for intercept in list(self.interceptors):
            response = intercept(request)
            if response is not None:
                return response
        return self.route(request)

    def close(self) -> None:
        pass

    # helpers
    def form(self, request: HttpRequest) -> dict[str, str]:
        assert isinstance(request.body, FormBody)
        return {k: v if isinstance(v, str) else v[0] for k, v in request.body.fields.items()}

    def writes(self, path_part: str = "/Messages") -> list[HttpRequest]:
        return [r for r in self.requests if r.method == "POST" and path_part in r.url]

    def creates(self) -> list[HttpRequest]:
        return [r for r in self.requests if r.method == "POST" and r.url.endswith("/Messages.json")]

    def route(self, request: HttpRequest) -> HttpResponse:
        url = urlsplit(request.url)
        path = url.path
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        m = re.fullmatch(r"/v2/PhoneNumbers/(.+)", path)
        if m and request.method == "GET":
            number = unquote(m.group(1)).replace(" ", "")
            country = self.valid_numbers.get(number)
            return json_response(
                200,
                {
                    "phone_number": number if country else None,
                    "country_code": country,
                    "valid": bool(country),
                    "validation_errors": [] if country else ["NOT_A_NUMBER"],
                },
            )
        base = "/2010-04-01/Accounts/%s/Messages" % ACCOUNT
        if path == base + ".json" and request.method == "POST":
            form = self.form(request)
            sid = "SM%030d" % self._next
            self._next += 1
            scheduled = form.get("ScheduleType") == "fixed"
            msg = {
                "sid": sid,
                "to": form["To"],
                "from": form.get("From"),
                "body": form.get("Body"),
                "status": "scheduled" if scheduled else self.create_status,
                "messaging_service_sid": form.get("MessagingServiceSid"),
                "date_created": format_datetime(datetime.now(dt_timezone.utc), usegmt=True),
                "date_sent": None,
                "error_code": None,
            }
            self.messages[sid] = msg
            return json_response(201, msg)
        m = re.fullmatch(re.escape(base) + r"/(SM\w+)\.json", path)
        if m and m.group(1) in self.messages:
            msg = self.messages[m.group(1)]
            if request.method == "POST":
                form = self.form(request)
                if form.get("Status") == "canceled":
                    if msg["status"] != "scheduled":
                        return json_response(400, {"code": 30409, "message": "cannot cancel"})
                    msg["status"] = "canceled"
                if "Body" in form:
                    msg["body"] = form["Body"]
            return json_response(200, msg)
        if path == base + ".json" and request.method == "GET":
            found = [
                msg
                for msg in self.messages.values()
                if ("To" not in query or msg["to"] == query["To"])
                and ("From" not in query or msg["from"] == query["From"])
            ]
            return json_response(200, {"messages": found, "next_page_uri": None})
        return json_response(404, {"code": 20404, "message": "not found"})

    def deliver(self, sid: str, status: str = "delivered", when: datetime | None = None) -> None:
        when = when or datetime.now(dt_timezone.utc)
        self.messages[sid]["status"] = status
        self.messages[sid]["date_sent"] = format_datetime(when, usegmt=True)


@pytest.fixture
def fake() -> Iterator[FakeTwilio]:
    fake = FakeTwilio()
    with override_settings(**TWILIO_SETTINGS):
        with use_client(build_client(transport=fake)):
            yield fake


@pytest.fixture
def shopper(db: Any) -> User:
    return User.objects.create_user("shopper", "shopper@example.com", "pass-word-1")


@pytest.fixture
def operator(db: Any) -> User:
    return User.objects.create_user("operator", "op@example.com", "pass-word-1", is_staff=True)


@pytest.fixture
def product(db: Any) -> Any:
    return create_product(price=Decimal("12.50"), num_in_stock=100)


def api(user: User | None) -> Client:
    client = Client()
    if user is not None:
        client.force_login(user)
    return client


def post(client: Client, url: str, body: object | None = None, key: str | None = None) -> Any:
    headers = {"Idempotency-Key": key} if key else None
    return client.post(url, json.dumps(body or {}), content_type="application/json", headers=headers)


def register(user: User, number: str = CANADA) -> Any:
    return post(api(user), "/api/contact-numbers", {"phoneNumber": number})


def place(user: User, product: Any) -> Any:
    return post(api(user), "/api/orders", {"lines": [{"productId": product.pk, "quantity": 2}]})


# --- outcome mapping ----------------------------------------------------------


def test_status_mappers_never_count_unfinished_or_unlisted_as_done() -> None:
    assert delivery_outcome("delivered") == Outcome.DONE
    assert delivery_outcome("sent") == Outcome.PENDING
    assert delivery_outcome("undelivered") == Outcome.FAILED
    assert delivery_outcome("something_new") == Outcome.UNKNOWN
    assert schedule_outcome("scheduled") == Outcome.DONE
    assert schedule_outcome("delivered") == Outcome.UNKNOWN
    assert cancel_outcome("canceled") == Outcome.DONE
    assert cancel_outcome("delivered") == Outcome.FAILED
    assert cancel_outcome("scheduled") == Outcome.PENDING
    for outcome in ("pending", "sending", "failed", "needs_review", "unknown", "other"):
        assert answer_status(outcome) not in (200, 201, 204)


def test_logged_urls_mask_phone_numbers() -> None:
    assert "4165550199" not in mask_url("https://lookups.twilio.com/v2/PhoneNumbers/%2B14165550199")


# --- Flow 1: contact numbers --------------------------------------------------


def test_registering_stores_the_providers_canonical_form(fake: FakeTwilio, shopper: User) -> None:
    response = register(shopper, "+1 416 555 0199")
    assert response.status_code == 201
    body = response.json()
    assert body["phoneNumber"] == CANADA
    assert isinstance(body["contactNumberId"], int)
    assert fake.requests[0].url.startswith("https://lookups.twilio.com/v2/PhoneNumbers/")


def test_an_unusable_number_is_rejected_at_registration(fake: FakeTwilio, shopper: User) -> None:
    response = register(shopper, "+15551234")
    assert response.status_code == 422
    assert not ContactNumber.objects.exists()


def test_numbers_are_private_to_their_owner(fake: FakeTwilio, shopper: User, operator: User) -> None:
    contact_id = register(shopper).json()["contactNumberId"]
    assert api(operator).get("/api/contact-numbers").json()["contactNumbers"] == []
    assert api(operator).delete("/api/contact-numbers/%s" % contact_id).status_code == 404
    assert api(None).get("/api/contact-numbers").status_code == 401
    assert api(shopper).delete("/api/contact-numbers/%s" % contact_id).status_code == 200
    assert api(shopper).get("/api/contact-numbers").json()["contactNumbers"] == []


# --- Flow 2: orders -----------------------------------------------------------


def test_placing_an_order_texts_the_shopper(fake: FakeTwilio, shopper: User, product: Any) -> None:
    register(shopper)
    response = place(shopper, product)
    assert response.status_code == 201
    body = response.json()
    assert body["orderId"]
    assert body["notification"]["sendOutcome"] == Outcome.PENDING  # queued is not delivered
    [create] = fake.creates()
    form = fake.form(create)
    assert form["To"] == CANADA and form["From"] == FROM
    ref = services.reference("order", body["orderId"], Notification.PLACED)
    assert ref.startswith("test-install:")
    assert form["Body"].endswith("Ref " + services.reference_code(ref))


def test_a_shopper_without_a_number_is_not_messaged(fake: FakeTwilio, shopper: User, product: Any) -> None:
    response = place(shopper, product)
    assert response.status_code == 201
    assert response.json()["notification"] is None
    assert fake.creates() == []


def test_a_refused_message_does_not_fail_the_order(fake: FakeTwilio, shopper: User, product: Any) -> None:
    register(shopper)
    fake.interceptors.append(
        lambda r: json_response(400, {"code": 21211, "message": "invalid"})
        if r.method == "POST" and r.url.endswith("/Messages.json")
        else None
    )
    response = place(shopper, product)
    assert response.status_code == 201
    assert response.json()["notification"]["sendOutcome"] == Outcome.FAILED


def test_orders_are_private_to_their_owner(fake: FakeTwilio, shopper: User, operator: User, product: Any) -> None:
    order_id = place(shopper, product).json()["orderId"]
    stranger = User.objects.create_user("stranger", "s@example.com", "pass-word-1")
    assert api(stranger).get("/api/orders/%s/notifications" % order_id).status_code == 404
    assert api(stranger).get("/api/my-orders").json()["orders"] == []
    assert post(api(shopper), "/api/orders/%s/dispatch" % order_id).status_code == 403
    assert api(operator).get("/api/orders/%s/notifications" % order_id).status_code == 200


def test_dispatch_queues_the_follow_up_with_the_provider(
    fake: FakeTwilio, shopper: User, operator: User, product: Any
) -> None:
    register(shopper)
    order_id = place(shopper, product).json()["orderId"]
    response = post(api(operator), "/api/orders/%s/dispatch" % order_id)
    assert response.status_code == 200
    assert response.json()["dispatched"] is True
    placed, on_its_way, follow_up = fake.creates()
    form = fake.form(follow_up)
    assert form["ScheduleType"] == "fixed"
    assert form["MessagingServiceSid"] == SERVICE
    assert form["From"] == FROM
    send_at = datetime.fromisoformat(form["SendAt"].replace("Z", "+00:00"))
    assert send_at > datetime.now(dt_timezone.utc) + timedelta(days=2)
    kinds = {n["kind"]: n for n in response.json()["notifications"]}
    assert kinds["follow_up"]["sendOutcome"] == Outcome.DONE  # scheduled = what was asked

    # A repeated dispatch sends nothing new.
    post(api(operator), "/api/orders/%s/dispatch" % order_id)
    assert len(fake.creates()) == 3


def test_cancel_calls_off_the_queued_follow_up(
    fake: FakeTwilio, shopper: User, operator: User, product: Any
) -> None:
    register(shopper)
    order_id = place(shopper, product).json()["orderId"]
    post(api(operator), "/api/orders/%s/dispatch" % order_id)
    follow_up = Notification.objects.get(kind=Notification.FOLLOW_UP)

    response = post(api(operator), "/api/orders/%s/cancel" % order_id)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "Cancelled"
    assert body["followUpCancellations"] == [
        {"notificationId": follow_up.pk, "outcome": Outcome.DONE, "detail": None}
    ]
    assert fake.messages[follow_up.provider_sid]["status"] == "canceled"
    cancel = [r for r in fake.writes("/Messages/SM") if fake.form(r).get("Status") == "canceled"]
    assert len(cancel) == 1
    # Cancelling again makes no second cancel call; the follow-up cannot be resent.
    post(api(operator), "/api/orders/%s/cancel" % order_id)
    assert len([r for r in fake.writes("/Messages/SM") if fake.form(r).get("Status")]) == 1
    resend = post(
        api(operator), "/api/notifications/%s/resend" % follow_up.pk, key="k1"
    )
    assert resend.status_code == 409


def test_a_follow_up_that_already_went_out_is_reported_too_late(
    fake: FakeTwilio, shopper: User, operator: User, product: Any
) -> None:
    register(shopper)
    order_id = place(shopper, product).json()["orderId"]
    post(api(operator), "/api/orders/%s/dispatch" % order_id)
    follow_up = Notification.objects.get(kind=Notification.FOLLOW_UP)
    fake.deliver(follow_up.provider_sid, "sent")

    body = post(api(operator), "/api/orders/%s/cancel" % order_id).json()
    assert body["status"] == "Cancelled"
    assert body["followUpCancellations"][0]["outcome"] == Outcome.FAILED
    # Reading the order back neither loops nor keeps retrying the refused cancel.
    cancels_before = len([r for r in fake.writes("/Messages/SM") if fake.form(r).get("Status")])
    Notification.objects.update(provider_checked_at=None)
    assert api(shopper).get("/api/orders/%s/notifications" % order_id).status_code == 200
    cancels_after = len([r for r in fake.writes("/Messages/SM") if fake.form(r).get("Status")])
    assert cancels_after == cancels_before


def test_a_follow_up_still_being_queued_is_called_off_when_it_resolves(
    fake: FakeTwilio, shopper: User, operator: User, product: Any
) -> None:
    register(shopper)
    order_id = place(shopper, product).json()["orderId"]
    # The scheduled create times out after reaching the provider...
    def time_out_after_landing(request: HttpRequest) -> HttpResponse | None:
        if request.method == "POST" and request.url.endswith("/Messages.json"):
            if fake.form(request).get("ScheduleType") == "fixed":
                fake.route(request)
                raise httpx.ReadTimeout("no reply")
        return None

    fake.interceptors.append(time_out_after_landing)
    # ...and the lookup does not see it yet.
    fake.interceptors.append(
        lambda r: json_response(200, {"messages": []})
        if r.method == "GET" and r.url.split("?")[0].endswith("/Messages.json")
        else None
    )
    post(api(operator), "/api/orders/%s/dispatch" % order_id)
    follow_up = Notification.objects.get(kind=Notification.FOLLOW_UP)
    assert follow_up.outcome == Outcome.UNKNOWN

    cancelled = post(api(operator), "/api/orders/%s/cancel" % order_id).json()
    assert cancelled["followUpCancellations"][0]["outcome"] == Outcome.UNKNOWN

    # Once the lookup can see it, the next check finds it and calls it off.
    fake.interceptors.clear()
    api(shopper).get("/api/orders/%s/notifications" % order_id)
    [queued] = [m for m in fake.messages.values() if m["messaging_service_sid"]]
    assert queued["status"] == "canceled"
    follow_up.refresh_from_db()
    assert follow_up.provider_sid == queued["sid"]
    assert len([r for r in fake.creates() if fake.form(r).get("ScheduleType")]) == 1


def test_my_orders_reports_delivery_from_the_provider(
    fake: FakeTwilio, shopper: User, product: Any
) -> None:
    register(shopper)
    place(shopper, product)
    sid = Notification.objects.get().provider_sid
    fake.deliver(sid)
    Notification.objects.update(provider_checked_at=None)
    [order] = api(shopper).get("/api/my-orders").json()["orders"]
    assert order["notifications"][0]["deliveryOutcome"] == Outcome.DONE
    assert order["notifications"][0]["providerStatus"] == "delivered"
    assert order["notifications"][0]["sendOutcome"] == Outcome.DONE


# --- the safe write -----------------------------------------------------------


def test_a_send_that_timed_out_after_landing_is_found_not_resent(
    fake: FakeTwilio, shopper: User, product: Any
) -> None:
    register(shopper)

    def land_then_time_out(request: HttpRequest) -> HttpResponse | None:
        if request.method == "POST" and request.url.endswith("/Messages.json"):
            fake.route(request)
            raise httpx.ReadTimeout("no reply")
        return None

    fake.interceptors.append(land_then_time_out)
    body = place(shopper, product).json()
    assert body["notification"]["sendOutcome"] == Outcome.PENDING
    assert body["notification"]["messageSid"] in fake.messages
    assert len(fake.creates()) == 1


def test_never_sent_and_maybe_sent_are_different_outcomes(
    fake: FakeTwilio, shopper: User, product: Any
) -> None:
    register(shopper)

    def fail_with(error: Exception) -> Callable[[HttpRequest], HttpResponse | None]:
        def intercept(request: HttpRequest) -> HttpResponse | None:
            if request.method == "POST" and request.url.endswith("/Messages.json"):
                raise error
            if request.method == "GET" and "/Messages.json" in request.url:
                return json_response(200, {"messages": []})
            return None

        return intercept

    fake.interceptors.append(fail_with(httpx.ConnectError("refused")))
    unsent = place(shopper, product).json()["notification"]
    fake.interceptors[:] = [fail_with(httpx.ReadTimeout("no reply"))]
    maybe = place(shopper, product).json()["notification"]
    assert unsent["sendOutcome"] == Outcome.FAILED
    assert maybe["sendOutcome"] == Outcome.UNKNOWN
    lookups = [r for r in fake.requests if r.method == "GET" and "/Messages.json" in r.url]
    assert len(lookups) == 1  # only the maybe-sent one was looked up


def test_a_truncated_answer_is_checked_by_lookup(fake: FakeTwilio, shopper: User, product: Any) -> None:
    register(shopper)

    def truncated(request: HttpRequest) -> HttpResponse | None:
        if request.method == "POST" and request.url.endswith("/Messages.json"):
            fake.route(request)
            return json_response(201, {})
        return None

    fake.interceptors.append(truncated)
    notification = place(shopper, product).json()["notification"]
    assert notification["messageSid"] in fake.messages
    assert len(fake.creates()) == 1


# --- Flow 3: operator actions -------------------------------------------------


def undelivered_notification(fake: FakeTwilio, shopper: User, product: Any) -> Notification:
    register(shopper, UNREACHABLE)
    place(shopper, product)
    notification = Notification.objects.get()
    fake.deliver(notification.provider_sid, "undelivered")
    return notification


def test_resend_is_idempotent_per_key(fake: FakeTwilio, shopper: User, operator: User, product: Any) -> None:
    original = undelivered_notification(fake, shopper, product)
    url = "/api/notifications/%s/resend" % original.pk
    first = post(api(operator), url, key="key-1")
    assert first.status_code == 202  # queued: accepted, not delivered
    again = post(api(operator), url, key="key-1")
    assert again.json()["notificationId"] == first.json()["notificationId"]
    assert len(fake.creates()) == 2  # the order message + one resend
    second = post(api(operator), url, {"idempotencyKey": "key-2"})
    assert second.json()["notificationId"] != first.json()["notificationId"]
    assert len(fake.creates()) == 3
    assert post(api(shopper), url, key="key-3").status_code == 403
    assert post(api(operator), url).status_code == 400


def test_a_delivered_message_is_not_resent(fake: FakeTwilio, shopper: User, operator: User, product: Any) -> None:
    register(shopper)
    place(shopper, product)
    notification = Notification.objects.get()
    fake.deliver(notification.provider_sid)
    response = post(
        api(operator), "/api/notifications/%s/resend" % notification.pk, key="k"
    )
    assert response.status_code == 409


def test_content_disposal_redacts_at_the_provider_and_keeps_the_record(
    fake: FakeTwilio, shopper: User, operator: User, product: Any
) -> None:
    register(shopper)
    place(shopper, product)
    notification = Notification.objects.get()
    fake.deliver(notification.provider_sid)
    response = api(operator).delete("/api/notifications/%s/content" % notification.pk)
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == Outcome.DONE and body["text"] is None
    assert body["messageSid"] == notification.provider_sid
    assert fake.messages[notification.provider_sid]["body"] == ""
    redact = [r for r in fake.writes("/Messages/SM") if "Body" in fake.form(r)]
    assert fake.form(redact[0])["Body"] == ""
    assert ProviderAction.objects.get().outcome == Outcome.DONE
    assert api(shopper).delete("/api/notifications/%s/content" % notification.pk).status_code == 403


def test_removing_a_number_calls_off_its_queued_follow_up(
    fake: FakeTwilio, shopper: User, operator: User, product: Any
) -> None:
    contact_id = register(shopper).json()["contactNumberId"]
    order_id = place(shopper, product).json()["orderId"]
    post(api(operator), "/api/orders/%s/dispatch" % order_id)
    follow_up = Notification.objects.get(kind=Notification.FOLLOW_UP)
    response = api(shopper).delete("/api/contact-numbers/%s" % contact_id)
    assert response.json()["followUpCancellations"][0]["outcome"] == Outcome.DONE
    assert fake.messages[follow_up.provider_sid]["status"] == "canceled"


def test_reconciliation_asks_for_our_number_and_lines_both_sides_up(
    fake: FakeTwilio, shopper: User, operator: User, product: Any
) -> None:
    register(shopper)
    place(shopper, product)
    ours = Notification.objects.get()
    now = datetime.now(dt_timezone.utc)
    fake.deliver(ours.provider_sid, when=now)
    # A message from our number that this app has no record of.
    fake.messages["SMforeign"] = {
        "sid": "SMforeign", "to": CANADA, "from": FROM, "body": "x", "status": "delivered",
        "date_sent": format_datetime(now, usegmt=True), "messaging_service_sid": None,
    }
    start = (now - timedelta(hours=1)).isoformat()
    end = (now + timedelta(hours=1)).isoformat()
    response = api(operator).get(
        "/api/notifications/reconciliation", {"from": start, "to": end}
    )
    assert response.status_code == 200
    report = response.json()
    assert [m["notificationId"] for m in report["matched"]] == [ours.pk]
    assert [m["messageSid"] for m in report["providerOnly"]] == ["SMforeign"]
    assert report["localOnly"] == []
    [listing] = [r for r in fake.requests if r.method == "GET" and "DateSent" in r.url]
    query = parse_qs(urlsplit(listing.url).query)
    assert query["From"] == [FROM]
    assert "DateSent>" in query and "DateSent<" in query
    assert api(shopper).get("/api/notifications/reconciliation", {"from": start, "to": end}).status_code == 403


def test_base_url_override_applies_to_messages_only(shopper: User, product: Any) -> None:
    fake = FakeTwilio()
    with override_settings(**{**TWILIO_SETTINGS, "TWILIO_BASE_URL": "http://mock.local:9999"}):
        with use_client(build_client(transport=fake)):
            register(shopper)
            place(shopper, product)
    lookup, create = fake.requests[0], fake.creates()[0]
    assert lookup.url.startswith("https://lookups.twilio.com/")
    assert create.url.startswith("http://mock.local:9999/2010-04-01/Accounts/")
