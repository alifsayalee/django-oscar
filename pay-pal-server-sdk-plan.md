# PayPal payments & saved cards for the django-oscar sandbox — plan and contract sheet

SDK: `pay-pal-server-sdk` 2.29 (import root `pay_pal_server_sdk`), installed into `venv/` from the
plugin copy (`pip install <plugin>/sdk/python/`, not editable). Every fact below comes from the SDK map
(`sdk-map.md`, `map/operations/*.md`), the model/enum modules it names, the operation docstrings, or a
sandbox smoke run (marked **SMOKE**). Decisions are marked **YOUR CALL**.

## Repo survey

| Convention | Exemplar |
| --- | --- |
| Sandbox-local apps live in `sandbox/apps/<name>/`, imported as `apps.<name>` (`sandbox/` is on `sys.path`) | `sandbox/apps/sitemaps.py`, `sandbox/urls.py` (`from apps.sitemaps import ...`) |
| Non-i18n URLs go in the plain `urlpatterns` list of `sandbox/urls.py` | `sandbox/urls.py` (`admin/`, `i18n/`) |
| Settings read through `environ.Env()` | `sandbox/settings.py` |
| `ATOMIC_REQUESTS = True` — every view is one transaction unless opted out | `sandbox/settings.py` |
| Order status transitions go through `order.set_status()` validated by `OSCAR_ORDER_STATUS_PIPELINE` | `src/oscar/apps/order/abstract_models.py` |
| Orders are placed via `OrderCreator.place_order(basket, total, shipping_method, shipping_charge, ...)` | `src/oscar/apps/checkout/mixins.py` |
| Payment amounts via `payment.Source` / `payment.Transaction` / `payment.SourceType`; saved cards via `payment.Bankcard` (masked number + `partner_reference`) | `src/oscar/apps/payment/abstract_models.py` |
| Stock allocation consumed/cancelled via `order.processing.EventHandler` | `src/oscar/apps/order/processing.py` |

Host is Django under WSGI (sync) → **sync `PayPalServerSdkClient`**.
Toolchain: `py -3.11 -m venv venv`, `venv\Scripts\pip install -e .[test]`; tests with Django's runner from
`sandbox/` (`python manage.py test apps.paypal_payments`); type check: `mypy --strict` on the gateway module
(the only module importing the SDK). Baseline: sandbox builds from scratch (see note below), `manage.py check` clean.

Bootstrap finding: the `oscar_import_catalogue` CSV step is **required** — without it only 11 products load and
`offers.json` / `orders.json` fail with FK errors. With it: 209 products, 249 countries, 2 users.

## Architecture (YOUR CALL)

New app `sandbox/apps/paypal_payments/` (label `paypal_payments`), routed at `/api/` from `sandbox/urls.py`.

- `gateway.py` — the only SDK importer. Holds the lazily-built, process-wide client (built after fork, on
  first use, lock-guarded), translates every SDK outcome into plain dataclasses or a `PayPalError`.
- `services.py` — domain logic (Oscar models, DB transactions, idempotency, state machine).
- `views.py` / `urls.py` — JSON HTTP API, session auth, staff gating.
- `models.py` — PayPal state that Oscar has no field for (ids/statuses/fees/refund keys/vault customer id).
  Everything else reuses Oscar: `order.Order/Line/ShippingAddress`, `basket.Basket`, `payment.Source/
  SourceType/Transaction`, `payment.Bankcard` (saved cards).
- `strategy.py` — Oscar pricing strategy: catalogue price, currency from `PAYPAL_CURRENCY`.

Order statuses added to the sandbox pipeline: `Pending payment` → `Authorised` / `Cancelled`;
`Authorised` → `Complete` / `Cancelled` / `Pending payment` (only when an authorization can no longer be
renewed). Existing statuses unchanged.

Idempotency: every PayPal write carries `PayPal-Request-Id`. The key is persisted (committed) **before** the
call, so a double-click or a retry after an unknown outcome replays the same key and PayPal returns the
same result. Keys are cleared only on a definitive rejection. Refund keys are derived deterministically
from (payment, caller key) via `uuid5`. Views run `non_atomic_requests` so the key survives a failed call.

## Contract sheet

### Client (sync)

| Row | Fact | Source |
| --- | --- | --- |
| Class | `PayPalServerSdkClient` from `pay_pal_server_sdk`; keyword-only ctor | sdk-map.md · Getting a client |
| Credentials | `oauth2=ClientCredentials(client_id=..., client_secret=...)` (`pay_pal_server_sdk.core`). Omitting it = unauthenticated, silently → gateway refuses to build without both | sdk-map.md · Servers & auth |
| Base URL | one keyword `base_url`; SDK default `https://api-m.sandbox.paypal.com`; token endpoint `/v1/oauth2/token` follows `base_url` | sdk-map.md · Servers & auth |
| Host selection | `PAYPAL_BASE_URL` set → used verbatim. Else `PAYPAL_ENVIRONMENT == "sandbox"` → `https://api-m.sandbox.paypal.com` (the only host the plugin declares). Any other environment without `PAYPAL_BASE_URL` → `ImproperlyConfigured` (the plugin declares no other host; never guessed). Always passed explicitly | YOUR CALL on sdk-map.md |
| Timeout | `timeout=20.0` (single float, per attempt) | sdk-map.md ctor table |
| Retries | keep default policy (retries GET/HEAD/PUT/OPTIONS on 408/429/5xx/transport; never POST/DELETE) — reads (get auth, search) are retried; writes are not re-sent by the SDK; app-level replay uses the persisted `PayPal-Request-Id` | python-configuration-resilience |
| Lifetime | module-level lazy singleton, closed via `atexit` | python-client-initialization |

### Operations (positional before `*`; every keyword-only param has a real default — pass only what's needed)

| Operation | Signature (used parts) | Returns | Error union |
| --- | --- | --- | --- |
| `orders.create_order` | `(body: OrderRequest, *, pay_pal_request_id=..., prefer="return=representation")` | `Order` | `CreateOrderErrorBody` = `Error` [400,401,422] \| `RawError` |
| `payments.get_authorized_payment` | `(authorization_id, *)` | `PaymentAuthorization` | `Error` [401,403,404] \| `RawError` |
| `payments.reauthorize_payment` | `(authorization_id, *, pay_pal_request_id, prefer="return=representation", body: ReauthorizeRequest \| None)` | `PaymentAuthorization` | `Error` [400,401,403,404,422] \| `RawError` |
| `payments.capture_authorized_payment` | `(authorization_id, *, pay_pal_request_id, prefer="return=representation", body=CaptureRequest(amount=Money, final_capture=True))` | `CapturedPayment` | `Error` [400,401,403,404,409,422] \| `RawError` |
| `payments.void_payment` | `(authorization_id, *, pay_pal_request_id, prefer="return=representation")` | `PaymentAuthorization` | `Error` [401,403,404,409,422] \| `RawError` |
| `payments.refund_captured_payment` | `(capture_id, *, pay_pal_request_id, prefer="return=representation", body=RefundRequest(amount=Money))` | `Refund` | `Error` [400,401,403,404,409,422] \| `RawError` |
| `vault.create_payment_token` | `(body: PaymentTokenRequest, *, pay_pal_request_id)` | `PaymentTokenResponse` | `Error` [400,403,404,422,500] \| `RawError` |
| `vault.delete_payment_token` | `(id_, *)` | **`None`** — success is "no exception" | `Error` [400,403,500] \| `RawError` (404 → `RawError`) |
| `transaction_search.search_transactions` | `(start_date: str, end_date: str, *, fields="transaction_info", page_size: int, page: int)` | `SearchResponse` | **Case B**: always `RawError` |

`prefer` defaults to `return=minimal` on every write above → pass `"return=representation"` or the response
lacks amounts/breakdowns. Failed token fetch raises `ApiError` with `.error` `OAuthProviderError | RawError`
(`pay_pal_server_sdk.core`), in both modes — checked first. Decode failure → `pydantic.ValidationError` /
`ValueError` (not `ApiError`). Transport failures → raw `httpx` exceptions.
`Error` (`pay_pal_server_sdk.models`): `name: str`, `message: str`, `debug_id: str` required;
`details: Optional[list[ErrorDetails]]`; `ErrorDetails.issue: str` required, `description: Optional[str]`.

### Request models (Python name = wire name unless noted; `Optional[T]` = `T | UnsetType`, never `None`)

| Model | Members used | Required |
| --- | --- | --- |
| `OrderRequest` | `intent: CheckoutPaymentIntentOrStr`, `purchase_units: list[PurchaseUnitRequest]`, `payment_source: Optional[PaymentSource]` | intent, purchase_units |
| `PurchaseUnitRequest` | `amount: AmountWithBreakdown`, `reference_id`, `custom_id`, `invoice_id`, `description` (Optional[str]) | amount |
| `AmountWithBreakdown` / `Money` | `currency_code: str`, `value: str` | both |
| `PaymentSource` | `card: Optional[CardRequest]` | — |
| `CardRequest` | `name`, `number`, `expiry` (`YYYY-MM`), `security_code`, `billing_address: Optional[Address]`, `vault_id` | none |
| `Address` | `address_line_1`, `address_line_2`, `admin_area_2`, `admin_area_1`, `postal_code` (Optional[str]), `country_code: str` | country_code |
| `CaptureRequest` | `amount: Optional[Money]`, `final_capture: bool = False`, `invoice_id` | — |
| `ReauthorizeRequest` | `amount: Optional[Money]` (only supported member) | — |
| `RefundRequest` | `amount: Optional[Money]` (omitted = full refund), `note_to_payer` | — |
| `PaymentTokenRequest` | `customer: Optional[Customer]`, `payment_source: PaymentTokenRequestPaymentSource` | payment_source |
| `PaymentTokenRequestPaymentSource` | `card: Optional[PaymentTokenRequestCard]` | — |
| `PaymentTokenRequestCard` | `name`, `number`, `expiry`, `security_code`, `billing_address: Optional[Address]` | none |
| `Customer` | `id`, `merchant_customer_id` (Optional[str]) | — |

Enum (`pay_pal_server_sdk.models.enums`, all open `…OrStr`): `CheckoutPaymentIntent.AUTHORIZE`;
`OrderStatus` CREATED/SAVED/APPROVED/VOIDED/COMPLETED/PAYER_ACTION_REQUIRED;
`AuthorizationStatus` CREATED/CAPTURED/DENIED/PARTIALLY_CAPTURED/VOIDED/PENDING;
`CaptureStatus` COMPLETED/DECLINED/PARTIALLY_REFUNDED/PENDING/REFUNDED/FAILED;
`RefundStatus` CANCELLED/FAILED/PENDING/COMPLETED. Unknown values arrive as `str` → handled by an explicit
fallback arm.

### Response members the code asserts on (all `Optional` → `UNSET` if absent → treated as outcome-unknown)

| Call | Members |
| --- | --- |
| `create_order` | `Order.id`, `.status`, `.purchase_units[0].payments.authorizations[0]` → `.id`, `.status`, `.amount.{currency_code,value}`, `.expiration_time`, `.create_time`; `payment_source.card.{brand,last_digits}` |
| `get_authorized_payment` / `reauthorize_payment` | `PaymentAuthorization.id`, `.status`, `.expiration_time`, `.create_time`, `.amount` |
| `capture_authorized_payment` | `CapturedPayment.id`, `.status`, `.amount`, `.seller_receivable_breakdown.{gross_amount,paypal_fee,net_amount}` (fee/net may be absent on PENDING) |
| `refund_captured_payment` | `Refund.id`, `.status`, `.amount` |
| `create_payment_token` | `PaymentTokenResponse.id`, `.customer.id`, `.payment_source.card.{brand,last_digits,expiry}` |
| `search_transactions` | `SearchResponse.transaction_details[].transaction_info.{transaction_id, transaction_event_code, transaction_initiation_date, transaction_amount, fee_amount, transaction_status, invoice_id, custom_field, paypal_reference_id}`, `.total_pages`, `.page`, `.last_refreshed_datetime` |

### Semantics (docstrings + SMOKE)

- **SMOKE** single-step `create_order(intent=AUTHORIZE, payment_source.card=…)` with `PayPal-Request-Id` →
  `status=COMPLETED`, authorization `CREATED`, `expiration_time` = create + 29 days. No separate
  `authorize_order` call needed. (Docstring: request id is mandatory for single-step create with a card.)
  `PAYER_ACTION_REQUIRED` = a browser challenge → rejected as unsupported (task: stop & report).
- Same with `card.vault_id=<token id>` → authorizes a saved card (**SMOKE**).
- Reauthorize: allowed after the 3-day honor period, within 29 days of the original authorization; a
  fresh one answers 422 `REAUTHORIZATION_TOO_SOON` (**SMOKE**). Returns a new authorization.
- Capture `return=representation` carries `seller_receivable_breakdown` gross/fee/net (**SMOKE**).
- Over-refund → 422 `REFUND_AMOUNT_EXCEEDED` (**SMOKE**); void after capture → 422 `PREVIOUSLY_CAPTURED`.
- Vault: `create_payment_token` with raw card works directly (**SMOKE**), PayPal assigns `customer.id`
  when only `merchant_customer_id` is sent; pass `customer.id` on later saves for the same shopper.
  `list_customer_payment_tokens` returned **no tokens** right after creation (**SMOKE**) → the app's own
  `Bankcard` rows are the source of truth for "my saved cards"; not used.
- Transaction search: RFC 3339 with seconds; **max range 31 days** → split; up to 3 h reporting lag; pages
  via `page`/`total_pages`; capture/refund ids appear as `transaction_id`; our `custom_id`/`invoice_id`
  appear as `custom_field`/`invoice_id` (**SMOKE**).

### Error translation (gateway boundary → `PayPalError(http_status, code, message, outcome_unknown, issue, debug_id)`)

| Input | Result |
| --- | --- |
| `ApiError` with `OAuthProviderError` or status 401/403 | 502 `paypal_auth_failed` (our credentials — never caller's fault) |
| `ApiError` 429 | 503 |
| `ApiError` 4xx with `Error` | 4xx→ 422/409/404 with first `details[].issue` + description |
| `ApiError` 5xx / other | 502, `outcome_unknown=True` |
| `ValidationError`/`ValueError` on decode | 502, `outcome_unknown=True` |
| `httpx.ConnectError/ConnectTimeout/PoolTimeout/ProxyError` | 502, `outcome_unknown=False` (never sent) |
| other `httpx.RequestError` | 504, `outcome_unknown=True` |
| required response member `UNSET` | 502, `outcome_unknown=True` |

## Assumptions & blockers

- None blocking. Minor: the catalogue is priced in GBP; per the task the charge currency comes from
  `PAYPAL_CURRENCY`, so API orders are priced with the catalogue amount in the configured currency (a
  sandbox strategy), and the Oscar order records that currency.
- Production host is not declared by the plugin → non-sandbox environments require `PAYPAL_BASE_URL`.

## Implementation status (2026-09-29)

- Built as planned: `sandbox/apps/paypal_payments/` (`gateway.py` is the only SDK importer; `mypy --strict` clean).
- 45 unit tests (fake transport + stub token source) pass; live sandbox E2E passed every flow: card auth,
  exact-total hold, idempotent re-pay, capture with fee/net, idempotent fulfil, partial/replayed/over refunds,
  vault save → reuse on a second order → delete, void on cancel, ownership/staff gating, reconciliation over
  all pages (range split into 31-day windows).
- The sandbox root logger is DEBUG: `httpx`/`httpcore` loggers capped at WARNING so PayPal traffic headers
  are not logged.
- Not live-verifiable: the honor-period reauthorization path (requires a 4+-day-old authorization; PayPal
  answers `REAUTHORIZATION_TOO_SOON` earlier) — covered by unit tests only.

## REQUIRED READING (loaded before implementation)

- MUST load `python-error-handling` — error boundary, unknown vs never-sent transport failures. (loaded)
- MUST load `python-client-initialization` — sync client, lazy post-fork singleton. (loaded)
- MUST load `python-calling-endpoints` — `prefer` default narrows responses; `delete_payment_token` returns None. (loaded)
- MUST load `python-models` — `UNSET` vs `None`, open enums, money as `str` via `Decimal`. (loaded)
- MUST load `python-configuration-resilience` — retry policy, base URL, timeout per attempt. (loaded)
- MUST load `python-authentication` — lazy token fetch, `OAuthProviderError`. (loaded)
- MUST load `python-testing` — stub transport + stub token source for the unit tests. (loaded)
