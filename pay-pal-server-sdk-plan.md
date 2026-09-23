# PayPal integration plan — django-oscar sandbox (`sandbox/apps/paypal_payments`)

## SDK identity (verified against the installed package)

| Fact | Value | Source |
|---|---|---|
| Distribution / import root | `paypal` / `paypal` (NOT `pay-pal-server-sdk` / `pay_pal_server_sdk` as the getting-started skill says — drift, the SDK map and `pyproject.toml` at commit `0ed3d220` both say `paypal`) | `sdk-map.md`, `pyproject.toml` |
| Version | 2.29 | `pip show paypal` |
| Install | non-editable `pip install <clone of github.com/context-plugins/paypal-python-sdk@main>` into `venv/` (the git+https install failed on a flaky partial clone; the local clone is complete) | — |
| Client | `paypal.PaypalClient` (sync). Keyword-only: `base_url`, `timeout`, `custom_http_client`, `oauth2`, `oauth2_token_source` | `sdk-map.md` Getting a client |
| Auth | `oauth2=ClientCredentials(client_id=…, client_secret=…)` from `paypal.core`; token from `<base_url>/v1/oauth2/token`, lazy, cached per client | `sdk-map.md` Servers & auth |
| Host | only server declared: `https://api-m.sandbox.paypal.com`; override with `base_url=`. The SDK declares NO live host → `PAYPAL_ENVIRONMENT` other than `sandbox` requires `PAYPAL_BASE_URL` (fail fast, ImproperlyConfigured) | `sdk-map.md` |
| Core imports | `ApiError, RawError, OAuthProviderError, ClientCredentials, HttpxClient, HttpRequest, HttpResponse, OAuthToken, UNSET, UnsetType` from `paypal.core` | `paypal/core/__init__.py` `__all__` |
| Error body model | `paypal.models.Error` (`name: str`, `message: str`, `debug_id: str` required; `details: Optional[list[ErrorDetails]]`); `ErrorDetails.issue: str` required, `description: Optional[str]` | `models/error.py`, `models/error_details.py` |

## Decisions

- **Sync client** — Django under WSGI. One lazily-built module-level `PaypalClient` (built after fork, on first use, under a lock), closed via `atexit`. Never per request.
- **Transport**: `HttpxClient(timeout=settings.PAYPAL_TIMEOUT_SECONDS)` wrapped in a logging transport (method, path, status, `paypal-debug-id` header; never headers/bodies).
- **Response mode**: parsed calls everywhere (raise `ApiError`), except `vault.delete_payment_token` (returns `None`) — parsed is fine; success = no exception.
- **`prefer="return=representation"`** on every write that has `prefer` (default `return=minimal` omits fee/breakdown and `void_payment` documents its body only for representation).
- **Retries**: SDK does none. Gateway retries once on transport error / 429 / 5xx ONLY for calls that are reads or carry a deterministic `pay_pal_request_id` (so a resend is deduplicated by PayPal); vault delete is naturally idempotent. Nothing else retried.
- **Idempotency**: deterministic `PayPal-Request-Id` per logical action (`<payment uuid>-<action>-<attempt|auth id|sha256(key)>`) + local DB lease (`lock_until`) + unique `(payment, idempotency_key)` for refunds.
- **Staleness at fulfil** (docstring of `reauthorize_payment`: 3-day honor period, reauthorize once from day 4 to 29, after that a new authorization is required): refresh the authorization; if past `expiration_time` → not renewable, order back to "Awaiting payment", 409 with operator guidance; if past honor period and not reauthorized → `reauthorize_payment` then capture the new authorization; if reauth refused but auth still unexpired → capture original; a capture 422 on a stale auth triggers one reauth+capture attempt.
- **Card direct**: single-step `create_order` with `payment_source.card` (or `card.vault_id`), `intent=AUTHORIZE`. Smoke showed status `COMPLETED` with the authorization inside `purchase_units[0].payments.authorizations[0]`; `APPROVED` → call `authorize_order`; `PAYER_ACTION_REQUIRED` → reject (browser approval unsupported by design).
- **Saved cards**: `vault.create_payment_token` with `payment_source.card` directly (smoke-verified); local record is Oscar's `payment.Bankcard` (`number` masked `XXXX-XXXX-XXXX-<last4>`, `partner_reference` = vault token id). Per-user PayPal vault customer id stored in `PayPalCustomer` and sent as `customer.id` on later saves.
- **Oscar reuse**: orders via `OrderCreator.place_order` from a per-request `Basket` (strategy via `Selector`, offers via `Applicator`, default shipping repository); payment ledger via `payment.Source` / `SourceType("PayPal")` / `Transaction` (`allocate`/`debit`/`refund`). PayPal-owned state in `PayPalPayment` / `PayPalRefund`.
- **Order statuses**: add `Awaiting payment`, `Payment authorised` to the sandbox pipeline; `Complete`, `Cancelled` existing.
- **Refunds authz**: order owner or `is_staff` (task lists refunds as an operator follow-up but only fulfil/cancel/reconciliation as staff-only).
- **Reconciliation**: split `[from,to]` into ≤31-day windows (docstring: "maximum supported range is 31 days"), page through `total_pages`, match `transaction_id` against capture/refund ids, then `invoice_id` / `custom_field`.

## Contract sheet (every row: map page + model module)

`*` = keyword-only boundary. Every keyword-only param has a real default; do not pass defensive `None`s. All `Optional[T]` members are `T | UnsetType` — never pass `None`.

| Operation | Signature (sync, parsed) | Returns | `ApiError.error` union | Must assert after call |
|---|---|---|---|---|
| `client.orders.create_order` | `(body: OrderRequest\|OrderRequestDict, *, pay_pal_request_id=None, prefer="return=minimal", …)` — map orders.md | `Order` | `CreateOrderErrorBody` = `Error` [400,401,422] \| `RawError` | `id`, `status` not UNSET |
| `client.orders.authorize_order` | `(id: str, *, pay_pal_request_id=None, prefer=…, body=None, …)` | `OrderAuthorizeResponse` | `Error` [400,401,403,404,422,500] \| `RawError` | authorization id |
| `client.payments.get_authorized_payment` | `(authorization_id: str, *, …)` | `PaymentAuthorization` | `Error` [401,403,404] \| `RawError` [500,…] | `status` |
| `client.payments.reauthorize_payment` | `(authorization_id: str, *, pay_pal_request_id=None, prefer=…, body: ReauthorizeRequest\|Dict\|None=None, …)` | `PaymentAuthorization` | `Error` [400,401,403,404,422] \| `RawError` | `id` |
| `client.payments.capture_authorized_payment` | `(authorization_id: str, *, pay_pal_request_id=None, prefer=…, body: CaptureRequest\|Dict\|None=None, …)` | `CapturedPayment` | `Error` [400,401,403,404,409,422] \| `RawError` | `id`, `status` |
| `client.payments.void_payment` | `(authorization_id: str, *, pay_pal_request_id=None, prefer=…, …)` | `PaymentAuthorization` | `Error` [401,403,404,409,422] \| `RawError` | — |
| `client.payments.refund_captured_payment` | `(capture_id: str, *, pay_pal_request_id=None, prefer=…, body: RefundRequest\|Dict\|None=None, …)` | `Refund` | `Error` [400,401,403,404,409,422] \| `RawError` | `id`, `status` |
| `client.vault.create_payment_token` | `(body: PaymentTokenRequest\|Dict, *, pay_pal_request_id=None, …)` | `PaymentTokenResponse` | `Error` [400,403,404,422,500] \| `RawError` | `id` |
| `client.vault.delete_payment_token` | `(id: str, *, …)` | `None` (one of the 11) | `Error` [400,403,500] \| `RawError` | — |
| `client.transaction_search.search_transactions` | `(start_date: str, end_date: str, *, fields="transaction_info", balance_affecting_records_only="Y", page_size=100, page=1, …)` | `SearchResponse` | Case B: always `RawError` | — |

Failures that are not the operation union: failed token fetch → `ApiError` whose `.error` is `OAuthProviderError | RawError` (check first); decode failure → `pydantic.ValidationError`/`ValueError` in both modes (outcome unknown on 2xx); transport → `httpx.HTTPError` unwrapped (outcome unknown).

### Request models (members this integration sets)

| Model (module) | Members used |
|---|---|
| `OrderRequest` (`models/order_request.py`) | `intent: CheckoutPaymentIntentOrStr` **required** (`AUTHORIZE`); `purchase_units: list[PurchaseUnitRequest]` **required**; `payment_source: Optional[PaymentSource]` |
| `PurchaseUnitRequest` | `amount: AmountWithBreakdown` **required**; `reference_id`, `custom_id`, `invoice_id`, `description`: `Optional[str]` |
| `AmountWithBreakdown` / `Money` | `currency_code: str`, `value: str` both **required** (money as string, `Decimal` formatted `:.2f`) |
| `PaymentSource` | `card: Optional[CardRequest]` |
| `CardRequest` | `name`, `number`, `expiry` (`YYYY-MM`), `security_code`, `vault_id`: `Optional[str]`; `billing_address: Optional[Address]` |
| `Address` | `address_line_1`, `address_line_2`, `admin_area_2`, `admin_area_1`, `postal_code`: `Optional[str]`; `country_code: str` **required** |
| `CaptureRequest` | `amount: Optional[Money]`, `invoice_id: Optional[str]`, `final_capture: Optional[bool]` |
| `ReauthorizeRequest` | `amount` only (docstring: "Supports only the amount request parameter") |
| `RefundRequest` | `amount: Optional[Money]`, `note_to_payer: Optional[str]` |
| `PaymentTokenRequest` | `customer: Optional[Customer]` (`id: Optional[str]`, `merchant_customer_id: Optional[str]`); `payment_source: PaymentTokenRequestPaymentSource` **required** → `card: Optional[PaymentTokenRequestCard]` (`name`,`number`,`expiry`,`security_code`,`billing_address`) |

No wire aliases on any member set above (`grep alias=` empty); `CardResponse.type_`↔`type` only on responses (unused).

### Response members read

- `Order.id`, `.status: OrderStatusOrStr` (CREATED, SAVED, APPROVED, VOIDED, COMPLETED, PAYER_ACTION_REQUIRED), `.purchase_units[0].payments.authorizations[0]` (`AuthorizationWithAdditionalData`: `id`, `status`, `amount: Money`, `expiration_time: str`, `create_time: str`), `.payment_source.card` (`CardResponse`: `last_digits`, `brand`). Same shape on `OrderAuthorizeResponse`.
- `PaymentAuthorization`: `id`, `status: AuthorizationStatusOrStr` (CREATED, CAPTURED, DENIED, PARTIALLY_CAPTURED, VOIDED, PENDING — open enum, unknown values arrive as `str`), `amount`, `expiration_time`, `create_time`.
- `CapturedPayment`: `id`, `status: CaptureStatusOrStr` (COMPLETED, DECLINED, PARTIALLY_REFUNDED, PENDING, REFUNDED, FAILED), `amount`, `seller_receivable_breakdown` (`gross_amount: Money` required, `paypal_fee`, `net_amount`: Optional).
- `Refund`: `id`, `status: RefundStatusOrStr` (CANCELLED, FAILED, PENDING, COMPLETED), `amount`.
- `PaymentTokenResponse`: `id`, `customer.id` (`CustomerResponse`), `payment_source.card` (`CardPaymentTokenEntity`: `name`, `last_digits`, `brand`, `expiry`).
- `SearchResponse`: `transaction_details: list[TransactionDetails]` → `.transaction_info` (`TransactionInformation`: `transaction_id`, `paypal_reference_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_amount`, `fee_amount`, `transaction_status`, `invoice_id`, `custom_field`); `page`, `total_pages`, `total_items`, `last_refreshed_datetime`.

### Observed on sandbox (scratch smoke, 2026-09-24)

token OK; search_transactions OK (has data); card order AUTHORIZE → `COMPLETED` + authorization `CREATED`; vault create/get/delete(204) OK; vault_id order OK; capture (representation) gives fee+net; same `PayPal-Request-Id` on refund returns the same refund; over-refund → 422 `REFUND_AMOUNT_EXCEEDED`; reauth inside honor period → 422 `REAUTHORIZATION_TOO_SOON`; capture of voided → 422 `AUTHORIZATION_VOIDED`. No 3-D Secure challenge encountered.

## Assumptions & Blockers

- No blockers. Minor: catalogue prices are GBP stock records; amounts are charged numerically in `PAYPAL_CURRENCY` as the task directs ("currency comes from configuration").
- Minor: live host is not declared by the SDK; production requires `PAYPAL_BASE_URL`.

## Build order

1. settings (PAYPAL_* + app + statuses) → 2. models + migration → 3. gateway (SDK boundary) → 4. services → 5. views/urls → 6. tests (stub transport) → 7. mypy on gateway → 8. live end-to-end run on the sandbox server.

## REQUIRED READING (loaded)

- MUST load python-client-initialization — sync client, lifetime, transport ownership. ✅
- MUST load python-authentication — OAuthProviderError surfaces from operation calls. ✅
- MUST load python-calling-endpoints — keyword-only tail, `prefer` default narrows responses. ✅
- MUST load python-models — `Optional` ≠ `typing.Optional`, UNSET leaks, open enums. ✅
- MUST load python-error-handling — the failure ladder. ✅
- MUST load python-configuration-resilience — no retries, timeout, logging transport. ✅
- MUST load python-testing — stub transport + token response first. ✅
