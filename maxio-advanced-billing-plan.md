# Maxio Advanced Billing — subscription billing for the Oscar sandbox

## Goal

Additive capability on the runnable sandbox site (`sandbox/`): a logged-in shopper lists plans,
subscribes, and reads their subscriptions back. Maxio is the system of record for subscriptions;
the local database only records *that we asked* (claim rows), per `python-configuration-resilience`.

Endpoints (new app `sandbox/apps/subscriptions/`, wired in `sandbox/urls.py` outside
`i18n_patterns`, next to `admin/` / `sitemap.xml`):

| Route | Action | Auth |
|---|---|---|
| `GET /api/subscription-plans` | list plans of `MAXIO_DEFAULT_PRODUCT_FAMILY`, each with `planHandle` | session login |
| `POST /api/subscriptions` | body `{"planHandle": "..."}` → ensure customer, create subscription; returns top-level `subscriptionId` | session login + CSRF |
| `GET /api/my-subscriptions` | caller's subscriptions, live from Maxio, plus local claims not yet settled | session login |

Caller identity: `request.user` (Oscar's `AUTH_USER_MODEL`, reused — no parallel user model).
Unauthenticated → `401` JSON. CSRF stays on (session-cookie auth); GET endpoints set the CSRF
cookie (`ensure_csrf_cookie`) so an API caller can echo it back as `X-CSRFToken`.

## Repo survey

- Host: Django 5.2 under WSGI, **sync** views everywhere → **sync client** (`MaxioAdvancedBillingClient`).
- Sandbox app convention: plain packages under `sandbox/apps/` (exemplar `sandbox/apps/user/models.py`),
  imported as `apps.<name>`; URLs in `sandbox/urls.py` (exemplar: the `admin/` / `sitemap.xml` entries).
- `ATOMIC_REQUESTS = True` in `sandbox/settings.py` → views that claim-then-call must be
  `@transaction.non_atomic_requests` and commit the claim before the provider call.
- Settings read via `django-environ` `env` object (exemplar: `SECRET_KEY`, `DEBUG` lines in `sandbox/settings.py`).
- Tests: repo suite uses pytest + `tests.settings` (does not load sandbox apps). New app's tests are
  Django `TestCase`s run with `sandbox/manage.py test apps.subscriptions` (sandbox `TEST_RUNNER` = DiscoverRunner).
- Toolchain: `venv` + pip (`py -3.11`). `maxio-advanced-billing` installed from git@main; its
  metadata came out with empty `Requires`, so `httpx`, `pydantic[email]`, `typing-extensions` were
  installed explicitly at the SDK's declared ranges. `mypy` installed for `--strict` on new files.
- Credentials verified present in env (`MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN=cp-exp-1`,
  `MAXIO_ENVIRONMENT=US`, `MAXIO_DEFAULT_PRODUCT_FAMILY=eshop-subscribe`). Ports: 37400–37419.
- Smoke (read-only, scratchpad) against the real site: `list_products_for_product_family("handle:eshop-subscribe")`
  → `basic-plan` 2900¢/1 month, `eshop-pro` 29900¢/1 month (IDs already differ from the task table —
  handles only); `read_product_by_handle` OK; misses on `read_customer_by_reference`,
  `find_subscription`, `read_product_by_handle` → **404, empty body**. No 403 gating observed.

## Configuration (settings in `sandbox/settings.py`)

| Setting | Source | Notes |
|---|---|---|
| `MAXIO_API_KEY` | env, default `''` | empty → client factory raises `ImproperlyConfigured` → API answers 503 |
| `MAXIO_SITE_SUBDOMAIN` | env, default `''` | `server_config={"production": {env: {"site": ...}}}` |
| `MAXIO_DEFAULT_PRODUCT_FAMILY` | env, default `''` | family handle; sent as `handle:<value>` |
| `MAXIO_BASE_URL` | env, default `None` | when set: `server_config={"production": {env: {"base_url": value}}}` verbatim (no `{site}` placeholder → used as-is) |
| `MAXIO_ENVIRONMENT` | env, default `'us'` | lower-cased; explicit map `{"us","eu"}`; unknown → `ImproperlyConfigured` (never silently `"us"`) |

## Contract sheet

Source of every row: `sdk-map.md`, `map/operations/{customers,products,product_families,subscriptions}.md`,
and the model/enum modules named in each block's Type sources (SDK 1.0, branch `main`).

**Client**: sync `MaxioAdvancedBillingClient` (root import). Keyword-only ctor: `environment`,
`timeout` (float, >0, default 30.0 — we pass 10.0), `server_config`, `custom_http_client`,
`basic_auth`, `bearer_auth`. Omitting `environment` = `"us"` silently → always passed.
`basic_auth=BasicAuthCredentials(username=<MAXIO_API_KEY>, password="x")` (`maxio_advanced_billing.core`;
key-as-username/`x`-as-password from the SDK's own reference: `curl -u <api_key>:x`). Omitting auth
sends unauthenticated requests silently → factory asserts key non-empty.
Lifetime: lazily-built module-global, lock-guarded, closed via `atexit`; built after fork on first use.
Transport: `LoggingTransport(HttpxClient(timeout=10.0))` passed as `custom_http_client` (client
`timeout=` does not reach a supplied transport — timeout set on `HttpxClient`). Logs method, URL, status, ms only.

Every keyword-only parameter has a real default → never pass defensive `None`s.
No operation below returns `None`. Async twins exist; not used.

| Operation | Positional / keyword-only | Returns | `ApiError.error` union |
|---|---|---|---|
| `client.product_families.list_products_for_product_family` | `product_family_id: str` (id **or** `handle:<handle>`) / `page=1`, `per_page=20` (max 200), `include_archived: bool\|None=None`, others unused | `list[ProductResponse]` | `str` [404] \| `RawError` |
| `client.products.read_product_by_handle` | `api_handle: str` / — | `ProductResponse` | `RawError` (Case B; 404 = unknown handle, empty body) |
| `client.customers.read_customer_by_reference` | `reference: str` / — | `CustomerResponse` | `RawError` (Case B; 404 = no match, empty body) |
| `client.customers.create_customer` | — / `body: CreateCustomerRequest\|Dict` | `CustomerResponse` | `CustomerErrorResponse1` [422] \| `RawError` |
| `client.subscriptions.create_subscription` | — / `body: CreateSubscriptionRequest\|Dict` | `SubscriptionResponse` | `ErrorListResponse1` [422] \| `RawError` |
| `client.subscriptions.find_subscription` | — / `reference: str\|None=None` | `SubscriptionResponse` | `RawError` [404, anything] |
| `client.customers.list_customer_subscriptions` | `customer_id: int` / — | `list[SubscriptionResponse]` | `RawError` (Case B) |

### Models (members the task sets or reads)

- `CreateCustomerRequest(customer: CreateCustomer)` — required.
- `CreateCustomer`: **required** `first_name: str`, `last_name: str`, `email: str`;
  `reference: Optional[str]` — **always set** (client-chosen reference = the reconciliation key,
  `oscar-user-<pk>`). All other members (`organization`, address, `locale`, `tax_exempt`, `parent_id`, …) → omit (provider default).
- `CreateSubscriptionRequest(subscription: CreateSubscription)` — required.
- `CreateSubscription` (all `Optional[...] = UNSET`):
  `product_handle: str` — set (plan handle; "Required, unless product_id given").
  `customer_id: int` — set (existing customer).
  `reference: str` — **always set** (client reference for the subscription = reconciliation key).
  `payment_collection_method: CollectionMethodOrStr` — **set to `CollectionMethod.REMITTANCE`** (members
  `AUTOMATIC`, `REMITTANCE`, `PREPAID`, `INVOICE`; RI sites: remittance/automatic/prepaid). Overrides the
  site default deliberately: sandbox probe with it omitted → 422 `"No payment method was on file for the
  $29.00 balance"`; this app captures no card, so the shopper is invoiced (remittance). Probe with remittance → `active`.
  Omit → provider default: `product_price_point_*` (product's default price point),
  `next_billing_at`/`initial_billing_at` (plan schedule), `components`, `coupon_*`, payment-profile/card attrs
  (plans do not require a payment method), `currency`, `metafields`.
  Import/migration only → never set: `previous_billing_at`, `import_mrr`, `activated_at`, `canceled_at`.
- `ErrorListResponse1.errors: list[str]` (required). `CustomerErrorResponse1.errors: Optional[Errors1]` (union in `models/unions/errors1.py`) — only logged / surfaced as text via `to_dict()`.
- `ProductResponse.product: Product` (required). `Product`: `handle: OptionalNullable[str]`, `name: Optional[str]`,
  `description: OptionalNullable[str]`, `price_in_cents: Optional[int]`, `interval: Optional[int]`,
  `interval_unit: Optional[IntervalUnitOrStr]` (members `DAY="day"`, `MONTH="month"`; open enum),
  `archived_at: OptionalNullable[RFC3339DateTime]`, `product_family: Optional[ProductFamily]` (`handle: Optional[str]`),
  `require_credit_card: Optional[bool]`.
- `CustomerResponse.customer: Customer` (required). `Customer.id: Optional[int]`, `reference: OptionalNullable[str]`.
- `SubscriptionResponse.subscription: Optional[Subscription]` (**not required** — a truncated 2xx decodes to UNSET).
  `Subscription`: `id: Optional[int]`, `state: Optional[SubscriptionStateOrStr]`,
  `next_assessment_at: OptionalNullable[RFC3339DateTime]`, `current_period_ends_at: OptionalNullable[...]`,
  `product_price_in_cents: Optional[int]`, `product: Optional[Product]`, `reference: OptionalNullable[str]`,
  `customer: Optional[Customer]`, `currency: Optional[str]`, `created_at`, `activated_at`, `canceled_at`.
  Note (docstring of `read_subscription`): with the new Catalog experience `product` may be `null`; it is typed
  non-nullable, so such a body would fail decode (`ValidationError`) — handled as "unreadable".

### Members to assert after each call (UNSET ⇒ outcome unknown / unreadable)

- `create_customer` → `customer.id` set, else unknown → reconcile by reference.
- `read_customer_by_reference` → `customer.id` set, else unreadable.
- `create_subscription` → `subscription` set, `subscription.id` set, `subscription.state` set; else unknown → reconcile.
- echo check before settle: `subscription.product.handle == requested plan handle` and
  `subscription.product_price_in_cents == Product.price_in_cents` read for that plan just before the create
  → mismatch ⇒ row `needs_review` (the subscription exists, not as asked).

### `SubscriptionState` → our outcome (every member, by name; anything else `unknown`)

| Members | Bucket | Row status | Holds the claim? |
|---|---|---|---|
| `ACTIVE`, `TRIALING` | active | `done` | yes |
| `PENDING`, `ASSESSING`, `AWAITING_SIGNUP` | pending | `pending` | yes |
| `PAST_DUE`, `SOFT_FAILURE`, `UNPAID` | problem (exists, billing issue) | `done` | yes |
| `PAUSED`, `ON_HOLD`, `SUSPENDED` | inactive (exists, not billing) | `done` | yes |
| `CANCELED`, `EXPIRED`, `TRIAL_ENDED` | ended | `ended` | **no** — may resubscribe |
| `FAILED_TO_CREATE` | failed | `failed` | **no** |
| any other value (open enum `str`) | unknown | `unknown` | yes (needs review) |

POST answer: `201` for bucket active/problem/inactive; `202` for pending/unknown; `502` for failed.

### Failure kinds (all calls) — one ladder, `apps/subscriptions/maxio/errors.py`

`ApiError`: 401/403 → `ProviderConfigError` (502); 429 → `ProviderUnavailable(503)`; 404 on lookups →
absence (only the lookup's own 404); 422 typed → `ProviderRejected` (422 to caller with provider messages);
other → `ProviderFailure` (502). `pydantic.ValidationError`/`ValueError` → `ProviderUnreadable` (outcome
unknown on a write; 502 on a read). `httpx.ConnectError|ConnectTimeout|PoolTimeout|ProxyError` →
`ProviderUnavailable(502, outcome_unknown=False)`; other `httpx.RequestError` →
`ProviderUnavailable(504, outcome_unknown=True)`. `str(e)` never surfaced.

### Retries

SDK performs **no retries**. We retry **reads only** (list plans, read product, lookups, list customer
subscriptions): max 2 attempts, on never-sent transport errors and 502/503/504/429, short backoff
(0.5 s). Writes are never resent; an unknown write is reconciled by reference under the same claim.

## Persistence (claim rows — one migration)

- `MaxioCustomer`: `user` OneToOne(AUTH_USER_MODEL), `reference` unique (`oscar-user-<pk>`),
  `maxio_customer_id` nullable, `status` (`sending|done|unknown|failed`), timestamps.
  The OneToOne is the claim. Flow: claim → `read_customer_by_reference` (404 ⇒ none) → create if none →
  on unknown outcome, lookup again → settle. **Verified in sandbox:** Maxio rejects a reused customer
  `reference` with 422 `CustomerErrorResponse1` `"Reference: must be unique - that value has been taken."`, and a
  reused subscription `reference` with 422 `ErrorListResponse1` carrying the same message. So (a) a 422 whose
  messages contain `"Reference: must be unique"` is a **landing** → look up by reference and keep it; (b) re-sending
  the create under the **same** reference (never a new one) is safe, because the provider rejects the duplicate.
  A stale `sending` (> SEND_WINDOW) or `unknown` row is therefore resolved by lookup first, then — only if the
  lookup is a definitive 404 — by re-sending under the same reference, and only by the request holding the claim.
- `SubscriptionRequest`: `user` FK, `plan_handle`, `reference` unique (uuid-based, sent as subscription
  `reference`), `status` (`sending|pending|done|unknown|needs_review|failed|ended`), `state` (raw Maxio state),
  `maxio_subscription_id` nullable, `price_in_cents`, timestamps.
  Constraint: `UNIQUE(user, plan_handle) WHERE status NOT IN ('failed','ended')` → double-click loser gets
  the winner's row (200/202), never a second create.

## CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| `planHandle` accepted by POST must be a non-archived product of `MAXIO_DEFAULT_PRODUCT_FAMILY` | `create_subscription.product_handle` ← `read_product_by_handle` (+ `product.product_family.handle`) / `list_products_for_product_family` | service: read product by handle, check family handle + not archived, else 400 |
| `customer_id` sent to create_subscription must be the id Maxio returned for this user's reference | `create_subscription.customer_id` ← `create_customer` / `read_customer_by_reference` | service (from settled `MaxioCustomer` row) |
| `customer_id` for my-subscriptions is the caller's own customer | `list_customer_subscriptions` ← `MaxioCustomer` of `request.user` | view uses only the caller's row |
| reconciliation reference = the one we sent | `find_subscription(reference)` ← `CreateSubscription.reference`; `read_customer_by_reference` ← `CreateCustomer.reference` | service |

## Assumptions & Blockers

- No blockers. Minor: plan price currency is not on `Product`; response reports cents + interval only (site currency
  from `subscription.currency` on subscriptions). Minor: plans list cached 60 s in the default cache.
- Resolved: duplicate-reference rejection verified live for customers and subscriptions (see Persistence).
- Minor: `payment_collection_method=remittance` is a deliberate override (no card capture in this app).

## Build order

1. settings + app skeleton + models/migration; 2. `maxio/client.py` (factory, logging transport),
`maxio/errors.py` (ladder), `maxio/states.py`; 3. `services.py` (plans, ensure_customer, subscribe,
my_subscriptions); 4. `views.py` + `urls.py` + wire into `sandbox/urls.py`; 5. tests with a stub
transport (success, 422, unmapped status, truncated 2xx, decode failure, ConnectError vs ReadTimeout,
double-submit, unknown state); 6. `mypy --strict` on new files; 7. build sandbox DB, run server on 37400,
live end-to-end.

## REQUIRED READING

- MUST load `python-error-handling` — error ladder, decode failures, transport split. (loaded)
- MUST load `python-client-initialization` — sync client, lifetime, custom transport timeout. (loaded)
- MUST load `python-configuration-resilience` — claim rows, reconcile, no retries, logging transport, pagination bound. (loaded)
- MUST load `python-calling-endpoints` — status→outcome mapping, keyword-only boundary. (loaded)
- MUST load `python-models` — UNSET vs None, open enums, handing values out. (loaded)
- MUST load `python-testing` — stub transport seam, failure-kind tests. (loaded)
- MUST load `python-authentication` — Basic credentials, secret loading. (load before wiring credentials)
