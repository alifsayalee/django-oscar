# PayPal integration plan — django-oscar sandbox

Adds **PayPal** card payments + saved cards to the `sandbox/` site as a new Django app
`sandbox/apps/api/`, routed under `/api/`. Additive; reuses Oscar `order.Order`/`order.Line`.

## SDK identity (verified against installed package, NOT the stale skill snapshot)

- Distribution & import root: **`paypal`** (skill said `pay-pal-server-sdk`/`pay_pal_server_sdk` — WRONG for this build).
- Install: `pip install "paypal @ git+https://github.com/context-plugins/paypal-python-sdk.git@main"` (installed OK, v2.29).
- Sync client: `from paypal import PaypalClient`. Core: `from paypal.core import ClientCredentials, ApiError, RawError`.
  Models: `from paypal.models import ...`. Enums: `paypal.models.enums`. Errors: `paypal.errors`.
- **Sync** client (Django WSGI sandbox — `sandbox/wsgi.py`). One long-lived module-scoped singleton
  (lazy), never per-request, never closed per-request. Do NOT mix sync/async.
- Auth: `oauth2=ClientCredentials(client_id=..., client_secret=...)`. Token fetched lazily & cached.
- `base_url`: pass `PAYPAL_BASE_URL` verbatim when set, else `None` → SDK default
  `https://api-m.sandbox.paypal.com`. Token endpoint moves with base_url.

## Config (all via `sandbox/settings.py`, read from env at runtime; NEVER hardcode values)

`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENVIRONMENT`, `PAYPAL_CURRENCY`, `PAYPAL_BASE_URL`
(optional override). Verified present: env=sandbox, currency=USD, base_url unset, creds len 80.

## Live smoke results (all confirmed against real sandbox creds)

- `transaction_search.search_transactions`: OK, 1599 items / **16 pages** → reconciliation MUST paginate all pages.
- **Direct card + intent=AUTHORIZE `create_order` AUTO-AUTHORIZES in one call** (order status COMPLETED,
  authorization present at `resp.purchase_units[0].payments.authorizations[0]`). Calling `authorize_order`
  after → 422 ORDER_ALREADY_AUTHORIZED. So **`/pay` = create_order(card, AUTHORIZE); do NOT call authorize_order**.
- `create_order` with a `payment_source` REQUIRES header `PayPal-Request-Id` (`pay_pal_request_id`) → also our idempotency key.
- `capture_authorized_payment` → `seller_receivable_breakdown`: gross=12.00, **paypal_fee=0.80, net_amount=11.20**.
- `refund_captured_payment` partial 5.00 → COMPLETED; `get_refund` COMPLETED.
- `void_payment` → VOIDED. `vault.create_payment_token(card)` → token, `payment_source.card.last_digits/brand/expiry`.
  Pay with saved card = `create_order(payment_source={"card":{"vault_id": token}})`. `delete_payment_token` OK.
- **GOTCHA (decode failure):** `void_payment`/`capture`/`refund`/`create`/`authorize` with the DEFAULT
  `prefer="return=minimal"` can return **204/empty → SDK raises `ValueError` ("Response body is not valid JSON"),
  NOT ApiError, bypassing both response modes.** FIX: always pass **`prefer="return=representation"`** on those
  calls so a JSON body is returned. (Server-side action still succeeds even on the ValueError, but we need the body.)
- `delete_payment_token` (returns None/204) parses fine with the plain parsed call.

## Contract sheet (operations in scope)

Client keyword-only; every op ends with keyword-only `request_options`. `prefer` default `"return=minimal"`.

| Operation | Call | Key body/params | Returns | Error union |
|---|---|---|---|---|
| create_order | `client.orders.create_order(body, pay_pal_request_id=key, prefer="return=representation")` | body=`OrderRequest`/dict: `intent="AUTHORIZE"`, `purchase_units=[{amount:{currency_code,value}, custom_id, invoice_id}]`, `payment_source={"card":{...}}` or `{"card":{"vault_id":tok}}` | `Order` (auto-authorized) | `Error`[400,401,422] \| RawError |
| capture_authorized_payment | `client.payments.capture_authorized_payment(auth_id, pay_pal_request_id=key, prefer="return=representation", body={"final_capture":True})` | | `CapturedPayment` (`.id`,`.status`,`.seller_receivable_breakdown.{gross_amount,paypal_fee,net_amount}`) | `Error`[400,401,403,404,409,422] \| RawError[500] |
| get_authorized_payment | `client.payments.get_authorized_payment(auth_id)` | | `PaymentAuthorization` (`.status`,`.expiration_time`) | `Error`[401,403,404] \| RawError[500] |
| reauthorize_payment | `client.payments.reauthorize_payment(auth_id, pay_pal_request_id=key, prefer="return=representation", body={"amount":{currency_code,value}})` | renews stale auth → **NEW auth id** | `PaymentAuthorization` | `Error`[400,401,403,404,422] \| RawError[500] |
| void_payment | `client.payments.void_payment(auth_id, prefer="return=representation")` | releases hold | `PaymentAuthorization` (status VOIDED) | `Error`[401,403,404,409,422] \| RawError[500] |
| refund_captured_payment | `client.payments.refund_captured_payment(capture_id, pay_pal_request_id=idem_key, prefer="return=representation", body={"amount":{currency_code,value}})` (omit amount → full) | | `Refund` (`.id`,`.status`,`.amount`) | `Error`[400,401,403,404,409,422] \| RawError[500] |
| get_refund | `client.payments.get_refund(refund_id)` | | `Refund` | `Error`[401,403,404] \| RawError[500] |
| get_order | `client.orders.get_order(id)` | | `Order` | `Error`[401,404] \| RawError |
| create_payment_token | `client.vault.create_payment_token(body, pay_pal_request_id=key)` | body=`PaymentTokenRequest`/dict: `payment_source={"card":{number,expiry,security_code,name,billing_address}}`, optional `customer={"id":cust}` | `PaymentTokenResponse` (`.id`, `.payment_source.card.{last_digits,brand,expiry,name}`, `.customer.id`) | `Error`[400,403,404,422,500] \| RawError |
| delete_payment_token | `client.vault.delete_payment_token(id)` (parsed, returns None) | | None | `Error`[400,403,500] \| RawError |
| search_transactions | `client.transaction_search.search_transactions(start_date, end_date, fields="transaction_info", page=n, page_size=100)` | **loop pages 1..total_pages** | `SearchResponse` (`.transaction_details[].transaction_info.{transaction_id,invoice_id,custom_field,transaction_amount,transaction_status,transaction_initiation_date}`, `.total_pages`,`.page`) | **Case B: RawError only** |

- **prefer="return=representation"** on create/capture/void/refund/reauthorize (avoids the 204 ValueError, gives body).
- Dates for search_transactions: ISO-8601, format `%Y-%m-%dT%H:%M:%S-0000` (or the caller's `from`/`to` passed through if already RFC3339). Range max 31 days per PayPal — chunk if wider.
- Money: `{"currency_code": settings.PAYPAL_CURRENCY, "value": f"{amount:.2f}"}`. Value = Oscar `order.total_incl_tax` to the cent.
- Card billing_address (`Address`): `address_line_1, admin_area_2 (city), admin_area_1 (state), postal_code, country_code`.
- Error handling: `try/except ApiError` → read `.status_code`, `.error` (`Error` has `.name`,`.message`,`.debug_id`,`.details`) via `isinstance(e.error, Error)` else `RawError.text()`. ALSO catch `ValueError`/`ValidationError` (decode failures) and `httpx` transport errors — not ApiError.

### CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| saved-card id passed to `/pay` must be one the caller saved | `POST /pay` ← `POST /payment-methods` | SavedCard row scoped to user; vault_id from create_payment_token |
| authorization_id captured/voided/reauthorized must come from this order's create_order | pay ↔ fulfil/cancel | stored on PayPalPayment |
| capture_id refunded must come from this order's capture | refunds ← fulfil | stored on PayPalPayment |
| refund total must never exceed captured amount | refunds (partial) | app: sum(refunds)+new ≤ captured_amount |
| reconciliation match key | reconciliation ↔ orders | purchase_unit `invoice_id`/`custom_id` = order.number ↔ txn `invoice_id`/`custom_field` |

## App design (`sandbox/apps/api/`)

Models (PayPal-specific state; Order/Line reused from Oscar):
- `PayPalPayment` (OneToOne `order.Order`): paypal_order_id, authorization_id, authorization_status,
  authorization_expires_at, capture_id, capture_status, state (PENDING/AUTHORIZED/CAPTURED/VOIDED/
  PARTIALLY_REFUNDED/REFUNDED/FAILED), currency, gross_amount, paypal_fee, net_amount, captured_amount,
  total_refunded, timestamps.
- `PayPalRefund` (FK PayPalPayment): refund_id, amount, status, idempotency_key; `unique_together(payment, idempotency_key)`.
- `SavedCard` (FK user): vault_token_id (unique), paypal_customer_id, brand, last_digits, expiry, cardholder_name, created.
- `PayPalCustomer` (OneToOne user): paypal_customer_id — reuse across saves so vault is per-shopper.

Order status strings (set directly, no pipeline dependency): "Awaiting payment", "Authorized",
"Fulfilled", "Cancelled", "Refunded", "Partially refunded".

Endpoints (all session-auth; `@csrf_exempt` JSON API for drivability; staff-only = is_staff):
- `POST /api/orders` → build basket from item ids+qty, place Oscar order (status Awaiting payment), create PayPalPayment(PENDING). → `{orderId}`
- `POST /api/orders/{orderId}/pay` (shopper) → create_order(card|vault_id, AUTHORIZE). Idempotent: reuse existing auth if present (select_for_update + stored key).
- `POST /api/orders/{orderId}/fulfil` (staff) → get_authorized_payment; if EXPIRED → reauthorize (new auth id); else capture. If capture 422 due to expiry → reauthorize then capture. If cannot renew → actionable error. Record fee/net. Idempotent (reuse capture).
- `POST /api/orders/{orderId}/cancel` (shopper) → void (before capture). Idempotent.
- `POST /api/orders/{orderId}/refunds` (staff) → refund (full/partial), idempotency_key required, cap at captured−refunded. → `{refundId}`
- `GET /api/my-orders` (shopper) → caller's orders + payment state.
- `GET /api/reconciliation?from=&to=` (staff) → paginate all search_transactions pages, match by invoice_id/custom_field to orders; report matched / paypal_only / app_only.
- `POST /api/payment-methods` (shopper) → create_payment_token(card); store SavedCard. → `{paymentMethodId}` + safe descriptor.
- `GET /api/payment-methods` (shopper) → caller's saved cards.
- `DELETE /api/payment-methods/{id}` (shopper) → vault.delete_payment_token + delete row.

Ownership: shopper endpoints filter by request.user; a saved card / order of another user → 404. Full PAN never stored/logged.

Idempotency: create_order/capture/refund carry `pay_pal_request_id` (PayPal dedupes); DB `select_for_update`
guards double-clicks; refunds keyed by caller idempotency_key (unique per payment).

Gateway module `apps/api/gateway.py`: builds the singleton PaypalClient from settings; helper functions
wrapping each op with `prefer="return=representation"` and a unified error→dict translation.

## Assumptions & Blockers
- **Assumption:** amount value = catalogue total (GBP numerals) sent with currency=PAYPAL_CURRENCY (USD).
  Task: "amounts come from catalogue prices; currency from configuration." Proceed.
- **Assumption:** synthetic shipping address (default country) since API carries no address and no UI. Proceed.
- **Assumption:** `@csrf_exempt` on JSON API endpoints (session-auth) for scriptability. Proceed.
- No blockers: all in-scope ops verified live. No challenge/3DS on the test card (no browser round-trip needed).

## REQUIRED READING (load before coding)
- MUST load `python-error-handling` — every op has an error boundary + the ValueError/ValidationError decode trap.
- MUST load `python-client-initialization` — singleton client construction/lifetime.
- MUST load `python-calling-endpoints` — positional/keyword split, prefer, raw vs parsed.
- MUST load `python-models` — building card/amount bodies, UNSET vs None, open enums, to_dict.
- MUST load `python-testing` — the verification harness that drives the API/fakes nothing of PayPal (real sandbox).
