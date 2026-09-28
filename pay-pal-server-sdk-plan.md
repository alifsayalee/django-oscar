# PayPal payments + saved cards for the Oscar sandbox — plan & contract sheet

## Scope

New Django app `sandbox/apps/shop_payments` (label `shop_payments`), routed at `/api/` from
`sandbox/urls.py`. Flows: place order → authorize (one-off card or saved card) → fulfil (capture,
reauthorize if stale) / cancel (void) → refunds (partial/full, idempotency key) → my-orders;
saved cards (vault create / list / delete); reconciliation (transaction search, all pages, all
≤31-day windows). Reuses Oscar models: `order.Order`/`Line` (placed through Oscar's `OrderCreator`
from a server-side `Basket`), `payment.Source`/`SourceType`/`Transaction` (amount ledger),
`payment.Bankcard` (saved card: masked number + expiry + `partner_reference` = vault token id).
New local models only for PayPal-owned state and write claims.

## Repo survey (conventions — pattern + exemplar)

| Convention | Pattern | Exemplar to imitate |
| --- | --- | --- |
| Sandbox apps | packages under `sandbox/apps/`, imported as `apps.<name>` | `sandbox/apps/offers.py`, `sandbox/apps/sitemaps.py` |
| URL wiring | plain `path()` entries before `i18n_patterns` | `sandbox/urls.py` |
| Settings | `django-environ` `env(...)` reads in `sandbox/settings.py` | `sandbox/settings.py` (`DEBUG = env.bool(...)`) |
| Oscar class loading | `get_class` / `get_model` | `src/oscar/apps/checkout/session.py` |
| Order status pipeline | `Pending → Being processed → Complete/Cancelled` | `sandbox/settings.py` `OSCAR_ORDER_STATUS_PIPELINE` |
| Tests | pytest + pytest-django, bare `assert`, `tests/` with `tests/settings.py` | `tests/integration/payment/test_models.py` |

- **Sync vs async:** Django under WSGI, sync views → **sync `PaypalClient`** (never `AsyncPaypalClient`).
- **Toolchain:** `py -3.11 -m venv venv`; `venv\Scripts\pip install -e .[test]`; SDK installed
  (non-editable) from the `main` branch of https://github.com/context-plugins/paypal-python-sdk —
  pip's own git clone was reset by the network, so it was installed from a `--depth 1 --branch main`
  clone of the same repo (built into a wheel; nothing points at the clone). Tests:
  `venv\Scripts\python -m pytest`; type check: `venv\Scripts\python -m mypy --strict` over the new
  app (project has no mypy config; mypy + django-stubs installed into the venv).
- **SDK identity drift vs plugin text:** installed distribution is **`paypal` 2.29**, import root
  **`paypal`** (the getting-started page says `pay_pal_server_sdk`; the SDK map and the package
  itself say `paypal`, client class `PaypalClient`). The map/package are authoritative.
- **Credentials:** `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENVIRONMENT` (`sandbox`),
  `PAYPAL_CURRENCY` (`USD`) present in env; read only through `sandbox/settings.py`.
- **Smoke (scratch, real sandbox, 2026-09-28):** single-step `create_order` (intent AUTHORIZE,
  `payment_source.card`) → order `COMPLETED` with `purchase_units[0].payments.authorizations[0]`
  status `CREATED` (no separate `authorize_order` needed, no payer action); `get_authorized_payment`
  OK; `reauthorize_payment` on a fresh auth → 422 `REAUTHORIZATION_TOO_SOON`;
  `capture_authorized_payment` (representation) → `COMPLETED` + `seller_receivable_breakdown`
  gross/fee/net; `refund_captured_payment` partial → `COMPLETED`; `vault.create_payment_token`
  with a raw card → token + `customer.id`; order with `card.vault_id` → authorized; `void_payment`
  → `VOIDED`; `delete_payment_token` raw → 204; `search_transactions` → 200, `total_pages` > 1,
  `custom_field` carries our `custom_id`. No 403 anywhere.

## Contract sheet

### Client

| Fact | Value | Source |
| --- | --- | --- |
| Class | `paypal.PaypalClient` (sync); keyword-only ctor | sdk-map.md "Getting a client" |
| Keywords used | `base_url: str`, `timeout: float` (default 30.0 — we set `PAYPAL_TIMEOUT`, default 20), `custom_http_client: HttpClient` (logging wrapper over `paypal.core.HttpxClient`), `oauth2: ClientCredentials(client_id, client_secret)` | sdk-map.md ctor table |
| Transport timeout | with `custom_http_client` the ctor `timeout` does NOT reach the wire → set it on `HttpxClient(timeout=...)` | python-configuration-resilience |
| Base URL | SDK declares ONE server `https://api-m.sandbox.paypal.com`; omission silently = sandbox. `PAYPAL_BASE_URL` set → used verbatim (moves token fetch too). Else `PAYPAL_ENVIRONMENT == "sandbox"` → the SDK's declared URL; any other value without `PAYPAL_BASE_URL` → `ImproperlyConfigured` (the plugin names no other host) | sdk-map.md "Servers & auth" |
| Lifetime | one lazily-built client per process (post-fork safe), `close()` at `atexit` | python-client-initialization |
| Auth | `oauth2=` MUST be set; omission = unauthenticated silently. Token fetched lazily at first call; failure = `ApiError` with `.error` `OAuthProviderError \| RawError` — checked first in the ladder → 502 | python-authentication |
| Retries | **SDK performs none.** We retry only idempotent reads (`get_*`, `search_transactions`) on connect errors/429/5xx, 3 attempts. Writes are never blindly retried; unknown outcomes are resolved by a same-`PayPal-Request-Id` resend | python-configuration-resilience |
| Keyword-only boundary | every param after `*` has a real default — pass only what we set, never defensive `None`s | map pages |

### Operations in scope (all `Error | RawError` union, `Error` from `paypal.models`: `name`, `message`, `debug_id`, `details`)

| Operation | Positional | Keyword-only we set | Returns | Error arms | Notes |
| --- | --- | --- | --- | --- | --- |
| `client.orders.create_order` | `body: OrderRequest\|Dict` | `pay_pal_request_id` (mandatory for single-step card create; kept 6h), `prefer="return=representation"` | `Order` | `Error` [400,401,422] · `RawError` | map orders.md; docstring on request id |
| `client.payments.get_authorized_payment` | `authorization_id` | — | `PaymentAuthorization` | `Error` [401,403,404] · `RawError` [500,…] | read |
| `client.payments.reauthorize_payment` | `authorization_id` | `pay_pal_request_id` (kept 45 days), `prefer`, `body: ReauthorizeRequest` (`amount: Money`) | `PaymentAuthorization` | `Error` [400,401,403,404,422] · `RawError` [500,…] | docstring: honor period 3 days; reauth allowed within 29-day period; after 30 days must re-authorize from scratch. Sandbox: once only, day 4–29 |
| `client.payments.capture_authorized_payment` | `authorization_id` | `pay_pal_request_id` (45 days), `prefer`, `body: CaptureRequest` (`amount`, `final_capture=True`, `invoice_id`) | `CapturedPayment` | `Error` [400,401,403,404,409,422] · `RawError` [500,…] | |
| `client.payments.void_payment` | `authorization_id` | `pay_pal_request_id` (45 days), `prefer` | `PaymentAuthorization` (may be empty on minimal) | `Error` [401,403,404,409,422] · `RawError` [500,…] | "cannot void a fully captured authorization" |
| `client.payments.refund_captured_payment` | `capture_id` | `pay_pal_request_id` (45 days), `prefer`, `body: RefundRequest` (`amount: Money`, `invoice_id`, `custom_id`) | `Refund` | `Error` [400,401,403,404,409,422] · `RawError` [500,…] | empty body = full refund; we always send the amount |
| `client.payments.get_refund` | `refund_id` | — | `Refund` | `Error` [401,403,404] · `RawError` | read (refresh pending refunds) |
| `client.payments.get_captured_payment` | `capture_id` | — | `CapturedPayment` | `Error` [401,403,404] · `RawError` | read (refresh pending captures) |
| `client.vault.create_payment_token` | `body: PaymentTokenRequest` (`payment_source.card: PaymentTokenRequestCard`) | `pay_pal_request_id` (kept 3h) | `PaymentTokenResponse` | `Error` [400,403,404,422,500] · `RawError` | |
| `client.vault.delete_payment_token` | `id` | — | **`None`** → use `with_raw_response` (`ApiResult[None, DeletePaymentTokenErrorBody]`) | `Error` [400,403,500] · `RawError` | one of the 11 None-returning ops |
| `client.orders.get_order` | `id` | — | `Order` | `Error` [401,404] · `RawError` | read (refresh an authorization left pending without an authorization id) |
| `client.vault.get_payment_token` | `id` | — | `PaymentTokenResponse` | `Error` [403,404,422,500] · `RawError` | read (rebuild a saved card whose vault write settled on an earlier request) |
| `client.transaction_search.search_transactions` | `start_date: str`, `end_date: str` (RFC 3339, seconds required, range ≤ 31 days) | `page_size` (default 100), `page` (default 1), `fields` default `"transaction_info"`, `balance_affecting_records_only` default `"Y"` | `SearchResponse` | **Case B: `RawError` only** | ≤3h reporting lag |

### Model members we set (required vs `UNSET`; `Optional[T]` = `T | UnsetType`, never pass `None`)

| Model | Required | Optional members we set |
| --- | --- | --- |
| `OrderRequest` | `intent: CheckoutPaymentIntentOrStr` (`CheckoutPaymentIntent.AUTHORIZE`), `purchase_units: list[PurchaseUnitRequest]` | `payment_source: PaymentSource` |
| `PurchaseUnitRequest` | `amount: AmountWithBreakdown` (`currency_code: str`, `value: str` — both required) | `reference_id`, `custom_id` (= our order reference), `invoice_id` (= our order reference), `description` |
| `PaymentSource` | — | `card: CardRequest` |
| `CardRequest` | — | one-off: `name`, `number`, `expiry` ("YYYY-MM"), `security_code`, `billing_address: Address`; saved: `vault_id` |
| `Address` | `country_code: str` | `address_line_1`, `address_line_2`, `admin_area_1`, `admin_area_2`, `postal_code` |
| `PaymentTokenRequest` | `payment_source: PaymentTokenRequestPaymentSource` | — (`customer` left unset) |
| `PaymentTokenRequestCard` | — | `name`, `number`, `expiry`, `security_code`, `billing_address` |
| `CaptureRequest` / `RefundRequest` / `ReauthorizeRequest` | — | `amount: Money` (`currency_code`, `value` required), `final_capture`, `invoice_id`, `custom_id` |

Money is `str` → formatted from `Decimal` with the currency's ISO-4217 exponent (python-models table);
echoes compared as `Decimal`.

### Response members we assert on, and every status member → outcome

- **create_order → `Order`**: `id`, `status: OrderStatusOrStr`, `purchase_units[0].payments.authorizations[0]`
  (`id`, `status`, `amount.value/currency_code`, `create_time`, `expiration_time`), `payment_source.card.last_digits/brand`.
  - `AuthorizationStatus`: `CREATED` → **done** (held) · `PENDING` → **pending** · `DENIED` → **failed** · `VOIDED` → **failed** (undone) · `CAPTURED`, `PARTIALLY_CAPTURED` → **needs_review** (taken outside this step) · anything else / absent → **unknown**.
  - If no authorization is present: `OrderStatus` `PAYER_ACTION_REQUIRED` → **failed** (3-DS challenge — not supported, reported with code `payer_action_required`) · `VOIDED` → **failed** · `CREATED`, `SAVED`, `APPROVED` → **pending** · `COMPLETED` (no auth) / unlisted / absent → **unknown**.
- **reauthorize → `PaymentAuthorization`**: `id` (new authorization), `status`, `amount`, `expiration_time`, `create_time`; same `AuthorizationStatus` map as above.
- **capture → `CapturedPayment`**: `id`, `status: CaptureStatusOrStr`, `amount`, `seller_receivable_breakdown.gross_amount/paypal_fee/net_amount`, `create_time`.
  - `CaptureStatus`: `COMPLETED` → **done** · `PENDING` → **pending** · `DECLINED`, `FAILED` → **failed** · `PARTIALLY_REFUNDED`, `REFUNDED` → **failed** (done then undone — for the capture step) · unlisted/absent → **unknown**.
- **void → `PaymentAuthorization`** (cancel mapper — the undoing is its done): `VOIDED` → **done** · `DENIED` → **done** (no hold exists) · `CAPTURED`, `PARTIALLY_CAPTURED` → **failed** (too late: money taken) · `CREATED`, `PENDING` → **pending** (hold still in place) · unlisted/absent → **unknown**.
- **refund → `Refund`**: `id`, `status: RefundStatusOrStr`, `amount`, `create_time`.
  - `RefundStatus`: `COMPLETED` → **done** · `PENDING` → **pending** · `FAILED`, `CANCELLED` → **failed** · unlisted/absent → **unknown**.
- **vault create → `PaymentTokenResponse`** (no status member in the model): `id` + `payment_source.card` (`last_digits`, `brand`, `expiry`) + optional `card.verification_status: CardVerificationStatus`: `FAILED` → **failed** · `VERIFIED` or unset **with** id and card last digits present → **done** · id or card missing → **unknown**.
- **vault delete (raw)**: `Success` (2xx) → **done**; `Failure` 404 → **done** (already gone); other `Failure` → error ladder.
- **search → `SearchResponse`**: `transaction_details[].transaction_info` (`transaction_id`, `paypal_reference_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_amount`, `fee_amount`, `transaction_status`, `custom_field`, `invoice_id`), `total_pages`, `page`.
- Decode failure (`ValidationError`/`ValueError`) is **not** `ApiError` and bypasses both modes: on a
  write it is "may have landed" → lookup; on a read → 502.

## Tables

### OPERATION OUTCOMES

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/orders/{id}/pay` → `orders.create_order` (authorization inside) | `purchase_units[0].payments.authorizations[0].status` (`AuthorizationStatus`), falling back to `Order.status` when no authorization | CREATED → done → 200 `payment.state: authorized`; PENDING → pending → 202; DENIED/VOIDED → failed → 402 `card_declined`; a PayPal 4xx refusal → 402 `payment_declined`; CAPTURED/PARTIALLY_CAPTURED → needs_review → 409; order PAYER_ACTION_REQUIRED → failed → 402 `payer_action_required`; order CREATED/SAVED/APPROVED → pending → 202; unlisted/absent → unknown → 504 `outcome_unknown` | `payments.pay` → `safe_write.safe_write` (outcome via `payments.authorize_step_outcome` → `outcomes.authorization_outcome` / `outcomes.order_without_authorization_outcome`) → `payments._apply_authorization` → `safe_write.answer_for`; view `views.pay` / `views._with_order` |
| `POST /api/orders/{id}/fulfil` → `payments.reauthorize_payment` (only when stale) | `PaymentAuthorization.status` | CREATED → done → continue to capture; PENDING → pending → 202 (fulfil not done); DENIED/VOIDED → failed → 409 `authorization_not_renewable`; CAPTURED/PARTIALLY_CAPTURED → needs_review → 409; unlisted → unknown → 504 | `payments._reauthorize` (`outcomes.authorization_outcome`; refusal → 409 `authorization_not_renewable`) |
| `POST /api/orders/{id}/fulfil` → `payments.capture_authorized_payment` | `CapturedPayment.status` | COMPLETED → done → 200 `paymentState: captured` + amount/fee/net; PENDING → pending → 202 `capture_pending`; DECLINED/FAILED → failed → 409 `capture_failed`; PARTIALLY_REFUNDED/REFUNDED → failed (undone) → 409; unlisted → unknown → 504 | `payments.fulfil` → `safe_write.safe_write` (`outcomes.capture_outcome`) → `payments._apply_capture` → `safe_write.answer_for` |
| `POST /api/orders/{id}/cancel` → `payments.void_payment` | `PaymentAuthorization.status` (cancel mapper) | VOIDED/DENIED → done → 200 `paymentState: voided`, order Cancelled; CREATED/PENDING → pending → 202; CAPTURED/PARTIALLY_CAPTURED → failed → 409 `already_captured` (refund instead); unlisted/empty → unknown → 504 | `payments.cancel` → `safe_write.safe_write` (`outcomes.void_outcome`) → `safe_write.answer_for` |
| `POST /api/orders/{id}/refunds` → `payments.refund_captured_payment` | `Refund.status` | COMPLETED → done → 200; PENDING → pending → 202; FAILED/CANCELLED → failed → 409 (reservation released); unlisted → unknown → 504 | `payments._send_refund` → `safe_write.safe_write` (`outcomes.refund_outcome`) → `payments._settle_refund` → `safe_write.answer_for` |
| repeat of any of the above (same order / same refund key) | stored outcome on the claim; `pending` refreshed by a read (`get_authorized_payment` / `get_captured_payment` / `get_refund`) | answered through the same `answer()`; `sending` inside window → 202 `in_progress` | `safe_write.safe_write` (claim-held branch), `payments._refresh_pending_authorization`, `payments._refresh_pending_capture`, `payments._repeat_refund`, `safe_write.refresh` |
| `POST /api/payment-methods` → `vault.create_payment_token` | `payment_source.card.verification_status` + presence of `id`/card | FAILED → failed → 402; id+card present → done → 201 `paymentMethodId`; missing → unknown → 504 | `cards.save_card` → `safe_write.safe_write` (`cards.read_token` + `outcomes.vault_outcome`) → `cards._record_card` |
| `DELETE /api/payment-methods/{id}` → `vault.delete_payment_token` (raw) | HTTP status of `ApiResult` | 2xx → done → 204; 404 → done → 204; other → error ladder (card stays hidden + unusable, claim `unknown`) → 502/504 | `cards.delete_card` (raw `with_raw_response.delete_payment_token`, `outcomes.delete_outcome`) → `safe_write.answer_for` |

### DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| authorize (create_order) | `shop_payments.PaymentWrite` row, `reference = <prefix>:o<number>:auth:<attempt>` (DB) | DB `UNIQUE(reference)` → `IntegrityError` | `try_claim` inside `safe_write` | `safe_write.try_claim` (called from `safe_write.safe_write` in `payments.pay`) |
| reauthorize | `PaymentWrite`, `reference = <prefix>:o<number>:reauth:<authorization_id>` | `UNIQUE(reference)` | `try_claim` | `safe_write.try_claim` via `payments._reauthorize` |
| capture | `PaymentWrite`, `reference = <prefix>:o<number>:capture:<authorization_id>`; plus `PayPalPayment.state` compare-and-set `authorized → capturing` (excludes a concurrent void) | `UNIQUE(reference)`; conditional `UPDATE … WHERE state='authorized'` affecting 0 rows | `try_claim`; `_transition` | `safe_write.try_claim` + `payments._transition` in `payments.fulfil` |
| void | `PaymentWrite`, `reference = <prefix>:o<number>:void:<authorization_id>`; `state` CAS `authorized → voiding` | `UNIQUE(reference)`; conditional UPDATE | `try_claim`; `_transition` | `safe_write.try_claim` + `payments._transition` in `payments.cancel` |
| refund | `PayPalRefund` row `UNIQUE(payment, idempotency_key)` + `PaymentWrite` `reference = <prefix>:o<number>:refund:<sha256(key)[:24]>`; refundable balance reserved by conditional `UPDATE … SET refund_reserved = refund_reserved + x WHERE refund_reserved + x <= captured_amount` | unique constraints; conditional UPDATE affecting 0 rows → 409 over-refund | `create_refund` / `try_claim` | `payments.refund` (conditional `UPDATE` reservation + `PayPalRefund`/`PaymentWrite` inserts in one `atomic`, `IntegrityError` → `payments._repeat_refund`) |
| vault create | `PaymentWrite`, `reference = <prefix>:u<user_pk>:card:<sha256(Idempotency-Key)[:24]>` (a fresh random key when the caller sends none — then each POST is a new card by definition) | `UNIQUE(reference)` | `try_claim` | `safe_write.try_claim` via `cards.save_card` |
| vault delete | `PaymentWrite`, `reference = <prefix>:card:<bankcard_pk>:delete` | `UNIQUE(reference)` | `try_claim` | `safe_write.try_claim` via `cards.delete_card`; `cards.usable_cards` hides the card while the claim exists |

`<prefix>` = `PAYPAL_REFERENCE_PREFIX` setting, or else a random id generated once and persisted in the
DB (`InstallIdentity`) — never shared by two installs using the same merchant account.

### UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| authorize (create_order) | same-reference resend (`PayPal-Request-Id` idempotent, 6h window); `repeat_is_safe=True` | the claim's `reference` sent as `PayPal-Request-Id` (also `custom_id`/`invoice_id`) | `safe_write.safe_write` → `safe_write._find(resend=send)`; repeat request → claim-held `checking` branch re-sends `payments.pay.send` |
| reauthorize | same-reference resend (45 days) | claim `reference` as `PayPal-Request-Id` | `safe_write._find(resend=send)` from `payments._reauthorize` |
| capture | same-reference resend (45 days) | claim `reference` as `PayPal-Request-Id` | `safe_write._find(resend=send)` from `payments.fulfil` |
| void | same-reference resend; empty body read through `get_authorized_payment(authorization_id)` | claim `reference` as `PayPal-Request-Id` | `safe_write._find(resend=send, find=lookup)` from `payments.cancel` |
| refund | same-reference resend (45 days) | claim `reference` as `PayPal-Request-Id` | `safe_write._find(resend=send)` from `payments._send_refund`; `payments._repeat_refund` for later repeats |
| vault create | same-reference resend (3h window) | claim `reference` as `PayPal-Request-Id` | `safe_write._find(resend=send)` from `cards.save_card` |
| vault delete | re-send delete by token id (a 404 means gone) | token id (`Bankcard.partner_reference`) under the delete claim | `safe_write._find(resend=send)` from `cards.delete_card` (its `send` maps 404 to `DELETED`) |

### WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| authorize | Oscar `Order` (Pending) + `PayPalPayment` (state `authorizing`, attempt n) + `PaymentWrite` claim `sending` with the reference | claim outcome + provider id/time; `PayPalPayment` paypal order id, authorization id/status/created/expires, card label; Oscar `Source` allocate + `Transaction`; order → `Being processed` | before: `orders.place_order` (Order + `PayPalPayment`), `payments.pay` (`_transition` → `authorizing`, `safe_write.try_claim`); after: `safe_write.complete` + `payments._apply_authorization` |
| reauthorize | `PaymentWrite` claim `sending` | new authorization id/status/expiry on `PayPalPayment`, `reauthorized_at`; claim completed | before: `payments._reauthorize` (`_transition` → `reauthorizing`, `try_claim`); after: `safe_write.complete` + the DONE branch of `payments._reauthorize` |
| capture | `PayPalPayment` state `capturing` + claim `sending` | capture id/status/amount/fee/net; Oscar `Source.debit`; order → `Complete`; stock allocations consumed | before: `payments.fulfil` (`_transition` → `capturing`, `try_claim`); after: `safe_write.complete` + `payments._apply_capture` |
| void | `PayPalPayment` state `voiding` + claim `sending` | auth status VOIDED, state `voided`, void `Transaction`; order → `Cancelled`; stock allocations cancelled | before: `payments.cancel` (`_transition` → `voiding`, `try_claim`); after: `safe_write.complete` + the DONE branch of `payments.cancel` |
| refund | `PayPalRefund` (key, amount, status `sending`) + reserved amount + claim `sending` | refund id/status; `Source.refund`; `refunded_amount`; reservation released on failure | before: `payments.refund` (atomic reservation + claim + `PayPalRefund`); after: `safe_write.complete` + `payments._settle_refund` |
| vault create | claim `sending` (user, reference) | `Bankcard` (masked number, type, expiry, `partner_reference` = token id); claim done with token id | before: `safe_write.try_claim` in `cards.save_card`; after: `safe_write.complete` + `cards._record_card` |
| vault delete | claim `sending` (card now hidden & unusable) | `Bankcard` row deleted; claim done | before: `safe_write.try_claim` in `cards.delete_card`; after: `safe_write.complete` + `Bankcard` delete in `cards.delete_card` |

## Design notes (decisions)

- Order ids exposed as `orderId` = Oscar `Order.number`. Oscar `Pending` = awaiting payment;
  authorized → `Being processed`; fulfilled → `Complete`; cancelled → `Cancelled`.
- Currency: `Order.currency = settings.PAYPAL_CURRENCY`; amounts = catalogue prices via Oscar's
  strategy (sandbox stockrecords are GBP-denominated numbers; the configured currency is applied, per
  the task). Shipping = Oscar's shipping `Repository` default (sandbox: free).
- Stale authorization at fulfil: read the authorization first. Past `expiration_time`, or
  VOIDED/DENIED → 409 `authorization_expired`/`authorization_not_renewable` with operator guidance
  (cancel + ask shopper to pay again). Past the 3-day honor period and not yet reauthorized →
  reauthorize, then capture the new authorization. A reauth refusal → 409 with PayPal's issue text.
- Auth: Django session. `POST /api/login` (Django `authenticate` + `login`), `POST /api/logout`,
  `GET /api/csrf`. CSRF enforced on unsafe methods. Unauthenticated → 401, non-staff on operator
  routes → 403, other users' orders/cards → 404.
- Card data: never persisted (only last4/brand/expiry month in `Bankcard`), never logged
  (logging transport logs method/path/status only; views are `sensitive_variables`/
  `sensitive_post_parameters`).
- Reconciliation: staff; `from`/`to` ISO-8601 (tz-aware; naive = UTC); split into ≤31-day windows,
  walk every page; match provider `transaction_id` to our capture/refund ids; local side filtered on
  the provider's clock (stored `provider_time`); unsettled local writes listed separately;
  provider-only rows that carry our reference prefix flagged.

## Findings during implementation (sheet revised)

- `sandbox/settings.py` sets `ATOMIC_REQUESTS: True`: a claim inserted inside the request transaction would
  stay invisible to other requests until the view returned. All API views are wrapped in
  `transaction.non_atomic_requests` (`views.api`), so every claim commits before PayPal is called.
- SQLite raised `database is locked` for concurrent requests (deferred transaction upgrade). For the SQLite
  engine only, `sandbox/settings.py` sets `OPTIONS = {'timeout': 20, 'transaction_mode': 'IMMEDIATE'}`.
- PayPal error bodies do not always match the SDK's `Error` model (a `links` item without `rel`, seen on
  the vault's 404), so decoding raises `ValidationError` with no status. `paypal_client.LoggingTransport`
  records the last HTTP status per thread (`last_status()`); a 4xx whose body failed to decode is treated
  as a refusal, never as an unknown outcome (`safe_write.safe_write`, `errors.read`).
- A 403 carrying `PERMISSION_DENIED` is a permission refusal (mapped to 502 `paypal_permission_denied`
  with PayPal's reason), distinct from a 401 credentials failure (502 `paypal_auth_failed`).
- Sandbox note: once, a vault token returned 201 and then answered `TOKEN_NOT_FOUND` / an order with its
  `vault_id` answered 403 `PERMISSION_DENIED` seconds later; not reproducible on later runs.

## Assumptions & Blockers

- **No blockers.** Every capability is exposed by the SDK and was smoke-tested against the sandbox.
- Minor: production host is not named by the plugin → non-sandbox environments require
  `PAYPAL_BASE_URL` (fail fast otherwise).
- Minor: `PayPal-Request-Id` retention (6h orders / 45d payments / 3h vault) bounds how late an
  unknown outcome can be resolved by resend; after that, the claim stays `unknown` for an operator.
- Minor: reauthorization rules (honor period 3 days, 29-day period) taken from the
  `reauthorize_payment` docstring.

## REQUIRED READING

| Hazard | Pointer |
| --- | --- |
| client construction, lifetime, custom transport ownership | MUST load python-client-initialization ✅ |
| oauth2 omission silent; token-fetch failure payload | MUST load python-authentication ✅ |
| keyword-only tail, `prefer` default minimal, None-returning delete, status≠id | MUST load python-calling-endpoints ✅ |
| `Optional` ≠ `typing.Optional`, open enums, money as str, UNSET on responses | MUST load python-models ✅ |
| ApiError union, OAuthProviderError first, transport split, decode failures | MUST load python-error-handling ✅ |
| no retries, safe write, reconciliation on provider clock, timeout on custom transport | MUST load python-configuration-resilience ✅ |
| transport stub seam, token request first, two transport-failure inputs | MUST load python-testing ✅ |
