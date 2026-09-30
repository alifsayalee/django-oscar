# PayPal Server SDK integration plan — django-oscar sandbox

Scope: PayPal card payments (authorize → capture at fulfilment → void / refund), saved cards (vault),
and a reconciliation report, exposed as a JSON API in a new sandbox app `sandbox/apps/paypal_payments`.

## Repo survey (conventions to imitate)

| Concern | Pattern | Exemplar |
| --- | --- | --- |
| Sandbox-local app | module under `sandbox/apps/`, imported as `apps.<name>` (sandbox dir is on `sys.path`) | `sandbox/apps/sitemaps.py`, `sandbox/apps/user/` |
| URL wiring | plain `path(...)` entries in `sandbox/urls.py` outside `i18n_patterns` (like `admin/`, `sitemap.xml`) | `sandbox/urls.py` |
| Settings | env-driven via `django-environ` `env(...)` / `os.environ.get` | `sandbox/settings.py` |
| Oscar model access | `get_model('order', 'Order')`, `get_class('order.utils', 'OrderCreator')` | `sandbox/apps/sitemaps.py` |
| Orders | `OrderCreator.place_order(basket, total, shipping_method, shipping_charge, user=..., status=...)` from a real `Basket` priced by the partner strategy | `src/oscar/apps/order/utils.py`, `src/oscar/apps/checkout/mixins.py` |
| Payment records | Oscar `payment.Source` / `SourceType` / `Transaction` (allocate / debit / refund) and `payment.Bankcard` (masked number + `partner_reference`) | `src/oscar/apps/payment/abstract_models.py` |
| Auth | Django session login (`django.contrib.auth.login`), `request.user`; `is_staff` for operator actions | Oscar dashboard `staff_member_required` |
| Tests | pytest + pytest-django, bare `assert` | `tests/integration/payment/` |

Host is **Django under WSGI → sync**. Toolchain: `venv` (py 3.11) + pip, project installed `-e .[test]`,
SDK installed from `marketplace/plugins/paypal/sdk/python/` (non-editable). Tests: `venv/Scripts/python -m pytest`.
Type checker: none configured → `mypy --strict` on the SDK boundary module (`gateway.py`, `transport.py`, `errors.py`).

Baseline: `tests/integration/payment tests/integration/order tests/functional/checkout` run on the untouched tree.

## Environment / credentials

- `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENVIRONMENT`, `PAYPAL_CURRENCY`, `PAYPAL_BASE_URL` read in
  `sandbox/settings.py` via env (no values in files).
- Host selection (`base_url` omitted = SDK default `https://api-m.sandbox.paypal.com`, silently): we ALWAYS pass
  `base_url`. `PAYPAL_BASE_URL` set → used verbatim (token fetch moves with it — map: "tokens come from
  `/v1/oauth2/token` on the base URL"). Unset → explicit map `{"sandbox": "https://api-m.sandbox.paypal.com"}`
  (the SDK's one declared server, sdk-map *Servers & auth*). Any other environment without `PAYPAL_BASE_URL` →
  `ImproperlyConfigured` (the plugin declares no other host; not invented).
- Smoke (read-only, scratch dir): token fetch OK; `search_transactions` 30-day window → 200, 1629 items;
  `vault.get_payment_token("nonexistent")` → 404 whose body **fails to decode as `Error`** (`links[].rel`
  missing) → `pydantic.ValidationError` on an error status. Consequence below (error boundary).

## Contract sheet

### Client (sync)

- `PayPalServerSdkClient(*, base_url, timeout=30.0, retry_options, custom_http_client, oauth2=ClientCredentials(client_id, client_secret))`
  — keyword-only. Module-level lazy singleton (built on first use → after fork), closed via `atexit` (`close()`).
- `oauth2` MUST be set (omission = unauthenticated requests, no error). Failed token fetch → `ApiError` with
  `.error` `OAuthProviderError | RawError`, raised out of the operation call, in both response modes.
- Transport: `custom_http_client=LoggingTransport(HttpxClient(timeout=30.0))` (`HttpxClient(*, timeout, proxy_url, verify)`),
  which logs method, path, status, elapsed (never headers/bodies) and records the last response status per thread.
  `HttpClient` protocol = `send(request) -> HttpResponse` + `close()`.
- Retries: **kept at the default policy** (retries GET/HEAD/PUT/OPTIONS on 408/429/5xx/no response, max 3). Every
  write here is POST/DELETE → never resent by the SDK. No second retry layer.
- Whole-call limit: sync → none via SDK; `timeout` bounds each wait (30 s).
- Every keyword-only parameter has a real default — never pass defensive `None`s.

### Operations in scope (all `Error | RawError` unless noted; `Error` members `name`, `message`, `debug_id`, `details[] (issue, description)`)

| Op | Positional | Keyword-only used | Returns | Error arms |
| --- | --- | --- | --- | --- |
| `orders.create_order` | `body: OrderRequest` | `pay_pal_request_id` (mandatory for card/vault_id single-step; kept 6 h), `prefer="return=representation"` | `Order` | `Error` [400,401,422] · `RawError` |
| `orders.authorize_order` | `id_` | `pay_pal_request_id` (6 h), `prefer="return=representation"` | `OrderAuthorizeResponse` | `Error` [400,401,403,404,422,500] · `RawError` |
| `orders.get_order` | `id_` | — | `Order` | `Error` [401,404] · `RawError` |
| `payments.capture_authorized_payment` | `authorization_id` | `pay_pal_request_id` (45 d), `prefer="return=representation"`, `body: CaptureRequest` | `CapturedPayment` | `Error` [400,401,403,404,409,422] · `RawError` [500,…] |
| `payments.get_authorized_payment` | `authorization_id` | — | `PaymentAuthorization` | `Error` [401,403,404] · `RawError` |
| `payments.reauthorize_payment` | `authorization_id` | `pay_pal_request_id` (45 d), `prefer="return=representation"`, `body: ReauthorizeRequest` | `PaymentAuthorization` | `Error` [400,401,403,404,422] · `RawError` |
| `payments.void_payment` | `authorization_id` | `pay_pal_request_id`, `prefer="return=representation"` | `PaymentAuthorization` | `Error` [401,403,404,409,422] · `RawError` |
| `payments.refund_captured_payment` | `capture_id` | `pay_pal_request_id` (45 d), `prefer="return=representation"`, `body: RefundRequest` | `Refund` | `Error` [400,401,403,404,409,422] · `RawError` |
| `vault.create_payment_token` | `body: PaymentTokenRequest` | `pay_pal_request_id` (3 h) | `PaymentTokenResponse` | `Error` [400,403,404,422,500] · `RawError` |
| `vault.delete_payment_token` | `id_` | — | **`None`** (raw peer `ApiResult[None, …]`) | `Error` [400,403,500] · `RawError` |
| `vault.list_customer_payment_tokens` | `customer_id` | `page_size=5`, `page=1` | `CustomerVaultPaymentTokensResponse` | `Error` [400,403,500] · `RawError` |
| `transaction_search.search_transactions` | `start_date: str`, `end_date: str` (RFC 3339, seconds required, **max 31-day range**) | `fields="transaction_info"`, `balance_affecting_records_only` (default `"Y"`; we send `"N"` to include authorizations/voids), `page_size=100`, `page=1` | `SearchResponse` | **Case B: `RawError` only** |

`prefer` defaults to `"return=minimal"` on every write → we pass `"return=representation"` so nested payments,
breakdowns and card details come back.

### Models set (required vs `UNSET`; `Optional[T]` = `T | UnsetType`, never `None`)

- `OrderRequest`: `intent` (req, `CheckoutPaymentIntent.AUTHORIZE`), `purchase_units: list[PurchaseUnitRequest]` (req), `payment_source: PaymentSource` (opt).
- `PurchaseUnitRequest`: `amount: AmountWithBreakdown` (req: `currency_code: str`, `value: str`), `reference_id`, `custom_id`, `invoice_id`, `description` (opt).
- `PaymentSource.card: CardRequest` — `name`, `number`, `expiry` (`YYYY-MM`), `security_code`, `billing_address: Address` (`country_code` req), `vault_id` (all opt).
- `CaptureRequest`: `amount: Money`, `invoice_id`, `final_capture: bool` (opt). `ReauthorizeRequest`: `amount: Money` (opt).
- `RefundRequest`: `amount: Money` (omit → full refund, per docstring), `custom_id`, `invoice_id`, `note_to_payer` (opt).
- `PaymentTokenRequest`: `payment_source: PaymentTokenRequestPaymentSource` (req) with `.card: PaymentTokenRequestCard` (`name`, `number`, `expiry`, `security_code`, `billing_address`); `customer: Customer` (`id`) opt.
- Responses: `Order.status` / `.purchase_units[].payments.authorizations[]` (`AuthorizationWithAdditionalData`: `id`, `status`, `amount`, `expiration_time`, `create_time`) / `.captures[]` (`OrdersCapture`) / `.refunds[]`; `Order.payment_source.card: CardResponse` (`last_digits`, `brand`, `expiry`).
  `CapturedPayment`: `id`, `status`, `amount`, `seller_receivable_breakdown: SellerReceivableBreakdown` (`gross_amount` **required**, `paypal_fee`, `net_amount`), `create_time`.
  `Refund`: `id`, `status`, `amount`, `custom_id`, `create_time`. `PaymentAuthorization`: `id`, `status`, `amount`, `expiration_time`, `create_time`.
  `PaymentTokenResponse`: `id`, `customer.id`, `payment_source.card: CardPaymentTokenEntity` (`last_digits`, `brand`, `expiry`).
  `SearchResponse`: `transaction_details[].transaction_info` (`transaction_id`, `paypal_reference_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_amount`, `fee_amount`, `transaction_status`, `invoice_id`, `custom_field`), `page`, `total_pages`, `total_items`, `last_refreshed_datetime`.
- Dates in these models are plain `str` (RFC 3339) — parsed with `datetime.fromisoformat`.
- Money is `str` → built with `Decimal.quantize` per currency exponent (ISO 4217 table from `python-models`); compared as `Decimal`.
- Assert-after-call members (UNSET is not an exception): create/authorize → order `id`, authorization `id`/`status`/`amount`; capture → `id`/`status`/`amount`; refund → `id`/`status`; vault → `id`. Missing = outcome unknown.
- Enums (open, may arrive as `str`): `OrderStatus` CREATED·SAVED·APPROVED·VOIDED·COMPLETED·PAYER_ACTION_REQUIRED; `AuthorizationStatus` CREATED·CAPTURED·DENIED·PARTIALLY_CAPTURED·VOIDED·PENDING; `CaptureStatus` COMPLETED·DECLINED·PARTIALLY_REFUNDED·PENDING·REFUNDED·FAILED; `RefundStatus` CANCELLED·FAILED·PENDING·COMPLETED.

### What each status means (the status of what the call asked for)

| Call | Status read | Done | Not done yet | Failed | anything else |
| --- | --- | --- | --- | --- | --- |
| create_order (AUTHORIZE, card) | `Order.status` | APPROVED (→ authorize) / COMPLETED with an authorization | CREATED, SAVED | VOIDED; PAYER_ACTION_REQUIRED = 3-D Secure browser challenge → **stop and report**, no approval round-trip | not done → unknown |
| authorize_order | `purchase_units[0].payments.authorizations[0].status` | CREATED (held) | PENDING | DENIED, VOIDED | unknown |
| reauthorize_payment | `PaymentAuthorization.status` | CREATED | PENDING | DENIED, VOIDED | unknown |
| capture_authorized_payment | `CapturedPayment.status` | COMPLETED | PENDING | DECLINED, FAILED | unknown |
| void_payment | `PaymentAuthorization.status` | VOIDED | — | anything else | unknown |
| refund_captured_payment | `Refund.status` | COMPLETED | PENDING | FAILED, CANCELLED | unknown |
| create_payment_token | no status field → `id` present + `payment_source.card` | id present | — | — | missing id → unknown |
| delete_payment_token | `-> None`; 2xx | 2xx; 404 = already gone | — | 4xx | 5xx/transport → unknown |

Amount check: authorization amount and capture amount compared to order total as `Decimal`, currency compared
case-insensitively; mismatch → payment `NEEDS_REVIEW`, returned (not raised) so the record survives.

Stale authorization (reauthorize docstring): 3-day honor period; reauthorize allowed within the 29-day
authorization period; ≥30 days → must create a new authorization. At fulfil: authorization older than the honor
period (by PayPal's `create_time`) → reauthorize first, then capture the new authorization id. Past
`expiration_time`, or reauthorize refused → 409 with an operator-actionable message (cancel + ask the shopper to pay again).

### Error boundary (one place: `errors.py` → `ProviderError(status_code, message, outcome_unknown, provider_status)`)

Order of arms: `ApiError` with `OAuthProviderError` → 502 config fault (nothing sent) · `ApiError` 401/403 → 502 ·
429 → 503 · 4xx with `Error` → same 4xx, message from `Error.name`/`details[0].issue` · other 4xx → same 4xx
generic · 5xx → 502 `outcome_unknown=True` · `ValidationError`/`ValueError` → triaged by the status the
LoggingTransport recorded: non-2xx → known rejection (4xx passthrough, 5xx unknown), 2xx → 502 outcome unknown ·
`httpx.ConnectError|ConnectTimeout|PoolTimeout|ProxyError` → 502 never sent (known) · `httpx.RequestError` → 504
outcome unknown. `str(e)` never surfaced; bodies never logged; card data never logged.

## DUPLICATE CLAIMS

| Write | Where the claim is stored | What rejects the second one | Where that rejection is caught | Where in the code |
| --- | --- | --- | --- | --- |
| Authorize (create_order + authorize_order) | `PayPalPayment.status` row in the sandbox DB (AWAITING_PAYMENT/DECLINED → AUTHORIZING) + `attempt` counter | single-statement conditional `UPDATE … WHERE status IN (AWAITING_PAYMENT, DECLINED)` returns 0 rows → `ClaimRejected` | `api` decorator's `except services.PaymentError` in `views.py` (409 with current payment state) | `services._claim` then `gateway.create_order` (→ `gateway.authorize_order` when PayPal answers APPROVED), in `services.pay_order` |
| Capture (fulfil) | `PayPalPayment.status` AUTHORIZED → CAPTURING | conditional `UPDATE` 0 rows → `ClaimRejected` | `api` decorator's `except services.PaymentError` | `services._claim` then `gateway.capture` (after `gateway.reauthorize` in `services._renew_authorization` when stale), in `services.fulfil_order` |
| Void (cancel) | `PayPalPayment.status` AUTHORIZED/AWAITING_PAYMENT/DECLINED/EXPIRED → VOIDING | conditional `UPDATE` 0 rows → `ClaimRejected` | `api` decorator's `except services.PaymentError` | `services._claim` then `gateway.void`, in `services.cancel_order` |
| Refund | `PayPalRefund` row, `UniqueConstraint(payment, idempotency_key)`; amount reserved by conditional `UPDATE payment SET refund_reserved = refund_reserved + x WHERE refund_reserved <= captured_amount - x AND status IN (CAPTURED, PARTIALLY_REFUNDED)` | `IntegrityError` on the insert (same key) · 0-row update (over-refund) → `PaymentError` 409 `REFUND_EXCEEDS_CAPTURE` inside the same `transaction.atomic` (rolls the insert back) | `except IntegrityError as claimed` in `services.refund_order` (returns the original refund; 409 `IDEMPOTENCY_KEY_REUSED` if the amount differs) | `PayPalRefund.objects.create` + conditional reservation `UPDATE` in `services.refund_order`, then `gateway.refund` |
| Save card | `CardSaveClaim` row, `UniqueConstraint(user, idempotency_key)` (key from `Idempotency-Key` header; absent → fresh key, no dedup) | `IntegrityError` on insert (inside a savepoint) | `except IntegrityError as claimed` in `services.save_card` (returns the card saved under that key, or 409 in progress) | `CardSaveClaim.objects.create` in `services.save_card`, then `gateway.vault_card` |
| Delete card | `PayPalVaultCard.state` ACTIVE → DELETING | none needed: a second DELETE re-sends a naturally idempotent delete (404 = already gone) | n/a | `PayPalVaultCard` `update(state=DELETING)` in `services.delete_card`, then `gateway.delete_vaulted_card` |

Provider keys beside each claim: `PayPal-Request-Id` = `<payment.request_ns>-<op>-<attempt>` (uuid namespace stored
on the row → stable across resends of that attempt, unique across DB resets). Claim released (status restored,
reservation returned, claim row marked failed) when PayPal refuses.

## UNKNOWN OUTCOMES

| Write | The operation you re-read with | The reference you search by | Where in the code | The test that leaves the outcome unknown |
| --- | --- | --- | --- | --- |
| create_order | re-send `create_order` with the **same** `PayPal-Request-Id` (docstring: server stores keys 6 h) — body is rebuilt from the saved card or, for a one-off card, only within the same request | `PayPal-Request-Id` | `services._create_paypal_order` except → immediate same-id re-send; still unknown → `services.pay_order` saves `UNKNOWN`/`create`; the next pay re-claims without a new attempt and re-sends the same id | `test_create_order_read_timeout_is_resent_under_the_same_request_id`, `test_create_order_unknown_twice_is_left_unknown_then_repaid_with_same_id` |
| authorize_order | `orders.get_order(paypal_order_id)` → authorizations | PayPal order id | `services.pay_order` except → `services._settle_or_raise` → `services.settle_payment` (also on the next action and `paypal_settle`) | `test_authorize_timeout_settled_by_get_order` |
| capture | `orders.get_order(paypal_order_id)` → captures (`get_captured_payment` for a PENDING capture) | PayPal order id / capture id | `services.fulfil_order` except → `services._settle_or_raise` → `services.settle_payment`; left `UNKNOWN` → settled first by the next fulfil / `paypal_settle` | `test_capture_5xx_is_unknown_then_settled`, `test_capture_unknown_and_unreadable_stays_unknown`, `test_capture_timeout_where_paypal_shows_no_capture_is_a_known_failure`, `test_truncated_success_body_is_an_unknown_outcome` |
| reauthorize | `orders.get_order` → newest authorization | PayPal order id | `services._renew_authorization` except → `services._try_settle` → `services.settle_payment`, then re-claims and captures | `test_reauthorize_timeout_settled_then_captured` |
| void | `payments.get_authorized_payment(authorization_id)` | authorization id | `services.cancel_order` except → `services._settle_or_raise` → `services.settle_payment` | `test_void_timeout_settled` |
| refund | `orders.get_order` → `refunds[]` matched by `custom_id` = our refund uuid | refund `custom_id` | `services.refund_order` except → `services._try_settle_refund` → `services.settle_refund` (also `paypal_settle`) | `test_refund_read_timeout_settled_by_custom_id`, `test_refund_timeout_not_found_at_paypal_is_released` |
| vault create | same `PayPal-Request-Id` re-send within the request (card still in memory); after that `vault.list_customer_payment_tokens(customer_id)` matched by last digits + expiry | request id / customer id | `services.save_card` except → same-id re-send; still unknown → `CardSaveClaim.state=UNKNOWN`; `services.settle_card_save` on the same key's retry / `paypal_settle` | `test_vault_timeout_unknown`, `test_vault_timeout_settled_by_listing_the_customers_cards` |
| vault delete | re-send DELETE (404 = gone) | token id | `services.delete_card` except leaves `PayPalVaultCard.state=DELETING` (hidden, unusable); the next DELETE / `paypal_settle` re-drives | `test_delete_card_timeout_keeps_card_hidden` |

A management command `paypal_settle` re-drives every row left `UNKNOWN` / `DELETING`.

## Implementation notes (post-build)

- The sandbox runs with `ATOMIC_REQUESTS=True`; the API views are `non_atomic_requests` so each claim is
  committed before PayPal is called and PayPal's answer is recorded even if the request later fails.
- Live sandbox observation: `create_order` with a card and intent AUTHORIZE answers `COMPLETED` with the
  authorization already in `purchase_units[0].payments.authorizations` — no separate `authorize_order` call.
  The APPROVED → `authorize_order` path is kept for other answers.
- The sandbox merchant account is shared with other stores (colliding `custom_id`s such as order numbers),
  so `custom_id` = the payment's `request_ns` UUID and `invoice_id` = `<order>-<ns8>-<attempt>`.
- Residual: a first-ever card save (no PayPal customer yet) whose outcome stays unknown after the same-id
  re-send cannot be looked up later (no customer id to list by); its claim stays `UNKNOWN` for review.

## Assumptions & Blockers

- Minor: orders are placed from a real Oscar `Basket` for the caller (stock allocated, free shipping, no shipping
  address — digital-style order) with `currency = PAYPAL_CURRENCY`; prices are the stock-record prices as-is.
- Minor: sandbox status pipeline gains `'Awaiting payment'` for API orders; regular checkout still starts at `Pending`.
- Minor: saved-card cardholder name is not stored locally (Bankcard `name` left blank) — only brand, last 4, expiry.
- Minor: a 3-D Secure challenge (`PAYER_ACTION_REQUIRED`) is reported as an error, never round-tripped (task rule).
- No blockers.

## REQUIRED READING

- Client construction, lifetime, transport ownership — MUST load `python-client-initialization` (loaded)
- Credentials, lazy token fetch, `OAuthProviderError` — MUST load `python-authentication` (loaded)
- Call shapes, `prefer` default narrowing, `-> None` delete — MUST load `python-calling-endpoints` (loaded)
- UNSET, open enums, money as `str`/Decimal — MUST load `python-models` (loaded)
- Error boundary incl. ValidationError on error bodies, httpx split — MUST load `python-error-handling` (loaded)
- Base URL, retries, duplicate claims, unknown outcomes, reconciliation window — MUST load `python-configuration-resilience` (loaded)
- Stub transport tests, both transport failure kinds — MUST load `python-testing` (loaded)
