# PayPal payments & saved cards (sandbox JSON API)

Card payments through PayPal for the sandbox site. The money is held at checkout, taken at
fulfilment and returned on refund. Shoppers can save cards in PayPal's vault. Orders are ordinary
Oscar orders (`order.Order` / `order.Line`, placed with Oscar's `OrderCreator`), and the payment
ledger uses Oscar's `payment.Source` / `payment.Transaction`.

## Configuration (environment → `sandbox/settings.py`)

| Setting | Meaning |
| --- | --- |
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | REST credentials of the merchant account (required) |
| `PAYPAL_ENVIRONMENT` | `sandbox` (the SDK's only known host); any other value needs `PAYPAL_BASE_URL` |
| `PAYPAL_CURRENCY` | ISO currency charged for every order (required) |
| `PAYPAL_BASE_URL` | optional; used verbatim for every PayPal call, token included |
| `PAYPAL_TIMEOUT` | optional, seconds per PayPal call (default 20) |
| `PAYPAL_REFERENCE_PREFIX` | optional prefix of the `PayPal-Request-Id`s this install sends (default: random per database) |

Install the SDK: `venv\Scripts\pip install -r sandbox\apps\paypal_payments\requirements.txt`.

## Endpoints

All endpoints use session auth (`GET /api/csrf`, then `POST /api/session` with
`{"username", "password"}`). Unsafe methods must send `X-CSRFToken`. Endpoints marked *staff* need
`is_staff`. Everything else only sees the caller's own orders and cards.

| Method & path | Body | Notes |
| --- | --- | --- |
| `POST /api/orders` | `{"items": [{"productId": 209, "quantity": 2}]}` | → `orderId`; status `Awaiting payment`; optional `Idempotency-Key` |
| `POST /api/orders/{orderId}/pay` | `{"card": {...}}` **or** `{"paymentMethodId": "..."}` | authorizes (holds) the order total |
| `POST /api/orders/{orderId}/fulfil` | — | *staff*; captures; renews an authorization past its 3-day honor period |
| `POST /api/orders/{orderId}/cancel` | — | *staff*; voids the hold (or cancels an unpaid order) |
| `POST /api/orders/{orderId}/refunds` | `{"amount": "5.00"}` (omit for the full remainder) | *staff*; `Idempotency-Key` header required; → `refundId` |
| `GET /api/orders/{orderId}` / `GET /api/my-orders` | — | order(s) with payment state |
| `POST /api/payment-methods` | `{"card": {...}}` | → `paymentMethodId`, brand, last 4, expiry |
| `GET /api/payment-methods` | — | the caller's saved cards |
| `DELETE /api/payment-methods/{paymentMethodId}` | — | removes the card from PayPal's vault |
| `GET /api/reconciliation?from=...&to=...` | — | *staff*; PayPal's transaction report vs this app's records |

The card object is
`{"number", "expiry": "YYYY-MM", "securityCode", "name", "billingAddress": {"addressLine1", "city", "state", "postalCode", "countryCode"}}`.

The HTTP status reflects the outcome PayPal reported: `200/201` done, `202` accepted but not
finished, `409` refused or needs review, `504` outcome unknown (repeat the same request to check it).

## Guarantees

* Every PayPal write is claimed in the database first (a unique reference that is also sent as
  `PayPal-Request-Id`). A double-click, a retry or a second worker never authorizes, captures,
  refunds, vaults or deletes twice, and an unanswered write is checked, never blindly re-sent.
* The amount PayPal echoes must equal the amount asked for, to the cent. If it doesn't, the payment
  is flagged `needs_review`.
* Refunds are reserved under a row lock, so the total refunded never exceeds the capture.
* Card numbers and security codes are never stored or logged. Only PayPal's vault token, the
  brand, the last 4 digits and the expiry are kept.

Tests: `venv\Scripts\python sandbox\manage.py test apps.paypal_payments` (PayPal is faked at the
SDK's transport seam).
