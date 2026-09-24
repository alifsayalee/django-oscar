# PayPal integration plan — django-oscar sandbox (`sandbox/apps/payments_api`)

## SDK identity (verified against the installed package + SDK map, commit `0ed3d22071e97650113c99fd82c42bc815f99dcf` of `context-plugins/paypal-python-sdk@main`)

| Fact | Value |
| --- | --- |
| Distribution / import root | `paypal` / `paypal` — **drift**: the `python-getting-started` skill page says `pay-pal-server-sdk` / `pay_pal_server_sdk`; the map (`sdk-map.md`) and `pyproject.toml` on `main` say `paypal`. Source wins. |
| Version | `2.29` |
| Client | sync `PaypalClient` (alias `Client`), keyword-only: `base_url`, `timeout` (30.0), `custom_http_client`, `oauth2`, `oauth2_token_source` |
| Install | `pip install "paypal @ git+https://github.com/context-plugins/paypal-python-sdk.git@0ed3d22071e97650113c99fd82c42bc815f99dcf"` → recorded in `sandbox/requirements-paypal.txt` (NOT in `pyproject.toml`: django-oscar is published to PyPI, which rejects direct-URL dependencies) |
| Imports | client from `paypal`; models from `paypal.models`; enums from `paypal.models.enums`; `ApiError`, `RawError`, `ClientCredentials`, `HttpxClient`, `HttpRequest`, `HttpResponse`, `OAuthProviderError`, `UNSET`, `UnsetType`, `Success`, `Failure` from `paypal.core` |

## Decisions

1. **Sync client.** Host is Django under WSGI (`sandbox/wsgi.py`, `runserver`) — no async views. `PaypalClient`, never `AsyncPaypalClient`.
2. **Client lifetime.** One module-level client, built lazily on first use (post-fork safe), guarded by a lock, closed via `atexit`. Transport: `LoggingTransport(HttpxClient(timeout=PAYPAL_TIMEOUT))` — logs method, path (no query), status, ms, `paypal-debug-id` header. Never headers/bodies (card data lives in bodies). The sandbox root logger runs at DEBUG, so `httpx`/`httpcore` loggers are pinned to WARNING in `settings.LOGGING` to keep wire traces out of the logs.
3. **Base URL.** Always passed explicitly. `PAYPAL_BASE_URL` set → used verbatim (moves token traffic too — map: tokens come from `/v1/oauth2/token` on the base URL). Else `PAYPAL_ENVIRONMENT == "sandbox"` → `https://api-m.sandbox.paypal.com` (the only server the map declares). Any other environment without `PAYPAL_BASE_URL` → `ImproperlyConfigured` (the plugin declares no other host; no URL from memory).
4. **Auth.** `oauth2=ClientCredentials(client_id=settings.PAYPAL_CLIENT_ID, client_secret=settings.PAYPAL_CLIENT_SECRET)`; empty values → `ImproperlyConfigured` before construction (an omitted `oauth2` silently means no auth).
5. **No retries of writes.** SDK performs none. Writes are made safe by a deterministic `PayPal-Request-Id` and our own stored outcome; a repeat of the same API request re-sends under the same id (PayPal dedups — verified live on capture + refund: repeat returns `200` with the original object). Reads (`get_captured_payment`, `get_refund`, `search_transactions`) retry up to 3× on never-sent transport errors, read timeouts, 429, 5xx with short backoff.
6. **Response mode.** Parsed calls everywhere (`ApiError` on failure) except `vault.delete_payment_token` → `with_raw_response` (returns `None`; the raw peer is the only place the status is visible).
7. **Oscar reuse.** Orders: Oscar `Basket` + `Selector` strategy + `OrderCreator.place_order` + `NoShippingRequired` (→ `order.Order`/`order.Line`). Money ledger: Oscar `payment.SourceType("PayPal")` / `payment.Source` (`allocate` / `debit` / `refund`) + `payment.Transaction`. Order status via the sandbox pipeline: `Pending` → `Being processed` (authorized) → `Complete` (captured) / `Cancelled`. Stock: `EventHandler.consume_stock_allocations` on fulfil, `cancel_stock_allocations` on cancel.
8. **PayPal-owned state** lives in new models in `payments_api`: `PaypalPayment` (1:1 Oscar order: reference, attempt, status, PayPal order/auth/capture ids + statuses, captured/fee/net, auth create/expiry times, last operator-facing error), `PaypalRefund` (unique `(payment, idempotency_key)`), `SavedCard` (vault token id + brand/last4/expiry only), `PaypalCustomer` (user → PayPal vault customer id). No PAN/CVV is ever stored or logged; card-handling functions are `@sensitive_variables`.
9. **Currency/amounts.** Currency = `settings.PAYPAL_CURRENCY`; amount = order `total_incl_tax` (from catalogue stock-record prices) formatted with the ISO-4217 exponent (`Decimal.quantize`, never `.2f`); an order total that the currency can't represent is rejected at order creation.
10. **HTTP surface.** Plain Django function views returning JSON (no DRF — not a sandbox dependency). Session auth + CSRF kept on; helper endpoints `GET /api/auth/csrf`, `POST /api/auth/login`, `POST /api/auth/logout` (Django `authenticate`/`login` = the sandbox's session login, including Oscar's email backend). Fulfil/cancel/reconciliation require `is_staff`; everything else scoped to `request.user`; foreign objects → 404.

## Contract sheet (every row from the map / named module / docstring / live sandbox smoke)

Keyword-only boundary: everything after `*` has a real default — never pass defensive `None`s. Every operation's `ApiError.error` union is `Error | RawError` except `search_transactions` (Case B: always `RawError`). `Error` (`paypal/models/error.py`): `name: str`, `message: str`, `debug_id: str`, `details: Optional[list[ErrorDetails]]`; `ErrorDetails`: `issue: str`, `description: Optional[str]`, `field`, `value`, `location`. Token failure: `ApiError` whose `.error` is `OAuthProviderError` (`error`, `error_description`) — checked first.

| Operation | Signature (positional · keyword) | Error arms | Use |
| --- | --- | --- | --- |
| `orders.create_order` | `(body: OrderRequest\|Dict, *, pay_pal_request_id, prefer="return=minimal", …)` → `Order` | `Error` 400/401/422 · `RawError` | authorize-at-create: `intent=AUTHORIZE` + `payment_source.card`; `pay_pal_request_id` **mandatory** for single-step card orders (docstring) — `f"{reference}-authorize-{attempt}"`; `prefer="return=representation"` |
| `payments.capture_authorized_payment` | `(authorization_id, *, pay_pal_request_id, prefer, body: CaptureRequest\|Dict)` → `CapturedPayment` | `Error` 400/401/403/404/409/422 · `RawError` 500+ | fulfil; id `f"{reference}-capture-{authorization_id}"` (keys kept 45 days) |
| `payments.reauthorize_payment` | `(authorization_id, *, pay_pal_request_id, prefer, body: ReauthorizeRequest\|Dict)` → `PaymentAuthorization` | `Error` 400/401/403/404/422 · `RawError` | renew stale auth; allowed once, day 4–29 after the original auth (live `422 REAUTHORIZATION_TOO_SOON` inside honor period); honor period 3 days; after 29/30 days a new authorization is required (docstring) |
| `payments.void_payment` | `(authorization_id, *, pay_pal_request_id, prefer)` → `PaymentAuthorization` | `Error` 401/403/404/409/422 · `RawError` | cancel; `prefer="return=representation"` (body only returned then — docstring) |
| `payments.refund_captured_payment` | `(capture_id, *, pay_pal_request_id, prefer, body: RefundRequest\|Dict)` → `Refund` | `Error` 400/401/403/404/409/422 · `RawError` | refunds; id `f"{reference}-refund-{refund.public_id}"`; over-refund → live `422 REFUND_AMOUNT_EXCEEDED` |
| `payments.get_captured_payment` | `(capture_id)` → `CapturedPayment` | `Error` 401/403/404 · `RawError` | refresh a PENDING capture |
| `payments.get_refund` | `(refund_id)` → `Refund` | `Error` 401/403/404 · `RawError` | refresh a PENDING refund |
| `vault.create_payment_token` | `(body: PaymentTokenRequest\|Dict, *, pay_pal_request_id)` → `PaymentTokenResponse` | `Error` 400/403/404/422/500 · `RawError` | save card (raw card accepted directly — live 201); keys kept 3 h |
| `vault.delete_payment_token` | `(id)` → `None` — **use `with_raw_response`** | `Error` 400/403/500 · `RawError` | remove card; live `204` |
| `transaction_search.search_transactions` | `(start_date: str, end_date: str, *, fields="transaction_info", balance_affecting_records_only="Y", page_size=100, page=1, …)` → `SearchResponse` | Case B `RawError` | reconciliation; RFC 3339 with seconds; **range ≤ 31 days** (live 400 "Date range is greater than 31 days"); **page_size ≤ 500** (live 400); up to 3 h reporting lag; pass `balance_affecting_records_only="N"` so authorizations/voids appear |

### Request members set (required ✱ / everything else `Optional[T] = UNSET` — never `None`)

- `OrderRequest`: `intent` ✱ (`CheckoutPaymentIntent.AUTHORIZE`), `purchase_units` ✱ (`list[PurchaseUnitRequest]`), `payment_source` (`PaymentSource`).
- `PurchaseUnitRequest`: `amount` ✱ (`AmountWithBreakdown`: `currency_code` ✱ str, `value` ✱ str), `reference_id` (Oscar order number), `custom_id` (our reference — reconciliation key), `invoice_id` (`f"{reference}-{attempt}"`).
- `PaymentSource.card` → `CardRequest`: `name`, `number`, `expiry` (`YYYY-MM`), `security_code`, `billing_address` (`Address`: `country_code` ✱, `address_line_1`, `address_line_2`, `admin_area_2`, `admin_area_1`, `postal_code`), **or** `vault_id` (saved card; live 201).
- `CaptureRequest`: `amount` (`Money`: `currency_code` ✱, `value` ✱), `final_capture=True`, `invoice_id`.
- `ReauthorizeRequest`: `amount` (`Money`).
- `RefundRequest`: `amount` (`Money`) — always sent explicitly (full = remaining).
- `PaymentTokenRequest`: `payment_source` ✱ (`PaymentTokenRequestPaymentSource.card` → `PaymentTokenRequestCard`: `name`, `number`, `expiry`, `security_code`, `billing_address`), `customer` (`Customer.id` = existing PayPal customer id, omitted on the shopper's first card).

### Response members asserted (absence ⇒ outcome unknown, never success)

- `Order`: `id`, `status` (`OrderStatus`), `purchase_units[0].payments.authorizations[0]` (`AuthorizationWithAdditionalData`: `id`, `status`, `amount`, `expiration_time`, `create_time`), `payment_source.card` (`CardResponse`: `brand`, `last_digits`).
- `CapturedPayment`: `id`, `status`, `amount`, `seller_receivable_breakdown` (`gross_amount` ✱, `paypal_fee`, `net_amount`), `status_details.reason`.
- `PaymentAuthorization`: `id`, `status`, `expiration_time`, `create_time`.
- `Refund`: `id`, `status`, `amount`.
- `PaymentTokenResponse`: `id`, `customer.id`, `payment_source.card` (`CardPaymentTokenEntity`: `brand`, `last_digits`, `expiry`).
- `SearchResponse`: `transaction_details[].transaction_info` (`transaction_id`, `paypal_reference_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_amount`, `fee_amount`, `transaction_status`, `invoice_id`, `custom_field`), `total_pages`, `last_refreshed_datetime`.

### Status → outcome maps (enumerated; default arm = `unknown`, never failed/done)

- `OrderStatus` (create_order): `COMPLETED` → read the authorization; `PAYER_ACTION_REQUIRED` → **failed: 3-DS browser challenge not supported (per brief: stop, no approval round-trip)**; `CREATED`/`SAVED`/`APPROVED` → pending; `VOIDED` → failed; other → unknown.
- `AuthorizationStatus`: `CREATED` → done (authorized); `PENDING` → pending; `DENIED` → failed (declined); `VOIDED` → voided (not done); `CAPTURED`/`PARTIALLY_CAPTURED` → captured-by-someone (treated as done for capture reconciliation); other → unknown.
- `CaptureStatus`: `COMPLETED` → done; `PENDING` → pending (reason from `CaptureIncompleteReason`); `DECLINED`/`FAILED` → failed; `PARTIALLY_REFUNDED`/`REFUNDED` → done (capture happened, later refunded); other → unknown.
- `RefundStatus`: `COMPLETED` → done; `PENDING` → pending; `FAILED`/`CANCELLED` → failed; other → unknown.

### Failure → HTTP mapping (one ladder, `gateway.py`)

`OAuthProviderError` → 502 config; 401/403 → 502; 429 → 503; 400/422 `Error` → 422 with PayPal `issue`/`description` (card declined, validation); 404/409 → 409 (state conflict at PayPal); 5xx/unmapped → 502 `outcome_unknown` for writes; `httpx.ConnectError|ConnectTimeout|PoolTimeout|ProxyError` → 502 known-not-sent; other `httpx.RequestError` → 504 `outcome_unknown`; `ValidationError`/`ValueError` decoding a 2xx → 502 `outcome_unknown`. Unknown outcomes leave the stored operation `unknown`; the next identical API request re-sends under the **same** `PayPal-Request-Id` (PayPal returns the original) — never under a new one.

## Build order

1. `sandbox/requirements-paypal.txt`; settings (`PAYPAL_*`, app in `INSTALLED_APPS`); `urls.py` `path("api/", include(...))`.
2. `payments_api`: `apps.py`, `models.py` + migration, `money.py`, `gateway.py` (client + error ladder + typed wrappers), `services.py` (orders, payments, refunds, cards, reconciliation), `views.py`, `urls.py`.
3. Tests (`sandbox/apps/payments_api/tests/`, Django `TestCase`, stub transport per `python-testing`), `mypy --strict` on `gateway.py`/`money.py`.
4. Live verification on the sandbox via HTTP; guide.

## Assumptions & Blockers

- No blockers. Minor: refunds are shopper-scoped (the brief lists only fulfil/cancel/reconciliation as operator actions). Refund idempotency key comes from the `Idempotency-Key` header (required). Reconciliation lists every PayPal transaction in range — the shared sandbox account has thousands from other merchants' activity — unmatched ones are reported as `paypalOnly`.
- Brief correction: `oscar_import_catalogue` on the three `books.*.csv` files is what produces the 209 products (the `*.csv` glob also hits `range-products.csv`, which is what "fails"); without it there are 11 products and `orders.json` fails with an FK error.

## REQUIRED READING (loaded before implementation)

- MUST load `python-error-handling` — the ladder above, `OAuthProviderError` first, never/maybe-sent split. ✔ loaded
- MUST load `python-client-initialization` — sync client, lazy post-fork singleton, close obligation. ✔ loaded
- MUST load `python-configuration-resilience` — explicit base URL, no-retry, may-have-landed writes by reference. ✔ loaded
- MUST load `python-calling-endpoints` — `prefer` default narrows responses; status allow-list; `-> None` raw peer. ✔ loaded
- MUST load `python-models` — `UNSET` vs `None`, open enums, `isinstance(UnsetType)` narrowing, currency exponent. ✔ loaded
- MUST load `python-authentication` — `oauth2=` must be set; lazy token fetch. ✔ loaded
- MUST load `python-testing` — stub transport seam, token request first, both transport-failure kinds. ✔ loaded
