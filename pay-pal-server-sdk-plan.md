# PayPal Server SDK — integration plan & contract sheet

Scope: PayPal card payments (authorize → capture at fulfilment → void / refund), vaulted cards, and a
transaction-search reconciliation report, exposed as a JSON API on the django-oscar **sandbox** site
(`sandbox/apps/payments/`, routed under `/api/`).

## Toolchain & environment (verified)

| Fact | Value |
| --- | --- |
| Interpreter / env | `py -3.11`, `venv/` at repo root (git-ignored), project installed `-e .[test]` |
| SDK distribution / import root | **`paypal` / `paypal`** (version `2.29`, from `git+https://github.com/context-plugins/paypal-python-sdk.git@main`). ⚠ The `python-getting-started` skill says `pay-pal-server-sdk` / `pay_pal_server_sdk` — that is drift; the SDK's own `pyproject.toml` and `sdk-map.md` say `paypal`, and that is what is installed. |
| SDK map | cloned read-only to `../tmp/paypal-python-sdk` (outside the repo) |
| Host framework | Django 5.2 under WSGI (`sandbox/wsgi.py`, `runserver`) → **sync** |
| DB | SQLite `sandbox/db.sqlite` (git-ignored); `ATOMIC_REQUESTS=True` project-wide |
| Tests | project suite = pytest + `tests/settings.py` (defaults to PostgreSQL). Baseline on SQLite, untouched tree: `tests/integration/payment tests/integration/order` → 139 passed, 2 failed (`TestConcurrentOrderPlacement`, PostgreSQL-only concurrency). New app tests: Django `TestCase`, run with `sandbox/manage.py test apps.payments`. |
| Type checker | none configured → installed `mypy` 2.3 + `django-stubs`; gate = `mypy --strict` on the new app |
| Catalogue bootstrap | ⚠ the task's sequence fails at `orders.json` (FK error) unless `oscar_import_catalogue sandbox/fixtures/books.*.csv` runs first; with it: 209 products, 203 stock records (GBP), 249 countries, 2 users (both staff) |

## Live smoke (scratchpad `../tmp/scratch`, real sandbox credential)

- `transaction_search.search_transactions` → 200 (1726 items / 346 pages over 30 days) — entitlement OK.
- `orders.create_order` intent `AUTHORIZE` + `payment_source.card` (4111…) → order `COMPLETED`,
  `purchase_units[0].payments.authorizations[0].status == CREATED`, `expiration_time` present — **single-step,
  no PAYER_ACTION_REQUIRED challenge**. `payments.void_payment` → `VOIDED`.
- `vault.create_payment_token` (card) → id + `customer.id` + card `last_digits`/`brand`/`expiry`; paying
  with `card.vault_id` → authorization `CREATED`; `capture_authorized_payment` → `COMPLETED` with
  `seller_receivable_breakdown.{gross_amount,paypal_fee,net_amount}`; `refund_captured_payment` partial →
  `COMPLETED`; `vault.delete_payment_token` → 204; paying with the deleted token → 403.
- `vault.list_customer_payment_tokens` returned no `payment_tokens` right after creation → saved-card
  ownership is kept locally, never derived from that list.

## Decisions

- **Sync** `PaypalClient` (Django WSGI). One lazily-built, process-wide client (built on first use, so after
  any fork), closed via `atexit`. Timeout 20 s (`PAYPAL_TIMEOUT`, optional).
- **Base URL**: `PAYPAL_BASE_URL` if set, used verbatim (covers the token call too — the SDK derives
  `/v1/oauth2/token` from `base_url`). Otherwise explicit map `{"sandbox": "https://api-m.sandbox.paypal.com"}`
  (the one server `sdk-map.md` declares); any other `PAYPAL_ENVIRONMENT` → `ImproperlyConfigured` asking for
  `PAYPAL_BASE_URL` (the plugin declares no other host, so none is invented). Always passed explicitly.
- **Credentials**: `oauth2=ClientCredentials(client_id=settings.PAYPAL_CLIENT_ID, client_secret=settings.PAYPAL_CLIENT_SECRET)`;
  empty values → `ImproperlyConfigured` before any call (an omitted `oauth2` would silently send no auth).
- **Retries**: none added. Every write is attempted once per request; a may-have-landed write is settled by
  the same-`PayPal-Request-Id` resend inside the safe write, never by a new reference.
- **Oscar reuse**: `order.Order`/`order.Line` (built via `OrderCreator.place_order` from a private, offer-free
  `basket.Basket`), `payment.SourceType`/`payment.Source`/`payment.Transaction` for money tracking
  (`allocate`/`debit`/`refund`). New app models only for what Oscar has no home for: provider ids/status/fee,
  the claim ledger, vaulted-card handles.
- **Amounts**: catalogue `StockRecord` price (strategy `price.incl_tax`) × quantity, `Decimal`, formatted with
  the currency's ISO-4217 exponent. Currency = `settings.PAYPAL_CURRENCY`, also written onto the Oscar order.
- **Order statuses** (added to `OSCAR_ORDER_STATUS_PIPELINE`): `Awaiting payment` → `Payment authorized` →
  `Complete`; `Awaiting payment`/`Payment authorized` → `Cancelled`.
- **Auth**: Django session login (`POST /api/auth/login` using `django.contrib.auth.authenticate/login`, plus the
  existing `/accounts/login/`); CSRF enforced; operator endpoints require `is_staff`.
- **Transactions**: API views are `non_atomic_requests`; the claim commits in its own `atomic()` **before** the
  provider call, so a crash after the call still leaves the claim.

## Contract sheet (all from `sdk-map.md`, `map/operations/*.md`, model/enum modules)

Common: every keyword-only param has a real default (no defensive `None`s). `Optional[T]` = `T | UnsetType`
(never pass `None`). Parsed call raises `ApiError` (`.error`, `.status_code`, `.response`). Decode failure raises
`pydantic.ValidationError`/`ValueError` in both modes. Failed token fetch raises `ApiError` with
`.error: OAuthProviderError(error, error_description, error_uri) | RawError`. httpx transport errors arrive
unwrapped. No retries in the SDK. `prefer` defaults to `"return=minimal"` → pass `"return=representation"`
on every write whose body we read. Imports: client from `paypal`; models from `paypal.models`; enums from
`paypal.models.enums`; `ApiError, RawError, ClientCredentials, OAuthProviderError, HttpxClient, HttpRequest,
HttpResponse, UNSET, UnsetType` from `paypal.core`.

| Operation | Signature (positional \| keyword-only) | Returns | `ApiError.error` union | Members asserted |
| --- | --- | --- | --- | --- |
| `client.orders.create_order` `POST /v2/checkout/orders` | `body: OrderRequest` \| `pay_pal_request_id` (header `PayPal-Request-Id`; "mandatory for single-step create order calls" with card/vault_id; keys stored 6 h), `prefer` | `Order` | `CreateOrderErrorBody = Error [400,401,422] \| RawError` | `id`, `status` (OrderStatus), `purchase_units[0].payments.authorizations[0].{id,status,amount.value,amount.currency_code,expiration_time,create_time}`, `payment_source.card.{last_digits,brand}` |
| `client.payments.get_authorized_payment` `GET /v2/payments/authorizations/{authorization_id}` | `authorization_id` \| — | `PaymentAuthorization` | `GetAuthorizedPaymentErrorBody = Error [401,403,404] \| RawError [500,…]` | `status`, `expiration_time`, `create_time` |
| `client.payments.reauthorize_payment` `POST …/{authorization_id}/reauthorize` | `authorization_id` \| `pay_pal_request_id` (45 d), `prefer`, `body: ReauthorizeRequest(amount: Optional[Money])` | `PaymentAuthorization` | `ReauthorizePaymentErrorBody = Error [400,401,403,404,422] \| RawError [500,…]` | `id` (new auth id), `status`, `amount`, `create_time`, `expiration_time` |
| `client.payments.capture_authorized_payment` `POST …/{authorization_id}/capture` | `authorization_id` \| `pay_pal_request_id` (45 d), `prefer`, `body: CaptureRequest(amount: Optional[Money], invoice_id, final_capture: Optional[bool])` | `CapturedPayment` | `CaptureAuthorizedPaymentErrorBody = Error [400,401,403,404,409,422] \| RawError [500,…]` | `id`, `status`, `amount`, `seller_receivable_breakdown.{gross_amount (required), paypal_fee, net_amount}`, `create_time` |
| `client.payments.void_payment` `POST …/{authorization_id}/void` | `authorization_id` \| `pay_pal_request_id` (45 d), `prefer` | `PaymentAuthorization` (body may be empty under `return=minimal`; we send `return=representation`) | `VoidPaymentErrorBody = Error [401,403,404,409,422] \| RawError [500,…]` | `id`, `status`, `update_time` |
| `client.payments.refund_captured_payment` `POST /v2/payments/captures/{capture_id}/refund` | `capture_id` \| `pay_pal_request_id` (45 d), `prefer`, `body: RefundRequest(amount: Optional[Money], invoice_id, custom_id, note_to_payer)` | `Refund` | `RefundCapturedPaymentErrorBody = Error [400,401,403,404,409,422] \| RawError [500,…]` | `id`, `status`, `amount`, `create_time` |
| `client.vault.create_payment_token` `POST /v3/vault/payment-tokens` | `body: PaymentTokenRequest(customer: Optional[Customer(id)], payment_source: PaymentTokenRequestPaymentSource(card: PaymentTokenRequestCard(name, number, expiry "YYYY-MM", security_code, billing_address: Address(country_code required))))` \| `pay_pal_request_id` (3 h) | `PaymentTokenResponse` | `CreatePaymentTokenErrorBody = Error [400,403,404,422,500] \| RawError` | `id`, `customer.id`, `payment_source.card.{last_digits,brand,expiry}` |
| `client.vault.delete_payment_token` `DELETE /v3/vault/payment-tokens/{id}` | `id` \| — | **`None`** → use `with_raw_response` (`ApiResult[None, …]`) to see 204 | `DeletePaymentTokenErrorBody = Error [400,403,500] \| RawError` | status code |
| `client.transaction_search.search_transactions` `GET /v1/reporting/transactions` | `start_date: str, end_date: str` (RFC 3339, seconds required, **max range 31 days**) \| `fields="transaction_info"`, `balance_affecting_records_only="Y"` (we pass `"N"` to include authorizations), `page_size=100`, `page=1` | `SearchResponse` | **Case B: `RawError` only** | `transaction_details[].transaction_info.{transaction_id, paypal_reference_id, transaction_event_code, transaction_initiation_date, transaction_amount, fee_amount, transaction_status, invoice_id, custom_field}`, `total_pages`, `page` |

Request models used: `OrderRequest(intent: CheckoutPaymentIntentOrStr [req], purchase_units: list[PurchaseUnitRequest] [req], payment_source)`,
`PurchaseUnitRequest(amount: AmountWithBreakdown [req], reference_id, invoice_id, custom_id, description)`,
`AmountWithBreakdown(currency_code: str [req], value: str [req])`, `Money(currency_code [req], value [req])`,
`PaymentSource(card: CardRequest)`, `CardRequest(name, number, expiry, security_code, billing_address, vault_id)`,
`Address(country_code [req], address_line_1, admin_area_2, postal_code, …)`. Enum `CheckoutPaymentIntent.AUTHORIZE`.
Wire aliases: none on the members we set (`CardResponse.type_` ↔ `type` is read-only, unused).
Transaction-search lag: "maximum of three hours for executed transactions to appear" (docstring).
Reauthorize rules (ReauthorizeRequest docstring): 3-day honor period; reauthorize **once**, days 4–29; after 30 days a
new authorization is required; the reauthorized payment has a new 3-day honor period.

### Status enums → outcome (`status_from_provider`)

| Enum (module) | done | pending (not-yet) | failed | unknown |
| --- | --- | --- | --- | --- |
| `OrderStatus` (create_order) | `COMPLETED` (then the authorization's own status decides) | `CREATED`, `SAVED`, `APPROVED` | `VOIDED`; `PAYER_ACTION_REQUIRED` → failed with "browser approval required — not supported" | anything else / absent |
| `AuthorizationStatus` (authorize, reauthorize) | `CREATED` | `PENDING` | `DENIED`, `VOIDED` | `CAPTURED`, `PARTIALLY_CAPTURED`, other, absent |
| `AuthorizationStatus` (void) | `VOIDED` | `CREATED`, `PENDING` | `CAPTURED`, `PARTIALLY_CAPTURED` (money moved — cannot release) | `DENIED`, other, absent |
| `CaptureStatus` (capture) | `COMPLETED` | `PENDING` | `DECLINED`, `FAILED`, `REFUNDED`, `PARTIALLY_REFUNDED` (undone ≠ done) | other, absent |
| `RefundStatus` (refund) | `COMPLETED` | `PENDING` | `FAILED`, `CANCELLED` | other, absent |

## OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| authorize (`orders.create_order`, intent AUTHORIZE) | `Order.status`, then `purchase_units[0].payments.authorizations[0].status` | order `COMPLETED` + auth `CREATED` → done: order → `Payment authorized`, `Source.allocate`; auth `PENDING` / order `CREATED`/`SAVED`/`APPROVED` → pending: 202, order stays `Awaiting payment`, record kept; `DENIED`/`VOIDED` → failed: 402, shopper may retry under the next attempt reference; `PAYER_ACTION_REQUIRED` → failed with explicit "browser approval not supported"; `CAPTURED`/`PARTIALLY_CAPTURED`/unlisted/absent → unknown: 202 + `needs operator`, never success | `sandbox/apps/payments/outcomes.py::order_outcome` → `authorization_outcome`; read in `services.py::pay_order.read`, acted on in `pay_order.apply` and `pay_order` (200/202/402) |
| reauthorize (`payments.reauthorize_payment`) | `PaymentAuthorization.status` | `CREATED` → done: capture proceeds on the new auth id; `PENDING` → pending: fulfil answers 202 "renewal pending", no capture; `DENIED`/`VOIDED` → failed: 409 operator message "cannot be renewed"; `CAPTURED`/`PARTIALLY_CAPTURED`/unlisted/absent → unknown: 502/504 needs review, no capture | `sandbox/apps/payments/outcomes.py::authorization_outcome` via `services.py::_read_authorization`; acted on in `services.py::_renew_authorization` (and its `apply`) |
| capture (`payments.capture_authorized_payment`) | `CapturedPayment.status` | `COMPLETED` → done: `Source.debit`, fee/net stored, order → `Complete`; `PENDING` → pending: 202, capture recorded pending, order not completed; `DECLINED`/`FAILED`/`REFUNDED`/`PARTIALLY_REFUNDED` → failed: 402/409, order not completed; unlisted/absent → unknown: 202 + needs review | `sandbox/apps/payments/outcomes.py::capture_outcome` in `services.py::fulfil_order.read`; acted on in `services.py::_record_capture` and `fulfil_order` (200/202/409) |
| void (`payments.void_payment`) | `PaymentAuthorization.status` | `VOIDED` → done: order → `Cancelled`, auth status stored; `CREATED`/`PENDING` → pending: 202, order not cancelled; `CAPTURED`/`PARTIALLY_CAPTURED` → failed: 409 "already captured — refund instead"; `DENIED`/unlisted/absent → unknown: 202 + needs review | `sandbox/apps/payments/outcomes.py::void_outcome` via `services.py::_read_authorization`; acted on in `services.py::cancel_order.apply` and `cancel_order` (200/202/409) |
| refund (`payments.refund_captured_payment`) | `Refund.status` | `COMPLETED` → done: `Source.refund`, 201; `PENDING` → pending: 202, amount stays reserved against the cap; `FAILED`/`CANCELLED` → failed: 402, reservation released; unlisted/absent → unknown: 202, amount stays reserved | `sandbox/apps/payments/outcomes.py::refund_outcome` in `services.py::refund_order.read`; acted on in `services.py::_apply_refund`, `_refresh_pending_refund`, `_answer_refund` |
| save card (`vault.create_payment_token`) | none — `PaymentTokenResponse` has no status member | done only when `id` and `payment_source.card` are present; otherwise unknown → 504 `OutcomeUnknown`, settled by repeating the request (same reference) | `sandbox/apps/payments/services.py::save_card.read` (done only with id + card, else unknown → `OutcomeUnknown`), `save_card.apply` |
| delete card (`vault.delete_payment_token`) | none — returns `None`; raw 204 | the card is marked removed locally first (unusable at once); 2xx or 404 → vault deletion done (204); any other failure → 202 "vault deletion pending", retried on a repeated DELETE | `sandbox/apps/payments/services.py::delete_saved_card` (`with_raw_response`; `Success` or 404 → done; anything else → 202, card already unusable, retried on repeat DELETE) |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| authorize (`create_order`) | `ProviderWrite` row, `reference = "{prefix}:o{order_pk}:authorize:{attempt}"` (attempt = refused attempts + 1), same string sent as `PayPal-Request-Id` | `UNIQUE(reference)` constraint in the DB (SQLite/PostgreSQL) | `IntegrityError` in `try_claim` → loser answers from the stored record (in flight → 409 "in progress"; unknown/stale → same-key resend) | `sandbox/apps/payments/safe_write.py::try_claim` / `safe_write`; reference from `safe_write.py::reference` + `next_attempt` in `services.py::pay_order` |
| reauthorize | `ProviderWrite` `"{prefix}:o{pk}:reauthorize:{attempt}"` | `UNIQUE(reference)` | `try_claim` | `sandbox/apps/payments/safe_write.py::try_claim` / `safe_write`, called from `services.py::_renew_authorization` |
| capture | `ProviderWrite` `"{prefix}:o{pk}:capture:{attempt}"` | `UNIQUE(reference)` | `try_claim` | `sandbox/apps/payments/safe_write.py::try_claim` / `safe_write`, called from `services.py::fulfil_order` |
| void | `ProviderWrite` `"{prefix}:o{pk}:void:{attempt}"` | `UNIQUE(reference)` | `try_claim` | `sandbox/apps/payments/safe_write.py::try_claim` / `safe_write`, called from `services.py::cancel_order` |
| refund | `ProviderWrite` `"{prefix}:o{pk}:refund:{sha256(caller key)[:32]}"` + amount reserved under a row lock on the payment record | `UNIQUE(reference)`; over-refund rejected inside the same `atomic()` that inserts the claim (`select_for_update` on `PaypalPayment`) | `try_claim` (duplicate key → stored outcome; same key + different amount → 422) | `sandbox/apps/payments/safe_write.py::try_claim` (runs `reserve`) / `safe_write`, called from `services.py::refund_order` (its `reserve` locks `PaypalPayment`) |
| save card | `ProviderWrite` `"{prefix}:u{user_pk}:vault:{card_fingerprint[:24]}:{generation}:{attempt}"` (fingerprint = HMAC-SHA256 keyed with `SECRET_KEY`, never reversible) | `UNIQUE(reference)` | `try_claim` → returns the already-saved card | `sandbox/apps/payments/safe_write.py::try_claim` / `safe_write`, called from `services.py::save_card` (fingerprint: `services.py::card_fingerprint`) |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| authorize (`create_order`) | same-`PayPal-Request-Id` resend of the identical body (PayPal returns the original order for a repeated key; window 6 h — outside it the record stays `unknown` for an operator) | claim reference = `PayPal-Request-Id` | `sandbox/apps/payments/safe_write.py::safe_write` (second `send(ref)` after 5xx / `httpx.RequestError` / `ValueError`; `checking` path for a stale or unknown claim) |
| reauthorize | same-key resend (45 d window) | claim reference | `sandbox/apps/payments/safe_write.py::safe_write` with `send` from `services.py::_renew_authorization` |
| capture | same-key resend (45 d) | claim reference | `sandbox/apps/payments/safe_write.py::safe_write` with `send` from `services.py::fulfil_order` |
| void | same-key resend (45 d) | claim reference | `sandbox/apps/payments/safe_write.py::safe_write` with `send` from `services.py::cancel_order` |
| refund | same-key resend (45 d) | claim reference (derived from the caller's idempotency key) | `sandbox/apps/payments/safe_write.py::safe_write` with `send` from `services.py::refund_order` (repeat of a sending/unknown key re-enters `safe_write` with the stored amount) |
| save card | same-key resend (3 h) | claim reference | `sandbox/apps/payments/safe_write.py::safe_write` with `send` from `services.py::save_card` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| authorize | Oscar `Order` (`Awaiting payment`) + committed `ProviderWrite(reference, outcome=sending, amount, currency)` + `PaypalPayment(state=authorizing)` | order id, auth id/status/expiry/create time, card label; `Source.allocate`; outcome | `sandbox/apps/payments/services.py::place_order` (Order + `PaypalPayment`), `safe_write.py::try_claim` (claim), `services.py::pay_order.apply` (after) |
| reauthorize | committed `ProviderWrite(sending)` | new auth id/status/expiry, `reauthorized_at`; outcome | `sandbox/apps/payments/safe_write.py::try_claim`, `services.py::_renew_authorization.apply` |
| capture | committed `ProviderWrite(sending, amount)` | capture id/status/amount/fee/net/time; `Source.debit`; order `Complete`; outcome | `sandbox/apps/payments/safe_write.py::try_claim`, `services.py::fulfil_order.apply` → `_record_capture` |
| void | committed `ProviderWrite(sending)` | auth status `VOIDED`; order `Cancelled`; outcome | `sandbox/apps/payments/safe_write.py::try_claim`, `services.py::cancel_order.apply` |
| refund | committed `ProviderWrite(sending, amount)` (amount reserved against the cap) | refund id/status; Oscar `Transaction(Refund)`; `Source.refund` on done; outcome | `sandbox/apps/payments/safe_write.py::try_claim` + `refund_order.reserve`, `services.py::_apply_refund` |
| save card | committed `ProviderWrite(sending)` | `SavedCard(vault token id, brand, last digits, expiry)`, `PaypalCustomer`; outcome | `sandbox/apps/payments/safe_write.py::try_claim`, `services.py::save_card.apply` |

## Error boundary (`python-error-handling` ladder → HTTP)

`OAuthProviderError` → 502 config; 401/403 → 502; 429 → 503; typed `Error` with 400/404/409/422 → caller
fault with PayPal's `message`/`details[].issue` (card declined etc. → 402 for payment writes); other → 502
(`outcome_unknown` for 5xx on writes, settled inside the safe write first); `ValidationError`/`ValueError` on a 2xx →
unknown; `ConnectError/ConnectTimeout/PoolTimeout/ProxyError` → 502 never-sent; other `httpx.RequestError` → 504
unknown. Never surface `str(e)`.

## Assumptions & Blockers

- Minor: PayPal honours `PayPal-Request-Id` de-duplication as the docstrings describe (6 h create order, 45 d payments,
  3 h vault). Beyond the window an unknown stays `unknown` for an operator.
- Minor: `transaction_search.search_transactions` max `page_size` is not stated in the SDK → the default `100` is used and
  every page is walked; range split into ≤31-day windows (docstring limit).
- Minor: for `PAYPAL_ENVIRONMENT` other than `sandbox`, `PAYPAL_BASE_URL` must be set (the plugin declares only the sandbox host).
- Design note: a write PayPal *refused* (400/404/409/422) keeps its claim as `failed` — PayPal has seen that
  `PayPal-Request-Id` — and the next attempt takes the next `:{attempt}` reference (`safe_write.py::next_attempt`).
  Only writes that provably never reached PayPal (connect failures, token-fetch failure, 401/403/429) release the claim.
- Minor: on SQLite `select_for_update` is a no-op; the refund cap still holds because SQLite serialises writers (a
  concurrent second writer fails with "database is locked" → 500, nothing sent). On PostgreSQL the row lock applies.
- No blockers: every capability (authorize, capture, reauthorize, void, refund, vault create/delete, transaction search) is
  in the SDK and was smoked live; no browser challenge was returned for the test card.

## REQUIRED READING

- Client construction/lifetime (lazy module-level sync client, `atexit` close) — MUST load `python-client-initialization` ✅ loaded
- Credentials, lazy token fetch, `OAuthProviderError` — MUST load `python-authentication` ✅ loaded
- Keyword-only calls, `prefer`, `with_raw_response` for `delete_payment_token`, status-driven outcomes — MUST load `python-calling-endpoints` ✅ loaded
- `UNSET` vs `None`, open enums, money as `Decimal` strings — MUST load `python-models` ✅ loaded
- Error ladder / transport split — MUST load `python-error-handling` ✅ loaded
- Safe write, reconciliation clocks, logging transport, no retries — MUST load `python-configuration-resilience` ✅ loaded
- Stub transport tests — MUST load `python-testing` ✅ loaded
