# PayPal integration plan — django-oscar sandbox

## Goal
Add PayPal card payments + saved cards to the sandbox as a new Django app `sandbox/apps/payments_api`,
exposed under `/api/`. Additive; reuses Oscar's `order.Order`/`order.Line` and `catalogue.Product`.

## SDK identity (verified against the INSTALLED package, not the stale getting-started snapshot)
- Distribution + import root: **`paypal`** (getting-started said `pay-pal-server-sdk`/`pay_pal_server_sdk`
  — WRONG for this version; the cloned source repo & installed wheel both use `paypal`). Installed via
  `pip install <local clone of github.com/context-plugins/paypal-python-sdk>` (git URL name-mismatch
  blocked the direct install; non-editable local install builds a proper wheel).
- Sync client: `from paypal import PaypalClient`. Client is **keyword-only**: `base_url`, `timeout=30.0`,
  `oauth2`, `custom_http_client`. Auth: `oauth2=ClientCredentials(client_id, client_secret)`.
- Core imports: `from paypal.core import ClientCredentials, ApiError, RawError, Success, Failure, OAuthProviderError`.
- **Sync** chosen: Django/WSGI host. Client built lazily as a module global, `close()` on `atexit`.
- Base URL: `PAYPAL_BASE_URL` verbatim if set, else `sandbox`→`https://api-m.sandbox.paypal.com`,
  `live`/`production`→`https://api-m.paypal.com`. Always passed explicitly.

## Toolchain / baseline
- `py -3.11 -m venv venv`; `venv/Scripts/pip install -e .[test]` (done). PayPal SDK installed (done).
- No DRF in the project → plain Django views + `JsonResponse`, session auth (`login_required`,
  `is_staff` check). No new heavy dependency.
- Type check: project has no mypy config; will run `venv/Scripts/mypy --strict` on the new app's SDK
  call sites. Tests: `venv/Scripts/pytest` for the new app's test module.

## Credentials (read via Django settings only; values never written to repo)
`sandbox/settings.py` reads with `env.str(..., default="")`:
`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENVIRONMENT`, `PAYPAL_CURRENCY`, `PAYPAL_BASE_URL`.
Missing-credential check lives in `build_client()`, not at import (so tests import cleanly).

## SMOKE RESULTS (real sandbox, reversible ops — all confirmed working)
1. create_order(AUTHORIZE) → CREATED. 2. authorize_order + direct card 4111… → order COMPLETED,
   authorization CREATED. 3. capture_authorized_payment → COMPLETED, breakdown gross 24.99 / **fee 1.14
   / net 23.85**. 4. refund_captured_payment(partial 10.00) → COMPLETED. 5. void_payment → VOIDED.
   6. create_setup_token(card) → APPROVED. 7. create_payment_token(token SETUP_TOKEN) → id, card VISA
   ****1111. 8. authorize_order with `card.vault_id` → COMPLETED (**saved card pays 2nd order**).
   9. delete_payment_token → 204. 10. search_transactions(2-day range) → 136 items / 2 pages.
- **No 3DS challenge** with 4111 test card → no browser round-trip needed.

## CRITICAL SDK BEHAVIOURS (found by smoke, must obey)
- **`void_payment` returns 204 empty by default → the SDK's decoder raises `ValueError` in BOTH parsed
  and raw modes** (map declares it returns `PaymentAuthorization`). FIX: always call
  `void_payment(id, prefer="return=representation")` so PayPal returns the voided authorization body.
- `delete_payment_token` genuinely returns `None` (map says so) → no decode; use raw peer for the 204 status.
- `search_transactions` is **Case B** (`.error` always `RawError`); paginates → loop `page` 1..`total_pages`.
- All create/authorize/capture calls pass `prefer="return=representation"` to get full bodies
  (breakdown, authorization/capture ids, statuses).

## Endpoints (all under /api/, plain Django views, session auth)
Shopper-scoped (login_required, acts only on caller's own rows):
- `POST /api/orders` → build Oscar basket from `[{product_id, quantity}]`, `OrderCreator.place_order`
  with Free shipping + default Country shipping address; create `PayPalPayment(status=PENDING_PAYMENT)`.
  Returns top-level **`orderId`** (= Oscar order number).
- `POST /api/orders/{orderId}/pay` → authorize. Body carries either `card{number,expiry,security_code,
  name,billing_address}` OR `paymentMethodId` (a saved card). create_order(AUTHORIZE, amount=order total)
  then authorize_order with card or `card.vault_id`. Store paypal_order_id, authorization_id,
  authorized_amount. Idempotent (see below).
- `POST /api/orders/{orderId}/refunds` → refund_captured_payment (full or partial), caller idempotency
  key. Returns top-level **`refundId`**.
- `GET /api/my-orders` → caller's orders + payment state.
- `POST /api/payment-methods` → create_setup_token(card) → create_payment_token; store SavedPaymentMethod.
  Returns top-level **`paymentMethodId`**. Response describes card safely (brand + last 4 only).
- `GET /api/payment-methods` → caller's saved cards (served from local DB — authoritative for ownership;
  PayPal list endpoint has propagation lag).
- `DELETE /api/payment-methods/{paymentMethodId}` → delete_payment_token + delete local row.

Operator-scoped (is_staff):
- `POST /api/orders/{orderId}/fulfil` → capture_authorized_payment; on expired auth
  (Error.details[].issue in {AUTHORIZATION_EXPIRED, AUTH_CAPTURE_CURRENCY_MISMATCH?…} → really
  `AUTHORIZATION_EXPIRED`/`INVALID_RESOURCE_ID` for expired) → reauthorize_payment then capture again;
  if reauthorize impossible → 409 with operator-actionable message. Store capture_id, captured_amount,
  paypal_fee, net_amount. Advance Oscar order status → 'Being processed'/'Complete'.
- `POST /api/orders/{orderId}/cancel` → void_payment(prefer=representation); only before fulfilment.
- `GET /api/reconciliation?from&to` → search_transactions over ALL pages; join to PayPalPayment via
  `custom_id`/`invoice_id` (= order number); list matched, paypal-only, app-only.

## Idempotency / integrity
- `select_for_update` on PayPalPayment for pay/fulfil/cancel/refund.
- pay: if status already AUTHORIZED/CAPTURED → return current state (no 2nd authorize). Reuse a stored
  `PayPal-Request-Id` per payment on create/authorize.
- fulfil: if already CAPTURED → return current. Stored capture request-id.
- refund: `unique_together(payment, idempotency_key)`; repeat key → return existing refund row.
  Enforce `sum(refunds)+new ≤ captured_amount` inside the transaction.
- cancel: only if AUTHORIZED (not captured); after void → status VOIDED.

## Data model (new app `payments_api`)
- `PayPalCustomer(user OneToOne, customer_id)` — stable PayPal vault customer per shopper.
- `PayPalPayment(order OneToOne→order.Order, user FK, paypal_order_id, authorization_id, capture_id,
  status, currency, authorized_amount, captured_amount, paypal_fee, net_amount, refunded_amount,
  authorize_request_id, capture_request_id, created, updated)`.
- `PayPalRefund(payment FK, refund_id, amount, idempotency_key, status, created)` unique(payment,key).
- `SavedPaymentMethod(user FK, payment_token_id unique, paypal_customer_id, brand, last_digits, expiry,
  created)`. Ownership: every query filtered by `user=request.user`.
- **No card PAN/CVV ever stored or logged.** Card fields flow straight into the SDK request and are dropped.

## Ports
Dev server binds `APP_PORT_BLOCK_BASE` (36740). Stop prior instance before restart.

---

# CONTRACT SHEET (grounded in SDK map + source; no open lookups)

Sync client; `Client`/`AsyncClient` do not mix. `close()` obligation. Every op ends with keyword-only
`request_options`; every keyword-only param has a real default (no defensive `None`s). Model construction
accepts Python name or wire alias (`validate_by_name` + `validate_by_alias`); serialize_by_alias=True.
`Optional[T]` here = `T | UNSET`, not `None`.

### orders (client.orders)
- `create_order(body: OrderRequest, *, prefer="return=minimal", pay_pal_request_id=None, …)` → `Order`.
  Error `CreateOrderErrorBody` = `Error`[400,401,422] | `RawError`. Pass `prefer="return=representation"`.
  Body: `OrderRequest(intent="AUTHORIZE", purchase_units=[PurchaseUnitRequest(amount=AmountWithBreakdown(
  currency_code, value), custom_id=<order#>, invoice_id=<order#>)])`.
- `authorize_order(id: str, *, prefer="return=minimal", pay_pal_request_id=None,
  body: OrderAuthorizeRequest|None=None, …)` → `OrderAuthorizeResponse`.
  Error `Error`[400,401,403,404,422,500] | RawError. Body:
  `OrderAuthorizeRequest(payment_source=OrderAuthorizeRequestPaymentSource(card=CardRequest(...)))`.
  Result: `resp.purchase_units[0].payments.authorizations[0].id/status`; `resp.status` == COMPLETED on success.
- (get_order available if needed: `get_order(id)`→Order.)

### payments (client.payments)
- `capture_authorized_payment(authorization_id: str, *, prefer="return=minimal", pay_pal_request_id=None,
  body: CaptureRequest|None=None, …)` → `CapturedPayment`. Error `Error`[400,401,403,404,409,422] |
  RawError[500,…]. Body `CaptureRequest(final_capture=True)`. Result: `.id`, `.status` (CaptureStatus),
  `.seller_receivable_breakdown.gross_amount/paypal_fee/net_amount` (each `Money.value`; fee/net UNSET while pending).
- `refund_captured_payment(capture_id: str, *, prefer=…, pay_pal_request_id=None, body: RefundRequest|None,
  …)` → `Refund`. Error `Error`[400,401,403,404,409,422] | RawError. Body `RefundRequest(amount=Money(cur,val))`
  for partial; omit amount for full. Result `.id`, `.status`, `.amount.value`.
- `reauthorize_payment(authorization_id: str, *, pay_pal_request_id=None, prefer=…, body: ReauthorizeRequest|None,
  …)` → `PaymentAuthorization`. Error `Error`[400,401,403,404,422] | RawError. Body `ReauthorizeRequest(amount=Money)`.
- `void_payment(authorization_id: str, *, prefer="return=minimal", pay_pal_request_id=None, …)` →
  `PaymentAuthorization`. **MUST pass prefer="return=representation"** (else 204→ValueError in both modes).
  Error `Error`[401,403,404,409,422] | RawError.
- `get_authorized_payment(authorization_id)` → `PaymentAuthorization` (for status re-read).

### vault (client.vault)
- `create_setup_token(body: SetupTokenRequest, *, pay_pal_request_id=None, …)` → `SetupTokenResponse`.
  Error `Error`[400,403,422,500] | RawError. Body `SetupTokenRequest(customer=Customer(id=<stored>|None,
  merchant_customer_id=str(user.pk)), payment_source=SetupTokenRequestPaymentSource(card=SetupTokenRequestCard(
  number,expiry,security_code,name,billing_address=Address(country_code))))`. Result `.id`, `.customer.id`, `.status`.
- `create_payment_token(body: PaymentTokenRequest, *, pay_pal_request_id=None, …)` → `PaymentTokenResponse`.
  Error `Error`[400,403,404,422,500] | RawError. Body `PaymentTokenRequest(payment_source=
  PaymentTokenRequestPaymentSource(token=VaultTokenRequest(id=<setup_token_id>, type_="SETUP_TOKEN")))`.
  Result `.id`, `.customer.id`, `.payment_source.card.brand`, `.payment_source.card.last_digits`, `.card.expiry`.
- `delete_payment_token(id: str, …)` → **None** (204). Use raw peer `with_raw_response` for status.
  Error `Error`[400,403,500] | RawError.
- `list_customer_payment_tokens(customer_id: str, *, page_size=5, page=1, total_required=False, …)` →
  `CustomerVaultPaymentTokensResponse` (has propagation lag; not relied on for GET).

### transaction_search (client.transaction_search)
- `search_transactions(start_date: str, end_date: str, *, fields="transaction_info",
  balance_affecting_records_only="Y", page_size=100, page=1, …)` → `SearchResponse`. **Case B**: `.error`
  always `RawError`. Dates ISO-8601 like `YYYY-MM-DDTHH:MM:SS-0000`. Result `.transaction_details[].transaction_info`
  (`.transaction_id`, `.invoice_id`, `.custom_field`, `.transaction_amount.value`, `.fee_amount`, `.transaction_status`),
  `.total_pages`, `.total_items`. Loop pages 1..total_pages for whole-range coverage.

### Return-None operations in scope: `vault.delete_payment_token` only (raw peer for status).

### Error handling ladder (per python-error-handling)
`except ApiError`: first `isinstance(e.error, OAuthProviderError)` → config error (502); then status map
(401/403→502, 429→503, 400/404/409/422 with typed `Error`→client fault, pass `Error.details[].issue`);
else RawError→502. `except ValidationError`/`ValueError` (decode) → outcome-unknown, re-read state.
`except (httpx.ConnectError,ConnectTimeout,PoolTimeout,ProxyError)` → known-not-sent 502; `except httpx.RequestError`
→ unknown 504. Expired-auth at capture detected via `Error.details[].issue == "AUTHORIZATION_EXPIRED"`
(or capture 422 `INVALID_RESOURCE_ID`) → reauthorize then recapture.

## CROSS-OPERATION INVARIANTS
| invariant | operations | enforced where |
|---|---|---|
| `card.vault_id` passed to authorize_order must be a payment token id from create_payment_token owned by the caller | orders.authorize_order ← vault.create_payment_token | implementation (SavedPaymentMethod filtered by user) |
| capture uses the `authorization_id` from this order's authorize_order | payments.capture_authorized_payment ← orders.authorize_order | implementation (PayPalPayment.authorization_id) |
| refund/void act on capture_id/authorization_id stored from prior steps | payments.refund_captured_payment/void_payment ← capture/authorize | implementation (PayPalPayment row) |
| reconciliation joins transaction_info.custom_field/invoice_id back to order number | transaction_search ← create_order custom_id/invoice_id | implementation |

## REQUIRED READING (load before coding)
- MUST load `python-client-initialization` (done) — client build/lifetime, sync choice.
- MUST load `python-authentication` (done) — oauth2, secret loading, OAuthProviderError.
- MUST load `python-calling-endpoints` (done) — signatures, prefer defaults narrowing responses, raw mode.
- MUST load `python-error-handling` (done) — ApiError union, decode/transport failures, status mapping.
- MUST load `python-models` — building request models, UNSET vs None, open enums, aliases.
- MUST load `python-testing` — seam is transport protocol / respx; test error+decode paths.

## Assumptions & Blockers
- Stale-auth renewal path can't be time-triggered in sandbox (needs >3 days); implemented defensively
  keyed on the capture error `issue`, verified structurally. Documented, not a blocker.
- Reconciliation range covering just-created payments may be empty (reporting lag) — expected, per task.
- No blockers; proceed.
