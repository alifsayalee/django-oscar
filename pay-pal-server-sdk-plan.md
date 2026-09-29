# PayPal Server SDK integration plan — django-oscar sandbox

Scope: PayPal card payments (authorize → capture at fulfilment → void / refund), saved cards (vault),
and a reconciliation report, exposed as a JSON API on the `sandbox/` Django site.

## Repo survey (conventions to imitate)

| Concern | Pattern | Exemplar |
| --- | --- | --- |
| Sandbox-local app | package under `sandbox/apps/`, imported as `apps.<name>` (sandbox dir is on `sys.path`) | `sandbox/apps/user/models.py`, `sandbox/apps/sitemaps.py` |
| URL wiring | `path(...)`/`include(...)` in `sandbox/urls.py`, outside `i18n_patterns` for non-localised routes | `sandbox/urls.py` (admin, sitemap) |
| Settings | module-level constants, env read through `environ.Env()` (`env.str`, `env.bool`) | `sandbox/settings.py` |
| Oscar model access | `get_model(app_label, name)` / `get_class(module, name)` from `oscar.core.loading` | `src/oscar/apps/checkout/mixins.py` |
| Order placement | `OrderCreator().place_order(basket, total, shipping_method, shipping_charge, user, status=...)` | `src/oscar/apps/order/utils.py` |
| Payment bookkeeping | `payment.Source` (`allocate`/`debit`/`refund`) + `payment.SourceType` + `payment.Transaction` | `src/oscar/apps/payment/abstract_models.py` |
| Order status | `order.set_status()` under `OSCAR_ORDER_STATUS_PIPELINE` | `sandbox/settings.py` |
| Tests | Django `TestCase` (sandbox `TEST_RUNNER` = DiscoverRunner) run via `sandbox/manage.py test apps.<name>`; `self.assert*` style | `tests/integration/order/test_models.py` style |

- **Sync.** Django under WSGI → `PayPalServerSdkClient` (sync). No `async def` views anywhere.
- **Toolchain.** `py -3.11 -m venv venv`; `venv\Scripts\pip install -e .[test]`; SDK installed from
  `<plugin>/sdk/python/` (non-editable) — probe prints `venv\Lib\site-packages\pay_pal_server_sdk`.
  No type checker configured in the repo → `mypy --strict` (installed into venv) on the new app's
  SDK-facing module (`gateway.py`). Tests: `cd sandbox && ..\venv\Scripts\python manage.py test apps.paypal_payments`.
- **Baseline.** Sandbox DB built per Makefile. Host-notes correction found: the documented sequence
  yields 11 products and `orders.json` fails on a FK; running
  `oscar_import_catalogue` on the three `books.*.csv` files (not `range-products.csv`) before
  `orders.json` gives 209 products / 203 stock records / 1 order, matching the expected state.
- **Credentials.** `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENVIRONMENT=sandbox`,
  `PAYPAL_CURRENCY=USD` present in env; read only through `sandbox/settings.py`.
- **Smoke (sandbox, 2026-09-29, from scratch dir):** token OK; `search_transactions` OK (returns
  `custom_field`, `invoice_id`, `paypal_reference_id`); `create_order` intent AUTHORIZE + card →
  **status `COMPLETED` with `purchase_units[0].payments.authorizations[0]` status `CREATED`** (no
  payer action); `get_authorized_payment` OK; `reauthorize_payment` inside honor period → 422
  `details[0].issue = REAUTHORIZATION_TOO_SOON`; `void_payment` → `VOIDED`; `vault.create_payment_token`
  with raw card → token id + `customer.id`; order with `card.vault_id` + stored_credential → COMPLETED/
  CREATED authorization; `delete_payment_token` raw → `Success(status_code=204)`. No 403 on any op.

## Contract sheet

### Client

| Fact | Value | Source |
| --- | --- | --- |
| Class | `PayPalServerSdkClient` from `pay_pal_server_sdk` (sync; never mix with `AsyncPayPalServerSdkClient`) | sdk-map.md "Getting a client" |
| Constructor | keyword-only: `base_url: str \| None`, `timeout: float = 30.0`, `retry_options`, `custom_http_client: HttpClient \| None`, `oauth2: ClientCredentialsOrDict \| None`, `oauth2_token_source` | sdk-map.md constructor table |
| Auth | `oauth2=ClientCredentials(client_id=..., client_secret=...)` (`pay_pal_server_sdk.core`); omitting it = silent no-auth → must always be set; fail fast if settings empty | sdk-map.md Servers & auth |
| Base URL | only declared server `https://api-m.sandbox.paypal.com`; token endpoint `/v1/oauth2/token` derived from `base_url`. We ALWAYS pass `base_url`: `PAYPAL_BASE_URL` verbatim if set, else `sandbox` → the declared URL; any other `PAYPAL_ENVIRONMENT` without `PAYPAL_BASE_URL` → `ImproperlyConfigured` (plugin declares no live host) | sdk-map.md, getting-started |
| Lifetime | one lazily-built module-level client per process (built after fork on first use, lock-guarded), closed via `atexit`; never per request | python-client-initialization |
| Timeout | `timeout=20.0` (one attempt) | python-configuration-resilience |
| Retries | **keep default** (`max_retries=3`, methods GET/HEAD/PUT/OPTIONS only) → only `get_authorized_payment`, `get_captured_payment`, `search_transactions` retry; every POST/DELETE is sent once. Replays of writes are ours and reuse a stored `PayPal-Request-Id` | python-configuration-resilience |

### Operations (all: parsed form raises `ApiError`; `with_raw_response` returns `ApiResult`; trailing kw-only `request_options`; every kw-only param has a real default — never pass defensive `None`s)

| Operation | Signature (positional \| after `*`) | Returns | `ApiError.error` union |
| --- | --- | --- | --- |
| `client.orders.create_order` | `(body: OrderRequest \| OrderRequestDict, *, pay_pal_request_id: str \| None = None, prefer: str \| None = "return=minimal", …)` | `Order` | `CreateOrderErrorBody` = `Error` [400,401,422] \| `RawError` |
| `client.orders.authorize_order` | `(id_: str, *, pay_pal_request_id=None, prefer="return=minimal", body: OrderAuthorizeRequest \| …Dict \| None = None, …)` | `OrderAuthorizeResponse` | `AuthorizeOrderErrorBody` = `Error` [400,401,403,404,422,500] \| `RawError` |
| `client.payments.get_authorized_payment` | `(authorization_id: str, *, …)` | `PaymentAuthorization` | `Error` [401,403,404] \| `RawError` [500, other] |
| `client.payments.reauthorize_payment` | `(authorization_id: str, *, pay_pal_request_id=None, prefer="return=minimal", body: ReauthorizeRequest \| …Dict \| None = None, …)` | `PaymentAuthorization` | `Error` [400,401,403,404,422] \| `RawError` |
| `client.payments.capture_authorized_payment` | `(authorization_id: str, *, pay_pal_request_id=None, prefer="return=minimal", body: CaptureRequest \| …Dict \| None = None, …)` | `CapturedPayment` | `Error` [400,401,403,404,409,422] \| `RawError` [500, other] |
| `client.payments.get_captured_payment` | `(capture_id: str, *, …)` | `CapturedPayment` | `Error` [401,403,404] \| `RawError` |
| `client.payments.void_payment` | `(authorization_id: str, *, pay_pal_request_id=None, prefer="return=minimal", …)` | `PaymentAuthorization` (body only with `return=representation`) | `Error` [401,403,404,409,422] \| `RawError` |
| `client.payments.refund_captured_payment` | `(capture_id: str, *, pay_pal_request_id=None, prefer="return=minimal", body: RefundRequest \| …Dict \| None = None, …)` | `Refund` | `Error` [400,401,403,404,409,422] \| `RawError` |
| `client.vault.create_payment_token` | `(body: PaymentTokenRequest \| PaymentTokenRequestDict, *, pay_pal_request_id=None, …)` | `PaymentTokenResponse` | `Error` [400,403,404,422,500] \| `RawError` |
| `client.vault.delete_payment_token` | `(id_: str, *, request_options=None)` | **`None`** → use `with_raw_response` (`ApiResult[None, DeletePaymentTokenErrorBody]`) to see the status | `Error` [400,403,500] \| `RawError` |
| `client.transaction_search.search_transactions` | `(start_date: str, end_date: str, *, transaction_id=None, …, fields="transaction_info", balance_affecting_records_only="Y", page_size=100, page=1, …)` | `SearchResponse` | **Case B: `RawError` only** |

Semantics from docstrings: `pay_pal_request_id` "mandatory for all single-step create order calls
(card…)", kept 6 h (orders) / 45 d (capture, reauthorize, void, refund) / 3 h (vault). Authorization
honor period **3 days**; reauthorize allowed "once from Day 4 to Day 29" (smoke), after that a new
authorization is needed. Refund: empty body = full, `amount` = partial. Search: RFC 3339 dates with
seconds, **max range 31 days**, up to 3 h reporting lag, 3 years history; `page` 1-based.

### Models set / read (wire names = Python names unless noted; `Optional[T]` = `T | UnsetType`, never pass `None`)

- `OrderRequest`: `intent: CheckoutPaymentIntentOrStr` (req) · `purchase_units: list[PurchaseUnitRequest]` (req) · `payment_source: Optional[PaymentSource]`.
- `PurchaseUnitRequest`: `amount: AmountWithBreakdown` (req: `currency_code: str`, `value: str`) · `custom_id`, `description`, `reference_id`: `Optional[str]`.
- `PaymentSource.card: Optional[CardRequest]` — `name`, `number`, `expiry` (`YYYY-MM`), `security_code`, `billing_address: Optional[Address]`, `vault_id`, `stored_credential: Optional[CardStoredCredential]`.
- `Address`: `country_code: str` (req) · `address_line_1`, `address_line_2`, `admin_area_2`, `admin_area_1`, `postal_code`: `Optional[str]`.
- `CardStoredCredential`: `payment_initiator: PaymentInitiatorOrStr` (req) · `payment_type: StoredPaymentSourcePaymentTypeOrStr` (req) · `usage` (default `DERIVED`).
- `Order` / `OrderAuthorizeResponse`: `id`, `status: OrderStatusOrStr`, `purchase_units: list[PurchaseUnit]`, `payment_source.card: CardResponse` (`last_digits`, `brand`, `expiry`, `name`) — all `Optional`.
- `PurchaseUnit.payments: Optional[PaymentCollection]` → `authorizations: Optional[list[AuthorizationWithAdditionalData]]` (`id`, `status: AuthorizationStatusOrStr`, `status_details.reason`, `amount: Money`, `expiration_time: str`, `create_time: str`).
- `PaymentAuthorization`: `id`, `status`, `status_details`, `amount`, `expiration_time`, `create_time` — all `Optional`.
- `ReauthorizeRequest.amount: Optional[Money]`. `CaptureRequest`: `amount: Optional[Money]`, `final_capture: bool = False`, `invoice_id: Optional[str]`.
- `CapturedPayment`: `id`, `status: CaptureStatusOrStr`, `amount`, `seller_receivable_breakdown: Optional[SellerReceivableBreakdown]` (`gross_amount: Money` **required** — the one required 2xx member; `paypal_fee`, `net_amount`: `Optional[Money]`).
- `RefundRequest`: `amount: Optional[Money]`, `custom_id`, `note_to_payer`: `Optional[str]`. `Refund`: `id`, `status: RefundStatusOrStr`, `amount` — `Optional`.
- `PaymentTokenRequest`: `payment_source: PaymentTokenRequestPaymentSource` (req; `.card: Optional[PaymentTokenRequestCard]` = `name`, `number`, `expiry`, `security_code`, `billing_address`) · `customer: Optional[Customer]` (`id: Optional[str]`).
- `PaymentTokenResponse`: `id`, `customer: CustomerResponse` (`id`), `payment_source.card: CardPaymentTokenEntity` (`last_digits`, `brand: CardBrandOrStr`, `expiry`, `name`) — all `Optional`.
- `SearchResponse`: `transaction_details: Optional[list[TransactionDetails]]`, `total_pages`, `page`, `last_refreshed_datetime` — `Optional`. `TransactionDetails.transaction_info: Optional[TransactionInformation]` (`transaction_id`, `paypal_reference_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_amount: Money`, `fee_amount: Money`, `transaction_status: str`, `custom_field`, `invoice_id`).
- `Error` (models): `name: str`, `message: str`, `debug_id: str` (req), `details: Optional[list[ErrorDetails]]` (`issue: str` req, `description: Optional[str]`).
- Money is `str`; format with `Decimal.quantize` using currency exponent (ISO 4217 table from python-models).
- `Optional[Any]` members in scope: none set by us (we build no model containing a bare `Any`).

### Enum members used (all open `…OrStr`; compare with `==`, treat unknown strings explicitly)

`CheckoutPaymentIntent.AUTHORIZE` · `OrderStatus`: CREATED, SAVED, APPROVED, VOIDED, COMPLETED, PAYER_ACTION_REQUIRED ·
`AuthorizationStatus`: CREATED, CAPTURED, DENIED, PARTIALLY_CAPTURED, VOIDED, PENDING ·
`CaptureStatus`: COMPLETED, DECLINED, PARTIALLY_REFUNDED, PENDING, REFUNDED, FAILED ·
`RefundStatus`: CANCELLED, FAILED, PENDING, COMPLETED · `PaymentInitiator.CUSTOMER` ·
`StoredPaymentSourcePaymentType.UNSCHEDULED` · `StoredPaymentSourceUsageType.SUBSEQUENT` (all in `pay_pal_server_sdk.models.enums`).

### Failure kinds at the boundary (one mapper, `gateway.ProviderError(status, code, message, outcome_unknown, issue)`)

1. `ApiError` with `OAuthProviderError` payload (token fetch; raises in both modes) → 502, config fault, `outcome_unknown=False`.
2. `ApiError` 401/403/429 → 502/503 (ours, not the caller's).
3. `ApiError` other 4xx with `Error` → same 4xx family surfaced (422/409/404→ our 422/409), carries `details[0].issue`.
4. `ApiError` ≥500 / `RawError` → 502, `outcome_unknown=True` for writes.
5. `pydantic.ValidationError` / `ValueError` from decoding (both modes) → 502, `outcome_unknown=True`.
6. `httpx.ConnectError | ConnectTimeout | PoolTimeout | ProxyError` → 502, `outcome_unknown=False`.
7. other `httpx.RequestError` → 504, `outcome_unknown=True`.
8. 2xx missing the members we depend on (`UNSET` id/status) → 502, `outcome_unknown=True`.

## Design

- New app `sandbox/apps/paypal_payments` (label `paypal_payments`): `gateway.py` (only module importing
  the SDK), `services.py` (claims + orchestration), `views.py` (JSON, session auth, CSRF enforced),
  `urls.py`, `models.py`, `money.py`, tests.
- Reused Oscar models: `order.Order`/`order.Line` (via `OrderCreator`, `Basket`, `Selector`,
  `Repository`, `OrderTotalCalculator`), `payment.SourceType`/`Source`/`Transaction`. New models hold
  only PayPal-owned state: `PayPalPayment` (1:1 Order), `PayPalRefund`, `SavedCard`, `PayPalCustomer`.
- Order status pipeline gains `Awaiting payment → Payment authorized → Complete`, `→ Cancelled`.
- Currency = `settings.PAYPAL_CURRENCY`; amounts = catalogue prices (fixture `price_currency` GBP is
  overridden: order `currency` set to the configured currency, per task).
- Views use `transaction.non_atomic_requests` (sandbox sets `ATOMIC_REQUESTS=True`) so each claim
  commits before its SDK call.
- Card data: validated then passed straight to the SDK; never saved, never logged; views/functions that
  touch it use `sensitive_post_parameters` / `sensitive_variables`.
- Stale authorization at fulfil: if `now > authorized_at + 3 days` → `reauthorize_payment` (full
  amount) first, store new authorization id; past `expiration_time`, not renewable, or reauth refused
  → 409 `authorization_not_renewable` with an operator action ("cancel the order, ask the shopper to
  pay again") plus PayPal's issue.
- Reconciliation: split `[from, to)` into ≤31-day windows, page every window to `total_pages`,
  match on capture/refund ids then `custom_field` (= Oscar order number we send as `custom_id`),
  classify matched / amount-mismatch / unknown-to-app / missing-from-PayPal (records newer than
  `last_refreshed_datetime` flagged "not yet reported"). Range capped at 366 days.

## DUPLICATE CLAIMS

| Write | Where the claim is stored | What rejects the second one | Where that rejection is caught | Where in the code |
| --- | --- | --- | --- | --- |
| Authorize (`POST /orders/{id}/pay`) → `create_order` (+`authorize_order`) | `PayPalPayment.state` row in SQLite (`AUTHORIZING`, `claim_expires_at`, stable `authorize_request_id`) | conditional `UPDATE … WHERE state IN (AWAITING_PAYMENT, FAILED) OR (state=AUTHORIZING AND claim_expires_at < now)` affecting 0 rows | `services._claim` raises `ClaimRejected` (0 rows) → caught in `services.pay_order` `except ClaimRejected` → 409 | `services.pay_order` (claim via `services._claim`) → `gateway.authorize_order_total` |
| Capture (`POST /orders/{id}/fulfil`) → (`reauthorize_payment`) + `capture_authorized_payment` | `PayPalPayment.state = CAPTURING`, `capture_request_id` | conditional `UPDATE … WHERE state=AUTHORIZED OR (state=CAPTURING AND claim expired)` → 0 rows | `except ClaimRejected` in `services.fulfil_order` → 409 | `services.fulfil_order` (claim via `services._claim`) → `gateway.reauthorize` / `gateway.capture_authorization` |
| Void (`POST /orders/{id}/cancel`) → `void_payment` | `PayPalPayment.state = VOIDING` | conditional `UPDATE … WHERE state=AUTHORIZED OR (state=VOIDING AND claim expired)` → 0 rows | `except ClaimRejected` in `services.cancel_order` → 409 | `services.cancel_order` (claim via `services._claim`) → `gateway.void_authorization` |
| Refund (`POST /orders/{id}/refunds`) → `refund_captured_payment` | `PayPalRefund` row, `UNIQUE(payment, idempotency_key)` + reservation `PayPalPayment.refund_reserved` | `IntegrityError` on the unique insert (same key); conditional `UPDATE … SET refund_reserved = refund_reserved + amt WHERE refund_reserved + amt <= captured_amount` → 0 rows (over-refund) | `except IntegrityError` in `services._claim_refund` (returns earlier refund / replays unknown one); 0-row reservation → `RefundRejected` → 422 | `services._claim_refund` → `gateway.refund_capture` |
| Save card (`POST /payment-methods`) → `create_payment_token` | `SavedCard` row `UNIQUE(user, idempotency_key)` (state `SAVING`), when the caller sends `Idempotency-Key` | `IntegrityError` on the unique insert | `except IntegrityError` in `services._claim_saved_card` → earlier card / 409 | `services._claim_saved_card` → `gateway.vault_card` |
| Delete card (`DELETE /payment-methods/{id}`) → `delete_payment_token` | `SavedCard.state = DELETING` | conditional `UPDATE … WHERE state=ACTIVE OR (state=DELETING AND claim expired)` → 0 rows | `except ClaimRejected` in `services.delete_saved_card` → 404/409 | `services.delete_saved_card` (claim via `services._claim`) → `gateway.delete_vaulted_card` |

## Assumptions & Blockers

- No blockers. Minor: live host not declared by the SDK → non-sandbox requires `PAYPAL_BASE_URL`.
- Minor: refunds are shopper-scoped (task lists only fulfil/cancel/reconciliation as operator actions).
- Minor: SQLite is the claim store (the codebase's own DB); claims are single-statement conditional
  UPDATEs / unique inserts, which SQLite and PostgreSQL both enforce.

## REQUIRED READING

- Client construction/lifetime — MUST load `python-client-initialization` (loaded).
- OAuth + token-fetch failure payload — MUST load `python-authentication` (loaded).
- Every try/except around SDK calls — MUST load `python-error-handling` (loaded).
- Signatures, raw vs parsed, `None`-returning delete — MUST load `python-calling-endpoints` (loaded).
- `UNSET`, open enums, money strings — MUST load `python-models` (loaded).
- base_url, retries, timeouts, duplicate claims — MUST load `python-configuration-resilience` (loaded).
- Tests with a stub transport — MUST load `python-testing` (loaded).
