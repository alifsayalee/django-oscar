# PayPal checkout API (`apps.paypal_checkout`)

Adds PayPal-backed card payments and saved cards to the Oscar sandbox as a REST
API under `/api/`. It is **additive**: the existing catalogue/basket/order/UI
flow is untouched. Orders and money movement reuse Oscar's own models
(`order.Order`, `order.Line`, `payment.Source`, `payment.Transaction`); the
PayPal-owned state (order/authorization/capture/refund ids and statuses, vaulted
cards) lives in this app's models.

All PayPal traffic goes through the **`paypal` Server SDK** (the `paypal`
distribution / import root), via the single wrapper in `gateway.py`.

## Money model

* **Authorize at checkout, capture at fulfilment.** `pay` places a *hold*
  (authorization) equal to the order total to the cent; the money is only *taken*
  at `fulfil` (capture). `cancel` voids the hold before fulfilment (no money
  moves); `refunds` returns captured money after fulfilment.
* Amounts come from the catalogue price of the ordered items; the currency is
  `settings.PAYPAL_CURRENCY`.
* Every payment operation is **idempotent in effect** (row locking + a stable
  `PayPal-Request-Id`), so a double-click never authorizes or captures twice.
  Refunds additionally take a caller idempotency key.

## Endpoints

| Method & path | Who | Purpose |
|---|---|---|
| `POST /api/orders` | shopper | Place an order from `{items:[{product_id, quantity}]}`. Returns **`orderId`**. |
| `POST /api/orders/{orderId}/pay` | shopper (own) | Authorize the total. Body: `{"card": {...}}` **or** `{"payment_method_id": <saved card>}`. |
| `POST /api/orders/{orderId}/fulfil` | operator (`is_staff`) | Capture the held funds; response shows captured amount, PayPal fee and net. |
| `POST /api/orders/{orderId}/cancel` | operator (`is_staff`) | Void the authorization before fulfilment. |
| `POST /api/orders/{orderId}/refunds` | shopper (own) | Refund captured funds, full or partial. Body: `{"amount": "5.00", "idempotency_key": "..."}` (omit `amount` for full). Returns **`refundId`**. |
| `GET /api/my-orders` | shopper | The caller's orders with payment state. |
| `GET /api/reconciliation?from=&to=` | operator (`is_staff`) | PayPal's transactions for an ISO-8601 range, lined up against this app's orders (all pages). |
| `POST /api/payment-methods` | shopper | Vault a card: `{"card": {...}}`. Returns **`paymentMethodId`** + safe description (brand/last4/expiry). |
| `GET /api/payment-methods` | shopper | The caller's saved cards. |
| `DELETE /api/payment-methods/{paymentMethodId}` | shopper (own) | Remove a saved card (also deletes the PayPal vault token). |

`card` fields: `number`, `expiry` (`"YYYY-MM"`), `security_code`, `name`,
`billing_address` (`address_line_1`, `admin_area_2`=city, `admin_area_1`=state,
`postal_code`, `country_code`). **The card number/CVV are never stored or logged.**

Auth is the sandbox's own Django **session** login; the caller is
`request.user`. Shopper endpoints act only on the caller's own data (others'
resources return 404). Operator endpoints require `is_staff`.

## Configuration (from the environment, via `sandbox/settings.py`)

`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENVIRONMENT`,
`PAYPAL_CURRENCY`, and the optional `PAYPAL_BASE_URL` (when set, used verbatim as
the API base for every call including the token request). No secret **values**
are ever committed.

## Verify it (live sandbox, no browser)

```
sandbox/manage.py paypal_smoke
```

Runs the full flow — save card, place/pay/fulfil/refund, reuse saved card, cancel,
ownership checks, delete card, reconciliation — against the live PayPal sandbox
using the sandbox test card `4111 1111 1111 1111`, and prints PASS/FAIL per check.

Unit tests (offline, via the SDK transport seam):

```
sandbox/manage.py test apps.paypal_checkout
```
