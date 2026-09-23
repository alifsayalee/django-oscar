# PayPal integration plan — django-oscar sandbox

Add PayPal payments (Orders v2 + Payments v2) and saved cards (Vault v3) plus a
TransactionSearch reconciliation report to the runnable sandbox at `sandbox/`, as a
new Django app exposed under `/api/`. Additive: reuses Oscar's own Order/Line/payment
models; does not replace the existing catalogue/basket/order flow.

## SDK identity (verified against installed package `paypal==2.29`)

- **Distribution/import root is `paypal`** (NOT `pay_pal_server_sdk` — the getting-started
  snapshot is stale; the installed wheel and the SDK map both say `paypal`). Client class
  `PaypalClient` / `AsyncPaypalClient` (aliases `Client`/`AsyncClient`).
- Installed as a proper wheel into `repo/venv` (`pip install <local clone>` — built
  `paypal-2.29-py3-none-any.whl`, independent of the clone directory).
- Auth: OAuth2 client credentials — `oauth2=ClientCredentials(client_id=, client_secret=)`.
  Token fetched lazily, cached on the client; client must be long-lived.
- One base URL knob (`base_url=`), default `https://api-m.sandbox.paypal.com`. Token
  endpoint derives from it. No environment enum.
- Imports: client from `paypal`; `ClientCredentials, ApiError, RawError, Success, Failure,
  OAuthProviderError, UNSET, UnsetType` from `paypal.core`; models from `paypal.models`;
  enums from `paypal.models.enums`.

## Sync vs async — SYNC

Django runs under WSGI (`sandbox/wsgi.py`), views are sync. Use `PaypalClient` (sync).
Client held as a lazily-initialised module global in the app's `client.py`, built from
`django.conf.settings`, closed via `atexit`. Never per-request. Teardown: `client.close()`.

## Settings (read via django settings; NEVER hard-code values)

Add to `sandbox/settings.py` (after `from oscar.defaults import *`), each read from env:
```
PAYPAL_CLIENT_ID     = env('PAYPAL_CLIENT_ID', default='')
PAYPAL_CLIENT_SECRET = env('PAYPAL_CLIENT_SECRET', default='')
PAYPAL_ENVIRONMENT   = env('PAYPAL_ENVIRONMENT', default='sandbox')
PAYPAL_CURRENCY      = env('PAYPAL_CURRENCY', default='USD')
PAYPAL_BASE_URL      = env('PAYPAL_BASE_URL', default='')   # optional override
```
Base URL resolution: if `PAYPAL_BASE_URL` set → use verbatim (for every call incl. token).
Else map environment: `sandbox`→`https://api-m.sandbox.paypal.com`,
`live`/`production`→`https://api-m.paypal.com` (fail on unknown value, don't default silently).

## Contract sheet — operations in scope (all sync, keyword-only tail, `request_options` last)

Every parsed call raises `ApiError` on non-2xx; `.error` union always ends in `RawError`.
Money is `str` scaled to currency — build with `Decimal`, format `f"{d:.2f}"`.

| Flow step | Operation | Signature essentials | Returns | Error union |
|---|---|---|---|---|
| create PayPal order | `client.orders.create_order(body, *, pay_pal_request_id=None, prefer=...)` | body=`OrderRequest`; pass `pay_pal_request_id` for idempotency | `Order` | `Error`[400,401,422] \| RawError |
| authorize (`/pay`) | `client.orders.authorize_order(id, *, pay_pal_request_id=None, prefer=..., body=None)` | id=paypal order id; body=`OrderAuthorizeRequest(payment_source=...)`; PayPal-Request-Id idempotent | `OrderAuthorizeResponse` | `Error`[400,401,403,404,422,500]\|RawError |
| capture (`/fulfil`) | `client.payments.capture_authorized_payment(authorization_id, *, pay_pal_request_id=None, prefer=..., body=None)` | body=`CaptureRequest` optional (final_capture); PayPal-Request-Id idempotent | `CapturedPayment` | `Error`[400,401,403,404,409,422]\|RawError |
| renew stale auth | `client.payments.reauthorize_payment(authorization_id, *, pay_pal_request_id=None, prefer=..., body=None)` | returns fresh auth | `PaymentAuthorization` | `Error`[400,401,403,404,422]\|RawError |
| cancel (void) | `client.payments.void_payment(authorization_id, *, pay_pal_request_id=None, prefer=...)` | releases held funds | `PaymentAuthorization` (parsed) | `Error`[401,403,404,409,422]\|RawError |
| refund (`/refunds`) | `client.payments.refund_captured_payment(capture_id, *, pay_pal_request_id=None, prefer=..., body=None)` | body=`RefundRequest(amount=Money)` optional (full if omitted); PayPal-Request-Id=idempotency key | `Refund` | `Error`[400,401,403,404,409,422]\|RawError |
| read auth (staleness/verify) | `client.payments.get_authorized_payment(authorization_id)` | | `PaymentAuthorization` | `Error`[401,403,404]\|RawError |
| read capture | `client.payments.get_captured_payment(capture_id)` | | `CapturedPayment` | `Error`[401,403,404]\|RawError |
| save card | `client.vault.create_payment_token(body, *, pay_pal_request_id=None)` | body=`PaymentTokenRequest(customer?, payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(...)))` | `PaymentTokenResponse` | `Error`[400,403,404,422,500]\|RawError |
| list saved (verify) | `client.vault.list_customer_payment_tokens(customer_id, *, page_size, page, total_required)` | | `CustomerVaultPaymentTokensResponse` | `Error`[400,403,500]\|RawError |
| delete saved | `client.vault.delete_payment_token(id)` | **returns `None`** — use `with_raw_response` to read status | `None` | `Error`[400,403,500]\|RawError |
| reconciliation | `client.transaction_search.search_transactions(start_date, end_date, *, fields='transaction_info', page_size=100, page=1, ...)` | **Case B — `.error` always `RawError`**; loop pages 1..total_pages | `SearchResponse` | RawError only |

### Key model shapes (built as dicts or models; keys = python names)

- `OrderRequest`: `intent` (req, `CheckoutPaymentIntent.AUTHORIZE`="AUTHORIZE"), `purchase_units` (req list of `PurchaseUnitRequest`).
- `PurchaseUnitRequest`: `amount` (req `AmountWithBreakdown{currency_code, value}`), `invoice_id` (unique per order → reconciliation match key), `custom_id`, `description`, `reference_id`.
- `OrderAuthorizeRequest`: `payment_source` (`OrderAuthorizeRequestPaymentSource`): `.card` = `CardRequest`.
- `CardRequest` (direct card): `name, number, expiry` ("YYYY-MM"), `security_code`, `billing_address` (`Address`). Vaulted card: `CardRequest(vault_id=<token>)`.
- `OrderAuthorizeResponse`: `id`, `status`, `purchase_units[].payments.authorizations[]` → `AuthorizationWithAdditionalData{id, status, amount, expiration_time}`. Authorization id lives here.
- `CapturedPayment`: `id`, `status`, `amount` (Money), `final_capture`, `seller_receivable_breakdown{gross_amount, paypal_fee, net_amount}` (all Money). **Assert `id` present after capture (write; UNSET => outcome unknown).**
- `Refund`: `id`, `status`, `amount` (Money), `seller_payable_breakdown`. **Assert `id` present.**
- `PaymentAuthorization` / `AuthorizationWithAdditionalData`: `status` (`AuthorizationStatus`: CREATED, CAPTURED, DENIED, PARTIALLY_CAPTURED, VOIDED, PENDING), `expiration_time`.
- `PaymentTokenRequest`: `customer` (`Customer{id?, merchant_customer_id?}`), `payment_source` (req). Vault card: `PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard{name,number,expiry,security_code,billing_address})`.
- `PaymentTokenResponse`: `id` (=vault token id, store this), `customer` (`.id` = paypal customer id, store for reuse), `payment_source.card` = `CardPaymentTokenEntity{last_digits, brand, expiry}` (safe display). **Assert `id` present.**
- `SearchResponse`: `transaction_details[]` → `.transaction_info` = `TransactionInformation{transaction_id, transaction_amount(Money), transaction_status, invoice_id, custom_field, transaction_initiation_date}`; `page`, `total_pages`, `total_items`.

### Enums
- `CheckoutPaymentIntent.AUTHORIZE`. Enums are open — reading a status may return a plain str; handle unknown arm.

## Architecture — new app `sandbox/apps/api/` (label `api`)

Register in `INSTALLED_APPS` and include `path('api/', include('apps.api.urls'))` in
`sandbox/urls.py` (OUTSIDE i18n_patterns, before them, so `/api/...` is not language-prefixed).

### Files
- `apps/api/__init__.py`, `apps.py` (AppConfig, name='apps.api', label='api')
- `apps/api/client.py` — lazy sync `PaypalClient` factory from settings; base-URL resolver; `atexit` close.
- `apps/api/paypal_service.py` — thin wrapper over SDK ops with ONE error boundary
  (`OAuthProviderError`→config error; typed `Error`→`PayPalRejected(status,message)`;
  `RawError`→`PayPalFailure`; `ValidationError`→`PayPalUnreadable`; `httpx.HTTPError`→`PayPalUnavailable`).
  Builds request models, asserts required ids on the write responses, extracts auth id/expiry,
  capture fee/net, refund id, card display.
- `apps/api/models.py`:
  - `PayPalPayment(order=OneToOne oscar Order)`: paypal_order_id, authorization_id, auth_status,
    auth_expiry (datetime), capture_id, capture_status, currency, gross_amount, paypal_fee,
    net_amount (Decimal), state (AWAITING_PAYMENT/AUTHORIZED/CAPTURED/VOIDED/REFUNDED/PARTIALLY_REFUNDED),
    authorize_request_id, capture_request_id (persisted PayPal-Request-Id for idempotent retries),
    amount_refunded (Decimal).
  - `PayPalCustomer(user=OneToOne)`: paypal_customer_id.
  - `SavedPaymentMethod(user=FK)`: paypal_token_id (unique), brand, last_digits, expiry, label,
    is_active. NEVER stores PAN/CVV.
  - `RefundRecord(payment=FK, idempotency_key)`: unique_together (payment, idempotency_key);
    paypal_refund_id, amount, status. Enforces refund idempotency.
- `apps/api/order_service.py` — build order from catalogue ids+qty using Oscar basket +
  OrderCreator (details pending Oscar-map subagent). Also money bookkeeping via Oscar
  `payment.Source`/`Transaction`.
- `apps/api/views.py` — the endpoints. Session auth via Django. JSON in/out. Staff-only
  decorator for fulfil/cancel/reconciliation. Shopper-scoping (own orders/cards only).
- `apps/api/urls.py` — routes below.
- `apps/api/tests.py` — unit tests using the transport-stub seam (fake `custom_http_client`),
  covering success + typed-error + RawError + decode-failure + auth-failure + idempotency +
  refund-cap + cross-shopper isolation. Plus a live sandbox integration test guarded by
  `skipif(no creds)`.

### Routes (all under /api/)
- `POST orders` → create order (shopper)
- `POST orders/<id>/pay` → authorize (shopper)
- `POST orders/<id>/fulfil` → capture (staff)
- `POST orders/<id>/cancel` → void (staff)
- `POST orders/<id>/refunds` → refund (staff)
- `GET  my-orders` → caller's orders + payment state (shopper)
- `GET  reconciliation?from&to` → report (staff)
- `POST payment-methods` → save card (shopper)
- `GET  payment-methods` → list (shopper)
- `DELETE payment-methods/<id>` → delete (shopper)

Response id fields: `orderId`, `paymentMethodId`, `refundId` top-level.

## Idempotency design
- **Authorize**: `select_for_update` on PayPalPayment row; if authorization_id already set → return existing (no 2nd auth). Generate+persist `authorize_request_id` (UUID) once; send as PayPal-Request-Id so a concurrent duplicate collapses at PayPal too.
- **Capture**: same pattern with `capture_request_id`; if capture_id set → return existing.
- **Void**: idempotent — if state VOIDED, return ok.
- **Refund**: caller-supplied idempotency key → RefundRecord unique (payment,key). Repeat key → return stored refund. Distinct keys allowed. Guard: sum(refunds) + new ≤ captured gross. Send key as PayPal-Request-Id.

## Resilience notes
- SDK does NO retries. Reads (get_authorized_payment, reconciliation) may add a bounded retry;
  writes are single-attempt + idempotency keys. Timeout set explicitly (~30s default fine for
  ops; keep default).
- Staleness: before capture, if auth_expiry passed OR get_authorized_payment status not
  capturable → `reauthorize_payment`; if that fails (EXPIRED/DENIED) → return an operator-actionable
  error ("authorization can no longer be renewed; re-collect payment").

## REQUIRED READING (companion skills) — ALL LOADED
- python-error-handling (MUST — error boundary) ✅
- python-client-initialization (MUST — client) ✅
- python-testing (MUST — stub transport for tests) ✅
- python-calling-endpoints ✅ · python-models ✅ · python-authentication ✅ · python-configuration-resilience ✅

## Confirmed against the live sandbox (behaviour the map cannot show)
1. **Direct/vaulted card authorize is single-step**: supplying `payment_source.card`
   (with card details or `vault_id`) on `create_order` with intent AUTHORIZE creates the
   authorization inline (order status COMPLETED, `purchase_units[].payments.authorizations[0]`).
   A separate `authorize_order` with a raw card on a card-less order returns 422. So `/pay`
   calls `create_authorized_order` (one `create_order` call) — verified with card 4111.
2. **`invoice_id` must be globally unique** (account setting → `DUPLICATE_INVOICE_ID` 422).
   We send `invoice_id = "<order.number>-<request_id[:12]>"` (unique) and `custom_id =
   order.number` (stable) so reconciliation matches on the report's `custom_field`.
3. **`void_payment` needs `prefer='return=representation'`**: the default (minimal) returns
   204 with an empty body the SDK cannot decode (raises ValueError); representation returns
   200 with a JSON body. Same care taken to prefer representation on authorize/capture/refund.

## Assumptions & Blockers (resolved)
- No blockers. Credentials verified (token fetch + search_transactions returned 200, reporting enabled).
- Direct card `4111...` authorize expected to succeed without 3DS challenge in this sandbox; if a
  challenge/PAYER_ACTION_REQUIRED is returned → STOP & report per task (do not build browser round-trip).
- Oscar order-creation exact API pending subagent map (order_service.py detail) — a repo-convention
  lookup, not an SDK lookup; SDK contract sheet has no open lookups.
