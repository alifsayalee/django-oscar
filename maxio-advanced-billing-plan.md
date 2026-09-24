# Maxio Advanced Billing — subscription billing for the Oscar sandbox

## Plan

New Django app `sandbox/apps/subscriptions/` (label `subscriptions`), wired into
`sandbox/settings.py` (`INSTALLED_APPS`, `MAXIO_*` settings) and `sandbox/urls.py` (`api/` prefix,
outside `i18n_patterns`, like `admin/`).

| Route | Action |
| --- | --- |
| `GET  /api/subscription-plans` | products of `MAXIO_DEFAULT_PRODUCT_FAMILY`, each with `planHandle` |
| `POST /api/billing-customer` | idempotently ensure the Maxio customer for `request.user` |
| `POST /api/subscriptions` `{"planHandle": ...}` | ensure customer, then subscribe; top-level `subscriptionId` |
| `GET  /api/subscriptions/<id>` | read one subscription owned by the caller |
| `GET  /api/my-subscriptions` | the caller's subscriptions (Maxio is the record) + unresolved local attempts |

Layers (exemplar conventions: app package like `sandbox/apps/user/`, URL wiring like `admin/` in
`sandbox/urls.py`, AppConfig like `src/oscar/apps/*/apps.py`):

- `maxio.py` — client factory (module-level, lazy, lock-guarded, closed `atexit`), settings → env/base
  URL, retry-for-reads, error translation into `ProviderError` subclasses, provider-state mapping.
- `models.py` — `BillingCustomer` (OneToOne to Oscar's `AUTH_USER_MODEL`) and `SubscriptionAttempt`
  (FK to user): our record of *that we asked*, keyed by the deterministic reference we sent.
- `services.py` — ensure-customer and subscribe flows (write-then-lookup reconciliation).
- `views.py` — plain Django `View`s returning `JsonResponse`, session auth (`401` JSON when anonymous),
  CSRF kept on (callers send `X-CSRFToken`).
- `tests.py` — Django `TestCase`s through the stub-transport seam; run with `manage.py test`.
- `admin.py` — read-only admin for the two models.

**Sync vs async: sync.** Django under WSGI (`sandbox/wsgi.py`), no async views → `MaxioAdvancedBillingClient`
+ `close()`. Client built lazily on first use (post-fork safe), one per process.

**Idempotency.** References: `<prefix>-cust-<user.pk>` and `<prefix>-sub-<user.pk>-<planHandle>-<seq>`.
`<prefix>` = `MAXIO_REFERENCE_PREFIX` setting, default derived from a hash of `SECRET_KEY` (unique per
install). Local row saved (`sending`) and committed BEFORE the write; unique constraints make a
concurrent double-click lose the insert and fall into lookup-only mode. Views are
`non_atomic_requests` so those commits are real.

**Toolchain.** `venv` + pip (`py -3.11 -m venv venv`, `pip install -e .[test]`). SDK installed from git
`@main`; its metadata declares no deps, so `httpx`, `pydantic[email]`, `typing-extensions` installed
explicitly per the identity table. Type check: `mypy --strict` on `maxio.py` (mypy installed in venv);
tests: `cd sandbox && ../venv/Scripts/python manage.py test apps.subscriptions`.

## Contract sheet

Client: `MaxioAdvancedBillingClient` (root `maxio_advanced_billing`), keyword-only ctor.
- `environment=` — `Literal["us","eu","maxio_api_gateway"]`; **omitting it is silently `"us"`**. We map
  `MAXIO_ENVIRONMENT` (`US`/`EU`, case-insensitive) explicitly; unknown → `ImproperlyConfigured`.
- `server_config={"production": {<env>: {"site": <subdomain>}}}`; with `MAXIO_BASE_URL` set:
  `{"production": {<env>: {"base_url": <url>}}}` (verbatim). `extra="forbid"` → wrong nesting raises.
- `basic_auth=BasicAuthCredentials(username=<MAXIO_API_KEY>, password="x")` (README: `curl -u <api_key>:x`).
  Omitting it = unauthenticated requests, no error → we fail fast on an empty key.
- `timeout=10.0` (default 30.0; must be > 0). **No retries in the SDK** — we retry reads only (below).
- Every keyword-only param has a real default: never pass defensive `None`s.

| Operation (all `production` server, basic auth) | Signature (positional \| after `*`) | Returns | `ApiError.error` |
| --- | --- | --- | --- |
| `product_families.list_products_for_product_family` GET `/product_families/{id}/products.json` | `product_family_id: str` \| `page=1, per_page=20 (max 200), include_archived=None, …` ; id may be `"handle:<family>"` | `list[ProductResponse]` | `str` [404] \| `RawError` |
| `customers.read_customer_by_reference` GET `/customers/lookup.json` | `reference: str` \| — | `CustomerResponse` | `RawError` (404 = not found; smoke: empty body) |
| `customers.create_customer` POST `/customers.json` | — \| `body: CreateCustomerRequest` | `CustomerResponse` | `CustomerErrorResponse1` [422] \| `RawError` |
| `customers.list_customer_subscriptions` GET `/customers/{customer_id}/subscriptions.json` | `customer_id: int` | `list[SubscriptionResponse]` | `RawError` |
| `subscriptions.create_subscription` POST `/subscriptions.json` | — \| `body: CreateSubscriptionRequest` | `SubscriptionResponse` | `ErrorListResponse1` [422] \| `RawError` |
| `subscriptions.find_subscription` GET `/subscriptions/lookup.json` | — \| `reference: str` | `SubscriptionResponse` | `RawError` (404 = not found; smoke confirmed) |
| `subscriptions.read_subscription` GET `/subscriptions/{subscription_id}.json` | `subscription_id: int` \| `include=None` | `SubscriptionResponse` | `RawError` |

None of these return `None`; parsed mode is used throughout (raw mode not needed — no header/status reads on success).

Models (members we set/read; `Optional[T]` = `T | UnsetType`, **not** `typing.Optional`; never pass `None`):
- `CreateCustomerRequest(customer: CreateCustomer)`; `CreateCustomer`: **required** `first_name: str`,
  `last_name: str`, `email: str`; `reference: Optional[str]` — **always set** (docs: must be unique).
- `CustomerResponse.customer: Customer` (required); `Customer.id: Optional[int]` — assert set.
- `CreateSubscriptionRequest(subscription: CreateSubscription)`; `CreateSubscription`:
  `product_handle: Optional[str]` (required unless `product_id`), `customer_id: Optional[int]`,
  `reference: Optional[str]` — **always set** (not documented unique → reconcile by `find_subscription`).
- `SubscriptionResponse.subscription: Optional[Subscription]` — **may be UNSET → assert set**.
- `Subscription`: `id: Optional[int]` (assert), `state: Optional[SubscriptionStateOrStr]` (assert),
  `reference: OptionalNullable[str]`, `product: Optional[Product]`, `customer: Optional[Customer]`,
  `product_price_in_cents: Optional[int]`, `current_billing_amount_in_cents: Optional[int]`,
  `currency: Optional[str]`, `current_period_ends_at: OptionalNullable[datetime]` (= next scheduled charge →
  `nextBillingAt`), `next_assessment_at: OptionalNullable[datetime]`, `activated_at`, `created_at`.
- `ProductResponse.product: Product` (required); `Product`: `id`, `name`, `handle: OptionalNullable[str]`,
  `description: OptionalNullable[str]`, `price_in_cents: Optional[int]`, `interval: Optional[int]`,
  `interval_unit: Optional[IntervalUnitOrStr]`, `require_credit_card: Optional[bool]`,
  `archived_at: OptionalNullable[datetime]`.
- Error bodies: `ErrorListResponse1.errors: list[str]` (required); `CustomerErrorResponse1.errors:
  Optional[CustomerError | list[str]]`, `CustomerError.customer: Optional[str]`.

`SubscriptionState` (open enum, `models.enums`) → our outcome (one function, `status_from_provider`):
- `done`: `ACTIVE`, `TRIALING`
- `pending`: `PENDING`, `ASSESSING`, `AWAITING_SIGNUP`
- `attention` (exists, not done — live but something outstanding): `PAST_DUE`, `SOFT_FAILURE`, `UNPAID`,
  `PAUSED`, `ON_HOLD`, `SUSPENDED`
- `ended` (not in effect): `CANCELED`, `EXPIRED`, `TRIAL_ENDED`, `FAILED_TO_CREATE`
- anything else (unlisted str from open enum) → `unknown` (neither done nor failed)
Only `done` answers 201/200 success; `pending`/`attention`/`unknown` answer 202.

Failure translation (one ladder, every call site, `python-error-handling`):
- `ApiError` 400/404/409/422 → caller's fault, passed through with provider messages (named list).
- 401/403 → 502 (our credentials); 429 → 503; 5xx / unmapped → 502, `outcome_unknown` on writes for 5xx.
- `ValidationError`/`ValueError` decode failure on 2xx → outcome unknown (502/unknown); on a lookup → error, never "not found".
- `httpx.ConnectError/ConnectTimeout/PoolTimeout/ProxyError` → 502, known (never sent).
- other `httpx.RequestError` → 504, outcome unknown → lookup by reference.
- Missing required members on 2xx (UNSET id/state) → treated as unreadable → outcome unknown.
Retries: reads only (GETs), up to 3 attempts on never-sent transport errors, read timeouts, 429/502/503/504;
writes are never auto-retried — reconciliation is by reference lookup.

## Assumptions & Blockers

- No blockers. Minor: API key maps to basic-auth username with password `x` (SDK README). Plans are
  identified by handle; numeric IDs are not relied on (smoke showed they already changed).
- One live subscription per (user, plan); subscribing again to a plan that is live returns it.
- Customer name falls back to the email local part when the Oscar user has no first/last name.
- Subscriptions are billed by invoice (`remittance`) since no card is captured; configurable.
- Host note: `orders.json` references products/stock records created by the book CSV imports, so
  `oscar_import_catalogue` over the `books.*.csv` fixtures must run before it (only
  `range-products.csv` reports 0 items). The repo's own `tests/` suite needs PostgreSQL (not
  available here); the app's tests run via `manage.py test apps.subscriptions` on SQLite.

## REQUIRED READING

- MUST load `python-client-initialization` — module-level client lifetime, close obligation. (loaded)
- MUST load `python-authentication` — basic auth, silent no-auth. (loaded)
- MUST load `python-calling-endpoints` — keyword-only boundary, status-gated outcomes. (loaded)
- MUST load `python-models` — UNSET vs None, open enums, handing values out. (loaded)
- MUST load `python-error-handling` — ladder, decode + transport failures. (loaded)
- MUST load `python-configuration-resilience` — server_config, no retries, write reconciliation. (loaded)
- MUST load `python-testing` — stub transport seam, both transport-failure kinds. (loaded)
