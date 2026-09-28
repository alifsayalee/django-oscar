# PayPal Server SDK plan — django-oscar sandbox: payments + saved cards

Scope: a new Django app `sandbox/apps/paypal_payments` exposing `/api/...` (orders, pay, fulfil, cancel,
refunds, my-orders, reconciliation, payment-methods) on the sandbox site, using the PayPal Server SDK for
Python for every PayPal interaction.

## SDK identity (lookup drift recorded)

| Fact | Value (source) |
| --- | --- |
| Distribution / import root | **`paypal` / `paypal`** (sdk-map.md header, `pyproject.toml`). The `python-getting-started` skill says `pay-pal-server-sdk` / `pay_pal_server_sdk` / `PayPalServerSdkClient` — **stale**; installing under that name fails. Map is authoritative. |
| Version | 2.29 (`pyproject.toml`), installed from `git+https://github.com/context-plugins/paypal-python-sdk.git@main` as `paypal @ git+…` |
| Client classes | `PaypalClient` (sync, alias `Client`), `AsyncPaypalClient` (sdk-map "Getting a client") |
| Constructor (keyword-only) | `base_url: str\|None=None`, `timeout: float=30.0`, `custom_http_client: HttpClient\|None`, `oauth2: ClientCredentialsOrDict\|None`, `oauth2_token_source` (sdk-map table) |
| Server | one server, one environment: default `https://api-m.sandbox.paypal.com`; override `base_url=` moves the token call (`/v1/oauth2/token`) too (sdk-map "Servers & auth") |
| Retries | none — SDK performs no retries (python-configuration-resilience) |
| Imports | client `paypal`; models `paypal.models`; enums `paypal.models.enums`; `ApiError, RawError, OAuthProviderError, ClientCredentials, HttpxClient, HttpRequest, HttpResponse, Success, Failure, UNSET, UnsetType` from `paypal.core` (`core/__init__.py __all__`) |

## Decisions

- **Sync** client (`PaypalClient`): host is Django under WSGI, all views sync. One lazily-built,
  process-wide client (built after fork: keyed on `os.getpid()`), closed via `atexit`.
- `base_url`: `settings.PAYPAL_BASE_URL` verbatim when set; else `PAYPAL_ENVIRONMENT=="sandbox"` →
  `https://api-m.sandbox.paypal.com` (the only server the map declares). Any other environment without
  `PAYPAL_BASE_URL` → `ImproperlyConfigured` (never silently default). Always passed explicitly.
- `oauth2=ClientCredentials(client_id=settings.PAYPAL_CLIENT_ID, client_secret=settings.PAYPAL_CLIENT_SECRET)`;
  empty values → `ImproperlyConfigured` before construction (a missing credential is otherwise silent).
- `timeout=20.0` (setting `PAYPAL_TIMEOUT`) on the transport we pass (`HttpxClient(timeout=…)` wrapped in a
  logging transport that logs method, path, status, `paypal-debug-id` only — never headers/bodies).
- Every write sends `prefer="return=representation"` (default `return=minimal` omits amounts/breakdown).
- Money: `Decimal` quantized per currency exponent → `str`; compared as `Decimal`.
- Retries: **no automatic retries on writes**; a repeat caller request re-enters the safe write, which
  resends only under the same `PayPal-Request-Id` inside the documented window. Transaction-search reads
  get a small bounded retry (429/5xx/transient, 3 attempts).

## Contract sheet (all in-scope operations)

Every keyword-only parameter has a real default — never pass defensive `None`s. All operations below are
Case A with union `Error | RawError` except `search_transactions` (Case B, `RawError` only). `Error`
(`paypal/models/error.py`): required `name: str`, `message: str`, `debug_id: str`;
`details: Optional[list[ErrorDetails]]` with `ErrorDetails.issue: str` (required), `description: Optional[str]`.
A failed token fetch raises `ApiError` whose `.error` is `OAuthProviderError | RawError` — checked first.
Decode failure → `ValidationError`/`ValueError` in both modes (observed: `get_payment_token` after delete
returned a non-JSON 2xx → `ValueError`).

| Operation | Signature (positional \| after `*`) | Returns | Error arms | Idempotency header & window |
| --- | --- | --- | --- | --- |
| `orders.create_order` | `(body: OrderRequest\|OrderRequestDict, *, pay_pal_request_id, prefer="return=minimal", …)` | `Order` | `Error` [400,401,422] · `RawError` | `PayPal-Request-Id`, 6 h; **mandatory for single-step card create**; smoke: same id → same order returned |
| `orders.authorize_order` | `(id: str, *, pay_pal_request_id, prefer, body: OrderAuthorizeRequest\|None, …)` | `OrderAuthorizeResponse` | `Error` [400,401,403,404,422,500] · `RawError` | `PayPal-Request-Id`, 6 h |
| `orders.get_order` | `(id: str, *, fields, …)` | `Order` | `Error` [401,404] · `RawError` | read |
| `payments.get_authorized_payment` | `(authorization_id: str, *, …)` | `PaymentAuthorization` | `Error` [401,403,404] · `RawError` [500,…] | read |
| `payments.reauthorize_payment` | `(authorization_id: str, *, pay_pal_request_id, prefer, body: ReauthorizeRequest\|None, …)` | `PaymentAuthorization` | `Error` [400,401,403,404,422] · `RawError` [500,…] | 45 days. Semantics (docstring + smoke): allowed **once**, day 4–29 after the original authorization; honor period 3 days; day 0 → 422 `REAUTHORIZATION_TOO_SOON` |
| `payments.capture_authorized_payment` | `(authorization_id: str, *, pay_pal_request_id, prefer, body: CaptureRequest\|None, …)` | `CapturedPayment` | `Error` [400,401,403,404,409,422] · `RawError` [500,…] | 45 days; smoke: same id → same capture |
| `payments.get_captured_payment` | `(capture_id: str, *, …)` | `CapturedPayment` | `Error` [401,403,404] · `RawError` | read |
| `payments.void_payment` | `(authorization_id: str, *, pay_pal_request_id, prefer, …)` — no body | `PaymentAuthorization` | `Error` [401,403,404,409,422] · `RawError` [500,…] | 45 days; smoke: same id → same VOIDED record; after capture → 422 `PREVIOUSLY_CAPTURED` |
| `payments.refund_captured_payment` | `(capture_id: str, *, pay_pal_request_id, prefer, body: RefundRequest\|None, …)` | `Refund` | `Error` [400,401,403,404,409,422] · `RawError` [500,…] | 45 days; smoke: same id → same refund; over-refund → 422 `REFUND_AMOUNT_EXCEEDED` |
| `payments.get_refund` | `(refund_id: str, *, …)` | `Refund` | `Error` [401,403,404] · `RawError` | read |
| `vault.create_payment_token` | `(body: PaymentTokenRequest\|PaymentTokenRequestDict, *, pay_pal_request_id, …)` | `PaymentTokenResponse` | `Error` [400,403,404,422,500] · `RawError` | `PayPal-Request-Id`, 3 h |
| `vault.delete_payment_token` | `(id: str, *, …)` | **`None`** → use `with_raw_response` to see status | `Error` [400,403,500] · `RawError` | none (idempotent by id) |
| `transaction_search.search_transactions` | `(start_date: str, end_date: str, *, fields="transaction_info", balance_affecting_records_only="Y", page_size=100, page=1, …)` | `SearchResponse` | **Case B: `RawError` only** | read. RFC 3339 with seconds; max range **31 days**; lag up to 3 h |

### Request models (members the task sets; `Optional[T]` = `T | UnsetType`, never `None`)

- `OrderRequest`: `intent: CheckoutPaymentIntentOrStr` (required; `CheckoutPaymentIntent.AUTHORIZE`),
  `purchase_units: list[PurchaseUnitRequest]` (required), `payment_source: Optional[PaymentSource]`.
- `PurchaseUnitRequest`: `amount: AmountWithBreakdown` (required: `currency_code: str`, `value: str`),
  `invoice_id: Optional[str]` (unique per merchant account), `custom_id: Optional[str]` (appears in
  reports as `custom_field`) — **always set**: our reference.
- `PaymentSource.card: Optional[CardRequest]`; `CardRequest`: `name, number, expiry ("YYYY-MM"),
  security_code, billing_address: Address, vault_id` (all Optional). `Address.country_code: str` required;
  `address_line_1, address_line_2, admin_area_2, admin_area_1, postal_code` Optional.
- `CaptureRequest`: `amount: Optional[Money]`, `invoice_id`, `final_capture: Optional[bool]`.
- `ReauthorizeRequest`: `amount: Optional[Money]`.
- `RefundRequest`: `amount: Optional[Money]` (omit = full), `custom_id`, `invoice_id`, `note_to_payer`.
- `PaymentTokenRequest`: `payment_source: PaymentTokenRequestPaymentSource` (required; `.card:
  PaymentTokenRequestCard` with `name, number, expiry, security_code, billing_address`),
  `customer: Optional[Customer]` (`id`, `merchant_customer_id`).
- `Money`: `currency_code: str`, `value: str` (both required).
- No `Optional[Any]` member is set by this integration.

### Response members asserted on, and status enums → outcome

| Response | Members read | Status enum → outcome |
| --- | --- | --- |
| `Order` (create/authorize) | `id`, `status`, `purchase_units[0].payments.authorizations[0]` (`AuthorizationWithAdditionalData`: `id, status, amount, expiration_time, create_time`), `payment_source.card.brand/last_digits` | `OrderStatus`: COMPLETED → read the authorization; APPROVED/CREATED/SAVED → pending (authorize step next); PAYER_ACTION_REQUIRED → failed (3-D Secure browser challenge — out of scope, reported); VOIDED → failed; anything else/absent → unknown |
| authorization (`AuthorizationStatus`) | `id, status, amount, create_time, expiration_time` | CREATED → done; PENDING → pending; DENIED → failed; VOIDED → failed (undone); CAPTURED / PARTIALLY_CAPTURED → failed for the *authorize* step (no longer a hold); unlisted/absent → unknown |
| reauthorize (`PaymentAuthorization`) | same | same map as authorization |
| capture (`CapturedPayment`, `CaptureStatus`) | `id, status, amount, create_time, seller_receivable_breakdown.gross_amount (required), paypal_fee, net_amount` | COMPLETED → done; PENDING → pending; DECLINED/FAILED → failed; PARTIALLY_REFUNDED/REFUNDED → failed (undone); unlisted/absent → unknown |
| void (`PaymentAuthorization`) — undoing step | `id, status, update_time` | VOIDED → done; CAPTURED/PARTIALLY_CAPTURED → failed (too late); DENIED → done (no hold remains); others/absent → unknown |
| refund (`Refund`, `RefundStatus`) — undoing step | `id, status, amount, create_time, seller_payable_breakdown` | COMPLETED → done; PENDING → pending; FAILED/CANCELLED → failed; unlisted/absent → unknown |
| payment token (`PaymentTokenResponse`) | `id, customer.id, payment_source.card.brand/last_digits/expiry, create_time` (extra) | **no status member exists on this model**; done ⇔ 2xx *and* `id` and `payment_source.card` present; otherwise unknown |
| `SearchResponse` | `transaction_details[].transaction_info.{transaction_id, paypal_reference_id, transaction_event_code, transaction_initiation_date, transaction_amount, fee_amount, transaction_status, invoice_id, custom_field}`, `total_pages`, `page`, `last_refreshed_datetime` | report only (D/P/S/V shown verbatim) |

## OPERATION OUTCOMES

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/orders/{id}/pay` → `orders.create_order` (+ `orders.authorize_order` when not auto-authorized) | `Order.status`, then `purchase_units[0].payments.authorizations[-1].status` | auth CREATED → done → 200; PENDING → pending → 202; DENIED/VOIDED/CAPTURED/PARTIALLY_CAPTURED → failed → 402; order PAYER_ACTION_REQUIRED → failed → 402 with "3-D Secure … not supported"; order VOIDED → failed → 402; order CREATED/APPROVED/SAVED → pending (authorize step follows); unlisted/absent, or "done" without an id or amount → unknown → 504 | `services.pay_order` (reads via `services.read_pay`; maps via `gateway.pay_outcome` → `gateway.authorization_outcome` / `gateway.order_outcome`; failure text from `services._pay_failure_message`); answered by `views.answer` from `views.pay` |
| `POST /api/orders/{id}/fulfil` → `payments.reauthorize_payment` (stale only) | `PaymentAuthorization.status` | CREATED → done (capture follows); PENDING → pending → 202; DENIED/VOIDED/CAPTURED/PARTIALLY_CAPTURED → failed → 409 `authorization_not_renewable` with operator instructions; PayPal 4xx (e.g. `REAUTHORIZATION_TOO_SOON`) → the same 409; unlisted/absent → unknown → 504 | `services._ensure_fresh_authorization` (maps via `gateway.authorization_outcome`, reads via `services.read_authorization`; operator message from `services._authorization_not_renewable`) |
| `POST /api/orders/{id}/fulfil` → `payments.capture_authorized_payment` | `CapturedPayment.status` | COMPLETED → done → 200 with captured amount, PayPal fee, net; PENDING → pending → 202; DECLINED/FAILED → failed → 409; PARTIALLY_REFUNDED/REFUNDED → failed → 409; unlisted/absent → unknown → 504 | `services.fulfil_order` (maps via `gateway.capture_outcome`, reads via `services.read_capture`, applied by `services._apply_capture`); answered by `views.answer` from `views.fulfil` |
| `POST /api/orders/{id}/cancel` → `payments.void_payment` | `PaymentAuthorization.status` | VOIDED/DENIED → done → 200; CAPTURED/PARTIALLY_CAPTURED → failed → 409; others/absent → unknown → 504; PayPal 422 (e.g. `PREVIOUSLY_CAPTURED`) → 422 with PayPal's reason | `services.cancel_order` (maps via `gateway.void_outcome`, reads via `services.read_void`); answered by `views.answer` from `views.cancel` |
| `POST /api/orders/{id}/refunds` → `payments.refund_captured_payment` | `Refund.status` | COMPLETED → done → 201; PENDING → pending → 202; FAILED/CANCELLED → failed → 409; unlisted/absent → unknown → 504 | `services.refund_order` (maps via `gateway.refund_outcome`, reads via `services.read_refund`, applied by `services._sync_refund`); answered by `views.answer` from `views.refunds` |
| `POST /api/payment-methods` → `vault.create_payment_token` | none on the model (recorded above) | id + card present → done → 201; anything missing → unknown → 504 | `services.save_card` (reads via `services.read_token`, maps via `gateway.token_outcome`); answered by `views.answer` from `views.payment_methods` |
| `DELETE /api/payment-methods/{id}` → `vault.delete_payment_token` | HTTP status of the raw result (`with_raw_response`) | 2xx or 404 → provider deletion done; any other status or exception → provider deletion `pending` (local removal already committed, card unusable), retried by the next DELETE; 200 either way; not the caller's card → 404 | `services.delete_card`, `services._delete_vault_token`; `views.payment_method` |
| repeat of any of the above | stored `ProviderWrite.outcome` | answered through the same `answer()` from the stored record; `sending` within `SEND_WINDOW` → 202 in progress; a stored `pending` is re-read by provider id first | `gateway.safe_write` (lost-claim branch), `gateway._refresh`; `views.answer` |

## DUPLICATE CLAIMS

Store: the project's own database (SQLite by default, any Django backend) — table `ProviderWrite` with a
UNIQUE `ref` column; `try_claim` = INSERT in its own committed transaction, `IntegrityError` = lost claim.
API views are `non_atomic_requests` (`views.api`) so the claim commits *before* the PayPal call.

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| create_order (authorize hold) | `ProviderWrite(ref="<install>:order:<number>:a<attempt>:create")` | UNIQUE constraint on `ProviderWrite.ref` | `IntegrityError` in `gateway.try_claim` | `services.pay_order` → `gateway.safe_write` → `gateway.try_claim` |
| authorize_order (fallback) | `ProviderWrite(ref="…:a<attempt>:authorize")` | UNIQUE `ref` | `gateway.try_claim` | `services.pay_order` (second `gateway.safe_write`) |
| reauthorize_payment | `ProviderWrite(ref="…:a<attempt>:reauthorize:<authorization_id>")` | UNIQUE `ref` | `gateway.try_claim` | `services._ensure_fresh_authorization` → `gateway.safe_write` |
| capture_authorized_payment | `ProviderWrite(ref="…:a<attempt>:capture:<authorization_id>")` | UNIQUE `ref` | `gateway.try_claim` | `services.fulfil_order` → `gateway.safe_write` |
| void_payment | `ProviderWrite(ref="…:a<attempt>:void:<authorization_id>")` | UNIQUE `ref` | `gateway.try_claim` | `services.cancel_order` → `gateway.safe_write` |
| refund_captured_payment | `ProviderWrite(ref="…:order:<number>:refund:<caller key>")` **and** `PayPalRefund` UNIQUE(payment, idempotency_key), with the refundable-balance check in a transaction that writes the payment row first (row lock / SQLite write lock) | UNIQUE constraints + the serialised balance check | `gateway.try_claim`; `services._reserve_refund` (same key → same refund; over balance → 422) | `services.refund_order` → `services._reserve_refund`, `gateway.safe_write` |
| create_payment_token | `ProviderWrite(ref="<install>:user:<pk>:card:<caller key>")` | UNIQUE `ref` | `gateway.try_claim` | `services.save_card` → `gateway.safe_write` |
| delete_payment_token | none needed — the local soft delete (a conditional UPDATE) is the effect that matters; provider delete is idempotent by token id and nothing downstream acts on its result | n/a | n/a | `services.delete_card` |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| create_order | same-reference resend (kind 2) under `PayPal-Request-Id = uuid5(ref)`, only within 6 h of the claim; outside it stays `unknown` for an operator (reconciliation lists it) | `PayPal-Request-Id` (`gateway.request_id_for(ref)`), also sent as `custom_id` | `gateway.safe_write` (resend after a 5xx / timeout / unreadable body; the `checking` branch after `gateway._take_over`; window `gateway.ORDERS_DEDUP_WINDOW`) called by `services.pay_order` |
| authorize_order | same-reference resend within 6 h | `PayPal-Request-Id = uuid5(ref)` | `gateway.safe_write` called by `services.pay_order` |
| reauthorize_payment | same-reference resend within 45 days | `PayPal-Request-Id = uuid5(ref)` | `gateway.safe_write` (`gateway.PAYMENTS_DEDUP_WINDOW`) called by `services._ensure_fresh_authorization` |
| capture_authorized_payment | same-reference resend within 45 days | `PayPal-Request-Id = uuid5(ref)` | `gateway.safe_write` called by `services.fulfil_order` |
| void_payment | same-reference resend within 45 days | `PayPal-Request-Id = uuid5(ref)` | `gateway.safe_write` called by `services.cancel_order` |
| refund_captured_payment | same-reference resend within 45 days | `PayPal-Request-Id = uuid5(ref)` (also `custom_id`) | `gateway.safe_write` called by `services.refund_order` |
| create_payment_token | same-reference resend within 3 h; outside → stays unknown | `PayPal-Request-Id = uuid5(ref)` | `gateway.safe_write` (`gateway.VAULT_DEDUP_WINDOW`) called by `services.save_card` |
| pending results (capture/refund/authorization/void) | lookup by provider id on the next request (`get_captured_payment`, `get_refund`, `get_authorized_payment`, `get_order`) | provider id stored at completion | `gateway._refresh` with `services._refresh_pay` and the `refresh=` lambdas in `services.fulfil_order`, `services.refund_order`, `services.cancel_order`, `services._ensure_fresh_authorization` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| create_order / authorize_order | Oscar `Order` (status "Awaiting payment"), `OrderPayment(attempt)`, `ProviderWrite(ref, outcome=sending, amount, currency)` | `ProviderWrite` outcome/provider id/status/time/data; `OrderPayment` authorization fields; Oscar `Source.allocate`, `PaymentEvent` "Authorised"; order → "Payment authorised" | before: `services.place_order`, `gateway.try_claim`; after: `gateway.complete`, `services._apply_pay` |
| reauthorize_payment | `ProviderWrite(sending)` + existing `OrderPayment` authorization | new authorization id/time on `OrderPayment`, `reauthorized=True`, Oscar `Transaction` "Reauthorise" | `gateway.try_claim`; `gateway.complete` and the locked update in `services._ensure_fresh_authorization` |
| capture_authorized_payment | `ProviderWrite(sending)` | capture id/status/amount/fee/net; `Source.debit`; `PaymentEvent` "Settled"; stock consumed; order → "Fulfilled" | `gateway.try_claim`; `gateway.complete`, `services._apply_capture` |
| void_payment | `ProviderWrite(sending)` | authorization VOIDED; `Transaction` "Void" and allocation cleared; `PaymentEvent` "Voided"; stock allocation cancelled; order → "Cancelled" | `gateway.try_claim`; `gateway.complete`, the post-call block of `services.cancel_order`, `services._cancel_order_status` |
| refund_captured_payment | `PayPalRefund(reserved amount, key)` + `ProviderWrite(sending)` | refund id/status/time; `Source.refund`; `OrderPayment.refunded_amount` and state; `PaymentEvent` "Refunded" | before: `services._reserve_refund`, `gateway.try_claim`; after: `gateway.complete`, `services._sync_refund` |
| create_payment_token | `ProviderWrite(sending)` (keyed by user + caller key, with a last-4/expiry fingerprint) | `SavedCard` (token id, customer id, brand, last4, expiry — never PAN/CVV) | `gateway.try_claim`; `gateway.complete`, `SavedCard` creation in `services.save_card` |
| delete_payment_token | `SavedCard.removed_at` committed first | `SavedCard.provider_deleted_at` | `services.delete_card`, `services._delete_vault_token` |

## Reconciliation

`GET /api/reconciliation?from&to` (staff): split `[from, to)` into ≤31-day chunks; page every chunk
(`page` 1..`total_pages`, `page_size=100`), `balance_affecting_records_only="N"` so authorizations appear.
Match on the provider's clock: local `ProviderWrite` rows whose stored `provider_time` ∈ window; match
PayPal `transaction_id` against our authorization/capture/refund ids (and `custom_field`/`invoice_id`
against our references). Findings: `matched`, `providerOnly`, `localOnly`, `unsettled` (no provider time /
unknown / sending), and `notYetReported` (local provider time later than PayPal's
`last_refreshed_datetime`).

## Assumptions & Blockers

- No blockers. The plugin covers every capability required (orders, authorizations, capture, reauthorize,
  void, refund, vault tokens, transaction search). No 3-D Secure challenge was returned by the sandbox.
- Minor: the live PayPal host is not declared by the SDK map; non-sandbox environments must set
  `PAYPAL_BASE_URL` (fail-fast otherwise). Not a gap: the override exists by mandate.
- Minor: Orders are placed in `PAYPAL_CURRENCY` with the catalogue's price numbers (sandbox stock is GBP;
  the task makes currency a configuration value).
- Minor: bootstrap on this machine needed the Makefile's `oscar_import_catalogue` CSV step to reach 209
  products (without it `orders.json` fails on a foreign key).
- Refunds are shopper-scoped (the task names only fulfil/cancel/reconciliation as operator actions).

## REQUIRED READING

| Hazard | Pointer |
| --- | --- |
| error boundary, OAuthProviderError first, never-sent vs unknown transport errors | MUST load python-error-handling (loaded) |
| lazy client, fork safety, close | MUST load python-client-initialization (loaded) |
| credentials keyword silent when omitted | MUST load python-authentication (loaded) |
| safe write, claims, same-key resend, reconciliation, timeout, logging transport | MUST load python-configuration-resilience (loaded) |
| keyword-only split, `prefer` default narrows responses, `-> None` delete | MUST load python-calling-endpoints (loaded) |
| `UNSET` vs `None`, open enums, money as str | MUST load python-models (loaded) |
| stub transport, token request first, both transport failures | MUST load python-testing (loaded) |
