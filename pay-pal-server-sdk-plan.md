# PayPal integration plan — django-oscar sandbox

## SDK identity (drift from the plugin skill — verified against the installed package)

| fact | value | source |
|---|---|---|
| distribution / import root | `paypal` / `paypal` (skill says `pay-pal-server-sdk` / `pay_pal_server_sdk` — **drifted**) | `sdk-map.md`, `pyproject.toml` v2.29 |
| install | `pip install "paypal @ git+https://github.com/context-plugins/paypal-python-sdk.git@main"` | getting-started |
| client | `paypal.PaypalClient` (sync). Keyword-only: `base_url`, `timeout`, `custom_http_client`, `oauth2`, `oauth2_token_source` | `sdk-map.md` |
| auth | `oauth2=ClientCredentials(client_id=…, client_secret=…)` (`paypal.core`). Omitting it = unauthenticated, silent | `sdk-map.md` |
| server | one server; default `https://api-m.sandbox.paypal.com`; token URL follows `base_url` | `sdk-map.md` Servers & auth |
| retries | **none** in SDK | configuration-resilience |

## Host decisions

- Django (WSGI, sync views) → **sync `PaypalClient`**, one lazily-built, process-wide instance (lock-guarded), closed at `atexit`. Built after fork by construction (first use).
- `base_url`: `PAYPAL_BASE_URL` if set (verbatim, token call included). Otherwise explicit map `{"sandbox": "https://api-m.sandbox.paypal.com"}`; any other `PAYPAL_ENVIRONMENT` value → `ImproperlyConfigured` naming `PAYPAL_BASE_URL` (the SDK/map declares only the sandbox host; no production host is available from the plugin → reported as a gap, not invented).
- Transport: `LoggingTransport(HttpxClient(timeout=PAYPAL_TIMEOUT))` — logs method, URL path, status, ms. Never headers/bodies.
- Settings read in `sandbox/settings.py` with empty defaults (import never raises); missing credential check lives in `build_client()`.
- Money: `Decimal`, quantized by currency exponent (JPY/KRW 0; KWD/BHD/TND 3; else 2), compared as `Decimal`.

## CONTRACT SHEET (sync parsed signatures; everything after `*` keyword-only with real defaults — never pass defensive `None`s)

`prefer` defaults to `"return=minimal"` on every write below → **always pass `prefer="return=representation"`** or the result lacks amounts/breakdowns.

| op | signature (positional ∣ kw used) | returns | `ApiError.error` union |
|---|---|---|---|
| `orders.create_order` | `(body: OrderRequest\|Dict, *, pay_pal_request_id, prefer)` | `Order` | `Error`[400,401,422] ∣ `RawError` |
| `payments.capture_authorized_payment` | `(authorization_id, *, pay_pal_request_id, prefer, body: CaptureRequest\|Dict)` | `CapturedPayment` | `Error`[400,401,403,404,409,422] ∣ `RawError`[500,…] |
| `payments.reauthorize_payment` | `(authorization_id, *, pay_pal_request_id, prefer, body: ReauthorizeRequest\|Dict)` | `PaymentAuthorization` | `Error`[400,401,403,404,422] ∣ `RawError` |
| `payments.void_payment` | `(authorization_id, *, pay_pal_request_id, prefer)` | `PaymentAuthorization` | `Error`[401,403,404,409,422] ∣ `RawError` |
| `payments.refund_captured_payment` | `(capture_id, *, pay_pal_request_id, prefer, body: RefundRequest\|Dict)` | `Refund` | `Error`[400,401,403,404,409,422] ∣ `RawError` |
| `vault.create_payment_token` | `(body: PaymentTokenRequest\|Dict, *, pay_pal_request_id)` | `PaymentTokenResponse` | `Error`[400,403,404,422,500] ∣ `RawError` |
| `vault.delete_payment_token` | `(id)` — **returns `None`**; use `with_raw_response` to see status | `None` / `ApiResult[None, …]` | `Error`[400,403,500] ∣ `RawError` (404 → `RawError`) |
| `transaction_search.search_transactions` | `(start_date: str, end_date: str, *, page_size=100, page=1, fields="transaction_info", balance_affecting_records_only="Y")` | `SearchResponse` | **Case B: always `RawError`** |

Idempotency (`pay_pal_request_id`, header `PayPal-Request-Id`), per docstrings: create_order 6h (mandatory for single-step card create), payments writes 45 days, vault create 3h. Smoke-verified: same key on capture/refund returns the original record.

### Request members set (all others left `UNSET` → provider default)

- `OrderRequest`: `intent` (req, `CheckoutPaymentIntent.AUTHORIZE`), `purchase_units` (req, one `PurchaseUnitRequest`), `payment_source` (`PaymentSource.card: CardRequest`).
- `PurchaseUnitRequest`: `amount: AmountWithBreakdown` (req: `currency_code: str`, `value: str`), `custom_id` = `"<order number>:<payment ref>"` (our client reference; carried onto captures/refunds and into transaction search `custom_field`). `invoice_id` omitted (account-level uniqueness would collide across DBs).
- `CardRequest` (one-off): `number`, `expiry` (`YYYY-MM`), `security_code`, `name`, optional `billing_address`. (saved): `vault_id` only.
- `CaptureRequest`: `final_capture=True`, `amount` = order total (explicit).
- `ReauthorizeRequest`: `amount` = order total.
- `RefundRequest`: `amount` explicit (full = remaining).
- `PaymentTokenRequest`: `payment_source.card: PaymentTokenRequestCard(number, expiry, security_code, name, billing_address?)`, `customer: Customer(id=…)` only when the shopper already has a PayPal customer id.

### Response members asserted (UNSET → "unreadable, outcome unknown")

- `Order`: `id`, `status: OrderStatus` [CREATED, SAVED, APPROVED, VOIDED, COMPLETED, PAYER_ACTION_REQUIRED], `purchase_units[0].payments.authorizations[0]` → `AuthorizationWithAdditionalData.id/status/amount/expiration_time/create_time`.
  - `PAYER_ACTION_REQUIRED` → **stop**: payment `failed`, API 409 "requires shopper approval in a browser — not supported" (task mandate).
- `AuthorizationStatus` → ours: CREATED→`authorized` (done) · PENDING→`pending` · DENIED→`failed` · VOIDED→`voided` · CAPTURED/PARTIALLY_CAPTURED→`captured` · other→`unknown`.
- `CapturedPayment`: `id`, `status: CaptureStatus`, `amount`, `seller_receivable_breakdown.gross_amount/paypal_fee/net_amount`, `create_time`. COMPLETED/PARTIALLY_REFUNDED/REFUNDED→`captured` (done) · PENDING→`pending` · DECLINED/FAILED→`failed` · other→`unknown`.
- `PaymentAuthorization` (void/reauth): `id`, `status` (map above), `expiration_time`, `create_time`.
- `Refund`: `id`, `status: RefundStatus` COMPLETED→`done` · PENDING→`pending` · FAILED/CANCELLED→`failed` · other→`unknown`; `amount`, `create_time`.
- `PaymentTokenResponse`: `id`, `customer.id`, `payment_source.card.{brand,last_digits,expiry,name}`.
- `SearchResponse`: `transaction_details[].transaction_info.{transaction_id, transaction_event_code, transaction_initiation_date, transaction_amount, fee_amount, transaction_status, custom_field}`, `total_pages`, `page`, `last_refreshed_datetime`. Range ≤ 31 days per call (docstring) → chunk; paginate to `total_pages`, bounded by a page cap with `truncated` flag.
- `Error` (typed arm): `name`, `message`, `debug_id` required; `details[].issue/description`.

### Error boundary (one ladder, `gateway.py`)

1. `ApiError` with `OAuthProviderError` → config error, 502 (never sent).
2. 401/403 → 502 (ours). 429 → 503. 400/404/409/422 with typed `Error` → caller-facing 422/409 with PayPal issue + description. Anything else → 502, `outcome_unknown = status >= 500`.
3. `pydantic.ValidationError`/`ValueError` (decode) → 502 `outcome_unknown=True` (smoke: a 404 body from vault failed to decode).
4. `httpx.ConnectError/ConnectTimeout/PoolTimeout/ProxyError` → 502, never sent. Other `httpx.RequestError` → 504, outcome unknown.

## Design

App `sandbox/apps/paypal_payments` (label `paypal_payments`), routed at `/api/` in `sandbox/urls.py` outside `i18n_patterns`.

Reused Oscar models: `order.Order`/`Line` (via `Basket` + `OrderCreator.place_order`, Default strategy, `Free` shipping, currency overridden to `PAYPAL_CURRENCY`), `order.set_status` pipeline (Pending → Being processed on authorize → Complete on fulfil; → Cancelled on cancel), `payment.Source`/`Transaction` mirror (allocate/debit/refund).

New models (PayPal-owned state + durable claims, not a parallel order model):
- `PayPalCustomer(user 1:1, paypal_customer_id)`.
- `SavedCard(public_id uuid, user, paypal_token_id unique, paypal_customer_id, brand, last_digits, expiry, name, status active|deleting|deleted, request_id)`.
- `PayPalPayment(order FK, ref uuid (PayPal-Request-Id + custom_id), status sending|authorized|pending|failed|needs_review|unknown|voided|captured, saved_card FK?, amount, currency, paypal_order_id, authorization_id/status/expires_at/created_at, reauthorized_at, reauth_request_id, capture_state + capture_request_id, capture_id/status/amount/fee/net/captured_at, void_state + void_request_id, refund_reserved, error_detail)`; **UniqueConstraint(order) WHERE status NOT IN (failed)** = one live payment per order.
- `PayPalRefund(public_id, payment FK, idempotency_key, request_id, amount, status sending|done|pending|failed|unknown, paypal_refund_id, created_at_provider)`; **Unique(payment, idempotency_key)**.

Flows (claim → call → reconcile → verify → settle):
- **pay**: claim = insert `PayPalPayment(status=sending)`; loser: in-flight → 409 in progress; `unknown`/stale `sending` → resend `create_order` under the **same** `PayPal-Request-Id` (needs the card again; saved card rebuildable) = the kind-2 lookup; done → return stored. Definitive rejection → `failed` (releases claim).
- **fulfil**: conditional update `capture_state '' → sending` is the claim. Honor-period check: if authorization older than 3 days (ReauthorizeRequest docstring) and not yet reauthorized → `reauthorize_payment` (same-key-safe) first; if `expires_at` passed, or already reauthorized and honor window elapsed, or PayPal refuses reauth → 409 with an operator-actionable message (cancel + ask shopper to pay again). Capture with `final_capture=True`, verify echoed amount, store gross/fee/net, Oscar `Complete` only when captured.
- **cancel**: never-paid → Oscar `Cancelled`. Authorized → conditional `void_state '' → sending` claim, `void_payment`, VOIDED → Oscar `Cancelled`. Captured → 409 (use refunds).
- **refund**: `Idempotency-Key` header (or `idempotencyKey` body) required. Same key+amount → stored outcome (sending/unknown → resend same PayPal-Request-Id); same key different amount → 422. New key: atomic reservation `UPDATE … SET refund_reserved = refund_reserved + amt WHERE capture_amount >= refund_reserved + amt` (one statement, any DB) → never beyond captured; released on definitive failure.
- **save card**: optional `Idempotency-Key`; `create_payment_token` with request id; store only token id + brand/last4/expiry/name.
- **delete card**: status → `deleting` (immediately invisible and unusable), `with_raw_response.delete_payment_token`; 2xx or 404 → `deleted`; else stays `deleting`, 502; repeat DELETE retries.
- **reconciliation** (staff): `from`/`to` ISO-8601; ≤31-day chunks, all pages; provider window narrowed back to [from, to). Local side on the provider clock (capture/refund `create_time`). Match by transaction id against the **set** of local capture+refund ids; report `matched`, `providerOnly` (flag `ours` via `custom_id` prefix), `localOnly`, `amountMismatches`, `unsettled` (local payments with unknown/sending/pending state), `truncated`, `lastRefreshed`.

### Revisions made during implementation

- The sandbox sets `ATOMIC_REQUESTS = True`, so the API views are `transaction.non_atomic_requests`. Otherwise a claim would only commit at the end of the request, after the PayPal call.
- SQLite runs with `transaction_mode = IMMEDIATE` and `timeout = 20`. A live concurrency test showed deferred transactions failing with "database is locked" (500) instead of the losing request getting a 409.
- Reconciliation also reports `notYetReportedByPayPal`: local records newer than PayPal's `last_refreshed_datetime`. That lag is expected and is not treated as a local-only discrepancy.
- `httpx`/`httpcore` loggers are set to WARNING. The sandbox root logger is at DEBUG, which would otherwise print transport internals.

## CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| `vault_id` sent to `create_order` must be a token `create_payment_token` returned for **this** user and not deleted | `orders.create_order` ← `vault.create_payment_token` | `SavedCard` lookup by (public_id, user, status=active) |
| `authorization_id` for capture/void/reauth must be the latest one PayPal returned for this order | `capture_authorized_payment`/`void_payment`/`reauthorize_payment` ← `create_order`/`reauthorize_payment` | stored on `PayPalPayment.authorization_id` |
| `capture_id` for refund must be the capture returned at fulfil | `refund_captured_payment` ← `capture_authorized_payment` | `PayPalPayment.capture_id` |
| sum(refunds) ≤ captured amount | `refund_captured_payment` × n | atomic reservation + PayPal REFUND_AMOUNT_EXCEEDED as backstop |
| `customer.id` sent to `create_payment_token` must be one PayPal returned for this user | `create_payment_token` ← `create_payment_token` | `PayPalCustomer` |

## Assumptions & Blockers

- Minor: no production host from the plugin → non-sandbox environment requires `PAYPAL_BASE_URL` (reported gap).
- Minor: expired-authorization error issue names are not in the SDK; the fulfil path decides from `expiration_time`/honor period and surfaces PayPal's own `details[].issue/description` when it refuses.
- Minor: the repo's own test suite requires PostgreSQL (baseline: connection refused), so app tests run via `sandbox/manage.py test` on SQLite.
- Minor: catalogue prices are GBP in fixtures; charged in `PAYPAL_CURRENCY` per the task.

## Toolchain

venv `venv/` (py3.11), `pip install -e .[test]` + SDK. Tests: `cd sandbox && ../venv/Scripts/python manage.py test apps.paypal_payments`. Types: `mypy --strict` on `gateway.py` (SDK-only module); `mypy` (non-strict, ignore-missing-imports) on the Django modules.

## REQUIRED READING (all loaded before implementation)

- MUST load `python-client-initialization` — client lifetime, transport ownership. ✔
- MUST load `python-authentication` — silent no-auth, OAuthProviderError. ✔
- MUST load `python-calling-endpoints` — prefer default, status mapping by name. ✔
- MUST load `python-models` — UNSET vs None, open enums, money scale. ✔
- MUST load `python-error-handling` — ladder, decode failures, httpx split. ✔
- MUST load `python-configuration-resilience` — claims, same-key resend, reconciliation, pagination bounds. ✔
- MUST load `python-testing` — stub transport seam. ✔
