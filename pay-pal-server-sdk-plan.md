# PayPal integration plan — django-oscar sandbox (`sandbox/apps/paypal_payments`)

## Scope
Flow 1 (orders → authorize → capture at fulfil / void at cancel / refunds, my-orders, reconciliation) and
Flow 2 (saved cards via Vault) as a JSON API under `/api/` on the sandbox project.

## Repo survey (conventions + exemplar to imitate)
| Convention | Exemplar |
| --- | --- |
| Sandbox-local Django apps live in `sandbox/apps/<name>` and are imported as `apps.<name>` | `sandbox/apps/sitemaps.py`, `sandbox/urls.py` (`from apps.sitemaps import …`) |
| URLs: plain `path()` list in `sandbox/urls.py`, outside `i18n_patterns` for non-localised routes | `sandbox/urls.py` (`admin/`, `sitemap.xml`) |
| Oscar models loaded with `get_model(app_label, name)` / classes with `get_class` | `src/oscar/apps/order/utils.py` |
| Order placement reuses `OrderCreator.place_order(basket, total, shipping_method, shipping_charge, user=…)` | `src/oscar/apps/order/utils.py` |
| Order status transitions via `order.set_status()` against `OSCAR_ORDER_STATUS_PIPELINE` (Pending → Being processed → Complete / Cancelled) | `sandbox/settings.py` |
| Payment accounting via Oscar `payment.Source` (allocate/debit/refund) + `payment.Transaction`; saved cards via Oscar `payment.Bankcard` (masked number, `partner_reference`) | `src/oscar/apps/payment/abstract_models.py` |
| Settings read with `django-environ` `env(...)` | `sandbox/settings.py` |
| `ATOMIC_REQUESTS=True` — payment views must opt out (`transaction.non_atomic_requests`) so claim rows commit BEFORE the provider call | `sandbox/settings.py` |
| Sync (Django under WSGI, no async views) → **sync `PaypalClient`** | — |

Toolchain: `py -3.11` venv at `venv/` (`pip install -e .[test]`), SDK installed from git as distribution **`paypal`**
(`pip install "paypal @ git+https://github.com/context-plugins/paypal-python-sdk.git@main"`), mypy + django-stubs installed.
Tests: repo uses pytest over `tests/` (settings `tests.settings`); the new app's tests run with the sandbox settings:
`cd sandbox && ../venv/Scripts/python manage.py test apps.paypal_payments`. Type check: `mypy --strict` over the
new app's non-migration modules that touch the SDK (`paypal_client.py`, `gateway.py`).
Baseline: `pytest tests/integration/order tests/integration/payment tests/functional/checkout` on the untouched tree → **every test errors before running**: `tests.settings` needs PostgreSQL on localhost:5432, which this machine does not have (environmental, pre-existing). New-app tests therefore run under the sandbox (SQLite) settings.
Sandbox bootstrap: the host note calling the CSV import optional is wrong here — `orders.json` needs stock records the book CSVs create; `oscar_import_catalogue` on the three `books.*.csv` files gives 209 products (only `range-products.csv` is invalid).

## SDK identity drift (vs python-getting-started)
The skill page says import root `pay_pal_server_sdk`, class `PayPalServerSdkClient`, distribution `pay-pal-server-sdk`.
The SDK map + installed package (v2.29) say: import root **`paypal`**, client **`PaypalClient`** (alias `Client`),
distribution **`paypal`**. Everything else (base URL, auth, controllers, 11 None-returning ops) matches. The map
and the installed package are authoritative; this sheet uses them.

## Configuration
Settings (all read in `sandbox/settings.py` via env, no values committed): `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`,
`PAYPAL_ENVIRONMENT` (default `sandbox`), `PAYPAL_CURRENCY` (default `USD`), `PAYPAL_BASE_URL` (default empty).
Base URL: `PAYPAL_BASE_URL` verbatim when set (token fetch follows `base_url` — map *Servers & auth*); else
`{"sandbox": "https://api-m.sandbox.paypal.com"}[PAYPAL_ENVIRONMENT]`. **The map declares only the sandbox host**
(`paypal/server/server_config.py`); any other environment without `PAYPAL_BASE_URL` → `ImproperlyConfigured`
naming `PAYPAL_BASE_URL` (never fall through to the sandbox default). Always pass `base_url=` explicitly.
Credentials checked when the client is built (lazily, first use) → `ImproperlyConfigured` naming the missing setting.

## Contract sheet (every row from `sdk-map.md`, `map/operations/*.md`, model/enum modules; verified by sandbox smoke 2026-09-24)

### Client
- `from paypal import PaypalClient`; `from paypal.core import ClientCredentials, ApiError, RawError, OAuthProviderError, HttpxClient, HttpRequest, HttpResponse, UNSET, UnsetType`.
- `PaypalClient(*, base_url: str|None, timeout: float=30.0, custom_http_client: HttpClient|None, oauth2: ClientCredentialsOrDict|None, oauth2_token_source)` — keyword-only. **Omitting `oauth2` = unauthenticated requests, no error** → always set.
- Sync only (host is WSGI). Held as lazily built module-global (thread-safe token cache), closed via `atexit`. Transport: `HttpxClient(timeout=20.0)` wrapped by a logging transport (method + path + status only). With a custom transport the client's `timeout=` does not reach the wire → timeout set on `HttpxClient`.
- **No retries in SDK.** Retries are ours: only "resend under the SAME PayPal-Request-Id" for writes (PayPal de-duplicates, confirmed in smoke), never under a new key.
- Every keyword-only param has a real default; do not pass `None`s. `prefer` defaults to `"return=minimal"` → we pass `prefer="return=representation"` on every write so bodies carry status/amounts.

### Operations (sync parsed spelling; all also on `.with_raw_response`)
| Op | Signature (positional \| keyword-only) | Returns | `ApiError.error` union |
| --- | --- | --- | --- |
| `orders.create_order` | `(body: OrderRequest\|Dict, *, pay_pal_request_id, prefer, …)` | `Order` | `CreateOrderErrorBody = Error [400,401,422] \| RawError` |
| `payments.capture_authorized_payment` | `(authorization_id, *, pay_pal_request_id, prefer, body: CaptureRequest\|Dict\|None)` | `CapturedPayment` | `Error [400,401,403,404,409,422] \| RawError [500,…]` |
| `payments.reauthorize_payment` | `(authorization_id, *, pay_pal_request_id, prefer, body: ReauthorizeRequest\|None)` | `PaymentAuthorization` | `Error [400,401,403,404,422] \| RawError [500,…]` |
| `payments.void_payment` | `(authorization_id, *, pay_pal_request_id, prefer)` | `PaymentAuthorization` | `Error [401,403,404,409,422] \| RawError [500,…]` |
| `payments.get_authorized_payment` | `(authorization_id)` | `PaymentAuthorization` | `Error [401,403,404] \| RawError` |
| `payments.get_captured_payment` | `(capture_id)` | `CapturedPayment` | `Error [401,403,404] \| RawError` |
| `payments.refund_captured_payment` | `(capture_id, *, pay_pal_request_id, prefer, body: RefundRequest\|None)` | `Refund` | `Error [400,401,403,404,409,422] \| RawError [500,…]` |
| `vault.create_payment_token` | `(body: PaymentTokenRequest\|Dict, *, pay_pal_request_id)` | `PaymentTokenResponse` | `Error [400,403,404,422,500] \| RawError` |
| `vault.delete_payment_token` | `(id)` | **`None`** (raw: `ApiResult[None, …]`) | `Error [400,403,500] \| RawError` |
| `transaction_search.search_transactions` | `(start_date: str, end_date: str, *, fields="transaction_info", balance_affecting_records_only="Y", page_size=100, page=1, …)` | `SearchResponse` | **Case B: always `RawError`** |
Not used: `orders.authorize_order` — smoke proved a card `create_order` with intent AUTHORIZE returns order `COMPLETED` with the authorization already in `purchase_units[0].payments.authorizations[0]`. If the order comes back `CREATED`/`APPROVED` without an authorization, it is treated as `unknown` (not silently authorized).

Auth failure on any call: `ApiError` with `.error` `OAuthProviderError | RawError` — raised even in raw mode → config error (502 to caller).

### Request models (members we set; everything else left `UNSET` = provider default)
- `OrderRequest`: `intent: CheckoutPaymentIntentOrStr` (req) = `CheckoutPaymentIntent.AUTHORIZE`; `purchase_units: list[PurchaseUnitRequest]` (req); `payment_source: Optional[PaymentSource]`.
- `PurchaseUnitRequest`: `amount: AmountWithBreakdown` (req: `currency_code: str`, `value: str`); `invoice_id` = `<Oscar order number>-<12 hex of payment reference>` — **revised after live run**: the sandbox merchant enforces unique invoice ids (`DUPLICATE_INVOICE_ID` 422) and Oscar order numbers restart per database, so each attempt carries its own, stored on `PayPalPayment.invoice_id` and used for reconciliation; `custom_id` = our payment reference; `description` = "Order <number>" (shopper-facing text, ≤127). `reference_id`, `items`, `shipping`, `soft_descriptor`, `payee`: omitted → provider default.
- `PaymentSource.card: CardRequest` — one-off: `number`, `expiry` (`YYYY-MM`), `security_code`, `name`, `billing_address: Address` (`country_code` required; `address_line_1`, `admin_area_2`, `admin_area_1`, `postal_code` optional). Saved: `vault_id` only (smoke: works without CVV/stored_credential). `attributes`, `stored_credential`, `experience_context`: omitted → provider default (UNVERIFIED need; smoke passed without).
- `CaptureRequest`: `final_capture=True` (whole authorization captured once); `invoice_id`: omit (already on the purchase unit); `amount`: omitted → full authorized amount (what we want).
- `ReauthorizeRequest.amount: Optional[Money]` — omit → same amount.
- `RefundRequest.amount: Optional[Money]` — always sent (explicit amount, computed full remainder for a full refund). Other members omitted.
- `PaymentTokenRequest`: `payment_source: PaymentTokenRequestPaymentSource` (req) → `.card: PaymentTokenRequestCard` (`name`, `number`, `expiry`, `security_code`, `billing_address`); `customer: Optional[Customer]` → `Customer(id=<PayPal customer id>)` when the shopper already has one (keeps one PayPal customer per shopper), omitted on first save (PayPal creates one; smoke).
- Money is `str`; built with `Decimal.quantize` using the currency exponent (JPY/KRW 0, KWD/BHD/TND 3, else 2). Echoes compared as `Decimal`.

### Response members we depend on (assert present — absent ⇒ outcome unknown)
- `Order.id`, `.status: OrderStatusOrStr`, `.purchase_units[0].payments.authorizations[0]` (`AuthorizationWithAdditionalData`: `id`, `status`, `amount: Money`, `expiration_time: str`, `create_time`), `.payment_source.card` (`brand`, `last_digits`).
- `PaymentAuthorization`: `id`, `status`, `amount`, `expiration_time`, `create_time`.
- `CapturedPayment`: `id`, `status`, `amount`, `create_time`, `seller_receivable_breakdown` (`gross_amount: Money` req, `paypal_fee`, `net_amount`).
- `Refund`: `id`, `status`, `amount`, `create_time` (offset form e.g. `-07:00`).
- `PaymentTokenResponse`: `id`, `customer.id`, `payment_source.card` (`brand`, `last_digits`, `expiry`, `name`).
- `SearchResponse`: `transaction_details[].transaction_info` (`transaction_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_amount`, `fee_amount`, `transaction_status`, `invoice_id`, `custom_field`), `total_pages`, `page`, `last_refreshed_datetime`.
- `Error` (typed arm): `name: str`, `message: str`, `debug_id: str` (required), `details: Optional[list[ErrorDetails]]` (`issue: str` req, `description`).

### Status enums → our outcome (every member named; anything else → `unknown`)
- `OrderStatus` (create_order): `COMPLETED` → look at authorization; `CREATED`/`SAVED`/`APPROVED` → unknown (no auth made); `PAYER_ACTION_REQUIRED` → **failed: 3-D Secure browser challenge — unsupported (task: stop/report)**; `VOIDED` → failed.
- `AuthorizationStatus`: `CREATED` → **authorized (done)**; `PENDING` → pending; `DENIED` → failed; `VOIDED` → voided; `CAPTURED`/`PARTIALLY_CAPTURED` → (after capture only) captured; else unknown.
- `CaptureStatus`: `COMPLETED` → **captured (done)**; `PENDING` → capture_pending; `DECLINED`/`FAILED` → capture_failed; `PARTIALLY_REFUNDED`/`REFUNDED` → captured (refund state tracked separately); else unknown.
- `RefundStatus`: `COMPLETED` → **done**; `PENDING` → pending; `FAILED`/`CANCELLED` → failed (reservation released); else unknown.
- `AuthorizationIncompleteReason` (`PENDING_REVIEW`, `DECLINED_BY_RISK_FRAUD_FILTERS`) surfaced as reason text.

### Semantics from docstrings / api-reference.md
- `pay_pal_request_id`: mandatory for single-step create-order with card; server keeps keys 6 h (create order), others 3–72 h / 45 days. A resend of an unknown write happens only within 3 h of the first send (smallest window) and under the same key; older → `needs_review`.
- Reauthorize: honor period 3 days; reauthorize from day 4 to 29 (smoke: `REAUTHORIZATION_TOO_SOON` 422 on day 0); after `expiration_time` (29 days) a new authorization is needed. Fulfil: within honor → capture; past honor → reauthorize then capture the NEW authorization id; expired or reauth rejected with capture also rejected → 409 with operator-actionable text.
- Refund: over-refund rejected by PayPal (`REFUND_AMOUNT_EXCEEDED` 422, smoke) — we also block locally with an atomic reservation.
- Void: "cannot void a fully captured authorization".
- search_transactions: RFC3339 with seconds; **max range 31 days** → split into ≤31-day windows; page via `page`/`total_pages`; `balance_affecting_records_only="Y"` (captures/refunds; authorizations are not balance-affecting and are not expected on the provider side). Reporting lags live activity (`last_refreshed_datetime`).
- delete_payment_token: 204; smoke shows GET still returns the token afterwards → "no longer usable" is enforced locally (token row marked deleted before PayPal call; payment with a non-active saved card refused).

### Error boundary (one ladder, `gateway._call`) → `PayPalError(status_code, message, outcome_unknown, issue, debug_id)`
auth (`OAuthProviderError`) → 502 config · 401/403 → 502 · 429 → 503 · `Error` with 400/404/409/422 → caller's (same status, issue + message) · 5xx/unmapped → 502, `outcome_unknown` = status≥500 · `ValidationError` → 502 unknown · `ConnectError/ConnectTimeout/PoolTimeout/ProxyError` → 502 known-not-sent · other `httpx.RequestError` → 504 unknown.

## Local data model (app `paypal_payments`, one migration)
- `PayPalPayment` — one live per order (partial `UniqueConstraint(order)` where status not in failed/expired/voided). reference (uuid, = PayPal-Request-Id + custom_id), status, amount, currency, saved card FK, PayPal ids+statuses (order, authorization, capture), authorization expiry/time, fee/net, captured/refunded/refund_reserved amounts, provider times, oscar `Source` FK, failure detail.
- `PayPalRefund` — unique (payment, idempotency_key); amount, status, paypal id/status, provider time.
- `PayPalSavedCard` — public id (= `paymentMethodId`), user, Oscar `Bankcard` (masked number/brand/expiry, `partner_reference` = vault token id), idempotency key (unique per user), vault token id, PayPal customer id, status (sending/active/deleting/deleted/failed/unknown).
- Oscar `Order`/`Line` via `OrderCreator`; Oscar `Source` (type "PayPal") + `Transaction` (Authorise/Debit/Refund/Void) for accounting.

## Assumptions & Blockers
- Minor: catalogue stock records are priced in GBP; per the task, amounts come from catalogue prices and the currency from `PAYPAL_CURRENCY` (so the numbers are charged in USD). Recorded on the order (`order.currency`).
- Minor: refunds endpoint — allowed for the order's owner (shopper-scoped) and for staff operators.
- Minor: offers/vouchers are not applied to API orders (list price), shipping = Oscar `Free`, tax = strategy's (NoTax).
- Minor: live environment host isn't in the plugin → requires `PAYPAL_BASE_URL` for any non-sandbox environment.
- No blockers: smoke produced no 3-D Secure challenge (PAYER_ACTION_REQUIRED) for the sandbox card.

## CROSS-OPERATION INVARIANTS
| invariant | operations | enforced where |
| --- | --- | --- |
| `vault_id` sent on create_order must be a token that `create_payment_token` returned for THIS shopper and is still active locally | `orders.create_order` ← `vault.create_payment_token` | `services.pay` (lookup by `paymentMethodId` + user + status=active) |
| `Customer.id` sent on create_payment_token must be the one PayPal returned for THIS shopper | `vault.create_payment_token` ← `vault.create_payment_token` | `services.save_card` |
| authorization id captured/voided/reauthorized must be the one create_order (or reauthorize) returned for THIS order | `payments.capture_authorized_payment`/`void_payment`/`reauthorize_payment` ← `orders.create_order`/`reauthorize_payment` | `PayPalPayment.authorization_id` |
| capture id refunded must be the one capture returned for this order; Σ refunds ≤ captured amount | `payments.refund_captured_payment` ← `payments.capture_authorized_payment` | atomic `refund_reserved` conditional update |
| deleted token id must be one saved by this shopper | `vault.delete_payment_token` ← `vault.create_payment_token` | `services.delete_card` |

## REQUIRED READING (all loaded before implementation)
- MUST load `python-error-handling` — the ladder above, auth-before-union, transport split (loaded).
- MUST load `python-client-initialization` — lazy module-global sync client, close via atexit (loaded).
- MUST load `python-configuration-resilience` — claim/call/reconcile/verify/settle; conditional-update transitions; reconciliation clocks, 31-day windows, bounded paging with `truncated` (loaded).
- MUST load `python-calling-endpoints` — status → outcome mapping by name with `unknown` default; `prefer` default narrows bodies (loaded).
- MUST load `python-models` — `UNSET` isinstance narrowing; currency exponent; `to_dict(exclude_unset=True)` (loaded).
- MUST load `python-authentication` — credentials read at build time, empty settings default + check (loaded).
- MUST load `python-testing` — StubTransport seam with token response first; two distinct transport-failure tests (loaded).

## Build order
1. settings + app skeleton + models/migration. 2. `paypal_client.py` (client factory, logging transport). 3. `gateway.py` (typed SDK calls + error ladder + status mapping). 4. `services.py` (orders, pay, fulfil, cancel, refunds, cards, reconciliation). 5. `views.py` + `urls.py` + wiring. 6. tests (stub transport). 7. mypy --strict + tests. 8. live sandbox E2E via HTTP on port 37820.
