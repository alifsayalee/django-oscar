# Maxio Advanced Billing — subscription billing for the django-oscar sandbox

## Scope

New Django app `sandbox/apps/subscriptions/` (label `subscriptions`), wired into `sandbox/urls.py`
under `/api/` (outside `i18n_patterns`, like `admin/` and `sitemap.xml`):

| Route | Action |
|---|---|
| `GET  /api/subscription-plans` | list plans of `MAXIO_DEFAULT_PRODUCT_FAMILY` (each carries `planHandle`) |
| `POST /api/subscriptions` | ensure Maxio customer for `request.user`, subscribe to `{"planHandle": …}`; returns top-level `subscriptionId` |
| `GET  /api/subscriptions/<int:subscription_id>` | read one of the caller's subscriptions (refresh / follow-up for 202s) |
| `GET  /api/my-subscriptions` | the caller's subscriptions (Maxio is the record) + local requests still in flight/unknown |

Auth: Django session (Oscar's `/en-gb/accounts/login/`), identity = `request.user`; unauthenticated →
`401` JSON (not a redirect). CSRF stays enforced on `POST` (session auth ⇒ CSRF is required).

## Repo survey (conventions to imitate)

- Sandbox apps are plain packages under `sandbox/apps/` imported as `apps.<name>` (exemplar:
  `sandbox/urls.py` → `from apps.sitemaps import base_sitemaps`). Settings module is `sandbox/settings.py`
  (uses `django-environ`'s `env(...)`; exemplar line: `THUMBNAIL_REDIS_URL = env('THUMBNAIL_REDIS_URL', default=None)`).
- Oscar models reused: the user model (`oscar.core.compat.AUTH_USER_MODEL`, as `apps/user/models.py` does);
  user email / first / last name feed the Maxio customer. Plans are **not** mirrored into Oscar's catalogue
  (Maxio is the system of record; a copy would be a parallel set).
- Integration-ledger models (one table for the user↔customer link, one for subscribe claims) are new: Oscar
  has nothing that records "we asked Maxio for X under reference R". They are required by
  python-configuration-resilience (one durable row per write). Migrations live in the app.
- Host is **WSGI Django, synchronous** → sync `MaxioAdvancedBillingClient`.
- Style: flake8 max-line 119, isort; views return `JsonResponse`.
- Toolchain: `pip` + `venv` (`py -3.11`), `venv\Scripts\pip install -e .[test]`; SDK installed from
  git (`maxio-advanced-billing @ git+https://github.com/context-plugins/maxio-python-sdk.git@main`) — its
  runtime deps (`httpx`, `pydantic[email]`, `typing-extensions`) did not install transitively here, so they
  are listed explicitly too. Tests: Django test runner from `sandbox/`: `..\venv\Scripts\python manage.py test apps.subscriptions`.
  Type check: `mypy --strict` over the new app (django-stubs installed in venv).
- Baseline: `manage.py check` clean (warnings only: templates.W003). DB build: `loaddata orders.json` fails
  with FK error on this machine (11 products load, not 209) — pre-existing, unrelated to billing.

## Configuration (settings.py — names fixed by the task)

`MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`, `MAXIO_DEFAULT_PRODUCT_FAMILY`, `MAXIO_BASE_URL` (optional,
verbatim base URL override), plus `MAXIO_ENVIRONMENT` (env var provided; `US`/`EU`, case-insensitive) and
`MAXIO_TIMEOUT_SECONDS` (default 10). All `env(..., default='')` — importing settings never raises; the
missing-credential check lives in the client factory and names the missing settings.

## Contract sheet

Source: SDK map (`sdk-map.md`, `map/operations/{product_families,customers,subscriptions}.md`) and the
modules named there, SDK version `1.0`, branch `main`.

### Client

- Sync `MaxioAdvancedBillingClient` from `maxio_advanced_billing`; keyword-only ctor: `environment`,
  `timeout`, `server_config`, `basic_auth`. Map: sdk-map.md *Getting a client*.
- `environment`: `"us"` | `"eu"` (`Environment` literal, `maxio_advanced_billing.server`). **Omitting it is
  silently `"us"`** — we always pass it, from `MAXIO_ENVIRONMENT` via an explicit map; unknown → error.
- `server_config` nesting (several servers × several environments), map *Servers & auth*:
  `{"production": {"<env>": {"site": <subdomain>}}}`; with `MAXIO_BASE_URL`:
  `{"production": {"<env>": {"base_url": <url>}}}`. `extra="forbid"`. All ops in scope use server `production`.
- Auth: `basic_auth=BasicAuthCredentials(username=<api key>, password="x")` — README request example
  `curl -u <api_key>:x`. Omitted credential ⇒ unauthenticated requests, no error: factory refuses empty key.
- Lifetime: one lazily built module-level client per process (built after fork, on first use, lock-guarded),
  closed via `atexit`. `close()` obligation. Timeout 10 s (float, >0).
- **No retries in the SDK.** We retry idempotent **reads** only (≤3 attempts, short backoff) on
  never-sent transport errors, `httpx.ReadTimeout`, 429/502/503/504. **Writes are never retried**; they go
  through claim → call → reconcile.

### Operations (sync parsed form; every keyword-only param has a real default — no defensive `None`s)

| Op | Signature (positional \| keyword-only) | Returns | `ApiError.error` union | Notes |
|---|---|---|---|---|
| `product_families.list_products_for_product_family` | `(product_family_id: str, *, page=1, per_page=20, …, include_archived: bool\|None=None, …)` | `list[ProductResponse]` | `ListProductsForProductFamilyErrorBody = str [404] \| RawError` | id param accepts `"handle:<family>"` (docstring). `per_page` max 200. 404 decodes body as JSON `str` — a non-JSON 404 body raises `ValidationError`/`ValueError` instead. Page loop bounded (MAX_PAGES=10, per_page=200), `truncated` surfaced. |
| `customers.read_customer_by_reference` | `(reference: str, *, request_options=None)` | `CustomerResponse` | `RawError` (Case B) | Smoke: miss → `404`, empty body. 404 = absent; anything else is a failure, never absence. |
| `customers.create_customer` | `(*, body: CreateCustomerRequest\|Dict\|None=None)` | `CustomerResponse` | `CreateCustomerErrorBody = CustomerErrorResponse1 [422] \| RawError` | body is required in practice. 422 may mean our reference already taken → look up by reference. |
| `subscriptions.create_subscription` | `(*, body: CreateSubscriptionRequest\|Dict\|None=None)` | `SubscriptionResponse` | `CreateSubscriptionErrorBody = ErrorListResponse1 [422] \| RawError` | write — claim first, never retried. |
| `subscriptions.find_subscription` | `(*, reference: str\|None=None)` | `SubscriptionResponse` | `FindSubscriptionErrorBody = RawError [404, other]` | reconcile lookup by the reference we sent; 404 → not found (yet). Smoke: miss → 404. |
| `subscriptions.read_subscription` | `(subscription_id: int, *, include=None)` | `SubscriptionResponse` | `RawError` (Case B) | ownership checked: `subscription.customer.id == caller's customer id`. |
| `customers.list_customer_subscriptions` | `(customer_id: int, *, request_options=None)` | `list[SubscriptionResponse]` | `RawError` (Case B) | no paging params. |

None of these returns `None`; parsed mode everywhere (outcome inspected via exceptions + `e.status_code`).

### Models (members the task sets / reads). `Optional[T]` = `T | UnsetType`, never `None`.

- `CreateCustomerRequest(customer: CreateCustomer)` — required.
- `CreateCustomer`: **required** `first_name: str`, `last_name: str`, `email: str`. Set: `reference: Optional[str]`
  — client-chosen reference, **always set** (`oscar-user-<pk>-<date_joined epoch>`, deterministic per user,
  distinct across DB rebuilds). All other members omitted (→ provider/site defaults: address, locale, tax,
  branding, parent — none in scope). Empty first/last name falls back to email local part / `"Customer"` (required non-empty is UNVERIFIED — sending a placeholder avoids the question).
- `CustomerResponse.customer: Customer` (required); `Customer.id: Optional[int]`, `reference: OptionalNullable[str]`
  — assert `id` is set after create/lookup, else outcome unknown.
- `CreateSubscriptionRequest(subscription: CreateSubscription)` — required.
- `CreateSubscription` (all optional): set `product_handle: str` (plan), `customer_id: int` (ensured customer),
  `reference: str` (client-chosen, stored on the claim row before the call — reconcile key),
  `payment_collection_method: CollectionMethodOrStr = CollectionMethod.REMITTANCE` (enum members `automatic`,
  `remittance`, `prepaid`, `invoice`; docstring: Relationship Invoicing accepts remittance/automatic/prepaid).
  **Overrides the default on purpose**: live run showed the default (automatic) refuses signup with
  422 "No payment method was on file for the $299.00 balance"; this API never captures a card, so
  collection is by invoice. Omitted: `product_price_point_*` (→ product default price point), `next_billing_at`/`initial_billing_at`/`previous_billing_at`/`activated_at`/
  `canceled_at`/`import_mrr` (import/migration or schedule overrides — not ours), `coupon_*`, `components`,
  `calendar_billing`, `metafields`, `currency`, `expires_at`, card/bank attributes — none in scope.
- `SubscriptionResponse.subscription: Optional[Subscription]` — **not required**: assert set.
- `Subscription` read members: `id: Optional[int]`, `state: Optional[SubscriptionStateOrStr]`,
  `product_price_in_cents: Optional[int]`, `current_period_ends_at: OptionalNullable[RFC3339DateTime]`
  ("when the next regularly scheduled attempted charge will occur" → **nextBillingAt**),
  `next_assessment_at: OptionalNullable[RFC3339DateTime]`, `activated_at`, `created_at`, `canceled_at`,
  `customer: Optional[Customer]`, `product: Optional[Product]`, `reference: OptionalNullable[str]` ("the
  reference value (provided by your app) for the subscription itself") — used to verify a reconciled lookup.
- `ProductResponse.product: Product` (required). `Product`: `id`, `name`, `handle: OptionalNullable[str]`,
  `description: OptionalNullable[str]`, `price_in_cents: Optional[int]`, `interval: Optional[int]`,
  `interval_unit: Optional[IntervalUnitOrStr]` (`day`, `month`, open), `archived_at`, `require_credit_card`,
  `trial_price_in_cents`, `trial_interval`, `initial_charge_in_cents`, `expiration_interval[_unit]`.
  Products without a handle, or archived, are not offered.
- `ErrorListResponse1.errors: list[str]` (required). `CustomerErrorResponse1.errors: Optional[Errors1]`,
  `Errors1 = CustomerError | list[str]`, `CustomerError.customer: Optional[str]`.
- Dates are `datetime` (RFC3339 converter); emit ISO-8601. Never hand `UNSET` to `JsonResponse` — map to `None`.

### Status mapping — `SubscriptionState` (`models/enums/subscription_state.py`), open enum

| Members | Our outcome | Holds claim? | POST answer |
|---|---|---|---|
| `active`, `trialing` | `active` (done) | yes | 201 |
| `pending`, `assessing`, `awaiting_signup` | `pending` | yes | 202 |
| `past_due`, `soft_failure`, `unpaid`, `paused`, `on_hold`, `suspended` | `attention` (exists, not in good standing) | yes | 202 |
| `failed_to_create` | `failed` | no | 502 (provider refused signup) |
| `canceled`, `expired`, `trial_ended` | `ended` | no | n/a (claim released; may resubscribe) |
| anything else / unset | `unknown` | yes | 202, never "done" |

### Error boundary (one place, `maxio.py`) → our `ProviderError` carrying `status_code`, `outcome_unknown`

`ApiError` 401/403 → 502 (our credentials); 429 → 503; typed arm with 400/404/409/422 → caller's (422 with
provider messages); other → 502. `ValidationError`/`ValueError` on decode → 502 `outcome_unknown=True` for
writes (reconcile). `httpx.ConnectError/ConnectTimeout/PoolTimeout/ProxyError` → 502 never sent (known).
Other `httpx.RequestError` → 504 `outcome_unknown=True`. `str(e)` never shown to callers; logged detail is
status + provider error text, never credentials.

### CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| `planHandle` accepted by POST must be a handle `GET /api/subscription-plans` returns (non-archived product of `MAXIO_DEFAULT_PRODUCT_FAMILY`) | `create_subscription.product_handle` ← `list_products_for_product_family` | `services.subscribe` validates against the (60 s cached) plan list; unknown → 400 |
| `customer_id` sent to create_subscription must be the one ensured for this user | `create_subscription.customer_id` ← `create_customer` / `read_customer_by_reference` | `services.ensure_customer` → `BillingCustomer.maxio_customer_id` |
| `GET /api/subscriptions/<id>` only for subscriptions whose `customer.id` is the caller's | `read_subscription` ← `create_customer` | view checks ownership; mismatch → 404 |
| `find_subscription(reference)` uses exactly the reference sent on create | `find_subscription` ← `create_subscription.reference` | claim row stores reference before the call |
| echoed plan/price must equal what was asked | `create_subscription` response ← plan list | `product.handle == planHandle` and `product_price_in_cents == plan.price_in_cents`, else `needs_review` |

## Host constraints found during implementation

- `sandbox/settings.py` sets `ATOMIC_REQUESTS: True`. The API views opt out (`transaction.non_atomic_requests`)
  so claim rows commit before Maxio is called and are never rolled back after Maxio acted.
- runserver calls `django.setup()` twice and `LOGGING` has `disable_existing_loggers: True`, which silences
  loggers imported in between; `apps.subscriptions` is named in `LOGGING` so its logs survive.

## Idempotency design

- **Customer**: `BillingCustomer` (OneToOne user, unique `reference`). `get_or_create` row first (unique
  constraint decides), then: lookup by reference (404 → absent) → create with reference → on 422 or
  unknown outcome, look up by reference again. Concurrent callers both converge on the one customer
  (Maxio lookup by the same reference); store `maxio_customer_id`.
- **Subscription**: `SubscriptionRequest` row with partial unique constraint
  `UNIQUE(user, plan_handle) WHERE status IN (sending, pending, active, attention, unknown, needs_review)`,
  `reference = uuid4 hex` generated at claim time. Loser of the claim never calls create: answers from the
  held row (sending & fresh → 202 in progress; sending stale / unknown → reconcile via find_subscription;
  active/pending/attention → refresh from Maxio; if refreshed state is `ended`, release and re-claim once).
- Writes: create never retried; on unknown outcome → `find_subscription(reference)` → settle; not found →
  row `unknown`, `504` with `reference` (a later POST reconciles under the same reference).

## Assumptions & Blockers

- No blockers. Minor: `MAXIO_ENVIRONMENT=US` maps to SDK `"us"`. Plans are the family's non-archived
  products with a handle. One live subscription per (user, plan); different plans allowed. Price currency is
  not exposed on `Product`; responses carry cents + decimal string without a currency code.

## REQUIRED READING (all loaded before implementation)

- MUST load `python-client-initialization` — module-global lazy client, close obligation, fork safety.
- MUST load `python-authentication` — basic auth, settings default empty + factory check, test placeholders.
- MUST load `python-calling-endpoints` — status-from-provider mapping, returned id ≠ success.
- MUST load `python-models` — `UNSET` vs `None`, open enums, dates, never pass `UNSET` to JSON.
- MUST load `python-error-handling` — ladder, transport split, decode failures.
- MUST load `python-configuration-resilience` — claim/call/reconcile/verify/settle, bounded paging, read retries.
- MUST load `python-testing` — StubTransport via `custom_http_client`, failure-kind tests.
