# PayPal Server SDK integration plan — django-oscar sandbox

Scope: add PayPal card payments (authorize → capture at fulfilment → void/refund), saved cards
(PayPal vault) and a reconciliation report to the runnable `sandbox/` site as a new Django app
`sandbox/apps/paypal_payments`, exposed under `/api/`.

## SDK identity (drift from the skill docs — verified against the installed package)

| Fact | Value | Source |
| --- | --- | --- |
| Distribution | `paypal` 2.29 (NOT `pay-pal-server-sdk`) — installed with `pip install "paypal @ git+https://github.com/context-plugins/paypal-python-sdk.git@main"` | `pyproject.toml` of the SDK repo |
| Import root | `paypal` (NOT `pay_pal_server_sdk`) | `sdk-map.md` |
| Sync client | `paypal.PaypalClient` (alias `Client`) | `sdk-map.md` "Getting a client" |
| Constructor | keyword-only: `base_url: str \| None`, `timeout: float = 30.0`, `custom_http_client: HttpClient \| None`, `oauth2: ClientCredentialsOrDict \| None`, `oauth2_token_source` | `sdk-map.md` |
| Base URL default | `https://api-m.sandbox.paypal.com` — the SDK's only declared server; token endpoint `/v1/oauth2/token` moves with `base_url` | `sdk-map.md` "Servers & auth" |
| Core imports | `ApiError`, `RawError`, `Success`, `Failure`, `ClientCredentials`, `HttpxClient`, `HttpRequest`, `HttpResponse`, `OAuthProviderError`, `UNSET`, `UnsetType` from `paypal.core` | `paypal/core/__init__.py` `__all__` |
| Enums | `OrderStatus`, `AuthorizationStatus`, `CaptureStatus`, `RefundStatus`, `CheckoutPaymentIntent` from `paypal.models.enums` | `paypal/models/enums/__init__.py` |
| Error model | `paypal.models.Error`: `name: str`, `message: str`, `debug_id: str`, `details: Optional[list[ErrorDetails]]`; `ErrorDetails.issue: str`, `description: Optional[str]` | `models/error.py`, `models/error_details.py` |

## Decisions

- **Sync client.** Django under WSGI (`sandbox/wsgi.py`, `manage.py runserver`) — no async views anywhere in
  the sandbox. One lazily-built, module-level `PaypalClient` per process (built after any fork, on first use),
  closed from `atexit`. Never one per request.
- **Host selection.** `PAYPAL_BASE_URL` set → used verbatim for every call incl. token. Otherwise
  `PAYPAL_ENVIRONMENT` is resolved through an explicit map `{"sandbox": "https://api-m.sandbox.paypal.com"}`;
  any other value without `PAYPAL_BASE_URL` raises `ImproperlyConfigured` (the plugin documents no other host,
  so no production host is hard-coded from memory). `base_url` is always passed explicitly.
- **Credentials.** `oauth2=ClientCredentials(client_id=settings.PAYPAL_CLIENT_ID, client_secret=settings.PAYPAL_CLIENT_SECRET)`;
  empty values → `ImproperlyConfigured` before the first call (an omitted `oauth2` would silently send unauthenticated requests).
- **Transport.** `custom_http_client=LoggingTransport(HttpxClient(timeout=20.0))` — logs method, URL, status and
  ms only; never headers or bodies (card data travels in bodies). Timeout set on the transport because the
  client's `timeout=` does not reach a supplied transport.
- **Retries.** SDK does none. Reads (`get_*`, `search_transactions`) get a small bounded retry on
  never-sent transport errors / 429 / 5xx. Writes are never blindly retried: every write goes through
  `safe_write` (claim → call → check → verify → complete); a repeat request settles an unknown by the
  same-`PayPal-Request-Id` resend (kind 2) inside the documented retention window, else stays `unknown`.
- **Claim store.** Existing SQLite/Django ORM DB. New model `ProviderWrite` with a `unique=True` `ref`
  column: `try_claim` = `INSERT` inside its own `transaction.atomic()`, `IntegrityError` → lost claim. Views
  are `transaction.non_atomic_requests` (the sandbox sets `ATOMIC_REQUESTS=True`, which would hold the
  claim uncommitted during the provider call and roll it back on error).
- **References.** `ref = f"{PAYPAL_REFERENCE_PREFIX}-{install_id}:{scope}:{step}"`; `install_id` is a random
  id persisted once per database (singleton `Installation` row) so a rebuilt sandbox DB never reuses an
  earlier DB's PayPal-Request-Ids. The ref is sent as `PayPal-Request-Id`. Caller-supplied idempotency keys
  are hashed (sha256, 24 hex) into the ref.
- **Oscar reuse.** Orders/lines: Oscar `Order`/`Line` created through Oscar's `Basket` + `OrderCreator` +
  `OrderTotalCalculator` + shipping `Repository`. Payment ledger: Oscar `payment.Source` (source type
  "PayPal", `reference` = PayPal order id) with `allocate`/`debit`/`refund` → Oscar `payment.Transaction`
  rows. Saved cards: Oscar `payment.Bankcard` (`number` = obfuscated `XXXX-XXXX-XXXX-1111`,
  `partner_reference` = PayPal vault payment-token id, `expiry_date`, `card_type` = PayPal brand; `name` left blank).
  Order status uses the sandbox pipeline: `Pending` (awaiting payment) → `Being processed` (authorized) →
  `Complete` (fulfilled) / `Cancelled`.
- **New models (app `paypal_payments`)**: `Installation` (install id), `OrderPayment` (1:1 Oscar Order: payment
  state, PayPal order/authorization/capture ids+statuses, captured amount, fee, net, refund reservation,
  pay attempt counter), `ProviderWrite` (the claim / outcome record for every PayPal write, incl. refunds).
- **Currency.** `PAYPAL_CURRENCY` setting; catalogue price values are charged in that currency, and the order
  created through the API records that currency. Amount strings formatted per ISO-4217 minor units.
- **Refund over-refund guard.** Atomic conditional `UPDATE OrderPayment SET refund_reserved = refund_reserved + x
  WHERE refund_reserved + x <= captured_amount`; released only when a refund definitively fails.
- **3-D Secure.** An order answering `PAYER_ACTION_REQUIRED` is recorded `failed` with a clear message and no
  approval round-trip is built (task instruction: STOP & report if encountered).
- **Auth / roles.** Django session login (`/api/auth/login` uses `django.contrib.auth.authenticate/login`, Oscar's
  EmailBackend); CSRF enforced (login returns `csrfToken`). Fulfil/cancel/reconciliation require `is_staff`;
  all others act only on `request.user`'s rows (foreign rows → 404).

## Contract sheet

All operations: sync client `PaypalClient`; async twins exist but are not used. Every keyword after `*` has a
real default — never pass defensive `None`s. `request_options` is the trailing keyword-only arg. Parsed calls
raise `ApiError`; `with_raw_response` returns `ApiResult` (`Success`/`Failure`). A failed token fetch raises
`ApiError` with `.error` `OAuthProviderError | RawError` in BOTH modes. Decode failures raise
`pydantic.ValidationError`/`ValueError` (not `ApiError`) in both modes. `httpx` errors arrive unwrapped.
No retries in the SDK.

| # | Operation (route) | Positional / keyword-only used | Body (members we set; required ✱) | Returns & members we must assert | Status enum → outcome | Error union | Source |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `client.orders.create_order` (POST /v2/checkout/orders) | `body` positional; `pay_pal_request_id=ref`, `prefer="return=representation"` (default `return=minimal` would drop purchase_units) | `OrderRequest`: `intent`✱ = `CheckoutPaymentIntent.AUTHORIZE`; `purchase_units`✱ = [`PurchaseUnitRequest`: `amount`✱ `AmountWithBreakdown(currency_code✱, value✱)`, `reference_id`, `custom_id` = our ref-prefix+order number, `description`]; `payment_source` = `PaymentSource(card=CardRequest(name, number, expiry "YYYY-MM", security_code, billing_address=Address(country_code✱, …)))` or `CardRequest(vault_id=token)` | `Order`: assert `id`, `status`; `purchase_units[0].payments.authorizations[0]` → `id`, `status`, `amount.value/currency_code`, `create_time`, `expiration_time` (all `Optional` → UNSET-guarded) | `OrderStatus`: COMPLETED→ read authorization; APPROVED→ authorize step (#2); CREATED, SAVED→pending; PAYER_ACTION_REQUIRED→failed (3DS, not supported); VOIDED→failed; unlisted→unknown. `AuthorizationStatus` (for the hold): CREATED→done; PENDING→pending; DENIED, VOIDED→failed; CAPTURED, PARTIALLY_CAPTURED→unknown (not what was asked); unlisted→unknown | `CreateOrderErrorBody` = `Error` [400,401,422] \| `RawError` | map/operations/orders.md; models/order_request.py, purchase_unit_request.py, amount_with_breakdown.py, payment_source.py, card_request.py, address.py, order.py, purchase_unit.py, payment_collection.py, authorization_with_additional_data.py; enums/order_status.py, authorization_status.py |
| 2 | `client.orders.authorize_order` (POST /v2/checkout/orders/{id}/authorize) | `id` positional; `pay_pal_request_id=ref`, `prefer="return=representation"`; no body | `OrderAuthorizeResponse`: same authorization path as #1 | as #1 (AuthorizationStatus) | `AuthorizeOrderErrorBody` = `Error` [400,401,403,404,422,500] \| `RawError` | orders.md; models/order_authorize_response.py |
| 3 | `client.payments.get_authorized_payment` (GET /v2/payments/authorizations/{authorization_id}) | `authorization_id` positional | – | `PaymentAuthorization`: `id`, `status`, `amount`, `create_time`, `expiration_time` | AuthorizationStatus as #1 | `GetAuthorizedPaymentErrorBody` = `Error` [401,403,404] \| `RawError` [500, other] | payments.md; models/payment_authorization.py |
| 4 | `client.payments.reauthorize_payment` (POST …/{authorization_id}/reauthorize) | `authorization_id` positional; `pay_pal_request_id=ref`, `prefer="return=representation"`; `body=ReauthorizeRequest(amount=Money(currency_code✱, value✱))` | – | `PaymentAuthorization`: NEW `id`, `status`, `amount`, `create_time`, `expiration_time` | CREATED→done; PENDING→pending; DENIED, VOIDED→failed; CAPTURED, PARTIALLY_CAPTURED→unknown; unlisted→unknown | `ReauthorizePaymentErrorBody` = `Error` [400,401,403,404,422] \| `RawError` [500, other]. Sandbox-verified: 422 issue `REAUTHORIZATION_TOO_SOON` inside the 3-day honor period. Docstring: allowed day 4–29 after original auth; after 29 days → new payment needed | payments.md; docstring payments.py:195 |
| 5 | `client.payments.capture_authorized_payment` (POST …/{authorization_id}/capture) | `authorization_id` positional; `pay_pal_request_id=ref`, `prefer="return=representation"`; `body=CaptureRequest(amount=Money, final_capture=True)` | – | `CapturedPayment`: `id`, `status`, `amount`, `create_time`, `seller_receivable_breakdown` (`gross_amount`✱ `Money`, `paypal_fee`, `net_amount`) | `CaptureStatus`: COMPLETED→done; PENDING→pending; DECLINED, FAILED→failed; REFUNDED, PARTIALLY_REFUNDED→failed (undone); unlisted→unknown | `CaptureAuthorizedPaymentErrorBody` = `Error` [400,401,403,404,409,422] \| `RawError` [500, other] | payments.md; models/captured_payment.py, seller_receivable_breakdown.py, money.py; enums/capture_status.py |
| 6 | `client.payments.get_captured_payment` (GET /v2/payments/captures/{capture_id}) | `capture_id` positional | – | as #5 | as #5 | `GetCapturedPaymentErrorBody` = `Error` [401,403,404] \| `RawError` | payments.md |
| 7 | `client.payments.void_payment` (POST …/{authorization_id}/void) | `authorization_id` positional; `pay_pal_request_id=ref`, `prefer="return=representation"` | – | `PaymentAuthorization`: `id`, `status`, `update_time` | cancel mapper: VOIDED→done; CAPTURED, PARTIALLY_CAPTURED→failed (too late); DENIED→failed; CREATED, PENDING→unknown; unlisted→unknown | `VoidPaymentErrorBody` = `Error` [401,403,404,409,422] \| `RawError`. Sandbox-verified: 422 issue `PREVIOUSLY_VOIDED` on a repeat → a landing, settled via #3 | payments.md |
| 8 | `client.payments.refund_captured_payment` (POST /v2/payments/captures/{capture_id}/refund) | `capture_id` positional; `pay_pal_request_id=ref`, `prefer="return=representation"`; `body=RefundRequest(amount=Money, custom_id=…)` (amount always sent, full = remaining) | – | `Refund`: `id`, `status`, `amount`, `create_time`, `seller_payable_breakdown` | refund mapper (the undoing IS the ask): `RefundStatus` COMPLETED→done; PENDING→pending; FAILED, CANCELLED→failed; unlisted→unknown | `RefundCapturedPaymentErrorBody` = `Error` [400,401,403,404,409,422] \| `RawError` | payments.md; models/refund_request.py, refund.py; enums/refund_status.py |
| 9 | `client.vault.create_payment_token` (POST /v3/vault/payment-tokens) | `body` positional; `pay_pal_request_id=ref` (retention 3 h) | `PaymentTokenRequest`: `payment_source`✱ = `PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(name, number, expiry, security_code, billing_address))`; `customer=Customer(merchant_customer_id=…)` | `PaymentTokenResponse`: `id`, `customer.id`, `payment_source.card` (`brand`, `last_digits`, `expiry`) | no status member on this model → done iff `id` AND `payment_source.card.last_digits` present; otherwise unknown | `CreatePaymentTokenErrorBody` = `Error` [400,403,404,422,500] \| `RawError` | vault.md; models/payment_token_request*.py, payment_token_response*.py, card_payment_token_entity.py, customer.py |
| 10 | `client.vault.delete_payment_token` (DELETE /v3/vault/payment-tokens/{id}) | `id` positional; returns **None** → use `with_raw_response` to see status | – | `ApiResult[None, …]`; sandbox-verified 204, and 204 again on repeat | 2xx→done; 404→done (already gone); other → error | `DeletePaymentTokenErrorBody` = `Error` [400,403,500] \| `RawError` | vault.md |
| 11 | `client.transaction_search.search_transactions` (GET /v1/reporting/transactions) | `start_date`, `end_date` positional (RFC3339, seconds required; max range 31 days); `fields="all"`, `balance_affecting_records_only="N"`, `page_size=500`, `page=n` | – | `SearchResponse`: `transaction_details[*].transaction_info` (`transaction_id`, `paypal_reference_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_amount`, `fee_amount`, `transaction_status`, `custom_field`), `page`, `total_pages` | `transaction_status` is a plain str: D denied, P pending, S success, V reversed (docstring) — reported verbatim | **Case B**: `.error` always `RawError` | transaction_search.md; models/search_response.py, transaction_details.py, transaction_information.py; docstring transaction_search.py:55 (up to 3 h reporting lag) |

`Optional[T]` members are `T | UnsetType` (not `typing.Optional`) — never pass `None`; read with
`isinstance(x, UnsetType)`. Model inputs use Python names (`type_` ↔ wire `type`; none of ours alias).
Response enums are open (`…OrStr`): unknown strings pass through → `unknown`.

## OPERATION OUTCOMES

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/orders/{id}/pay` → `orders.create_order` (+ `orders.authorize_order` when APPROVED) | `Order.status` then `purchase_units[0].payments.authorizations[0].status` | Order COMPLETED + auth CREATED → done → 200 `authorized`; auth PENDING → pending → 202; auth DENIED/VOIDED → failed → 402 `payment_declined`; auth CAPTURED/PARTIALLY_CAPTURED/unlisted/absent → unknown → 504 `outcome_unknown`; Order APPROVED → next step #2 (its own claim), same mapping; Order CREATED/SAVED → pending → 202; PAYER_ACTION_REQUIRED → failed → 402 `payer_action_required`; VOIDED → failed → 402; unlisted/absent order status → unknown → 504; echoed amount ≠ order total → needs_review → 409 | `services.pay` → `gateway.safe_write` with `services.read_hold` + `services.create_order_outcome` / `services.authorize_order_outcome` (→ `services.hold_outcome`), answered by `services._finish_pay`; pending re-read by `services.refresh_hold` |
| `POST /api/orders/{id}/fulfil` → `payments.reauthorize_payment` (only when stale) | `PaymentAuthorization.status` | CREATED → done (continue to capture); PENDING → pending → 202; DENIED/VOIDED → failed → 409 `authorization_renewal_failed` with PayPal's issue text; CAPTURED/PARTIALLY_CAPTURED/unlisted → unknown → 504 | `services._renew` (outcome `services.reauthorize_outcome` → `services.hold_outcome`; 4xx → `authorization_renewal_failed`); unknown → 504 in `services._renew` |
| `POST /api/orders/{id}/fulfil` → `payments.capture_authorized_payment` | `CapturedPayment.status` | COMPLETED → done → 200 `captured` (+amount, fee, net); PENDING → pending → 202 `capture_pending`; DECLINED/FAILED → failed → 409; REFUNDED/PARTIALLY_REFUNDED → failed (undone) → 409; unlisted/absent → unknown → 504; echoed amount ≠ total → needs_review → 409 | `services._capture` (outcome `services.capture_outcome`, read `services.read_capture`, pending re-read `get_captured_payment`), answered by `services._finish_capture` |
| `POST /api/orders/{id}/cancel` → `payments.void_payment` | `PaymentAuthorization.status` | VOIDED → done → 200 `voided`; CAPTURED/PARTIALLY_CAPTURED/DENIED → failed → 409; CREATED/PENDING/unlisted/absent → unknown → 504; 422 `PREVIOUSLY_VOIDED` → landing, re-read via `get_authorized_payment` and mapped the same | `services._void` (outcome `services.void_outcome`, `is_landing` on `PREVIOUSLY_VOIDED`, `find` = `get_authorized_payment`) |
| `POST /api/orders/{id}/refunds` → `payments.refund_captured_payment` | `Refund.status` | COMPLETED → done → 201 `completed`; PENDING → pending → 202; FAILED/CANCELLED → failed → 409 (reservation released); unlisted/absent → unknown → 504 (reservation kept); echoed amount ≠ requested → needs_review → 409 | `services.refund` (outcome `services.refund_outcome`, read `services.read_refund`, pending re-read `get_refund`; answer `match write.outcome` at the end of `services.refund`) |
| `POST /api/payment-methods` → `vault.create_payment_token` | no status member; `id` + `payment_source.card.last_digits` | both present → done → 201; either absent → unknown → 504; 4xx → failed → 4xx/422 with PayPal message | `services.save_card` (outcome `services.vault_outcome`, read `services.read_token`) |
| `DELETE /api/payment-methods/{id}` → `vault.delete_payment_token` | HTTP status (raw peer) | 2xx or 404 → done → 204; other 4xx → error 4xx/502; 5xx/transport → 502/504, row kept | `services.delete_card` (`with_raw_response` + `match` on `Success` / `Failure`) |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| create_order (pay attempt n) | `ProviderWrite` row, `ref=…:o{order_pk}:pay{n}` | DB `UNIQUE(ref)` constraint on INSERT | `IntegrityError` in `try_claim` | `services.pay` → `gateway.try_claim` (catches `IntegrityError`) → loser branch of `gateway.safe_write` |
| advancing to pay attempt n+1 after a failed attempt | `OrderPayment.pay_attempt` | conditional `UPDATE … WHERE pay_attempt = n` (row count 0 → lost) | row-count check | `services.pay` (`OrderPayment.objects.filter(pk=…, pay_attempt=n).update(pay_attempt=F(…)+1)`) |
| authorize_order | `ProviderWrite` `ref=…:o{pk}:pay{n}:authorize` | `UNIQUE(ref)` | `try_claim` | `services._authorize` → `gateway.try_claim` |
| reauthorize_payment | `ProviderWrite` `ref=…:o{pk}:reauth:{authorization_id}` | `UNIQUE(ref)` | `try_claim` | `services._renew` → `gateway.try_claim` |
| capture_authorized_payment | `ProviderWrite` `ref=…:o{pk}:capture:{authorization_id}` | `UNIQUE(ref)` | `try_claim` | `services._capture` → `gateway.try_claim` |
| void_payment | `ProviderWrite` `ref=…:o{pk}:void:{authorization_id}` | `UNIQUE(ref)` | `try_claim` | `services._void` → `gateway.try_claim` |
| refund_captured_payment | `ProviderWrite` `ref=…:o{pk}:refund:{sha256(caller key)}` + amount reservation on `OrderPayment.refund_reserved` | `UNIQUE(ref)`; conditional UPDATE for the amount | `try_claim`; row-count check → 409 | `services.refund` → `gateway.try_claim`; amount reserved in `reserve` (the step's `on_claimed`) + `OrderPayment` check constraint `paypal_refunds_within_capture` |
| create_payment_token | `ProviderWrite` `ref=…:u{user_pk}:card:{sha256(caller key or HMAC card fingerprint)}` (+ `~{generation}` after a released claim) | `UNIQUE(ref)` | `try_claim` | `services.save_card` (`services.card_fingerprint` / caller key) → `gateway.try_claim`; generation suffix from `gateway._claim_ref` |
| POST /api/orders (local only, no provider write) | `OrderPayment.client_key` unique per user | `UNIQUE(user, client_key)` | `IntegrityError` → return existing order | `services.place_order` (`IntegrityError` on constraint `paypal_unique_order_client_key` → existing order) |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| create_order | same-reference resend (PayPal-Request-Id, retained 6 h per docstring); older than the window → stays `unknown` for an operator | `ref` (PayPal-Request-Id) | `gateway.safe_write` (step 2 resend when `resending`, step 3 `finder = step.send`); outside the window → `gateway.outcome_unknown` |
| authorize_order | same-reference resend (6 h) | `ref` | `gateway.safe_write` via `services._authorize` |
| reauthorize_payment | same-reference resend (45 days) | `ref` | `gateway.safe_write` via `services._renew` |
| capture_authorized_payment | same-reference resend (45 days) | `ref` | `gateway.safe_write` via `services._capture` |
| void_payment | same-reference resend (45 days); `PREVIOUSLY_VOIDED` → lookup `get_authorized_payment(authorization_id)` | `ref` / authorization id | `gateway.safe_write` via `services._void` (`find` = `client().payments.get_authorized_payment`) |
| refund_captured_payment | same-reference resend (45 days) | `ref` | `gateway.safe_write` via `services.refund` |
| create_payment_token | same-reference resend (3 h); after the window stays `unknown` | `ref` | `gateway.safe_write` via `services.save_card` |
| delete_payment_token | resend (DELETE by id is idempotent: sandbox-verified 204 on repeat) — no claim needed, nothing downstream acts on it except removing our row after 2xx/404 | token id | `services.delete_card` (card row kept on `httpx.RequestError` → 504; repeat DELETE resends) |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| create_order / authorize_order | Oscar Order + `OrderPayment` (awaiting_payment) + `ProviderWrite(ref, outcome=sending, amount, currency)` committed | `ProviderWrite` outcome/provider_id/status/provider_time; `OrderPayment` paypal_order_id, authorization id/status/expiry; Oscar `Source.allocate` → Transaction(Authorise); order status → Being processed | before: `services.place_order`/`_create_order` + `gateway.try_claim`; after: `gateway.complete` + `services._apply_authorization` (and `services._record_pending_hold` for pending) |
| reauthorize_payment | `ProviderWrite(sending)` | new authorization id/status/expiry on `OrderPayment`; Transaction(Reauthorise) | before: `gateway.try_claim` in `services._renew`; after: `gateway.complete` + `services._apply_reauthorization` |
| capture_authorized_payment | `ProviderWrite(sending)` | capture id/status, captured amount, PayPal fee, net on `OrderPayment`; `Source.debit` → Transaction(Debit); order status → Complete | before: `gateway.try_claim` in `services._capture`; after: `gateway.complete` + `services._apply_capture` |
| void_payment | `ProviderWrite(sending)` | authorization status VOIDED on `OrderPayment`; Transaction(Void); order status → Cancelled | before: `gateway.try_claim` in `services._void`; after: `gateway.complete` + `services._apply_void` |
| refund_captured_payment | amount reserved on `OrderPayment.refund_reserved` + `ProviderWrite(sending, amount, caller key)` | refund id/status on `ProviderWrite`; `Source.refund` → Transaction(Refund); `OrderPayment.refunded_amount`, state | before: `gateway.try_claim` then `reserve` in `services.refund`; after: `gateway.complete` + `services._apply_refund` |
| create_payment_token | `ProviderWrite(sending)` | Oscar `Bankcard` (obfuscated number, token id in partner_reference, brand, expiry) linked from the claim | before: `gateway.try_claim` in `services.save_card`; after: `gateway.complete` + `services._apply_vaulted_card` |
| delete_payment_token | `Bankcard` row (still usable until PayPal confirms) | `Bankcard` deleted after 2xx/404; the create claim is released (generation bump) so the same card can be saved again | `services.delete_card` (deletes `Bankcard` and marks the create claim `released` only after PayPal 2xx/404) |

## Reconciliation design

- Parse `from`/`to` (ISO-8601, timezone required → else 400). Split into ≤31-day chunks; per chunk page
  through `search_transactions` (`fields="all"`, balance-affecting records only — the default) until
  `page >= total_pages`. Filter provider rows to `from <= transaction_initiation_date < to` (the provider clock).
- Local side on the same clock: money movements only — `ProviderWrite` rows of kind capture/refund whose
  stored `provider_time` is in the window (done/pending/needs_review); `unsettled` = capture/refund writes
  still `sending`/`unknown` claimed inside the window. Holds (authorize/void) move no money and are not
  balance-affecting records, so they are not reconciled.
- Match against the set: provider rows grouped by `transaction_id`; each local write takes all rows with its
  provider id. Report `matched`, `localOnly` (flagged `withinReportingLag` inside PayPal's 3-hour lag),
  `paypalOnly` (split into `thisSite` = `custom_field` carrying our prefix, and `other`), `unsettled`,
  `amountMismatches`.
- Code: `services.reconciliation`, `services._fetch_transactions`, `services._provider_row`, `services._local_row`.

## Assumptions & Blockers

- (minor) The plugin declares only the sandbox host; a non-sandbox `PAYPAL_ENVIRONMENT` requires `PAYPAL_BASE_URL`.
- (minor) Staleness is decided from PayPal's own authorization `create_time` (3-day honor period, docstring) and
  `expiration_time`; a capture 422 whose issue contains `EXPIRED` also triggers one renewal attempt — that
  issue name is UNVERIFIED (not in the plugin; settle by observing a real expired authorization).
- (minor) Refunds are shopper-scoped (task: only fulfil/cancel/reconciliation are operator actions).
- (minor) The prompt's build sequence omits `oscar_import_catalogue`; without it only 11 products exist and
  `orders.json` fails with a FK error. The CSV import is run (as the Makefile does).
- No blockers: card authorization (COMPLETED + auth CREATED), vaulting, void, delete and transaction search
  were all smoke-tested against the sandbox with the provided credentials.

## REQUIRED READING

- MUST load `paypal:python-error-handling` — error ladder, OAuthProviderError first, never-sent vs unknown split. (loaded)
- MUST load `paypal:python-client-initialization` — module-level sync client, close obligation, transport keyword. (loaded)
- MUST load `paypal:python-configuration-resilience` — safe write, no retries, reconciliation clock rules, logging transport. (loaded)
- MUST load `paypal:python-calling-endpoints` — `prefer` default narrows responses; status → outcome → `answer`. (loaded)
- MUST load `paypal:python-models` — UNSET vs None, open enums, Decimal money strings. (loaded)
- MUST load `paypal:python-authentication` — lazy token fetch, failure surfaces from the operation. (loaded)
- MUST load `paypal:python-testing` — stub transport seam, token request first, lowercase headers. (loaded)
