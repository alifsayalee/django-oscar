# PayPal integration plan — django-oscar sandbox

## SDK identity (verified against installed package + shipped map)
- Distribution/import root: **`paypal`** (map's ground truth; the getting-started snapshot's
  `pay_pal_server_sdk` name is stale). Installed into `.venv` from the local clone of
  `github.com/context-plugins/paypal-python-sdk@main`.
- Sync client: `from paypal import PaypalClient`; creds `from paypal.core import ClientCredentials`.
- Core runtime: `from paypal.core import ApiError, RawError, Success, Failure` (+ `OAuthProviderError` if present).
- **Sync** client (Django WSGI). Held as a lazily-built module-level singleton (client init skill:
  long-lived, app-scoped, token cache lives on the client). Not closed per request.
- Base URL: default `https://api-m.sandbox.paypal.com`. Override rule: if `PAYPAL_BASE_URL` set → use
  verbatim (covers token endpoint too). Else derive from `PAYPAL_ENVIRONMENT`
  (`live`/`production` → `https://api-m.paypal.com`, else sandbox).

## Empirically verified semantics (smoke tests against sandbox, card 4111 1111 1111 1111)
1. **Single-step authorize**: `orders.create_order(body, pay_pal_request_id=RID, prefer="return=representation")`
   with `intent="AUTHORIZE"` + `payment_source.card` → order `status=COMPLETED`, and
   `purchase_units[0].payments.authorizations[0]` present with `id` + `status="CREATED"` + `expiration_time`.
   No separate `authorize_order` needed. Saved-card pay: `payment_source.card = {"vault_id": <token>}`.
2. **Capture at fulfil**: `payments.capture_authorized_payment(auth_id, pay_pal_request_id=RID,
   prefer="return=representation", body={"final_capture": True})` → `CapturedPayment` with
   `seller_receivable_breakdown.gross_amount/paypal_fee/net_amount` (e.g. 10.00 / 0.75 / 9.25).
3. **Cancel before fulfil**: `payments.void_payment(auth_id, prefer="return=representation")` → `status=VOIDED`.
   ⚠️ With default `prefer="return=minimal"` void returns **204 empty → decoder raises ValueError**.
   Always pass `prefer="return=representation"`.
4. **Refund after fulfil**: `payments.refund_captured_payment(cap_id, pay_pal_request_id=KEY,
   prefer="return=representation", body={"amount": {"currency_code","value"}})` → `Refund`. Repeating the
   SAME `pay_pal_request_id` returns the SAME refund id (idempotent). Omit amount = full remaining
   (we always send explicit amount = remaining to avoid empty-body edge cases).
5. **Stale auth**: `payments.get_authorized_payment(auth_id)` → status; if `EXPIRED`,
   `payments.reauthorize_payment(auth_id, prefer="return=representation", body={"amount":{...}})` → new auth
   (new id) then capture the new id. If reauthorize fails (>29–30 days) → operator-actionable error.
6. **Vault save card**: `vault.create_payment_token({"payment_source":{"card":{number,expiry,security_code,
   name,billing_address}}, "customer":{"id":<existing?>}}, pay_pal_request_id=RID)` → `PaymentTokenResponse`
   with `.id` (vault token), `.customer.id`, `.payment_source.card.{last_digits,brand,expiry}`.
7. **Delete card**: `vault.with_raw_response.delete_payment_token(id)` → `Success`, HTTP **204** (return
   type None; raw peer is the way to read the status). `get_payment_token` on a deleted id → 404 with
   empty body → **ValueError**, not ApiError.
8. **list_customer_payment_tokens** lags (returned 0 right after create). → serve `GET /payment-methods`
   from the **local DB**, not this call.
9. **Reconciliation**: `transaction_search.search_transactions(start, end, fields="transaction_info",
   page_size=100, page=n)` (Case B: error is always `RawError`). Dates formatted `%Y-%m-%dT%H:%M:%S-0000`.
   Response `SearchResponse.transaction_details[].transaction_info.{invoice_id, custom_field,
   transaction_id, transaction_amount, fee_amount, transaction_status}`. Paginate via `total_pages`;
   chunk range into ≤31-day windows. `total_items` may be `UNSET`.

## Error handling (per python-error-handling)
Wrap every call: catch `ApiError` (check auth/`OAuthProviderError` first → 5xx config; typed `Error` →
map status; `RawError` → carry status+text), `ValueError`/`ValidationError` (empty/undecodable body —
"outcome unknown" but for the 204 void/delete cases treat as success via raw status), `httpx.HTTPError`
(unreachable). Never leak `str(e)`; log detail, return a written message. Assert on the members we
depend on (auth id, capture id, refund id) right after each call.

## Amounts
Value = order.total_incl_tax quantized to 2dp `str`. Currency = `settings.PAYPAL_CURRENCY`. Must equal
order total to the cent. `custom_id` and `invoice_id` on the purchase unit = Oscar order number (used
for reconciliation matching against `transaction_info.custom_field` / `invoice_id`).

## Idempotency
- Authorize (`/pay`): DB row `PayPalPayment` with unique `order`; a stored `auth_request_id`
  (PayPal-Request-Id). Re-`pay` when already AUTHORIZED/CAPTURED → return existing (no second auth).
  `select_for_update` to serialize double-clicks.
- Capture (`/fulfil`): short-circuit if `capture_id` present; stable `capture_request_id`.
- Refund: caller supplies idempotency key → unique `(payment, idempotency_key)`; reuse as
  PayPal-Request-Id. Repeat key → return stored refund. Distinct keys → distinct partial refunds.

## App layout — `sandbox/apps/paypal_api/` (INSTALLED_APPS `apps.paypal_api`, label `paypal_api`)
- `models.py`: `PayPalCustomer(user OneToOne, customer_id)`, `SavedCard(user FK, vault_id unique, brand,
  last_digits, expiry, label)`, `PayPalPayment(order OneToOne, user FK, currency, amount, status,
  paypal_order_id, authorization_id/status/expiry, capture_id/status, captured_amount, paypal_fee,
  net_amount, auth_request_id, capture_request_id)`, `PayPalRefund(payment FK, refund_id, amount,
  currency, status, idempotency_key; unique_together (payment, idempotency_key))`. Payment status enum:
  PENDING_PAYMENT/AUTHORIZED/CAPTURED/PARTIALLY_REFUNDED/REFUNDED/CANCELLED/FAILED.
- Also record into Oscar's own payment ledger: `SourceType('PayPal')` + `Source(order)` with
  allocate/debit/refund + `Transaction`s (reuse `src/oscar/apps/payment` models).
- `gateway.py`: client singleton + base-url derivation + thin wrappers translating SDK failures to a
  `PayPalError(message, http_status, code)` domain exception.
- `services.py`: order creation (reuse `Basket`+`strategy.Default`+`Free`+`OrderTotalCalculator`+
  `OrderCreator.place_order`), pay/fulfil/cancel/refund, save/list/delete card, reconcile.
- `views.py` (plain Django JSON views, `login_required`; staff-gated fulfil/cancel/reconciliation;
  `csrf_exempt` so the session-auth API is drivable by a programmatic caller), `urls.py`.
- Wire: `sandbox/settings.py` adds 5 `PAYPAL_*` settings (read via env, no hardcoded values) + app to
  INSTALLED_APPS; `sandbox/urls.py` includes `api/` → `apps.paypal_api.urls`.

## Endpoints
POST `/api/orders` → {orderId}; POST `/api/orders/{id}/pay`; POST `/api/orders/{id}/fulfil` (staff);
POST `/api/orders/{id}/cancel` (staff); POST `/api/orders/{id}/refunds` → {refundId}; GET `/api/my-orders`;
GET `/api/reconciliation?from&to` (staff); POST/GET `/api/payment-methods` (POST → {paymentMethodId});
DELETE `/api/payment-methods/{id}`. Ownership scoping: shopper endpoints act only on caller's own
orders/cards; staff endpoints require `is_staff`.

## Challenge (3DS) handling
If create_order returns no authorization (e.g. status needs payer action / approve link) → raise
PayPalError telling the operator a browser challenge was required. Sandbox test card does not trigger it.

## REQUIRED READING (loaded)
- python-getting-started (lookup layer) — loaded.
- python-error-handling — loaded (empty-body ValueError, auth-first ladder, no retries).
- python-client-initialization — loaded (sync, singleton, close obligation).
- Contract facts (signatures, models, enums) taken from the SDK map + source modules above; no memory.
