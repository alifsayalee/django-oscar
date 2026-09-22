"""Standalone end-to-end verification of the PayPal API against a running site.

Drives every flow through the HTTP API exactly as an external caller would —
session login, save card, create order, authorize, fulfil (capture), refund,
pay a second order with the saved card, cancel, reconciliation — and checks
ownership isolation and operator-only access.

Prerequisites (see the app README / task guide):
    * the sandbox server is running and reachable at PAYPAL_API_BASE
      (default http://127.0.0.1:8000)
    * `manage.py paypal_demo_users` has created the demo shopper + operator
    * PAYPAL_* env vars are configured for the *server* process

Usage:
    python -m pip install requests        # if needed
    PAYPAL_API_BASE=http://127.0.0.1:8000 python sandbox/apps/paypal_api/verify_api.py

Uses PayPal's sandbox test card 4111 1111 1111 1111. Some sandbox risk declines
(TRANSACTION_REFUSED) are transient; the card steps retry a few times.
"""
import os
import sys
import time
import uuid
import datetime

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("This script needs 'requests' (pip install requests).")

BASE = os.environ.get("PAYPAL_API_BASE", "http://127.0.0.1:8000").rstrip("/") + "/api"
SHOPPER = (os.environ.get("DEMO_SHOPPER_EMAIL", "shopper1@example.com"),
           os.environ.get("DEMO_SHOPPER_PASSWORD", "ShopPass123!"))
OPERATOR = (os.environ.get("DEMO_OPERATOR_EMAIL", "operator1@example.com"),
            os.environ.get("DEMO_OPERATOR_PASSWORD", "OpPass123!"))
SHOPPER2 = (os.environ.get("DEMO_SHOPPER2_EMAIL", "shopper2@example.com"),
            os.environ.get("DEMO_SHOPPER2_PASSWORD", "ShopPass123!"))
PRODUCT_A = int(os.environ.get("DEMO_PRODUCT_A", "9"))
PRODUCT_B = int(os.environ.get("DEMO_PRODUCT_B", "10"))

CARD = {
    "number": "4111111111111111", "expiry": "2030-01", "securityCode": "123",
    "name": "John Doe",
    "billingAddress": {"addressLine1": "1 Main St", "city": "San Jose",
                       "state": "CA", "postalCode": "95131", "countryCode": "US"},
}
PASSED: list = []
FAILED: list = []


def check(name, cond, extra=""):
    (PASSED if cond else FAILED).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))


class Client:
    def __init__(self):
        self.s = requests.Session()

    def _h(self):
        return {"X-CSRFToken": self.s.cookies.get("csrftoken") or ""}

    def get(self, path, **kw):
        return self.s.get(BASE + path, **kw)

    def post(self, path, body=None):
        return self.s.post(BASE + path, json=body, headers=self._h())

    def delete(self, path):
        return self.s.delete(BASE + path, headers=self._h())

    def login(self, email, password):
        self.get("/session")
        self.post("/session/login", {"username": email, "password": password})
        self.get("/session")
        return self


def refused(r):
    return r.status_code == 422 and ("TRANSACTION_REFUSED" in r.text or "INSTRUMENT_DECLINED" in r.text)


def pay_retry(client, path, body, tries=6, delay=4):
    for i in range(tries):
        r = client.post(path, body)
        if not refused(r) or i == tries - 1:
            return r
        time.sleep(delay)
    return r


def main():
    shopper = Client().login(*SHOPPER)
    check("shopper authenticated", shopper.get("/session").json().get("authenticated") is True)
    check("anonymous is rejected", requests.get(BASE + "/my-orders").status_code == 401)

    # save a card
    r = pay_retry(shopper, "/payment-methods", {"card": CARD})
    check("save card", r.status_code == 201 and r.json().get("last4") == "1111", r.text[:120])
    pm_id = r.json().get("paymentMethodId")
    check("saved card listed", any(c["paymentMethodId"] == pm_id
                                   for c in shopper.get("/payment-methods").json()["paymentMethods"]))

    # create + pay + fulfil + refund
    r = shopper.post("/orders", {"items": [{"productId": PRODUCT_A, "quantity": 1}]})
    check("create order", r.status_code == 201 and "orderId" in r.json(), r.text[:120])
    oid = r.json()["orderId"]

    r = pay_retry(shopper, f"/orders/{oid}/pay", {"card": CARD})
    check("authorize (hold)", r.status_code == 200 and r.json()["status"] == "AUTHORIZED", r.text[:120])
    check("double pay idempotent",
          shopper.post(f"/orders/{oid}/pay", {"card": CARD}).json()["paypal"]["authorizationId"]
          == r.json()["paypal"]["authorizationId"])
    check("shopper cannot fulfil", shopper.post(f"/orders/{oid}/fulfil", {}).status_code == 403)

    operator = Client().login(*OPERATOR)
    r = operator.post(f"/orders/{oid}/fulfil", {})
    check("operator fulfil (capture) shows fee+net",
          r.status_code == 200 and r.json()["status"] == "CAPTURED"
          and r.json()["paypal"]["fee"] is not None, r.text[:120])

    key = "refund-" + uuid.uuid4().hex
    r = shopper.post(f"/orders/{oid}/refunds", {"amount": "2.00", "idempotencyKey": key})
    check("partial refund", r.status_code == 201 and "refundId" in r.json(), r.text[:120])
    check("refund idempotent (same key)",
          shopper.post(f"/orders/{oid}/refunds", {"amount": "2.00", "idempotencyKey": key}).json().get("refundId")
          == r.json()["refundId"])
    check("over-refund rejected",
          400 <= shopper.post(f"/orders/{oid}/refunds",
                              {"amount": "9999", "idempotencyKey": uuid.uuid4().hex}).status_code < 500)

    # second order paid with the saved card, then cancelled
    o2 = shopper.post("/orders", {"items": [{"productId": PRODUCT_B, "quantity": 2}]}).json()["orderId"]
    r = pay_retry(shopper, f"/orders/{o2}/pay", {"savedCardId": pm_id})
    check("pay with saved card", r.status_code == 200 and r.json().get("paidWithSavedCardId") == pm_id, r.text[:120])
    check("operator cancel (release hold)",
          operator.post(f"/orders/{o2}/cancel", {}).json().get("status") == "CANCELLED")

    # isolation
    other = Client().login(*SHOPPER2)
    check("cannot touch another's order",
          other.post(f"/orders/{oid}/refunds", {"amount": "1", "idempotencyKey": "x"}).status_code == 404)
    check("cannot delete another's card", other.delete(f"/payment-methods/{pm_id}").status_code == 404)

    # delete saved card -> unusable
    check("delete saved card", shopper.delete(f"/payment-methods/{pm_id}").status_code == 200)
    o3 = shopper.post("/orders", {"items": [{"productId": PRODUCT_A, "quantity": 1}]}).json()["orderId"]
    check("deleted card unusable", shopper.post(f"/orders/{o3}/pay", {"savedCardId": pm_id}).status_code == 404)

    # reconciliation (operator, whole range)
    now = datetime.datetime.now(datetime.timezone.utc)
    params = {"from": (now - datetime.timedelta(days=30)).isoformat(), "to": now.isoformat()}
    r = operator.get("/reconciliation", params=params)
    check("reconciliation (operator, all pages)",
          r.status_code == 200 and "matched" in r.json(),
          f"txns={r.json().get('paypalTransactionCount')} pages={r.json().get('pagesScanned')}")
    check("shopper cannot reconcile", shopper.get("/reconciliation", params=params).status_code == 403)

    print(f"\n==== {len(PASSED)} passed, {len(FAILED)} failed ====")
    if FAILED:
        print("FAILED:", FAILED)
        sys.exit(1)
    print("All PayPal API flows verified.")


if __name__ == "__main__":
    main()
