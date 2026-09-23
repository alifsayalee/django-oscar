# PayPal integration plan — django-oscar sandbox

Adds PayPal card payments + saved cards to the sandbox as a new Django app
`sandbox/apps/payments_api/` (app label `payments_api`), routed under `/api/`.
Additive: reuses Oscar's Order/OrderLine models; does not touch the storefront flow.

## Toolchain / environment (verified)

- Python 3.11 venv at `repo/venv`. Project installed editable with `[test]` extras.
- PayPal SDK **installed** into the venv from the local clone of the map repo
  (`pip install ../sdk-map`) — the `git+https` install failed on this host (git exit 128),
  the local clone is byte-identical. Distribution `paypal` **2.29**, import root `paypal`,
  sync client `PaypalClient`. (The `python-getting-started` snapshot named `pay_pal_server_sdk`;
  the installed package and its own SDK map say `paypal` — trust the package. Verified importable.)
- Sandbox DB built: `migrate`, `loaddata auth/child_products`, `oscar_populate_countries`,
  then `oscar_import_catalogue` on the three book CSVs → **209 products, 203 priced stockrecords,
  2 users** (`superuser` staff+super, `staff` staff), 249 countries. The `pages/ranges/offers`
  and `orders` sample fixtures fail on FK constraints against the incomplete pre-import catalogue;
  they are storefront sample data the API does not use — skipped, not a blocker.
- Type checker: project ships none configured for app code; will install `mypy` into venv and run
  `mypy` on the new app (SDK is generated under `--strict` and checks clean).
- Tests: `pytest` available via `[test]`. New app ships its own unit tests using the transport seam.

## Credentials & config (secrets stay out of the repo)

Read via django-environ in `sandbox/settings.py`, all with defaults so importing settings needs no
secrets (values NEVER written to any file):

```
PAYPAL_CLIENT_ID     = env.str('PAYPAL_CLIENT_ID', default='')
PAYPAL_CLIENT_SECRET = env.str('PAYPAL_CLIENT_SECRET', default='')
PAYPAL_ENVIRONMENT   = env.str('PAYPAL_ENVIRONMENT', default='sandbox')
PAYPAL_CURRENCY      = env.str('PAYPAL_CURRENCY', default='USD')
PAYPAL_BASE_URL      = env.str('PAYPAL_BASE_URL', default='')   # optional verbatim override
```

Base-URL resolution (in the client factory): if `PAYPAL_BASE_URL` is non-empty use it **verbatim**
for every call incl. the token fetch; else map `PAYPAL_ENVIRONMENT`: `sandbox` →
`https://api-m.sandbox.paypal.com`, `live`/`production` → `https://api-m.paypal.com`; unknown value
→ raise ImproperlyConfigured (never silently default). Confirmed: token endpoint derives from
`base_url`, so one knob moves everything.

## Client (python-client-initialization)

Django/WSGI → **sync** `PaypalClient`, lazily-initialised module global in
`apps/payments_api/paypal_client.py` (`get_client()`), so importing the module needs no creds.
`oauth2=ClientCredentials(client_id, client_secret)`, `timeout=20.0` (deliberate, not the 30s default),
`base_url=` resolved above. Long-lived, reused; closed via `atexit`. Credentials asserted present at
first use (raise ImproperlyConfigured if blank) — the SDK builds happily with no auth and would 401.

## Sync vs async: SYNC everywhere (Django WSGI). The two clients never mix.

## ATOMIC_REQUESTS gotcha (decisive)

`sandbox/settings.py` sets `ATOMIC_REQUESTS=True`: a whole request is one transaction, so a
`transaction.atomic()` inside a view is only a savepoint and **nothing commits until the view
returns**. That defeats the durable-claim-before-provider-call pattern (a claim row would roll back
if anything after the PayPal call raised, stranding a real hold/capture at PayPal). Therefore the
payment-mutating views run with **`@transaction.non_atomic_requests`** (decorate `dispatch`), and
each view drives its own `transaction.atomic()` blocks that genuinely commit: claim-commit → call
PayPal → settle-commit. Reads may stay atomic.

## Framework: plain Django JSON views (no DRF dependency added)

Session auth (Django's own). A small base view: rejects anonymous → 401; operator views additionally
require `request.user.is_staff` → 403. `@csrf_exempt` on the API (cookie-session JSON API driven by a
script/curl; documented). JSON in/out via a shared helper. No new runtime deps.

## Data model (durable rows — the app's own DB; never the record of *what exists* at PayPal)

Full PAN/CVV/expiry are NEVER stored and NEVER logged. Only PayPal ids + safe descriptors.

- `PayPalCustomer(user OneToOne, customer_id, created)` — one PayPal customer id per shopper, so a
  shopper's vaulted cards group under one customer.
- `SavedCard(user FK, paypal_token_id unique, brand, last_digits, expiry 'YYYY-MM', created)` —
  Flow 2. Listing reads THIS table (PayPal's vault list lags — verified empty right after a save),
  scoped by `user`. Delete removes the row AND deletes the PayPal token.
- `OrderPayment(order OneToOne→order.Order, user FK, status, currency, amount Decimal,
  invoice_id unique, paypal_order_id, authorization_id, auth_status, auth_expiry,
  authorize_request_id unique-null, capture_id, capture_status, captured_value, paypal_fee,
  net_amount, capture_request_id, capture_time, created, updated)` — one durable row per order.
  `status` ∈ {awaiting_payment, authorizing, authorized, capturing, captured, partially_refunded,
  refunded, cancelling, voided, failed, needs_review, unknown}. This row is the claim for the
  authorize/capture/void operations.
- `PaymentRefund(order_payment FK, idempotency_key, paypal_refund_id, amount Decimal, status,
  created)` — `unique_together(order_payment, idempotency_key)` is the refund idempotency guard.

## Money formatting (python-models)

Currency-aware exponent, never hardcoded `.2f`:
`EXPONENT={'JPY':0,'KRW':0,'HUF':0,'TWD':0,'KWD':3,'BHD':3,'TND':3}` default 2;
`value = str(Decimal(amount).quantize(Decimal(1).scaleb(-places)))`. Order total → this string.
Compare echoed vs sent as `Decimal` (verify step). The hold value == order total to the cent.

## Status mapping (python-calling-endpoints — one function, explicit members, `_`→unknown)

- authorization (`AuthorizationStatus`): CREATED→authorized(hold live) · CAPTURED/PARTIALLY_CAPTURED→captured
  · VOIDED→voided · DENIED→failed · PENDING→pending · `_`→unknown.
- capture (`CaptureStatus`): COMPLETED→captured · PARTIALLY_REFUNDED→partially_refunded ·
  REFUNDED→refunded · DECLINED/FAILED→failed · PENDING→pending · `_`→unknown.
- Never `case _: failed`. Read outcome from the status by name; an id back ≠ success.

---

## HTTP endpoints (all under `/api/`, each action separately invocable)

Flow 1 (shopper unless noted):
- `POST /api/orders` — body `{items:[{productId,quantity}...]}` → build Oscar basket (Selector
  strategy), `NoShippingRequired`, `OrderTotalCalculator`, `OrderCreator.place_order(user=...)`.
  Create `OrderPayment(status=awaiting_payment, amount=total, invoice_id)`. Returns `{orderId}` top-level.
- `POST /api/orders/{orderId}/pay` — body either `{card:{number,expiry,securityCode,name,billingAddress}}`
  or `{savedCardId}`. Single-step `orders.create_order(intent=AUTHORIZE, payment_source.card=…,
  pay_pal_request_id=authorize_request_id, prefer=return=representation)` → authorization under
  `purchase_units[0].payments.authorizations[0]`. Claim-first + idempotent (below).
- `POST /api/orders/{orderId}/fulfil` — **operator**. Capture (`payments.capture_authorized_payment`,
  final_capture=True, return=representation). Stale-auth handling below. Persist captured amount,
  `seller_receivable_breakdown.paypal_fee`, `.net_amount`, capture_time. Advance Oscar status.
- `POST /api/orders/{orderId}/cancel` — **operator**. Void (`payments.void_payment`,
  prefer=return=representation) before fulfilment → releases hold. Oscar status → Cancelled.
- `POST /api/orders/{orderId}/refunds` — shopper (owner) or operator. Body `{amount?, idempotencyKey}`.
  `payments.refund_captured_payment` (amount for partial, empty for full). Caps total refunded ≤
  captured. Returns `{refundId}`.
- `GET /api/my-orders` — caller's OrderPayments + Oscar order info + payment state.
- `GET /api/reconciliation?from&to` — **operator**. Chunk range into ≤31-day windows, page each,
  narrow to instants, match provider tx to OrderPayments by invoice_id (set-match), classify
  matched / local-only / provider-only / unsettled.

Flow 2 (shopper):
- `POST /api/payment-methods` — body `{card:{...}}` → ensure PayPalCustomer, `vault.create_payment_token`
  (customer.id + payment_source.card), store SavedCard (safe descriptor from response card). Returns
  `{paymentMethodId}`.
- `GET /api/payment-methods` — caller's SavedCards (from DB), safe fields only.
- `DELETE /api/payment-methods/{paymentMethodId}` — caller's card: `vault.delete_payment_token`
  (raw peer, expect 204), delete row. 404 if not caller's.

Scoping: every OrderPayment/SavedCard query filtered by `user=request.user`; a mismatch → 404.

## Idempotency / resilience (python-configuration-resilience, python-error-handling)

- **/pay**: OrderPayment is the claim. In a committed atomic block: conditional transition
  `OrderPayment.filter(pk, status='awaiting_payment').update(status='authorizing',
  authorize_request_id=uuid)` — rowcount 0 means already-claimed → return current state (if already
  `authorized`, idempotent success; if `authorizing`, reuse stored request-id). Commit. Then call
  create_order with the stored `authorize_request_id` (PayPal dedupes 6h). Settle in a second commit.
  Transport never-sent → status back to awaiting_payment (release). Read-timeout/5xx (may have landed)
  → status `unknown`, no blind retry; a retry reuses the SAME request-id. Verify echoed auth amount ==
  order total (Decimal) before settling → mismatch → `needs_review`.
- **/fulfil**: claim authorized→capturing conditionally; reuse `capture_request_id`. If already
  captured → idempotent return. Stale auth: on capture failure whose PayPal issue indicates an
  expired/invalid authorization, call `reauthorize_payment`; on success retry capture; if reauth
  itself fails (beyond the 29-day window / not reauthorizable) → return 409 with the PayPal message
  verbatim-ish so an operator can act ("authorization can no longer be renewed; re-collect payment").
- **/cancel**: conditional authorized→cancelling; void; idempotent if already voided.
- **/refunds**: `unique_together(order_payment, idempotency_key)` — `get_or_create` on the key; on
  IntegrityError / existing row return the stored refund (no second refund). Two DISTINCT keys =
  two legitimate partial refunds. App-side cap: sum(prior refund amounts)+new ≤ captured_value.
- Boundary error mapping (python-error-handling table): 401/403(from PayPal)→502; 429→503;
  400/404/409/422 with typed `Error`→ surface as client fault with PayPal `message`; ValidationError
  decode on 2xx write→ treat as unknown (look up by invoice_id), on error→ reject; transport
  never-sent→502, no-reply→504. void/capture/refund/create MUST use `prefer=return=representation`
  (verified: an empty `return=minimal` body raises ValueError in BOTH response modes, incl. raw peer).

## Reconciliation details

Provider filter is whole-instant but capped at **31 days** (verified 400 `Date range is greater than
31 days`). Split [from,to] into ≤31-day sub-windows; for each, page `search_transactions(start,end,
fields='transaction_info', page_size=100, page=n)` until `page > total_pages` or short page, bounded
by MAX_PAGES; format datetimes `YYYY-MM-DDTHH:MM:SS+0000`. Narrow returned tx to the caller's exact
instants. Match tx.transaction_info.invoice_id to OrderPayment.invoice_id against the SET (one order
owns auth+capture+refund tx). Local side filtered on capture_time (provider clock); OrderPayments
authorized-not-captured in the created window reported as `unsettled`. Report `truncated` if any page
cap hit. Empty result over a fresh range is expected (reporting lag) — NOT a gap.

---

## CONTRACT SHEET (grounded in the SDK map + source, verified by live smoke)

Client: `from paypal import PaypalClient` · `from paypal.core import ClientCredentials, ApiError,
RawError, Success, Failure, UNSET`. All operations sync, parsed unless noted; each also has
`with_raw_response`. Path params positional; everything after `*` keyword-only with real defaults.

### orders.create_order  (used for /pay — single-step card authorize)
- `create_order(body: OrderRequest|dict, *, pay_pal_request_id=None, prefer='return=minimal', …) -> Order`
- body `OrderRequest`: `intent`(req, `CheckoutPaymentIntent.AUTHORIZE`="AUTHORIZE"), `purchase_units`
  (req `list[PurchaseUnitRequest]`), `payment_source`(opt `PaymentSource`).
  - `PurchaseUnitRequest`: `amount`(req `AmountWithBreakdown{currency_code,value}`), `custom_id`(set:
    order.number), `invoice_id`(set: unique invoice → match key), `reference_id` omit→'default'.
  - `PaymentSource.card = CardRequest`: raw card → `{name,number,expiry 'YYYY-MM',security_code,
    billing_address:Address{address_line_1,admin_area_2,admin_area_1,postal_code,country_code(req)}}`;
    saved card → `{vault_id: <token>}`.
- **pay_pal_request_id is MANDATORY for single-step card create** (docstring) AND is the idempotency
  key. `prefer='return=representation'` to get authorizations back.
- Returns `Order`: `id`, `status`(`OrderStatus`; COMPLETED expected), `purchase_units[0].payments
  .authorizations[0]` = `AuthorizationWithAdditionalData` → `id`, `status`(`AuthorizationStatus`),
  `amount{value,currency_code}`, `expiration_time`. Verified live: status COMPLETED, auth CREATED.
- Errors `CreateOrderErrorBody = Error | RawError` [400,401,422 typed]. A browser challenge would
  surface as status `PAYER_ACTION_REQUIRED` + approve link → STOP & report (not expected for 4111 card).

### orders.get_order(id, *, fields=None,…) -> Order  (state re-read; Error[401,404]|RawError)

### payments.capture_authorized_payment  (/fulfil)
- `capture_authorized_payment(authorization_id, *, pay_pal_request_id=None, prefer='return=minimal',
  body: CaptureRequest|dict|None, …) -> CapturedPayment`
- body `CaptureRequest`: `amount`(opt Money), `final_capture`(opt bool → True), `invoice_id`(opt).
- Use `prefer='return=representation'` + `pay_pal_request_id=capture_request_id`.
- `CapturedPayment`: `id`, `status`(`CaptureStatus`; COMPLETED), `amount{value,currency_code}`,
  `seller_receivable_breakdown`(`SellerReceivableBreakdown`: `gross_amount`(req Money), `paypal_fee`
  (Money), `net_amount`(Money)), `final_capture`. Verified live: gross 12.34 fee 0.81 net 11.53.
- Errors `Error[400,401,403,404,409,422] | RawError[500,…]`.

### payments.reauthorize_payment(authorization_id, *, pay_pal_request_id, prefer='return=minimal',
  body: ReauthorizeRequest{amount?}, …) -> PaymentAuthorization  (stale-auth renewal; use
  return=representation. Error[400,401,403,404,422]|RawError.)

### payments.void_payment(authorization_id, *, prefer='return=minimal', pay_pal_request_id, …)
  -> PaymentAuthorization  (/cancel). **MUST pass prefer='return=representation'** — verified: default
  minimal returns empty body → ValueError in both modes. Returns status VOIDED. Error[401,403,404,409,422]|RawError.

### payments.refund_captured_payment(capture_id, *, pay_pal_request_id, prefer='return=minimal',
  body: RefundRequest|dict|None, …) -> Refund  (/refunds)
- body `RefundRequest`: `amount`(opt Money — partial; omit for full), `custom_id`, `invoice_id`,
  `note_to_payer`. Use return=representation + pay_pal_request_id=idempotency key.
- `Refund`: `id`, `status`, `amount`. Error[400,401,403,404,409,422]|RawError. Verified live: COMPLETED.

### vault.create_payment_token  (/payment-methods POST)
- `create_payment_token(body: PaymentTokenRequest|dict, *, pay_pal_request_id=None, …) -> PaymentTokenResponse`
- body: `customer`(opt `Customer{id?, merchant_customer_id?}` — pass stored customer id to group),
  `payment_source`(req `PaymentTokenRequestPaymentSource{card: PaymentTokenRequestCard{name,number,
  expiry,security_code,brand?,billing_address?}}`).
- `PaymentTokenResponse`: `id`(token), `customer.id`(store first time), `payment_source.card`
  =`CardPaymentTokenEntity{last_digits,brand,expiry,name}` (safe descriptor). Verified live.
- Error `Error[400,403,404,422,500] | RawError`.

### vault.delete_payment_token(id, *, request_options=None) -> None  (/payment-methods DELETE)
- Returns None. Use `with_raw_response` to read status (200/204). Error[400,403,500]|RawError.

### vault.list_customer_payment_tokens(customer_id, *, page_size=5, page=1, total_required=False,…)
  -> CustomerVaultPaymentTokensResponse  (NOT relied on for the shopper list — lags; DB is source.)

### transaction_search.search_transactions(start_date, end_date, *, transaction_id=None,…,
  fields='transaction_info', balance_affecting_records_only='Y', page_size=100, page=1, …) -> SearchResponse
- **Case B**: error is always `RawError` (no typed arm). Dates `YYYY-MM-DDTHH:MM:SS+0000`. Max 31-day
  range (verified). `SearchResponse`: `transaction_details[].transaction_info`
  =`TransactionInformation{transaction_id, transaction_amount{value,currency_code}, invoice_id,
  custom_field, transaction_event_code, transaction_status, transaction_initiation_date}`,
  `total_items`, `total_pages`, `page`.

### None-returning ops in scope: only `vault.delete_payment_token`. (patch/update-tracking not used.)
### Error unions: every op above is `Error | RawError` EXCEPT search_transactions (RawError only).
### No SDK retries — retry/idempotency is ours (request-ids + durable rows), built as above.
### base_url selects the sandbox host by default; we set it explicitly from config.

## CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| a `vault_id` used to pay must be a token this shopper created | `orders.create_order(payment_source.card.vault_id)` ← `vault.create_payment_token` | app: `SavedCard.filter(user=..., id=savedCardId)` → its `paypal_token_id` |
| a capture's `authorization_id` must be one this order's /pay produced | `payments.capture_authorized_payment` ← `orders.create_order` | app: `OrderPayment.authorization_id` |
| a refund's `capture_id` must be this order's capture | `payments.refund_captured_payment` ← `payments.capture_authorized_payment` | app: `OrderPayment.capture_id` |
| a void's `authorization_id` must be this order's live auth | `payments.void_payment` ← `orders.create_order` | app: `OrderPayment.authorization_id`, status guard |
| reconciliation matches provider tx to a local order by invoice | `transaction_search.search_transactions` ↔ `orders.create_order(invoice_id)` | app: `OrderPayment.invoice_id` set-match |
| a delete makes a saved card unusable to pay | `vault.delete_payment_token` → `orders.create_order` | app: row deleted, so vault_id no longer resolvable for this shopper |

## REQUIRED READING (all loaded before implementation)
- python-error-handling — MUST load (every boundary). LOADED.
- python-client-initialization — MUST load (client + non_atomic_requests). LOADED.
- python-configuration-resilience — MUST load (idempotency, reconciliation, 31-day chunk, pagination). LOADED.
- python-calling-endpoints — MUST load (status-not-id, prefer defaults, response modes). LOADED.
- python-models — MUST load (UNSET, open enums, currency-exponent money). LOADED.
- python-testing — MUST load (transport-seam stub before any test / verify script). LOADED.

## Assumptions & blockers
- No blockers. Direct-card single-step AUTHORIZE + capture + refund + vault + saved-card-reuse all
  verified live against the sandbox with card 4111…; void needs return=representation; search caps at
  31 days; vault-list lags. Design decisions (single-step /pay, own-DB card listing,
  non_atomic_requests, plain-Django JSON, invoice_id match key) are made — proceeding.
