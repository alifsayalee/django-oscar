# PayPal Server SDK integration plan — django-oscar sandbox

Scope: PayPal card payments (authorize → capture / void → refund), vaulted ("saved") cards, and a
PayPal-vs-app reconciliation report, exposed as a JSON API under `/api/` by a new sandbox app
`sandbox/apps/paypal_payments/`.

## Repo survey

| Convention | Exemplar |
| --- | --- |
| Sandbox-local apps live under `sandbox/apps/` and are imported as `apps.<name>` (settings `ROOT_URLCONF='urls'`, sandbox dir on `sys.path`) | `sandbox/apps/user/models.py`, `sandbox/urls.py` (`from apps.sitemaps import …`) |
| Non-i18n routes go in the top-level `urlpatterns` list of `sandbox/urls.py`; Oscar's own routes are in `i18n_patterns` | `sandbox/urls.py` |
| Settings read env through `django-environ` (`env.str/bool/list`) | `sandbox/settings.py` |
| Oscar models are loaded with `oscar.core.loading.get_model` / `get_class` | `src/oscar/apps/order/processing.py` |
| Orders are written by `OrderCreator.place_order` from a `Basket` | `src/oscar/apps/order/utils.py` |
| Payment bookkeeping: `payment.Source` (`allocate`/`debit`/`refund`) + `payment.Transaction`; order-level `PaymentEvent` via `EventHandler.create_payment_event`; stock via `EventHandler.consume_stock_allocations` / `cancel_stock_allocations` | `src/oscar/apps/payment/abstract_models.py`, `src/oscar/apps/order/processing.py` |
| Order status pipeline: `Pending` → `Being processed` → `Complete`; `Pending`/`Being processed` → `Cancelled` | `sandbox/settings.py` `OSCAR_ORDER_STATUS_PIPELINE` |
| `DATABASES['default']['ATOMIC_REQUESTS'] = True` — every view is one transaction unless marked `transaction.non_atomic_requests` | `sandbox/settings.py` |
| Tests: pytest + pytest-django, plain `assert`, `@pytest.mark.django_db`, settings from `tests/settings.py` | `tests/integration/order/test_creator.py` |

- **Sync vs async**: Django under WSGI, sync views → **sync `PayPalServerSdkClient`**.
- **Toolchain**: `py -3.11 -m venv venv`; `venv\Scripts\pip install -e .[test]`; SDK installed (non-editable) from
  `<plugin>/sdk/python/` → `venv/Lib/site-packages/pay_pal_server_sdk` (v2.29, verified importable).
  Tests: `venv\Scripts\python -m pytest <path>`. Type checker: none configured → `mypy --strict` on the new module files
  that touch the SDK (installed into venv).
- **Baseline**: `pytest tests/integration/{order,payment,basket} tests/functional/checkout` on the untouched tree
  — all DB tests error (no PostgreSQL on this host); see Assumptions.
- **Credentials**: `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENVIRONMENT=sandbox`, `PAYPAL_CURRENCY=USD` present in env;
  `PAYPAL_BASE_URL` unset → host is the SDK default `https://api-m.sandbox.paypal.com`.
- **Sandbox DB**: task's build sequence + `oscar_import_catalogue` of the CSVs (the CSV import does work here: 209 products) —
  catalogue prices are GBP stock records.

### Read-only / reversible smoke against the real credential (scratchpad, not the project)

| Operation | Result |
| --- | --- |
| token fetch | OK |
| `orders.create_order` intent AUTHORIZE + `payment_source.card` (+ `pay_pal_request_id`, `prefer="return=representation"`) | **single step**: response already carries `purchase_units[0].payments.authorizations[0]` status `CREATED`, `expiration_time` (+29d). No `authorize_order` call needed; **no 3-DS / payer-action challenge** |
| `payments.get_authorized_payment` | OK, `create_time`, `expiration_time`, `status` |
| `payments.capture_authorized_payment` (`prefer=representation`) | `status COMPLETED`, `seller_receivable_breakdown.{gross_amount,paypal_fee,net_amount}` present |
| `payments.refund_captured_payment` partial, same `pay_pal_request_id` twice | same refund id returned both times (PayPal dedupes) |
| `vault.create_payment_token` with card | `id`, `customer.id`, `payment_source.card.{brand,last_digits,expiry,name}` |
| `create_order` with `card.vault_id` | single-step authorization as above |
| `payments.void_payment` | `status VOIDED` |
| `payments.reauthorize_payment` inside honor period | 422 `Error`, `details[0].issue = REAUTHORIZATION_TOO_SOON` |
| `vault.delete_payment_token` | OK (`None`) |
| `vault.get_payment_token` on a deleted token | error body fails to decode → **`pydantic.ValidationError`** (not `ApiError`) |
| `transaction_search.search_transactions` 30-day window | OK; account is shared, so it contains other integrations' transactions (foreign `invoice_id`s) |

## Contract sheet

Client (all facts: `sdk-map.md`, `map/operations/*.md`, model modules):

- `from pay_pal_server_sdk import PayPalServerSdkClient` — keyword-only ctor: `base_url: str|None`, `timeout: float=30.0`,
  `retry_options`, `custom_http_client`, `oauth2: ClientCredentialsOrDict|None`, `oauth2_token_source`.
- **Sync client**, one per process, built lazily on first use (after any fork), closed via `atexit` → `client.close()`.
- `oauth2=ClientCredentials(client_id=…, client_secret=…)` (`pay_pal_server_sdk.core`). Omitting it = unauthenticated — the factory refuses to build without both values.
- **Host**: SDK declares one server, default `https://api-m.sandbox.paypal.com`; no environment enum. `PAYPAL_BASE_URL` set → passed verbatim as `base_url` (moves token fetch too). Unset → `PAYPAL_ENVIRONMENT == "sandbox"` → `base_url="https://api-m.sandbox.paypal.com"` passed explicitly; any other environment without `PAYPAL_BASE_URL` → `ImproperlyConfigured` (the plugin documents no non-sandbox host; see Assumptions).
- **Retries**: default policy **kept** (retries GET/HEAD/PUT/OPTIONS on 408/429/5xx/transport; never POST/DELETE). Every POST we send carries our own stored `PayPal-Request-Id`, so our resume path re-sends the same key. No extra retry layer.
- **Timeout**: `timeout=20.0` per wait (sync: no whole-call limit exists).
- Every keyword-only param has a real default — never pass defensive `None`s.

| Operation | Positional / keyword-only we set | Returns (parsed) | `ApiError.error` union |
| --- | --- | --- | --- |
| `client.orders.create_order` | `body: OrderRequest` / `*, pay_pal_request_id: str, prefer="return=representation"` (request id **mandatory** for single-step card orders — api-reference) | `Order` | `CreateOrderErrorBody` = `Error` [400,401,422] \| `RawError` |
| `client.payments.get_authorized_payment` | `authorization_id` | `PaymentAuthorization` | `Error` [401,403,404] \| `RawError` [500,…] |
| `client.payments.reauthorize_payment` | `authorization_id` / `*, pay_pal_request_id, prefer="return=representation", body: ReauthorizeRequest(amount=Money)` | `PaymentAuthorization` | `Error` [400,401,403,404,422] \| `RawError` |
| `client.payments.capture_authorized_payment` | `authorization_id` / `*, pay_pal_request_id, prefer="return=representation", body: CaptureRequest(amount=Money, final_capture=True, invoice_id)` | `CapturedPayment` | `Error` [400,401,403,404,409,422] \| `RawError` [500,…] |
| `client.payments.void_payment` | `authorization_id` / `*, pay_pal_request_id, prefer="return=representation"` | `PaymentAuthorization` | `Error` [401,403,404,409,422] \| `RawError` [500,…] |
| `client.payments.refund_captured_payment` | `capture_id` / `*, pay_pal_request_id, prefer="return=representation", body: RefundRequest(amount=Money, invoice_id, note_to_payer)` | `Refund` | `Error` [400,401,403,404,409,422] \| `RawError` [500,…] |
| `client.vault.create_payment_token` | `body: PaymentTokenRequest` / `*, pay_pal_request_id` | `PaymentTokenResponse` | `Error` [400,403,404,422,500] \| `RawError` |
| `client.vault.delete_payment_token` | `id_` (returns **`None`**; raw peer `ApiResult[None, DeletePaymentTokenErrorBody]` used to see status) | `None` | `Error` [400,403,500] \| `RawError` |
| `client.transaction_search.search_transactions` | `start_date: str, end_date: str` (RFC 3339, seconds required, **max range 31 days**) / `*, page` (every page to `total_pages`); defaults `page_size=100`, `fields="transaction_info"`, `balance_affecting_records_only="Y"` kept | `SearchResponse` | **Case B**: always `RawError` |

Auth failure on any call: `ApiError` with `.error` `OAuthProviderError | RawError` (from `pay_pal_server_sdk.core`), raised even from `with_raw_response`.

Models (members we set/read; `Optional[T]` = `T | UnsetType`, **never pass `None`**; read with `isinstance(x, UnsetType)` to narrow):

| Model (module) | Members |
| --- | --- |
| `OrderRequest` | `intent: CheckoutPaymentIntentOrStr` **required** (`CheckoutPaymentIntent.AUTHORIZE`); `purchase_units: list[PurchaseUnitRequest]` **required**; `payment_source: Optional[PaymentSource]` |
| `PurchaseUnitRequest` | `amount: AmountWithBreakdown` **required**; `invoice_id`, `custom_id`, `description`, `reference_id`: `Optional[str]` |
| `AmountWithBreakdown`, `Money` | `currency_code: str`, `value: str` both **required** |
| `PaymentSource` | `card: Optional[CardRequest]` |
| `CardRequest` | `number`, `expiry` (`YYYY-MM`), `security_code`, `name`, `vault_id`: `Optional[str]`; `billing_address: Optional[Address]` |
| `PaymentTokenRequest` | `payment_source: PaymentTokenRequestPaymentSource` **required**; `customer: Optional[Customer]` (`id: Optional[str]`) |
| `PaymentTokenRequestPaymentSource` / `PaymentTokenRequestCard` | `card` / `name, number, expiry (YYYY-MM), security_code: Optional[str]`, `billing_address: Optional[Address]` |
| `Order` (response) | `id`, `status: Optional[OrderStatusOrStr]`, `purchase_units: Optional[list[PurchaseUnit]]` → `.payments: Optional[PaymentCollection]` → `.authorizations: Optional[list[AuthorizationWithAdditionalData]]` (`id`, `status`, `amount`, `expiration_time`, `create_time`); `payment_source.card: CardResponse` (`brand`, `last_digits`) |
| `PaymentAuthorization` | `id`, `status: Optional[AuthorizationStatusOrStr]`, `amount`, `create_time`, `expiration_time` |
| `CapturedPayment` | `id`, `status: Optional[CaptureStatusOrStr]`, `amount`, `seller_receivable_breakdown: Optional[SellerReceivableBreakdown]` (`gross_amount: Money` required, `paypal_fee`, `net_amount`: Optional), `create_time` |
| `Refund` | `id`, `status: Optional[RefundStatusOrStr]`, `amount`, `seller_payable_breakdown`, `create_time` |
| `PaymentTokenResponse` | `id`, `customer: Optional[CustomerResponse]` (`id`), `payment_source.card: Optional[CardPaymentTokenEntity]` (`brand`, `last_digits`, `expiry`, `name`) |
| `SearchResponse` | `transaction_details: Optional[list[TransactionDetails]]`, `page`, `total_pages`, `total_items`, `last_refreshed_datetime` |
| `TransactionDetails.transaction_info: TransactionInformation` | `transaction_id`, `paypal_reference_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_amount: Money`, `fee_amount`, `transaction_status`, `invoice_id`, `custom_field` |
| `Error` (typed arm) | `name`, `message`, `debug_id`: `str` required; `details: Optional[list[ErrorDetails]]` (`issue: str` required, `description`, `field`) |

Enums (`pay_pal_server_sdk.models.enums`, all open `…OrStr` — always keep a fallback arm): `CheckoutPaymentIntent{CAPTURE,AUTHORIZE}`;
`OrderStatus{CREATED,SAVED,APPROVED,VOIDED,COMPLETED,PAYER_ACTION_REQUIRED}`; `AuthorizationStatus{CREATED,CAPTURED,DENIED,PARTIALLY_CAPTURED,VOIDED,PENDING}`;
`CaptureStatus{COMPLETED,DECLINED,PARTIALLY_REFUNDED,PENDING,REFUNDED,FAILED}`; `RefundStatus{CANCELLED,FAILED,PENDING,COMPLETED}`.

Members we must assert on after each call (2xx truncation decodes cleanly as `UNSET`):
create_order → `id`, `status`, first authorization `id`/`status`/`amount`; capture → `id`, `status`, `amount`; void → `status`;
reauthorize → `id`; refund → `id`, `status`; create_payment_token → `id`. Missing → `outcome_unknown` error.

Semantics (docstrings / api-reference): 3-day honor period after authorization; reauthorize allowed day 4–29 once
(smoke: `REAUTHORIZATION_TOO_SOON` inside honor period); authorization valid 29 days (`expiration_time`); refund with
empty body = full refund; `PayPal-Request-Id` retained 6h (orders) / 45 days (payments). Transaction search lags up to 3h;
max 31-day range per call.

Money formatting: `Decimal.quantize` by ISO-4217 exponent of `PAYPAL_CURRENCY` (python-models table), never float.

Error boundary (one function, all call sites): `ApiError` → `OAuthProviderError` / 401 / 403 → 502 config; 429 → 503;
typed `Error` 4xx → same 4xx with `name`/`message`/`details[].issue`/`debug_id`; other 4xx → same status; 5xx → 502
`outcome_unknown`. `pydantic.ValidationError`/`ValueError` from decoding → 502 `outcome_unknown` on success paths.
`httpx.ConnectError|ConnectTimeout|PoolTimeout|ProxyError` → 502, known-not-sent; other `httpx.RequestError` → 504,
`outcome_unknown`. Nothing logs `str(e)` bodies or card data; card-holding functions are `@sensitive_variables`.

## DUPLICATE CLAIMS

Claim store = the sandbox's own database (SQLite by default, any Django backend), via unique constraints. Views
that claim are `transaction.non_atomic_requests` so the claim commits before the SDK call.

| Write | Where the claim is stored | What rejects the second one | Where that rejection is caught | Where in the code |
| --- | --- | --- | --- | --- |
| Pay (create_order single-step authorize) | `OperationClaim` row, unique `key = "authorize:<order pk>"`, holds the `PayPal-Request-Id` | DB unique constraint on `OperationClaim.key` → `IntegrityError` | `except IntegrityError` in the claim helper → existing claim: done → return payment; in progress → 409; outcome-unknown → atomic takeover and resend with the same request id | `claims.claim_or_existing` (key `authorize:<pk>`, from `services.pay_order`), then `gateway.authorize` → `client.orders.create_order` |
| Fulfil (capture, incl. reauthorize) | `OperationClaim`, unique `key = "settle:<order pk>"` (shared with cancel: one settlement per authorization) | unique constraint → `IntegrityError` | same helper `except IntegrityError` | `claims.claim_or_existing` (key `settle:<pk>`, action `capture`, from `services.fulfil_order`), then `gateway.reauthorize` → `client.payments.reauthorize_payment` (via `services._renew_if_stale`, only when past the honor period) and `gateway.capture` → `client.payments.capture_authorized_payment` |
| Cancel (void) | `OperationClaim`, `key = "settle:<order pk>"` | unique constraint → `IntegrityError` | same helper | `claims.claim_or_existing` (key `settle:<pk>`, action `void`, from `services.cancel_order`), then `gateway.void` → `client.payments.void_payment` |
| Refund | `PayPalRefund` row, unique `(payment, idempotency_key)`, plus the amount reserved on `PayPalPayment.refund_reserved` by a conditional `UPDATE … WHERE refund_reserved + amount <= captured_amount` in the same transaction | unique constraint → `IntegrityError`; over-refund → reservation update touches 0 rows | `except IntegrityError` in `services._claim_refund`; 0-row reservation → 409 | `services._claim_refund` (insert `PayPalRefund` + conditional reservation on `refund_reserved_minor`), then `gateway.refund` → `client.payments.refund_captured_payment` (via `services._send_refund`) |
| Save card (vault) | `OperationClaim`, `key = "vault:<user pk>:<Idempotency-Key>"` | unique constraint → `IntegrityError` | same helper | `claims.claim_or_existing` (key `vault:<user>:<key>`, from `services.save_card`), then `gateway.vault_card` → `client.vault.create_payment_token` |
| Place order (no PayPal call) | `OperationClaim`, `key = "order:<user pk>:<Idempotency-Key>"` when the header is sent | unique constraint → `IntegrityError` | same helper | `claims.claim_or_existing` (key `order:<user>:<key>`, from `services.place_order`), then no SDK call — `services._create_order` → Oscar `OrderCreator.place_order` |
| Delete saved card | `SavedCard.deleted_at` tombstone set by conditional `UPDATE … WHERE deleted_at IS NULL` | 0 rows → already deleted → 404 | `services.delete_card` row-count check | `services.delete_card` (conditional tombstone `UPDATE`), then `gateway.delete_vault_token` → `client.vault.delete_payment_token` (via `services.purge_vault_token`) |

## Implementation steps

1. `sandbox/settings.py`: `PAYPAL_CLIENT_ID/SECRET/ENVIRONMENT/CURRENCY/BASE_URL` via `env`, app in `INSTALLED_APPS`, logger.
2. App `sandbox/apps/paypal_payments/`: `models.py` (PayPalPayment, PayPalRefund, SavedCard, PayPalCustomer, OperationClaim) + migration;
   `client.py` (lazy client factory); `errors.py` (boundary); `money.py`; `claims.py`; `services.py`; `reconciliation.py`;
   `views.py` (JSON, session auth, CSRF enforced, staff checks); `urls.py`; management command `paypal_purge_deleted_cards`.
3. Wire `path('api/', include('apps.paypal_payments.urls'))` into `sandbox/urls.py`.
4. Tests under `sandbox/apps/paypal_payments/tests/` with a stub transport (python-testing), including error-boundary split.
5. `mypy --strict` on SDK-touching modules; run tests; live end-to-end run against sandbox.

## Assumptions & Blockers

- (minor) **No production host in the plugin.** The SDK declares only the sandbox server. Non-sandbox deployments must set
  `PAYPAL_BASE_URL`; otherwise startup of the PayPal client raises `ImproperlyConfigured`. Not a blocker for this task (sandbox).
- (minor) Catalogue stock records are priced in GBP; per the task the numeric catalogue price is charged in `PAYPAL_CURRENCY`
  and the Oscar `Order.currency` is set to it.
- (minor) Offers/vouchers are not applied to API orders (amounts come straight from catalogue prices); shipping = Oscar's default `Free` method, no address.
- (minor) Refunds are shopper-scoped (the task lists only fulfil/cancel/reconciliation as operator actions).
- (minor) Baseline: Oscar's own test suite (`tests/settings.py`) targets PostgreSQL on localhost:5432, which this machine does not have — every DB test errors on the untouched tree (368 errors in the order/payment/basket/checkout subset). The new app's tests run against the sandbox settings (SQLite) instead.
- No blockers.

## REQUIRED READING (all loaded before implementation)

- MUST load `python-error-handling` — the boundary/ladder, unknown-vs-unsent split, OAuthProviderError. (loaded)
- MUST load `python-client-initialization` — lazy per-process client, close obligation. (loaded)
- MUST load `python-configuration-resilience` — base_url, retries kept, duplicate-claim rule. (loaded)
- MUST load `python-calling-endpoints` — `prefer` default narrows responses; `None`-returning delete. (loaded)
- MUST load `python-models` — `UNSET` narrowing, money strings, open enums. (loaded)
- MUST load `python-authentication` — lazy token, failure shape. (loaded)
- MUST load `python-testing` — stub transport + token response first. (loaded)
