"""End-to-end self-check that drives the PayPal checkout API against the real
PayPal sandbox, using Django's test client so every call goes through the real
HTTP endpoints (auth, views, services, SDK).

    sandbox/manage.py paypal_selfcheck

Exercises: place order -> authorize with the sandbox test card -> fulfil
(capture, with fee/net) -> partial refund; save a card -> reuse it to pay a
second order; cancel (void) a third order before fulfilment; my-orders;
reconciliation; delete a saved card.
"""

import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.test import Client

from oscar.apps.partner.strategy import Selector
from oscar.core.loading import get_model

Product = get_model("catalogue", "Product")

TEST_CARD = {
    "number": "4111111111111111",
    "expiry": "2030-01",
    "security_code": "123",
    "name": "Sandbox Buyer",
    "billing_address": {
        "address_line_1": "1 Main St",
        "admin_area_2": "San Jose",
        "admin_area_1": "CA",
        "postal_code": "95131",
        "country_code": "US",
    },
}


class Command(BaseCommand):
    help = "Drive the PayPal checkout API end to end against the PayPal sandbox."

    def handle(self, *args, **options):
        import logging

        for name in ("httpcore", "httpx", "urllib3"):
            logging.getLogger(name).setLevel(logging.WARNING)

        self.failures = 0
        User = get_user_model()

        shopper, _ = User.objects.get_or_create(
            username="selfcheck_shopper",
            defaults={"email": "selfcheck_shopper@example.com"},
        )
        shopper.is_staff = False
        shopper.save()
        operator, _ = User.objects.get_or_create(
            username="selfcheck_operator",
            defaults={"email": "selfcheck_operator@example.com", "is_staff": True},
        )
        operator.is_staff = True
        operator.save()

        product_ids = self._purchasable_products(2)
        if not product_ids:
            self.stderr.write("No purchasable products found; load the catalogue first.")
            return
        pid = product_ids[0]

        shopper_c = Client(SERVER_NAME="localhost")
        shopper_c.force_login(shopper)
        op_c = Client(SERVER_NAME="localhost")
        op_c.force_login(operator)

        # 1) Place an order.
        r = self._post(shopper_c, "/api/orders", {"items": [{"productId": pid, "quantity": 1}]})
        order_id = r["orderId"]
        self._check("place order", r.get("paymentState") == "AWAITING_PAYMENT")

        # 2) Pay (authorize) with the sandbox test card.
        r = self._post(shopper_c, f"/api/orders/{order_id}/pay", {"card": TEST_CARD})
        self._check("authorize (hold)", r.get("paymentState") == "AUTHORIZED" and r.get("authorizationId"))
        auth_amount = r.get("amount")

        # Idempotency: paying again must not create a second hold.
        r2 = self._post(shopper_c, f"/api/orders/{order_id}/pay", {"card": TEST_CARD})
        self._check("authorize idempotent", r2.get("authorizationId") == r.get("authorizationId"))

        # 3) Fulfil (capture) as operator; fee/net must be reported.
        r = self._post(op_c, f"/api/orders/{order_id}/fulfil", {})
        self._check(
            "capture (fee/net reported)",
            r.get("paymentState") == "CAPTURED"
            and r.get("captureId")
            and r.get("paypalFee") is not None
            and r.get("netAmount") is not None,
        )
        self.stdout.write(
            f"    captured={r.get('capturedAmount')} fee={r.get('paypalFee')} net={r.get('netAmount')}"
        )

        # 4) Partial refund with an idempotency key.
        r = self._post(
            shopper_c,
            f"/api/orders/{order_id}/refunds",
            {"amount": "1.00", "idempotencyKey": "selfcheck-refund-1"},
        )
        refund_id = r.get("refundId")
        self._check("partial refund", bool(refund_id))
        # Repeat under same key -> must not refund twice.
        r_dup = self._post(
            shopper_c,
            f"/api/orders/{order_id}/refunds",
            {"amount": "1.00", "idempotencyKey": "selfcheck-refund-1"},
        )
        self._check("refund idempotent", r_dup.get("refundId") == refund_id)
        # Over-refund must be rejected.
        r_over, status = self._post_status(
            shopper_c,
            f"/api/orders/{order_id}/refunds",
            {"amount": "9999.00", "idempotencyKey": "selfcheck-refund-2"},
        )
        self._check("over-refund rejected", status == 409)

        # 5) Save a card and reuse it to pay a second order.
        r = self._post(shopper_c, "/api/payment-methods", {"card": TEST_CARD})
        pm_id = r.get("paymentMethodId")
        self._check(
            "save card (safe display, no PAN)",
            bool(pm_id) and r.get("maskedNumber", "").endswith("1111") and "4111" not in r.get("maskedNumber", ""),
        )

        r = self._post(shopper_c, "/api/orders", {"items": [{"productId": product_ids[-1], "quantity": 1}]})
        order2 = r["orderId"]
        r = self._post(shopper_c, f"/api/orders/{order2}/pay", {"paymentMethodId": pm_id})
        self._check("pay with saved card", r.get("paymentState") == "AUTHORIZED" and r.get("authorizationId"))

        # 6) Cancel (void) a third order before fulfilment.
        r = self._post(shopper_c, "/api/orders", {"items": [{"productId": pid, "quantity": 1}]})
        order3 = r["orderId"]
        self._post(shopper_c, f"/api/orders/{order3}/pay", {"card": TEST_CARD})
        r = self._post(op_c, f"/api/orders/{order3}/cancel", {})
        self._check("cancel (void hold)", r.get("paymentState") == "CANCELLED")

        # 7) Access control: shopper cannot fulfil.
        _, status = self._post_status(shopper_c, f"/api/orders/{order2}/fulfil", {})
        self._check("shopper cannot fulfil (403)", status == 403)

        # 8) my-orders lists the caller's orders with payment state.
        r = self._get(shopper_c, "/api/my-orders")
        self._check("my-orders", isinstance(r.get("orders"), list) and len(r["orders"]) >= 3)

        # 9) Reconciliation (operator) over a recent window (PayPal limits each
        # query to 31 days; the report chunks a wider range itself). A just-created
        # range can come back empty due to PayPal's reporting lag - that is
        # expected, so we only assert the report is well-formed.
        import datetime as dt

        now = dt.datetime.now(dt.timezone.utc)
        frm = (now - dt.timedelta(days=35)).strftime("%Y-%m-%dT%H:%M:%SZ")
        to = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        r = self._get(op_c, f"/api/reconciliation?from={frm}&to={to}")
        self._check(
            "reconciliation report",
            "matched" in r and "onlyInPayPal" in r and "onlyInApp" in r,
        )
        self.stdout.write(
            f"    paypalTxns={r.get('paypalTransactionCount')} appOrders={r.get('appOrderCount')} matched={len(r.get('matched', []))}"
        )

        # 10) Delete the saved card; it must no longer be usable.
        status = self._delete(shopper_c, f"/api/payment-methods/{pm_id}")
        self._check("delete saved card", status == 200)
        r = self._get(shopper_c, "/api/payment-methods")
        gone = all(c.get("paymentMethodId") != pm_id for c in r.get("paymentMethods", []))
        self._check("deleted card no longer listed", gone)

        if self.failures:
            self.stderr.write(self.style.ERROR(f"\n{self.failures} check(s) FAILED"))
        else:
            self.stdout.write(self.style.SUCCESS("\nAll checks passed."))

    # ---- helpers ----
    def _purchasable_products(self, n):
        strategy = Selector().strategy()
        ids = []
        for product in Product.objects.all():
            info = strategy.fetch_for_product(product)
            if info.stockrecord is not None and info.availability.is_available_to_buy:
                ids.append(product.id)
            if len(ids) >= n:
                break
        while ids and len(ids) < n:
            ids.append(ids[0])
        return ids

    def _post(self, client, url, payload):
        resp = client.post(url, data=json.dumps(payload), content_type="application/json")
        return self._json(resp)

    def _post_status(self, client, url, payload):
        resp = client.post(url, data=json.dumps(payload), content_type="application/json")
        return self._json(resp), resp.status_code

    def _get(self, client, url):
        return self._json(client.get(url))

    def _delete(self, client, url):
        return client.delete(url).status_code

    def _json(self, resp):
        try:
            return json.loads(resp.content.decode("utf-8"))
        except ValueError:
            return {"_raw": resp.content.decode("utf-8", "replace"), "_status": resp.status_code}

    def _check(self, label, ok):
        if ok:
            self.stdout.write(self.style.SUCCESS(f"[PASS] {label}"))
        else:
            self.failures += 1
            self.stdout.write(self.style.ERROR(f"[FAIL] {label}"))
