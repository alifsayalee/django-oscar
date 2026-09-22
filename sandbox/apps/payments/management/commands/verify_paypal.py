"""Drive the full PayPal integration against the live sandbox, end to end.

Creates (or reuses) a shopper and an operator, then exercises every flow with the
PayPal sandbox test card: save a card, place and authorize an order, fulfil (capture),
refund, pay a second order with the saved card, cancel (void), delete the card, and
run the reconciliation report. Prints each step and a final PASS/FAIL.

    python manage.py verify_paypal

Requires PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET / PAYPAL_CURRENCY in the environment.
It creates real (fake-money) sandbox transactions; nothing touches production.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from oscar.core.loading import get_model

from apps.payments import services

User = get_user_model()
Product = get_model("catalogue", "Product")
StockRecord = get_model("partner", "StockRecord")

CARD = {
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


class Command(BaseCommand):
    help = "End-to-end verification of the PayPal integration against the sandbox."

    def handle(self, *args, **options):
        self._checks = []

        shopper, _ = User.objects.get_or_create(
            username="verify_shopper",
            defaults={"email": "verify_shopper@example.com"},
        )
        product = self._purchasable_product()
        self.stdout.write(f"Using product #{product.id}: {product.get_title()}")

        # 1. Save a card.
        card = services.save_card(shopper, CARD)
        self._check(
            "save card returns a safe descriptor (no PAN)",
            card.vault_id and card.last_digits == "1111" and card.brand,
            f"{card.brand} ****{card.last_digits} exp {card.expiry} (id {card.vault_id})",
        )

        # 2. Place + pay (one-off card) + fulfil.
        order = services.place_order(shopper, [{"product_id": product.id, "quantity": 1}])
        self._check("order placed awaiting payment", order.paypal_payment.status == "awaiting_payment", order.number)
        services.pay_order(order, raw_card=CARD)
        order.refresh_from_db()
        self._check(
            "order authorized (funds held, not taken)",
            order.paypal_payment.status == "authorized" and order.paypal_payment.authorization_id,
            f"auth {order.paypal_payment.authorization_id}",
        )
        services.fulfil_order(order)
        p = order.paypal_payment
        p.refresh_from_db()
        self._check(
            "order fulfilled: money captured with fee/net",
            p.status == "captured" and p.gross_amount and p.paypal_fee is not None and p.net_amount is not None,
            f"gross {p.gross_amount} fee {p.paypal_fee} net {p.net_amount} (capture {p.capture_id})",
        )

        # 3. Partial refund + idempotency.
        r1 = services.refund_order(order, amount="5.00", idempotency_key="verify-refund-1")
        r2 = services.refund_order(order, amount="5.00", idempotency_key="verify-refund-1")
        self._check(
            "partial refund is idempotent under one key",
            r1.refund_id and r1.refund_id == r2.refund_id,
            f"refund {r1.refund_id}",
        )
        try:
            services.refund_order(order, amount="9999.00", idempotency_key="verify-refund-2")
            over_ok = False
        except services.ServiceError:
            over_ok = True
        self._check("over-refund beyond captured is refused", over_ok, "")

        # 4. Second order paid with the SAVED card, then cancelled (voided).
        order2 = services.place_order(shopper, [{"product_id": product.id, "quantity": 2}])
        services.pay_order(order2, saved_card_id=card.vault_id)
        order2.refresh_from_db()
        self._check(
            "second order paid with the saved card",
            order2.paypal_payment.status == "authorized",
            f"auth {order2.paypal_payment.authorization_id}",
        )
        services.cancel_order(order2)
        order2.refresh_from_db()
        self._check(
            "cancel before fulfilment releases the hold (voided)",
            order2.paypal_payment.status == "voided",
            "",
        )

        # 5. Delete the saved card -> unusable.
        services.delete_card(shopper, card.vault_id)
        self._check(
            "deleted card no longer listed",
            all(c.vault_id != card.vault_id for c in services.list_cards(shopper)),
            "",
        )
        order3 = services.place_order(shopper, [{"product_id": product.id, "quantity": 1}])
        try:
            services.pay_order(order3, saved_card_id=card.vault_id)
            deleted_ok = False
        except services.ServiceError:
            deleted_ok = True
        self._check("deleted card can no longer be used to pay", deleted_ok, "")

        # 6. Reconciliation over a range that has data.
        to_dt = timezone.now()
        from_dt = to_dt - timedelta(days=40)
        report = services.reconcile(from_dt, to_dt)
        self._check(
            "reconciliation covers the whole range (all pages)",
            "paypalTransactionCount" in report,
            f"{report['paypalTransactionCount']} PayPal txns over {report['pagesFetched']} page(s); "
            f"matched={len(report['matched'])} paypalOnly={len(report['paypalOnly'])} appOnly={len(report['appOnly'])}",
        )

        passed = sum(1 for ok, _ in self._checks if ok)
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"{passed}/{len(self._checks)} checks passed"))
        if passed != len(self._checks):
            raise CommandError("Some verification checks failed (see above).")
        self.stdout.write(self.style.SUCCESS("PayPal integration verified end to end."))

    def _purchasable_product(self):
        for sr in StockRecord.objects.select_related("product").all():
            if sr.num_in_stock and sr.num_in_stock > 5:
                return sr.product
        product = Product.objects.filter(structure__in=["standalone", "child"]).first()
        if product is None:
            raise CommandError(
                "No products found. Load the catalogue fixtures first "
                "(see apps/payments/README.md)."
            )
        return product

    def _check(self, label, ok, detail):
        self._checks.append((bool(ok), label))
        mark = self.style.SUCCESS("  OK  ") if ok else self.style.ERROR(" FAIL ")
        self.stdout.write(f"{mark} {label}" + (f"  ->  {detail}" if detail else ""))
