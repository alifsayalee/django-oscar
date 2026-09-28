# PayPal Server SDK — integration plan & contract sheet

Scope: PayPal card payments (authorize → capture at fulfilment → void / refund), vaulted cards, and a
transaction-search reconciliation report, exposed as a JSON API on the django-oscar sandbox
(`sandbox/`), as the new Django app `sandbox/apps/paypal_payments` (import path `apps.paypal_payments`,
app label `paypal_payments`), routed under `/api/` from `sandbox/urls.py`.

## SDK identity (verified against the installed package — the skill's identity table has drifted)

| Fact | Value (source) |
| --- | --- |
| Distribution | `paypal` 2.29 — installed with `pip install "paypal @ git+https://github.com/context-plugins/paypal-python-sdk.git@main"` (`pyproject.toml` `name = "paypal"`; the skill's `pay-pal-server-sdk` name does not exist) |
| Import root | `paypal` (`sdk-map.md`: Root package `paypal`) — **not** `pay_pal_server_sdk` |
| Sync client | `PaypalClient` (alias `Client`) — `from paypal import PaypalClient` (sdk-map *Getting a client*) |
| Constructor | keyword-only: `base_url: str \| None = None`, `timeout: float = 30.0`, `custom_http_client: HttpClient \| None`, `oauth2: ClientCredentialsOrDict \| None`, `oauth2_token_source` (sdk-map constructor table) |
| Auth | `oauth2=ClientCredentials(client_id=..., client_secret=...)` from `paypal.core`; token from `<base_url>/v1/oauth2/token`, lazy, cached on the client. Omitting `oauth2` = unauthenticated requests, no error at construction. |
| Server | one server, one environment; only base URL declared is `https://api-m.sandbox.paypal.com`; override with `base_url=` (moves the token call too). No environment enum. |
| Imports | models `paypal.models`; enums `paypal.models.enums`; error aliases `paypal.errors`; `ApiError`, `RawError`, `Success`, `Failure`, `UNSET`, `UnsetType`, `HttpxClient`, `HttpRequest`, `HttpResponse`, `OAuthProviderError`, `ClientCredentials` from `paypal.core` (`core/__init__.py __all__`) |
| Map | cloned at `scratch/paypal-python-sdk` (branch `main`), `sdk-map.md` + `map/operations/*.md` |

## Decisions

| Decision | Value |
| --- | --- |
| Sync vs async | **Sync** `PaypalClient`. Host is Django under WSGI with sync views. Never `AsyncPaypalClient`. |
| Client lifetime | One process-wide client, built lazily on first use (post-fork safe) by `gateway.get_client()`, closed by an `atexit` hook. Never per request. |
| Base URL | `settings.PAYPAL_BASE_URL` if set (verbatim, also for the token call) else `{"sandbox": "https://api-m.sandbox.paypal.com"}[PAYPAL_ENVIRONMENT]`; any other environment without `PAYPAL_BASE_URL` → `ImproperlyConfigured` (the SDK declares no other host, so none is invented). Always passed explicitly. |
| Credentials | `settings.PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` (read from env in `sandbox/settings.py`); missing → `ImproperlyConfigured` before any call. |
| Currency | `settings.PAYPAL_CURRENCY`; amounts formatted by currency exponent with `Decimal` (never float). |
| Timeout | client `timeout=20.0`; no per-call override. |
| Retries | **None automatic.** The SDK has none; we add none. A repeat request re-checks an unknown/pending write via same-key resend (below). |
| Logging | `LoggingTransport` wraps `HttpxClient`: method + path + status + ms only; never headers or bodies (card data, bearer token). |
| Response mode | Parsed calls everywhere except `vault.delete_payment_token` (returns `None` → `with_raw_response` to see 204) and `vault.create_payment_token` (raw, to read the 200/201 status since the response has no status enum). |
| Claim store | The app's own SQLite/Postgres DB via Django ORM: `PayPalWrite.ref` has a UNIQUE constraint; claim = INSERT, the DB rejects the second. Views are `transaction.non_atomic_requests` so the claim is **committed before** the PayPal call (the sandbox sets `ATOMIC_REQUESTS=True`). |
| Reference | `deterministic_ref(*parts)` = `"{prefix}-{parts...}"`; prefix = `settings.PAYPAL_REFERENCE_PREFIX` or a per-install random id persisted once in `InstallIdentity`. Sent as `PayPal-Request-Id` on every write, and as `invoice_id`/`custom_id` on the purchase unit. |

## Contract sheet — operations in scope

All signatures: sync parsed spelling; `.with_raw_response.<op>` takes the same parameters. Every
keyword-only parameter has a real default — never pass defensive `None`s. `prefer` defaults to
`"return=minimal"` on every write that has it: **always pass `prefer="return=representation"`** or the
body carries only id/status/links.

| Op | Signature (positional ｜ keyword-only) | Returns | `ApiError.error` union (status → arm) | Idempotency header / retention (docstring) |
| --- | --- | --- | --- | --- |
| `orders.create_order` | `body: OrderRequest\|OrderRequestDict` ｜ `pay_pal_request_id`, `prefer`, … | `Order` | `CreateOrderErrorBody` = `Error` [400,401,422] ｜ `RawError` [else — incl. 403 seen for a deleted vault id] | `pay_pal_request_id`, 6 h; mandatory for single-step card orders |
| `payments.capture_authorized_payment` | `authorization_id: str` ｜ `pay_pal_request_id`, `prefer`, `body: CaptureRequest\|Dict` | `CapturedPayment` | `Error` [400,401,403,404,409,422] ｜ `RawError` [500, else] | 45 days; smoke: same key → same capture, HTTP 200 |
| `payments.reauthorize_payment` | `authorization_id: str` ｜ `pay_pal_request_id`, `prefer`, `body: ReauthorizeRequest\|Dict` | `PaymentAuthorization` | `Error` [400,401,403,404,422] ｜ `RawError` [500, else] | 45 days |
| `payments.void_payment` | `authorization_id: str` ｜ `pay_pal_request_id`, `prefer` | `PaymentAuthorization` | `Error` [401,403,404,409,422] ｜ `RawError` [500, else] | 45 days |
| `payments.get_authorized_payment` | `authorization_id: str` ｜ — | `PaymentAuthorization` | `Error` [401,403,404] ｜ `RawError` | read |
| `payments.refund_captured_payment` | `capture_id: str` ｜ `pay_pal_request_id`, `prefer`, `body: RefundRequest\|Dict` | `Refund` | `Error` [400,401,403,404,409,422] ｜ `RawError` [500, else] | 45 days |
| `vault.create_payment_token` | `body: PaymentTokenRequest\|Dict` ｜ `pay_pal_request_id` | `PaymentTokenResponse` | `Error` [400,403,404,422,500] ｜ `RawError` | 3 h |
| `vault.delete_payment_token` | `id: str` ｜ — | **`None`** (raw: `ApiResult[None, DeletePaymentTokenErrorBody]`) | `Error` [400,403,500] ｜ `RawError` [404, else] | none; smoke: repeat DELETE → 204 again |
| `transaction_search.search_transactions` | `start_date: str, end_date: str` ｜ `fields="transaction_info"`, `balance_affecting_records_only="Y"`, `page_size=100`, `page=1`, … | `SearchResponse` | **Case B**: always `RawError` | read. Dates RFC 3339 with seconds; **max range 31 days**; up to 3 h lag |

Auth failures (token fetch) raise `ApiError` with `.error` `OAuthProviderError | RawError` out of any
call, in both response modes → check first → 502 "PayPal refused our credentials".

Decode failures raise `pydantic.ValidationError`/`ValueError` in **both** modes. Smoke-observed: a
`404` from `vault.get_payment_token` has a body that does not decode as `Error` (a link without
`rel`) → `ValueError`. So `get_payment_token` is **not** used; an unreadable *error* body = rejected,
an unreadable *2xx* body = outcome unknown.

### Request models the code sets (required vs `UNSET`)

`Optional[T]` here is `T | UnsetType` — never pass `None`. Dict companions keyed by Python name.

| Model | Members set | Notes |
| --- | --- | --- |
| `OrderRequest` | `intent` (**req**, `CheckoutPaymentIntent.AUTHORIZE`), `purchase_units` (**req**, one), `payment_source` | |
| `PurchaseUnitRequest` | `amount` (**req**), `custom_id` (= order number), `invoice_id` (= `{prefix}-{order}-{attempt}`, unique per merchant), `description` | |
| `AmountWithBreakdown` / `Money` | `currency_code` (**req** str), `value` (**req** str, currency-exponent formatted) | |
| `PaymentSource` | `card: CardRequest` | |
| `CardRequest` | one-off: `name`, `number`, `expiry` (`YYYY-MM`), `security_code`, `billing_address`; saved: `vault_id` only | |
| `Address` | `country_code` (**req**), `address_line_1`, `address_line_2`, `admin_area_2` (city), `admin_area_1` (state), `postal_code` | |
| `CaptureRequest` | `amount`, `final_capture=True` | |
| `RefundRequest` | `amount` (omit for full refund — docstring: empty payload = full) | we always send the amount explicitly |
| `ReauthorizeRequest` | `amount` (the authorized total) | only member supported |
| `PaymentTokenRequest` | `payment_source` (**req**) → `card: PaymentTokenRequestCard` (`name`, `number`, `expiry`, `security_code`, `billing_address`); `customer: Customer` (`id`) when the shopper already has a PayPal customer id | |

### Response members read, and status → outcome maps

Every step's outcome comes from its own status enum by name (`paypal.models.enums`); anything
unlisted, or an absent status (`UNSET`) → `unknown` (never done).

| Step | Members asserted | Status enum → outcome |
| --- | --- | --- |
| authorize (`create_order`) | `Order.id`, `Order.status`, `purchase_units[0].payments.authorizations[0]` → `.id`, `.status`, `.amount.value/currency_code`, `.create_time`, `.expiration_time`; `payment_source.card.brand/last_digits` | `Order.status == PAYER_ACTION_REQUIRED` → `failed` (3-DS challenge: not supported, reported). Else `AuthorizationStatus`: `CREATED` → done · `PENDING` → pending · `DENIED` → failed · `VOIDED` → failed (undone) · `CAPTURED`, `PARTIALLY_CAPTURED` → unknown (not a fresh hold; operator review) · else/absent → unknown |
| reauthorize | `.id`, `.status`, `.amount`, `.create_time`, `.expiration_time` | same `AuthorizationStatus` map as authorize |
| capture | `.id`, `.status`, `.amount`, `.create_time`, `.seller_receivable_breakdown.gross_amount/paypal_fee/net_amount` | `CaptureStatus`: `COMPLETED` → done · `PENDING` → pending · `DECLINED`, `FAILED` → failed · `REFUNDED` → failed (undone) · `PARTIALLY_REFUNDED` → done (capture still in effect) · else → unknown |
| void (call-off, `cancel_outcome`) | `.status` | `VOIDED` → done · `DENIED` → done (nothing held) · `CAPTURED`, `PARTIALLY_CAPTURED` → failed (too late: money taken) · `CREATED`, `PENDING` → unknown · else → unknown |
| refund | `.id`, `.status`, `.amount`, `.create_time` | `RefundStatus`: `COMPLETED` → done · `PENDING` → pending · `FAILED`, `CANCELLED` → failed · else → unknown |
| save card (`create_payment_token`, raw) | HTTP status of `Success`, `.id`, `.customer.id`, `.payment_source.card.brand/last_digits/expiry/verification_status` | **no status enum on this response.** `Success` (200 replay / 201 created) with `payment_source.card` present → done, unless `CardVerificationStatus.FAILED` → failed; card absent or id absent → unknown |
| delete card (call-off, raw) | HTTP status | `Success` 204 → done · `Failure` 404 → done (gone) · other `Failure` 4xx → failed · 5xx → unknown |
| search | `total_pages`, `page`, `transaction_details[].transaction_info.transaction_id/transaction_event_code/transaction_initiation_date/transaction_amount/fee_amount/transaction_status/invoice_id/custom_field/paypal_reference_id` | read; `transaction_status` D/P/S/V reported verbatim |

## OPERATION OUTCOMES

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/orders/{id}/pay` → `orders.create_order` (single-step AUTHORIZE) | `Order.status`, then `authorizations[0].status` (`AuthorizationStatus`) | `CREATED` → done → 200 `authorized` · `PENDING` → pending → 202 · `DENIED` → failed → 402 `payment_declined` · `VOIDED` → failed → 402 · `PAYER_ACTION_REQUIRED` (order) → failed → 402 `payer_action_required` · `CAPTURED`/`PARTIALLY_CAPTURED`/unlisted/absent → unknown → 504 `outcome_unknown` · echoed amount ≠ order total → needs_review → 409 | `services.pay` → `safe_write.safe_write` with `gateway.read_authorize` + `gateway.authorize_outcome` (→ `gateway.authorization_outcome`); HTTP via `views.answer` |
| repeat of `/pay` (same attempt ref) | stored `PayPalWrite.outcome` | `sending` (in window) → 202 in progress · done/failed/needs_review → answered from the record · pending/unknown → same-key resend (≤ 6 h) then mapped as above; outside the key window stays unknown → 504 | `safe_write.safe_write` (lost-claim branch: `SEND_WINDOW`, `KEY_WINDOW`, pending `lookup=gateway.get_authorization`); ref from `safe_write.attempt_ref` |
| `POST /api/orders/{id}/fulfil` → `payments.reauthorize_payment` (only when stale) | `PaymentAuthorization.status` | `CREATED` → done → proceed to capture · `PENDING` → pending → 202 · `DENIED`/`VOIDED` → failed → 409 `authorization_not_renewable` with operator action · unlisted → unknown → 504 · 4xx refusal → 409 `authorization_not_renewable` + PayPal issue text | `services.fulfil` → `services.reauthorize` with `gateway.read_authorization` + `gateway.authorization_outcome`; refusal → `services.not_renewable` |
| `POST /api/orders/{id}/fulfil` → `payments.capture_authorized_payment` | `CapturedPayment.status` (`CaptureStatus`) | `COMPLETED` → done → 200 `captured` with amount/fee/net · `PENDING` → pending → 202 `capture_pending` · `DECLINED`/`FAILED`/`REFUNDED` → failed → 409 · `PARTIALLY_REFUNDED` → done · unlisted → unknown → 504 · echoed amount ≠ total → needs_review → 409 | `services.capture` with `gateway.read_capture` + `gateway.capture_outcome`; fee/net from `gateway.capture_breakdown` |
| `POST /api/orders/{id}/cancel` → `payments.void_payment` (call-off) | `PaymentAuthorization.status` via `cancel_outcome` | `VOIDED` → done → 200 `cancelled` · `DENIED` → done · `CAPTURED`/`PARTIALLY_CAPTURED` → failed → 409 `already_captured` (refund instead) · `CREATED`/`PENDING`/unlisted → unknown → 504 · 4xx refusal → looked up by authorization id and mapped by `cancel_outcome` | `services.cancel` with `gateway.void_outcome`; 4xx → `found` (inner fn: `gateway.get_authorization`) |
| `POST /api/orders/{id}/refunds` → `payments.refund_captured_payment` | `Refund.status` (`RefundStatus`) | `COMPLETED` → done → 201 `refunded` · `PENDING` → pending → 202 · `FAILED`/`CANCELLED` → failed → 409 (reservation released) · unlisted → unknown → 504 · echoed amount ≠ requested → needs_review → 409 | `services.refund` with `gateway.read_refund` + `gateway.refund_outcome` |
| `POST /api/payment-methods` → `vault.create_payment_token` | HTTP status of the raw `Success` + `payment_source.card.verification_status` | 200/201 with card and not `FAILED` → done → 201 · `CardVerificationStatus.FAILED` → failed → 402 `card_verification_failed` · card/id absent → unknown → 504 | `services.save_card` → `gateway.save_card` (raw) + `gateway.read_saved_card` + `gateway.save_card_outcome` |
| `DELETE /api/payment-methods/{id}` → `vault.delete_payment_token` (call-off) | HTTP status of the raw result | 204 → done → 200 `deleted` · 404 → done (gone) · other 4xx → failed → 409 (card stays) · 5xx/transport-after-send → unknown → 504 (card stays hidden and unusable; a repeat DELETE re-checks) | `services.delete_card` → `gateway.delete_card` (raw) + `gateway.read_deleted` + `gateway.delete_outcome` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| authorize (`create_order`) | `PayPalWrite` row, ref `{prefix}-auth-{order_number}-{attempt}` (attempt = count of definitively failed earlier attempts, `safe_write.attempt_ref`) | DB UNIQUE constraint on `PayPalWrite.ref` (INSERT → `IntegrityError`) | `try_claim` catches `IntegrityError` inside a savepoint → returns False → `safe_write` loads the existing record | `services.pay` → `safe_write.attempt_ref('auth', number)` → `safe_write.try_claim` (`PayPalWrite.ref` unique=True in `models.PayPalWrite`) |
| reauthorize | `PayPalWrite` row, ref `{prefix}-reauth-{order_number}-{authorization_id}-{attempt}` | same UNIQUE constraint | same `try_claim` | `services.reauthorize` → `attempt_ref('reauth', number, auth_id)` → `safe_write.try_claim` |
| capture | `PayPalWrite` row, ref `{prefix}-capture-{order_number}-{authorization_id}-{attempt}` | same UNIQUE constraint | same `try_claim` | `services.capture` → `attempt_ref('capture', number, auth_id)` → `safe_write.try_claim` |
| void | `PayPalWrite` row, ref `{prefix}-void-{order_number}-{authorization_id}-{attempt}` | same UNIQUE constraint | same `try_claim` | `services.cancel` → `attempt_ref('void', number, auth_id)` → `safe_write.try_claim` |
| refund | `PayPalWrite` row, ref `{prefix}-refund-{order_number}-{sha256(caller key)[:20]}`, created in the same DB transaction as the `PayPalRefund` reservation (payment row write-locked first) | same UNIQUE constraint, plus UNIQUE (`payment`, `idempotency_key`) on `PayPalRefund` | `reserve_refund` — same key returns the existing refund; same key + different amount → 422 | `services.refund` (reservation block) → `PayPalRefund.objects.create` + `safe_write.try_claim`; `models.PayPalRefund` constraint `paypal_refund_unique_key` |
| save card (`create_payment_token`) | `PayPalWrite` row, ref `{prefix}-card-{user_id}-{digest}-{attempt}`, digest = sha256 of caller `Idempotency-Key` if sent, else HMAC(SECRET_KEY, number‖expiry‖times this user deleted a card) | same UNIQUE constraint | same `try_claim` | `services.save_card` → `services.card_digest` + `attempt_ref('card', user, digest)` → `safe_write.try_claim` |
| delete card (`delete_payment_token`) | `PayPalWrite` row, ref `{prefix}-carddel-{saved_card_pk}-{attempt}` | same UNIQUE constraint | same `try_claim` | `services.delete_card` → `attempt_ref('carddel', card.pk)` → `safe_write.try_claim` |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| authorize | same-reference resend (`PayPal-Request-Id` dedup, kind 2) within 6 h; beyond it, stays unknown for operator | `{prefix}-auth-{order_number}-{attempt}` | `safe_write.safe_write` (`resending` path, `KEY_WINDOW[AUTHORIZE]`), `send` = `gateway.authorize(key, …)` |
| reauthorize | same-reference resend within 45 days | `{prefix}-reauth-{order_number}-{authorization_id}` | `safe_write.safe_write` (`resending`), `send` = `gateway.reauthorize(key, …)` |
| capture | same-reference resend within 45 days (smoke-verified: returns the original capture) | `{prefix}-capture-{order_number}-{authorization_id}` | `safe_write.safe_write` (`resending`), `send` = `gateway.capture(key, …)`; pending → `gateway.get_capture` |
| void | same-reference resend within 45 days; after a 4xx, lookup of the authorization by its own id (`get_authorized_payment`) | the record's own id (`authorization_id`) and `{prefix}-void-…` | `safe_write.safe_write` (`resending`), `send` = `gateway.void(key, …)`; `services.cancel.found` → `gateway.get_authorization` |
| refund | same-reference resend within 45 days | `{prefix}-refund-{order_number}-{key digest}` | `safe_write.safe_write` (`resending`), `send` = `gateway.refund(key, …)`; pending → `gateway.get_refund` |
| save card | same-reference resend within 3 h | `{prefix}-card-{user_id}-{digest}` | `safe_write.safe_write` (`resending`), `send` = `gateway.save_card(key, …)` |
| delete card | resend of DELETE by the token's own id (smoke-verified idempotent: 204 again) | the record's own id (`paypal_token_id`) | `safe_write.safe_write` (`resending`), `send` = `gateway.delete_card(token_id)` (404 → `Deleted(404)` = done) |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| authorize | Oscar `Order` (status `Pending`) + `OrderPayment` (`awaiting_payment`) + committed `PayPalWrite(ref, outcome=sending, amount, currency)` | `PayPalWrite` outcome/provider id/provider time; `OrderPayment` authorization id/status/times/card brand+last4, state `authorized`; Oscar `Source.allocate` + `Transaction`; Oscar status `Being processed` | `services.place_order` (Order + `OrderPayment`), `safe_write.try_claim` (committed: views are `transaction.non_atomic_requests` via `views.api`), then `services.pay.apply` |
| reauthorize | `OrderPayment` with the stale authorization + committed `PayPalWrite(sending)` | new authorization id/time on `OrderPayment`; `PayPalWrite` outcome | `safe_write.try_claim`, then `services.reauthorize.apply` |
| capture | `OrderPayment` (authorized) + committed `PayPalWrite(sending)` | capture id/status/amount/fee/net/time; state `captured`; Oscar `Source.debit`; Oscar status `Complete` | `safe_write.try_claim`, then `services.capture.apply` (`Source.debit`, `services.advance_order_status`) |
| void | `OrderPayment` (authorized) + committed `PayPalWrite(sending)` | state `voided`; Oscar status `Cancelled`; Oscar `Transaction` "Void" | `safe_write.try_claim`, then `services.cancel.apply` |
| refund | `PayPalRefund(reserved, amount, key)` + committed `PayPalWrite(sending)` | refund PayPal id/status/time; `OrderPayment.refunded_amount`, state; Oscar `Source.refund` | `services.refund` reservation block, then `services.refund.apply` (`Source.refund`) |
| save card | committed `PayPalWrite(sending)` keyed to the user (no card data stored) | `SavedCard(active, token id, customer id, brand, last4, expiry)` | `safe_write.try_claim(claim_fields={'user': …})`, then `services.save_card.apply` (`SavedCard.objects.update_or_create`) |
| delete card | `SavedCard` set to `deleting` (hidden, unusable) + committed `PayPalWrite(sending)` | `SavedCard` `deleted` on done; back to `active` on a definitive refusal | `services.delete_card` (state `deleting` block), `safe_write.try_claim`, then `services.delete_card.apply`; refusal restores `active` |

## Other behaviour (my calls)

- **No claim release; attempts instead.** A definitively failed write keeps its `failed` record (an
  operator can find it). The next legitimate attempt of that step gets a new reference: the base
  reference plus the number of earlier `failed` attempts (`safe_write.attempt_ref`). A repeat of an attempt
  that is still in flight or unknown derives the same reference and is caught by the claim. Refunds are the
  exception: a failed refund is final for its caller key (send a new key to try again).

- **Staleness at fulfil** (reauthorize docstring: 3-day honor period; reauthorize from day 4 to day 29;
  after 30 days a new authorization is required): age measured on PayPal's clock (authorization
  `create_time`). Age ≤ 3 days → capture directly. 3 < age ≤ 29 days → reauthorize once, then capture
  the new authorization. Age > 29 days, `expiration_time` passed, or reauthorization refused →
  409 `authorization_not_renewable` with "void it via cancel and ask the shopper to pay again".
  A capture refused with 4xx on an authorization older than the honor period triggers the
  same reauthorize-then-capture once.
- **Refund cap**: reservation under a write-lock on the payment row: sum of non-failed refunds +
  requested ≤ captured amount, else 422 `refund_exceeds_captured`. PayPal also enforces it
  (smoke: `REFUND_AMOUNT_EXCEEDED`).
- **Reconciliation**: splits `[from, to)` into ≤ 31-day windows, pages each to `total_pages`; filters on
  PayPal's `transaction_initiation_date`; local side = captures/refunds whose stored PayPal time is in
  the window; matches by transaction id, then by `invoice_id`/`custom_field` carrying our prefix;
  reports `matched`, `paypal_only`, `app_only`, `unsettled` (local writes in the window with no PayPal
  time: sending/pending/unknown).
- **Ownership**: every shopper endpoint filters by `request.user`; another shopper's order/card → 404.
  Operator endpoints require `is_staff` (401 anonymous, 403 non-staff).
- **Card data**: only passed through to PayPal in memory; never persisted, never logged; view functions
  handling it are `sensitive_variables`.

## Error boundary (python-error-handling)

`ProviderError(status_code, code, message, outcome_unknown)`; `provider_error(status, error)`:
`OAuthProviderError` or 401/403 (except where an op's 403 is caller-caused — the deleted-vault 403 on
`create_order` is mapped to 409 `payment_method_unusable`) → 502; 429 → 503; 4xx with `Error` → same
4xx family (422 → 402 `payment_declined` on authorize, else 409/422 with PayPal issue + description);
5xx → 502 with `outcome_unknown`. `NEVER_SENT` = `ConnectError, ConnectTimeout, PoolTimeout,
ProxyError` → 502 known; other `httpx.RequestError` → 504 unknown; `ValueError` on 2xx → 504 unknown.

## Tests

`sandbox/apps/paypal_payments/tests/` run with `sandbox/manage.py test apps.paypal_payments`, stub
transport via `custom_http_client` + stub token source; cover: status maps (refused + unlisted
values), double submit → one provider call with the derived `PayPal-Request-Id`, never-sent vs
read-timeout, refund cap, ownership, staleness/reauthorize, reconciliation paging and windows.
Type check: `mypy --strict` on the PayPal-facing modules.

## Assumptions & Blockers

- **No blockers.** Every capability is in the SDK: orders (single-step card authorize, vault_id), payments
  (capture, reauthorize, void, refund), vault (create/delete payment token), transaction search.
  Smoke on the sandbox: card authorize returned `COMPLETED`/`CREATED` without a payer-action challenge.
- Minor: sandbox catalogue prices are GBP stock records; per the task, the charge currency comes from
  `PAYPAL_CURRENCY` and the numeric price is used as-is.
- Minor: the host note calls the CSV import optional; on this machine `child_products.json` gives only
  11 products and `oscar_import_catalogue` supplies the other 198 (and `orders.json` needs them).
- Minor: reauthorization cannot be exercised live (sandbox rejects it inside 3 days:
  `REAUTHORIZATION_TOO_SOON`); covered by stubbed tests.

## REQUIRED READING

| Hazard | Pointer |
| --- | --- |
| Error boundary, decode failures, transport split, token-fetch failures | MUST load `python-error-handling` (loaded) |
| Client construction, lifetime, transport wrapping | MUST load `python-client-initialization` (loaded) |
| Safe write, no retries, base URL, logging transport, reconciliation | MUST load `python-configuration-resilience` (loaded) |
| Call shapes, `prefer` default, status → outcome, `answer` | MUST load `python-calling-endpoints` (loaded) |
| `UNSET` vs `None`, open enums, money as `Decimal` strings | MUST load `python-models` (loaded) |
| OAuth2 client credentials, failed token fetch | MUST load `python-authentication` (loaded) |
| Stub transport, both transport failures, double-submit test | MUST load `python-testing` (loaded) |
