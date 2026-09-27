# PayPal Server SDK — integration plan & contract sheet

Scope: PayPal card payments (authorize → capture → refund / void) and saved cards (vault) for the
django-oscar **sandbox** site, exposed as a JSON API under `/api/` by a new Django app
`sandbox/apps/paypal_api/`.

## SDK identity (drift from the skill snapshot — recorded, not patched around)

The `python-getting-started` skill describes distribution `pay-pal-server-sdk` / import root
`pay_pal_server_sdk` / client `PayPalServerSdkClient`. The SDK actually published on
`context-plugins/paypal-python-sdk@main` (version `2.29`, same spec version) is:

| Fact | Value (from `sdk-map.md` and `pyproject.toml` of the clone, confirmed by import) |
| --- | --- |
| Distribution | `paypal` — installed with `pip install "paypal @ git+https://github.com/context-plugins/paypal-python-sdk.git@main"` |
| Import root | `paypal` |
| Sync client | `PaypalClient` (alias `Client`) — **used** |
| Async client | `AsyncPaypalClient` — not used |
| Core runtime | `paypal.core` (`ApiError`, `RawError`, `Success`, `Failure`, `ClientCredentials`, `HttpxClient`, `HttpRequest`, `HttpResponse`, `OAuthProviderError`, `UNSET`, `UnsetType`) |
| Models / enums / errors | `paypal.models` / `paypal.models.enums` / `paypal.errors` |
| Base URL | one server, one environment: `base_url: str \| None`, default `https://api-m.sandbox.paypal.com`; token endpoint `/v1/oauth2/token` follows `base_url` |

## Decisions

| Decision | Choice | where in the code |
| --- | --- | --- |
| Sync vs async | **Sync** `PaypalClient` — Django under WSGI (`sandbox/wsgi.py`, `runserver`), all views are `def`. | `gateway._build_client` (constructs `PaypalClient`); every view in `views.py` is a plain `def` |
| Client lifetime | One module-level client built lazily on first use (post-fork safe), guarded by a lock, closed via `atexit`. | `gateway.get_client` (lock + `atexit.register(_client.close)`), `gateway.set_client` |
| Host selection | `PAYPAL_BASE_URL` if set → used verbatim (API + token). Else `PAYPAL_ENVIRONMENT`: `sandbox` → `https://api-m.sandbox.paypal.com` (the SDK map's only declared server); any other value → `ImproperlyConfigured` naming `PAYPAL_BASE_URL` (the plugin declares no other host, so none is invented). Always passed explicitly. | `gateway.base_url`, passed as `base_url=` in `gateway._build_client` |
| Credentials | `oauth2=ClientCredentials(client_id=settings.PAYPAL_CLIENT_ID, client_secret=settings.PAYPAL_CLIENT_SECRET)`; missing values → `ImproperlyConfigured` before the first call (an omitted `oauth2` silently sends unauthenticated requests). | `gateway._build_client` |
| Timeout | `timeout` set on our own `HttpxClient(timeout=20.0)` transport (the client's `timeout=` does not reach a custom transport). | `gateway._build_client` (`HttpxClient(timeout=settings.PAYPAL_TIMEOUT_SECONDS)`) |
| Transport wrapper | `LoggingTransport` logs method, path, status, duration (never headers/bodies) and records the last response status in a `contextvars.ContextVar`, so a decode failure can be classified 2xx (unknown) vs non-2xx (rejected, detail lost). | `gateway.LoggingTransport.send`, `gateway.last_status`; read by `errors.call_paypal` and the `ValueError` arm of `safe_write.safe_write` |
| Retries | **None on writes** (the safe write's lookup settles unknowns). Reads (`get_*`, `search_transactions`) retried up to 3 attempts on connect errors / 429 / 5xx with short backoff. | `gateway.read_with_retry` (wrapped by `errors.call_paypal` at every read) |
| `Prefer` header | Every write sends `prefer="return=representation"`. Smoke-verified: `void_payment` with the default `return=minimal` returns an empty 2xx body the SDK cannot decode (`ValueError`). | `payments.pay_order`, `payments._renew_if_stale`, `payments.fulfil_order`, `payments.cancel_order`, `payments.refund_order` (`prefer="return=representation"`) |
| Claim store | The project's own database (SQLite by default, `ATOMIC_REQUESTS=True`): model `PayPalOperation` with a **unique** `ref` column; `try_claim` = INSERT, `IntegrityError` → lost. API views are `non_atomic_requests` so a claim commits **before** the provider call. | `models.PayPalOperation` (`ref` unique), `safe_write.try_claim`; `views.api` applies `transaction.non_atomic_requests` |
| Money | Integer minor units internally; ISO-4217 exponent table; `Decimal` quantize for the wire string; echoed amounts compared as `Decimal`. | `money.to_minor`, `money.wire`, `money.from_minor`; step 4 of `safe_write.safe_write` |
| Order model | Oscar `order.Order`/`Line` built through Oscar's `Basket` + `OrderCreator.place_order`, status `Awaiting payment` (added to the sandbox pipeline). Price numbers from the catalogue strategy; currency from `PAYPAL_CURRENCY`. | `orders.place_order` (Basket + `OrderCreator.place_order`, status `AWAITING_PAYMENT`); `'Awaiting payment'` in `OSCAR_ORDER_STATUS_PIPELINE` (`sandbox/settings.py`) |
| Payment records | Oscar `payment.Source` (allocated/debited/refunded) + `payment.Transaction` (Authorise/Debit/Refund/Void) per PayPal event; PayPal-specific state (ids, statuses, fee/net, refund reservation) in `PayPalPayment` (1:1 order). | `models.PayPalPayment`; `payments._source`, `payments._record_authorization`, `payments._record_capture`, void/refund blocks of `payments.cancel_order` / `payments.refund_order` |
| Saved cards | Oscar `payment.Bankcard` (`number` masked `XXXX-XXXX-XXXX-1111`, `card_type` = brand, `expiry_date`, `partner_reference` = PayPal vault token id). PayPal customer id per user in `PayPalCustomer`. Full PAN/CVC never stored or logged. | `cards.save_card`, `cards.describe_card`, `models.PayPalCustomer`; `payments.parse_card` (validation only, never persisted) |
| References | `ref = f"{PAYPAL_REFERENCE_PREFIX}:…"`; prefix from settings, default derived from a hash of `SECRET_KEY` + DB name (unique per install, stable across restarts). Sent as `PayPal-Request-Id`; pay attempts also set `invoice_id`/`custom_id`. | `gateway.reference_prefix`, `payments.ref`, `payments.key_digest`; `invoice_id`/`custom_id` in `payments.pay_order` |
| Staff | fulfil / cancel / reconciliation require `request.user.is_staff`; everything else (incl. refunds, per the task text) acts only on the caller's own orders/cards. | `views.api(staff=True)` on `views.order_fulfil`, `views.order_cancel`, `views.reconciliation_report`; `orders.get_order(number, user=...)` and `cards.get_card` scope the rest |
| Auth | Django session (Oscar login form at `/en-gb/accounts/login/`); CSRF stays enforced (`X-CSRFToken`). Unauthenticated → 401 JSON. | `views.api` (`request.user.is_authenticated` → 401) |

## Contract sheet (every fact from the SDK map, the model modules, or the sandbox smoke)

Invariants for every operation below: parsed call raises `ApiError` (unparametrized catch, narrow
`e.error`); raw peer via `client.<group>.with_raw_response.<op>` returns `Success | Failure`; every
parameter after `*` is keyword-only **with a real default** (no defensive `None`s); trailing
`request_options: RequestOptionsOrDict | None`; **the SDK performs no retries**; a decode failure
raises `ValueError`/`pydantic.ValidationError` in **both** modes; a failed token fetch raises
`ApiError` whose `.error` is `OAuthProviderError | RawError`; `httpx` transport errors arrive
unwrapped. Models: `Optional[T]` = `T | UnsetType` (never pass `None`); enums are open (`…OrStr`).
All error unions in scope except search are `Error | RawError` (`Error`: `name`, `message`,
`debug_id` required, `details: Optional[list[ErrorDetails]]` with `issue` required, `description`).
Smoke: a 404 error body failed to decode as `Error` (`ValidationError` on `links[].rel`) — the
non-2xx decode-failure path is real and must be classified as a rejection, not an unknown.

| # | Operation (accessor) | Signature (positional \| after `*`) | Returns | Error union | Members asserted / statuses |
| --- | --- | --- | --- | --- | --- |
| 1 | `client.orders.create_order` — `POST /v2/checkout/orders` | `body: OrderRequest\|OrderRequestDict` \| `pay_pal_request_id`, `prefer="return=minimal"`, … | `Order` | `CreateOrderErrorBody` = `Error` [400,401,422] \| `RawError` | Body: `intent` (**required**, `CheckoutPaymentIntent.AUTHORIZE`), `purchase_units` (**required** `list[PurchaseUnitRequest]`: `amount: AmountWithBreakdown` **required** {`currency_code`, `value`} ; `invoice_id`, `custom_id` Optional), `payment_source: PaymentSource` → `card: CardRequest` {`name`, `number`, `expiry` `YYYY-MM`, `security_code`, `billing_address: Address` (`country_code` required; `address_line_1`, `address_line_2`, `admin_area_2`, `admin_area_1`, `postal_code`), `vault_id`}. **Smoke: with a card source + AUTHORIZE the create alone authorizes** (order `COMPLETED`, `purchase_units[0].payments.authorizations[0].status == CREATED`); `PayPal-Request-Id` is "mandatory for single-step create order calls", keys kept 6 h. **Smoke: a repeat under the same key is refused (422 `TRANSACTION_REFUSED`), even with an identical body** — a resend is not a lookup. Assert: `id`, `status`, `purchase_units[0].payments.authorizations[0]` {`id`, `status`, `amount`, `create_time`, `expiration_time`}, `payment_source.card` {`brand`, `last_digits`}. `OrderStatus`: `COMPLETED` → read the authorization; `CREATED`/`SAVED`/`APPROVED` → pending; `PAYER_ACTION_REQUIRED` → pending (3-D Secure challenge; never observed); `VOIDED` → failed; unlisted/absent → unknown. |
| 2 | `client.orders.get_order` — `GET /v2/checkout/orders/{id}` | `id` \| `fields`, … | `Order` | `GetOrderErrorBody` = `Error` [401,404] \| `RawError` | `purchase_units[0].payments.captures[]` (`OrdersCapture`: `id`, `status`, `amount`, `seller_receivable_breakdown`, `create_time`). |
| 3 | `client.payments.get_authorized_payment` — `GET /v2/payments/authorizations/{authorization_id}` | `authorization_id` \| … | `PaymentAuthorization` | `Error` [401,403,404] \| `RawError` [500, other] | `id`, `status`, `amount`, `create_time`, `expiration_time`, `supplementary_data.related_ids.order_id`. |
| 4 | `client.payments.reauthorize_payment` — `POST …/authorizations/{authorization_id}/reauthorize` | `authorization_id` \| `pay_pal_request_id` (keys 45 days), `prefer`, `body: ReauthorizeRequest` {`amount: Money`} | `PaymentAuthorization` | `Error` [400,401,403,404,422] \| `RawError` | Docstring: after the 3-day honor period, "from 4 to 29 days"; 30 days after the original → a new authorization is required. **Smoke: 422 `REAUTHORIZATION_TOO_SOON` before day 4.** Assert `id`, `status`, `amount`, `create_time`, `expiration_time`. |
| 5 | `client.payments.capture_authorized_payment` — `POST …/authorizations/{authorization_id}/capture` | `authorization_id` \| `pay_pal_request_id` (45 days), `prefer`, `body: CaptureRequest` {`amount: Money`, `final_capture: bool`} | `CapturedPayment` | `Error` [400,401,403,404,409,422] \| `RawError` [500, other] | Assert `id`, `status`, `amount`, `seller_receivable_breakdown` {`gross_amount` (**required**), `paypal_fee`, `net_amount`}, `create_time`. Smoke: fee `0.81`, net `11.53` on `12.34`. |
| 6 | `client.payments.void_payment` — `POST …/authorizations/{authorization_id}/void` | `authorization_id` \| `pay_pal_request_id` (45 days), `prefer` | `PaymentAuthorization` | `Error` [401,403,404,409,422] \| `RawError` | **Must send `prefer="return=representation"`** (minimal → empty body → `ValueError`). Smoke: repeat under same key → `VOIDED` again (idempotent). Assert `id`, `status`, `update_time`. |
| 7 | `client.payments.refund_captured_payment` — `POST /v2/payments/captures/{capture_id}/refund` | `capture_id` \| `pay_pal_request_id` (45 days), `prefer`, `body: RefundRequest` {`amount: Money`, `note_to_payer`} | `Refund` | `Error` [400,401,403,404,409,422] \| `RawError` | **Smoke: repeat under the same key returns the same refund id** (de-duplicated). Assert `id`, `status`, `amount`, `create_time`. |
| 8 | `client.payments.get_captured_payment` — `GET /v2/payments/captures/{capture_id}` | `capture_id` \| … | `CapturedPayment` | `Error` [401,403,404] \| `RawError` | Refresh a pending capture. |
| 9 | `client.vault.create_payment_token` — `POST /v3/vault/payment-tokens` | `body: PaymentTokenRequest` \| `pay_pal_request_id` (keys 3 h) | `PaymentTokenResponse` | `Error` [400,403,404,422,500] \| `RawError` | Body: `payment_source: PaymentTokenRequestPaymentSource` (**required**) → `card: PaymentTokenRequestCard` {`name`, `number`, `expiry`, `security_code`, `billing_address`}; `customer: Customer` {`id`} Optional (PayPal generates one when absent; smoke: passing it back reuses the customer). **Smoke: same key → same token id.** Assert `id`, `customer.id`, `payment_source.card` {`brand`, `last_digits`, `expiry`, `verification_status` (`CardVerificationStatus`: `VERIFIED`, `FAILED`; smoke: `UNSET`)}. **No status member on this response.** |
| 10 | `client.vault.delete_payment_token` — `DELETE /v3/vault/payment-tokens/{id}` | `id` | **`None`** (raw: `ApiResult[None, …]`) | `Error` [400,403,500] \| `RawError` | Smoke: 204, and a repeat → 204 (idempotent). Called through `with_raw_response` so the status is observable. |
| 11 | `client.vault.get_payment_token` | `id` | `PaymentTokenResponse` | `Error` [403,404,422,500] \| `RawError` | **Smoke: a deleted token answers 204 with an empty body → `ValueError`.** Not used as a lookup. |
| 12 | `client.transaction_search.search_transactions` — `GET /v1/reporting/transactions` | `start_date: str`, `end_date: str` (RFC 3339, seconds required) \| `fields="transaction_info"`, `balance_affecting_records_only="Y"`, `page_size=100`, `page=1`, … | `SearchResponse` {`transaction_details`, `page`, `total_pages`, `total_items`, `last_refreshed_datetime`} | **Case B: `RawError` only** | Range **> 31 days → 400** (smoke). Up to 3 h reporting lag (docstring; smoke: `last_refreshed_datetime` ≈ 2 h behind). `TransactionDetails.transaction_info: TransactionInformation` {`transaction_id`, `paypal_reference_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_amount`, `fee_amount`, `transaction_status` (D/P/S/V), `invoice_id`, `custom_field`}. Send `balance_affecting_records_only="N"` so authorizations appear. Smoke: the same `transaction_id` can appear twice (P then S). |

Enums (`paypal.models.enums`), every member with its outcome:

| Enum | done | pending | failed | anything else |
| --- | --- | --- | --- | --- |
| `AuthorizationStatus` for the **authorize** step (create_order, reauthorize) | `CREATED` | `PENDING` | `DENIED`, `VOIDED` | `CAPTURED`, `PARTIALLY_CAPTURED` (not a hold any more — operator) and unlisted → `unknown` |
| `AuthorizationStatus` for the **void** step | `VOIDED`, `DENIED` (no funds held) | — | `CAPTURED`, `PARTIALLY_CAPTURED` (too late) | `CREATED`, `PENDING`, unlisted → `unknown` |
| `CaptureStatus` for the **capture** step | `COMPLETED` | `PENDING` | `DECLINED`, `FAILED`, `REFUNDED` (undone) | `PARTIALLY_REFUNDED`, unlisted → `unknown` |
| `RefundStatus` for the **refund** step | `COMPLETED` | `PENDING` | `FAILED`, `CANCELLED` | unlisted → `unknown` |
| `OrderStatus` (create_order envelope) | `COMPLETED` → decided by the authorization | `CREATED`, `SAVED`, `APPROVED`, `PAYER_ACTION_REQUIRED` | `VOIDED` | unlisted/absent → `unknown` |
| Vault token (no status member) | `id` **and** `payment_source.card` present, `verification_status` absent or `VERIFIED` | — | `verification_status == FAILED` | `id` or card missing → `unknown` |
| Vault delete (`None` return) | `Success` (2xx) | — | — | anything else via the error ladder |

## OPERATION OUTCOMES

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/orders/{id}/pay` → `orders.create_order` (single-step authorize) | `Order.status` + `purchase_units[0].payments.authorizations[0].status` | done: order `COMPLETED` + auth `CREATED` → 200 `paymentStatus=authorized`. pending: auth `PENDING`; order `CREATED`/`SAVED`/`APPROVED`/`PAYER_ACTION_REQUIRED` → 202 `authorization_pending`/`payer_action_required`. failed: auth `DENIED`/`VOIDED`, order `VOIDED` → 409 `authorization_declined` (a new `/pay` may be attempted). unknown: auth `CAPTURED`/`PARTIALLY_CAPTURED`, unlisted, absent → 504 `outcome_unknown`. Echoed amount ≠ order total → 409 `needs_review`. | `safe_write.order_outcome` + `safe_write.authorization_outcome`, read by `payments._read_order`; driven by `payments.pay_order`; answered by `views.order_pay` → `views.answer` |
| `POST /api/orders/{id}/fulfil` → `payments.reauthorize_payment` (only when stale) | `PaymentAuthorization.status` | done `CREATED` → continue to capture. pending `PENDING` → 202. failed `DENIED`/`VOIDED` or 4xx refusal → 409 `authorization_not_renewable` with PayPal's issue and the operator action. unknown → 504. | `payments._renew_if_stale` (`safe_write.authorization_outcome`, `payments._read_authorization`) |
| `POST /api/orders/{id}/fulfil` → `payments.capture_authorized_payment` | `CapturedPayment.status` | done `COMPLETED` → 200 with captured amount, fee, net; order → `Complete`. pending `PENDING` → 202 `capture_pending` (a later `/fulfil` refreshes it). failed `DECLINED`/`FAILED`/`REFUNDED` → 409 `capture_failed`. unknown `PARTIALLY_REFUNDED`/unlisted/absent → 504. Amount ≠ total → 409 `needs_review`. | `safe_write.capture_outcome`, `payments._read_capture`, `payments._refresh_pending`; `payments.fulfil_order` → `views.order_fulfil` → `views.answer` |
| `POST /api/orders/{id}/cancel` → `payments.void_payment` | `PaymentAuthorization.status` (void mapper) | done `VOIDED`/`DENIED` → 200 order `Cancelled`. failed `CAPTURED`/`PARTIALLY_CAPTURED` → 409 `already_captured`. unknown `CREATED`/`PENDING`/unlisted → 504 (a repeat re-checks). | `safe_write.void_outcome`; `payments.cancel_order` → `views.order_cancel` → `views.answer` |
| `POST /api/orders/{id}/refunds` → `payments.refund_captured_payment` | `Refund.status` | done `COMPLETED` → 201 `refundId`. pending `PENDING` → 202 `refundId`. failed `FAILED`/`CANCELLED` → 409 (reservation released). unknown → 504 (reservation kept). Amount ≠ requested → 409 `needs_review`. | `safe_write.refund_outcome`, `payments._read_refund`, `payments._refresh_pending`; `payments.refund_order` → `views.order_refunds` → `views.answer` |
| `POST /api/payment-methods` → `vault.create_payment_token` | no status member: `id`, `payment_source.card`, `card.verification_status` | done: id + card present, verification absent/`VERIFIED` → 201 `paymentMethodId`. failed: `FAILED` → 409 `card_verification_failed`. unknown: id or card missing → 504. | `safe_write.vault_outcome` (status built as `("TOKENIZED"\|"INCOMPLETE", verification)` by `cards._read_token`); `cards.save_card` → `views.payment_methods` → `views.answer` |
| `DELETE /api/payment-methods/{id}` → `vault.delete_payment_token` | HTTP status via `with_raw_response` (`Success`) | done: 2xx → 204, card removed. Error ladder otherwise; unknown → 504 (card stays hidden and unusable, a repeat DELETE resends). | `safe_write.delete_outcome`; `cards.delete_card` (`with_raw_response.delete_payment_token(...).unwrap()`) → `views.payment_method_detail` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| create_order (pay) | `PayPalOperation` row, `ref = {prefix}:order:{number}:authorize:{attempt}` (attempt advances only after a definitive failure / abandoned challenge) | UNIQUE constraint on `PayPalOperation.ref` | `IntegrityError` in `try_claim` | `safe_write.try_claim` (INSERT; `IntegrityError` → lost), called from `safe_write.safe_write`; attempt chosen in `payments.pay_order` |
| reauthorize_payment | `PayPalOperation`, `ref = {prefix}:order:{number}:reauthorize:{authorization_id}` | UNIQUE `ref`; plus the payment state compare-and-set `authorized → capturing` (one fulfil/cancel wins) | `IntegrityError` in `try_claim`; zero-row UPDATE in `begin_transition` | `safe_write.try_claim` via `payments._renew_if_stale`; `payments._transition` in `payments.fulfil_order` |
| capture_authorized_payment | `PayPalOperation`, `ref = {prefix}:order:{number}:capture` | UNIQUE `ref` + state compare-and-set | as above | `safe_write.try_claim` via `payments.fulfil_order`; `payments._transition` (AUTHORIZED → CAPTURING) |
| void_payment | `PayPalOperation`, `ref = {prefix}:order:{number}:void` | UNIQUE `ref` + state compare-and-set `authorized → voiding` | as above | `safe_write.try_claim` via `payments.cancel_order`; `payments._transition` (AUTHORIZED → VOIDING) |
| refund_captured_payment | `PayPalOperation`, `ref = {prefix}:order:{number}:refund:{sha256(Idempotency-Key)[:32]}`; request fingerprint stored | UNIQUE `ref`; same key + different amount → 409; refundable amount reserved by a conditional `UPDATE … WHERE refund_reserved + amount <= captured` | `IntegrityError` in `try_claim`; zero-row reservation UPDATE | `safe_write.try_claim` via `payments.refund_order`; fingerprint check and `reserve` (conditional UPDATE, `on_claimed`) in `payments.refund_order`; DB check constraint `paypal_refunds_within_capture` |
| create_payment_token | `PayPalOperation`, `ref = {prefix}:user:{id}:vault:{sha256(Idempotency-Key)[:32]}` | UNIQUE `ref` | `IntegrityError` in `try_claim` | `safe_write.try_claim` via `cards.save_card` (fingerprint check in `cards.save_card`) |
| delete_payment_token | `PayPalOperation`, `ref = {prefix}:user:{id}:vault-delete:{token}` | UNIQUE `ref` | `IntegrityError` in `try_claim` | `safe_write.try_claim` via `cards.delete_card` |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| create_order (pay) | **Lookup** (kind 3): `transaction_search.search_transactions` from the claim time to now, `balance_affecting_records_only="N"`, filtered on `invoice_id == ref-derived invoice id`, then `payments.get_authorized_payment(transaction_id)`. A same-key resend is **not** used (smoke: refused). Before PayPal's `last_refreshed_datetime` passes the claim time the lookup cannot see it → stays `unknown`. | `invoice_id = {prefix}-{number}-{attempt}` (same attempt as the claim ref) | `payments._find_authorization_by_invoice` (the `find` of `payments.pay_order`), run by step 3 of `safe_write.safe_write` |
| reauthorize_payment | Same-reference resend (`PayPal-Request-Id`, 45-day store) | claim ref | `find=` resend in `payments._renew_if_stale` (`repeat_is_safe=True`) |
| capture_authorized_payment | Lookup (kind 1): `orders.get_order(paypal_order_id)` → `purchase_units[0].payments.captures` (one final capture per order) | PayPal order id recorded before the call + claim ref | `payments._find_capture` (the `find` of `payments.fulfil_order`) |
| void_payment | Same-reference resend (smoke: a same-key repeat answers `VOIDED` again; 45-day store) | claim ref (authorization id recorded before the call) | `find=` same-key `void_payment` resend in `payments.cancel_order` (`repeat_is_safe=True`) |
| refund_captured_payment | Same-reference resend (smoke-verified de-duplication, 45-day store) | claim ref | `find=send` in `payments.refund_order` (`repeat_is_safe=True`) |
| create_payment_token | Same-reference resend (smoke-verified, 3 h store) | claim ref | `find=send` in `cards.save_card` (`repeat_is_safe=True`) |
| delete_payment_token | Same-reference resend (smoke: repeat delete → 204) | claim ref (token id) | `find=send` in `cards.delete_card` (`repeat_is_safe=True`) |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| create_order (pay) | Oscar `Order` (`Awaiting payment`) + `PayPalOperation(ref, sending, amount, currency, invoice_id, attempt)` | outcome, PayPal order id, authorization id/status/create/expiry, card brand/last4 → `PayPalPayment`; Oscar `Source` + `Transaction(Authorise)`; order → `Pending` | before: `orders.place_order` (Order + `PayPalPayment`), `safe_write.try_claim` (claim with `invoice_id`, amount); after: `safe_write.complete`, `payments._record_authorization` |
| reauthorize_payment | `PayPalPayment.state = capturing` + `PayPalOperation(ref, sending)` | new authorization id/status/times | before: `payments._transition` + `safe_write.try_claim`; after: the `_apply_once` block of `payments._renew_if_stale` |
| capture_authorized_payment | `PayPalPayment.state = capturing` + `PayPalOperation(ref, sending)` | capture id/status, gross/fee/net → `PayPalPayment`; `Source.amount_debited`; `Transaction(Debit)`; order → `Complete` | before: `payments._transition` + `safe_write.try_claim`; after: `payments._record_capture` |
| void_payment | `PayPalPayment.state = voiding` + `PayPalOperation(ref, sending)` | status; `Transaction(Void)`; order → `Cancelled`; stock allocations released | before: `payments._transition` + `safe_write.try_claim`; after: the `_apply_once` block of `payments.cancel_order`, `payments._cancel_locally` |
| refund_captured_payment | `PayPalOperation(ref, sending, amount, fingerprint)` + reserved amount on `PayPalPayment` | refund id/status; `refunded_minor`; `Source.amount_refunded`; `Transaction(Refund)` | before: `safe_write.try_claim` + `reserve` (`on_claimed`) in `payments.refund_order`; after: its `_apply_once` block, or `payments._release_reservation` (the `release` hook runs before a refused claim is released) |
| create_payment_token | `PayPalOperation(ref, sending)` | Oscar `Bankcard` (masked) + `PayPalCustomer` | before: `safe_write.try_claim`; after: the `Op.DONE` block of `cards.save_card` |
| delete_payment_token | `PayPalOperation(ref, sending)` (hides the card and blocks its use immediately) | outcome; `Bankcard` deleted on done | before: `safe_write.try_claim` (hides the card via `cards._deleting_tokens`); after: `card.delete()` in `cards.delete_card` |

## Reconciliation design

`GET /api/reconciliation?from&to` (staff): split `[from, to)` into ≤ 30-day windows, page each
window until `page >= total_pages`, keep records whose `transaction_initiation_date` is in
`[from, to)`. Local side: `PayPalOperation` rows (authorize, reauthorize, capture, refund) with a
`provider_time` in the window, matched **as a set** by `provider_id == transaction_id`. Report:
`matched`, `providerOnly` (flagged when `invoice_id`/`custom_field` carries our prefix), `localOnly`,
`notYetReported` (local events newer than `last_refreshed_datetime`), `unsettled` (claims still
`sending`/`unknown`). Where in the code: `reconciliation.parse_range`, `reconciliation.fetch_transactions` (30-day windows, all pages, narrowed to the window), `reconciliation.reconcile` (set matching, `notYetReportedByPayPal`, `unsettled`); `views.reconciliation_report`.

## Assumptions & Blockers

- No blockers. Every required capability exists in the SDK and was smoke-verified on the sandbox
  account; no 3-D Secure challenge was returned for the test card.
- Minor: catalogue prices are GBP stock records; per the task the **number** comes from the catalogue
  and the **currency** from `PAYPAL_CURRENCY` (no FX conversion).
- Minor: refunds are shopper-scoped (the task lists only fulfil, cancel and reconciliation as operator
  actions).
- Minor: reauthorization is allowed once, from day 4 to day 29 (docstring + smoke); past
  `expiration_time` the operator is told to cancel and have the shopper pay again.
- Minor: the pay lookup depends on PayPal's reporting lag; an unknown pay stays `unknown` (504) until
  the report catches up — never turned into `failed` by time.

## REQUIRED READING

| Hazard | Skill |
| --- | --- |
| Error ladder: `ApiError` narrowing, `OAuthProviderError` first, decode failures in both modes, sent vs maybe-sent `httpx` errors | MUST load `python-error-handling` |
| Client construction, lifetime, transport ownership, closed on shutdown | MUST load `python-client-initialization` |
| Credentials, lazy token fetch, missing-credential silence | MUST load `python-authentication` |
| Safe write (claim → call → check → verify → complete), no retries, base URL, logging transport, reconciliation clocks | MUST load `python-configuration-resilience` |
| Keyword-only params, `Prefer` defaults narrowing responses, `-> None` delete, `status_from_provider` / `answer` | MUST load `python-calling-endpoints` |
| `UNSET` vs `None`, open enums, money strings, never hand SDK values out raw | MUST load `python-models` |
| Stub transport, token request first, both transport failures, same-operation-twice test | MUST load `python-testing` |

## Verification (2026-09-27)

- Unit tests (stub PayPal at the SDK transport seam, real views): `cd sandbox && ../venv/Scripts/python manage.py test apps.paypal_api` → 30 tests OK.
- Type check: `mypy --strict` with the django-stubs plugin over `apps.paypal_api` → no issues (16 files). The pre-existing
  `sandbox/settings.py` is excluded from error reporting (untyped `django-environ`).
- Live, against the PayPal sandbox through the HTTP API: card authorization (double-click → one authorization), capture at
  fulfil with PayPal's fee and net, partial + full refunds (same key → one refund; over-refund → 409), void on cancel,
  card vaulted, reused to pay and capture a second order, deleted, then refused for payment; cross-shopper access → 404/400;
  shopper on staff endpoints → 403; reconciliation paged the full 31-day window (6,163 PayPal records).
- Not exercisable live: the stale-authorization renewal (needs a 4-day-old authorization) and the unknown-outcome lookup;
  both are covered by unit tests.
- The repository's own test suite needs PostgreSQL (`tests/settings`), which this machine does not have; it was not run.
