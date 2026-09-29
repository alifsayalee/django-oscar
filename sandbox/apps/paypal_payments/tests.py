"""
Tests for the PayPal payments API.

PayPal is faked at the SDK's transport seam (``custom_http_client``), so the
real SDK builds and decodes every request; nothing reaches the network.
Run from ``sandbox/``:  python manage.py test apps.paypal_payments
"""

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal as D

import httpx
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from oscar.core.loading import get_model
from oscar.test.factories import create_product
from pay_pal_server_sdk import PayPalServerSdkClient
from pay_pal_server_sdk.core import HttpRequest, OAuthToken

from . import gateway as gw
from .models import PayPalPayment, PayPalRefund

Country = get_model("address", "Country")
Order = get_model("order", "Order")
Bankcard = get_model("payment", "Bankcard")
Source = get_model("payment", "Source")

CARD = {
    "number": "4111 1111 1111 1111",
    "expiry": "2031-12",
    "securityCode": "123",
    "name": "Test Shopper",
    "billingAddress": {
        "line1": "1 Main St",
        "city": "San Jose",
        "state": "CA",
        "postalCode": "95131",
        "countryCode": "US",
    },
}
SHIPPING = {
    "firstName": "Test",
    "lastName": "Shopper",
    "line1": "1 Main St",
    "city": "San Jose",
    "state": "CA",
    "postalCode": "95131",
    "countryCode": "US",
}


# ================
# Stub PayPal
# ================


@dataclass
class StubResponse:
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    chunks: list[bytes] = field(default_factory=list)
    url: str = "https://api.test/"
    closed: bool = False

    def iter_bytes(self, chunk_size: int | None) -> Iterator[bytes]:
        yield from self.chunks
        self.close()

    def read(self) -> bytes:
        self.close()
        return b"".join(self.chunks)

    def close(self) -> None:
        self.closed = True


def json_response(status, body=None):
    return StubResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        chunks=[json.dumps(body).encode()] if body is not None else [],
    )


class StubTokenSource:
    def fetch(self, credentials):
        return OAuthToken(access_token="t", token_type="Bearer")


class FakePayPal:
    """A routing transport: (METHOD, path regex) -> handler(request, match)."""

    def __init__(self):
        self.routes = []
        self.requests = []
        self.errors = {}

    def on(self, method, pattern, handler):
        self.routes.insert(0, (method, re.compile(pattern), handler))

    def fail_next(self, method, pattern, error):
        self.errors[(method, pattern)] = error

    def send(self, request: HttpRequest):
        self.requests.append(request)
        path = httpx.URL(request.url).path
        for (method, pattern), error in list(self.errors.items()):
            if method == request.method and re.search(pattern, path):
                del self.errors[(method, pattern)]
                raise error
        for method, pattern, handler in self.routes:
            match = pattern.search(path)
            if method == request.method and match:
                return handler(request, match)
        return json_response(
            404, {"name": "RESOURCE_NOT_FOUND", "message": "no route", "debug_id": "x"}
        )

    def close(self):
        pass

    def calls(self, method, pattern):
        rx = re.compile(pattern)
        return [
            r
            for r in self.requests
            if r.method == method and rx.search(httpx.URL(r.url).path)
        ]


def paypal_error(status, issue, description="rejected"):
    return json_response(
        status,
        {
            "name": "UNPROCESSABLE_ENTITY",
            "message": "failed business validation",
            "debug_id": "dbg1",
            "details": [{"issue": issue, "description": description}],
        },
    )


class PayPalWorld:
    """Stateful sandbox double: orders, authorizations, captures, refunds, vault."""

    def __init__(self, fake):
        self.fake = fake
        self.n = 0
        self.by_request_id = {}
        self.authorizations = {}
        self.captures = {}
        self.tokens = {}
        self.order_status = "COMPLETED"
        self.auth_status = "CREATED"
        self.hold_override = None
        self.search_pages = None
        fake.on("POST", r"^/v2/checkout/orders$", self.create_order)
        fake.on("GET", r"^/v2/payments/authorizations/(\w+)$", self.get_auth)
        fake.on("POST", r"^/v2/payments/authorizations/(\w+)/capture$", self.capture)
        fake.on("POST", r"^/v2/payments/authorizations/(\w+)/void$", self.void)
        fake.on(
            "POST", r"^/v2/payments/authorizations/(\w+)/reauthorize$", self.reauthorize
        )
        fake.on("POST", r"^/v2/payments/captures/(\w+)/refund$", self.refund)
        fake.on("POST", r"^/v3/vault/payment-tokens$", self.vault)
        fake.on("DELETE", r"^/v3/vault/payment-tokens/(\w+)$", self.delete_token)
        fake.on("GET", r"^/v1/reporting/transactions$", self.search)

    def _id(self, prefix):
        self.n += 1
        return "%s%015d" % (prefix, self.n)

    def _replay(self, request, build):
        key = request.headers.get("paypal-request-id")
        if key and key in self.by_request_id:
            return self.by_request_id[key]
        response = build()
        if key:
            self.by_request_id[key] = response
        return response

    def create_order(self, request, match):
        def build():
            body = request.body.value
            unit = body["purchase_units"][0]
            if self.order_status == "PAYER_ACTION_REQUIRED":
                return json_response(
                    200, {"id": self._id("O"), "status": "PAYER_ACTION_REQUIRED"}
                )
            auth_id = self._id("A")
            amount = dict(unit["amount"])
            if self.hold_override:
                amount["value"] = self.hold_override
            now = timezone.now()
            self.authorizations[auth_id] = {
                "id": auth_id,
                "status": self.auth_status,
                "amount": amount,
                "create_time": now.isoformat(),
                "expiration_time": (now + timedelta(days=29)).isoformat(),
            }
            return json_response(
                201,
                {
                    "id": self._id("O"),
                    "status": "COMPLETED",
                    "intent": "AUTHORIZE",
                    "payment_source": {
                        "card": {"brand": "VISA", "last_digits": "1111"}
                    },
                    "purchase_units": [
                        {
                            "reference_id": unit.get("reference_id"),
                            "amount": unit["amount"],
                            "payments": {
                                "authorizations": [self.authorizations[auth_id]]
                            },
                        }
                    ],
                },
            )

        return self._replay(request, build)

    def get_auth(self, request, match):
        auth = self.authorizations.get(match.group(1))
        if auth is None:
            return paypal_error(404, "INVALID_RESOURCE_ID")
        return json_response(200, auth)

    def capture(self, request, match):
        def build():
            auth = self.authorizations[match.group(1)]
            amount = request.body.value["amount"]
            capture_id = self._id("C")
            self.captures[capture_id] = {
                "amount": D(amount["value"]),
                "refunded": D("0"),
            }
            auth["status"] = "CAPTURED"
            return json_response(
                201,
                {
                    "id": capture_id,
                    "status": "COMPLETED",
                    "amount": amount,
                    "final_capture": True,
                    "seller_receivable_breakdown": {
                        "gross_amount": amount,
                        "paypal_fee": {
                            "currency_code": amount["currency_code"],
                            "value": "0.62",
                        },
                        "net_amount": {
                            "currency_code": amount["currency_code"],
                            "value": str(D(amount["value"]) - D("0.62")),
                        },
                    },
                },
            )

        return self._replay(request, build)

    def void(self, request, match):
        def build():
            auth = self.authorizations[match.group(1)]
            if auth["status"] == "CAPTURED":
                return paypal_error(422, "PREVIOUSLY_CAPTURED")
            auth["status"] = "VOIDED"
            return json_response(200, auth)

        return self._replay(request, build)

    def reauthorize(self, request, match):
        def build():
            old = self.authorizations[match.group(1)]
            new_id = self._id("A")
            now = timezone.now()
            self.authorizations[new_id] = {
                "id": new_id,
                "status": "CREATED",
                "amount": old["amount"],
                "create_time": now.isoformat(),
                "expiration_time": old["expiration_time"],
            }
            return json_response(201, self.authorizations[new_id])

        return self._replay(request, build)

    def refund(self, request, match):
        def build():
            capture = self.captures[match.group(1)]
            amount = request.body.value["amount"]
            value = D(amount["value"])
            if capture["refunded"] + value > capture["amount"]:
                return paypal_error(422, "REFUND_AMOUNT_EXCEEDED")
            capture["refunded"] += value
            return json_response(
                201, {"id": self._id("R"), "status": "COMPLETED", "amount": amount}
            )

        return self._replay(request, build)

    def vault(self, request, match):
        def build():
            card = request.body.value["payment_source"]["card"]
            customer = request.body.value.get("customer", {})
            token_id = self._id("T")
            self.tokens[token_id] = True
            return json_response(
                200,
                {
                    "id": token_id,
                    "customer": {"id": customer.get("id") or "CUST1"},
                    "payment_source": {
                        "card": {
                            "brand": "VISA",
                            "last_digits": card["number"][-4:],
                            "expiry": card["expiry"],
                        }
                    },
                },
            )

        return self._replay(request, build)

    def delete_token(self, request, match):
        if not self.tokens.pop(match.group(1), None):
            return json_response(
                404, {"name": "RESOURCE_NOT_FOUND", "message": "gone", "debug_id": "d"}
            )
        return StubResponse(status_code=204)

    def search(self, request, match):
        params = dict(httpx.URL(request.url).params)
        page = int(params.get("page", 1))
        pages = self.search_pages or [[]]
        txns = pages[page - 1] if page <= len(pages) else []
        return json_response(
            200,
            {
                "transaction_details": [{"transaction_info": t} for t in txns],
                "page": page,
                "total_pages": len(pages),
                "last_refreshed_datetime": timezone.now().isoformat(),
            },
        )


# =====
# Tests
# =====


@override_settings(
    PAYPAL_CURRENCY="USD",
    PAYPAL_ENVIRONMENT="sandbox",
    PAYPAL_CLIENT_ID="test-id",
    PAYPAL_CLIENT_SECRET="test-secret",
)
class ApiTestCase(TestCase):
    def setUp(self):
        self.fake = FakePayPal()
        self.world = PayPalWorld(self.fake)
        client = PayPalServerSdkClient(
            custom_http_client=self.fake,
            retry_options=0,
            oauth2={"client_id": "test-id", "client_secret": "test-secret"},
            oauth2_token_source=StubTokenSource(),
        )
        self.previous = gw.set_gateway(gw.PayPalGateway(client))
        self.addCleanup(gw.set_gateway, self.previous)

        Country.objects.create(
            iso_3166_1_a2="US",
            name="United States",
            printable_name="United States",
            is_shipping_country=True,
        )
        self.product = create_product(price=D("12.34"), num_in_stock=50, title="Book")
        self.product2 = create_product(
            price=D("5.00"), num_in_stock=50, title="Other book"
        )
        User = get_user_model()
        self.alice = User.objects.create_user(
            "alice", "alice@example.com", "pw-alice-123"
        )
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw-bob-123")
        self.operator = User.objects.create_user(
            "op", "op@example.com", "pw-op-123", is_staff=True
        )
        self.shopper = Client()
        self.shopper.force_login(self.alice)
        self.other = Client()
        self.other.force_login(self.bob)
        self.staff = Client()
        self.staff.force_login(self.operator)

    def post(self, client, url, body=None, **headers):
        return client.post(
            url,
            data=json.dumps(body or {}),
            content_type="application/json",
            headers=headers,
        )

    def place(self, qty=2):
        r = self.post(
            self.shopper,
            "/api/orders",
            {
                "items": [{"itemId": self.product.pk, "quantity": qty}],
                "shippingAddress": SHIPPING,
            },
        )
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def paid_order(self):
        order = self.place()
        r = self.post(
            self.shopper, "/api/orders/%s/pay" % order["orderId"], {"card": CARD}
        )
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def fulfilled_order(self):
        order = self.paid_order()
        r = self.post(self.staff, "/api/orders/%s/fulfil" % order["orderId"])
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()


class OrderTests(ApiTestCase):
    def test_place_order_uses_catalogue_price_and_configured_currency(self):
        order = self.place(qty=2)
        self.assertEqual(order["status"], "Pending payment")
        self.assertEqual(order["total"], "24.68")
        self.assertEqual(order["currency"], "USD")
        self.assertIsNone(order["payment"])
        db = Order.objects.get(number=order["orderId"])
        self.assertEqual(db.user, self.alice)
        self.assertEqual(db.lines.get().quantity, 2)

    def test_requires_login(self):
        r = Client().post("/api/orders", data="{}", content_type="application/json")
        self.assertEqual(r.status_code, 401)

    def test_unknown_item(self):
        r = self.post(
            self.shopper,
            "/api/orders",
            {"items": [{"itemId": 999999, "quantity": 1}], "shippingAddress": SHIPPING},
        )
        self.assertEqual(r.status_code, 422)

    def test_shipping_address_required_for_shippable_items(self):
        r = self.post(
            self.shopper,
            "/api/orders",
            {"items": [{"itemId": self.product.pk, "quantity": 1}]},
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "shipping_address_required")

    def test_my_orders_is_scoped_to_caller(self):
        order = self.place()
        mine = self.shopper.get("/api/my-orders").json()["orders"]
        self.assertEqual([o["orderId"] for o in mine], [order["orderId"]])
        self.assertEqual(self.other.get("/api/my-orders").json()["orders"], [])


class PayTests(ApiTestCase):
    def test_authorizes_exact_total_and_records_hold(self):
        order = self.place(qty=2)
        r = self.post(
            self.shopper, "/api/orders/%s/pay" % order["orderId"], {"card": CARD}
        )
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual(body["status"], "Authorised")
        self.assertEqual(body["payment"]["status"], "AUTHORIZED")
        self.assertEqual(
            body["payment"]["card"], {"brand": "VISA", "lastDigits": "1111"}
        )

        (req,) = self.fake.calls("POST", r"^/v2/checkout/orders$")
        sent = req.body.value
        self.assertEqual(sent["intent"], "AUTHORIZE")
        self.assertEqual(
            sent["purchase_units"][0]["amount"],
            {"currency_code": "USD", "value": "24.68"},
        )
        self.assertEqual(sent["purchase_units"][0]["custom_id"], order["orderId"])
        self.assertEqual(sent["payment_source"]["card"]["number"], "4111111111111111")
        self.assertTrue(req.headers["paypal-request-id"])
        self.assertEqual(req.headers["prefer"], "return=representation")

        source = Source.objects.get(order__number=order["orderId"])
        self.assertEqual(source.amount_allocated, D("24.68"))
        self.assertEqual(source.amount_debited, D("0.00"))

    def test_card_number_is_never_stored(self):
        order = self.paid_order()
        payment = PayPalPayment.objects.get(order__number=order["orderId"])
        stored = json.dumps(
            {f.name: str(getattr(payment, f.attname)) for f in payment._meta.fields}
        )
        self.assertNotIn("4111111111111111", stored)
        self.assertFalse(Bankcard.objects.exists())

    def test_double_click_does_not_authorize_twice(self):
        order = self.paid_order()
        r = self.post(
            self.shopper, "/api/orders/%s/pay" % order["orderId"], {"card": CARD}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.fake.calls("POST", r"^/v2/checkout/orders$")), 1)

    def test_unknown_outcome_is_settled_by_replaying_the_same_request_id(self):
        order = self.place()
        self.fake.fail_next(
            "POST", r"^/v2/checkout/orders$", httpx.ReadTimeout("no reply")
        )
        r = self.post(
            self.shopper, "/api/orders/%s/pay" % order["orderId"], {"card": CARD}
        )
        self.assertEqual(r.status_code, 504)
        self.assertTrue(r.json()["error"]["outcomeUnknown"])
        r = self.post(
            self.shopper, "/api/orders/%s/pay" % order["orderId"], {"card": CARD}
        )
        self.assertEqual(r.status_code, 201, r.content)
        first, second = self.fake.calls("POST", r"^/v2/checkout/orders$")
        self.assertEqual(
            first.headers["paypal-request-id"], second.headers["paypal-request-id"]
        )

    def test_refused_connection_is_a_known_failure(self):
        order = self.place()
        self.fake.fail_next(
            "POST", r"^/v2/checkout/orders$", httpx.ConnectError("refused")
        )
        r = self.post(
            self.shopper, "/api/orders/%s/pay" % order["orderId"], {"card": CARD}
        )
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("outcomeUnknown", r.json()["error"])

    def test_decline_then_retry_uses_a_new_request_id(self):
        order = self.place()
        self.fake.on(
            "POST",
            r"^/v2/checkout/orders$",
            lambda req, m: paypal_error(422, "INSTRUMENT_DECLINED", "declined"),
        )
        r = self.post(
            self.shopper, "/api/orders/%s/pay" % order["orderId"], {"card": CARD}
        )
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.json()["error"]["paypalIssue"], "INSTRUMENT_DECLINED")
        self.fake.routes.pop(0)
        r = self.post(
            self.shopper, "/api/orders/%s/pay" % order["orderId"], {"card": CARD}
        )
        self.assertEqual(r.status_code, 201)
        first, second = self.fake.calls("POST", r"^/v2/checkout/orders$")
        self.assertNotEqual(
            first.headers["paypal-request-id"], second.headers["paypal-request-id"]
        )

    def test_browser_challenge_is_rejected(self):
        order = self.place()
        self.world.order_status = "PAYER_ACTION_REQUIRED"
        r = self.post(
            self.shopper, "/api/orders/%s/pay" % order["orderId"], {"card": CARD}
        )
        self.assertEqual(r.status_code, 402)
        self.assertEqual(
            r.json()["error"]["code"], "card_requires_browser_authentication"
        )

    def test_hold_that_differs_from_total_is_released(self):
        order = self.place()
        self.world.hold_override = "1.00"
        r = self.post(
            self.shopper, "/api/orders/%s/pay" % order["orderId"], {"card": CARD}
        )
        self.assertEqual(r.status_code, 502)
        self.assertEqual(len(self.fake.calls("POST", r"/void$")), 1)
        self.assertEqual(
            Order.objects.get(number=order["orderId"]).status, "Pending payment"
        )

    def test_other_shopper_cannot_pay_or_see(self):
        order = self.place()
        r = self.post(
            self.other, "/api/orders/%s/pay" % order["orderId"], {"card": CARD}
        )
        self.assertEqual(r.status_code, 404)

    def test_invalid_card_is_rejected_locally(self):
        order = self.place()
        r = self.post(
            self.shopper,
            "/api/orders/%s/pay" % order["orderId"],
            {"card": {**CARD, "number": "4111111111111112"}},
        )
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.fake.requests)

    def test_bad_credentials_are_a_server_side_failure(self):
        order = self.place()
        client = PayPalServerSdkClient(
            custom_http_client=self.fake,
            retry_options=0,
            oauth2={"client_id": "bad", "client_secret": "bad"},
        )
        self.fake.on(
            "POST",
            r"^/v1/oauth2/token$",
            lambda req, m: json_response(401, {"error": "invalid_client"}),
        )
        gw.set_gateway(gw.PayPalGateway(client))
        r = self.post(
            self.shopper, "/api/orders/%s/pay" % order["orderId"], {"card": CARD}
        )
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["error"]["code"], "paypal_auth_failed")


class FulfilTests(ApiTestCase):
    def test_operator_only(self):
        order = self.paid_order()
        r = self.post(self.shopper, "/api/orders/%s/fulfil" % order["orderId"])
        self.assertEqual(r.status_code, 403)

    def test_capture_records_fee_and_net(self):
        order = self.fulfilled_order()
        self.assertEqual(order["status"], "Complete")
        capture = order["payment"]["capture"]
        self.assertEqual(capture["amount"], "24.68")
        self.assertEqual(capture["paypalFee"], "0.62")
        self.assertEqual(capture["netAmount"], "24.06")
        self.assertEqual(order["payment"]["status"], "CAPTURED")
        (req,) = self.fake.calls("POST", r"/capture$")
        self.assertEqual(
            req.body.value["amount"], {"currency_code": "USD", "value": "24.68"}
        )
        source = Source.objects.get(order__number=order["orderId"])
        self.assertEqual(source.amount_debited, D("24.68"))

    def test_repeat_fulfil_does_not_capture_twice(self):
        order = self.fulfilled_order()
        r = self.post(self.staff, "/api/orders/%s/fulfil" % order["orderId"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.fake.calls("POST", r"/capture$")), 1)

    def test_stale_authorization_is_renewed_before_capture(self):
        order = self.paid_order()
        payment = PayPalPayment.objects.get(order__number=order["orderId"])
        old = payment.authorization_id
        PayPalPayment.objects.filter(pk=payment.pk).update(
            honor_period_started_at=timezone.now() - timedelta(days=5)
        )
        r = self.post(self.staff, "/api/orders/%s/fulfil" % order["orderId"])
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(len(self.fake.calls("POST", r"/%s/reauthorize$" % old)), 1)
        (capture,) = self.fake.calls("POST", r"/capture$")
        payment.refresh_from_db()
        self.assertNotEqual(payment.authorization_id, old)
        self.assertIn(payment.authorization_id, capture.url)

    def test_expired_authorization_is_actionable(self):
        order = self.paid_order()
        payment = PayPalPayment.objects.get(order__number=order["orderId"])
        self.world.authorizations[payment.authorization_id]["expiration_time"] = (
            timezone.now() - timedelta(days=1)
        ).isoformat()
        r = self.post(self.staff, "/api/orders/%s/fulfil" % order["orderId"])
        self.assertEqual(r.status_code, 409)
        err = r.json()["error"]
        self.assertEqual(err["code"], "authorization_expired")
        self.assertIn("pay again", err["message"])
        self.assertFalse(self.fake.calls("POST", r"/capture$"))
        self.assertEqual(
            Order.objects.get(number=order["orderId"]).status, "Pending payment"
        )
        # the shopper can now pay again
        r = self.post(
            self.shopper, "/api/orders/%s/pay" % order["orderId"], {"card": CARD}
        )
        self.assertEqual(r.status_code, 201, r.content)

    def test_renewal_refused_is_actionable(self):
        order = self.paid_order()
        PayPalPayment.objects.filter(order__number=order["orderId"]).update(
            honor_period_started_at=timezone.now() - timedelta(days=5)
        )
        self.fake.on(
            "POST",
            r"/reauthorize$",
            lambda req, m: paypal_error(422, "REAUTHORIZATION_NOT_ALLOWED"),
        )
        r = self.post(self.staff, "/api/orders/%s/fulfil" % order["orderId"])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "authorization_renewal_failed")
        self.assertFalse(self.fake.calls("POST", r"/capture$"))


class CancelTests(ApiTestCase):
    def test_cancel_releases_the_hold(self):
        order = self.paid_order()
        r = self.post(self.staff, "/api/orders/%s/cancel" % order["orderId"])
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "Cancelled")
        self.assertEqual(r.json()["payment"]["status"], "VOIDED")
        self.assertEqual(len(self.fake.calls("POST", r"/void$")), 1)
        self.assertFalse(self.fake.calls("POST", r"/capture$"))

    def test_cancel_unpaid_order(self):
        order = self.place()
        r = self.post(self.staff, "/api/orders/%s/cancel" % order["orderId"])
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.fake.requests)

    def test_cannot_cancel_after_fulfilment(self):
        order = self.fulfilled_order()
        r = self.post(self.staff, "/api/orders/%s/cancel" % order["orderId"])
        self.assertEqual(r.status_code, 409)

    def test_operator_only(self):
        order = self.paid_order()
        self.assertEqual(
            self.post(
                self.shopper, "/api/orders/%s/cancel" % order["orderId"]
            ).status_code,
            403,
        )


class RefundTests(ApiTestCase):
    def refund(self, order, key, amount=None, client=None):
        body = {} if amount is None else {"amount": amount}
        return self.post(
            client or self.shopper,
            "/api/orders/%s/refunds" % order["orderId"],
            body,
            **{"Idempotency-Key": key},
        )

    def test_partial_refunds_never_exceed_capture(self):
        order = self.fulfilled_order()
        r1 = self.refund(order, "k1", "10.00")
        self.assertEqual(r1.status_code, 201, r1.content)
        self.assertTrue(r1.json()["refundId"])
        r2 = self.refund(order, "k2", "10.00")
        self.assertEqual(r2.status_code, 201)
        self.assertNotEqual(r1.json()["refundId"], r2.json()["refundId"])
        self.assertEqual(r2.json()["payment"]["refundableAmount"], "4.68")
        r3 = self.refund(order, "k3", "5.00")
        self.assertEqual(r3.status_code, 422)
        self.assertEqual(r3.json()["error"]["code"], "refund_exceeds_captured")
        self.assertEqual(len(self.fake.calls("POST", r"/refund$")), 2)
        r4 = self.refund(order, "k4")  # the rest
        self.assertEqual(r4.status_code, 201)
        self.assertEqual(r4.json()["amount"], "4.68")
        self.assertEqual(r4.json()["payment"]["status"], "REFUNDED")

    def test_same_key_does_not_refund_twice(self):
        order = self.fulfilled_order()
        r1 = self.refund(order, "same", "3.00")
        r2 = self.refund(order, "same", "3.00")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.json()["refundId"], r2.json()["refundId"])
        self.assertEqual(len(self.fake.calls("POST", r"/refund$")), 1)
        r3 = self.refund(order, "same", "4.00")
        self.assertEqual(r3.status_code, 422)

    def test_unknown_outcome_replays_same_request_id(self):
        order = self.fulfilled_order()
        self.fake.fail_next("POST", r"/refund$", httpx.ReadTimeout("no reply"))
        self.assertEqual(self.refund(order, "k", "1.00").status_code, 504)
        self.assertEqual(self.refund(order, "k", "1.00").status_code, 201)
        first, second = self.fake.calls("POST", r"/refund$")
        self.assertEqual(
            first.headers["paypal-request-id"], second.headers["paypal-request-id"]
        )
        self.assertEqual(PayPalRefund.objects.count(), 1)

    def test_idempotency_key_required(self):
        order = self.fulfilled_order()
        r = self.post(
            self.shopper,
            "/api/orders/%s/refunds" % order["orderId"],
            {"amount": "1.00"},
        )
        self.assertEqual(r.status_code, 400)

    def test_not_before_fulfilment(self):
        order = self.paid_order()
        self.assertEqual(self.refund(order, "k", "1.00").status_code, 409)

    def test_other_shopper_cannot_refund(self):
        order = self.fulfilled_order()
        self.assertEqual(
            self.refund(order, "k", "1.00", client=self.other).status_code, 404
        )


class SavedCardTests(ApiTestCase):
    def save(self, client=None, key=None):
        headers = {"Idempotency-Key": key} if key else {}
        return self.post(
            client or self.shopper, "/api/payment-methods", {"card": CARD}, **headers
        )

    def test_save_list_pay_delete(self):
        r = self.save()
        self.assertEqual(r.status_code, 201, r.content)
        card = r.json()
        self.assertEqual(card["lastDigits"], "1111")
        self.assertEqual(card["brand"], "VISA")
        self.assertNotIn("4111111111111111", r.content.decode())
        listed = self.shopper.get("/api/payment-methods").json()["paymentMethods"]
        self.assertEqual(
            [c["paymentMethodId"] for c in listed], [card["paymentMethodId"]]
        )

        order = self.place()
        r = self.post(
            self.shopper,
            "/api/orders/%s/pay" % order["orderId"],
            {"paymentMethodId": card["paymentMethodId"]},
        )
        self.assertEqual(r.status_code, 201, r.content)
        sent = self.fake.calls("POST", r"^/v2/checkout/orders$")[0].body.value
        self.assertEqual(set(sent["payment_source"]["card"]), {"vault_id"})

        r = self.shopper.delete("/api/payment-methods/%s" % card["paymentMethodId"])
        self.assertEqual(r.status_code, 204)
        self.assertEqual(
            len(self.fake.calls("DELETE", r"^/v3/vault/payment-tokens/")), 1
        )
        self.assertEqual(
            self.shopper.get("/api/payment-methods").json()["paymentMethods"], []
        )
        order2 = self.place()
        r = self.post(
            self.shopper,
            "/api/orders/%s/pay" % order2["orderId"],
            {"paymentMethodId": card["paymentMethodId"]},
        )
        self.assertEqual(r.status_code, 404)

    def test_cards_belong_to_their_owner(self):
        card = self.save().json()
        self.assertEqual(
            self.other.get("/api/payment-methods").json()["paymentMethods"], []
        )
        self.assertEqual(
            self.other.delete(
                "/api/payment-methods/%s" % card["paymentMethodId"]
            ).status_code,
            404,
        )
        order = self.post(
            self.other,
            "/api/orders",
            {
                "items": [{"itemId": self.product.pk, "quantity": 1}],
                "shippingAddress": SHIPPING,
            },
        ).json()
        r = self.post(
            self.other,
            "/api/orders/%s/pay" % order["orderId"],
            {"paymentMethodId": card["paymentMethodId"]},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(
            len(self.shopper.get("/api/payment-methods").json()["paymentMethods"]), 1
        )

    def test_second_save_reuses_vault_customer(self):
        self.save()
        self.save()
        second = self.fake.calls("POST", r"^/v3/vault/payment-tokens$")[1].body.value
        self.assertEqual(second["customer"], {"id": "CUST1"})

    def test_save_with_same_key_is_idempotent(self):
        a = self.save(key="save-1")
        b = self.save(key="save-1")
        self.assertEqual(b.status_code, 200)
        self.assertEqual(a.json()["paymentMethodId"], b.json()["paymentMethodId"])
        self.assertEqual(Bankcard.objects.count(), 1)


class ReconciliationTests(ApiTestCase):
    def test_operator_only(self):
        r = self.shopper.get(
            "/api/reconciliation",
            {"from": "2026-01-01T00:00:00Z", "to": "2026-01-02T00:00:00Z"},
        )
        self.assertEqual(r.status_code, 403)

    def test_lines_up_both_sides_across_pages_and_windows(self):
        order = self.fulfilled_order()
        payment = PayPalPayment.objects.get(order__number=order["orderId"])
        self.world.search_pages = [
            [
                {
                    "transaction_id": payment.capture_id,
                    "transaction_event_code": "T0006",
                    "transaction_amount": {"currency_code": "USD", "value": "24.68"},
                    "fee_amount": {"currency_code": "USD", "value": "-0.62"},
                    "transaction_status": "S",
                    "custom_field": order["orderId"],
                }
            ],
            [
                {
                    "transaction_id": "UNKNOWN0000000001",
                    "transaction_event_code": "T0006",
                    "transaction_amount": {"currency_code": "USD", "value": "9.99"},
                    "transaction_status": "S",
                }
            ],
        ]
        start = (timezone.now() - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = (timezone.now() + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = self.staff.get("/api/reconciliation", {"from": start, "to": end})
        self.assertEqual(r.status_code, 200, r.content)
        report = r.json()
        searches = self.fake.calls("GET", r"^/v1/reporting/transactions$")
        self.assertEqual(len(searches), 4)  # 2 windows (40 days > 31) x 2 pages
        self.assertEqual(report["summary"]["matched"], 1)
        self.assertEqual(report["matched"][0]["orderId"], order["orderId"])
        self.assertTrue(report["matched"][0]["amountMatches"])
        self.assertIn(
            "UNKNOWN0000000001", [t["transactionId"] for t in report["paypalOnly"]]
        )
        self.assertEqual(report["appOnly"], [])

    def test_app_payment_missing_at_paypal_is_reported(self):
        order = self.fulfilled_order()
        start = (timezone.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = (timezone.now() + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = self.staff.get("/api/reconciliation", {"from": start, "to": end})
        self.assertEqual(r.status_code, 200, r.content)
        (missing,) = r.json()["appOnly"]
        self.assertEqual(missing["orderId"], order["orderId"])
        self.assertTrue(missing["withinReportingLag"])


class SessionTests(TestCase):
    def test_session_login_with_csrf(self):
        get_user_model().objects.create_user(
            "carol", "carol@example.com", "pw-carol-123"
        )
        client = Client(enforce_csrf_checks=True)
        token = client.get("/api/session").json()["csrfToken"]
        r = client.post(
            "/api/session",
            data=json.dumps({"username": "carol", "password": "pw-carol-123"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403)  # no CSRF token
        r = client.post(
            "/api/session",
            data=json.dumps({"username": "carol", "password": "pw-carol-123"}),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["authenticated"])


class ConfigurationTests(TestCase):
    @override_settings(
        PAYPAL_BASE_URL="https://paypal.mock.test", PAYPAL_ENVIRONMENT="sandbox"
    )
    def test_base_url_override_wins(self):
        self.assertEqual(gw.base_url(), "https://paypal.mock.test")

    @override_settings(PAYPAL_BASE_URL="", PAYPAL_ENVIRONMENT="sandbox")
    def test_sandbox_host(self):
        self.assertEqual(gw.base_url(), "https://api-m.sandbox.paypal.com")

    @override_settings(PAYPAL_BASE_URL="", PAYPAL_ENVIRONMENT="production")
    def test_unknown_environment_needs_explicit_host(self):
        with self.assertRaises(ImproperlyConfigured):
            gw.base_url()

    @override_settings(
        PAYPAL_BASE_URL="https://paypal.mock.test",
        PAYPAL_CLIENT_ID="id",
        PAYPAL_CLIENT_SECRET="secret",
        PAYPAL_ENVIRONMENT="sandbox",
    )
    def test_override_reaches_token_and_api_calls(self):
        fake = FakePayPal()
        fake.on(
            "POST",
            r"^/v1/oauth2/token$",
            lambda req, m: json_response(
                200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600}
            ),
        )
        fake.on(
            "GET",
            r"^/v2/payments/authorizations/A1$",
            lambda req, m: json_response(200, {"id": "A1", "status": "CREATED"}),
        )
        original = gw.PayPalServerSdkClient

        def build(**kwargs):
            return original(custom_http_client=fake, **kwargs)

        gw.PayPalServerSdkClient = build
        try:
            gw._build().get_authorization("A1")
        finally:
            gw.PayPalServerSdkClient = original
        urls = [r.url for r in fake.requests]
        self.assertEqual(len(urls), 2)  # the token request and the API call
        self.assertTrue(
            all(u.startswith("https://paypal.mock.test/") for u in urls), urls
        )

    def test_amount_formatting_follows_currency(self):
        self.assertEqual(gw.format_amount(D("10"), "USD"), "10.00")
        self.assertEqual(gw.format_amount(D("1000"), "JPY"), "1000")
