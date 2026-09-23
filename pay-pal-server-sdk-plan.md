# PayPal integration plan — django-oscar sandbox (`sandbox/apps/payments`)

## Scope

New Django app `sandbox/apps/payments` (label `payments`), mounted at `/api/` in `sandbox/urls.py`
(outside `i18n_patterns`, next to `admin/`). JSON endpoints:

| Route | Who | PayPal ops |
|---|---|---|
| `POST /api/orders` | shopper | none (Oscar `OrderCreator`) |
| `POST /api/orders/{orderId}/pay` | shopper (own order) | `orders.create_order` (single-step, intent AUTHORIZE, card or `vault_id`) |
| `POST /api/orders/{orderId}/fulfil` | `is_staff` | `payments.get_authorized_payment`, `payments.reauthorize_payment`, `payments.capture_authorized_payment` |
| `POST /api/orders/{orderId}/cancel` | `is_staff` | `payments.void_payment` |
| `POST /api/orders/{orderId}/refunds` | shopper (own order) | `payments.refund_captured_payment` |
| `GET /api/my-orders` | shopper | none |
| `GET /api/reconciliation?from&to` | `is_staff` | `transaction_search.search_transactions` (paged, ≤31-day chunks) |
| `POST/GET /api/payment-methods`, `DELETE /api/payment-methods/{id}` | shopper | `vault.create_payment_token`, `vault.delete_payment_token` |
| `GET /api/csrf`, `POST /api/session` | anyone | none — helpers around Django's own session login so the API is drivable without HTML forms |

`orderId` = Oscar `Order.number`. Oscar models reused: `order.Order`/`Line` (via a transient
`basket.Basket` + `OrderCreator.place_order`), order status pipeline (`Pending` → `Being processed` →
`Complete` / `Cancelled`, via `Order.set_status`), `payment.Source`/`payment.Transaction` (amounts
allocated/debited/refunded — visible in Oscar's dashboard). New models hold only PayPal state Oscar has
no place for: `PayPalPayment` (1:1 order), `PayPalRefund`, `SavedCard`, `PayPalCustomer`,
`PayPalTransaction` (ledger of every PayPal id we created, with PayPal's timestamp — reconciliation's
local side).

## Host decisions

- **Sync client** (`PaypalClient`): Django under WSGI, sync views. Held as a lazily built process-wide
  singleton (`apps/payments/gateway.py:get_client()`), built on first use (after any fork), closed via
  `atexit`. Never per request.
- `ATOMIC_REQUESTS = True` in sandbox settings → every view that talks to PayPal is
  `transaction.non_atomic_requests` so the claim row commits **before** the provider call.
- Credentials: `sandbox/settings.py` reads `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`,
  `PAYPAL_ENVIRONMENT` (default `sandbox`), `PAYPAL_CURRENCY` (default `USD`), `PAYPAL_BASE_URL`
  (default empty) from env with empty defaults; `get_client()` refuses missing id/secret and an unknown
  environment. Base URL: `PAYPAL_BASE_URL` verbatim if set, else `{"sandbox": "https://api-m.sandbox.paypal.com",
  "live": "https://api-m.paypal.com"}[PAYPAL_ENVIRONMENT]` (KeyError → ImproperlyConfigured). Always passed
  explicitly as `base_url=` (token URL derives from it — SDK map, Servers & auth).
  The live host string is YOUR CALL (the SDK declares only the sandbox server); the override exists for it.
- Currency: `PAYPAL_CURRENCY`; amounts are catalogue `StockRecord.price` numbers (fixtures are GBP-priced;
  the task mandates currency from configuration). Money is `Decimal`, formatted with the currency exponent
  (JPY/KRW 0, KWD/BHD/TND 3, else 2).
- Timeout 20 s client-wide (`timeout=20.0`); logging transport wrapper (method, path, status, ms — no
  headers/bodies) around `HttpxClient`.

## SDK identity (verified against installed package)

- Distribution `paypal` 2.29, import root **`paypal`** (the getting-started skill says
  `pay_pal_server_sdk`; the installed package and its `sdk-map.md` say `paypal` — source wins).
  Installed into `venv` from `git+https://github.com/context-plugins/paypal-python-sdk.git@main`
  (fell back to a non-editable install of the same clone after a transient network reset).
- `from paypal import PaypalClient`; `from paypal.core import ApiError, RawError, ClientCredentials,
  OAuthProviderError, HttpxClient, HttpRequest, HttpResponse, UNSET, UnsetType, Success, Failure`;
  models from `paypal.models`; enums from `paypal.models.enums`.
- Constructor keyword-only: `base_url`, `timeout`, `custom_http_client`, `oauth2`, `oauth2_token_source`.
- **No retries in the SDK.** We add none for writes; writes are made safe by PayPal-Request-Id +
  claim rows. Reads (reconciliation paging) are not retried either — a failure fails the report (502/504).
- Every keyword-only param has a real default — never pass `None` defensively.
- `Optional[T]` = `T | UnsetType`; never pass `None` for it. Read with `isinstance(x, UnsetType)`.

## Contract sheet

All ops: raise `ApiError`; `.error` = `Error | RawError` (Case A) unless noted. `Error`
(`paypal/models/error.py`): `name: str`, `message: str`, `debug_id: str` required; `details:
Optional[list[ErrorDetails]]` (`ErrorDetails.issue: str` required, `description: Optional[str]`).
Statuses NOT listed in an op's arms arrive as `RawError` (smoke: create_order 403 → RawError).
Token-fetch failure: `ApiError` with `.error` `OAuthProviderError | RawError` — checked first.

### orders.create_order
`create_order(body: OrderRequest | OrderRequestDict, *, pay_pal_request_id=None, prefer="return=minimal", ...)` → `Order`.
Error arms: `Error` [400, 401, 422] · `RawError` [rest]. Docstring: `PayPal-Request-Id` **mandatory for
single-step create with a card payment source**; keys kept 6 h.
- We send: `prefer="return=representation"` (default minimal omits purchase_units.payments),
  `pay_pal_request_id=f"{payment.reference}-auth-{attempt}"` — `reference` is a random per-payment id, because
  order numbers repeat when the sandbox DB is rebuilt while PayPal's idempotency keys (and duplicate-invoice
  checks) outlive it.
- `OrderRequest`: `intent` (req, `CheckoutPaymentIntent.AUTHORIZE`), `purchase_units: list[PurchaseUnitRequest]` (req),
  `payment_source: Optional[PaymentSource]`.
- `PurchaseUnitRequest`: `amount: AmountWithBreakdown` (req: `currency_code: str`, `value: str`);
  `reference_id` and `custom_id` (purpose: our order number), `invoice_id` (`{reference}-{attempt}` —
  unique per attempt, echoed by PayPal's reporting). `description`, `items`, `shipping`, `payee`, `soft_descriptor`
  omitted → provider/account default.
- `PaymentSource.card: CardRequest` — one-off: `name`, `number`, `expiry` (`YYYY-MM`), `security_code`,
  `billing_address: Address` (`country_code` req; `address_line_1`, `address_line_2`, `admin_area_2`,
  `admin_area_1`, `postal_code`). Saved: `vault_id` only. `attributes`/`stored_credential`/
  `experience_context` omitted (provider default; smoke test: vault_id alone authorizes).
- Response `Order`: `id`, `status: OrderStatusOrStr`, `purchase_units[0].payments.authorizations[0]`
  (`AuthorizationWithAdditionalData`: `id`, `status`, `amount: Money`, `expiration_time`, `create_time`,
  `status_details.reason`), `payment_source.card` (`CardResponse`: `last_digits`, `brand`, `expiry`).
  Assert `id` and an authorization id are set, else outcome unknown.
- `OrderStatus`: CREATED, SAVED, APPROVED → pending(unexpected for single-step: `needs_review`);
  COMPLETED → look at authorization; VOIDED → failed; PAYER_ACTION_REQUIRED → **failed + report
  (3-D Secure challenge; task says STOP — we do not build an approval round-trip)**; other → unknown.
- `AuthorizationStatus`: CREATED → `authorized` (done); PENDING → `authorization_pending`; DENIED → failed
  (declined); VOIDED → failed; CAPTURED / PARTIALLY_CAPTURED → unexpected here → needs_review; other → unknown.
- Echo check: `Decimal(auth.amount.value) == order total` and currency == configured, else `needs_review`.
- Smoke (2026-09-23): card and `vault_id` both return `status=COMPLETED` with authorization `CREATED`;
  authorization `expiration_time` = create + 29 days.

### payments.get_authorized_payment
`get_authorized_payment(authorization_id: str, *, ...)` → `PaymentAuthorization` (`status`, `expiration_time`,
`create_time`, `amount`, `id`). Arms `Error` [401, 403, 404] · `RawError` [500, rest].

### payments.reauthorize_payment
`reauthorize_payment(authorization_id: str, *, pay_pal_request_id=None, prefer="return=minimal", body: ReauthorizeRequest|Dict|None=None)` → `PaymentAuthorization` (new auth id).
Arms `Error` [400, 401, 403, 404, 422] · `RawError` [500, rest]. Docstring: allowed after the 3-day honor
period, day 4–29, new 3-day honor period; `ReauthorizeRequest.amount: Optional[Money]` — we send the order total (the original amount).
Smoke: day 0 → 422 `details[0].issue == "REAUTHORIZATION_TOO_SOON"` ("only allowed once from Day 4 to Day 29").
We send `pay_pal_request_id=f"{reference}-reauth-{auth_id}"`, `prefer="return=representation"`.

### payments.capture_authorized_payment
`capture_authorized_payment(authorization_id: str, *, pay_pal_request_id=None, prefer="return=minimal", body: CaptureRequest|Dict|None=None)` → `CapturedPayment`.
Arms `Error` [400, 401, 403, 404, 409, 422] · `RawError` [500, rest]. Request-id kept 45 days (smoke:
same id → same capture returned).
- Body: `amount: Money` (order total), `final_capture: True`. `invoice_id` (already on the order), `note_to_payer`,
  `soft_descriptor`, `payment_instruction` omitted → account default.
- Response: `id`, `status: CaptureStatusOrStr`, `amount`, `create_time`, `seller_receivable_breakdown`
  (`gross_amount: Money` required, `paypal_fee`, `net_amount`: Optional). Assert `id`.
- `CaptureStatus`: COMPLETED → `captured`; PENDING → `capture_pending`; DECLINED, FAILED → `capture_failed`;
  PARTIALLY_REFUNDED, REFUNDED → treat as captured (refund state tracked separately); other → unknown.
- Stale-auth errors (422 issue): `AUTHORIZATION_EXPIRED` → reauthorize then capture; `AUTHORIZATION_VOIDED` →
  terminal. Issue strings other than those observed in smoke are UNVERIFIED — so the ladder decides on the
  authorization's own state (`get_authorized_payment`) rather than on issue strings alone.

### payments.void_payment
`void_payment(authorization_id: str, *, pay_pal_request_id=None, prefer="return=minimal")` → `PaymentAuthorization`.
Arms `Error` [401, 403, 404, 409, 422] · `RawError` [500, rest]. Smoke: repeat under same request id → same
VOIDED body. `AuthorizationStatus.VOIDED` → `voided`; anything else → unknown.

### payments.refund_captured_payment
`refund_captured_payment(capture_id: str, *, pay_pal_request_id=None, prefer="return=minimal", body: RefundRequest|Dict|None=None)` → `Refund`.
Arms `Error` [400, 401, 403, 404, 409, 422] · `RawError` [500, rest]. Docstring: empty body = full refund;
`amount` for partial. We always send `amount` (explicit, verifiable). `invoice_id`, `note_to_payer`,
`custom_id` (refund row id — purpose: reference), `payment_instruction` omitted except custom_id.
`pay_pal_request_id=f"{reference}-refund-{sha256(idempotency_key)[:24]}-{attempt}"` — smoke: same id → same refund.
- Response `Refund`: `id`, `status: RefundStatusOrStr`, `amount`, `create_time`,
  `seller_payable_breakdown.total_refunded_amount`.
- `RefundStatus`: COMPLETED → completed; PENDING → pending; CANCELLED, FAILED → failed (reservation
  released); other → unknown. Smoke: over-refund → 422 issue `REFUND_AMOUNT_EXCEEDED`.

### vault.create_payment_token
`create_payment_token(body: PaymentTokenRequest | PaymentTokenRequestDict, *, pay_pal_request_id=None)` → `PaymentTokenResponse`.
Arms `Error` [400, 403, 404, 422, 500] · `RawError` [rest]. Request id kept 3 h.
- `PaymentTokenRequest`: `payment_source: PaymentTokenRequestPaymentSource` (req; `.card:
  PaymentTokenRequestCard` — `name`, `number`, `expiry`, `security_code`, `billing_address`; `brand` omitted →
  PayPal detects), `customer: Optional[Customer]` (`id` — PayPal-generated customer id, sent once we hold
  one for the user; `merchant_customer_id` — purpose: our user reference `oscar-user-{pk}`, sent on first save).
- Response: `id` (token id), `customer.id`, `payment_source.card` (`CardPaymentTokenEntity`: `last_digits`,
  `brand`, `expiry`, `name`, `verification_status`). Assert `id` and `customer.id`.
- Smoke: returns token + `customer.id`; no status field on the response (vaulted synchronously).

### vault.delete_payment_token
`delete_payment_token(id: str)` → **`None`**; raw peer `ApiResult[None, DeletePaymentTokenErrorBody]` — we use
`with_raw_response` to see the status. Arms `Error` [400, 403, 500] · `RawError` [rest]. Smoke: 204, and 204
again on repeat (idempotent). 404 → treat as already gone.

### vault.list_customer_payment_tokens — not used
Ownership and listing come from our `SavedCard` rows (PayPal's customer id is per user, but the local row is
what binds a token to a shopper and survives deletes-in-progress).

### transaction_search.search_transactions
`search_transactions(start_date: str, end_date: str, *, transaction_id=None, ..., fields="transaction_info", balance_affecting_records_only="Y", page_size=100, page=1)` → `SearchResponse`.
**Case B**: `.error` always `RawError`. Docstring: RFC 3339 with seconds; **max range 31 days**; up to 3 h
reporting lag. We send `balance_affecting_records_only="N"` (authorizations are not balance-affecting and
must be matched too), `page_size=500` (smoke: 500 accepted, 501 → 400 `must be less than or equal to 500`),
read page 1 of every ≤31-day chunk, then pages 2..`total_pages` concurrently (4 workers, sync client is
thread-safe), capped at 100 pages per chunk with a `truncated` flag in the result.
Smoke: this shared sandbox account holds ~6.2k transactions per 31 days (13 pages of 500); a 45-day report
takes ~40 s.
- `SearchResponse`: `transaction_details: Optional[list[TransactionDetails]]`, `total_pages`, `page`,
  `last_refreshed_datetime`. `TransactionDetails.transaction_info: TransactionInformation` —
  `transaction_id`, `paypal_reference_id`, `transaction_event_code`, `transaction_initiation_date`,
  `transaction_amount: Money`, `fee_amount`, `transaction_status: str`, `invoice_id`.
- Smoke: 30-day range works with `%Y-%m-%dT%H:%M:%SZ`; auth ids appear with event T1300, captures T0005/T00xx.

## CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| `vault_id` sent to create_order must be a token `create_payment_token` returned **for this shopper** and not deleted | `orders.create_order` ← `vault.create_payment_token` | `SavedCard` lookup by (public id, user, status=active) |
| `customer.id` sent to create_payment_token must be the one PayPal returned for this user | `vault.create_payment_token` ← itself | `PayPalCustomer` 1:1 user |
| auth id passed to capture/void/reauthorize/get must be the one create_order (or reauthorize) returned for this order | payments.* ← orders.create_order / reauthorize | `PayPalPayment.authorization_id` |
| capture id passed to refund must be the one capture returned for this order | refund ← capture | `PayPalPayment.capture_id` |
| sum of non-failed refunds ≤ captured amount | refund ← capture | atomic conditional UPDATE on `PayPalPayment.refund_reserved` |
| a token passed to delete must belong to the caller | delete ← create_payment_token | `SavedCard` lookup by user |

## Idempotency & state design (python-configuration-resilience)

- `PayPalPayment` (1:1 order) is the claim for pay/fulfil/cancel. Transitions are single conditional
  UPDATEs (`filter(state__in=...).update(state=<in-flight>)`); the loser answers from the row (202 while
  in flight, current state otherwise). In-flight states: `authorizing`, `capturing`, `voiding`.
- Pay: `attempt` increments only after a definitive failure; request id `"{reference}-auth-{attempt}"`, so a
  retry after an unknown outcome re-sends under the **same** id and PayPal returns the original order
  (the reconciliation lookup — there is no search-by-reference for orders). Unknown → state `unknown`, 504.
  Never-sent transport errors (ConnectError/ConnectTimeout/PoolTimeout/ProxyError) or 4xx → back to
  `awaiting_payment`/`authorization_failed`.
- Fulfil: authorization state read first; honor period (3 days from auth `create_time`) elapsed → reauthorize;
  expired (`expiration_time` passed) or reauth refused → 409 with operator-actionable message, payment
  state `authorization_expired` (shopper may pay again). Capture request id `"{reference}-capture-{auth_id}"`; reauth `"{reference}-reauth-{auth_id}"`; void `"{reference}-void-{auth_id}"`.
- Refund: `PayPalRefund` row UNIQUE(payment, idempotency_key); same key + same amount → the existing refund;
  same key + different amount → 409. Reservation via conditional UPDATE before the call; released on failed.
- Order status (Oscar pipeline) changed via `Order.set_status` only by the request that won the claim.
- Amount echo checks on authorization, capture and refund → `needs_review` on mismatch.

## Error boundary (python-error-handling)

`gateway.call()` converts every SDK failure into `ProviderError(status, message, outcome_unknown, issue)`:
OAuthProviderError / 401 / 403 → 502 (our config); 429 → 503; `Error` with 400/404/409/422 → caller's
(422/409 with PayPal's issue + description, never `str(e)`); 5xx/unmapped → 502; `ValidationError` on
decode → 502 outcome_unknown; never-sent transport → 502 outcome known; other `httpx.RequestError` → 504
outcome_unknown. Card data never logged; request bodies never logged.

## Assumptions & blockers

- None blocking. Minor: live base URL string is our call; catalogue prices are GBP numbers charged in the
  configured currency (task mandate). Setup note: the sandbox's CSV catalogue import is needed to reach
  209 products (fixtures alone give 11) and `orders.json` loads only after it.

## Tests / checks

- `sandbox/apps/payments/tests.py`, Django `TestCase` (sandbox's `DiscoverRunner`), stub transport via
  `custom_http_client` + stub token source; cover: pay echo mismatch, declined, unknown vs never-sent,
  refund over-cap, idempotent refund key, ownership 404s, delete, reconciliation paging.
- `mypy --strict` on `sandbox/apps/payments` (django-stubs).

## REQUIRED READING

- MUST load `python-error-handling` — error boundary, OAuthProviderError first, transport split. (loaded)
- MUST load `python-client-initialization` — singleton, close, sync client. (loaded)
- MUST load `python-configuration-resilience` — claim rows, same-ref retry, echo checks, reconciliation clocks, paging bounds. (loaded)
- MUST load `python-authentication` — settings with empty defaults, check in `get_client()`. (loaded)
- MUST load `python-calling-endpoints` — prefer=representation, raw peer for delete, status mapping. (loaded)
- MUST load `python-models` — UNSET vs None, open enums, Decimal money with currency exponent. (loaded)
- MUST load `python-testing` — stub transport + token source. (loaded)
