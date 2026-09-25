# PayPal integration plan — django-oscar sandbox (`sandbox/apps/payments`)

## Scope

A new Django app `sandbox/apps/payments` (label `sandbox_payments`) exposes a JSON API under `/api/`:

| Route | Who | What |
| --- | --- | --- |
| `POST /api/orders` | shopper | basket → Oscar `OrderCreator.place_order`, status `Awaiting payment` |
| `POST /api/orders/{orderId}/pay` | shopper (owner) | PayPal create order (`intent=AUTHORIZE`, card or vaulted card) → hold |
| `POST /api/orders/{orderId}/fulfil` | `is_staff` | reauthorize if stale → capture; records gross / fee / net |
| `POST /api/orders/{orderId}/cancel` | `is_staff` | void the authorization |
| `POST /api/orders/{orderId}/refunds` | shopper (owner) | refund the capture, full or partial, caller idempotency key |
| `GET /api/my-orders` | shopper | caller's orders + payment state |
| `GET /api/reconciliation?from=&to=` | `is_staff` | PayPal transaction search vs local records |
| `POST/GET /api/payment-methods`, `DELETE /api/payment-methods/{id}` | shopper | vault a card / list / delete |
| `GET /api/auth/csrf`, `POST /api/auth/login`, `POST /api/auth/logout` | anyone | Django session login (`django.contrib.auth.login`) for API callers |

## SDK identity (verified against the installed package — the skill's identity table has drifted)

| Fact | Value | Source |
| --- | --- | --- |
| Distribution | `paypal` 2.29 (NOT `pay-pal-server-sdk`; pip refused that name) | `pyproject.toml` in the clone, `pip show paypal` |
| Install | `pip install "paypal @ git+https://github.com/context-plugins/paypal-python-sdk.git@main"` | done in `venv/` |
| Import root | `paypal` (NOT `pay_pal_server_sdk`) | `venv/Lib/site-packages/paypal/__init__.py` |
| Client | `PaypalClient` (sync) — alias `Client`; async `AsyncPaypalClient` | `sdk-map.md` "Getting a client" |
| Map | clone at `tmp/paypal-python-sdk/sdk-map.md`, `map/operations/*.md` | |

## Decisions

- **Sync vs async**: Django under WSGI, sync views → **`PaypalClient`** (sync). One lazily-built module-level client per process (`get_client()`), built after fork on first use, closed via `atexit`.
- **Host**: `PAYPAL_BASE_URL` if set, used verbatim (token fetch moves with it — `sdk-map.md` Servers & auth). Else `PAYPAL_ENVIRONMENT` resolved through an explicit map; the plugin names only `https://api-m.sandbox.paypal.com`, so `sandbox` → that URL and **any other environment without `PAYPAL_BASE_URL` raises `ImproperlyConfigured`** (no live URL from memory). Omitting `base_url` would silently select sandbox — we always pass it.
- **Auth**: `oauth2=ClientCredentials(client_id=settings.PAYPAL_CLIENT_ID, client_secret=settings.PAYPAL_CLIENT_SECRET)`; empty id/secret → `ImproperlyConfigured` at client build (a missing keyword would silently send unauthenticated).
- **Timeout**: 20 s via our own `HttpxClient(timeout=20.0)` wrapped by a logging transport (method, path, status, ms — never headers/bodies).
- **Retries**: the SDK does none. We add **none automatically**; a caller repeat is the retry, and it goes through the safe write under the SAME reference.
- **Response mode**: parsed calls everywhere (raise `ApiError`), except `vault.delete_payment_token` (returns `None`) which uses `with_raw_response` to see the 204.
- **`prefer="return=representation"`** on every write (default `return=minimal` omits amounts/breakdown).
- **Money**: `Decimal`, formatted per currency exponent (ISO 4217 table from `python-models`), compared as `Decimal`.
- **Currency**: order currency = `settings.PAYPAL_CURRENCY`; amounts are the Oscar catalogue prices (sandbox stock records are GBP-priced; the numeric price is used as-is in the configured currency — per task statement).
- **Reuse Oscar models**: `order.Order/Line` via `OrderCreator`, `basket.Basket` + strategy + offer `Applicator`, `payment.SourceType/Source/Transaction` (Authorise/Debit/Refund), `order.PaymentEvent` via `EventHandler`, stock via `EventHandler.consume/cancel_stock_allocations`. New tables hold only provider state: `OrderPayment`, `PaymentRefund`, `SavedCard`, `PayPalCustomer`, `ProviderWrite` (claim store).
- **Oscar status pipeline** (settings): add `Awaiting payment` → (`Payment authorised`, `Cancelled`); `Payment authorised` → (`Complete`, `Cancelled`, `Awaiting payment`).
- **References**: `deterministic_ref(*parts)` = `f"{install_prefix()}-{parts…}"`, sent as `PayPal-Request-Id`. `install_prefix()` is `PAYPAL_REQUEST_PREFIX` when set, else a random `osc…` prefix generated once and stored (`InstallIdentity`) — revised after the live run hit `DUPLICATE_INVOICE_ID`: the sandbox PayPal account is shared with other installs, so order numbers alone collide. The same prefix qualifies `custom_id` (`{prefix}-{number}`) and `invoice_id` (`{prefix}-{number}-{attempt}`). Caller refund keys and card-save keys: `[A-Za-z0-9_-]{1,64}`. For the authorize step the recorded provider id is the **authorization** id (what holds the money, and what transaction search reports); the PayPal order id is kept on `OrderPayment`.
- **Honor period / renewal**: from the `reauthorize_payment` docstring — honor period 3 days; reauthorize allowed after it, within 29 days of the original authorization; ≥ 30 days needs a new authorization. Fulfil: auth older than 3 days → reauthorize first; ≥ 29 days, or PayPal refuses the reauthorization, or status not `CREATED` → 409 with an operator-actionable message and the payment moves to `authorization_expired` so the shopper can `POST /pay` again (new attempt).
- **3-D Secure**: a `PAYER_ACTION_REQUIRED` order status is recorded failed with a message; no approval round-trip is built (observed not to happen on the sandbox test card: single-step create returned `COMPLETED` with an authorization).
- **Reconciliation**: split `[from,to)` into ≤ 31-day windows (end_date doc), page every window until `page >= total_pages`, `balance_affecting_records_only="N"` (authorizations/voids are not balance-affecting), `fields="transaction_info"`; narrow back to `[from,to)` on `transaction_initiation_date`; local side filtered on the stored **provider** time; matched against the *set* of PayPal ids an order owns (order id, authorization(s), capture, refunds) plus `invoice_id`/`custom_field`.

## Contract sheet (all from `sdk-map.md` + `map/operations/*.md` + model/enum modules; no open lookups)

Shared: every operation also exists as `client.<group>.with_raw_response.<op>` returning `ApiResult` (`Success`/`Failure`); async twins not used. Everything after `*` is keyword-only with a real default — no defensive `None`s. Trailing `request_options` on all. Headers: `pay_pal_request_id` → `PayPal-Request-Id`, `prefer` → `Prefer` (default `"return=minimal"`).

| Operation | Signature (positional \| keyword-only used) | Returns | `ApiError.error` | Request-id retention (docstring) |
| --- | --- | --- | --- | --- |
| `orders.create_order` | `(body: OrderRequest\|OrderRequestDict, *, pay_pal_request_id, prefer)` | `Order` | `CreateOrderErrorBody = Error [400,401,422] \| RawError` | 6 h; "mandatory for all single-step create order calls" |
| `orders.authorize_order` | `(id, *, pay_pal_request_id, prefer, body=None)` | `OrderAuthorizeResponse` | `Error [400,401,403,404,422,500] \| RawError` | 6 h |
| `orders.get_order` | `(id)` | `Order` | `Error [401,404] \| RawError` | read |
| `payments.get_authorized_payment` | `(authorization_id)` | `PaymentAuthorization` | `Error [401,403,404] \| RawError[500,…]` | read |
| `payments.reauthorize_payment` | `(authorization_id, *, pay_pal_request_id, prefer, body: ReauthorizeRequest\|Dict = None)` | `PaymentAuthorization` | `Error [400,401,403,404,422] \| RawError[500,…]` | 45 d |
| `payments.capture_authorized_payment` | `(authorization_id, *, pay_pal_request_id, prefer, body: CaptureRequest\|Dict = None)` | `CapturedPayment` | `Error [400,401,403,404,409,422] \| RawError[500,…]` | 45 d |
| `payments.get_captured_payment` | `(capture_id)` | `CapturedPayment` | `Error [401,403,404] \| RawError[500,…]` | read |
| `payments.void_payment` | `(authorization_id, *, pay_pal_request_id, prefer)` | `PaymentAuthorization` | `Error [401,403,404,409,422] \| RawError[500,…]` | 45 d |
| `payments.refund_captured_payment` | `(capture_id, *, pay_pal_request_id, prefer, body: RefundRequest\|Dict = None)` | `Refund` | `Error [400,401,403,404,409,422] \| RawError[500,…]` | 45 d |
| `payments.get_refund` | `(refund_id)` | `Refund` | `Error [401,403,404] \| RawError[500,…]` | read |
| `vault.create_payment_token` | `(body: PaymentTokenRequest\|Dict, *, pay_pal_request_id)` | `PaymentTokenResponse` | `Error [400,403,404,422,500] \| RawError` | 3 h |
| `vault.get_payment_token` | `(id)` | `PaymentTokenResponse` | `Error [403,404,422,500] \| RawError` | read |
| `vault.delete_payment_token` | `(id)` | **`None`** → use `with_raw_response` (`ApiResult[None, DeletePaymentTokenErrorBody]`) | `Error [400,403,500] \| RawError` | none (DELETE by id — harmless to repeat) |
| `transaction_search.search_transactions` | `(start_date: str, end_date: str, *, fields="transaction_info", balance_affecting_records_only="Y", page_size=100, page=1, transaction_currency=None)` | `SearchResponse` | **Case B: always `RawError`** | read |

Failed token fetch: `ApiError` with `.error` `OAuthProviderError | RawError` (both from `paypal.core`), surfaces from the operation call, also in raw mode. Decode failure: `pydantic.ValidationError`/`ValueError`, both modes. Transport: `httpx` exceptions unwrapped. Imports: `from paypal import PaypalClient`; `from paypal.core import ApiError, RawError, ClientCredentials, OAuthProviderError, HttpxClient, HttpRequest, HttpResponse, UNSET, UnsetType, Success, Failure`; `from paypal.models import Error, …`; enums from `paypal.models.enums`.

### Model members set / read (`Optional[T]` = `T | UnsetType`; never pass `None`)

| Model (module) | Members (required **bold**) — wire name = python name unless noted |
| --- | --- |
| `OrderRequest` (`models/order_request.py`) | **`intent`**: `CheckoutPaymentIntentOrStr` (`AUTHORIZE`) · **`purchase_units`**: `list[PurchaseUnitRequest]` · `payment_source`: `PaymentSource` |
| `PurchaseUnitRequest` | **`amount`**: `AmountWithBreakdown` · `reference_id`, `custom_id` (= Oscar order number), `invoice_id` (= `{number}-{attempt}`), `description` : `str` |
| `AmountWithBreakdown` / `Money` | **`currency_code`**: `str` · **`value`**: `str` |
| `PaymentSource` | `card`: `CardRequest` |
| `CardRequest` | `name`, `number`, `expiry` (`YYYY-MM`), `security_code`, `vault_id`: `str` · `billing_address`: `Address` |
| `PaymentTokenRequest` | **`payment_source`**: `PaymentTokenRequestPaymentSource` (`card`: `PaymentTokenRequestCard` = `name, number, expiry, security_code, billing_address`) · `customer`: `Customer` (`id` — PayPal-generated vault customer id) |
| `PaymentTokenResponse` | `id` · `customer.id` · `payment_source.card` (`CardPaymentTokenEntity`: `last_digits`, `brand`, `expiry`, `name`) — **no status member** |
| `Order` / `OrderAuthorizeResponse` | `id` · `status`: `OrderStatusOrStr` · `purchase_units[0].payments.authorizations[0]` (`AuthorizationWithAdditionalData`: `id`, `status`, `amount`, `expiration_time`, `create_time`) · `payment_source.card` (`CardResponse`: `last_digits`, `brand`) |
| `PaymentAuthorization` | `id`, `status`: `AuthorizationStatusOrStr`, `status_details.reason`, `amount`, `expiration_time`, `create_time`, `update_time` |
| `CaptureRequest` | `amount`: `Money` · `final_capture`: `bool` · `invoice_id` |
| `CapturedPayment` | `id`, `status`: `CaptureStatusOrStr`, `status_details.reason`, `amount`, `seller_receivable_breakdown` (**`gross_amount`**, `paypal_fee`, `net_amount`), `create_time`, `update_time` |
| `ReauthorizeRequest` | `amount`: `Money` |
| `RefundRequest` | `amount`: `Money` · `invoice_id` · `custom_id` · `note_to_payer` |
| `Refund` | `id`, `status`: `RefundStatusOrStr`, `status_details.reason`, `amount`, `create_time`, `update_time` |
| `SearchResponse` | `transaction_details`: `list[TransactionDetails]` (`transaction_info`: `TransactionInformation`) · `page`, `total_pages`, `total_items`, `last_refreshed_datetime` |
| `TransactionInformation` | `transaction_id`, `paypal_reference_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_amount`, `fee_amount`, `transaction_status` (`D`/`P`/`S`/`V`), `invoice_id`, `custom_field` |
| `Error` (`models/error.py`) | **`name`**, **`message`**, **`debug_id`**, `details`: `list[ErrorDetails]` (**`issue`**, `description`) |

Truncated-2xx guard: only `SellerReceivableBreakdown.gross_amount` is required on the 2xx paths used; everything else decodes as `UNSET`, so every write asserts `id` and `status` are set and treats a missing one as **unknown**.

### Enum members (`paypal/models/enums/*.py`) → outcome (`status_from_provider`)

| Enum | done | pending (not-yet) | failed | anything unlisted / absent |
| --- | --- | --- | --- | --- |
| `OrderStatus` (create_order) | `COMPLETED` (single-step: authorization embedded) | `CREATED`, `SAVED`, `APPROVED` | `VOIDED`, `PAYER_ACTION_REQUIRED` (3DS – not supported) | unknown |
| `AuthorizationStatus` (authorize / reauthorize) | `CREATED` | `PENDING` | `DENIED`, `VOIDED`, `CAPTURED`, `PARTIALLY_CAPTURED` (hold no longer available to take) | unknown (e.g. `EXPIRED` arrives as `str`) |
| `AuthorizationStatus` (void — the undo is its done) | `VOIDED` | `PENDING`, `CREATED` | `CAPTURED`, `PARTIALLY_CAPTURED`, `DENIED` | unknown |
| `CaptureStatus` (capture) | `COMPLETED` | `PENDING` | `DECLINED`, `FAILED`, `REFUNDED`, `PARTIALLY_REFUNDED` (undone, fully or in part) | unknown |
| `RefundStatus` (refund — the undo is its done) | `COMPLETED` | `PENDING` | `FAILED`, `CANCELLED` | unknown |
| vault token (no status member) | `id` present **and** `payment_source.card` present | — | — | unknown (no id) |

## OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| create order + authorize (`orders.create_order`) | `Order.status`, then `purchase_units[0].payments.authorizations[0].status` | Order `COMPLETED` → read the embedded authorization: `CREATED` → done: payment `authorized`, Oscar order → `Payment authorised`, Source.allocate · auth `PENDING` → payment `auth_pending` (202; next `/pay` re-reads via `get_authorized_payment`) · auth `DENIED`/`VOIDED`/`CAPTURED`/`PARTIALLY_CAPTURED` → `auth_failed` (402, shopper may retry → new attempt) · Order `CREATED`/`SAVED`/`APPROVED` → pending: `auth_pending` 202 (no authorization yet; a later `/pay` resends the same reference) · `PAYER_ACTION_REQUIRED` → failed, 402 "card requires 3-D Secure browser approval — not supported" · `VOIDED` → failed · unlisted / absent / missing auth → unknown: `auth_unknown`, 504 | `outcomes.order_outcome` + `outcomes.authorization_outcome` via `payflow._read_order`; applied by `payflow._apply_order_result` → `payflow._finish_authorization`; APPROVED follow-up `payflow._authorize_approved`; pending re-read `payflow._refresh_authorization` |
| reauthorize (`payments.reauthorize_payment`) | `PaymentAuthorization.status` | `CREATED` → done: new authorization id replaces the old on the payment, capture proceeds against it · `PENDING` → pending: new authorization id recorded, payment `auth_pending`, fulfil answers 202; the next fulfil re-reads it via `get_authorized_payment` · `DENIED`/`VOIDED`/`CAPTURED`/`PARTIALLY_CAPTURED` → failed: payment `authorization_expired`, 409 operator message · unlisted/absent → unknown: `capture_unknown`, 504 | `outcomes.authorization_outcome` via `fulfilment._read_authorization`; applied by `fulfilment._apply_reauthorization`; branches in `fulfilment._renew_if_stale` (FAILED/refused → `fulfilment._expire`) |
| capture (`payments.capture_authorized_payment`) | `CapturedPayment.status` | `COMPLETED` → done: payment `captured`, capture id/amount/fee/net stored, Oscar order → `Complete`, Source.debit, stock consumed · `PENDING` → `capture_pending` 202, next fulfil re-reads via `get_captured_payment` · `DECLINED`/`FAILED` → `capture_failed` 402 · `REFUNDED`/`PARTIALLY_REFUNDED` → failed (undone) → `needs_review` 409 · unlisted/absent → `capture_unknown` 504 | `outcomes.capture_outcome` via `fulfilment._read_capture`; applied by `fulfilment._finish_capture`; pending re-read `fulfilment._refresh_capture` |
| void (`payments.void_payment`) | `PaymentAuthorization.status` | `VOIDED` → done: payment `voided`, Oscar order → `Cancelled`, stock allocation cancelled · `PENDING`/`CREATED` → pending: `void_pending` 202 · `CAPTURED`/`PARTIALLY_CAPTURED` → failed: 409 "already captured — refund instead" · `DENIED` → failed 409 · unlisted/absent → `void_unknown` 504 | `outcomes.void_outcome` via `fulfilment._read_void`; applied by `fulfilment._finish_void`; pending re-read `fulfilment._refresh_void` |
| refund (`payments.refund_captured_payment`) | `Refund.status` | `COMPLETED` → done: refund `done`, `refunded_amount += amount`, Source.refund, 201 · `PENDING` → refund `pending`, reservation kept, 202 · `FAILED`/`CANCELLED` → failed: reservation released, 402 · unlisted/absent → unknown: reservation kept, 504 | `outcomes.refund_outcome` via `fulfilment._read_refund`; applied by `fulfilment._finish_refund` (books `refunded_amount` / releases reservation); pending re-read `fulfilment._refresh_refund` |
| save card (`vault.create_payment_token`) | none on `PaymentTokenResponse` (no status member) | `id` + `payment_source.card` present → done: card `active`, 201 · missing id → unknown: card `unknown`, 504 (resend under same key settles it) | `cards._read_token` (id + `payment_source.card` → done, else unknown); applied by `cards._finish` |

## DUPLICATE CLAIMS

Store: the app's database (SQLite here, any Django DB) — `ProviderWrite.ref` has a **UNIQUE constraint**; `try_claim` is an INSERT that fails with `IntegrityError` for every second claimant (released `failed`-without-provider-id claims are re-taken by a single conditional `UPDATE … WHERE outcome='failed' AND provider_id=''`). Coarse per-order state changes are conditional `UPDATE … WHERE state IN (…)` (compare-and-set), never read-then-write.

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| create order (authorize) | `ProviderWrite` row `ref = {prefix}-ord-{number}-a{attempt}-create`; plus `OrderPayment.state` CAS `awaiting_payment/auth_failed/authorization_expired → authorizing` | UNIQUE(`ref`); CAS update matching 0 rows | `IntegrityError` in claim insert; 0-row CAS → answer from stored state (202 in progress / 200 authorized) | `safe_write.try_claim` (INSERT on UNIQUE `ProviderWrite.ref`) inside `safe_write.safe_write`, called from `payflow._authorize`; order-level CAS `payflow._cas` in `payflow.pay` |
| reauthorize | `ProviderWrite` `ref = {prefix}-auth-{authorization_id}-reauth`; `OrderPayment.state` CAS `authorized → capturing` | UNIQUE(`ref`); CAS | same | `safe_write.try_claim` via `safe_write.safe_write` in `fulfilment._renew_if_stale`; CAS `payflow._cas` in `fulfilment.fulfil` |
| capture | `ProviderWrite` `ref = {prefix}-auth-{authorization_id}-capture`; `OrderPayment.state` CAS `authorized → capturing` | UNIQUE(`ref`); CAS | same | `safe_write.try_claim` via `safe_write.safe_write` in `fulfilment._capture`; CAS `payflow._cas` in `fulfilment.fulfil` |
| void | `ProviderWrite` `ref = {prefix}-auth-{authorization_id}-void`; CAS `authorized → voiding` | UNIQUE(`ref`); CAS | same | `safe_write.try_claim` via `safe_write.safe_write` in `fulfilment.cancel`; CAS `payflow._cas` in `fulfilment.cancel` |
| refund | `PaymentRefund` UNIQUE(`payment`, `idempotency_key`) + `ProviderWrite` `ref = {prefix}-ord-{number}-refund-{key}`; amount reserved by conditional `UPDATE … SET refund_reserved = refund_reserved + x WHERE refund_reserved + x <= captured_amount` | UNIQUE constraints; reservation UPDATE matching 0 rows (over-refund) | `IntegrityError` → answer the stored refund (409 if the amount differs); 0-row reservation → 409 "exceeds refundable" | `PaymentRefund` create + `fulfilment._reserve` (conditional UPDATE) in `fulfilment.refund` (IntegrityError → `fulfilment._repeat_refund`); provider claim in `fulfilment._send_refund` → `safe_write.safe_write` |
| save card | `SavedCard` UNIQUE(`request_ref`) + `ProviderWrite` `ref = {prefix}-usr-{user_id}-card-{Idempotency-Key}` (a request without a key gets a fresh key: a new key is a second card the caller meant) | UNIQUE constraints | `IntegrityError` → answer the stored card | `SavedCard` create on UNIQUE `request_ref` in `cards.save_card` (IntegrityError → stored card); provider claim `safe_write.safe_write` in `cards.save_card` |
| delete card | none needed — DELETE by id is harmless to repeat; local row soft-deleted first so it can no longer be used to pay | — | — | `cards.delete_card` (marks `DELETED` before calling `vault.with_raw_response.delete_payment_token`; repeat retries the provider delete) |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| create order (authorize) | same-reference resend (kind 2 — PayPal stores `PayPal-Request-Id` 6 h) on the next `/pay`; beyond 6 h no resend — stays `auth_unknown` for an operator | `{prefix}-ord-{number}-a{attempt}-create` | `safe_write.safe_write` `checking` path (resend under the same `ref`, refused beyond `ORDERS_KEY_RETENTION`), reached from `payflow.pay` → `payflow._authorize` for `auth_unknown` / stale `authorizing` |
| reauthorize | same-reference resend (45 d) on the next `/fulfil` | `{prefix}-auth-{authorization_id}-reauth` | `safe_write.safe_write` `checking` path (`PAYMENTS_KEY_RETENTION`), reached from `fulfilment.fulfil` → `fulfilment._renew_if_stale` for `capture_unknown` / stale `capturing` |
| capture | same-reference resend (45 d) on the next `/fulfil` | `{prefix}-auth-{authorization_id}-capture` | `safe_write.safe_write` `checking` path (`PAYMENTS_KEY_RETENTION`), reached from `fulfilment.fulfil` → `fulfilment._capture` |
| void | same-reference resend (45 d) on the next `/cancel` | `{prefix}-auth-{authorization_id}-void` | `safe_write.safe_write` `checking` path (`PAYMENTS_KEY_RETENTION`), reached from `fulfilment.cancel` for `void_unknown` / stale `voiding` |
| refund | same-reference resend (45 d) on a repeat of the request with the same caller key | `{prefix}-ord-{number}-refund-{key}` | `safe_write.safe_write` `checking` path (`PAYMENTS_KEY_RETENTION`), reached from `fulfilment._repeat_refund` → `fulfilment._send_refund` for `sending`/`unknown` refunds |
| save card | same-reference resend (3 h) on a repeat with the same `Idempotency-Key`; beyond 3 h stays `unknown` (hidden from list, unusable) | `{prefix}-usr-{user_id}-card-{key}` | `safe_write.safe_write` `checking` path (`VAULT_KEY_RETENTION`), reached from `cards.save_card` on a repeat with the same key |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| create order (authorize) | Oscar `Order` (Awaiting payment) + `OrderPayment` (state `authorizing`, `attempt`, `amount`) + `ProviderWrite` (`sending`, ref) | `ProviderWrite` outcome/provider id/status/provider time; `OrderPayment` PayPal order id, authorization id/status/expiry/time, card brand+last4; Oscar status + Source/Transaction(Authorise) + PaymentEvent | `ordering.place_order` (Order + `OrderPayment`), CAS in `payflow.pay`, claim in `safe_write.try_claim`; after: `safe_write.complete` + `payflow._apply_order_result` in one transaction (`safe_write.safe_write`) |
| reauthorize | `OrderPayment` (`capturing`) + `ProviderWrite` (`sending`) | new authorization id/status/time on `OrderPayment`; `ProviderWrite` completed | CAS in `fulfilment.fulfil`, claim `safe_write.try_claim`; after: `safe_write.complete` + `fulfilment._apply_reauthorization` |
| capture | `OrderPayment` (`capturing`) + `ProviderWrite` (`sending`) | capture id/status/amount/fee/net/time; Oscar `Complete`, Source.debit, PaymentEvent, stock consumed | CAS in `fulfilment.fulfil`, claim `safe_write.try_claim`; after: `safe_write.complete` + `fulfilment._finish_capture` |
| void | `OrderPayment` (`voiding`) + `ProviderWrite` (`sending`) | void status/time; Oscar `Cancelled`, stock allocation cancelled, Transaction(Void) | CAS in `fulfilment.cancel`, claim `safe_write.try_claim`; after: `safe_write.complete` + `fulfilment._finish_void` |
| refund | `PaymentRefund` (`sending`, key, amount) + reservation on `OrderPayment.refund_reserved` + `ProviderWrite` (`sending`) | PayPal refund id/status/time; `refunded_amount`; Source.refund; reservation released on failure | `fulfilment.refund` (row + `fulfilment._reserve`), claim `safe_write.try_claim`; after: `safe_write.complete` + `fulfilment._finish_refund` |
| save card | `SavedCard` (`sending`, request_ref, owner) + `ProviderWrite` (`sending`) | token id, brand, last digits, expiry, vault customer id (`PayPalCustomer`) | `cards.save_card` (row), claim `safe_write.try_claim`; after: `safe_write.complete` + `cards._finish` |

## Error boundary

One ladder (`translate` in the app) around every SDK call site, reads included, order: `OAuthProviderError` or 401/403 → 502 config · 429 → 503 · provider 4xx with `Error` body → 422/409 with PayPal's `message` + `details[].issue` · other 4xx → 422 · 5xx → 502 `outcome_unknown=True` · `ValidationError`/`ValueError` on 2xx → 502 unreadable, outcome unknown · `ConnectError/ConnectTimeout/PoolTimeout/ProxyError` → 502 never sent · other `httpx.RequestError` → 504 outcome unknown. `str(e)` never returned to callers; card data never logged or stored.

## Tests

`sandbox/apps/payments/tests.py` — Django `TestCase`, run from `sandbox/` with the sandbox's own runner: `python manage.py test apps.payments` (34 tests). The repo's top-level `tests/` suite needs PostgreSQL and is not affected. Fake the transport (`custom_http_client=StubTransport`, token response queued first). Cover: status mapping (refused + unlisted), same operation twice → one provider call with the derived `PayPal-Request-Id`, ConnectError vs ReadTimeout outcome split, over-refund refusal, ownership isolation, staff-only operator routes, amount mismatch → `needs_review`.

## Assumptions & Blockers

- (minor) `PayPal-Request-Id` de-duplicates within the documented retention ("The server stores keys for N") — treated as kind-2 idempotency (same key returns the original result). No separate "duplicate" error body is documented, so none is special-cased.
- (minor) No live base URL in the plugin → non-sandbox environments require `PAYPAL_BASE_URL`.
- (minor) Refunds are shopper-scoped per the task ("Fulfil, cancel and reconciliation are operator actions … Every other endpoint is shopper-scoped").
- (minor) Setup note: on this machine the CSV catalogue import is **required** (`child_products.json` alone gives 11 products and `orders.json` then fails on a foreign key); with the three `books.*.csv` files imported the catalogue has 209 products.
- Blockers: none. Reconciliation, vaulting, direct card processing all answered on the sandbox smoke (search 200, vault create/list/delete 2xx, card create-order `COMPLETED` with authorization, reauthorize/capture/refund/void 2xx or documented 4xx).

## REQUIRED READING

- MUST load `python-error-handling` — the ladder above, `OAuthProviderError` first, never `str(e)` — loaded.
- MUST load `python-client-initialization` — sync client, one per process, close obligation, custom transport timeout — loaded.
- MUST load `python-configuration-resilience` — safe write (claim/call/check/verify/complete), no retries, reconciliation clocks — loaded.
- MUST load `python-testing` — stub transport seam, token request first, lowercase headers — loaded.
- MUST load `python-calling-endpoints` — `status_from_provider`, allow-list success, raw mode for `None` returns — loaded.
- MUST load `python-models` — `UNSET` vs `None`, open enums, money as `str` via `Decimal` — loaded.
- MUST load `python-authentication` — `oauth2=` keyword, lazy token fetch, secrets from settings — loaded.
