"""
Drive every PayPal flow through the HTTP API of a running sandbox server.

    venv\\Scripts\\python sandbox\\apps\\paypal_payments\\e2e_check.py --base-url http://127.0.0.1:38440 \\
        --shopper shopper:PASSWORD --staff staff:PASSWORD --product 209 --product 207

Uses PayPal's sandbox test card. Nothing here talks to PayPal directly; every
step goes through /api/.
"""

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

TEST_CARD = {
    "number": "4111111111111111",
    "expiry": f"{datetime.now().year + 3}-12",
    "securityCode": "123",
    "name": "Sandbox Shopper",
    "billingAddress": {
        "addressLine1": "2211 North First Street",
        "city": "San Jose",
        "state": "CA",
        "postalCode": "95131",
        "countryCode": "US",
    },
}


class Api:
    def __init__(self, base_url: str, credentials: str) -> None:
        self.http = httpx.Client(base_url=base_url, timeout=300.0)
        username, password = credentials.split(":", 1)
        self.http.get("/api/csrf")
        self.call("POST", "/api/login", {"username": username, "password": password}, expect=(200,))

    def call(
        self, method: str, path: str, body: object = None, *, expect: tuple[int, ...] = (200, 201), key: str = ""
    ) -> Any:
        headers = {"X-CSRFToken": self.http.cookies.get("csrftoken") or ""}
        if key:
            headers["Idempotency-Key"] = key
        response = self.http.request(method, path, json=body, headers=headers)
        data = response.json() if response.content else None
        if response.status_code not in expect:
            raise SystemExit(f"FAIL {method} {path}: HTTP {response.status_code} {json.dumps(data)}")
        return data


def step(title: str, detail: object = "") -> None:
    print(f"  ok  {title}" + (f"  {detail}" if detail != "" else ""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:38440")
    parser.add_argument("--shopper", required=True, help="username:password of a non-staff user")
    parser.add_argument("--staff", required=True, help="username:password of an is_staff user")
    parser.add_argument("--product", type=int, action="append", required=True, help="purchasable product id")
    args = parser.parse_args()

    shopper = Api(args.base_url, args.shopper)
    staff = Api(args.base_url, args.staff)
    items = [{"productId": pid, "quantity": 2 if i == 0 else 1} for i, pid in enumerate(args.product)]
    run = uuid.uuid4().hex[:8]

    print("Flow 1: one-off card -> authorize -> fulfil (capture) -> refunds")
    order = shopper.call("POST", "/api/orders", {"items": items})
    a = order["orderId"]
    step("order placed", f"orderId={a} total={order['total']} {order['currency']} status={order['status']}")
    paid = shopper.call("POST", f"/api/orders/{a}/pay", {"card": TEST_CARD})["payment"]
    assert paid["state"] == "authorized", paid
    assert paid["amount"] == order["total"], (paid["amount"], order["total"])
    step("authorized", f"authorization={paid['authorization']['id']} amount={paid['amount']}")
    again = shopper.call("POST", f"/api/orders/{a}/pay", {"card": TEST_CARD})["payment"]
    assert again["authorization"]["id"] == paid["authorization"]["id"]
    step("double-click pay answered from the stored authorization (no second hold)")

    shopper.call("POST", f"/api/orders/{a}/fulfil", expect=(403,))
    step("shopper cannot fulfil (403)")
    captured = staff.call("POST", f"/api/orders/{a}/fulfil")["payment"]
    cap = captured["capture"]
    assert captured["state"] == "captured", captured
    step(
        "fulfilled -> captured",
        f"capture={cap['id']} gross={cap['amount']} fee={cap['paypalFee']} net={cap['netAmount']}",
    )

    key1 = f"refund-{run}-1"
    r1 = shopper.call("POST", f"/api/orders/{a}/refunds", {"amount": "5.00"}, key=key1)
    r1_again = shopper.call("POST", f"/api/orders/{a}/refunds", {"amount": "5.00"}, key=key1)
    assert r1["refundId"] == r1_again["refundId"]
    step("partial refund 5.00", f"refundId={r1['refundId']} paypal={r1['refund']['paypalRefundId']} (repeat: same)")
    r2 = shopper.call("POST", f"/api/orders/{a}/refunds", {}, key=f"refund-{run}-2")
    step(
        "refund of the remainder",
        f"refundId={r2['refundId']} amount={r2['refund']['amount']} state={r2['payment']['state']}",
    )
    over = shopper.call(
        "POST",
        f"/api/orders/{a}/refunds",
        {"amount": "1.00"},
        expect=(409, 422),
        key=f"refund-{run}-3",
    )
    step("over-refund refused", over["error"])

    print("Flow 2: saved card -> reuse on a second order -> delete")
    card = shopper.call("POST", "/api/payment-methods", {"card": TEST_CARD}, key=f"card-{run}")
    method_id = card["paymentMethodId"]
    step("card saved", f"paymentMethodId={method_id} {card['label']} exp {card['expiry']}")
    listed = shopper.call("GET", "/api/payment-methods")["paymentMethods"]
    assert any(m["paymentMethodId"] == method_id for m in listed)
    step("card listed")
    b = shopper.call("POST", "/api/orders", {"items": items[:1]})["orderId"]
    paid_b = shopper.call("POST", f"/api/orders/{b}/pay", {"paymentMethodId": method_id})["payment"]
    assert paid_b["state"] == "authorized", paid_b
    step("second order paid with the saved card", f"orderId={b} authorization={paid_b['authorization']['id']}")
    cap_b = staff.call("POST", f"/api/orders/{b}/fulfil")["payment"]
    step("second order fulfilled", f"capture={cap_b['capture']['id']} net={cap_b['capture']['netAmount']}")

    print("Flow 1b: cancel before fulfilment releases the hold")
    c = shopper.call("POST", "/api/orders", {"items": items[:1]})["orderId"]
    shopper.call("POST", f"/api/orders/{c}/pay", {"paymentMethodId": method_id})
    cancelled = staff.call("POST", f"/api/orders/{c}/cancel")["payment"]
    assert cancelled["state"] == "voided", cancelled
    step("cancelled -> authorization voided", f"orderId={c} status={cancelled['authorization']['status']}")

    shopper.call("DELETE", f"/api/payment-methods/{method_id}", expect=(204,))
    assert not any(
        m["paymentMethodId"] == method_id for m in shopper.call("GET", "/api/payment-methods")["paymentMethods"]
    )
    d = shopper.call("POST", "/api/orders", {"items": items[:1]})["orderId"]
    shopper.call("POST", f"/api/orders/{d}/pay", {"paymentMethodId": method_id}, expect=(404,))
    step("deleted card is gone and can no longer pay")

    mine = shopper.call("GET", "/api/my-orders")["orders"]
    states = {o["orderId"]: (o["payment"] or {}).get("state") for o in mine if o["orderId"] in (a, b, c, d)}
    step("my-orders", states)

    now = datetime.now(UTC).replace(microsecond=0)
    report = staff.call(
        "GET",
        f"/api/reconciliation?from={(now - timedelta(days=45)).isoformat().replace('+00:00', 'Z')}"
        f"&to={now.isoformat().replace('+00:00', 'Z')}",
    )
    step(
        "reconciliation (45 days)",
        f"paypal={report['paypalTransactionCount']} matched={len(report['matched'])} "
        f"paypalOnly={len(report['paypalOnly'])} localOnly={len(report['localOnly'])} "
        f"unsettled={len(report['unsettled'])}",
    )
    print("All flows passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
