# Maxio Advanced Billing — integration plan & contract sheet

Adds recurring-subscription billing to the django-oscar **sandbox** site as an additive,
parallel capability. Maxio Advanced Billing is the system of record for subscriptions; the
existing catalogue/basket/order flow is untouched.

## Host decisions (from the repo survey)

- **Sync**, not async. The sandbox is Django under WSGI (`sandbox/wsgi.py`, `uwsgi.ini`). Use
  `MaxioAdvancedBillingClient` (the sync client) and `client.close()`. Never the async client.
- **Client class:** `maxio_advanced_billing.MaxioAdvancedBillingClient`.
- **Client lifetime:** one lazily-built module global in `apps/subscriptions/maxio_client.py`,
  closed via `atexit`. Never per-request (a pool + would re-pay connection setup each time).
- **App layout:** new Django app `sandbox/apps/subscriptions/`, mirroring the existing
  `sandbox/apps/*` convention (exemplar: `sandbox/apps/user/` — plain app package; AppConfig
  name `apps.subscriptions`). Wired into `sandbox/settings.py` INSTALLED_APPS and
  `sandbox/urls.py`.
- **Auth of callers:** Django session login. Views check `request.user.is_authenticated`;
  unauthenticated → 401 JSON (not a login redirect). CSRF stays enforced (production-grade with
  session cookies); the verification guide shows sending `X-CSRFToken`.
- **No new heavy deps** (no DRF): plain Django `View` + `JsonResponse`.

## Credentials & configuration (read via Django settings, never hard-coded)

`sandbox/settings.py` reads, with empty-string defaults so import never raises:
`MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`, `MAXIO_DEFAULT_PRODUCT_FAMILY`, `MAXIO_BASE_URL`.
The missing-credential check lives in `build_client()`, not in settings.

- **Auth scheme:** HTTP Basic. Maxio/Chargify convention (confirmed in the SDK's
  `api-reference.md` line 8175: `curl -u <api_key>:x`): `BasicAuthCredentials(username=MAXIO_API_KEY, password="x")`.
- **environment:** `"us"` (the sandbox is US; the four required settings do not include an
  environment name, and `"us"` is the SDK default AB environment). **Omitting it is silently
  `"us"` anyway — we pass it explicitly.**
- **Base URL:** production/us template is `https://{site}.chargify.com`, `{site}` default
  `"subdomain"`. Override via `server_config`:
  - if `MAXIO_BASE_URL` set → `{"production": {"us": {"base_url": MAXIO_BASE_URL}}}` (verbatim);
  - else → `{"production": {"us": {"site": MAXIO_SITE_SUBDOMAIN}}}`.
  `ServerConfig` is `extra="forbid"`; nesting is the several-environments arm (server → env → field).
- **timeout:** set explicitly to 15.0s (default 30 is too long on a request path).

## Smoke test (done, read-only, against real sandbox) ✔

`environment="us"` + site override + Basic(api_key:"x") authenticates. `list_product_families`
returned one family (id resolved at run time); its products `basic-plan` (2900) and `eshop-pro`
(29900), both interval 1 month. `MAXIO_DEFAULT_PRODUCT_FAMILY` is a **handle** (not numeric) →
resolve to id via `list_product_families`. Missing-customer `read_customer_by_reference` → HTTP
404 `RawError`. IDs differ from the task table (re-seeded), confirming handles are the stable key.

## Endpoints (each separately invocable, under `/api/`, outside i18n_patterns)

| Method | Path | View | Purpose |
|---|---|---|---|
| GET | `/api/subscription-plans` | `PlansView` | list plans; each entry carries `planHandle` |
| POST | `/api/subscriptions` | `SubscribeView` | subscribe; returns top-level `subscriptionId` |
| GET | `/api/my-subscriptions` | `MySubscriptionsView` | caller's subscriptions |

## The hero flow: subscribe (idempotent, durable-claim pattern)

Per `python-configuration-resilience` "one operation, one durable row". A double-click must not
create two customers or two subscriptions.

Durable row `SubscriptionIntent` (new model + migration; this records *that we asked* — the
provider stays the record of what exists):
- `user` FK, `plan_handle`, `reference` (unique), `status`
  (`pending|done|failed|needs_review|unknown`), `maxio_customer_id`, `maxio_subscription_id`,
  `created_at`, `updated_at`.
- Partial-unique constraint: `UniqueConstraint(fields=[user, plan_handle],
  condition=Q(status__in=["pending","done"]))` → one LIVE claim per (user, plan); a
  failed/ended row does not block a fresh attempt. (SQLite supports partial indexes.)

`subscribe(user, plan_handle)`:
1. **Validate** `plan_handle` against the family's product handles (cross-op invariant) — also
   yields name/price for the response. Unknown handle → `PlanNotFound` (404).
2. **Claim first:** `reference = f"oscar-sub-{user.id}-{plan_handle}"`. Insert
   `SubscriptionIntent(status="pending")`; on `IntegrityError` (live claim exists) → load by
   reference and return that outcome (idempotent replay). No check-then-act gap.
3. **Ensure customer** (idempotent): `customer_reference = f"oscar-user-{user.id}"`.
   `read_customer_by_reference` → id; on 404 → `create_customer`; if create returns 422 whose
   body says the reference is taken (a concurrent create landed) → re-read by reference. Return id.
4. **Adopt-or-create.** Within the single-flight claim, first
   `list_customer_subscriptions(customer_id)` and adopt any live (active/trialing/pending)
   subscription to this product — defence in depth against a Maxio subscription our local
   records lost (DB reset, or a prior attempt that failed for us but landed there). This is
   safe (not the "list-then-create race") precisely because the durable claim serialises
   (user, plan). Otherwise **create** sending `reference` (so reconciliation-by-reference
   works): `create_subscription(body=CreateSubscriptionRequest(subscription=CreateSubscription(
   product_handle=plan_handle, customer_id=<id>, reference=reference, payment_collection_method="remittance")))`.
5. **Reconcile on unknown:** `NEVER_SENT` transport errors / 4xx → row `failed`, release, raise.
   5xx / read-timeout / decode error → `find_subscription(reference=reference)`; found → keep;
   not found → row `unknown`, raise `OutcomeUnknown`.
6. **Assert & settle:** require `resp.subscription` and `.subscription.id` present (else
   `MaxioUnreadable`). Row → `maxio_customer_id`, `maxio_subscription_id`,
   `status = status_from_provider(sub.state)`. Return `subscriptionId` + plan/price/state/next-billing.

`status_from_provider(state)` (SubscriptionState, open enum — handle unknown arm):
`active`,`trialing` → `done`; `pending`,`assessing`,`awaiting_signup` → `pending`;
`failed_to_create`,`canceled`,`expired` → `failed`; any other member/str → `unknown`
(never a blanket `failed` default). Plans have no trial and no card required, so `active` is
expected immediately.

`list_my_subscriptions(user)`: `read_customer_by_reference(f"oscar-user-{user.id}")`; on 404 →
`[]`. Else `list_customer_subscriptions(customer_id)` → map each `.subscription`.

`list_plans()`: resolve family id (cache in-process), `list_products_for_product_family(fam_id)`
→ one entry per product with `planHandle`.

---

## CONTRACT SHEET

**Sync client only** (`MaxioAdvancedBillingClient`, alias `Client`). Sync/async do not mix.
`close()` obligation, held as a lazy module global.

**Keyword-only boundary:** every operation below takes only keyword args after `*` except the
positional path params noted. Every keyword-only param has a real default — no defensive `None`s.

**No operation in scope returns `None`.** (create_subscription returns `SubscriptionResponse`,
not None; `override_subscription` — the only None-returning sub op — is NOT used.)

### Operations in scope

| Operation | Signature (relevant) | Returns (parsed) | Error union (Case) |
|---|---|---|---|
| `product_families.list_product_families` | `(*, ...)` all optional | `list[ProductFamilyResponse]` | `RawError` (Case B) |
| `product_families.list_products_for_product_family` | `(product_family_id: str, *, page=1, per_page=20, ...)` | `list[ProductResponse]` | `ListProductsForProductFamilyErrorBody` = `str`[404] \| `RawError` (Case A) |
| `customers.read_customer_by_reference` | `(reference: str, *, ...)` | `CustomerResponse` | `RawError` (Case B; 404 here = not found) |
| `customers.create_customer` | `(*, body: CreateCustomerRequest\|Dict\|None=None, ...)` | `CustomerResponse` | `CreateCustomerErrorBody` = `CustomerErrorResponse1`[422] \| `RawError` (Case A) |
| `customers.list_customer_subscriptions` | `(customer_id: int, *, ...)` | `list[SubscriptionResponse]` | `RawError` (Case B) |
| `subscriptions.create_subscription` | `(*, body: CreateSubscriptionRequest\|Dict\|None=None, ...)` | `SubscriptionResponse` | `CreateSubscriptionErrorBody` = `ErrorListResponse1`[422] \| `RawError` (Case A) |
| `subscriptions.find_subscription` | `(*, reference: str\|None=None, ...)` | `SubscriptionResponse` | `FindSubscriptionErrorBody` = `RawError`[404, unmapped] (Case A) |

### Request model members we set (required unless noted; all others left UNSET → provider default)

- `CreateCustomerRequest.customer: CreateCustomer` (required).
  `CreateCustomer`: `first_name: str`, `last_name: str`, `email: str` (**all required**);
  `reference: Optional[str]` — we SET it (client-chosen idempotency/lookup key). Nothing else set.
- `CreateSubscriptionRequest.subscription: CreateSubscription` (required).
  `CreateSubscription` — all members `Optional=UNSET`; we set exactly:
  - `product_handle: Optional[str]` — the plan handle (product selection; alt to product_id).
  - `customer_id: Optional[int]` — existing customer (alt to customer_reference/attributes).
  - `reference: Optional[str]` — client-chosen subscription reference (idempotency/lookup key).
  - `payment_collection_method: Optional[CollectionMethodOrStr]` — set to
    `CollectionMethod.REMITTANCE`. **Purpose:** override the default `automatic` (card)
    collection so the subscription can be created without a payment method on file. With
    `automatic` and a non-zero balance Maxio returns 422 "No payment method was on file"
    even on a "payment method not required" plan (confirmed against sandbox); remittance
    bills by invoice instead. Enum (`CollectionMethod`): `automatic, remittance, prepaid,
    invoice` (relationship-invoicing uses `remittance`).
  - Leave `product_price_point_*` UNSET → product's default price point. Leave all card/trial/
    billing-date fields UNSET → provider default.

`Optional[T]` here = `T | UnsetType` (NOT `typing.Optional`) — never pass `None` to clear; omit.

### Response members we read (assert the ones we depend on; guard UNSET before crossing boundary)

- `ProductFamilyResponse.product_family: ProductFamily` → `.id: int`, `.handle: str`.
- `ProductResponse.product: Product` → `.id`, `.handle` (OptionalNullable), `.name`,
  `.price_in_cents: int`, `.interval: int`, `.interval_unit: IntervalUnitOrStr`.
- `CustomerResponse.customer: Customer` → `.id: int`.
- `SubscriptionResponse.subscription: Optional[Subscription]` (**UNSET-guard: assert present**)
  → `.id: int` (**assert present on create**), `.state: SubscriptionStateOrStr`,
  `.next_assessment_at: OptionalNullable[RFC3339DateTime]` (next billing date),
  `.current_period_ends_at: OptionalNullable[RFC3339DateTime]`, `.product: Optional[Product]`,
  `.customer: Optional[Customer]`.

`SubscriptionState` members (open enum): `pending, failed_to_create, trialing, assessing,
active, soft_failure, past_due, suspended, canceled, expired, paused, unpaid, trial_ended,
on_hold, awaiting_signup`. `str(state)` yields the wire value (str-enum). Handle unknown via a
`str()` arm.

`RFC3339DateTime` is a `datetime` alias → `.isoformat()` (guard UNSET/None).

### Money

`price_in_cents` is authoritative `int`. Present `priceInCents` verbatim; format a display string
with `Decimal(cents)/100` at 2 places (site currency USD; there is no `Decimal` arm and no
currency field on `Product`, so USD is assumed and stated). Never `%f`/`round()`.

### Error translation ladder (one place, applied at every SDK call site)

Basic auth (no OAuth) → no `OAuthProviderError`. Branch on status:
- `ApiError`: `401/403` → `MaxioConfigError` (502, ours); `429` → `MaxioUnavailable(503)`;
  `400/404/409/422` (typed or raw) → `MaxioRejected(status, msg)` (caller fault, passthrough);
  else (5xx + unmapped) → `MaxioUnavailable(502)`. **404 on `read_customer_by_reference` is caught
  earlier as "not found", not an error.**
- `pydantic.ValidationError`/`ValueError` (decode) → `MaxioUnreadable` (success=502 unknown;
  error-body decode = rejection). On a write, unknown → reconcile by reference.
- `(ConnectError, ConnectTimeout, PoolTimeout, ProxyError)` → `MaxioUnavailable(502, outcome_unknown=False)`.
- `httpx.RequestError` (base, AFTER the tuple) → `MaxioUnavailable(504, outcome_unknown=True)`.
- SDK performs **no retries** — none added here (single user-facing call; the durable claim +
  reconcile-by-reference covers the write). Stated, not silently omitted.

### CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| `planHandle` accepted by subscribe must be a product handle of the configured family | `subscriptions.create_subscription` ← `product_families.list_products_for_product_family` | `subscribe()` step 1 validation |
| subscription `reference` used to reconcile == the one sent on create | `subscriptions.find_subscription` ← `subscriptions.create_subscription` | `subscribe()` steps 4–5 |
| customer `reference` used to look up == the one sent on create | `customers.read_customer_by_reference` ← `customers.create_customer` | `ensure_customer()` |
| a customer's subscriptions are read from the customer id resolved by reference | `customers.list_customer_subscriptions` ← `customers.read_customer_by_reference` | `list_my_subscriptions()` |

## REQUIRED READING (all loaded before implementation)

- `python-client-initialization` — MUST load (client construction/lifetime). ✔
- `python-authentication` — MUST load (Basic scheme, secrets from settings). ✔
- `python-calling-endpoints` — MUST load (call shape, status-not-just-id). ✔
- `python-models` — MUST load (UNSET/Optional, open enums, dates, money). ✔
- `python-error-handling` — MUST load (single ApiError, transport/decode kinds). ✔ (floor)
- `python-configuration-resilience` — MUST load (durable claim, reconcile, no retries). ✔ (floor)
- `python-testing` — MUST load (transport stub seam) before the test file. ✔ (floor)

## Assumptions (minor — decided, proceeding)

- `environment="us"` fixed (matches sandbox; not among the four required settings).
- Site currency USD for display formatting (`priceInCents` remains authoritative).
- Idempotency keyed on (user, plan_handle) live claim; resubscribing to a plan whose prior
  subscription was canceled returns the existing intent (acceptable for this scope; noted).
