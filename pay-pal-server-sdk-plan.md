# PayPal integration — plan & contract sheet

Add PayPal card payments + saved cards to the django-oscar **sandbox** site as a new Django app
`sandbox/apps/paypal_checkout/`, routed under `/api/`. Reuse Oscar's Order/Line/Source/Transaction
models. All SDK facts below are grounded in the installed `paypal` package (v2.29) and the SDK map;
several were confirmed live against the sandbox credentials (see "Live smoke results").

## SDK identity (verified against installed package — skill snapshot had DRIFTED)
- Distribution & import root: **`paypal`** (NOT `pay_pal_server_sdk`). `pip install ../paypal-python-sdk` (non-editable, git build failed on network).
- Sync client: `from paypal import PaypalClient` (alias `Client`). Async: `AsyncPaypalClient`/`AsyncClient`.
- Construct keyword-only: `PaypalClient(oauth2=ClientCredentials(client_id=…, client_secret=…), base_url=…, timeout=30.0)`.
- Auth types & `ApiError`/`ApiResult`/`RawError`/`ClientCredentials` from `paypal.core`. Models `paypal.models`. Enums `paypal.models.enums`. Error unions `paypal.errors`.
- No environment enum. `base_url=None` → default `https://api-m.sandbox.paypal.com`. Token from `<base_url>/v1/oauth2/token`.
- **SDK does NO retries.** Timeout is single float mapped to httpx.

## Sync vs async — **SYNC**. Django sandbox views are sync WSGI (`sandbox/wsgi.py`, oscar views sync). Use `PaypalClient`; hold ONE module-level client, `close()` never needed for process-lifetime singleton (kept open; pool reused). Never mix async.

## Credentials (from Django settings in sandbox/settings.py — names only, never values)
Read env into settings: `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENVIRONMENT`, `PAYPAL_CURRENCY`, `PAYPAL_BASE_URL` (optional override; when set, pass verbatim as `base_url` to the client — moves token traffic too). Env currently: environment=sandbox, currency=USD, base_url unset. Ports 36500–36519.

## Client `.with_raw_response` access = property on controller: `client.payments.with_raw_response.void_payment(...)` (NOT `.method.with_raw_response`).

---
## Contract sheet — operations in scope (map-verified; keyword-only after `*`; every kw has a real default)

### Orders (`client.orders`, `paypal/apis/orders.py`)
- `create_order(body: OrderRequest|dict, *, pay_pal_request_id=None, prefer="return=minimal", …) -> Order`
  - body: `{"intent": "AUTHORIZE", "purchase_units": [PurchaseUnitRequest]}`. Pass `prefer="return=representation"` to get full body.
  - `PurchaseUnitRequest`: `amount: AmountWithBreakdown{currency_code:str, value:str}` (required); optional `reference_id`, `invoice_id`, `custom_id`, `description`. Set `invoice_id=custom_id=order.number` for reconciliation match.
  - Returns `Order`: `.id`, `.status` (OrderStatus enum: CREATED/APPROVED/COMPLETED/VOIDED/…), `.purchase_units[].payments.{authorizations,captures,refunds}`.
  - Error: `CreateOrderErrorBody` = `Error`[400,401,422] | `RawError`.
- `authorize_order(id: str, *, pay_pal_request_id=None, prefer="return=minimal", body: OrderAuthorizeRequest|dict=None, …) -> OrderAuthorizeResponse`
  - body: `{"payment_source": {"card": CardRequest}}`. Docstring: "a valid payment_source must be provided in the request" — so **create (no card) then authorize (with card) is the supported 2-step**.
  - `CardRequest` one-off: `{number, expiry:"YYYY-MM", security_code, name, billing_address: Address{address_line_1, admin_area_2(city), admin_area_1(state), postal_code, country_code(req)}}`.
  - `CardRequest` saved: `{"vault_id": <token id>}`.
  - Returns `OrderAuthorizeResponse` (has `.status`, `.purchase_units[].payments.authorizations[]` → `AuthorizationWithAdditionalData{id, status(AuthorizationStatus: CREATED/CAPTURED/VOIDED/…), amount:Money, expiration_time}`).
  - Use `prefer="return=representation"` to receive the authorization in the body.
  - Error: `AuthorizeOrderErrorBody` = `Error`[400,401,403,404,422,500] | RawError.

### Payments (`client.payments`, `paypal/apis/payments.py`)
- `capture_authorized_payment(authorization_id: str, *, pay_pal_request_id=None, prefer="return=minimal", body: CaptureRequest|dict=None, …) -> CapturedPayment`
  - body: `{"final_capture": True}` (full) or `{"amount": Money, "final_capture": True}`. Use `prefer="return=representation"`.
  - Returns `CapturedPayment`: `.id`, `.status`(CaptureStatus: COMPLETED/…), `.amount:Money`, `.seller_receivable_breakdown: SellerReceivableBreakdown{gross_amount:Money, paypal_fee:Money, net_amount:Money}` → **captured/fee/net**.
  - Error: `Error`[400,401,403,404,409,422] | RawError[500,…].
- `reauthorize_payment(authorization_id, *, pay_pal_request_id=None, prefer=…, body: ReauthorizeRequest|dict=None) -> PaymentAuthorization` — body `{"amount": Money}`. Used to renew a stale authorization before capture. (Sandbox: a fresh auth often can't be reauthorized; implement defensively.)
- `get_authorized_payment(authorization_id) -> PaymentAuthorization` — `.status` to check CREATED/EXPIRED/VOIDED.
- `refund_captured_payment(capture_id: str, *, pay_pal_request_id=None, prefer=…, body: RefundRequest|dict=None) -> Refund`
  - body: `{"amount": {"currency_code", "value"}, "invoice_id":…, "custom_id":…}`; omit amount for full refund. Use `prefer="return=representation"`.
  - `pay_pal_request_id` = caller idempotency key (repeat → same refund). Returns `Refund{id, status(RefundStatus: COMPLETED/…), amount:Money}`.
  - Error: `Error`[400,401,403,404,409,422] | RawError[500,…].
- `void_payment(authorization_id, *, prefer="return=minimal", pay_pal_request_id=None) -> PaymentAuthorization`
  - **GOTCHA: default returns 204 empty → decode ValueError in BOTH modes.** MUST pass `prefer="return=representation"` → 200 with body. Confirmed. Cancel path = void the authorization.

### Vault (`client.vault`, `paypal/apis/vault.py`)
- `create_payment_token(body: PaymentTokenRequest|dict, *, pay_pal_request_id=None) -> PaymentTokenResponse`
  - body save-card: `{"customer": {"id": <paypal customer id or omit on first>}, "payment_source": {"card": {number, expiry, security_code, name, billing_address}}}`.
  - Returns `PaymentTokenResponse`: `.id` (vault token id), `.customer.id` (CustomerResponse — persist for grouping/reuse), `.payment_source.card: CardPaymentTokenEntity{last_digits, brand(CardBrand), expiry, name}` → **safe description**.
  - Error: `Error`[400,403,404,422,500] | RawError.
- `delete_payment_token(id, *) -> None` — **returns 204/None; use `client.vault.with_raw_response.delete_payment_token(id)`, check `.response.status_code` in {204,404}** (404 = already gone, idempotent-OK). Error: `Error`[400,403,500] | RawError.
- `list_customer_payment_tokens(customer_id: str, *, page_size=5, page=1, total_required=False) -> CustomerVaultPaymentTokensResponse` (`.payment_tokens: list[PaymentTokenResponse]`, `.total_pages`). Not required for app flow (own DB is source of truth) — optional cross-check.

### Transaction search (`client.transaction_search`, reconciliation)
- `search_transactions(start_date: str, end_date: str, *, transaction_status=None, fields="transaction_info", balance_affecting_records_only="Y", page_size=100, page=1, …) -> SearchResponse`
  - dates ISO-8601 (e.g. `2026-09-01T00:00:00-0000`). `SearchResponse`: `.transaction_details: list[TransactionDetails]` → `.transaction_info: TransactionInformation{transaction_id, transaction_amount:Money, transaction_status, invoice_id, custom_field, transaction_initiation_date}`, `.total_pages`, `.page`. **Case B**: `.error` always `RawError`.
  - **Cover the whole range: loop page=1..total_pages.** Match `invoice_id`/`custom_field` to order.number.
  - Sandbox reporting LAGS: recent range may be empty — expected, not a gap.

### Error model
`ApiError` (`paypal.core`): `.error` (per-op union), `.status_code`, `.response`. `Error` body: `name:str, message:str, debug_id:str, details:list[ErrorDetails{field,issue,description}]`. Narrow `isinstance(e.error, Error)` for typed; else `RawError` (`.status_code`, `.text()`, `.json()`). **Decode failure raises ValueError/ValidationError, not ApiError** (204/empty bodies — handled per-op above). httpx transport errors arrive unwrapped.

---
## Live smoke results (sandbox, confirmed working)
create_order→CREATED · authorize+card→auth CREATED amt=25.00 · capture→COMPLETED gross 25.00/fee 1.14/net 23.86 · partial refund 10.00→COMPLETED · create_payment_token→token+customer+last4 1111/VISA · authorize+vault_id→auth CREATED · void(representation)→200 then GET=VOIDED · delete_payment_token(raw)→204.

---
## App design (`sandbox/apps/paypal_checkout/`)
INSTALLED_APPS entry `'apps.paypal_checkout'` (apps/ is top-level on sys.path since commands run from sandbox/). URL include in `sandbox/urls.py`: `path('api/', include('apps.paypal_checkout.urls'))` **outside** i18n_patterns.

### Models (own PayPal state; Oscar Order/Line/Source/Transaction reused for order + money)
- `PayPalPayment(OneToOne Order)`: `paypal_order_id`, `status`(PENDING/AUTHORIZED/CAPTURED/PARTIALLY_REFUNDED/REFUNDED/CANCELLED/FAILED), `authorization_id`, `capture_id`, `currency`, `authorized_amount`, `captured_amount`, `paypal_fee`, `net_amount`, `amount_refunded`, timestamps. Also mirror to Oscar `Source`(amount_allocated/debited/refunded) + `Transaction` for dashboard integration.
- `PayPalCustomer(OneToOne User)`: `paypal_customer_id` (persist from first vault response, reuse).
- `SavedCard(FK User)`: `paypal_vault_id`(unique), `brand`, `last_digits`, `expiry`, `cardholder_name`, `created`. **No PAN/CVV ever.**
- `Refund(FK PayPalPayment)`: `paypal_refund_id`, `amount`, `status`, `idempotency_key` (unique per payment). Distinct keys = distinct partial refunds; repeat key = return existing.

### Endpoints (DRF or plain Django JSON views — use plain Django JSON + session auth to avoid new deps; check DRF availability)
Shopper (login required, own data): POST /api/orders (orderId) · POST /api/orders/{id}/pay · POST /api/orders/{id}/cancel · POST /api/orders/{id}/refunds (refundId) · GET /api/my-orders · POST/GET/DELETE /api/payment-methods[/{id}].
Operator (is_staff): POST /api/orders/{id}/fulfil · GET /api/reconciliation?from=&to=.

### Idempotency
- pay: `select_for_update` on PayPalPayment; if already AUTHORIZED return current. Stable PayPal-Request-Id per (order, op) stored/derived → PayPal-level idempotency too.
- fulfil/capture: if already CAPTURED return current.
- refund: caller idempotency_key; unique(payment,key). If capture stale at fulfil → reauthorize then re-capture; if not renewable → clear operator error.
- Amounts to the cent from Oscar order.total_incl_tax; currency from settings.PAYPAL_CURRENCY. Guard: total refunded never exceeds captured.

### Ownership: every shopper endpoint filters by request.user; saved card belongs to saver; 404 (not 403) on others' resources to avoid enumeration.

## Assumptions & Blockers
- No blockers. All in-scope ops confirmed live. No 3DS challenge with test card (direct auth succeeded) — if a challenge (PAYER_ACTION_REQUIRED needing browser) ever returned on pay, STOP+report per task; detect by order/auth status.
- Reauthorize can't be forced stale in sandbox → implemented defensively, exercised only structurally.

## REQUIRED READING (load before implementing the governed step)
- `python-error-handling` — MUST load (error boundary; decode-failure/empty-body traps). [FLOOR]
- `python-client-initialization` — MUST load before constructing PaypalClient (lifetime/singleton). [FLOOR]
- `python-calling-endpoints` — MUST load before first call (kw-only split, raw vs parsed, with_raw_response). [read source; load]
- `python-models` — MUST load when building request bodies (Optional=UNSET not None, dict companions, enums open).
- `python-authentication` — MUST load when wiring oauth2 (failed token fetch bypasses non-raising mode).
- `python-testing` — MUST load before the verification script/tests (transport seam). [FLOOR]
