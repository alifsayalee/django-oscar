"""
Tests for SMS order notifications. The Twilio SDK client is real; only its
transport is replaced, so every request is built by the SDK and asserted here.
Numbers are fictitious (555-01xx) and nothing leaves the process.

Run from sandbox/:  python manage.py test apps.sms_notifications
"""

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from oscar.core.loading import get_model
from oscar.test.factories import create_product
from twilio_sdk.core import FormBody, HttpRequest, HttpResponse

from . import gateway
from .models import ContactNumber, Notification
from .safe_write import ref_token

Order = get_model("order", "Order")
Outcome = Notification.Outcome

SHOPPER_NUMBER = "+15550100001"
OTHER_NUMBER = "+15550100002"
FROM_NUMBER = "+15550100099"

TEST_SETTINGS = {
    "TWILIO_ACCOUNT_SID": "ACtest",
    "TWILIO_AUTH_TOKEN": "test-token",
    "TWILIO_FROM_NUMBER": FROM_NUMBER,
    "TWILIO_MESSAGING_SERVICE_SID": "MGtest",
    "TWILIO_BASE_URL": "",
    "SMS_NOTIFICATIONS_INSTALL_ID": "test",
}


class StubTransport:
    """Satisfies the SDK's sync transport protocol; answers queued responses (or raises queued errors)."""

    def __init__(self, *answers: HttpResponse | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[HttpRequest] = []

    def queue(self, *answers: HttpResponse | Exception) -> None:
        self.answers.extend(answers)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self.answers:
            raise AssertionError(f"unexpected provider call: {request.method} {request.url}")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self) -> None:
        pass


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(status_code=status, headers={"content-type": "application/json"}, content=json.dumps(body).encode())


def message(sid: str, status: str, to: str = SHOPPER_NUMBER, body: str = "hello", **extra: Any) -> dict[str, Any]:
    return {
        "sid": sid,
        "status": status,
        "to": to,
        "from": FROM_NUMBER,
        "body": body,
        "direction": "outbound-api",
        "date_created": "Thu, 24 Sep 2026 10:00:00 +0000",
        "date_sent": None,
        "error_code": None,
        "error_message": None,
        **extra,
    }


def listing(*messages: dict[str, Any]) -> dict[str, Any]:
    return {"messages": list(messages), "next_page_uri": None, "page": 0, "page_size": 100}


def form(request: HttpRequest) -> dict[str, Any]:
    assert isinstance(request.body, FormBody)
    return dict(request.body.fields)


def query(request: HttpRequest) -> dict[str, list[str]]:
    return parse_qs(urlsplit(request.url).query)


@override_settings(**TEST_SETTINGS)
class ApiTestCase(TestCase):
    def setUp(self) -> None:
        self.transport = StubTransport()
        gateway.set_client(gateway.build_client(transport=self.transport))
        self.addCleanup(gateway.set_client, None)
        User = get_user_model()
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "pass-word-123")
        self.other = User.objects.create_user("other", "other@example.com", "pass-word-123")
        self.staff = User.objects.create_user("operator", "operator@example.com", "pass-word-123", is_staff=True)
        self.product = create_product(price=10, num_in_stock=100)

    def login(self, user: Any) -> None:
        self.client.force_login(user)

    def add_number(self, user: Any, number: str = SHOPPER_NUMBER) -> ContactNumber:
        return ContactNumber.objects.create(user=user, phone_number=number, country_code="US")

    def place_order(self, queued_status: str = "queued") -> tuple[int, Notification]:
        self.transport.queue(json_response(201, message("SMplaced", queued_status)))
        self.login(self.shopper)
        response = self.client.post(
            "/api/orders", {"items": [{"productId": self.product.pk, "quantity": 2}]}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 201, response.content)
        order_id = response.json()["orderId"]
        return order_id, Notification.objects.get(order_id=order_id, kind="placed")


class ContactNumberTests(ApiTestCase):
    def test_stores_the_providers_canonical_form(self) -> None:
        self.transport.queue(json_response(200, {"phone_number": SHOPPER_NUMBER, "country_code": "US", "national_format": "(555) 010-0001"}))
        self.login(self.shopper)
        response = self.client.post(
            "/api/contact-numbers", {"phoneNumber": "(555) 010-0001", "countryCode": "us"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["phoneNumber"], SHOPPER_NUMBER)
        self.assertIn("contactNumberId", response.json())
        request = self.transport.requests[0]
        self.assertTrue(request.url.startswith("https://lookups.twilio.com/v1/PhoneNumbers/"))
        self.assertEqual(query(request)["CountryCode"], ["US"])
        self.assertTrue(request.headers["authorization"].startswith("Basic "))

    def test_rejects_a_number_the_provider_does_not_know(self) -> None:
        self.transport.queue(json_response(404, {"code": 20404, "message": "not found", "status": 404}))
        self.login(self.shopper)
        response = self.client.post("/api/contact-numbers", {"phoneNumber": "+1555"}, content_type="application/json")
        self.assertEqual(response.status_code, 422)
        self.assertFalse(ContactNumber.objects.exists())

    def test_provider_credentials_refused_is_not_the_callers_fault(self) -> None:
        self.transport.queue(json_response(401, {"code": 20003, "message": "auth"}))
        self.login(self.shopper)
        response = self.client.post("/api/contact-numbers", {"phoneNumber": SHOPPER_NUMBER}, content_type="application/json")
        self.assertEqual(response.status_code, 502)

    def test_shoppers_cannot_see_or_delete_each_others_numbers(self) -> None:
        mine = self.add_number(self.shopper)
        self.login(self.other)
        self.assertEqual(self.client.get("/api/contact-numbers").json()["contactNumbers"], [])
        self.assertEqual(self.client.delete(f"/api/contact-numbers/{mine.pk}").status_code, 404)
        self.login(self.shopper)
        self.assertEqual(self.client.delete(f"/api/contact-numbers/{mine.pk}").status_code, 204)
        self.assertEqual(self.client.get("/api/contact-numbers").json()["contactNumbers"], [])

    def test_anonymous_callers_are_refused(self) -> None:
        self.assertEqual(self.client.get("/api/contact-numbers").status_code, 401)


class OrderFlowTests(ApiTestCase):
    def test_placing_an_order_texts_the_shopper(self) -> None:
        self.add_number(self.shopper)
        order_id, n = self.place_order()
        self.assertEqual(Order.objects.get(pk=order_id).lines.get().quantity, 2)
        self.assertEqual((n.outcome, n.provider_sid, n.provider_status), (Outcome.PENDING, "SMplaced", "queued"))
        request = self.transport.requests[0]
        self.assertTrue(request.url.endswith("/2010-04-01/Accounts/ACtest/Messages.json"))
        fields = form(request)
        self.assertEqual((fields["To"], fields["From"]), (SHOPPER_NUMBER, FROM_NUMBER))
        self.assertIn(f"(ref {ref_token(n.reference)})", fields["Body"])

    def test_no_number_on_file_means_no_message(self) -> None:
        self.login(self.shopper)
        response = self.client.post(
            "/api/orders", {"items": [{"productId": self.product.pk, "quantity": 1}]}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(response.json()["notifications"], [])

    def test_unsent_and_unanswered_sends_are_different_outcomes(self) -> None:
        self.add_number(self.shopper)
        self.login(self.shopper)
        # Refused connection: never left -> failed, and no lookup.
        self.transport.queue(httpx.ConnectError("refused"))
        first = self.client.post("/api/orders", {"items": [{"productId": self.product.pk}]}, content_type="application/json")
        self.assertEqual(first.status_code, 201)  # the order is placed regardless
        unsent = Notification.objects.get(order_id=first.json()["orderId"])
        self.assertEqual(len(self.transport.requests), 1)
        # Read timeout: may have landed -> looked up by reference, not found -> unknown.
        self.transport.queue(httpx.ReadTimeout("no reply"), json_response(200, listing()))
        second = self.client.post("/api/orders", {"items": [{"productId": self.product.pk}]}, content_type="application/json")
        self.assertEqual(second.status_code, 201)
        unknown = Notification.objects.get(order_id=second.json()["orderId"])
        self.assertEqual((unsent.outcome, unknown.outcome), (Outcome.FAILED, Outcome.UNKNOWN))
        lookup = self.transport.requests[-1]
        self.assertEqual((lookup.method, query(lookup)["To"]), ("GET", [SHOPPER_NUMBER]))

    def test_unknown_send_is_settled_by_finding_it_not_by_resending(self) -> None:
        self.add_number(self.shopper)
        self.transport.queue(httpx.ReadTimeout("no reply"), json_response(200, listing()))
        self.login(self.shopper)
        response = self.client.post("/api/orders", {"items": [{"productId": self.product.pk}]}, content_type="application/json")
        n = Notification.objects.get(order_id=response.json()["orderId"])
        self.assertEqual(n.outcome, Outcome.UNKNOWN)
        # Later, a read endpoint checks again and finds the message carrying our token.
        found = message("SMlate", "delivered", body=n.body)
        self.transport.queue(json_response(200, listing(found)))
        self.client.get(f"/api/orders/{n.order_id}/notifications")
        n.refresh_from_db()
        self.assertEqual((n.outcome, n.provider_sid), (Outcome.DONE, "SMlate"))
        self.assertFalse(any(r.method == "POST" for r in self.transport.requests[2:]))

    def test_dispatch_twice_sends_each_message_once_and_queues_the_follow_up(self) -> None:
        self.add_number(self.shopper)
        order_id, _ = self.place_order()
        self.transport.queue(json_response(201, message("SMdisp", "queued")), json_response(201, message("SMfollow", "scheduled")))
        self.login(self.staff)
        self.assertEqual(self.client.post(f"/api/orders/{order_id}/dispatch").status_code, 200)
        again = self.client.post(f"/api/orders/{order_id}/dispatch")
        self.assertEqual((again.status_code, again.json()["changed"]), (200, False))
        self.assertEqual(len(self.transport.requests), 3)  # placed + dispatched + follow-up, nothing more
        followup = form(self.transport.requests[2])
        self.assertEqual((followup["ScheduleType"], followup["MessagingServiceSid"]), ("fixed", "MGtest"))
        self.assertIn("SendAt", followup)
        n = Notification.objects.get(order_id=order_id, kind="followup")
        self.assertEqual((n.outcome, n.provider_status), (Outcome.PENDING, "scheduled"))

    def test_cancel_calls_off_the_queued_follow_up(self) -> None:
        self.add_number(self.shopper)
        order_id, _ = self.place_order()
        self.transport.queue(json_response(201, message("SMdisp", "queued")), json_response(201, message("SMfollow", "scheduled")))
        self.login(self.staff)
        self.client.post(f"/api/orders/{order_id}/dispatch")
        self.transport.queue(json_response(200, message("SMfollow", "canceled")), json_response(201, message("SMcanc", "queued")))
        response = self.client.post(f"/api/orders/{order_id}/cancel")
        self.assertEqual(response.status_code, 200)
        cancel_request = self.transport.requests[3]
        self.assertTrue(cancel_request.url.endswith("/Messages/SMfollow.json"))
        self.assertEqual(form(cancel_request), {"Status": "canceled"})
        n = Notification.objects.get(order_id=order_id, kind="followup")
        self.assertEqual((n.cancel_outcome, n.provider_status, n.outcome), (Outcome.DONE, "canceled", Outcome.FAILED))
        self.assertIsNotNone(n.canceled_at)
        self.assertTrue(Notification.objects.filter(order_id=order_id, kind="cancelled").exists())

    def test_follow_up_already_sent_is_reported_not_hidden(self) -> None:
        self.add_number(self.shopper)
        order_id, _ = self.place_order()
        self.transport.queue(json_response(201, message("SMdisp", "queued")), json_response(201, message("SMfollow", "scheduled")))
        self.login(self.staff)
        self.client.post(f"/api/orders/{order_id}/dispatch")
        self.transport.queue(
            json_response(400, {"code": 30409, "message": "cannot cancel"}),
            json_response(200, message("SMfollow", "delivered")),
            json_response(201, message("SMcanc", "queued")),
        )
        self.client.post(f"/api/orders/{order_id}/cancel")
        n = Notification.objects.get(order_id=order_id, kind="followup")
        self.assertEqual(n.cancel_outcome, Outcome.FAILED)

    def test_operator_actions_need_staff(self) -> None:
        self.add_number(self.shopper)
        order_id, _ = self.place_order()
        self.assertEqual(self.client.post(f"/api/orders/{order_id}/dispatch").status_code, 403)
        self.assertEqual(self.client.get("/api/notifications/reconciliation?from=2026-09-01T00:00:00Z&to=2026-09-02T00:00:00Z").status_code, 403)

    def test_shoppers_only_see_their_own_orders(self) -> None:
        self.add_number(self.shopper)
        order_id, _ = self.place_order()
        self.login(self.other)
        self.assertEqual(self.client.get(f"/api/orders/{order_id}/notifications").status_code, 404)
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"], [])


class StatusMappingTests(TestCase):
    def test_only_delivered_states_are_done(self) -> None:
        self.assertEqual(gateway.outcome_of_send("delivered"), Outcome.DONE)
        self.assertEqual(gateway.outcome_of_send("sent"), Outcome.PENDING)
        self.assertEqual(gateway.outcome_of_send("scheduled"), Outcome.PENDING)
        self.assertEqual(gateway.outcome_of_send("undelivered"), Outcome.FAILED)
        self.assertEqual(gateway.outcome_of_send("canceled"), Outcome.FAILED)
        self.assertEqual(gateway.outcome_of_send("something_new"), Outcome.UNKNOWN)
        self.assertEqual(gateway.outcome_of_send(None), Outcome.UNKNOWN)

    def test_cancel_is_done_only_when_canceled(self) -> None:
        self.assertEqual(gateway.outcome_of_cancel("canceled"), Outcome.DONE)
        self.assertEqual(gateway.outcome_of_cancel("sent"), Outcome.FAILED)
        self.assertEqual(gateway.outcome_of_cancel("scheduled"), Outcome.PENDING)
        self.assertEqual(gateway.outcome_of_cancel(None), Outcome.UNKNOWN)

    def test_logged_urls_carry_no_phone_numbers(self) -> None:
        redacted = gateway.redact_url(f"https://lookups.twilio.com/v1/PhoneNumbers/%2B15550100001?To={SHOPPER_NUMBER}")
        self.assertEqual(redacted, "lookups.twilio.com/v1/PhoneNumbers/[redacted]")


class OperatorTests(ApiTestCase):
    def failed_notification(self) -> Notification:
        self.add_number(self.shopper)
        _, n = self.place_order(queued_status="undelivered")
        self.assertEqual(n.outcome, Outcome.FAILED)
        return n

    def test_resend_under_one_key_sends_once_and_a_new_key_sends_again(self) -> None:
        n = self.failed_notification()
        self.login(self.staff)
        self.transport.queue(json_response(201, message("SMre1", "queued")))
        first = self.client.post(f"/api/notifications/{n.pk}/resend", HTTP_IDEMPOTENCY_KEY="key-1")
        repeat = self.client.post(f"/api/notifications/{n.pk}/resend", HTTP_IDEMPOTENCY_KEY="key-1")
        self.assertEqual((first.status_code, repeat.status_code), (202, 202))
        self.assertEqual(first.json()["notificationId"], repeat.json()["notificationId"])
        self.assertTrue(repeat.json()["repeat"])
        self.assertEqual(len(self.transport.requests), 2)  # the order message + ONE resend
        self.transport.queue(json_response(201, message("SMre2", "queued")))
        fresh = self.client.post(f"/api/notifications/{n.pk}/resend", HTTP_IDEMPOTENCY_KEY="key-2")
        self.assertNotEqual(fresh.json()["notificationId"], first.json()["notificationId"])
        self.assertEqual(len(self.transport.requests), 3)
        self.assertEqual(form(self.transport.requests[1])["Body"].split(" (ref ")[0], n.body.split(" (ref ")[0])

    def test_resend_needs_a_key_and_a_failed_message(self) -> None:
        self.add_number(self.shopper)
        _, n = self.place_order(queued_status="queued")
        self.login(self.staff)
        self.assertEqual(self.client.post(f"/api/notifications/{n.pk}/resend").status_code, 400)
        self.transport.queue(json_response(200, message("SMplaced", "delivered")))
        self.assertEqual(self.client.post(f"/api/notifications/{n.pk}/resend", HTTP_IDEMPOTENCY_KEY="k").status_code, 409)

    def test_resend_to_a_removed_number_is_refused(self) -> None:
        n = self.failed_notification()
        n.contact_number.deleted_at = n.created_at
        n.contact_number.save()
        self.login(self.staff)
        self.assertEqual(self.client.post(f"/api/notifications/{n.pk}/resend", HTTP_IDEMPOTENCY_KEY="k").status_code, 409)

    def test_content_disposal_redacts_at_the_provider_and_keeps_the_outcome(self) -> None:
        self.add_number(self.shopper)
        _, n = self.place_order(queued_status="delivered")
        self.login(self.staff)
        self.transport.queue(json_response(200, message("SMplaced", "delivered", body="")), json_response(200, message("SMplaced", "delivered", body="")))
        response = self.client.delete(f"/api/notifications/{n.pk}/content")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(form(self.transport.requests[1]), {"Body": ""})
        self.assertEqual(self.transport.requests[2].method, "GET")  # confirmed with the provider
        body = response.json()
        self.assertEqual((body["content"], body["outcome"], body["provider"]["sid"]), (None, "done", "SMplaced"))
        n.refresh_from_db()
        self.assertEqual(n.body, "")

    def test_content_of_a_message_in_flight_is_not_disposed(self) -> None:
        self.add_number(self.shopper)
        _, n = self.place_order(queued_status="queued")
        self.login(self.staff)
        self.transport.queue(json_response(200, message("SMplaced", "sending")))
        self.assertEqual(self.client.delete(f"/api/notifications/{n.pk}/content").status_code, 409)

    def test_reconciliation_lines_up_both_sides(self) -> None:
        self.add_number(self.shopper)
        _, n = self.place_order(queued_status="delivered")
        self.login(self.staff)
        in_window = "Thu, 24 Sep 2026 10:00:05 +0000"
        self.transport.queue(
            json_response(
                200,
                listing(
                    message("SMplaced", "delivered", date_sent=in_window),
                    message("SMforeign", "delivered", to=OTHER_NUMBER, date_sent=in_window),
                    message("SMinbound", "received", direction="inbound", date_sent=in_window),
                    message("SMlate", "delivered", date_sent="Fri, 25 Sep 2026 10:00:00 +0000"),
                ),
            )
        )
        response = self.client.get("/api/notifications/reconciliation?from=2026-09-24T00:00:00Z&to=2026-09-25T00:00:00Z")
        self.assertEqual(response.status_code, 200)
        report = response.json()
        self.assertEqual(report["summary"]["matched"], 1)
        self.assertEqual([e["providerSid"] for e in report["providerOnly"]], ["SMforeign"])
        self.assertEqual(report["summary"]["excludedInboundCopies"], 1)
        request = self.transport.requests[-1]
        q = query(request)
        self.assertEqual(q["From"], [FROM_NUMBER])  # asked for this app's number only
        self.assertIn("DateSent>", q)
        self.assertIn("DateSent<", q)
        n.refresh_from_db()
        self.assertIsNotNone(n.provider_date_sent)

    def test_removing_a_number_cancels_what_is_queued_for_it(self) -> None:
        self.add_number(self.shopper)
        order_id, _ = self.place_order()
        self.transport.queue(json_response(201, message("SMdisp", "queued")), json_response(201, message("SMfollow", "scheduled")))
        self.login(self.staff)
        self.client.post(f"/api/orders/{order_id}/dispatch")
        self.login(self.shopper)
        number = ContactNumber.objects.get(user=self.shopper)
        self.transport.queue(json_response(200, message("SMfollow", "canceled")))
        self.assertEqual(self.client.delete(f"/api/contact-numbers/{number.pk}").status_code, 204)
        n = Notification.objects.get(order_id=order_id, kind="followup")
        self.assertEqual(n.cancel_outcome, Outcome.DONE)


@override_settings(**{**TEST_SETTINGS, "TWILIO_BASE_URL": "http://127.0.0.1:9/mock"})
class BaseUrlTests(TestCase):
    def test_base_url_override_applies_to_messaging_only(self) -> None:
        transport = StubTransport(
            json_response(201, message("SM1", "queued")),
            json_response(200, {"phone_number": SHOPPER_NUMBER}),
        )
        gateway.set_client(gateway.build_client(transport=transport))
        self.addCleanup(gateway.set_client, None)
        gateway.send_message(SHOPPER_NUMBER, "hi")
        gateway.lookup_number(SHOPPER_NUMBER)
        self.assertTrue(transport.requests[0].url.startswith("http://127.0.0.1:9/mock/2010-04-01/"))
        self.assertTrue(transport.requests[1].url.startswith("https://lookups.twilio.com/"))
