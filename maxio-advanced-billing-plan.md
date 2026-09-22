# Maxio Advanced Billing — Integration plan & contract sheet

Adds recurring-subscription billing to the django-oscar **sandbox** site (`sandbox/`), with Maxio
Advanced Billing as the system of record. Additive/parallel to the existing one-time commerce flow.

## Host survey (facts that shape the code)

- **Runtime**: Django under WSGI (`sandbox/wsgi.py`), sync request path. → **use the SYNC client**
  `MaxioAdvancedBillingClient`. (No async anywhere in the sandbox.)
- **Auth to our own API**: Django session login (`django.contrib.auth`). Identity = `request.user`.
- **App layout**: sandbox apps live in `sandbox/apps/` (e.g. `apps.user`, `apps.offers`). URLs are
  wired in `sandbox/urls.py`; settings in `sandbox/settings.py` reads env via `django-environ`
  (`env = environ.Env()`), and `INSTALLED_APPS` is a plain list.
- **Oscar models to reuse**: no new domain models needed. Maxio is the system of record; the app's
  user is the Django `AUTH_USER_MODEL`. Idempotency uses a **stable Maxio customer `reference`**
  derived from `user.pk` — no local mapping table, so **no migrations**.
- **Test stack**: pytest (`pyproject.toml` `[project.optional-dependencies].test`), repo tests under
  `tests/`. Sandbox app tests will live beside the app and use the transport-seam stub.

## Settings (read via Django settings, names fixed by task; never hardcode values)

`sandbox/settings.py` adds, read from env:
- `MAXIO_API_KEY` (→ Basic auth username; password is the literal `"x"` — per SDK README
  `curl -u <api_key>:x`)
- `MAXIO_SITE_SUBDOMAIN` (→ `{site}` template var of the `production`/`us` server)
- `MAXIO_DEFAULT_PRODUCT_FAMILY` (handle used to scope which products are offered as plans)
- `MAXIO_BASE_URL` (optional; when set, used verbatim as the `production`/`us` `base_url` override)

## Client construction (grounded: sdk-map.md, python-client-initialization)

- Sync `MaxioAdvancedBillingClient`, **keyword-only**, long-lived (module-level singleton in the app),
  `close()` obligation → registered via `atexit` (WSGI guidance).
- `environment="us"` (explicit — omitting is silently `"us"`; task says `MAXIO_ENVIRONMENT=US`).
- Server has template var `{site}` defaulting to literal `"subdomain"` — **must override**:
  `server_config={"production": {"us": {"site": <MAXIO_SITE_SUBDOMAIN>}}}`.
  If `MAXIO_BASE_URL` set: `server_config={"production": {"us": {"base_url": <MAXIO_BASE_URL>}}}`.
- `basic_auth=BasicAuthCredentials(username=<MAXIO_API_KEY>, password="x")`.
- `timeout` explicit (e.g. 30.0).

## Endpoints (all under `/api/`, session-auth, each separately invocable)

1. `GET /api/subscription-plans` → list plans for the configured family. Each entry carries
   `planHandle`.
2. `POST /api/subscriptions` → ensure customer (idempotent) + create subscription (idempotent per
   plan). Returns `subscriptionId` as a **top-level** field.
3. `GET /api/my-subscriptions` → the caller's subscriptions from Maxio.

## Contract sheet — operations in scope

Sync client. Every op also has `.with_raw_response` (returns `ApiResult`, no raise) and an async twin
(not used). Keyword-only tail after `*`; every keyword has a real default (no defensive `None`s).
`Optional[T]` on models = `T | UnsetType` (NOT `None`). Money is `*_in_cents: int`.

### A. `client.products.list_products(*, per_page=20, page=1, include_archived=None, ...) -> list[ProductResponse]`
- **Error**: Case B → `.error` is always `RawError`. (No typed arm.)
- `ProductResponse.product: Product` (required wrapper field).
- `Product` fields used: `id:int`, `name:str`, `handle:OptionalNullable[str]` (**planHandle**),
  `description:OptionalNullable[str]`, `price_in_cents:int`, `interval:int`,
  `interval_unit:IntervalUnitOrStr`, `require_credit_card:bool`, `product_family:Optional[ProductFamily]`
  (`.handle` for family filter), `product_price_point_handle`, `taxable`.
- Filter to `product_family.handle == MAXIO_DEFAULT_PRODUCT_FAMILY`; skip archived
  (`include_archived` left default/None ⇒ archived excluded). Use `per_page=200`.
- **Smoke verified**: returns `basic-plan` ($2900) and `eshop-pro` ($29900), family `eshop-subscribe`,
  `require_credit_card=False`.

### B. `client.customers.read_customer_by_reference(reference: str) -> CustomerResponse`  (idempotency lookup)
- required positional `reference` (query param). **Error**: Case B → `RawError`.
- **Smoke verified**: unknown reference ⇒ `ApiError` `status_code=404`, `e.error` is `RawError`.
  → treat **404** as "customer absent"; any other status = real failure.
- `CustomerResponse.customer: Customer`; read `.id:int`, `.reference`.

### C. `client.customers.create_customer(*, body: CreateCustomerRequest|Dict) -> CustomerResponse`
- **Error**: Case A → `.error` is `CustomerErrorResponse1 | RawError` ([422] typed).
- Body: `CreateCustomerRequest(customer=CreateCustomer(...))`.
- `CreateCustomer` **required**: `first_name:str`, `last_name:str`, `email:str`. Optional set:
  `reference` (our stable id), `organization`.

### D. `client.subscriptions.create_subscription(*, body: CreateSubscriptionRequest|Dict) -> SubscriptionResponse`
- **Error**: Case A → `.error` is `ErrorListResponse1 | RawError` ([422] typed; `.errors: list[str]`).
- Body: `CreateSubscriptionRequest(subscription=CreateSubscription(...))`.
- `CreateSubscription` (all `Optional`): set `product_handle:str` (plan) + `customer_id:int`.
  Both plans have payment method NOT required ⇒ no card attributes needed.
- Returns `SubscriptionResponse.subscription: Optional[Subscription]`.

### E. `client.customers.list_customer_subscriptions(customer_id: int) -> list[SubscriptionResponse]`  (idempotency + my-subscriptions)
- required positional `customer_id` (path). **Error**: Case B → `RawError`.
- `Subscription` fields read: `id:int` (**subscriptionId**), `state:SubscriptionStateOrStr`,
  `next_assessment_at:OptionalNullable[RFC3339DateTime]` (**next billing date**),
  `current_period_ends_at`, `product:Optional[Product]` (→ `.handle`, `.name`),
  `product_price_in_cents` / `current_billing_amount_in_cents:int`, `currency:str`, `reference`,
  `created_at`, `activated_at`.

### SubscriptionState (open enum) — idempotency classification
Live/problem/awaiting (block a duplicate → return existing): `pending, trialing, assessing, active,
soft_failure, past_due, suspended, paused, unpaid, on_hold, awaiting_signup`.
Terminal (allow new subscribe): `canceled, expired, failed_to_create, trial_ended`.
Reading is open-enum safe: compare `.value`/string; unknown values treated as **active** (safe:
avoids duplicate create).

## Idempotency design (hero flow: Subscribe)
- Maxio customer `reference = f"oscar-user-{user.pk}"` → `read_customer_by_reference`; 404 ⇒
  `create_customer`. A double-click never makes two customers (reference is the key).
- Before `create_subscription`: `list_customer_subscriptions(customer_id)`; if any subscription for
  the requested `product_handle` is in a non-terminal state, **return it** (idempotent) rather than
  creating a second. Otherwise create.

## Error boundary (grounded: python-error-handling)
Single translation point. Ladder: `ApiError` → (typed arm isinstance → 422 domain reject as our 400/409)
→ `RawError` arm (carry status; 404 handled specially in lookup as "absent") ; `ValidationError`
(decode/our-payload) → 502 "unreadable/unknown"; `httpx.HTTPError` → 502 "provider unreachable".
Never surface `str(e)`/traceback to the caller. No OAuth scheme here (Basic only) ⇒ no
`OAuthProviderError` path. **SDK does NO retries** — reads (`list_*`) may be retried; we keep writes
single-attempt (create). Assert `subscription.id is not UNSET` after create (truncated-body guard).

## File layout (new)
```
sandbox/apps/subscriptions/
  __init__.py
  apps.py            # AppConfig, name="apps.subscriptions", label="maxio_subscriptions"
  client.py          # build_client() + module singleton + atexit close; reads django settings
  service.py         # MaxioSubscriptionService: list_plans / subscribe / list_my_subscriptions + errors
  serializers.py     # plain dict shaping (planHandle, subscriptionId, camelCase out)
  views.py           # 3 JSON views, login-required, session auth
  urls.py            # /subscription-plans, /subscriptions, /my-subscriptions
  tests/…            # transport-seam stub tests
```
Wire: add app to `INSTALLED_APPS`; add Maxio settings block to `sandbox/settings.py`; add
`path('api/', include('apps.subscriptions.urls'))` in `sandbox/urls.py` (outside i18n_patterns).

## Assumptions & Blockers
- **Assumption (minor)**: "plans" = Maxio *products* in the configured family (Pro/Basic are products
  with handles). Grounded by smoke test. Proceed.
- **Assumption (minor)**: payment method not required (task states it; smoke shows
  `require_credit_card=False`) ⇒ subscribe needs no card capture / 3DS. Proceed.
- **Assumption (minor)**: customer first/last/email sourced from Django user (fallbacks when blank).
  Proceed.
- No blockers. All 5 in-scope ops smoke-tested against the real sandbox (reads + a safe 404 lookup;
  create ops validated by shape, exercised live in self-verification).

## REQUIRED READING (all loaded)
- `python-client-initialization` — MUST load before building the client (done).
- `python-authentication` — Basic-auth keyword shape (done).
- `python-calling-endpoints` — call/response modes (done).
- `python-models` — UNSET/Optional/open-enum/date types (done).
- `python-error-handling` — the error boundary (done; floor: always).
- `python-configuration-resilience` — server_config nesting, no-retry, timeout (done).
- `python-testing` — transport-seam stub before any test file (done; floor).
