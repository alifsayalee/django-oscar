# PayPal Server SDK (Python) integration plan — django-oscar sandbox

Add PayPal card payments + saved cards to the `sandbox/` site as a new Django app
`apps.payments_api`, routed under `/api/`. Reuse Oscar's own order/line models.

## Toolchain / environment (verified)
- Python 3.11 via `py -3.11`. venv at `repo/.venv`. Project installed `pip install -e .[test]`.
- PayPal SDK installed into the same venv: `pip install "paypal @ git+https://github.com/context-plugins/paypal-python-sdk.git@main"` → **distribution & import root `paypal`, version 2.29** (the getting-started snapshot's `pay_pal_server_sdk` name is stale; the installed package is `paypal` — verified by import).
- Django 5.2.17, Oscar (sandbox). SQLite. Ports: base 36400, size 20 → bind 36400.
- SDK map cloned to scratchpad `paypal-python-sdk/` (branch main, matches installed 2.29).
- Sync host (Django WSGI) → **sync `PaypalClient`**. Module-level lazy singleton, `atexit` close.

## Credentials (settings.py, via `env(...)`, names only — never values)
`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENVIRONMENT`, `PAYPAL_CURRENCY`,
`PAYPAL_BASE_URL` (optional). When `PAYPAL_BASE_URL` set → pass verbatim as `base_url` (moves the
OAuth token endpoint too, per map). Else pass `base_url=None` → SDK default
`https://api-m.sandbox.paypal.com`. Verified live: env has sandbox creds, CURRENCY=USD, no BASE_URL.

## Client construction (python-client-initialization)
`PaypalClient(oauth2=ClientCredentials(client_id, client_secret), base_url=PAYPAL_BASE_URL or None, timeout=30.0)`.
Keyword-only. Long-lived singleton. `oauth2` MUST be set (omitting = unauthenticated, no error). Token
fetched lazily on first call; a bad-cred failure surfaces as `ApiError` with `OAuthProviderError` payload
out of the first operation — treat as configuration error.

## SMOKE RESULTS (live sandbox — authoritative decisions)
1. **Direct-card AUTHORIZE auto-authorizes at create.** `create_order(intent=AUTHORIZE,
   payment_source.card=<full card>)` returns status `COMPLETED` with the authorization already at
   `purchase_units[0].payments.authorizations[0]` (status `CREATED`). Calling `authorize_order` after
   is **422 ORDER_ALREADY_AUTHORIZED**. → `/pay` = create_order only; read the auth off the response.
   Same for vault_id card.
2. **Capture** returns `seller_receivable_breakdown` {gross, paypal_fee, net_amount}. Verified 24.99 →
   fee 1.14, net 23.85.
3. **Refund idempotency** via `PayPal-Request-Id` (`pay_pal_request_id`): same id → same refund id;
   distinct id → distinct partial refund. Over-refund → **422 REFUND_AMOUNT_EXCEEDED** (PayPal enforces;
   we also guard).
4. **`void_payment` returns 204 (empty) under default `prefer=return=minimal`** → SDK decode raises
   `ValueError` in BOTH response modes (the decode-failure trap). **FIX: always pass
   `prefer="return=representation"`** → 200 + body (status VOIDED). Verified.
5. **`reauthorize_payment`** on a fresh auth → **422 REAUTHORIZATION_TOO_SOON** ("only Day 4–29"). The
   success path is not reproducible on a fresh sandbox auth (age-gated). Implement per documented errors;
   this is a sandbox timing limitation, NOT a gap.
6. **`delete_payment_token` returns 204**, read via `with_raw_response` (`payload=None`, status 204). No
   ValueError because parsed return type is `None`.
7. **vault `list_customer_payment_tokens` lags** (0 immediately after create) → **list saved cards from
   our own DB**, not from PayPal.
8. **`search_transactions`**: 1177 items / 12 pages / 100 per page → **must paginate all pages**, and
   chunk ranges >31 days (PayPal per-request limit). Empty recent range is expected (reporting lag).

## Contract sheet (all facts from SDK map + source, verified live)
Sync client. All bodies passed as typed models. `Optional[T]` = `T | UNSET` (never pass `None`).
Money = `str` scaled to currency: `f"{Decimal(x):.2f}"`. Enums open (accept member or wire str).

| Operation | Call | Body model (key required members) | Returns | Notes |
|---|---|---|---|---|
| create order | `client.orders.create_order(body, pay_pal_request_id=?, prefer="return=representation")` | `OrderRequest(intent="AUTHORIZE", purchase_units=[PurchaseUnitRequest(amount=AmountWithBreakdown(currency_code,value), custom_id)], payment_source=PaymentSource(card=CardRequest(...)))` | `Order` (status, purchase_units[].payments.authorizations[]) | `CardRequest`: one-off {number,expiry"YYYY-MM",security_code,name,billing_address?} OR saved {vault_id}. Error `Error|RawError` [400,401,422] |
| capture | `client.payments.capture_authorized_payment(authorization_id, pay_pal_request_id=?, prefer="return=representation")` | `CaptureRequest` optional (omit) | `CapturedPayment` (id, status, seller_receivable_breakdown{gross_amount,paypal_fee,net_amount}) | Error `Error|RawError` [400,401,403,404,409,422] |
| reauthorize | `client.payments.reauthorize_payment(authorization_id, pay_pal_request_id=?, prefer="return=representation")` | `ReauthorizeRequest(amount=Money)?` omit | `PaymentAuthorization` (id,status,expiration_time) | age-gated 422 in sandbox |
| void | `client.payments.void_payment(authorization_id, prefer="return=representation")` | — | `PaymentAuthorization` (status VOIDED) | MUST pass representation (else 204→ValueError) |
| refund | `client.payments.refund_captured_payment(capture_id, pay_pal_request_id=<idem key>, body, prefer="return=representation")` | `RefundRequest(amount=Money(currency_code,value))` (omit amount = full) | `Refund` (id,status,seller_payable_breakdown) | idem via request id |
| create setup token | `client.vault.create_setup_token(body, pay_pal_request_id=?)` | `SetupTokenRequest(customer=Customer(id)?, payment_source=SetupTokenRequestPaymentSource(card=SetupTokenRequestCard(number,expiry,security_code,name,billing_address?)))` | `SetupTokenResponse(id, customer)` | |
| create payment token | `client.vault.create_payment_token(body, pay_pal_request_id=?)` | `PaymentTokenRequest(customer=Customer(id)?, payment_source=PaymentTokenRequestPaymentSource(token=VaultTokenRequest(id=<setup id>, type_="SETUP_TOKEN")))` | `PaymentTokenResponse(id, customer.id, payment_source.card{last_digits,brand,expiry,name})` | store token id + card metadata in our DB |
| list tokens | `client.vault.list_customer_payment_tokens(customer_id, page_size, page, total_required=True)` | — | `CustomerVaultPaymentTokensResponse` | we list from DB (lag); used for reconciliation cross-check only if needed |
| delete token | `client.vault.with_raw_response.delete_payment_token(id)` | — | `ApiResult[None,...]` status 204 | raw mode; read status |
| search txns | `client.transaction_search.search_transactions(start_date, end_date, fields="all", page_size=100, page=N)` | — | `SearchResponse(transaction_details[].transaction_info{transaction_id,transaction_status,transaction_amount}, total_pages, total_items)` | **Case B: error is always `RawError`**. Paginate all pages; chunk >31d |

Error unions: all above are `Error | RawError` except `search_transactions` (RawError only) and
`create/delete` vault (`Error | RawError`). `Error` distinguishing members: `name`, `message`,
`debug_id`, `details[].issue`.

Response-member guards (decode/UNSET trap): after create assert an authorization id exists; after
capture assert `id`; after refund assert `id`. `UNSET` never crosses our API boundary (map to None).

## App design
- **Models** (`apps/payments_api/models.py`):
  - `PayPalCustomer(user O2O, paypal_customer_id)` — groups a user's vault tokens.
  - `SavedPaymentMethod(user FK, paypal_token_id, brand, last_digits, expiry, label, created)` — exposed id = pk.
  - `OrderPayment(order O2O oscar order, currency, status, paypal_order_id, authorization_id,
    authorization_status, authorization_expiry, capture_id, capture_status, captured_amount,
    paypal_fee, net_amount, ts)`. status: PENDING/AUTHORIZED/CAPTURED/PARTIALLY_REFUNDED/REFUNDED/VOIDED/FAILED.
  - `PaymentRefund(payment FK, refund_id, amount, status, idempotency_key, created)`; unique(payment, idempotency_key).
- **gateway.py**: builds requests, one error boundary translating `ApiError`(→`OAuthProviderError` config /
  typed `Error` 4xx / `RawError`), `ValidationError`, `httpx.HTTPError` into `PayPalError(status, message, issues)`.
- **service.py**: order placement (Basket + NoShippingRequired + OrderTotalCalculator + OrderCreator),
  authorize/fulfil/cancel/refund/save/list/delete/reconcile; DB guards (select_for_update) for idempotency.
- **views.py**: plain Django `View` + `JsonResponse`, `csrf_exempt`, session auth. Base enforces
  auth(401)/staff(403)/ownership(404). Operator: fulfil, cancel, reconciliation. Refund + all others shopper-scoped.
- **urls.py** wired into `sandbox/urls.py` under `/api/`.
- Oscar status transitions: create→Pending; pay→Being processed; fulfil→Complete; cancel→Cancelled.

## Idempotency
- pay: DB guard (only if OrderPayment.status==PENDING) + `pay_pal_request_id=f"auth-{number}"`.
- fulfil: DB guard (skip if already captured) + `pay_pal_request_id=f"cap-{number}"`.
- refund: caller idempotency key → PaymentRefund unique row + `pay_pal_request_id=key`. Guard remaining ≤ captured.

## STOP-and-report conditions
- 3DS/browser challenge: if create returns no authorization and status/links indicate PAYER_ACTION_REQUIRED →
  return 422 "browser approval required" (do NOT build approval round-trip). Not triggered by 4111 w/o SCA.
- Any needed capability the plugin lacks → stop. (None found; all flows verified live.)

## Resilience
- SDK has **no retries** (by design). Timeout 30s. We do not add blind retries to writes (avoid double
  charge); idempotency keys + DB guards protect re-submits. Reads (reconciliation) paginate sequentially.

## REQUIRED READING (loaded before coding)
- python-client-initialization — MUST load (client singleton, close). ✅
- python-calling-endpoints — MUST load (keyword-only, raw vs parsed, `-> None`). ✅
- python-error-handling — MUST load (ApiError union, decode/ValueError trap, OAuthProviderError). ✅
- python-models — MUST load (UNSET vs None, Money as str/Decimal, open enums, wire alias `type_`). ✅
- python-authentication — MUST load (oauth2 ClientCredentials, lazy token, base_url moves token). ✅
- python-configuration-resilience — MUST load (no retries, timeout, base_url). ✅
- python-testing — MUST load before any test/verification-with-fake-transport. (load next)
