# Maxio Advanced Billing integration — plan & contract sheet

Add recurring-subscription billing to the django-oscar **sandbox** site (`sandbox/`), with Maxio
Advanced Billing as the system of record. Additive, parallel to the existing cart/checkout flow.
New Django app `apps.subscriptions`, wired into `sandbox/urls.py` under `/api/`.

## Host decisions (from repo survey)

- **Sync vs async:** **SYNC.** Django under WSGI (`sandbox/wsgi.py`, `uwsgi.ini`). Use
  `MaxioAdvancedBillingClient` (never the async class). Teardown: `client.close()`.
- **Client lifetime:** one lazily-initialised module-global client, built on first use (never at
  import — secrets must not be read at import time; `settings.py` defaults them to `""`). Closed via
  `atexit`.
- **Auth:** HTTP Basic. Maxio convention (verified in SDK `README.md` line 32: `curl -u <api_key>:x`):
  `BasicAuthCredentials(username=<MAXIO_API_KEY>, password="x")`.
- **Server/base URL:** environments+servers SDK. `environment="us"` (env `MAXIO_ENVIRONMENT=US`; read
  it, map lower-case, default `"us"`). All in-scope operations use the `production` server. Configure
  via `server_config={"production": {"us": {...}}}`:
  - default: `{"site": <MAXIO_SITE_SUBDOMAIN>}` → `https://<sub>.chargify.com`.
  - override: when `MAXIO_BASE_URL` set, `{"base_url": <MAXIO_BASE_URL>}` (verbatim; no `{site}`
    placeholder so it renders as-is), and omit `site`.
- **Timeout:** set explicitly (default 30s is too long for a request path) — use 30s here (subscribe
  makes several calls; keep generous but bounded). Per-call default acceptable.
- **Settings (read via `django.conf.settings`, values from env, never hard-coded):** `MAXIO_API_KEY`,
  `MAXIO_SITE_SUBDOMAIN`, `MAXIO_DEFAULT_PRODUCT_FAMILY`, `MAXIO_BASE_URL` (optional), plus
  `MAXIO_ENVIRONMENT` (optional, default us). All default to `""`/`None` so import never raises;
  `build_client()` checks required ones and raises if missing.

## Toolchain

- `py -3.11 -m venv venv`; project installed editable with `[test]`; SDK installed from git
  (`maxio-advanced-billing @ git+...@main`) + runtime deps (pydantic[email], httpx, typing-extensions
  — these did not auto-resolve on first git install; installed explicitly).
- Run manage.py from `sandbox/` (its dir is sys.path[0]); `../venv/Scripts/python manage.py ...`.
- Type checker: project ships none. Install `mypy` into venv, run `mypy --strict` on the app's SDK-
  touching modules (SDK is generated under mypy --strict).
- Tests: pytest is available via `[test]` extra; write app tests using the transport-seam stub
  (python-testing). Run with the app's own settings.

## Credentials / smoke (verified against real sandbox `cp-exp-1`)

- Family `eshop-subscribe` → id **3026728** (stale doc said 3023074 — confirms IDs re-seed; resolve by
  handle at runtime, never hard-code the id).
- Products: `eshop-pro` (Pro Plan, 29900¢, 1 month), `basic-plan` (Basic Plan, 2900¢, 1 month). Both
  report `request_credit_card=true`.
- `read_customer_by_reference` on an unknown ref → **404 RawError** (Case B). This is the "not found"
  signal for customer idempotency.
- **CORRECTION to task premise:** "payment method not required" is FALSE for these plans as seeded —
  `create_subscription` with only product+customer returns **422 `ErrorListResponse1`**:
  `"No payment method was on file for the $299.00 balance"`. The production-grade card-free path is
  **`payment_collection_method="remittance"`** (Relationship Invoicing): verified → HTTP 201,
  `state=active`, `current_period_ends_at` one month out, no card. This is the design used for subscribe.
  (This is a design decision within the SDK's documented surface, not a plugin gap.)

---

## Contract sheet (all facts from SDK map + source modules; no memory)

Sync client. Every operation below is called `client.<controller>.<operation>(...)`. Keyword-only tail
after `*`; every keyword-only param has a real default (no defensive `None`s). Bodies passed as models
from `maxio_advanced_billing.models`.

### OP1 — list product families (resolve family handle → id)
- `client.product_families.list_product_families()` → `list[ProductFamilyResponse]`.
- Error: **Case B**, `.error` always `RawError`.
- Each item: `.product_family` (`ProductFamily`): `.id: int`, `.handle: str|None`, `.name`.
- Use: find the one whose `handle == MAXIO_DEFAULT_PRODUCT_FAMILY`; take `.id`. If none → 404 to caller
  (misconfig). Cache the resolved id on the client wrapper (module-level, refreshable).

### OP2 — list plans for the family  →  GET /api/subscription-plans
- `client.product_families.list_products_for_product_family(product_family_id: str, *, page=1,
  per_page=20, include_archived=None, ...)` → `list[ProductResponse]`. Positional: `product_family_id`
  (str — pass `str(resolved_id)`).
- Error: **Case A**, arms `str` [404] · `RawError`. 404 → family missing.
- Each item `.product` (`Product`): `.id:int`, `.name`, `.handle: str|None` (**→ `planHandle`**),
  `.price_in_cents:int`, `.interval:int`, `.interval_unit` (`IntervalUnitOrStr`, open enum → may be str),
  `.description`, `.product_family`.
- Response guard: assert `.product.handle` present for each returned plan (skip archived).

### OP3 — customer lookup by reference (idempotency read)
- `client.customers.read_customer_by_reference(reference: str)` → `CustomerResponse`. Positional:
  `reference`.
- Error: **Case B**, `RawError`. **404 = not found → create.** Map 404 RawError to None; any other
  status re-raise.
- Return `.customer` (`Customer`): `.id:int`, `.reference:str|None`, `.email`, `.first_name`, `.last_name`.

### OP4 — create customer (idempotent via provider-unique reference)
- `client.customers.create_customer(*, body: CreateCustomerRequest|dict)` → `CustomerResponse`.
- Body: `CreateCustomerRequest(customer=CreateCustomer(...))`.
  `CreateCustomer` **required**: `first_name:str`, `last_name:str`, `email:str`. Optional set here:
  `reference:str` (the stable per-user ref). (All other members `UNSET` → provider defaults; leave alone.)
- Error: **Case A**, arms `CustomerErrorResponse1` [422] · `RawError`. A 422 whose message indicates the
  **reference is already taken** ⇒ a concurrent create landed → look up by reference (OP3) and use it
  (landing, not failure). Other 422 → real validation error → surface.
- Response guard: assert `.customer.id is not UNSET`; else outcome unknown.

### OP5 — create subscription  →  POST /api/subscriptions
- `client.subscriptions.create_subscription(*, body: CreateSubscriptionRequest|dict)` →
  `SubscriptionResponse`. Use **`with_raw_response`** to observe the 201 status explicitly.
- Body: `CreateSubscriptionRequest(subscription=CreateSubscription(...))`. Fields SET (each with reason):
  - `product_handle:str` = the plan handle — **defines which plan** (alt to product_id; handle is stable).
  - `customer_id:int` = the ensured customer's id — **binds to existing customer** (alt to
    customer_reference/customer_attributes).
  - `payment_collection_method="remittance"` (`CollectionMethodOrStr`) — **overrides** the account's
    default auto-charge so no card is required (invoice/remittance). Chosen deliberately; see correction.
  - `reference:str` = deterministic `oscar-user-<pk>-<plan_handle>` — our idempotency handle on the
    subscription (client-chosen reference; always set).
  - Everything else `UNSET` → provider/product defaults (interval, currency, etc.). Do not set.
- Error: **Case A**, arms `ErrorListResponse1` [422] · `RawError`. `ErrorListResponse1.errors:
  list[str]`.
- Returns `SubscriptionResponse.subscription` (`Subscription`, itself `Optional` → assert present):
  - `.id:int` → **`subscriptionId`** (top-level in response). Assert not UNSET (else outcome unknown).
  - `.state` (`SubscriptionStateOrStr`, open enum) → map to outcome (see status map below).
  - `.current_period_ends_at` (`OptionalNullable[RFC3339DateTime]`) → **next billing date**.
  - `.product` (`Product`) → `.handle`, `.name`, `.price_in_cents`.
  - `.customer` (`Customer`), `.current_period_started_at`, `.total_revenue_in_cents`.

### OP6 — list a customer's subscriptions  →  GET /api/my-subscriptions (and idempotency belt)
- `client.customers.list_customer_subscriptions(customer_id: int)` → `list[SubscriptionResponse]`.
  Positional `customer_id`.
- Error: **Case B**, `RawError`.
- Each `.subscription` mapped like OP5's response.

### SubscriptionState → outcome map (from enums/subscription_state.py; open enum)
`status_from_state(state)`:
- done/live: `active`, `trialing`, `assessing` → **"active"** (usable)
- pending: `pending`, `awaiting_signup` → **"pending"** (accepted, not yet live)
- problem (still current, not duplicable): `past_due`, `soft_failure`, `paused`, `on_hold`,
  `suspended`, `unpaid` → **"problem"** (treat as CURRENT for idempotency — a live claim)
- ended: `canceled`, `expired`, `failed_to_create`, `trial_ended` → **"ended"**
- `str()` unknown (newer than SDK) → **"unknown"** (neither live nor ended → needs review; do not
  silently treat as ended)
- For idempotency, "current" = outcome in {active, pending, problem, unknown}. Only {ended} frees a
  (user, plan) pair to resubscribe.

### Client-chosen reference / idempotency invariants
- **Customer reference** = `oscar-user-<user.pk>` — stable per Django user; provider enforces uniqueness
  → dedupes customers across concurrent creates.
- **Subscription reference** = `oscar-user-<user.pk>-<plan_handle>` — recorded, used for reconciliation.

---

## CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| `product_handle` given to create_subscription must be one a plan listing returns | OP5 ← OP2 | implementation: validate requested `planHandle` against OP2 list before OP5 |
| `customer_id` given to create_subscription must be an existing customer's id | OP5 ← OP3/OP4 | implementation: ensure-customer runs before create |
| a (user, plan) may hold only one non-ended subscription | OP5 ← OP6 + local claim row | implementation: durable claim row (unique partial index) + provider list belt |
| family handle in settings must resolve to a real family id | OP1 | implementation: 404/500 if unresolved |

---

## Local persistence — the durable claim row (python-configuration-resilience: one operation, one row)

`create_subscription` behind `POST /api/subscriptions` is a read-then-create across the provider; an
in-process guard cannot close the double-click / concurrent race. One durable row per logical
subscribe, keyed by (user, plan_handle), records **that we asked** (provider stays record of what
exists). This is an operation-tracking table, NOT a parallel copy of Oscar/Maxio domain models.

Model `MaxioSubscription` (app `subscriptions`):
- `user` FK (AUTH_USER_MODEL), `plan_handle` CharField, `customer_reference`, `subscription_reference`.
- `provider_customer_id` (int, null), `provider_subscription_id` (int, null),
- `status` CharField in {sending, active, pending, problem, ended, failed, unknown}, `state_raw`,
- `current_period_ends_at` (nullable), `price_in_cents`, `plan_name`, timestamps.
- **Partial unique constraint:** unique (user, plan_handle) WHERE status NOT IN ('ended','failed') —
  one live claim per pair; `sending`/`unknown`/live all hold it. (SQLite supports partial unique index.)

Subscribe flow (view is `@transaction.non_atomic_requests`; commit the claim BEFORE the provider call):
1. Validate `planHandle` against OP2 (else 400/404).
2. Ensure customer (OP3→OP4, provider-unique reference) → `customer_id`.
3. **Claim:** `atomic()` savepoint → create row status="sending". IntegrityError ⇒ a claim already
   exists → load it: if it already has a provider_subscription_id, re-read via provider and return it
   (idempotent 200); if status "sending" and fresh → return 202 in-progress; if "sending" stale or
   "unknown" → reconcile by listing customer subs filtered by reference, settle or mark unknown.
4. Call OP5 (remittance) with the subscription reference. On:
   - Success → set provider_subscription_id, status=status_from_state(state), fields; commit; return
     201 with `subscriptionId`.
   - `ErrorListResponse1` 422 mentioning existing/duplicate → look up existing sub for customer+plan
     (OP6 filter by reference/product handle), settle; else surface 422.
   - NEVER_SENT transport (ConnectError/ConnectTimeout/PoolTimeout/ProxyError) → status=failed
     (release claim), 502.
   - RequestError / ValidationError (may have landed) → reconcile via OP6; if found settle, else
     status=unknown, 504.
5. `GET /api/my-subscriptions`: ensure-customer (lookup only; if no customer → empty list), OP6, map;
   also reflect local rows.

## Error boundary (python-error-handling)

Central translator `_call()` / service methods raise app exceptions mapped to HTTP:
- `OAuthProviderError` — N/A (Basic auth, no OAuth) but check defensively → 502.
- 401/403 → 502 (our credentials, not caller's). 429 → 503.
- typed 4xx (400/404/409/422) with known body → caller-fault status + safe message.
- ValidationError (decode) → 502 on success-body, but for a rejection body → surface 422; guard members.
- transport never-sent → 502; may-have-landed → 504 (+ reconcile).
- unmapped → 502. Always `raise ... from e`. Never leak `str(e)`/traceback to caller.

## API surface (session auth, request.user)

- `GET  /api/subscription-plans` → `{ "plans": [ { planHandle, name, priceInCents, priceFormatted,
  interval, intervalUnit, description }, ... ] }`. Login required.
- `POST /api/subscriptions` body `{ "planHandle": "eshop-pro" }` → 201 `{ subscriptionId, planHandle,
  state, status, nextBillingDate, priceInCents, ... }` (subscriptionId top-level). Idempotent.
- `GET  /api/my-subscriptions` → `{ "subscriptions": [ { subscriptionId, planHandle, planName, state,
  status, nextBillingDate, priceInCents }, ... ] }`.
- Auth: Django session login (`login_required`-style, but JSON 401 not redirect). Identity from
  `request.user`. CSRF: use DRF-less plain views; POST needs CSRF token OR mark endpoints with a
  session-auth + CSRF-exempt-for-API decision — decision: require CSRF (session), document token use;
  provide sessionid+csrftoken flow in the verification guide.

Decision on framework: **plain Django JSON views** (no DRF dependency added) — the sandbox has no DRF.
Each capability is its own view/URL (separately invocable).

## REQUIRED READING (companions loaded before implementation)
- MUST load python-error-handling — error boundary. **loaded**
- MUST load python-client-initialization — client construct/lifetime. **loaded**
- MUST load python-configuration-resilience — server config + durable-row idempotency. **loaded**
- MUST load python-calling-endpoints — call shapes, response modes. **loaded**
- MUST load python-models — UNSET, open enums, RFC3339DateTime, remittance enum. **loaded**
- MUST load python-authentication — basic_auth, secret loading. **loaded**
- MUST load python-testing — transport-seam stub for app tests. **loaded**

## Assumptions & Blockers
- **Assumption (verified, not blocker):** remittance collection is the intended card-free path; task's
  "payment method not required" is inaccurate for the seeded $299/$29 plans. Proceeding with remittance.
- No DRF in repo → plain Django views. No new infra (one app, one migration, SQLite).
- Metered `api-call` component: out of scope for the hero flow (subscribe/plans/read-back); not needed.
