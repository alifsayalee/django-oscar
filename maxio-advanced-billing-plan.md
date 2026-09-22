# Maxio Advanced Billing — integration plan (django-oscar sandbox)

## Goal

Add recurring-subscription billing to the django-oscar sandbox site, with **Maxio Advanced
Billing** as system of record. Additive, parallel to the existing cart/checkout flow. Exposed as
HTTP JSON endpoints on the sandbox project, under `/api/`, authenticated by Django session login.

Hero flow **Subscribe**: a logged-in shopper browses plans → subscribes → sees it in their account.
Ensuring the Maxio customer is idempotent (double-click never creates two customers/subscriptions).

## Endpoints (each separately invocable)

| Method | Path | Capability |
|---|---|---|
| `GET`  | `/api/subscription-plans` | List available plans; each entry carries `planHandle` |
| `POST` | `/api/subscriptions` | Ensure customer + enroll in a plan; returns top-level `subscriptionId` |
| `GET`  | `/api/my-subscriptions` | List the caller's subscriptions with plan/price/state/next-billing |

Auth: Django session (`request.user`); unauthenticated → 401. Identity taken from `request.user`.

## Repo conventions (exemplars to imitate)

- Sync WSGI Django. No DRF installed → plain Django views returning `JsonResponse`. **Sync client.**
- New app under `sandbox/apps/` — mirror `sandbox/apps/user/` (a package with `__init__.py`,
  `models.py`) and `sandbox/apps/sitemaps.py` (uses `oscar.core.loading.get_model`). App is a plain
  Django app package; register in `INSTALLED_APPS` in `sandbox/settings.py`.
- URL wiring: `sandbox/urls.py` — add `path('api/', include('apps.subscriptions.urls'))` OUTSIDE the
  `i18n_patterns` block (API paths are not language-prefixed), before the Oscar catch-all.
- Reuse Oscar models via `get_model` (e.g. the auth user) rather than new models. We persist the
  local user↔Maxio-customer link in one small model in the new app (idempotency anchor).
- Settings read via `django-environ` `env` object already in `settings.py`. Add Maxio settings there
  reading env vars by name (never values): `MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`,
  `MAXIO_DEFAULT_PRODUCT_FAMILY`, `MAXIO_BASE_URL` (optional override).

## Toolchain

- `py -3.11`, venv at `repo/venv`. Installed: `-e .[test]`, `maxio-advanced-billing` (from git), plus
  `pydantic[email]` + `httpx` (the SDK's runtime deps were NOT auto-pulled — installed explicitly),
  and `mypy`.
- Tests: `pytest` (pytest-django present). Type check: `mypy` on the new app files.
- Run mgmt cmds from `sandbox/` (settings module `settings`). DB: SQLite beside project, built via
  the documented loaddata sequence.

## SDK decisions

- **Sync** client `MaxioAdvancedBillingClient` (Django WSGI). Long-lived, lazily constructed
  module-global in the app; never per-request. No `aclose`; process exit tears down the pool (dev
  server). Auth: **Basic**, `username = MAXIO_API_KEY`, `password = "x"` (SDK README line 32:
  `curl -u <api_key>:x`).
- **Environment**: `"us"` (default). Base URL: `production`/`us` template `https://{site}.chargify.com`
  with `{site}` = `MAXIO_SITE_SUBDOMAIN` → `server_config={"production": {"us": {"site": <sub>}}}`.
  If `MAXIO_BASE_URL` is set, override verbatim: `server_config={"production": {"us": {"base_url": <url>}}}`.
- Response mode: **parsed** (raises `ApiError`) everywhere; we do not need success-path status codes.
- No retries in SDK — acceptable for this integration (documented; interactive requests, no idempotency
  keys available on these ops).

## Contract sheet (grounded in SDK map + source; verified by live read-only smoke)

Client class: `MaxioAdvancedBillingClient` (sync). Keyword-only constructor. Import roots:
`maxio_advanced_billing` (client), `.core` (`BasicAuthCredentials`, `ApiError`, `RawError`),
`.models` (request/response models). All ops below: **Auth** basic OR bearer; **Server** `production`;
trailing keyword-only `request_options`. Keyword-only boundary marked by `*` — no defensive `None`s.

### Operations in scope

1. **`client.products.list_products(*, per_page=..., include_archived=..., ...) -> list[ProductResponse]`**
   - Case B → error is always `RawError`. Used for `GET /api/subscription-plans`.
   - `ProductResponse.product: Product`. Read: `handle` (→ `planHandle`), `id`, `name`,
     `price_in_cents`, `interval`, `interval_unit`, `product_family.handle`, `require_credit_card`.
   - Filter client-side to `product.product_family.handle == MAXIO_DEFAULT_PRODUCT_FAMILY`.
     (Live: returns 2 products, `eshop-pro` 29900¢ and `basic-plan` 2900¢, both `require_credit_card=False`.)
   - NOTE: `list_products_for_product_family(handle)` 404s on a *handle* and hits a decode trap
     (error mapper decodes non-JSON 404 body as JSON `str` → `ValueError`). **Do not use it.**

2. **`client.customers.read_customer_by_reference(reference: str) -> CustomerResponse`**
   - positional `reference` (query param). Case B → `RawError`. 404 when not found →
     `ApiError(status_code=404, error=RawError)` (verified live; body is not decoded, clean).
   - Idempotency lookup by our stable reference.

3. **`client.customers.create_customer(*, body: CreateCustomerRequest|dict|None) -> CustomerResponse`**
   - Case A → `CreateCustomerErrorBody = CustomerErrorResponse1 [422] | RawError`.
   - Body: `CreateCustomerRequest(customer=CreateCustomer(...))`. `CreateCustomer` required:
     `first_name`, `last_name`, `email`; set `reference` (our stable ref). Read back `.customer.id`.

4. **`client.customers.list_customer_subscriptions(customer_id: int) -> list[SubscriptionResponse]`**
   - positional `customer_id` (path). Case B → `RawError`. Used for `GET /api/my-subscriptions`.

5. **`client.subscriptions.create_subscription(*, body: CreateSubscriptionRequest|dict|None) -> SubscriptionResponse`**
   - Case A → `CreateSubscriptionErrorBody = ErrorListResponse1 [422] | RawError`.
   - Body: `CreateSubscriptionRequest(subscription=CreateSubscription(...))`. Set:
     `product_handle` (plan handle from caller), `customer_id` (from ensured customer),
     `payment_collection_method='remittance'`. Leave everything else UNSET (trial/setup/expiry/taxes
     stay plan-configured). No `next_billing_at` (so it activates now).
   - **Live finding (revised from smoke):** although `require_credit_card=False`, an *automatic*-
     collection subscription with an immediate balance is rejected 422 "No payment method was on file
     for the $X balance". `payment_collection_method='remittance'` (invoice-based Relationship
     Invoicing) creates the subscription `active` with no card — verified live (sub 94465328/94465342).
     This is the card-free path the task's "subscribe without card capture / 3-DS" requires; it is a
     design decision the SDK supports (the `payment_collection_method` field / `CollectionMethod` open
     enum), not a gap.
   - Read back: `.subscription.id` (→ `subscriptionId`), `.state`, `.product.handle`/`.product.name`,
     `.product_price_in_cents`, `.current_period_ends_at` (next billing date).

### Response models — members we depend on (assert immediately)

- `CustomerResponse.customer: Customer` → `.id: Optional[int]`, `.reference`.  Guard: `id is not UNSET`.
- `SubscriptionResponse.subscription: Optional[Subscription]` → `.id`, `.state`
  (`SubscriptionStateOrStr`, open enum; e.g. `active`), `.product: Optional[Product]`,
  `.product_price_in_cents`, `.current_period_ends_at: OptionalNullable[RFC3339DateTime]`.
  Guard on create: `subscription is not UNSET and subscription.id not in (UNSET, None)` → else
  "outcome unknown".
- `Product` → `.handle`, `.name`, `.price_in_cents`, `.interval`, `.interval_unit`.

`Optional[T]` here is `T | UnsetType` (NOT typing.Optional); compare against `UNSET` from `.core`,
not `None`. `OptionalNullable[T]` may also be `None`. Serialize response values by converting UNSET/None
to JSON `null` and datetimes via `str()`/isoformat.

### Error boundary (single translation layer in a service module)

Order: (1) `ApiError` → if `isinstance(e.error, RawError)`: map by `e.status_code`
(404 → not-found/None for lookups); else typed arm (`CustomerErrorResponse1` / `ErrorListResponse1`)
→ 422 provider-rejected with messages. (2) `pydantic.ValidationError` / `ValueError` → decode failure:
success-body-unreadable = outcome unknown (502); do NOT map to domain absence. (3) `httpx.HTTPError`
→ provider unreachable (502/503). Never surface `str(e)` to caller; log detail, return written message.
Guard reads too. A returned subscription id means accepted — also read `.state` back to the caller.

### CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| `product_handle` supplied to create_subscription must be a plan handle that list_products returns for the configured family | `subscriptions.create_subscription` ← `products.list_products` | implementation: validate requested `planHandle` against the live plan list before subscribing |
| `customer_id` on create_subscription must be an id returned by ensure-customer (read_customer_by_reference or create_customer) | `subscriptions.create_subscription` ← `customers.*` | implementation: ensure-customer runs first, returns the id |
| `customer_id` for list_customer_subscriptions must be the ensured customer's id for the caller | `customers.list_customer_subscriptions` ← `customers.*` | implementation: look up local link, else ensure |

## Idempotency design

- Stable customer `reference` = `oscar-user-<django_user_pk>` (deterministic per user).
- Local model `MaxioCustomer(user OneToOne, maxio_customer_id, reference)` anchors the link so we do
  not re-lookup on every call and can detect existing subscriptions.
- Ensure-customer: if local link exists → use it. Else `read_customer_by_reference(reference)`; on
  404 → `create_customer`; store link. Wrap in `select_for_update`/get_or_create to survive a
  double-click race; `ATOMIC_REQUESTS=True` is already on in settings.
- Subscribe: after ensuring customer, check existing active subscriptions for the same product handle
  (via `list_customer_subscriptions`) → if one is active/trialing for that plan, return it instead of
  creating a duplicate (idempotent subscribe).

## Assumptions & Blockers

- **Assumption**: `MAXIO_DEFAULT_PRODUCT_FAMILY` env var holds the family *handle* (`eshop-subscribe`)
  — confirmed live (smoke printed `family: eshop-subscribe` and products carry that family handle).
- **Resolved during verification**: plans set `require_credit_card=False`, but a card-free subscribe
  still needs `payment_collection_method='remittance'` to avoid the immediate-balance rejection (see
  create_subscription row). Handled; not a blocker.
- No blockers. All in-scope ops smoke-tested read-only against the real sandbox; the full Subscribe
  flow verified live (fresh create 201 + idempotent re-subscribe 200 + read-back).

## REQUIRED READING (companions — all loaded before coding)

- `python-error-handling` — MUST load — the error boundary (ApiError single type, per-op union, decode
  & transport failures bypass ApiError). ✅ loaded.
- `python-client-initialization` — MUST load — sync client, keyword-only, long-lived, close obligation,
  server_config shape. ✅ loaded.
- `python-calling-endpoints` — MUST load — signatures, parsed vs raw, UNSET-omission-is-a-decision. ✅ loaded.
- `python-models` — MUST load — UNSET vs None, OptionalNullable, building request bodies. ✅ loaded.
- `python-testing` — MUST load — StubTransport seam for unit tests of the service layer. ✅ loaded.

## Build order

1. `sandbox/settings.py`: add Maxio settings (env-read) + register app.
2. App scaffold: `sandbox/apps/subscriptions/` (`__init__.py`, `apps.py`, `models.py`, `migrations/`).
3. `client.py`: lazy module-global sync client factory from settings.
4. `services.py`: `MaxioService` — ensure_customer, list_plans, subscribe, list_my_subscriptions;
   error boundary + serialization helpers; local exceptions.
5. `views.py` + `urls.py`: three JSON endpoints, session-auth-gated.
6. Migration for `MaxioCustomer`.
7. Tests: `tests.py` with StubTransport unit tests (success, 422 typed, 404 raw, decode-failure,
   transport-failure, idempotent re-subscribe).
8. Verify: migrate, run server on assigned port, drive all three endpoints end-to-end against real
   sandbox with a logged-in session; confirm a real subscription is created and read back.
