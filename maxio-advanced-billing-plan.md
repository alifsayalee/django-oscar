# Maxio Advanced Billing — subscription billing for the django-oscar sandbox

## Scope

An additive subscribe flow on the `sandbox/` site, with Maxio as the billing system of record.
The existing basket/checkout flow is not touched.

| Route | Auth | Purpose |
|---|---|---|
| `GET  /api/subscription-plans` | public | live plans in `MAXIO_DEFAULT_PRODUCT_FAMILY`, each with `planHandle` |
| `POST /api/billing-customer` | session | idempotently ensure the Maxio customer for `request.user` |
| `POST /api/subscriptions` `{"planHandle": …}` | session | ensure customer + enroll; top-level `subscriptionId` |
| `GET  /api/subscriptions/<id>` | session | read one of the caller's subscriptions, live |
| `GET  /api/my-subscriptions` | session | the caller's subscriptions, live from Maxio, plus unresolved local claims |

## Repo survey (conventions to imitate)

- Sandbox apps live in `sandbox/apps/` and import as `apps.<name>` (exemplar: `sandbox/apps/sitemaps.py`,
  imported in `sandbox/urls.py` as `from apps.sitemaps import …`). New app: `sandbox/apps/subscriptions/`
  registered in `INSTALLED_APPS` as `apps.subscriptions.apps.SubscriptionsConfig`.
- URLs: `sandbox/urls.py` uses `path()` + `include()`; `/api/` goes **outside** `i18n_patterns` (no `/en/` prefix).
- Settings: `sandbox/settings.py` reads the environment through `env = environ.Env()` (`env.bool`, `env.list`).
- User model: Django's default `auth.User` (sandbox sets no `AUTH_USER_MODEL`); reference it through
  `oscar.core.compat.AUTH_USER_MODEL` like `sandbox/apps/user/models.py`. Auth = Django session login
  (Oscar's `/en/accounts/login/`); CSRF middleware stays on — POSTs need `X-CSRFToken`.
- Host is **sync** (Django under WSGI, `runserver`) → **sync client**.
- Tests: the repo's `tests/` targets `tests.settings` (library tests). The app's tests live in
  `sandbox/apps/subscriptions/tests.py`, Django `TestCase` + `self.assert*` (Oscar's own style), run with
  `sandbox/manage.py test apps.subscriptions`.
- Toolchain: `py -3.11 -m venv venv`, `venv\Scripts\pip install -e .[test]`; SDK installed into the venv from
  `git+https://github.com/context-plugins/maxio-python-sdk.git@main` (v1.0). No type checker configured →
  `mypy --strict` on the SDK-facing modules (`gateway.py`, `plans.py`-level logic) with `--ignore-missing-imports`.
- Baseline: `sandbox/manage.py check` clean on untouched tree.

## Credentials / environment

- Settings (in `sandbox/settings.py`, env-read, empty defaults so import never raises):
  `MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`, `MAXIO_DEFAULT_PRODUCT_FAMILY`, `MAXIO_BASE_URL` (optional),
  plus `MAXIO_ENVIRONMENT` (default `us`) and `MAXIO_TIMEOUT_SECONDS` (default 10).
- Missing-credential check lives in the client factory (raises `ImproperlyConfigured`, which the views
  turn into 503 `billing_not_configured`), never at import.
- Auth: `basic_auth=BasicAuthCredentials(username=<API key>, password="x")` — README: `curl -u <api_key>:x`.
- Environment: explicit map `{"us": "us", "eu": "eu"}` on `MAXIO_ENVIRONMENT.lower()`; anything else →
  `ImproperlyConfigured` (never fall through to the silent `"us"` default). `maxio_api_gateway` is not
  supported (it needs bearer auth + connector).
- Base URL: `MAXIO_BASE_URL` set → `server_config={"production": {env: {"base_url": MAXIO_BASE_URL}}}` verbatim;
  else `{"production": {env: {"site": MAXIO_SITE_SUBDOMAIN}}}` (template `https://{site}.chargify.com` for us).
- Smoke (read-only, real key): `list_products_for_product_family("handle:eshop-subscribe")` → 2 products
  (basic-plan 2900 / eshop-pro 29900, interval 1 month, not archived); `read_customer_by_reference(unknown)` → 404
  empty body; `find_subscription(reference=unknown)` → 404; `list_customers` → 200. No 403-gated operation.

## CONTRACT SHEET

**Client**: `MaxioAdvancedBillingClient` (sync), keyword-only ctor: `environment`, `timeout`, `server_config`,
`custom_http_client`, `basic_auth`. Held as a lazily-built module global (lock-guarded, built on first use in the
worker, i.e. after any fork), closed via `atexit`. Transport = `LoggingTransport(HttpxClient(timeout=T))` passed as
`custom_http_client` (so the timeout is set on the transport; client `timeout=` would not reach the wire). Logs
method, URL, status, ms — never headers/bodies. Imports: `maxio_advanced_billing` (client),
`maxio_advanced_billing.core` (`ApiError`, `RawError`, `BasicAuthCredentials`, `HttpxClient`, `HttpRequest`,
`HttpResponse`, `UNSET`, `UnsetType`), `maxio_advanced_billing.models` (`ErrorListResponse1`,
`CustomerErrorResponse1`, `SubscriptionResponse`, `CustomerResponse`, `ProductResponse`, `Subscription`, `Product`),
`maxio_advanced_billing.models.enums` (`SubscriptionState`).

Every keyword-only parameter has a real default → never pass defensive `None`s. Every call below may also take
`request_options=` (keys `timeout`, `extra_headers`). **The SDK performs no retries** (see Resilience).
None of the in-scope operations returns `None`.

| # | Operation (all server `production`, auth `basic_auth`) | Signature (positional \| after `*`) | Returns | `ApiError.error` union |
|---|---|---|---|---|
| 1 | `product_families.list_products_for_product_family` — `GET /product_families/{product_family_id}/products.json` | `product_family_id: str` \| `page=1, per_page=20, include_archived=None, …` | `list[ProductResponse]` | `str` [404] · `RawError` |
| 2 | `customers.read_customer_by_reference` — `GET /customers/lookup.json` | `reference: str` (query, positional) | `CustomerResponse` | Case B: `RawError` (404 = not found, empty body) |
| 3 | `customers.create_customer` — `POST /customers.json` | \| `body: CreateCustomerRequest\|Dict` | `CustomerResponse` | `CustomerErrorResponse1` [422] · `RawError` |
| 4 | `subscriptions.create_subscription` — `POST /subscriptions.json` | \| `body: CreateSubscriptionRequest\|Dict` | `SubscriptionResponse` | `ErrorListResponse1` [422] · `RawError` |
| 5 | `subscriptions.find_subscription` — `GET /subscriptions/lookup.json` | \| `reference: str` | `SubscriptionResponse` | `RawError` [404, other] |
| 6 | `subscriptions.read_subscription` — `GET /subscriptions/{subscription_id}.json` | `subscription_id: int` \| `include=None` | `SubscriptionResponse` | Case B: `RawError` |
| 7 | `customers.list_customer_subscriptions` — `GET /customers/{customer_id}/subscriptions.json` | `customer_id: int` | `list[SubscriptionResponse]` | Case B: `RawError` |
| 8 | `sites.read_site` — `GET /site.json` | \| (none) | `SiteResponse` (`site: Site` required) | Case B: `RawError` |

**Revision after the live run (2026-09-24):** with collection left at the provider default, Maxio answered
`422 {"errors": ["No payment method was on file for the $299.00 balance"]}` — the plans do not *require* a
payment method, but `automatic` collection still charges one at signup. This API captures no card, so #4 now sets
`payment_collection_method` to invoice billing: `CollectionMethod.REMITTANCE` on Relationship Invoicing sites,
`CollectionMethod.INVOICE` on legacy Statements sites (docstring of `CreateSubscription.payment_collection_method` /
`CollectionMethod`). The architecture is read once per client from #8 `Site.relationship_invoicing_enabled:
Optional[bool]` (UNSET → remittance, the current architecture).

**Revision (host):** the sandbox sets `ATOMIC_REQUESTS: True`, which kept claim rows uncommitted during the Maxio
call (and held SQLite's write lock → `database is locked` under a double-click). The API views are
`transaction.non_atomic_requests`, so each claim commits before the call it guards.

Row details:

1. `product_family_id` = `"handle:" + MAXIO_DEFAULT_PRODUCT_FAMILY` (docstring: "id or its handle prefixed with
   `handle:`"). `per_page=200` (docstring max 200); bounded page loop (MAX_PAGES=10, `truncated` logged). Omit
   `include_archived` (archived excluded) and also skip any product whose `archived_at` is set. Members used
   (`ProductResponse.product: Product` required): `Product.handle: OptionalNullable[str]`, `name: Optional[str]`,
   `description: OptionalNullable[str]`, `price_in_cents: Optional[int]`, `interval: Optional[int]`,
   `interval_unit: Optional[IntervalUnitOrStr]` (`day`/`month`), `archived_at`, `id`. A product without a handle or
   `price_in_cents` is skipped (cannot be subscribed to by handle / priced).
2. 404 → "not found" (`None`); any other failure propagates through the ladder (never mapped to absence).
3. `CreateCustomer` required: `first_name: str`, `last_name: str`, `email: str`. Set: `reference: Optional[str]` —
   **always set**, our claim reference (api-reference: "If provided, the `reference` value must be unique" → the
   provider rejects a duplicate, so a resend under the SAME reference is safe). Every other member UNSET (omit →
   provider/site defaults: locale, tax, surcharging, branding etc.). Result members asserted:
   `CustomerResponse.customer.id: Optional[int]` must be an int (UNSET → outcome unknown); `reference` echo checked.
4. `CreateSubscription` set: `product_handle` (plan handle from #1), `customer_id` (from #2/#3), `reference`
   (**always**, our claim reference; "reference value (provided by your app) for the subscription itself").
   Omitted on purpose (omit → provider default): `product_price_point_*` (product default price point),
   `payment_collection_method` (site/product default), `next_billing_at`/`initial_billing_at`/`previous_billing_at`/
   `activated_at`/`canceled_at`/`import_mrr` (import/migration only), coupons, components, payment profile (plans do
   not require a payment method). Result: `SubscriptionResponse.subscription: Optional[Subscription]` — UNSET →
   outcome unknown. Members asserted/used: `id: Optional[int]` (must be int), `state: Optional[SubscriptionStateOrStr]`,
   `product.handle`, `product_price_in_cents: Optional[int]`, `next_assessment_at: OptionalNullable[RFC3339DateTime]`,
   `current_period_ends_at`, `created_at`, `activated_at`, `reference: OptionalNullable[str]`, `customer.id`.
   Duplicate-reference behaviour of subscriptions is **UNVERIFIED** (docs do not say subscription references are
   unique) → on an unresolved outcome we never resend; the claim stays `unknown` and is re-checked by lookup (#5).
5. Our reconciliation lookup (reference we sent). 404 → not found (`None`).
6. `subscription_id: int`, `include` omitted. Ownership check: `subscription.customer.id` must equal the caller's
   Maxio customer id, else 404.
7. The caller's live list for `/api/my-subscriptions`.

**Status mapping — `SubscriptionState` (models/enums/subscription_state.py), by name, one function:**

| member(s) | our outcome | meaning |
|---|---|---|
| `ACTIVE`, `TRIALING` | `done` | in effect, nothing outstanding |
| `PENDING`, `ASSESSING`, `AWAITING_SIGNUP` | `pending` | accepted, not in effect yet |
| `PAST_DUE`, `SOFT_FAILURE`, `UNPAID`, `PAUSED`, `ON_HOLD`, `SUSPENDED` | `needs_attention` | exists, not done |
| `CANCELED`, `EXPIRED`, `TRIAL_ENDED` | `ended` | end of life (claim released) |
| `FAILED_TO_CREATE` | `failed` | signup failed (claim released) |
| anything else (open enum `str`) | `unknown` | neither done nor failed |

Only `done` answers 201; everything else answers 202 with its status.

**Decode failures** (`pydantic.ValidationError` / `ValueError`) raise in both modes and are not `ApiError`: on a
write → outcome unknown → reconcile by reference; on a read → 502 "unreadable".

**Environment**: `environment` passed explicitly from `MAXIO_ENVIRONMENT` (never the silent default).

## Error boundary (one ladder, `gateway.py`)

`ApiError`: 401/403 → `ProviderConfigError` (502); 429 → `ProviderUnavailable(503, outcome_unknown=False)`;
400/404/409/422 → `ProviderRejected(status, messages)` (caller's; messages read from the typed arm:
`ErrorListResponse1.errors: list[str]`, `CustomerErrorResponse1.errors` (`Errors1`, read via `to_dict`),
`RawError.text()` otherwise); ≥500 → `ProviderFailure(502, outcome_unknown=True)`; any other 4xx →
`ProviderFailure(502, outcome_unknown=False)`. `ValidationError`/`ValueError` → `ProviderUnreadable(502,
outcome_unknown=True)`. `httpx.ConnectError|ConnectTimeout|PoolTimeout|ProxyError` → `ProviderUnavailable(502,
outcome_unknown=False)` (never sent). `httpx.RequestError` → `ProviderUnavailable(504, outcome_unknown=True)`.
Views: `ProviderRejected` → its status with messages; everything else → its status + a fixed message; never
`str(e)`.

## Resilience / idempotency

- Reads (#1, #2, #5, #6, #7): retried in-house, max 3 attempts, backoff 0.3s·2^n, only on never-sent transport
  errors, `ReadTimeout`, and 429/502/503/504 (honouring `retry-after` up to 5 s). Worst case < 3×10 s + 1 s.
- Writes (#3, #4): **never retried blindly**. One durable claim row per provider write, inserted BEFORE the call,
  keyed by a reference we generate and send:
  - `BillingCustomer` (one per user, `UNIQUE(user)`, `UNIQUE(reference)`): statuses `sending|done|failed|unknown`.
    Loser of the claim: `done` → reuse; `sending` & fresh (< 60 s) → 409 in progress; stale `sending`/`unknown` →
    lookup by reference → found → settle; not found → re-claim with a conditional UPDATE and resend under the SAME
    reference (provider rejects duplicates); `failed` → conditional re-claim + resend.
  - `SubscriptionEnrollment` (`UNIQUE(reference)`, conditional `UNIQUE(user, plan_handle) WHERE status NOT IN
    ('failed','ended')`): statuses `sending|done|pending|needs_attention|needs_review|unknown|failed|ended`.
    Loser: answers from the held row through the same done gate; fresh `sending` → 409; stale `sending`/`unknown` →
    lookup by reference → settle if found, else stays `unknown` (504) — no resend.
  - Create outcome: never-sent → `failed` (claim released); 422 → lookup by reference first (a duplicate means an
    earlier attempt landed) → found → keep, else `failed` + 422 to caller; 401/403/429/other 4xx → `failed`;
    5xx / read timeout / decode failure → reconcile by reference → found → settle, else `unknown` (504).
  - Verify before settle: echoed `product.handle == planHandle` and `product_price_in_cents == plan price` (ints,
    cents) → mismatch → `needs_review`. Customer: echoed `reference` must equal ours.
  - Read paths refresh stored claims from Maxio (state → status; `ended`/`failed` release the claim).
- Timeout: 10 s per request (settings), set on the transport.

## CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| `planHandle` accepted by POST /api/subscriptions must be a live (non-archived) product handle returned by #1 for `MAXIO_DEFAULT_PRODUCT_FAMILY` | #4 ← #1 | `services.subscribe` resolves the plan from #1 before claiming; unknown → 404 |
| `customer_id` sent to #4 must be the id #2/#3 returned for the caller's claim reference | #4 ← #2/#3 | `services.ensure_customer` |
| subscription id read via #6 must belong to the caller's Maxio customer | #6 ← #2/#3 | `services.get_subscription` ownership check |
| lookup reference for #2/#5 must be exactly the reference sent on #3/#4 | #2 ← #3, #5 ← #4 | stored on the claim row before the call |

## Assumptions & Blockers

- No blockers. Minor: subscription-reference uniqueness unverified → handled by never resending (above).
- Minor: users with blank first/last name get the email local-part as name; a user without email gets 422.
- Minor: `/api/subscription-plans` is public (catalogue data); every other route requires a session.

## REQUIRED READING

- MUST load `python-client-initialization` — module-global sync client, transport ownership, close. (loaded)
- MUST load `python-authentication` — basic auth, secrets through settings, empty-default + factory check. (loaded)
- MUST load `python-calling-endpoints` — keyword-only boundary, status-from-provider gate. (loaded)
- MUST load `python-models` — `UNSET` vs `None`, open enums, `isinstance(…, UnsetType)` narrowing. (loaded)
- MUST load `python-error-handling` — the ladder above, transport split, decode failures. (loaded)
- MUST load `python-configuration-resilience` — claim/call/reconcile/verify/settle, no retries, timeout. (loaded)
- MUST load `python-testing` — stub transport via `custom_http_client`, error/transport/decode tests. (loaded)

## Build order

1. Build sandbox DB (Makefile steps). 2. settings + app skeleton + models + migration. 3. `gateway.py` (client,
transport, ladder, typed wrappers). 4. `services.py`. 5. `views.py`/`urls.py`. 6. tests (stub transport). 7. mypy
--strict, tests, `manage.py check`. 8. Live E2E via runserver on port 37780 with session login.
