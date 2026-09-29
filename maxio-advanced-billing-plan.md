# Maxio Advanced Billing — subscription billing for the Oscar sandbox

## Scope

An additive subscription capability on the runnable sandbox site (`sandbox/`), with Maxio Advanced
Billing as the billing system of record. The existing basket/checkout flow is untouched.

| Endpoint | Action |
| --- | --- |
| `GET /api/subscription-plans` | List the plans in the configured product family (each carries `planHandle`) |
| `POST /api/billing-customer` | Ensure a Maxio customer exists for the logged-in user (idempotent) |
| `POST /api/subscriptions` | Subscribe the logged-in user to `{"planHandle": ...}`; returns top-level `subscriptionId` |
| `GET /api/my-subscriptions` | The user's subscriptions, read through from Maxio (local snapshot fallback) |
| `GET /api/subscriptions/<id>` | One of the user's subscriptions, read from Maxio |

Auth: Django session login (the sandbox's own `/accounts/login/`); identity = `request.user`. POSTs
are CSRF-protected by the existing `CsrfViewMiddleware`; the GET endpoints set the `csrftoken` cookie.
Unauthenticated → `401` JSON (no redirect).

## Repo survey (conventions to follow)

| Convention | Exemplar |
| --- | --- |
| Sandbox-local code lives under `sandbox/apps/`, imported as `apps.<name>` (sandbox dir is on `sys.path`) | `sandbox/apps/sitemaps.py`, import in `sandbox/urls.py` |
| Settings read via `django-environ` `env.*(…, default=…)` | `sandbox/settings.py` (`DEBUG`, `SECRET_KEY`, `THUMBNAIL_REDIS_URL`) |
| URLs: plain `path()` list in `sandbox/urls.py`; non-i18n routes sit outside `i18n_patterns` | `sandbox/urls.py` (`sitemap.xml`) |
| User model via `oscar.core.compat.AUTH_USER_MODEL` | `sandbox/apps/user/models.py` |
| Django app config classes (`apps.py`, `label`) | `src/oscar/apps/wishlists/apps.py` |
| `DATABASES['default']['ATOMIC_REQUESTS'] = True` — views needing their own commits must opt out (`transaction.non_atomic_requests`) | `sandbox/settings.py` |
| Host is **sync** Django under WSGI → **sync** SDK client | `sandbox/wsgi.py` |

Toolchain: `pip` + `venv` (`venv\Scripts\python`), Python 3.11. `maxio-advanced-billing` 1.0 installed
from the plugin copy (non-editable) + `mypy`, `django-stubs` for type checking (repo has no mypy
config → `mypy --strict` on the new files). Tests: Django's runner from `sandbox/`
(`python manage.py test apps.subscriptions`), SQLite. Baseline: `manage.py check` clean apart from the
pre-existing `templates.W003` warning; sandbox DB builds (note: the CSV `oscar_import_catalogue` step
IS required here — `orders.json` references product 12, which only the CSV import creates).

Reuse of Oscar models: the user is Oscar's configured `AUTH_USER_MODEL`; plans are not mirrored into
Oscar's catalogue (Maxio is the source of truth for plans), and no parallel user/customer model is
created — `BillingCustomer` is a 1:1 link table from the Oscar user to the Maxio customer id.

## Credentials / environment

Settings in `sandbox/settings.py` (names mandated), read from env via `env.str`, no values committed:
`MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`, `MAXIO_DEFAULT_PRODUCT_FAMILY`, `MAXIO_BASE_URL` (optional),
plus `MAXIO_ENVIRONMENT` (env var provided; default `us`) and `MAXIO_TIMEOUT` (default `15`).

- `environment=` is always passed explicitly: `MAXIO_ENVIRONMENT.lower()` validated against
  `{"us", "eu"}` (`Environment` literal, `maxio_advanced_billing/server/environment.py`); unknown →
  `ImproperlyConfigured`. Omitting it would silently be `"us"`.
- `server_config={"production": {env: {"site": MAXIO_SITE_SUBDOMAIN}}}`; when `MAXIO_BASE_URL` is set →
  `{"production": {env: {"base_url": MAXIO_BASE_URL}}}` verbatim (`ProductionUsConfig`/`ProductionEuConfig`:
  `base_url`, `site` — `server/server_config.py`).
- Auth: `basic_auth=BasicAuthCredentials(username=MAXIO_API_KEY, password="x")` — `<api_key>:x` per the
  SDK's `api-reference.md` (Invoices PDF retrieval sample). Missing key/subdomain/family →
  `ImproperlyConfigured` before any call (never send unauthenticated).
- Smoke (read-only, real credential, scratchpad): `list_products_for_product_family("handle:eshop-subscribe")`
  → 2 products (`basic-plan` 2900¢/1 month, `eshop-pro` 29900¢/1 month, `require_credit_card=False`);
  `read_customer_by_reference(<unknown>)` → `ApiError` 404, `RawError`, empty body;
  `read_product_by_handle`, `list_subscriptions` → 200. No 403s. Product IDs already differ from the
  task table → handles only.

## Contract sheet

Sync client: `MaxioAdvancedBillingClient` (from `maxio_advanced_billing`). Held as a lazily-built
module-level singleton (thread-safe init, built after fork on first use), closed via `atexit`.
Constructor keyword-only: `environment`, `timeout`, `server_config`, `retry_options`,
`custom_http_client`, `basic_auth`. `custom_http_client` = a logging wrapper around
`HttpxClient(timeout=MAXIO_TIMEOUT)` (`core`, keyword-only `timeout`, `proxy_url`, `verify`) — so the
timeout is set on that transport (client `timeout=` doesn't reach a custom transport). Wrapper logs
method, URL path, status, elapsed ms — never headers/bodies.

Keyword-only boundary: all params after `*` have real defaults; never pass defensive `None`s.
Async peers exist but are not used (sync host). None of the in-scope operations return `None`.

| Operation | Signature (sync, parsed) | Returns | `ApiError.error` |
| --- | --- | --- | --- |
| `client.product_families.list_products_for_product_family` | `(product_family_id: str, *, page=1, per_page=20, …, include_archived: bool \| None = None, …, request_options=None)` — `product_family_id` = id or `"handle:<handle>"` (docstring) | `list[ProductResponse]` | `str` [404] \| `RawError` |
| `client.customers.read_customer_by_reference` | `(reference: str, *, request_options=None)` | `CustomerResponse` | `RawError` only (Case B); miss = 404 |
| `client.customers.create_customer` | `(*, body: CreateCustomerRequest \| CreateCustomerRequestDict \| None = None, request_options=None)` | `CustomerResponse` | `CustomerErrorResponse1` [422] \| `RawError` |
| `client.customers.list_customer_subscriptions` | `(customer_id: int, *, request_options=None)` | `list[SubscriptionResponse]` | `RawError` only |
| `client.subscriptions.create_subscription` | `(*, body: CreateSubscriptionRequest \| CreateSubscriptionRequestDict \| None = None, request_options=None)` | `SubscriptionResponse` | `ErrorListResponse1` [422] \| `RawError` |
| `client.subscriptions.read_subscription` | `(subscription_id: int, *, include=None, request_options=None)` | `SubscriptionResponse` | `RawError` only |

Models (members this integration sets/reads; wire name = Python name for all of them):

- `CreateCustomerRequest(customer: CreateCustomer)` — `CreateCustomer`: **required** `first_name: str`,
  `last_name: str`, `email: str`; `reference: Optional[str] = UNSET` (set to the claim's stored `oscar-cus-<hex>`).
- `CreateSubscriptionRequest(subscription: CreateSubscription)` — `CreateSubscription` all `Optional`/`UNSET`:
  set `product_handle: str`, `customer_id: int`, `reference: str` (the claim's stored `oscar-sub-<hex>`),
  `payment_collection_method: CollectionMethodOrStr = CollectionMethod.REMITTANCE` (`automatic`, `remittance`,
  `prepaid`, `invoice`; docstring: `remittance`/`automatic`/`prepaid` valid under Relationship Invoicing).
  No payment profile. Defaulted members `defer_signup=False`, `dunning_communication_delay_enabled=False`
  are always serialized (real model defaults).
- `CustomerResponse.customer: Customer` (required) — `Customer.id: Optional[int]`, `reference: OptionalNullable[str]`.
- `SubscriptionResponse.subscription: Optional[Subscription]` — **not required**: guard `UNSET`.
  `Subscription`: `id: Optional[int]`, `state: Optional[SubscriptionStateOrStr]`,
  `current_period_ends_at` / `next_assessment_at` / `activated_at`: `OptionalNullable[RFC3339DateTime]`,
  `created_at: Optional[RFC3339DateTime]`, `product_price_in_cents: Optional[int]`,
  `currency: Optional[str]`, `reference: OptionalNullable[str]`, `customer: Optional[Customer]`,
  `product: Optional[Product]`.
- `ProductResponse.product: Product` (required) — `Product`: `id`, `name: Optional[str]`,
  `handle: OptionalNullable[str]`, `description: OptionalNullable[str]`, `price_in_cents: Optional[int]`,
  `interval: Optional[int]`, `interval_unit: Optional[IntervalUnitOrStr]` (`day`, `month`, open),
  `require_credit_card: Optional[bool]`, `archived_at: OptionalNullable[RFC3339DateTime]`,
  `trial_price_in_cents`/`trial_interval`: `OptionalNullable[int]`, `trial_interval_unit`,
  `initial_charge_in_cents: OptionalNullable[int]`.
- `ErrorListResponse1.errors: list[str]` (required). `CustomerErrorResponse1.errors: Optional[ErrorsModel]`
  (`per_page`, `price_point` lists) — a 422 whose body doesn't fit raises `ValidationError` (decode
  failure of an *error* body = rejection with lost detail, not an outage).
- `SubscriptionState` (open, `models/enums/subscription_state.py`): `pending`, `failed_to_create`,
  `trialing`, `assessing`, `active`, `soft_failure`, `past_due`, `suspended`, `canceled`, `expired`,
  `paused`, `unpaid`, `trial_ended`, `on_hold`, `awaiting_signup`. Terminal for "live" purposes:
  `canceled`, `expired`, `failed_to_create`. `str(member)` = wire value. Unknown values pass as `str`.
- `Optional[T]` here is `T | UnsetType` (not `typing.Optional`); narrow with `isinstance(v, UnsetType)`
  before handing values out; never pass `None` to an `Optional[...]` member.

Assert-after-call (outcome unknown if missing): `create_customer` → `customer.id`;
`create_subscription` → `subscription` and `subscription.id`.

Errors (`core`: `ApiError`, `RawError`; `pydantic.ValidationError`; `httpx`) — one translation point
`apps/subscriptions/errors.py` → `BillingError(status, code, message, outcome_unknown)`:
- `ApiError` 401/403 → 502 (our credentials); 429 → 503; typed/untyped 4xx on a write → 422/400 with
  provider messages (`ErrorListResponse1.errors`); 404 on lookups handled at call site; 5xx → 502,
  `outcome_unknown=True`.
- `ValidationError` on a 2xx → 502 `outcome_unknown=True`; on a non-2xx error body (raised inside the
  error mapper) → treated as rejection — distinguished by where it's raised: we only get it from the
  error path for `create_customer`, handled as "rejected, detail lost".
- `httpx.ConnectError | ConnectTimeout | PoolTimeout | ProxyError` → 502 never sent (`outcome_unknown=False`);
  other `httpx.RequestError` → 504 `outcome_unknown=True`.
- Logging of error details (status, `RawError.text()` truncated), never `str(e)` to callers.

Retries: on by default; tuned to `RetryOptions(max_retries=2, initial_delay=0.5, max_delay=4.0)` —
GET/PUT only (default method set), so POSTs (`create_customer`, `create_subscription`) are **never**
resent by the SDK; a POST with an unknown outcome is reconciled by re-reading (below), not retried.

## DUPLICATE CLAIMS

| Write | Where the claim is stored | What rejects the second one | Where that rejection is caught | Where in the code |
| --- | --- | --- | --- | --- |
| Create Maxio customer for a user | `subscriptions_billingcustomer` row (SQLite/DB), `user` OneToOne, `status=pending` inserted and committed before any SDK call | DB unique constraint on `user_id` (OneToOne) → `IntegrityError`; stale-pending takeover by conditional `UPDATE … WHERE claimed_at = <seen>` (row count 0 = refused) | `except IntegrityError` in the claim function; row-count check on takeover | `services._claim_customer` → `services._create_or_adopt_customer` |
| Create Maxio subscription for a user + product family | `subscriptions_subscription` row, `claim_state=pending`, `is_live=True`, committed before the SDK call; Maxio `reference` (random `oscar-sub-<hex>`, generated with the claim) stored beside it | Partial unique constraint `(user, product_family) WHERE is_live` → `IntegrityError`; stale/unknown takeover by conditional `UPDATE` | `except IntegrityError` in the claim function | `services._claim_subscription` → `services._create_subscription_in_maxio` |

Order per row: claim → SDK call → record. Claim released (customer row deleted / subscription
`is_live=False, claim_state=released`) when Maxio definitively refuses or the request was never sent.
Unknown outcome → claim stays pending with `outcome_unknown=True`; the next attempt reconciles by
re-reading (`read_customer_by_reference` / `list_customer_subscriptions` matched on `reference`)
before any new create.

## Build order

1. `sandbox/apps/subscriptions/` app: `apps.py`, `models.py` (+ migration), `admin.py`.
2. `maxio.py` client factory + logging transport; settings in `sandbox/settings.py`; `INSTALLED_APPS`.
3. `errors.py`, `services.py` (plans, customer, subscribe, read-back), `views.py`, `urls.py`; include in `sandbox/urls.py`.
4. `tests.py` (stub transport seam, Django `TestCase`).
5. `mypy --strict` on the app, `manage.py test apps.subscriptions`, live E2E via runserver on port block.

## Revisions after the live run (2026-09-29)

- **Collection method.** Live `create_subscription` with no payment profile was refused (422,
  `"No payment method was on file for the $299.00 balance"`) even though the plan does not require a
  card: Maxio attempts an automatic charge at signup. Fix: send `payment_collection_method=remittance`
  (invoice billing). A plan whose `require_credit_card` is true is refused with `422
  payment_method_required` before any write, since this API captures no card.
- **References.** `oscar-user-<pk>` collided with a customer another install had created on the shared
  Maxio site (lookup-by-reference adopted it). Customer and subscription references are now random
  (`oscar-cus-<hex>`, `oscar-sub-<hex>`), generated when the claim row is inserted and committed before the
  create — every retry and reconciliation reuses the stored value, so recovery by reference still works.

## Assumptions & Blockers

- Minor: one live subscription per user per product family (a second POST for the same plan returns the
  existing one — idempotent replay; for a different plan → `409`). Plan changes/cancellation are out of scope.
- Minor: plan list cached 60 s in Django's cache (locmem) to avoid a Maxio round-trip per request.
- Minor: Maxio customer name falls back to email local-part / username when the Oscar user has no
  first/last name (the seeded users have none); an account without an email → `400`.
- No blockers. The DB (the one the app already uses) can hold claims beyond a process.

## REQUIRED READING

- Error boundary, decode failures, transport split — MUST load `python-error-handling` (loaded)
- Client construction, lifetime, WSGI placement — MUST load `python-client-initialization` (loaded)
- Base URL/server_config, retries, timeouts, logging transport, duplicate claims — MUST load `python-configuration-resilience` (loaded)
- Basic auth, missing credentials are silent — MUST load `python-authentication` (loaded)
- Call shapes, keyword-only tail, raw vs parsed — MUST load `python-calling-endpoints` (loaded)
- `UNSET`, open enums, handing values out — MUST load `python-models` (loaded)
- Stub transport tests — MUST load `python-testing` (loaded)
