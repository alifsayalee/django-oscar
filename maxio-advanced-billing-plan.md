# Maxio Advanced Billing — integration plan (django-oscar sandbox)

## Goal

Add recurring-subscription billing to the django-oscar sandbox site (`sandbox/`) as a new,
additive Django app, with **Maxio Advanced Billing** as the system of record. Three HTTP
endpoints under `/api/`, session-authenticated, identity taken from `request.user`:

- `GET  /api/subscription-plans`  — list available plans; each entry carries `planHandle`.
- `POST /api/subscriptions`       — subscribe the caller to a plan; returns `subscriptionId` top-level.
- `GET  /api/my-subscriptions`    — list the caller's subscriptions.

## Repo survey (conventions + exemplars)

- **Sync app.** Standard Django WSGI site (`sandbox/wsgi.py`). → **Use the sync SDK client** `MaxioAdvancedBillingClient`. Never the async one.
- App layout: apps live under `sandbox/apps/` (e.g. `apps/user/`, `apps/offers.py`). Exemplar: `sandbox/apps/user/` (has `__init__.py`, `models.py`). Apps referenced by dotted path `apps.<name>` (see `INSTALLED_APPS` and `sandbox/urls.py`).
- URL wiring: `sandbox/urls.py` builds `urlpatterns`; Oscar's own URLs are mounted under `i18n_patterns`. Our `/api/` routes must sit **outside** `i18n_patterns` (no language prefix) — add to the top-level `urlpatterns`.
- Settings: `sandbox/settings.py` uses `django-environ` (`env = environ.Env(...)`); read new settings through `env(...)` with safe defaults. `DEBUG` on by default. DB is SQLite beside the project.
- Auth: session login; `AUTHENTICATION_BACKENDS` includes Oscar's `EmailBackend`. Seeded users from `auth.json`: `superuser`, `staff`.
- Tests: pytest-django present, but the oscar `tests/` tree uses its own settings and won't see a sandbox app. → put app tests in `sandbox/apps/subscriptions/tests.py` and run via `sandbox/manage.py test apps.subscriptions` (Django runner, sandbox settings). SDK calls are faked at the transport seam (see python-testing).

## Toolchain

- venv at `repo/venv` (py -3.11). Installed: `-e .[test]`, plus SDK `maxio-advanced-billing==1.0` from git, plus its runtime deps (`httpx`, `pydantic[email]`, `typing-extensions` — these did NOT come transitively from the git install and were added explicitly), plus `mypy`.
- Type check: `venv/Scripts/mypy --strict <files>` (SDK ships `py.typed`, generated under mypy --strict).
- Tests: `sandbox/manage.py test apps.subscriptions`.

## Credentials / environment (verified)

- Settings read at runtime from env, **values never written to the repo**:
  `MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`, `MAXIO_DEFAULT_PRODUCT_FAMILY`, `MAXIO_BASE_URL` (optional override). `MAXIO_ENVIRONMENT` read optionally, lower-cased, default `us`.
- **Auth scheme (from SDK api-reference.md line 8175, `curl -u <api_key>:x`)**: HTTP Basic, `username=<MAXIO_API_KEY>`, `password="x"`. Set via `basic_auth={"username": key, "password": "x"}`.
- **Environment**: `"us"` (default; omitting is silently `"us"`). `MAXIO_ENVIRONMENT` env is `US`.
- **Base URL / server_config**: production/us template is `https://{site}.chargify.com`, `{site}` defaults to the literal `"subdomain"` — MUST override. When `MAXIO_BASE_URL` set → `server_config={"production":{"us":{"base_url": MAXIO_BASE_URL}}}`; else → `server_config={"production":{"us":{"site": MAXIO_SITE_SUBDOMAIN}}}`.
- **Smoke run (read-only, real sandbox) confirmed**: family `eshop-subscribe` id=3026728; products `eshop-pro`($299,29900c) `basic-plan`($29,2900c), both `require_credit_card=False`; customer lookup of a missing reference → `ApiError` status 404, `.error` is `RawError`. IDs differ from the task table → resolve family/plan by **handle**, never a hard-coded id.

## Architecture (new app `sandbox/apps/subscriptions/`)

No local DB models (reuse Oscar/`request.user`; Maxio is the system of record). Files:

- `__init__.py`
- `apps.py` — `SubscriptionsConfig` (name `apps.subscriptions`).
- `client.py` — module-scoped, lazily-built **singleton** `MaxioAdvancedBillingClient` from Django settings, guarded by a `threading.Lock` (client owns an httpx pool; must be long-lived, not per-request; sync client is thread-safe). `get_client()` + `close_client()`. Raises `ImproperlyConfigured` if key/subdomain (and no base_url) missing.
- `services.py` — business logic, the single place that talks to the SDK:
  - `list_plans()` → resolve family id by handle, `list_products_for_product_family`, map to dicts incl. `planHandle`.
  - `customer_reference(user)` → deterministic `f"eshop-user-{user.pk}"`.
  - `ensure_customer(user)` → lookup by reference; on 404 create; on create-race 422 re-lookup. Returns customer id (idempotent).
  - `find_active_subscription(customer_id, plan_handle)` → `list_customer_subscriptions`, reuse a non-terminal sub for the same product handle.
  - `subscribe(user, plan_handle)` → ensure_customer → find_active (reuse if present) → else `create_subscription`. Idempotent on double-submit.
  - `list_my_subscriptions(user)` → lookup customer; empty if none; else `list_customer_subscriptions` mapped.
  - Mapping helpers serialize Subscription/Product to JSON-safe dicts (RFC3339 datetimes → isoformat).
  - Raises a small `MaxioError`/`PlanNotFound` on failures for the view layer to translate.
- `views.py` — 3 plain Django views returning `JsonResponse`; `@login_required` (401 JSON for unauthenticated API callers instead of redirect); method routing; POST reads JSON body `{"planHandle": ...}` defaulting to `eshop-pro`. Each action separately invocable.
- `urls.py` — the three named routes.

Wire-up: add `'apps.subscriptions.apps.SubscriptionsConfig'` to `INSTALLED_APPS`; add settings block; `path('api/', include('apps.subscriptions.urls'))` in `sandbox/urls.py` top-level urlpatterns (outside i18n).

CSRF: session-auth state-changing POST keeps Django CSRF protection (production posture). Verification guide will show fetching the CSRF cookie/token. (No CSRF exemption.)

---

## CONTRACT SHEET (no open lookups)

**Client**: `MaxioAdvancedBillingClient` (sync; alias `Client`). Keyword-only ctor:
`environment="us"`, `timeout=30.0`, `server_config`, `basic_auth`, `bearer_auth`, `custom_http_client`.
Import: `from maxio_advanced_billing import MaxioAdvancedBillingClient`. `basic_auth` from `maxio_advanced_billing.core import BasicAuthCredentials` (or plain dict). **Must** `client.close()` at shutdown; hold as a long-lived singleton, never per-request. Sync and async clients do not mix.

**Response mode**: use the **parsed** call (raises `ApiError`) for every op — none of our ops return `None`, so we never need `with_raw_response`.

Operations in scope (sync parsed signatures; everything after `*` is keyword-only with a real default):

| Op | Signature (relevant) | Returns | Error case |
|---|---|---|---|
| `client.product_families.list_product_families` | `(*, date_field=…, …)` | `list[ProductFamilyResponse]` | Case B → `RawError` |
| `client.product_families.list_products_for_product_family` | `(product_family_id: str, *, page=1, per_page=20, …)` | `list[ProductResponse]` | Case A: `str`[404] · `RawError` |
| `client.customers.read_customer_by_reference` | `(reference: str, *, …)` | `CustomerResponse` | Case B → `RawError` (404 when missing) |
| `client.customers.create_customer` | `(*, body: CreateCustomerRequest\|dict\|None=None, …)` | `CustomerResponse` | Case A: `CustomerErrorResponse1`[422] · `RawError` |
| `client.customers.list_customer_subscriptions` | `(customer_id: int, *, …)` | `list[SubscriptionResponse]` | Case B → `RawError` |
| `client.subscriptions.create_subscription` | `(*, body: CreateSubscriptionRequest\|dict\|None=None, …)` | `SubscriptionResponse` | Case A: `ErrorListResponse1`[422] · `RawError` |

**Bodies (required vs UNSET; `Optional[T]` here = `T | UnsetType`, NOT `typing.Optional` — do not pass `None`):**

- `CreateCustomerRequest{ customer: CreateCustomer }`. `CreateCustomer` **required**: `first_name: str`, `last_name: str`, `email: str`. Optional we set: `reference: str` (our deterministic ref). Others omitted → provider default.
- `CreateSubscriptionRequest{ subscription: CreateSubscription }`. `CreateSubscription` all fields Optional/UNSET. We set:
  - `product_handle: str` (the plan handle) — "Required unless product_id given".
  - `customer_id: int` (existing customer, from ensure_customer) — "Required unless customer_reference/customer_attributes given".
  - `payment_collection_method = CollectionMethod.REMITTANCE`. **Verified against sandbox**: although `require_credit_card=False`, the provider default (automatic) still attempts to charge the first $299 period immediately and 422s ("No payment method was on file for the $299.00 balance"). `remittance` (invoice-based collection) activates the subscription with no card capture / 3-DS, which is what these card-free plans intend.
  - Optional `reference` on subscription: omit (idempotency handled by list-check, not reference).

**Response fields we read (JSON-safe mapping):**

- `ProductResponse.product: Product` → `id`, `name`, `handle` (→`planHandle`), `price_in_cents`, `interval`, `interval_unit`, `product_price_point_handle`, `require_credit_card`, `description`.
- `CustomerResponse.customer: Customer` → `id`, `reference`, `email`, `first_name`, `last_name`.
- `SubscriptionResponse.subscription: Optional[Subscription]` (UNSET-able → guard `is not None`). `Subscription` → `id`(→`subscriptionId`/`subscriptionId`), `state` (enum, coerce to str value), `product: Optional[Product]` (→ plan name/handle/price), `product_price_in_cents`, `current_period_ends_at` (OptionalNullable RFC3339 → **nextBillingDate**), `next_assessment_at`, `activated_at`, `created_at`, `customer`.
  - **Decode note**: a truncated 2xx body fails to decode as `ValidationError`/`ValueError` (NOT `ApiError`, bypasses both modes). Guard the error boundary for it.

**Enums:**
- `SubscriptionState` values: `pending, failed_to_create, trialing, assessing, active, soft_failure, past_due, suspended, canceled, expired, paused, unpaid, trial_ended, on_hold, awaiting_signup`. Open enum → may arrive as bare `str`; coerce via `getattr(v, "value", v)`.
- Terminal (non-reusable) states for idempotency: `{canceled, expired, failed_to_create}`. Any other state on a sub for the same product handle ⇒ reuse it.
- `CollectionMethod`: `automatic, remittance, prepaid, invoice` (not set by us).

**Error handling** (`from maxio_advanced_billing.core import ApiError, RawError`):
- Every parsed call raises `ApiError` on error status; `.status_code:int`, `.error` per-op union, `.response`.
- `read_customer_by_reference` missing → `ApiError` 404, `.error` is `RawError` → treat as "no customer".
- `create_customer` duplicate reference race → `ApiError` 422 (`CustomerErrorResponse1`) → re-lookup and reuse.
- Decode failure → `ValidationError`/`ValueError`, not `ApiError`. httpx transport errors arrive unwrapped (`httpx.HTTPError`). Catch broadly at the service boundary, map to `MaxioError` → view returns 502.
- **No retries in the SDK** — we deliberately do NOT add retry/backoff (sandbox scope); documented.

### CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| `product_handle` passed to `create_subscription` must be a handle returned by `list_products_for_product_family` | `subscriptions.create_subscription` ← `product_families.list_products_for_product_family` | `services.subscribe` validates plan_handle against `list_plans()` before creating |
| `customer_id` passed to `create_subscription`/`list_customer_subscriptions` must be an id from `read_customer_by_reference`/`create_customer` | create_subscription, list_customer_subscriptions ← customers.* | `services.ensure_customer` is the sole source of customer ids |
| family id for `list_products_for_product_family` must be one from `list_product_families` (handle match) | list_products_for_product_family ← list_product_families | `services.list_plans` resolves by handle |

## Assumptions & Blockers

- **Resolved**: card-free subscribe requires `payment_collection_method=remittance` (invoice billing); the default automatic collection 422s trying to charge immediately. Verified end-to-end on sandbox (subscription created `active`, no card).
- **Assumption (minor)**: subscription-level idempotency via list-check + reuse of non-terminal sub is sufficient for double-click (single-process dev server). Documented residual race window under true concurrency. No extra infra introduced (per constraints).
- No gaps: every capability needed is covered by the plugin's SDK map. Proceed.

## REQUIRED READING (load before implementing)

- **MUST load `python-client-initialization`** — singleton client, keyword-only ctor, close() obligation, sync/async don't mix.
- **MUST load `python-authentication`** — basic_auth shape; unset credential ⇒ unauthenticated silently.
- **MUST load `python-calling-endpoints`** — keyword-only boundary, parsed vs raw modes.
- **MUST load `python-models`** — `Optional[T]=T|UNSET` (no None), frozen models, `…Dict` companions, open enums, to_dict.
- **MUST load `python-error-handling`** — single `ApiError`, per-op union, decode failures are ValidationError/ValueError not ApiError, unwrapped httpx errors.
- **MUST load `python-configuration-resilience`** — server_config/base-URL, timeout semantics, NO retries.
- **MUST load `python-testing`** — fake the transport seam (`custom_http_client`), assert built request, cover error/decode paths.
