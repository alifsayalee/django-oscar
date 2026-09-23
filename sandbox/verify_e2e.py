"""End-to-end verification of the PayPal flows against the real sandbox.

Drives the real Django view stack (URL routing, session auth, views, services, live PayPal
SDK calls) via django.test.Client with force_login. Run from sandbox/:
    ../venv/Scripts/python verify_e2e.py
Writes real orders to db.sqlite and creates real (reversible) PayPal sandbox transactions.
"""
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

if "testserver" not in settings.ALLOWED_HOSTS:
    settings.ALLOWED_HOSTS = list(settings.ALLOWED_HOSTS) + ["testserver"]

from django.contrib.auth import get_user_model  # noqa: E402
from django.test import Client  # noqa: E402
from oscar.apps.partner.models import StockRecord  # noqa: E402

User = get_user_model()

CARD = {
    "name": "John Shopper",
    "number": "4111111111111111",
    "expiry": "2030-01",
    "securityCode": "123",
    "billingAddress": {"addressLine1": "1 Main St", "city": "San Jose", "state": "CA",
                       "postalCode": "95131", "countryCode": "US"},
}

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def call(client, method, url, body=None):
    fn = getattr(client, method)
    kwargs = {"content_type": "application/json"}
    if body is not None:
        kwargs["data"] = json.dumps(body)
    resp = fn(url, **kwargs)
    try:
        data = resp.json()
    except Exception:
        data = {"_raw": resp.content.decode()[:300]}
    return resp.status_code, data


def main():
    shopper, _ = User.objects.get_or_create(username="e2e_shopper",
                                             defaults={"email": "e2e_shopper@example.com"})
    shopper.is_staff = False
    shopper.save()
    shopper2, _ = User.objects.get_or_create(username="e2e_shopper2",
                                              defaults={"email": "e2e_shopper2@example.com"})
    operator, _ = User.objects.get_or_create(username="e2e_operator",
                                             defaults={"email": "e2e_op@example.com"})
    operator.is_staff = True
    operator.save()

    product_ids = list(
        StockRecord.objects.filter(price__isnull=False)
        .values_list("product_id", flat=True)[:2]
    )
    print("Using product ids:", product_ids)

    shopper_c = Client()
    shopper_c.force_login(shopper)
    shopper2_c = Client()
    shopper2_c.force_login(shopper2)
    op_c = Client()
    op_c.force_login(operator)
    anon_c = Client()

    print("\n== Auth guards ==")
    st, _ = call(anon_c, "post", "/api/orders", {"items": []})
    check("anonymous -> 401", st == 401, f"got {st}")

    print("\n== Flow 2: save a card ==")
    st, data = call(shopper_c, "post", "/api/payment-methods", {"card": CARD})
    check("save card -> 201 + paymentMethodId", st == 201 and "paymentMethodId" in data, str(data)[:200])
    payment_method_id = data.get("paymentMethodId")
    check("saved card shows safe descriptor (last4, brand)",
          data.get("lastDigits") == "1111" and bool(data.get("brand")), str(data)[:160])
    st, data = call(shopper_c, "get", "/api/payment-methods")
    check("list cards shows it", any(c["paymentMethodId"] == payment_method_id
                                     for c in data.get("paymentMethods", [])))
    st, data = call(shopper2_c, "get", "/api/payment-methods")
    check("other shopper does NOT see it",
          all(c["paymentMethodId"] != payment_method_id for c in data.get("paymentMethods", [])))

    print("\n== Flow 1: order 1 -> pay (raw card) -> fulfil -> refund ==")
    st, data = call(shopper_c, "post", "/api/orders",
                    {"items": [{"productId": product_ids[0], "quantity": 1},
                               {"productId": product_ids[1], "quantity": 2}]})
    check("create order -> 201 + orderId", st == 201 and "orderId" in data, str(data)[:160])
    order_id = data.get("orderId")
    total = data.get("total")
    check("order starts awaiting_payment",
          data.get("payment", {}).get("status") == "awaiting_payment")

    st, data = call(shopper2_c, "post", f"/api/orders/{order_id}/pay", {"card": CARD})
    check("other shopper cannot pay my order -> 404", st == 404, f"got {st}")

    st, data = call(shopper_c, "post", f"/api/orders/{order_id}/pay", {"card": CARD})
    pay = data.get("payment", {})
    check("pay -> authorized (hold placed)", st == 200 and pay.get("status") == "authorized",
          str(data)[:220])
    check("authorization id present", bool(pay.get("authorizationId")))
    check("hold amount equals order total to the cent", pay.get("amount") == total,
          f"hold={pay.get('amount')} total={total}")

    # idempotent double pay
    st, data2 = call(shopper_c, "post", f"/api/orders/{order_id}/pay", {"card": CARD})
    check("double pay is idempotent (same authorization id)",
          data2.get("payment", {}).get("authorizationId") == pay.get("authorizationId"))

    st, data = call(shopper_c, "post", f"/api/orders/{order_id}/fulfil", {})
    check("shopper cannot fulfil -> 403", st == 403, f"got {st}")

    st, data = call(op_c, "post", f"/api/orders/{order_id}/fulfil", {})
    pay = data.get("payment", {})
    check("operator fulfil -> captured", st == 200 and pay.get("status") == "captured",
          str(data)[:220])
    check("capture shows captured amount, fee and net",
          pay.get("capturedAmount") and pay.get("paypalFee") and pay.get("netAmount"),
          f"amt={pay.get('capturedAmount')} fee={pay.get('paypalFee')} net={pay.get('netAmount')}")

    key = f"refund-{uuid.uuid4().hex[:8]}"
    st, data = call(shopper_c, "post", f"/api/orders/{order_id}/refunds",
                    {"amount": "1.00", "idempotencyKey": key})
    check("partial refund -> 201 + refundId", st == 201 and "refundId" in data, str(data)[:200])
    refund_id = data.get("refundId")
    st, data = call(shopper_c, "post", f"/api/orders/{order_id}/refunds",
                    {"amount": "1.00", "idempotencyKey": key})
    check("repeat refund same key -> same refundId (no double refund)",
          data.get("refundId") == refund_id, str(data)[:160])
    # over-refund guard
    st, data = call(shopper_c, "post", f"/api/orders/{order_id}/refunds",
                    {"amount": "9999.00", "idempotencyKey": f"over-{uuid.uuid4().hex[:6]}"})
    check("refund beyond captured is rejected", st in (409, 422), f"got {st}: {str(data)[:120]}")

    print("\n== Flow 1+2: order 2 -> pay with SAVED card -> cancel (void) ==")
    st, data = call(shopper_c, "post", "/api/orders",
                    {"items": [{"productId": product_ids[0], "quantity": 1}]})
    order2_id = data.get("orderId")
    check("create order 2 -> 201", st == 201 and order2_id, str(data)[:120])
    st, data = call(shopper_c, "post", f"/api/orders/{order2_id}/pay",
                    {"savedCardId": payment_method_id})
    pay = data.get("payment", {})
    check("pay order 2 with SAVED card -> authorized",
          st == 200 and pay.get("status") == "authorized", str(data)[:220])
    st, data = call(op_c, "post", f"/api/orders/{order2_id}/cancel", {})
    check("operator cancel before fulfil -> voided (hold released)",
          st == 200 and data.get("payment", {}).get("status") == "voided", str(data)[:200])

    print("\n== my-orders ==")
    st, data = call(shopper_c, "get", "/api/my-orders")
    check("my-orders lists the shopper's orders with payment state",
          st == 200 and len(data.get("orders", [])) >= 2)

    print("\n== delete saved card, then it can't pay ==")
    st, data = call(shopper2_c, "delete", f"/api/payment-methods/{payment_method_id}")
    check("other shopper cannot delete my card -> 404", st == 404, f"got {st}")
    st, data = call(shopper_c, "delete", f"/api/payment-methods/{payment_method_id}")
    check("owner deletes card -> 200", st == 200, str(data)[:120])
    st, data = call(shopper_c, "get", "/api/payment-methods")
    check("deleted card no longer listed",
          all(c["paymentMethodId"] != payment_method_id for c in data.get("paymentMethods", [])))
    st, data = call(shopper_c, "post", "/api/orders",
                    {"items": [{"productId": product_ids[0], "quantity": 1}]})
    order3_id = data.get("orderId")
    st, data = call(shopper_c, "post", f"/api/orders/{order3_id}/pay",
                    {"savedCardId": payment_method_id})
    check("deleted card can no longer be used to pay -> 404", st == 404, f"got {st}")

    print("\n== reconciliation (operator) ==")
    st, data = call(op_c, "get",
                    "/api/reconciliation?from=2025-08-01T00:00:00Z&to=2025-09-23T23:59:59Z")
    check("reconciliation -> 200 with report structure",
          st == 200 and "matched" in data and "counts" in data, str(data.get("counts"))[:200])
    print("   reconciliation counts:", data.get("counts"))
    st, data = call(shopper_c, "get",
                    "/api/reconciliation?from=2025-08-01T00:00:00Z&to=2025-09-23T23:59:59Z")
    check("shopper cannot reconcile -> 403", st == 403, f"got {st}")

    print(f"\n==== RESULT: {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
