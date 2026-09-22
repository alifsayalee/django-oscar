"""End-to-end verification of the PayPal payments API against the live sandbox.

Run it against a *running* sandbox server (see the app README). It creates two
shopper accounts and one staff account, then drives every flow over real HTTP:
place -> pay (authorize) -> fulfil (capture) -> refund; cancel (void); saved-card
save/list/reuse/delete; reconciliation; and the authentication / ownership /
staff-scoping rules.

Usage (from the repo root, with the venv active and the server running):

    python sandbox/apps/payments_api/verify_paypal.py [BASE_URL]

BASE_URL defaults to http://127.0.0.1:36400 .
"""
import datetime
import json
import os
import sys
import time
import uuid
from importlib import import_module

# Make the sandbox project importable and configured.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SANDBOX = os.path.abspath(os.path.join(_HERE, "..", ".."))
sys.path.insert(0, _SANDBOX)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")

import django  # noqa: E402

django.setup()

import requests  # noqa: E402
from django.conf import settings  # noqa: E402
from django.contrib.auth import (  # noqa: E402
    BACKEND_SESSION_KEY,
    HASH_SESSION_KEY,
    SESSION_KEY,
    get_user_model,
)

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:36400"
User = get_user_model()
CARD = {"number": "4111111111111111", "expiry": "2030-12",
        "security_code": "123", "name": "Test Buyer"}

PASS: list[str] = []
FAIL: list[str] = []


def check(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("PASS " if cond else "FAIL ") + label)


def make_user(username, staff):
    u, _ = User.objects.get_or_create(
        username=username, defaults={"email": f"{username}@example.com"}
    )
    u.email = f"{username}@example.com"
    u.is_staff = staff
    u.set_password("verifypass123")
    u.save()
    return u


def sess(user):
    engine = import_module(settings.SESSION_ENGINE)
    s = engine.SessionStore()
    s[SESSION_KEY] = str(user.pk)
    s[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
    s[HASH_SESSION_KEY] = user.get_session_auth_hash()
    s.save()
    r = requests.Session()
    r.cookies.set("sessionid", s.session_key)
    return r


def pp(resp):
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, resp.text[:300]


def do_pay(session, oid, payload, tries=6):
    """Pay, retrying only the sandbox's transient card-velocity refusal."""
    sc, body = None, None
    for attempt in range(tries):
        sc, body = pp(session.post(f"{BASE}/api/orders/{oid}/pay", json=payload))
        if sc == 422 and "TRANSACTION_REFUSED" in json.dumps(body):
            print(f"  (sandbox card-velocity refusal, retry {attempt + 1})")
            time.sleep(5)
            continue
        return sc, body
    return sc, body


def find_product_ids():
    """Two purchasable catalogue product ids (with stock)."""
    from oscar.core.loading import get_model

    SR = get_model("partner", "StockRecord")
    ids = [
        sr.product_id
        for sr in SR.objects.all()
        if (sr.num_in_stock or 0) > 0
    ]
    if len(ids) < 1:
        raise SystemExit("No purchasable products; load the catalogue fixtures first.")
    return ids[0], ids[-1]


def main():
    p1, p2 = find_product_ids()
    S = sess(make_user("verify_shopper", False))
    S2 = sess(make_user("verify_shopper2", False))
    ST = sess(make_user("verify_staff", True))

    check(pp(requests.get(BASE + "/api/my-orders"))[0] == 401, "anonymous -> 401")

    # Flow 1: place, pay, fulfil, refund
    sc, body = pp(S.post(BASE + "/api/orders",
                         json={"items": [{"product_id": p1, "quantity": 2}]}))
    check(sc == 201 and "orderId" in body, "place order -> 201 + orderId")
    oid = body["orderId"]
    print(f"  orderId={oid} amount={body['payment']['amount']} {body['payment']['currency']}")

    sc, body = do_pay(S, oid, {"card": CARD})
    check(sc == 200 and body["payment"]["status"] == "authorized",
          "pay (authorize) -> funds held")
    auth_id = body["payment"]["authorization"]["id"]

    sc, body2 = do_pay(S, oid, {"card": CARD})
    check(sc == 200 and body2["payment"]["authorization"]["id"] == auth_id,
          "pay is idempotent (no second authorization)")

    check(pp(S.post(f"{BASE}/api/orders/{oid}/fulfil"))[0] == 403,
          "shopper cannot fulfil (operator-only) -> 403")

    sc, body = pp(ST.post(f"{BASE}/api/orders/{oid}/fulfil"))
    cap = body.get("payment", {}).get("capture") if sc == 200 else None
    check(sc == 200 and body["payment"]["status"] == "captured",
          "fulfil (capture) -> funds taken")
    check(bool(cap and cap["captured_amount"] and cap["paypal_fee"] and cap["net_amount"]),
          f"capture shows captured/fee/net: {cap}")

    k1 = uuid.uuid4().hex
    r1 = pp(S.post(f"{BASE}/api/orders/{oid}/refunds",
                   json={"amount": "5.00", "idempotencyKey": k1}))
    check(r1[0] == 201 and r1[1].get("refundId"), "partial refund -> 201 + refundId")
    r1b = pp(S.post(f"{BASE}/api/orders/{oid}/refunds",
                    json={"amount": "5.00", "idempotencyKey": k1}))
    check(r1b[1].get("refundId") == r1[1]["refundId"],
          "refund idempotent (same key -> same refundId)")
    r2 = pp(S.post(f"{BASE}/api/orders/{oid}/refunds",
                   json={"amount": "3.00", "idempotencyKey": uuid.uuid4().hex}))
    check(r2[0] == 201 and r2[1]["refundId"] != r1[1]["refundId"],
          "distinct partial refund (new key)")
    over = pp(S.post(f"{BASE}/api/orders/{oid}/refunds",
                     json={"amount": "1000.00", "idempotencyKey": uuid.uuid4().hex}))
    check(over[0] == 422, "over-refund rejected -> 422")

    # Flow 1b: cancel (void) before fulfilment
    sc, body = pp(S.post(BASE + "/api/orders",
                         json={"items": [{"product_id": p2, "quantity": 1}]}))
    oid2 = body["orderId"]
    do_pay(S, oid2, {"card": CARD})
    sc, body = pp(ST.post(f"{BASE}/api/orders/{oid2}/cancel"))
    check(sc == 200 and body["payment"]["status"] == "voided",
          "cancel (void) -> hold released")

    # Ownership
    check(pp(S2.post(f"{BASE}/api/orders/{oid}/refunds",
                     json={"amount": "1.00", "idempotencyKey": uuid.uuid4().hex}))[0] == 404,
          "shopper cannot act on another shopper's order -> 404")

    # Flow 2: saved cards
    sc, body = pp(S.post(BASE + "/api/payment-methods",
                         json={"card": CARD, "label": "my visa"}))
    pmid = body.get("paymentMethodId")
    check(sc == 201 and pmid and body.get("last_digits") == "1111"
          and "number" not in json.dumps(body),
          f"save card -> 201 ({body.get('brand')} ****{body.get('last_digits')}), no PAN stored")
    check(any(pm["paymentMethodId"] == pmid
              for pm in pp(S.get(BASE + "/api/payment-methods"))[1]["paymentMethods"]),
          "saved card appears in caller's list")
    check(all(pm["paymentMethodId"] != pmid
              for pm in pp(S2.get(BASE + "/api/payment-methods"))[1].get("paymentMethods", [])),
          "another shopper cannot see the saved card")

    sc, body = pp(S.post(BASE + "/api/orders",
                         json={"items": [{"product_id": p1, "quantity": 1}]}))
    oid3 = body["orderId"]
    sc, body = do_pay(S, oid3, {"paymentMethodId": pmid})
    check(sc == 200 and body["payment"]["status"] == "authorized",
          "pay a second order with the SAVED card -> authorized")
    check(pp(ST.post(f"{BASE}/api/orders/{oid3}/fulfil"))[1]["payment"]["status"] == "captured",
          "fulfil saved-card order -> captured")

    check(pp(S2.delete(f"{BASE}/api/payment-methods/{pmid}"))[0] == 404,
          "another shopper cannot delete the saved card -> 404")
    check(pp(S.delete(f"{BASE}/api/payment-methods/{pmid}"))[0] == 200,
          "owner deletes saved card -> 200")
    check(all(pm["paymentMethodId"] != pmid
              for pm in pp(S.get(BASE + "/api/payment-methods"))[1]["paymentMethods"]),
          "deleted card no longer listed")
    sc, body = pp(S.post(BASE + "/api/orders",
                         json={"items": [{"product_id": p1, "quantity": 1}]}))
    check(pp(S.post(f"{BASE}/api/orders/{body['orderId']}/pay",
                    json={"paymentMethodId": pmid}))[0] == 404,
          "deleted card can no longer be used to pay -> 404")

    # my-orders reflects payment state
    orders = pp(S.get(BASE + "/api/my-orders"))[1]["orders"]
    check(any(o["orderId"] == oid and o["payment"]["status"] == "partially_refunded"
              for o in orders),
          "my-orders reflects payment state")

    # Reconciliation (operator)
    now = datetime.datetime.now(datetime.timezone.utc)
    frm = (now - datetime.timedelta(days=25)).isoformat()
    to = now.isoformat()
    sc, body = pp(ST.get(BASE + "/api/reconciliation", params={"from": frm, "to": to}))
    check(sc == 200 and "paypal_transaction_count" in body, "reconciliation -> 200")
    print(f"  reconciliation: {body.get('paypal_transaction_count')} PayPal txns, "
          f"{body.get('app_transaction_count')} app txns, {len(body.get('matched', []))} matched")
    check(pp(S.get(BASE + "/api/reconciliation", params={"from": frm, "to": to}))[0] == 403,
          "shopper cannot reconcile (operator-only) -> 403")

    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


if __name__ == "__main__":
    main()
