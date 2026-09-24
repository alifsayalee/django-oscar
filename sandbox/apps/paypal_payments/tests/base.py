import json
from decimal import Decimal
from typing import Any

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from oscar.test.factories import create_product

from . import stubs

CARD = {
    "number": "4111 1111 1111 1111", "expiry": "2030-12", "securityCode": "123", "name": "Alice Shopper",
    "billingAddress": {"line1": "1 Main St", "city": "San Jose", "state": "CA", "postalCode": "95131",
                       "countryCode": "US"},
}
PREFIX = "osctest"


@override_settings(PAYPAL_REFERENCE_PREFIX=PREFIX, PAYPAL_CURRENCY="USD")
class ApiTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        User = get_user_model()
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw-alice-123")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw-bob-123")
        self.staff = User.objects.create_user("staff", "staff@example.com", "pw-staff-123", is_staff=True)
        self.product = create_product(title="Book", price=Decimal("12.50"), num_in_stock=20)
        self.paypal = stubs.PayPalStub()
        self.addCleanup(self.paypal.restore)
        self.transport = self.paypal.transport

    # -- HTTP helpers -------------------------------------------------------

    def as_user(self, user: Any) -> None:
        self.client.force_login(user)

    def post(self, path: str, body: dict[str, Any] | None = None, **headers: str) -> Any:
        return self.client.post(path, data=json.dumps(body or {}), content_type="application/json", headers=headers)

    def place_order(self, user: Any = None, quantity: int = 2) -> str:
        self.as_user(user or self.alice)
        response = self.post("/api/orders", {"lines": [{"productId": self.product.pk, "quantity": quantity}]})
        self.assertEqual(response.status_code, 201, response.content)
        order_id: str = response.json()["orderId"]
        return order_id

    def authorized_order(self, amount: str = "25.00") -> str:
        order_id = self.place_order()
        self.transport.queue(stubs.json_response(
            201, stubs.order("PPO1", amount, auth=stubs.authorization("AUTH1", amount))))
        response = self.post("/api/orders/%s/pay" % order_id, {"card": CARD})
        self.assertEqual(response.status_code, 200, response.content)
        self.transport.requests.clear()
        return order_id

    def captured_order(self, amount: str = "25.00") -> str:
        order_id = self.authorized_order(amount)
        self.as_user(self.staff)
        self.transport.queue(stubs.json_response(201, stubs.capture("CAP1", amount)))
        response = self.post("/api/orders/%s/fulfil" % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.transport.requests.clear()
        self.as_user(self.alice)
        return order_id
