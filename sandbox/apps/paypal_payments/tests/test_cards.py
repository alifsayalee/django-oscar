from typing import Any

from paypal.core import HttpResponse

from apps.paypal_payments.models import PaymentOperation, SavedCard

from . import stubs
from .base import CARD, ApiTestCase


class SavedCardTests(ApiTestCase):
    def save(self) -> Any:
        self.transport.queue(stubs.json_response(200, stubs.payment_token("TOK1")))
        return self.post("/api/payment-methods", {"card": CARD})

    def test_save_card_returns_a_safe_description(self) -> None:
        self.as_user(self.alice)
        response = self.save()
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(set(body), {"paymentMethodId", "type", "brand", "lastDigits", "expiry", "createdAt"})
        self.assertEqual((body["brand"], body["lastDigits"], body["expiry"]), ("VISA", "1111", "2030-12"))
        self.assertNotIn("4111111111111111", response.content.decode())
        request = self.transport.requests[0]
        self.assertTrue(request.url.endswith("/v3/vault/payment-tokens"))
        self.assertTrue(request.headers["paypal-request-id"].startswith("osctest-u%s-vault-" % self.alice.pk))
        for row in list(SavedCard.objects.values()) + list(PaymentOperation.objects.values()):
            self.assertNotIn("4111111111111111", str(row))

    def test_saving_the_same_card_twice_keeps_one(self) -> None:
        self.as_user(self.alice)
        first = self.save().json()
        second = self.post("/api/payment-methods", {"card": CARD})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["paymentMethodId"], first["paymentMethodId"])
        self.assertEqual(len(self.transport.requests), 1)

    def test_cards_are_private_to_their_owner(self) -> None:
        self.as_user(self.alice)
        card_id = self.save().json()["paymentMethodId"]
        self.as_user(self.bob)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        self.assertEqual(self.client.delete("/api/payment-methods/%s" % card_id).status_code, 404)
        order_id = self.place_order(self.bob)
        response = self.post("/api/orders/%s/pay" % order_id, {"paymentMethodId": card_id})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(self.transport.requests), 1)  # only alice's save reached PayPal

    def test_pay_with_saved_card_sends_the_vault_id(self) -> None:
        self.as_user(self.alice)
        card_id = self.save().json()["paymentMethodId"]
        order_id = self.place_order()
        self.transport.queue(stubs.json_response(
            201, stubs.order("PPO1", "25.00", auth=stubs.authorization("AUTH1", "25.00"))))
        response = self.post("/api/orders/%s/pay" % order_id, {"paymentMethodId": card_id})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.transport.body(self.transport.requests[-1])["payment_source"],
                         {"card": {"vault_id": "TOK1"}})
        self.assertEqual(response.json()["payment"]["card"]["paymentMethodId"], card_id)

    def test_deleted_card_is_gone_and_unusable(self) -> None:
        self.as_user(self.alice)
        card_id = self.save().json()["paymentMethodId"]
        self.transport.queue(HttpResponse(status_code=204, headers={}))
        self.assertEqual(self.client.delete("/api/payment-methods/%s" % card_id).status_code, 204)
        self.assertTrue(self.transport.requests[-1].url.endswith("/v3/vault/payment-tokens/TOK1"))
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        order_id = self.place_order()
        self.assertEqual(self.post("/api/orders/%s/pay" % order_id, {"paymentMethodId": card_id}).status_code, 404)

    def test_card_is_removed_even_when_paypal_delete_fails(self) -> None:
        self.as_user(self.alice)
        card_id = self.save().json()["paymentMethodId"]
        self.transport.queue(stubs.json_response(500, {"name": "INTERNAL_SERVER_ERROR"}))
        response = self.client.delete("/api/payment-methods/%s" % card_id)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.client.get("/api/payment-methods").json()["paymentMethods"], [])
        # A repeat retries PayPal's side.
        self.transport.queue(HttpResponse(status_code=204, headers={}))
        self.assertEqual(self.client.delete("/api/payment-methods/%s" % card_id).status_code, 204)

    def test_rejects_malformed_cards(self) -> None:
        self.as_user(self.alice)
        bad = dict(CARD, expiry="12/30")
        self.assertEqual(self.post("/api/payment-methods", {"card": bad}).status_code, 400)
        self.assertEqual(self.transport.requests, [])
