# PayPal integration plan — django-oscar sandbox (`sandbox/apps/paypal_payments`)

## SDK identity (verified against the installed package + its source tree, `main` @ 2.29)

| Fact | Value |
| --- | --- |
| Distribution / import root | `paypal` / `paypal` (**drift**: the plugin's getting-started page says `pay-pal-server-sdk` / `pay_pal_server_sdk`; the installed package and its own `sdk-map.md` say `paypal`) |
| Client | `paypal.PaypalClient` (sync) — keyword-only: `base_url`, `timeout` (30.0), `custom_http_client`, `oauth2`, `oauth2_token_source` |
| Install | not on an index; installed (non-editable) from the `main` branch archive of `context-plugins/paypal-python-sdk` |
| Hosts declared by the SDK | only `https://api-m.sandbox.paypal.com` (the default). No other host is documented by the plugin, so a non-`sandbox` `PAYPAL_ENVIRONMENT` **requires** `PAYPAL_BASE_URL`; we never invent a host |
| Auth | `oauth2=ClientCredentials(client_id=…, client_secret=…)`; token from `<base_url>/v1/oauth2/token`, lazily, cached per client. Omitting `oauth2` sends unauthenticated requests silently → we fail fast at client build if either credential is empty |

## Decisions

- **Sync client.** Django under WSGI with sync views → `PaypalClient`. One lazily built, process-wide client (built after fork on first use), closed via `atexit`. Never per request.
- **Transport seam:** `HttpxClient(timeout=…)` wrapped by our `_ObservedTransport` which (a) logs method, path, status and latency only — never headers or bodies (no card data, no bearer token); (b) records the last response status in a thread-local so a `ValidationError` raised while decoding an *error* body can still be classified by status (observed live: vault 404 bodies fail to decode as `Error`).
- **Retries:** the SDK does none. We add none for writes; every write carries a deterministic `PayPal-Request-Id` so a *caller* retry (double click, network retry) replays safely at PayPal. Reads (`get_authorized_payment`, `get_captured_payment`, `search_transactions`) are retried up to 3 times with exponential backoff on 429/5xx/transport failures — a live 40-day reconciliation hit a transient 503 mid-pagination.
- **Search page size 500:** PayPal's own 400 for `page_size=501` states the maximum ("must be less than or equal to 500").
- **`prefer="return=representation"` on every write.** Default is `return=minimal`; observed live: `void_payment` then answers 204/empty and the parsed call raises `ValueError`; captures would omit `seller_receivable_breakdown`.
- **Money:** `Decimal`, quantized per currency exponent (table incl. 0- and 3-decimal currencies), sent as `str`.
- **Order total → PayPal:** one purchase unit, `amount.value` = Oscar `order.total_incl_tax` in `PAYPAL_CURRENCY`; `custom_id` = Oscar order number (reference only; matching is by PayPal ids).

## Contract sheet (every row from the SDK map / source modules; `R` = raw peer used)

All calls: `ApiError.error` union is `Error | RawError` (Case A) except `search_transactions` (Case B: `RawError` only). Keyword-only tail after positional params; every keyword has a real default, so no defensive `None`s. Models are frozen pydantic; `Optional[T]` = `T | UnsetType` (never pass `None`).

| Operation (map page) | Positional | Keywords we set | Returns | Members we assert on |
| --- | --- | --- | --- | --- |
| `orders.create_order` (orders.md) | `body: OrderRequest` | `pay_pal_request_id` (mandatory for single-step card orders, docstring), `prefer=REP` | `Order` | `id`, `status`, `purchase_units[0].payments.authorizations[0].{id,status,amount,expiration_time,create_time}` |
| `payments.get_authorized_payment` (payments.md) | `authorization_id` | – | `PaymentAuthorization` | `status`, `expiration_time`, `create_time` |
| `payments.reauthorize_payment` | `authorization_id` | `pay_pal_request_id`, `prefer=REP`, `body=ReauthorizeRequest(amount=Money)` | `PaymentAuthorization` | `id`, `status` |
| `payments.capture_authorized_payment` | `authorization_id` | `pay_pal_request_id`, `prefer=REP`, `body=CaptureRequest(amount, final_capture=True)` | `CapturedPayment` | `id`, `status`, `amount`, `seller_receivable_breakdown.{gross_amount (required), paypal_fee, net_amount}` |
| `payments.get_captured_payment` | `capture_id` | – | `CapturedPayment` | as above (used when breakdown absent/pending) |
| `payments.void_payment` | `authorization_id` | `pay_pal_request_id`, `prefer=REP` (**mandatory**, see above) | `PaymentAuthorization` | `status` |
| `payments.refund_captured_payment` | `capture_id` | `pay_pal_request_id`, `prefer=REP`, `body=RefundRequest(amount=Money)` | `Refund` | `id`, `status`, `amount`, `seller_payable_breakdown` |
| `vault.create_payment_token` (vault.md) | `body: PaymentTokenRequest` | `pay_pal_request_id` | `PaymentTokenResponse` | `id`, `customer.id`, `payment_source.card.{brand,last_digits,expiry,name}` |
| `vault.delete_payment_token` | `id` | – (**R**: returns `None`; raw peer used to see status) | `None` | status 204 |
| `transaction_search.search_transactions` (transaction_search.md) | `start_date, end_date` (RFC 3339 with seconds; ≤31 days per call) | `fields="transaction_info"`, `balance_affecting_records_only="N"`, `page_size=500`, `page` | `SearchResponse` | `total_pages`, `transaction_details[].transaction_info.{transaction_id, paypal_reference_id, transaction_event_code, transaction_initiation_date, transaction_amount, fee_amount, transaction_status, custom_field}` |

### Request models (members set; source `paypal/models/<snake>.py`)

- `OrderRequest`: `intent: CheckoutPaymentIntentOrStr` (req) = `CheckoutPaymentIntent.AUTHORIZE`; `purchase_units: list[PurchaseUnitRequest]` (req); `payment_source: PaymentSource`.
- `PurchaseUnitRequest`: `amount: AmountWithBreakdown` (req; `currency_code: str`, `value: str`); `custom_id`, `description`.
- `PaymentSource.card: CardRequest` — `name`, `number`, `expiry` (`YYYY-MM`), `security_code`, `billing_address: Address` (`address_line_1`, `address_line_2`, `admin_area_2`, `admin_area_1`, `postal_code`, `country_code` req) **or** `vault_id`.
- `PaymentTokenRequest`: `customer: Customer(id=…)` (optional; reuse the shopper's PayPal customer id), `payment_source: PaymentTokenRequestPaymentSource` (req) → `card: PaymentTokenRequestCard` (`name`, `number`, `expiry`, `security_code`, `billing_address`).
- `Money` (`currency_code`, `value`) for capture / reauthorize / refund amounts.

### Enums (`paypal.models.enums`; all open `…OrStr` — unknown wire values arrive as `str`)

- `CheckoutPaymentIntent`: CAPTURE, AUTHORIZE
- `OrderStatus`: CREATED, SAVED, APPROVED, VOIDED, COMPLETED, PAYER_ACTION_REQUIRED
- `AuthorizationStatus`: CREATED, CAPTURED, DENIED, PARTIALLY_CAPTURED, VOIDED, PENDING
- `CaptureStatus`: COMPLETED, DECLINED, PARTIALLY_REFUNDED, PENDING, REFUNDED, FAILED
- `RefundStatus`: CANCELLED, FAILED, PENDING, COMPLETED

### Observed sandbox behaviour (scratchpad smoke, 2026-09-23)

- Card + `AUTHORIZE` create_order → `COMPLETED`, authorization already present (single step; no `authorize_order` call needed). `PAYER_ACTION_REQUIRED` would mean a browser challenge → **stop/report, do not build an approval round-trip**.
- Authorization `expiration_time` = create + 29 days. Reauthorize inside the 3-day honor period → 422 `REAUTHORIZATION_TOO_SOON`. Capture of a voided auth → 422 `AUTHORIZATION_VOIDED`; void twice → 422 `PREVIOUSLY_VOIDED`. Expired card → 422 `CARD_EXPIRED`.
- Vault direct card tokenization works; passing `customer.id` groups tokens; delete is 204 and repeat delete is also 204.
- Vault 404 error bodies fail to decode as `Error` → `ValidationError` on a non-2xx.
- Transaction search: 30-day window had 64 pages at page_size 100 → pagination is mandatory.

### Failure boundary (one translator, `paypal_payments/gateway.py`)

1. `ApiError` whose `.error` is `OAuthProviderError` → config fault → HTTP 502 `payment_provider_misconfigured`.
2. `ApiError` 401/403 → ours → 502; 429 → 503; 400/404/409/422 with `Error` → the caller's → 422 (or 409) carrying PayPal `name` + first `details[].issue`; everything else → 502.
3. `ValidationError`/`ValueError`: last status non-2xx → rejected (422, detail lost); 2xx → **outcome unknown** → 502 + payment left in its in-progress state for reconciliation (never reported as a decline).
4. `httpx.ConnectError | ConnectTimeout | PoolTimeout | ProxyError` → never sent → 502, `outcome_unknown=False` (in-progress claim released).
5. other `httpx.RequestError` → maybe landed → 504, `outcome_unknown=True` (claim kept; a repeat of the same request replays the same `PayPal-Request-Id`).
6. After every success, assert the members we depend on (ids); missing → outcome unknown.

## Architecture

`sandbox/apps/paypal_payments/` (label `paypal_payments`; named to avoid shadowing the `paypal` SDK):
- `models.py` — `PayPalPayment` (1:1 Oscar `order.Order`): state machine, PayPal order/authorization/capture ids & statuses, fee/net, request-key attempt counter; `PayPalRefund` (idempotency key unique per payment); `PayPalCustomer` (user → vault customer id). Saved cards reuse Oscar's `payment.Bankcard` (`partner_reference` = vault token id, masked number, brand, expiry). Money movement is mirrored on Oscar's `payment.Source`/`Transaction` (`allocate`/`debit`/`refund`) and Oscar order status pipeline (`Pending → Being processed → Complete`, `→ Cancelled`).
- `gateway.py` — client factory (settings), transport, error translation, typed wrappers.
- `services.py` — order placement (Oscar `Basket` + `OrderCreator`), pay / fulfil / cancel / refund / vault / reconciliation, with atomic state claims (conditional `UPDATE … WHERE state=…`) so a double click cannot authorize or capture twice.
- `views.py` + `urls.py` — JSON endpoints, `non_atomic_requests` (so PayPal calls never run inside the sandbox's `ATOMIC_REQUESTS` transaction), session auth, CSRF enforced, staff gating for fulfil/cancel/reconciliation.
- Settings: `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENVIRONMENT`, `PAYPAL_CURRENCY`, `PAYPAL_BASE_URL` read from env in `sandbox/settings.py`.
- Tests: `sandbox/apps/paypal_payments/tests/` (Django `TestCase`, stub transport per python-testing).

## Assumptions & blockers

- No blockers. Minor: API orders need no shipping (`Free` shipping method, optional shipping address); order currency = `PAYPAL_CURRENCY` applied to the catalogue's numeric prices (the catalogue fixtures are priced in GBP; the brief says currency comes from configuration).
- Staleness rule: honor period = 3 days after the (re)authorization's `create_time` (docstring of `reauthorize_payment`); expiry = PayPal's `expiration_time`.

## REQUIRED READING (all loaded before implementation)

- MUST load `python-error-handling` — the ladder above, ValidationError-on-error-body, transport split.
- MUST load `python-client-initialization` — module-level lazy client, close obligation, fork safety.
- MUST load `python-authentication` — `OAuthProviderError` surfaces from the first operation call.
- MUST load `python-calling-endpoints` — `prefer` default narrows responses; raw peer for `delete_payment_token`.
- MUST load `python-models` — `UNSET` handling, open enums, currency exponent.
- MUST load `python-configuration-resilience` — no retries, explicit base URL, logging via transport.
- MUST load `python-testing` — stub transport, token request first, lowercase header keys.
