# PayPal payments & saved cards (sandbox app)

JSON API under `/api/` that takes card payments through PayPal (hold at checkout,
capture at fulfilment, void on cancel, refund on return), lets shoppers save cards
in PayPal's vault, and reconciles PayPal's transaction records against orders.
Built on Oscar's own `Order`/`Line`, `payment.Source`/`Transaction` and
`PaymentEvent` models; PayPal-specific state lives in `PayPalPayment`,
`PayPalRefund`, `SavedCard` and `OperationClaim`.

## Configuration (environment only)

| Variable | Meaning |
| --- | --- |
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | merchant REST credentials (required) |
| `PAYPAL_ENVIRONMENT` | `sandbox` selects `https://api-m.sandbox.paypal.com`; anything else requires `PAYPAL_BASE_URL` |
| `PAYPAL_CURRENCY` | currency every order is charged in (catalogue prices are used as-is) |
| `PAYPAL_BASE_URL` | optional; used verbatim for every PayPal call, token request included |
| `PAYPAL_INVOICE_PREFIX` | optional (default `OSCAR-`); marks this shop's invoices for reconciliation |

## Endpoints

Authenticate with the Django session: `GET /api/auth/csrf`, then
`POST /api/auth/login {"username","password"}` (or the storefront login). Send the
`X-CSRFToken` header on every POST/DELETE.

| Endpoint | Who | Notes |
| --- | --- | --- |
| `POST /api/orders` `{"items":[{"productId":209,"quantity":2}]}` | shopper | → `orderId`; optional `Idempotency-Key` |
| `POST /api/orders/{orderId}/pay` `{"card":{"number","expiry":"YYYY-MM","securityCode","name"}}` or `{"paymentMethodId": "..."}` | owner | authorizes the exact order total |
| `POST /api/orders/{orderId}/fulfil` | staff | captures; renews a hold past its 3-day honor period first |
| `POST /api/orders/{orderId}/cancel` | staff | voids the hold (or cancels an unpaid order) |
| `POST /api/orders/{orderId}/refunds` `{"amount":"5.00"}` (omit for the remainder) | owner | `Idempotency-Key` header required → `refundId` |
| `GET /api/orders/{orderId}`, `GET /api/my-orders` | owner | orders with payment state, capture fee/net, refunds |
| `POST /api/payment-methods` `{"card":{...}}` | shopper | → `paymentMethodId`, brand, last digits, expiry |
| `GET /api/payment-methods`, `DELETE /api/payment-methods/{id}` | owner | |
| `GET /api/reconciliation?from=ISO&to=ISO` | staff | every page of every 31-day window; `matched` / `paypalOnly` / `appOnly` |

Every PayPal write is claimed in the database before PayPal is called, so repeats
and double clicks never authorize, capture or refund twice; a call whose outcome was
lost is resumed with the same `PayPal-Request-Id`. Deleted cards whose vault token
could not be removed at once are retried by `manage.py paypal_purge_deleted_cards`.

## Tests

```
cd sandbox
set PYTHONPATH=.
..\venv\Scripts\python -m pytest apps/paypal_payments/tests --ds=settings --import-mode=importlib -p no:cacheprovider
```
