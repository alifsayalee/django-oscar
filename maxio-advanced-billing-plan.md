# Maxio Advanced Billing — subscription billing for the django-oscar sandbox

SDK: `maxio-advanced-billing` 1.0 (import root `maxio_advanced_billing`), installed from
`git+https://github.com/context-plugins/maxio-python-sdk.git@main` (commit `3d84ecef`) into `venv/`.
Map read from a scratch clone of the same commit (outside the repo).

## Plan

1. **App** `sandbox/apps/subscriptions/` (label `subscriptions`), added to `INSTALLED_APPS`, URLs
   included from `sandbox/urls.py` under `api/` (outside `i18n_patterns`, like `admin/`).
   Exemplars: `src/oscar/apps/*/apps.py` for AppConfig, `sandbox/urls.py` for URL wiring.
2. **Settings** (`sandbox/settings.py`, via `environ.Env` like the rest of the file; all default empty so
   import never raises): `MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`, `MAXIO_DEFAULT_PRODUCT_FAMILY`,
   `MAXIO_BASE_URL`, `MAXIO_ENVIRONMENT` (default `us`), `MAXIO_TIMEOUT` (default 10s),
   `MAXIO_REFERENCE_PREFIX` (default `oscar-sandbox`).
3. **Client** (`maxio.py`): sync `MaxioAdvancedBillingClient` (Django WSGI → sync), built lazily on
   first use under a lock (never at import; post-fork safe), closed at `atexit`. Missing settings →
   `MaxioConfigError` naming the missing setting names. Logging transport wraps `HttpxClient`
   (method, URL, status, ms — no headers/bodies).
4. **Gateway** (`gateway.py`): the only module that calls the SDK; one error ladder converting SDK /
   httpx / pydantic failures into `ProviderError(status_code, message, outcome_unknown)` subclasses.
   Reads retried (≤3 attempts, backoff, transient only); writes never retried.
5. **Local durable rows** (`models.py`, one migration), reusing Oscar's user model
   (`AUTH_USER_MODEL`) — they record *that we asked*, Maxio stays the record of what exists:
   - `BillingCustomer` — OneToOne(user) = the claim; `reference` unique; `maxio_customer_id`; `status`.
   - `SubscriptionEnrollment` — user, plan_handle, `reference` (unique, generated at claim),
     `maxio_subscription_id`, `status`, echo fields; `UniqueConstraint(user, plan_handle)` where status
     not in (failed, ended).
6. **Services** (`services.py`): `ensure_customer(user)`, `subscribe(user, plan_handle)`,
   `list_plans()`, `my_subscriptions(user)` — claim → call → reconcile (lookup by reference) →
   verify (echoed handle/price/customer) → settle (status from Maxio state, by name).
7. **Views** (`views.py`, JSON, session auth, CSRF enforced on POST, 401 JSON when anonymous):
   - `GET  /api/subscription-plans` → `[{planHandle, name, priceInCents, price, interval, intervalUnit, ...}]`
   - `POST /api/billing-customer` → ensure Maxio customer (idempotent), separately invocable
   - `POST /api/subscriptions` `{"planHandle": "..."}` → `{subscriptionId, status, plan, price, state, nextBillingAt, ...}`
   - `GET  /api/my-subscriptions` → the user's subscriptions read back from Maxio
   - `GET  /api/subscriptions/<subscriptionId>` → one subscription (owned by caller only)
8. **Tests** (`sandbox/apps/subscriptions/tests.py`, Django `TestCase`, stub transport through
   `custom_http_client`), run with `cd sandbox && ../venv/Scripts/python manage.py test apps.subscriptions`.
9. **mypy --strict** over the new app (with django-stubs), then live end-to-end against sandbox.

## Contract sheet

Client: **sync** `MaxioAdvancedBillingClient` (`from maxio_advanced_billing import MaxioAdvancedBillingClient`).
Constructor keyword-only: `environment`, `timeout` (default 30.0 → we set 10.0), `server_config`,
`custom_http_client`, `basic_auth`, `bearer_auth`. Held module-level, lazily built; `close()` at exit.
Never mix with `AsyncMaxioAdvancedBillingClient`.

- **Environment**: omitted = `"us"` silently → we always pass it, from `MAXIO_ENVIRONMENT` through an
  explicit map `{"us": "us", "eu": "eu"}` (case-insensitive); unknown value → config error. (sdk-map *Servers & auth*)
- **Base URL**: `server_config=ServerConfig(production=ProductionConfig(us=ProductionUsConfig(site=MAXIO_SITE_SUBDOMAIN)))`
  (`eu` → `ProductionEuConfig`); when `MAXIO_BASE_URL` is set → `ProductionUsConfig(base_url=MAXIO_BASE_URL)`
  verbatim. Typed models rather than the dict form: the nested dict does not type-check against
  `ServerConfigDict` under mypy. `ServerConfig` is `extra="forbid"`. (`server/server_config.py`)
- **Auth**: `basic_auth=BasicAuthCredentials(username=MAXIO_API_KEY, password="x")` — the SDK README's
  request example is `curl -u <api_key>:x`. Omitting it sends unauthenticated → we refuse to build.
  (`core` exports `BasicAuthCredentials`; README.md)
- **Retries**: SDK performs none. Ours: reads only; `httpx.TimeoutException|ConnectError`,
  status 429/500/502/503/504; 3 attempts, 0.5s·2^n backoff. Writes: none (reconcile by reference).
- **Keyword-only defaults**: every keyword-only parameter has a real default; never pass defensive `None`s.
- **Decode failure** raises `ValidationError`/`ValueError` in both modes (not `ApiError`). httpx
  exceptions arrive unwrapped.

### Operations

| # | Operation | Signature (positional \| keyword-only) | Returns | Error union | Assert on |
|---|---|---|---|---|---|
| 1 | `client.product_families.list_products_for_product_family` — `GET /product_families/{product_family_id}/products.json` | `(product_family_id: str, *, page=1, per_page=20, …, include_archived: bool\|None=None, …)`; id is `"handle:<family>"` per docstring | `list[ProductResponse]` | `ListProductsForProductFamilyErrorBody = str [404] \| RawError` | each `.product` (required) → `Product.handle`, `.price_in_cents`, `.interval`, `.interval_unit` (all `Optional`/UNSET-able); skip entries without handle |
| 2 | `client.customers.create_customer` — `POST /customers.json` | `(*, body: CreateCustomerRequest\|Dict)` | `CustomerResponse` (`.customer: Customer` required) | `CreateCustomerErrorBody = CustomerErrorResponse1 [422] \| RawError` | `.customer.id` not UNSET, `.customer.reference == ours` |
| 3 | `client.customers.read_customer_by_reference` — `GET /customers/lookup.json` | `(reference: str, *)` | `CustomerResponse` | Case B `RawError`; **404 = not found** (smoke-verified) | `.customer.id`, `.customer.email` |
| 4 | `client.subscriptions.create_subscription` — `POST /subscriptions.json` | `(*, body: CreateSubscriptionRequest\|Dict)` | `SubscriptionResponse` (`.subscription: Optional[Subscription]` — may be UNSET → outcome unknown) | `CreateSubscriptionErrorBody = ErrorListResponse1 [422] (errors: list[str], required) \| RawError` | `.subscription.id`, `.state`, `.product.handle`, `.product_price_in_cents`, `.customer.id` |
| 5 | `client.subscriptions.find_subscription` — `GET /subscriptions/lookup.json` | `(*, reference: str\|None=None)` | `SubscriptionResponse` | `FindSubscriptionErrorBody = RawError [404, any]`; **404 = not found** (smoke-verified) | as #4 |
| 6 | `client.customers.list_customer_subscriptions` — `GET /customers/{customer_id}/subscriptions.json` | `(customer_id: int, *)` | `list[SubscriptionResponse]` | Case B `RawError` | as #4 per entry |
| 7 | `client.subscriptions.read_subscription` — `GET /subscriptions/{subscription_id}.json` | `(subscription_id: int, *, include=None)` | `SubscriptionResponse` | Case B `RawError` | as #4 + `.customer.id` == caller's |

None of these return `None`. Parsed mode used throughout (we never need a success-path header).

### Request members we set

`CreateCustomerRequest(customer=CreateCustomer(...))` — `models/create_customer.py`:
- `first_name: str`, `last_name: str`, `email: str` — **required**. Oscar users may have blank names →
  fallback first = email local-part, last = username/email local-part.
- `reference: Optional[str]` — **always set** (the client-chosen reference we look a may-have-landed
  write up by): `f"{MAXIO_REFERENCE_PREFIX}-user-{user.pk}"`.
- everything else UNSET (omit → provider/site default).

`CreateSubscriptionRequest(subscription=CreateSubscription(...))` — `models/create_subscription.py`:
- `product_handle: Optional[str]` — the plan handle ("Required, unless a product_id is given").
- `customer_id: Optional[int]` — the existing Maxio customer id from `BillingCustomer`.
- `reference: Optional[str]` — **always set**: the enrollment's generated reference; lookup via #5.
- `payment_collection_method: Optional[CollectionMethodOrStr]` — **set** to `CollectionMethod.REMITTANCE`
  (setting `MAXIO_PAYMENT_COLLECTION_METHOD`, default `remittance`, validated against
  `models/enums/collection_method.py`: `AUTOMATIC`, `REMITTANCE`, `PREPAID`, `INVOICE`). Revised after the
  live run: omitted (→ site default `automatic`) Maxio answered 422 "No payment method was on file for the
  $299.00 balance" — this integration captures no card, so signup must be invoiced. Echo verified.
- NOT set: `next_billing_at`,
  `initial_billing_at`, `previous_billing_at`, `activated_at`, `canceled_at`, `import_mrr`
  (import/migration only), `product_price_point_*` (omit → product's default price point), payment
  profile attributes (plans don't require a payment method), `currency` (omit → site default).
- `Optional[T]` is `T | UnsetType` — never pass `None`.

### Response members we read

- `Product` (`models/product.py`): `id`, `name`, `handle` (OptionalNullable), `description`
  (OptionalNullable), `price_in_cents`, `interval`, `interval_unit: IntervalUnitOrStr`
  (`IntervalUnit.DAY="day"`, `MONTH="month"`, open), `archived_at` (OptionalNullable datetime),
  `require_credit_card`, `product_family.handle`, `trial_price_in_cents`, `initial_charge_in_cents`.
- `Subscription` (`models/subscription.py`): `id`, `state: SubscriptionStateOrStr`,
  `product_price_in_cents`, `current_billing_amount_in_cents`, `next_assessment_at`,
  `current_period_ends_at`, `activated_at`, `created_at`, `canceled_at`, `reference`, `currency`,
  `product: Product`, `customer: Customer`.
- `Customer` (`models/customer.py`): `id`, `reference`, `email`, `first_name`, `last_name`.
- All `Optional` → map UNSET/None to JSON `null` before leaving our boundary.

### Subscription state → local status (by name; `models/enums/subscription_state.py`)

| Maxio `SubscriptionState` member | local status | holds the plan claim |
|---|---|---|
| `ACTIVE`, `TRIALING` | `active` (done) | yes |
| `PAST_DUE`, `SOFT_FAILURE`, `UNPAID`, `PAUSED`, `ON_HOLD`, `SUSPENDED` | `problem` (exists, not in good standing) | yes |
| `PENDING`, `ASSESSING`, `AWAITING_SIGNUP` | `pending` | yes |
| `CANCELED`, `EXPIRED`, `TRIAL_ENDED` | `ended` | no |
| `FAILED_TO_CREATE` | `failed` | no |
| any other / unknown string | `unknown` | yes |

Local-only statuses: `sending` (claimed, provider not answered), `needs_review` (landed but not as
asked), `unknown` (may have landed; reconcile by reference).

### Error ladder (gateway, identical at every call site)

1. `ApiError` 401/403 → `ProviderConfigError` → our **502**.
2. `ApiError` 429 → `ProviderUnavailable(503, outcome_unknown=False)`.
3. `ApiError` 422 with typed arm (`CustomerErrorResponse1` / `ErrorListResponse1`) or 400/404/409 →
   `ProviderRejected(status, messages)` → caller's **422/400/404/409** with the provider's messages.
4. other `ApiError` (5xx, unmapped 4xx) → `ProviderFailure` **502** (writes: outcome unknown on 5xx).
5. `ValidationError`/`ValueError` on decode → `ProviderUnreadable` **502**, outcome unknown.
6. `httpx.ConnectError|ConnectTimeout|PoolTimeout|ProxyError` → **502**, never sent (known).
7. `httpx.RequestError` (rest) → **504**, outcome unknown → reconcile by reference.
Never surface `str(e)`; log narrowed fields.

## CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| `planHandle` accepted by `POST /api/subscriptions` must be a non-archived product returned by #1 for `MAXIO_DEFAULT_PRODUCT_FAMILY` | #4 ← #1 | `services.subscribe` (fresh #1 read; unknown → 400 before any write) |
| `customer_id` sent to #4 must be the id #2/#3 returned for *this* user's reference | #4 ← #2/#3 | `BillingCustomer` row; verified by echoed `subscription.customer.id` |
| subscription reference sent to #4 is the one looked up with #5 | #5 ← #4 | enrollment row `reference` |
| customer reference sent to #2 is the one looked up with #3 | #3 ← #2 | `BillingCustomer.reference` |
| `subscriptionId` accepted by `GET /api/subscriptions/<id>` must belong to the caller's customer | #7 ← #6/#4 | view checks `subscription.customer.id == user's maxio_customer_id` (404 otherwise) |
| echoed price on #4 equals the plan price read in #1 | #4 ↔ #1 | `services.subscribe` verify step → `needs_review` |
| echoed `payment_collection_method` on #4 equals the one sent | #4 | verify step → `needs_review` |

## Assumptions & Blockers

- No blockers. All capabilities are covered by the SDK map.
- Minor: Maxio customer reference uniqueness is enforced provider-side (docstring: "unique reference
  ID … single match"); a 422 on create is therefore reconciled by lookup rather than treated as failure.
- Minor: subscription reference uniqueness is not documented; our claim row prevents duplicates
  regardless, and lookup by reference (#5) is the reconciliation path.
- Minor: one live enrollment per (user, plan). Subscribing to a different plan is allowed.
- Wire note: the SDK percent-encodes `handle:` in the path (`handle%3Aeshop-subscribe`); Maxio accepts it (live-verified).
- Logging: `apps.subscriptions` logger registered in `LOGGING` (the sandbox uses `disable_existing_loggers`);
  `httpx`/`httpcore` quieted to WARNING.
- Pre-existing, unrelated: `loaddata sandbox/fixtures/orders.json` fails with a FK error on this tree
  and the catalogue seeds 11 products, not ~209. Not needed by this feature.

## REQUIRED READING (all loaded before implementation)

- Client construction/lifetime → MUST load `python-client-initialization` ✔
- Credentials (basic auth, loading secrets, empty-default settings) → MUST load `python-authentication` ✔
- Calls, status mapping by name → MUST load `python-calling-endpoints` ✔
- UNSET vs None, open enums, handing values out → MUST load `python-models` ✔
- Error ladder → MUST load `python-error-handling` ✔
- Writes (claim/call/reconcile/verify/settle), retries, timeouts, logging transport → MUST load `python-configuration-resilience` ✔
- Tests with stub transport → MUST load `python-testing` ✔
