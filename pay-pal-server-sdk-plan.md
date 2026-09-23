# PayPal integration plan — django-oscar sandbox (`sandbox/apps/paypal_payments`)

## SDK identity drift (verified against the installed package — the lookup skill is stale)

| Skill said | Installed reality (`venv/Lib/site-packages/paypal`, map `sdk-map.md`) |
|---|---|
| distribution `pay-pal-server-sdk` | distribution **`paypal`** 2.29 (`pip install "paypal @ git+https://github.com/context-plugins/paypal-python-sdk.git@main"`) |
| import root `pay_pal_server_sdk` | import root **`paypal`** |
| `PayPalServerSdkClient` | **`PaypalClient`** (alias `Client`); async `AsyncPaypalClient` |

The installed package matches the `main` clone byte-for-byte (`diff -rq`), so the map describes the code we run.

## Decisions

- **Sync.** Django under WSGI, sync views → `paypal.PaypalClient`. One long-lived client per process,
  built lazily on first use (never at import, so tests and `manage.py` never need credentials), guarded
  by a lock; `close()` from `atexit`. Transport: `HttpxClient(timeout=PAYPAL_TIMEOUT)` wrapped in a
  logging transport (method, URL, status, ms — never headers/bodies). Because we pass our own transport,
  the timeout lives on `HttpxClient`, not on the client's `timeout=`.
- **Base URL.** `PAYPAL_BASE_URL` if set, verbatim (token fetch moves with it — map *Servers & auth*).
  Otherwise `PAYPAL_ENVIRONMENT` → explicit map `{"sandbox": "https://api-m.sandbox.paypal.com"}`; the SDK
  declares only that one server, so any other environment **requires** `PAYPAL_BASE_URL` and an unknown
  value raises `ImproperlyConfigured` (no silent default). Always passed explicitly as `base_url=`.
- **Credentials.** `settings.PAYPAL_*` read with `env.str(..., default="")` in `sandbox/settings.py`
  (import never raises); `get_client()` refuses empty id/secret with `ImproperlyConfigured` naming the
  missing settings. Tests use a stub transport + placeholder credentials via `override_settings`.
- **Request ids.** Every `PayPal-Request-Id` is derived from a per-payment UUID (`PayPalPayment.reference`),
  a refund's UUID, or a saved card's UUID — never from the order number, which repeats across shops.
- **Retries.** The SDK performs none. We add none automatically; every write carries a deterministic
  `PayPal-Request-Id`, and an unknown outcome is recovered by the *next* caller request re-sending under
  the **same** id (PayPal collapses duplicates: 6 h for orders, 45 d for reauthorize, 3 h vault — docstrings).
- **Oscar reuse.** `order.Order`/`Line` created through Oscar's `OrderCreator` from a private basket;
  status changes via `Order.set_status` (pipeline in settings gains `Awaiting payment` and
  `Payment authorized`); money bookkeeping on `payment.Source`/`Transaction` (`allocate`/`debit`/`refund`)
  with a `SourceType` "PayPal"; `order.PaymentEvent` + `PaymentEventType` for Authorised/Settled/Refunded/
  Voided; stock via `EventHandler.consume_stock_allocations` / `cancel_stock_allocations`.
- **New local models** (one app, one migration): `PayPalPayment` (1:1 order — the durable claim row for
  pay/fulfil/cancel), `PayPalRefund` (UNIQUE(payment, idempotency_key) — the refund claim),
  `SavedCard` (UNIQUE(user, request_ref) — the save claim; stores only PayPal token id, brand, last 4,
  expiry, holder name), `PayPalCustomer` (user → PayPal vault customer id). No PAN/CVV ever stored.
- **State transitions are claims**: one conditional `UPDATE ... WHERE state IN (...)` decides the single
  request that may call PayPal (double-click safe across processes). Losers answer from the row.
- **Refund ceiling**: reserved atomically with `UPDATE payment SET refund_reserved = refund_reserved + x
  WHERE refund_reserved + x <= captured_amount`; released only on a definitive failure.
- **Reconciliation**: `search_transactions` in ≤31-day windows (docstring: max range 31 days), every page
  (`page` 1..`total_pages`, page cap → `truncated: true` in the response), `balance_affecting_records_only="N"`
  so authorizations/voids appear, narrowed back to [from, to) on `transaction_initiation_date`. Local side
  filtered on the **provider's** timestamps we stored (authorization/capture/refund `create_time`). Matching
  by PayPal transaction id against the set of every id an order owns (auth(s), capture, refunds), fallback
  `custom_field == payment.custom_id` (unique per payment). Buckets: matched, provider_only,
  local_only, not_yet_reported (local newer than PayPal's `last_refreshed_datetime`), unsettled (local rows
  with no provider time).
- **Auth/roles**: Django session (`/api/session` login/logout + Oscar's own login page both work), CSRF
  enforced on unsafe methods (`X-CSRFToken`, token from `GET /api/csrf`). Staff-only: fulfil, cancel,
  reconciliation. All shopper lookups filter by `request.user` → 404 on another user's ids.
- **Secrets**: views/services holding card data use `sensitive_variables`/`sensitive_post_parameters`;
  nothing logs request bodies.
- **Currency scale**: amounts quantized by currency exponent (`JPY/KRW 0`, `KWD/BHD/TND 3`, else 2) and
  compared as `Decimal`.

## Contract sheet (every row from `sdk-map.md`, `map/operations/*.md`, and the model/enum modules)

Common: client `PaypalClient(*, base_url, timeout, custom_http_client, oauth2, oauth2_token_source)` —
all keyword-only. `oauth2=ClientCredentials(client_id=..., client_secret=...)` from `paypal.core`.
Every keyword-only op param has a real default — no defensive `None`s. Trailing `request_options`.
Every op also on `.with_raw_response` → `ApiResult` (`Success`/`Failure` in `paypal.core`).
Decode failure → `pydantic.ValidationError`/`ValueError` in both modes. Token failure → `ApiError` whose
`.error` is `OAuthProviderError | RawError` (`paypal.core`). Transport → raw `httpx` exceptions.
Imports: models `paypal.models`, enums `paypal.models.enums`, core `paypal.core`.

| Operation | Positional / keyword-only we set | Returns | Error union |
|---|---|---|---|
| `orders.create_order` | `body: OrderRequest` / `pay_pal_request_id`, `prefer="return=representation"` | `Order` | `Error`[400,401,422] \| `RawError` |
| `orders.authorize_order` | `id` / `pay_pal_request_id`, `prefer="return=representation"` | `OrderAuthorizeResponse` | `Error`[400,401,403,404,422,500] \| `RawError` |
| `payments.get_authorized_payment` | `authorization_id` | `PaymentAuthorization` | `Error`[401,403,404] \| `RawError`[500,…] |
| `payments.reauthorize_payment` | `authorization_id` / `pay_pal_request_id`, `prefer`, `body=ReauthorizeRequest(amount=Money)` | `PaymentAuthorization` | `Error`[400,401,403,404,422] \| `RawError` |
| `payments.capture_authorized_payment` | `authorization_id` / `pay_pal_request_id`, `prefer`, `body=CaptureRequest(amount, final_capture=True)` (invoice_id omitted → inherits the order's) | `CapturedPayment` | `Error`[400,401,403,404,409,422] \| `RawError` |
| `payments.get_captured_payment` | `capture_id` | `CapturedPayment` | `Error`[401,403,404] \| `RawError` |
| `payments.void_payment` | `authorization_id` / `pay_pal_request_id`, `prefer` | `PaymentAuthorization` | `Error`[401,403,404,409,422] \| `RawError` |
| `payments.refund_captured_payment` | `capture_id` / `pay_pal_request_id`, `prefer`, `body=RefundRequest(amount=Money, note_to_payer?)` | `Refund` | `Error`[400,401,403,404,409,422] \| `RawError` |
| `vault.create_payment_token` | `body: PaymentTokenRequest` / `pay_pal_request_id` | `PaymentTokenResponse` | `Error`[400,403,404,422,500] \| `RawError` |
| `vault.delete_payment_token` | `id` — **returns `None`**; parsed mode (success = no exception; 204 vs 200 irrelevant), PayPal 404 → already deleted | `None` | `Error`[400,403,500] \| `RawError` |
| `transaction_search.search_transactions` | `start_date, end_date` (RFC3339, seconds required, ≤31 days) / `page_size=100`, `page`, `balance_affecting_records_only="N"`, `fields="transaction_info"` (default) | `SearchResponse` | Case B: always `RawError` |

`Error` (`paypal.models`): `name: str`, `message: str`, `debug_id: str` required; `details: Optional[list[ErrorDetails]]`
(`issue: str` required, `description`, `field`, `location`, `value` — **`value` may echo input; never surfaced**).

### Request members we set (Python name = wire name unless noted; `Optional` = `T | UNSET`, never `None`)

- `OrderRequest`: `intent` **req** (`CheckoutPaymentIntent.AUTHORIZE`); `purchase_units` **req**;
  `payment_source` (set: card or vault id). `processing_instruction`, `payer`, `application_context`: omit → provider default.
- `PurchaseUnitRequest`: `amount` **req** `AmountWithBreakdown(currency_code req, value req str)`;
  `reference_id` = order number; `custom_id` = `"{number}-{payment.reference[:12]}"` (reconciliation key,
  appears as `custom_field`; order numbers alone repeat across shops sharing one PayPal account);
  `invoice_id` = `"{custom_id}-A{attempt}"` (new per definitive retry);
  `description` = "Order {number}". `items`/`shipping`/`payee`/`soft_descriptor`: omit → provider default.
- `PaymentSource.card: CardRequest` — `name`, `number`, `expiry` ("YYYY-MM"), `security_code`,
  `billing_address: Address(country_code req, address_line_1, admin_area_2, admin_area_1, postal_code)`;
  **or** `vault_id` (saved card). `attributes`/`stored_credential`/`experience_context`: omit → provider default
  (verification method stays the account's).
- `PaymentTokenRequest`: `payment_source: PaymentTokenRequestPaymentSource` **req** with
  `card: PaymentTokenRequestCard(name, number, expiry, security_code, billing_address)`; `customer: Customer(id=...)`
  set only once we hold the user's PayPal customer id (omit on first save → PayPal assigns one).
- `CaptureRequest`: `amount: Money`, `final_capture=True`. `ReauthorizeRequest`: `amount: Money`
  (docstring: "Supports only the amount request parameter"). `RefundRequest`: `amount: Money`; `note_to_payer` only when the caller gives a reason.
- `Money`: `currency_code: str` **req**, `value: str` **req**.

### Response members we assert on, and status → our state (every enum member mapped; else `unknown`)

- `Order` / `OrderAuthorizeResponse`: `id`, `status: OrderStatus` — `COMPLETED`→ read authorization;
  `CREATED`/`APPROVED`/`SAVED` → call `authorize_order`; `PAYER_ACTION_REQUIRED` → **stop**, failed with
  "requires shopper approval in a browser (not supported)"; `VOIDED` → failed; else unknown.
  `purchase_units[0].payments.authorizations[0]: AuthorizationWithAdditionalData` (`id`, `status`, `amount`,
  `expiration_time`, `create_time`). `payment_source.card: CardResponse` (`last_digits`, `brand`).
- `AuthorizationStatus` (`CREATED`→authorized, `PENDING`→pending, `DENIED`→failed(declined),
  `VOIDED`→voided, `CAPTURED`/`PARTIALLY_CAPTURED`→captured-elsewhere → needs_review; else unknown).
  `status_details.reason` (`PENDING_REVIEW`, `DECLINED_BY_RISK_FRAUD_FILTERS`) surfaced as text.
- `CapturedPayment`: `id`, `status: CaptureStatus` (`COMPLETED`→captured, `PENDING`→capture_pending,
  `DECLINED`/`FAILED`→failed, `PARTIALLY_REFUNDED`/`REFUNDED`→captured (refund state tracked separately); else unknown),
  `amount: Money`, `seller_receivable_breakdown: SellerReceivableBreakdown` (`gross_amount` **req**, `paypal_fee`, `net_amount`),
  `create_time`.
- `Refund`: `id`, `status: RefundStatus` (`COMPLETED`→completed, `PENDING`→pending, `FAILED`/`CANCELLED`→failed; else unknown),
  `amount`, `create_time`.
- `PaymentTokenResponse`: `id`, `customer.id`, `payment_source.card: CardPaymentTokenEntity`
  (`last_digits`, `brand`, `expiry`, `name`, `verification_status: CardVerificationStatus` `VERIFIED`/`FAILED`).
  No status field: `id` present → active; `verification_status == FAILED` → rejected (delete token, 422); missing id → unknown.
- `SearchResponse`: `transaction_details[].transaction_info: TransactionInformation` (`transaction_id`,
  `transaction_event_code`, `transaction_initiation_date`, `transaction_amount`, `fee_amount`,
  `transaction_status`, `invoice_id`, `custom_field`, `paypal_reference_id`), `page`, `total_pages`,
  `total_items`, `last_refreshed_datetime`.

UNVERIFIED (no source settles it; handled without relying on it): whether a reporting `transaction_id`
equals the authorization/capture/refund id → matcher falls back to `custom_field`; `page_size` maximum →
we use the default 100. PayPal's exact 422 issue codes for expired authorizations → we decide staleness
from the docstring's day counts (honor 3 days, reauthorize until day 29) and `expiration_time`, and
surface PayPal's `name`/`message`/`issue` verbatim if it still refuses.

## Error boundary (one place: `paypal_gateway.call()`)

`OAuthProviderError` → `ProviderConfigError` 502 · status 401/403 → 502 · 429 → 503 ·
typed `Error` with 400/404/409/422 → `ProviderRejected` (our view maps to 402 for payment declines,
409/422 otherwise, with `name`, `message`, `issue` codes, `debug_id`) · other → 502 ·
`ValidationError` on 2xx → `ProviderUnavailable(504, outcome_unknown=True)` ·
`ConnectError/ConnectTimeout/PoolTimeout/ProxyError` → 502 outcome known ·
`httpx.RequestError` → 504 outcome_unknown. 5xx on a write → unknown.

## CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| `vault_id` sent to `create_order` must be a token `create_payment_token` returned for **this user** and not deleted | `orders.create_order` ← `vault.create_payment_token` | `SavedCard` lookup by (user, status=active) |
| `authorization_id` for capture/void/reauthorize is the latest one PayPal returned for this order | `payments.*` ← `orders.create_order`/`authorize_order`/`reauthorize_payment` | `PayPalPayment.authorization_id` |
| `capture_id` for refund is the one `capture_authorized_payment` returned | `refund_captured_payment` ← `capture_authorized_payment` | `PayPalPayment.capture_id` |
| Σ refunds ≤ captured amount | `refund_captured_payment` ← `capture_authorized_payment` | atomic reservation UPDATE |
| captured amount = authorized amount = order total | capture ← create_order | echoed-amount checks → `needs_review` |
| `customer.id` reused on later saves is the one PayPal returned | `create_payment_token` ← `create_payment_token` | `PayPalCustomer` |

## Tests

Django `TestCase`s under `sandbox/apps/payments_api/tests/` run with pytest-django (`--ds=settings`,
`sandbox` on `pythonpath`), stub transport at `custom_http_client` (python-testing): token queued first,
assertions on the built request (method, path, `paypal-request-id` header, body wire keys), refused-status
and unlisted-status cases, 422 decline, `ConnectError` vs `ReadTimeout` (502/known vs 504/unknown),
bad credentials → config error, over-refund refused, idempotent refund key, ownership 404s, staff-only 403s,
reconciliation paging + window narrowing. Type check: `mypy --strict` over the new app's gateway module.

## Assumptions & Blockers

- Minor: the sandbox account returns `COMPLETED`+authorization for a single-step card order (if it
  returns `PAYER_ACTION_REQUIRED` we stop and report, per task).
- None blocking.

## REQUIRED READING (all loaded)

- MUST load python-error-handling — the ladder above, auth-error-first, two transport arms.
- MUST load python-configuration-resilience — claim rows, same-ref recovery, echoed-amount check, reconciliation clocks, bounded paging.
- MUST load python-client-initialization — lazy module client, own transport carries the timeout, close at exit.
- MUST load python-calling-endpoints — status-by-name mapping with `unknown` default; `delete_payment_token` returns `None`.
- MUST load python-models — `UNSET` vs `None`, open enums (`…OrStr`), `to_dict` at the boundary.
- MUST load python-authentication — settings default empty, check in `get_client()`, test placeholders.
- MUST load python-testing — stub transport, token first, lowercase header asserts.

## Verification record (2026-09-23)

- Live sandbox, test Visa: authorization of the exact order total (45.00), capture at fulfilment with
  PayPal fee 1.66 / net 43.34, partial refund 10.00 + remainder 35.00, over-refund refused, saved card
  vaulted and reused on a second order (authorized + captured), void on cancel, delete; ownership 404s.
  PayPal answered single-step card orders `COMPLETED` with the authorization inline (no payer action).
- 51 unit tests (stub transport at `custom_http_client`), `mypy --strict` clean on `gateway.py` and
  `paypal_calls.py`.
