# PayPal integration plan — django-oscar sandbox (`paypal` Python SDK, v2.29)

## Goal
Add PayPal card payments + saved cards to the sandbox as a new Django app `sandbox/apps/paypal_api/`,
routed under `/api/`. Reuse Oscar's `order.Order`/`order.Line` and `payment` models; PayPal owns the
money movement (authorize hold → capture at fulfil → void on cancel → refund on return) and card vaulting.

## SDK identity (installed & verified)
- Distribution `paypal` **2.29**; import root `paypal`. Client `PaypalClient` (sync) / `AsyncPaypalClient`.
- **Sync client** (Django WSGI). Built lazily as a module-global singleton, long-lived, pooled transport.
- Auth: `oauth2=ClientCredentials(client_id, client_secret)`. Token fetched lazily; a failed fetch raises
  `ApiError` with `OAuthProviderError` payload out of the *operation* call (check first in the ladder).
- Base URL: single server, default `https://api-m.sandbox.paypal.com`. `PAYPAL_BASE_URL` (if set) used
  verbatim; else map `PAYPAL_ENVIRONMENT`: `live`/`production` → `https://api-m.paypal.com`, else `None`
  (SDK sandbox default). Omitting base_url is silently sandbox — acceptable here (env=sandbox).
- **No retries in the SDK**; **no pagination** — both are ours. `timeout` default 30s (we set ~30s).

## Credentials (from Django settings, values never in repo)
`settings.py` reads (with empty-string defaults so import never raises):
`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENVIRONMENT`, `PAYPAL_CURRENCY`, `PAYPAL_BASE_URL`.
`build_client()` validates non-empty id/secret and raises `RuntimeError` naming missing vars.

## Empirically confirmed against the sandbox (scratchpad smoke — see below)
- **Pay (direct card):** `orders.create_order(OrderRequest(intent="AUTHORIZE", purchase_units=[amount],
  payment_source.card={number,expiry,security_code,name,billing_address}))` → **201**, order.status
  `COMPLETED`, and an **authorization is present inline** at
  `order.purchase_units[0].payments.authorizations[0]` with status `CREATED`. **No separate
  `authorize_order` call is needed** (fallback: if authorizations absent, call `authorize_order(id)`).
- **Pay with saved card:** same call with `payment_source.card.vault_id=<token>` → 201, authorization created.
- **Fulfil (capture):** `payments.capture_authorized_payment(auth_id)` → 201, status `COMPLETED`,
  `seller_receivable_breakdown.{gross_amount,paypal_fee,net_amount}` populated (24.99 / 1.14 / 23.85).
- **Cancel (void):** `payments.void_payment(auth_id)` on success returns **204 empty body → SDK raises
  `ValueError`** (decode failure, bypasses raw mode). Handle: treat `ValueError` from void as success,
  confirm via `get_authorized_payment` → status `VOIDED`. A re-void returns a proper `Failure 422`.
  Idempotent cancel = read status first; only void when `CREATED`/`PENDING`; `VOIDED` already = success.
- **Refund:** `payments.refund_captured_payment(capture_id, body=RefundRequest(amount=Money))` → 201
  `COMPLETED`. Partial refunds allowed; guard total refunded ≤ captured.
- **Reconcile:** `transaction_search.search_transactions(start,end,...)` → 200, `total_pages` real
  (3 pages in test) → **must loop all pages**. `transaction_info.transaction_status` is a raw `str`
  code (S=success, P=pending, D=denied, V=reversed); event codes T0006/T0005 payments, T1107 refund.
- **Vault:** `vault.create_payment_token(PaymentTokenRequest(customer, payment_source.card={raw card}))`
  → 201, returns `id` (vault_id), `customer.id`, and safe display
  `payment_source.card.{brand,last_digits,expiry,name}`. `delete_payment_token(id)` → 204 (handled,
  return type is `None`). `list_customer_payment_tokens` lags/empty in sandbox → we list from OUR DB.

## Architecture / files
`sandbox/apps/paypal_api/`
- `apps.py` — AppConfig (`label = "paypal_api"`).
- `client.py` — `build_client()` singleton, `settings`-driven; `gateway.py` — thin service wrapping every
  SDK call with the error boundary (maps to our exceptions) and the void empty-body handling.
- `models.py`:
  - `PayPalCustomer(user OneToOne, paypal_customer_id)` — one PayPal customer per shopper (for vaulting).
  - `PaymentMethod(user FK, paypal_vault_id, paypal_customer_id, brand, last_digits, expiry_yyyy_mm,
    cardholder_name, created)` — saved card; safe display only, never PAN/CVV. Delete = remove row +
    `delete_payment_token`.
  - `OrderPayment(order OneToOne, currency, amount, status, paypal_order_id, authorization_id,
    auth_status, auth_expiry, capture_id, capture_status, captured_value, paypal_fee, net_value,
    paypal_event_time, created, updated)` — the durable idempotency row (the claim). `status` ∈
    {AWAITING_PAYMENT, AUTHORIZED, CAPTURED, PARTIALLY_REFUNDED, REFUNDED, CANCELLED, FAILED}.
  - `PaymentRefund(payment FK, idempotency_key, paypal_refund_id, amount, status, created)` with
    `UNIQUE(payment, idempotency_key)` — the refund claim; repeat key → return existing.
- `serializers`/plain views (DRF is not an Oscar dep; use plain Django JsonResponse views + a small
  auth/parse helper). Session auth via Django; `request.user`. Operator endpoints require `is_staff`.
- `services.py` — order placement (build basket from catalogue ids → OrderCreator.place_order),
  pay/fulfil/cancel/refund/reconcile/vault orchestration.
- `urls.py` — routes below; wired into `sandbox/urls.py` under `path("api/", include(...))` OUTSIDE
  Oscar's i18n_patterns.
- `migrations/`.

### Money / currency
Order currency set to `PAYPAL_CURRENCY` at creation. PayPal `value = quantize(order.total_incl_tax, 2)`
as string; `currency_code = PAYPAL_CURRENCY`. Authorized/captured amount asserted == order total to the
cent (compare `Decimal`). `custom_id = order.number` on the purchase unit for reconciliation matching;
`invoice_id = f"{order.number}"` too (unique per order).

### Idempotency (durable row + status guard; SDK has no retries so no accidental dup)
- `/pay`: `select_for_update` the `OrderPayment`; if `status in {AUTHORIZED,CAPTURED,...}` → return existing
  (no PayPal call). Else create_order with `pay_pal_request_id=f"auth-{order.number}"` (PayPal dedups a
  resend). Store ids, set AUTHORIZED, verify echoed amount == order total.
- `/fulfil`: guard `status==CAPTURED` → return existing. Else ensure auth fresh (reauthorize if stale),
  capture with `pay_pal_request_id=f"cap-{order.number}"`, store fee/net, set CAPTURED.
- `/cancel`: guard — only when `AUTHORIZED` (pre-capture). Void (idempotent via status read). CANCELLED.
- `/refunds`: caller `idempotency_key`; `get_or_create` the `PaymentRefund` claim (unique) — if it existed
  with a paypal id, return it; else call refund with `pay_pal_request_id=<idempotency_key>`, store. Reject
  if requested amount > (captured − already_refunded).

### Ownership / auth
- Shopper endpoints scope every query to `request.user`. Saved card & order lookups filter by owner →
  another shopper's id yields 404. Operator endpoints (`fulfil`, `cancel`, `reconciliation`) require
  `request.user.is_staff` (403 otherwise). All endpoints require login (401 otherwise). CSRF: these are
  programmatic JSON endpoints authenticated by session — mark views `csrf_exempt` (documented) OR require
  `X-CSRFToken`. Decision: `csrf_exempt` on the API views (they are session-auth JSON APIs driven by a
  client, mirroring typical API usage); ownership + is_staff checks enforce authorization.

## Routes (all under /api/)
| Method & path | Who | Action |
|---|---|---|
| POST `/api/orders` | shopper | place Oscar order from `{items:[{id/upc,quantity}]}`; status awaiting-payment; returns `orderId` |
| POST `/api/orders/{orderId}/pay` | shopper(owner) | authorize: body `{card:{...}}` OR `{paymentMethodId}` |
| POST `/api/orders/{orderId}/fulfil` | staff | capture (reauth if stale) |
| POST `/api/orders/{orderId}/cancel` | staff | void (pre-capture only) |
| POST `/api/orders/{orderId}/refunds` | shopper(owner) | refund; body `{amount?, idempotencyKey}`; returns `refundId` |
| GET `/api/my-orders` | shopper | caller's orders + payment state |
| GET `/api/reconciliation?from&to` | staff | PayPal txns (all pages) vs app orders |
| POST `/api/payment-methods` | shopper | vault card; returns `paymentMethodId` + safe display |
| GET `/api/payment-methods` | shopper | caller's saved cards |
| DELETE `/api/payment-methods/{id}` | shopper(owner) | remove saved card (DB + PayPal) |

---

## CONTRACT SHEET (grounded in SDK map + installed source; no open lookups)

**Client:** `PaypalClient(*, base_url=None, timeout=30.0, oauth2=ClientCredentials(...))`. Keyword-only.
Groups: `client.orders`, `client.payments`, `client.vault`, `client.transaction_search`. Every op also on
`.with_raw_response.<op>` → `Success(payload,response)` / `Failure(error,response)`; parsed form raises
`ApiError`. `request_options` is the trailing keyword on all.

### Operations used (signatures; all params after `*` keyword-only, all have real defaults)
1. `orders.create_order(body: OrderRequest, *, pay_pal_request_id=None, prefer="return=minimal", ...)`
   → parsed `Order`. Use `prefer="return=representation"` to get authorizations inline. Error union
   `Error | RawError` (400,401,422 typed).
2. `orders.authorize_order(id, *, pay_pal_request_id=None, prefer=..., body=None)` → `OrderAuthorizeResponse`.
   (Fallback only.) Error `Error|RawError` (400,401,403,404,422,500).
3. `orders.get_order(id, *, fields=None, ...)` → `Order`. Error `Error|RawError` (401,404).
4. `payments.capture_authorized_payment(authorization_id, *, pay_pal_request_id=None, prefer="return=minimal", ...)`
   → `CapturedPayment`. Use `prefer="return=representation"` for `seller_receivable_breakdown`. Error
   `Error|RawError` (400,401,403,404,409,422; 500→RawError).
5. `payments.get_authorized_payment(authorization_id, ...)` → `PaymentAuthorization`. Error (401,403,404).
6. `payments.reauthorize_payment(authorization_id, *, pay_pal_request_id=None, body=ReauthorizeRequest|None)`
   → `PaymentAuthorization`. Error (400,401,403,404,422).
7. `payments.void_payment(authorization_id, *, prefer="return=minimal", ...)` → parsed `PaymentAuthorization`
   **but success is 204 empty → raises `ValueError`**; use `.with_raw_response` and catch `ValueError` as
   success. Error (401,403,404,409,422).
8. `payments.refund_captured_payment(capture_id, *, pay_pal_request_id=None, body=RefundRequest|None, ...)`
   → `Refund`. Error (400,401,403,404,409,422; 500→RawError).
9. `vault.create_payment_token(body: PaymentTokenRequest, *, pay_pal_request_id=None)` → `PaymentTokenResponse`.
   Error (400,403,404,422,500).
10. `vault.delete_payment_token(id, *, ...)` → **`None`** (204 handled; raw peer `ApiResult[None,...]`).
    Error (400,403,500).
11. `transaction_search.search_transactions(start_date, end_date, *, transaction_currency=None, fields="transaction_info",
    balance_affecting_records_only="Y", page_size=100, page=1, ...)` → `SearchResponse` (**Case B: error is
    `RawError` only**). Loop `page` 1..`total_pages` (cap MAX_PAGES). Use `fields="all"` to get amounts.

### Request models (required fields only; everything else `Optional`=UNSET; `Optional`≠typing.Optional, no None)
- `OrderRequest`: `intent` (req, `CheckoutPaymentIntent.AUTHORIZE`), `purchase_units: list[PurchaseUnitRequest]`
  (req); optional `payment_source: PaymentSource`.
- `PurchaseUnitRequest`: `amount: AmountWithBreakdown` (req); set `custom_id`, `invoice_id` for reconcile.
- `AmountWithBreakdown`: `currency_code` (req), `value` (req).
- `PaymentSource`: all optional; set `card: CardRequest`.
- `CardRequest` (all optional): one-off = `number,expiry(YYYY-MM),security_code,name,billing_address:Address`;
  saved = `vault_id`.
- `Address`: `country_code` (req) only.
- `RefundRequest` (all optional): set `amount: Money`.
- `Money`: `currency_code` (req), `value` (req).
- `PaymentTokenRequest`: `payment_source: PaymentTokenRequestPaymentSource` (req); optional `customer: Customer`.
- `PaymentTokenRequestPaymentSource`: optional `card: PaymentTokenRequestCard`.
- `PaymentTokenRequestCard` (all optional): `number,expiry,security_code,name,billing_address,brand`.
- `Customer` (all optional): `id` (reuse per-user PayPal customer), `merchant_customer_id`.

### Response reads (attribute paths; every hop Optional/UNSET → guard each)
- create/authorize: `order.status` (OrderStatus); auth = `order.purchase_units[i].payments.authorizations[j]`
  (`AuthorizationWithAdditionalData`): `.id`, `.status` (AuthorizationStatus), `.amount.value/.currency_code`,
  `.expiration_time`.
- capture: `CapturedPayment.id`, `.status` (CaptureStatus), `.amount.value`,
  `.seller_receivable_breakdown.{gross_amount,paypal_fee,net_amount}.value`, `.update_time`.
- refund: `Refund.id`, `.status` (RefundStatus), `.amount.value`.
- auth get: `PaymentAuthorization.status`, `.expiration_time`.
- vault: `PaymentTokenResponse.id`, `.customer.id`, `.payment_source.card.{brand,last_digits,expiry,name}`
  (card model `CardPaymentTokenEntity`; only wire alias in scope is its `type_`→"type", unused).
- search: `SearchResponse.total_pages`, `.page`, `.total_items`, `.transaction_details[k].transaction_info`
  (`TransactionInformation`): `.transaction_id`, `.transaction_status` (**raw str** S/P/D/V),
  `.transaction_amount.{value,currency_code}`, `.transaction_initiation_date`, `.custom_field` (=our custom_id),
  `.transaction_event_code`.

### Status enum → outcome mapping (enumerate by name; default arm = unknown, never "failed")
- `AuthorizationStatus`: CREATED/PENDING → held(ok to capture); CAPTURED/PARTIALLY_CAPTURED → captured;
  VOIDED → cancelled; DENIED → failed; `_` → unknown.
- `CaptureStatus`: COMPLETED → captured/done; PENDING → pending; PARTIALLY_REFUNDED/REFUNDED → refunded;
  DECLINED/FAILED → failed; `_` → unknown.
- `RefundStatus`: COMPLETED → done; PENDING → pending; CANCELLED/FAILED → failed; `_` → unknown.
- `OrderStatus`: COMPLETED/APPROVED → processed; CREATED/SAVED → not-yet; PAYER_ACTION_REQUIRED → **STOP &
  report challenge** (task mandate); VOIDED → cancelled; `_` → unknown.
- reconcile `transaction_status` (str): S → success; P → pending; D → denied; V → reversed; else unknown.

### Error boundary (one place → our exceptions; order: auth → 401/403 → 429 → typed 4xx → 5xx/unmapped)
`ApiError`→ if `OAuthProviderError` → ConfigError(502); `status in (401,403)` → Upstream(502);
`429`→Upstream(503); `isinstance(error, Error) and status in (400,404,409,422)` → ClientFault(status,message);
else Upstream(502). `ValidationError`→Unreadable (outcome unknown; on writes re-read). httpx
`ConnectError/ConnectTimeout/PoolTimeout/ProxyError`→Unavailable(never sent); other `httpx.RequestError`
→Unavailable(outcome unknown). `void_payment` `ValueError` on 2xx → treat as success (special-cased at call).

### CROSS-OPERATION INVARIANTS
| invariant | operations | enforced where |
|---|---|---|
| `card.vault_id` used in create_order must be an id returned by `create_payment_token` and owned by caller | create_order ← create_payment_token | `PaymentMethod` lookup filtered by `user`; 404 if not owner |
| capture/void/reauth `authorization_id` must be the one stored from this order's create_order | payments.* ← orders.create_order | `OrderPayment.authorization_id` |
| refund `capture_id` must be the one stored at fulfil | refund ← capture | `OrderPayment.capture_id` |
| reconcile match key `custom_field` == our `custom_id` == `order.number` | search_transactions ← create_order(custom_id) | reconciliation matcher |
| per-user PayPal `customer.id` reused across vault calls | create_payment_token(customer.id) ← prior create_payment_token | `PayPalCustomer` |

## REQUIRED READING (companions — all loaded)
- `python-error-handling` — MUST load (error boundary). ✅ loaded
- `python-client-initialization` — MUST load (client singleton/lifetime). ✅ loaded
- `python-configuration-resilience` — MUST load (idempotency durable-row, reconciliation clock/paging, no retries). ✅ loaded
- `python-authentication` — MUST load (oauth2, secret loading via settings, token-fetch failure). ✅ loaded
- `python-calling-endpoints` — MUST load (raw vs parsed, status-not-id, None returns). ✅ loaded
- `python-models` — MUST load before building payloads (UNSET/open enums/serialize). ⬜ load next
- `python-testing` — MUST load before test files / verification scripts faking transport. ⬜ load before tests

## Assumptions & blockers (minor — proceed)
- Oscar order needs a shipping address (Country row) + shipping method → use `Free` shipping + a default
  shipping address built from the shopper's saved/oscar address or a minimal one; billing not required.
- Anonymous PayPal card challenge (3DS `PAYER_ACTION_REQUIRED`) → STOP & report per mandate (not expected
  for the test Visa; handled by mapping OrderStatus).
- Reconciliation empty over a just-created range is expected sandbox lag — not a gap.
