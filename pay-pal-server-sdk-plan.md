# PayPal Server SDK — integration plan & contract sheet

Scope: add PayPal card payments (authorize → capture at fulfilment → void / refund), saved cards
(PayPal vault) and a reconciliation report to the django-oscar **sandbox** site, as a new Django app
`sandbox/apps/paypal_payments/`, routed under `/api/`.

## Toolchain & environment (surveyed, read-only)

| Fact | Value |
| --- | --- |
| Env manager | `py -3.11 -m venv venv` + `venv\Scripts\pip install -e .[test]` |
| SDK distribution | **`paypal`** (NOT `pay-pal-server-sdk` as `python-getting-started` says — drift, see Assumptions) installed from `git+https://github.com/context-plugins/paypal-python-sdk.git@main` (commit `0ed3d22`), version `2.29` |
| SDK import root | **`paypal`** (NOT `pay_pal_server_sdk`) — `PaypalClient` / `AsyncPaypalClient`, aliases `Client` / `AsyncClient` |
| SDK map | cloned beside the repo (`../sdk-src/sdk-map.md`, `map/operations/*.md`) — outside the project |
| Host app | Django 5.2 under WSGI, **sync** views; `ATOMIC_REQUESTS=True` in `sandbox/settings.py` |
| DB | SQLite (`sandbox/db.sqlite`), unique constraints available → the claim store |
| Project tests | `pytest` over `tests/` needs PostgreSQL (`tests/settings.py`) → baseline cannot run here (connection refused on :5432, no infra allowed). New tests run with `sandbox/manage.py test apps.paypal_payments` (SQLite). |
| Type checker | none configured → install `mypy` into the venv and run `mypy --strict` on the PayPal gateway modules (pure SDK code); Django modules checked non-strict with `--ignore-missing-imports` |
| Sandbox data | build steps from the task + **`oscar_import_catalogue` of the 3 `books.*.csv` files is REQUIRED** (task notes say optional; without it only 11 products load and `orders.json` fails with a FK error). With it: 209 products, 249 countries, 2 users, 1 order. |
| Catalogue currency | stock records are priced in GBP; per the task the numeric catalogue price is charged in `settings.PAYPAL_CURRENCY` |

## Conventions to imitate (pattern + ONE exemplar)

| Convention | Exemplar |
| --- | --- |
| Sandbox-local app code lives under `sandbox/apps/`, imported as `apps.<name>` | `sandbox/apps/sitemaps.py` (imported in `sandbox/urls.py` as `from apps.sitemaps import …`) |
| Oscar class loading via `get_model` / `get_class` | `src/oscar/apps/order/utils.py` |
| Order placement through `OrderCreator.place_order` with a basket | `src/oscar/apps/order/utils.py` |
| Payment bookkeeping via Oscar `Source` / `SourceType` / `Transaction` (`allocate`/`debit`/`refund`) | `src/oscar/apps/payment/abstract_models.py` |
| Settings read through `environ.Env` in `sandbox/settings.py` | `sandbox/settings.py` (`env.bool('DEBUG', …)`) |

## Decisions

- **Sync client** (`PaypalClient`) — Django under WSGI. One lazily-built, process-wide client
  (`gateway.get_client()`), built after fork on first use, closed via `atexit`. Never per request.
- **Base URL**: `PAYPAL_BASE_URL` if set, used verbatim (token endpoint moves with it — SDK derives
  `/v1/oauth2/token` from `base_url`). Otherwise `PAYPAL_ENVIRONMENT` must be `sandbox` →
  `https://api-m.sandbox.paypal.com` (the SDK map's only server). Any other environment without an
  explicit `PAYPAL_BASE_URL` → `ImproperlyConfigured` (the map declares no live host; we do not
  supply one from memory). Always passed explicitly — never the silent default.
- **Auth**: `oauth2=ClientCredentials(client_id=settings.PAYPAL_CLIENT_ID, client_secret=settings.PAYPAL_CLIENT_SECRET)`;
  both checked non-empty at client build (missing keyword = silent no-auth).
- **Timeout**: `timeout=20.0` client-wide. **No retries** anywhere (SDK performs none; we add none —
  unknown outcomes are settled by the same-reference check inside the safe write).
- **Logging**: `LoggingTransport` wrapping `HttpxClient(timeout=20.0)` logs method, URL path, status, ms —
  never headers or bodies (card data travels in bodies).
- **Response mode**: parsed calls everywhere except `vault.delete_payment_token` (returns `None`) →
  `with_raw_response` to observe the 204.
- **`prefer="return=representation"`** on every write (the default `return=minimal` omits
  `purchase_units[].payments` and `seller_receivable_breakdown`).
- **Pay = single-step create order** with `intent=AUTHORIZE` and `payment_source.card` (raw card or
  `vault_id`). Smoke-verified: status `COMPLETED` with `purchase_units[0].payments.authorizations[0]`
  status `CREATED`. `authorize_order` is therefore not called; `PAYER_ACTION_REQUIRED` is reported as a
  stop condition (3-DS challenge not supported, per task).
- **Stale authorization**: fulfil reads the authorization (`get_authorized_payment`), and when its age
  exceeds `PAYPAL_AUTH_HONOR_PERIOD_DAYS` (setting, default 3 — PayPal's own 422 text says
  reauthorization is allowed "from Day 4 to Day 29") it reauthorizes first, then captures the NEW
  authorization id. A capture refused with 422 also triggers one reauthorize attempt. A refused
  reauthorization or a passed `expiration_time` → 409 with an operator action.
- **Refund cap**: a local reservation (refund row inserted under a write lock on the payment row)
  guarantees Σ(non-failed refunds) ≤ captured; PayPal's `REFUND_AMOUNT_EXCEEDED` 422 is the second guard.
- **Refund idempotency key**: caller-supplied (`Idempotency-Key` header or `idempotencyKey` body field),
  scoped per order; same key + same amount → the original refund; same key + different amount → 422.
- **Saved cards**: `vault.create_payment_token` straight from card details (smoke-verified). We store
  only token id, PayPal customer id, brand, last digits, expiry. Delete = local soft delete first
  (immediately unusable), then `vault.delete_payment_token`.
- **Reconciliation**: `transaction_search.search_transactions` over ≤31-day chunks (docstring: "The
  maximum supported range is 31 days"), every page up to `total_pages`, filtered back to the exact
  instants on `transaction_initiation_date`; matched by PayPal transaction id against the capture /
  refund ids we store with their provider time (same clock on both sides).
- **Views** are `transaction.non_atomic_requests` so a claim commits before the provider call.
- **CSRF stays on** (session auth). `GET /api/session` sets the CSRF cookie and reports the user;
  `CSRF_FAILURE_VIEW` answers JSON under `/api/` (Django's page elsewhere).
- **Reconciliation lag**: local writes newer than PayPal's `last_refreshed_datetime` are reported as
  `awaitingPayPalReporting`, not as `localOnly` discrepancies.

## Contract sheet (every row from the SDK map / source; no open lookups)

Common to every call below: sync parsed signature on `PaypalClient`; everything after `*` is
keyword-only with a real default (no defensive `None`s); trailing `request_options`; each has a
`with_raw_response` peer; async peers identical (not used). Auth `oauth2`. Base URL rule above.

| Operation | Signature (positional ∣ keyword-only we set) | Returns | `ApiError.error` union (status → arm) | Members we must assert on |
| --- | --- | --- | --- | --- |
| `client.orders.create_order` `POST /v2/checkout/orders` | `body: OrderRequest\|OrderRequestDict` ∣ `pay_pal_request_id`, `prefer` | `Order` | `CreateOrderErrorBody` = `Error` [400,401,422] ∣ `RawError` | `id`, `status` (OrderStatus), `purchase_units[0].payments.authorizations[0]` → `id`, `status` (AuthorizationStatus), `amount.value`/`currency_code`, `create_time`, `expiration_time`; `payment_source.card.brand`/`last_digits` |
| `client.payments.get_authorized_payment` `GET /v2/payments/authorizations/{authorization_id}` | `authorization_id` | `PaymentAuthorization` | `GetAuthorizedPaymentErrorBody` = `Error` [401,403,404] ∣ `RawError` [500, other] | `status`, `expiration_time`, `create_time` |
| `client.payments.reauthorize_payment` `POST …/{authorization_id}/reauthorize` | `authorization_id` ∣ `pay_pal_request_id`, `prefer`, `body: ReauthorizeRequest` | `PaymentAuthorization` | `ReauthorizePaymentErrorBody` = `Error` [400,401,403,404,422] ∣ `RawError` [500, other] | `id` (NEW auth id), `status`, `amount`, `create_time`, `expiration_time` |
| `client.payments.capture_authorized_payment` `POST …/{authorization_id}/capture` | `authorization_id` ∣ `pay_pal_request_id`, `prefer`, `body: CaptureRequest` | `CapturedPayment` | `CaptureAuthorizedPaymentErrorBody` = `Error` [400,401,403,404,409,422] ∣ `RawError` [500, other] | `id`, `status` (CaptureStatus), `amount`, `seller_receivable_breakdown.gross_amount` (required member) / `paypal_fee` / `net_amount`, `create_time` |
| `client.payments.get_captured_payment` `GET /v2/payments/captures/{capture_id}` | `capture_id` | `CapturedPayment` | `GetCapturedPaymentErrorBody` = `Error` [401,403,404] ∣ `RawError` [500, other] | as capture |
| `client.payments.void_payment` `POST …/{authorization_id}/void` | `authorization_id` ∣ `pay_pal_request_id`, `prefer` | `PaymentAuthorization` | `VoidPaymentErrorBody` = `Error` [401,403,404,409,422] ∣ `RawError` [500, other] | `status`, `update_time` |
| `client.payments.refund_captured_payment` `POST /v2/payments/captures/{capture_id}/refund` | `capture_id` ∣ `pay_pal_request_id`, `prefer`, `body: RefundRequest` | `Refund` | `RefundCapturedPaymentErrorBody` = `Error` [400,401,403,404,409,422] ∣ `RawError` [500, other] | `id`, `status` (RefundStatus), `amount`, `create_time` |
| `client.vault.create_payment_token` `POST /v3/vault/payment-tokens` | `body: PaymentTokenRequest\|Dict` ∣ `pay_pal_request_id` | `PaymentTokenResponse` | `CreatePaymentTokenErrorBody` = `Error` [400,403,404,422,500] ∣ `RawError` | `id`, `customer.id`, `payment_source.card.brand`/`last_digits`/`expiry`/`verification_status` |
| `client.vault.delete_payment_token` `DELETE /v3/vault/payment-tokens/{id}` | `id` | **`None`** → use `with_raw_response` (`ApiResult[None, DeletePaymentTokenErrorBody]`) | `DeletePaymentTokenErrorBody` = `Error` [400,403,500] ∣ `RawError` | `Success.response.status_code` |
| `client.transaction_search.search_transactions` `GET /v1/reporting/transactions` | `start_date: str`, `end_date: str` (RFC 3339, seconds required) ∣ `page_size` (default 100), `page` (default 1); defaults `fields="transaction_info"`, `balance_affecting_records_only="Y"` left as is | `SearchResponse` | **Case B** — always `RawError` | `transaction_details[].transaction_info` → `transaction_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_amount`, `fee_amount`, `transaction_status`, `invoice_id`, `custom_field`; `total_pages`, `last_refreshed_datetime` |

Header wire names: `pay_pal_request_id` → `PayPal-Request-Id`; `prefer` → `Prefer`.
Key retention (docstrings): create_order 6 h; capture/reauthorize/void/refund 45 days; create_payment_token 3 h.

### Request models / members set (required vs `UNSET`)

| Model (module) | Members we set | Required? |
| --- | --- | --- |
| `OrderRequest` (`models/order_request.py`) | `intent` = `CheckoutPaymentIntent.AUTHORIZE`; `purchase_units`; `payment_source` | intent, purchase_units required; payment_source `UNSET`-optional |
| `PurchaseUnitRequest` | `reference_id`=order number, `invoice_id`=claim reference, `custom_id`=`<prefix>:<order number>`, `amount` | amount required; rest optional — **reference fields always set** |
| `AmountWithBreakdown` / `Money` | `currency_code: str`, `value: str` (Decimal formatted to currency exponent) | both required |
| `PaymentSource` → `card: CardRequest` | raw: `name`, `number`, `expiry` (`YYYY-MM`), `security_code`, `billing_address: Address`; saved: `vault_id` | all `UNSET`-optional |
| `Address` | `address_line_1`, `address_line_2`, `admin_area_2`, `admin_area_1`, `postal_code`, **`country_code` (required)** | |
| `CaptureRequest` | `amount: Money`, `final_capture=True` | optional |
| `ReauthorizeRequest` | `amount: Money` | optional |
| `RefundRequest` | `amount: Money` | optional |
| `PaymentTokenRequest` | `customer: Customer` (`id` or `merchant_customer_id`), `payment_source: PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(name, number, expiry, security_code, billing_address))` | payment_source required |

`Optional[T]` here is `T | UnsetType`: never pass `None`; omit instead. No member we set is typed bare
`Any`. Response enums are open (`…OrStr`): an unknown string → `unknown`.

### Status enums (from `models/enums/`) → outcome

| Enum | done | pending (not-yet) | failed | unknown |
| --- | --- | --- | --- | --- |
| `OrderStatus` (create_order) | `COMPLETED` *(only with a readable authorization — the authorization status decides)* | `CREATED`, `SAVED`, `APPROVED`, `PAYER_ACTION_REQUIRED` (→ stop & report) | `VOIDED` | anything else / absent |
| `AuthorizationStatus` for **authorize / reauthorize** steps | `CREATED` | `PENDING` | `DENIED`, `VOIDED` | `CAPTURED`, `PARTIALLY_CAPTURED`, unlisted, absent |
| `AuthorizationStatus` for **void** step | `VOIDED` | `PENDING`, `CREATED` | `CAPTURED`, `PARTIALLY_CAPTURED` | `DENIED` → unknown (not our undo), unlisted, absent |
| `CaptureStatus` (capture) | `COMPLETED` | `PENDING` | `DECLINED`, `FAILED`, `REFUNDED`, `PARTIALLY_REFUNDED` (done-then-undone) | unlisted, absent |
| `RefundStatus` (refund) | `COMPLETED` | `PENDING` | `FAILED`, `CANCELLED` | unlisted, absent |
| `CardVerificationStatus` (vault create; optional member) | `VERIFIED` or absent-with-token-id | — | `FAILED` | unlisted |

### Error ladder (one place, `errors.py`)

`OAuthProviderError` → 502 config; 401/403 → 502; 429 → 503; `Error` arm with 400/404/409/422 →
caller's (422/404/409 with PayPal `issue`/`description`); 5xx & unmapped → 502 (a 5xx on a write goes
to the safe write's lookup first → 504 if still unknown); `ValidationError`/`ValueError` on 2xx →
unknown (504); `ConnectError`/`ConnectTimeout`/`PoolTimeout`/`ProxyError` → 502, outcome known;
other `httpx.RequestError` → 504, `outcome_unknown`.

## OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| create_order (authorize at pay) | `Order.status` then `purchase_units[0].payments.authorizations[0].status` | done: auth `CREATED` (order `COMPLETED`) → payment `authorized`, 200. pending: auth `PENDING`; order `CREATED`/`SAVED`/`APPROVED` → payment `authorizing`, 202 (a repeat pay settles it by `get_order`). `PAYER_ACTION_REQUIRED` → failed w/ `payer_action_required`, 402 + stop report. failed: auth `DENIED`/`VOIDED`, order `VOIDED` → payment back to `awaiting_payment` (new attempt allowed), 402. unknown: auth `CAPTURED`/`PARTIALLY_CAPTURED`, unlisted, absent → op `unknown`, payment `authorizing`, 202; echoed amount ≠ total → `needs_review`, 409 | `sandbox/apps/paypal_payments/gateway.py` `read_authorized_order` → `authorization_outcome` / `order_outcome`; branches in `sandbox/apps/paypal_payments/services.py` `pay` (inner `on_complete`, `payer_action_required` / `payment_declined` raises) |
| reauthorize_payment | `PaymentAuthorization.status` | done `CREATED` → new auth id stored, continue to capture. pending `PENDING` → 202, no capture. failed `DENIED`/`VOIDED` → 409 operator action "cannot renew". unknown (`CAPTURED`, `PARTIALLY_CAPTURED`, unlisted, absent) → 202 unknown | `sandbox/apps/paypal_payments/gateway.py` `read_authorization` → `authorization_outcome`; branches in `sandbox/apps/paypal_payments/services.py` `_reauthorize` (`_not_renewable` on FAILED / refusal, `_Accepted` on pending/unknown) |
| capture_authorized_payment | `CapturedPayment.status` | done `COMPLETED` → payment `captured`, gross/fee/net stored, order Complete, 200. pending `PENDING` → payment `capturing`, 202 (later fulfil re-reads via get_captured_payment). failed `DECLINED`/`FAILED`/`REFUNDED`/`PARTIALLY_REFUNDED` → payment back to `authorized`, 402 `capture_failed`. unknown → 202 | `sandbox/apps/paypal_payments/gateway.py` `read_capture` → `capture_outcome`; branches in `sandbox/apps/paypal_payments/services.py` `_capture` (inner `on_complete`; `capture_failed` 402 / `_Accepted` 202) |
| void_payment | `PaymentAuthorization.status` | done `VOIDED` → payment `voided`, order Cancelled, 200. pending `PENDING`/`CREATED` → 202. failed `CAPTURED`/`PARTIALLY_CAPTURED` → payment `needs_review`, 409 `void_refused`. unknown (`DENIED`, unlisted, absent) → 202 | `sandbox/apps/paypal_payments/gateway.py` `read_authorization(for_void=True)` → `void_outcome`; branches in `sandbox/apps/paypal_payments/services.py` `cancel` (inner `on_complete`; FAILED → `needs_review` + 409 `void_refused`) |
| refund_captured_payment | `Refund.status` | done `COMPLETED` → refund `done`, refunded total updated, 201. pending `PENDING` → 202 (amount stays reserved). failed `FAILED`/`CANCELLED` → reservation released, 402. unknown → 202, reservation kept | `sandbox/apps/paypal_payments/gateway.py` `read_refund` → `refund_outcome`; `sandbox/apps/paypal_payments/services.py` `refund` (inner `on_complete`, `_update_refund_totals`); HTTP mapping in `sandbox/apps/paypal_payments/views.py` `refunds` |
| create_payment_token | `PaymentTokenResponse.id` + `payment_source.card.verification_status` | done: id present and verification not `FAILED` → saved card created, 201. failed: `FAILED` → token deleted at PayPal, 402. unknown: no id / unlisted verification → 202 unknown, no saved card | `sandbox/apps/paypal_payments/gateway.py` `read_payment_token` → `vault_outcome`; `sandbox/apps/paypal_payments/services.py` `save_card` (FAILED → `_delete_token_at_paypal` + 402); `sandbox/apps/paypal_payments/views.py` `payment_methods` (202 when not done) |
| delete_payment_token | HTTP status (returns `None`) | done 204 (or 404 = already gone) → `provider_deleted=True`. other failure → card stays locally deleted, `provider_deleted=False`, 202; a repeat DELETE retries | `sandbox/apps/paypal_payments/services.py` `_delete_token_at_paypal` (`with_raw_response`, 204/404 = gone) and `delete_card`; `sandbox/apps/paypal_payments/views.py` `payment_method` (204 / 202 pending) |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| create_order (pay) | `PayPalOperation` row, `reference = <prefix>-<order#>-auth-<attempt>` | DB `UNIQUE(reference)` → `IntegrityError` | `try_claim` returns False → loser answers from the stored record | `sandbox/apps/paypal_payments/models.py` `PayPalOperation.reference` (unique); `sandbox/apps/paypal_payments/safe_write.py` `try_claim`, called from `safe_write` in `sandbox/apps/paypal_payments/services.py` `pay` (plus the `_move` compare-and-set AWAITING→AUTHORIZING) |
| reauthorize_payment | `PayPalOperation`, `reference = <prefix>-<order#>-reauth-<old auth id>` | DB `UNIQUE(reference)` | `try_claim` | `sandbox/apps/paypal_payments/safe_write.py` `try_claim` / `safe_write` loser branch, called from `sandbox/apps/paypal_payments/services.py` `_reauthorize` |
| capture_authorized_payment | `PayPalOperation`, `reference = <prefix>-<order#>-capture-<auth id>` | DB `UNIQUE(reference)` | `try_claim` | `sandbox/apps/paypal_payments/safe_write.py` `try_claim` / `safe_write` loser branch, called from `sandbox/apps/paypal_payments/services.py` `_capture` (plus `fulfil`'s `_move` AUTHORIZED→CAPTURING) |
| void_payment | `PayPalOperation`, `reference = <prefix>-<order#>-void-<auth id>` | DB `UNIQUE(reference)` | `try_claim` | `sandbox/apps/paypal_payments/safe_write.py` `try_claim` / `safe_write` loser branch, called from `sandbox/apps/paypal_payments/services.py` `cancel` (plus `_move` AUTHORIZED→VOIDING) |
| refund_captured_payment | `PayPalRefund` row (`UNIQUE(order_payment, idempotency_key)`) + `PayPalOperation`, `reference = <prefix>-<order#>-refund-<sha256(key)[:24]>` | DB unique constraints | refund reservation catches `IntegrityError` → replay path; `try_claim` | `sandbox/apps/paypal_payments/models.py` `PayPalRefund` constraint `paypal_refund_unique_key` + unique `reference`; `sandbox/apps/paypal_payments/services.py` `_reserve_refund` (IntegrityError → 409 in_progress; existing key → replay); then `safe_write.try_claim` |
| create_payment_token | `PayPalOperation`, `reference = <prefix>-u<user id>-vault-<key>` (key = `Idempotency-Key` header, else server-generated per request) | DB `UNIQUE(reference)` | `try_claim` | `sandbox/apps/paypal_payments/safe_write.py` `try_claim` / `safe_write` loser branch, called from `sandbox/apps/paypal_payments/services.py` `save_card`; answer from `SavedCard.operation_reference` |
| delete_payment_token | none needed — delete by id is harmless to repeat; local soft delete is idempotent | n/a | n/a | `sandbox/apps/paypal_payments/services.py` `delete_card` (conditional `update(deleted_at=…)` filtered on `deleted_at__isnull`) |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| create_order (pay) | same-reference resend (`PayPal-Request-Id` → PayPal returns the original order; smoke-verified), only within the 6 h key window; beyond it → stays `unknown` for an operator | `<prefix>-<order#>-auth-<attempt>` | `sandbox/apps/paypal_payments/safe_write.py` `safe_write` — the `if result is None:` same-reference resend, and the `checking` branch gated by `_may_resend(existing, KEY_WINDOW_CREATE_ORDER)`; `_settle_from_read` (get_order) for a stored pending |
| reauthorize_payment | same-reference resend (45-day key window) | `<prefix>-<order#>-reauth-<old auth id>` | `sandbox/apps/paypal_payments/safe_write.py` `safe_write` same-reference resend (`KEY_WINDOW_PAYMENTS`), from `sandbox/apps/paypal_payments/services.py` `_reauthorize` |
| capture_authorized_payment | same-reference resend (45-day window; smoke-verified returns original capture) | `<prefix>-<order#>-capture-<auth id>` | `sandbox/apps/paypal_payments/safe_write.py` `safe_write` same-reference resend (`KEY_WINDOW_PAYMENTS`); `_settle_from_read` via `get_captured_payment` for a pending capture (`sandbox/apps/paypal_payments/services.py` `_capture`) |
| void_payment | same-reference resend (45-day window) | `<prefix>-<order#>-void-<auth id>` | `sandbox/apps/paypal_payments/safe_write.py` `safe_write` same-reference resend; `_settle_from_read` via `get_authorized_payment` (`sandbox/apps/paypal_payments/services.py` `cancel`) |
| refund_captured_payment | same-reference resend (45-day window; smoke-verified returns original refund) | `<prefix>-<order#>-refund-<hash>` | `sandbox/apps/paypal_payments/safe_write.py` `safe_write` same-reference resend; `_settle_from_read` via `get_refund` (`sandbox/apps/paypal_payments/services.py` `refund`, reservation kept while unknown) |
| create_payment_token | same-reference resend (3 h window); beyond → stays `unknown` | `<prefix>-u<user>-vault-<key>` | `sandbox/apps/paypal_payments/safe_write.py` `safe_write` same-reference resend (`KEY_WINDOW_VAULT`), from `sandbox/apps/paypal_payments/services.py` `save_card` |
| delete_payment_token | repeat the delete (idempotent by id; 404 = gone) | token id | `sandbox/apps/paypal_payments/services.py` `delete_card` → `_delete_token_at_paypal`, re-run by a repeated DELETE in `sandbox/apps/paypal_payments/views.py` `payment_method` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| create_order (pay) | Oscar `Order` + `OrderPayment(state=awaiting_payment, attempt=n)` + `PayPalOperation(ref, outcome=sending, amount, currency)` | op outcome/provider id/status/time; `OrderPayment` paypal order id, auth id/status/expiry, card brand/last4; Oscar `Source.allocate` | before: `sandbox/apps/paypal_payments/services.py` `place_order` (OrderPayment) + `sandbox/apps/paypal_payments/safe_write.py` `try_claim`; after: `safe_write._record` → `complete` + `pay`'s `on_complete` (incl. `_oscar_source(...).allocate`) |
| reauthorize_payment | `PayPalOperation(ref, sending)` | op outcome; `OrderPayment.authorization_id` ← new id, `authorized_at`, expiry | before: `sandbox/apps/paypal_payments/safe_write.py` `try_claim`; after: `_record` → `complete` + `sandbox/apps/paypal_payments/services.py` `_reauthorize` `on_complete` |
| capture_authorized_payment | `PayPalOperation(ref, sending)` | op outcome; capture id/status, gross/fee/net; Oscar `Source.debit`; order status Complete | before: `sandbox/apps/paypal_payments/safe_write.py` `try_claim`; after: `_record` → `complete` + `sandbox/apps/paypal_payments/services.py` `_capture` `on_complete` (`debit`, `_advance_order_status`) |
| void_payment | `PayPalOperation(ref, sending)` | op outcome; payment `voided`; Oscar `Transaction(Void)`; order Cancelled | before: `sandbox/apps/paypal_payments/safe_write.py` `try_claim`; after: `_record` → `complete` + `sandbox/apps/paypal_payments/services.py` `cancel` `on_complete` (Void `Transaction`, order Cancelled) |
| refund_captured_payment | `PayPalRefund(key, amount, outcome=sending)` (the reservation) + `PayPalOperation(ref, sending)` | refund id/status/time; payment refunded total/state; Oscar `Source.refund` | before: `sandbox/apps/paypal_payments/services.py` `_reserve_refund` + `sandbox/apps/paypal_payments/safe_write.py` `try_claim`; after: `_record` → `complete` + `refund` `on_complete` (`Source.refund`, `_update_refund_totals`) |
| create_payment_token | `PayPalOperation(ref, sending)` (no card data) | op outcome; `SavedCard(token id, customer id, brand, last4, expiry)` | before: `sandbox/apps/paypal_payments/safe_write.py` `try_claim` (no card data); after: `_record` → `complete` + `sandbox/apps/paypal_payments/services.py` `save_card` `on_complete` (`SavedCard.get_or_create`) |
| delete_payment_token | `SavedCard.deleted_at` set (card unusable) | `SavedCard.provider_deleted=True` | before: `sandbox/apps/paypal_payments/services.py` `delete_card` sets `deleted_at`; after: `provider_deleted=True` in `delete_card` |

## Assumptions & Blockers

- **Minor (skill drift):** `python-getting-started` names the distribution `pay-pal-server-sdk` and import
  root `pay_pal_server_sdk`; the actual SDK at the documented repo/branch is distribution `paypal`,
  import root `paypal`, client `PaypalClient`. The SDK map (`sdk-map.md`) agrees with the installed
  package; we follow the map. Not a capability gap.
- **Minor:** catalogue prices are GBP; charged numerically in `PAYPAL_CURRENCY` (task: "Amounts come from
  catalogue prices; the currency comes from configuration"). Orders whose total has more decimals than
  the currency allows are rejected at order creation.
- **Minor:** live host not declared by the SDK → non-sandbox environments require `PAYPAL_BASE_URL`.
- **Minor:** honor period (3 days) is a setting, sourced from PayPal's own 422 description observed in
  smoke (`REAUTHORIZATION_TOO_SOON`: "only allowed once from Day 4 to Day 29").
- No blockers: every flow was smoke-verified against the sandbox (authorize, vault create, pay with
  vault id, idempotent replay, capture with fee breakdown, partial refund + replay, over-refund 422,
  vault delete 204, search_transactions paging fields).

## REQUIRED READING

- MUST load `python-error-handling` — error ladder, transport split, OAuthProviderError first. (loaded)
- MUST load `python-client-initialization` — sync client, lifetime, transport wrapper. (loaded)
- MUST load `python-configuration-resilience` — safe write (claim/call/check/verify/complete), no retries, reconciliation clocks. (loaded)
- MUST load `python-testing` — StubTransport seam, token-first request, both transport failure kinds. (loaded)
- MUST load `python-calling-endpoints` — status_from_provider allow-list, `-> None` delete, `prefer` default narrowing. (loaded)
- MUST load `python-models` — `UNSET` vs `None`, open enums, money as `Decimal` strings per currency exponent. (loaded)
- MUST load `python-authentication` — `oauth2=` must be set; lazy token fetch failure mode. (loaded)
