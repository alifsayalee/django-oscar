# PayPal integration plan — django-oscar sandbox

## SDK identity (verified against freshly-cloned map + installed package)
- Distribution AND import root: **`paypal`** (NOT `pay_pal_server_sdk` — the getting-started skill's
  identity table was stale; the cloned `sdk-map.md` + `pyproject.toml` say `name = "paypal"`, version 2.29).
- Installed at `repo/venv/Lib/site-packages/paypal` (confirmed importable).
- Sync client: `PaypalClient` (alias `Client`). Async: `AsyncPaypalClient`. **We use SYNC** — Django
  sandbox views are sync WSGI. Client is keyword-only, owns an httpx pool, must be long-lived
  (module singleton) and `close()`d at shutdown (we hold a process-lifetime singleton; no per-request build).
- Auth: `oauth2=ClientCredentials(client_id, client_secret)` from `paypal.core`. Token fetched lazily,
  cached, from `<base_url>/v1/oauth2/token`. A **failed token fetch does NOT raise `ApiError`** and
  bypasses raw mode — see python-authentication.
- Base URL: one knob `base_url`. Default `https://api-m.sandbox.paypal.com`. We ALWAYS pass base_url
  explicitly (deterministic): `PAYPAL_BASE_URL` verbatim if set, else sandbox host for
  `PAYPAL_ENVIRONMENT=sandbox`, else `https://api-m.paypal.com`.
- SDK does **no retries**. We add small idempotent-safe retry only where needed; otherwise rely on
  PayPal-Request-Id idempotency + our DB state guards.

## Imports
```
from paypal import PaypalClient
from paypal.core import ClientCredentials, ApiError, RawError
from paypal.models import (OrderRequest, PurchaseUnitRequest, AmountWithBreakdown,
    OrderAuthorizeRequest, OrderAuthorizeRequestPaymentSource, CardRequest,
    CaptureRequest, ReauthorizeRequest, RefundRequest, Money,
    PaymentTokenRequest, PaymentTokenRequestPaymentSource, PaymentTokenRequestCard, Customer)
from paypal.models import Error  # typed error arm
```
Enums are open (`...OrStr`) — pass plain strings ("AUTHORIZE", etc.); an unknown wire value passes
through as str, never raises.

---
## Contract sheet — operations in scope

Response mode: use the **parsed** call (raises `ApiError`) everywhere EXCEPT `delete_payment_token`
(returns `None`; use `.with_raw_response` to observe status). Keyword-only boundary: everything after
`*` is keyword; each has a real default so no defensive `None`s.

### orders.create_order(body, *, pay_pal_request_id=, prefer=, ...)  -> Order
- body: `OrderRequest(intent="AUTHORIZE", purchase_units=[PurchaseUnitRequest(
    amount=AmountWithBreakdown(currency_code=CFG, value="12.34"),
    custom_id=order.number, invoice_id=order.number, description=...)])`
- headers: `pay_pal_request_id=f"ord-{order.number}-create"` (PayPal idempotency), `prefer="return=representation"`.
- Return `Order`: `.id` = paypal order id, `.status` ("CREATED").
- Error: CreateOrderErrorBody = `Error`[400,401,422] | RawError.

### orders.authorize_order(id, *, body=, pay_pal_request_id=, prefer=, ...) -> OrderAuthorizeResponse
- Docstring: "a valid payment_source must be provided in the request" — so we authorize with the card
  directly (no browser approval needed for the sandbox test card).
- body: `OrderAuthorizeRequest(payment_source=OrderAuthorizeRequestPaymentSource(card=CardRequest(...)))`
  - one-off: `CardRequest(number, expiry="YYYY-MM", security_code, name)`
  - saved card: `CardRequest(vault_id=<vault token>)`
- headers: `pay_pal_request_id=f"ord-{order.number}-auth"`, `prefer="return=representation"`.
- Return `OrderAuthorizeResponse`: `.status`, `.purchase_units[0].payments.authorizations[0].{id,status,expiration_time}`.
  authorization id lives at that nested path (AuthorizationWithAdditionalData has id/status/expiration_time).
- **Challenge detection**: if `.status == "PAYER_ACTION_REQUIRED"` or any link rel == "payer-action",
  or no authorization returned -> STOP: return 402 to caller + note in report (do NOT build approval round-trip).
- Error: AuthorizeOrderErrorBody = `Error`[400,401,403,404,422,500] | RawError.

### payments.get_authorized_payment(authorization_id) -> PaymentAuthorization
- `.status` (AuthorizationStatus: CREATED/CAPTURED/DENIED/PARTIALLY_CAPTURED/VOIDED/PENDING), `.expiration_time`.
- Used at fulfil to check freshness before capture.

### payments.capture_authorized_payment(authorization_id, *, body=, pay_pal_request_id=, prefer=) -> CapturedPayment
- body: `CaptureRequest(amount=Money(cur,val), final_capture=True)` (look up capture_request.py fields at impl).
- headers: `pay_pal_request_id=f"ord-{order.number}-capture"`, `prefer="return=representation"`.
- Return `CapturedPayment`: `.id` = capture id, `.status` (CaptureStatus: COMPLETED=done, PENDING=pending,
  DECLINED/FAILED=failed, REFUNDED/PARTIALLY_REFUNDED later), `.seller_receivable_breakdown.{gross_amount,
  paypal_fee, net_amount}` each Money{currency_code,value}. paypal_fee/net_amount are Optional (absent while pending).
- Error: `Error`[400,401,403,404,409,422] | RawError[500,...]. A 422 with issue like
  AUTHORIZATION_EXPIRED / cannot capture -> trigger reauthorize path.

### payments.reauthorize_payment(authorization_id, *, body=, pay_pal_request_id=, prefer=) -> PaymentAuthorization
- Renew a stale authorization. body `ReauthorizeRequest(amount=Money)` (look up fields at impl).
- Return PaymentAuthorization with (possibly new) `.id`, `.status`, `.expiration_time`.
- If reauthorize itself fails (can no longer renew) -> return operator-actionable 409 message.

### payments.void_payment(authorization_id, *, pay_pal_request_id=, prefer=) -> PaymentAuthorization
- cancel-before-fulfil: releases held funds. `.status` -> VOIDED.
- Error: `Error`[401,403,404,409,422] | RawError.

### payments.refund_captured_payment(capture_id, *, body=, pay_pal_request_id=, prefer=) -> Refund
- body: `RefundRequest(amount=Money(cur,val))` for partial; omit body (empty) for full.
- header `pay_pal_request_id=<caller idempotency key>` (PayPal dedupes repeats of same key).
- Return `Refund`: `.id` = refund id, `.status` (RefundStatus: COMPLETED/PENDING/FAILED/CANCELLED), `.amount`.
- Error: `Error`[400,401,403,404,409,422] | RawError.

### vault.create_payment_token(body, *, pay_pal_request_id=) -> PaymentTokenResponse
- body: `PaymentTokenRequest(customer=Customer(merchant_customer_id=f"oscar-cust-{user.id}"),
    payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(number, expiry, security_code, name)))`
- Return `PaymentTokenResponse`: `.id` = vault token (store as Bankcard.partner_reference),
  `.payment_source.card` = CardPaymentTokenEntity{brand, last_digits, expiry, name} -> safe description.
- Error: `Error`[400,403,404,422,500] | RawError.

### vault.delete_payment_token(id) -> None   (use .with_raw_response -> ApiResult[None,...], check status ~204)
- Error: `Error`[400,403,500] | RawError.

### transaction_search.search_transactions(start_date, end_date, *, fields="transaction_info", page_size=100, page=1, ...) -> SearchResponse
- start_date/end_date positional (ISO-8601 with tz). Loop page 1..`.total_pages` to cover WHOLE range.
- `.transaction_details[].transaction_info.{transaction_id, transaction_amount(Money), transaction_status,
   invoice_id, custom_field}`. Match app orders by invoice_id==order.number OR transaction_id==capture_id.
- **Case B**: `.error` is always `RawError` (no typed arm).
- Empty result for a just-created range is EXPECTED (reporting lag) — not a gap.

## CROSS-OPERATION INVARIANTS
| invariant | operations | enforced where |
|---|---|---|
| authorization_id passed to capture/void/reauthorize must be one returned by authorize_order | authorize_order -> capture/void/reauthorize | impl: stored on PayPalPayment |
| capture_id passed to refund must be one returned by capture | capture -> refund | impl: stored on PayPalPayment |
| vault_id (card.vault_id) passed at authorize must be a token create_payment_token returned & owned by caller | create_payment_token -> authorize_order | impl: Bankcard(user).partner_reference lookup |
| paymentMethodId the caller pays with must be the caller's own saved card | list/create payment-methods -> pay | impl: Bankcard filtered by user |
| refund total across partial refunds must never exceed captured gross | capture -> refunds | impl: sum(PayPalRefund.amount) <= gross_amount |
| reconciliation lines up PayPal txns vs app orders by invoice_id/capture_id | search_transactions <-> PayPalPayment | impl |

---
## Django design (additive app `sandbox/apps/api/`, label `api`, routed at `/api/`)

Reuse Oscar models: `order.Order`/`order.Line` (via `OrderCreator`), `payment.Source`/`Transaction`/
`SourceType` (money movement, dashboard-visible), `payment.Bankcard` (saved cards: user FK +
partner_reference=vault token; number auto-obfuscated). New models hold ONLY PayPal-owned state that
has no Oscar home:
- `PayPalPayment` (OneToOne Order): paypal_order_id, authorization_id, authorization_status,
  authorization_expiry, capture_id, capture_status, currency, gross_amount, paypal_fee, net_amount,
  state {AWAITING_PAYMENT, AUTHORIZED, CAPTURED, VOIDED, REFUNDED, PARTIALLY_REFUNDED, FAILED}.
- `PayPalRefund` (FK PayPalPayment): refund_id, amount, status, idempotency_key
  (unique_together(payment, idempotency_key)).

Endpoints (session auth; is_staff for fulfil/cancel/reconciliation; else caller-scoped):
- POST /api/orders {items:[{productId,quantity}]} -> {orderId}
- POST /api/orders/{orderId}/pay {card:{...}} | {paymentMethodId} -> authorize
- POST /api/orders/{orderId}/fulfil (staff) -> capture (+ reauthorize if stale)
- POST /api/orders/{orderId}/cancel (staff) -> void
- POST /api/orders/{orderId}/refunds {amount?, idempotencyKey} -> {refundId}
- GET  /api/my-orders
- GET  /api/reconciliation?from=&to= (staff)
- POST /api/payment-methods {card:{...}} -> {paymentMethodId}
- GET  /api/payment-methods
- DELETE /api/payment-methods/{paymentMethodId}

Idempotency in effect: per-order DB row `select_for_update` + state checks; PayPal-Request-Id per
(order, phase); refund keyed by (payment, idempotencyKey). Double-click never double-authorizes/captures.

Secrets: read via django-environ in settings.py using the exact names; never write values to repo.
Full PAN never stored (Bankcard obfuscates; we pass already-safe last_digits) and never logged.

## Assumptions & Blockers
- No DRF assumed available -> plain Django JsonResponse views + csrf_exempt (session-auth JSON API,
  drivable by curl/script). If DRF present we still use plain views for zero new deps.
- Order total currency: catalogue price value taken numerically, labelled with PAYPAL_CURRENCY (per task).
- refund endpoint is shopper-scoped (task lists only fulfil/cancel/reconciliation as operator).
- Challenge/3DS requiring browser -> reported as error, not worked around (per task).
No blockers requiring user input (headless). Proceed.

## REQUIRED READING (load before coding)
- python-error-handling — MUST load (error boundary; ApiError.error union; decode failure != ApiError)
- python-client-initialization — MUST load (keyword-only, pool ownership, singleton lifetime)
- python-authentication — MUST load (oauth2; failed token fetch bypasses raw mode)
- python-calling-endpoints — MUST load (parsed vs raw; None-returning ops; id!=success)
- python-models — MUST load (Optional=T|UNSET not None; open enums; wire aliases; to_dict)
- python-configuration-resilience — MUST load (no retries; idempotency vs double request; reconciliation paging)
- python-testing — MUST load (transport seam for verification script)
