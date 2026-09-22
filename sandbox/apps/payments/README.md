# Sandbox PayPal payments &amp; saved cards API

An **additive** Django app that makes the django-oscar sandbox actually collect money,
with **PayPal** as the processor, and lets a shopper save a card for reuse. It reuses
Oscar's own `order` and `payment` models — it does not replace the catalogue / basket /
order flow. Every PayPal interaction goes through the `paypal` Python SDK (root package
`paypal`), isolated behind `gateway.py`.

## How it works (money movement)

| Endpoint | Who | PayPal action |
| --- | --- | --- |
| `POST /api/orders` | shopper | Places an Oscar order from catalogue item ids/quantities. Starts **awaiting payment**. Returns `orderId`. |
| `POST /api/orders/{orderId}/pay` | shopper (owner) | **Authorize**: a PayPal order with `intent=AUTHORIZE` processes the card and places a **hold** equal to the order total. Does not take the money. Card details *or* a saved `paymentMethodId`. |
| `POST /api/orders/{orderId}/fulfil` | operator (`is_staff`) | **Capture** the hold — this is when the money is taken. Records the captured amount, PayPal's fee and net proceeds. A stale authorization is re-authorized first; one that can't be renewed returns an actionable error. |
| `POST /api/orders/{orderId}/cancel` | operator (`is_staff`) | **Void** the hold before fulfilment — no money moves. |
| `POST /api/orders/{orderId}/refunds` | shopper (owner) | **Refund** a captured payment, full or partial. Carries an idempotency key; a repeat under the same key never refunds twice. Returns `refundId`. |
| `GET /api/my-orders` | shopper | The caller's orders with their payment state. |
| `POST /api/payment-methods` | shopper | Vault a card. Returns `paymentMethodId` and a safe descriptor (brand/last 4/expiry) — never full card details. |
| `GET /api/payment-methods` | shopper | The caller's saved cards. |
| `DELETE /api/payment-methods/{paymentMethodId}` | shopper (owner) | Remove a saved card (deletes the PayPal vault token); afterwards it can't be listed or used. |
| `GET /api/reconciliation?from={ISO}&to={ISO}` | operator (`is_staff`) | PayPal's own transaction records for a date range, lined up against this app's orders (whole range, all pages). |

- **Auth**: the sandbox's own Django session login. Identity is `request.user`.
- **Ownership**: every shopper endpoint acts only on the caller's own orders/cards; another
  shopper's object returns `404`.
- **Idempotency**: a double-clicked pay/fulfil never charges twice; refunds are keyed.
- **Amounts** come from catalogue prices; the **currency** comes from `PAYPAL_CURRENCY`.
- **PCI**: full card number / CVV are never stored in this database and never logged.

## Configuration

Read from the environment through `sandbox/settings.py` (values are never committed):

| Setting | Meaning |
| --- | --- |
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` | Sandbox REST app credentials. |
| `PAYPAL_ENVIRONMENT` | `sandbox` (informational). |
| `PAYPAL_CURRENCY` | Currency for every charge, e.g. `USD`. |
| `PAYPAL_BASE_URL` | *Optional.* If set, used verbatim as the API base for every call (incl. the OAuth token request). Unset → SDK default sandbox host. |

## One-time setup (Python 3.11)

```bash
py -3.11 -m venv .venv
.venv\Scripts\pip install -e .[test]
.venv\Scripts\pip install "paypal @ git+https://github.com/context-plugins/paypal-python-sdk.git@main"
```

Build the sandbox database (from the repo root):

```bash
sandbox/manage.py migrate
sandbox/manage.py loaddata sandbox/fixtures/auth.json
sandbox/manage.py loaddata sandbox/fixtures/child_products.json
sandbox/manage.py oscar_populate_countries --initial-only
```

## Verify it yourself

**1 — Live end to end (one command).** Exercises every flow against the PayPal sandbox
with the test card `4111 1111 1111 1111` (save card → authorize → capture with fee/net →
refund → pay a 2nd order with the saved card → cancel/void → delete card →
reconciliation):

```bash
cd sandbox
../.venv/Scripts/python manage.py verify_paypal
```

**2 — Offline test suite** (no network; the SDK transport and the gateway are faked):

```bash
cd sandbox
../.venv/Scripts/python manage.py test apps.payments
```

**3 — Raw HTTP with curl.** Create a shopper with a known password, then drive the API.

```bash
# a staff shopper for the demo (staff so it can also fulfil/reconcile)
sandbox/manage.py shell -c "from django.contrib.auth import get_user_model as G; \
u=G().objects.create_user('demo','demo@example.com','demo-pass-123',is_staff=True); print('ok')"

# run the server on your assigned port
sandbox/manage.py runserver 127.0.0.1:8000
```

```bash
BASE=http://127.0.0.1:8000
# log in via Oscar's session login (CSRF-protected form)
curl -s -c jar.txt "$BASE/en-gb/accounts/login/" -o /dev/null
CSRF=$(awk '/csrftoken/{print $7}' jar.txt)
curl -s -b jar.txt -c jar.txt -e "$BASE/en-gb/accounts/login/" "$BASE/en-gb/accounts/login/" \
  -d "csrfmiddlewaretoken=$CSRF" -d "login-username=demo@example.com" \
  -d "login-password=demo-pass-123" -d "login_submit=1" -o /dev/null

# save a card
curl -s -b jar.txt "$BASE/api/payment-methods" -H 'Content-Type: application/json' \
  -d '{"name":"John Doe","number":"4111111111111111","expiry":"2030-01","security_code":"123",
       "billing_address":{"address_line_1":"1 Main St","admin_area_2":"San Jose",
       "admin_area_1":"CA","postal_code":"95131","country_code":"US"}}'

# place an order (use a purchasable product id, e.g. a Django T-shirt)
curl -s -b jar.txt "$BASE/api/orders" -H 'Content-Type: application/json' \
  -d '{"items":[{"product_id":9,"quantity":1}]}'          # -> {"orderId": "100001", ...}

# authorize it with the test card (hold placed, money not taken)
curl -s -b jar.txt "$BASE/api/orders/100001/pay" -H 'Content-Type: application/json' \
  -d '{"card":{"number":"4111111111111111","expiry":"2030-01","security_code":"123",
       "billing_address":{"address_line_1":"1 Main St","admin_area_2":"San Jose",
       "admin_area_1":"CA","postal_code":"95131","country_code":"US"}}}'

# ... or pay with the saved card:  -d '{"paymentMethodId":"<id from /api/payment-methods>"}'

# operator: fulfil (capture), then a partial refund
curl -s -b jar.txt "$BASE/api/orders/100001/fulfil" -X POST
curl -s -b jar.txt "$BASE/api/orders/100001/refunds" -H 'Content-Type: application/json' \
  -d '{"amount":"5.00","idempotencyKey":"refund-1"}'

# cancel a different, unfulfilled order (voids the hold)
curl -s -b jar.txt "$BASE/api/orders/100002/cancel" -X POST

curl -s -b jar.txt "$BASE/api/my-orders"
curl -s -b jar.txt "$BASE/api/reconciliation?from=2026-08-01T00:00:00Z&to=2026-09-30T00:00:00Z"
```

> PayPal's transaction reporting lags live activity, so `reconciliation` over a range
> covering payments you just made may legitimately show them under `appOnly` (known to
> the app, not yet in PayPal's report). That is expected sandbox behaviour.

## Layout

```
apps/payments/
  gateway.py     # the only SDK boundary: calls + one error-translation layer
  services.py    # Oscar order placement + payment lifecycle orchestration
  views.py       # JSON HTTP endpoints (session auth, staff gate, ownership)
  urls.py        # routes under /api/
  models.py      # PayPalPayment, SavedCard, PayPalRefund (PayPal-owned state)
  tests.py       # SDK-transport-seam tests + mocked-gateway service/view tests
  management/commands/verify_paypal.py   # live end-to-end verification
```
