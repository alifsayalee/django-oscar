# PayPal Server SDK integration plan — django-oscar sandbox

## Scope

New Django app `sandbox/apps/payments_api` (label `payments_api`), routed at `/api/` from
`sandbox/urls.py` (outside `i18n_patterns`). Flow 1 (orders → authorize → fulfil/capture →
cancel/void → refunds → my-orders → reconciliation) and Flow 2 (saved cards via PayPal Vault).

## Repo survey (conventions to imitate)

| Concern | Pattern | Exemplar |
| --- | --- | --- |
| Sandbox app layout | package under `sandbox/apps/`, imported as `apps.<name>` | `sandbox/apps/offers.py`, `sandbox/apps/sitemaps.py` |
| URL wiring | `path(...)` entries in `sandbox/urls.py` before `i18n_patterns` | `sandbox/urls.py` |
| Settings | `environ.Env()` reads in `sandbox/settings.py` | `sandbox/settings.py` (`env.bool`, `env.str`) |
| Order creation | Oscar `OrderCreator.place_order(basket, total, ...)` | `src/oscar/apps/order/utils.py` |
| Payment bookkeeping | Oscar `Source` / `Transaction` / `PaymentEvent` | `src/oscar/apps/payment/abstract_models.py`, `src/oscar/apps/order/processing.py` |
| Saved cards | Oscar `Bankcard` (masked number + `partner_reference`) | `src/oscar/apps/payment/abstract_models.py` |
| Status pipeline | `OSCAR_ORDER_STATUS_PIPELINE` Pending → Being processed → Complete / Cancelled | `sandbox/settings.py` |
| Lint | flake8 max-line-length 119 (`setup.cfg`) | — |

Sync vs async: **sync**. Django under WSGI (`sandbox/wsgi.py`, sync views). → `PaypalClient`, `close()`.

Toolchain: `py -3.11 -m venv venv`, `venv\Scripts\pip install -e .[test]`; SDK installed as
`paypal @ git+https://github.com/context-plugins/paypal-python-sdk.git@main` (version 2.29).
**Drift from the getting-started skill:** the distribution and import root are both `paypal`
(not `pay-pal-server-sdk` / `pay_pal_server_sdk`); client classes are `PaypalClient` /
`AsyncPaypalClient` (aliases `Client` / `AsyncClient`). All facts below come from the installed
package and the SDK map cloned from the same branch (`main`, API spec 2.29).
Tests: `sandbox\manage.py test apps.payments_api` (Django runner, `TestCase`). Type check:
`mypy --strict` on the SDK-facing gateway module (project has no mypy config).

Baseline: sandbox DB built with the given sequence; `child_products.json` loads 11 products,
and `oscar_import_catalogue` of the three CSVs *did* import on this machine (209 products,
203 stock records); `orders.json` then loads. Catalogue prices are GBP; configured currency is
read from `PAYPAL_CURRENCY` — per the task, catalogue *amounts* are charged in the configured
currency.

## Credentials / environment

`sandbox/settings.py` reads `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENVIRONMENT`,
`PAYPAL_CURRENCY`, `PAYPAL_BASE_URL` from the environment (no values in the repo).
Host selection: `PAYPAL_BASE_URL` if set, verbatim (moves the token call too — sdk-map "Servers &
auth"). Else `PAYPAL_ENVIRONMENT == "sandbox"` → `https://api-m.sandbox.paypal.com` (the SDK's only
declared server). Any other environment without `PAYPAL_BASE_URL` → `ImproperlyConfigured` (the SDK
declares no other host; we pass `base_url` explicitly always, never rely on the default).
Missing client id/secret → `ImproperlyConfigured` at first use (never an unauthenticated client).

Smoke run (scratch, outside repo, 2026-09-24): token 200; card `create_order` AUTHORIZE → 201
`COMPLETED` **with the authorization already created** (single-step; calling `authorize_order`
afterwards → 422 `ORDER_ALREADY_AUTHORIZED`); `capture_authorized_payment` 201 with
`seller_receivable_breakdown` gross/fee/net; partial refund 201; vault `create_payment_token` with raw
card 201 (customer id returned); order with `card.vault_id` 201 `COMPLETED`; `void_payment` 200
`VOIDED`; `delete_payment_token` 204; `reauthorize_payment` inside honor period → 422
`REAUTHORIZATION_TOO_SOON`; `search_transactions` 200 (1806 items / 19 pages for 30 days). No
browser challenge (`PAYER_ACTION_REQUIRED`) observed.

## Contract sheet (all ops sync, `client.<group>.<op>`; async twins identical — not used)

Invariants: every keyword after `*` has a real default — never pass defensive `None`s. Trailing
`request_options` (keys `timeout`, `extra_headers`). Parsed call raises `ApiError`; raw peer via
`.with_raw_response` returns `Success`/`Failure`. Imports: client `from paypal import PaypalClient`;
`from paypal.core import ApiError, RawError, ClientCredentials, HttpxClient, HttpRequest,
HttpResponse, OAuthProviderError, Success, Failure, UNSET, UnsetType`; models `from paypal.models
import ...`; enums `from paypal.models.enums import ...`.

| Operation | Signature (positional \| keyword-only used) | Returns | `ApiError.error` union |
| --- | --- | --- | --- |
| `orders.create_order` | `(body: OrderRequest\|OrderRequestDict, *, pay_pal_request_id, prefer)` | `Order` | `CreateOrderErrorBody` = `Error` [400,401,422] \| `RawError` |
| `orders.authorize_order` | `(id: str, *, pay_pal_request_id, prefer)` (only if create returns `APPROVED`) | `OrderAuthorizeResponse` | `AuthorizeOrderErrorBody` = `Error` [400,401,403,404,422,500] \| `RawError` |
| `payments.get_authorized_payment` | `(authorization_id: str)` | `PaymentAuthorization` | `Error` [401,403,404] \| `RawError` [500,…] |
| `payments.reauthorize_payment` | `(authorization_id: str, *, pay_pal_request_id, prefer)` | `PaymentAuthorization` | `Error` [400,401,403,404,422] \| `RawError` [500,…] |
| `payments.capture_authorized_payment` | `(authorization_id: str, *, pay_pal_request_id, prefer, body: CaptureRequest\|Dict)` | `CapturedPayment` | `Error` [400,401,403,404,409,422] \| `RawError` [500,…] |
| `payments.get_captured_payment` | `(capture_id: str)` | `CapturedPayment` | `Error` [401,403,404] \| `RawError` [500,…] |
| `payments.void_payment` | `(authorization_id: str, *, pay_pal_request_id, prefer)` | `PaymentAuthorization` | `Error` [401,403,404,409,422] \| `RawError` [500,…] |
| `payments.refund_captured_payment` | `(capture_id: str, *, pay_pal_request_id, prefer, body: RefundRequest\|Dict)` | `Refund` | `Error` [400,401,403,404,409,422] \| `RawError` [500,…] |
| `vault.create_payment_token` | `(body: PaymentTokenRequest\|Dict, *, pay_pal_request_id)` | `PaymentTokenResponse` | `Error` [400,403,404,422,500] \| `RawError` |
| `vault.delete_payment_token` | `(id: str)` → **returns `None`**; use `with_raw_response` for status | `None` | `Error` [400,403,500] \| `RawError` (404 → `RawError`) |
| `transaction_search.search_transactions` | `(start_date: str, end_date: str, *, fields="transaction_info", balance_affecting_records_only="Y", page_size=100, page=1)` | `SearchResponse` | **Case B**: always `RawError` |

Header semantics (api-reference): `pay_pal_request_id` — orders: stored 6 h, *mandatory* for
single-step create with a card payment source; payments: stored 45 days; vault: 3 h. `prefer`
defaults to `"return=minimal"` (id/status/links only) → we pass `"return=representation"` on every
write whose body we read. `search_transactions`: dates RFC 3339 with seconds; **max range 31 days**;
`page` starts at 1; results lag up to 3 h; last 3 years only. **Observed live (2026-09-24):** a
window whose start PayPal has not processed yet answers **404** `INVALID_REQUEST` "Data for the given
start date is not available." (a `RawError`, Case B) — treated as "no report data yet" for that
window and listed under `paypalDataUnavailable`, never as a failure; any other status still fails.

### Request members set (wire name = Python name unless noted; all `Optional[T]` = `T | UNSET`, never `None`)

- `OrderRequest`: `intent` (req, `CheckoutPaymentIntent.AUTHORIZE`), `purchase_units` (req list),
  `payment_source` (opt).
- `PurchaseUnitRequest`: `amount` (req `AmountWithBreakdown`), `reference_id`, `custom_id`,
  `invoice_id`, `description`, `items` (opt list `ItemRequest`).
- `AmountWithBreakdown`: `currency_code` (req str), `value` (req str), `breakdown` (opt
  `AmountBreakdown.item_total: Money`). `Money`: `currency_code`, `value` (both req str).
- `ItemRequest`: `name` (req), `unit_amount` (req `Money`), `quantity` (req **str**), `sku` (opt).
- `PaymentSource.card: CardRequest` — `name`, `number`, `expiry` (`YYYY-MM`), `security_code`,
  `billing_address: Address`, `vault_id` (all opt).
- `Address`: `country_code` (req), `address_line_1`, `address_line_2`, `admin_area_2`,
  `admin_area_1`, `postal_code` (opt).
- `CaptureRequest`: `amount: Money`, `final_capture: bool`, `invoice_id` (opt).
- `RefundRequest`: `amount: Money`, `invoice_id`, `note_to_payer` (opt; empty = full refund — we
  always send an explicit amount).
- `PaymentTokenRequest`: `payment_source: PaymentTokenRequestPaymentSource` (req; `.card:
  PaymentTokenRequestCard` = `name`, `number`, `expiry`, `security_code`, `billing_address`),
  `customer: Customer` (opt; `id`, `merchant_customer_id`).

### Response members read (all `Optional` — assert on the ones we depend on)

- `Order`: `id`, `status: OrderStatusOrStr` {CREATED, SAVED, APPROVED, VOIDED, COMPLETED,
  PAYER_ACTION_REQUIRED}, `purchase_units[0].payments.authorizations[0]`, `payment_source.card`
  (`last_digits`, `brand`, `expiry`). **Assert** `id` and the authorization `id`/`status`/`amount`.
- `AuthorizationWithAdditionalData` / `PaymentAuthorization`: `id`, `status:
  AuthorizationStatusOrStr` {CREATED, CAPTURED, DENIED, PARTIALLY_CAPTURED, VOIDED, PENDING},
  `status_details.reason` {PENDING_REVIEW, DECLINED_BY_RISK_FRAUD_FILTERS}, `amount: Money`,
  `expiration_time: str`, `create_time: str`.
- `CapturedPayment`: `id`, `status: CaptureStatusOrStr` {COMPLETED, DECLINED, PARTIALLY_REFUNDED,
  PENDING, REFUNDED, FAILED}, `amount`, `seller_receivable_breakdown` (`gross_amount` req,
  `paypal_fee`, `net_amount`), `status_details.reason`. **Assert** `id`, `status`.
- `Refund`: `id`, `status: RefundStatusOrStr` {CANCELLED, FAILED, PENDING, COMPLETED}, `amount`,
  `seller_payable_breakdown` (`paypal_fee`, `net_amount`, `total_refunded_amount`). **Assert** `id`.
- `PaymentTokenResponse`: `id`, `customer.id`, `payment_source.card` (`CardPaymentTokenEntity`:
  `last_digits`, `brand: CardBrandOrStr`, `expiry`, `type_` wire `type`). **Assert** `id`,
  `last_digits`.
- `SearchResponse`: `transaction_details[].transaction_info` (`transaction_id`,
  `paypal_reference_id`, `transaction_event_code`, `transaction_status`, `transaction_amount`,
  `fee_amount`, `invoice_id`, `custom_field`, `transaction_initiation_date`), `total_pages`, `page`,
  `last_refreshed_datetime`.
- `Error`: `name`, `message`, `debug_id` (req); `details[]: ErrorDetails` (`issue` req,
  `description`, `field`).
- Enums are open (`…OrStr`) — compare to members, handle unknown strings explicitly.

### SDK-wide facts the implementation must obey

1. **Decode failures** raise `pydantic.ValidationError`/`ValueError`, not `ApiError`, in both modes.
   On a write: outcome unknown.
2. **Token fetch failure** raises `ApiError` whose `.error` is `OAuthProviderError | RawError`, out of
   the operation call, even in raw mode → configuration error (502, nothing sent).
3. **Transport errors** are raw `httpx` exceptions: `ConnectError/ConnectTimeout/PoolTimeout/
   ProxyError` = never sent; other `httpx.RequestError` = unknown outcome.
4. **No retries** in the SDK. Ours: reads retried on never-sent/5xx/429 (bounded, 3 attempts); writes
   retried only under the **same** `PayPal-Request-Id` (PayPal de-duplicates), bounded to 2 attempts;
   an unresolved unknown outcome is persisted as "in flight" and re-sent under the same key on the
   next request (kind 2 lookup — same key is the lookup).
5. Client: one module-level `PaypalClient` built lazily after fork, `base_url` explicit,
   `timeout=PAYPAL_TIMEOUT` (default 20 s) via our own `HttpxClient` wrapped in a logging transport
   (method, path, status, `paypal-debug-id` only — never bodies/headers/query of card data);
   closed at `atexit`.
6. Money: `Decimal`, formatted with the currency's ISO exponent (JPY 0, KWD 3, default 2). Held amount
   compared to order total exactly (Decimal, same currency) — mismatch → void and fail.

### Deterministic references (idempotency)

| Write | `PayPal-Request-Id` |
| --- | --- |
| authorize (create order) | `{prefix}-{order}-auth-{attempt}` — new attempt only after a definitive decline |
| capture | `{prefix}-{order}-capture-{authorization_id}` |
| reauthorize | `{prefix}-{order}-reauth-{authorization_id}` |
| void | `{prefix}-{order}-void-{authorization_id}` |
| refund | `{prefix}-{order}-refund-{sha256(caller key)[:24]}` |
| vault token | `{prefix}-user{pk}-card-{sha256(caller key)[:24]}` (random if no key) |

`prefix` = setting `PAYPAL_REFERENCE_PREFIX` (default `oscar`). App-side state machine rows make a
double-click a no-op (compare-and-set claims), and PayPal's key de-dup covers concurrent in-flight
requests.

### Stale authorization policy (fulfil)

Honor period 3 days, reauthorize allowed day 4–29, ≥ 30 days needs a new authorization
(reauthorize docstring). At fulfil: if authorization older than 3 days → `reauthorize_payment`, then
capture the new authorization id. If older than 29 days, `expiration_time` passed, or status not
capturable (VOIDED/DENIED/unknown) → 409 `AUTHORIZATION_EXPIRED` with an operator-actionable message
(cancel the order; ask the shopper to pay again). A reauthorize refusal → 409 with PayPal's issue.

## Build order

1. settings + app skeleton + models (extension tables beside Oscar's `Source`/`Transaction`/
   `Bankcard`) + migration.
2. `gateway.py` (typed SDK boundary, error ladder, retries) → mypy --strict.
3. `services.py` (orders, payments, refunds, cards, reconciliation).
4. `views.py` + `urls.py` (JSON, session auth, CSRF kept on, staff checks).
5. Tests with a stub transport (success, typed error, RawError, decode failure, never-sent vs
   unknown, bad credentials, stale auth, refund bounds/idempotency, ownership).
6. Live verification on the sandbox via the HTTP API.

## Assumptions & blockers

- No blockers. Minor assumptions: catalogue amounts are charged in `PAYPAL_CURRENCY` as the task
  specifies; production host must come from `PAYPAL_BASE_URL` (SDK declares only the sandbox host).
- Reconciliation matches PayPal `transaction_id` / `paypal_reference_id` / `invoice_id` /
  `custom_field` against ids and references this app stored; event-code meanings are not interpreted.

## REQUIRED READING (loaded before implementation)

- Client construction, lifetime, WSGI placement, fork safety — MUST load `python-client-initialization` ✔
- OAuth credentials, lazy token, `OAuthProviderError` — MUST load `python-authentication` ✔
- Error ladder, decode/transport failures, status → caller mapping — MUST load `python-error-handling` ✔
- No retries, idempotency keys, may-have-landed lookups, timeouts, logging transport — MUST load `python-configuration-resilience` ✔
- Positional/keyword split, `prefer` default narrowing, `-> None` raw peer — MUST load `python-calling-endpoints` ✔
- `UNSET` vs `None`, open enums, money formatting — MUST load `python-models` ✔
- Stub transport, token request first, both transport-failure inputs — MUST load `python-testing` ✔
