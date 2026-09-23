# PayPal payments + saved cards for the django-oscar sandbox — plan & contract sheet

## Goal

Add an **additive** HTTP API to the runnable sandbox (`sandbox/`) that collects money through
**PayPal** (direct card + vaulted cards), on top of Oscar's own order/order-line models. Two flows:

- **Flow 1 — Pay for an order:** create order (awaiting payment) → authorize (hold) → fulfil
  (capture) → cancel (void, pre-fulfilment) / refund (post-fulfilment, full or partial) →
  my-orders → reconciliation.
- **Flow 2 — Saved cards (vault):** save card → list → delete; reuse a saved card to pay.

All PayPal interaction goes through the **paypal** plugin's Python SDK (`paypal`, class
`PaypalClient`). The SDK is the sole reference; no web search, no external knowledge.

## SDK identity (drift note — VERIFIED against installed package)

The `python-getting-started` snapshot names root `pay_pal_server_sdk` / `PayPalServerSdkClient`. The
**actually installed** SDK (v2.29, built from the plugin's own clone) uses root **`paypal`**, client
**`PaypalClient`** / **`AsyncPaypalClient`**, aliases `Client`/`AsyncClient`. Imports:
`from paypal import PaypalClient`; `from paypal.core import ClientCredentials, ApiError, RawError,
Success, Failure, UNSET`; `from paypal.models import ...`; `from paypal.models.enums import ...`;
`from paypal.errors import ...`. Per the skill's own rule ("trust the interpreter over the page"), I
follow the installed package. Confirmed: `import paypal; from paypal import PaypalClient` works.

## Host decisions

- **Sync client** (`PaypalClient`). Host is Django under WSGI → sync (per python-client-initialization).
  Teardown obligation: `client.close()` / context manager. Client is **long-lived**, built lazily via
  a module-level factory (`gateway.get_client()`), never per request, never at import.
- **base_url**: resolved explicitly on every construction. If `PAYPAL_BASE_URL` is set, use it verbatim
  (token traffic rides the same base_url — good, satisfies the "verbatim for every call incl. token"
  rule). Else derive from `PAYPAL_ENVIRONMENT`: `sandbox`→`https://api-m.sandbox.paypal.com`,
  `live`/`production`→`https://api-m.paypal.com`. Unknown value → raise (no silent default).
- **Auth**: `oauth2=ClientCredentials(client_id, client_secret)`. Token fetched lazily + cached on the
  long-lived client. Credentials read from Django settings (which read env at import with **empty
  defaults** so import never raises); `gateway.get_client()` raises `RuntimeError` naming any missing
  var. Secrets never written to the repo.
- **Response mode**: use **`with_raw_response`** for every write/lookup where I must inspect status or
  branch on failure without exceptions (authorize, capture, void, refund, reauthorize, vault
  create/delete, search). Parsed mode acceptable for simple reads; but the durable-write guard wants
  the raw peer. Decode/token failures still raise in both modes — handled in the error boundary.

## Architecture

New Django app **`apps.paypal_checkout`** under `sandbox/apps/paypal_checkout/`, added to
`INSTALLED_APPS`, urls wired into `sandbox/urls.py` under `/api/`. Reuses Oscar `order.Order` /
`order.Line` via `OrderCreator` (mirrors `oscar.test.factories.create_order`). No parallel order model.

Files:
- `apps.py` — `PaypalCheckoutConfig` (label `paypal_checkout`).
- `settings` additions (in `sandbox/settings.py`): `PAYPAL_CLIENT_ID/SECRET/ENVIRONMENT/CURRENCY/BASE_URL`.
- `money.py` — Decimal amount formatting by currency exponent (USD=2; JPY/KRW=0; KWD/BHD/TND=3).
- `exceptions.py` — `ProviderError` hierarchy: `ProviderConfigError`(502), `ProviderRejected`(caller
  fault, carries status+message), `ProviderUnavailable`(status, outcome_unknown), `ProviderUnreadable`,
  `ApiClientError`(4xx for our-API caller mistakes), `ChallengeRequired` (3DS → STOP/report).
- `gateway.py` — `get_client()` factory; `PayPalGateway` wrapping each SDK op with the error boundary
  (auth→OAuthProviderError? SDK has no OAuthProviderError export — see note; transport split; typed vs
  raw). Translates SDK failures into `ProviderError`. Also carries "verify on the wire" logging
  transport behind `PAYPAL_DEBUG_WIRE` flag.
- `models.py` — `PayPalPayment`, `PayPalRefund`, `SavedCard` (below).
- `services.py` — order creation (basket→OrderCreator), authorize, capture(+reauth), void, refund,
  reconciliation, vault save/list/delete. Concurrency via `select_for_update` + conditional updates.
- `serializers.py` — dict builders for responses (never leak `UNSET`, never full card data).
- `views.py` + `urls.py` — session-auth JSON endpoints; staff gate on operator actions.
- `migrations/` — one migration.
- `tests.py` — unit tests via the SDK **stub transport** seam (python-testing).

### Local models (additive; provider stays record of what exists, rows record that we asked)

**`PayPalPayment`** (OneToOne `order.Order`, related_name `paypal_payment`):
`order`, `paypal_order_id`, `authorization_id`, `authorization_status`, `authorization_expiry`,
`capture_id`, `capture_status`, `captured_value`, `paypal_fee`, `net_amount`, `currency`,
`amount` (authorized order total), `status` (lifecycle enum below), `provider_time` (capture
create_time — the provider clock for reconciliation), `created`, `updated`.
Lifecycle enum: `AWAITING_PAYMENT`, `AUTHORIZED`, `CAPTURED`, `PARTIALLY_REFUNDED`, `REFUNDED`,
`VOIDED`, `FAILED`, `NEEDS_REVIEW`.

**`PayPalRefund`** (FK `PayPalPayment`, related_name `refunds`):
`payment`, `refund_id`, `amount`, `currency`, `status`, `idempotency_key`, `created`.
`unique_together = (payment, idempotency_key)` — the durable claim for refund idempotency.

**`SavedCard`** (FK `AUTH_USER_MODEL`, related_name `saved_cards`):
`user`, `paypal_token_id`, `paypal_customer_id`, `brand`, `last_digits`, `expiry`, `created`.
Never stores PAN/CVV. PayPal customer id = stable `oscar-cust-{user.pk}` supplied on vault create so
tokens are listable/scoped by customer.

### Endpoints (all under `/api/`, session auth, JSON)

| Method + path | Who | Action |
|---|---|---|
| `POST /api/orders` | shopper | build basket from `[{id,quantity}]`, place Oscar order (status Pending), create `PayPalPayment(AWAITING_PAYMENT)` + PayPal v2 order (intent=AUTHORIZE, invoice_id=order.number, amount=total@PAYPAL_CURRENCY). Returns **`orderId`** (Oscar order number). |
| `POST /api/orders/{orderId}/pay` | shopper (own) | authorize: card `{card:{number,expiry,security_code,name}}` **or** `{paymentMethodId}` (saved card → vault_id). Hold = order total to the cent. Idempotent. |
| `POST /api/orders/{orderId}/fulfil` | staff | capture; renew stale auth via reauthorize; record captured/fee/net. Idempotent. |
| `POST /api/orders/{orderId}/cancel` | staff | void authorization (pre-fulfilment). Idempotent no-op if already voided. |
| `POST /api/orders/{orderId}/refunds` | shopper (own) | refund capture, full/partial; caller `idempotencyKey`; never exceed captured. Returns **`refundId`**. |
| `GET /api/my-orders` | shopper | caller's orders + payment state. |
| `GET /api/reconciliation?from=&to=` | staff | search_transactions over whole range (paged), line up vs local by invoice_id. |
| `POST /api/payment-methods` | shopper | vault a card; returns **`paymentMethodId`** + safe description. |
| `GET /api/payment-methods` | shopper | caller's saved cards. |
| `DELETE /api/payment-methods/{id}` | shopper (own) | delete vault token + local row. |

Ownership: every order/saved-card query filtered by `request.user`; operator actions gated on
`is_staff`. Full card details never stored/logged.

## PayPal call sequence (grounded in SDK map + source)

1. **Create order** — `client.orders.create_order(OrderRequest(intent=AUTHORIZE,
   purchase_units=[PurchaseUnitRequest(amount=AmountWithBreakdown(currency_code, value),
   invoice_id=order.number, custom_id=order.number)]), pay_pal_request_id=f"{number}-create")`.
   Returns `Order` (status CREATED, id = paypal_order_id).
2. **Authorize (pay)** — `client.orders.authorize_order(paypal_order_id,
   body=OrderAuthorizeRequest(payment_source=OrderAuthorizeRequestPaymentSource(card=CardRequest(...))),
   pay_pal_request_id=f"{number}-auth", prefer="return=representation")`. Card = raw
   number/expiry/security_code/name **or** `CardRequest(vault_id=<saved token>)`. Returns
   `OrderAuthorizeResponse`; auth lives at
   `purchase_units[0].payments.authorizations[0]` (`AuthorizationWithAdditionalData`: `id`, `status`,
   `expiration_time`, `amount`). If response `status == PAYER_ACTION_REQUIRED` or a payer-action/3DS
   link is present → **ChallengeRequired** (STOP/report; do not build approval round-trip).
   Verify authorized `amount` == order total to the cent (Decimal compare) else NEEDS_REVIEW.
3. **Fulfil (capture)** — `client.payments.capture_authorized_payment(authorization_id,
   pay_pal_request_id=f"{number}-capture", prefer="return=representation",
   body=CaptureRequest(final_capture=True))`. Returns `CapturedPayment`: `id`, `status`,
   `seller_receivable_breakdown.gross_amount/paypal_fee/net_amount`, `create_time`. On failure
   indicating expired/uncapturable auth → **reauthorize** then retry once.
4. **Reauthorize (stale auth)** — `client.payments.reauthorize_payment(authorization_id,
   body=ReauthorizeRequest(amount=Money(...)), pay_pal_request_id=f"{number}-reauth-{n}")`. Returns
   `PaymentAuthorization` with a new `id` (use it for the retry). If reauthorize itself fails →
   surface an operator-actionable error ("authorization can no longer be renewed; ask the shopper to
   pay again"). NB reauthorize is documented for PayPal-account payments; for a card auth it may be
   refused → that is exactly the "can no longer be renewed" branch.
5. **Cancel (void)** — `client.payments.void_payment(authorization_id,
   pay_pal_request_id=f"{number}-void")`. Returns `PaymentAuthorization` (status VOIDED).
6. **Refund** — `client.payments.refund_captured_payment(capture_id,
   body=RefundRequest(amount=Money(...))  # omit amount for full,
   pay_pal_request_id=<caller idempotencyKey>)`. Returns `Refund`: `id`, `status`, `amount`.
7. **Vault save** — `client.vault.create_payment_token(PaymentTokenRequest(
   customer=Customer(id=f"oscar-cust-{user.pk}"),
   payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(number, expiry,
   security_code, name))), pay_pal_request_id=<uuid>)`. Returns `PaymentTokenResponse`: `id` (token),
   `customer.id`, `payment_source.card` (`CardPaymentTokenEntity`: `last_digits`, `brand`, `expiry`)
   → safe description.
8. **Vault list** — `client.vault.list_customer_payment_tokens(customer_id, page_size, page,
   total_required=True)` → `CustomerVaultPaymentTokensResponse` (`total_pages`, `payment_tokens`).
   Local DB is the ownership source; PayPal used to confirm/enrich. (List served from local rows.)
9. **Vault delete** — `client.vault.delete_payment_token(token_id)` → `None` (use `with_raw_response`
   to see 204/404). Then delete local row.
10. **Reconciliation** — `client.transaction_search.search_transactions(start_date, end_date,
    fields="transaction_info", page_size=500, page=n)` paged 1..total_pages. Match `transaction_info.
    invoice_id` against local order numbers. **Case B** error (RawError only).

### Amounts

`value = order.total_incl_tax` (Decimal from catalogue prices), `currency_code = PAYPAL_CURRENCY`.
Format via `money.format_amount(Decimal, currency)` quantizing to the currency's exponent. "To the
cent" = Decimal equality after quantize.

### Idempotency / durable-write guard (python-configuration-resilience)

- `PayPalPayment` row is the durable claim, created at order creation. `pay`/`fulfil`/`cancel` take
  `select_for_update` on it; re-entry returns the recorded outcome (authorization_id/capture_id/void
  already set → return existing; no second authorize/capture). `PayPal-Request-Id` deterministic per
  (order, action) is the PayPal-side backstop.
- Refund: `PayPalRefund` unique `(payment, idempotency_key)` is the claim. Same key → return existing;
  `IntegrityError` on the insert → load & return existing. Two different keys = two legitimate partial
  refunds. Over-refund guard: `sum(non-failed refund amounts) + new ≤ captured_value`.
- No-op transitions (cancel already-cancelled) gated with conditional `.exclude(status=...).update()`
  so side effects fire once.
- Amount echoed-check after authorize (Decimal compare) → NEEDS_REVIEW on mismatch.

### Reconciliation correctness

- Page the whole range (bounded loop with page cap + `truncated` flag). `search_transactions` takes
  date-time instants, so no day-widening needed; still filter returned rows to `[from,to)` defensively.
- Match against the **set** of an order's transactions (auth + capture + refunds share invoice_id):
  group provider txns by invoice_id; each local order consumes its whole group; leftovers = provider-
  only; local orders with no group = app-only. Report matched / provider_only / app_only / (unsettled
  = local orders awaiting/authorized with no capture yet). Empty range in sandbox = expected, not a gap.

## CONTRACT SHEET (facts an implementer must obey — from map + source, no memory)

**Client (sync).** `PaypalClient(*, base_url=None, timeout=30.0, custom_http_client=None,
oauth2=ClientCredentialsOrDict|None, oauth2_token_source=None)`. All keyword-only. `close()` on
shutdown. Base URL default = sandbox if omitted → pass explicitly.

**Enums (wire values).**
- `CheckoutPaymentIntent`: `CAPTURE`, `AUTHORIZE`.
- `OrderStatus`: `CREATED, SAVED, APPROVED, VOIDED, COMPLETED, PAYER_ACTION_REQUIRED`.
- `AuthorizationStatus`: `CREATED, CAPTURED, DENIED, PARTIALLY_CAPTURED, VOIDED, PENDING`.
  → done: (none by itself; CAPTURED means already captured), live: CREATED, PENDING(accepted),
  ended: VOIDED/DENIED (failed). For "hold placed" success = `CREATED`.
- `CaptureStatus`: `COMPLETED`(done), `PENDING`(pending), `DECLINED/FAILED`(failed),
  `PARTIALLY_REFUNDED/REFUNDED`(post-refund states). unknown default = review.
- `RefundStatus`: `COMPLETED`(done), `PENDING`(pending/accepted), `CANCELLED/FAILED`(failed).
- `CardBrand`: VISA, MASTERCARD, AMEX, DISCOVER, … (open enum; read as `OrStr`).

**Operations in scope (signature highlights; `*`=keyword-only tail; `request_options` always last).**

| op | positional | key kwargs | returns | error union |
|---|---|---|---|---|
| `orders.create_order` | `body: OrderRequest\|Dict` | `pay_pal_request_id`, `prefer` | `Order` | `Error`[400,401,422] \| RawError |
| `orders.authorize_order` | `id: str` | `body: OrderAuthorizeRequest`, `pay_pal_request_id`, `prefer` | `OrderAuthorizeResponse` | `Error`[400,401,403,404,422,500] \| RawError |
| `orders.get_order` | `id: str` | — | `Order` | `Error`[401,404] \| RawError |
| `payments.capture_authorized_payment` | `authorization_id: str` | `body: CaptureRequest`, `pay_pal_request_id`, `prefer` | `CapturedPayment` | `Error`[400,401,403,404,409,422] \| RawError[500,…] |
| `payments.reauthorize_payment` | `authorization_id: str` | `body: ReauthorizeRequest`, `pay_pal_request_id` | `PaymentAuthorization` | `Error`[400,401,403,404,422] \| RawError[500,…] |
| `payments.void_payment` | `authorization_id: str` | `pay_pal_request_id`, `prefer` | `PaymentAuthorization` | `Error`[401,403,404,409,422] \| RawError[500,…] |
| `payments.refund_captured_payment` | `capture_id: str` | `body: RefundRequest`, `pay_pal_request_id`, `prefer` | `Refund` | `Error`[400,401,403,404,409,422] \| RawError[500,…] |
| `payments.get_captured_payment` | `capture_id: str` | — | `CapturedPayment` | `Error`[401,403,404] \| RawError[500,…] |
| `vault.create_payment_token` | `body: PaymentTokenRequest\|Dict` | `pay_pal_request_id` | `PaymentTokenResponse` | `Error`[400,403,404,422,500] \| RawError |
| `vault.list_customer_payment_tokens` | `customer_id: str` | `page_size=5,page=1,total_required=False` | `CustomerVaultPaymentTokensResponse` | `Error`[400,403,500] \| RawError |
| `vault.delete_payment_token` | `id: str` | — | **`None`** (use `with_raw_response` for 204/404) | `Error`[400,403,500] \| RawError |
| `transaction_search.search_transactions` | `start_date: str, end_date: str` | `fields="transaction_info", page_size=100(→500), page=1` | `SearchResponse` | **RawError only (Case B)** |

All typed arms above are `paypal.models.Error` (members `name`, `message`, `debug_id`, `details`).
`search_transactions` has NO typed arm — `.error` is always `RawError`.

**Request model shapes (members actually set; `Optional[T]=UNSET`, omit to leave provider default; `None` is NOT a legal value for these):**
- `OrderRequest(intent, purchase_units=[PurchaseUnitRequest], payment_source?)`. `intent` required.
- `PurchaseUnitRequest(amount=AmountWithBreakdown(currency_code,value), invoice_id?, custom_id?, description?)`. `amount` required.
- `OrderAuthorizeRequest(payment_source=OrderAuthorizeRequestPaymentSource(card=CardRequest))`.
- `CardRequest(name?, number?, expiry?  # "YYYY-MM", security_code?, vault_id?)` — raw card XOR vault_id.
- `CaptureRequest(amount?, final_capture?, ...)` — omit amount for full capture; set `final_capture=True`.
- `ReauthorizeRequest(amount?: Money)`.
- `RefundRequest(amount?: Money, custom_id?, invoice_id?, note_to_payer?)` — omit amount ⇒ full refund.
- `Money(currency_code: str, value: str)` — both required.
- `PaymentTokenRequest(customer?: Customer(id?), payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(number?,expiry?,security_code?,name?)))`. payment_source required.

**Response members I depend on (assert immediately; UNSET means absent, guard it):**
- `Order.id`, `Order.status`.
- `OrderAuthorizeResponse.status`, `.purchase_units[0].payments.authorizations[0]` →
  `.id`, `.status`, `.expiration_time`, `.amount.value/.currency_code`. Guard the list is non-empty.
- `CapturedPayment.id`, `.status`, `.seller_receivable_breakdown.gross_amount.value`,
  `.seller_receivable_breakdown.paypal_fee.value`, `.seller_receivable_breakdown.net_amount.value`,
  `.create_time`. (`seller_receivable_breakdown` absent while PENDING — guard.)
- `PaymentAuthorization.id`, `.status`, `.expiration_time`.
- `Refund.id`, `.status`, `.amount.value`.
- `PaymentTokenResponse.id`, `.customer.id`, `.payment_source.card.last_digits/.brand/.expiry`.
- `CustomerVaultPaymentTokensResponse.total_pages`, `.payment_tokens[].id`.
- `SearchResponse.total_pages`, `.transaction_details[].transaction_info.{transaction_id,invoice_id,
  transaction_amount,fee_amount,transaction_status,transaction_initiation_date}`.

**Money handling:** money is `str`. Build/compare with `Decimal`; format by currency exponent.
Currency code plain `str`.

**No retries in SDK.** Any retry (only the reauth-then-capture retry, once) is mine.
**Decode/token failure raises in both modes** and bypasses `with_raw_response` — handled first in the
boundary. Transport exceptions arrive unwrapped (`httpx.*`): split never-sent vs may-have-landed.

**Auth failure payload:** the map/getting-started mention `OAuthProviderError` for token-fetch
failures, but confirm whether `paypal.core` exports it before importing (installed-package check). If
not exported, treat a token-fetch `ApiError` by status/`RawError` instead. → VERIFY in Step 2.

## CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| authorize/capture/void/refund all act on ids minted by an earlier op (paypal_order_id→auth_id→capture_id→refund_id); each persisted on `PayPalPayment`/`PayPalRefund` before the next op needs it | create→authorize→capture→refund/void | implementation (durable row) |
| `pay` with `paymentMethodId` requires a `SavedCard` the caller owns; its `paypal_token_id` becomes `CardRequest.vault_id` | vault.create ← orders.authorize | implementation (ownership filter) |
| reconciliation matches provider txn `invoice_id` to local `order.number`; so `invoice_id`=order.number must be sent on create_order (and it propagates to capture/refund) | create_order → search_transactions | implementation |
| refund amount bounded by captured amount across all refunds of a capture | capture ← refunds | implementation (over-refund guard) |

## Assumptions & Blockers

- **Assumption:** account is entitled for direct card auth + vaulting (task states so). Will confirm
  with a live smoke (create→authorize→void; vault create→delete) in Step 2 before building on it.
- **Assumption:** catalogue numeric total is treated as an amount in `PAYPAL_CURRENCY` (no FX);
  task says "amounts come from catalogue prices; currency from configuration."
- **Assumption:** callers authenticate via Django session; API views are `csrf_exempt` (programmatic
  clients) but require `request.user.is_authenticated`; operator actions require `is_staff`. Self-
  verification drives endpoints via Django test `Client` with `force_login` against real PayPal.
- **No blockers** requiring user input (headless).

## REQUIRED READING (loaded before implementation)

- MUST load `python-error-handling` — every SDK call has an error boundary. **[loaded]**
- MUST load `python-client-initialization` — client factory/lifetime. **[loaded]**
- MUST load `python-authentication` — oauth2 + secret loading. **[loaded]**
- MUST load `python-calling-endpoints` — call shapes, status-from-provider. **[loaded]**
- MUST load `python-models` — UNSET/Optional/enums/Money/Decimal. **[loaded]**
- MUST load `python-configuration-resilience` — durable write, idempotency, reconciliation, pagination. **[loaded]**
- MUST load `python-testing` — stub transport seam for unit tests. **[loaded]**
