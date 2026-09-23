# paypal_api — PayPal payments & saved cards for the Oscar sandbox

An additive Django app that lets the sandbox storefront collect money through
**PayPal** and lets shoppers **save a card** for reuse. It reuses Oscar's own
`order`/`payment` models and adds only the state PayPal owns (authorization,
capture, refund and vault ids + statuses). No storefront UI; everything is a
JSON API under `/api/`.

All PayPal traffic goes through the generated **PayPal Server SDK** (`paypal`
package) via `gateway.py`; nothing talks to PayPal directly.

## Money flow (Flow 1)

1. `POST /api/orders` — place an order from catalogue items (`{"items":[{"productId":N,"quantity":Q}]}`).
   Reuses `OrderCreator`; the order starts **awaiting payment**. Returns `orderId`.
2. `POST /api/orders/{orderId}/pay` — **authorize** (hold) the order total. Body is
   either `{"card":{...}}` (one-off) or `{"paymentMethodId":N}` (a saved card).
   PayPal holds exactly the order total, to the cent.
3. `POST /api/orders/{orderId}/fulfil` — *(operator)* **capture** the money. The
   payment then shows PayPal's captured amount, fee and net proceeds. A stale
   authorization is reauthorized rather than failing outright; one that can no
   longer be renewed reports an operator-actionable reason.
4. `POST /api/orders/{orderId}/cancel` — *(operator)* **void** before fulfilment; the hold is released.
5. `POST /api/orders/{orderId}/refunds` — refund a captured payment, full or
   partial. Body `{"amount"?, "idempotencyKey"}`. Returns `refundId`. Never
   refunds beyond the captured amount.
6. `GET /api/my-orders` — the caller's orders with payment state.
7. `GET /api/reconciliation?from=&to=` — *(operator)* PayPal's transactions for a
   date-time range (whole range, all pages) lined up against app orders.

## Saved cards (Flow 2)

- `POST /api/payment-methods` — vault a card. Returns `paymentMethodId` + safe
  display (brand, last 4, expiry). Full card details are never stored.
- `GET /api/payment-methods` — the caller's saved cards.
- `DELETE /api/payment-methods/{paymentMethodId}` — remove a saved card (local +
  PayPal vault token); afterwards it cannot be seen or used to pay.

## Rules

- **Auth:** Django session login; identity from `request.user`. Shopper endpoints
  act only on the caller's own data (another shopper's id → 404). `fulfil`,
  `cancel` and `reconciliation` require `is_staff`.
- **Idempotency:** every payment action is idempotent in effect — the
  `OrderPayment` row (a `select_for_update` claim) plus a status guard mean a
  double-click never authorizes or captures twice; refunds also use the caller's
  idempotency key, and PayPal's `PayPal-Request-Id` derived from stable ids.
- **Secrets:** credentials are read from settings
  (`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENVIRONMENT`,
  `PAYPAL_CURRENCY`, `PAYPAL_BASE_URL`), which read the like-named env vars. No
  values live in the repo.

## Layout

| File | Purpose |
|---|---|
| `client.py` | Lazy, process-wide PayPal SDK client from settings |
| `gateway.py` | Every SDK call + the single error boundary; returns plain data |
| `services.py` | Order placement + idempotent pay/fulfil/cancel/refund/vault/reconcile |
| `views.py` / `urls.py` | JSON HTTP endpoints, auth/ownership/staff checks |
| `models.py` | `OrderPayment`, `PaymentMethod`, `PaymentRefund`, `PayPalCustomer` |
| `tests.py` | Offline gateway tests via the SDK's transport stub |

Run the offline tests: `python manage.py test apps.paypal_api`.
