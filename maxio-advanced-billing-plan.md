# Maxio Advanced Billing — Subscription billing integration plan

Additive, parallel capability on the django-oscar **sandbox** site: a logged-in shopper browses
Maxio plans, subscribes to one, and reads their subscriptions back. Maxio Advanced Billing is the
system of record. No change to the existing cart/checkout flow.

## Host decisions (from repo survey)

- **Sync**, not async. The sandbox is a classic WSGI Django site; views are sync functions. Use
  `MaxioAdvancedBillingClient` (sync). `close()` obligation handled by a long-lived module-scoped
  singleton (Django process lifetime), not per-request construction.
- New Django app lives at `sandbox/apps/subscriptions/`, importable as `apps.subscriptions`
  (the sandbox puts `sandbox/` on `sys.path`; existing `apps.user`, `apps.sitemaps` prove the
  pattern). Wired into `INSTALLED_APPS` in `sandbox/settings.py` and into `sandbox/urls.py`
  under `/api/`.
- Auth: Django session login (`request.user`); identity taken from the authenticated user.
  Endpoints reject anonymous callers with 401.
- Reuse Oscar's `User` model (`get_user_model()`) — no parallel user store. A small local model
  records the Maxio customer id ↔ Django user mapping (idempotency ledger).
- Settings read via env at runtime through `sandbox/settings.py`: `MAXIO_API_KEY`,
  `MAXIO_SITE_SUBDOMAIN`, `MAXIO_DEFAULT_PRODUCT_FAMILY`, `MAXIO_BASE_URL` (optional override).
  No secret values in the repo.

## Endpoints (each action separately invocable)

| Method | Route | Action |
|---|---|---|
| `GET`  | `/api/subscription-plans` | List available plans for the configured product family. Each entry carries `planHandle`. |
| `POST` | `/api/subscriptions` | Subscribe the authenticated user to a plan (`planHandle` in body, default `eshop-pro`). Returns `subscriptionId` top-level. Idempotent. |
| `GET`  | `/api/my-subscriptions` | List the authenticated user's subscriptions. |

## Auth to Maxio

HTTP Basic: `username = MAXIO_API_KEY`, `password = "x"` (confirmed by `api-reference.md` line 8175:
`curl -u <api_key>:x`). `environment="us"` (task `MAXIO_ENVIRONMENT=US`; SDK env literal is lowercase
`"us"`). Base URL: `production`/`us` template is `https://{site}.chargify.com` where `{site}`
defaults to `"subdomain"` — override with `server_config={"production":{"us":{"site": MAXIO_SITE_SUBDOMAIN}}}`.
When `MAXIO_BASE_URL` is set, override `{"production":{"us":{"base_url": MAXIO_BASE_URL}}}` verbatim instead.

## The hero flow — Subscribe (POST /api/subscriptions)

**Idempotency uses durable claim rows written BEFORE the provider call** (per
`python-configuration-resilience`: a read-then-create — including "ask Maxio first" — is a
check-then-act race an in-process lock cannot close). Two local ledger tables, both keyed to the
Django user, both writing the claim before calling Maxio, both reconciling by a reference we send:

1. **Ensure Maxio customer** — `MaxioCustomer` ledger, `OneToOne(user)` (unique = the claim).
   Reference `f"oscar-user-{user.pk}"`.
   - `get_or_create(user)` — the unique row is the claim; the loser reads the winner's row.
   - If `customer_id` already stored → return it (no Maxio call).
   - Else `read_customer_by_reference(reference)`: found → store id. `ApiError 404` →
     `create_customer(...)` (first/last/email from the Django user with fallbacks, + `reference`).
     A `422` "reference already used" is a *landing* → `read_customer_by_reference` again and store
     the id (never treat it as failure). Maxio enforces unique customer reference per site, so the
     reference is the cross-process guard even if two workers race the create.
2. **Claim the subscription** — `MaxioSubscription` ledger. Reference
   `f"oscar-sub-{user.pk}-{plan_handle}"`. Partial-unique `(user, plan_handle)` where status is
   non-terminal, so a double-click's second request hits `IntegrityError` and never calls create.
   - Won the claim (row inserted, status `sending`): continue.
   - Lost the claim (`IntegrityError`): load the held row. If it has a `subscription_id`, read it
     back from Maxio and return. Else reconcile via `find_subscription(reference)`; settle & return
     if found, otherwise return the held row's status (in-progress/unknown).
3. **Provider-side guard, then create.** Before create, `find_subscription(reference)` (catches a
   subscription created out-of-band / before the ledger existed); if a live one exists → settle &
   return it. Else `create_subscription(subscription=CreateSubscription(product_handle=<handle>,
   customer_id=<id>, reference=<reference>))`. Payment method not required, so no card attributes.
4. **Settle from the returned state, not from the fact an id came back** (per
   `python-calling-endpoints`): map `subscription.state` through the enum table below to
   done/pending/failed/unknown; store all four faithfully.
   - `NEVER_SENT` transport error → mark claim `failed`, raise (nothing landed).
   - may-have-landed (5xx / read-timeout / decode error) → reconcile with `find_subscription(reference)`;
     found → settle; not found → mark `unknown` (an empty lookup cannot prove it did not land).

Response: `subscriptionId` (top-level) + plan/price/state/next-billing-date detail block.

---

## CONTRACT SHEET (no open lookups)

Sync client class `MaxioAdvancedBillingClient` (alias `Client`), keyword-only constructor. The
sync and async clients do not mix. Client owns an httpx pool → hold one module-scoped singleton,
`close()` at process end (Django has no clean per-process teardown hook for the dev server; the OS
reclaims the pool — acceptable for the sandbox; we expose a `close()` for tests).

All operations below: `basic_auth OR bearer_auth`, `server = production`, trailing keyword-only
`request_options`. Every keyword-only param has a real default — no defensive `None` passing.

### Operations in scope

1. **`client.product_families.list_product_families()`** → `list[ProductFamilyResponse]`.
   Case B (`RawError`). Used to resolve family **handle → numeric id** (the family-products endpoint
   needs the numeric id, not the handle — verified by smoke: handle 404s with an HTML body that then
   fails to decode as the `str` error arm, raising `ValueError`). `ProductFamilyResponse.product_family`
   → `ProductFamily{ id:int, handle:str, name:str }`.

2. **`client.product_families.list_products_for_product_family(product_family_id: str, *, per_page=…, include_archived=…)`**
   → `list[ProductResponse]`. Case A: `ListProductsForProductFamilyErrorBody` = `str`[404] · `RawError`.
   Pass the **numeric family id as a str**. `ProductResponse.product` → `Product`:
   `id:int`, `name:str`, `handle:OptionalNullable[str]`, `price_in_cents:int`, `interval:int`,
   `interval_unit:IntervalUnitOrStr`, `archived_at:OptionalNullable[RFC3339DateTime]`. planHandle = `handle`.

3. **`client.customers.read_customer_by_reference(reference: str)`** → `CustomerResponse`.
   Case A: `FindCustomerBy…` → arms `RawError`[404, unmapped] (no typed arm). On not-found raises
   `ApiError` with `status_code == 404`. `CustomerResponse.customer` → `Customer{ id:int, first_name,
   last_name, email, reference }`.

4. **`client.customers.create_customer(*, body: CreateCustomerRequest|dict)`** → `CustomerResponse`.
   Case A: `CreateCustomerErrorBody` = `CustomerErrorResponse1`[422] · `RawError`.
   Body: `CreateCustomerRequest(customer=CreateCustomer(...))`.
   `CreateCustomer` **required**: `first_name: str`, `last_name: str`, `email: str`.
   Optional set here (each with purpose): `reference` (client-chosen idempotency key — always set,
   it is what a may-have-landed create is looked up by).

5. **`client.customers.list_customer_subscriptions(customer_id: int)`** → `list[SubscriptionResponse]`.
   Case B (`RawError`). Used for duplicate-guard and for `GET /api/my-subscriptions`.

6. **`client.subscriptions.create_subscription(*, body: CreateSubscriptionRequest|dict)`** →
   `SubscriptionResponse`. Case A: `CreateSubscriptionErrorBody` = `ErrorListResponse1`[422] · `RawError`.
   Body: `CreateSubscriptionRequest(subscription=CreateSubscription(...))`.
   `CreateSubscription` — all fields `Optional`/`UNSET`; set here:
   - `product_handle: Optional[str]` — the plan (stable handle). *(purpose: selects the plan;
     alternative is `product_id` but handles are stable, ids are not.)*
   - `customer_id: Optional[int]` — the ensured Maxio customer. *(purpose: attaches to existing
     customer instead of creating a nested one; avoids duplicate customers.)*
   No `credit_card_attributes` — plans have payment method not required.

7. **`client.subscriptions.read_subscription(subscription_id: int)`** → `SubscriptionResponse`.
   Case B. (Not strictly required but used to confirm-read after create if needed.)

### `SubscriptionResponse` / `Subscription` members used
`subscription: Optional[Subscription]`. `Subscription`:
`id:int`, `state:SubscriptionStateOrStr`, `current_period_ends_at:OptionalNullable[RFC3339DateTime]`
(**next billing date**), `next_assessment_at:OptionalNullable[RFC3339DateTime]`,
`current_billing_amount_in_cents:int`, `product:Optional[Product]`, `customer:Optional[Customer]`,
`created_at:RFC3339DateTime`.

### SubscriptionState enum (wire values) — outcome mapping for the create result
`pending`, `failed_to_create`, `trialing`, `assessing`, `active`, `soft_failure`, `past_due`,
`suspended`, `canceled`, `expired`, `paused`, `unpaid`, `trial_ended`, `on_hold`, `awaiting_signup`.
- **done/success (subscription is live)**: `active`, `trialing`, `assessing`, `pending`, `soft_failure`,
  `past_due`, `on_hold` — a subscription record exists and is not terminal.
- **failed**: `failed_to_create`.
- **terminal / not-live (excluded from duplicate-guard "active" set)**: `canceled`, `expired`,
  `failed_to_create`, `trial_ended`, `unpaid`, `suspended`.
- Anything unlisted → `unknown` (open enum: an unknown wire value passes through as plain `str`).
- Enum is **open** (`SubscriptionStateOrStr`): compare against `.value`/str, tolerate unknowns.

Duplicate-guard "already subscribed" set (non-terminal, treat as existing): `active`, `trialing`,
`assessing`, `pending`, `soft_failure`, `past_due`, `on_hold`, `paused`, `awaiting_signup`.

### Error unions / handling
- Decode failure raises `ValidationError`/`ValueError`, **not** `ApiError`, bypassing both response
  modes (seen in smoke when a 404 HTML body hit a `str` decoder). Boundary must also catch these.
- `httpx` transport errors arrive unwrapped (`httpx.HTTPError`).
- Parsed calls raise `ApiError` (`.error`, `.status_code`, `.response`); we use parsed calls
  throughout (no `None`-returning ops in scope, so no need for `with_raw_response`).
- Narrow `read_customer_by_reference` not-found by `status_code == 404` (its typed arm is only `RawError`).

### CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| `create_subscription.product_handle` must be a handle returned by `list_products_for_product_family` | create_subscription ← list_products_for_product_family | implementation (validate requested handle against the live plan list before creating; 400 otherwise) |
| `create_subscription.customer_id` / `list_customer_subscriptions.customer_id` must be an id from `read_customer_by_reference`/`create_customer` | create_subscription, list_customer_subscriptions ← customers.* | implementation (ensure-customer step) |
| family handle → numeric id: `list_products_for_product_family.product_family_id` must be an id from `list_product_families` (handle 404s) | list_products_for_product_family ← list_product_families | implementation (resolve + cache) |

### No retries
SDK performs **no retries**. For this additive sandbox feature we deliberately do not add
retry/backoff; idempotency (reference lookup + duplicate-guard) is the safety mechanism instead of
retries. Documented, not accidental.

---

## REQUIRED READING (load before implementing)
- MUST load `python-error-handling` — the error boundary (ApiError vs decode-failure vs transport). **[floor]**
- MUST load `python-client-initialization` — keyword-only ctor, pool ownership, singleton lifetime. **[floor]**
- MUST load `python-configuration-resilience` — base-URL/server override, no-retries, write-safety
  (duplicate-request / may-have-landed reasoning) before `create_customer`/`create_subscription`. **[floor]**
- MUST load `python-calling-endpoints` — positional/keyword-only split, response modes, "id ≠ success".
- MUST load `python-models` — `Optional[T]` = `T|UNSET` (not `None`), open enums, `to_dict`, dict-vs-model bodies.
- MUST load `python-authentication` — basic_auth shape, "omitted credential sends unauthenticated".
- MUST load `python-testing` — the transport seam for the throwaway/verification tests. **[floor]**

## Assumptions & Blockers
- **Assumption (minor):** `MAXIO_ENVIRONMENT=US` maps to SDK `environment="us"`. Confirmed against
  smoke test (US host answered). Proceed.
- **Assumption (minor):** next-billing-date = `current_period_ends_at`. Confirmed in smoke it equals
  `next_assessment_at` for these plans; expose both, label `current_period_ends_at` as next billing.
- **Assumption (minor):** plans have `payment method not required`, so create succeeds without card.
  Confirmed in smoke (existing subs are `active`). Proceed.
- No blockers.
