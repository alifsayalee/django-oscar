# PayPal integration plan — django-oscar sandbox (`sandbox/apps/paypal_payments`)

## SDK identity (verified against the installed package, not the skill snapshot)

| Fact | Value |
|---|---|
| Distribution / import root | **`paypal`** / **`paypal`** — DRIFT: the skill page says `pay-pal-server-sdk` / `pay_pal_server_sdk`; the map (`sdk-map.md`) and the installed package both say `paypal`. Code uses `paypal`. |
| Version | 2.29 (`pyproject.toml`), installed from `github.com/context-plugins/paypal-python-sdk@main` (non-editable, from a clone outside the repo) |
| Client | `PaypalClient` (sync), keyword-only: `base_url`, `timeout=30.0`, `custom_http_client`, `oauth2`, `oauth2_token_source` |
| Auth | `oauth2=ClientCredentials(client_id=…, client_secret=…)` from `paypal.core`; token from `<base_url>/v1/oauth2/token`, lazy, cached on the client |
| Servers | one: `https://api-m.sandbox.paypal.com` (default, silent). No environment enum. |
| Retries | **none** in the SDK |
| Imports | client `paypal`; models `paypal.models`; enums `paypal.models.enums`; `ApiError, RawError, OAuthProviderError, UNSET, UnsetType, HttpxClient, HttpRequest, HttpResponse, ClientCredentials` from `paypal.core` |

## Repo survey

- Host: Django (WSGI) → **sync `PaypalClient`**, one lazily-built module-level instance (built on first use, never at import; closed via `atexit`). Exemplar for app layout: `sandbox/apps/user/` (plain Django app under `sandbox/apps`, imported as `apps.<name>`); URL wiring exemplar: `sandbox/urls.py` (non-i18n `path(...)` entries at top of `urlpatterns`).
- `DATABASES.default.ATOMIC_REQUESTS = True` → views that must commit a claim before a provider call use `@transaction.non_atomic_requests` + explicit `transaction.atomic()` blocks.
- Oscar reuse: `order.Order`/`order.Line` via `OrderCreator.place_order` from a throwaway `basket.Basket`; `Order.set_status` (pipeline `Pending → Being processed → Complete`, `→ Cancelled`); `payment.SourceType`/`payment.Source` (`allocate`/`debit`/`refund` create `payment.Transaction` rows); shipping `oscar.apps.shipping.methods.Free`; `address.ShippingAddress` + `address.Country`.
- Toolchain: `venv` (py 3.11) + `pip install -e .[test]`; SDK installed into the same venv; `mypy` + `django-stubs` installed for the type gate.
- Baseline: Oscar's own pytest suite (`tests/`) needs PostgreSQL (`psycopg2 … port 5432 refused`) — not runnable here. Gate for this work: `sandbox/manage.py check`, `sandbox/manage.py test apps.paypal_payments`, `mypy` on the new app.
- Sandbox bootstrap: the host notes are wrong on one point — here the `oscar_import_catalogue sandbox/fixtures/*.csv` step is **required** (without it: 11 products and `orders.json` fails with a FK error; with it: 209 products, 203 stock records, orders.json loads).
- Credentials: `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENVIRONMENT=sandbox`, `PAYPAL_CURRENCY=USD` present in env. Read in `sandbox/settings.py` with empty defaults (import never raises); `build_client()` refuses empties.

## Smoke results (scratch dir, sandbox account)

- token + `search_transactions` (30-day window): 200, data present.
- `create_order` intent AUTHORIZE + `payment_source.card` (4111…, `prefer="return=representation"`, `pay_pal_request_id`): order `status=COMPLETED`, `purchase_units[0].payments.authorizations[0].status=CREATED`, amount echoed. **No payer challenge.**
- `vault.create_payment_token` with `payment_source.card` directly: returns `id` + `customer.id` + card `brand/last_digits/expiry`.
- `create_order` with `card.vault_id`: COMPLETED/authorization CREATED.
- `void_payment`: `status=VOIDED`. Repeated void → 422 `PREVIOUSLY_VOIDED`; capture of voided → 422 `AUTHORIZATION_VOIDED`.
- `reauthorize_payment` on a fresh auth → 422 issue `REAUTHORIZATION_TOO_SOON` ("only allowed once from Day 4 to Day 29").
- `vault.delete_payment_token` → None (success). `get_payment_token` on a deleted token → 404 whose body **fails to decode** into `Error` (`links[].rel` missing) → `pydantic.ValidationError` on an ERROR path. The boundary must classify a `ValidationError` by the HTTP status actually received (recorded by the transport wrapper), not assume "unknown".

## CONTRACT SHEET

Rules holding for every row: everything after `*` is keyword-only with a real default (never pass defensive `None`s); `request_options` last; parsed call raises `ApiError`; decode failure raises `ValidationError`/`ValueError` in both modes; `Optional[T]` = `T | UnsetType` (never pass `None`); enums are open (`…OrStr`) so every status mapping has an `unknown` default arm; money is `str` built from `Decimal` quantized to the currency exponent (USD 2; JPY/KRW 0; KWD/BHD/TND 3).

### 1. `client.orders.create_order` — `POST /v2/checkout/orders`
- Signature: `create_order(body: OrderRequest | OrderRequestDict, *, pay_pal_mock_response=None, pay_pal_request_id=None, pay_pal_partner_attribution_id=None, pay_pal_client_metadata_id=None, prefer="return=minimal", pay_pal_auth_assertion=None, request_options=None) -> Order`
- We pass: `pay_pal_request_id` = claim row's request id (header `PayPal-Request-Id`; docstring: mandatory for single-step card orders, stored 6 h → resend under same id is the lookup); `prefer="return=representation"` (default minimal omits payments).
- Body `OrderRequest`: `intent` (req, `CheckoutPaymentIntent.AUTHORIZE`), `purchase_units: list[PurchaseUnitRequest]` (req), `payment_source: PaymentSource` (opt).
  - `PurchaseUnitRequest`: `amount: AmountWithBreakdown` (req: `currency_code: str`, `value: str`); `reference_id` (opt, our order number — our reference), `custom_id` (opt, our order number — reconciliation `custom_field`), `invoice_id` (opt, `"{number}-{attempt}-{request-id[:8]}"` — our client-chosen reference, globally unique per attempt). No `items`/`breakdown` (omit → PayPal takes amount as total). No `payee`, `payment_instruction`, `soft_descriptor` (omit → account default).
  - `PaymentSource.card: CardRequest` — one-off: `name`, `number`, `expiry` (`YYYY-MM`), `security_code`, `billing_address: Address` (`address_line_1`, `address_line_2`, `admin_area_2`, `admin_area_1`, `postal_code`, `country_code` req); saved: `vault_id` only. `attributes`/`stored_credential`/`experience_context` omitted (omit → provider default).
- Response `Order`: `id`, `status: OrderStatusOrStr`, `purchase_units[0].payments.authorizations[0]` (`AuthorizationWithAdditionalData`: `id`, `status`, `amount: Money`, `expiration_time`, `create_time`), `payment_source.card` (`CardResponse`: `brand`, `last_digits`).
- Must assert: `id` set; authorization present with `id`, `amount` equal to order total (Decimal compare, same currency).
- `OrderStatus` members → our outcome: `COMPLETED` → read authorization status; `PAYER_ACTION_REQUIRED` → failed (browser challenge unsupported — reported, never round-tripped); `VOIDED` → failed; `CREATED`/`SAVED`/`APPROVED` → pending (no authorization yet; operator follow-up); other → unknown.
- `AuthorizationStatus` members: `CREATED` → done (authorized); `PENDING` → pending; `DENIED` → failed; `VOIDED` → failed; `CAPTURED`/`PARTIALLY_CAPTURED` → needs_review on a fresh create; other → unknown.
- Error union `CreateOrderErrorBody = Error | RawError` (`Error` for 400, 401, 422).

### 2. `client.payments.get_authorized_payment` — `GET /v2/payments/authorizations/{authorization_id}`
- `get_authorized_payment(authorization_id: str, *, pay_pal_mock_response=None, pay_pal_auth_assertion=None, request_options=None) -> PaymentAuthorization`
- Uses: `status`, `expiration_time`, `create_time`, `amount`. Error union `Error` [401, 403, 404] | `RawError` [500, other].

### 3. `client.payments.reauthorize_payment` — `POST …/authorizations/{authorization_id}/reauthorize`
- `reauthorize_payment(authorization_id: str, *, pay_pal_request_id=None, prefer="return=minimal", pay_pal_auth_assertion=None, body: ReauthorizeRequest | ReauthorizeRequestDict | None = None, request_options=None) -> PaymentAuthorization`
- Body `ReauthorizeRequest.amount: Money` (opt) — we send the order total explicitly. Docstring: allowed once, day 4–29 after original; new auth has a fresh 3-day honor period; past 29 days → a new authorization is required.
- Response `PaymentAuthorization`: new `id`, `status` (map as row 1), `amount`, `create_time`, `expiration_time`. Error union `Error` [400, 401, 403, 404, 422] | `RawError`. 422 `details[].issue` e.g. `REAUTHORIZATION_TOO_SOON` surfaced verbatim to the operator.

### 4. `client.payments.capture_authorized_payment` — `POST …/authorizations/{authorization_id}/capture`
- `capture_authorized_payment(authorization_id: str, *, pay_pal_mock_response=None, pay_pal_request_id=None, prefer="return=minimal", pay_pal_auth_assertion=None, body: CaptureRequest | CaptureRequestDict | None = None, request_options=None) -> CapturedPayment`
- Body `CaptureRequest`: `amount: Money` (order total), `final_capture: bool` (True). `invoice_id`, `note_to_payer`, `soft_descriptor`, `payment_instruction` omitted (omit → authorization/account default).
- Response `CapturedPayment`: `id`, `status: CaptureStatusOrStr`, `amount`, `create_time`, `seller_receivable_breakdown` (`gross_amount: Money` req, `paypal_fee`, `net_amount`).
- `CaptureStatus` members: `COMPLETED` → done; `PENDING` → pending; `DECLINED`/`FAILED` → failed; `PARTIALLY_REFUNDED`/`REFUNDED` → needs_review on a fresh capture; other → unknown.
- Error union `Error` [400, 401, 403, 404, 409, 422] | `RawError` [500, other].

### 5. `client.payments.void_payment` — `POST …/authorizations/{authorization_id}/void`
- `void_payment(authorization_id: str, *, pay_pal_mock_response=None, pay_pal_auth_assertion=None, pay_pal_request_id=None, prefer="return=minimal", request_options=None) -> PaymentAuthorization`
- `prefer="return=representation"`; outcome from `status`: `VOIDED` → done; other → unknown. 422 issue `PREVIOUSLY_VOIDED` = already landed → done.
- Error union `Error` [401, 403, 404, 409, 422] | `RawError`.

### 6. `client.payments.refund_captured_payment` — `POST /v2/payments/captures/{capture_id}/refund`
- `refund_captured_payment(capture_id: str, *, pay_pal_mock_response=None, pay_pal_request_id=None, prefer="return=minimal", pay_pal_auth_assertion=None, body: RefundRequest | RefundRequestDict | None = None, request_options=None) -> Refund`
- `pay_pal_request_id` = claim row's request id (stored 45 days → resend under same id is the lookup). Body `RefundRequest.amount: Money` always sent (explicit, even for "full"). `custom_id`/`invoice_id`/`note_to_payer`/`payment_instruction` omitted.
- Response `Refund`: `id`, `status: RefundStatusOrStr`, `amount`, `create_time`, `seller_payable_breakdown`.
- `RefundStatus` members: `COMPLETED` → done; `PENDING` → pending; `CANCELLED`/`FAILED` → failed; other → unknown.
- Error union `Error` [400, 401, 403, 404, 409, 422] | `RawError`.

### 7. `client.vault.create_payment_token` — `POST /v3/vault/payment-tokens`
- `create_payment_token(body: PaymentTokenRequest | PaymentTokenRequestDict, *, pay_pal_request_id=None, request_options=None) -> PaymentTokenResponse`
- Body: `payment_source: PaymentTokenRequestPaymentSource` (req) → `card: PaymentTokenRequestCard` (`name`, `number`, `expiry`, `security_code`, `billing_address`); `customer: Customer` (opt; `id` = PayPal-generated customer id, sent once we have one for the user; omit on first save → PayPal creates one).
- Response `PaymentTokenResponse`: `id`, `customer.id`, `payment_source.card` (`CardPaymentTokenEntity`: `brand`, `last_digits`, `expiry`, `name`). No status member. Must assert `id`.
- Error union `Error` [400, 403, 404, 422, 500] | `RawError`.

### 8. `client.vault.delete_payment_token` — `DELETE /v3/vault/payment-tokens/{id}` — **returns `None`**
- `delete_payment_token(id: str, *, request_options=None) -> None`. Success = no exception. 404 → already gone → treat as deleted. Error union `Error` [400, 403, 500] | `RawError`.

### 9. `client.transaction_search.search_transactions` — `GET /v1/reporting/transactions`
- `search_transactions(start_date: str, end_date: str, *, transaction_id=None, transaction_type=None, transaction_status=None, transaction_amount=None, transaction_currency=None, payment_instrument_type=None, store_id=None, terminal_id=None, fields="transaction_info", balance_affecting_records_only="Y", page_size=100, page=1, request_options=None) -> SearchResponse`
- Dates RFC 3339 with seconds (`%Y-%m-%dT%H:%M:%SZ`); **max range 31 days** → split into ≤31-day windows; up to 3 h reporting lag. We pass `balance_affecting_records_only="N"` (include authorizations/voids so every local record can match), `page`, `page_size=500` (max undocumented; 500 verified accepted on the sandbox). Pages iterate 1..`total_pages`. Live: the shared sandbox returns ~7k records / 31 days and an occasional transient 503 → read-only retry with backoff (2 retries).
- Response `SearchResponse`: `transaction_details[].transaction_info` (`transaction_id`, `paypal_reference_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_amount`, `fee_amount`, `transaction_status`, `invoice_id`, `custom_field`), `total_pages`, `page`, `last_refreshed_datetime`.
- Error: **Case B — always `RawError`**.

### Not used
`orders.authorize_order` (single-step create authorizes directly), `orders.get_order`, setup tokens, `list_customer_payment_tokens` (our DB is the list of the caller's cards; PayPal is consulted on save/delete/pay).

## CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| a `vault_id` sent on pay must be a token id returned by `create_payment_token` for **this** user and not deleted | `create_order` ← `create_payment_token` / `delete_payment_token` | `SavedCard` lookup by (public id, user, active) |
| `customer.id` sent on a later save must be one returned for this user | `create_payment_token` ← `create_payment_token` | `PayPalCustomer` (one per user) |
| the authorization captured/voided/reauthorized must be the one returned for this order (latest after reauth) | `capture/void/reauthorize` ← `create_order`/`reauthorize_payment` | `OrderPayment.authorization_id` |
| refund `capture_id` must be the capture returned at fulfil; Σ refunds ≤ captured amount | `refund_captured_payment` ← `capture_authorized_payment` | `OrderPayment.capture_id`; conditional (CAS) reservation of `refund_reserved` |
| captured amount == authorized amount == order total | `capture` ← `create_order` | echoed-amount checks → `needs_review` |

## Design

- App `apps.paypal_payments` (label `paypal_payments`): models `PayPalCustomer`, `SavedCard`, `OrderPayment` (1:1 Oscar `Order`), `PaymentOperation` (durable claim, UNIQUE `key`, own `request_id` sent as `PayPal-Request-Id`, status `sending/done/pending/failed/unknown/needs_review`), `PayPalRefund`.
- Claims: authorize `authorize:{order}:{attempt}`; reauthorize `reauthorize:{order}:{auth_id}`; capture `capture:{order}:{auth_id}`; void `void:{order}:{auth_id}`; refund `refund:{order}:{Idempotency-Key}` (fingerprint = amount; same key + different amount → 422); save card `vault:{user}:{Idempotency-Key or uuid}`. State transitions are single conditional UPDATEs (`filter(state__in=…).update(...)`); the loser answers from the existing row (200 same result / 409 in progress), never calls PayPal.
- Lookup after a may-have-landed write (5xx, read timeout, unreadable 2xx): resend under the **same** `PayPal-Request-Id` once (orders 6 h, refunds 45 days, per docstrings). Still unreadable → row `unknown`, answer 504 with the reference.
- Staleness: honor period 3 days (from `CheckoutPaymentIntent.AUTHORIZE` docstring). At fulfil: GET auth; `VOIDED`/`DENIED` → 409; past `expiration_time` → state `expired` (re-payable) + 409 "authorization expired, shopper must pay again"; older than 3 days → reauthorize first; reauth refused → still attempt capture; capture refused → 409 with PayPal's issue + description + concrete operator options.
- Error boundary (`gateway.py`): `OAuthProviderError` → 502 config; 401/403 → 502; 429 → 503; 400/404/409/422 with `Error` → caller-facing 4xx (422/404/409) carrying `name`, `details[].issue/description`, `debug_id`; 5xx/unmapped → 502 `outcome_unknown` for writes; `ValidationError` → classified by the recorded HTTP status (≥400 → rejected, 2xx → unknown); `ConnectError/ConnectTimeout/PoolTimeout/ProxyError` → 502 never sent; other `httpx.RequestError` → 504 unknown.
- Transport: `RecordingTransport(HttpxClient(timeout=…))` logs method, path, status, ms — never headers/bodies — and records the last status per thread. Client timeout 20 s.
- Base URL: `PAYPAL_BASE_URL` if set (verbatim, token call included since the SDK derives it); else `PAYPAL_ENVIRONMENT=sandbox` → `https://api-m.sandbox.paypal.com` (map); any other environment without `PAYPAL_BASE_URL` → `ImproperlyConfigured` (the plugin documents no other host).
- Currency: `PAYPAL_CURRENCY`; Oscar order `currency` set to it; amounts from Oscar strategy prices (sandbox strategy has no tax).
- Card data: only passed through to PayPal; never persisted or logged; stored card = PayPal token id + brand/last4/expiry/name.
- Reconciliation: window [from, to); provider side from windows ≤31 d, page loop bounded (MAX_PAGES) with `truncated` flag; local side = our provider records (authorization, capture, refunds) filtered by their **provider** timestamps; matching by PayPal transaction id against the set per order; output `matched`, `providerOnly`, `localOnly`, `unsettled`, `truncated`, `lastRefreshed`.

## Assumptions & Blockers

- No blocker. Minor: non-sandbox environments require `PAYPAL_BASE_URL` (no live host in the plugin). Refunds are shopper-scoped per the task text (owner of the order), not staff.
- `page_size` max undocumented → 500 used (accepted live).
- Live finding: the merchant account rejects a reused `invoice_id` ("Duplicate Invoice ID detected"); Oscar order numbers repeat across installations, so `invoice_id` = `{order}-{attempt}-{request-id prefix}` (unique, and identical on a same-request-id resend).

## REQUIRED READING

- MUST load `python-error-handling` — error union narrowing, non-ApiError failures (loaded).
- MUST load `python-client-initialization` — sync client, lifetime, transport override (loaded).
- MUST load `python-configuration-resilience` — claims, lookup-after-write, echoed amount, reconciliation, pagination (loaded).
- MUST load `python-calling-endpoints` — status→outcome mapping, `None` returns (loaded).
- MUST load `python-models` — `UNSET`, open enums, money formatting (loaded).
- MUST load `python-authentication` — credentials from settings, OAuthProviderError (loaded).
- MUST load `python-testing` — stub transport seam for the app's tests (loaded).
