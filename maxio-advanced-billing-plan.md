# Maxio Advanced Billing — subscription billing for the Oscar sandbox

## Goal

Additive recurring-subscription capability on the `sandbox/` Django site, Maxio Advanced
Billing as system of record. JSON endpoints under `/api/`, Django session auth, caller identity
from `request.user` (Oscar's user model via `AUTH_USER_MODEL`).

| Route | Action |
|---|---|
| `GET  /api/subscription-plans` | list plans of `MAXIO_DEFAULT_PRODUCT_FAMILY` (each carries `planHandle`) |
| `GET  /api/billing-customer` | show the caller's Maxio customer link (no provider write) |
| `POST /api/billing-customer` | ensure a Maxio customer exists for the caller (idempotent) |
| `POST /api/subscriptions` | subscribe caller to `planHandle` (idempotent; returns top-level `subscriptionId`) |
| `GET  /api/subscriptions/<subscriptionId>` | read one of the caller's subscriptions from Maxio |
| `GET  /api/my-subscriptions` | caller's subscriptions from Maxio + any unsettled local claims |

## Repo survey (read-only)

- Host is **Django 5.2 under WSGI → sync client** (`MaxioAdvancedBillingClient`). No async anywhere.
- Sandbox apps are plain packages under `sandbox/apps/` (exemplar: `sandbox/apps/user/`); new app
  goes to `sandbox/apps/maxio_billing/` and into `INSTALLED_APPS` as `apps.maxio_billing`.
- URLs: `sandbox/urls.py` — plain `path()` list before the `i18n_patterns` block; API routes are
  added there (outside the language prefix) via `include('apps.maxio_billing.urls')`.
- Settings read env with `django-environ` (`env = environ.Env()`), exemplar `sandbox/settings.py`.
- `DATABASES['default']['ATOMIC_REQUESTS'] = True` → billing views that write claims MUST be
  `transaction.non_atomic_requests`, otherwise the claim row is not durable before the provider call
  and every status write rolls back on an exception.
- Login: Oscar `/<lang>/accounts/login/` form (`login-username`, `login-password`, `login_submit`),
  CSRF enforced; fixtures give `superuser@example.com` / `staff@example.com`, password `testing`.
- Toolchain: `py -3.11 -m venv venv`, `venv\Scripts\pip install -e .[test]`; SDK installed from
  git (`maxio-advanced-billing @ git+…@main`) — its built wheel lost its dependency metadata, so
  `httpx`, `pydantic[email]`, `typing-extensions` were installed explicitly at the pinned ranges.
  Tests: `sandbox/manage.py test apps.maxio_billing`; type check: `mypy --strict` with
  `django-stubs` (config kept outside the repo).

## Credentials / environment

Settings (all from env, empty default, never raise at import): `MAXIO_API_KEY`,
`MAXIO_SITE_SUBDOMAIN`, `MAXIO_DEFAULT_PRODUCT_FAMILY`, `MAXIO_BASE_URL` (optional verbatim
override), plus `MAXIO_ENVIRONMENT` (`US`→`"us"`, `EU`→`"eu"`, default `us`; unknown value →
`ImproperlyConfigured`, never silent fallback), `MAXIO_TIMEOUT_SECONDS` (default 15),
`MAXIO_PAYMENT_COLLECTION_METHOD` (default `remittance`). The missing-credential check lives in
the client factory, not settings.

## Smoke results (scratchpad, real sandbox credential)

- `list_products_for_product_family("handle:<MAXIO_DEFAULT_PRODUCT_FAMILY>")` → 2 products (`basic-plan` 2900,
  `eshop-pro` 29900, 1 month). **Numeric IDs already differ from the brief** — only handles are used.
- `read_customer_by_reference` / `find_subscription` for an unknown ref → `ApiError` 404 `RawError`, empty body.
- Duplicate customer `reference` → 422 `CustomerErrorResponse1`, `errors == ["Reference: must be unique - that value has been taken."]`.
- Duplicate subscription `reference` → 422 `ErrorListResponse1`, same message. **Both references are
  provider-enforced unique → a retry under the SAME reference is safe; a "taken" 422 = landed.**
- Subscribe without `payment_collection_method` → 422 `"No payment method was on file for the $29.00 balance"`.
  With `payment_collection_method="remittance"` → `active`, `next_assessment_at` one month out.
  (Smoke subscription 94490728 was canceled afterwards.)

## Contract sheet

Client: `from maxio_advanced_billing import MaxioAdvancedBillingClient` (sync). Keyword-only ctor:
`environment="us"|"eu"` (**omitting it is silently `"us"`** — always pass it), `timeout: float`,
`server_config={"production": {<env>: {"site": <subdomain>}}}` or, with `MAXIO_BASE_URL`,
`{"production": {<env>: {"base_url": <url>}}}` (ServerConfig is `extra="forbid"`), 
`basic_auth=BasicAuthCredentials(username=<api key>, password="x")` (`maxio_advanced_billing.core`;
README: `curl -u <api_key>:x`). Credentials keyword is optional at the type level → factory refuses
empty key. One long-lived lazily-built module client (thread-safe for sync), closed at `atexit`.
All ops: server `production`, auth `basic_auth OR bearer_auth`. Every keyword-only param has a
real default — no defensive `None`s. Trailing `request_options` (`timeout`, `extra_headers`).
**No retries in the SDK**; decode failure → `pydantic.ValidationError`/`ValueError` (not `ApiError`,
both modes); httpx exceptions arrive unwrapped. None of the ops below returns `None`.

| # | Operation | Signature (positional \| after `*`) | Returns | `ApiError.error` union | Members asserted |
|---|---|---|---|---|---|
| 1 | `product_families.list_products_for_product_family` | `product_family_id: str` ("id or `handle:`-prefixed handle") \| `page=1, per_page=20` (max 200), `include_archived` (unset ⇒ archived excluded) | `list[ProductResponse]` (`.product: Product`, required) | `ListProductsForProductFamilyErrorBody` = `str` [404] \| `RawError` | `product.handle` (skip entries without), `product.name`, `price_in_cents`, `interval`, `interval_unit` |
| 2 | `customers.read_customer_by_reference` | `reference: str` \| — | `CustomerResponse` (`.customer: Customer`, required) | `RawError` (Case B); 404 ⇒ not found | `customer.id` |
| 3 | `customers.create_customer` | — \| `body: CreateCustomerRequest` | `CustomerResponse` | `CreateCustomerErrorBody` = `CustomerErrorResponse1` [422] \| `RawError` | `customer.id`, `customer.reference == sent` |
| 4 | `subscriptions.create_subscription` | — \| `body: CreateSubscriptionRequest` | `SubscriptionResponse` (`.subscription: Optional[Subscription]`) | `CreateSubscriptionErrorBody` = `ErrorListResponse1` [422] (`errors: list[str]`) \| `RawError` | `subscription.id`, `.state`, `.reference`, `.product.handle`, `.product_price_in_cents`, `.customer.id` |
| 5 | `subscriptions.find_subscription` | — \| `reference: str` | `SubscriptionResponse` | `FindSubscriptionErrorBody` = `RawError` [404, other]; 404 ⇒ not found | as #4 |
| 6 | `customers.list_customer_subscriptions` | `customer_id: int` \| — | `list[SubscriptionResponse]` | `RawError` (Case B) | as #4 |
| 7 | `subscriptions.read_subscription` | `subscription_id: int` \| `include` (not used) | `SubscriptionResponse` | `RawError` (Case B) | as #4 + `.customer.id` ownership check |

Request models (`maxio_advanced_billing.models`; no wire aliases on any member used; nothing typed bare `Any`):

- `CreateCustomerRequest(customer: CreateCustomer)` — required. `CreateCustomer`: **required**
  `first_name: str`, `last_name: str`, `email: str`; set `reference: Optional[str]` — **always set**:
  our client-chosen, provider-unique reference (the lookup key). All other members left `UNSET`
  (omit → provider/site default).
- `CreateSubscriptionRequest(subscription: CreateSubscription)` — required. `CreateSubscription` (all `Optional`):
  - `product_handle` — the plan handle (required unless `product_id`).
  - `customer_id` — existing Maxio customer id.
  - `reference` — **always set**: our client-chosen, provider-unique subscription reference.
  - `payment_collection_method: CollectionMethodOrStr` (`maxio_advanced_billing.models.enums.CollectionMethod`:
    `AUTOMATIC`, `REMITTANCE`, `PREPAID`, `INVOICE`) — **omit → provider default (automatic, card
    required at signup)**. Set from `MAXIO_PAYMENT_COLLECTION_METHOD` (default `remittance`), because the
    plans take no payment method and automatic collection rejects signup without one (smoke).
  - Not set: `next_billing_at`, `initial_billing_at`, `previous_billing_at`, `activated_at`,
    `import_mrr`, `canceled_at` — **import/migration only**; `coupon_*`, `components`, price points,
    `customer_attributes` etc. — not in scope, omit → provider default.
- `Optional[T]` here is `T | UnsetType`, **never** `None`. Read responses with `isinstance(x, UnsetType)`.

Response members read (`Subscription`): `id: Optional[int]`, `state: Optional[SubscriptionStateOrStr]`,
`reference: OptionalNullable[str]`, `product: Optional[Product]`, `product_price_in_cents: Optional[int]`,
`current_period_ends_at` / `next_assessment_at: OptionalNullable[RFC3339DateTime]` (next billing date =
`next_assessment_at`, fallback `current_period_ends_at`), `activated_at`, `created_at`, `canceled_at`,
`customer: Optional[Customer]` (`id`, `reference`), `payment_collection_method`.

**Status mapping** — `SubscriptionState` (`maxio_advanced_billing.models.enums`), open enum, mapped
member by member in ONE function; default arm = `unknown`:

| Member(s) | Local status | Answer |
|---|---|---|
| `ACTIVE`, `TRIALING` | `active` (done) | 201 |
| `PENDING`, `ASSESSING`, `AWAITING_SIGNUP` | `pending` | 202 |
| `PAST_DUE`, `SOFT_FAILURE`, `UNPAID`, `PAUSED`, `ON_HOLD`, `SUSPENDED`, `TRIAL_ENDED` | `attention` (exists, not in good standing) | 202 |
| `CANCELED`, `EXPIRED` | `ended` | 200 (existing, ended) |
| `FAILED_TO_CREATE` | `failed` | 502 |
| anything else (unknown str) | `unknown` | 202 |

**Error boundary** (one ladder, `apps/maxio_billing/gateway.py`): `ApiError` 401/403 → 502 config;
429 → 503; 400/404/409/422 typed or raw → caller's (422 with the provider's message list); 5xx and
unmapped → 502 with `outcome_unknown` for 5xx on writes; `ValidationError`/`ValueError` on decode →
unreadable (outcome unknown on writes); `httpx.ConnectError|ConnectTimeout|PoolTimeout|ProxyError` →
502 never-sent (known); other `httpx.RequestError` → 504 outcome unknown. Never `str(e)` to the caller.

**Duplicate protection** (durable claim rows in the app's own DB — one table each, one migration):

- `BillingCustomer`: `user` OneToOne (unique) + `reference` unique (`oscar-<uuid4>`), `status`
  (`sending|linked|unknown|failed`), `maxio_customer_id`. Claim → call → reconcile by
  `read_customer_by_reference` → settle. 422 "Reference … taken" ⇒ landed ⇒ lookup.
- `SubscriptionEnrollment`: `user`, `plan_handle`, `reference` unique (`oscar-sub-<uuid4>`), `status`,
  `maxio_subscription_id`, `state`, `next_billing_at`, price echo. **UniqueConstraint(user, plan_handle)
  WHERE status NOT IN ('failed','ended')** — the constraint decides the double-click winner. The loser
  never calls create: fresh `sending` → 202 in progress; stale `sending`/`unknown` → `find_subscription`
  by the row's reference → settle, or still `unknown`.
- Customer step and subscription step are claimed separately (one claim per provider write).
- Verify before settle: echoed `product.handle == plan_handle`, `product_price_in_cents ==` listed
  plan price, `customer.id ==` our customer ⇒ else `needs_review` (kept, visible, not "active").
- Retry: none automatic on writes. Reads (plans list) are cached 60 s; no retry loop.
- Pagination: plans loop bounded (`per_page=200`, max 5 pages; `truncated` flag in the response).

## CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| `planHandle` accepted by `POST /api/subscriptions` must be a handle returned by the family listing | `subscriptions.create_subscription` ← `product_families.list_products_for_product_family` | service: validate against (cached) plan list; unknown → 422 before any claim |
| `customer_id` sent on create must be the id `create_customer`/`read_customer_by_reference` returned for OUR reference | `create_subscription` ← `create_customer` / `read_customer_by_reference` | service: taken only from the `linked` `BillingCustomer` row |
| subscription read by id must belong to the caller's customer | `read_subscription` ← `create_customer` | view: `subscription.customer.id == caller's maxio_customer_id` else 404 |
| `find_subscription(reference)` / `read_customer_by_reference(reference)` only with references WE sent | reconcile ← create | service: reference from the claim row only |

## Assumptions & Blockers

- No blockers. Minor: `trialing` counted as active (plans have no trial). Minor: plans endpoint is
  readable anonymously (catalog data); all other routes require login (401 JSON, not a redirect).
- Customer names: Oscar users may have blank first/last names (fixtures do). Fallback order:
  request body `firstName`/`lastName` → user fields → email local part / `"Customer"`. Decided, not a gap.

## REQUIRED READING

- MUST load `python-client-initialization` — module-level lazy sync client, close at exit. (loaded)
- MUST load `python-authentication` — basic_auth, secrets read at build time, not import. (loaded)
- MUST load `python-calling-endpoints` — status-from-provider mapping, allow-list success. (loaded)
- MUST load `python-models` — `UNSET` vs `None`, open enums, never hand `UNSET` out. (loaded)
- MUST load `python-error-handling` — single ladder, never-sent vs unknown split. (loaded)
- MUST load `python-configuration-resilience` — claim/call/reconcile/verify/settle, bounded paging. (loaded)
- MUST load `python-testing` — stub transport via `custom_http_client`. (loaded)

## Implementation notes (as built)

- Code: `sandbox/apps/maxio_billing/` — `client.py` (lazy process-wide sync client, logging
  transport, closed at exit), `gateway.py` (every SDK call + the one error ladder + state mapping;
  `UNSET` never leaves it), `services.py` (claim/call/reconcile/verify/settle), `views.py`,
  `urls.py`, `models.py` + `migrations/0001_initial.py`, `admin.py` (operators can find
  `unknown` / `needs_review` rows), `tests.py` (stub transport via `custom_http_client`).
- Retries: deliberately **none** on writes (a repeat of the same POST reconciles by reference
  instead, and Maxio rejects a reused reference). Reads are not retried either; plans are cached 60 s.
- A request that finds a claim still inside its send window answers `409 in_progress` and never
  calls Maxio. A stale `sending` / `unknown` claim is looked up by reference first, then taken
  over by one conditional UPDATE and resent under the **same** reference.
- Type check: `mypy --strict` (+ django-stubs plugin) over `apps.maxio_billing` — clean.
- Live verification: two real subscriptions (Pro, Basic) created for `staff@example.com`
  through the API and read back; concurrent double-submits produced exactly one create each.
