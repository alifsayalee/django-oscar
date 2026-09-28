# PayPal payments + saved cards for the django-oscar sandbox — plan & contract sheet

## Scope

A new Django app `sandbox/apps/payments_api` (app label `payments_api`) exposes the JSON API under
`/api/` in `sandbox/urls.py`. It reuses Oscar's `order.Order`/`order.Line` (created by
`OrderCreator.place_order` from a basket that is frozen, submitted and never shown in the UI) and
Oscar's `payment.Source` / `payment.SourceType` / `payment.Transaction`. Its own models hold PayPal
state (hold, capture, refunds, saved cards) and the claim store for provider writes.

| Endpoint | Who | What it does |
| --- | --- | --- |
| `POST /api/orders` | shopper | Oscar order from `{items:[{productId, quantity}], shippingAddress?}`. Status `Pending`, payment `awaiting_payment` |
| `POST /api/orders/{orderId}/pay` | shopper (owner) | `{card:{…}}` or `{paymentMethodId}`. Single-step PayPal create-order with intent AUTHORIZE (puts a hold on the money) |
| `POST /api/orders/{orderId}/fulfil` | staff | Reauthorizes if the hold is past its 3-day honor period, then captures the full hold and records gross/fee/net |
| `POST /api/orders/{orderId}/cancel` | staff | Voids the hold (or just cancels if it was never paid) |
| `POST /api/orders/{orderId}/refunds` | shopper (owner) or staff | `{amount?, idempotencyKey}` (or an `Idempotency-Key` header). Full or partial refund of the capture |
| `GET /api/my-orders` | shopper | Your own orders, with their payment state |
| `POST/GET /api/payment-methods`, `DELETE /api/payment-methods/{id}` | shopper | Vault a card; list your saved cards; delete one |
| `GET /api/reconciliation?from&to` | staff | PayPal transaction search over the whole range (31-day windows × every page), matched against local captures and refunds |

Authentication uses Django session login (`request.user`). Callers who are not signed in get `401`,
and non-staff callers of an operator endpoint get `403`. Another shopper's objects answer `404`.
Refunds are an operator action in practice, but the task leaves them unrestricted, so the rule is
"owner or staff".

## Repo survey (conventions → exemplar)

| Convention | Exemplar |
| --- | --- |
| Sandbox apps live under `sandbox/apps/`, imported as `apps.<name>` (sandbox dir on sys.path) | `sandbox/apps/offers.py`, `sandbox/apps/sitemaps.py` |
| URL wiring: plain `path()` entries in `urlpatterns` before the `i18n_patterns` block | `sandbox/urls.py` |
| Settings via `django-environ` (`env.str`, `env.bool`) | `sandbox/settings.py` |
| Oscar models via `get_model` / `get_class` | `src/oscar/apps/checkout/mixins.py` |
| Django is **sync** (WSGI, `sandbox/wsgi.py`); `DATABASES['default']['ATOMIC_REQUESTS'] = True` | `sandbox/settings.py` |
| Tests: Django `TestCase`, `DiscoverRunner` (`TEST_RUNNER` in sandbox settings), run with `manage.py test` from `sandbox/` | `tests/integration/order/test_models.py` |

The toolchain is pip + venv (`venv/`, Python 3.11, `pip install -e .[test]`). The SDK installs from
git as the distribution **`paypal`**, pinned to commit `0ed3d22071e97650113c99fd82c42bc815f99dcf` in
`sandbox/requirements.txt`. The project has no type checker configured, so `mypy --strict` goes into
the venv and runs over `sandbox/apps/payments_api/paypal_gateway/`. Oscar ships no stubs, so the Django
layer is checked non-strict.

Baseline on the untouched tree: `pytest tests/integration/order tests/integration/payment` needs
PostgreSQL by default. With `DATABASE_ENGINE=django.db.backends.sqlite3` it gives 138 passed and 3
failed. The failures are `TestConcurrentOrderPlacement` ×2 and
`LineTests::test_shipping_status_after_two_full_line_events`; they already existed and expect
PostgreSQL. The app's own suite runs with `cd sandbox && ../venv/Scripts/python manage.py test
apps.payments_api`.

Sandbox build note, observed: after `loaddata child_products.json`, only 11 products existed and
`loaddata pages/ranges/offers/orders` failed on foreign keys. Running `oscar_import_catalogue
fixtures/*.csv` next gave 209 products, and the remaining fixtures then loaded.

**SDK identity drift, verified against the installed package and map (these override the skill
page):** distribution `paypal`, import root `paypal`, clients `PaypalClient` / `AsyncPaypalClient`
(aliases `Client` / `AsyncClient`), core `paypal.core`, models `paypal.models`, enums
`paypal.models.enums`, errors `paypal.errors`. The map is `sdk-map.md` + `map/operations/*.md` from
the clone at the pinned commit.

## Sync vs async, client lifetime, host

- **Sync `PaypalClient`**, because Django runs under WSGI. The two clients do not mix.
- One lazily built, module-level client per process, built on first use and therefore after any
  fork. `atexit` closes it (`client.close()`).
- Transport: `LoggingTransport(HttpxClient(timeout=PAYPAL_TIMEOUT))` passed as `custom_http_client`.
  The timeout lives on the transport because the client's `timeout=` does not reach a custom
  transport. The log carries only method, URL path and status, never headers or bodies.
- **Host**: `PAYPAL_BASE_URL` when set, verbatim; this moves the token fetch too. Otherwise
  `{"sandbox": "https://api-m.sandbox.paypal.com"}[PAYPAL_ENVIRONMENT]`. The SDK declares only the
  sandbox host (map: *Servers & auth*). Any other environment without `PAYPAL_BASE_URL` raises
  `PayPalConfigurationError` (the API answers 503 `paypal_not_configured`), so nothing falls silently
  to the default. (`PayPalConfig.resolved_base_url`)
- Auth: `oauth2=ClientCredentials(client_id=settings.PAYPAL_CLIENT_ID, client_secret=…)`. An empty id,
  secret or currency raises `PayPalConfigurationError` before any call (`PayPalConfig.validate`).
  Omitting `oauth2` would send unauthenticated requests.
- **No blind retries on writes.** A write that may have landed is settled only by a resend under the
  **same** `PayPal-Request-Id`. That happens up to 2 times inside the same request after a 409/5xx
  (observed live: PayPal answers 409 to concurrent refunds of one capture), and on the caller's next
  repeat. Reads (authorization/capture/refund/token gets, transaction search) retry at most 3 times
  with backoff on never-sent/timeout/429/5xx (`errors.guarded_read`).

## Contract sheet

All operations are on the sync client. Everything after `*` is keyword-only with a real default, so
no defensive `None`s. Every call passes `request_options` only where a per-call timeout is needed.

| Operation | Positional | Keyword-only used | Parsed return | `ApiError.error` union (statuses) | Source |
| --- | --- | --- | --- | --- | --- |
| `client.orders.create_order` | `body: OrderRequest` | `pay_pal_request_id` (header `PayPal-Request-Id`), `prefer="return=representation"` | `Order` | `CreateOrderErrorBody = Error [400,401,422] \| RawError` | map/operations/orders.md |
| `client.payments.get_authorized_payment` | `authorization_id` | — | `PaymentAuthorization` | `Error [401,403,404] \| RawError [500,…]` | payments.md |
| `client.payments.reauthorize_payment` | `authorization_id` | `pay_pal_request_id` (kept 45 days), `prefer`, `body: ReauthorizeRequest` (only `amount`) | `PaymentAuthorization` | `Error [400,401,403,404,422] \| RawError [500,…]` | payments.md |
| `client.payments.capture_authorized_payment` | `authorization_id` | `pay_pal_request_id` (kept 45 days), `prefer`, `body: CaptureRequest` | `CapturedPayment` | `Error [400,401,403,404,409,422] \| RawError [500,…]` | payments.md |
| `client.payments.get_captured_payment` | `capture_id` | — | `CapturedPayment` | `Error [401,403,404] \| RawError [500,…]` | payments.md |
| `client.payments.void_payment` | `authorization_id` | `pay_pal_request_id` (kept 45 days), `prefer` | `PaymentAuthorization` | `Error [401,403,404,409,422] \| RawError [500,…]` | payments.md |
| `client.payments.refund_captured_payment` | `capture_id` | `pay_pal_request_id` (kept 45 days), `prefer`, `body: RefundRequest` | `Refund` | `Error [400,401,403,404,409,422] \| RawError [500,…]` | payments.md |
| `client.payments.get_refund` | `refund_id` | — | `Refund` | `Error [401,403,404] \| RawError [500,…]` | payments.md |
| `client.vault.create_payment_token` | `body: PaymentTokenRequest` | `pay_pal_request_id` (kept 3 hours) | `PaymentTokenResponse` | `Error [400,403,404,422,500] \| RawError` | vault.md |
| `client.vault.get_payment_token` | `id` | — | `PaymentTokenResponse` | `Error [403,404,422,500] \| RawError` | vault.md |
| `client.vault.delete_payment_token` | `id` | — | **`None`** → use `with_raw_response` (`ApiResult[None, …]`) | `Error [400,403,500] \| RawError` | vault.md |
| `client.transaction_search.search_transactions` | `start_date: str`, `end_date: str` (RFC 3339, seconds required, **≤31-day range**) | `page_size=100`, `page` (1-based), `fields="transaction_info"`, `balance_affecting_records_only="Y"` | `SearchResponse` | **Case B: `RawError` only** | transaction_search.md |

Failures that are not `ApiError`:
- A failed token fetch raises `ApiError` with `.error` of type `OAuthProviderError | RawError`
  (`paypal.core`). This happens in raw mode too, and nothing was sent → 502.
- A decode failure raises `pydantic.ValidationError` or `ValueError` in both modes. On a 2xx the
  outcome is unknown.
- `httpx` exceptions arrive unwrapped. `ConnectError`, `ConnectTimeout`, `PoolTimeout` and
  `ProxyError` mean never sent. Any other `httpx.RequestError` means the request may have landed.

Only one 2xx return type has a required member: `SellerReceivableBreakdown.gross_amount: Money`.
Everything else is `Optional[T] = UNSET` (`T | UnsetType`, never `None`). A truncated body therefore
decodes cleanly, so each row below names the members the code has to assert on.

### Request members set (required vs UNSET)

| Model (`paypal.models`) | Members set | Notes |
| --- | --- | --- |
| `OrderRequest` | `intent` (required, `CheckoutPaymentIntent.AUTHORIZE`), `purchase_units` (required), `payment_source` | |
| `PurchaseUnitRequest` | `amount: AmountWithBreakdown` (required), `reference_id`, `custom_id`, `invoice_id` | `invoice_id` = the pay-attempt reference, `custom_id` = `{prefix}-{order.number}` (reconciliation key). Both always set |
| `AmountWithBreakdown` / `Money` | `currency_code: str`, `value: str` (both required) | value = `Decimal` quantized to the currency exponent |
| `PaymentSource` | `card: CardRequest` | |
| `CardRequest` | one-off: `number`, `expiry` (`YYYY-MM`), `security_code`, `name`, `billing_address`. Saved: `vault_id` | |
| `Address` | `country_code` (required), `address_line_1`, `address_line_2`, `admin_area_2`, `admin_area_1`, `postal_code` | |
| `CaptureRequest` | `amount: Money`, `final_capture=True` | |
| `ReauthorizeRequest` | `amount: Money` | |
| `RefundRequest` | `amount: Money`, `custom_id`, `invoice_id`, `note_to_payer` | `invoice_id` = the refund reference (unique per refund) |
| `PaymentTokenRequest` | `payment_source: PaymentTokenRequestPaymentSource` (required) → `card: PaymentTokenRequestCard` (`number`, `expiry`, `security_code`, `name`, `billing_address`); `customer: Customer(merchant_customer_id=…)` | |

Wire aliases that matter: `CardPaymentTokenEntity.type_` / `CardResponse.type_` ↔ `"type"`, and
`Token.type_` ↔ `"type"` (not used). None of the members this app sets have aliases. No member the
app sets is `Optional[Any]`.

### Response members read, status enums and outcomes

| Response | Members asserted | Status enum → outcome |
| --- | --- | --- |
| `Order` (create) | `id`, `status`, `purchase_units[0].payments.authorizations[0]` (`id`, `status`, `amount`, `create_time`, `expiration_time`) | `OrderStatus`: `COMPLETED` → go on to read the authorization. `PAYER_ACTION_REQUIRED` → **stop: a browser challenge is required** (failed, reported, never built). `CREATED`/`SAVED`/`APPROVED` → pending (no authorization yet). `VOIDED` → failed. Anything else → unknown |
| `AuthorizationWithAdditionalData` / `PaymentAuthorization` (pay, reauthorize) | `id`, `status`, `amount.value/currency_code`, `create_time`, `expiration_time` | `AuthorizationStatus`: `CREATED` → **done** (hold in place). `PENDING` → pending. `DENIED` → failed. `VOIDED` → failed (undone). `CAPTURED`/`PARTIALLY_CAPTURED` → unknown for a hold step (not what was asked, operator review). Any other value or UNSET → unknown |
| `PaymentAuthorization` (void) | `id`, `status` | cancel mapper: `VOIDED` → **done**. `CAPTURED`/`PARTIALLY_CAPTURED` → failed (too late). Else → unknown |
| `CapturedPayment` (capture) | `id`, `status`, `amount`, `seller_receivable_breakdown.gross_amount/paypal_fee/net_amount`, `create_time` | `CaptureStatus`: `COMPLETED` → **done**. `PENDING` → pending (`status_details.reason` shown). `DECLINED`/`FAILED` → failed. `PARTIALLY_REFUNDED`/`REFUNDED` → failed for the capture step itself, but when the capture is re-read after refunds they mean the capture landed (see refund bookkeeping). Else → unknown |
| `Refund` | `id`, `status`, `amount`, `create_time` | `RefundStatus`: `COMPLETED` → **done**. `PENDING` → pending. `FAILED`/`CANCELLED` → failed. Else → unknown |
| `PaymentTokenResponse` | `id`, `payment_source.card.(brand, last_digits, expiry)`, `customer.id` | **The model declares no status member.** A payment token is by definition the vaulted instrument. Done iff `id` and `payment_source.card` are both set. Otherwise unknown |
| `delete_payment_token` raw `Success` | `response.status_code` | 2xx → done. A 404 `Failure` → done (already gone). Anything else → the failure mapping |
| `SearchResponse` | `transaction_details[].transaction_info.(transaction_id, transaction_event_code, transaction_status, transaction_initiation_date, transaction_amount, fee_amount, custom_field, invoice_id, paypal_reference_id)`, `total_pages`, `page`, `last_refreshed_datetime` | read-only. `transaction_status` letters (D/P/S/V per the docstring) are shown verbatim |

Facts observed in the sandbox smoke run (scratchpad, 2026-09-28):
- A single-step create with a card and intent AUTHORIZE returns `Order.status == COMPLETED` with an
  authorization in state `CREATED`, so no `authorize_order` call is needed.
- A repeat under the same `PayPal-Request-Id` returns the same order id, so the resend is a safe
  lookup.
- Over-refunding gives a 422 `Error` with issue `REFUND_AMOUNT_EXCEEDED`. Voiding after a capture
  gives a 422 with `PREVIOUSLY_CAPTURED`.
- Paying with a deleted vault id gives a 403 `RawError`.
- The account is shared with other installs, since transaction search shows foreign `custom_field`s.
  This is why every reference carries a per-install prefix.

### Authorization staleness (from the `reauthorize_payment` docstring)

- Honor period: 3 days.
- Reauthorization is allowed after the honor period, within 29 days of the original authorization.
- From 30 days, a new authorization is needed.

The fulfil flow reads the hold with `get_authorized_payment`. If the provider's `create_time` is more
than 3 days old, it reauthorizes the hold under its own claim before capturing. It reauthorizes the
current (newest) hold, carries the original authorization time forward, and captures the new id.

A reauthorize refusal, or an original authorization 29 or more days old, answers `409` with
`code: "authorization_expired"`. The message tells the operator the hold can no longer be renewed:
the shopper must pay again (`POST /pay`), or the order should be cancelled. The payment goes back to
`awaiting_payment` with a new attempt number.

## OPERATION OUTCOMES

Mappers live in `paypal_gateway/outcomes.py`. The outcome becomes the caller's HTTP status only in
the flow function named in each row; success comes from `done` alone.

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /pay` → `orders.create_order` (hold) | `purchase_units[0].payments.authorizations[0].status` (`AuthorizationStatus`). With no authorization, the PayPal `Order.status` (`OrderStatus`, wrapped in `OrderWithoutHold`, because both enums share wire values) | CREATED → done → 200 `payment.status: authorized`. PENDING → pending → 202 `authorization_pending`. DENIED/VOIDED → failed → 402 `payment_declined`, the attempt moves on. CAPTURED/PARTIALLY_CAPTURED/unlisted/UNSET → unknown → 504 `outcome_unknown`. Order CREATED/SAVED/APPROVED → pending → 202. Order `PAYER_ACTION_REQUIRED` → failed → 402 `payer_action_required` (reported, not built). Order VOIDED → failed → 402. Echoed amount ≠ total → needs_review → 409. Claim in flight elsewhere → 202 `inProgress` | `outcomes.pay_outcome` / `hold_outcome` / `order_without_hold_outcome`, read by `operations.read_hold`, answered in `services.pay` |
| `POST /fulfil` → `payments.reauthorize_payment` | `PaymentAuthorization.status` | CREATED → done (the capture follows on the new id). PENDING → pending → 409 `reauthorization_pending` (retry fulfil). DENIED/VOIDED or a 4xx refusal → failed → 409 `authorization_expired`, which tells the operator to have the shopper pay again or cancel. Unlisted/UNSET → unknown → 504 | `outcomes.hold_outcome`, `operations.read_authorization`, `services._reauthorize` / `services._hold_gone` |
| `POST /fulfil` → `payments.capture_authorized_payment` | `CapturedPayment.status` (`CaptureStatus`) | COMPLETED → done → 200 with `capture.capturedAmount/paypalFee/netAmount`. PENDING → pending → 202 `capture_pending` (the next fulfil re-reads it). DECLINED/FAILED → failed → 409 `capture_failed`. REFUNDED/PARTIALLY_REFUNDED → failed for this step, but when found on a repeat the money did land, so it is booked as captured (`captured_money_landed`). Unlisted/UNSET → unknown → 504. Amount mismatch → needs_review → 409 | `outcomes.capture_outcome`, `operations.read_capture`, `services._capture` / `services._apply_capture` |
| `POST /cancel` → `payments.void_payment` | `PaymentAuthorization.status` | VOIDED → done → 200 order `Cancelled`, payment `voided`. CAPTURED/PARTIALLY_CAPTURED (or a 422 `PREVIOUSLY_CAPTURED`) → failed → 409 `already_captured`. Unlisted/UNSET → unknown → 504. Never paid → no write, 200 `cancelled` | `outcomes.void_outcome`, `operations.read_void`, `services.cancel` |
| `POST /refunds` → `payments.refund_captured_payment` | `Refund.status` (`RefundStatus`) | COMPLETED → done → 201 `refundId`. PENDING → pending → 202 `refundId`. FAILED/CANCELLED or a 4xx refusal → failed → 409 `refund_failed`, reservation released. Unlisted/UNSET → unknown → 504 `outcomeUnknown` + `refundId`, reservation kept. Amount mismatch → needs_review → 409 | `outcomes.refund_outcome`, `operations.read_refund`, `services._send_refund` / `services._finish_refund` / `services._refund_result` |
| `POST /payment-methods` → `vault.create_payment_token` | none declared; `read_vault` passes `True` iff `id` and `payment_source.card` are set | True → done → 201 `paymentMethodId` (200 on a repeat). Anything else → unknown → 504. Claim in flight → 409 `in_progress` | `outcomes.vault_outcome`, `operations.read_vault` / `operations.vaulted_card_of`, `services.save_card` |
| `DELETE /payment-methods/{id}` → `vault.delete_payment_token` | HTTP status of the raw `Success`/`Failure` | 2xx or 404 → done → 204. Another 4xx → failed → card back to `active`, mapped error. 5xx/timeout → unknown → 504, and the card stays `deleting` (hidden, unusable) until a repeat DELETE settles it | `operations.delete_token`, `services.delete_card` |

## DUPLICATE CLAIMS

The claim store is the `ProviderWrite` table (`reference` is `unique=True`). `try_claim` is a plain
`INSERT` committed in its own transaction, outside `ATOMIC_REQUESTS` (the API views are
`transaction.non_atomic_requests`). The database's unique index rejects the second one
(`IntegrityError`). Every reference is `{install_prefix}:{…}`, where the install prefix is a random
id persisted once per database (`InstallIdentity`, `claims.install_prefix`), or the
`PAYPAL_REFERENCE_PREFIX` setting. The same reference goes to PayPal as `PayPal-Request-Id`.

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| hold (`create_order`) | `ProviderWrite(reference="{p}:o{number}:pay:{attempt}")`. `attempt` = `PayPalPayment.attempt`, moved on only by a conditional UPDATE when an attempt definitively failed | unique index on `ProviderWrite.reference` | `DatabaseClaimStore.try_claim` (`IntegrityError` → False) → `safe_write` answers from `load_existing` (in flight → 202, settled → its outcome) | `services.pay` → `safe_write.safe_write`; `services._fail_attempt` |
| reauthorize | `{p}:o{number}:reauth:{authorization_id}` | same | same | `services._reauthorize` |
| capture | `{p}:o{number}:capture:{authorization_id}` | same | same | `services._capture` |
| void | `{p}:o{number}:void:{authorization_id}` | same | same | `services.cancel` |
| refund | `PayPalRefund` row, unique `(payment, idempotency_key)` (`paypal_refund_key_unique`), plus `ProviderWrite` `{p}:o{number}:refund:{sha256(key)[:20]}`. Over-refunding is blocked by a conditional `UPDATE … SET refund_reserved = refund_reserved + x WHERE refund_reserved <= captured_amount - x`, with a DB check constraint as a backstop | the unique constraints, and the conditional UPDATE's row count | `services.refund` (`IntegrityError` → `_repeat_refund`; 0 rows → 409 `exceeds_refundable`) | `services.refund` / `services._send_refund` |
| vault card | `{p}:u{user_id}:card:{hmac_sha256(SECRET_KEY, number, expiry)[:24]}:{generation}` (`generation` = the count of this user's deleted cards with that fingerprint) | unique index on `ProviderWrite.reference` (and `SavedCard.reference`) | `try_claim` → existing record → the same saved card (200) | `services.save_card`, `services._fingerprint` |
| delete card | conditional `UPDATE SavedCard SET status='deleting' WHERE status='active'` | the card leaves `active` for every later request; a delete is idempotent at PayPal (404 = gone) | `services.delete_card` | `services.delete_card` |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| hold (`create_order`) | Same-reference resend: PayPal de-duplicates `PayPal-Request-Id`, and a repeat was observed returning the original order. This happens on the caller's repeat `/pay`, within the 6-hour retention; after that the record stays `unknown` for an operator | `{p}:o{number}:pay:{attempt}` | `safe_write.safe_write` (the `checking`/`resending` branch, `resend_window=REQUEST_ID_RETENTION["pay"]`) |
| reauthorize | same-reference resend (kept 45 days); in-request re-check on a 409/5xx | `{p}:o{number}:reauth:{auth_id}` | `safe_write.safe_write` via `services._reauthorize` |
| capture | Same-reference resend (kept 45 days); in-request re-check on a 409/5xx. `fulfil` also re-reads the authorization first, and a CAPTURED authorization goes on to the capture step, whose resend returns the original capture | `{p}:o{number}:capture:{auth_id}` | `safe_write.safe_write` via `services._capture` |
| void | Same-reference resend (kept 45 days). `find` = `get_authorized_payment` of the same authorization | `{p}:o{number}:void:{auth_id}` | `safe_write.safe_write` via `services.cancel` (`find=`) |
| refund | Same-reference resend (kept 45 days) on the caller's repeat with the same idempotency key, and in-request after a 409/5xx. Verified live: an `unknown` refund settled on a same-key repeat | `{p}:o{number}:refund:{keyhash}` | `safe_write.safe_write` via `services._repeat_refund` → `services._send_refund` |
| vault card | same-reference resend (kept 3 hours) on a repeat POST of the same card | `{p}:u{uid}:card:{fp}:{gen}` | `safe_write.safe_write` via `services.save_card` |
| delete card | repeat DELETE re-sends the delete of the same token (404 → done) | the PayPal token id | `services.delete_card` → `operations.delete_token` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| hold | `PayPalPayment` (order, amount, currency, attempt) from `place_order`, plus `ProviderWrite(outcome="sending")` under the reference | Authorization id/status/create/expiry, PayPal order id, card brand + last 4, `ProviderWrite` outcome + provider id + provider time, Oscar `Source.allocate` (`Transaction 'Authorise'`), order → `Being processed` | `services.place_order`, `DatabaseClaimStore.try_claim` → `DatabaseClaimStore.complete`, `services._apply_hold` |
| reauthorize | `ProviderWrite(sending)` for `reauth:{auth_id}` | new authorization id/status/renewed/expiry, `Transaction 'Reauthorise'` | `services._reauthorize` |
| capture | `ProviderWrite(sending)` for `capture:{auth_id}` | capture id/status/amount/fee/net/time, `Source.debit` (`Transaction 'Debit'`), order → `Complete` (once, by conditional update) | `services._apply_capture` |
| void | `ProviderWrite(sending)` for `void:{auth_id}` | payment `voided`, `Transaction 'Void'`, order → `Cancelled` | `services.cancel` |
| refund | `PayPalRefund(status="sending", key, amount)` + `refund_reserved` + `ProviderWrite(sending)` | refund id/PayPal status/time. When done: `refunded_amount` and `Source.refund` (`Transaction 'Refund'`). When failed: the reservation is released | `services.refund`, `services._apply_refund`, `services._finish_refund` |
| vault card | `ProviderWrite(sending)` with the fingerprint reference (no card data) | `SavedCard(paypal_token_id, vault_customer_id, brand, last_digits, expiry, fingerprint, reference)` | `services.save_card` |
| delete card | `SavedCard.status = 'deleting'` (already hidden and unusable) | `status='deleted'` + `deleted_at`, or back to `active` on a definite refusal | `services.delete_card` |

## Money

- `Decimal` everywhere. The wire string comes from `to_minor_string(amount, currency)`, which uses
  ISO-4217 exponents (the table from `python-models`, with 2 as the default).
- The amount to hold is `Order.total_incl_tax`, which Oscar computes from catalogue prices with
  offers applied and Free shipping. The currency is `settings.PAYPAL_CURRENCY`, which is also stored
  on the order.
- Every echoed amount is compared as `(Decimal, currency)`.

## Security

- Card numbers and CVCs exist only in the request body in memory and in the SDK request. They are
  never in the DB, and never in logs: the transport logs no bodies, and the app never logs the card
  payload or a `str()` of an input `ValidationError`.
- Card input is validated by our own code before any SDK model is built, so pydantic errors cannot
  echo a PAN.
- The saved-card fingerprint is an HMAC, not the PAN.
- The API views are CSRF-exempt JSON endpoints that require a session login. Session-cookie CSRF is
  a real risk for state-changing calls. Mitigation: requests must be `Content-Type:
  application/json` (browsers cannot send that cross-site without a CORS preflight, and no CORS is
  enabled).

## Assumptions & Blockers

- **No blockers.** The DB (SQLite by default, any Django DB) holds claims in a unique-indexed table
  that outlives the process.
- Minor assumption: the catalogue prices are GBP-denominated numbers, but the task mandates the
  currency from `PAYPAL_CURRENCY`. The price number is used as-is in that currency, with no FX
  conversion.
- Minor assumption: any `PAYPAL_ENVIRONMENT` other than `sandbox` needs `PAYPAL_BASE_URL`, because
  the SDK declares no other host.
- Minor assumption: a card challenge (`PAYER_ACTION_REQUIRED`) is reported as `402
  payer_action_required` and not built further, per the task.

## REQUIRED READING

- Client construction, lifetime, custom transport — MUST load `python-client-initialization` (loaded)
- OAuth2 credentials, token-fetch failure payload — MUST load `python-authentication` (loaded)
- Keyword-only calls, `with_raw_response` for `delete_payment_token`, `prefer` default narrowing the
  response — MUST load `python-calling-endpoints` (loaded)
- UNSET vs None, open enums, `to_dict` — MUST load `python-models` (loaded)
- `ApiError` ladder, `OAuthProviderError`, decode/transport failures — MUST load `python-error-handling` (loaded)
- Safe write, no retries, reconciliation on the provider clock — MUST load `python-configuration-resilience` (loaded)
- Stub transport tests — MUST load `python-testing` (loaded)
