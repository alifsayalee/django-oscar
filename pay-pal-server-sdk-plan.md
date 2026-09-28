# PayPal Server SDK — integration plan & contract sheet (django-oscar sandbox)

## Scope

New Django app `sandbox/apps/paypal_payments/` exposing `/api/...` (wired in `sandbox/urls.py`):
session login helpers, orders (create/pay/fulfil/cancel/refunds), my-orders, saved cards
(create/list/delete) and reconciliation. Reuses Oscar `order.Order`/`order.Line` (via Oscar's
`OrderCreator` over a dedicated `Basket`), `payment.Source`/`payment.Transaction`/`payment.SourceType`
for the payment ledger. Provider state lives in new models of the app.

## Host decisions (repo survey)

| Decision | Value | Exemplar |
| --- | --- | --- |
| Sync vs async | **sync** — Django under WSGI, sync views → `PaypalClient` only, never `AsyncPaypalClient` | `sandbox/wsgi.py` |
| Toolchain | `pip` + `venv/` (py 3.11); `venv\Scripts\pip install -e .[test]`; SDK installed as distribution **`paypal`** from `git+https://github.com/context-plugins/paypal-python-sdk.git@main` (version 2.29) | `setup.py` / `pyproject.toml` |
| Tests | Django `DiscoverRunner` from `sandbox/`: `venv\Scripts\python sandbox\manage.py test apps.paypal_payments` | `sandbox/settings.py` `TEST_RUNNER` |
| Type check | none configured → `mypy --strict` on the app's PayPal gateway module(s) (installed into venv) | — |
| App/URL conventions | apps under `sandbox/apps/`, URLs outside `i18n_patterns` like `admin/` | `sandbox/urls.py` |
| Settings | `environ.Env` reads in `sandbox/settings.py` | `sandbox/settings.py` |
| Transactions | `ATOMIC_REQUESTS=True` → PayPal-calling views are `@transaction.non_atomic_requests`; each claim/complete is its own committed write | Django docs pattern |
| Claim store | the project's relational DB (SQLite default, Postgres via `settings_postgres.py`): unique constraint + `IntegrityError` | `src/oscar/apps/order/utils.py` (uniqueness on order number) |

## SDK identity (lookups)

- **Drift from the skill page:** distribution AND import root are `paypal` (not `pay-pal-server-sdk` /
  `pay_pal_server_sdk`); clients `PaypalClient` / `AsyncPaypalClient` (aliases `Client`/`AsyncClient`).
  Source: `sdk-map.md` header table + `paypal/__init__.py`.
- Imports: client from `paypal`; models from `paypal.models`; enums from `paypal.models.enums`;
  `ApiError, RawError, Success, Failure, UNSET, UnsetType, ClientCredentials, HttpxClient, HttpRequest,
  HttpResponse, OAuthProviderError` from `paypal.core`.
- Constructor (keyword-only): `base_url: str|None=None`, `timeout: float=30.0`,
  `custom_http_client: HttpClient|None`, `oauth2: ClientCredentialsOrDict|None`,
  `oauth2_token_source`. We pass `base_url`, `timeout=20.0`, `custom_http_client=LoggingTransport(HttpxClient(timeout=20.0))`, `oauth2=ClientCredentials(...)`.
- **Base URL:** SDK declares one server `https://api-m.sandbox.paypal.com` (default when `base_url`
  omitted — silent). We always pass `base_url` explicitly: `PAYPAL_BASE_URL` verbatim when set; else
  map `PAYPAL_ENVIRONMENT` via `{"sandbox": "https://api-m.sandbox.paypal.com"}`; any other value
  without `PAYPAL_BASE_URL` → `ImproperlyConfigured` (the SDK/map documents no other host; not guessed).
  Token endpoint `/v1/oauth2/token` derives from the same base URL.
- **Auth:** `oauth2=ClientCredentials(client_id, client_secret)`; omitting it = unauthenticated
  silently → we raise `ImproperlyConfigured` if either setting is empty. Token fetched lazily, cached
  on the client; failed fetch raises `ApiError` with `.error` `OAuthProviderError | RawError` out of the
  operation call, in both response modes.
- **Client lifetime:** one module-level client built lazily on first use (after any fork), closed via
  `atexit`. Transport is `HttpxClient` wrapped by our `LoggingTransport` (method, path, status, ms — no
  headers/bodies); the transport's own timeout is set (client `timeout=` does not reach a custom
  transport).
- **No retries in the SDK** — deliberately none added for writes (safe write + same-reference resend
  instead); reads are not retried either (single attempt, bounded by 20 s timeout).
- Every keyword-only param has a real default; we never pass defensive `None`s.
- `prefer` defaults to `"return=minimal"` on writes → we pass `prefer="return=representation"` on
  create_order/authorize/capture/reauthorize/refund/void so status + amounts + breakdown come back.

## Contract sheet — operations in scope

All Case A ops: `.error` is `Error | RawError`; `Error(name: str, message: str, debug_id: str,
details: Optional[list[ErrorDetails]])`, `ErrorDetails(issue: str, description: Optional[str], field, value, location)`.
Observed in sandbox: a PayPal 404 body can FAIL to decode as `Error` (`links[].rel` missing) →
`pydantic.ValidationError` on a non-2xx. Our transport records the last response status per thread so
the boundary can tell "unreadable rejection" (non-2xx) from "unreadable success" (2xx → outcome unknown).

| op | route | signature (positional \| after `*`) | returns | error arms |
| --- | --- | --- | --- | --- |
| `client.orders.create_order` | POST /v2/checkout/orders | `body: OrderRequest\|Dict` \| `pay_pal_request_id`, `prefer`, … | `Order` | Error [400,401,422] · RawError |
| `client.orders.get_order` | GET /v2/checkout/orders/{id} | `id` \| `fields`, … | `Order` | Error [401,404] · RawError |
| `client.payments.get_authorized_payment` | GET /v2/payments/authorizations/{authorization_id} | `authorization_id` | `PaymentAuthorization` | Error [401,403,404] · RawError [500,…] |
| `client.payments.reauthorize_payment` | POST …/{authorization_id}/reauthorize | `authorization_id` \| `pay_pal_request_id`, `prefer`, `body: ReauthorizeRequest` | `PaymentAuthorization` | Error [400,401,403,404,422] · RawError |
| `client.payments.capture_authorized_payment` | POST …/{authorization_id}/capture | `authorization_id` \| `pay_pal_request_id`, `prefer`, `body: CaptureRequest` | `CapturedPayment` | Error [400,401,403,404,409,422] · RawError |
| `client.payments.void_payment` | POST …/{authorization_id}/void | `authorization_id` \| `pay_pal_request_id`, `prefer` | `PaymentAuthorization` | Error [401,403,404,409,422] · RawError |
| `client.payments.refund_captured_payment` | POST /v2/payments/captures/{capture_id}/refund | `capture_id` \| `pay_pal_request_id`, `prefer`, `body: RefundRequest` | `Refund` | Error [400,401,403,404,409,422] · RawError |
| `client.payments.get_captured_payment` | GET /v2/payments/captures/{capture_id} | `capture_id` | `CapturedPayment` | Error [401,403,404] · RawError [500,…] |
| `client.payments.get_refund` | GET /v2/payments/refunds/{refund_id} | `refund_id` | `Refund` | Error [401,403,404] · RawError |
| `client.vault.create_payment_token` | POST /v3/vault/payment-tokens | `body: PaymentTokenRequest\|Dict` \| `pay_pal_request_id` | `PaymentTokenResponse` | Error [400,403,404,422,500] · RawError |
| `client.vault.delete_payment_token` | DELETE /v3/vault/payment-tokens/{id} | `id` | **`None`** → use `with_raw_response` (`ApiResult[None, …]`) | Error [400,403,500] · RawError |
| `client.transaction_search.search_transactions` | GET /v1/reporting/transactions | `start_date: str, end_date: str` \| `fields="transaction_info"`, `balance_affecting_records_only="Y"`, `page_size=100`, `page=1` | `SearchResponse` | **Case B** — RawError only |

Idempotency header semantics (docstrings, `paypal/apis/*.py`): `PayPal-Request-Id` kept **6 h** for
create_order/authorize_order (mandatory for single-step card create); **45 days** for
capture/reauthorize/refund/void; **3 h** for vault create_payment_token. A repeat under the same id
returns the original (kind 2 lookup = same-reference resend).

Semantics (docstrings): honor period **3 days** after authorization; reauthorize allowed days 4–29,
after 30 days a new authorization is needed; `search_transactions` range max **31 days**, 3 h lag,
`page` 1-based, `total_pages` on response; `fields` default `transaction_info`,
`balance_affecting_records_only` `Y` (only balance-affecting) / `N` (all) — we send `"N"`.

### Model members we set (required vs UNSET; companions keyed by Python name)

- `OrderRequest`: `intent: CheckoutPaymentIntentOrStr` **required** (`CheckoutPaymentIntent.AUTHORIZE`),
  `purchase_units: list[PurchaseUnitRequest]` **required**, `payment_source: Optional[PaymentSource]`.
- `PurchaseUnitRequest`: `amount: AmountWithBreakdown` **required** (`currency_code: str`, `value: str`
  both required), `custom_id: Optional[str]` (we set `ref-prefix:order-number`), `invoice_id: Optional[str]`
  (set `<prefix>-<number>-<attempt>`), `description: Optional[str]`.
- `PaymentSource.card: Optional[CardRequest]` — `CardRequest`: `name`, `number`, `expiry` (`YYYY-MM`),
  `security_code`, `billing_address: Optional[Address]`, `vault_id: Optional[str]` (saved card) — all Optional.
- `Address`: `country_code: str` **required**; `address_line_1`, `address_line_2`, `admin_area_2`,
  `admin_area_1`, `postal_code` Optional.
- `CaptureRequest`: `amount: Optional[Money]`, `invoice_id`, `final_capture: Optional[bool]` (True).
- `ReauthorizeRequest`: `amount: Optional[Money]` (only supported member).
- `RefundRequest`: `amount: Optional[Money]`, `custom_id`, `invoice_id`, `note_to_payer` — all Optional.
- `Money`: `currency_code: str`, `value: str` both **required**. Amount strings built from `Decimal`
  quantized to the currency's minor units (ISO table from python-models; USD → 2).
- `PaymentTokenRequest`: `payment_source: PaymentTokenRequestPaymentSource` **required**
  (`card: Optional[PaymentTokenRequestCard]` — `name, number, expiry, security_code, brand,
  billing_address` all Optional), `customer: Optional[Customer]` (`id: Optional[str]` PayPal-generated,
  `merchant_customer_id: Optional[str]`).
- `Optional[T]` here is `T | UnsetType` — never pass `None`. No `Optional[Any]` member is set by us.

### Response members read, and status enums → outcome

- `Order`: `id`, `status: OrderStatusOrStr`, `purchase_units[0].payments.authorizations[0]`
  (`AuthorizationWithAdditionalData`: `id`, `status`, `amount: Money`, `create_time`, `expiration_time`),
  `payment_source.card` (`CardResponse`: `last_digits`, `brand`, `expiry`). Smoke: single-step card
  create with intent AUTHORIZE returned `COMPLETED` + authorization `CREATED`.
  - `OrderStatus`: COMPLETED → read the authorization's status (below); PAYER_ACTION_REQUIRED →
    **failed** with `payer_action_required` (3-D Secure challenge; task says STOP, no approval round-trip);
    CREATED/SAVED/APPROVED → pending; VOIDED → failed; anything else → unknown.
- `AuthorizationStatus` (authorize step): CREATED → done; PENDING → pending; DENIED → failed;
  VOIDED → failed (undone); CAPTURED/PARTIALLY_CAPTURED → done (the hold exists and was already
  drawn on); unlisted/UNSET → unknown.
- `PaymentAuthorization` (reauthorize step): same AuthorizationStatus mapping as authorize.
- `PaymentAuthorization` (void step, `cancel_outcome`): VOIDED → done; CAPTURED/PARTIALLY_CAPTURED →
  failed (too late); DENIED → done (nothing held); CREATED/PENDING → unknown; else unknown. A 404 on the
  lookup → unknown (we never treat a missing authorization as released).
- `CapturedPayment`: `id`, `status: CaptureStatusOrStr`, `amount: Money`, `create_time`,
  `seller_receivable_breakdown` (`gross_amount: Money` **required**, `paypal_fee: Optional[Money]`,
  `net_amount: Optional[Money]`). `CaptureStatus`: COMPLETED → done; PENDING → pending;
  DECLINED/FAILED → failed; REFUNDED/PARTIALLY_REFUNDED → failed (done then undone — never success for
  the capture step); unlisted → unknown.
- `Refund`: `id`, `status: RefundStatusOrStr`, `amount`, `create_time`. `RefundStatus`: COMPLETED → done;
  PENDING → pending; FAILED/CANCELLED → failed; unlisted → unknown.
- `PaymentTokenResponse`: `id`, `customer.id`, `payment_source.card` (`CardPaymentTokenEntity`:
  `last_digits`, `brand`, `expiry`, `name`). **No status member** → done only when `id` AND
  `payment_source.card.last_digits` are present (the vaulted card is echoed); otherwise unknown.
- `delete_payment_token` raw: `Success` (2xx, observed 204 — also for an already-deleted token) → done;
  `Failure` 404 → done (gone); other 4xx → failed; 5xx → unknown.
- `SearchResponse`: `transaction_details[].transaction_info` (`TransactionInformation`:
  `transaction_id`, `paypal_reference_id`, `transaction_event_code`, `transaction_initiation_date`,
  `transaction_amount: Money`, `fee_amount`, `transaction_status` (D/P/S/V), `invoice_id`,
  `custom_field`), `total_pages`, `page`.

## OPERATION OUTCOMES

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/orders/{id}/pay` → `orders.create_order` (AUTHORIZE, card or vault_id) | `Order.status`, then `purchase_units[0].payments.authorizations[-1].status` | Order COMPLETED + auth CREATED → done 200; auth CAPTURED/PARTIALLY_CAPTURED → done 200; auth PENDING → pending 202; auth DENIED/VOIDED → failed 409 (`payment_declined`, next pay = new attempt); Order PAYER_ACTION_REQUIRED → failed 409 (`payer_action_required`); Order VOIDED → failed 409; Order CREATED/SAVED/APPROVED → pending 202; unlisted/absent status, or COMPLETED without an authorization → unknown 504; in-flight claim → `sending` 202; echoed amount ≠ order total → needs_review 409 | `provider.order_authorize_outcome` + `provider.authorization_outcome` (called by `provider.read_order_authorization`); `services.pay_order` → `services.apply_authorization`; `views.answer` |
| `POST /api/orders/{id}/fulfil` → `payments.reauthorize_payment` (only when the 3-day honor period passed) | `PaymentAuthorization.status` | CREATED/CAPTURED/PARTIALLY_CAPTURED → done (continue to capture); PENDING → pending 202; DENIED/VOIDED → failed 409 `authorization_renewal_failed` with operator action; unlisted → unknown 504; >29 days → 409 `authorization_expired` with no PayPal call | `provider.authorization_outcome` (via `provider.read_authorization`); `services._renew_authorization`; `views.answer` |
| `POST /api/orders/{id}/fulfil` → `payments.capture_authorized_payment` | `CapturedPayment.status` | COMPLETED → done 200 (amount, PayPal fee, net recorded); PENDING → pending 202; DECLINED/FAILED → failed 409; REFUNDED/PARTIALLY_REFUNDED → failed 409 (undone); unlisted/absent → unknown 504; echoed amount ≠ total → needs_review 409 | `provider.capture_outcome` (via `provider.read_capture`); `services.fulfil_order` → `services.apply_capture`; `views.answer` |
| `POST /api/orders/{id}/cancel` → `payments.void_payment` | `PaymentAuthorization.status` (the call-off's own mapper) | VOIDED → done 200; DENIED → done 200 (nothing held); CAPTURED/PARTIALLY_CAPTURED → failed 409 (payment → needs_review: PayPal says money was taken); CREATED/PENDING/unlisted → unknown 504 | `provider.void_outcome` (via `provider.read_authorization(for_void=True)`); `services.cancel_order` → `services.apply_void`; `views.answer` |
| `POST /api/orders/{id}/refunds` → `payments.refund_captured_payment` | `Refund.status` | COMPLETED → done 201; PENDING → pending 202 (a repeat re-reads via `get_refund`); FAILED/CANCELLED → failed 409 (reservation released); unlisted/absent → unknown 504; echoed amount ≠ requested → needs_review 409 | `provider.refund_outcome` (via `provider.read_refund`); `services.refund_order` → `services.apply_refund`; `views.answer` |
| `POST /api/payment-methods` → `vault.create_payment_token` | none on `PaymentTokenResponse` — the echoed `payment_source.card.last_digits` with the token `id` | id + echoed card → done 201; anything less → unknown 504 | `provider.vault_outcome` (via `provider.read_vault`); `services.save_card`; `views.answer` |
| `DELETE /api/payment-methods/{id}` → `vault.delete_payment_token` (call-off; returns `None`, read through `with_raw_response`) | raw result's HTTP status | 2xx → done 200; 404 → done 200 (gone); other 4xx → refused (ProviderError, card back to active); 5xx/transport/unreadable → unknown 504 (card stays hidden and unusable; a repeat re-sends) | `provider.delete_token`; `services.delete_card`; `views.answer` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| authorize (create_order) | `ProviderWrite` row `ref = <prefix>:order:<number>:authorize:<attempt>` (+ `PayPalPayment.state` compare-and-set awaiting→authorizing) | DB UNIQUE on `ProviderWrite.ref` (INSERT → `IntegrityError`) | `safe_write.try_claim` returns False → `safe_write.safe_write` answers from `load_existing` (in flight → 202 `sending`; settled → stored outcome) | `safe_write.try_claim`, `safe_write.safe_write`, `services.pay_order` (`services._move`) |
| reauthorize | `ProviderWrite` `ref = …:order:<n>:reauthorize:<authorization_id>` | DB UNIQUE on `ref` | `safe_write.try_claim` → `safe_write.safe_write` | `safe_write.try_claim`, `services._renew_authorization` |
| capture | `ProviderWrite` `ref = …:order:<n>:capture:<authorization_id>` (+ state authorized→capturing CAS) | DB UNIQUE on `ref`; CAS rowcount 0 | `safe_write.safe_write`; `services.fulfil_order` | `safe_write.try_claim`, `services.fulfil_order` (`services._move`) |
| void | `ProviderWrite` `ref = …:order:<n>:void:<authorization_id>` (+ state authorized→voiding CAS) | DB UNIQUE on `ref`; CAS rowcount 0 | `safe_write.safe_write`; `services.cancel_order` | `safe_write.try_claim`, `services.cancel_order` (`services._move`) |
| refund | `PayPalRefund` UNIQUE(payment, idempotency_key) + `ProviderWrite` `ref = …:order:<n>:refund:<sha256(key)[:32]>`; amount reserved under the payment row's DB write lock | DB UNIQUE constraints; reservation check `amount > captured − reserved` under the lock → 409 | `services._create_refund` (IntegrityError → the winner's refund), `services._reserve`, `safe_write.safe_write` | `services.refund_order`, `services._create_refund`, `services._reserve`, `services._lock_payment`, `safe_write.try_claim` |
| save card (create_payment_token) | `ProviderWrite` `ref = …:user:<uid>:vault:<Idempotency-Key hash, or HMAC(SECRET_KEY; uid, card, expiry, deletions)>` | DB UNIQUE on `ref` | `safe_write.safe_write` | `services.save_card`, `safe_write.try_claim` |
| delete card | `ProviderWrite` `ref = …:vault-delete:<token_id>` (+ `SavedCard.state` active→deleting CAS) | DB UNIQUE on `ref` | `safe_write.safe_write` | `services.delete_card`, `safe_write.try_claim` |

A stale `sending` claim (older than `SEND_WINDOW`, 90 s) or an `unknown`/`pending` one is checked by exactly one request at a time: `safe_write._take_over` (compare-and-set on `claimed_at`).

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| authorize (create_order) | `get_order` by the stored PayPal order id when known, else a same-reference resend (`PayPal-Request-Id`, kept 6 h; the repeat request carries the card again, or the saved vault id) | claim `ref` / PayPal order id | `safe_write._check` with `services.pay_order`'s `find` (`provider.get_order`) / `send` (`provider.create_authorization`) |
| reauthorize | same-reference resend (45 d) | claim `ref` | `safe_write._check` with `services._renew_authorization`'s `send` (`provider.reauthorize`) |
| capture | `get_captured_payment` by capture id when known, else same-reference resend (45 d) | claim `ref` / capture id | `safe_write._check` with `services.fulfil_order`'s `find` (`provider.get_capture`) / `send` (`provider.capture`) |
| void | `get_authorized_payment` by the authorization's own id, else same-reference resend (45 d) | authorization id / claim `ref` | `safe_write._check` with `services.cancel_order`'s `find` (`provider.get_authorization`) / `send` (`provider.void`) |
| refund | `get_refund` by refund id when known, else same-reference resend (45 d) | claim `ref` / refund id | `safe_write._check` with `services.refund_order`'s `find` (`provider.get_refund`) / `send` (`provider.refund`) |
| save card | same-reference resend (3 h) | claim `ref` | `safe_write._check` with `services.save_card`'s `send` (`provider.vault_card`) |
| delete card | same-reference resend (DELETE is idempotent: 204 on repeat, 404 = gone, observed) | token id (the record's own id) | `safe_write._check` with `services.delete_card`'s `send` (`provider.delete_token`) |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| authorize | Oscar `Order` + `PayPalPayment(state=authorizing, pay_attempt=n)` + `ProviderWrite(ref, outcome=sending)` | `ProviderWrite` outcome/provider id/PayPal time/data; payment PayPal order id, authorization id/status/times, card label; Oscar `Source.allocate`; order status `Payment authorized` | before: `services.place_order`, `services._move`, `safe_write.try_claim`; after: `safe_write.complete`, `services.apply_authorization` |
| reauthorize | `ProviderWrite(ref, sending)`; payment state `capturing` | new authorization id/status/create time/expiry on the payment; `Source` 'Reauthorise' transaction | before: `safe_write.try_claim`; after: `safe_write.complete`, `services._renew_authorization` |
| capture | `PayPalPayment(state=capturing)` + `ProviderWrite(ref, sending)` | capture id/status/amount/fee/net/time; `Source.debit`; order `Complete`; stock allocation consumed | before: `services._move`, `safe_write.try_claim`; after: `safe_write.complete`, `services.apply_capture` |
| void | `PayPalPayment(state=voiding)` + `ProviderWrite(ref, sending)` | payment `voided`; order `Cancelled`; stock allocation cancelled; `Source` allocation released ('Void' transaction) | before: `services._move`, `safe_write.try_claim`; after: `safe_write.complete`, `services.apply_void` |
| refund | `PayPalRefund(key, amount, outcome=sending)` + reservation on the payment + `ProviderWrite(ref, sending)` | refund id/status; `refunded_amount`, payment state; `Source.refund` (done); reservation released (failed) | before: `services._create_refund`, `services._reserve`, `safe_write.try_claim`; after: `safe_write.complete`, `services.apply_refund`, `services._release` |
| save card | `ProviderWrite(ref, sending, owner=user)` | `SavedCard(token id, customer id, brand, last 4, expiry)`; the user's PayPal customer id | before: `safe_write.try_claim`; after: `safe_write.complete`, `services._record_saved_card` |
| delete card | `SavedCard(state=deleting)` + `ProviderWrite(ref, sending)` | `SavedCard.state=deleted` + deletions counter (done) / back to `active` (refused) | before: `services.delete_card`, `safe_write.try_claim`; after: `safe_write.complete`, `services.delete_card` |

## Reconciliation design

`GET /api/reconciliation?from&to` (staff): split `[from,to)` into ≤31-day windows; for each, page
`search_transactions(start, end, fields="transaction_info", balance_affecting_records_only="N",
page_size=100, page=p)` until `p >= total_pages`. Local side: every `ProviderWrite` with outcome done/
pending whose **provider_time** is in the window (auth/capture/refund ids). Findings: `matched`
(transaction_id ∈ local ids, amounts compared as Decimal), `provider_only` (tagged `ours` when
`custom_field`/`invoice_id` carries our prefix, else `foreign`), `local_only`, `unsettled` (local writes
without provider time: sending/unknown/needs_review, claimed in window). Transport/API errors on a read
→ 502/504, never an empty report.

## Assumptions & Blockers

- No blockers. PayPal features used (card direct + vault v3 + reporting) all present in the SDK and
  verified by smoke.
- Minor: SDK declares only the sandbox host; a non-sandbox `PAYPAL_ENVIRONMENT` requires
  `PAYPAL_BASE_URL` (we fail fast instead of guessing a host).
- Minor: catalogue prices are GBP in fixtures; per task, amounts come from catalogue prices and the
  currency from `PAYPAL_CURRENCY` (the order's `currency` is set to it).
- Minor: the host notes' "CSV import optional" is wrong for this checkout — without
  `oscar_import_catalogue sandbox/fixtures/*.csv` there are only 11 products and `orders.json` fails.
- Minor: 3-D Secure challenge (PAYER_ACTION_REQUIRED) is reported as a failed pay with
  `payer_action_required`; no approval round-trip is built (task instruction).

## REQUIRED READING

- Client construction/lifetime/transport → MUST load `python-client-initialization` (loaded)
- Credentials & token-fetch failure → MUST load `python-authentication` (loaded)
- Calls, `with_raw_response` for `delete_payment_token`, `status_from_provider`/`answer` → MUST load `python-calling-endpoints` (loaded)
- Request bodies, `UNSET`, open enums, money strings → MUST load `python-models` (loaded)
- Error boundary, decode failures, transport split → MUST load `python-error-handling` (loaded)
- Safe write, base URL, timeouts, logging transport, reconciliation → MUST load `python-configuration-resilience` (loaded)
- Stub transport tests → MUST load `python-testing` (loaded)
