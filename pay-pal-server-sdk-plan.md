# PayPal Server SDK (Python) — integration plan & contract sheet

Scope: PayPal card payments (authorize → capture → refund / void) and saved cards (vault) for the
django-oscar sandbox site, exposed under `/api/` by a new app `sandbox/apps/paypal_payments/`.

## SDK identity (verified against the installed package + cloned map, branch `main`)

| Fact | Value |
| --- | --- |
| Distribution / import root | `paypal` / `paypal` (**drift**: the getting-started skill names `pay-pal-server-sdk` / `pay_pal_server_sdk`; the real `pyproject.toml` and map say `paypal`) |
| Version | `2.29` — installed into `venv` from `git+https://github.com/context-plugins/paypal-python-sdk.git@main` |
| Client | **sync** `paypal.PaypalClient` (Django under WSGI is sync). Keyword-only ctor: `base_url`, `timeout`, `custom_http_client`, `oauth2`, `oauth2_token_source` |
| Lifetime | one lazily-built module-level client per process (built after fork, on first use), `close()` via `atexit` |
| Auth | `oauth2=ClientCredentials(client_id=…, client_secret=…)` from `paypal.core`; omitting it silently sends unauthenticated requests → we fail fast when settings are empty |
| Base URL | SDK declares only `https://api-m.sandbox.paypal.com` (its default). We always pass `base_url` explicitly: `PAYPAL_BASE_URL` verbatim when set (token fetch follows it); else `PAYPAL_ENVIRONMENT == "sandbox"` → the SDK's sandbox URL; any other environment without `PAYPAL_BASE_URL` → `ImproperlyConfigured` (the SDK declares no other host, so we do not invent one) |
| Retries | the SDK performs **none**. We add exactly one immediate same-key resend for a write whose outcome is unknown (see UNKNOWN OUTCOMES); reads are not retried |
| Timeout | `PAYPAL_TIMEOUT` seconds (default 20.0), passed to our own `HttpxClient(timeout=…)` wrapped by a logging transport |
| Keyword-only params | every param after `*` has a real default; we never pass defensive `None`s |
| `Optional[T]` | means `T | UnsetType`; never pass `None`. Read with `isinstance(x, UnsetType)` |
| Decode failure | raises `ValueError`/`pydantic.ValidationError` in both modes (seen live: `get_payment_token` after delete → 2xx empty body → `ValueError`) |
| Failed token fetch | `ApiError` whose `.error` is `OAuthProviderError | RawError` → our `ProviderConfigError` (502) |

## Contract sheet — operations in scope

All `Error`-arm ops: `.error` is `Error | RawError`; `Error` = `name: str`, `message: str`, `debug_id: str`, `details: Optional[list[ErrorDetails]]` (`ErrorDetails.issue: str`, `description: Optional[str]`).
`prefer` defaults to `"return=minimal"` — we pass `prefer="return=representation"` on every write so status/amount come back.

| Op (`client.…`) | Signature (positional ∣ keyword-only used) | Returns | Error union (statuses) | `PayPal-Request-Id` retention |
| --- | --- | --- | --- | --- |
| `orders.create_order` | `body: OrderRequest` ∣ `pay_pal_request_id`, `prefer` | `Order` | `CreateOrderErrorBody` = `Error` [400,401,422] ∣ `RawError` | 6 h; **mandatory** for single-step card orders |
| `orders.authorize_order` | `id` ∣ `pay_pal_request_id`, `prefer` | `OrderAuthorizeResponse` | `Error` [400,401,403,404,422,500] ∣ `RawError` | 6 h |
| `payments.capture_authorized_payment` | `authorization_id` ∣ `pay_pal_request_id`, `prefer`, `body: CaptureRequest` | `CapturedPayment` | `Error` [400,401,403,404,409,422] ∣ `RawError` [500,…] | 45 d |
| `payments.reauthorize_payment` | `authorization_id` ∣ `pay_pal_request_id`, `prefer`, `body: ReauthorizeRequest` | `PaymentAuthorization` | `Error` [400,401,403,404,422] ∣ `RawError` [500,…] | 45 d |
| `payments.void_payment` | `authorization_id` ∣ `pay_pal_request_id`, `prefer` | `PaymentAuthorization` | `Error` [401,403,404,409,422] ∣ `RawError` [500,…] | 45 d |
| `payments.refund_captured_payment` | `capture_id` ∣ `pay_pal_request_id`, `prefer`, `body: RefundRequest` | `Refund` | `Error` [400,401,403,404,409,422] ∣ `RawError` [500,…] | 45 d |
| `vault.create_setup_token` | `body: SetupTokenRequest` ∣ `pay_pal_request_id` | `SetupTokenResponse` | `Error` [400,403,422,500] ∣ `RawError` | 3 h |
| `vault.create_payment_token` | `body: PaymentTokenRequest` ∣ `pay_pal_request_id` | `PaymentTokenResponse` | `Error` [400,403,404,422,500] ∣ `RawError` | 3 h |
| `vault.delete_payment_token` | `id` | **`None`** (use `with_raw_response` to see 204) | `Error` [400,403,500] ∣ `RawError` | n/a (delete by id, harmless to repeat) |
| `transaction_search.search_transactions` | `start_date: str`, `end_date: str` ∣ `fields` (default `"transaction_info"`), `balance_affecting_records_only` (default `"Y"`), `page_size` (default 100), `page` (default 1) | `SearchResponse` | **Case B**: always `RawError` | n/a (read). RFC 3339 with seconds; **max range 31 days**; up to 3 h reporting lag |

### Request members set (required vs `UNSET`, wire alias)

- `OrderRequest`: `intent` (req, `CheckoutPaymentIntent.AUTHORIZE`), `purchase_units: list[PurchaseUnitRequest]` (req), `payment_source: PaymentSource` (opt, set).
- `PurchaseUnitRequest`: `amount: AmountWithBreakdown` (req: `currency_code: str`, `value: str`), `reference_id`, `custom_id` (= order number), `invoice_id` (= `{prefix}-{order number}-{attempt}`), `description` (all opt, set).
- `PaymentSource.card: CardRequest` — `name`, `number`, `expiry` (`YYYY-MM`), `security_code`, `billing_address: Address` (`country_code` req; `address_line_1`, `address_line_2`, `admin_area_2`, `admin_area_1`, `postal_code` opt) **or** `vault_id` (saved card).
- `CaptureRequest`: `amount: Money` (`currency_code`, `value` req), `invoice_id`, `final_capture=True`.
- `ReauthorizeRequest`: `amount: Money`.
- `RefundRequest`: `amount: Money`, `invoice_id`, `custom_id` (= refund public id).
- `SetupTokenRequest`: `payment_source: SetupTokenRequestPaymentSource` (req) → `card: SetupTokenRequestCard` (`name`, `number`, `expiry`, `security_code`, `billing_address`); `customer: Customer` (`id` = PayPal customer id once known).
- `PaymentTokenRequest`: `payment_source: PaymentTokenRequestPaymentSource` (req) → `token: VaultTokenRequest` (`id: str` req; `type_: VaultTokenRequestType` req, **wire alias `type`**, value `SETUP_TOKEN`); `customer: Customer`.

### Response members asserted on (absent ⇒ outcome unknown, never success)

- `Order`: `id`, `status: OrderStatus`, `purchase_units[0].payments.authorizations[0]` → `AuthorizationWithAdditionalData` (`id`, `status`, `amount: Money`, `create_time`, `expiration_time`).
- `OrderAuthorizeResponse`: same shape as `Order` for the authorization.
- `PaymentAuthorization`: `id`, `status`, `amount`, `create_time`, `expiration_time`.
- `CapturedPayment`: `id`, `status: CaptureStatus`, `amount`, `create_time`, `seller_receivable_breakdown` (`gross_amount: Money` **required**, `paypal_fee`, `net_amount`).
- `Refund`: `id`, `status: RefundStatus`, `amount`, `create_time`.
- `SetupTokenResponse`: `id`, `status: PaymentTokenStatus`, `customer.id`.
- `PaymentTokenResponse`: `id`, `customer.id`, `payment_source.card` → `CardPaymentTokenEntity` (`brand`, `last_digits`, `expiry`, `name`). **No status member.**
- `SearchResponse`: `transaction_details[].transaction_info` → `TransactionInformation` (`transaction_id`, `paypal_reference_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_amount`, `fee_amount`, `transaction_status` (`S`/`P`/`D`/`V`, plain `str`), `invoice_id`, `custom_field`), `total_pages`, `page`, `last_refreshed_datetime`.

### Enums (all open: unknown wire value arrives as `str` → `unknown`)

- `OrderStatus`: CREATED, SAVED, APPROVED, VOIDED, COMPLETED, PAYER_ACTION_REQUIRED
- `AuthorizationStatus`: CREATED, CAPTURED, DENIED, PARTIALLY_CAPTURED, VOIDED, PENDING
- `CaptureStatus`: COMPLETED, DECLINED, PARTIALLY_REFUNDED, PENDING, REFUNDED, FAILED
- `RefundStatus`: CANCELLED, FAILED, PENDING, COMPLETED
- `PaymentTokenStatus`: CREATED, PAYER_ACTION_REQUIRED, APPROVED, VAULTED, TOKENIZED
- `CheckoutPaymentIntent`: CAPTURE, AUTHORIZE · `VaultTokenRequestType`: SETUP_TOKEN

### Smoke results (scratch script, real sandbox credential, 2026-09-25)

- Single-step card `create_order` (intent AUTHORIZE, `payment_source.card`) → `status=COMPLETED` with the authorization inline (`CREATED`). `authorize_order` is only needed if PayPal answers `APPROVED`.
- `reauthorize_payment` inside the honor period → 422, issue `REAUTHORIZATION_TOO_SOON`.
- Refund over the remaining captured amount → 422, issue `REFUND_AMOUNT_EXCEEDED`.
- Vault setup token → `APPROVED`; payment token → id + card (brand VISA, last_digits 1111); order with `card.vault_id` works; delete → 204.
- `search_transactions` has data (922 items over 20 days) → no 403 gating.
- No 3-D Secure challenge (`PAYER_ACTION_REQUIRED`) seen.

## Application design

- Reuses Oscar models: `order.Order`/`order.Line` (placed via Oscar's `Basket` + `OrderCreator` + `OrderTotalCalculator` + `Applicator`),
  `payment.Source`/`payment.Transaction` (money ledger: allocate/debit/refund), `payment.SourceType` ("PayPal"),
  `payment.Bankcard` (saved-card display: masked number from PayPal's `last_digits`, brand, expiry; `partner_reference` = PayPal payment-token id).
  Order status pipeline (sandbox settings): `Pending` (awaiting payment) → `Being processed` (authorized) → `Complete` (fulfilled); `Cancelled`.
  Stock: `EventHandler.consume_stock_allocations` at fulfil, `cancel_stock_allocations` at cancel.
- New models (PayPal state that Oscar has no place for): `PayPalPayment` (1:1 order; PayPal order/authorization/capture ids+statuses, fee, net, refund reservation),
  `PayPalOperation` (**the claim store**: one row per provider write step, `ref` UNIQUE, `request_id` UNIQUE), `PayPalCustomer` (user → PayPal vault customer id),
  `PayPalSavedCard` (1:1 Bankcard; token id, `removed_at`).
- Claim store = the project's database (SQLite by default, Postgres-capable): `UNIQUE` constraints on `PayPalOperation.ref` / `(payment, kind, seq)`; refund cap enforced by one conditional `UPDATE … WHERE refund_reserved + amount <= captured_amount`.
- Reference: `ref = "{PAYPAL_REFERENCE_PREFIX}:{install key}:{kind}:{scope}:{seq|key}"` (`writes.make_ref`); the header value sent is `uuid5(NAMESPACE_URL, ref)` (deterministic, 36 chars). The install key (`models.PayPalInstallation`, random per database) was added after the live run: order numbers restart at 100001 in every fresh sandbox while the PayPal account is shared, so prefix + order number collided (PayPal answered `DUPLICATE_INVOICE_ID`; a colliding PayPal-Request-Id could have returned another install's cached result). Invoice ids use `writes.invoice_prefix()`.
- `status_from_provider` per step → `done` / `pending` / `failed` / `unknown` (+ `needs_review` for amount mismatch).
- Money: `Decimal`, formatted by ISO-4217 exponent of `PAYPAL_CURRENCY`; echoed amounts compared as `Decimal`.
  Catalogue prices are stored in GBP in the fixtures; per the task, amounts come from catalogue prices and the currency from configuration — the order is recorded in `PAYPAL_CURRENCY` with the catalogue price value.

## OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| `orders.create_order` (pay) | `Order.status`, then the inline authorization's `AuthorizationWithAdditionalData.status` | Order: COMPLETED → read the authorization (below); APPROVED → not-yet: run `authorize_order` step; CREATED, SAVED → pending (payment `authorization_pending`, 202); PAYER_ACTION_REQUIRED → failed + reported as unsupported 3-D Secure challenge (task says STOP; we never build an approval round-trip); VOIDED → failed; absent/other → unknown (202, `outcomeUnknown`). Authorization: CREATED → done (hold in place; Oscar `Source.allocate`, order → `Being processed`); PENDING → pending; DENIED → failed; VOIDED → failed (undone); CAPTURED, PARTIALLY_CAPTURED → needs_review (unexpected for a fresh hold); absent/other → unknown | `payments.pay` → `writes.safe_write(outcome_of=payments._order_outcome)` → `outcomes.order_create_step` / `outcomes.authorization`; recorded by `payments._apply_authorization` → `payments._apply_hold_outcome`; PAYER_ACTION_REQUIRED / declines answered in `payments._continue_hold` |
| `orders.authorize_order` | `OrderAuthorizeResponse` authorization status | same authorization mapping as the row above | `payments._authorize_step` → `outcomes.authorization` via `payments._order_outcome`; recorded by `payments._apply_authorization` |
| `payments.reauthorize_payment` | `PaymentAuthorization.status` | CREATED → done (new authorization id replaces the old); PENDING → pending; DENIED, VOIDED → failed; CAPTURED, PARTIALLY_CAPTURED → needs_review; absent/other → unknown | `payments._renew_stale_authorization` → `outcomes.authorization`; recorded by `payments._apply_reauthorization` |
| `payments.capture_authorized_payment` | `CapturedPayment.status` | COMPLETED → done (fee/net recorded, `Source.debit`, order → `Complete`); PENDING → pending (202, order not completed); DECLINED, FAILED → failed; PARTIALLY_REFUNDED, REFUNDED → failed (undone — never reported as a fresh capture); absent/other → unknown | `payments.fulfil` → `outcomes.capture`; recorded by `payments._apply_capture`; a pending capture re-read by `payments._refresh_capture` |
| `payments.void_payment` | `PaymentAuthorization.status` | VOIDED → done (the undo is what was asked: order → `Cancelled`, stock released); CREATED, PENDING → pending (hold not yet released); CAPTURED, PARTIALLY_CAPTURED, DENIED → failed (money moved / nothing to void; operator told to refund instead where captured); absent/other → unknown | `payments.cancel` → `outcomes.void`; recorded by `payments._apply_void` |
| `payments.refund_captured_payment` | `Refund.status` | COMPLETED → done (`Source.refund`, refunded total updated); PENDING → pending (reservation kept, 202); FAILED, CANCELLED → failed (reservation released); absent/other → unknown (reservation kept) | `payments.refund` → `outcomes.refund`; recorded by `payments._apply_refund` / `payments._release_if_failed`; a pending refund re-read by `payments._refresh_refund` |
| `vault.create_setup_token` | `SetupTokenResponse.status` | APPROVED, VAULTED, TOKENIZED → done (proceed to payment token); CREATED → pending; PAYER_ACTION_REQUIRED → failed + reported as unsupported challenge; absent/other → unknown | `cards.save_card` → `outcomes.setup_token` (read by `cards._read_setup_token`); PAYER_ACTION_REQUIRED answered as `card_verification_required` in `cards.save_card` |
| `vault.create_payment_token` | none — `PaymentTokenResponse` has no status member | `id` present **and** `payment_source.card` present → done; otherwise unknown (never success). Recorded as a row here because it is a write with a response we read | `cards._read_payment_token` (status = id AND card present) → `outcomes.payment_token`; Bankcard written by the `apply` closure in `cards.save_card` |
| `vault.delete_payment_token` | none — returns `None`; raw `Success` (2xx) is the only signal | 2xx → done (card row deleted); `ApiError` 404 → done (already gone); other → card stays hidden/unusable, removal retried on the next DELETE | `cards.delete_card` (`with_raw_response`; `match Success / Failure(404) / Failure`) |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| pay: `create_order` | `PayPalOperation` row `kind=create_order`, `ref={prefix}:pay:{order}:{seq}`, inserted before the call | DB `UNIQUE(ref)` and `UNIQUE(payment, kind, seq)` → `IntegrityError` | `try_claim` returns False → `load_existing`; in-flight → 409 "in progress"; settled → answered from the stored outcome | `writes.try_claim` (UNIQUE `PayPalOperation.ref` and constraint `paypal_one_claim_per_payment_step` in `models.PayPalOperation.Meta`) called from `payments.pay` with `payments.next_seq`; loser handled in `writes.safe_write` (`InProgress` / settled return) |
| pay: `authorize_order` (only when APPROVED) | `PayPalOperation` `kind=authorize_order`, same `seq` | DB `UNIQUE(ref)` | `try_claim` | `writes.try_claim` from `payments._authorize_step`; loser in `writes.safe_write` |
| fulfil: `reauthorize_payment` | `PayPalOperation` `kind=reauthorize` | DB `UNIQUE(ref)` | `try_claim` | `writes.try_claim` from `payments._renew_stale_authorization`; loser in `writes.safe_write` |
| fulfil: `capture_authorized_payment` | `PayPalOperation` `kind=capture` | DB `UNIQUE(ref)` | `try_claim` | `writes.try_claim` from `payments.fulfil`; loser in `writes.safe_write` |
| cancel: `void_payment` | `PayPalOperation` `kind=void` | DB `UNIQUE(ref)` | `try_claim` | `writes.try_claim` from `payments.cancel`; loser in `writes.safe_write` |
| refund: `refund_captured_payment` | `PayPalOperation` `kind=refund`, `ref={prefix}:refund:{order}:{caller Idempotency-Key}` + refund reservation on `PayPalPayment.refund_reserved` | DB `UNIQUE(ref)`; same key with a different amount → 409; cap: conditional `UPDATE` affecting 0 rows → 422 | `try_claim` / `reserve_refund` | `writes.try_claim` from `payments.refund` (key reused with another amount → 409 there); cap = conditional `UPDATE` in `payments.refund`, released by `payments._release_if_failed`; backstop `CheckConstraint paypal_refunds_within_capture` |
| save card: `create_setup_token` | `PayPalOperation` `kind=setup_token`, `ref={prefix}:card:{user}:{caller Idempotency-Key}` | DB `UNIQUE(ref)` | `try_claim` | `writes.try_claim` from `cards.save_card` (`Idempotency-Key` required by `views.idempotency_key`); loser in `writes.safe_write` |
| save card: `create_payment_token` | `PayPalOperation` `kind=payment_token`, same key scope | DB `UNIQUE(ref)` | `try_claim` | `writes.try_claim` from `cards.save_card`; a repeat after it is done answers the stored card in `cards.save_card` |
| order placement (`POST /api/orders`, local only) | none — no provider write; each call is a new order awaiting payment, and paying it is itself claimed | n/a | n/a | n/a (no provider write) |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| `create_order` | same-reference resend (PayPal de-duplicates by `PayPal-Request-Id` for 6 h): once immediately in the request, and again when the caller repeats `/pay` (card details re-supplied or saved card id stored on the claim). Beyond 6 h no resend: stays `unknown` for an operator | `PayPalOperation.request_id` = uuid5 of the claim ref | `writes.safe_write` (one immediate same-`request_id` resend; later repeats resend under the stored `request_id` inside `writes.RESEND_WINDOWS`, else `OutcomeUnknown` for an operator); identical request rebuilt in `payments.pay` from `op.inputs` |
| `authorize_order` | same-reference resend (a second authorize of a completed order is refused by PayPal anyway) | same claim's `request_id` | `writes.safe_write` from `payments._authorize_step` (PayPal order id from `op.inputs`) |
| `reauthorize_payment` | same-reference resend (45 d) | claim `request_id` | `writes.safe_write` from `payments._renew_stale_authorization` (authorization id from `op.inputs`) |
| `capture_authorized_payment` | same-reference resend (45 d) | claim `request_id` | `writes.safe_write` from `payments.fulfil` (authorization id from `op.inputs`) |
| `void_payment` | same-reference resend (45 d) | claim `request_id` | `writes.safe_write` from `payments.cancel` |
| `refund_captured_payment` | same-reference resend (45 d) | claim `request_id` (derived from the caller's key) | `writes.safe_write` from `payments.refund` (capture id from `op.inputs`) |
| `create_setup_token` | same-reference resend (3 h), card re-supplied by the caller's repeat under the same Idempotency-Key; beyond 3 h stays unknown | claim `request_id` | `writes.safe_write` from `cards.save_card` |
| `create_payment_token` | same-reference resend (3 h) using the stored setup-token id | claim `request_id` | `writes.safe_write` from `cards.save_card` (setup token id from `op.inputs`) |
| `delete_payment_token` | repeatable by id: the next DELETE re-sends it | PayPal token id | `cards.delete_card` (card hidden first via `PayPalSavedCard.removed_at`; the next DELETE re-sends) |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| `create_order` | Oscar Order (Pending) + `PayPalPayment` + `PayPalOperation(sending, ref, request_id, amount, currency, payment_method id)` | op outcome/provider id/status/provider time; payment: PayPal order id, authorization id/status/create+expiry time, state; Oscar `Source.allocate` + order status when done | `orders.place_order` (Order, Source, PayPalPayment) and `writes.try_claim` in `payments.pay` before; `payments._apply_authorization` inside `writes.safe_write`'s transaction after |
| `authorize_order` | op claim for the step, PayPal order id already on payment | same as above | `writes.try_claim` in `payments._authorize_step`; `payments._apply_authorization` |
| `reauthorize_payment` | op claim (sending, amount) | new authorization id/status/times on payment; op outcome | `writes.try_claim` in `payments._renew_stale_authorization`; `payments._apply_reauthorization` |
| `capture_authorized_payment` | op claim (sending, amount) | capture id/status, captured amount, fee, net, provider time; `Source.debit`; order `Complete`; stock consumed | `writes.try_claim` in `payments.fulfil`; `payments._apply_capture` |
| `void_payment` | op claim (sending) | authorization status VOIDED, payment state, order `Cancelled`, stock allocation cancelled | `writes.try_claim` in `payments.cancel`; `payments._apply_void` |
| `refund_captured_payment` | op claim (sending, amount, key) + `refund_reserved` incremented | refund id/status/provider time; `Source.refund`; `refunded_amount`; reservation released on failure | `writes.try_claim` and the reservation `UPDATE` in `payments.refund`; `payments._apply_refund` |
| `create_setup_token` | op claim (sending, user, key) | setup token id/status, PayPal customer id | `writes.try_claim` in `cards.save_card`; `writes.complete` (setup token id/status) |
| `create_payment_token` | op claim (sending, setup token id) | payment token id; Oscar `Bankcard` (masked) + `PayPalSavedCard`; `PayPalCustomer` | `writes.try_claim` in `cards.save_card`; the `apply` closure in `cards.save_card` (Bankcard, PayPalSavedCard, PayPalCustomer) |
| `delete_payment_token` | `PayPalSavedCard.removed_at` set (card hidden and unusable) | Bankcard + saved-card rows deleted on 2xx/404 | `cards.delete_card` |

## Reconciliation

`GET /api/reconciliation?from&to`: split `[from, to)` into ≤31-day windows, page every window to `total_pages`,
narrow back to the caller's instants on `transaction_initiation_date`, de-duplicate by transaction id.
Local side on the **provider's clock** (`PayPalOperation.provider_time`) for capture/refund ops; ops with no provider time
(sending/unknown) claimed in the window reported as `unsettled`. Match by transaction id (capture/refund ids) against the set;
leftovers → `paypalOnly` (flagged when their `invoice_id` carries our prefix) / `appOnly` (flagged `notYetReported` when
younger than PayPal's 3 h reporting lag, later than `last_refreshed_datetime`, or inside a window PayPal has no data for yet); amount disagreement → `amountMismatch`.

Live finding: for a window that starts after PayPal's reporting data ends, `search_transactions` answers **404 `INVALID_REQUEST`** ("Data for the given start date is not available"). That window is listed under `paypalNotYetAvailable` instead of failing the report (`reconciliation._not_available_yet`); any other search failure is reported.

Where in the code: `reconciliation.fetch_paypal` (31-day windows, every page, narrowing), `reconciliation.report` (matching).

## Assumptions & Blockers

- No blockers. Every needed capability exists in the SDK (orders, payments, vault, transaction search) and was smoke-tested.
- Minor: live host is not declared by the SDK → non-sandbox environments require `PAYPAL_BASE_URL` (fail fast otherwise).
- Minor: the honor period (3 days) comes from the `reauthorize_payment` docstring; configurable as `PAYPAL_AUTH_HONOR_PERIOD_DAYS`.
- Minor: saving a card and refunding require an `Idempotency-Key` header (400 without it) — the only way a double submit of those can be recognised.
- Minor: host note correction — `oscar_import_catalogue` of the CSVs is required on this machine (it creates 198 of the 209 products; `orders.json` fails its FK without it).

## REQUIRED READING

| Hazard | Skill |
| --- | --- |
| client lifetime, sync vs async, close obligation | MUST load `python-client-initialization` (loaded) |
| credentials, failed token fetch payload | MUST load `python-authentication` (loaded) |
| keyword-only split, `prefer` default narrowing, `-> None` delete, status ≠ id | MUST load `python-calling-endpoints` (loaded) |
| `UNSET` vs `None`, open enums, wire alias `type_`, Decimal money | MUST load `python-models` (loaded) |
| `ApiError` ladder, never-sent vs may-have-landed, decode failures | MUST load `python-error-handling` (loaded) |
| safe write, claims, resend-by-same-key, reconciliation clocks | MUST load `python-configuration-resilience` (loaded) |
| stub transport tests, token request first, two transport failures | MUST load `python-testing` (loaded) |
