# PayPal integration plan — django-oscar sandbox (`sandbox/apps/payments`)

## SDK facts (verified against the installed package + its SDK map, v2.29)

| Fact | Value | Source |
|---|---|---|
| Distribution / import root | `paypal` / `paypal` (**not** `pay-pal-server-sdk` / `pay_pal_server_sdk` as the getting-started page says — the map and package disagree with it; the package wins) | `sdk-map.md`, `pip show paypal` |
| Install | `pip install "paypal @ git+https://github.com/context-plugins/paypal-python-sdk.git@main"` into `venv` | getting-started |
| Client | **sync** `PaypalClient` (Django under WSGI → sync). Keyword-only: `base_url`, `timeout` (30.0 default), `custom_http_client`, `oauth2`, `oauth2_token_source` | `sdk-map.md` |
| Auth | `oauth2=ClientCredentials(client_id=…, client_secret=…)` from `paypal.core`. Omitting it = unauthenticated, silently. Token fetched lazily from `<base_url>/v1/oauth2/token`, cached on the client | `sdk-map.md` Servers & auth |
| Base URL | only server `https://api-m.sandbox.paypal.com` (default when `base_url` omitted — silent). `PAYPAL_BASE_URL` set → passed verbatim (moves the token call too). `PAYPAL_ENVIRONMENT=sandbox` → the SDK's declared sandbox URL, passed explicitly. Any other environment without `PAYPAL_BASE_URL` → `ImproperlyConfigured` (the SDK declares no other host; not guessed) | `sdk-map.md`, `server/server_config.py` |
| Lifetime | one lazily-built module-level client per process (post-fork safe), closed via `atexit` | client-initialization |
| Retries | **SDK performs none.** We add none on writes; writes are made safe by PayPal-Request-Id + durable claim rows; unknown outcomes are resumed under the same request id | configuration-resilience |
| Timeout | client `timeout=20.0` | — |
| Transport logging | wrap `HttpxClient` in a transport that logs method, URL path, status, ms — never headers/bodies (card data) | configuration-resilience |
| Core imports | `ApiError, RawError, HttpxClient, HttpRequest, HttpResponse, OAuthProviderError, ClientCredentials, UNSET, UnsetType` from `paypal.core`; models from `paypal.models`; enums from `paypal.models.enums` | `core/__init__.py` `__all__` |
| Keyword-only | every param after `*` has a real default — no defensive `None`s | map |
| `prefer` default | `"return=minimal"` on create_order / capture / refund / void / reauthorize → **we pass `prefer="return=representation"`** or the breakdown/amount/times are absent | map + docstrings |

## Per-operation contract sheet

All typed arms are `paypal.models.Error` (`name: str`, `message: str`, `debug_id: str`, `details: Optional[list[ErrorDetails]]` with `issue: str`, `description: Optional[str]`). Union last arm is always `RawError`.

| Operation (sync, parsed) | Positional / keyword-only we use | Returns | Error union (status → arm) |
|---|---|---|---|
| `orders.create_order(body, *, pay_pal_request_id, prefer)` | body `OrderRequest` | `Order` | `CreateOrderErrorBody` = `Error` [400,401,422] \| `RawError` |
| `orders.authorize_order(id, *, pay_pal_request_id, prefer)` | only if create returns `APPROVED` without an authorization | `OrderAuthorizeResponse` | `AuthorizeOrderErrorBody` = `Error` [400,401,403,404,422,500] \| `RawError` |
| `orders.get_order(id)` | reconcile an unknown create | `Order` | `Error` [401,404] \| `RawError` |
| `payments.get_authorized_payment(authorization_id)` | staleness check before capture | `PaymentAuthorization` | `Error` [401,403,404] \| `RawError` (500+) |
| `payments.reauthorize_payment(authorization_id, *, pay_pal_request_id, prefer, body)` | body `ReauthorizeRequest(amount=Money)` | `PaymentAuthorization` | `Error` [400,401,403,404,422] \| `RawError` |
| `payments.capture_authorized_payment(authorization_id, *, pay_pal_request_id, prefer, body)` | body `CaptureRequest(amount=Money, final_capture=True, invoice_id)` | `CapturedPayment` | `Error` [400,401,403,404,409,422] \| `RawError` |
| `payments.get_captured_payment(capture_id)` | refresh a PENDING capture | `CapturedPayment` | `Error` [401,403,404] \| `RawError` |
| `payments.void_payment(authorization_id, *, pay_pal_request_id, prefer)` | — | `PaymentAuthorization` | `Error` [401,403,404,409,422] \| `RawError` |
| `payments.refund_captured_payment(capture_id, *, pay_pal_request_id, prefer, body)` | body `RefundRequest(amount=Money, invoice_id)` | `Refund` | `Error` [400,401,403,404,409,422] \| `RawError` |
| `vault.create_payment_token(body, *, pay_pal_request_id)` | body `PaymentTokenRequest(customer=Customer(id=…)?, payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(...)))` | `PaymentTokenResponse` | `Error` [400,403,404,422,500] \| `RawError` |
| `vault.delete_payment_token(id)` | **returns `None`**; use `with_raw_response` to see the status | `None` | `Error` [400,403,500] \| `RawError` (404 → already gone) |
| `transaction_search.search_transactions(start_date, end_date, *, page_size, page)` | RFC3339 with seconds; **max range 31 days**; `page` starts at 1; default `fields="transaction_info"`, `balance_affecting_records_only="Y"` (smoke: `"N"` answered 503 on sandbox) | `SearchResponse` | **Case B**: always `RawError` |

`with_raw_response` used for: `vault.delete_payment_token` only.

### Request members we set (everything else left `UNSET` → PayPal/account default)

- `OrderRequest`: `intent` (required, `CheckoutPaymentIntent.AUTHORIZE`), `purchase_units` (required, one `PurchaseUnitRequest`), `payment_source`.
- `PurchaseUnitRequest`: `amount` (required `AmountWithBreakdown(currency_code, value)`), `reference_id`=order number, `custom_id`=order number (reconciliation key), `invoice_id`=`<order number>-<payment ref hex[:12]>` (must be unique per PayPal account — the live run hit `DUPLICATE_INVOICE_ID` with `<order number>-<attempt>` because Oscar order numbers repeat across installs sharing the account).
- `PaymentSource.card` = `CardRequest(name, number, expiry "YYYY-MM", security_code, billing_address=Address(country_code required, …))` for one-off; `CardRequest(vault_id=…)` for a saved card.
- `PaymentTokenRequestCard(name, number, expiry, security_code, billing_address)`; `Customer(id=…)` only when this user already has a PayPal customer id (omit → PayPal creates one; its id is stored).
- `Money(currency_code: str, value: str)` both required; value formatted per currency exponent (JPY/KRW 0, KWD/BHD/TND 3, else 2) from `Decimal`.
- No `stored_credential`, `experience_context`, `verification` → omitted = provider default (account's 3DS/verification configuration governs).

### Response members asserted (absent → outcome unknown / needs_review)

- create_order → `id`, `status`; `purchase_units[0].payments.authorizations[0]` → `id`, `status`, `amount`, `expiration_time`, `create_time`. Smoke: card source + AUTHORIZE → order `COMPLETED` with authorization already `CREATED` (a following `authorize_order` answers 422 `ORDER_ALREADY_AUTHORIZED`).
- capture → `id`, `status`, `amount`, `seller_receivable_breakdown.gross_amount / paypal_fee / net_amount`, `create_time`.
- refund → `id`, `status`, `amount`, `create_time` (offset times, e.g. `-07:00` → parse with `fromisoformat`).
- token → `id`, `customer.id`, `payment_source.card.brand / last_digits / expiry`.
- search → `transaction_details[].transaction_info.transaction_id / transaction_event_code / transaction_initiation_date / transaction_amount / fee_amount / transaction_status / invoice_id / custom_field`; `total_pages`, `page`.

### Status mappings (every member by name; default arm = `unknown`)

- `OrderStatus`: `COMPLETED` → authorization present, read it · `APPROVED` → call `authorize_order` · `PAYER_ACTION_REQUIRED` → **stop**: failed with "requires shopper approval in a browser — unsupported" (task: report, do not build) · `CREATED`/`SAVED` → pending · `VOIDED` → failed · else unknown.
- `AuthorizationStatus`: `CREATED` → authorized · `PENDING` → pending · `DENIED` → failed · `VOIDED` → voided · `CAPTURED`/`PARTIALLY_CAPTURED` → captured · else unknown.
- `CaptureStatus`: `COMPLETED`/`PARTIALLY_REFUNDED`/`REFUNDED` → captured · `PENDING` → capture_pending · `DECLINED`/`FAILED` → failed · else unknown.
- `RefundStatus`: `COMPLETED` → completed · `PENDING` → pending · `FAILED`/`CANCELLED` → failed (reservation released) · else unknown.
- `AuthorizationStatus` on void: `VOIDED` → voided · else unknown.

## Design

- New app `sandbox/apps/payments` (label `payments`), URLs included under `api/` in `sandbox/urls.py` (outside `i18n_patterns`, like `admin/`).
- Reuses Oscar models: `order.Order`/`order.Line` (placed through Oscar's `Basket` + `OrderCreator`, strategy pricing, offers applied), `payment.Source`/`payment.SourceType`/`payment.Transaction` (allocate / debit / refund ledger), `order.Order.set_status` (pipeline extended in settings: `Awaiting payment → Payment authorised → Fulfilled`, cancellable before fulfilment).
- Own models (PayPal state only): `PayPalPayment` (one live per order: partial unique constraint on `order` where state ≠ failed; ref uuid; paypal order/authorization/capture ids+statuses, provider times, gross/fee/net, `refund_reserved`), `PayPalRefund` (unique `(payment, idempotency_key)`), `SavedCard` (user, PayPal token id, brand/last4/expiry, customer id, state), `PayPalCustomer` (user → PayPal customer id), `OrderRequestKey` (optional `Idempotency-Key` on order placement).
- Claims: pay = insert `PayPalPayment` (constraint decides); fulfil/cancel = conditional `UPDATE … WHERE state IN (…)`; refund = insert refund row + conditional `UPDATE refund_reserved = refund_reserved + x WHERE refund_reserved + x <= captured` in one transaction; save card = insert `SavedCard(state=creating, client_ref unique per user)`; delete = conditional update active→deleting.
- PayPal-Request-Id per logical action derived from the claim row: `<ref>-create`, `<ref>-capture`, `<ref>-reauth-<n>`, `<ref>-void`, `refund-<uuid>`, `card-<uuid>`. Unknown outcome → row `unknown`; the same endpoint re-invoked resends under the same id (PayPal dedupes; docstrings: 45 days payments, 3 h vault).
- Error ladder (one place, `errors.py`): OAuthProviderError/401/403 → 502 config · 429 → 503 · typed `Error` with 400/404/409/422 → caller's (422 for business rejections, with PayPal `issue`) · other → 502 · `ValidationError`/`ValueError` on decode → unknown (504) · `ConnectError/ConnectTimeout/PoolTimeout/ProxyError` → 502 never sent · other `httpx.RequestError` → 504 outcome unknown.
- Fulfil: re-read authorization; `expiration_time` passed → 409 "authorization expired, can no longer be renewed — cancel and ask the shopper to pay again"; past the 3-day honor period (from provider `create_time`) → `reauthorize_payment` first; reauth rejected → 409 with PayPal's issue, same operator wording. Capture 422 → one reauthorize-and-retry.
- Reconciliation (staff): validate ISO-8601 `from`/`to`; split into ≤31-day windows; page each window `page=1..total_pages` with page-cap + `truncated` flag; match PayPal `transaction_id` against local capture/refund ids (set matching); local side filtered on provider times; report `matched`, `paypalOnly`, `appOnly`, `unsettled`.
- `GET /api/products` lists purchasable catalogue ids and prices so every flow is drivable through the API alone.
- Security: session auth + CSRF (endpoint `GET /api/csrf` hands out the cookie; login through `POST /api/login` using `django.contrib.auth.authenticate/login`), JSON 401/403; card fields validated locally (digits, Luhn, expiry) before any model is built; views `sensitive_variables`/`sensitive_post_parameters`; never log bodies; saved card responses expose brand/last4/expiry only.
- Currency: `PAYPAL_CURRENCY`; the amount is the catalogue price (Oscar strategy) quantised to the currency exponent; `Order.currency` = `PAYPAL_CURRENCY`.

## CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| `vault_id` sent on create_order must be a token id returned by `create_payment_token` for **this** user and not deleted | `orders.create_order` ← `vault.create_payment_token` | `SavedCard` lookup by (user, active) |
| `Customer.id` sent on create_payment_token must be one PayPal returned for this user | `vault.create_payment_token` ← previous response | `PayPalCustomer` row |
| capture / void / reauthorize `authorization_id` must be the one create_order (or reauthorize) returned for this order | payments.* ← orders.create_order | `PayPalPayment.authorization_id` |
| refund `capture_id` is the capture of this order; Σ refunds ≤ captured gross | refund ← capture | `refund_reserved` conditional update |
| capture amount = authorized amount = order total | capture ← create_order | echo verified as `Decimal` before settle |
| reconciliation ids are PayPal's capture/refund ids | search_transactions ↔ capture/refund | report matcher |

## Assumptions & Blockers

- No blockers. Minor: `list_customer_payment_tokens` returned no tokens right after vaulting in the smoke, so the saved-card list is served from the local `SavedCard` table (ownership lives there anyway).
- Sandbox 3DS challenge (`PAYER_ACTION_REQUIRED`) did not occur for the test Visa; code stops with a clear error if it does.
- The getting-started skill page's identity table (`pay_pal_server_sdk`) disagrees with the shipped package (`paypal`); the package + its map were used.

## Toolchain

- `venv` (py 3.11), `venv\Scripts\pip install -e .[test]` + SDK. Tests: `venv\Scripts\python sandbox\manage.py test apps.payments` (sandbox settings); type check: `mypy --strict` on the new app (installed into venv).

## REQUIRED READING (loaded before implementation)

- MUST load `python-client-initialization` — client placement (WSGI lazy global, post-fork). ✔
- MUST load `python-authentication` — oauth2 keyword, OAuthProviderError. ✔
- MUST load `python-calling-endpoints` — prefer default narrows responses, status mapping, `None`-returning delete. ✔
- MUST load `python-models` — UNSET vs None, open enums, Money as str with currency exponent. ✔
- MUST load `python-error-handling` — ladder, never-sent vs unknown. ✔
- MUST load `python-configuration-resilience` — claims, reconcile, pagination bounds, reconciliation clocks. ✔
- MUST load `python-testing` — stub transport seam. ✔
