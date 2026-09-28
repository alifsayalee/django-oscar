# PayPal integration plan — django-oscar sandbox (`sandbox/apps/paypal_payments`)

## Identity drift (recorded, settles every import below)

The `python-getting-started` skill names the distribution `pay-pal-server-sdk` / import root
`pay_pal_server_sdk`. The SDK repository it points at (branch `main`, version `2.29`) actually ships
distribution **`paypal`**, import root **`paypal`**, clients **`PaypalClient`** / **`AsyncPaypalClient`**
(`sdk-map.md` "Root package `paypal`"). Installed with
`pip install "paypal @ git+https://github.com/context-plugins/paypal-python-sdk.git@main"`.
Everything else on the skill page (controllers, error unions, base URL) matches the map.

## Repo survey

| Fact | Value / exemplar |
| --- | --- |
| Host | Django 5.2 WSGI site, `sandbox/` project; settings via `django-environ` (`sandbox/settings.py`) |
| Sync vs async | **sync** — WSGI views, no async views anywhere → `PaypalClient` |
| App convention | plain modules under `sandbox/apps/` imported as `apps.<name>` (exemplar: `sandbox/apps/sitemaps.py`, used by `sandbox/urls.py`) |
| URL convention | `path(...)` entries in `sandbox/urls.py` outside `i18n_patterns` (exemplar: `sitemap.xml` routes) |
| Order model | Oscar `order.Order` / `order.Line` via `oscar.apps.order.utils.OrderCreator.place_order` (exemplar: `src/oscar/apps/checkout/mixins.py`) |
| Payment bookkeeping | Oscar `payment.Source` / `payment.SourceType` / `payment.Transaction` (`src/oscar/apps/payment/abstract_models.py`) |
| DB | SQLite, `ATOMIC_REQUESTS=True` → payment views are `transaction.non_atomic_requests` so claims commit before the provider call |
| Auth | Django session auth (`AuthenticationMiddleware`, Oscar `EmailBackend`); CSRF middleware on |
| Toolchain | `venv` (py 3.11) + pip; tests via Django `DiscoverRunner` (`sandbox/manage.py test`); type check `mypy --strict` on the new app with `paypal` checked against its `py.typed` |
| Baseline | `tests/integration/order`, `tests/integration/payment`, `tests/functional/checkout` on the untouched tree: needs PostgreSQL by default; with `DATABASE_ENGINE=django.db.backends.sqlite3` 263 passed, 3 failed (`TestConcurrentOrderPlacement`, row locks unsupported by SQLite) |
| Credentials | env `PAYPAL_CLIENT_ID/SECRET/ENVIRONMENT/CURRENCY` present (`sandbox`, `USD`), read only through `settings.py` |
| Host selected | `PAYPAL_BASE_URL` if set (verbatim); else `PAYPAL_ENVIRONMENT=sandbox` → `https://api-m.sandbox.paypal.com` (the map's only server). Any other environment without `PAYPAL_BASE_URL` → `ImproperlyConfigured` (the map declares no other host; no URL is written from memory) |
| Smoke (scratch, real credential) | `orders.create_order` (AUTHORIZE + card, single step) → order `COMPLETED`, authorization `CREATED`, no payer action; `vault.create_payment_token` with raw card → id + `customer.id`; `transaction_search.search_transactions` → 200 with data; `payments.void_payment` → `VOIDED`; `vault.delete_payment_token` raw → `Success` 204. None gated. |

## Contract sheet

Client: `from paypal import PaypalClient`; `from paypal.core import ClientCredentials, ApiError, RawError, OAuthProviderError, UNSET, UnsetType, Success, Failure, HttpxClient, HttpRequest, HttpResponse`;
models from `paypal.models`; enums from `paypal.models.enums`.

* Construction (keyword-only): `PaypalClient(base_url=<resolved>, timeout=<float>, custom_http_client=LoggingTransport(HttpxClient(timeout=...)), oauth2=ClientCredentials(client_id=..., client_secret=...))`. Held module-level, built lazily on first use (post-fork), closed via `atexit`. `oauth2` always set (a missing credential is `ImproperlyConfigured` at build time). Omitting `base_url` silently = sandbox → always passed.
* Every keyword after `*` has a real default; no defensive `None`s. `prefer` defaults to `"return=minimal"` → we pass `"return=representation"` on every write whose body we read.
* SDK performs **no retries**. We add none automatically: a write whose outcome is unknown is settled by a same-`PayPal-Request-Id` resend on the next request for that operation (see UNKNOWN OUTCOMES).
* Decode failure raises `pydantic.ValidationError`/`ValueError` in both modes → treated as unknown on a write. A failed token fetch raises `ApiError` with `OAuthProviderError | RawError` → 502 config error.

| Operation | Signature (positional / after `*`) | Body model (members we set) | Returns | Error union | Members asserted |
| --- | --- | --- | --- | --- | --- |
| `orders.create_order` | `(body, *, pay_pal_request_id, prefer)` | `OrderRequest(intent=CheckoutPaymentIntent.AUTHORIZE, purchase_units=[PurchaseUnitRequest(amount=AmountWithBreakdown(currency_code, value), custom_id, invoice_id, description)], payment_source=PaymentSource(card=CardRequest(...)))`; one-off card: `CardRequest(number, expiry "YYYY-MM", security_code, name, billing_address=Address(country_code req., address_line_1, admin_area_2, admin_area_1, postal_code))`; saved card: `CardRequest(vault_id, stored_credential=CardStoredCredential(payment_initiator=CUSTOMER, payment_type=UNSCHEDULED, usage=SUBSEQUENT))` | `Order` | `CreateOrderErrorBody = Error \| RawError` (Error: 400, 401, 422) | `id`, `status: OrderStatus`, `purchase_units[0].payments.authorizations[0].{id,status,amount,expiration_time,create_time}`, `payment_source.card.{brand,last_digits}` |
| `payments.reauthorize_payment` | `(authorization_id, *, pay_pal_request_id, prefer, body)` | `ReauthorizeRequest(amount=Money(currency_code, value))` | `PaymentAuthorization` | `ReauthorizePaymentErrorBody = Error \| RawError` (Error: 400,401,403,404,422; Raw: 500) | `id`, `status`, `amount`, `create_time`, `expiration_time` |
| `payments.capture_authorized_payment` | `(authorization_id, *, pay_pal_request_id, prefer, body)` | `CaptureRequest(amount=Money, invoice_id, final_capture=True)` | `CapturedPayment` | `CaptureAuthorizedPaymentErrorBody = Error \| RawError` (Error: 400,401,403,404,409,422; Raw: 500) | `id`, `status: CaptureStatus`, `amount`, `seller_receivable_breakdown.{gross_amount (required), paypal_fee, net_amount}`, `create_time` |
| `payments.get_authorized_payment` | `(authorization_id)` | — | `PaymentAuthorization` | `GetAuthorizedPaymentErrorBody` (Error: 401,403,404) | `status`, `expiration_time` |
| `payments.get_captured_payment` | `(capture_id)` | — | `CapturedPayment` | `GetCapturedPaymentErrorBody` | as capture |
| `payments.void_payment` | `(authorization_id, *, pay_pal_request_id, prefer)` (no body) | — | `PaymentAuthorization` | `VoidPaymentErrorBody` (Error: 401,403,404,409,422; Raw 500) | `id`, `status`, `update_time` |
| `payments.refund_captured_payment` | `(capture_id, *, pay_pal_request_id, prefer, body)` | `RefundRequest(amount=Money, invoice_id, custom_id, note_to_payer)` | `Refund` | `RefundCapturedPaymentErrorBody` (Error: 400,401,403,404,409,422; Raw 500) | `id`, `status: RefundStatus`, `amount`, `create_time` |
| `payments.get_refund` | `(refund_id)` | — | `Refund` | `GetRefundErrorBody` | as refund |
| `orders.get_order` | `(id)` | — | `Order` | `GetOrderErrorBody` (Error: 401,404) | as create_order |
| `vault.create_payment_token` | `(body, *, pay_pal_request_id)` (keys kept 3 h) | `PaymentTokenRequest(customer=Customer(id) when known, payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(number, expiry, security_code, name, billing_address)))` | `PaymentTokenResponse` | `CreatePaymentTokenErrorBody` (Error: 400,403,404,422,500) | `id`, `customer.id`, `payment_source.card.{brand,last_digits,expiry,name}` — no status field |
| `vault.delete_payment_token` | `(id)` → **returns `None`**; called via `with_raw_response` | — | `ApiResult[None, DeletePaymentTokenErrorBody]` | Error: 400,403,500 | `Success.response.status_code` |
| `transaction_search.search_transactions` | `(start_date, end_date, *, page_size, page, fields, balance_affecting_records_only)`; dates RFC 3339 with seconds; range ≤ 31 days; page_size ≤ default 100 | — | `SearchResponse` | **Case B: `RawError` only** | `transaction_details[].transaction_info.{transaction_id, paypal_reference_id, transaction_event_code, transaction_initiation_date, transaction_amount, fee_amount, transaction_status, invoice_id, custom_field}`, `total_pages`, `page` |

Enums (all open `…OrStr`; an unlisted value arrives as `str` → `unknown`):
* `OrderStatus`: CREATED, SAVED, APPROVED, VOIDED, COMPLETED, PAYER_ACTION_REQUIRED
* `AuthorizationStatus`: CREATED, CAPTURED, DENIED, PARTIALLY_CAPTURED, VOIDED, PENDING
* `CaptureStatus`: COMPLETED, DECLINED, PARTIALLY_REFUNDED, PENDING, REFUNDED, FAILED
* `RefundStatus`: CANCELLED, FAILED, PENDING, COMPLETED
* `CheckoutPaymentIntent`: CAPTURE, AUTHORIZE
* Stored credential: `PaymentInitiator.CUSTOMER`, `StoredPaymentSourcePaymentType.UNSCHEDULED`, `StoredPaymentSourceUsageType.SUBSEQUENT`

Models: `Optional[T]` = `T | UnsetType` (never `None`); `Money.value`/`AmountWithBreakdown.value` are `str` → built from `Decimal` with the currency's ISO exponent; `Token.type_`/`CardResponse.type_` alias `type` (not used). No `Optional[Any]` member is set by us.

## OPERATION OUTCOMES

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/orders/{id}/pay` → `orders.create_order` (AUTHORIZE) | `purchase_units[0].payments.authorizations[0].status` (`AuthorizationStatus`); order `status` only when no authorization is present | CREATED → done (200, payment `authorized`); PENDING → pending (202); DENIED → failed (402/409, shopper may pay again); VOIDED → failed; CAPTURED / PARTIALLY_CAPTURED → needs_review (409); no authorization + order PAYER_ACTION_REQUIRED → failed ("browser approval required, not supported"); no authorization + order VOIDED → failed; no authorization + any other order status / absent → unknown (504); unlisted string → unknown | `services.authorize_outcome`, applied in `services.authorize` (`read`/`apply`), answered by `views.answer` from `views.pay_post` (failed → 402) |
| `POST /api/orders/{id}/fulfil` → `payments.reauthorize_payment` | `PaymentAuthorization.status` | CREATED → done; PENDING → pending (fulfil answers 202, capture not attempted); DENIED / VOIDED / CAPTURED / PARTIALLY_CAPTURED → failed (operator told the hold cannot be renewed); absent / unlisted → unknown | `services.reauthorize_outcome` in `services._renew_authorization`; refusal handled by `services._renewal_refused` |
| `POST /api/orders/{id}/fulfil` → `payments.capture_authorized_payment` | `CapturedPayment.status` (`CaptureStatus`) | COMPLETED → done (200, fee/net recorded); PENDING → pending (202); DECLINED / FAILED → failed (409); REFUNDED / PARTIALLY_REFUNDED → failed (undone) — cannot occur on the first answer; absent / unlisted → unknown (504) | `services.capture_outcome` in `services._capture`, answered by `views.answer` from `views.fulfil_post` |
| `POST /api/orders/{id}/cancel` → `payments.void_payment` | `PaymentAuthorization.status` | VOIDED → done (200, order Cancelled); CAPTURED / PARTIALLY_CAPTURED → failed (409 "already captured — refund instead"); CREATED / PENDING / DENIED → unknown (void not reflected; next cancel re-checks); absent / unlisted → unknown (504) | `services.void_outcome` in `services._void`, answered by `views.answer` from `views.cancel_post` |
| `POST /api/orders/{id}/refunds` → `payments.refund_captured_payment` | `Refund.status` (`RefundStatus`) | COMPLETED → done (200); PENDING → pending (202); FAILED / CANCELLED → failed (409, reservation released); absent / unlisted → unknown (504, reservation kept) | `services.refund_outcome` in `services.refund` / `services._apply_refund`, answered by `views.answer` from `views.refunds_post` |
| `POST /api/payment-methods` → `vault.create_payment_token` | none on `PaymentTokenResponse`; outcome from presence of `id` **and** `payment_source.card` | both present → done (201); id without card / body absent → unknown (504); 4xx refusal → failed (400/422) | `services.vault_outcome` over `read` in `services.save_card`, answered by `views.answer` from `views.payment_methods_post` |
| `DELETE /api/payment-methods/{id}` → `vault.delete_payment_token` | HTTP status of the raw `Success`/`Failure` (operation returns `None`) | 2xx → done; 404 → done (already gone); other 4xx → failed (card stays deleted locally, provider token flagged for operator); 5xx / transport → unknown (card stays deleted locally) | `services.delete_outcome` over the raw `ApiResult` status in `services.delete_card` (`vault.with_raw_response.delete_payment_token`) |

## DUPLICATE CLAIMS

Claim store: `ProviderWrite` table in the site's own database, `ref` column `unique=True`; `try_claim` is an
`INSERT` committed in its own transaction, the `IntegrityError` on the second insert is the rejection.

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| authorize (`create_order`) | `ProviderWrite(ref="<prefix>-<order#>-auth-<attempt>")` | DB unique constraint on `ProviderWrite.ref` | `IntegrityError` in `try_claim` | `safe_write.try_claim` (catches `IntegrityError`) via `safe_write.safe_write`; ref built in `services.authorize` |
| reauthorize | `ProviderWrite(ref="<prefix>-<order#>-reauth-<authorization id>")` | unique `ref` | `try_claim` | `safe_write.try_claim` via `safe_write.safe_write`; ref built in `services._renew_authorization` |
| capture | `ProviderWrite(ref="<prefix>-<order#>-capture-<authorization id>")` | unique `ref` | `try_claim` | `safe_write.try_claim` via `safe_write.safe_write`; ref built in `services._capture` |
| void | `ProviderWrite(ref="<prefix>-<order#>-void-<authorization id>")` | unique `ref` | `try_claim` | `safe_write.try_claim` via `safe_write.safe_write`; ref built in `services._void` |
| refund | `ProviderWrite(ref="<prefix>-<order#>-refund-<sha256(caller key)[:20]>")` + `PayPalRefund` unique `(payment, key_hash)`; the refundable amount is reserved by a conditional `UPDATE … WHERE refund_reserved + x <= captured_amount` | unique `ref`; the conditional update matching 0 rows rejects over-refund | `try_claim`; `reserve_refund_amount` | `services._reserve_refund` (conditional UPDATE + `PayPalRefund` unique key) and `safe_write.try_claim` via `safe_write.safe_write` from `services.refund` |
| save card (`create_payment_token`) | `ProviderWrite(ref="<prefix>-u<user id>-card-<sha256(caller key or fresh uuid)[:20]>")` | unique `ref` | `try_claim` | `safe_write.try_claim` via `safe_write.safe_write`; ref built in `services.save_card` |
| delete card | `ProviderWrite(ref="<prefix>-card-<saved card pk>-delete")` | unique `ref` | `try_claim` | `safe_write.try_claim` via `safe_write.safe_write`; ref built in `services.delete_card` |

`<prefix>` = `settings.PAYPAL_REFERENCE_PREFIX` if set, else an install id generated once and stored in
the DB (`InstallIdentity`), so two installs sharing the merchant account never reuse a reference.

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| authorize | same-reference resend (`PayPal-Request-Id`, kept 6 h) on the shopper's next `pay` for that order (payment details must be resent); when the claim holds a PayPal order id, `orders.get_order(id)` | `<prefix>-<order#>-auth-<attempt>` | `safe_write.safe_write` same-reference resend (check block) with `send` from `services.authorize`; `safe_write._refresh` with `find` = `orders.get_order` |
| reauthorize | same-reference resend (`PayPal-Request-Id`, kept 45 d) on the next `fulfil` | `<prefix>-<order#>-reauth-<auth id>` | `safe_write.safe_write` same-reference resend with `send` from `services._renew_authorization`; `_refresh` with `get_authorized_payment` |
| capture | same-reference resend (45 d) on the next `fulfil`; pending → `payments.get_captured_payment(capture id)` | `<prefix>-<order#>-capture-<auth id>` | `safe_write.safe_write` same-reference resend with `send` from `services._capture`; `safe_write._refresh` with `find` = `payments.get_captured_payment` |
| void | same-reference resend (45 d); lookup `payments.get_authorized_payment(auth id)` | `<prefix>-<order#>-void-<auth id>` | `safe_write.safe_write` same-reference resend with `send` from `services._void`; `_refresh` with `find` = `payments.get_authorized_payment` |
| refund | same-reference resend (45 d) on the repeat request with the same caller key; pending → `payments.get_refund(refund id)` | `<prefix>-<order#>-refund-<hash>` | `safe_write.safe_write` same-reference resend with `send` from `services.refund`; `safe_write._refresh` with `find` = `payments.get_refund` |
| save card | same-reference resend (kept 3 h) on a repeat with the same `Idempotency-Key` | `<prefix>-u<id>-card-<hash>` | `safe_write.safe_write` same-reference resend with `send` from `services.save_card` |
| delete card | resend of the delete (idempotent by id; 404 = gone) on a repeat `DELETE` | `<prefix>-card-<pk>-delete` | `safe_write.safe_write` resend with `send` from `services.delete_card` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| authorize | Oscar `Order` + `PayPalPayment(status=awaiting_payment)` + `ProviderWrite(ref, sending)` | PayPal order id, authorization id/status/expiry/time, card brand/last4 on `PayPalPayment`; `ProviderWrite` outcome; Oscar `Source.allocate` + order status `Being processed` | before: `services.place_order` (Order + `PayPalPayment`), `safe_write.try_claim`; after: `apply` in `services.authorize` inside `safe_write._settle` |
| reauthorize | `PayPalPayment(authorized)` + `ProviderWrite(ref, sending)` | new authorization id/time/expiry on `PayPalPayment`; outcome | before: `safe_write.try_claim`; after: `apply` in `services._renew_authorization` inside `safe_write._settle` |
| capture | `PayPalPayment(authorized)` + `ProviderWrite(ref, sending)` | capture id/status, captured amount, fee, net, capture time; Oscar `Source.debit`; order `Complete`; stock allocation consumed | before: `safe_write.try_claim`; after: `apply` in `services._capture` inside `safe_write._settle` |
| void | `PayPalPayment(authorized)` + `ProviderWrite(ref, sending)` | status `voided`; order `Cancelled`; stock allocation cancelled | before: `safe_write.try_claim`; after: `apply` in `services._void` inside `safe_write._settle` |
| refund | `PayPalRefund(status=sending)` + reservation on `PayPalPayment.refund_reserved` + `ProviderWrite(ref, sending)` | PayPal refund id/status/time; `amount_refunded`; Oscar `Source.refund`; reservation released on failure | before: `services._reserve_refund`, `safe_write.try_claim`; after: `services._apply_refund` inside `safe_write._settle`; release in `services._release_refund` |
| save card | `ProviderWrite(ref, sending)` (user + key) | `SavedCard(token id, brand, last4, expiry)`, `PayPalCustomer(vault customer id)` | before: `safe_write.try_claim`; after: `apply` in `services.save_card` inside `safe_write._settle` |
| delete card | `SavedCard.deleted_at` set (unusable, hidden) + `ProviderWrite(ref, sending)` | outcome on `ProviderWrite` and `SavedCard.provider_deleted` | before: `SavedCard.deleted_at` update in `services.delete_card`, `safe_write.try_claim`; after: `apply` in `services.delete_card` |

## Reconciliation

`GET /api/reconciliation?from&to` (staff). Provider side: `search_transactions` over the range split into
≤31-day windows, every page (`page` 1…`total_pages`). Local side on the provider's clock: captures and
refunds whose stored PayPal `create_time` falls in the range. Matching: PayPal `transaction_id` ==
our capture id / refund id; secondarily `custom_field`/`invoice_id` carrying our `<prefix>-<order#>`.
Findings: `matched`, `paypalOnly`, `appOnly`, `unsettled` (our writes in the range with no provider time:
`sending`/`unknown`/`pending`).

## Assumptions & Blockers

* No blockers. The claim store is the site's own database (unique constraint) — durable across processes.
* Minor: the catalogue is priced in GBP; the task mandates currency from `PAYPAL_CURRENCY`, so the order
  is placed and charged as the catalogue amount in the configured currency.
* Minor: seeded users are both `is_staff`; a shopper account is created for verification (documented).
* Minor: `POST /api/session` provides Django session login for API callers (same `authenticate`/`login`).
* Minor: saving a card without an `Idempotency-Key` header is not de-duplicated (a fresh key is generated).
* Minor: the brief says the CSV catalogue import is optional; on this host it is required for the 209 products.

## REQUIRED READING

* MUST load `python-client-initialization` — module-level lazy client, `atexit` close, custom transport keyword.
* MUST load `python-authentication` — `oauth2=ClientCredentials`, OAuthProviderError on token fetch.
* MUST load `python-calling-endpoints` — `prefer` default narrows the body; `delete_payment_token` returns `None` → raw peer.
* MUST load `python-models` — `Optional` ≠ `typing.Optional`, open enums, Decimal money strings.
* MUST load `python-error-handling` — error ladder, never-sent vs unknown transport split.
* MUST load `python-configuration-resilience` — safe write, no retries, reconciliation on provider clock.
* MUST load `python-testing` — stub transport + token response for the unit tests.
(All seven loaded before implementation.)
