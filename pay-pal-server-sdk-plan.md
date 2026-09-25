# PayPal integration plan — django-oscar sandbox (`sandbox/apps/payments`)

Scope: PayPal card payments (authorize → capture at fulfilment → refund / void) and vaulted cards for the
sandbox storefront, exposed as a JSON API under `/api/`. Additive: Oscar's catalogue/basket/order flow is
reused, not replaced.

## SDK identity (verified against the installed package, 2026-09-25)

| Fact | Value | Source |
| --- | --- | --- |
| Distribution / import root | `paypal` / `paypal` — **drift**: the getting-started skill says `pay-pal-server-sdk` / `pay_pal_server_sdk`; the SDK map and the installed package both say `paypal` | `sdk-map.md`, `pip show paypal` |
| Version | `2.29` | `pyproject.toml` |
| Install | `pip install -r sandbox/apps/payments/requirements.txt` → `paypal @ https://codeload.github.com/context-plugins/paypal-python-sdk/zip/0ed3d22071e97650113c99fd82c42bc815f99dcf` (branch `main` head on 2026-09-25, byte-identical to what was verified; github.com git transport is reset on this host, the codeload archive works). Non-editable. | host probe |
| Client | `PaypalClient` (sync), keyword-only ctor: `base_url`, `timeout=30.0`, `custom_http_client`, `oauth2`, `oauth2_token_source` | `sdk-map.md` Getting a client |
| Auth | `oauth2=ClientCredentials(client_id=..., client_secret=...)` from `paypal.core`; token from `<base_url>/v1/oauth2/token`, lazy, cached per client. Omitting `oauth2` = unauthenticated, silent. Failed token fetch = `ApiError` whose `.error` is `OAuthProviderError \| RawError`, raised even from `with_raw_response`. | `sdk-map.md` Servers & auth; python-authentication |
| Host | SDK declares ONE server: `https://api-m.sandbox.paypal.com` (the default when `base_url` omitted). App: `PAYPAL_BASE_URL` if set (verbatim, token call included since it derives from base_url); else `PAYPAL_ENVIRONMENT == "sandbox"` → that URL; any other environment without `PAYPAL_BASE_URL` → `ImproperlyConfigured` (the SDK knows no other host; no URL is invented). | `sdk-map.md` Servers & auth |
| Sync vs async | **Sync** `PaypalClient` — Django under WSGI/runserver, sync views. Never `AsyncPaypalClient`. | repo survey |
| Client lifetime | one module-level client, built lazily on first use (after any fork), closed via `atexit`; never per request. | python-client-initialization |
| Retries | SDK performs none. App: no automatic retry of writes (safe write below settles unknowns); bounded retry (3 attempts, backoff) for idempotent reads only (`search_transactions`, `get_*`) on never-sent transport errors, 429, 5xx. | python-configuration-resilience |
| Timeout | client `timeout=PAYPAL_TIMEOUT_SECONDS` (default 20.0). | |
| Logging | `LoggingTransport` wrapping `HttpxClient`: logs method, URL, status, ms — never headers or bodies. | python-configuration-resilience |
| Error model | single `ApiError` (`.error`, `.status_code`, `.response`); decode failure raises `ValidationError`/`ValueError` in both modes; httpx exceptions unwrapped. | `sdk-map.md` Error-handling model |

## Contract sheet

Every keyword-only parameter has a real default: pass only what is set below; never defensive `None`s.
Every op also exists as `client.<group>.with_raw_response.<op>` returning `ApiResult` (`Success`/`Failure`).

| # | Operation | Positional | Keyword-only used | Returns (parsed) | `ApiError.error` union (status → arm) | Source |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `client.orders.create_order` `POST /v2/checkout/orders` | `body: OrderRequest` | `pay_pal_request_id`, `prefer="return=representation"` (default `return=minimal` omits purchase_units) | `Order` | `CreateOrderErrorBody` = `Error` [400,401,422] \| `RawError` [else; observed 403 for a deleted vault token] | map orders.md |
| 2 | `client.orders.authorize_order` `POST /v2/checkout/orders/{id}/authorize` | `id: str` | `pay_pal_request_id`, `prefer="return=representation"` | `OrderAuthorizeResponse` | `AuthorizeOrderErrorBody` = `Error` [400,401,403,404,422,500] \| `RawError` | map orders.md |
| 3 | `client.orders.get_order` `GET /v2/checkout/orders/{id}` | `id: str` | — | `Order` | `GetOrderErrorBody` = `Error` [401,404] \| `RawError` | map orders.md |
| 4 | `client.payments.capture_authorized_payment` `POST /v2/payments/authorizations/{authorization_id}/capture` | `authorization_id: str` | `pay_pal_request_id` (kept 45 days), `prefer="return=representation"`, `body: CaptureRequest` | `CapturedPayment` | `CaptureAuthorizedPaymentErrorBody` = `Error` [400,401,403,404,409,422] \| `RawError` [500, else] | map payments.md |
| 5 | `client.payments.reauthorize_payment` `POST /v2/payments/authorizations/{authorization_id}/reauthorize` | `authorization_id: str` | `pay_pal_request_id` (45 days), `prefer="return=representation"`, `body: ReauthorizeRequest` | `PaymentAuthorization` | `ReauthorizePaymentErrorBody` = `Error` [400,401,403,404,422] \| `RawError` [500, else] | map payments.md |
| 6 | `client.payments.void_payment` `POST /v2/payments/authorizations/{authorization_id}/void` | `authorization_id: str` | `pay_pal_request_id` (45 days), `prefer="return=representation"` — **mandatory**: with the default `return=minimal` sandbox answers an empty 2xx and the SDK raises `ValueError` decoding it (observed) | `PaymentAuthorization` | `VoidPaymentErrorBody` = `Error` [401,403,404,409,422] \| `RawError` [500, else] | map payments.md; smoke |
| 7 | `client.payments.get_authorized_payment` `GET /v2/payments/authorizations/{authorization_id}` | `authorization_id: str` | — | `PaymentAuthorization` | `GetAuthorizedPaymentErrorBody` = `Error` [401,403,404] \| `RawError` [500, else] | map payments.md |
| 8 | `client.payments.get_captured_payment` `GET /v2/payments/captures/{capture_id}` | `capture_id: str` | — | `CapturedPayment` | `GetCapturedPaymentErrorBody` = `Error` [401,403,404] \| `RawError` | map payments.md |
| 9 | `client.payments.refund_captured_payment` `POST /v2/payments/captures/{capture_id}/refund` | `capture_id: str` | `pay_pal_request_id` (45 days), `prefer="return=representation"`, `body: RefundRequest` | `Refund` | `RefundCapturedPaymentErrorBody` = `Error` [400,401,403,404,409,422] \| `RawError` [500, else] | map payments.md |
| 10 | `client.payments.get_refund` `GET /v2/payments/refunds/{refund_id}` | `refund_id: str` | — | `Refund` | `GetRefundErrorBody` = `Error` [401,403,404] \| `RawError` | map payments.md |
| 11 | `client.vault.create_payment_token` `POST /v3/vault/payment-tokens` | `body: PaymentTokenRequest` | `pay_pal_request_id` (kept 3 hours) | `PaymentTokenResponse` | `CreatePaymentTokenErrorBody` = `Error` [400,403,404,422,500] \| `RawError` | map vault.md |
| 12 | `client.vault.delete_payment_token` `DELETE /v3/vault/payment-tokens/{id}` | `id: str` | — | **`None`** → use `with_raw_response` (`ApiResult[None, DeletePaymentTokenErrorBody]`) to see the status (204 observed; repeat delete also 204) | `DeletePaymentTokenErrorBody` = `Error` [400,403,500] \| `RawError` | map vault.md; smoke |
| 13 | `client.transaction_search.search_transactions` `GET /v1/reporting/transactions` | `start_date: str`, `end_date: str` (RFC 3339 with seconds; max range 31 days) | `fields="transaction_info"`, `balance_affecting_records_only="N"` (default `"Y"` hides authorizations), `page_size=100`, `page` | `SearchResponse` | **Case B**: `.error` always `RawError`. Observed: a window starting past PayPal's reporting horizon answers **404** `{"name":"INVALID_REQUEST","message":"Data for the given start date is not available."}` — treated as "no records yet" (`paypal_gateway._data_not_available_yet`), and the report shows `providerDataUntil` (`last_refreshed_datetime`) | map transaction_search.md; docstring; smoke |

Auth failure on any of them: `ApiError(error=OAuthProviderError | RawError)` — checked first in the ladder.

### Models set by the app (required vs `UNSET`; `Optional[T]` = `T | UnsetType`, never `None`)

| Model (module) | Members the app sets | Required | Notes |
| --- | --- | --- | --- |
| `OrderRequest` (`models/order_request.py`) | `intent`, `purchase_units`, `payment_source` | `intent: CheckoutPaymentIntentOrStr`, `purchase_units` required | `intent=CheckoutPaymentIntent.AUTHORIZE` |
| `PurchaseUnitRequest` (`purchase_unit_request.py`) | `amount`, `reference_id`, `invoice_id`, `custom_id`, `description` | `amount` | **`invoice_id` is the client-chosen reference — always set.** PayPal refuses a reused one with 422 issue `DUPLICATE_INVOICE_ID` (observed) |
| `AmountWithBreakdown` / `Money` | `currency_code: str`, `value: str` | both | value formatted with `Decimal.quantize` per ISO-4217 exponent of `PAYPAL_CURRENCY` |
| `PaymentSource` (`payment_source.py`) | `card: CardRequest` | — | |
| `CardRequest` (`card_request.py`) | one-off: `name`, `number`, `expiry` (`YYYY-MM`), `security_code`, `billing_address`; saved: `vault_id` only | none | |
| `Address` (`address.py`) | `address_line_1`, `address_line_2`, `admin_area_2`, `admin_area_1`, `postal_code`, `country_code` | `country_code: str` | |
| `CaptureRequest` | `amount: Money`, `final_capture: bool` | none | |
| `ReauthorizeRequest` | `amount: Money` | none | |
| `RefundRequest` | `amount: Money`, `note_to_payer` | none | amount always sent explicitly (full = remaining) |
| `PaymentTokenRequest` (`payment_token_request.py`) | `payment_source`, `customer` | `payment_source` | `customer=Customer(id=<paypal customer id>)` once known |
| `PaymentTokenRequestPaymentSource` / `PaymentTokenRequestCard` | `card`: `name`, `number`, `expiry`, `security_code`, `billing_address` | none | direct card vaulting works in sandbox (observed); no setup token needed |
| `Customer` (`customer.py`) | `id` | none | |

### Response members the app reads (each asserted after the call; absent = outcome unknown)

| Response | Members read | Status enum → outcome |
| --- | --- | --- |
| `Order` / `OrderAuthorizeResponse` | `id`, `status`, `purchase_units[0].payments.authorizations[0]` (`AuthorizationWithAdditionalData`: `id`, `status`, `amount`, `create_time`, `expiration_time`), `payment_source.card` (`last_digits`, `brand`) | `OrderStatus`: `COMPLETED` → read the authorization's status; `APPROVED` → run authorize step; `CREATED`, `SAVED`, `PAYER_ACTION_REQUIRED` → pending; `VOIDED` → failed; unlisted/absent → unknown |
| `PaymentAuthorization` / `AuthorizationWithAdditionalData` | `id`, `status`, `amount`, `create_time`, `expiration_time`, `status_details.reason` | `AuthorizationStatus` (authorize/reauthorize): `CREATED` → done; `CAPTURED`, `PARTIALLY_CAPTURED` → done (hold landed and was taken); `PENDING` → pending; `DENIED`, `VOIDED` → failed; unlisted/absent → unknown. (void): `VOIDED` → done; `DENIED` → done (nothing held); `CREATED`, `PENDING` → pending; `CAPTURED`, `PARTIALLY_CAPTURED` → failed; unlisted → unknown |
| `CapturedPayment` | `id`, `status`, `amount`, `create_time`, `seller_receivable_breakdown` (`gross_amount` — **the one required member on a 2xx path**, `paypal_fee`, `net_amount`) | `CaptureStatus`: `COMPLETED` → done; `PENDING` → pending; `DECLINED`, `FAILED` → failed; `REFUNDED`, `PARTIALLY_REFUNDED` → failed (done then undone); unlisted → unknown |
| `Refund` | `id`, `status`, `amount`, `create_time` | `RefundStatus`: `COMPLETED` → done; `PENDING` → pending; `FAILED`, `CANCELLED` → failed; unlisted → unknown |
| `PaymentTokenResponse` | `id`, `customer.id`, `payment_source.card` (`last_digits`, `brand`, `expiry`, `name`), `create_time` via `model_extra` not used | no status member exists on this resource: done ⇔ `id` AND `payment_source.card.last_digits` present; otherwise unknown |
| `SearchResponse` | `transaction_details[].transaction_info` (`transaction_id`, `paypal_reference_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_amount`, `fee_amount`, `transaction_status`, `invoice_id`, `custom_field`), `total_pages`, `page` | report only (no write) |
| `Error` (typed error arm) | `name`, `message`, `debug_id` (required), `details[].issue` | observed issues: `DUPLICATE_INVOICE_ID`, `TRANSACTION_REFUSED`, `REFUND_AMOUNT_EXCEEDED`, `PREVIOUSLY_VOIDED`, `AUTHORIZATION_VOIDED`, `REAUTHORIZATION_TOO_SOON` |

Other facts: reauthorize is allowed once, Day 4–29 after the original authorization (observed issue text);
honor period 3 days; authorization `expiration_time` = create + 29 days (observed). Same-key resend of
`create_order` (card, single-step) is **refused** with 422 `TRANSACTION_REFUSED` (observed) — create_order is
not deduplicated by key; capture, refund and void resends under the same key return the original (observed).

## Design

- New app `sandbox/apps/payments` (label `sandbox_payments`), routed at `/api/` from `sandbox/urls.py`
  (outside `i18n_patterns`, like `admin/`). Sync Django views returning `JsonResponse`.
- Reuse Oscar: `order.Order`/`Line` placed through Oscar's `OrderCreator` from a fresh `Basket`
  (prices from the partner strategy); `payment.Source`/`Transaction`/`SourceType` record allocate/debit/
  refund; `payment.Bankcard` is the saved card (masked number, `partner_reference` = PayPal token id).
- New models (only what Oscar lacks): `PayPalPayment` (1:1 with `payment.Source`: PayPal order/auth/
  capture ids + statuses, fee, net, refunded total), `PayPalCustomer` (user → PayPal vault customer id),
  `PaymentOperation` (the claim ledger — one row per provider write step, unique `ref`).
- Order statuses added to the sandbox pipeline: `Awaiting payment` → `Payment authorised` → `Complete`
  (fulfilled) and → `Cancelled`.
- Auth: Django session. `POST /api/login` / `POST /api/logout` (JSON wrappers over
  `django.contrib.auth.authenticate/login`, same session the storefront uses). CSRF enforced on all other
  unsafe methods (`X-CSRFToken`). Fulfil/cancel/reconciliation require `is_staff`; everything else scoped
  to `request.user`; foreign objects answer 404.
- Views are `transaction.non_atomic_requests` (settings have `ATOMIC_REQUESTS=True`): a claim must commit
  before the provider call.
- References: `PAYMENTS_REFERENCE_PREFIX` (env; default derived from a hash of SECRET_KEY + DB name) —
  unique per install. `ref = f"{prefix}:{order_number}:{step}:{…}"`; PayPal-Request-Id = UUID5 of ref;
  invoice id = `f"{prefix}-{order_number}-{attempt}"`.
- Card data: only in request memory → SDK model → PayPal. Never stored (Bankcard stores `XXXX-XXXX-XXXX-<last4>`
  from PayPal's `last_digits`), never logged (`sensitive_variables`/`sensitive_post_parameters` on every
  frame holding it; transport logs no bodies).

## OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| `orders.create_order` (pay: single-step card authorization) | `Order.status`, then `purchase_units[0].payments.authorizations[0].status` | `COMPLETED` + auth `CREATED`/`CAPTURED`/`PARTIALLY_CAPTURED` → done: order `Payment authorised`, Source.allocate; `COMPLETED` + auth `PENDING` → pending (202, nothing downstream); auth `DENIED`/`VOIDED` → failed (402, shopper may retry with a new attempt); `APPROVED` → pending, then authorize step (row below); `CREATED`/`SAVED` → pending; `PAYER_ACTION_REQUIRED` → pending + `payer_action_required` flag (409 "3-D Secure challenge not supported", no approval round-trip); `VOIDED` → failed; unlisted/absent/unreadable → unknown (202 for an unlisted status; 504 outcome_unknown when unreadable) | `paypal_gateway.order_outcome` + `paypal_gateway.authorization_outcome` via `paypal_gateway.read_checkout_order`; applied in `services._on_authorized`; answered in `views.pay_view` |
| `orders.authorize_order` (only when create answered `APPROVED`) | `OrderAuthorizeResponse.purchase_units[0].payments.authorizations[0].status` | `CREATED`/`CAPTURED`/`PARTIALLY_CAPTURED` → done; `PENDING` → pending; `DENIED`/`VOIDED` → failed; unlisted/absent → unknown | `services._authorize_approved` (read = `paypal_gateway.read_checkout_order`, on_complete = `services._on_authorized`) |
| `payments.reauthorize_payment` (fulfil of a stale authorization) | `PaymentAuthorization.status` | `CREATED`/`CAPTURED`/`PARTIALLY_CAPTURED` → done: new auth id becomes current (only while fulfilment holds the capturing mutex; otherwise the new hold is flagged `needs_review`); `PENDING` → pending (re-read with `get_authorized_payment`; still pending → 409 `reauthorization_pending`, mutex kept); `DENIED`/`VOIDED` → failed → try the existing hold, else "cannot be renewed"; unlisted → unknown (mutex kept until re-checked) | `services._renew_if_stale` (read = `paypal_gateway.read_authorization(…, authorization_outcome)`, pending via `services._refresh_pending`), `services._on_reauthorized`, `services._release_unless_captured`, `services._not_renewable` |
| `payments.capture_authorized_payment` (fulfil) | `CapturedPayment.status` | `COMPLETED` → done: record gross/fee/net, Source.debit, order `Complete`; `PENDING` → pending (202; repeat fulfil refreshes via `get_captured_payment`); `DECLINED`/`FAILED` → failed (402 to operator); `REFUNDED`/`PARTIALLY_REFUNDED` → failed (undone); unlisted → unknown | `paypal_gateway.capture_outcome` via `paypal_gateway.read_capture`; `services._on_captured`; `services.fulfil` (402 `capture_declined`, pending refresh via `services._refresh_pending`); `views.fulfil_view` |
| `payments.void_payment` (cancel) | `PaymentAuthorization.status` | `VOIDED` → done: order `Cancelled`; `DENIED` → done (nothing held); `CREATED`/`PENDING` → pending (202; a repeat cancel re-reads it with `get_authorized_payment`); `CAPTURED`/`PARTIALLY_CAPTURED` → failed (409 `void_refused`); unlisted → unknown. A hold past its expiration time is cancelled locally with no void (PayPal lets it lapse) | `paypal_gateway.void_outcome` in `services._void_write`; `services._on_voided`; `services.cancel` (pending refresh via `services._refresh_pending`, expiry branch), `services._void`; `views.cancel_view` |
| `payments.refund_captured_payment` | `Refund.status` | `COMPLETED` → done: Source.refund, refunded total += amount; `PENDING` → pending (202; repeat request refreshes via `get_refund`); `FAILED`/`CANCELLED` → failed (reservation released); unlisted → unknown | `paypal_gateway.refund_outcome` via `paypal_gateway.read_refund`; `services._on_refunded`; `services.refund` (+ `services._refresh_pending`); `views.refunds_view` |
| `vault.create_payment_token` | none on this resource | `id` + `payment_source.card.last_digits` present → done: Bankcard created; either absent → unknown (202); no other value exists | `paypal_gateway.read_payment_token`; `services._on_vaulted`; `views.payment_methods_view` |
| `vault.delete_payment_token` | HTTP status (op returns `None`; raw peer) | 2xx or 404 → done (token gone); `Failure` → failed (local card already unusable; op row kept for `paypal_retry_deletions`); transport unknown → unknown | `paypal_gateway.delete_payment_token` (matches `Success`/`Failure`); `services.delete_from_vault`; `views.payment_method_view` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| place order (`POST /api/orders`, local only) with `Idempotency-Key` | `PaymentOperation` row, ref `{prefix}:user:{uid}:place:{sha(key)}`, inserted in the same DB transaction as the order | DB UNIQUE constraint on `PaymentOperation.ref` | `IntegrityError` → answer the order already created under that key | `services.place_order` (claim create + `except IntegrityError`) |
| `orders.create_order` (pay attempt n) | `PaymentOperation` ref `{prefix}:{order}:pay:{n}` (n = attempts so far + 1; a new attempt only after a failed one). Only the request that wins the order-level mutex (`PayPalPayment.lifecycle` awaiting→authorizing, a conditional UPDATE) may derive n — a live 5-way race before this rule produced two attempts with different n | conditional UPDATE (loser → 202, no call); UNIQUE on `ref` | `services.pay` returns None → `views.pay_view` 202; `IntegrityError` in `safe_write.insert_claim` → `safe_write.run` loser path answers from the stored record (in flight → 202) or checks it | `services._start_attempt`, `services.pay`, `safe_write.insert_claim`, `safe_write.run` step 1 |
| `orders.authorize_order` | `PaymentOperation` ref `{prefix}:{order}:pay:{n}:authorize` | UNIQUE on `ref` | same | `services._authorize_approved`, `safe_write.insert_claim`, `safe_write.run` |
| `payments.reauthorize_payment` | ref `{prefix}:{order}:reauthorize:{auth_id}`; fulfil holds the authorized→capturing mutex | UNIQUE on `ref` | same | `services._renew_if_stale`, `services.fulfil` (`services._transition`), `safe_write.run` |
| `payments.capture_authorized_payment` | ref `{prefix}:{order}:capture:{auth_id}`; mutex authorized→capturing (conditional UPDATE) so fulfil and cancel cannot both write | UNIQUE on `ref` | same | `services._capture_write`, `services.fulfil`, `safe_write.insert_claim`, `safe_write.run` |
| `payments.void_payment` | ref `{prefix}:{order}:void:{auth_id}`; mutex authorized→voiding | UNIQUE on `ref` | same | `services._void_write`, `services.cancel`, `safe_write.run` |
| `payments.refund_captured_payment` | ref `{prefix}:{order}:refund:{sha(caller key)}`, inserted (or a refused one re-taken) under a write lock on the payment row together with the refundable-ceiling check | UNIQUE on `ref`; ceiling check serialized by the payment row write-lock | `IntegrityError` → same-key repeat (answered from stored; different amount → 422 `idempotency_key_reused`) | `services.refund` (nested `claim`), `safe_write.run` |
| `vault.create_payment_token` | ref `{prefix}:user:{uid}:vault:{sha(caller key)}` | UNIQUE on `ref` | same (different card under the same key → 422 via fingerprint) | `services.save_card`, `safe_write.insert_claim`, `safe_write.run` |
| `vault.delete_payment_token` | not a create/charge: deleting by id is harmless to repeat (repeat observed 204); local row deleted first, op row (`get_or_create` by ref) records the provider call | n/a | n/a | `services.delete_card` |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| `orders.create_order` | lookup only (a same-key resend is refused, and a new key is a second charge): `search_transactions` over [claimed_at − 1h, now] with `balance_affecting_records_only="N"`, filter `invoice_id == ref's invoice id`, then `get_authorized_payment(transaction_id)`; also a 422 `DUPLICATE_INVOICE_ID` on first send = landed → lookup. Not found inside the reporting window (lag ≤3h per the docstring) → stays unknown; a repeat pay re-runs the lookup, never a create. Not found once the claim is older than 2× the documented lag (6h) → PayPal's complete record says the write never happened: `failed` (`not_found_at_paypal`) and the shopper may try again under a NEW invoice id — a deliberate decision: without it an order whose request never reached PayPal is stuck forever | invoice id `{prefix}-{order}-{n}` | `paypal_gateway.find_authorization_by_invoice` passed as `find` from `services.pay` (with `already_landed=paypal_gateway.has_issue("DUPLICATE_INVOICE_ID")`, `absent_after=2 * REPORTING_LAG`), executed in `safe_write._call_check_verify` step 3 |
| `orders.authorize_order` | lookup: `get_order(paypal_order_id)` → authorizations[0] | PayPal order id recorded on the create step's row + ref | `services._authorized_or_none(paypal_gateway.get_checkout_order(…))` as `find` in `services._authorize_approved`; `safe_write.run` step 3 |
| `payments.reauthorize_payment` | same-reference resend (PayPal-Request-Id kept 45 days) | UUID5(ref) | `safe_write.run` step 3 (resend when `repeat_is_safe`, within `resend_window`) — Write built in `services._renew_if_stale` |
| `payments.capture_authorized_payment` | same-reference resend (observed: returns the original capture) | UUID5(ref) | `safe_write.run` step 3 — Write from `services._capture_write`; a later fulfil re-enters via `services.fulfil` (capturing branch) |
| `payments.void_payment` | on transport failure: lookup `get_authorized_payment(auth_id)`; on a stale claim: same-reference resend (observed: returns VOIDED); on 422 `PREVIOUSLY_VOIDED` → lookup | UUID5(ref) / auth id | `services._void_write` (`find=paypal_gateway.get_authorization`, `already_landed=has_issue("PREVIOUSLY_VOIDED")`); `safe_write.run` |
| `payments.refund_captured_payment` | same-reference resend (observed: returns the original refund) | UUID5(ref) | `safe_write.run` step 3 — Write from `services._refund_write` |
| `vault.create_payment_token` | same-reference resend with the card from the retried request, only inside the 3-hour key window (`VAULT_KEY_WINDOW` = 2h45m); outside it → stays unknown for an operator | UUID5(ref) | `services.save_card` (`repeat_is_safe=True`, `resend_window=VAULT_KEY_WINDOW`); `safe_write.run` |
| `vault.delete_payment_token` | repeat delete by token id (idempotent) — `paypal_retry_deletions` command and a repeat DELETE | token id | `services.delete_from_vault`; `management/commands/paypal_retry_deletions.Command.handle` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| `orders.create_order` | `PaymentOperation(ref, outcome=sending, amount, currency, invoice id)` + `PayPalPayment`/`Source` for the order | outcome, auth id, auth status, provider time, expiry; Source.allocate; order status | before: `services._create_order` (PayPalPayment/Source), `safe_write.insert_claim` (claim_fields from `services.pay`); after: `safe_write.complete` + `services._on_authorized` in one transaction |
| `orders.authorize_order` | `PaymentOperation(ref:authorize, sending)`, PayPal order id on the create row | outcome, auth id/status/time | `safe_write.insert_claim`; `safe_write.complete` + `services._on_authorized` |
| `payments.reauthorize_payment` | `PaymentOperation(ref, sending, amount)` | outcome, new auth id/time/expiry on `PayPalPayment` | `safe_write.insert_claim`; `safe_write.complete` + `services._on_reauthorized` |
| `payments.capture_authorized_payment` | `PaymentOperation(ref, sending, amount, detail.authorization_id)` | outcome, capture id/status, gross/fee/net, Source.debit, order `Complete` | `safe_write.insert_claim` (claim_fields from `services._capture_write`); `safe_write.complete` + `services._on_captured` |
| `payments.void_payment` | `PaymentOperation(ref, sending)` | outcome, auth status, order `Cancelled` | `safe_write.insert_claim`; `safe_write.complete` + `services._on_voided` |
| `payments.refund_captured_payment` | `PaymentOperation(ref, sending, amount)` reserving the amount against the ceiling | outcome, refund id/status, Source.refund, refunded total | before: `services.refund` nested `claim`; after: `safe_write.complete` + `services._on_refunded` |
| `vault.create_payment_token` | `PaymentOperation(ref, sending, user, fingerprint=last4+expiry)` | outcome, token id, `Bankcard`, `PayPalCustomer` | `safe_write.insert_claim`; `safe_write.complete` + `services._on_vaulted` |
| `vault.delete_payment_token` | Bankcard deleted + `PaymentOperation(ref, sending, provider_id=token id)` in one transaction | outcome | before: `services.delete_card`; after: `services.delete_from_vault` |

## Reconciliation

`GET /api/reconciliation?from&to` (staff): provider side = `search_transactions` over [from, to) split into
≤31-day windows, every page, narrowed back to `transaction_initiation_date ∈ [from, to)`. Local side =
`PaymentOperation` rows with a provider id whose stored **provider time** is in the window (same clock).
Matched by transaction id (set-based: an order owns all of its auth/capture/refund records), with a
fallback on `paypal_reference_id`. Four findings: `matched`, `localOnly` (flag `withinReportingLag` for
<3h old), `providerOnly` (flag `thisInstall` when invoice/custom id carries our prefix), `unsettled`
(sending/unknown/needs_review rows claimed in the window with no provider time).

## Invariants added after review (all covered by tests)

- A settled outcome (`done`, `needs_review`, `failed` with a PayPal id) is never moved back by a later check,
  and domain effects (allocate / debit / refund / void bookkeeping) apply once per transition
  (`safe_write._settled`, `safe_write.complete`).
- Nothing after the claim can leave it silently `sending`: an unforeseen failure is recorded `unknown` and
  answered 504 (`safe_write.run`); an unconfigured client releases the claim (nothing was sent).
- A hold that lands after its order moved on is flagged `needs_review` (`orphan_hold`), never dropped;
  an order changed in Oscar's dashboard is never forced through the pipeline (`services._set_order_status`).
- Order-level mutexes whose holder died are re-taken only after they are idle longer than any send
  (`services._retake_idle`).

## Assumptions & Blockers

- No blockers. The claim store is the existing Django database (SQLite in the sandbox) — UNIQUE constraints
  hold across processes.
- Minor: `PAYPAL_ENVIRONMENT` values other than `sandbox` require `PAYPAL_BASE_URL` (the SDK declares only
  the sandbox server).
- Minor: refunds are shopper-scoped per the task ("every other endpoint is shopper-scoped").
- Minor: the sandbox's API-created orders are charged in `PAYPAL_CURRENCY` using the catalogue's price
  numbers (catalogue stockrecords are GBP-denominated; the task mandates the configured currency).
- Minor: reauthorization cannot be exercised live (only Day 4–29); covered by stub-transport tests.

## Checks

- Tests: `cd sandbox && ../venv/Scripts/python manage.py test apps.payments` (Django `TestCase`, stub
  transport — no network). Existing repo suite: `venv/Scripts/pytest tests` (untouched).
- Types: `venv/Scripts/mypy --strict` on the SDK-facing modules (`paypal_gateway.py`, `safe_write.py`,
  `money.py`); plain `mypy` on the rest (Django is untyped here).
- Lint: `flake8`/`black` per repo config.

## REQUIRED READING

- MUST load `python-client-initialization` — module-level sync client, lazy after fork, `close()` at exit.
- MUST load `python-authentication` — `oauth2=` must be set; `OAuthProviderError` first in the ladder.
- MUST load `python-calling-endpoints` — `prefer` default narrows responses; `delete_payment_token` returns `None` (raw peer); status→outcome by name, default unknown.
- MUST load `python-models` — `Optional` ≠ `typing.Optional`; open enums; money as `Decimal` strings.
- MUST load `python-error-handling` — never-sent vs may-have-landed split; decode failures; one ladder.
- MUST load `python-configuration-resilience` — safe write (claim, call, check, verify, complete); no retries; reconciliation clocks.
- MUST load `python-testing` — stub transport via `custom_http_client`; token request first; two transport failure inputs.
