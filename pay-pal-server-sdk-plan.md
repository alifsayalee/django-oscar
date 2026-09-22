# PayPal Server SDK (Python) — Integration plan & contract sheet

Adds PayPal card payments + saved cards to the django-oscar **sandbox** site as a new Django app
(`sandbox/apps/paypal_api`), reusing Oscar's `Order`/`Line` and `payment.Source`/`Transaction`
models. All PayPal traffic goes through the installed `paypal` SDK (import root `paypal`,
distribution builds as `paypal`; the getting-started snapshot's `pay_pal_server_sdk` name is a
stale earlier release — installed source is ground truth).

## Toolchain / environment (verified)
- Env manager: `pip` + venv at `repo/.venv` (Python 3.11). Project installed `-e .[test]`.
- SDK installed from `git+https://github.com/context-plugins/paypal-python-sdk.git@main`
  (installs as distribution `paypal`; the `pay-pal-server-sdk @` spec fails on a name mismatch —
  use the bare git URL). Import root: `paypal`; client `PaypalClient`/alias `Client`.
- No DRF in the project → plain Django views + `JsonResponse`.
- Django is **WSGI/sync** → use the **sync** `Client`; hold one module-level, close via `atexit`.
- Tests/checks: `pytest` (project `.[test]`); type check with `mypy` (install into venv, run on
  touched files — SDK ships `py.typed`, generated under `mypy --strict`).
- DB: SQLite, seeded (209 products, 249 countries, users `superuser`/`staff`). Purchasable
  stockrecords: products **9, 10** (stock 100/200, price 15.00). Catalogue currency is GBP.

## Credentials & config (settings.py, names only — never values)
Read via `django-environ` in `sandbox/settings.py`:
- `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` — OAuth2 client-credentials.
- `PAYPAL_ENVIRONMENT` (`sandbox`|`live`), `PAYPAL_CURRENCY` (e.g. `USD`).
- `PAYPAL_BASE_URL` — **optional override**. If set (non-empty) → pass verbatim as `base_url` for
  every call incl. the token fetch (SDK derives the token endpoint from base_url). If empty →
  derive: `sandbox`→`https://api-m.sandbox.paypal.com`, `live`→`https://api-m.paypal.com`.
  (SDK default base_url is the sandbox host, but we always pass it explicitly.)
- Amount decoupling: **amount = Oscar `order.total_incl_tax`** (catalogue prices); **currency =
  `PAYPAL_CURRENCY`** (config), per task ("amounts come from catalogue prices; the currency comes
  from configuration"). Values formatted with `Decimal.quantize` to the currency's exponent
  (USD/GBP/EUR=2; JPY/KRW=0; BHD/KWD/OMR/TND=3; default 2). Never `%f`/`round`.

## Smoke against real sandbox (DONE, reversible) — findings that shape the build
- **Direct card + `intent=AUTHORIZE` AUTO-AUTHORIZES at `create_order`** (order status
  `COMPLETED`, authorization embedded at `purchase_units[0].payments.authorizations[0]`, status
  `CREATED`). Calling `authorize_order` afterwards → 422 `ORDER_ALREADY_AUTHORIZED`. **So the
  `/pay` step is a single `create_order` call; read the embedded authorization; only fall back to
  `authorize_order` if none is embedded and order status is `APPROVED`.**
- Paying with a vaulted card (`payment_source.card.vault_id`) behaves identically (auto-authorize).
- `capture_authorized_payment` → `CapturedPayment` with `seller_receivable_breakdown`
  {`gross_amount`, `paypal_fee`, `net_amount`} (verified 12.34 → fee 0.81 → net 11.53).
- `refund_captured_payment` partial refund → `Refund` status `COMPLETED`.
- `vault.create_payment_token` → `PaymentTokenResponse` id + `payment_source.card`
  {`brand`=VISA, `last_digits`=1111, `expiry`}.
- **`void_payment` returns 204 No Content by default → SDK decoder raises `ValueError` (decode
  failure, not ApiError).** Pass `prefer="return=representation"` → 200 + JSON body. Same guard
  applies to any op whose default `prefer=return=minimal` yields an empty 2xx.
- `search_transactions` over a 25-day window → total_pages 12 → **MUST paginate all pages**.
- Test card 4111 1111 1111 1111 completed with **no 3DS challenge** — no browser round-trip needed.

## Architecture
```
sandbox/apps/paypal_api/
  apps.py            AppConfig (name='apps.paypal_api', label='paypal_api')
  money.py           currency-exponent formatting (Decimal → str; parse str → Decimal)
  paypal_client.py   lazy module-level sync Client from settings; atexit close
  gateway.py         thin PayPal wrapper: one method per SDK op, error translation to domain
                     exceptions, prefer=representation, PayPal-Request-Id plumbing, UNSET guards
  exceptions.py      PaymentError hierarchy (Rejected/Config/Unavailable/Unreadable/Conflict/
                     ChallengeRequired) with an HTTP status each
  models.py          SavedCard, PayPalPayment, PayPalRefund (sidecars; Order/Line/Source reused)
  services.py        domain orchestration (create_order, authorize/pay, fulfil, cancel, refund,
                     save_card, list/delete card, my_orders, reconciliation) — all DB-atomic,
                     idempotent, ownership-scoped
  views.py           plain Django JSON views; session auth; @staff for operator ops
  urls.py            /api/... routes
  migrations/0001_initial.py
```
Wired: `INSTALLED_APPS += 'apps.paypal_api.apps.PayPalApiConfig'`; `sandbox/urls.py` gets
`path('api/', include('apps.paypal_api.urls'))` OUTSIDE `i18n_patterns` (no language prefix).

### Data model (sidecars for PayPal-owned state; not a parallel order set)
- `SavedCard(user FK, vault_id unique, brand, last4, expiry 'YYYY-MM', label, created)` — one
  shopper's vaulted card. No PAN/CVV ever stored.
- `PayPalPayment(order OneToOne→order.Order, paypal_order_id, authorization_id, capture_id,
  status, currency, amount, captured_amount=0, refunded_amount=0, paypal_fee null, net_amount null,
  saved_card FK null, source FK→payment.Source null, created, updated)`.
  `status ∈ {AWAITING_PAYMENT, AUTHORIZED, CAPTURED, PARTIALLY_REFUNDED, REFUNDED, CANCELLED,
  FAILED}`. Authoritative money state (mirrors PayPal); also mirrored into Oscar `Source`/
  `Transaction` (allocate on authorize, debit on capture, refund on refund) for reuse/visibility.
- `PayPalRefund(payment FK, refund_id, amount, currency, status, idempotency_key, created)`,
  `unique_together=(payment, idempotency_key)` — refund idempotency + partial-refund ledger.

### Ownership & auth
- Session auth via `request.user`. Shopper endpoints act only on `request.user`'s own rows
  (SavedCard.user / PayPalPayment.order.user). Operator ops (fulfil, cancel, reconciliation)
  require `is_staff`. 401 if anonymous, 403 if not owner/not staff, 404 never leaks others' ids
  (scope querysets by user).

### Idempotency (task: no double auth/capture; refunds carry caller key)
- `create_order`/pay: `select_for_update` on PayPalPayment row; if already `AUTHORIZED`/beyond →
  return existing (no 2nd auth). Deterministic `PayPal-Request-Id = f"auth-{order.number}"` so even
  a racing 2nd call is deduped by PayPal.
- capture/fulfil: gate on status; if already `CAPTURED`+ → no-op return. `PayPal-Request-Id =
  f"capture-{authorization_id}"`.
- cancel/void: if already `CANCELLED` → no-op (do NOT re-fire). `PayPal-Request-Id =
  f"void-{authorization_id}"`.
- refund: **caller-supplied `idempotencyKey`**; claim `PayPalRefund(payment,key)` FIRST (unique
  constraint) → on IntegrityError return the existing refund (no 2nd refund). Send key as
  `PayPal-Request-Id`. Two different keys = two legit partial refunds. Cap:
  `refunded_amount + amount ≤ captured_amount` (enforced before provider call).

### Stale-authorization renewal (fulfil)
On capture, if PayPal rejects with an expired/voided-authorization issue (422/404, issue name
contains `EXPIRED`/`AUTHORIZATION` cannot capture), call `reauthorize_payment(auth_id)` → new
authorization id → retry capture once. If reauthorize itself fails (can no longer renew) → surface
`PaymentConflict` with an operator-actionable message (re-authorization no longer possible; a new
payment is required). Renewal updates `authorization_id`.

### Reconciliation (operator; whole range)
`search_transactions(start,end)` paginated over **all** `total_pages` (bounded by a MAX_PAGES
backstop; result carries `truncated` if the backstop trips). Match PayPal txns to app orders by
`invoice_id` == order.number (also set `custom_id`=order.number). Report per-order matched set
(one order ↔ many txns: auth/capture/refund) + `paypal_only` (in PayPal, not app) + `app_only`
(app payments in range with no PayPal txn — expected-empty when reporting lags; documented, not a
gap). Filter app side on `PayPalPayment.created` (same clock intent). ISO date-times passed
through, so no whole-day widening needed.

---

## CONTRACT SHEET (all facts from SDK map + source modules under `paypal/`)

**Client** (sync): `Client(*, base_url=<computed>, timeout=<set>, oauth2=ClientCredentials(
client_id, client_secret))`. Keyword-only. `oauth2` may be a dict. Token fetched lazily on 1st
call from `<base_url>/v1/oauth2/token`, cached on the client → client must be long-lived. Base URL
omitted ⇒ SDK default `https://api-m.sandbox.paypal.com` (we always pass explicit). `close()` on
shutdown. `.with_raw_response.<op>` peer returns `ApiResult` (Success/Failure) instead of raising —
used where we need the status code (void/delete `-> ...`/None).

**Response mode:** parsed (raises `ApiError`) by default; use `with_raw_response` for void
(observe 200) and delete (observe 204).

**Keyword-only boundary:** every op takes required params positionally (path, then body where
required-positional), everything after `*` is keyword-only with real defaults (no defensive
`None`s). `prefer` defaults to `"return=minimal"` — we pass `"return=representation"` on
create/authorize/capture/refund/void to get full/any body.

### Operations in scope

| Op | Call | Body (model/dict) | Returns | Notes |
|---|---|---|---|---|
| `client.orders.create_order` | `create_order(body, *, pay_pal_request_id=, prefer=)` | `OrderRequest`/dict — **required** `intent` (`"AUTHORIZE"`), **required** `purchase_units: list` (each **required** `amount: AmountWithBreakdown` {currency_code, value}; opt `custom_id`, `invoice_id`), opt `payment_source: {card: CardRequest}` | `Order` | direct card → auto-authorize; read `purchase_units[0].payments.authorizations[0].{id,status}` |
| `client.orders.authorize_order` | `authorize_order(id, *, pay_pal_request_id=, prefer=)` | `OrderAuthorizeRequest`/None | `OrderAuthorizeResponse` | fallback only if no embedded auth & status `APPROVED` |
| `client.payments.capture_authorized_payment` | `capture_authorized_payment(authorization_id, *, pay_pal_request_id=, prefer=, body=)` | `CaptureRequest`/dict — opt `amount: Money`, opt `final_capture: bool` | `CapturedPayment` | read `id`, `status`(`COMPLETED`), `seller_receivable_breakdown.{gross_amount,paypal_fee,net_amount}: Money` |
| `client.payments.reauthorize_payment` | `reauthorize_payment(authorization_id, *, pay_pal_request_id=, prefer=, body=)` | `ReauthorizeRequest`/dict — opt `amount: Money` | `PaymentAuthorization` | new auth id/status on stale-auth renewal |
| `client.payments.void_payment` | `with_raw_response.void_payment(authorization_id, *, prefer="return=representation", pay_pal_request_id=)` | — | `ApiResult[PaymentAuthorization,…]` | default 204 → decode ValueError; representation → 200 |
| `client.payments.refund_captured_payment` | `refund_captured_payment(capture_id, *, pay_pal_request_id=, prefer=, body=)` | `RefundRequest`/dict — opt `amount: Money`, opt `custom_id`, `invoice_id` | `Refund` | omit `amount` = full refund; read `id`, `status` |
| `client.payments.get_authorized_payment` | `get_authorized_payment(authorization_id, *)` | — | `PaymentAuthorization` | status re-check |
| `client.vault.create_payment_token` | `create_payment_token(body, *, pay_pal_request_id=)` | `PaymentTokenRequest`/dict — **required** `payment_source: PaymentTokenRequestPaymentSource` {`card: PaymentTokenRequestCard`{name,number,expiry,security_code,billing_address}}, opt `customer: {id}` | `PaymentTokenResponse` | read `id`, `payment_source.card: CardPaymentTokenEntity`{`brand`,`last_digits`,`expiry`} |
| `client.vault.delete_payment_token` | `with_raw_response.delete_payment_token(id, *)` | — | `ApiResult[None,…]` | 204; treat 404 as already-gone |
| `client.vault.list_customer_payment_tokens` | `list_customer_payment_tokens(customer_id, *, page_size=, page=)` | — | `CustomerVaultPaymentTokensResponse` | (optional; we track locally) |
| `client.transaction_search.search_transactions` | `search_transactions(start_date, end_date, *, page_size=, page=, fields=, ...)` | — | `SearchResponse` | **Case B: error is always `RawError`** (no typed arm). paginate `total_pages`; `transaction_details[].transaction_info: TransactionInformation`{`transaction_id`,`transaction_amount:Money`,`transaction_status`,`fee_amount`,`invoice_id`,`custom_field`,`transaction_initiation_date`} |

**Return `None` ops in scope:** `vault.delete_payment_token` (raw peer `ApiResult[None,…]`, use
`with_raw_response` to read 204). No other in-scope op returns None (void returns
PaymentAuthorization but 204-empties under minimal prefer — handled via representation).

**Money on the wire:** `Money`/`AmountWithBreakdown` = {`currency_code: str`, `value: str`} both
**required**. `value` is a string scaled to the currency → build with `Decimal.quantize`.

**Enums:** `CheckoutPaymentIntent` = `"AUTHORIZE"|"CAPTURE"` (open; pass string ok). Status enums
(`AuthorizationStatus`, `CaptureStatus`, `RefundStatus`) are open `(str,Enum)` → compare with
`== "COMPLETED"` etc., handle unknown as str. `str(member)` yields the wire value (str enums).

**Optional[T] = T | UNSET** (NOT `typing.Optional`): omit to skip; never pass `None`. Responses:
absent members read back `UNSET` (falsy). Guard every field we depend on with
`is not UNSET`/`isinstance` and treat missing critical id/status as "outcome unknown"
(`PaymentUnreadable`).

### Error unions (per operation; every alias ends in `RawError`)
- All orders/payments/vault ops in scope: typed arm is **`Error`** (`paypal/models/error.py`;
  members incl. `name`, `message`, `debug_id`, `details: list` where each detail has `issue`,
  `description`). `AuthorizeOrderErrorBody = Error | RawError`, etc.
- `search_transactions`: **no typed arm** → `.error` is always `RawError` (Case B).
- Token-fetch failure raises `ApiError` with `.error = OAuthProviderError | RawError` — check
  **first** in the ladder → `PaymentConfigError` (nothing was sent).

### Error boundary (one ladder, in gateway.py)
```
except ApiError as e:
    if isinstance(e.error, OAuthProviderError): -> PaymentConfigError            # 502 (our misconfig)
    if isinstance(e.error, Error):
        # inspect e.status_code + e.error.details[*].issue
        # expired/void-auth issue on capture -> signal ReauthNeeded (caller reauthorizes)
        -> PaymentRejected(e.status_code, first issue/message)                   # map 4xx->4xx, 409->Conflict
    -> PaymentProviderError(e.status_code, e.error.text())                       # RawError arm -> 502
except ValidationError -> PaymentUnreadable (outcome unknown; do NOT assume failure) # 502
except httpx.HTTPError -> PaymentUnavailable (outcome unknown)                   # 502/504
```
`raise ... from e` always. Never log card data or `str(e)`/model_dump of credentials. void's
204-decode `ValueError` is avoided by `prefer=representation`; any residual empty-body ValueError
on a `with_raw_response` path is treated as success by reading the raw response status.

## CROSS-OPERATION INVARIANTS
| invariant | operations | enforced where |
|---|---|---|
| `payment_source.card.vault_id` used to pay must be a token this shopper created | `orders.create_order` ← `vault.create_payment_token` | services: look up `SavedCard(user, id)`; use its `vault_id`; 404 if not owned |
| `capture_authorized_payment(authorization_id)` id comes from this order's `create_order`/`reauthorize` response | capture ← create/reauthorize | services: read from `PayPalPayment.authorization_id` |
| `refund_captured_payment(capture_id)` id comes from this order's capture | refund ← capture | services: read from `PayPalPayment.capture_id` |
| `void_payment(authorization_id)` id from this order's authorization | void ← create | services: `PayPalPayment.authorization_id` |
| `delete_payment_token(id)` id from a token this shopper owns | vault delete ← vault create | services: `SavedCard(user, id)`; delete PayPal token then local row |
| reconciliation match key: PayPal `invoice_id`/`custom_field` == Oscar `order.number` | search ↔ create | services: set both to order.number on create; match on read |

## REQUIRED READING (all loaded before implementation)
- `python-error-handling` — MUST load (error boundary, decode/transport/token failures). LOADED.
- `python-client-initialization` — MUST load (sync client, lifetime, close, WSGI placement). LOADED.
- `python-calling-endpoints` — MUST load (keyword-only, prefer default narrowing, status-vs-id,
  amount scale). LOADED.
- `python-models` — MUST load (UNSET vs None, open enums, Money as string, Dict companions). LOADED.
- `python-authentication` — MUST load (oauth2 client-credentials, lazy token, OAuthProviderError,
  secrets from settings/env). LOADED.
- `python-configuration-resilience` — MUST load (no retries, base_url selection, pagination bound
  + truncated flag, reconciliation clock/set-matching, double-submit claim-first). LOADED.
- `python-testing` — MUST load (transport stub seam, token-request-first, assert built request,
  cover decode/transport/token failures) before any test file. LOADED.

## Assumptions & Blockers
- **No blockers.** All in-scope ops verified against the real sandbox credential (reversible smoke).
- Assumption (minor): amount currency decoupled from catalogue currency per task wording. Proceed.
- Assumption (minor): direct-card auto-authorize is the `/pay` mechanism (verified). Proceed.
- Retries: deliberately **omitted** in the SDK sense; idempotency keys + claim-first records make
  the money ops safe under double-submit (the real risk here), which retries would not address.
