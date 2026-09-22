# PayPal payments API (`apps.paypal_api`)

Adds real **PayPal** card payments and **saved cards** to the django-oscar sandbox
site as an additive Django app, mounted under `/api/`. It reuses Oscar's own
`order.Order`/`order.Line` and mirrors money into `payment.Source`/`payment.Transaction`;
PayPal-owned state (hold / capture / refund ids, status, fee, net proceeds, vault
token) lives in three sidecar models. All PayPal traffic goes through the
`paypal` Server SDK — no direct REST calls, no other PayPal reference.

## Money movement

* **Authorize** at pay time (a hold; money is *not* taken).
* **Capture** at fulfilment (money is taken; PayPal's captured amount, fee and net
  proceeds are recorded).
* **Void** on cancel before fulfilment (the hold is released).
* **Refund** after fulfilment, full or partial (never beyond what was captured).
* A stale authorization is **reauthorized** before capture; one that can no longer
  be renewed surfaces an operator-actionable conflict.

Payment operations are idempotent in effect (a double-click never authorizes or
captures twice). Refunds carry a caller-supplied idempotency key.

## Configuration (settings, read from the environment — values never in the repo)

`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENVIRONMENT` (`sandbox`/`live`),
`PAYPAL_CURRENCY`, `PAYPAL_BASE_URL` (optional; used verbatim for every call,
including the OAuth2 token request, when set — otherwise derived from the
environment). Order amounts come from the catalogue; the currency comes from
`PAYPAL_CURRENCY`.

## Endpoints

All are session-authenticated (Django login); state-changing calls are
CSRF-protected. Shopper endpoints act only on the caller's own data;
`fulfil`, `cancel` and `reconciliation` require `is_staff`.

| Method & path | Who | Purpose |
|---|---|---|
| `GET  /api/session` | any | bootstraps the CSRF cookie; reports auth state |
| `POST /api/session/login` | any | Django session login (`{username,password}`; username is the email) |
| `POST /api/session/logout` | any | end the session |
| `POST /api/orders` | shopper | place an order from `{items:[{productId,quantity}]}` → **`orderId`** |
| `POST /api/orders/{orderId}/pay` | shopper | authorize with `{card:{…}}` or `{savedCardId}` (a hold) |
| `POST /api/orders/{orderId}/fulfil` | operator | capture (take the money) |
| `POST /api/orders/{orderId}/cancel` | operator | void the hold before fulfilment |
| `POST /api/orders/{orderId}/refunds` | shopper | refund `{amount?, idempotencyKey}` → **`refundId`** |
| `GET  /api/my-orders` | shopper | the caller's orders + payment state |
| `GET  /api/reconciliation?from=&to=` | operator | PayPal transactions (all pages) vs app orders |
| `GET/POST /api/payment-methods` | shopper | list / save a card → **`paymentMethodId`** |
| `DELETE /api/payment-methods/{id}` | shopper | remove a saved card |

Full card details are used transiently for the PayPal call only — never stored in
this app's database and never logged.

## Verify it end-to-end (PayPal sandbox, no browser)

From the repo root, with the `PAYPAL_*` env vars set and the venv active:

```bash
# one-time build of the sandbox catalogue (if not already done)
sandbox/manage.py migrate
sandbox/manage.py loaddata sandbox/fixtures/auth.json sandbox/fixtures/child_products.json
sandbox/manage.py oscar_populate_countries --initial-only
sandbox/manage.py loaddata sandbox/fixtures/pages.json sandbox/fixtures/ranges.json sandbox/fixtures/offers.json sandbox/fixtures/orders.json

# demo shopper + operator accounts
sandbox/manage.py paypal_demo_users

# run the site, then in another shell drive every flow against real PayPal sandbox
sandbox/manage.py runserver 127.0.0.1:8000
PAYPAL_API_BASE=http://127.0.0.1:8000 python sandbox/apps/paypal_api/verify_api.py
```

`verify_api.py` exercises: save card → create order → authorize → fulfil (capture,
with fee/net) → partial refund (+ idempotency) → pay a second order with the saved
card → cancel → ownership isolation → operator-only access → reconciliation.

Unit tests (transport-stubbed gateway + service idempotency/ownership):

```bash
sandbox/manage.py test apps.paypal_api
```

PayPal's sandbox risk engine occasionally returns a transient `TRANSACTION_REFUSED`
for the shared test card; the verification script retries the card steps. PayPal's
transaction reporting also lags, so a reconciliation window over just-created
payments can legitimately come back empty — the report is correct over a range
that has data.
