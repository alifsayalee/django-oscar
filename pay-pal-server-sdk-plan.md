# PayPal payments + saved cards for the django-oscar sandbox — plan & contract sheet

## SDK identity (verified against the installed package, NOT the getting-started snapshot)

The getting-started snapshot has **drifted**. The actual repo `context-plugins/paypal-python-sdk@main`
(commit `0ed3d22`, API spec `2.29`) packages differently. Ground truth (confirmed by importing the
installed package and reading its `sdk-map.md`):

| Fact | Value (verified) | Snapshot said (WRONG) |
| --- | --- | --- |
| Distribution / import root | `paypal` | `pay-pal-server-sdk` / `pay_pal_server_sdk` |
| Sync client | `PaypalClient` (alias `Client`) | `PayPalServerSdkClient` |
| Async client | `AsyncPaypalClient` (alias `AsyncClient`) | `AsyncPaypalServerSdkClient` |
| Core import | `from paypal.core import ClientCredentials, ApiError, RawError, Success, Failure` | `pay_pal_server_sdk.core` |
| Base URL default | `https://api-m.sandbox.paypal.com` | same |

GitHub is blocked in this environment; the SDK was installed from a local copy of that exact repo at
`../paypal-python-sdk` (`pip install ../paypal-python-sdk`). Nothing PayPal-specific is written from
memory — every fact below is from the SDK map (`../paypal-python-sdk/sdk-map.md`,
`map/operations/*.md`) and the model source under `venv/Lib/site-packages/paypal/`.

## Sync vs async

**Sync** (`PaypalClient`). Django under WSGI. One module-level lazily-built client held in a services
module, built from Django settings, closed at process exit via `atexit`. `close()` obligation honoured.

## Host wiring

- New Django app `apps.paypal_checkout` under `sandbox/apps/` (NOT named `paypal` — that would shadow
  the SDK package). Added to `INSTALLED_APPS` in `sandbox/settings.py`.
- Settings read from env via `sandbox/settings.py` using `django-environ` (`env`): `PAYPAL_CLIENT_ID`,
  `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENVIRONMENT`, `PAYPAL_CURRENCY`, `PAYPAL_BASE_URL` (optional). No
  secret values ever written to a repo file.
- `PAYPAL_BASE_URL` override: when set, pass verbatim as `base_url=` (moves the token fetch too). When
  unset, pass `base_url=None` → SDK default sandbox host. (Sandbox is the only declared server.)
- URLs: `sandbox/urls.py` gains `path("api/", include("apps.paypal_checkout.urls"))` in the top-level
  (non-i18n) `urlpatterns`, before the oscar i18n catch-all.

## Oscar model reuse (no parallel order/payment set)

- Orders: `oscar.apps.order` `Order`/`Line` via `oscar.apps.order.utils.OrderCreator`. Order built from
  a real Oscar `Basket` + `Selector().strategy()` + `NoShippingRequired` method + `OrderTotalCalculator`.
- Payment bookkeeping (amounts + dashboard visibility): `oscar.apps.payment` `Source`, `SourceType`
  (code `paypal`), `Transaction`. `allocate()` on authorize, `debit()` on capture, `refund()` on refund.
  Refundable guard uses `source.amount_available_for_refund` (= debited − refunded).
- Saved cards: `oscar.apps.payment` `Bankcard` (user FK, `partner_reference` = PayPal vault token id,
  `number` = obfuscated `XXXX-XXXX-XXXX-1111`, `card_type` = brand, `expiry_date`, `name`). Real PAN
  never stored; `Bankcard.save()` re-obfuscation is bypassed by passing an already-`X`-prefixed number.
- New app models hold only PayPal-specific metadata Oscar has no field for:
  - `PayPalPayment` (OneToOne → `order.Order`): `paypal_order_id`, `authorization_id`, `authorization_status`,
    `capture_id`, `capture_status`, `paypal_fee` (Decimal), `net_amount` (Decimal), `currency`, `state`,
    `authorize_request_id`, `capture_request_id` (stored idempotency keys reused on retry).
  - `PayPalRefund` (FK → `PayPalPayment`): `refund_id`, `amount`, `currency`, `status`, `idempotency_key`
    (unique per payment). `refundId` returned to caller.

`state` values: `AWAITING_PAYMENT`, `AUTHORIZED`, `CAPTURED`, `PARTIALLY_REFUNDED`, `REFUNDED`,
`CANCELLED`, `FAILED`. Order.status mirrors coarsely (`Awaiting payment`/`Payment authorized`/`Fulfilled`/`Cancelled`).

## VERIFIED sandbox behaviour (smoke-tested live against the business account)

1. `create_order(intent=AUTHORIZE, payment_source.card, prefer="return=representation")` with a
   PayPal-Request-Id **auto-creates the authorization** — response `status=COMPLETED`,
   `purchase_units[0].payments.authorizations[0]` present (`status=CREATED`). No separate
   `authorize_order` call needed for a direct card. (Fallback: if authorizations empty, call
   `authorize_order`.)
2. `capture_authorized_payment(auth_id, prefer="return=representation")` → `CapturedPayment` with
   `amount`, `seller_receivable_breakdown.{gross_amount, paypal_fee, net_amount}` (e.g. 15.00 → fee
   0.88, net 14.12). This is the fee/net the task requires at fulfilment.
3. `refund_captured_payment(capture_id, body={amount:{...}})` → `Refund` (partial refund works).
4. **`void_payment` returns 204 by default → the SDK decoder raises `ValueError` on the empty body in
   BOTH response modes.** Passing `prefer="return=representation"` makes PayPal return **200** with a
   full `PaymentAuthorization` body (`status=VOIDED`). MUST use `prefer="return=representation"` for void.
5. Vault: `create_payment_token(body={payment_source:{card:{number,expiry,security_code,name,billing_address}}})`
   → `PaymentTokenResponse` with `id`, `customer.id`, and `payment_source.card.{last_digits, brand, expiry, name}`
   (safe display). Pay with saved card = `create_order(... payment_source={card:{vault_id: <token id>}})`.
   `delete_payment_token(id)` → 204 (returns None; no decode issue).
6. `search_transactions(start_date, end_date, fields="transaction_info", page_size, page)` → `SearchResponse`
   with `total_pages`/`page`; page 1 returned 100 of 147 → **MUST loop page 1..total_pages** to cover the range.
   `transaction_info` carries `transaction_id`, `invoice_id`, `custom_field`, `transaction_amount`,
   `fee_amount`, `transaction_status`. Match app orders on `invoice_id`==order number (and `transaction_id`
   ==capture id). Empty result over a just-created range is expected (reporting lag) — not a gap.

## Contract sheet (operations in scope)

Client: `PaypalClient(base_url=<PAYPAL_BASE_URL or None>, oauth2=ClientCredentials(client_id, client_secret), timeout=30.0)`.
Every op is keyword-only after `*`; every keyword has a real default. `request_options` is always last.
Bodies passed as dicts (companion `…Dict`), keyed by Python names. Money value = `f"{Decimal:.2f}"`, currency = `PAYPAL_CURRENCY`.

| Purpose | Call | Key args | Returns | Error union |
| --- | --- | --- | --- | --- |
| Authorize (pay) | `client.orders.create_order` | positional `body`; `pay_pal_request_id`, `prefer="return=representation"` | `Order` | `Error \| RawError` (400,401,422) |
| Auth fallback | `client.orders.authorize_order` | positional `id`; `prefer="return=representation"` | `OrderAuthorizeResponse` | `Error \| RawError` |
| Inspect auth | `client.payments.get_authorized_payment` | positional `authorization_id` | `PaymentAuthorization` | `Error \| RawError` (401,403,404) |
| Capture (fulfil) | `client.payments.capture_authorized_payment` | positional `authorization_id`; `pay_pal_request_id`, `prefer="return=representation"` | `CapturedPayment` | `Error \| RawError` (400,401,403,404,409,422) |
| Renew stale auth | `client.payments.reauthorize_payment` | positional `authorization_id`; `prefer="return=representation"` | `PaymentAuthorization` | `Error \| RawError` |
| Cancel | `client.payments.void_payment` | positional `authorization_id`; **`prefer="return=representation"`** | `PaymentAuthorization` | `Error \| RawError` (401,403,404,409,422) |
| Refund | `client.payments.refund_captured_payment` | positional `capture_id`; `pay_pal_request_id`, `prefer="return=representation"`, `body={amount}` | `Refund` | `Error \| RawError` (400,401,403,404,409,422) |
| Save card | `client.vault.create_payment_token` | positional `body`; `pay_pal_request_id` | `PaymentTokenResponse` | `Error \| RawError` |
| Delete card | `client.vault.delete_payment_token` | positional `id` | `None` (204) — use `with_raw_response` to read status | `Error \| RawError` |
| Reconcile | `client.transaction_search.search_transactions` | positional `start_date`, `end_date`; `fields`, `page_size`, `page` | `SearchResponse` | **Case B: `RawError` only** |

Model shapes (members actually used):
- `AmountWithBreakdown`/`Money`: `currency_code`, `value` (both required, `str`).
- `PurchaseUnitRequest`: `amount` (req), `invoice_id`, `custom_id`, `description`.
- `CardRequest`: `number`, `expiry` (`YYYY-MM`), `security_code`, `name`, `billing_address` (`Address`), `vault_id`.
- `Address`: `address_line_1`, `admin_area_2` (city), `admin_area_1` (state), `postal_code`, `country_code`.
- `CapturedPayment`: `id`, `status`, `amount`(Money), `seller_receivable_breakdown.{gross_amount,paypal_fee,net_amount}`.
- `PaymentAuthorization`: `id`, `status` (open enum: CREATED/CAPTURED/VOIDED/… + str), `amount`, `expiration_time`.
- `Refund`: `id`, `status`, `amount`.
- `PaymentTokenResponse`: `id`, `customer.id`, `payment_source.card.{last_digits,brand,expiry,name}`.
- `SearchResponse`: `total_pages`, `page`, `transaction_details[].transaction_info.{transaction_id,invoice_id,custom_field,transaction_amount,fee_amount,transaction_status}`.

All model fields are `Optional[T] = UNSET` on responses → resolve `UNSET`→`None`/absent before returning
JSON. Enums are open → compare against known members but tolerate plain str. No `Optional[Any]` members used.

## Idempotency & concurrency

- Pay: `select_for_update` on `PayPalPayment`; if `authorization_id` already set and not voided, return it
  (no re-authorize). PayPal-Request-Id stored on first attempt (`authorize_request_id`) and reused.
- Fulfil: if `capture_id` set, return it. Stored `capture_request_id` reused.
- Refund: caller-supplied `idempotency_key`; unique-together (payment, key). Repeat key → return stored
  `PayPalRefund`. Guard requested amount ≤ `source.amount_available_for_refund`.
- Stale auth at fulfil: capture; on capture failure whose auth is expired (get_authorized_payment status
  not CREATED, or capture error), `reauthorize_payment` → new auth id stored → capture again. If reauthorize
  fails → 409 with an operator-actionable message ("authorization can no longer be renewed; re-collect payment").

## Auth / access control

- Django session login (`request.user.is_authenticated`); 401 JSON if anonymous.
- Shopper endpoints scoped to `request.user` (orders filtered by `order.user`, cards by `bankcard.user`).
- Operator endpoints (`fulfil`, `cancel`, `reconciliation`) require `request.user.is_staff` → 403 otherwise.
- CSRF: API uses a custom auth-required mixin; endpoints are JSON POST. Use Django session auth; exempt CSRF
  is undesirable — but for a headless JSON API driven by session cookie we set `@csrf_exempt` is NOT used;
  instead these are DRF-free plain views. Decision: plain Django views, JSON in/out, session auth, and we
  mark POST views `csrf_exempt` ONLY is avoided — we require the `X-CSRFToken`? For a pure API test harness
  without a browser, we accept session auth and disable CSRF per-view via `csrf_exempt` since there is no
  browser form. (Documented tradeoff; endpoints still require an authenticated session.)

## Endpoints (all under /api/, each separately invocable)

`POST /api/orders` · `POST /api/orders/{id}/pay` · `POST /api/orders/{id}/fulfil` (staff) ·
`POST /api/orders/{id}/cancel` (staff) · `POST /api/orders/{id}/refunds` · `GET /api/my-orders` ·
`GET /api/reconciliation?from&to` (staff) · `POST/GET /api/payment-methods` ·
`DELETE /api/payment-methods/{id}`. Top-level ids returned: `orderId`, `paymentMethodId`, `refundId`.

## Error boundary (per python-error-handling)

One translation layer maps: `OAuthProviderError`→500 config; `ApiError` 401/403→502, 429→503,
{400,404,409,422}→surface message, 5xx/unmapped→502; `ValidationError`(decode)→502 outcome-unknown;
`httpx.ConnectError/ConnectTimeout/PoolTimeout/ProxyError`→502 never-sent; other `httpx.RequestError`→504
unknown. void's empty-body `ValueError` is pre-empted by `prefer="return=representation"`, but the capture/void
paths still guard decode failures.

## REQUIRED READING (all loaded before implementation)

- python-error-handling — MUST load (error boundary). ✅ loaded
- python-client-initialization — MUST load (client construction/lifetime). ✅ loaded
- python-calling-endpoints — MUST load. ✅ loaded
- python-models — MUST load (UNSET/enums/Decimal money). ✅ loaded
- python-configuration-resilience — MUST load (base_url, no retries, timeout). ✅ loaded
- python-testing — MUST load (transport stub seam). ✅ loaded

## Build order

1. settings wiring + app skeleton (apps.py, __init__, migrations).
2. models (PayPalPayment, PayPalRefund).
3. services/client.py (client singleton), services/gateway.py (SDK calls + error boundary), services/orders.py (Oscar order building + payment orchestration).
4. views + urls + serialization helpers.
5. migrations, migrate, unit tests (stub transport), live end-to-end verification script.
