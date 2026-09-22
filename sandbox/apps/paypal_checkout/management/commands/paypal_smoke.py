"""Live self-verification of the PayPal checkout API against the sandbox.

Drives every endpoint end to end through the real URLConf/views and the live
PayPal sandbox (no browser), using Django's in-process test client with session
login, and prints a PASS/FAIL line per check.

    sandbox/manage.py paypal_smoke [--product-id N]

It creates its own throwaway shopper/operator users and places real (sandbox)
orders, so run it against the sandbox only. Requires the PAYPAL_* environment
variables to be set (see sandbox/settings.py).
"""
import datetime
import json
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.test import Client
from oscar.core.loading import get_model

User = get_user_model()
StockRecord = get_model("partner", "StockRecord")

CARD = {
    "number": "4111111111111111", "expiry": "2029-12", "security_code": "123",
    "name": "Jane Doe",
    "billing_address": {"address_line_1": "1 Main St", "admin_area_2": "San Jose",
                        "admin_area_1": "CA", "postal_code": "95131", "country_code": "US"},
}


class Command(BaseCommand):
    help = "Verify the PayPal checkout API end to end against the live sandbox."

    def add_arguments(self, parser):
        parser.add_argument("--product-id", type=int, default=None,
                            help="Catalogue product id to purchase (defaults to the first with a price).")

    def handle(self, *args, **opts):
        if "testserver" not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS = list(settings.ALLOWED_HOSTS) + ["testserver"]

        product_id = opts["product_id"] or self._first_priced_product()
        if product_id is None:
            self.stderr.write("No purchasable product found; load the catalogue fixtures first.")
            return

        self.failures = []
        shopper = self._user("smoke_shopper", is_staff=False)
        shopper2 = self._user("smoke_shopper2", is_staff=False)
        operator = self._user("smoke_operator", is_staff=True)
        sc = self._client(shopper)
        sc2 = self._client(shopper2)
        oc = self._client(operator)

        # Flow 2: save a card.
        r = sc.post("/api/payment-methods", data=json.dumps({"card": CARD}),
                    content_type="application/json")
        b = self._json(r)
        pmid = b.get("paymentMethodId")
        self._check("save card returns id + safe description (no PAN)",
                    r.status_code == 201 and pmid and b.get("lastDigits") == "1111"
                    and "4111" not in json.dumps(b), b)

        # Flow 1: place -> pay -> fulfil -> partial refunds.
        order1 = self._json(sc.post("/api/orders",
                                    data=json.dumps({"items": [{"product_id": product_id, "quantity": 2}]}),
                                    content_type="application/json")).get("orderId")
        self._check("place order returns orderId", bool(order1), order1)

        b = self._json(sc.post(f"/api/orders/{order1}/pay",
                               data=json.dumps({"card": CARD}), content_type="application/json"))
        self._check("pay authorizes (hold, not taken)",
                    b.get("paymentStatus") == "authorized" and b.get("capturedAmount") == "0.00", b)
        auth_id = b.get("authorizationId")
        b2 = self._json(sc.post(f"/api/orders/{order1}/pay",
                                data=json.dumps({"card": CARD}), content_type="application/json"))
        self._check("pay is idempotent (no double authorize)", b2.get("authorizationId") == auth_id, b2)

        self._check("shopper cannot fulfil (operator only)",
                    sc.post(f"/api/orders/{order1}/fulfil").status_code == 403)

        b = self._json(oc.post(f"/api/orders/{order1}/fulfil"))
        self._check("operator fulfil captures with fee/net",
                    b.get("paymentStatus") == "captured" and b.get("captureId")
                    and b.get("paypalFee") not in (None, "0.00")
                    and b.get("netAmount") not in (None, "0.00"), b)

        key = "smoke-" + uuid.uuid4().hex[:8]
        b = self._json(sc.post(f"/api/orders/{order1}/refunds",
                               data=json.dumps({"amount": "5.00", "idempotency_key": key}),
                               content_type="application/json"))
        rid = b.get("refundId")
        self._check("partial refund returns refundId", bool(rid) and b.get("paymentStatus") == "partially_refunded", b)
        b = self._json(sc.post(f"/api/orders/{order1}/refunds",
                               data=json.dumps({"amount": "5.00", "idempotency_key": key}),
                               content_type="application/json"))
        self._check("repeat refund (same key) is idempotent", b.get("refundId") == rid, b)

        # Flow 2 reuse + cancel: pay a 2nd order with the saved card, then void.
        order2 = self._json(sc.post("/api/orders",
                                    data=json.dumps({"items": [{"product_id": product_id, "quantity": 1}]}),
                                    content_type="application/json")).get("orderId")
        b = self._json(sc.post(f"/api/orders/{order2}/pay",
                               data=json.dumps({"payment_method_id": pmid}),
                               content_type="application/json"))
        self._check("pay 2nd order with SAVED card", b.get("paymentStatus") == "authorized", b)
        b = self._json(oc.post(f"/api/orders/{order2}/cancel"))
        self._check("operator cancel voids the hold (no money moved)",
                    b.get("paymentStatus") == "cancelled" and b.get("capturedAmount") == "0.00", b)

        # Ownership isolation.
        self._check("another shopper cannot touch this order (404)",
                    sc2.post(f"/api/orders/{order1}/pay", data=json.dumps({"card": CARD}),
                             content_type="application/json").status_code == 404)

        # my-orders scoping.
        nums = {o["orderId"] for o in self._json(sc.get("/api/my-orders")).get("orders", [])}
        self._check("my-orders lists caller's orders", {order1, order2} <= nums)
        self._check("my-orders excludes other shoppers",
                    order1 not in {o["orderId"] for o in self._json(sc2.get("/api/my-orders")).get("orders", [])})

        # Delete card and prove it can no longer pay.
        self._check("delete saved card", sc.delete(f"/api/payment-methods/{pmid}").status_code == 200)
        order3 = self._json(sc.post("/api/orders",
                                    data=json.dumps({"items": [{"product_id": product_id, "quantity": 1}]}),
                                    content_type="application/json")).get("orderId")
        self._check("deleted card can no longer pay (404)",
                    sc.post(f"/api/orders/{order3}/pay", data=json.dumps({"payment_method_id": pmid}),
                            content_type="application/json").status_code == 404)

        # Reconciliation (operator), last 2 days.
        now = datetime.datetime.now(datetime.timezone.utc)
        frm = (now - datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S-0000")
        to = now.strftime("%Y-%m-%dT%H:%M:%S-0000")
        r = oc.get("/api/reconciliation", {"from": frm, "to": to})
        rb = self._json(r)
        self._check("reconciliation returns a report", r.status_code == 200 and "counts" in rb, rb.get("counts"))
        if r.status_code == 200:
            self.stdout.write(f"    reconciliation counts: {rb.get('counts')} "
                              "(a recent range may legitimately be empty due to PayPal reporting lag)")
        self._check("reconciliation is operator-only",
                    sc.get("/api/reconciliation", {"from": frm, "to": to}).status_code == 403)

        self.stdout.write("")
        if self.failures:
            self.stderr.write(self.style.ERROR(f"{len(self.failures)} CHECK(S) FAILED: {self.failures}"))
        else:
            self.stdout.write(self.style.SUCCESS("ALL CHECKS PASSED"))

    # -- helpers --
    def _first_priced_product(self):
        sr = StockRecord.objects.exclude(price=None).select_related("product").first()
        return sr.product_id if sr else None

    def _user(self, username, *, is_staff):
        u, _ = User.objects.get_or_create(
            username=username, defaults={"email": f"{username}@example.com", "is_staff": is_staff})
        if u.is_staff != is_staff:
            u.is_staff = is_staff
            u.save(update_fields=["is_staff"])
        return u

    def _client(self, user):
        c = Client()
        c.force_login(user)
        return c

    def _json(self, resp):
        try:
            return json.loads(resp.content)
        except Exception:
            return {}

    def _check(self, name, cond, extra=""):
        mark = self.style.SUCCESS("PASS") if cond else self.style.ERROR("FAIL")
        self.stdout.write(f"[{mark}] {name}" + (f" -- {extra}" if (extra and not cond) else ""))
        if not cond:
            self.failures.append(name)
