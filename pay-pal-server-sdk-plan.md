# PayPal Server SDK integration plan — django-oscar sandbox

Scope: new Django app `sandbox/apps/payments` (label `sandbox_payments`) exposing `/api/...` endpoints for
order placement, card authorization, capture-at-fulfilment, void-on-cancel, refunds, saved cards (vault)
and reconciliation. Reuses Oscar's `order.Order`/`order.Line` (via `OrderCreator`) and `payment.Source` /
`payment.SourceType` / `payment.Transaction` for money bookkeeping.

## Host decisions

| Decision | Choice | Source |
| --- | --- | --- |
| Sync vs async | **sync** `PayPalServerSdkClient` — the sandbox is Django under WSGI, all views sync | repo survey (`sandbox/wsgi.py`, no async views) |
| Client lifetime | one module-level, lazily built client per process (built after fork, on first use); closed from `atexit` | python-client-initialization |
| Transaction model | sandbox sets `ATOMIC_REQUESTS=True`; every payment view is `transaction.non_atomic_requests` so the claim commits **before** the provider call and survives a later exception | repo survey (`sandbox/settings.py`) |
| Claim store | the sandbox DB (SQLite default / Postgres alternative) — a `ProviderWrite` row with a UNIQUE `ref`; insert-or-fail via `IntegrityError` | codebase already enforces uniqueness through DB constraints |
| Base URL | `PAYPAL_BASE_URL` verbatim if set; else `PAYPAL_ENVIRONMENT == "sandbox"` → `https://api-m.sandbox.paypal.com` (the SDK's only declared server, sdk-map *Servers & auth*); any other environment without `PAYPAL_BASE_URL` → `ImproperlyConfigured` (no host is invented). Always passed explicitly. The token endpoint follows `base_url` (sdk-map). | sdk-map.md |
| Credentials | `oauth2=ClientCredentials(client_id=settings.PAYPAL_CLIENT_ID, client_secret=settings.PAYPAL_CLIENT_SECRET)`; missing value → `ImproperlyConfigured` at client build (omitting `oauth2` would silently send unauthenticated) | python-authentication |
| Timeout | `timeout=20.0` client-wide | python-configuration-resilience |
| Retries | **none built** — the SDK performs none; an unknown outcome is recorded and resolved by a same-reference resend on the next request, never by an automatic loop | python-configuration-resilience |
| Currency | `settings.PAYPAL_CURRENCY`; catalogue price numbers are charged in that currency, and `Order.currency` is set to it (sandbox stock records are GBP; the configured currency wins) | task |
| Money format | `Decimal.quantize` with the ISO exponent table (python-models); compared as `Decimal` | python-models |
| Tests | Django `TestCase` in `sandbox/apps/payments/tests.py`, run with `sandbox/manage.py test apps.payments`; stub transport seam (`custom_http_client`) | python-testing |
| Type check | `mypy --strict` over `sandbox/apps/payments` (SDK ships `py.typed`) — mypy installed into venv | skill rule |

## Contract sheet (SDK 2.29, verified against installed package + map)

Every keyword-only parameter has a real default: pass only what is set, never defensive `None`s.
Async twins exist for every op (same names/params, awaited) — unused here. All ops end with keyword-only
`request_options`. `prefer` defaults to `"return=minimal"` on every write below — **we always pass
`prefer="return=representation"`** so status/amount/breakdown are in the body.

| Operation | Signature (positional \| after `*`) | Returns | `ApiError.error` union | Idempotency header / window |
| --- | --- | --- | --- | --- |
| `client.orders.create_order` | `body: OrderRequest` \| `pay_pal_request_id`, `prefer`, … | `Order` | `CreateOrderErrorBody = Error \| RawError` (Error: 400,401,422) | `PayPal-Request-Id`, 6 h; mandatory for card payment_source |
| `client.orders.authorize_order` | `id_: str` \| `pay_pal_request_id`, `prefer`, `body: OrderAuthorizeRequest` | `OrderAuthorizeResponse` | `AuthorizeOrderErrorBody = Error \| RawError` (400,401,403,404,422,500) | `PayPal-Request-Id`, 6 h |
| `client.payments.get_authorized_payment` | `authorization_id` | `PaymentAuthorization` | `Error \| RawError` (401,403,404; 500 raw) | read |
| `client.payments.reauthorize_payment` | `authorization_id` \| `pay_pal_request_id`, `prefer`, `body: ReauthorizeRequest` | `PaymentAuthorization` | `Error \| RawError` (400,401,403,404,422; 500 raw) | 45 days |
| `client.payments.capture_authorized_payment` | `authorization_id` \| `pay_pal_request_id`, `prefer`, `body: CaptureRequest` | `CapturedPayment` | `Error \| RawError` (400,401,403,404,409,422; 500 raw) | 45 days |
| `client.payments.get_captured_payment` | `capture_id` | `CapturedPayment` | `Error \| RawError` | read |
| `client.payments.void_payment` | `authorization_id` \| `pay_pal_request_id`, `prefer` | `PaymentAuthorization` (body only with `return=representation`) | `Error \| RawError` (401,403,404,409,422; 500 raw) | 45 days |
| `client.payments.refund_captured_payment` | `capture_id` \| `pay_pal_request_id`, `prefer`, `body: RefundRequest` | `Refund` | `Error \| RawError` (400,401,403,404,409,422; 500 raw) | 45 days |
| `client.vault.create_payment_token` | `body: PaymentTokenRequest` \| `pay_pal_request_id` | `PaymentTokenResponse` | `Error \| RawError` (400,403,404,422,500) | 3 hours |
| `client.vault.delete_payment_token` | `id_` | **`None`** → use `with_raw_response` (`ApiResult[None, DeletePaymentTokenErrorBody]`) | `Error \| RawError` (400,403,500) | none — smoke: DELETE of an absent id answers 204, so a same-id resend is safe |
| `client.transaction_search.search_transactions` | `start_date: str, end_date: str` \| `page_size=100`, `page=1`, `fields="transaction_info"`, `balance_affecting_records_only="Y"` | `SearchResponse` | **Case B: always `RawError`** | read; RFC3339 with seconds; max range 31 days |

Model members set (required in **bold**; everything else `Optional[T]=UNSET`, never `None`):

- `OrderRequest`: **`intent`** (`CheckoutPaymentIntent.AUTHORIZE`), **`purchase_units: list[PurchaseUnitRequest]`**, `payment_source: PaymentSource`.
- `PurchaseUnitRequest`: **`amount: AmountWithBreakdown`** (**`currency_code`**, **`value`** str), `reference_id`, `custom_id`, `invoice_id`, `description`.
- `PaymentSource.card: CardRequest` — `name`, `number`, `expiry` (`YYYY-MM`), `security_code`, `billing_address: Address` (**`country_code`**, `address_line_1`, `address_line_2`, `admin_area_2`, `admin_area_1`, `postal_code`), `vault_id` (saved-card payment).
- `ReauthorizeRequest.amount: Money` (**`currency_code`**, **`value`**). `CaptureRequest`: `amount: Money`, `final_capture: bool=False` (we send `True`), `invoice_id`. `RefundRequest`: `amount: Money`.
- `PaymentTokenRequest`: **`payment_source: PaymentTokenRequestPaymentSource`** (`card: PaymentTokenRequestCard` — `name`, `number`, `expiry`, `security_code`, `billing_address: Address`), `customer: Customer` (`id`).
- Wire aliases touched: `CardPaymentTokenEntity.type_`↔`type`, `CardResponse.type_`↔`type` (read only; not used). No other alias on members we set.
- No `Optional[Any]` member is set by us.

Response members asserted after each call (UNSET → outcome `unknown`, never success):

- `Order` (create): `id`, `status: OrderStatusOrStr`, `purchase_units[0].amount.value/currency_code`, `create_time`.
- `OrderAuthorizeResponse`: `purchase_units[0].payments.authorizations[0]` → `AuthorizationWithAdditionalData`: `id`, `status`, `amount`, `create_time`, `expiration_time`.
- `PaymentAuthorization` (reauthorize/void/get): `id`, `status`, `amount`, `create_time`/`update_time`, `expiration_time`.
- `CapturedPayment`: `id`, `status`, `amount`, `create_time`, `seller_receivable_breakdown` (**`gross_amount`** required, `paypal_fee`, `net_amount`) — the one nested required member on a 2xx path we read.
- `Refund`: `id`, `status`, `amount`, `create_time`.
- `PaymentTokenResponse`: `id`, `customer.id`, `payment_source.card` (`last_digits`, `brand`, `expiry`, `name`). **Declares no status member.**
- `SearchResponse`: `transaction_details[*].transaction_info` (`transaction_id`, `paypal_reference_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_amount`, `fee_amount`, `transaction_status`, `invoice_id`, `custom_field`), `page`, `total_pages`.

Status enums (open `…OrStr`; unlisted value → `unknown`):

- `OrderStatus` (create step): `APPROVED` done · `COMPLETED` done · `CREATED`, `SAVED` pending · `PAYER_ACTION_REQUIRED` failed (needs a browser approval — not built; reported) · `VOIDED` failed.
- `AuthorizationStatus` (authorize / reauthorize steps): `CREATED` done · `PENDING` pending · `DENIED` failed · `VOIDED` failed (undone) · `CAPTURED`, `PARTIALLY_CAPTURED` unknown (hold already converted — needs review).
- `AuthorizationStatus` (void step, `cancel_outcome`): `VOIDED` done · `CAPTURED`, `PARTIALLY_CAPTURED` failed (too late) · anything else unknown.
- `CaptureStatus` (capture step): `COMPLETED` done · `PENDING` pending · `DECLINED`, `FAILED` failed · `REFUNDED`, `PARTIALLY_REFUNDED` failed (done then undone).
- `RefundStatus` (refund step): `COMPLETED` done · `PENDING` pending · `FAILED`, `CANCELLED` failed.
- Vault create: no status field — done iff `id` AND `payment_source.card` are present; otherwise unknown.
- Vault delete (`cancel_outcome` equivalent): raw `Success` (2xx) done; anything else from a lookup is unknown.

Error facts:

- Decode failure → `pydantic.ValidationError`/`ValueError`, not `ApiError`, in both response modes. **Observed in smoke:** `vault.get_payment_token` 404 body fails to decode (`links[].rel` missing) → `ValidationError`. We therefore never use `get_payment_token` as a lookup.
- Failed token fetch → `ApiError` with `OAuthProviderError | RawError` payload, from the operation call → our 502 (configuration).
- Transport exceptions are raw `httpx`: `ConnectError/ConnectTimeout/PoolTimeout/ProxyError` = never sent; other `httpx.RequestError` = may have landed.
- Error (typed arm): `name`, `message`, `debug_id`, `details[*].issue/description` — surfaced to operators, never card data.

## OPERATION OUTCOMES

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/orders/{id}/pay` → `orders.create_order` | `Order.status` | APPROVED/COMPLETED → done (continue to authorize); CREATED/SAVED → pending (202, nothing downstream); PAYER_ACTION_REQUIRED → failed (409 "card requires browser approval, not supported"); VOIDED → failed (409); UNSET/unlisted → unknown (504 outcome_unknown) | `services._create_paypal_order` (read) → `outcomes.order_create_outcome`; answered by `services._not_done` / `outcomes.http_status` |
| `POST /api/orders/{id}/pay` → `orders.authorize_order` | `purchase_units[0].payments.authorizations[0].status` | CREATED → done (200, hold placed); PENDING → pending (202); DENIED → failed (409 declined); VOIDED → failed (409); CAPTURED/PARTIALLY_CAPTURED/UNSET/unlisted → unknown (504) | `services._authorize` (read via `services._first_authorization`) → `outcomes.authorization_outcome`; `services._not_done` |
| `POST /api/orders/{id}/fulfil` → `payments.reauthorize_payment` | `PaymentAuthorization.status` | CREATED → done (continue to capture); PENDING → pending (202); DENIED/VOIDED → failed (409 "cannot be renewed", operator action); other → unknown (504) | `services._reauthorize` (read `services._read_authorization`) → `outcomes.authorization_outcome`; branches in `services._ensure_fresh_authorization` / `services._not_renewable` |
| `POST /api/orders/{id}/fulfil` → `payments.capture_authorized_payment` | `CapturedPayment.status` | COMPLETED → done (200 with amount/fee/net); PENDING → pending (202; a repeat fulfil re-reads the capture); DECLINED/FAILED → failed (409); REFUNDED/PARTIALLY_REFUNDED → failed per rule (undone); other → unknown (504) | `services._capture` → `outcomes.capture_outcome`; `services._apply_capture`, `services._capture_not_done`, pending re-read in `services._refresh_capture` |
| `POST /api/orders/{id}/cancel` → `payments.void_payment` | `PaymentAuthorization.status` (cancel_outcome) | VOIDED → done (200, funds released); CAPTURED/PARTIALLY_CAPTURED → failed (409 "already captured, refund instead"); other/UNSET → unknown (504) | `services.cancel` (read `services._read_authorization`) → `outcomes.void_outcome` |
| `POST /api/orders/{id}/refunds` → `payments.refund_captured_payment` | `Refund.status` | COMPLETED → done (200); PENDING → pending (202); FAILED/CANCELLED → failed (409, reservation released); other → unknown (504) | `services.refund` → `outcomes.refund_outcome`; `services._apply_refund`, pending re-read in `services._refresh_refund` |
| `POST /api/payment-methods` → `vault.create_payment_token` | none declared on `PaymentTokenResponse` | `id` + `payment_source.card` present → done (200 with paymentMethodId); either absent → unknown (504) | `cards.save_card` (`read` reports `outcomes.CARD_VAULTED` only when id + card entity present) → `outcomes.vault_outcome` |
| `DELETE /api/payment-methods/{id}` → `vault.delete_payment_token` | raw `Success`/`Failure` status code (op returns `None`) | 2xx → done (200, removed); refused 4xx → failed (4xx with PayPal's message; card stays hidden/unusable locally, claim released so a repeat DELETE resends); transport/5xx/unreadable → unknown (504; card stays hidden/unusable, repeat DELETE resends) | `cards.delete_card` (`send` uses `with_raw_response`, `Failure.unwrap()`) → `outcomes.delete_outcome` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| create_order (pay) | `ProviderWrite` row, ref `{prefix}:o{order_pk}:a{attempt}:create` | UNIQUE constraint on `ProviderWrite.ref` | `IntegrityError` in `try_claim` | `safe_write.try_claim` on `ProviderWrite.ref` (unique); ref from `services._ref(payment, "create")` in `services._create_paypal_order` |
| authorize_order (pay) | `ProviderWrite`, ref `…:a{attempt}:authorize` | UNIQUE `ref` | `try_claim` | `safe_write.try_claim`; ref `services._ref(payment, "authorize")` in `services._authorize` |
| reauthorize_payment (fulfil) | `ProviderWrite`, ref `…:a{attempt}:reauth{n}` | UNIQUE `ref` | `try_claim` | `safe_write.try_claim`; ref `services._ref(payment, f"reauth{n}")` in `services._reauthorize` |
| capture_authorized_payment (fulfil) | `ProviderWrite`, ref `…:a{attempt}:capture` | UNIQUE `ref` | `try_claim` | `safe_write.try_claim`; ref `services._ref(payment, "capture")` in `services._capture` |
| void_payment (cancel) | `ProviderWrite`, ref `…:a{attempt}:void` | UNIQUE `ref` | `try_claim` | `safe_write.try_claim`; ref `services._ref(payment, "void")` in `services.cancel` |
| refund_captured_payment | `PaymentRefund` row (UNIQUE `(payment, idempotency_key)`, reserves the amount) + `ProviderWrite` ref `…:refund:{sha256(key)[:24]}` | UNIQUE constraints on both | `IntegrityError` in refund reservation and `try_claim` | `services.refund` (`PaymentRefund` unique `ref` + `unique_refund_key_per_payment`, amount reserved under the payment row lock; `services.refundable_amount`) then `safe_write.try_claim` |
| create_payment_token | `ProviderWrite`, ref `{prefix}:u{user}:card:{hmac(card fingerprint or caller key)[:24]}:d{times_deleted}` | UNIQUE `ref` | `try_claim` | `cards.save_card` (`SavedCard.ref` unique via `get_or_create`) then `safe_write.try_claim` |
| delete_payment_token | `ProviderWrite`, ref `{prefix}:pm{saved_card_pk}:delete` | UNIQUE `ref` | `try_claim` | `cards.delete_card` → `safe_write.try_claim` on `{prefix}:pm{pk}:delete` |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| create_order | same-reference resend (PayPal-Request-Id dedup, 6 h) — needs the card details again, which the repeat request carries | the create ref | `safe_write.safe_write` (claim held + `unknown`/stale `sending` → `resending` branch calls `send(ref)` again) with `send` in `services._create_paypal_order` |
| authorize_order | same-reference resend (6 h) | the authorize ref | `safe_write.safe_write` resend branch; `send` in `services._authorize` |
| reauthorize_payment | same-reference resend (45 days) | the reauth ref | `safe_write.safe_write` resend branch; `send` in `services._reauthorize` |
| capture_authorized_payment | same-reference resend (45 days) | the capture ref | `safe_write.safe_write` resend branch; `send` in `services._capture` |
| void_payment | same-reference resend (45 days); a 4xx on it → `get_authorized_payment` status VOIDED counts as landed | the void ref / authorization id | `safe_write.safe_write` resend branch + `safe_write._landed` calling `landed` in `services.cancel` |
| refund_captured_payment | same-reference resend (45 days) | the refund ref | `safe_write.safe_write` resend branch; `send` in `services.refund` (row keeps its reservation as `unknown`) |
| create_payment_token | same-reference resend (3 h) — repeat request carries the card again | the card ref | `safe_write.safe_write` resend branch; `send` in `cards.save_card` |
| delete_payment_token | same-id resend (DELETE by id; 204 even when already absent — smoke) | the saved card's PayPal token id | `safe_write.safe_write` resend branch; `send` in `cards.delete_card` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| create_order | Oscar `Order` + `OrderPayment` (state `authorizing`, attempt n) + `ProviderWrite(create ref, sending)` | `ProviderWrite` outcome/provider id/time; `OrderPayment.paypal_order_id` | before: `services.place_order` (Order + `OrderPayment`), `services.pay` (state `authorizing`), `safe_write.try_claim`; after: `safe_write.complete` + `on_complete` in `services._create_paypal_order` |
| authorize_order | `ProviderWrite(authorize ref, sending)` | authorization id/status/expiry on `OrderPayment`; Oscar `Source.allocate`; order status `Being processed` | before: `safe_write.try_claim`; after: `safe_write.complete` + `on_complete` in `services._authorize` → `services._record_authorization` (`Source.allocate`, order status) |
| reauthorize_payment | `ProviderWrite(reauth ref, sending)` | new authorization id/expiry, reauth count | before: `safe_write.try_claim`; after: `on_complete` in `services._reauthorize` |
| capture_authorized_payment | `ProviderWrite(capture ref, sending)`; `OrderPayment.state=capturing` | capture id/status, captured amount, fee, net; `Source.debit`; order status `Complete` | before: `services.fulfil` (state `capturing`) + `safe_write.try_claim`; after: `on_complete` in `services._capture` → `services._apply_capture` (`Source.debit`), `services._fill_fee_breakdown` |
| void_payment | `ProviderWrite(void ref, sending)` | authorization status VOIDED; `Transaction(Void)`; order status `Cancelled` | before: `safe_write.try_claim`; after: `on_complete` in `services.cancel` (`Transaction(Void)`, order `Cancelled`) |
| refund_captured_payment | `PaymentRefund(reserved, outcome sending)` + `ProviderWrite(refund ref, sending)` | refund id/status; `Source.refund`; refunded total | before: `PaymentRefund` created in `services.refund` + `safe_write.try_claim`; after: `on_complete` → `services._apply_refund` (`Source.refund`) |
| create_payment_token | `SavedCard(state sending, last4/brand/expiry from request)` + `ProviderWrite(card ref, sending)` | PayPal token id, customer id (`PayPalCustomer`), brand/last4 as PayPal reports | before: `SavedCard` row in `cards.save_card` + `safe_write.try_claim`; after: `on_complete` in `cards.save_card` (token id, brand/last4, `PayPalCustomer.paypal_customer_id`) |
| delete_payment_token | `SavedCard.deleted_at` set (hidden + unusable) + `ProviderWrite(delete ref, sending)` | outcome; `times_deleted` bump on the user | before: `SavedCard.deleted_at` set in `cards.delete_card` + `safe_write.try_claim`; after: `on_complete` in `cards.delete_card` (`delete_outcome`, `times_deleted`) |

## Reconciliation design

`GET /api/reconciliation?from&to` (staff): split `[from, to)` into ≤31-day windows; per window loop
`page=1..total_pages` (`page_size=100`); narrow each record to `transaction_initiation_date ∈ [from,to)`.
Local side = `ProviderWrite` rows of kind capture/refund whose **provider time** is in the window; rows
with no provider time yet (sending/unknown) claimed in the window are **unsettled**. Match by PayPal
transaction id (= capture/refund id) against the whole provider set; output `matched`, `localOnly`,
`providerOnly`, `unsettled`. Provider records carry `invoice_id` (`{prefix}-{order number}-a{attempt}`)
so a provider-only record still names the order when it is ours.

## Assumptions & Blockers

- Minor: the documented seed sequence (without the CSV step) yields 11 products and the pages/offers/orders fixtures then fail on foreign keys; running `oscar_import_catalogue sandbox/fixtures/*.csv` after `oscar_populate_countries` (despite its `New items: 0` log line) brings the catalogue to 209 and lets the later fixtures load. Not a blocker.
- Minor: seeded stock records price in GBP; amounts are charged in `PAYPAL_CURRENCY` (config wins).
- Minor: SDK decode defect on vault 404 error bodies (observed) — avoided by never looking a token up by GET.
- Minor: the python-testing skill mentions a `retry_options=` constructor keyword; the SDK map's constructor table has no such keyword — not passed.
- Minor: baseline `pytest tests/unit -n 4` on the untouched tree already errors (DB setup under xdist on Windows) — pre-existing.
- Amount mismatches are returned by `safe_write` as a `needs_review` result (409) rather than raised; a hold under review can still be voided via cancel.
- No blockers.

## REQUIRED READING

- Client construction/lifetime — MUST load `python-client-initialization` (loaded).
- Credentials, token-fetch failure payload — MUST load `python-authentication` (loaded).
- Signatures, `with_raw_response` for `delete_payment_token`, `status_from_provider`/`answer` — MUST load `python-calling-endpoints` (loaded).
- `UNSET` vs `None`, open enums, money formatting — MUST load `python-models` (loaded).
- Error ladder, never-sent vs unknown split, decode failures — MUST load `python-error-handling` (loaded).
- Safe write, claim store, reconciliation on provider clock, no retries — MUST load `python-configuration-resilience` (loaded).
- Stub transport tests — MUST load `python-testing` (loaded).
