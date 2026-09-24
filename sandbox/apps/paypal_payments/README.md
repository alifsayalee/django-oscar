# PayPal payments & saved cards (`/api/`)

A JSON API on the sandbox site. PayPal puts a hold on the card when the shopper pays. The money is
taken when an operator fulfils the order, and returned by a refund. Shoppers can also save cards in
PayPal's vault and use them to pay later. All PayPal calls go through the PayPal Server SDK (`paypal`).

## Configuration (environment → `sandbox/settings.py`)

| Variable | Meaning |
| --- | --- |
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | REST app credentials of the merchant (business) account |
| `PAYPAL_ENVIRONMENT` | `sandbox` selects `https://api-m.sandbox.paypal.com`; any other value requires `PAYPAL_BASE_URL` |
| `PAYPAL_CURRENCY` | currency every order is charged in (catalogue prices are used as amounts) |
| `PAYPAL_BASE_URL` | optional; used verbatim for every PayPal call, the OAuth token request included |
| `PAYPAL_TIMEOUT` | seconds per PayPal call (default 20) |
| `PAYPAL_AUTH_HONOR_PERIOD_DAYS` | days after which fulfilment renews (reauthorizes) the hold before capturing (default 3) |
| `PAYPAL_REFERENCE_PREFIX` | optional; defaults to a random prefix generated when the database was migrated |

## Endpoints

Log in with the site's normal session login, e.g. by posting the form at `/en-gb/accounts/login/`.
`GET /api/session` returns a `csrfToken`. Send it as `X-CSRFToken` on every POST or DELETE.

| Method & path | Who | Notes |
| --- | --- | --- |
| `POST /api/orders` `{"lines":[{"productId":12,"quantity":2}]}` | shopper | creates an Oscar order (state `awaiting_payment`) and returns `orderId` |
| `POST /api/orders/{orderId}/pay` `{"card":{number,expiry:"YYYY-MM",securityCode,name,billingAddress}}` or `{"paymentMethodId":…}` | owner | authorizes (holds) the order total |
| `POST /api/orders/{orderId}/fulfil` | staff | captures the payment and reports the captured amount, PayPal fee and net amount; renews a stale hold first |
| `POST /api/orders/{orderId}/cancel` | staff | voids the hold (before fulfilment only) |
| `POST /api/orders/{orderId}/refunds` `{"amount":"5.00"}` + `Idempotency-Key` header | owner | full refund when `amount` is omitted; returns `refundId` |
| `GET /api/my-orders` | shopper | the caller's orders with their payment state |
| `POST /api/payment-methods` `{"card":{…}}` (optional `Idempotency-Key`) | shopper | returns `paymentMethodId`, brand, last digits, expiry |
| `GET /api/payment-methods` · `DELETE /api/payment-methods/{id}` | owner | |
| `GET /api/reconciliation?from=…&to=…` | staff | PayPal's transactions compared with this app's captures and refunds |

A `202` response means PayPal hasn't confirmed the outcome yet. Repeat the same request to re-check it; it is
never sent to PayPal again under a new reference. A `504` with `outcome_unknown: true` means the same thing.

## Tests

    cd sandbox && python manage.py test apps.paypal_payments
