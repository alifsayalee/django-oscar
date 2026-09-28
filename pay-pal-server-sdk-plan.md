# PayPal integration plan — django-oscar sandbox

Scope: PayPal card payments (authorize → capture at fulfilment → void on cancel → refunds),
saved cards (PayPal Vault), and a PayPal-vs-app reconciliation report, exposed as a JSON API
under `/api/` on the sandbox project (`sandbox/`), as a new Django app `sandbox/apps/paypal_integration`.

## Survey (read-only)

| Fact | Finding | Exemplar |
| --- | --- | --- |
| Host framework | Django (WSGI, **sync**) — sandbox `settings.py`, `wsgi.py` | `sandbox/wsgi.py` |
| Settings convention | `django-environ` `env = environ.Env()`; `env('X', default=...)` | `sandbox/settings.py` |
| App convention | local apps live in `sandbox/apps/<name>`, imported as `apps.<name>` | `sandbox/apps/sitemaps.py`, `sandbox/urls.py` |
| URL convention | `path(...)` list in `sandbox/urls.py`; non-i18n routes before `i18n_patterns` | `sandbox/urls.py` |
| DB | SQLite by default, **`ATOMIC_REQUESTS=True`** → payment views must opt out (`transaction.non_atomic_requests`) so the claim commits before the PayPal call | `sandbox/settings.py` |
| Order model to reuse | `oscar.apps.order` `Order`/`Line` via `OrderCreator.place_order` from a transient `Basket` | `src/oscar/apps/order/utils.py` |
| Payment models to reuse | `payment.Source` (allocated/debited/refunded), `payment.SourceType`, `payment.Transaction`, `payment.Bankcard` (masked number + `partner_reference` = vault token id) | `src/oscar/apps/payment/abstract_models.py` |
| Order statuses | pipeline `Pending → Being processed → Complete`, `Cancelled`. Pending = awaiting payment; Being processed = authorized; Complete = fulfilled/captured | `sandbox/settings.py` |
| Operator | `user.is_staff` | fixtures `auth.json` |
| Uniqueness store for claims | the Django DB (unique constraint on `ProviderWrite.ref`) — survives process restarts, shared by every worker | new model |
| Env manager | `py -3.11 -m venv venv`, `venv\Scripts\pip install -e .[test]` | — |
| SDK install | `venv\Scripts\pip install "paypal @ git+https://github.com/context-plugins/paypal-python-sdk.git@main"` — **drift from the skill text: distribution AND import root are `paypal`, clients `PaypalClient`/`AsyncPaypalClient`** (the SDK's own `sdk-map.md` confirms) | — |
| Tests | pytest + pytest-django; repo `tests/` uses `tests.settings`. New tests: `sandbox/apps/paypal_integration/tests.py`, run from `sandbox/` with `--ds=settings` | `tests/integration/payment` |
| Type check | none configured → `mypy --strict` on the SDK-facing module (`gateway.py`) | — |
| Baseline | `pytest tests/integration/payment tests/integration/order --sqlite`: 138 passed, 3 failed (pre-existing `TestConcurrentOrderPlacement` under SQLite), 1 skipped | — |
| Credentials | env `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENVIRONMENT`(=sandbox), `PAYPAL_CURRENCY`(=USD) present; `PAYPAL_BASE_URL` optional | — |
| Host selected | SDK declares one server `https://api-m.sandbox.paypal.com`. `PAYPAL_BASE_URL` set → used verbatim (token call too, SDK derives `/v1/oauth2/token` from it). Else `PAYPAL_ENVIRONMENT=sandbox` → SDK's sandbox URL passed explicitly. Any other environment without `PAYPAL_BASE_URL` → `ImproperlyConfigured` (the SDK source names no other host; not filled from memory) | — |

### Live smoke (scratchpad, sandbox credential)

- `search_transactions` 30-day window: 200, `total_pages` populated → reporting entitled.
- `create_order` intent `AUTHORIZE` + `payment_source.card` + `PayPal-Request-Id`, `Prefer: return=representation`:
  order `status=COMPLETED`, `purchase_units[0].payments.authorizations[0].status=CREATED`, `expiration_time` = +29 days. No payer action.
- Same with `card.vault_id`: works.
- `create_payment_token` with card: `id`, `customer.id` (PayPal creates the customer), `payment_source.card{brand,last_digits,expiry}`.
- `list_customer_payment_tokens` right after creation: `payment_tokens` UNSET (lags) → never used as source of truth.
- `reauthorize_payment` inside honor period: 422 `Error`, `details[].issue = REAUTHORIZATION_TOO_SOON`.
- `capture_authorized_payment` twice with same `PayPal-Request-Id`: same capture id returned → PayPal de-duplicates (kind 2).
- `seller_receivable_breakdown`: `gross_amount`, `paypal_fee`, `net_amount`.
- Over-refund: 422 `CAPTURE_FULLY_REFUNDED`.
- `delete_payment_token` on an already-deleted token: 204 → repeat is safe.
- `get_payment_token` on a deleted token: **404 whose body fails `Error` decode → `pydantic.ValidationError`** (error body `links[].rel` missing). → never use `get_payment_token` as the delete lookup; and the error ladder must tell "non-2xx body undecodable" from "2xx undecodable" — done by an observing transport that records the last response status in a `ContextVar`.
- All smoke writes voided/refunded/deleted.

## Contract sheet

Client: **sync** `paypal.PaypalClient` (Django WSGI is sync). Module-level lazy singleton built on first use
(post-fork), closed via `atexit`. Keyword-only constructor:
`PaypalClient(base_url=<resolved>, timeout=<PAYPAL_TIMEOUT>, custom_http_client=ObservingTransport(HttpxClient(timeout=...)), oauth2=ClientCredentials(client_id=..., client_secret=...))`.
With `custom_http_client`, `timeout=` does not reach the wire → timeout set on `HttpxClient(timeout=...)`.
`oauth2` is optional at the type level → settings validation fails fast if id/secret empty.
Imports: `paypal` (client), `paypal.core` (`ClientCredentials`, `ApiError`, `RawError`, `OAuthProviderError`, `HttpxClient`, `HttpRequest`, `HttpResponse`, `UNSET`, `UnsetType`), `paypal.models` (models), `paypal.models.enums` (enums).
Every keyword-only parameter has a real default → never pass defensive `None`s. `prefer` defaults to `"return=minimal"` → pass `"return=representation"` on every write whose body is read.
**No retries in the SDK**; this integration adds none automatically — an unknown outcome is resolved by the safe write's same-reference resend/lookup on the next request.

| Operation (sync, parsed) | Positional / keyword-only | Body model: members set (required ✱) | Returns → members asserted | `ApiError.error` union |
| --- | --- | --- | --- | --- |
| `client.orders.create_order(body, *, pay_pal_request_id, prefer, …)` `POST /v2/checkout/orders` | `body` / rest | `OrderRequest`: `intent`✱=`CheckoutPaymentIntent.AUTHORIZE`, `purchase_units`✱=[`PurchaseUnitRequest`(`amount`✱=`AmountWithBreakdown`(`currency_code`✱, `value`✱ str), `invoice_id`, `custom_id`)], `payment_source`=`PaymentSource(card=CardRequest(name, number, expiry "YYYY-MM", security_code, billing_address) \| CardRequest(vault_id))`. `pay_pal_request_id` **mandatory for single-step card create; server keeps keys 6 h** | `Order`: `id`, `status: OrderStatus`, `purchase_units[0].payments.authorizations[0]` (`AuthorizationWithAdditionalData`: `id`, `status: AuthorizationStatus`, `amount: Money`, `create_time`, `expiration_time`), `payment_source.card` (`brand`, `last_digits`) | `CreateOrderErrorBody` = `Error`[400,401,422] \| `RawError` |
| `client.payments.get_authorized_payment(authorization_id, *, …)` | `authorization_id` | — | `PaymentAuthorization`: `id`, `status`, `amount`, `create_time`, `expiration_time` | `GetAuthorizedPaymentErrorBody` = `Error`[401,403,404] \| `RawError`[500,…] |
| `client.payments.reauthorize_payment(authorization_id, *, pay_pal_request_id, prefer, body)` | `authorization_id` | `ReauthorizeRequest(amount=Money)` (only member) | `PaymentAuthorization`: `id` (new), `status`, `amount`, `create_time`, `expiration_time` | `ReauthorizePaymentErrorBody` = `Error`[400,401,403,404,422] \| `RawError`[500,…] |
| `client.payments.capture_authorized_payment(authorization_id, *, pay_pal_request_id, prefer, body)` keys kept 45 days | `authorization_id` | `CaptureRequest(amount=Money, final_capture=True)` | `CapturedPayment`: `id`, `status: CaptureStatus`, `amount`, `create_time`, `seller_receivable_breakdown` (`gross_amount`✱, `paypal_fee`, `net_amount`) | `CaptureAuthorizedPaymentErrorBody` = `Error`[400,401,403,404,409,422] \| `RawError`[500,…] |
| `client.payments.get_captured_payment(capture_id, *, …)` | `capture_id` | — | `CapturedPayment` as above | `GetCapturedPaymentErrorBody` = `Error`[401,403,404] \| `RawError` |
| `client.payments.void_payment(authorization_id, *, pay_pal_request_id, prefer)` | `authorization_id` | no body | `PaymentAuthorization`: `id`, `status`, `update_time` | `VoidPaymentErrorBody` = `Error`[401,403,404,409,422] \| `RawError`[500,…] |
| `client.payments.refund_captured_payment(capture_id, *, pay_pal_request_id, prefer, body)` keys kept 45 days | `capture_id` | `RefundRequest(amount=Money)` (omit amount = full refund; we always send it) | `Refund`: `id`, `status: RefundStatus`, `amount`, `create_time` | `RefundCapturedPaymentErrorBody` = `Error`[400,401,403,404,409,422] \| `RawError`[500,…] |
| `client.payments.get_refund(refund_id, *, …)` | `refund_id` | — | `Refund` | `GetRefundErrorBody` = `Error`[401,403,404] \| `RawError` |
| `client.vault.create_payment_token(body, *, pay_pal_request_id)` keys kept 3 h | `body` | `PaymentTokenRequest`: `payment_source`✱=`PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(name, number, expiry, security_code, billing_address))`, `customer=Customer(id)` when known | `PaymentTokenResponse`: `id`, `customer.id`, `payment_source.card` (`CardPaymentTokenEntity`: `brand`, `last_digits`, `expiry`) | `CreatePaymentTokenErrorBody` = `Error`[400,403,404,422,500] \| `RawError` |
| `client.vault.delete_payment_token(id, *)` → **returns `None`** → use `with_raw_response` to see 204 | `id` | — | `Success.response.status_code` | `DeletePaymentTokenErrorBody` = `Error`[400,403,500] \| `RawError` |
| `client.transaction_search.search_transactions(start_date, end_date, *, fields="transaction_info", balance_affecting_records_only="Y", page_size=100, page=1)` — max range **31 days**; lags up to 3 h | `start_date`, `end_date` (RFC 3339, seconds required) | — | `SearchResponse`: `transaction_details[].transaction_info` (`transaction_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_amount: Money`, `fee_amount`, `transaction_status` str D/P/S/V, `invoice_id`, `custom_field`), `total_pages`, `page` | **Case B**: always `RawError` |

`Error` members: `name`✱, `message`✱, `debug_id`✱, `details: list[ErrorDetails]` (`issue`✱, `description`). `OAuthProviderError` (token fetch): `error`, `error_description` — raised from the first operation, in both modes → 502 "PayPal refused our credentials".
Optional members are `T | UnsetType`: never pass `None`; read with `isinstance(x, UnsetType)`. Money is `str`, built with `Decimal.quantize` per currency exponent. Enums are open (`…OrStr`): unknown value → `unknown`.
Decode failure raises `ValidationError`/`ValueError` in both modes; `httpx` exceptions arrive unwrapped.

### Status enums → outcome (`status_from_provider` per step)

| Step | done | pending | failed | anything else |
| --- | --- | --- | --- | --- |
| authorize (`AuthorizationStatus` of the new authorization; order `OrderStatus.PAYER_ACTION_REQUIRED` → failed "3-D Secure challenge not supported") | `CREATED` | `PENDING` | `DENIED`, `VOIDED`, `CAPTURED`/`PARTIALLY_CAPTURED` (hold no longer in effect as a hold) | unknown |
| reauthorize (`AuthorizationStatus`) | `CREATED` | `PENDING` | `DENIED`, `VOIDED`, `CAPTURED`, `PARTIALLY_CAPTURED` | unknown |
| capture (`CaptureStatus`) | `COMPLETED` | `PENDING` | `DECLINED`, `FAILED`, `REFUNDED`, `PARTIALLY_REFUNDED` (done then undone) | unknown |
| void (`AuthorizationStatus`, cancel mapper) | `VOIDED` | — | `CAPTURED`, `PARTIALLY_CAPTURED` (too late) | unknown |
| refund (`RefundStatus`) | `COMPLETED` | `PENDING` | `FAILED`, `CANCELLED` | unknown |
| vault create (`PaymentTokenResponse` has **no status member**; the documented 200/201 body is the token) | `id` AND `payment_source.card.last_digits` present | — | — (4xx is refused) | unknown (id or card missing) |
| vault delete (`None` return) | raw `Success` 2xx | — | — | unknown |

## OPERATION OUTCOMES

The caller's status comes only from `views.outcome_status` (done→200/201, pending/sending→202, failed/needs_review→409, anything else→504). A refusal raised at send time is answered through `gateway.classify` → `views.api` (card problem 422, conflict 409, our credentials 502, rate limit 503).

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/orders/{id}/pay` → `orders.create_order` (AUTHORIZE, card or vault_id) | `Order.status` + `purchase_units[0].payments.authorizations[0].status` | `CREATED`→done 200 (order → Being processed); `PENDING`→pending 202; `DENIED`/`VOIDED`/`CAPTURED`/`PARTIALLY_CAPTURED`→failed 409; order `PAYER_ACTION_REQUIRED`→failed 409 "3-D Secure challenge not supported"; no authorization in the body→pending 202 when the order has a status, unknown 504 when not; unlisted value→unknown 504; amount ≠ order total→needs_review 409; PayPal 4xx→failed, mapped 4xx (e.g. 422 `CARD_EXPIRED`) | `services.pay` → `gateway.create_authorized_order` → `gateway._order_answer` / `gateway.authorization_outcome`; effects `services._apply_authorization` |
| `POST /api/orders/{id}/fulfil` → `payments.reauthorize_payment` (only past the 3-day honor period) | `PaymentAuthorization.status` | `CREATED`→done (capture proceeds on the new authorization); `PENDING`/sending→409 `authorization_renewal_pending` (retry); `DENIED`/`VOIDED`/`CAPTURED`/`PARTIALLY_CAPTURED` or a PayPal refusal→capture attempted on the original, and if that is refused too, 409 naming both reasons and the operator action; unlisted→unknown 504 | `services._fresh_authorization` → `services._reauthorize` → `gateway.reauthorize` / `gateway.authorization_outcome`; effects `services._apply_reauthorization` |
| `POST /api/orders/{id}/fulfil` → `payments.capture_authorized_payment` | `CapturedPayment.status` | `COMPLETED`→done 200 (order → Complete, fee/net stored); `PENDING`→pending 202 (a later fulfil re-reads it); `DECLINED`/`FAILED`/`REFUNDED`/`PARTIALLY_REFUNDED`→failed 409; unlisted/absent→unknown 504; amount mismatch→needs_review 409; PayPal 409/422→409 `capture_refused` with the operator action | `services.fulfil` → `services._capture` → `gateway.capture` / `gateway.capture_outcome`; refresh `gateway.get_capture`; effects `services._apply_capture` |
| `POST /api/orders/{id}/cancel` → `payments.void_payment` | `PaymentAuthorization.status` | `VOIDED`→done 200 (order → Cancelled); `CAPTURED`/`PARTIALLY_CAPTURED`→failed 409 (too late — refund instead); other/absent→unknown 504 | `services.cancel` → `gateway.void` / `gateway.void_outcome`; effects `services._apply_void` |
| `POST /api/orders/{id}/refunds` → `payments.refund_captured_payment` | `Refund.status` | `COMPLETED`→done 201; `PENDING`→pending 202 (a repeat re-reads it); `FAILED`/`CANCELLED`→failed 409; unlisted/absent→unknown 504; amount mismatch→needs_review 409; PayPal 4xx→failed, mapped 4xx, reservation released | `services.refund` → `gateway.refund` / `gateway.refund_outcome`; refresh `gateway.get_refund`; effects `services._apply_refund` |
| `POST /api/payment-methods` → `vault.create_payment_token` | none on the model (see sheet) | id + `payment_source.card.last_digits` present→done 201; either missing→unknown 504; PayPal 4xx→failed, mapped 4xx | `services.save_card` → `gateway.vault_card` → `gateway._token_answer`; effects `services._apply_vaulted_card` |
| `DELETE /api/payment-methods/{id}` → `vault.delete_payment_token` | raw result (`Success` 2xx / `Failure`) | `Success`→done 204 (Bankcard row deleted); `Failure` 4xx→failed, mapped 4xx (card usable again); transport/5xx→unknown 504 (card stays hidden and unusable until settled) | `services.delete_card` → `gateway.delete_vaulted_card`; effects `services._apply_card_deleted` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| authorize (create_order) — ref `{prefix}:ord:{number}:auth:{attempt}` (attempt advances only after a definitive `failed`, under the payment row lock) | `ProviderWrite` row (DB) | UNIQUE constraint on `ProviderWrite.ref` → `IntegrityError` | `safe_write.try_claim` | ref + claim in `services.pay` (under `services._lock_payment`); `models.ProviderWrite.ref` |
| reauthorize — ref `{prefix}:ord:{number}:reauth:{authorization_id}` | `ProviderWrite` | UNIQUE `ref` | `safe_write.try_claim` (via `safe_write.safe_write`) | `services._reauthorize` |
| capture — ref `{prefix}:ord:{number}:capture:{authorization_id}` | `ProviderWrite` | UNIQUE `ref` | `safe_write.try_claim` (via `safe_write.safe_write`) | `services._capture` |
| void — ref `{prefix}:ord:{number}:void:{authorization_id}` | `ProviderWrite` | UNIQUE `ref` | `safe_write.try_claim` | `services.cancel` (claim under `services._lock_payment`) |
| refund — ref `{prefix}:ord:{number}:refund:{sha256(caller key)[:32]}`; the claim row carries the reserved amount | `ProviderWrite` (inserted in the transaction that locks the payment row and checks captured − reserved) | UNIQUE `ref`; over-reservation rejected under the payment row lock; a key replayed with another amount → 422 (`fingerprint`) | `safe_write.try_claim` | `services.refund` |
| vault create — ref `{prefix}:user:{user_id}:card:{sha256(Idempotency-Key or fresh uuid)[:32]}` | `ProviderWrite` | UNIQUE `ref` | `safe_write.try_claim` (via `safe_write.safe_write`) | `services.save_card` |
| vault delete — ref `{prefix}:card:{bankcard_id}:delete` | `ProviderWrite` | UNIQUE `ref` | `safe_write.try_claim` (via `safe_write.safe_write`) | `services.delete_card` |

`{prefix}` is `PAYPAL_REFERENCE_PREFIX` or a random per-database prefix (`services._prefix`, `models.InstallSetting`). This was added after the live run hit `DUPLICATE_INVOICE_ID` from another install sharing this PayPal account.

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| authorize (create_order) | same-reference resend (`PayPal-Request-Id` = ref; PayPal returns the original), only inside the 6 h key window; past it, stays `unknown` for an operator | the authorize ref | `safe_write.safe_write` (`find=send`, `resend_window=services.CREATE_ORDER_KEY_WINDOW`) from `services.pay` |
| reauthorize | same-reference resend (`PayPal-Request-Id`); retention undocumented → treated as 3 h | the reauth ref | `safe_write.safe_write` (`find=send`, `services.REAUTHORIZE_KEY_WINDOW`) from `services._reauthorize` |
| capture | same-reference resend (`PayPal-Request-Id`, 45 days); verified live that a repeat returns the same capture | the capture ref | `safe_write.safe_write` (`find=send`, `services.PAYMENTS_KEY_WINDOW`) from `services._capture` |
| void | first-send transport failure → lookup `get_authorized_payment` (VOIDED = done); later requests → same-reference resend | the void ref / authorization id | `safe_write.safe_write` with `find=gateway.get_authorization_for_void` from `services.cancel` |
| refund | same-reference resend (`PayPal-Request-Id`, 45 days) | the refund ref | `safe_write.safe_write` (`find=send`, `services.PAYMENTS_KEY_WINDOW`) from `services.refund` |
| vault create | same-reference resend (`PayPal-Request-Id`, 3 h window; past it stays unknown) | the card ref | `safe_write.safe_write` (`find=send`, `services.VAULT_KEY_WINDOW`) from `services.save_card` |
| vault delete | resend the delete (idempotent: 204 on an already-deleted token, verified live) | token id (under the delete ref) | `safe_write.safe_write` (`find=send`) from `services.delete_card` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| authorize | Oscar `Order` (Pending), `payment.Source` + `PayPalPayment` (from `services.place_order`), `ProviderWrite(ref, sending, amount)` committed | outcome, PayPal order id, authorization id/status/expiry on `PayPalPayment`; `Source.allocate` → `Transaction(Authorise)`; order → Being processed | claim `services.pay`; record `safe_write.complete` + `services._apply_authorization` |
| reauthorize | `ProviderWrite(ref, sending, amount)` | new authorization id/status/expiry on `PayPalPayment`; `Transaction(Reauthorise)` | claim `safe_write.safe_write`; record `safe_write.complete` + `services._apply_reauthorization` |
| capture | `ProviderWrite(ref, sending, amount)` | capture id/status, captured/fee/net on `PayPalPayment`; `Source.debit` → `Transaction(Debit)`; order → Complete | claim `safe_write.safe_write` from `services._capture`; record `safe_write.complete` + `services._apply_capture` |
| void | `ProviderWrite(ref, sending)` | authorization status VOIDED; `Transaction(Void)`; order → Cancelled | claim `services.cancel`; record `safe_write.complete` + `services._apply_void` |
| refund | `ProviderWrite(ref, sending, amount)` = the reservation, returned as `refundId` | refund id/status; `Source.refund` → `Transaction(Refund)` when done | claim `services.refund`; record `safe_write.complete` + `services._apply_refund` |
| vault create | `ProviderWrite(ref, sending, fingerprint=last4:expiry)` | `Bankcard(user, brand, masked number, expiry, partner_reference=token id)`, `PayPalCustomer` | claim `safe_write.safe_write` from `services.save_card`; record `safe_write.complete` + `services._apply_vaulted_card` |
| vault delete | `ProviderWrite(ref, sending, bankcard_id)` (card immediately excluded from list and from paying: `services._deleting_card_ids`) | Bankcard row deleted on done | claim `safe_write.safe_write` from `services.delete_card`; record `safe_write.complete` + `services._apply_card_deleted` |

## Reconciliation

`GET /api/reconciliation?from&to` (staff): split `[from,to)` into ≤31-day chunks, page each chunk to `total_pages`.
Provider side filtered on `transaction_initiation_date`; local side = `ProviderWrite` capture/refund rows filtered on the
stored **provider time** (`create_time` from PayPal). Findings: `matched`, `paypalOnly`, `appOnly`, `unsettled` (local
claims with no provider time — sending/unknown). Match on `transaction_id == provider_id` (set, not first hit).

## Assumptions & Blockers

- Minor: catalogue prices are GBP in fixtures; per the task, the **number** comes from the catalogue price and the currency from `PAYPAL_CURRENCY` (order recorded in that currency).
- Minor: refunds endpoint is shopper-scoped (task: only fulfil/cancel/reconciliation are operator actions) — the order owner requests the return.
- Minor: 3-D Secure `PAYER_ACTION_REQUIRED` is answered as a refusal (409) — no approval round-trip, as instructed. Not observed with the sandbox test card.
- Minor: reauthorization is only possible 4–29 days after authorization (docstring + live `REAUTHORIZATION_TOO_SOON`); past 29 days / expired / refused → 409 with an operator-actionable message (cancel, ask shopper to pay again).
- Minor: reauthorize's PayPal-Request-Id retention is undocumented in the SDK; treated as 3 h (the shortest documented window).
- Found live: the fixture build needs `oscar_import_catalogue` on the CSVs (skipping it leaves 11 products and `orders.json` fails on a foreign key).
- No blockers.

## REQUIRED READING

| Hazard | Pointer |
| --- | --- |
| client lifetime, sync/async, custom transport timeout | MUST load `python-client-initialization` (loaded) |
| OAuth token fetch failure is `ApiError[OAuthProviderError]` from the operation | MUST load `python-authentication` (loaded) |
| keyword-only tail, `prefer` default, `None` returns | MUST load `python-calling-endpoints` (loaded) |
| `UNSET`, open enums, money strings | MUST load `python-models` (loaded) |
| error ladder, decode failures, never-sent vs unknown transport errors | MUST load `python-error-handling` (loaded) |
| safe write, no retries, reconciliation on provider clock, logging transport | MUST load `python-configuration-resilience` (loaded) |
| transport-seam fakes, token response first | MUST load `python-testing` (loaded) |
