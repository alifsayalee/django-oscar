# PayPal payments API (`apps.payments_api`)

An **additive** Django app for the Oscar sandbox that makes the site actually collect
money via **PayPal**, and lets a shopper **save a card** for reuse. It reuses Oscar's
own `order.Order`/`order.Line` models and exposes everything as a session-authenticated
JSON API under `/api/`. No storefront UI, no change to the existing catalogue/basket/
checkout flow.

All PayPal interaction goes through the **`paypal`** Server SDK (`gateway.py`). Money is
held at checkout (authorize), taken at fulfilment (capture), released on cancel (void)
and returned on refund.

## Endpoints

| Method & path | Who | What |
|---|---|---|
| `POST /api/orders` | shopper | Place an order from catalogue `items` (`[{product_id, quantity}]`). Reuses Oscar's order model. Returns `orderId`. Starts *awaiting payment*. |
| `POST /api/orders/{orderId}/pay` | shopper (own) | **Authorize** the total (hold, not take). Body: `{"card": {...}}` for a one-off card, **or** `{"paymentMethodId": N}` to use a saved card. |
| `POST /api/orders/{orderId}/fulfil` | **operator** | **Capture** (take the money). Response shows PayPal's captured amount, fee and net. Renews a stale authorization automatically; if it can no longer be renewed, says so. |
| `POST /api/orders/{orderId}/cancel` | **operator** | **Void** the hold before fulfilment — no money ever moves. |
| `POST /api/orders/{orderId}/refunds` | shopper (own) | **Refund** a captured payment, full or partial. Body: `{"amount"?, "idempotencyKey"}` (or `Idempotency-Key` header). Returns `refundId`. Never refunds beyond what was captured. |
| `GET /api/my-orders` | shopper | The caller's orders with payment state. |
| `GET /api/reconciliation?from=&to=` | **operator** | PayPal's transaction record for a date range (ISO-8601), lined up against this app's payments. Covers the whole range (paginated, chunked into ≤31-day windows). |
| `POST /api/payment-methods` | shopper | Save a card. Returns `paymentMethodId` + a safe description (brand + last 4). |
| `GET /api/payment-methods` | shopper | The caller's saved cards. |
| `DELETE /api/payment-methods/{paymentMethodId}` | shopper (own) | Remove a saved card. Afterwards it is neither listed nor usable to pay. |

**Auth:** Django session login (`request.user`). Operator endpoints require `is_staff`;
every other endpoint is scoped to the caller's own data (401 unauthenticated, 403 not
staff, 404 for another user's resource). The JSON views are `csrf_exempt` (API clients,
not browser forms) — authorization is enforced per request by the auth + ownership/staff
checks.

## Configuration (environment → `sandbox/settings.py`)

Read at run time; **never committed**:

- `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` — sandbox business REST credentials.
- `PAYPAL_ENVIRONMENT` — informational (`sandbox`).
- `PAYPAL_CURRENCY` — currency sent to PayPal (e.g. `USD`).
- `PAYPAL_BASE_URL` — *optional* override; when set it is used verbatim for every PayPal
  call including the OAuth token request. Unset → the SDK's sandbox default.

## Design notes

- **Direct-card authorize.** Creating a PayPal order with `intent=AUTHORIZE` and inline
  card details **auto-creates the authorization** in the same call; the app reads the
  authorization off that response (a separate `authorize_order` would be
  `ORDER_ALREADY_AUTHORIZED`).
- **Amounts** come from the Oscar order total; **currency** from `PAYPAL_CURRENCY`. Money
  is formatted with `Decimal` to the cent.
- **Idempotency.** `pay` and `fulfil` are guarded by the local payment state plus a stable
  `PayPal-Request-Id`, so a double-click never authorizes or captures twice. `refunds`
  take a caller idempotency key: the same key returns the same refund; distinct keys are
  distinct partial refunds; the app never refunds beyond the captured amount.
- **State PayPal owns** (order id, authorization id/status/expiry, capture id/status,
  refund ids/status) is persisted on `OrderPayment`/`PaymentRefund` so later requests can
  act on it.
- **Saved cards** are vaulted with PayPal (setup token → payment token); the app stores
  only the token id and safe descriptors (brand, last 4) — never the PAN, which is never
  written to the app DB or the logs.
- **Reconciliation** lag: PayPal's reporting trails live activity, so a range covering
  very recent payments can legitimately come back empty. The report is correct over ranges
  that have data.

## Verifying it works

1. Build the sandbox DB and catalogue (from the repo root, venv active):
   ```
   python sandbox/manage.py migrate
   python sandbox/manage.py loaddata sandbox/fixtures/auth.json sandbox/fixtures/child_products.json
   python sandbox/manage.py oscar_populate_countries --initial-only
   ```
2. With `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` / `PAYPAL_CURRENCY` in the environment,
   start the server:
   ```
   python sandbox/manage.py runserver 127.0.0.1:36400 --noreload
   ```
3. In another shell (venv active), run the end-to-end verification against the live sandbox
   (uses PayPal's test card `4111 1111 1111 1111`):
   ```
   python sandbox/apps/payments_api/verify_paypal.py
   ```
   It exercises pay → fulfil → refund, cancel/void, saved-card save/reuse/delete, and
   reconciliation, plus the auth/ownership/staff rules, and prints `N passed, 0 failed`.

Unit tests (no network, fake the SDK transport):
```
python sandbox/manage.py test apps.payments_api
```
