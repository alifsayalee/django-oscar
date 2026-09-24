# Maxio Advanced Billing — subscription billing for the Oscar sandbox

## Scope

Additive, parallel subscription capability on the runnable sandbox (`sandbox/`), exposed as a JSON
HTTP API under `/api/`. Maxio Advanced Billing is the billing system of record. The existing
catalogue → basket → checkout flow is untouched.

| Endpoint | Action |
| --- | --- |
| `GET  /api/subscription-plans` | List plans (products) in the configured product family; each carries `planHandle` |
| `POST /api/billing-customer` | Ensure (idempotently) a Maxio customer exists for the logged-in user |
| `POST /api/subscriptions` | Subscribe the logged-in user to a plan (`{"planHandle": ...}`); returns top-level `subscriptionId` |
| `GET  /api/my-subscriptions` | The logged-in user's subscriptions, read from Maxio |
| `GET  /api/my-subscriptions/<id>` | One of the user's subscriptions, read from Maxio (ownership enforced) |

Auth: Django session login (Oscar's `/accounts/login/`), identity from `request.user`. Unauthenticated → 401
JSON. CSRF stays on for POSTs (session auth).

## Repo survey (pattern → exemplar)

- Sandbox-local apps live in `sandbox/apps/` as plain packages (`sandbox/apps/user/`); imported as `apps.<name>`
  because `sandbox/` is on `sys.path` (`sandbox/urls.py` does `from apps.sitemaps import ...`).
- Top-level URLs: `sandbox/urls.py` uses `path(...)` + `include(...)`; non-i18n routes go in the first
  `urlpatterns` list (exemplar: `sitemap.xml` entries). `/api/` goes there (no language prefix).
- Settings: `sandbox/settings.py` reads env through `django-environ` (`env = environ.Env()`, `env.str/bool/int`).
- `DATABASES['default']['ATOMIC_REQUESTS'] = True` — every view is one transaction unless opted out.
- Host is **sync** Django under WSGI → **sync** SDK client.
- Django app config exemplar: `src/oscar/apps/*/apps.py`; tests: Django `TestCase` style is used by the
  repo's `tests/` (pytest + pytest-django available). The new app's tests run with
  `sandbox/manage.py test apps.subscriptions` (sandbox settings).
- Lint config: flake8 max-line-length 119 (`setup.cfg`). No mypy config in the repo → run
  `mypy --strict` on the new app's integration module(s) (installed into the venv).

## Toolchain

- `py -3.11 -m venv venv`, `venv\Scripts\pip install -e .[test]` (done).
- SDK: `pip install "maxio-advanced-billing @ git+https://github.com/context-plugins/maxio-python-sdk.git@main"`.
  **Finding:** the installed metadata carries no `Requires-Dist` (pip 24 + this build), so its runtime deps
  must be installed explicitly: `httpx>=0.28.1,<1`, `pydantic[email]>=2.11,<3`, `typing-extensions>=4.13,<5`.
  Recorded in `sandbox/requirements-billing.txt`.
- Baseline: `manage.py check` clean (only pre-existing `templates.W003`).

## Environment / credentials

Settings (in `sandbox/settings.py`, read from env, no values in repo): `MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`,
`MAXIO_DEFAULT_PRODUCT_FAMILY`, `MAXIO_BASE_URL` (optional verbatim override), plus `MAXIO_ENVIRONMENT`
(env var given as `US`; lower-cased, must be `us` or `eu` — unknown value raises `ImproperlyConfigured`,
never silently falls back), `MAXIO_TIMEOUT` (default 15 s), `MAXIO_CUSTOMER_REFERENCE_PREFIX`.

Host: `environment="us"` → `production` server `https://{site}.chargify.com`, `site` set from
`MAXIO_SITE_SUBDOMAIN` via `server_config={"production": {"us": {"site": ...}}}`. With `MAXIO_BASE_URL` set:
`server_config={"production": {<env>: {"base_url": MAXIO_BASE_URL}}}` (no `{site}` placeholder → used verbatim).
Omitting `environment` is silently `"us"` — always pass it.

Read-only smoke (scratchpad, real key): `list_products_for_product_family("handle:eshop-subscribe")` → 2 products
(`basic-plan` 2900¢/1 month, `eshop-pro` 29900¢/1 month; IDs already differ from the task table → address by
handle only). `read_customer_by_reference(<unknown>)` → `ApiError` 404, empty body.
`find_subscription(reference=<unknown>)` → 404. `list_subscriptions(per_page=1)` → 200.

## Contract sheet

Source: `sdk-map.md`, `map/operations/{product_families,customers,subscriptions}.md`, models under
`maxio_advanced_billing/models/`, docstrings in `maxio_advanced_billing/apis/`.

**Client:** `MaxioAdvancedBillingClient` (sync). Keyword-only ctor: `environment`, `timeout`, `server_config`,
`basic_auth`. `basic_auth=BasicAuthCredentials(username=<api key>, password="x")` (README: `curl -u <api_key>:x`).
Omitting `basic_auth` → unauthenticated, silently; factory refuses to build without a key.
One process-wide client, built lazily (post-fork), thread-safe; closed via `atexit`. `close()` obligation.

**Invariants (all ops below):** positional args are the listed ones; everything after `*` keyword-only with a
real default (no defensive `None`s). All take `request_options`. Raw peer via `.with_raw_response`. No retries in SDK.
Decode failure → `pydantic.ValidationError`/`ValueError` (not `ApiError`, both modes). Transport errors = raw `httpx`.
Server `production`, auth `basic_auth OR bearer_auth`.

| Operation | Signature (positional · kw-only used) | Returns | `ApiError.error` union |
| --- | --- | --- | --- |
| `product_families.list_products_for_product_family` | `(product_family_id: str, *, page=1, per_page=20, ...)`; id or `"handle:<h>"` | `list[ProductResponse]` | `str` [404] · `RawError` |
| `customers.read_customer_by_reference` | `(reference: str)` | `CustomerResponse` | `RawError` (Case B); miss = 404 |
| `customers.create_customer` | `(*, body: CreateCustomerRequest \| ...Dict)` | `CustomerResponse` | `CustomerErrorResponse1` [422] · `RawError` |
| `customers.list_customer_subscriptions` | `(customer_id: int)` | `list[SubscriptionResponse]` | `RawError` (Case B) |
| `subscriptions.create_subscription` | `(*, body: CreateSubscriptionRequest \| ...Dict)` | `SubscriptionResponse` | `ErrorListResponse1` [422] · `RawError` |
| `subscriptions.find_subscription` | `(*, reference: str)` | `SubscriptionResponse` | `RawError` [404, other] |
| `subscriptions.read_subscription` | `(subscription_id: int)` | `SubscriptionResponse` | `RawError` (Case B) |

None of these return `None`.

**Models (no wire aliases on any of these — `grep alias=` = 0):**

- `CreateCustomerRequest(customer: CreateCustomer)` — required.
- `CreateCustomer`: required `first_name: str`, `last_name: str`, `email: str`; set `reference: Optional[str]`
  (UNSET default) — **always set**. Docstring: reference must
  be unique per site ("you may only create one customer for a given reference value"). Reference =
  `BillingAccount.reference` (`MAXIO_CUSTOMER_REFERENCE_PREFIX` + uuid4 hex), not derived from the user pk.
- `CreateSubscriptionRequest(subscription: CreateSubscription)` — required.
- `CreateSubscription`: all `Optional[...]=UNSET`. Set `product_handle: str`, `customer_reference: str`
  (docstring: required unless `customer_id`/`customer_attributes`), `reference: str` — **always set**, per
  enrollment, stored locally before the call. Also set `payment_collection_method: Optional[CollectionMethodOrStr]`
  from `MAXIO_PAYMENT_COLLECTION_METHOD` (default `remittance`). `CollectionMethod` = automatic, remittance, prepaid,
  invoice (docstring: Relationship Invoicing → remittance/automatic/prepaid; legacy Statements → invoice/automatic).
  **Live finding:** with the default (automatic) Maxio answers 422 "No payment method was on file for the $299.00
  balance" although the product has `require_credit_card=false`; the API captures no card, so it bills by invoice. Provider uniqueness of subscription `reference`: **UNVERIFIED**
  (not stated anywhere in the SDK docs) → idempotency is enforced locally (see design), not assumed from Maxio.
- `CustomerResponse(customer: Customer)` required. `Customer.id: Optional[int]` — assert set after read/create.
- `SubscriptionResponse(subscription: Optional[Subscription])` — **may be UNSET → guard**.
  `Subscription`: `id: Optional[int]` (assert on create), `state: Optional[SubscriptionStateOrStr]`,
  `next_assessment_at: OptionalNullable[RFC3339DateTime]`, `current_period_ends_at: OptionalNullable[...]`,
  `created_at: Optional[...]`, `product_price_in_cents: Optional[int]`, `currency: Optional[str]`,
  `reference: OptionalNullable[str]`, `customer: Optional[Customer]`, `product: Optional[Product]`.
- `ProductResponse(product: Product)` required. `Product`: `id`, `name`, `handle` (OptionalNullable),
  `description` (OptionalNullable), `price_in_cents`, `interval`, `interval_unit: Optional[IntervalUnitOrStr]`,
  `archived_at`, `require_credit_card`.
- Enums (open; unknown value arrives as plain `str`): `SubscriptionState` = pending, failed_to_create, trialing,
  assessing, active, soft_failure, past_due, suspended, canceled, expired, paused, unpaid, trial_ended, on_hold,
  awaiting_signup. `IntervalUnit` = day, month. Output via `str(member)` = wire value (str enums).
- `ErrorListResponse1.errors: list[str]` (required). `CustomerErrorResponse1.errors: Optional[Errors1]`, where
  `Errors1 = CustomerError | list[str]` (`models/unions/errors1.py`) and `CustomerError.customer: Optional[str]`.
- `UNSET` from `maxio_advanced_billing.core`; narrow with `isinstance(x, UnsetType)`; never hand `UNSET` to
  `JsonResponse` — map into own dicts.

## Design

- App `sandbox/apps/subscriptions/`: `maxio.py` (client factory), `gateway.py` (all SDK calls + error
  translation → own `BillingError` types), `services.py` (customer ensure + subscribe orchestration),
  `views.py` (JSON), `urls.py`, `models.py` (`SubscriptionEnrollment`), migration, `tests.py`.
- Reuse Oscar's user model (`AUTH_USER_MODEL`); no parallel product/customer tables. Plans come from Maxio.
- `SubscriptionEnrollment` = local idempotency ledger only (user FK, plan_handle, reference unique, status
  pending/active/unknown/failed/ended, maxio ids).
- `BillingAccount` (user 1:1, random unique customer reference, cached Maxio customer id). **Live finding:** a
  reference derived from the user pk collided with another install's customer on the shared Maxio site, so the
  reference is random per user and stored locally. Partial unique constraint: one *open* (pending/active/unknown)
  enrollment per (user, plan_handle) → a double-click's second insert hits `IntegrityError` → returns the
  first's result (or 409 while still in flight).
- Subscribe flow (view is `non_atomic_requests` so the pending row commits before the provider call):
  1. validate `planHandle` against the family's plans (cached 5 min);
  2. existing open enrollment → reconcile: active → re-read and return 200; pending/unknown → `find_subscription(reference)`;
     found → mark active, 200; not found → stays unknown (409, `outcomeUnknown`) until it is older than the
     reconciliation window (10 min ≫ timeout), then marked failed and a new enrollment is allowed;
     active but Maxio state canceled/expired → marked ended, new enrollment allowed;
  3. ensure customer (lookup by reference → 404 → create with reference → on 422 re-lookup);
  4. create subscription with the enrollment's reference; success → active + 201;
     rejected (4xx) → failed; unknown outcome (5xx / read timeout / unreadable 2xx) → lookup by reference, else unknown.
- Error boundary (per python-error-handling): 400/404/409/422 → caller's (422 with provider messages);
  401/403 → 502; 429 → 503; 5xx/unmapped → 502; never-sent transport (ConnectError, ConnectTimeout, PoolTimeout,
  ProxyError) → 502 `outcomeUnknown=false`; other `httpx.RequestError` → 504 `outcomeUnknown=true`;
  `ValidationError` on 2xx → 502 unknown. Never surface `str(e)`; log status + detail.
- Retries: none on writes (reconciled by reference instead). Reads: one retry only for never-sent transport
  failures and 502/503/504 (bounded: 2 × 15 s timeout).
- Logging: a `LoggingTransport` wrapping `HttpxClient` logs method, URL path, status, latency (no headers/bodies).

## Assumptions & Blockers

- No blockers. All needed capabilities are in the SDK.
- Minor: subscription `reference` uniqueness at Maxio is unverified → handled locally (above).
- Decision: no card is captured, so subscriptions use `remittance` (invoice) collection — configurable.

## REQUIRED READING

- Client construction/lifetime — MUST load `python-client-initialization` (loaded).
- Credentials — MUST load `python-authentication` (loaded).
- Calls, kw-only boundary, response modes — MUST load `python-calling-endpoints` (loaded).
- UNSET / open enums / Dict companions — MUST load `python-models` (loaded).
- Error boundary — MUST load `python-error-handling` (loaded).
- Writes, timeouts, no-retry, reconciliation — MUST load `python-configuration-resilience` (loaded).
- Tests (stub transport) — MUST load `python-testing` (loaded).
