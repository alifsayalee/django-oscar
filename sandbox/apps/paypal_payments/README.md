# PayPal payments and saved cards (`/api/`)

A JSON API on the sandbox site that takes real card payments through PayPal:
hold the money when the shopper pays, take it when an operator fulfils,
release it on cancel, give it back on refund. Shoppers can save a card
(vaulted at PayPal) and pay later orders with it. Orders are ordinary Oscar
orders; the PayPal state lives beside them (`PayPalPayment`, `PayPalRefund`).

## Configuration (`sandbox/settings.py`, read from the environment)

| Setting | Meaning |
| --- | --- |
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | REST credentials of the merchant account |
| `PAYPAL_ENVIRONMENT` | `sandbox` selects `https://api-m.sandbox.paypal.com`; any other value needs `PAYPAL_BASE_URL` |
| `PAYPAL_CURRENCY` | currency every order is charged in (amounts come from catalogue prices) |
| `PAYPAL_BASE_URL` | optional; used verbatim for every PayPal call, token request included |
| `PAYPAL_TIMEOUT` | seconds per PayPal request (default 20) |
| `PAYPAL_REFERENCE_PREFIX` | optional prefix for references sent to PayPal; default is an id generated once per database |

## Endpoints

Authenticate with Django's session login. `GET /api/session` returns a
`csrfToken` (and sets the `csrftoken` cookie); send it as `X-CSRFToken` on
every POST/DELETE. `POST /api/session {"username", "password"}` signs in
(`email` works too); `DELETE /api/session` signs out.

| Method & path | Who | What |
| --- | --- | --- |
| `POST /api/orders` | shopper | `{"items": [{"itemId", "quantity"}], "shippingAddress": {...}}` → `orderId`, state `awaiting_payment` |
| `POST /api/orders/{orderId}/pay` | owner | `{"card": {number, expiry "YYYY-MM", securityCode, name, billingAddress}}` or `{"paymentMethodId"}` → authorization (hold) |
| `POST /api/orders/{orderId}/fulfil` | staff | captures; shows captured amount, PayPal fee, net; renews a stale hold first |
| `POST /api/orders/{orderId}/cancel` | staff | voids the hold (or cancels an unpaid order) |
| `POST /api/orders/{orderId}/refunds` | owner | `Idempotency-Key` header required; `{"amount": "5.00"}` or `{}` for the rest → `refundId` |
| `GET /api/orders/{orderId}/refunds` | owner | the order's refunds |
| `GET /api/my-orders` | shopper | the caller's orders with payment state |
| `GET /api/reconciliation?from=…&to=…` | staff | PayPal's transactions for the range vs. this site's captures and refunds |
| `POST /api/payment-methods` | shopper | `{"card": {...}}`, optional `Idempotency-Key` → `paymentMethodId`, brand, last digits, expiry |
| `GET /api/payment-methods` | shopper | the caller's saved cards |
| `DELETE /api/payment-methods/{paymentMethodId}` | owner | removes the card (hidden and unusable at once; deleted at PayPal) |

Status codes for payment actions: `200`/`201` done; `202` accepted but not
finished (repeat the request to check on it); `402` card declined (pay again
with another card); `409` refused or needs review; `504` PayPal's answer was
lost (`outcomeUnknown: true`): repeat the same request, which settles it and
never charges twice.

## Guarantees and how they are kept

* **No double charges.** Each PayPal write (authorize, reauthorize, capture,
  void, refund, vault, delete) first inserts a `ProviderWrite` row under a
  reference derived from the order and the step; the unique constraint lets
  exactly one request make the call, in any process. The same reference is
  sent as `PayPal-Request-Id`, so a retry after a lost answer is replayed by
  PayPal instead of repeated.
* **Refunds never exceed the capture.** A refund reserves its amount with a
  conditional `UPDATE` before PayPal is called; the reservation is released
  only when PayPal refuses or fails the refund. The caller's idempotency key
  identifies a refund: the same key never refunds twice, different keys are
  different refunds.
* **Stale holds.** PayPal honours an authorization for three days. Fulfilment
  after that reauthorizes first; if PayPal will not renew it but the original
  is still capturable, it is captured; if it can no longer be captured the
  operator gets a `409 authorization_expired` saying to cancel and have the
  shopper pay again.
* **Card data.** Card numbers and security codes are validated, forwarded to
  PayPal and dropped; they are never stored, never logged (PayPal HTTP calls
  are logged as method, path, status and debug id only), and PayPal's error
  `value` fields are not echoed.

## Operating it

* `ProviderWrite` rows with outcome `unknown`, `needs_review` or a stale
  `sending` are the ones to look at; each carries its PayPal id when known and
  the last PayPal error (issue codes, debug id).
* Reconciliation filters both sides on PayPal's clock and lists `matched`,
  `paypalOnly` (PayPal knows it, we do not), `appOnly` (we recorded it, PayPal
  does not list it yet) and `unsettled` (writes whose outcome is not settled).
  PayPal can take up to three hours to list a transaction, so very recent
  captures show as `appOnly` until then.
* Tests: `cd sandbox && python manage.py test apps.paypal_payments`
  (PayPal is faked at the SDK transport; no network).
