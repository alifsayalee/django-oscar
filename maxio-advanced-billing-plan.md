# Maxio Advanced Billing — subscription billing for the Oscar sandbox

## Scope

Additive recurring-billing capability on the runnable sandbox site (`sandbox/`), Maxio as system of record.
New Django app `sandbox/apps/subscriptions/`, wired into `sandbox/urls.py`, JSON endpoints under `/api/`:

| Route | Action | Maxio calls |
| --- | --- | --- |
| `GET /api/subscription-plans` | list plans in `MAXIO_DEFAULT_PRODUCT_FAMILY` | `product_families.list_products_for_product_family` |
| `GET /api/billing-customer` | show the caller's Maxio customer (404 if none yet) | `customers.read_customer_by_reference` |
| `POST /api/billing-customer` | ensure (idempotent) a Maxio customer for the caller | `read_customer_by_reference` → `create_customer` |
| `POST /api/subscriptions` | subscribe caller to `{"planHandle": ...}` (idempotent) | ensure-customer, `products.read_product_by_handle`, `customers.list_customer_subscriptions`, `subscriptions.create_subscription` |
| `GET /api/my-subscriptions` | caller's subscriptions (read from Maxio) | `customers.list_customer_subscriptions` |
| `GET /api/subscriptions/<id>` | one subscription, only if it is the caller's | `subscriptions.read_subscription` |

Auth: Django session login (`request.user`), unauthenticated → 401 JSON. Views are plain Django function views
returning `JsonResponse` (sandbox has no DRF). CSRF stays on for POST (session auth ⇒ CSRF is required).

## Repo survey

- Host is **sync Django under WSGI** → sync `MaxioAdvancedBillingClient`. No async anywhere.
- Sandbox apps exemplar: `sandbox/apps/user/` (plain package, models in `models.py`); URL exemplar `sandbox/urls.py`
  (`path(...)` list, Oscar under `i18n_patterns`; `/api/` goes OUTSIDE i18n_patterns, like `admin/`).
- Settings exemplar: `sandbox/settings.py` uses `environ.Env()` (`env.str`, `env.bool`).
- `ATOMIC_REQUESTS=True` — the subscribe view opts out (`transaction.non_atomic_requests`) so the claim row commits
  before the Maxio call.
- Oscar user model: `oscar.core.compat.AUTH_USER_MODEL` (Django's `auth.User` in the sandbox). No Oscar model covers
  recurring billing, so the app adds ONE ledger model (`MaxioSubscriptionEnrollment`) keyed to the Oscar user; plans are
  NOT mirrored into Oscar `Product` (Maxio is system of record).
- Toolchain: `py -3.11` venv at `venv/`, `pip install -e .[test]`; SDK installed from plugin `sdk/python/` (non-editable).
  Tests: `sandbox/manage.py test apps.subscriptions` (Django runner, sandbox settings). Type check: `mypy --strict` on
  the new app's Maxio layer (project has no mypy config; install mypy into venv).
- Smoke (read-only, real key): family products by `handle:eshop-subscribe` → `basic-plan` (2900 ¢, 1 month),
  `eshop-pro` (29900 ¢, 1 month), ids already differ from the brief ⇒ always use handles. `read_customer_by_reference`
  miss → `ApiError` 404, empty body. `read_product_by_handle`, `list_subscriptions` OK. No 403s.

## Configuration (settings.py, env-backed, no values in repo)

`MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`, `MAXIO_DEFAULT_PRODUCT_FAMILY`, `MAXIO_BASE_URL` (optional override, used
verbatim), `MAXIO_ENVIRONMENT` (`US`/`EU`, case-insensitive; unknown → `ImproperlyConfigured`), plus
`MAXIO_TIMEOUT` (default 10s), `MAXIO_CUSTOMER_REFERENCE_PREFIX` (default `oscar-sandbox-user-`).

## Contract sheet

**Client** — `MaxioAdvancedBillingClient` (sync). Keyword-only ctor: `environment="us"|"eu"` (omitting ⇒ `"us"`
silently — we always pass it), `timeout: float`, `server_config`, `retry_options`, `basic_auth`. Source: sdk-map.md.
- Auth: `basic_auth=BasicAuthCredentials(username=<api key>, password="x")` — `curl -u <api_key>:x` (README.md:32).
  Omitting it = silent no-auth; we fail fast with `ImproperlyConfigured` if key empty.
- Base URL: `server_config={"production": {<env>: {"site": subdomain}}}`; with `MAXIO_BASE_URL`:
  `{"production": {<env>: {"base_url": MAXIO_BASE_URL}}}` (ProductionUsConfig/EuConfig: `base_url`, `site`).
  `ServerConfig` is `extra="forbid"`.
- Lifetime: one lazily-built module-level client per process (post-fork safe), closed via `atexit`.
- Retries: keep SDK default (GET/HEAD/PUT/OPTIONS on 408/429/5xx, 3 retries). POST is NOT retried — we do not add POST
  because the plugin does not state that Maxio honours `Idempotency-Key`; idempotency is app-level (below).
  Timeout 10 s per attempt.

**Operations** (all: positional path/required params, keyword-only tail with real defaults — no defensive `None`s;
every call also has `with_raw_response`; none of ours return `None`)

| Operation | Signature (positional \| kw) | Returns | `ApiError.error` |
| --- | --- | --- | --- |
| `product_families.list_products_for_product_family` | `product_family_id: str` (id or `"handle:<h>"`) \| `page=1, per_page=20, include_archived=None, …` | `list[ProductResponse]` | `str` [404] \| `RawError` |
| `products.read_product_by_handle` | `api_handle: str` | `ProductResponse` | `RawError` (Case B) |
| `customers.read_customer_by_reference` | `reference: str` | `CustomerResponse` | `RawError` (404 on miss) |
| `customers.create_customer` | \| `body: CreateCustomerRequest\|Dict` | `CustomerResponse` | `CustomerErrorResponse1` [422] \| `RawError` |
| `customers.list_customer_subscriptions` | `customer_id: int` | `list[SubscriptionResponse]` | `RawError` |
| `subscriptions.create_subscription` | \| `body: CreateSubscriptionRequest\|Dict` | `SubscriptionResponse` | `ErrorListResponse1` [422] \| `RawError` |
| `subscriptions.read_subscription` | `subscription_id: int` \| `include=None` | `SubscriptionResponse` | `RawError` |

Imports: `ApiError, RawError, BasicAuthCredentials, UNSET, UnsetType` from `maxio_advanced_billing.core`; models from
`maxio_advanced_billing.models`; `SubscriptionState` from `maxio_advanced_billing.models.enums`.

**Models (members we set / read)** — `Optional[T]` = `T | UnsetType` (NOT `typing.Optional`; never pass `None`).
- `CreateCustomerRequest(customer: CreateCustomer)`; `CreateCustomer`: **required** `first_name: str`, `last_name: str`,
  `email: str`; we set `reference: Optional[str]`. No aliases.
- `CreateSubscriptionRequest(subscription: CreateSubscription)`; `CreateSubscription` (all Optional): we set
  `product_handle: str`, `customer_id: int`, `payment_collection_method: CollectionMethodOrStr` =
  `CollectionMethod.REMITTANCE` (setting `MAXIO_PAYMENT_COLLECTION_METHOD`). `CollectionMethod`: automatic, remittance,
  prepaid (Relationship Invoicing), invoice (legacy). Required: live run with the default (automatic) got
  422 "No payment method was on file for the $299.00 balance"; api-reference.md:8100 — remittance ⇒ invoice left open,
  no payment attempted. Site has `relationship_invoicing_enabled=True` (`sites.read_site`). `defer_signup`/`dunning_communication_delay_enabled` default `False`.
- `CustomerResponse.customer: Customer` (required) — `Customer.id: Optional[int]`, `reference`, `email`,
  `first_name`, `last_name` (Optional/OptionalNullable).
- `ProductResponse.product: Product` (required) — `id`, `name`, `handle` (OptionalNullable), `description`
  (OptionalNullable), `price_in_cents: Optional[int]`, `interval: Optional[int]`, `interval_unit: Optional[IntervalUnitOrStr]`
  (`day`/`month`), `archived_at` (OptionalNullable dt), `require_credit_card`, `product_family: Optional[ProductFamily]`
  (`.handle: Optional[str]`).
- `SubscriptionResponse.subscription: Optional[Subscription]` (**not required** — assert it). `Subscription`: `id`,
  `state: Optional[SubscriptionStateOrStr]`, `product: Optional[Product]`, `customer: Optional[Customer]`,
  `product_price_in_cents: Optional[int]`, `current_billing_amount_in_cents`, `next_assessment_at` /
  `current_period_ends_at` (OptionalNullable RFC3339 dt), `activated_at`, `created_at`, `currency: Optional[str]`,
  `canceled_at`.
- `SubscriptionState` (open enum, `OrStr`): pending, failed_to_create, trialing, assessing, active, soft_failure,
  past_due, suspended, canceled, expired, paused, unpaid, trial_ended, on_hold, awaiting_signup. "Live" for our
  duplicate check = everything except canceled, expired, failed_to_create; unknown strings treated as live (safe side).
- `ErrorListResponse1.errors: list[str]` (required). `CustomerErrorResponse1.errors: Optional[ErrorsModel]` — the model
  only declares `per_page`/`price_point`; real keys arrive as preserved extras ⇒ render via `to_dict()`.
- Required-member assertions after each used call: customer `.customer.id` set; subscription `.subscription` and
  `.subscription.id` set; else "outcome unknown" 502.

**Failure mapping (one place, `maxio.py`)** → `MaxioError(status, message, outcome_unknown)`:
401/403 → 502 (our creds); 429 → 503; typed/raw 4xx → same 4xx with message; 5xx → 502 (outcome_unknown for writes);
`pydantic.ValidationError`/`ValueError` on decode → 502 outcome_unknown; `httpx.ConnectError|ConnectTimeout|PoolTimeout|ProxyError`
→ 502 never-sent; other `httpx.RequestError` → 504 outcome_unknown. Unknown-outcome subscribe ⇒ enrollment left
`pending`; next attempt reconciles by listing the customer's subscriptions before creating.

## Idempotency design

- Customer: deterministic `reference = MAXIO_CUSTOMER_REFERENCE_PREFIX + user.pk`; lookup-by-reference first, create on
  404; if create returns 422 (reference taken by a concurrent request) re-read by reference.
- Subscription: `MaxioSubscriptionEnrollment(user, plan_handle, status, maxio_subscription_id, ...)` with a partial
  unique constraint on (user, plan_handle) where status in (pending, active). Claim row committed first, INSERT-first
  (no SELECT before it in the same transaction — SQLite's deferred read→write upgrade fails with "database is locked"
  instead of waiting, observed live); a concurrent
  double-click hits IntegrityError → returns the existing enrollment (200 with its subscription, or 409 while pending).
  Before `create_subscription`, reconcile: an existing live Maxio subscription for this customer + product handle is
  adopted instead of creating a new one.

## Assumptions & Blockers

- No blockers. Plugin covers every capability required.
- Minor: one live subscription per (user, plan). Customer name falls back to username/email local part when the Oscar
  user has no first/last name. `payment method not required` on both plans ⇒ no payment profile sent.

## REQUIRED READING (loaded before implementation)

- MUST load `python-client-initialization` — module-level lazy client, close obligation, post-fork construction. ✔
- MUST load `python-authentication` — basic_auth silent-omission trap. ✔
- MUST load `python-calling-endpoints` — keyword-only tail, parsed vs raw mode. ✔
- MUST load `python-models` — `UNSET`/Optional semantics, open enums, never hand UNSET to JsonResponse. ✔
- MUST load `python-error-handling` — one `ApiError`, per-op unions, decode + httpx failures. ✔
- MUST load `python-configuration-resilience` — server_config nesting, retries on by default, timeout per attempt. ✔
- MUST load `python-testing` — stub transport seam via `custom_http_client`, `retry_options=0`. ✔
