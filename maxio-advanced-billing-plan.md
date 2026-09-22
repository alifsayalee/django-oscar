# Maxio Advanced Billing — integration plan (django-oscar sandbox)

## Goal
Add recurring-subscription billing to the `sandbox/` django-oscar site with Maxio Advanced Billing as
system of record. Additive, parallel to the existing one-time cart/checkout flow. Expose three HTTP
endpoints under `/api/`, session-authenticated, identity from `request.user`.

- `GET  /api/subscription-plans`  → list available plans; each entry carries its own `planHandle`.
- `POST /api/subscriptions`       → ensure a Maxio customer exists (idempotent) + enroll; returns
                                    `subscriptionId` as a top-level field; confirms plan/price/state/
                                    next-billing-date.
- `GET  /api/my-subscriptions`    → the caller's subscriptions.

## Host decision: SYNC
Django under WSGI (sandbox runs via `runserver`/`uwsgi`). → use `MaxioAdvancedBillingClient` (sync).
`close()` obligation; client held as a lazy module-level singleton (WSGI placement per
python-client-initialization). No async anywhere.

## Client construction (verified live)
- Auth: HTTP **Basic**, `username = MAXIO_API_KEY`, `password = "x"` (confirmed by api-reference
  `curl -u <api_key>:x` and a live 200 from `list_customers`).
- `environment="us"` (this is the "several servers, several environments" arm → nesting
  `{server: {environment: {...}}}`).
- Base URL: production/us template is `https://{site}.chargify.com`, `{site}` default `"subdomain"`.
  - If `MAXIO_BASE_URL` set → `server_config={"production": {"us": {"base_url": MAXIO_BASE_URL}}}`.
  - Else → `server_config={"production": {"us": {"site": MAXIO_SITE_SUBDOMAIN}}}`.
- `timeout` set explicitly (e.g. 30.0).
- Settings read via Django settings (django-environ) in `sandbox/settings.py`:
  `MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`, `MAXIO_DEFAULT_PRODUCT_FAMILY`, `MAXIO_BASE_URL`.
  Values NEVER written into the repo — read from env at runtime only.

## Live smoke results (sandbox, 2026-09-23)
- Family `eshop-subscribe` → id 3026728 (IDs reassigned as the task warned; resolve by handle).
- Products: `basic-plan` $29.00 (2900c, 1 month), `eshop-pro` $299.00 (29900c, 1 month).
- `read_customer_by_reference("missing")` → `ApiError`, status 404, `.error` is `RawError` (Case B).
- `list_customers` → 200 (auth OK).

## Contract sheet (grounded in SDK map + source; sync client; keyword-only after `*`)

Money: `Product.price_in_cents` is `int` — no float/Decimal hazard.
Enum reads are OPEN (`...OrStr`) — a value may come back as a plain `str`; handle via `str(member)` /
value comparison.

### list plans
- `client.product_families.list_product_families() -> list[ProductFamilyResponse]` — Case B (RawError).
  - `.product_family` → `ProductFamily` (`.id: int`, `.handle`, `.name`). Resolve family id by matching
    `handle == MAXIO_DEFAULT_PRODUCT_FAMILY`.
- `client.product_families.list_products_for_product_family(product_family_id: str, *, per_page=..., ...)
   -> list[ProductResponse]` — Case A: arms `str` [404] | `RawError`.
  - `product_family_id` positional (str) — pass `str(family_id)`.
  - `.product` → `Product`: `.handle` (OptionalNullable str), `.name`, `.price_in_cents: int`,
    `.interval: int`, `.interval_unit` (IntervalUnitOrStr), `.description`, `.id`,
    `.product_price_point_handle`.

### ensure customer (idempotent)
- `client.customers.read_customer_by_reference(reference: str) -> CustomerResponse` — Case B (RawError).
  `.customer` → `Customer`: `.id: int`, `.reference`, `.first_name`, `.last_name`, `.email`.
  404 ⇒ not found (catch ApiError, status_code==404).
- `client.customers.create_customer(*, body: CreateCustomerRequest|dict) -> CustomerResponse` — Case A:
  arms `CustomerErrorResponse1` [422] | `RawError`.
  - body shape: `{"customer": CreateCustomer}`. `CreateCustomer` REQUIRED: `first_name: str`,
    `last_name: str`, `email: str`; optional `reference: str`.
  - Idempotency: reference collision on concurrent create ⇒ 422; on 422 re-read by reference.

### subscribe
- `client.customers.list_customer_subscriptions(customer_id: int) -> list[SubscriptionResponse]` —
  Case B. Used to detect an existing live subscription to the same product (double-click idempotency).
- `client.subscriptions.create_subscription(*, body: CreateSubscriptionRequest|dict)
   -> SubscriptionResponse` — Case A: arms `ErrorListResponse1` [422] | `RawError`.
  - body shape: `{"subscription": CreateSubscription}`. Set:
    `product_handle: str` (the plan handle), `customer_id: int`,
    `payment_collection_method="remittance"` (plans need no card; avoids automatic/card requirement).
  - `SubscriptionResponse.subscription` (Optional) → `Subscription`:
    `.id: int` (→ `subscriptionId`), `.state` (SubscriptionStateOrStr),
    `.current_period_ends_at` / `.next_assessment_at` (OptionalNullable RFC3339DateTime → next billing),
    `.product` (Product), `.product_price_point_id`.

### list my subscriptions
- `client.customers.list_customer_subscriptions(customer_id) -> list[SubscriptionResponse]` — Case B.

## Idempotency design (Maxio is system of record; no local parallel models — mandate honored)
- Customer reference = `f"oscar-user-{user.pk}"` (stable per Django user).
- ensure_customer: read_customer_by_reference → 404 → create_customer → on 422 re-read by reference.
- subscribe: ensure_customer, then list_customer_subscriptions; if a subscription to the same
  `product_handle` exists in a LIVE state (not in {canceled, expired, failed_to_create}), return it
  instead of creating a second. Otherwise create.

## Error boundary (python-error-handling)
Single translation layer in `services.py`:
- `except ApiError` → inspect `.status_code` / `.error` (isinstance narrowing where a typed arm exists);
  map provider 4xx → our 4xx, else 5xx. 404 on read = "not found" sentinel, not an error.
- `except pydantic.ValidationError` → "unreadable response; outcome unknown" (do not assume failure).
- `except httpx.HTTPError` → "provider unavailable; outcome unknown".
Assert on members we depend on after each call (e.g. `subscription.id is not UNSET`).
No retries in the SDK — reads may be retried; the create is left single-attempt + pre-check for
idempotency (no idempotency-key parameter on create_subscription).

## Django wiring
- New app `sandbox/apps/subscriptions/` (label `maxio_subscriptions`), added to `INSTALLED_APPS`.
  - `apps.py`  — AppConfig (name `apps.subscriptions`).
  - `client.py` — lazy singleton building `MaxioAdvancedBillingClient` from settings; `close` via atexit.
  - `exceptions.py` — `BillingError` hierarchy (`PlanNotFound`, `ProviderError`, `ProviderUnavailable`).
  - `services.py` — `list_plans()`, `ensure_customer(user)`, `subscribe(user, plan_handle)`,
    `list_my_subscriptions(user)`; the SDK is used ONLY here.
  - `views.py`  — 3 session-authenticated JSON views (plain Django, no new heavy deps). Each capability
    separately invocable (own route). Auth: `request.user.is_authenticated` (401 JSON if not).
    CSRF: enforced on POST (session auth); GET plans uses `ensure_csrf_cookie` so a caller can obtain
    the token. Identity from `request.user`.
  - `urls.py`   — routes; included from `sandbox/urls.py` under `/api/`.
- No Django models (Maxio is source of truth; reuse Oscar user model for identity/attrs).

## Response shapes (our own; identifiers mandated)
- plans: `{"plans": [{"planHandle","name","priceInCents","priceFormatted","currency","interval",
  "intervalUnit","description"}...]}`.
- POST subscriptions: top-level `subscriptionId`, plus `planHandle`, `state`, `priceInCents`,
  `nextBillingDate`, `customerId`, `created` (bool: created vs already-existing).
- my-subscriptions: `{"subscriptions": [{"subscriptionId","planHandle","planName","state",
  "priceInCents","currentPeriodEndsAt","nextBillingDate"}...]}`.

## Verification
- Self: Django test-Client script with `force_login` on the seeded superuser → GET plans, POST
  subscribe (real Maxio subscription created), GET my-subscriptions; assert subscriptionId round-trips.
  Double-POST returns same subscriptionId (idempotency).
- Provide manual guide (run server on assigned port block, login, drive endpoints).

## Assumptions & blockers
- Assumption: `payment_collection_method="remittance"` lets subscribe succeed with no card (plans are
  "payment method not required"). Will confirm against sandbox during verification; if create 422s on
  collection method, fall back to letting Maxio default and report. (Minor assumption → proceed.)
- Assumption: default plan for POST when `planHandle` omitted = `eshop-pro`. Validate provided handle
  against the live plan list → clean 400 (`PlanNotFound`) otherwise.
- No blockers. All in-scope operations smoke-tested read-only; auth/config confirmed.

## REQUIRED READING (companion skills)
- python-client-initialization — MUST load before building the client (loaded).
- python-calling-endpoints — MUST load before first call (loaded).
- python-models — MUST load for request/response models (loaded).
- python-error-handling — MUST load for the error boundary (loaded, floor).
- python-configuration-resilience — MUST load for server_config/base_url/timeout (loaded).
- python-testing — MUST load before writing the verification script / any test that fakes transport.
