# Maxio Advanced Billing — Subscription billing for the django-oscar sandbox

## Goal & scope

Add recurring-subscription billing to the sandbox site (`sandbox/`), with Maxio Advanced
Billing as system of record. Additive, parallel to the existing cart/checkout. New Django app
`apps.subscriptions` wired into `sandbox/urls.py` under `/api/`. Session auth. Three endpoints,
each separately invocable:

- `GET  /api/subscription-plans`  — list plans in the configured product family; each entry carries `planHandle`.
- `POST /api/subscriptions`       — ensure Maxio customer (idempotent), subscribe, return `subscriptionId` (top-level).
- `GET  /api/my-subscriptions`    — read the caller's subscriptions back from Maxio.

## Host survey (conventions to imitate)

- Sandbox apps live under `sandbox/apps/` and are imported as `apps.<name>` (`sandbox/` is on
  `sys.path`; `DJANGO_SETTINGS_MODULE=settings`). Exemplar: `sandbox/apps/offers.py`, `sandbox/apps/user/`.
- URLs: `sandbox/urls.py` uses `path('', include(...))`. I add `path('api/', include('apps.subscriptions.urls'))`
  OUTSIDE `i18n_patterns` (APIs must not be language-prefixed).
- Settings: `sandbox/settings.py` uses `django-environ` `env` — read Maxio config with `env.str(..., default='')`.
- **Sync** host (Django/WSGI) → **sync** `MaxioAdvancedBillingClient`. Client held as a lazily-initialised
  module global built from Django settings (WSGI pattern from python-client-initialization). Never per-request.
- DB is SQLite, starts empty; one migration for the ledger model is fine (`sandbox.manage.py migrate`).

## Toolchain

- `venv` (py -3.11), project installed `-e .[test]`. SDK installed from git; its deps
  (`pydantic[email]`, `httpx`, `typing-extensions`) installed explicitly (pip did not auto-pull them).
- Run app: `venv/Scripts/python sandbox/manage.py runserver`. Tests: `pytest` (pytest-django present) or `manage.py test`.
- Type check: project ships no mypy config; run `venv/Scripts/python -m mypy --strict` on touched files (install mypy into venv).

## Credentials / environment (verified live)

- Env vars present: `MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`, `MAXIO_ENVIRONMENT` (value `"US"` — **uppercase**),
  `MAXIO_DEFAULT_PRODUCT_FAMILY` (value `"eshop-subscribe"` — a **handle**, not an id). `MAXIO_BASE_URL` unset.
- Settings (exact names required): `MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`, `MAXIO_DEFAULT_PRODUCT_FAMILY`,
  `MAXIO_BASE_URL`; plus `MAXIO_ENVIRONMENT` (default `'us'`). **Values never written into the repo** — read at runtime.
- **Environment must be lowercased** before passing to the SDK: `"US"` → `"us"` (SDK raises ValueError otherwise). Verified.
- Auth: HTTP Basic, `username=<MAXIO_API_KEY>`, `password="x"` (from SDK README `curl -u <api_key>:x`). Verified.
- Server selection (several servers × several environments arm): `environment=<lower>`,
  `server_config={"production": {<env>: {"site": <MAXIO_SITE_SUBDOMAIN>}}}`. When `MAXIO_BASE_URL` is set,
  use `{"production": {<env>: {"base_url": <MAXIO_BASE_URL>}}}` verbatim instead. Verified working (returned live data).

### Live smoke results (read-only, from scratchpad)
- `list_product_families()` → family handle `eshop-subscribe` → **id 3026728** (task's 3023074 is stale, as warned).
- `list_products_for_product_family("3026728")` → `basic-plan` (2900¢), `eshop-pro` (29900¢), both monthly. Handles stable.
- `read_customer_by_reference("<miss>")` → raises `ApiError`, `status_code=404`, `type(e.error)=RawError` (Case B).

## Contract sheet (from SDK map + source; no open lookups)

**Client**: `MaxioAdvancedBillingClient` (sync). Keyword-only ctor: `basic_auth=BasicAuthCredentials(username,password)`,
`environment`, `server_config`, `timeout=30.0`. Held as module global; `close()` at process exit (atexit). No retries in SDK.

All operations below: server `production`; auth `basic_auth OR bearer_auth`; trailing `*, request_options`.
Everything after `*` is keyword-only with a real default.

| Operation | Signature (relevant) | Returns (parsed) | Error case |
|---|---|---|---|
| `product_families.list_product_families` | `(*, ...date filters...)` | `list[ProductFamilyResponse]` | Case B `RawError` |
| `product_families.list_products_for_product_family` | `(product_family_id: str, *, page=1, per_page=20, ...)` | `list[ProductResponse]` | Case A: `str`[404] · `RawError` |
| `customers.read_customer_by_reference` | `(reference: str, *, request_options=None)` | `CustomerResponse` | Case B `RawError` (404 on miss) |
| `customers.create_customer` | `(*, body: CreateCustomerRequest\|dict\|None=None, ...)` | `CustomerResponse` | Case A: `CustomerErrorResponse1`[422] · `RawError` |
| `customers.list_customer_subscriptions` | `(customer_id: int, *, request_options=None)` | `list[SubscriptionResponse]` | Case B `RawError` |
| `subscriptions.create_subscription` | `(*, body: CreateSubscriptionRequest\|dict\|None=None, ...)` | `SubscriptionResponse` | Case A: `ErrorListResponse1`[422] · `RawError` |
| `subscriptions.find_subscription` | `(*, reference: str\|None=None, ...)` | `SubscriptionResponse` | Case A: `RawError`[404, unmapped] |

**Model shapes used** (members set / read; `Optional[T]` here = `T | UNSET`, not None):
- `CreateSubscriptionRequest{ subscription: CreateSubscription }`. `CreateSubscription`: set `product_handle: str`
  (the planHandle), `customer_id: int` (from ensured customer), `reference: str` (our deterministic idempotency reference — **always set**),
  and `payment_collection_method = CollectionMethod.REMITTANCE` (`maxio_advanced_billing.models.enums`; values
  `automatic`/`remittance`/`prepaid`/`invoice`). **Verified live:** without this the create fails 422 "No payment method
  was on file for the $299.00 balance" — the account default is `automatic` (card), so a no-card/invoice subscribe must
  override it to `remittance`. This is a deliberate task-driven override (payment method not required → invoice-billed).
  Everything else UNSET → provider/plan defaults (no trial, per task).
- `CreateCustomerRequest{ customer: CreateCustomer }`. `CreateCustomer` **required**: `first_name, last_name, email`;
  set `reference: str` (deterministic per user). Derive names/email from the Django user.
- `CustomerResponse{ customer: Customer }`. Read `customer.id`, `.reference`, `.email`.
- `SubscriptionResponse{ subscription: Optional[Subscription]=UNSET }` — **guard `is UNSET`** before use.
  `Subscription` read: `id:int`, `state: SubscriptionStateOrStr`, `product: Product`, `product_price_in_cents:int`,
  `current_period_ends_at: OptionalNullable[RFC3339DateTime]` (→ nextBillingDate), `next_assessment_at`, `created_at`.
- `ProductResponse{ product: Product }`. `Product` read: `id`, `name`, `handle: OptionalNullable[str]`,
  `description`, `price_in_cents:int`, `interval:int`, `interval_unit: IntervalUnitOrStr` (`day`/`month`).
- `Customer` (nested in subscription) read: `id`, `reference`.

**Enums** (open — value may be enum member or plain str; serialize with `str(x)`):
- `SubscriptionState`: `pending, failed_to_create, trialing, assessing, active, soft_failure, past_due, suspended,
  canceled, expired, paused, unpaid, trial_ended, on_hold, awaiting_signup`. Outcome mapping for our ledger:
  `active`/`trialing` → done; `pending`/`assessing`/`awaiting_signup` → pending; `failed_to_create`/`canceled`/`expired`/`suspended`/`unpaid`/`past_due` → failed/ended; anything unlisted → unknown.
- `IntervalUnit`: `day`, `month`.

**Return `None` operations**: none in scope.

**Decode-failure rule**: a truncated 2xx body raises `pydantic.ValidationError`/`ValueError`, NOT `ApiError`, and
bypasses both response modes. After create_subscription assert `resp.subscription is not UNSET` and `.id is not UNSET`;
an absent id on a write = outcome unknown → reconcile via `find_subscription(reference)`.

## CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| `product_handle` sent to create_subscription must be a handle returned by list_products_for_product_family | create_subscription ← list_products_for_product_family | service validates the posted planHandle against the live plan list before subscribing |
| `customer_id` sent to create_subscription must be an id returned by read/create_customer | create_subscription ← customers.* | service ensures customer first, passes its id |
| subscription `reference` sent on create is what find_subscription looks it up by | find_subscription ← create_subscription | same deterministic reference string used for both |

## Idempotency design (one operation, one durable row — python-configuration-resilience)

- **Ledger model** `MaxioSubscription` (Django, in `apps.subscriptions.models`) — the durable claim row.
  Not a copy of Maxio's records; it records *that we asked*. Fields: `user` FK, `plan_handle`, `reference` (unique),
  `maxio_customer_id`, `maxio_subscription_id`, `status` (sending/done/pending/failed/needs_review/unknown),
  `created_at`, `updated_at`. Deterministic `reference = f"oscar-sub-u{user.id}-{plan_handle}"`;
  customer reference = `f"oscar-cust-u{user.id}"`.
- **Customer**: idempotent ensure = `read_customer_by_reference(cust_ref)`; on `ApiError` 404 → `create_customer`.
  Guard the create/lookup race: if create returns 422 (reference taken) re-lookup. One customer per user.
- **Subscribe**: CLAIM first via `get_or_create(reference=…)` in a transaction (unique constraint = race winner).
  If row exists and already `done`/`pending` with a subscription id → return it (idempotent, 200). If `sending`
  within a recent window → 202 in-progress. Only the claim winner calls `create_subscription`. On success settle
  the row from the returned `state`. Belt-and-braces: Maxio also enforces unique subscription reference, so a
  cross-process duplicate create → find_subscription(reference) and adopt.
- No-retry SDK: user-facing path makes one attempt; timeout set short (10s). No auto-retry added (documented).

## Error boundary (python-error-handling)

One translation layer `apps/subscriptions/errors.py` → `MaxioError(status_code, message, *, outcome_unknown)`.
Ladder (most specific first): `ApiError` → 401/403 ⇒ 502 (our creds); 429 ⇒ 503; typed 400/404/409/422 ⇒ client fault
(pass status+detail); else ⇒ 502. `ValidationError` ⇒ 502 unreadable (unknown). httpx never-sent tuple ⇒ 502 known;
`httpx.RequestError` ⇒ 504 outcome_unknown. Views render JSON `{"error": msg}` with mapped HTTP status; never leak `str(e)`.

## Files

- `sandbox/apps/subscriptions/__init__.py`, `apps.py` (AppConfig, label `subscriptions`)
- `client.py` (settings→client factory, module global), `services.py` (business logic + error translation),
  `serializers.py` (model→dict), `errors.py`, `models.py` (ledger), `migrations/0001_initial.py`, `views.py`, `urls.py`
- Edit `sandbox/settings.py` (add 5 Maxio settings + app to INSTALLED_APPS), `sandbox/urls.py` (route `api/`)
- Tests: `apps/subscriptions/tests.py` faking the transport seam (python-testing).

## REQUIRED READING (loaded)
- python-client-initialization — MUST load (client factory/lifetime). ✓
- python-calling-endpoints — MUST load (call shapes, status-not-id). ✓
- python-error-handling — MUST load (error boundary). ✓
- python-configuration-resilience — MUST load (idempotent write, durable row, server config, timeout). ✓
- python-models — MUST load (UNSET, open enums, RFC3339 serialization). ← load before serializers
- python-testing — MUST load (transport fake). ← load before tests

## Verified end to end (live sandbox)
- `GET /api/subscription-plans` → basic-plan ($29.00), eshop-pro ($299.00), each with `planHandle`.
- `POST /api/subscriptions {eshop-pro}` → 201, real subscription id 94481365, state `active`, customer 99091462.
- Double-click → 200 with the **same** subscriptionId (no second subscription; customer reused).
- `POST {basic-plan}` → 201 distinct subscription 94481370, **same** customer 99091462 (idempotent customer).
- `GET /api/my-subscriptions` → both subscriptions read back from Maxio.
- Error paths: unauth→401, wrong method→405, unknown plan→404, credentials→502, rate-limit→503, refused→502(known), read-timeout on write→504(unknown).

## Assumptions
- Plans listing requires auth too (task frames a logged-in shopper). All three endpoints require session auth.
- CSRF: API POST is `csrf_exempt` so a programmatic caller with a session cookie can drive it; documented tradeoff.
- Re-subscribe after cancel reuses the deterministic reference (returns existing row); acceptable for sandbox. Documented.
- Customer name/email derived from Django user (email may be blank on staff acct → fallback synthesized email).
