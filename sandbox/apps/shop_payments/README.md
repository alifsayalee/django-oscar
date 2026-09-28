# Shop payments (PayPal) — JSON API

Orders, PayPal card payments and saved cards for the sandbox, under `/api/`.
Built on Oscar's own models (`order.Order`/`Line`, `payment.Source`/`Transaction`,
`payment.Bankcard`) and the PayPal Server SDK (`paypal` package, sync client).

## Configuration (environment → `sandbox/settings.py`)

| Setting | Meaning |
| --- | --- |
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | REST credentials of the merchant account |
| `PAYPAL_ENVIRONMENT` | `sandbox` (the only host the SDK declares) |
| `PAYPAL_CURRENCY` | ISO currency charged for every order (amounts are catalogue prices) |
| `PAYPAL_BASE_URL` | optional; used verbatim for every PayPal call, token request included |
| `PAYPAL_TIMEOUT` | optional; seconds per PayPal request (default 20) |
| `PAYPAL_REFERENCE_PREFIX` | optional; prefix of references sent to PayPal (default: random, stored in the DB) |

## Endpoints

Session auth (Django login). Send `X-CSRFToken` on POST/DELETE (`GET /api/csrf`).

| Method & path | Who | Notes |
| --- | --- | --- |
| `GET /api/csrf` · `POST /api/login` · `POST /api/logout` · `GET /api/me` | any | `login` takes `{"username" or "email", "password"}` |
| `POST /api/orders` | shopper | `{"items":[{"productId":12,"quantity":2}], "shippingAddress":{firstName,lastName,line1,city,postcode,countryCode}}` → `orderId` |
| `POST /api/orders/{orderId}/pay` | owner | `{"card":{number,expiry:"YYYY-MM",securityCode,name?,billingAddress?}}` or `{"paymentMethodId":"…"}` — authorizes (holds) the total |
| `POST /api/orders/{orderId}/fulfil` | staff | captures (reauthorizes first if the 3-day honor period passed) |
| `POST /api/orders/{orderId}/cancel` | staff | voids the hold before fulfilment |
| `POST /api/orders/{orderId}/refunds` | owner | header `Idempotency-Key` (required); `{"amount":"5.00"}` or `{}` for the remainder → `refundId` |
| `GET /api/my-orders` | shopper | orders with payment state |
| `POST /api/payment-methods` · `GET` · `DELETE /api/payment-methods/{id}` | shopper | optional `Idempotency-Key` on POST → `paymentMethodId` |
| `GET /api/reconciliation?from=…&to=…` | staff | ISO-8601 date-times; every ≤31-day window and page |

Status codes: `200/201` done · `202` accepted but not finished at PayPal (repeat the same request to
check) · `402` payment refused · `409` conflict or not allowed in the current state · `504` with
`"outcomeUnknown": true` when PayPal's answer was lost (repeat the same request; it is never performed twice).

## Tests

```
venv\Scripts\python -m pytest sandbox/apps/shop_payments/tests --ds=settings -o pythonpath=sandbox
```
