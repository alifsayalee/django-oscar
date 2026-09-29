# PayPal payments & saved cards API (`/api/`)

A JSON API on the sandbox site that takes real card payments through PayPal
(authorize at checkout, capture at fulfilment, void on cancel, refund on
return) and lets a shopper save a card in PayPal's vault. It builds on Oscar's
own models: `order.Order`/`Line`, `payment.Source`/`Transaction` for the money
movements, and `payment.Bankcard` for saved cards (masked number plus the PayPal
vault token). No card number or security code is ever stored or logged.

## Configuration (environment → `sandbox/settings.py`)

| Setting | Meaning |
| --- | --- |
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | REST app credentials of the merchant account |
| `PAYPAL_ENVIRONMENT` | `sandbox` selects `https://api-m.sandbox.paypal.com`; other environments need `PAYPAL_BASE_URL` |
| `PAYPAL_CURRENCY` | ISO 4217 code that API orders are priced and charged in (the amounts come from catalogue prices) |
| `PAYPAL_BASE_URL` | Optional. When set, it is used as-is as the base of every PayPal call, including the token request |
| `PAYPAL_TIMEOUT` | Optional. Seconds per PayPal HTTP attempt (default 20) |

## Authentication

Django session login. `GET /api/session` sets the `csrftoken` cookie and
returns `csrfToken`. `POST /api/session` with `{"username", "password"}` logs in.
Every unsafe request must send `X-CSRFToken`. Fulfil, cancel and
reconciliation require an `is_staff` user. All other endpoints act only on the
caller's own orders and cards; anything that belongs to another user returns 404.

## Endpoints

| Method & path | Body / query | Notes |
| --- | --- | --- |
| `POST /api/orders` | `{"items":[{"itemId":209,"quantity":2}], "shippingAddress":{"firstName","lastName","line1","city","state","postalCode","countryCode"}}` | 201, top-level `orderId`; status `Pending payment` |
| `POST /api/orders/{orderId}/pay` | `{"card":{"number","expiry":"YYYY-MM","securityCode","name","billingAddress":{...}}}` **or** `{"paymentMethodId":"3"}` | Authorizes (holds) the exact order total. 201 on first success, 200 on a repeat |
| `POST /api/orders/{orderId}/fulfil` | — (staff) | Captures. Renews an authorization past its 3-day honor period; returns 409 with an actionable message if it cannot be renewed |
| `POST /api/orders/{orderId}/cancel` | — (staff) | Voids the hold before fulfilment |
| `POST /api/orders/{orderId}/refunds` | header `Idempotency-Key` (required); `{"amount":"5.00"}` (omit for the whole remainder) | 201 with top-level `refundId`; the same key returns the same refund (200) |
| `GET /api/my-orders` | — | Caller's orders with payment state (authorization, capture with fee/net, refunds, refundable amount) |
| `GET /api/reconciliation?from=…&to=…` | ISO-8601 date-times (staff) | PayPal Transaction Search across every page and 31-day window, matched against this app's captures and refunds: `matched`, `paypalOnly`, `appOnly` |
| `POST /api/payment-methods` | `{"card":{...}}`, optional `Idempotency-Key` | 201 with top-level `paymentMethodId`, brand, last digits and expiry |
| `GET /api/payment-methods` | — | Caller's saved cards |
| `DELETE /api/payment-methods/{paymentMethodId}` | — | 204. Deletes the vault token at PayPal and the local card |

Errors are returned as `{"error": {"code", "message", ...}}`. When
`outcomeUnknown` is true, PayPal may have acted. Repeat the same request
(refunds: with the same `Idempotency-Key`) and it settles against PayPal
without charging or refunding twice.

## Tests

    cd sandbox && python manage.py test apps.paypal_payments

PayPal is faked at the SDK's transport seam, so the tests make no network calls.
