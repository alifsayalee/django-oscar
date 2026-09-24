# PayPal integration plan — django-oscar sandbox

Scope: PayPal card payments (authorize → capture at fulfilment → void/refund), vaulted cards and a
reconciliation report, exposed as a JSON API on the sandbox project (`sandbox/`), in a new Django app
`sandbox/apps/paypal_payments/`, routed under `/api/`.

## SDK identity (verified against the installed package, not the skill snapshot)

| Fact | Value | Source |
| --- | --- | --- |
| Distribution / import root | `paypal` / `paypal` — **the skill snapshot says `pay-pal-server-sdk` / `pay_pal_server_sdk`; that is drift** | `sdk-map.md` header, `pyproject.toml` |
| Version | `2.29` | `sdk-map.md` |
| Install | `pip install "paypal @ git+https://github.com/context-plugins/paypal-python-sdk.git@main"` into `venv/` | python-getting-started |
| Client | `paypal.PaypalClient` (sync). Keyword-only: `base_url`, `timeout` (30.0), `custom_http_client`, `oauth2`, `oauth2_token_source` | `sdk-map.md` Getting a client |
| Auth | `oauth2=ClientCredentials(client_id=…, client_secret=…)` from `paypal.core`; omitting it = unauthenticated, silently | `sdk-map.md` Servers & auth |
| Server | one declared: `https://api-m.sandbox.paypal.com` (the default when `base_url` omitted). Token endpoint `/v1/oauth2/token` follows `base_url` | `sdk-map.md` Servers & auth |
| Retries | **none** in the SDK | python-configuration-resilience |
| Imports | client from `paypal`; `ApiError, RawError, Success, Failure, HttpxClient, HttpRequest, HttpResponse, OAuthProviderError, UNSET, UnsetType, ClientCredentials` from `paypal.core`; models from `paypal.models`; enums from `paypal.models.enums` | `sdk-map.md`, `paypal/core/__init__.py` `__all__` |

**Sync vs async:** sync `PaypalClient`. The host is Django under WSGI with sync views. One module-level
client, built lazily on first use (so it is built after a fork), held in `gateway.py`, closed at
`atexit`. Never built per request. The per-call `request_options` are not used; the client timeout is
set on the `HttpxClient` we pass in, because a custom transport ignores the client's `timeout=`.

**Keyword-only boundary:** each op's positional params are listed below; everything after `*` has a real
default. Do not pass defensive `None`s.

**`prefer` default is `"return=minimal"`** on create/authorize/capture/void/reauthorize/refund. We
always pass `prefer="return=representation"`, because a minimal body carries no amounts, breakdowns
or authorization data.

## Configuration (sandbox/settings.py)

| Setting | Read from | Behaviour |
| --- | --- | --- |
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | env, default `""` | a client is only built when both are non-empty; otherwise `ImproperlyConfigured` at first use → 503 |
| `PAYPAL_ENVIRONMENT` | env, default `"sandbox"` | explicit map: `sandbox` → the SDK's declared server. Any other value (e.g. `live`) **requires** `PAYPAL_BASE_URL`, because the plugin names no other host. Unknown value with no override → `ImproperlyConfigured` |
| `PAYPAL_CURRENCY` | env, default `"USD"` | currency of every order/amount; exponent from the ISO table (python-models) |
| `PAYPAL_BASE_URL` | env, optional | when set, passed verbatim as `base_url` (moves the token call too) |
| `PAYPAL_REFERENCE_PREFIX` | env, optional | install-unique prefix for every reference; default = a random token stored once in the DB (`IntegrationInstall`) |

## Contract sheet — operations in scope

Error unions: every op below is `Error | RawError` (typed `Error`: `name`, `message`, `debug_id`
required; `details: Optional[list[ErrorDetails]]`, `ErrorDetails.issue: str` required).
`search_transactions` has no typed arm: its `.error` is **always `RawError`**. A failed token fetch
raises `ApiError` whose `.error` is `OAuthProviderError | RawError`. We check that first and treat it
as a configuration fault (502).

| op | positional | keyword-only we set | returns | error arms (status) |
| --- | --- | --- | --- | --- |
| `orders.create_order` | `body: OrderRequest\|Dict` | `pay_pal_request_id` (mandatory for single-step card create; kept 6h), `prefer` | `Order` | `Error` [400,401,422] · `RawError` |
| `orders.authorize_order` | `id` | `pay_pal_request_id`, `prefer` | `OrderAuthorizeResponse` | `Error` [400,401,403,404,422,500] · `RawError` |
| `orders.get_order` | `id` | — | `Order` | `Error` [401,404] · `RawError` |
| `payments.get_authorized_payment` | `authorization_id` | — | `PaymentAuthorization` | `Error` [401,403,404] · `RawError` [500,…] |
| `payments.reauthorize_payment` | `authorization_id` | `pay_pal_request_id` (kept 45 days), `prefer`, `body: ReauthorizeRequest` (`amount: Optional[Money]`) | `PaymentAuthorization` | `Error` [400,401,403,404,422] · `RawError` [500,…] |
| `payments.capture_authorized_payment` | `authorization_id` | `pay_pal_request_id` (45 days), `prefer`, `body: CaptureRequest` (`amount`, `final_capture`, `invoice_id` all Optional) | `CapturedPayment` | `Error` [400,401,403,404,409,422] · `RawError` [500,…] |
| `payments.get_captured_payment` | `capture_id` | — | `CapturedPayment` | `Error` [401,403,404] · `RawError` |
| `payments.void_payment` | `authorization_id` | `pay_pal_request_id`, `prefer` | `PaymentAuthorization` | `Error` [401,403,404,409,422] · `RawError` [500,…] |
| `payments.refund_captured_payment` | `capture_id` | `pay_pal_request_id` (45 days), `prefer`, `body: RefundRequest` (`amount: Optional[Money]`, `custom_id`, `invoice_id`, `note_to_payer` Optional) | `Refund` | `Error` [400,401,403,404,409,422] · `RawError` [500,…] |
| `vault.create_payment_token` | `body: PaymentTokenRequest` | `pay_pal_request_id` (kept 3h) | `PaymentTokenResponse` | `Error` [400,403,404,422,500] · `RawError` |
| `vault.delete_payment_token` | `id` | — | **`None`**. Use `with_raw_response` to see the status | `Error` [400,403,500] · `RawError` |
| `transaction_search.search_transactions` | `start_date: str`, `end_date: str` (RFC 3339, seconds required, range ≤ 31 days) | `page_size` (default 100; 500 accepted, smoke-verified), `page` (1-based), `balance_affecting_records_only` (default `"Y"`; we send `"N"` so authorizations appear), `fields` (default `transaction_info`) | `SearchResponse` | **`RawError` only (Case B)** |

### Request members we set (required vs UNSET)

- `OrderRequest`: `intent` **required** (`CheckoutPaymentIntent.AUTHORIZE`), `purchase_units` **required**, `payment_source` Optional.
- `PurchaseUnitRequest`: `amount: AmountWithBreakdown` **required** (`currency_code`, `value` both required str); `invoice_id`, `custom_id`, `description` Optional. **We always set `invoice_id` = the attempt reference** (unique per merchant account, smoke: a reused one → 422 `DUPLICATE_INVOICE_ID`) and `custom_id` = `{prefix}-{order number}` so the reconciliation report can match rows.
- `PaymentSource.card: CardRequest`: `name`, `number`, `expiry` (`YYYY-MM`), `security_code`, `billing_address: Address` (`country_code` **required**), `vault_id`. All Optional.
- `PaymentTokenRequest`: `payment_source: PaymentTokenRequestPaymentSource` **required** → `.card: PaymentTokenRequestCard` (`name`, `number`, `expiry`, `security_code`, `billing_address`), plus `customer: Customer` (`id` Optional) so all of a shopper's cards share one PayPal customer.
- `Money`: `currency_code`, `value` both **required** `str`. Built with `Decimal.quantize` and the currency's exponent.
- Every `pay_pal_request_id` = our claim reference, derived from install prefix + order/user + step + attempt. **Never random.**

### Response members we assert on (UNSET ⇒ outcome unknown, never success)

- `Order`: `id`, `status: OrderStatusOrStr`, `purchase_units[0].payments.authorizations[0]` (`id`, `status`, `amount`, `create_time`, `expiration_time`), `payment_source.card` (`brand`, `last_digits`).
- `PaymentAuthorization`: `id`, `status: AuthorizationStatusOrStr`, `amount`, `create_time`/`update_time`, `expiration_time`.
- `CapturedPayment`: `id`, `status: CaptureStatusOrStr`, `amount`, `seller_receivable_breakdown` (`gross_amount` required; `paypal_fee`, `net_amount` Optional), `create_time`.
- `Refund`: `id`, `status: RefundStatusOrStr`, `amount`, `create_time`.
- `PaymentTokenResponse`: `id`, `customer.id`, `payment_source.card` (`brand`, `last_digits`, `expiry`). **No status field.**
- `SearchResponse`: `transaction_details[].transaction_info` (`transaction_id`, `paypal_reference_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_amount`, `fee_amount`, `transaction_status` (str: D/P/S/V), `invoice_id`, `custom_field`), `page`, `total_pages`.
- Time strings are RFC 3339 with `Z` **or** an offset (`-07:00` seen in smoke). Parse them with `datetime.fromisoformat`.

### Status enums (members read from `paypal/models/enums/`)

- `OrderStatus`: CREATED, SAVED, APPROVED, VOIDED, COMPLETED, PAYER_ACTION_REQUIRED
- `AuthorizationStatus`: CREATED, CAPTURED, DENIED, PARTIALLY_CAPTURED, VOIDED, PENDING
- `CaptureStatus`: COMPLETED, DECLINED, PARTIALLY_REFUNDED, PENDING, REFUNDED, FAILED
- `RefundStatus`: CANCELLED, FAILED, PENDING, COMPLETED
- Enums are open (`…OrStr`): an unlisted string maps to `unknown`.

### Smoke results (scratch dir, sandbox credential)

- Card create_order with intent AUTHORIZE and a card source → `COMPLETED` with authorization `CREATED`. **No payer-action challenge** for 4111…1111.
- Resending create_order under the same `PayPal-Request-Id` → 422 `TRANSACTION_REFUSED`, **not** the original. There is no de-dupe here, so the unknown-outcome check is a lookup, not a resend. A new request id with an old invoice id → 422 `DUPLICATE_INVOICE_ID`, so PayPal will not create a second one for the same reference.
- capture / void / refund / vault-create under the same request id → the original result (kind-2 de-dupe verified).
- A second capture under a new id → 422 `AUTHORIZATION_ALREADY_CAPTURED`. A second void → 422 `PREVIOUSLY_VOIDED`. A refund past the remaining amount → 422 `REFUND_AMOUNT_EXCEEDED`. Reauthorize inside the honor period → 422 `REAUTHORIZATION_TOO_SOON`.
- delete_payment_token → 204, also when repeated. Getting a deleted token → a non-JSON body (`ValueError`).
- search over a window that starts after PayPal's `last_refreshed_datetime` → **404** `INVALID_REQUEST` "Data for the given start date is not available." (reporting lag). The report lists that window under `paypalDataNotYetAvailable`; it is not an error.
- Reporting rows: authorization T1300 (transaction id = authorization id), capture T0005 (= capture id, reference = authorization id), refund T1107 (= refund id), void events T9900/T1302 (reference = authorization id). Matching uses the transaction id, then the referenced id.
- search: > 31 days → 400 with a `RawError` body. `page_size=500` is accepted. `total_pages` is present. The account is shared and busy (~6.6k rows in 31 days).

## OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| `orders.create_order` (pay step) | `Order.status`, then `purchase_units[0].payments.authorizations[0].status` | Order: COMPLETED → go to the authorization status. APPROVED → not yet: run the `authorize_order` step. CREATED, SAVED → pending. PAYER_ACTION_REQUIRED → failed with `payer_action_required` (we never do a browser round-trip; reported). VOIDED → failed. Absent or unlisted → unknown. Authorization: CREATED → **done** (held). PENDING → pending. DENIED, VOIDED → failed. CAPTURED, PARTIALLY_CAPTURED → done (the hold succeeded and is already being taken; state refreshed at fulfil). Absent or unlisted → unknown | `sandbox/apps/paypal_payments/payments.py` `read_authorization` → `statuses.order_outcome` / `statuses.authorization_outcome` (`sandbox/apps/paypal_payments/statuses.py`); applied by `payments._apply_authorization` → `_set_state_from_authorization`; answered by `views._payment_response` (200 only for done) |
| `orders.authorize_order` | `OrderAuthorizeResponse.purchase_units[0].payments.authorizations[0].status` (`AuthorizationStatus`) | the authorization mapping above | `sandbox/apps/paypal_payments/payments.py` `_authorize_approved` → `read_authorization` → `statuses.authorization_outcome` |
| `payments.reauthorize_payment` | `PaymentAuthorization.status` | CREATED → done. PENDING → pending. DENIED, VOIDED → failed. CAPTURED, PARTIALLY_CAPTURED → done. Absent or unlisted → unknown | `sandbox/apps/paypal_payments/payments.py` `_reauthorize` → `read_reauthorization` → `statuses.authorization_outcome` |
| `payments.capture_authorized_payment` | `CapturedPayment.status` (`CaptureStatus`) | COMPLETED → **done** (fee and net read from `seller_receivable_breakdown`). PARTIALLY_REFUNDED → done (the capture stands; the refunds are tracked separately). PENDING → pending (order stays `capture_pending`; a repeat of fulfil re-reads it). DECLINED, FAILED → failed. REFUNDED → failed (undone). Absent or unlisted → unknown | `sandbox/apps/paypal_payments/payments.py` `fulfil` → `read_capture` → `statuses.capture_outcome`; applied by `_apply_capture` |
| `payments.void_payment` | `PaymentAuthorization.status` | VOIDED → **done** (this write's goal). DENIED → done (nothing is held). CREATED, PENDING → pending (not yet released). CAPTURED, PARTIALLY_CAPTURED → failed (money already moved: cancel refused). Absent or unlisted → unknown | `sandbox/apps/paypal_payments/payments.py` `cancel` → `read_void` → `statuses.void_outcome`; post-write block in `cancel` (`_cancel_locally`, NEEDS_REVIEW on failed) |
| `payments.refund_captured_payment` | `Refund.status` (`RefundStatus`) | COMPLETED → **done**. PENDING → pending (counted against the refundable amount). FAILED, CANCELLED → failed (released from the refundable amount). Absent or unlisted → unknown (still counted) | `sandbox/apps/paypal_payments/payments.py` `refund` → `read_refund` → `statuses.refund_outcome`; `_apply_refund`; `views.refund_order` (201 done / 402 failed / 202 otherwise) |
| `vault.create_payment_token` | none: `PaymentTokenResponse` has no status member | `id` present **and** `payment_source.card` present → done (the token exists in the vault). Either one UNSET → unknown (never success). The shopper sees the card only when done | `sandbox/apps/paypal_payments/cards.py` `read_token`; `save_card` returns no card unless done (`views.payment_methods` → 202) |
| `vault.delete_payment_token` | none (returns `None`) | a 2xx `Success` → deleted at the provider. `Failure` → provider delete outstanding; the local card is already unusable, and a repeat DELETE retries | `sandbox/apps/paypal_payments/cards.py` `delete_card` (`match` on `Success` / `Failure`) |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| create_order (pay attempt n) | `PaymentOperation` row, `reference` = `{prefix}-{order}-pay-{n}`, inserted **and committed** before the call | the DB `UNIQUE` constraint on `PaymentOperation.reference` (IntegrityError). Attempt n+1 is only allowed when attempt n is `failed`, decided under the payment-row write lock | `try_claim` catches `IntegrityError` → the loser answers from the stored record | `sandbox/apps/paypal_payments/claims.py` `try_claim` (catches `IntegrityError`) called from `payments.pay` under `orders.locked_payment`; UNIQUE on `models.PaymentOperation.reference` |
| authorize_order (after APPROVED) | `PaymentOperation` `{prefix}-{order}-az-{n}` | same UNIQUE constraint | `try_claim` | `claims.try_claim` from `payments._authorize_approved` |
| reauthorize_payment | `PaymentOperation` `{prefix}-{order}-reauth-{k}` | same | `try_claim` | `claims.try_claim` from `payments._reauthorize` (under `orders.locked_payment`) |
| capture_authorized_payment | `PaymentOperation` `{prefix}-{order}-cap-{k}` (k increments only after a `failed` attempt with no provider effect) | same | `try_claim` | `claims.try_claim` from `payments.fulfil` (under `orders.locked_payment`) |
| void_payment | `PaymentOperation` `{prefix}-{order}-void-{k}` | same | `try_claim` | `claims.try_claim` from `payments.cancel` (under `orders.locked_payment`) |
| refund_captured_payment | `PaymentOperation` `{prefix}-{order}-rf-{sha256(Idempotency-Key)[:24]}`. The in-flight + done total is checked against the capture under the same payment-row write lock | same UNIQUE constraint. The same key with a different amount → 422 | `try_claim` + `RefundService` | `claims.try_claim` / `claims.retake_released` from `payments.refund`; headroom `payments._committed_refunds` under `orders.locked_payment` |
| create_payment_token | `PaymentOperation` `{prefix}-u{user}-vault-{hmac(card fingerprint)[:20]}-{n}` | same UNIQUE constraint | `try_claim` | `claims.try_claim` from `cards.save_card` (reference from `cards._fingerprint`) |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| create_order | **lookup** (a resend is refused, see smoke): `transaction_search.search_transactions` over [claimed_at − 1h, now], filtered on `invoice_id == reference`. On a hit, `payments.get_authorized_payment(transaction_id)`. Empty → still unknown (reports lag up to 3h) | the attempt reference, sent as `invoice_id` (and as `PayPal-Request-Id`) | `claims.safe_write` (repeat_is_safe=False) → `payments._find_authorization_by_invoice` (uses `reconciliation.iterate_transactions`) |
| authorize_order | lookup: `orders.get_order(paypal_order_id)` → `purchase_units[0].payments.authorizations[0]` | the PayPal order id recorded before the call + the step reference | inner `find` in `payments._authorize_approved` (`orders.get_order`) |
| reauthorize_payment | same-reference resend (docstring: keys kept 45 days) | `PayPal-Request-Id` = step reference | `claims.safe_write(find=send, repeat_is_safe=True)` in `payments._reauthorize` |
| capture_authorized_payment | same-reference resend (smoke-verified de-dupe). A 422 `AUTHORIZATION_ALREADY_CAPTURED` on a check = landed → lookup via `orders.get_order` captures | `PayPal-Request-Id` = step reference | `payments._find_capture` (same-id resend, then `orders.get_order` captures on AUTHORIZATION_ALREADY_CAPTURED) |
| void_payment | same-reference resend (smoke-verified). A 422 `PREVIOUSLY_VOIDED` = landed → `get_authorized_payment` | step reference | inner `find` in `payments.cancel` (same-id resend, then `get_authorized_payment` on PREVIOUSLY_VOIDED) |
| refund_captured_payment | same-reference resend (smoke-verified) | step reference | `claims.safe_write(find=send, repeat_is_safe=True)` in `payments.refund` |
| create_payment_token | same-reference resend (smoke-verified, 3h window). The shopper's repeat POST carries the card again and derives the same reference | step reference | `claims.safe_write(find=send, repeat_is_safe=True)` in `cards.save_card` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| create_order | Oscar `Order` + `PayPalPayment(state=authorizing)` + committed claim `PaymentOperation(outcome=sending, reference, amount)` | outcome, PayPal order id, authorization id/status/expiry/provider time, card brand/last4. Oscar `Source.allocate` + `Transaction(Authorise)` when done | before: `orders.place_order` + claim in `payments.pay`; after: `payments._apply_authorization` (`_record_txn` allocate) |
| authorize_order | the claim + `PayPalPayment.paypal_order_id` | same as above | before: claim in `payments._authorize_approved`; after: `_apply_authorization` + `claims.complete(pay_op, …)` |
| reauthorize_payment | claim + the old authorization id | the new authorization id/expiry/created time on `PayPalPayment` | before: claim in `payments._reauthorize`; after: its post-write locked block (`reauthorized_from`, new id, `_record_txn`) |
| capture_authorized_payment | claim (amount = order total) + `PayPalPayment(state=capturing)` | capture id/status, gross/fee/net, provider time. Oscar `Source.debit` + order status → Complete | before: claim + CAPTURING in `payments.fulfil`; after: `payments._apply_capture` (`_record_txn` debit, `_advance_order_status`, `_adjust_stock`) |
| void_payment | claim + `PayPalPayment(state=voiding)` | outcome, provider time. Oscar order status → Cancelled, stock allocation released | before: claim + VOIDING in `payments.cancel`; after: its post-write block (`_record_txn` Void, `_cancel_locally`) |
| refund_captured_payment | claim (amount, idempotency key hash, public refund id) | refund id/status/time. `PayPalPayment.refunded_amount`, Oscar `Source.refund` | before: claim in `payments.refund`; after: `payments._apply_refund` (`_record_txn` refund) |
| create_payment_token | claim (user, reference) | `SavedCard` (token id, customer id, brand, last4, expiry) linked to the claim | before: claim in `cards.save_card`; after: `cards._record_card` |
| delete_payment_token | `SavedCard.deleted_at` set (hidden and unusable) **before** the call | `provider_deleted_at` | `cards.delete_card` (`deleted_at` update, then provider call, then `provider_deleted_at`) |

## Reconciliation design

`GET /api/reconciliation?from&to`. Both params are ISO-8601 with an offset (naive → 400). `from < to`,
and the range is ≤ 3 years (the reporting API's limit).
Provider side: split [from, to) into ≤ 31-day windows. For each window, walk every page (`page_size=500`,
`page` 1…`total_pages`) with `balance_affecting_records_only="N"`. Narrow again in code to
`from ≤ transaction_initiation_date < to`. Local side: every provider id we recorded (authorization,
reauthorization, capture, refund) whose **provider time** is in the window. Matching is against the set:
one order owns all of its PayPal rows. Report: `matched` (per order, every PayPal row), `appOnly`
(recorded by us, absent at PayPal), `paypalOnly` split into `ours` (invoice/custom id carries our
prefix: a stranded write) and `foreign` (other activity on the account), `unsettled` (claims in the
window with no provider time: sending/unknown). An empty range is a normal result.

## Error boundary (python-error-handling)

One translator in `gateway.py`. It maps to `PaymentProviderError(status_code, message, outcome_unknown,
issue)`. OAuthProviderError → 502 config. 401/403 → 502. 429 → 503. 400/404/409/422 with a typed `Error`
→ the caller's (422/409/404/400) with PayPal's `issue`. 5xx/unmapped → 502, `outcome_unknown` true on
5xx. `ConnectError/ConnectTimeout/PoolTimeout/ProxyError` → 502 never sent. Other `httpx.RequestError`
→ 504 unknown. `ValueError`/`ValidationError` → 502 unreadable (unknown on a write). The safe write
sees raw exceptions first and settles unknown outcomes itself.

Retries: none on writes (the safe write's same-reference check replaces them). Reads
(get/search) get up to 3 attempts on 429/5xx/transport, with backoff.
Logging: a transport wrapper logs method, path, status and ms. Never headers or bodies. Card fields
never reach a log; views handling card data use `sensitive_variables`.

## Assumptions & Blockers

- Minor: live PayPal host is not named by the plugin → non-sandbox environments must set `PAYPAL_BASE_URL`.
- Minor: the task notes call the CSV import optional; on this machine it is required (without it 11
  products load and the ranges/orders fixtures fail FK checks).
- Minor: the account is shared, so reconciliation shows many `foreign` PayPal-only rows. That is expected.
- Baseline: `tests/integration/order/test_creator.py::TestConcurrentOrderPlacement::test_single_usage`
  fails on SQLite before any change (a Postgres concurrency test). Everything else in payment/order/checkout passes (264).
- No blockers.

## REQUIRED READING

- Client lifetime, custom transport, close: MUST load python-client-initialization (loaded)
- OAuth credentials and token-failure shape: MUST load python-authentication (loaded)
- Keyword-only calls, `with_raw_response` for `delete_payment_token`, status-by-name: MUST load python-calling-endpoints (loaded)
- UNSET vs None, open enums, Money as Decimal str: MUST load python-models (loaded)
- Error ladder, never-sent vs unknown: MUST load python-error-handling (loaded)
- Safe write, claims, reconciliation clocks, timeouts, logging: MUST load python-configuration-resilience (loaded)
- Stub transport tests: MUST load python-testing (loaded)
