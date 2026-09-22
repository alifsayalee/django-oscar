"""Tests for the PayPal payments app.

Two seams are exercised:

* ``GatewayTests`` fake the SDK's *transport* (the documented test seam) so the real
  request-building and response-decoding pipeline runs with no network -- this is
  where the SDK contract (request shape, the empty-204 void, error translation) is
  pinned.
* ``ServiceTests`` / ``ViewTests`` mock the gateway module so the Oscar-side logic
  (order placement, state transitions, idempotency, ownership, staff gates) is tested
  offline and deterministically.

Run: ``python manage.py test apps.payments`` (from the ``sandbox`` directory).
"""

import json
from decimal import Decimal
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from oscar.core.loading import get_model
from oscar.test.factories import CountryFactory, create_product

from paypal import PaypalClient
from paypal.core import HttpResponse, OAuthToken

from . import gateway, services
from .models import PayPalPayment, PayPalRefund, SavedCard

Order = get_model("order", "Order")
User = get_user_model()

RAW_CARD = {
    "name": "John Doe",
    "number": "4111111111111111",
    "expiry": "2030-01",
    "security_code": "123",
    "billing_address": {
        "address_line_1": "1 Main St",
        "admin_area_2": "San Jose",
        "admin_area_1": "CA",
        "postal_code": "95131",
        "country_code": "US",
    },
}


# ---------------------------------------------------------------------------
# SDK transport seam
# ---------------------------------------------------------------------------


class StubTransport:
    """Satisfies the SDK's sync transport protocol (send + close)."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        pass

    @property
    def last_request(self):
        return self.requests[-1] if self.requests else None


class StubTokenSource:
    def fetch(self, credentials):
        return OAuthToken(access_token="test-token", token_type="Bearer")


def json_response(status, body):
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


ORDER_WITH_AUTH = {
    "id": "ORDER1",
    "status": "COMPLETED",
    "purchase_units": [
        {
            "payments": {
                "authorizations": [
                    {
                        "id": "AUTH1",
                        "status": "CREATED",
                        "expiration_time": "2026-10-21T07:20:32Z",
                    }
                ]
            }
        }
    ],
}

CAPTURE_BODY = {
    "id": "CAP1",
    "status": "COMPLETED",
    "amount": {"currency_code": "USD", "value": "15.00"},
    "seller_receivable_breakdown": {
        "gross_amount": {"currency_code": "USD", "value": "15.00"},
        "paypal_fee": {"currency_code": "USD", "value": "0.88"},
        "net_amount": {"currency_code": "USD", "value": "14.12"},
    },
}


class GatewayTests(TestCase):
    def _client(self, *responses):
        transport = StubTransport(*responses)
        client = PaypalClient(
            custom_http_client=transport,
            oauth2={"client_id": "id", "client_secret": "secret"},
            oauth2_token_source=StubTokenSource(),
        )
        gateway._client = client
        return transport

    def tearDown(self):
        gateway.reset_client()

    def test_create_authorized_order_builds_request_and_parses_auth(self):
        transport = self._client(json_response(201, ORDER_WITH_AUTH))
        result = gateway.create_authorized_order(
            amount=Decimal("15.00"),
            currency="USD",
            invoice_id="oscar-100001",
            custom_id="100001",
            request_id="pay-100001",
            card=RAW_CARD,
        )
        req = transport.last_request
        self.assertEqual(req.method, "POST")
        self.assertTrue(req.url.endswith("/v2/checkout/orders"))
        self.assertEqual(req.body.value["intent"], "AUTHORIZE")
        self.assertEqual(
            req.body.value["purchase_units"][0]["amount"]["value"], "15.00"
        )
        self.assertEqual(
            req.body.value["payment_source"]["card"]["number"], "4111111111111111"
        )
        # Idempotency key rides as the PayPal-Request-Id header (lowercased).
        self.assertEqual(req.headers["paypal-request-id"], "pay-100001")
        self.assertEqual(result["authorization_id"], "AUTH1")
        self.assertEqual(result["authorization_status"], "CREATED")
        self.assertIsNotNone(result["authorization_expiry"])

    def test_pay_with_vault_id_sends_no_pan(self):
        transport = self._client(json_response(201, ORDER_WITH_AUTH))
        gateway.create_authorized_order(
            amount=Decimal("9.99"),
            currency="USD",
            invoice_id="oscar-1",
            custom_id="1",
            request_id="pay-1",
            vault_id="VAULT123",
        )
        card = transport.last_request.body.value["payment_source"]["card"]
        self.assertEqual(card["vault_id"], "VAULT123")
        self.assertNotIn("number", card)

    def test_declined_authorization_raises(self):
        body = {"id": "ORDER1", "status": "COMPLETED", "purchase_units": [{"payments": {}}]}
        self._client(json_response(201, body))
        with self.assertRaises(gateway.PayPalError) as ctx:
            gateway.create_authorized_order(
                amount=Decimal("15.00"), currency="USD", invoice_id="i",
                custom_id="1", request_id="r", card=RAW_CARD,
            )
        self.assertEqual(ctx.exception.status_code, 402)

    def test_capture_parses_fee_and_net(self):
        self._client(json_response(201, CAPTURE_BODY))
        result = gateway.capture(authorization_id="AUTH1", request_id="capture-1")
        self.assertEqual(result["capture_id"], "CAP1")
        self.assertEqual(result["gross_amount"], Decimal("15.00"))
        self.assertEqual(result["paypal_fee"], Decimal("0.88"))
        self.assertEqual(result["net_amount"], Decimal("14.12"))

    def test_void_handles_empty_204_body(self):
        # PayPal returns 204 with an empty body -> the SDK decoder raises; the gateway
        # must treat that as success and confirm via a re-read.
        self._client(
            HttpResponse(status_code=204, headers={}),
            json_response(200, {"id": "AUTH1", "status": "VOIDED"}),
        )
        result = gateway.void("AUTH1")
        self.assertEqual(result["status"], "VOIDED")

    def test_typed_error_is_translated_with_issues(self):
        body = {
            "name": "UNPROCESSABLE_ENTITY",
            "message": "The card was declined.",
            "debug_id": "abc123",
            "details": [{"issue": "CARD_DECLINED", "description": "Declined by issuer."}],
        }
        self._client(json_response(422, body))
        with self.assertRaises(gateway.PayPalError) as ctx:
            gateway.capture(authorization_id="AUTH1", request_id="c")
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertTrue(any("CARD_DECLINED" in i for i in ctx.exception.issues))

    def test_transport_failure_is_outcome_unknown(self):
        self._client(httpx.ConnectError("refused"))
        with self.assertRaises(gateway.PayPalError) as ctx:
            gateway.capture(authorization_id="AUTH1", request_id="c")
        self.assertTrue(ctx.exception.outcome_unknown)
        self.assertEqual(ctx.exception.status_code, 504)

    def test_search_transactions_page_parses_items(self):
        body = {
            "transaction_details": [
                {
                    "transaction_info": {
                        "transaction_id": "T1",
                        "invoice_id": "oscar-100001",
                        "transaction_amount": {"currency_code": "USD", "value": "15.00"},
                        "transaction_status": "S",
                    }
                }
            ],
            "total_pages": 3,
            "page": 1,
        }
        self._client(json_response(200, body))
        items, total_pages = gateway.search_transactions_page(
            start_date="2026-08-01T00:00:00-0000",
            end_date="2026-08-31T00:00:00-0000",
            page=1,
        )
        self.assertEqual(total_pages, 3)
        self.assertEqual(items[0]["invoice_id"], "oscar-100001")
        self.assertEqual(items[0]["amount"], "15.00")


# ---------------------------------------------------------------------------
# service / view layer with the gateway mocked
# ---------------------------------------------------------------------------


@override_settings(
    PAYPAL_CURRENCY="USD",
    HAYSTACK_SIGNAL_PROCESSOR="haystack.signals.BaseSignalProcessor",
)
class ServiceTestBase(TestCase):
    def setUp(self):
        CountryFactory(iso_3166_1_a2="US", is_shipping_country=True)
        CountryFactory(iso_3166_1_a2="GB", is_shipping_country=True)
        self.product = create_product(price=Decimal("15.00"), num_in_stock=100)
        self.shopper = User.objects.create_user(
            username="shopper1", email="s1@example.com", password="pw-123456"
        )
        self.other = User.objects.create_user(
            username="shopper2", email="s2@example.com", password="pw-123456"
        )
        self.staff = User.objects.create_user(
            username="op", email="op@example.com", password="pw-123456", is_staff=True
        )

    def _place(self, user=None):
        return services.place_order(
            user or self.shopper, [{"product_id": self.product.id, "quantity": 1}]
        )

    def _auth_result(self):
        return {
            "paypal_order_id": "ORDER1",
            "order_status": "COMPLETED",
            "authorization_id": "AUTH1",
            "authorization_status": "CREATED",
            "authorization_expiry": None,
        }

    def _capture_result(self):
        return {
            "capture_id": "CAP1",
            "status": "COMPLETED",
            "gross_amount": Decimal("15.00"),
            "paypal_fee": Decimal("0.88"),
            "net_amount": Decimal("14.12"),
            "currency": "USD",
        }


class ServiceTests(ServiceTestBase):
    def test_place_order_creates_awaiting_payment(self):
        order = self._place()
        self.assertEqual(order.status, "Pending")
        self.assertEqual(order.paypal_payment.status, PayPalPayment.AWAITING_PAYMENT)
        self.assertEqual(order.currency, "USD")
        self.assertEqual(order.total_incl_tax, Decimal("15.00"))

    def test_place_order_rejects_unknown_product(self):
        with self.assertRaises(services.ServiceError):
            services.place_order(self.shopper, [{"product_id": 999999, "quantity": 1}])

    def test_pay_is_idempotent(self):
        order = self._place()
        with mock.patch.object(
            gateway, "create_authorized_order", return_value=self._auth_result()
        ) as m:
            services.pay_order(order, raw_card=RAW_CARD)
            services.pay_order(order, raw_card=RAW_CARD)  # double click
        self.assertEqual(m.call_count, 1)
        order.refresh_from_db()
        self.assertEqual(order.paypal_payment.status, PayPalPayment.AUTHORIZED)
        self.assertEqual(order.status, "Being processed")

    def test_fulfil_captures_and_records_breakdown(self):
        order = self._place()
        with mock.patch.object(
            gateway, "create_authorized_order", return_value=self._auth_result()
        ):
            services.pay_order(order, raw_card=RAW_CARD)
        with mock.patch.object(
            gateway, "capture", return_value=self._capture_result()
        ) as m:
            services.fulfil_order(order)
            services.fulfil_order(order)  # idempotent
        self.assertEqual(m.call_count, 1)
        payment = order.paypal_payment
        payment.refresh_from_db()
        self.assertEqual(payment.status, PayPalPayment.CAPTURED)
        self.assertEqual(payment.paypal_fee, Decimal("0.88"))
        self.assertEqual(payment.net_amount, Decimal("14.12"))
        order.refresh_from_db()
        self.assertEqual(order.status, "Complete")

    def test_cancel_voids_before_fulfilment(self):
        order = self._place()
        with mock.patch.object(
            gateway, "create_authorized_order", return_value=self._auth_result()
        ):
            services.pay_order(order, raw_card=RAW_CARD)
        with mock.patch.object(gateway, "void", return_value={"status": "VOIDED"}) as m:
            services.cancel_order(order)
        m.assert_called_once()
        order.refresh_from_db()
        self.assertEqual(order.paypal_payment.status, PayPalPayment.VOIDED)
        self.assertEqual(order.status, "Cancelled")

    def test_cannot_cancel_after_capture(self):
        order = self._place_and_capture()
        with self.assertRaises(services.ServiceError):
            services.cancel_order(order)

    def _place_and_capture(self):
        order = self._place()
        with mock.patch.object(
            gateway, "create_authorized_order", return_value=self._auth_result()
        ):
            services.pay_order(order, raw_card=RAW_CARD)
        with mock.patch.object(
            gateway, "capture", return_value=self._capture_result()
        ):
            services.fulfil_order(order)
        return order

    def test_refund_idempotent_and_capped(self):
        order = self._place_and_capture()
        with mock.patch.object(
            gateway, "refund", return_value={"refund_id": "REF1", "status": "COMPLETED"}
        ) as m:
            r1 = services.refund_order(order, amount="5.00", idempotency_key="k1")
            r2 = services.refund_order(order, amount="5.00", idempotency_key="k1")
        self.assertEqual(m.call_count, 1)  # same key -> one PayPal call
        self.assertEqual(r1.refund_id, r2.refund_id)
        payment = order.paypal_payment
        payment.refresh_from_db()
        self.assertEqual(payment.status, PayPalPayment.PARTIALLY_REFUNDED)
        # Over-refund beyond the captured amount is refused.
        with self.assertRaises(services.ServiceError):
            services.refund_order(order, amount="100.00", idempotency_key="k2")

    def test_two_distinct_partial_refunds_allowed(self):
        order = self._place_and_capture()
        with mock.patch.object(
            gateway, "refund",
            side_effect=[
                {"refund_id": "REF1", "status": "COMPLETED"},
                {"refund_id": "REF2", "status": "COMPLETED"},
            ],
        ):
            services.refund_order(order, amount="5.00", idempotency_key="k1")
            services.refund_order(order, amount="10.00", idempotency_key="k2")
        payment = order.paypal_payment
        payment.refresh_from_db()
        self.assertEqual(payment.total_refunded, Decimal("15.00"))
        self.assertEqual(payment.status, PayPalPayment.REFUNDED)

    def test_stale_authorization_is_renewed_then_captured(self):
        order = self._place()
        with mock.patch.object(
            gateway, "create_authorized_order", return_value=self._auth_result()
        ):
            services.pay_order(order, raw_card=RAW_CARD)
        expired = gateway.PayPalError("AUTHORIZATION expired", status_code=422)
        renewed = {
            "authorization_id": "AUTH2",
            "status": "CREATED",
            "expiry": None,
        }
        with mock.patch.object(
            gateway, "capture",
            side_effect=[expired, self._capture_result()],
        ) as cap, mock.patch.object(
            gateway, "reauthorize", return_value=renewed
        ) as reauth:
            services.fulfil_order(order)
        reauth.assert_called_once()
        self.assertEqual(cap.call_count, 2)
        order.refresh_from_db()
        self.assertEqual(order.paypal_payment.status, PayPalPayment.CAPTURED)
        self.assertEqual(order.paypal_payment.authorization_id, "AUTH2")

    def test_unrenewable_authorization_gives_actionable_error(self):
        order = self._place()
        with mock.patch.object(
            gateway, "create_authorized_order", return_value=self._auth_result()
        ):
            services.pay_order(order, raw_card=RAW_CARD)
        expired = gateway.PayPalError("AUTHORIZATION expired", status_code=422)
        cannot = gateway.PayPalError("cannot reauthorize", status_code=422)
        with mock.patch.object(gateway, "capture", side_effect=expired), mock.patch.object(
            gateway, "reauthorize", side_effect=cannot
        ):
            with self.assertRaises(services.ServiceError) as ctx:
                services.fulfil_order(order)
        self.assertIn("pay again", ctx.exception.message)

    def test_saved_card_scoped_to_owner(self):
        with mock.patch.object(
            gateway, "vault_card",
            return_value={
                "vault_id": "V1", "customer_id": "C1", "brand": "VISA",
                "last_digits": "1111", "expiry": "2030-01", "name": "John Doe",
            },
        ):
            card = services.save_card(self.shopper, RAW_CARD)
        self.assertEqual(services.list_cards(self.shopper), [card])
        self.assertEqual(services.list_cards(self.other), [])
        # Another shopper cannot delete it.
        with self.assertRaises(services.ServiceError):
            services.delete_card(self.other, "V1")

    def test_delete_card_makes_it_unusable(self):
        with mock.patch.object(
            gateway, "vault_card",
            return_value={
                "vault_id": "V1", "customer_id": "C1", "brand": "VISA",
                "last_digits": "1111", "expiry": "2030-01", "name": "John Doe",
            },
        ):
            services.save_card(self.shopper, RAW_CARD)
        with mock.patch.object(gateway, "delete_vault_token") as m:
            services.delete_card(self.shopper, "V1")
        m.assert_called_once_with("V1")
        self.assertFalse(SavedCard.objects.filter(vault_id="V1").exists())
        order = self._place()
        with self.assertRaises(services.ServiceError):
            services.pay_order(order, saved_card_id="V1")


class ViewTests(ServiceTestBase):
    def _login(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_authentication_required(self):
        self.assertEqual(Client().get("/api/my-orders").status_code, 401)

    def test_full_flow_over_http(self):
        c = self._login(self.shopper)
        resp = c.post(
            "/api/orders",
            data=json.dumps({"items": [{"product_id": self.product.id, "quantity": 1}]}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        order_id = resp.json()["orderId"]

        with mock.patch.object(
            gateway, "create_authorized_order", return_value=self._auth_result()
        ):
            resp = c.post(
                f"/api/orders/{order_id}/pay",
                data=json.dumps({"card": RAW_CARD}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["payment"]["state"], "authorized")

        # ownership: another shopper gets 404 (not 403 -- do not leak existence)
        other = self._login(self.other)
        self.assertEqual(
            other.post(f"/api/orders/{order_id}/pay",
                       data=json.dumps({"card": RAW_CARD}),
                       content_type="application/json").status_code,
            404,
        )
        # staff gate on fulfil
        self.assertEqual(c.post(f"/api/orders/{order_id}/fulfil").status_code, 403)

        staff = self._login(self.staff)
        with mock.patch.object(
            gateway, "capture", return_value=self._capture_result()
        ):
            resp = staff.post(f"/api/orders/{order_id}/fulfil")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["payment"]["state"], "captured")

        with mock.patch.object(
            gateway, "refund", return_value={"refund_id": "REF1", "status": "COMPLETED"}
        ):
            resp = c.post(
                f"/api/orders/{order_id}/refunds",
                data=json.dumps({"amount": "5.00", "idempotencyKey": "k1"}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["refundId"], "REF1")

    def test_reconciliation_requires_staff(self):
        shopper = self._login(self.shopper)
        self.assertEqual(
            shopper.get("/api/reconciliation?from=2026-08-01T00:00:00Z&to=2026-08-02T00:00:00Z").status_code,
            403,
        )

    def test_save_card_over_http_hides_pan(self):
        c = self._login(self.shopper)
        with mock.patch.object(
            gateway, "vault_card",
            return_value={
                "vault_id": "V1", "customer_id": "C1", "brand": "VISA",
                "last_digits": "1111", "expiry": "2030-01", "name": "John Doe",
            },
        ):
            resp = c.post(
                "/api/payment-methods",
                data=json.dumps(RAW_CARD),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["paymentMethodId"], "V1")
        self.assertNotIn("4111", resp.content.decode())
