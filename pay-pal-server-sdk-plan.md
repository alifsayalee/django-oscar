# PayPal integration plan — django-oscar sandbox

PayPal payments + saved cards on the sandbox site, as a new Django app `apps.payments`
under `sandbox/apps/`, wired into `sandbox/urls.py` under `/api/`. Reuses Oscar's
`order.Order`/`order.Line` and `payment.Source`/`payment.Transaction`. All PayPal calls go
through the **`paypal` Python SDK** (root package `paypal`, dist `paypal` 2.29).

## Toolchain / environment (verified)
- `py -3.11`, venv at `repo/.venv`. Installed editable `django-oscar 4.2` (Django 5.2.17) with
  `[test]` extras; installed SDK `paypal 2.29` (from source clone) + `mypy`.
- DB: sqlite at `sandbox/db.sqlite`. Built: migrate + auth.json (users 1 `superuser`,
  2 `staff`, both `is_staff`) + child_products.json (11 products, stockrecords 3/4 = Django
  T-shirt @ 15.00 GBP with stock). offers/ranges/orders demo fixtures fail on absent
  stockrecords — not needed.
- Settings module `settings` resolved from `sandbox/`. `ROOT_URLCONF='urls'`, `DEBUG=True`,
  `ATOMIC_REQUESTS=True`. Catalogue currency GBP; PayPal currency from `PAYPAL_CURRENCY` (USD).
- Run checks: `../.venv/Scripts/python manage.py test apps.payments` from `sandbox/`;
  `mypy` on touched files.

## Sync vs async
**Sync.** Django under WSGI. Use `PaypalClient` (alias `Client`); teardown `client.close()`.
Client is module-scoped singleton, built lazily (`apps/payments/gateway.py`).

## Credentials & settings (sandbox/settings.py — names only, never values)
Read via `environ.Env`: `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENVIRONMENT`,
`PAYPAL_CURRENCY`, `PAYPAL_BASE_URL` (optional). Client: `oauth2=ClientCredentials(id,secret)`,
`base_url = PAYPAL_BASE_URL or None` (None → SDK default sandbox `https://api-m.sandbox.paypal.com`;
token endpoint derives from base_url). `timeout=60.0`. If `PAYPAL_BASE_URL` set, pass verbatim.

## Architecture / files (all new, additive)
- `sandbox/apps/payments/__init__.py`
- `apps.py` — `PaymentsConfig` (name `apps.payments`, `default_auto_field`).
- `gateway.py` — SDK client singleton + all PayPal call wrappers + one error boundary
  (`PayPalError` with http status + operator-facing message). Money helper `money(Decimal)->str`.
- `models.py` — `PayPalPayment`, `SavedCard`, `PayPalRefund` (see below).
- `services.py` — order placement (Oscar basket→place_order) + payment orchestration
  (pay/fulfil/cancel/refund) + saved-card save/list/delete + reconciliation.
- `views.py` — JSON endpoints (plain Django views, `JsonResponse`), session auth,
  `csrf_exempt` (session-authenticated JSON API), staff gate for operator actions.
- `urls.py` — routes under `/api/`.
- `migrations/`.
- `tests.py` — unit tests with a **faked SDK transport** (respx/httpx not needed; fake the
  gateway seam) + service-level tests with mocked gateway.
- `sandbox/settings.py` — add PayPal settings block + register app in `INSTALLED_APPS`.
- `sandbox/urls.py` — include `apps.payments.urls` under `api/`.

No DRF (not installed). No new infra.

## Data model (app-owned PayPal state; Oscar Source/Transaction = money ledger)
- `PayPalPayment(order OneToOne→order.Order, source FK→payment.Source, currency, status,
  paypal_order_id, authorization_id, authorization_status, authorization_expiry,
  capture_id, capture_status, gross_amount, paypal_fee, net_amount, created/updated)`.
  `status` ∈ {AWAITING_PAYMENT, AUTHORIZED, CAPTURED, PARTIALLY_REFUNDED, REFUNDED,
  VOIDED, FAILED}.
- `SavedCard(user FK, vault_id unique, paypal_customer_id, brand, last_digits, expiry,
  cardholder_name, created, deleted flag)`. No PAN/CVV ever. Ownership by `user`.
- `PayPalRefund(payment FK, idempotency_key, refund_id, amount, status, unique(payment,
  idempotency_key))` — refund idempotency + ledger.

## Order/fulfilment status mapping (Oscar pipeline Pending→Being processed→Complete/Cancelled)
- POST /api/orders → Oscar order `Pending`, PayPalPayment `AWAITING_PAYMENT`.
- pay → `Being processed`, PayPalPayment `AUTHORIZED`.
- fulfil → `Complete`, PayPalPayment `CAPTURED`.
- cancel (pre-fulfil) → `Cancelled`, PayPalPayment `VOIDED`.
- refund (post-fulfil) → order stays `Complete`; payment `PARTIALLY_REFUNDED`/`REFUNDED`.

## Endpoints (all under /api/, session auth; operator = is_staff)
| Method/path | who | action |
|---|---|---|
| POST /api/orders | shopper | build basket from {items:[{product_id,quantity}]}, place Oscar order (own user), create PayPalPayment AWAITING. → `orderId`=order.number |
| POST /api/orders/{orderId}/pay | shopper(owner) | create PayPal order intent=AUTHORIZE + card{raw or saved vault_id}; hold=order total. Idempotent. |
| POST /api/orders/{orderId}/fulfil | staff | capture authorization; renew if stale; record gross/fee/net; order→Complete |
| POST /api/orders/{orderId}/cancel | shopper(owner) or staff | void authorization pre-fulfil; order→Cancelled |
| POST /api/orders/{orderId}/refunds | shopper(owner) or staff | refund captured, full/partial, idempotency key; → `refundId` |
| GET /api/my-orders | shopper | caller's orders + payment state |
| POST /api/payment-methods | shopper | vault a card; → `paymentMethodId`; safe descriptor only |
| GET /api/payment-methods | shopper | caller's saved cards |
| DELETE /api/payment-methods/{id} | shopper(owner) | delete vault token + local row |
| GET /api/reconciliation?from&to | staff | search_transactions over full range (all pages) vs app orders |

Ownership: every shopper endpoint filters by `request.user`; 404 on another user's object.

## Idempotency (verified behaviours)
- create_order/pay: deterministic `PayPal-Request-Id = f"pay-{order.number}"`; a second pay
  when already AUTHORIZED returns existing state (guard on PayPalPayment.status + stored ids).
- capture: `PayPal-Request-Id = f"capture-{order.number}"`; guard on capture_id present.
- refund: caller-supplied idempotency key → `PayPalRefund` unique row (claim-first); on repeat
  key return stored refund. PayPal itself dedupes same PayPal-Request-Id (verified: same key →
  same refund id).
- Amounts: value from Oscar order total (`total_incl_tax`), paired with PAYPAL_CURRENCY, 2dp
  via `Decimal`. Hold == order total to the cent.
- Refund cap: sum(refunds) may never exceed captured gross; enforce in app before calling.

---

# CONTRACT SHEET (paypal SDK 2.29 — verified via SDK map + live sandbox smoke)

**Client**: `from paypal import PaypalClient` / `from paypal.core import ClientCredentials,
ApiError, RawError, Success, Failure, UNSET`. Constructor keyword-only:
`PaypalClient(*, base_url=None, timeout=30.0, oauth2=ClientCredentials|dict, ...)`.
Base URL default (base_url=None) = `https://api-m.sandbox.paypal.com`. **No retries** in SDK.
Token fetched lazily → a bad credential surfaces as `ApiError` with `OAuthProviderError` payload
out of the first operation call.

**Response mode**: use **parsed** calls (raise `ApiError`) everywhere EXCEPT where noted.
`prefer="return=representation"` REQUIRED to get full bodies (fee/net on capture; body on void).

**Operations in scope** (accessor · positional · key kwargs · returns · error union):

1. `client.orders.create_order(body: OrderRequest, *, pay_pal_request_id, prefer)` →
   `Order`; err `CreateOrderErrorBody = Error|RawError` [400,401,422].
   - body: `OrderRequest(intent="AUTHORIZE", purchase_units=[PurchaseUnitRequest(
     amount=AmountWithBreakdown(currency_code, value), invoice_id, custom_id)],
     payment_source=PaymentSource(card=CardRequest(...)))`.
   - **VERIFIED: with payment_source.card the authorization is created INLINE by create_order;
     do NOT call authorize_order (that → 422 ORDER_ALREADY_AUTHORIZED).**
   - **VERIFIED: `pay_pal_request_id` is MANDATORY when payment_source present (else 400
     PAYPAL_REQUEST_ID_REQUIRED).**
   - Auth id/status: `order.purchase_units[0].payments.authorizations[0].{id,status}`
     (status CREATED = hold placed, money held not captured).
   - CardRequest raw: `name,number,expiry("YYYY-MM"),security_code,billing_address=Address(
     address_line_1,admin_area_2,admin_area_1,postal_code,country_code)`.
   - CardRequest saved: `CardRequest(vault_id=<token id>)`.
   - `intent` enum `CheckoutPaymentIntent`: AUTHORIZE|CAPTURE. Money value = `str` 2dp.
2. `client.payments.capture_authorized_payment(authorization_id, *, pay_pal_request_id, prefer,
   body=None)` → `CapturedPayment`; err `Error|RawError` [400,401,403,404,409,422; RawError 500].
   - VERIFIED gross/fee/net at `cap.seller_receivable_breakdown.{gross_amount,paypal_fee,
     net_amount}.value` (needs prefer=representation). `cap.id`=capture id, `cap.status`
     COMPLETED. Idempotent on pay_pal_request_id.
3. `client.payments.void_payment(authorization_id, *, prefer, pay_pal_request_id)` →
   `PaymentAuthorization`; err `Error|RawError` [401,403,404,409,422; RawError 500].
   - **VERIFIED: pass `prefer="return=representation"` → returns VOIDED body cleanly. Without it
     PayPal returns 204 empty → SDK decoder raises `ValueError` (NOT ApiError, bypasses raw
     mode). Handling: prefer=representation + `except ValueError`→treat as success, re-read via
     get_authorized_payment (status VOIDED).**
4. `client.payments.refund_captured_payment(capture_id, *, pay_pal_request_id, prefer, body:
   RefundRequest)` → `Refund`; err `Error|RawError` [400,401,403,404,409,422; RawError 500].
   - full refund = empty body; partial = `RefundRequest(amount=Money(currency_code,value),
     invoice_id?, custom_id?, note_to_payer?)`. `refund.id`,`refund.status` COMPLETED.
   - **VERIFIED: same pay_pal_request_id → same refund id (idempotent).**
5. `client.payments.reauthorize_payment(authorization_id, *, pay_pal_request_id, prefer, body:
   ReauthorizeRequest(amount=Money)?)` → `PaymentAuthorization`; err `Error|RawError`
   [400,401,403,404,422; RawError 500]. Used to renew a stale auth before capture.
6. `client.payments.get_authorized_payment(authorization_id, *)` → `PaymentAuthorization`
   (status/expiration_time); err `Error|RawError` [401,403,404].
7. `client.payments.get_captured_payment(capture_id, *)` → `CapturedPayment`; err [401,403,404].
8. `client.vault.create_setup_token(body: SetupTokenRequest, *, pay_pal_request_id)` →
   `SetupTokenResponse(id,status)`; err `Error|RawError` [400,403,422,500].
   - body: `SetupTokenRequest(customer=Customer(merchant_customer_id=f"oscar-{user.pk}"),
     payment_source=SetupTokenRequestPaymentSource(card=SetupTokenRequestCard(name,number,
     expiry,security_code,billing_address)))`.
9. `client.vault.create_payment_token(body: PaymentTokenRequest, *, pay_pal_request_id)` →
   `PaymentTokenResponse`; err `Error|RawError` [400,403,404,422,500].
   - body: `PaymentTokenRequest(payment_source=PaymentTokenRequestPaymentSource(
     token=VaultTokenRequest(id=setup_token_id, type_="SETUP_TOKEN")))`.
   - VERIFIED response: `pt.id`=vault id; `pt.customer.id`=paypal customer id;
     `pt.payment_source.card.{brand,last_digits,expiry,name}` = safe descriptor.
   - `VaultTokenRequest.type_` wire alias `type`; enum `VaultTokenRequestType.SETUP_TOKEN`.
10. `client.vault.delete_payment_token(id, *)` → **None** (VERIFIED 204). Use parsed call
    (returns None on success); err `Error|RawError` [400,403,500].
11. `client.vault.list_customer_payment_tokens(customer_id, *, page_size=5, page=1,
    total_required=False)` → `CustomerVaultPaymentTokensResponse(payment_tokens[], total_items,
    total_pages)`; err [400,403,500]. (App uses own DB as source of truth; this is a cross-check.)
12. `client.transaction_search.search_transactions(start_date, end_date, *, fields=
    "transaction_info", transaction_id?, page_size=100, page=1)` → `SearchResponse(
    transaction_details[], total_items, total_pages, page)`; **Case B: err is `RawError` only**.
    - VERIFIED live: returns total_pages>1 → **must page 1..total_pages** to cover whole range.
    - `td.transaction_info.{transaction_id, transaction_amount(Money), invoice_id, custom_field,
      transaction_status, transaction_initiation_date}`. Match app orders by invoice_id.
    - Dates: ISO-8601 with tz, format `%Y-%m-%dT%H:%M:%S-0000` (or +HH:MM). Reporting lags →
      recent ranges may be empty (expected, not a gap).
    - **DO NOT retry/void irreversibly**; read-only.

**Enums** (wire values): CheckoutPaymentIntent {AUTHORIZE,CAPTURE}; AuthorizationStatus
{CREATED,CAPTURED,DENIED,PARTIALLY_CAPTURED,VOIDED,PENDING}; CaptureStatus {COMPLETED,DECLINED,
PARTIALLY_REFUNDED,PENDING,REFUNDED,FAILED}; RefundStatus {CANCELLED,FAILED,PENDING,COMPLETED};
VaultTokenRequestType {SETUP_TOKEN}; StoreInVaultInstruction {ON_SUCCESS}. Enums are OPEN
(`…OrStr`): compare by value string; an unknown value passes through as str.

**Models — Optional[T] = T|UNSET (no None arm).** Omit optional fields; never pass None. Money
is `str` scaled to currency: build with `Decimal`, `f"{d:.2f}"`. Read SDK values out via
`to_dict()` or explicit UNSET→None mapping before JSON to the HTTP client (UNSET is not JSON
serializable).

**Failure kinds to handle at the gateway boundary** (order: auth→typed→raw→decode→transport):
- `ApiError` + `isinstance(e.error, OAuthProviderError)` → config error (nothing sent).
- `ApiError` + typed `Error` → provider rejection, surface `.message`/`.details[].issue` + status.
- `ApiError` + `RawError` → `.text()`, status.
- `ValueError`/`pydantic.ValidationError` → decode failure. **Special-case void's empty 204 as
  success.** Otherwise: 2xx→outcome unknown (re-read); non-2xx→rejection.
- `httpx.HTTPError` → transport, outcome unknown.
- Assert required members after each write (auth id after pay, capture id after capture,
  vault id after tokenize) — absent = outcome unknown.

**PAYER_ACTION_REQUIRED / 3DS challenge**: if create_order returns order.status
PAYER_ACTION_REQUIRED or an authorization is not produced with an `approve` link requiring
browser, **STOP and surface an actionable error** (task rule) — do not build an approval
round-trip. (Not seen for test card 4111 direct card.)

## CROSS-OPERATION INVARIANTS
| invariant | operations | enforced where |
|---|---|---|
| pay's saved-card `vault_id` must be a token this shopper created (POST payment-methods) | create_order.payment_source.card.vault_id ← create_payment_token | app: SavedCard filtered by user, not deleted |
| capture/void/refund act on ids stored from this order's pay step | payments.* ← create_order | app: PayPalPayment.{authorization_id,capture_id} |
| refund total ≤ captured gross | refund_captured_payment ← capture_authorized_payment | app: sum(PayPalRefund.amount) ≤ gross |
| reconciliation lines PayPal txns to app orders by invoice_id | search_transactions ↔ PayPalPayment | app: invoice_id = f"oscar-{order.number}" |

## Assumptions (minor; proceed)
- Both seeded users are staff; create a non-staff shopper in tests/verification.
- Oscar order currency set to PAYPAL_CURRENCY at placement (value from catalogue total) so app
  records match the charged currency.
- CSRF: JSON API is session-authenticated; views `csrf_exempt` (documented), auth still enforced.

## REQUIRED READING (all loaded)
- MUST load python-client-initialization — client singleton, close(), sync. ✓
- MUST load python-authentication — oauth2 ClientCredentials, lazy token, OAuthProviderError. ✓
- MUST load python-calling-endpoints — prefer default narrows body; -> None ops. ✓
- MUST load python-models — Optional=UNSET, Money as str/Decimal, open enums, UNSET not JSON. ✓
- MUST load python-error-handling — single ApiError, decode ValueError bypasses modes (void!). ✓
- MUST load python-configuration-resilience — no retries, pagination bounds, reconciliation clock. ✓
- MUST load python-testing — before tests: fake transport seam. (load at test step)
