# PayPal payments & saved cards API (sandbox)

JSON API, routed under `/api/`, that places Oscar orders, holds the money with
PayPal at checkout, takes it at fulfilment, and gives it back on a return. It
talks to PayPal through the PayPal Server SDK for Python (`paypal`).

## Configuration

All values come from the environment through `sandbox/settings.py`:

| Setting | Meaning |
|---|---|
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | REST credentials of the merchant (business) account |
| `PAYPAL_ENVIRONMENT` | `sandbox` selects `https://api-m.sandbox.paypal.com`; any other value requires `PAYPAL_BASE_URL` |
| `PAYPAL_CURRENCY` | ISO-4217 currency every order is priced and charged in |
| `PAYPAL_BASE_URL` | optional; used verbatim for every PayPal call, including the token request |
| `PAYPAL_TIMEOUT` | optional; seconds per PayPal HTTP request (default 20) |

The SDK is installed from its repository:
`pip install "paypal @ git+https://github.com/context-plugins/paypal-python-sdk.git@main"`.

## Endpoints

Callers log in with Django's session (`POST /api/session`, or the storefront
login page) and send `X-CSRFToken` on unsafe methods (`GET /api/csrf` returns
a token). `*` = staff only.

| Method & path | What it does |
|---|---|
| `POST /api/orders` | `{"lines": [{"productId": 9, "quantity": 2}], "shippingAddress"?: {...}}` → `201 {"orderId", ...}`, status `Awaiting payment` |
| `POST /api/orders/{orderId}/pay` | `{"card": {number, expiry "YYYY-MM", securityCode, name, billingAddress?}}` **or** `{"paymentMethodId"}` → authorizes the order total |
| `POST /api/orders/{orderId}/fulfil` * | captures; renews a hold past PayPal's 3-day honor period; refuses (409, with operator guidance) one older than 29 days |
| `POST /api/orders/{orderId}/cancel` * | voids the hold (or cancels an unpaid order) |
| `POST /api/orders/{orderId}/refunds` | `Idempotency-Key` header (or `idempotencyKey`), optional `amount` (default: the remainder) → `201 {"refundId", ...}` |
| `GET /api/orders/{orderId}` | one order (own orders; staff: any) |
| `GET /api/my-orders` | the caller's orders with payment state, capture fee/net and refunds |
| `GET /api/reconciliation?from=&to=` * | PayPal's transaction report for the range lined up against the app's orders |
| `POST /api/payment-methods` | `{"card": {...}}` (+ optional `Idempotency-Key`) → `201 {"paymentMethodId", brand, lastDigits, expiry}` |
| `GET /api/payment-methods` | the caller's saved cards |
| `DELETE /api/payment-methods/{paymentMethodId}` | removes it at PayPal and here → `204` |

Error bodies are `{"error": "...", ...}`; PayPal failures add `paypalError`,
`paypalIssues`, `paypalDebugId` and `outcomeUnknown`. A `504` with
`outcomeUnknown: true` means PayPal may have acted: repeat the same request (for
refunds, with the same idempotency key) — it is re-sent under the same
`PayPal-Request-Id`, so PayPal returns the original result instead of acting twice.

## Payment states

`awaiting_payment → authorized → captured` (+ `refundState`
`none | partially_refunded | refunded`); `authorized → voided` on cancel;
`payment_failed` after a decline (the shopper may pay again). Transitional
states (`authorizing`, `capturing`, `voiding`, `*_unknown`, `*_pending`) and
`needs_review` (PayPal did something other than what was asked) are visible to
operators in the reconciliation report's `unsettled` list.

## Tests

```
venv\Scripts\pytest sandbox/apps/paypal_payments/tests --ds=settings -o pythonpath=sandbox
```

The tests fake PayPal at the SDK's transport seam; they need no credentials.
