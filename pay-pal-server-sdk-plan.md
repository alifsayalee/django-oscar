# PayPal integration plan — django-oscar sandbox (`sandbox/apps/paypal_payments`)

## Scope

HTTP API under `/api/` on the sandbox project:

| Route | Who | What |
| --- | --- | --- |
| `POST /api/orders` | shopper | Place an Oscar order (catalogue product ids + quantities), status `Pending payment` |
| `POST /api/orders/{orderId}/pay` | shopper | Authorize the total, using a one-off card or a saved card |
| `POST /api/orders/{orderId}/fulfil` | staff | Capture (reauthorize first if the hold is stale) |
| `POST /api/orders/{orderId}/cancel` | staff | Void the authorization, or cancel an unpaid order |
| `POST /api/orders/{orderId}/refunds` | shopper (own order) | Full or partial refund; needs an `Idempotency-Key` header |
| `GET /api/my-orders` | shopper | The caller's orders with their payment state |
| `GET /api/reconciliation?from=&to=` | staff | PayPal transactions vs local payments |
| `POST/GET /api/payment-methods`, `DELETE /api/payment-methods/{id}` | shopper | Saved cards (PayPal vault) |
| `GET /api/csrf`, `POST /api/login`, `POST /api/logout` | anyone | Django session login for API callers (CSRF enforced) |

Refunds are a shopper-callable "return" in the task wording (only fulfil, cancel and reconciliation are staff-only). A refund acts only on an order the caller owns; staff may also refund any order.

## SDK identity (drift from the skill page, verified against the installed package)

| Fact | Value |
| --- | --- |
| Distribution / import root | **`paypal` / `paypal`**. The skill page says `pay-pal-server-sdk` / `pay_pal_server_sdk`; the SDK repo's `pyproject.toml` and `sdk-map.md` both say `paypal`. |
| Install | `pip install "paypal @ git+https://github.com/context-plugins/paypal-python-sdk.git@main"`, version 2.29, into `venv/` |
| Client | `paypal.PaypalClient` (sync). The host is Django under WSGI, so it is **sync**; `AsyncPaypalClient` is not used. |
| Construction | keyword-only: `base_url`, `timeout`, `custom_http_client`, `oauth2` |
| Lifetime | one lazily built module-level client per process, built on first use (so after any fork), closed by `atexit` → `client.close()` |
| Auth | `oauth2=ClientCredentials(client_id=settings.PAYPAL_CLIENT_ID, client_secret=settings.PAYPAL_CLIENT_SECRET)`. Missing values fail at build time (`ImproperlyConfigured`), never sent unauthenticated. |
| Host | The SDK declares one server, `https://api-m.sandbox.paypal.com`. `PAYPAL_BASE_URL`, if set, is used verbatim (the token endpoint moves with it). Otherwise `PAYPAL_ENVIRONMENT=sandbox` → the SDK's sandbox URL. Any other environment with no `PAYPAL_BASE_URL` → `ImproperlyConfigured`: the plugin documents no other host, so none is guessed. `base_url` is always passed explicitly. |
| Retries | the SDK does **none**. We add none for writes; every write goes through the safe write (claim → call → check → verify → complete). Reads are not retried either; a failed read is reported as 502/504. |
| Timeout | `timeout=20.0` on the client (a single float, applied to connect/read/write/pool) |
| Logging | a transport wrapper logs method, URL and status only, never headers or bodies |

Every keyword-only parameter has a real default, so no defensive `None`s are passed. `Optional[T]` in models is `T | UnsetType`: we never pass `None`; we read with `isinstance(x, UnsetType)`.

## Contract sheet

All operations: the parsed call raises `paypal.core.ApiError`; the error union is `Error | RawError` (`paypal.models.Error`: `name`, `message`, `debug_id`, `details: list[ErrorDetails]` with `issue`, `description`). The exception is `search_transactions`, which is Case B (`RawError` only). A failed token fetch raises `ApiError` with `.error` of `OAuthProviderError | RawError`, out of the operation call, in both response modes. A decode failure raises `ValueError`/`pydantic.ValidationError`, not `ApiError`. Transport errors come through as raw `httpx` exceptions.

| Operation | Signature (positional \| keyword-only) | Body we send | Result members we assert | Status enum → outcome |
| --- | --- | --- | --- | --- |
| `orders.create_order` | `(body: OrderRequest, *, pay_pal_request_id, prefer="return=minimal", …)` → `Order` | `OrderRequest(intent=CheckoutPaymentIntent.AUTHORIZE, purchase_units=[PurchaseUnitRequest(reference_id, custom_id, invoice_id, amount=AmountWithBreakdown(currency_code, value))])`. No `payment_source`, so no money moves. `prefer="return=representation"`. | `id`, `status`, `purchase_units[0].amount.value/currency_code` | `OrderStatus`: CREATED, SAVED, APPROVED → done (the order shell exists) · COMPLETED → done · PAYER_ACTION_REQUIRED → pending · VOIDED → failed · other/UNSET → unknown |
| `orders.authorize_order` | `(id: str, *, pay_pal_request_id, prefer, body: OrderAuthorizeRequest)` → `OrderAuthorizeResponse` | `OrderAuthorizeRequest(payment_source=OrderAuthorizeRequestPaymentSource(card=CardRequest(number, expiry "YYYY-MM", security_code, name, billing_address=Address(... country_code))))`, or `card=CardRequest(vault_id=token)` | `status` (order), `purchase_units[0].payments.authorizations[0]`: `id`, `status`, `amount`, `create_time`, `expiration_time`, `status_details.reason` | `AuthorizationStatus`: CREATED → done · PENDING → pending · DENIED → failed · VOIDED → failed (undone) · CAPTURED, PARTIALLY_CAPTURED → done (the hold existed and moved on) · other/UNSET → unknown. An order `status` of PAYER_ACTION_REQUIRED means a browser challenge: recorded pending, reported as `payer_action_required` (the task says STOP; no approval round-trip is built). |
| `orders.get_order` | `(id: str, *, fields=None, …)` → `Order` | — | `purchase_units[0].payments.authorizations[*]` (the lookup for authorize and reauthorize) | read only |
| `payments.get_authorized_payment` | `(authorization_id: str, …)` → `PaymentAuthorization` | — | `status`, `expiration_time`, `create_time` | read only, used for the staleness check before capture and as the void lookup |
| `payments.reauthorize_payment` | `(authorization_id: str, *, pay_pal_request_id, prefer, body: ReauthorizeRequest)` → `PaymentAuthorization` | `ReauthorizeRequest(amount=Money(currency_code, value))` | `id` (new authorization), `status`, `amount`, `create_time` | `AuthorizationStatus` as for authorize |
| `payments.capture_authorized_payment` | `(authorization_id: str, *, pay_pal_request_id, prefer, body: CaptureRequest)` → `CapturedPayment` | `CaptureRequest(amount=Money(...), final_capture=True, invoice_id)` | `id`, `status`, `amount`, `seller_receivable_breakdown.gross_amount` (required member) / `.paypal_fee` / `.net_amount`, `create_time` | `CaptureStatus`: COMPLETED → done · PENDING → pending · DECLINED, FAILED → failed · REFUNDED, PARTIALLY_REFUNDED → failed (undone) · other/UNSET → unknown |
| `payments.void_payment` | `(authorization_id: str, *, pay_pal_request_id, prefer)` → `PaymentAuthorization` | no body; `prefer="return=representation"` (the docstring says the body only comes back with representation) | `id`, `status`, `update_time` | For the void write: VOIDED → done · PENDING, CREATED → pending (not released yet) · DENIED → done (no hold exists; nothing to release) · CAPTURED, PARTIALLY_CAPTURED → failed (money moved; void impossible) · other/UNSET → unknown |
| `payments.refund_captured_payment` | `(capture_id: str, *, pay_pal_request_id, prefer, body: RefundRequest)` → `Refund` | `RefundRequest(amount=Money(...), invoice_id, note_to_payer?)` | `id`, `status`, `amount`, `create_time`, `seller_payable_breakdown.total_refunded_amount` | `RefundStatus`: COMPLETED → done · PENDING → pending · FAILED, CANCELLED → failed · other/UNSET → unknown |
| `vault.create_payment_token` | `(body: PaymentTokenRequest, *, pay_pal_request_id)` → `PaymentTokenResponse` | `PaymentTokenRequest(customer=Customer(id)?, payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(number, expiry, security_code, name, billing_address)))` | `id`, `customer.id`, `payment_source.card.last_digits/brand/expiry/name` | **The response has no status member.** Done = `id` present AND `payment_source.card` present (the vaulted instrument echoed back). An `id` with no card → unknown. |
| `vault.delete_payment_token` | `(id: str)` → `None` | — | returns None; success = no exception | none (returns `None`; parsed mode is enough since success is "no raise") |
| `transaction_search.search_transactions` | `(start_date: str, end_date: str, *, fields="transaction_info", balance_affecting_records_only="Y", page_size=100, page=1, …)` → `SearchResponse` | RFC 3339 with seconds; **range ≤ 31 days** (docstring), so the range is split into windows; `balance_affecting_records_only="N"` so authorizations appear too; `page` loops to `total_pages` | `transaction_details[*].transaction_info`: `transaction_id`, `paypal_reference_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_amount`, `fee_amount`, `transaction_status`, `invoice_id`, `custom_field`; `total_pages` | read only. Case B error (`RawError`). |

Enum imports: `paypal.models.enums` — `OrderStatus`, `AuthorizationStatus`, `CaptureStatus`, `RefundStatus`, `CheckoutPaymentIntent`. Models: `paypal.models`. Runtime: `paypal.core` (`ApiError`, `RawError`, `OAuthProviderError`, `ClientCredentials`, `HttpxClient`, `HttpRequest`, `HttpResponse`, `UNSET`, `UnsetType`).

Money is a `str`. It is built from `Decimal` quantized to the currency's ISO-4217 exponent (2 unless listed otherwise) and compared as `Decimal`.

### Verified against the real sandbox (scratchpad smoke, outside the repo)

- **Single-step** `create_order` with a card `payment_source`: a repeat under the same `PayPal-Request-Id` → 422 `TRANSACTION_REFUSED`, not a replay. **So it is not used.**
- `create_order` with **no** payment source: a repeat under the same key returns the **same** order id (kind 2 replay).
- `authorize_order` with card or `vault_id` works without a challenge. A repeat under the same key → 422 `TRANSACTION_REFUSED` (no replay). `get_order(id)` lists the authorization, so that is the lookup (kind 1).
- `capture_authorized_payment`, `refund_captured_payment`, `vault.create_payment_token`: a repeat under the same key returns the same object (kind 2).
- `void_payment` repeated → VOIDED again. A duplicate `invoice_id` on create → 422 `DUPLICATE_INVOICE_ID`. Over-refund → 422 `REFUND_AMOUNT_EXCEEDED`. Reauthorize inside the honor period → 422 `REAUTHORIZATION_TOO_SOON`.
- `get_payment_token` on a deleted token returned an empty 2xx body (a decode failure), so we never read a token back after delete.
- `search_transactions` over 30 days returns data (1728 items in the merchant account), with pagination via `total_pages`.

## Design

- **App** `sandbox/apps/paypal_payments` (label `paypal_payments`), wired at `path('api/', include('apps.paypal_payments.urls'))` outside `i18n_patterns`.
- **Reused Oscar models**: `order.Order`/`Line` (through `OrderCreator.place_order` over a dedicated basket, using `Selector().strategy(user=...)` prices and `shipping.methods.Free`); `payment.Source` (`amount_allocated`/`debited`/`refunded`) and `payment.Transaction` as the money ledger; `payment.SourceType` "PayPal".
- **New models**:
  - `PayPalPayment`: one per order. Holds the PayPal state a later request acts on: paypal order id, authorization id/status/expiry/time, capture id/status/gross/fee/net, currency, amount, state.
  - `PayPalOperation`: the **claim**. Unique `reference`, kind, outcome, provider id/status/time, amount, error.
  - `PayPalRefund`: caller key, amount, PayPal refund id/status.
  - `SavedCard`: user, vault token id, brand, last digits, expiry, name, `deleted_at`.
  - `PayPalCustomer`: user → vault customer id.
  - `InstallIdentity`: a random install token created once, so references never collide across a DB rebuild or across installs sharing one PayPal account.
- **Card data**: the PAN and CVC exist only in the request body and the outgoing PayPal request. They are never stored, never logged, and never echoed back. Django's error reporting is kept away from them with `sensitive_post_parameters`/`sensitive_variables`.
- **Views** are `transaction.non_atomic_requests`, because the sandbox sets `ATOMIC_REQUESTS=True`. The claim has to be **committed** before the provider call, and the outcome committed after it.
- **Order status**: we add `'Pending payment': ('Pending', 'Cancelled')` to `OSCAR_ORDER_STATUS_PIPELINE` and leave the existing statuses alone. Authorized → `Pending`. Fulfilled → `Being processed` → `Complete`. Cancelled → `Cancelled`.
- **Stale authorization**: the docstring gives a 3-day honor period and 29-day reauthorize window, so before capture we call `get_authorized_payment`:
  - VOIDED/DENIED, or past `expiration_time` → cannot renew. 409 `authorization_expired` with an operator action ("cancel and ask the shopper to pay again").
  - Older than 3 days → `reauthorize_payment` (safe write), then capture the new authorization.
  - If reauthorize is refused → 409 `authorization_not_renewable`, carrying PayPal's issue and the same operator action.
  - A capture 422 whose issue names an expired authorization triggers one reauthorize attempt.

### OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| `orders.create_order` (pay step 1) | `Order.status` (`OrderStatus`) | CREATED/SAVED/APPROVED/COMPLETED → done: go on to authorize · PAYER_ACTION_REQUIRED → pending: payment `payer_action_required`, 202, stop · VOIDED → failed: payment `declined`, 402 `provider_failed`; the next pay uses a new attempt number · other value or UNSET → unknown: 504 `outcome_unknown`; the payment stays `authorizing` and a repeat checks it | `outcomes.order_outcome`, called from `service._read_order_shell`; state set by `apply_create` and answered in `service.pay_order` via `service._answer_from_stored` |
| `orders.authorize_order` (pay step 2) | the newest `purchase_units[0].payments.authorizations[].status` (`AuthorizationStatus`), plus `Order.status` for PAYER_ACTION_REQUIRED | CREATED → done: payment `authorized`, `Source.allocate`, order `Pending` · CAPTURED/PARTIALLY_CAPTURED → done · PENDING → pending: payment `authorization_pending`, 202 · DENIED/VOIDED → failed: payment `declined`, 402 `payment_declined` · order PAYER_ACTION_REQUIRED → pending: payment `payer_action_required`, 202 · other value, UNSET or no authorization → unknown: 504 | `outcomes.authorization_outcome`, called from `service._read_authorization`; applied in `service._apply_authorization`; answered in `service.pay_order` |
| `payments.reauthorize_payment` (fulfil, stale hold) | `PaymentAuthorization.status` | CREATED/CAPTURED/PARTIALLY_CAPTURED → done: the new authorization id, time and expiry replace the old ones, then capture · PENDING → pending: 409 `reauthorization_pending` ("fulfil again shortly") · DENIED/VOIDED → failed: 409 `authorization_not_renewable` with the operator action · a 4xx refusal → 409 `authorization_not_renewable` carrying PayPal's issue · other value → unknown: 504 | `outcomes.authorization_outcome`, called from `service._read_payment_authorization`; `service._reauthorize` (its `apply`) |
| `payments.capture_authorized_payment` (fulfil) | `CapturedPayment.status` (`CaptureStatus`) | COMPLETED → done: gross/fee/net from `seller_receivable_breakdown`, `Source.debit`, order `Being processed` → `Complete` · PENDING → pending: payment `capture_pending`, 202; the next fulfil re-checks by resend · DECLINED/FAILED → failed: payment back to `authorized` with an operator note, 402 `capture_declined` · REFUNDED/PARTIALLY_REFUNDED → failed (undone), same path · other value or UNSET → unknown: 504; payment back to `authorized`; the next fulfil re-checks under the same reference | `outcomes.capture_outcome`, called from `service._read_capture`; `service._apply_capture`; answered in `service.fulfil_order` |
| `payments.void_payment` (cancel) | `PaymentAuthorization.status` | VOIDED → done: payment `voided`, Source allocation zeroed plus a `Void` transaction, order `Cancelled` · DENIED → done (no hold to release) · PENDING/CREATED → pending: payment stays `voiding`, 202; the next cancel re-checks · CAPTURED/PARTIALLY_CAPTURED → failed: payment `needs_review`, 409 `already_captured` (use a refund) · other value or UNSET → unknown: 504 | `outcomes.void_outcome`, called from the `read` inside `service.cancel_order`; `service._apply_void` |
| `payments.refund_captured_payment` | `Refund.status` (`RefundStatus`) | COMPLETED → done: `Source.refund`, `refunded_amount` updated, payment `partially_refunded`/`refunded`, 201 · PENDING → pending: 202, the amount stays reserved · FAILED/CANCELLED → failed: 402, the reservation is released (refund outcome `failed`) · other value or UNSET → unknown: 504, the amount stays reserved | `outcomes.refund_outcome`, called from the `read` inside `service.refund_order`; `service._apply_refund`, `service._sync_refund` |
| `vault.create_payment_token` | none on the response (the model has no status member) | `id` and `payment_source.card` both present → done: `SavedCard` (+ `PayPalCustomer`) created, 201 · `id` without a card, or no `id` → unknown: 504 `outcome_unknown` | `outcomes.vault_outcome`, called from the `read` inside `service.save_card`; the `apply` inside `service.save_card` |
| `vault.delete_payment_token` | none (returns `None`; success = no exception) | no exception → `provider_deleted=True` · any exception → logged by code; the card stays hidden and unusable locally, with `provider_deleted=False` for an operator | `service.delete_card` |

### DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| create_order (per pay attempt) | a `PayPalOperation` row with `reference` = `{prefix}-{install}:{order}:create:{attempt}`, committed before the call | the DB UNIQUE constraint on `PayPalOperation.reference` | `IntegrityError` inside `try_claim` → the loser answers from the stored outcome (409 `in_progress` while in flight) or checks a stale/unknown claim | `safewrite.try_claim` and `safewrite.safe_write` step 1, called from `service.pay_order` |
| authorize_order (per pay attempt) | a `PayPalOperation` row, `{prefix}-{install}:{order}:authorize:{attempt}` | the same UNIQUE constraint | the same | `safewrite.try_claim` via `safewrite.safe_write`, called from `service.pay_order` |
| pay request as a whole (double-click) | `PayPalPayment.state` moves `awaiting_payment`/`declined` → `authorizing` in one conditional `UPDATE` that also increments `attempt` | the conditional UPDATE matches 0 rows for a second request, which then resumes the same attempt (through the per-step claims above) or answers the stored state | `AlreadyDone` in `service.pay_order` returns the current payment; an in-flight step answers 409 `in_progress` | `service._begin_pay_attempt` |
| reauthorize_payment | a `PayPalOperation` row, `{prefix}-{install}:{order}:reauthorize:{old_auth_id}` | the UNIQUE constraint | `safewrite.try_claim` | `service._reauthorize` |
| capture_authorized_payment | a `PayPalOperation` row, `{prefix}-{install}:{order}:capture:{auth_id}`; plus `PayPalPayment.state` `authorized` → `capturing` by conditional UPDATE | the UNIQUE constraint | `safewrite.try_claim`; a finished capture answers from the stored state | `run_capture` inside `service.fulfil_order` |
| void_payment | a `PayPalOperation` row, `{prefix}-{install}:{order}:void:{auth_id}`; plus `PayPalPayment.state` → `voiding` by conditional UPDATE | the UNIQUE constraint | `safewrite.try_claim` | `service.cancel_order` |
| refund_captured_payment | a `PayPalRefund` row (UNIQUE `(payment, idempotency_key)` and UNIQUE `reference`), inserted together with the balance check in one transaction that first writes the payment row; then a `PayPalOperation` with the same reference `{prefix}-{install}:{order}:refund:{sha256(key)[:32]}` | the UNIQUE constraints; the balance check sees every non-failed reservation | the existing row is returned for the same key (a different amount → 409 `idempotency_key_reused`); a second claim is caught in `try_claim` | `service._reserve_refund`; `safewrite.try_claim` via `service.refund_order` |
| create_payment_token | a `PayPalOperation` row, `{prefix}-{install}:vault:{user}:{sha256(Idempotency-Key)[:32]}` (a request without the header gets a fresh key: a new save by definition) | the UNIQUE constraint (plus UNIQUE `SavedCard.reference` and `vault_token_id`) | `safewrite.try_claim` → a repeat returns the stored card | `service.save_card` |

### UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| create_order | same-reference resend (a verified replay returns the same order id) | `PayPal-Request-Id` = the claim reference | `safewrite.safe_write` with `repeat_is_safe=True, find=send_create`, in `service.pay_order` |
| authorize_order | lookup `orders.get_order(paypal_order_id)` → its authorizations (a resend is refused, so only a lookup is safe) | the PayPal order id recorded by step 1 (whose create carried the step-1 reference) | `service._find_authorization` (the `find` given to `safe_write` in `service.pay_order`) |
| reauthorize_payment | lookup `orders.get_order(paypal_order_id)` → an authorization other than the old id | the PayPal order id plus the old authorization id | `service._find_reauthorization` (via the `find` inside `service._reauthorize`) |
| capture_authorized_payment | same-reference resend (verified replay) | `PayPal-Request-Id` = the capture reference | `safewrite.safe_write` with `repeat_is_safe=True`, in `run_capture` inside `service.fulfil_order` |
| void_payment | lookup `payments.get_authorized_payment(auth_id)` | the authorization id | the `find` lambda passed to `safe_write` in `service.cancel_order` |
| refund_captured_payment | same-reference resend (verified replay) | `PayPal-Request-Id` = the refund reference | `safewrite.safe_write` with `repeat_is_safe=True`, in `service.refund_order` |
| create_payment_token | same-reference resend (verified replay) | `PayPal-Request-Id` = the vault reference | `safewrite.safe_write` with `repeat_is_safe=True`, in `service.save_card` |

### WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| create_order | the Oscar Order, `PayPalPayment(state=authorizing, attempt=n)` and `PayPalOperation(reference, outcome=sending)`, all committed | operation outcome, provider id/status/time; `PayPalPayment.paypal_order_id` | `service._begin_pay_attempt` and `safewrite.try_claim`, then `safewrite.complete` → `apply_create` (in `service.pay_order`) |
| authorize_order | `PayPalOperation(sending)`, with the PayPal order id on the payment | authorization id/status/time/expiry on the payment; `Source.allocate`; order `Pending` | `safewrite.try_claim` → `safewrite.complete` → `service._apply_authorization` |
| reauthorize_payment | `PayPalOperation(sending)` | the new authorization id/time/expiry; a `Reauthorise` Source transaction | `service._reauthorize` (its `apply`) |
| capture_authorized_payment | `PayPalOperation(sending)`, payment state `capturing` | capture id/status/gross/fee/net/time; `Source.debit`; order `Complete` | `service.fulfil_order` → `service._apply_capture` |
| void_payment | `PayPalOperation(sending)`, payment state `voiding` | authorization status; payment `voided`; a `Void` Source transaction; order `Cancelled` | `service.cancel_order` → `service._apply_void` |
| refund_captured_payment | `PayPalRefund(outcome=sending, amount reserved)`, then `PayPalOperation(sending)` | refund id/status; `Source.refund`; payment refund state | `service._reserve_refund`, then `service._apply_refund` / `service._sync_refund` |
| create_payment_token | `PayPalOperation(sending)` | `SavedCard` + `PayPalCustomer` | the `apply` inside `service.save_card` |
| delete_payment_token | `SavedCard.deleted_at` set and committed (the card is hidden and unusable) | `SavedCard.provider_deleted=True` | `service.delete_card` |

### Error boundary (one ladder, `errors.translate`)

- `OAuthProviderError`, 401 or 403 → 502 `provider_config`.
- 429 → 503.
- A 4xx with `Error` → the same 4xx (402 for card declines), carrying PayPal's `issue`/`description` but never `str(e)`.
- 5xx → 502, with outcome unknown.
- `ValidationError`/`ValueError` on 2xx → 502 unreadable.
- `ConnectError`/`ConnectTimeout`/`PoolTimeout`/`ProxyError` → 502, outcome known.
- Other `httpx.RequestError` → 504, outcome unknown.
- `OutcomeUnknown` → 504. `AmountMismatch` → 502 `needs_review`.

### Reconciliation

- Split `[from, to)` into ≤31-day windows and paginate each until `total_pages`, with `balance_affecting_records_only="N"` and `page_size=500` (PayPal rejects 501: "must be less than or equal to 500"). Pages 2..N are fetched four at a time (`reconciliation.fetch_provider_transactions`).
- Narrow the provider records back to `[from, to)` on `transaction_initiation_date`.
- Local side: `PayPalOperation`s with `outcome=done` whose stored **provider time** falls in the window. Unsettled rows (sending/unknown/pending/needs_review, or no provider time) are reported separately.
- Match by PayPal id, then by `invoice_id`/`custom_field` carrying our install prefix. Match against the whole set (`reconciliation.build_report`). Verified read-only against the sandbox: the reporting `transaction_id` of a T0006 capture, a T1300 authorization, a T1302 void and a T1107 refund is that capture, authorization or refund id itself.
- Report: `matched`, `provider_only`, `local_only`, `unsettled`, and amount mismatches.

## Assumptions & Blockers

- Minor: order amounts use the catalogue price number (fixtures are GBP-priced) with `PAYPAL_CURRENCY` as the currency, as the task mandates.
- Minor: shipping is free (Oscar `Free` method); no shipping address is required by the API.
- Minor: no live-host URL is in the plugin, so non-sandbox environments require `PAYPAL_BASE_URL`.
- No blockers: every capability was found in the SDK and verified in the sandbox.

## REQUIRED READING

- Client construction and lifetime → MUST load `python-client-initialization` (loaded)
- Credentials, token-fetch failure → MUST load `python-authentication` (loaded)
- Operation calls, response modes, status gating → MUST load `python-calling-endpoints` (loaded)
- UNSET, open enums, money strings → MUST load `python-models` (loaded)
- Error ladder, transport failures → MUST load `python-error-handling` (loaded)
- Safe write, reconciliation, base URL, timeout, logging → MUST load `python-configuration-resilience` (loaded)
- Stub transport tests → MUST load `python-testing` (loaded)

## Toolchain

- `venv\Scripts\python` (3.11). Tests: `cd sandbox && ..\venv\Scripts\python manage.py test apps.paypal_payments`. Type check: `mypy --strict` on the app (mypy + django-stubs installed into the venv).
