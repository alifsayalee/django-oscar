# PayPal payments + saved cards for the django-oscar sandbox — plan & contract sheet

## Scope

New Django app `sandbox/apps/payments_api` (label `payments_api`), routed under `/api/` from
`sandbox/urls.py`. Endpoints: `POST /api/orders`, `POST /api/orders/{id}/pay`, `POST /api/orders/{id}/fulfil`
(staff), `POST /api/orders/{id}/cancel` (staff), `POST /api/orders/{id}/refunds`, `GET /api/my-orders`,
`GET /api/reconciliation` (staff), `POST|GET /api/payment-methods`, `DELETE /api/payment-methods/{id}`,
plus `GET /api/csrf` and `POST|DELETE /api/session` (Django session login, JSON front).

Reused Oscar models: `order.Order` / `order.Line` (placed via `OrderCreator` from a transient `basket.Basket`),
`payment.Source` / `payment.SourceType` / `payment.Transaction` (amount allocated/debited/refunded),
`order.PaymentEvent`, `payment.Bankcard` (saved card: masked number, expiry, `partner_reference` = vault token id).
New models only for what Oscar has no place for: `PayPalPayment` (PayPal ids/status/fee/net per order, 1:1 with
Oscar `Source`) and `ProviderWrite` (the claim ledger of every PayPal write), `InstallIdentity` (reference prefix).

## Repo survey

| Convention | Pattern | Exemplar |
| --- | --- | --- |
| sandbox app layout | package under `sandbox/apps/`, imported as `apps.<name>` | `sandbox/apps/sitemaps.py`, `sandbox/urls.py` (`from apps.sitemaps import …`) |
| URL wiring | `path(...)` / `include(...)` in `sandbox/urls.py`; non-i18n routes before `i18n_patterns` | `sandbox/urls.py` |
| settings | `environ.Env()` reads in `sandbox/settings.py` | `sandbox/settings.py` (`env.bool('DEBUG', …)`) |
| order statuses | `Pending → Being processed → Complete`, `Cancelled` | `sandbox/settings.py` `OSCAR_ORDER_STATUS_PIPELINE` |
| payment bookkeeping | `Source.allocate/debit/refund` + `Transaction` | `src/oscar/apps/payment/abstract_models.py` |
| order placement | `OrderCreator().place_order(basket, total, shipping_method, shipping_charge, user=…)` | `src/oscar/apps/order/utils.py` |
| tests | Django `TestCase`, sandbox `TEST_RUNNER = DiscoverRunner` → `manage.py test apps.payments_api` | — |
| sync vs async | **sync** — Django under WSGI, sync views | `sandbox/wsgi.py` |
| DB | SQLite default, `ATOMIC_REQUESTS=True` → API views use `transaction.non_atomic_requests` so the claim commits before the PayPal call | `sandbox/settings.py` |

Toolchain: `py -3.11 -m venv venv`, `venv\Scripts\pip install -e .[test]`; SDK installed from source:
`pip install "paypal @ git+https://github.com/context-plugins/paypal-python-sdk.git@main"` (installed 2.29).
Type check: `mypy` (installed into venv; project has none configured) → `mypy --strict` on the new app's
PayPal-facing modules. Tests: `venv\Scripts\python sandbox\manage.py test apps.payments_api`.
Baseline (untouched tree, `pytest --sqlite tests/integration/order tests/functional/checkout`): 217 passed,
3 failed (pre-existing `TestConcurrentOrderPlacement` on SQLite), 1 skipped.

**Drift from the getting-started skill:** the SDK's real distribution/import root is `paypal` (client
`PaypalClient`), not `pay-pal-server-sdk` / `pay_pal_server_sdk`. Everything below is from the installed `paypal`
package and its map (`sdk-map.md`, `map/operations/*.md` in the SDK repo, branch `main`).

## SDK contract sheet (no open lookups)

**Client.** Sync `paypal.PaypalClient` (keyword-only): `base_url: str|None`, `timeout: float=30.0`,
`custom_http_client: HttpClient|None`, `oauth2: ClientCredentialsOrDict|None`. One long-lived, lazily-built
module-level client per process (built after fork, on first use), `close()` at `atexit`. Never `AsyncPaypalClient`.
Transport: `paypal.core.HttpxClient(timeout=…)` wrapped in a logging transport (method, path, status, ms — never
headers/bodies); since we pass a transport, the timeout is set on `HttpxClient`.
Auth: `oauth2=ClientCredentials(client_id=…, client_secret=…)` from `paypal.core` — **always set**; omitting it
sends unauthenticated requests silently. Token fetched lazily from `<base_url>/v1/oauth2/token`, cached on the
client. Failed token fetch → `ApiError` whose `.error` is `OAuthProviderError | RawError` (`paypal.core`), raised
even through `with_raw_response`.
Base URL: the SDK declares ONE server, `https://api-m.sandbox.paypal.com` (the default when `base_url` is omitted —
silent). Resolution: `PAYPAL_BASE_URL` if set (verbatim, token call included) → else map `{"sandbox":
"https://api-m.sandbox.paypal.com"}[PAYPAL_ENVIRONMENT]` → any other environment without a base URL raises
`ImproperlyConfigured` (the SDK/map declares no other host; we do not invent one). `base_url` always passed explicitly.
**No retries** in the SDK; we add none on writes (duplicate risk) — writes go through the safe write; reads are not
retried either (a failed read surfaces as 502/504 to the caller).
Every keyword-only parameter has a real default — no defensive `None`s. `prefer` defaults to `"return=minimal"`:
we pass `prefer="return=representation"` on every write whose result we read.

Imports: models from `paypal.models`, enums from `paypal.models.enums`, runtime from `paypal.core`
(`ApiError`, `RawError`, `Success`, `Failure`, `ClientCredentials`, `HttpxClient`, `HttpRequest`, `HttpResponse`,
`OAuthProviderError`, `UNSET`, `UnsetType`).

| Operation (sync, parsed) | Positional / keyword-only | Returns | `ApiError.error` union | Notes (source) |
| --- | --- | --- | --- | --- |
| `client.orders.create_order(body, *, pay_pal_request_id=None, prefer="return=minimal", …)` | `body: OrderRequest\|OrderRequestDict` positional | `Order` | `CreateOrderErrorBody` = `Error` [400,401,422] \| `RawError` | `PayPal-Request-Id` mandatory for single-step card orders, kept 6 h (docstring). **Smoke: a replay under the same id answered 422 `TRANSACTION_REFUSED`, not the original → NOT a safe lookup** |
| `client.orders.get_order(id, *, fields=None, …)` | `id` | `Order` | `GetOrderErrorBody` = `Error` [401,404] \| `RawError` | lookup for the authorize-order step |
| `client.orders.authorize_order(id, *, pay_pal_request_id=None, prefer=…, body=None, …)` | `id` | `OrderAuthorizeResponse` | `AuthorizeOrderErrorBody` = `Error` [400,401,403,404,422,500] \| `RawError` | only when create answers `APPROVED` without an authorization (smoke: card single-step returns `COMPLETED` + inline authorization) |
| `client.payments.get_authorized_payment(authorization_id, *, …)` | `authorization_id` | `PaymentAuthorization` | `GetAuthorizedPaymentErrorBody` = `Error` [401,403,404] \| `RawError` [500,…] | freshness check before capture; void lookup |
| `client.payments.reauthorize_payment(authorization_id, *, pay_pal_request_id=None, prefer=…, body: ReauthorizeRequest\|Dict\|None=None, …)` | `authorization_id` | `PaymentAuthorization` | `ReauthorizePaymentErrorBody` = `Error` [400,401,403,404,422] \| `RawError` [500,…] | docstring: honor period 3 days; allowed once, day 4–29 from the original authorization; request id kept 45 days. Smoke: fresh auth → 422 `REAUTHORIZATION_TOO_SOON` |
| `client.payments.capture_authorized_payment(authorization_id, *, pay_pal_request_id=None, prefer=…, body: CaptureRequest\|Dict\|None=None, …)` | `authorization_id` | `CapturedPayment` | `CaptureAuthorizedPaymentErrorBody` = `Error` [400,401,403,404,409,422] \| `RawError` [500,…] | request id kept 45 days; **smoke: same-id replay returns the same capture** |
| `client.payments.get_captured_payment(capture_id, *, …)` | `capture_id` | `CapturedPayment` | `GetCapturedPaymentErrorBody` = `Error` [401,403,404] \| `RawError` | refresh a pending capture |
| `client.payments.void_payment(authorization_id, *, pay_pal_request_id=None, prefer=…, …)` | `authorization_id` | `PaymentAuthorization` | `VoidPaymentErrorBody` = `Error` [401,403,404,409,422] \| `RawError` [500,…] | smoke: same-id replay → `VOIDED`; new id after void → 422 issue `PREVIOUSLY_VOIDED` (landing) |
| `client.payments.refund_captured_payment(capture_id, *, pay_pal_request_id=None, prefer=…, body: RefundRequest\|Dict\|None=None, …)` | `capture_id` | `Refund` | `RefundCapturedPaymentErrorBody` = `Error` [400,401,403,404,409,422] \| `RawError` [500,…] | empty body = full refund (docstring); we always send `amount`. Smoke: same-id replay → same refund; over-refund → 422 `REFUND_AMOUNT_EXCEEDED` |
| `client.vault.create_payment_token(body, *, pay_pal_request_id=None, …)` | `body: PaymentTokenRequest\|Dict` | `PaymentTokenResponse` | `CreatePaymentTokenErrorBody` = `Error` [400,403,404,422,500] \| `RawError` | request id kept 3 h; smoke: same-id replay → same token |
| `client.vault.delete_payment_token(id, *, …)` | `id` | **`None`** → use `with_raw_response` (`ApiResult[None, DeletePaymentTokenErrorBody]`) | `Error` [400,403,500] \| `RawError` | smoke: 204, and 204 again on repeat (idempotent) |
| `client.transaction_search.search_transactions(start_date, end_date, *, fields="transaction_info", balance_affecting_records_only="Y", page_size=100, page=1, …)` | `start_date`, `end_date` (RFC 3339 strings, seconds required) | `SearchResponse` | **Case B: always `RawError`** | max range 31 days; up to 3 h reporting lag (docstring). We pass `balance_affecting_records_only="N"` so holds/voids appear. Smoke: 200, `total_pages` 57 → pagination required |

Models the task sets (members; `Optional[T]` = `T | UnsetType`, never `None`):
- `OrderRequest`: `intent: CheckoutPaymentIntentOrStr` (req) · `purchase_units: list[PurchaseUnitRequest]` (req) · `payment_source: Optional[PaymentSource]`.
- `PurchaseUnitRequest`: `amount: AmountWithBreakdown` (req: `currency_code: str`, `value: str`) · `reference_id`, `custom_id`, `description`: `Optional[str]`. **`custom_id` = our per-attempt reference** (the create's client-chosen reference, always set; the lookup key via transaction search `custom_field`).
- `PaymentSource.card: Optional[CardRequest]`: `name`, `number`, `expiry` (`YYYY-MM`), `security_code`, `billing_address: Optional[Address]` (`country_code: str` req; `address_line_1`, `address_line_2`, `admin_area_2`, `admin_area_1`, `postal_code` optional), `vault_id: Optional[str]` (saved-card payment).
- `CaptureRequest`: `amount: Optional[Money]` · `final_capture: Optional[bool]`. `ReauthorizeRequest`: `amount: Optional[Money]`. `RefundRequest`: `amount: Optional[Money]` · `custom_id: Optional[str]`. `Money`: `currency_code: str`, `value: str` (both req).
- `PaymentTokenRequest`: `payment_source: PaymentTokenRequestPaymentSource` (req) → `card: Optional[PaymentTokenRequestCard]` (`name`, `number`, `expiry`, `security_code`, `billing_address`).
- No `Optional[Any]` member is set by us (the `Any` trap does not apply). Wire aliases: only `type_`↔`type` on card responses (we read it nowhere).

Members read (assert on each; an absent one ⇒ `unknown`, never success):
- `Order`: `id`, `status: OrderStatusOrStr`, `purchase_units[0].payments.authorizations[0]` → `id`, `status`, `amount.value/currency_code`, `create_time`, `expiration_time`, `custom_id`; `payment_source.card.last_digits/brand`.
- `PaymentAuthorization`: `id`, `status`, `amount`, `create_time`, `expiration_time`, `custom_id`, `status_details.reason`.
- `CapturedPayment`: `id`, `status`, `amount`, `create_time`, `seller_receivable_breakdown` → `gross_amount` (req), `paypal_fee`, `net_amount` (smoke: present with `return=representation`).
- `Refund`: `id`, `status`, `amount`, `create_time`.
- `PaymentTokenResponse`: `id`, `payment_source.card` → `last_digits`, `brand`, `expiry`, `verification_status`; `create_time` (extra field, preserved; read via `model_extra`). **The token has no status member.**
- `SearchResponse`: `transaction_details[].transaction_info` → `transaction_id`, `paypal_reference_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_amount`, `fee_amount`, `transaction_status`, `custom_field`, `invoice_id`; `total_pages`, `page`, `last_refreshed_datetime`.

Status enums → outcome (`done` / `pending` / `failed`; anything unlisted or absent = `unknown`):
- `AuthorizationStatus` (authorize & reauthorize steps): `CREATED`→done · `CAPTURED`→done · `PARTIALLY_CAPTURED`→done · `PENDING`→pending · `DENIED`→failed · `VOIDED`→failed (undone).
- `OrderStatus` when no authorization is present: `PAYER_ACTION_REQUIRED`→failed (browser challenge — unsupported, reported) · `APPROVED`→pending (needs authorize-order step) · `CREATED`/`SAVED`→pending · `VOIDED`→failed · `COMPLETED` (no auth) → unknown.
- `CaptureStatus` (capture step): `COMPLETED`→done · `PARTIALLY_REFUNDED`→done · `PENDING`→pending · `DECLINED`→failed · `FAILED`→failed · `REFUNDED`→failed (undone).
- `AuthorizationStatus` for the void step (`void_outcome`): `VOIDED`→done · `CAPTURED`/`PARTIALLY_CAPTURED`→failed (too late) · else unknown.
- `RefundStatus` (refund step — its done is the undoing): `COMPLETED`→done · `PENDING`→pending · `FAILED`→failed · `CANCELLED`→failed.
- Vault token (no status member): `payment_source.card.verification_status` `FAILED`→failed; otherwise `id` + `payment_source.card.last_digits` present → done; else unknown.
- Vault delete (`None` return): raw `Success` (2xx) → done; `Failure` 404 → done (already gone); other → failed/unknown per ladder.

Decode: a type-mismatched or non-JSON 2xx raises `ValidationError`/`ValueError` (not `ApiError`) in both modes → outcome unknown for writes. Smoke: a GET on a deleted vault token answers 204 with an empty body → `ValueError`.

Money: amounts are `str`; built with `Decimal.quantize` per currency exponent (ISO 4217 map, default 2), compared as `Decimal`.

## OPERATION OUTCOMES

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/orders/{id}/pay` → `orders.create_order` (+ `authorize_order` if needed) | `purchase_units[0].payments.authorizations[0].status` (`AuthorizationStatus`), else `Order.status` | CREATED/CAPTURED/PARTIALLY_CAPTURED → done → 200 `authorized`; PENDING → pending → 202; DENIED/VOIDED → failed → 402 `card_declined` (order stays payable); order PAYER_ACTION_REQUIRED → failed → 409 `payer_action_required`; APPROVED/CREATED/SAVED → pending → authorize-order step; unlisted/absent → unknown → 504 `outcome_unknown` | `payments.authorize` → `payments._run_authorize` (reads via `payments._read_paypal_order`, maps via `payments._authorize_step_outcome` → `outcomes.authorization_outcome` / `outcomes.order_outcome`) → `payments._apply_authorization`; answered by `views.pay` through `outcomes.answer` |
| `POST /api/orders/{id}/fulfil` → `payments.reauthorize_payment` (stale only) | `PaymentAuthorization.status` | CREATED → done → continue to capture; PENDING → pending → 202; DENIED/VOIDED → failed → 409 `authorization_not_renewable`; 4xx refusal → 409 with PayPal issue + operator action; unlisted → unknown → 504 | `payments._renew` (`safe_write` with `outcomes.authorization_outcome`); refusals turned into `payments._expired`; answered by `views.fulfil` |
| `POST /api/orders/{id}/fulfil` → `payments.capture_authorized_payment` | `CapturedPayment.status` | COMPLETED/PARTIALLY_REFUNDED → done → 200 `captured` with amount/fee/net; PENDING → pending → 202 `capture_pending`; DECLINED/FAILED/REFUNDED → failed → 409; unlisted/absent → unknown → 504 | `payments._capture` (`outcomes.capture_outcome`, read by `payments._capture_answer`) → `payments._apply_capture`; pending refresh in `payments.fulfil`; answered by `views.fulfil` through `outcomes.answer` |
| `POST /api/orders/{id}/cancel` → `payments.void_payment` | `PaymentAuthorization.status` | VOIDED → done → 200 `cancelled`; CAPTURED/PARTIALLY_CAPTURED → failed → 409 `already_captured`; 422 `PREVIOUSLY_VOIDED` → landing → lookup → VOIDED → done; unlisted → unknown → 504 | `payments.cancel` (`outcomes.void_outcome`; landing via `errors.issues_of`); answered by `views.cancel` through `outcomes.answer` |
| `POST /api/orders/{id}/refunds` → `payments.refund_captured_payment` | `Refund.status` | COMPLETED → done → 201; PENDING → pending → 202; FAILED/CANCELLED → failed → 409 (reservation released); unlisted/absent → unknown → 504 (reservation kept) | `payments.refund` → `payments._run_refund` (`outcomes.refund_outcome`, read by `payments._refund_answer`) → `payments._apply_refunds`; answered by `views.refunds` through `outcomes.answer` |
| `POST /api/payment-methods` → `vault.create_payment_token` | no status member; `payment_source.card.verification_status` + presence of `id`/`last_digits` | verification FAILED → failed → 422 `card_not_verified`; id+last_digits → done → 201; else unknown → 504 | `cards.save_card` (read by `cards._token_answer`, mapped by `outcomes.vault_token_outcome`); answered by `views.payment_methods` through `outcomes.answer` |
| `DELETE /api/payment-methods/{id}` → `vault.delete_payment_token` | HTTP status (raw peer; returns `None`) | 2xx → done → 200 `providerDeletion: done`; 404 → done; other 4xx → failed → 200 with `providerDeletion: failed` (card already unusable locally; operator retry command); 5xx/transport → unknown → `providerDeletion: pending_retry` | `cards.delete_vault_token` (raw `vault.with_raw_response.delete_payment_token`, 404 → `'gone'`); reported by `views.payment_method` as `vaultDeletion` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| create_order (authorize attempt N of order) | `ProviderWrite` row, `ref = {prefix}:order:{number}:authorize:{N}` (N = count of failed attempts) | DB unique constraint on `ProviderWrite.ref` (committed before the call) | `IntegrityError` in `try_claim` → answer from the existing row | `payments.authorize` (`safe_write.try_claim`; loser answered via `payments._run_authorize` → `safe_write.safe_write`) |
| authorize_order (fallback step) | `ProviderWrite`, `ref = …:authorize:{N}:order-authorize` | same unique constraint | same | `payments._authorize_paypal_order` (`safe_write.safe_write` claim) |
| reauthorize_payment | `ProviderWrite`, `ref = {prefix}:order:{number}:reauthorize:{authorization_id}` | same unique constraint | same | `payments._renew` (`safe_write.safe_write` claim) |
| capture_authorized_payment | `ProviderWrite`, `ref = {prefix}:order:{number}:capture:{authorization_id}` | same unique constraint | same | `payments._capture` (`safe_write.safe_write` claim) |
| void_payment | `ProviderWrite`, `ref = {prefix}:order:{number}:void:{authorization_id}` | same unique constraint | same | `payments.cancel` (`safe_write.safe_write` claim) |
| refund_captured_payment | `ProviderWrite`, `ref = {prefix}:order:{number}:refund:{sha256(user, Idempotency-Key)}` — same key = repeat, new key = new refund | same unique constraint; over-refund additionally blocked by reserving the amount inside the claim transaction under `select_for_update` of the `PayPalPayment` row | `IntegrityError` → existing row; insufficient refundable → 409 | `payments.refund` (reservation: `select_for_update` + `orders.refundable_amount` + `safe_write.try_claim` in one transaction; fingerprint check on reuse) |
| create_payment_token | `ProviderWrite`, `ref = {prefix}:user:{pk}:vault:{sha256(Idempotency-Key)}` (a request without a key is a new save) | same unique constraint | same | `cards.save_card` (`safe_write.safe_write` claim) |
| delete_payment_token | `ProviderWrite`, `ref = {prefix}:vault-delete:{token_id}`, created in the same transaction that deletes the `Bankcard` | same unique constraint (second DELETE finds no card → 404) | `IntegrityError`/`DoesNotExist` | `cards.delete_card` (`Bankcard` delete + `safe_write.try_claim` in one transaction) |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| create_order | lookup: `transaction_search.search_transactions` over [claimed_at − 1 h, now], `balance_affecting_records_only="N"`, filter `custom_field == ref`, then `get_authorized_payment(transaction_id)` and verify its `custom_id == ref` (never a resend — replay is refused) | `custom_id` = claim `ref` | `safe_write.safe_write` step 3 calling `payments._run_authorize.find` → `reconcile.find_authorization_by_custom_id`; operator re-check `paypal_retry_writes` → `payments.run_pending_check` |
| authorize_order | lookup: `orders.get_order(paypal_order_id)` → its authorization | PayPal order id recorded before the step | `safe_write.safe_write` step 3 calling `payments._authorize_paypal_order.find` (SDK `client.orders.get_order`) |
| reauthorize_payment | same-reference resend (`PayPal-Request-Id`, 45 d) | claim `ref` | `safe_write.safe_write` (`repeat_is_safe=True`) with `payments._renew`'s `find` = same-id `reauthorize_payment` |
| capture_authorized_payment | same-reference resend (`PayPal-Request-Id`, 45 d; smoke-verified) | claim `ref` | `safe_write.safe_write` (`repeat_is_safe=True`) with `payments._capture.send` as `find`; re-entered from `payments.fulfil` (state `capturing`) and `payments.run_pending_check` |
| void_payment | same-reference resend (45 d; smoke-verified); a 422 `PREVIOUSLY_VOIDED` answer is a landing → `get_authorized_payment` | claim `ref` | `safe_write.safe_write` (`repeat_is_safe=True`, `landed=` PREVIOUSLY_VOIDED) with `payments.cancel`'s `find` = `get_authorized_payment` |
| refund_captured_payment | same-reference resend (45 d; smoke-verified) | claim `ref` | `safe_write.safe_write` (`repeat_is_safe=True`) with `payments._run_refund.send` as `find`; re-entered by a repeat with the same Idempotency-Key or `payments.run_pending_check` |
| create_payment_token | same-reference resend (3 h; smoke-verified) | claim `ref` | `safe_write.safe_write` (`repeat_is_safe=True`) with `cards.save_card.send` as `find`; re-entered by the shopper repeating with the same Idempotency-Key (card data is never stored, so the command cannot) |
| delete_payment_token | same-reference resend (delete by id is idempotent: 204 twice, smoke-verified) via `paypal_retry_writes` command | token id | `cards.delete_vault_token` (`find` = same `send`), retried by `management/commands/paypal_retry_writes.py` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| create_order | Oscar `Order` + `PayPalPayment` (state `authorizing`) + committed `ProviderWrite(ref, sending, amount, currency)` | PayPal order id, authorization id/status/expiry/time, outcome; Oscar `Source.allocate` + `Transaction(Authorise)`; order → `Being processed` | before: `orders.place_order` + `payments.authorize` (`try_claim`, state `authorizing`); after: `payments._apply_authorization` (`Source.allocate`, order → Being processed) |
| authorize_order | `ProviderWrite(sending)` carrying the PayPal order id | as above | before: `payments._authorize_paypal_order` claim (`target_id` = PayPal order id); after: `payments._apply_authorization` |
| reauthorize_payment | `ProviderWrite(sending)` | new authorization id/status/time on `PayPalPayment` | before: `payments._renew` claim; after: the `select_for_update` block at the end of `payments._renew` |
| capture_authorized_payment | `ProviderWrite(sending, amount)`; `PayPalPayment.state = capturing` | capture id/status, gross/fee/net, provider time; `Source.debit`; order → `Complete` | before: `payments._capture` claim + state `capturing`; after: `payments._apply_capture` (`Source.debit`, order → Complete) |
| void_payment | `ProviderWrite(sending)` | auth status VOIDED; order → `Cancelled`; `Transaction(Void)` | before: `payments.cancel` claim; after: the atomic block in `payments.cancel` (`Transaction` Void, order → Cancelled) |
| refund_captured_payment | `ProviderWrite(sending, amount, key hash)` — the reservation | refund id/status/time; `Source.refund`; refunded total & state | before: `payments.refund` (claim carrying amount + fingerprint); after: `payments._apply_refunds` (`Source.refund`, state) |
| create_payment_token | `ProviderWrite(sending)` for the user | `Bankcard(user, brand, XXXX-last4, expiry, partner_reference=token)`; token id on the write | before: `cards.save_card` claim; after: `Bankcard` creation in `cards.save_card` |
| delete_payment_token | `Bankcard` already deleted + `ProviderWrite(sending, provider_id=token)` committed together | outcome of the provider deletion | before: `cards.delete_card` (card deleted + claim, one transaction); after: `cards.delete_vault_token` |

## Other decisions

- Order/payment states (`PayPalPayment.state`): `awaiting_payment`, `authorizing`, `authorized`, `authorization_pending`,
  `authorization_unknown`, `authorization_expired`, `capturing`, `capture_pending`, `captured`, `partially_refunded`,
  `refunded`, `voided`, `needs_review`. Oscar order status mirrors: Pending → Being processed (authorized) → Complete
  (captured); Cancelled.
- Staleness: authorization older than the 3-day honor period (from its `create_time`) → reauthorize once (days 4–29);
  past `expiration_time`, already reauthorized, or refused → 409 with operator-actionable text; state
  `authorization_expired` re-opens `pay` for the shopper.
- Access: fulfil/cancel/reconciliation require `is_staff` (403); every other endpoint acts on the caller's own rows
  only (another user's order/card → 404). Unauthenticated → 401 JSON. CSRF stays enforced (session auth).
- Card data: never stored (only `Bankcard` masked number/expiry/brand), never logged (logging transport logs method,
  path, status only; `httpx`/`httpcore` loggers at WARNING; `sensitive_variables`/`sensitive_post_parameters` on
  card-handling code so Django error reports omit them).
- Reconciliation: split [from, to) into ≤31-day windows; page every window to `total_pages`; keep provider rows with
  `transaction_initiation_date` in [from, to); local side = `ProviderWrite` rows (authorize/capture/refund/reauthorize/
  void) whose stored **provider time** is in the window; unsettled = rows with no provider time (sending/unknown).
  Match by provider id (`transaction_id` / `paypal_reference_id`) as a set; provider rows carrying our prefix in
  `custom_field` but unmatched are flagged `ours_unrecorded`; local rows newer than `last_refreshed_datetime` are
  `not_yet_reported` (reporting lag), not missing.
- Reference prefix: `PAYPAL_REFERENCE_PREFIX` setting if set, else a random install id persisted once in the DB
  (`InstallIdentity`), so two installs sharing a PayPal account (or a rebuilt DB) never reuse a reference.
- SQLite: `sandbox/settings.py` sets `transaction_mode: IMMEDIATE` (+ 20 s busy timeout) for the SQLite default, so
  concurrent requests queue for the write lock instead of deadlocking on a read→write upgrade (seen live on the
  double-click test before the change). A still-locked database answers 503 `busy` (`views.api`).
- Declines: a PayPal refusal of `pay` answers 402 `payment_declined` with PayPal's issue codes; the order stays payable.
- Retries: none added. Operator command `paypal_retry_writes` re-checks `unknown`/stale `sending` writes through the
  same safe write (lookup / same-reference resend) and retries unfinished vault deletions.

## Verification record

- `mypy --strict` clean on `safe_write.py errors.py outcomes.py money.py paypal_client.py reconcile.py payments.py
  cards.py references.py`.
- `manage.py test apps.payments_api`: 45 tests (stub transport at the SDK transport seam) — pass.
- Live sandbox run over HTTP (`/api/...` only): authorization (double click → one hold), capture with fee/net,
  idempotent + distinct partial refunds, over-refund refused, saved card vaulted and reused, void, card deletion,
  cross-shopper isolation, reconciliation across two 31-day windows and all pages — all passed.
- Host-note correction: `oscar_import_catalogue sandbox/fixtures/*.csv` is NOT optional — without it the catalogue has
  11 products and `orders.json` fails with a foreign-key error; with it: 209 products, 249 countries, 1 order.

## Assumptions & Blockers

- No blockers. Claim store = the project's own DB (unique constraint), which outlives the process.
- Minor: production host is not declared by the SDK → non-sandbox environments require `PAYPAL_BASE_URL`.
- Minor: create_order unknown outcomes can only be settled via transaction search, which lags up to 3 h — such an
  order stays `authorization_unknown` (504, visible, not re-payable) until the check finds it or an operator resolves it.
- Minor: offers/vouchers are not applied to API orders — amounts are catalogue prices (task requirement).

## REQUIRED READING

- Client construction/lifetime, sync choice — MUST load `python-client-initialization` (loaded).
- Credentials, token-fetch failure — MUST load `python-authentication` (loaded).
- Calls, `prefer` default, `-> None` delete, status→outcome, `answer` — MUST load `python-calling-endpoints` (loaded).
- `UNSET` vs `None`, open enums, money strings — MUST load `python-models` (loaded).
- Error ladder, unsent vs unknown transport failures, decode failures — MUST load `python-error-handling` (loaded).
- Safe write, claims, no retries, reconciliation on provider clock, logging transport — MUST load `python-configuration-resilience` (loaded).
- Stub transport tests — MUST load `python-testing` (loaded).
