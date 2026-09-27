# Maxio Advanced Billing — subscription billing for the django-oscar sandbox

## Scope

Additive subscription capability on the sandbox site (`sandbox/`), Maxio Advanced Billing as the
system of record. New Django app `sandbox/apps/maxio_billing/`, wired into `sandbox/urls.py` under
`/api/` (outside `i18n_patterns`, so no language prefix).

| Endpoint | Purpose |
| --- | --- |
| `GET  /api/session` | sets the CSRF cookie, returns `{authenticated, user, csrfToken}` |
| `POST /api/session` | Django session login (`authenticate` + `login`) — same backends the storefront uses |
| `GET  /api/subscription-plans` | active plans of `MAXIO_DEFAULT_PRODUCT_FAMILY`; each carries `planHandle` |
| `POST /api/billing-customer` | ensure (idempotently) the caller's Maxio customer — separately invocable step |
| `POST /api/subscriptions` | subscribe caller to `planHandle`; returns top-level `subscriptionId` |
| `GET  /api/my-subscriptions` | caller's subscriptions read back from Maxio + unresolved local write records |
| `GET  /api/subscriptions/<id>` | one subscription, ownership-checked against the caller's customer |

Identity: `request.user` (Oscar's `AUTH_USER_MODEL`, Django session auth). Unauthenticated → 401 JSON.
CSRF enforced on POSTs (session auth).

## Repo survey

| Convention | Pattern | Exemplar |
| --- | --- | --- |
| sandbox apps | package under `sandbox/apps/`, imported as `apps.<name>` | `sandbox/apps/sitemaps.py` (`from apps.sitemaps import …` in `sandbox/urls.py`) |
| URLs | `path()` entries in `sandbox/urls.py` | `sandbox/urls.py` |
| settings | `environ.Env()` reads in `sandbox/settings.py` | `sandbox/settings.py` (`env.bool('DEBUG', …)`) |
| user model | Oscar/Django `auth.User` via `get_user_model()` | `src/oscar/apps/customer/views.py` |
| transactions | `ATOMIC_REQUESTS = True` → our write views must be `non_atomic_requests` so claims commit before the provider call |
| sync/async | Django under WSGI, sync views → **sync `MaxioAdvancedBillingClient`** |

Toolchain: `py -3.11` venv at `venv/` (gitignored), `pip install -e .[test]`; SDK installed from its
repository (`maxio-advanced-billing 1.0`; its runtime deps `httpx`, `pydantic[email]`,
`typing-extensions` installed explicitly because the built wheel came out with empty `Requires`).
Tests for the new app: `cd sandbox && ../venv/Scripts/python manage.py test apps.maxio_billing`
(SQLite). Type check: `mypy --strict` over `sandbox/apps/maxio_billing` (mypy + django-stubs installed
into the venv). Baseline: repo's `tests/` suite needs PostgreSQL on :5432 (tests/settings.py) — not
available here, fails before and after; not attributable to this work.

Credentials verified present as env vars (`MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`, `MAXIO_ENVIRONMENT`
= `US`, `MAXIO_DEFAULT_PRODUCT_FAMILY`). Read-only smoke run from scratch: `list_products_for_product_family("handle:<family>")`
returned both seeded plans (IDs already differ from the brief — handles are the key);
`read_customer_by_reference` / `find_subscription` on a missing reference → `ApiError` 404 `RawError`.

## Contract sheet

### Client (sync — `MaxioAdvancedBillingClient`, root `maxio_advanced_billing`)

| Fact | Value | Source |
| --- | --- | --- |
| Constructor | keyword-only: `environment`, `timeout` (30.0 default, must be > 0), `server_config`, `custom_http_client`, `basic_auth`, `bearer_auth` | sdk-map.md *Getting a client* |
| Environment | `Literal["us","eu","maxio_api_gateway"]`, default `"us"` silently → always pass explicitly; map `MAXIO_ENVIRONMENT` (`US`/`EU`, case-insensitive) through an explicit dict, unknown → `ImproperlyConfigured` | `server/environment.py` |
| Server config | `{"production": {"<env>": {"site": subdomain}}}`; `MAXIO_BASE_URL` set → `{"production": {"<env>": {"base_url": url}}}` verbatim. `extra="forbid"` | sdk-map.md *Servers & auth*, `server/server_config.py` |
| Auth | `basic_auth=BasicAuthCredentials(username=<API key>, password="x")` (API key as username per the SDK's own README/pyproject request example `-u <api_key>:x`). Omitting it = silent no-auth → factory refuses to build without a key | sdk-map.md, pyproject description |
| Lifetime | one module-level client, built lazily (post-fork) on first use, closed via `atexit` | python-client-initialization |
| Transport | `HttpxClient(timeout=…)` from `maxio_advanced_billing.core`, wrapped in a logging transport (method, URL, status, ms — no headers/bodies); passed as `custom_http_client` → timeout set on the transport | `core/httpx_transport.py` |
| Retries | SDK does none. Reads: none added (fail fast, caller re-requests). Writes: never retried blind — safe write only | python-configuration-resilience |
| Response modes | parsed (raising) for every call; no `-> None` operation in scope | map pages |

### Operations in scope (all server `production`, auth `basic_auth` OR `bearer_auth`)

| Operation | Signature (positional \| `*` keyword-only; every kw-only has a real default — no defensive `None`s) | Returns | `ApiError.error` union |
| --- | --- | --- | --- |
| `sites.read_site` | `(*)` | `SiteResponse` (`site: Site` required; `Site.relationship_invoicing_enabled: Optional[bool]`, `Site.currency: Optional[str]`) | Case B `RawError` |
| `product_families.list_products_for_product_family` | `(product_family_id: str, *, page=1, per_page=20, …, include_archived: bool \| None = None, …)`; id is the numeric id or `"handle:<handle>"` (docstring) | `list[ProductResponse]` | `ListProductsForProductFamilyErrorBody` = `str` [404] \| `RawError` |
| `customers.create_customer` | `(*, body: CreateCustomerRequest \| Dict \| None = None)` — body always passed | `CustomerResponse` | `CreateCustomerErrorBody` = `CustomerErrorResponse1` [422] \| `RawError` |
| `customers.read_customer_by_reference` | `(reference: str, *)` | `CustomerResponse` | Case B `RawError` (404 = not found) |
| `customers.list_customer_subscriptions` | `(customer_id: int, *)` | `list[SubscriptionResponse]` | Case B `RawError` |
| `subscriptions.create_subscription` | `(*, body: CreateSubscriptionRequest \| Dict \| None = None)` — body always passed | `SubscriptionResponse` | `CreateSubscriptionErrorBody` = `ErrorListResponse1` [422] \| `RawError` |
| `subscriptions.find_subscription` | `(*, reference: str \| None = None)` | `SubscriptionResponse` | `FindSubscriptionErrorBody` = `RawError` [404, unmapped] |
| `subscriptions.read_subscription` | `(subscription_id: int, *, include=None)` | `SubscriptionResponse` | Case B `RawError` |

### Models (members the code sets or reads; `Optional[T]` = `T | UnsetType`, never pass `None`)

| Model | Members | Notes |
| --- | --- | --- |
| `CreateCustomerRequest` | `customer: CreateCustomer` (required) | |
| `CreateCustomer` | `first_name: str`, `last_name: str`, `email: str` **required**; `reference: Optional[str]` — **always set** (claim reference) | wire names = python names |
| `CustomerResponse` | `customer: Customer` (required) | |
| `Customer` | `id: Optional[int]`, `reference: OptionalNullable[str]`, `email: Optional[str]` | assert `id` set after create/lookup |
| `CreateSubscriptionRequest` | `subscription: CreateSubscription` (required) | |
| `CreateSubscription` | `product_handle: Optional[str]`, `customer_id: Optional[int]`, `payment_collection_method: Optional[CollectionMethodOrStr]`, `reference: Optional[str]` — **always set** (claim reference) | no payment method is captured, so collection must be invoice-based: `CollectionMethod.REMITTANCE` on a Relationship Invoicing site, `CollectionMethod.INVOICE` on a legacy Statements site (field docstring). Verified live: with the site default (`automatic`) Maxio answered 422 "No payment method was on file" |
| `SubscriptionResponse` | `subscription: Optional[Subscription]` — **may be UNSET on a 2xx** → treat as unreadable (outcome unknown) | |
| `Subscription` | `id: Optional[int]`, `state: Optional[SubscriptionStateOrStr]`, `reference: OptionalNullable[str]`, `product: Optional[Product]`, `customer: Optional[Customer]`, `product_price_in_cents: Optional[int]`, `currency: Optional[str]`, `current_period_ends_at` / `next_assessment_at` / `activated_at`: `OptionalNullable[RFC3339DateTime]`, `created_at: Optional[RFC3339DateTime]`, `updated_at` | assert `id` and `state`; `next_assessment_at` = next billing date |
| `ProductResponse` | `product: Product` (required) | |
| `Product` | `id`, `name`, `handle` (nullable), `description` (nullable), `price_in_cents: Optional[int]`, `interval: Optional[int]`, `interval_unit: Optional[IntervalUnitOrStr]`, `archived_at` (nullable), `require_credit_card: Optional[bool]` | skip entries with no handle or with `archived_at` set |
| `ErrorListResponse1` | `errors: list[str]` (required) | |
| `CustomerErrorResponse1` | `errors: Optional[Errors1]` | message rendered via `to_dict(exclude_unset=True)` |

`Optional[Any]` members in scope: none set by us.

### Status enum — `SubscriptionState` (`models/enums/subscription_state.py`, open: `SubscriptionStateOrStr`)

| Member (wire) | Outcome | Why |
| --- | --- | --- |
| `active` | done | normal, paid, in effect |
| `trialing` | done | valid trial subscription, in effect (seeded plans have no trial) |
| `pending`, `assessing`, `awaiting_signup` | pending | transient / not yet in effect |
| `past_due`, `soft_failure`, `unpaid`, `paused`, `on_hold`, `suspended` | pending | exists but something is wrong — not done, surfaced |
| `failed_to_create` | failed | signup failed |
| `canceled`, `expired`, `trial_ended` | failed | happened, then ended — no longer in effect |
| any other value / state absent | unknown | never done |

Customer create has no status field: outcome = done when the response carries `customer.id`,
otherwise unknown.

### Error boundary (python-error-handling)

One `ProviderError(status_code, message, outcome_unknown)`; `provider_error(status, error)` maps:
401/403 → 502 (our credentials), 429 → 503, typed 4xx body → same 4xx with provider messages,
other 4xx → same 4xx, 5xx/other → 502 `outcome_unknown` for 5xx. `ValidationError`/`ValueError` on
2xx → 502 unknown. Never-sent (`ConnectError`, `ConnectTimeout`, `PoolTimeout`, `ProxyError`) → 502
known; other `httpx.RequestError` → 504 unknown. Reads are guarded the same way.
`OutcomeUnknown` → 504 `outcomeUnknown: true`. `str(e)` never surfaced.

## OPERATION OUTCOMES

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/billing-customer` → `customers.create_customer` (or found by reference) | none on the model; presence of `customer.id` | id present → done → 200 `{customerId, outcome:"done"}`; id absent → unknown → 504 `outcomeUnknown`; claim in flight → sending → 202; stored failed → 409 | `services.customer_outcome` (status → outcome), `safe_write.SafeWrite._verify_and_complete` (completes from it), `views.answer` (outcome → HTTP), via `views.billing_customer` |
| `POST /api/subscriptions` → `subscriptions.create_subscription` (or found by reference) | `subscription.state` (`SubscriptionState`) | active/trialing → done → 201 (first) / 200 (repeat) with `subscriptionId`; pending/assessing/awaiting_signup/past_due/soft_failure/unpaid/paused/on_hold/suspended → pending → 202 with `subscriptionId`; failed_to_create/canceled/expired/trial_ended → failed → 409; price/plan echo mismatch → needs_review → 409; unlisted/absent state → unknown → 504; claim in flight → sending → 202 | `services.subscription_outcome` (every `SubscriptionState` member by name, default unknown), `services._subscription_answer`, `safe_write.SafeWrite._verify_and_complete` (echo check → needs_review, missing id → unknown), `views.answer` (done→201/200, pending/sending→202, failed/needs_review→409, else 504), via `views.subscriptions` |
| `POST /api/subscriptions` → customer step (same as row 1, runs first; subscription step only runs when it is done) | as row 1 | not done → answered through the same `answer`, subscription never attempted | `services.subscribe` (returns `SubscribeResult('customer', …)` unless `customer.record.outcome == DONE`), `views.subscriptions` → `views.answer` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| create customer | `maxio_billing.BillingWrite` row in the site database (SQLite/Postgres), keyed by `reference` = `<install prefix>:user:<pk>:customer` | the database `UNIQUE` constraint on `BillingWrite.reference` (insert-or-fail in its own committed transaction; a released `failed` claim is re-taken by a conditional `UPDATE … WHERE outcome='failed' AND provider_id=''`) | `IntegrityError` caught in `try_claim` → loser path (in-flight → 202, stored outcome → answered, stale/unknown → lookup only) | `models.BillingWrite.reference` (`unique=True`, migration `0001_initial`), `safe_write.try_claim` (insert-or-fail / conditional re-take), `safe_write.SafeWrite.run` loser branch; reference from `services.customer_reference` |
| create subscription | same table, `reference` = `<install prefix>:user:<pk>:subscription:<planHandle>` (or `…:subscription:key:<sha256(Idempotency-Key)[:24]>` when the caller sends `Idempotency-Key`) | same UNIQUE constraint / conditional UPDATE | same | `safe_write.try_claim`, `safe_write.SafeWrite.run` loser branch; reference from `services.subscription_reference`; `views.api_view` applies `transaction.non_atomic_requests` so the claim commits before the call |

Install prefix: `MAXIO_REFERENCE_PREFIX` setting when set; otherwise a per-database install id
(`BillingInstall` singleton row, uuid4, created by migration) — never a user id alone, because the
Maxio site is shared with other installs.

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| create customer | lookup: `customers.read_customer_by_reference(ref)` (404 → not found → still unknown). Also used after a 422 (reference may already exist) — only a found record settles it | the customer claim reference | `services.ensure_customer.find` (`read_customer_by_reference`, 404 → None via `services._not_found_is_none`), invoked by `safe_write.SafeWrite._check` |
| create subscription | lookup: `subscriptions.find_subscription(reference=ref)` (404 → not found → still unknown). Provider uniqueness of subscription `reference` is not documented in the SDK → `repeat_is_safe=False`, never resent | the subscription claim reference | `services.subscribe.find` (`find_subscription(reference=…)`, 404 → None, reference must match), invoked by `safe_write.SafeWrite._check` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| create customer | `BillingWrite(reference, user, kind="customer", outcome="sending", claimed_at)` committed | outcome, `provider_id` (customer id), `provider_time` (customer `created_at`), `detail` | before: `safe_write.try_claim` with `claim_fields` from `services.ensure_customer`; after: `safe_write.complete` (from `_verify_and_complete` / `_check` / `_send`) |
| create subscription | `BillingWrite(reference, user, kind="subscription", plan_handle, expected_price_in_cents, outcome="sending", claimed_at)` committed | outcome, `provider_id` (subscription id), `provider_time` (`created_at`), `provider_state`, `detail` | before: `safe_write.try_claim` with `claim_fields` from `services.subscribe` (plan handle, expected price); after: `safe_write.complete`; echo check in `safe_write.SafeWrite._verify_and_complete` against `sent=(plan.price_in_cents, plan.handle)` |

Echoed-amount check (subscription): the plan's `price_in_cents` read from Maxio at request time must
equal `subscription.product_price_in_cents`, and `subscription.product.handle` must equal the requested
handle; mismatch → `needs_review`.

## Assumptions & Blockers

- Minor: seeded plans need no payment method at signup, but the site's default collection is
  `automatic`, which Maxio refuses without a card on file (observed live, 422). Subscriptions are
  therefore created with invoice-based collection chosen from `sites.read_site` (read once per process).
- Minor: one subscription per (user, plan) unless the caller supplies `Idempotency-Key` — a
  double-click with no key is a repeat, not a second subscription.
- Minor: `MAXIO_ENVIRONMENT` accepts `US`/`EU`; the gateway environment (bearer auth) is out of scope.
- No blockers: the site database holds a claim that outlives the process (UNIQUE constraint).

## REQUIRED READING

| Step | Pointer |
| --- | --- |
| error boundary, never-sent vs unknown | MUST load python-error-handling (loaded) |
| client construction/lifetime | MUST load python-client-initialization (loaded) |
| safe write, server config, logging transport | MUST load python-configuration-resilience (loaded) |
| calls, status → outcome, `answer` | MUST load python-calling-endpoints (loaded) |
| UNSET / open enums / serialization | MUST load python-models (loaded) |
| basic auth wiring | MUST load python-authentication (loaded) |
| tests with stub transport | MUST load python-testing (loaded) |
