# PayPal Server SDK — integration plan & contract sheet

Scope: PayPal payments (authorize → capture → void / refund), saved cards (vault) and a
reconciliation report for the django-oscar **sandbox** site, exposed as JSON endpoints under `/api/`
by a new Django app `sandbox/apps/paypal_payments/`.

## Toolchain & environment (surveyed)

| Fact | Value |
| --- | --- |
| Interpreter / env | `py -3.11` venv at `venv/` (gitignored); `venv\Scripts\pip install -e .[test]` |
| SDK | `pay-pal-server-sdk` 2.29, installed from `git+file:///D:/APIMatic/sdk-regen/paypal-python-sdk.git@main`; import root `pay_pal_server_sdk`; map read from a scratch clone of the same branch (version 2.29 matches) |
| Host app | Django (WSGI, sync views), SQLite by default, `ATOMIC_REQUESTS=True` |
| Sync vs async | **sync** — `PayPalServerSdkClient`; `close()` at interpreter exit (`atexit`) |
| Tests | Django `TestCase` in `sandbox/apps/paypal_payments/tests/`, run with `venv\Scripts\python sandbox\manage.py test apps.paypal_payments` (repo's own pytest suite is `tests/` with `tests.settings`; sandbox had no tests) |
| Type check | none configured → `mypy --strict --ignore-missing-imports` on the new app's SDK-facing modules (Django is untyped here) |
| Baseline | untouched tree has no sandbox tests; the Oscar suite under `tests/` is not affected by this additive app |
| Credentials | `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENVIRONMENT` (=`sandbox`), `PAYPAL_CURRENCY` (=`USD`) present; `PAYPAL_BASE_URL` unset. All read in `sandbox/settings.py` via `env(...)`, values never written anywhere |
| Base URL | `PAYPAL_BASE_URL` if set (verbatim, token fetch included — the SDK derives `/v1/oauth2/token` from `base_url`); else `{"sandbox": "https://api-m.sandbox.paypal.com"}[PAYPAL_ENVIRONMENT]`. The SDK declares only the sandbox host; any other environment **requires** `PAYPAL_BASE_URL` → `ImproperlyConfigured` otherwise (never a silent default) |
| Ports | `APP_PORT_BLOCK_BASE`=39360, size 20 |

Repo conventions (pattern → exemplar to imitate):
- Oscar model loading via `get_model`/`get_class` → `src/oscar/apps/order/utils.py`
- Order placement through `OrderCreator.place_order` + `OrderTotalCalculator` + `OrderNumberGenerator` → `src/oscar/apps/checkout/mixins.py`
- Oscar payment bookkeeping via `payment.Source` / `SourceType` / `Transaction` (`allocate`/`debit`/`refund`) → `src/oscar/apps/payment/abstract_models.py`
- sandbox app layout → `sandbox/apps/` (plain package, added to `INSTALLED_APPS` as `apps.<name>`); URLs appended in `sandbox/urls.py`

## Read-only smoke results (scratch, real sandbox credential)

| Operation | Observed |
| --- | --- |
| token fetch | OK |
| `orders.create_order` (intent AUTHORIZE, `payment_source.card`) | 200, order `COMPLETED`, `purchase_units[0].payments.authorizations[0].status = CREATED`; **no payer action / 3-DS challenge** |
| same call, same `PayPal-Request-Id` | **422 `TRANSACTION_REFUSED`** — NOT a replay of the original |
| same `invoice_id`, new request id | **422 `DUPLICATE_INVOICE_ID`** (account enforces unique invoice ids) |
| `orders.create_order` with `card.vault_id` | 200 COMPLETED, authorization CREATED, `payment_source.card.last_digits` echoed |
| `payments.void_payment` | 200 VOIDED; replay same key → VOIDED again; new key → 422 `PREVIOUSLY_VOIDED`; after capture → 422 `PREVIOUSLY_CAPTURED` |
| `payments.reauthorize_payment` on a fresh auth | 422 `REAUTHORIZATION_TOO_SOON` (day 4–29 only) |
| `payments.capture_authorized_payment` | 200 COMPLETED with `seller_receivable_breakdown` (gross/paypal_fee/net); replay same key → same capture; new key → 422 `AUTHORIZATION_ALREADY_CAPTURED` |
| `payments.refund_captured_payment` | 200 COMPLETED; replay same key → same refund; over-refund → 422 `REFUND_AMOUNT_EXCEEDED`; capture becomes `REFUNDED` after full refund; a concurrent second refund on the same capture → `PREVIOUS_REQUEST_IN_PROGRESS` |
| `vault.create_payment_token` (card) | 200, id + `customer.id` + `payment_source.card{brand,last_digits,expiry}`; replay same key → same token |
| `vault.delete_payment_token` (raw) | `Success` 204; repeat → 204 again |
| `vault.get_payment_token` on deleted token | 404 whose body **fails to decode** as `Error` → `pydantic.ValidationError` (not used by this integration) |
| `transaction_search.search_transactions` | 200; `page_size` ≤ 500 (1000 → 400); shared sandbox account holds thousands of foreign transactions; `last_refreshed_datetime` lags ~2 h. T1300 = authorization (`transaction_id` = authorization id), T0005 = capture (`paypal_reference_id` = authorization id), T1107 = refund (`paypal_reference_id` = capture id); `invoice_id` inherited by capture/refund |

## Contract sheet

All operations: sync parsed call raises `ApiError`; `.with_raw_response` returns `ApiResult`
(`Success`/`Failure`). The async twins are identical and unused. Every operation ends with keyword-only
`request_options`. Everything after `*` has a real default — no defensive `None`s. **The SDK performs
no retries**; this integration adds none on its own except the same-reference check inside the safe
write. `prefer` defaults to `"return=minimal"` on every write below — this integration always passes
`prefer="return=representation"` so status/amount/breakdown come back.

Client: `PayPalServerSdkClient(base_url=..., timeout=PAYPAL_TIMEOUT, oauth2=ClientCredentials(client_id=, client_secret=), custom_http_client=LoggingTransport(HttpxClient(timeout=PAYPAL_TIMEOUT)))`
— keyword-only; omitting `oauth2` = unauthenticated (we always set it); token fetched lazily; a failed
fetch raises `ApiError` whose `.error` is `OAuthProviderError | RawError` (checked first in the ladder);
imports: `pay_pal_server_sdk` (client), `pay_pal_server_sdk.core` (`ApiError`, `RawError`,
`ClientCredentials`, `OAuthProviderError`, `HttpxClient`, `HttpClient`, `HttpRequest`, `HttpResponse`,
`UNSET`, `UnsetType`), `pay_pal_server_sdk.models`, `pay_pal_server_sdk.models.enums`.

| Operation | Positional | Keyword-only used | Returns | `ApiError.error` union | Members asserted on |
| --- | --- | --- | --- | --- | --- |
| `client.orders.create_order` | `body: OrderRequest` | `pay_pal_request_id`, `prefer` | `Order` | `CreateOrderErrorBody` = `Error` [400,401,422] \| `RawError` | `id`, `status: OrderStatus`, `purchase_units[0].payments.authorizations[0].{id,status,amount.value,amount.currency_code,create_time,expiration_time}`, `payment_source.card.{brand,last_digits}` |
| `client.orders.authorize_order` | `id_` | `pay_pal_request_id`, `prefer` | `OrderAuthorizeResponse` | `AuthorizeOrderErrorBody` = `Error` [400,401,403,404,422,500] \| `RawError` | same authorization members (only if create answers `APPROVED` without an authorization) |
| `client.orders.get_order` | `id_` | — | `Order` | `GetOrderErrorBody` = `Error` [401,404] \| `RawError` | authorization members (lookup after an unknown `authorize_order`) |
| `client.payments.get_authorized_payment` | `authorization_id` | — | `PaymentAuthorization` | `Error` [401,403,404] \| `RawError` [500,…] | `id`, `status`, `amount`, `create_time`, `expiration_time` |
| `client.payments.reauthorize_payment` | `authorization_id` | `pay_pal_request_id`, `prefer`, `body: ReauthorizeRequest{amount: Money}` | `PaymentAuthorization` | `Error` [400,401,403,404,422] \| `RawError` [500,…] | `id` (the NEW authorization id), `status`, `amount`, `create_time`, `expiration_time` |
| `client.payments.capture_authorized_payment` | `authorization_id` | `pay_pal_request_id`, `prefer`, `body: CaptureRequest{amount: Money, final_capture: bool}` | `CapturedPayment` | `Error` [400,401,403,404,409,422] \| `RawError` [500,…] | `id`, `status: CaptureStatus`, `amount`, `seller_receivable_breakdown.{gross_amount (REQUIRED member), paypal_fee, net_amount}`, `create_time` |
| `client.payments.get_captured_payment` | `capture_id` | — | `CapturedPayment` | `Error` [401,403,404] \| `RawError` [500,…] | as capture |
| `client.payments.void_payment` | `authorization_id` | `pay_pal_request_id`, `prefer` | `PaymentAuthorization` | `Error` [401,403,404,409,422] \| `RawError` [500,…] | `id`, `status`, `update_time` |
| `client.payments.refund_captured_payment` | `capture_id` | `pay_pal_request_id`, `prefer`, `body: RefundRequest{amount: Money}` | `Refund` | `Error` [400,401,403,404,409,422] \| `RawError` [500,…] | `id`, `status: RefundStatus`, `amount`, `create_time` |
| `client.payments.get_refund` | `refund_id` | — | `Refund` | `Error` [401,403,404] \| `RawError` [500,…] | as refund |
| `client.vault.create_payment_token` | `body: PaymentTokenRequest` | `pay_pal_request_id` | `PaymentTokenResponse` | `Error` [400,403,404,422,500] \| `RawError` | `id`, `customer.id`, `payment_source.card.{brand,last_digits,expiry,verification_status}` |
| `client.vault.delete_payment_token` | `id_` | — | **`None`** → use `with_raw_response` (`ApiResult[None, DeletePaymentTokenErrorBody]`) | `Error` [400,403,500] \| `RawError` | `status_code` of the `Success`/`Failure` |
| `client.transaction_search.search_transactions` | `start_date: str`, `end_date: str` (RFC 3339, seconds required, range ≤ 31 days) | `fields="transaction_info"`, `balance_affecting_records_only="N"`, `page_size=500`, `page` | `SearchResponse` | **Case B**: always `RawError` | `transaction_details[].transaction_info.{transaction_id, paypal_reference_id, transaction_event_code, transaction_status, invoice_id, custom_field, transaction_amount, fee_amount, transaction_initiation_date}`, `total_pages`, `page`, `last_refreshed_datetime` |

Model members set (required vs `UNSET`; `Optional[T]` here is `T | UnsetType` — never pass `None`):

| Model | Members set | Notes |
| --- | --- | --- |
| `OrderRequest` | `intent` (required, `CheckoutPaymentIntent.AUTHORIZE`), `purchase_units` (required), `payment_source` | |
| `PurchaseUnitRequest` | `amount` (required `AmountWithBreakdown{currency_code, value}` both required str), `invoice_id` (**always set** = attempt reference; unique per merchant account), `custom_id` (= Oscar order number), `description` | money as `f"{Decimal:.2f}"` string |
| `PaymentSource.card: CardRequest` | one-off: `name`, `number`, `expiry` (`YYYY-MM`), `security_code`, `billing_address: Address` (`country_code` required); saved: `vault_id` only | |
| `PaymentTokenRequest` | `payment_source` (required `PaymentTokenRequestPaymentSource{card: PaymentTokenRequestCard{name, number, expiry, security_code, billing_address}}`), `customer: Customer{id}` once known | |
| `CaptureRequest` | `amount: Money`, `final_capture=True` | |
| `ReauthorizeRequest` | `amount: Money` | |
| `RefundRequest` | `amount: Money` (always explicit, also for a full refund) | |

Status enums (`pay_pal_server_sdk.models.enums`, all open `…OrStr`: an unknown string arrives as `str` → `unknown`):

| Step | Enum → outcome |
| --- | --- |
| authorize (create_order / authorize_order / reauthorize / lookup) — `AuthorizationStatus` | `CREATED` → done · `PENDING` → pending · `DENIED` → failed · `VOIDED` → failed (undone) · `CAPTURED`, `PARTIALLY_CAPTURED` → needs_review (not what this step asked) · anything else / absent → unknown. `OrderStatus.PAYER_ACTION_REQUIRED` with no authorization → failed with code `payer_action_required` (approval round-trip deliberately not built) |
| capture — `CaptureStatus` | `COMPLETED` → done · `PENDING` → pending · `DECLINED`, `FAILED` → failed · `REFUNDED`, `PARTIALLY_REFUNDED` → failed (done then undone) · else → unknown |
| void (cancel) — own mapper `cancel_outcome` over `AuthorizationStatus` | `VOIDED` → done · `DENIED` → done (nothing held) · `CAPTURED`, `PARTIALLY_CAPTURED` → failed (too late) · `CREATED`, `PENDING`, else → unknown |
| refund — `RefundStatus` | `COMPLETED` → done · `PENDING` → pending · `FAILED`, `CANCELLED` → failed · else → unknown |
| vault save — `PaymentTokenResponse` has **no status**; `CardVerificationStatus` on the card | `payment_source.card` present with `last_digits` and `verification_status` ∈ {UNSET, `VERIFIED`} → done · `FAILED` → failed · card/id absent → unknown |
| vault delete — raw `Success`/`Failure` status code | 200/204/404 → done (gone) · other 4xx → failed · 5xx/transport → unknown |

Error ladder (one place, `gateway.translate` / `gateway.translate_status`): `OAuthProviderError` or 401/403 → 502
`paypal_auth_failed`; 429 → 503; typed `Error` 4xx → the same 4xx with PayPal `details[0].issue` as code;
other 4xx → same 4xx generic; 5xx → 502 `outcome_unknown` on writes; `pydantic.ValidationError`/
`ValueError` → unreadable (outcome unknown on a write); `httpx.ConnectError | ConnectTimeout | PoolTimeout |
ProxyError` → never sent (502, known); other `httpx.RequestError` → may have landed (504, unknown).
`str(e)` is never returned to callers.

### OPERATION OUTCOMES

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/orders/{id}/pay` → `orders.create_order` (and `orders.authorize_order` when needed) | `purchase_units[0].payments.authorizations[0].status` (`AuthorizationStatus`), plus `Order.status` | CREATED → done → 200 `paymentState=authorized`; PENDING → pending → 202; DENIED/VOIDED → failed → 402 `payment_declined` (order stays payable); CAPTURED/PARTIALLY_CAPTURED → needs_review → 409; `PAYER_ACTION_REQUIRED` → failed 402 `payer_action_required`; absent/unlisted → unknown → 504 `outcome_unknown` | `services._authorize` / `services._authorize_created_order` → `gateway.read_authorization` + `gateway.authorization_outcome` → `services._complete_authorization` → `services.answer_status` (via `views._outcome_response`) |
| `POST /api/orders/{id}/fulfil` → `payments.reauthorize_payment` (stale auth only) | `PaymentAuthorization.status` | CREATED → done → continue to capture; PENDING → pending → 202; DENIED/VOIDED/4xx refusal → failed → capture attempted on the still-valid original, else 409 with operator instructions; unknown → 504 | `services._reauthorize` (`gateway.reauthorize`, `gateway.authorization_outcome`); refusal falls through to `services._capture` |
| `POST /api/orders/{id}/fulfil` → `payments.capture_authorized_payment` | `CapturedPayment.status` (`CaptureStatus`) | COMPLETED → done → 200 with captured amount / PayPal fee / net; PENDING → pending → 202; DECLINED/FAILED → failed → 409 `capture_failed`; REFUNDED/PARTIALLY_REFUNDED → failed → 409; expired authorization → 409 `authorization_expired` (operator guidance); absent/unlisted → unknown → 504 | `services._capture` → `gateway.read_capture` + `gateway.capture_outcome` → `services._complete_capture` (expiry via `services._authorization_no_longer_capturable`, `services._expired_message`) |
| `POST /api/orders/{id}/cancel` → `payments.void_payment` | `PaymentAuthorization.status` via `cancel_outcome` | VOIDED/DENIED → done → 200 `paymentState=voided`, order Cancelled; CAPTURED/PARTIALLY_CAPTURED → failed → 409 `already_captured` (refund instead); CREATED/PENDING/unlisted → unknown → 504 | `services.cancel` (`gateway.void`, `gateway.read_void`, `gateway.cancel_outcome`) |
| `POST /api/orders/{id}/refunds` → `payments.refund_captured_payment` | `Refund.status` (`RefundStatus`) | COMPLETED → done → 201 (200 on a repeat) with `refundId`; PENDING → pending → 202; FAILED/CANCELLED → failed → 409 (reservation released); unlisted/absent → unknown → 504 | `services._send_refund` → `gateway.read_refund` + `gateway.refund_outcome` → `services._complete_refund` |
| `POST /api/payment-methods` → `vault.create_payment_token` | `payment_source.card` (+ `verification_status`) — no status enum on this response | card with last digits & not FAILED → done → 201 with `paymentMethodId`; `verification_status=FAILED` → failed → 402 `card_verification_failed`; missing card/id → unknown → 504 | `services.save_card` (`gateway.vault_card`, `gateway.read_payment_token`, `gateway.vault_outcome`) |
| `DELETE /api/payment-methods/{id}` → `vault.delete_payment_token` (raw) | status code of the `Success`/`Failure` | 200/204/404 → done → 200 `deleted`; other 4xx → failed → card stays active, 4xx; 5xx/transport → unknown → 504 (card hidden and unusable, repeat DELETE re-checks) | `services.delete_card` (`gateway.delete_payment_token` raw peer, `gateway.read_delete`, `gateway.delete_outcome`) |

### DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| authorize (create_order + authorize_order step) | `OrderPayment.state` row in the app DB (SQLite/PostgreSQL) | conditional `UPDATE … SET state='authorizing' WHERE pk=… AND state IN (awaiting_payment, authorization_failed, authorization_expired)` — the DB applies it to exactly one caller (`rowcount` 0 for the other) | loser reloads the row and answers from it (in flight → 202, done → 200 replay, unknown/stale → check) | `services.pay` → `services._claim(OrderPayment, …, PAYABLE_STATES, AUTHORIZING)`; takeover of an unresolved claim `services._claim_check` |
| reauthorize (fulfil step 1) | same row, `state='capturing'` claim taken by fulfil | conditional `UPDATE … WHERE state IN ('authorized')` | loser answers from the row | `services.fulfil` → `services._claim(OrderPayment, …, (AUTHORIZED,), CAPTURING)`; `services._reauthorize` runs under it |
| capture (fulfil step 2) | same row / same fulfil claim | same conditional UPDATE | same | `services.fulfil` → same claim; `services._capture`; checks via `services._claim_check` |
| void (cancel) | same row, `state='voiding'` | conditional `UPDATE … WHERE state IN ('authorized','authorization_pending')` | loser answers from the row | `services.cancel` → `services._claim(OrderPayment, …, (AUTHORIZED, AUTHORIZATION_PENDING), VOIDING)` |
| refund | `PaymentRefund` row, `UNIQUE(payment, idempotency_key)` + conditional `UPDATE OrderPayment SET refund_reserved = refund_reserved + amt WHERE captured_amount >= refund_reserved + amt` | the unique constraint (`IntegrityError`) for the same key; the conditional UPDATE for an over-refund across different keys | `IntegrityError` → load existing refund and answer from it (409 if amount differs); rowcount 0 → 409 `refund_exceeds_captured` | `services._claim_new_refund` (`PaymentRefund` `UniqueConstraint paypal_refund_unique_key` + `services._reserve`), `services._reclaim_failed_refund`; `IntegrityError` answered in `services.refund` |
| vault save | `SavedCard` row, `UNIQUE(reference)`; reference = HMAC(SECRET_KEY, user + caller Idempotency-Key) or HMAC(SECRET_KEY, user + card + deletion count) | the unique constraint | `IntegrityError` → load existing and answer from it | `services.save_card` (`SavedCard.reference` unique, built by `services._card_reference`) |
| vault delete | `SavedCard.state` | conditional `UPDATE … SET state='deleting' WHERE state='active'` | loser answers from the row | `services.delete_card` → `services._claim(SavedCard, …, (ACTIVE,), DELETING)` / `services._claim_check` |

### UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| `orders.create_order` | same-reference resend when the caller re-submits the payment source (provider refuses a second one: `DUPLICATE_INVOICE_ID` / `TRANSACTION_REFUSED` on a resend = landed), then lookup: `transaction_search.search_transactions` over the claim window filtered by `invoice_id`, then `payments.get_authorized_payment(transaction_id)`; not found → stays unknown (reporting lags ≤ 3 h) | attempt reference (`invoice_id` = `PayPal-Request-Id`) | `gateway.perform(checking=…, repeat_is_safe=True, landed=…)` in `services._authorize`; lookup `gateway.find_authorization_by_invoice` |
| `orders.authorize_order` | lookup `orders.get_order(paypal_order_id)` → its authorization | PayPal order id recorded before the call | `services._authorize_created_order` (find = `gateway.get_order`) |
| `payments.reauthorize_payment` | same-reference resend (`PayPal-Request-Id`) | `<attempt ref>-reauthorize` | `services._reauthorize` (find = same-reference `gateway.reauthorize`) |
| `payments.capture_authorized_payment` | same-reference resend (observed: returns the original capture) — made once immediately after a lost answer and again on every repeat of the request | `<attempt ref>-capture` | `services._capture` (find = same-reference `gateway.capture`); pending: `services._recheck_capture` |
| `payments.void_payment` | lookup `payments.get_authorized_payment(authorization_id)` (the record's own id) | authorization id | `services.cancel` (find = `gateway.get_authorization`, `landed` on `PREVIOUSLY_VOIDED`) |
| `payments.refund_captured_payment` | same-reference resend (observed: returns the original refund); pending → `payments.get_refund(refund_id)` | `<payment ref>-refund-<local refund id>` | `services._send_refund` (find = same-reference `gateway.refund`); pending: `services._recheck_refund` |
| `vault.create_payment_token` | same-reference resend (observed: returns the same token; 3-hour key window) | `SavedCard.reference` | `services.save_card` (find = same-reference `gateway.vault_card` inside `VAULT_KEY_WINDOW`, else lookup-only → unknown) |
| `vault.delete_payment_token` | same-id resend (observed idempotent: 204 twice; 404 = gone) | PayPal token id | `services.delete_card` (find = `gateway.delete_payment_token` by token id) |

### WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| authorize | Oscar `Order` + `OrderPayment(state=authorizing, reference, attempt, claimed_at)` committed | PayPal order id, authorization id/status/amount/create & expiry time, card brand/last4, outcome state; Oscar `Source.allocate` + payment event + order status `Being processed` | `services.place_order` creates `OrderPayment`; claim committed in `services.pay`; recorded in `services._complete_authorization` (`services._source(...).allocate`, `services._payment_event`, `services._order_status`) |
| reauthorize | `OrderPayment(state=capturing, step=reauthorize)` | new authorization id/status/times, `reauthorized_at` | `services._reauthorize` (`services._save` of new authorization id/times) |
| capture | `OrderPayment(state=capturing, step=capture)` | capture id/status, captured amount, PayPal fee, net, provider time; Oscar `Source.debit`, stock consumed, order `Complete` | `services._complete_capture` (`Source.debit`, `EventHandler.consume_stock_allocations`, `services._order_status`) |
| void | `OrderPayment(state=voiding)` | authorization status, `voided_at`; Oscar `Transaction('Void')`, stock released, order `Cancelled` | `services.cancel` (`Source._create_transaction('Void', …)`, `EventHandler.cancel_stock_allocations`) |
| refund | `PaymentRefund(state=sending, key, amount)` + reservation on `OrderPayment.refund_reserved` | PayPal refund id/status/provider time; `refunded_amount`; Oscar `Source.refund`; reservation released on failure | `services._claim_new_refund` → `services._complete_refund` (`Source.refund`, `services._release` on failure) |
| vault save | `SavedCard(state=sending, reference)` (no card data) | PayPal token id, customer id (`PayPalCustomer`), brand/last4/expiry, state | `services.save_card` (row created `sending` before `gateway.vault_card`; `PayPalCustomer` recorded after) |
| vault delete | `SavedCard(state=deleting)` | `state=deleted`, `deleted_at` (or back to `active` on refusal, `delete_unknown` on a lost answer) | `services.delete_card` (`deleting` claim before `gateway.delete_payment_token`; `deleted`/`active`/`delete_unknown` after) |

## Design decisions (YOUR CALL — not SDK facts)

- Endpoints are plain Django JSON views (no DRF in the project). Session auth via Django's `login()`
  plus a `GET/POST/DELETE /api/session` helper; CSRF stays enforced (send `X-CSRFToken`).
- Views are `transaction.non_atomic_requests` so each claim commits before the provider call.
- Orders are placed through a dedicated Oscar `Basket` + `OrderCreator.place_order` (Free shipping, no
  address), so the existing order/line/stock-allocation model is reused. Amount = catalogue price via
  the request strategy; currency = `PAYPAL_CURRENCY`.
- Refunds are shopper-scoped (the brief lists only fulfil, cancel, reconciliation as operator actions).
- Reconciliation: split `[from, to)` into ≤ 31-day windows, page each to `total_pages` at 500/page,
  match by our invoice-id prefix and by known authorization/capture/refund ids; buckets: matched,
  PayPal-only, app-only, not-yet-reported (after `last_refreshed_datetime`), unsettled.

## Implementation notes (post-build)

- Error ladder lives in `gateway.translate` / `gateway.translate_status`; the call/check/verify core in
  `gateway.perform`; the HTTP status of every outcome in `services.answer_status`.
- A concurrent second refund on the same capture is refused by PayPal (`PREVIOUS_REQUEST_IN_PROGRESS`,
  observed live); it is a released failure, its reservation is returned, and the same key may retry.
- Logging: `client.LoggingTransport` (method, path, status, latency, PayPal debug id); `httpx`/`httpcore`
  pinned to WARNING and `apps.paypal_payments` named in `LOGGING` in `sandbox/settings.py`.
- Checks run: `mypy --strict` clean on `gateway.py`/`client.py`; `mypy --check-untyped-defs` clean on the
  app; 39 unit tests (`manage.py test apps.paypal_payments`) against a stub transport; live sandbox e2e.

## Assumptions & Blockers

- Minor: PayPal's live host is not declared by the SDK; production needs `PAYPAL_BASE_URL` set.
- Minor: `invoice_id` uniqueness is an account setting; the claim + request-id refusal still guard
  against duplicates if it were off.
- Minor: stale-authorization renewal (day 4–29) cannot be exercised live in one session; covered by
  unit tests with a fake transport.
- No blockers: the card flow completed without a payer-action challenge.

## REQUIRED READING

- Client construction, lifetime, close obligation → MUST load `python-client-initialization` (loaded)
- Credentials / token failure (`OAuthProviderError`) → MUST load `python-authentication` (loaded)
- Operation signatures, `prefer` default, `-> None` delete → MUST load `python-calling-endpoints` (loaded)
- `UNSET`, open enums, money strings → MUST load `python-models` (loaded)
- Error ladder (never-sent vs unknown, decode failures) → MUST load `python-error-handling` (loaded)
- Safe write, retries, reconciliation, logging transport → MUST load `python-configuration-resilience` (loaded)
- Stub transport tests → MUST load `python-testing` (loaded)
