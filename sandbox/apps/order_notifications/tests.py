import json
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import parse_qs, urlparse

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from oscar.test.factories import create_product
from twilio_sdk.core import HttpRequest, HttpResponse

from . import gateway as gw
from . import services
from .models import ContactNumber, Notification, ResendRequest

User = get_user_model()

ACCOUNT = "AC00000000000000000000000000000000"
TOKEN = "test-auth-token-value"
FROM = "+15005550006"
SERVICE = "MG00000000000000000000000000000000"
SHOPPER_NUMBER = "+14165550123"


def json_response(status, body):
    return HttpResponse(
        status_code=status, headers={"content-type": "application/json"}, content=json.dumps(body).encode()
    )


def message_json(sid, status="queued", body="text", to=SHOPPER_NUMBER, date_sent=None, direction="outbound-api"):
    return {
        "sid": sid, "status": status, "body": body, "to": to, "from": FROM, "direction": direction,
        "date_created": "Thu, 24 Sep 2026 10:00:00 +0000", "date_sent": date_sent,
        "error_code": None, "error_message": None, "account_sid": ACCOUNT,
    }


class FakeTwilio:
    """
    Satisfies the SDK's sync transport protocol and plays the provider:
    it keeps messages by sid and answers the calls this app makes.
    """

    def __init__(self):
        self.requests = []
        self.messages = {}
        self.counter = 0
        self.fail_next = []        # exceptions or HttpResponses for the next create calls
        self.list_pages = None     # optional canned list pages

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        url = urlparse(request.url)
        path = url.path
        if path.startswith("/v1/PhoneNumbers/"):
            number = path.rsplit("/", 1)[1].replace("%2B", "+")
            if not re.fullmatch(r"\+\d{10,15}", number):
                return json_response(404, {"code": 20404, "message": "not found", "status": 404})
            return json_response(200, {
                "phone_number": number, "country_code": "CA", "national_format": "x",
                "caller_name": None, "carrier": None, "add_ons": None, "url": "u",
            })
        match = re.search(r"/2010-04-01/Accounts/[^/]+/Messages(?:/([^/]+))?\.json$", path)
        assert match, request.url
        sid = match.group(1)
        if request.method == "POST" and sid is None:
            if self.fail_next:
                failure = self.fail_next.pop(0)
                if isinstance(failure, Exception):
                    raise failure
                return failure
            self.counter += 1
            fields = request.body.fields
            new_sid = "SM%032d" % self.counter
            status = "scheduled" if fields.get("ScheduleType") == "fixed" else "queued"
            self.messages[new_sid] = message_json(new_sid, status=status, body=fields["Body"], to=fields["To"])
            return json_response(201, self.messages[new_sid])
        if request.method == "POST":
            fields = request.body.fields
            message = self.messages[sid]
            if fields.get("Status") == "canceled":
                if message["status"] != "scheduled":
                    return json_response(400, {"code": 30409, "message": "not cancellable", "status": 400})
                message["status"] = "canceled"
            if "Body" in fields:
                message["body"] = fields["Body"]
            return json_response(200, message)
        if request.method == "GET" and sid:
            return json_response(200, self.messages[sid])
        if request.method == "GET":
            if self.list_pages is not None:
                query = parse_qs(url.query)
                index = int(query.get("Page", ["0"])[0])
                return json_response(200, self.list_pages[index])
            return json_response(200, {"messages": list(self.messages.values()), "next_page_uri": None})
        raise AssertionError(request.url)

    def close(self):
        pass

    def creates(self):
        return [r for r in self.requests if r.method == "POST" and r.url.endswith("/Messages.json")]


class NotificationTestCase(TestCase):
    def setUp(self):
        self.fake = FakeTwilio()
        self.previous = services.set_gateway(gw.TwilioGateway(
            account_sid=ACCOUNT, auth_token=TOKEN, from_number=FROM,
            messaging_service_sid=SERVICE, http_client=self.fake,
        ))
        self.shopper = User.objects.create_user("shopper", "shopper@example.com", "pass-word-123")
        self.other = User.objects.create_user("other", "other@example.com", "pass-word-123")
        self.staff = User.objects.create_user("op", "op@example.com", "pass-word-123", is_staff=True)
        self.product = create_product(price=10, num_in_stock=100)

    def tearDown(self):
        services.set_gateway(self.previous)

    def as_user(self, user):
        self.client.force_login(user)

    def post(self, url, data=None, **extra):
        return self.client.post(url, json.dumps(data or {}), content_type="application/json", **extra)

    def register_number(self, user=None):
        self.as_user(user or self.shopper)
        response = self.post("/api/contact-numbers", {"phoneNumber": SHOPPER_NUMBER})
        self.assertIn(response.status_code, (200, 201), response.content)
        return response.json()["contactNumberId"]

    def place_order(self):
        self.as_user(self.shopper)
        response = self.post("/api/orders", {"lines": [{"productId": self.product.pk, "quantity": 2}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()


class ContactNumberTests(NotificationTestCase):
    def test_registers_the_providers_canonical_form(self):
        contact_id = self.register_number()
        contact = ContactNumber.objects.get(pk=contact_id)
        self.assertEqual(contact.phone_number, SHOPPER_NUMBER)
        self.assertEqual(contact.user, self.shopper)

    def test_invalid_number_is_rejected_at_registration(self):
        self.as_user(self.shopper)
        response = self.post("/api/contact-numbers", {"phoneNumber": "12345"})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(ContactNumber.objects.exists())

    def test_numbers_are_private_to_their_owner(self):
        contact_id = self.register_number()
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/contact-numbers").json()["contactNumbers"], [])
        self.assertEqual(self.client.delete("/api/contact-numbers/%s" % contact_id).status_code, 404)

    def test_deleted_number_is_not_listed_or_messaged(self):
        contact_id = self.register_number()
        self.assertEqual(self.client.delete("/api/contact-numbers/%s" % contact_id).status_code, 200)
        self.assertEqual(self.client.get("/api/contact-numbers").json()["contactNumbers"], [])
        self.place_order()
        self.assertEqual(self.fake.creates(), [])

    def test_unauthenticated_is_refused(self):
        self.assertEqual(self.client.get("/api/contact-numbers").status_code, 401)


class OrderFlowTests(NotificationTestCase):
    def test_order_placed_message_is_sent_from_our_number(self):
        self.register_number()
        order = self.place_order()
        self.assertIn("orderId", order)
        [create] = self.fake.creates()
        fields = create.body.fields
        self.assertEqual(fields["To"], SHOPPER_NUMBER)
        self.assertEqual(fields["From"], FROM)
        self.assertIn(order["orderNumber"], fields["Body"])
        [notification] = order["notifications"]
        self.assertEqual(notification["outcome"], "pending")
        self.assertTrue(notification["providerMessageSid"].startswith("SM"))

    def test_no_number_means_no_message(self):
        order = self.place_order()
        self.assertEqual(order["notifications"], [])
        self.assertEqual(self.fake.requests, [])

    def test_refused_connection_does_not_fail_the_order_and_is_known_not_sent(self):
        self.register_number()
        self.fake.fail_next.append(httpx.ConnectError("refused"))
        order = self.place_order()
        [notification] = order["notifications"]
        self.assertEqual((notification["submitState"], notification["outcome"]), ("not_sent", "not_sent"))

    def test_read_timeout_does_not_fail_the_order_and_is_unknown(self):
        self.register_number()
        self.fake.fail_next.append(httpx.ReadTimeout("no reply"))
        order = self.place_order()
        [notification] = order["notifications"]
        self.assertEqual((notification["submitState"], notification["outcome"]), ("unknown", "unknown"))

    def test_unknown_send_is_resolved_by_its_reference(self):
        self.register_number()
        # The request "times out" but the provider did create the message.
        self.fake.fail_next.append(httpx.ReadTimeout("no reply"))
        order = self.place_order()
        notification = Notification.objects.get(pk=order["notifications"][0]["notificationId"])
        self.fake.messages["SM_LANDED"] = message_json("SM_LANDED", status="delivered", body=notification.body)
        response = self.client.get("/api/orders/%s/notifications" % order["orderId"]).json()
        self.assertEqual(response["notifications"][0]["providerMessageSid"], "SM_LANDED")
        self.assertEqual(response["notifications"][0]["outcome"], "delivered")

    def test_provider_rejection_does_not_fail_the_order(self):
        self.register_number()
        self.fake.fail_next.append(json_response(400, {"code": 21211, "message": "bad To", "status": 400}))
        order = self.place_order()
        self.assertEqual(order["notifications"][0]["outcome"], "not_sent")
        self.assertEqual(order["notifications"][0]["errorCode"], 21211)

    def test_dispatch_is_staff_only(self):
        self.register_number()
        order = self.place_order()
        self.assertEqual(self.post("/api/orders/%s/dispatch" % order["orderId"]).status_code, 403)

    def test_dispatch_sends_now_and_queues_followup_with_provider(self):
        self.register_number()
        order = self.place_order()
        self.as_user(self.staff)
        response = self.post("/api/orders/%s/dispatch" % order["orderId"])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "Dispatched")
        placed, dispatched, followup = self.fake.creates()
        self.assertNotIn("ScheduleType", dispatched.body.fields)
        fields = followup.body.fields
        self.assertEqual(fields["ScheduleType"], "fixed")
        self.assertEqual(fields["MessagingServiceSid"], SERVICE)
        self.assertEqual(fields["From"], FROM)
        send_at = datetime.fromisoformat(fields["SendAt"].replace("Z", "+00:00"))
        self.assertGreater(send_at, datetime.now(dt_timezone.utc) + timedelta(days=2))
        kinds = {n["kind"]: n["outcome"] for n in response.json()["notifications"]}
        self.assertEqual(kinds["delivery_followup"], "scheduled")

    def test_cancel_calls_off_the_followup_before_telling_the_shopper(self):
        self.register_number()
        order = self.place_order()
        self.as_user(self.staff)
        self.post("/api/orders/%s/dispatch" % order["orderId"])
        response = self.post("/api/orders/%s/cancel" % order["orderId"])
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["status"], "Cancelled")
        [called_off] = body["followUpsCalledOff"]
        self.assertEqual(called_off["outcome"], "canceled")
        cancel_request = [r for r in self.fake.requests
                          if r.method == "POST" and not r.url.endswith("/Messages.json")][0]
        self.assertEqual(cancel_request.body.fields["Status"], "canceled")
        self.assertIn("cancelled", self.fake.creates()[-1].body.fields["Body"])

    def test_orders_are_private_to_their_owner(self):
        self.register_number()
        order = self.place_order()
        self.as_user(self.other)
        self.assertEqual(self.client.get("/api/orders/%s/notifications" % order["orderId"]).status_code, 404)
        self.assertEqual(self.client.get("/api/my-orders").json()["orders"], [])

    def test_my_orders_shows_refreshed_outcomes(self):
        self.register_number()
        order = self.place_order()
        sid = order["notifications"][0]["providerMessageSid"]
        self.fake.messages[sid]["status"] = "undelivered"
        self.fake.messages[sid]["error_code"] = 30007
        Notification.objects.update(last_checked_at=None)
        orders = self.client.get("/api/my-orders").json()["orders"]
        self.assertEqual(orders[0]["notifications"][0]["outcome"], "failed")
        self.assertEqual(orders[0]["notifications"][0]["errorCode"], 30007)

    def test_unlisted_status_is_unknown_not_done(self):
        self.assertEqual(gw.outcome_from_status("something_new"), "unknown")
        self.assertEqual(gw.outcome_from_status(gw.MessageEnumStatus.UNDELIVERED), "failed")
        self.assertEqual(gw.outcome_from_status(gw.MessageEnumStatus.SENT), "sent")


class OperatorActionTests(NotificationTestCase):
    def failed_notification(self):
        self.register_number()
        order = self.place_order()
        sid = order["notifications"][0]["providerMessageSid"]
        self.fake.messages[sid]["status"] = "undelivered"
        Notification.objects.update(last_checked_at=None)
        return order["notifications"][0]["notificationId"]

    def test_resend_is_idempotent_per_key(self):
        notification_id = self.failed_notification()
        self.as_user(self.staff)
        url = "/api/notifications/%s/resend" % notification_id
        first = self.post(url, HTTP_IDEMPOTENCY_KEY="key-1")
        self.assertEqual(first.status_code, 201, first.content)
        repeat = self.post(url, HTTP_IDEMPOTENCY_KEY="key-1")
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(repeat.json()["notificationId"], first.json()["notificationId"])
        self.assertTrue(repeat.json()["replayed"])
        self.assertEqual(len(self.fake.creates()), 2)  # the original + one resend
        second = self.post(url, {"idempotencyKey": "key-2"})
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(second.json()["notificationId"], first.json()["notificationId"])
        self.assertEqual(len(self.fake.creates()), 3)

    def test_resend_requires_a_key_and_an_undelivered_message(self):
        self.register_number()
        order = self.place_order()
        self.as_user(self.staff)
        url = "/api/notifications/%s/resend" % order["notifications"][0]["notificationId"]
        self.assertEqual(self.post(url).status_code, 400)
        self.assertEqual(self.post(url, HTTP_IDEMPOTENCY_KEY="k").status_code, 409)

    def test_key_reused_for_another_notification_is_refused(self):
        notification_id = self.failed_notification()
        self.as_user(self.staff)
        self.post("/api/notifications/%s/resend" % notification_id, HTTP_IDEMPOTENCY_KEY="k")
        other = Notification.objects.exclude(pk=notification_id).first()
        response = self.post("/api/notifications/%s/resend" % other.pk, HTTP_IDEMPOTENCY_KEY="k")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(ResendRequest.objects.count(), 1)

    def test_content_disposal_redacts_at_provider_and_keeps_the_record(self):
        self.register_number()
        order = self.place_order()
        notification = order["notifications"][0]
        self.fake.messages[notification["providerMessageSid"]]["status"] = "delivered"
        self.as_user(self.staff)
        response = self.client.delete("/api/notifications/%s/content" % notification["notificationId"])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.json()["body"])
        self.assertEqual(response.json()["outcome"], "delivered")
        self.assertEqual(self.fake.messages[notification["providerMessageSid"]]["body"], "")
        redact = [r for r in self.fake.requests if r.method == "POST" and "Body" in r.body.fields
                  and not r.url.endswith("/Messages.json")]
        self.assertEqual(redact[0].body.fields["Body"], "")
        self.assertEqual(Notification.objects.get(pk=notification["notificationId"]).body, "")

    def test_reconciliation_asks_for_our_number_and_follows_pages(self):
        self.register_number()
        order = self.place_order()
        ours = order["notifications"][0]["providerMessageSid"]
        sent = "Thu, 24 Sep 2026 10:05:00 +0000"
        self.fake.messages[ours]["date_sent"] = sent
        self.fake.list_pages = [
            {"messages": [self.fake.messages[ours]],
             "next_page_uri": "/2010-04-01/Accounts/%s/Messages.json?PageSize=1000&Page=1&PageToken=PA1" % ACCOUNT},
            {"messages": [message_json("SMforeign", status="delivered", date_sent=sent),
                          message_json("SMinbound", status="received", date_sent=sent, direction="inbound")],
             "next_page_uri": None},
        ]
        self.as_user(self.staff)
        response = self.client.get(
            "/api/notifications/reconciliation",
            {"from": "2026-09-24T00:00:00Z", "to": "2026-09-24T23:59:59Z"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual([m["providerMessageSid"] for m in report["matched"]], [ours])
        self.assertEqual([m["providerMessageSid"] for m in report["providerOnly"]], ["SMforeign"])
        lists = [r for r in self.fake.requests if r.method == "GET" and "/Messages.json" in r.url]
        self.assertEqual(len(lists), 2)
        for request in lists:
            query = parse_qs(urlparse(request.url).query)
            self.assertEqual(query["From"], [FROM])
            self.assertIn("DateSent>", query)
            self.assertIn("DateSent<", query)
        self.assertEqual(parse_qs(urlparse(lists[1].url).query)["PageToken"], ["PA1"])

    def test_operator_actions_are_staff_only(self):
        notification_id = self.failed_notification()
        self.as_user(self.shopper)
        self.assertEqual(self.post("/api/notifications/%s/resend" % notification_id,
                                   HTTP_IDEMPOTENCY_KEY="k").status_code, 403)
        self.assertEqual(self.client.delete("/api/notifications/%s/content" % notification_id).status_code, 403)
        self.assertEqual(self.client.get("/api/notifications/reconciliation").status_code, 403)

    def test_auth_token_is_never_returned(self):
        notification_id = self.failed_notification()
        self.as_user(self.staff)
        response = self.post("/api/notifications/%s/resend" % notification_id, HTTP_IDEMPOTENCY_KEY="k")
        self.assertNotIn(TOKEN, response.content.decode())


class GatewayConfigurationTests(TestCase):
    def test_base_url_override_applies_to_messaging_only(self):
        fake = FakeTwilio()
        gateway = gw.TwilioGateway(account_sid=ACCOUNT, auth_token=TOKEN, from_number=FROM,
                                   messaging_service_sid=SERVICE, base_url="https://mock.example.test/base",
                                   http_client=fake)
        gateway.send(SHOPPER_NUMBER, "hi")
        gateway.lookup_number(SHOPPER_NUMBER)
        self.assertTrue(fake.requests[0].url.startswith("https://mock.example.test/base/2010-04-01/"))
        self.assertTrue(fake.requests[1].url.startswith("https://lookups.twilio.com/"))

    def test_auth_401_is_a_configuration_error(self):
        class Refusing(FakeTwilio):
            def send(self, request):
                return json_response(401, {"code": 20003, "message": "auth"})
        gateway = gw.TwilioGateway(account_sid=ACCOUNT, auth_token=TOKEN, from_number=FROM,
                                   messaging_service_sid=SERVICE, http_client=Refusing())
        with self.assertRaises(gw.ProviderConfigError) as ctx:
            gateway.send(SHOPPER_NUMBER, "hi")
        self.assertEqual((ctx.exception.http_status, ctx.exception.outcome_unknown), (502, False))

    def test_truncated_success_body_is_an_unknown_outcome(self):
        class Truncated(FakeTwilio):
            def send(self, request):
                return json_response(201, {})
        gateway = gw.TwilioGateway(account_sid=ACCOUNT, auth_token=TOKEN, from_number=FROM,
                                   messaging_service_sid=SERVICE, http_client=Truncated())
        with self.assertRaises(gw.ProviderError) as ctx:
            gateway.send(SHOPPER_NUMBER, "hi")
        self.assertTrue(ctx.exception.outcome_unknown)

    def test_missing_credentials_fail_fast(self):
        with self.assertRaises(gw.ProviderConfigError):
            gw.TwilioGateway(account_sid="", auth_token="", from_number=FROM, messaging_service_sid=SERVICE)
