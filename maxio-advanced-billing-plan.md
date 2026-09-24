# Maxio Advanced Billing — subscription billing for the django-oscar sandbox

## Scope

Additive subscription capability on the sandbox site (`sandbox/`), as a new Django app
`sandbox/apps/subscriptions` (app label `maxio_subscriptions`), routed under `/api/`:

| Endpoint | What it does | Maxio calls |
| --- | --- | --- |
| `GET /api/subscription-plans` | list active plans of `MAXIO_DEFAULT_PRODUCT_FAMILY`, each with `planHandle` | `product_families.list_products_for_product_family` |
| `POST /api/maxio-customer` | ensure (idempotently) a Maxio customer for the session user | `customers.create_customer`, check: `customers.read_customer_by_reference` |
| `POST /api/subscriptions` `{"planHandle": ...}` | ensure the customer (same safe write as above), then subscribe; returns top-level `subscriptionId` | `product_families.list_products_for_product_family` (plan validation + price), `sites.read_site` (once per process: collection method), `subscriptions.create_subscription`, check: `subscriptions.find_subscription` |
| `GET /api/my-subscriptions` | the caller's subscriptions, read live from Maxio and reconciled into local records | `customers.list_customer_subscriptions` |
| `GET /api/subscriptions/<id>` | one of the caller's subscriptions, read live | `subscriptions.read_subscription` |
| `GET/POST/DELETE /api/session` | Django session login/logout + CSRF token, so the flow is drivable through the API alone | none |

Auth: Django session login (the sandbox's own); identity = `request.user`. Unauthenticated → 401 JSON.
CSRF: Django's standard CSRF protection stays on for POST (caller sends `X-CSRFToken`).

## Repo survey

| Convention | Pattern | Exemplar |
| --- | --- | --- |
| Sandbox-local apps | package under `sandbox/apps/`, imported as `apps.<name>` (sandbox dir is on `sys.path`) | `sandbox/apps/sitemaps.py`, `sandbox/urls.py` (`from apps.sitemaps import ...`) |
| URL wiring | `path(...)` / `include(...)` in `sandbox/urls.py`, outside `i18n_patterns` for non-localised routes | `sandbox/urls.py` (`admin/`, `i18n/`, `sitemap.xml`) |
| Settings | `environ.Env()` reads in `sandbox/settings.py` | `sandbox/settings.py` (`env.bool('DEBUG', ...)`) |
| User model | `oscar.core.compat.AUTH_USER_MODEL` / `get_user_model()` (Oscar reuses Django auth user) | `src/oscar/apps/customer/abstract_models.py` |
| Transactions | `ATOMIC_REQUESTS = True` — views that commit a claim before a provider call must opt out with `transaction.non_atomic_requests` | `sandbox/settings.py` |
| Sync vs async | **sync** — Django under WSGI, sync views; no `async def` anywhere in sandbox | `sandbox/wsgi.py` |

Toolchain: `pip` + venv (`venv/`, Python 3.11). The SDK is not a project dependency yet; installed with
`pip install "maxio-advanced-billing @ git+https://github.com/context-plugins/maxio-python-sdk.git@main"`.
**Its built wheel declares no `Requires-Dist`** — `httpx`, `pydantic[email]`, `typing-extensions` had to be
installed explicitly (ranges from the SDK's `pyproject.toml`). Recorded in the app's `requirements.txt`.
Tests: the Oscar suite (`pytest tests/`) needs PostgreSQL (baseline: fails to connect — not runnable here);
the new app's tests run on SQLite via `cd sandbox && ../venv/Scripts/python manage.py test apps.subscriptions`.
Type checker: none configured → `mypy --strict` on the new app's non-migration modules.
Baseline sandbox DB: `orders.json` fixture fails with an FK error and only 11 products load — pre-existing,
irrelevant to this API.

Credential check: `MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`, `MAXIO_ENVIRONMENT` (`US`),
`MAXIO_DEFAULT_PRODUCT_FAMILY` present. Read-only smoke (scratchpad, outside repo) passed:
family listing via `handle:<family>` returned `eshop-pro` (29900¢/1 month) and `basic-plan` (2900¢/1 month) —
IDs already differ from the task table, confirming "handles only"; `read_customer_by_reference` and
`find_subscription` answer **404 with an empty body** on a miss; `list_subscriptions` 200. No 403s.

## Contract sheet

### Client

| Fact | Value | Source |
| --- | --- | --- |
| Sync or async | **sync** `MaxioAdvancedBillingClient`; never `AsyncMaxioAdvancedBillingClient` (no bridge; do not mix) | sdk-map.md *Getting a client* |
| Holder | lazily-built module-level singleton in `apps/subscriptions/maxio.py`, built on first use (post-fork), guarded by a lock for construction only, closed via `atexit` | python-client-initialization |
| Constructor | keyword-only: `environment=`, `timeout=`, `server_config=`, `basic_auth=` | sdk-map.md constructor table |
| environment | explicit, from `MAXIO_ENVIRONMENT` via map `{"us": "us", "eu": "eu"}` (case-insensitive); unknown value → `ImproperlyConfigured`. Omitting it would silently be `"us"` | sdk-map.md *Servers & auth* |
| server_config | `MAXIO_BASE_URL` set → `{"production": {env: {"base_url": MAXIO_BASE_URL}}}` verbatim; else `{"production": {env: {"site": MAXIO_SITE_SUBDOMAIN}}}`. `extra="forbid"`: wrong nesting raises | `server/server_config.py` (`ProductionUsConfig.base_url/site`) |
| auth | `basic_auth={"username": MAXIO_API_KEY, "password": "x"}` — README: `curl -u <api_key>:x`. Omitting it sends unauthenticated requests silently → guard: empty key → `ImproperlyConfigured` | README.md; sdk-map.md *Servers & auth* |
| timeout | `10.0` s (default 30 too long for a request path). Per call none | `base_client.py` |
| Retries | **SDK performs none.** Reads: none added (a failed read answers 502/504 to the caller, who may retry — reads are safe). Writes: never blindly retried; unknown outcomes settled by lookup under the same reference | python-configuration-resilience |
| Logging | `LoggingTransport` wrapping `HttpxClient(timeout=10.0)`, logs method, URL path, status, ms — never headers/bodies. Passing `custom_http_client` means the client `timeout=` no longer applies → set on `HttpxClient` | python-configuration-resilience |

### Operations (every operation also has an identical `Async…` peer — not used)

| Operation | Signature (positional \| after `*`) | Returns | `ApiError.error` union | Notes |
| --- | --- | --- | --- | --- |
| `client.product_families.list_products_for_product_family` | `product_family_id: str` \| `page=1, per_page=20, …, include_archived=None, request_options=None` | `list[ProductResponse]` | `ListProductsForProductFamilyErrorBody` = `str` [404] \| `RawError` | `product_family_id` accepts `"handle:<handle>"` (docstring). Paginate `per_page=200` until a short page |
| `client.customers.create_customer` | — \| `body: CreateCustomerRequest \| CreateCustomerRequestDict \| None = None, request_options=None` | `CustomerResponse` | `CreateCustomerErrorBody` = `CustomerErrorResponse1` [422] \| `RawError` | body required in practice. Docstring: "you may only create one customer for a given reference value" — Maxio enforces reference uniqueness → a 422 may mean "already exists" (confirm by lookup). SDK sends a random `Idempotency-Key` header; overridden per claim via `request_options={"extra_headers": {"Idempotency-Key": <ref-derived uuid5>}}` (enforcement undocumented — not relied on) |
| `client.customers.read_customer_by_reference` | `reference: str` \| `request_options=None` | `CustomerResponse` | `RawError` (Case B) | miss = 404 empty body (smoke) → `None` |
| `client.subscriptions.create_subscription` | — \| `body: CreateSubscriptionRequest \| CreateSubscriptionRequestDict \| None = None, request_options=None` | `SubscriptionResponse` | `CreateSubscriptionErrorBody` = `ErrorListResponse1` [422] (`errors: list[str]`) \| `RawError` | subscription `reference` uniqueness **not documented** → resend not safe; check = lookup only. Random `Idempotency-Key` overridden as above |
| `client.subscriptions.find_subscription` | — \| `reference: str \| None = None, request_options=None` | `SubscriptionResponse` | `FindSubscriptionErrorBody` = `RawError` [404, unmapped] | miss = 404 → `None` |
| `client.subscriptions.read_subscription` | `subscription_id: int` \| `include=None, request_options=None` | `SubscriptionResponse` | `RawError` (Case B) | 404 → our 404 |
| `client.sites.read_site` | — \| `request_options=None` | `SiteResponse` (`site: Site`, required) | `RawError` (Case B) | `Site.relationship_invoicing_enabled: Optional[bool]` → collection method (below). Smoke: `True` on this site |
| `client.customers.list_customer_subscriptions` | `customer_id: int` \| `request_options=None` | `list[SubscriptionResponse]` | `RawError` (Case B) | |

No operation in scope returns `None`. Every keyword-only parameter has a real default — pass only what is needed.

### Models (members the task sets or reads; `Optional[T]` = `T | UnsetType`, **not** `typing.Optional` — never pass `None`)

| Model (module) | Member | Required? | Wire alias | Use |
| --- | --- | --- | --- | --- |
| `CreateCustomerRequest` (`models/create_customer_request.py`) | `customer: CreateCustomer` | required | — | |
| `CreateCustomer` (`models/create_customer.py`) | `first_name: str`, `last_name: str`, `email: str` | **required** | — | from `user.first_name/last_name/email`; blank name → fallback from email / `"Customer"`; no email → 400 |
| | `reference: Optional[str]` | UNSET-able, **always set** | — | the claim reference |
| `CustomerResponse` | `customer: Customer` | required | — | |
| `Customer` (`models/customer.py`) | `id: Optional[int]`, `reference: OptionalNullable[str]`, `created_at: Optional[RFC3339DateTime]` | UNSET-able | — | **assert `id` is set**, else outcome unknown |
| `CreateSubscriptionRequest` | `subscription: CreateSubscription` | required | — | |
| `CreateSubscription` (`models/create_subscription.py`) | `product_handle: Optional[str]`, `customer_id: Optional[int]`, `reference: Optional[str]`, `payment_collection_method: Optional[CollectionMethodOrStr]` | UNSET-able; all four **always set** | — | `reference` = claim reference. Collection method: `CollectionMethod.REMITTANCE` when `relationship_invoicing_enabled`, else `CollectionMethod.INVOICE` (docstring: Relationship Invoicing → `remittance`/`automatic`/`prepaid`; legacy Statements → `invoice`/`automatic`). Found live: with the default (`automatic`) Maxio answers 422 "No payment method was on file for the $299.00 balance" |
| `CollectionMethod` (`models/enums/collection_method.py`) | `AUTOMATIC`, `REMITTANCE`, `PREPAID`, `INVOICE` | — | — | |
| `SubscriptionResponse` (`models/subscription_response.py`) | `subscription: Optional[Subscription]` | **UNSET-able** | — | UNSET → outcome unknown |
| `Subscription` (`models/subscription.py`) | `id: Optional[int]`, `state: Optional[SubscriptionStateOrStr]`, `reference: OptionalNullable[str]`, `product: Optional[Product]`, `product_price_in_cents: Optional[int]`, `currency: Optional[str]`, `next_assessment_at: OptionalNullable[RFC3339DateTime]`, `current_period_ends_at: OptionalNullable[…]`, `created_at: Optional[…]`, `updated_at: Optional[…]`, `activated_at: OptionalNullable[…]`, `canceled_at: OptionalNullable[…]` | UNSET-able | — | assert `id` and `state` set; missing → unknown |
| `ProductResponse` | `product: Product` | required | — | |
| `Product` (`models/product.py`) | `id`, `name: Optional[str]`, `handle: OptionalNullable[str]`, `description: OptionalNullable[str]`, `price_in_cents: Optional[int]`, `interval: Optional[int]`, `interval_unit: Optional[IntervalUnitOrStr]`, `archived_at: OptionalNullable[…]`, `require_credit_card: Optional[bool]`, `trial_price_in_cents`, `initial_charge_in_cents: OptionalNullable[int]` | UNSET-able | — | products with no handle or with `archived_at` set are not offered |
| `ErrorListResponse1` (`models/error_list_response1.py`) | `errors: list[str]` | required | — | 422 detail to caller |
| `CustomerErrorResponse1` (`models/customer_error_response1.py`) | `errors: Optional[Errors1]` | UNSET-able | — | 422 → confirm by lookup, then detail |

No member in scope is typed bare `Optional[Any]`. Every SDK value is mapped into plain dicts (UNSET → `None`) before it reaches `JsonResponse`.

### Enum `SubscriptionState` (`models/enums/subscription_state.py`, open: `SubscriptionStateOrStr`)

Members → outcome (`status_from_provider`):

| Members | Outcome | Why |
| --- | --- | --- |
| `ACTIVE`, `TRIALING` | **done** | subscription in effect |
| `PENDING`, `ASSESSING`, `AWAITING_SIGNUP` | **pending** | provider not finished / not yet started |
| `PAST_DUE`, `SOFT_FAILURE`, `UNPAID`, `PAUSED`, `ON_HOLD`, `SUSPENDED` | **pending** (exists, something outstanding — not done) | surfaced with the raw state |
| `FAILED_TO_CREATE`, `CANCELED`, `EXPIRED`, `TRIAL_ENDED` | **failed** (failed, or done-then-undone) | |
| anything else / UNSET | **unknown** | never done |

### Errors → HTTP at our boundary (one ladder, `apps/subscriptions/errors.py`)

| Failure | Our status | `outcomeUnknown` |
| --- | --- | --- |
| `ApiError` 400/404/409/422 with the op's typed arm | same status (404 → 404, 422 → 422) with detail | false |
| `ApiError` 401/403 | 502 | false |
| `ApiError` 429 | 503 | false |
| `ApiError` 5xx / unmapped | 502 (on a write: looked up first; still unknown → 504) | reads false; writes decided by the safe write |
| `httpx.ConnectError/ConnectTimeout/PoolTimeout/ProxyError` | 502 | false |
| other `httpx.RequestError` | 504 | true |
| `ValidationError`/`ValueError` on decode | 502 on a read; on a write: settle by lookup, else 504 unknown | reads false |

## OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| `subscriptions.create_subscription` | `SubscriptionResponse.subscription.state` (`SubscriptionStateOrStr`) | done: `active`, `trialing` → record `done`, answer 201. pending: `pending`, `assessing`, `awaiting_signup`, `past_due`, `soft_failure`, `unpaid`, `paused`, `on_hold`, `suspended` → record `pending`, answer 202 with state. failed: `failed_to_create`, `canceled`, `expired`, `trial_ended` → record `failed` (keeps provider id), answer 409 `subscription_failed`. unknown: any other value, or `state` UNSET → record `unknown`, answer 202 `outcome: unknown`. `subscription` or `id` UNSET → record `unknown`, look up by reference, and if still not found answer 504 `outcomeUnknown: true` | `outcomes.status_from_provider`, applied in `service._read_subscription`. The answer is chosen in `views.subscriptions`: 201 only for `Outcome.DONE`, 409 for `FAILED`, 202 for everything else |
| `customers.create_customer` | none — `Customer` carries no status field | done only when `customer.id` is present; absent id → `unknown` (may have landed; settle by reference lookup) | `service._read_customer` (outcome done). A missing id is caught in `writes.safe_write`: `read(result).provider_id is None` after the send → lookup by reference; still nothing → `Outcome.UNKNOWN` + `OutcomeUnknown` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| `customers.create_customer` | `MaxioCustomer` row (SQLite/any Django DB), committed before the call (views are `non_atomic_requests`) | DB unique constraint on `MaxioCustomer.user` (OneToOne) and `reference` → `IntegrityError` | `try_claim` catches `IntegrityError`, returns False; loser answers from the row / checks | claim: `models.MaxioCustomer` (`user` OneToOne, `reference` unique). Insert-or-fail: `writes.Claim.try_claim`, called from `writes.safe_write` via `service.ensure_customer`. Commit before the call: `views.api_view` (`transaction.non_atomic_requests`) |
| `subscriptions.create_subscription` | `SubscriptionEnrollment` row, committed before the call | DB `UniqueConstraint(user, plan_handle, attempt)` and unique `reference` → `IntegrityError` | `try_claim` catches `IntegrityError`, returns False | claim: `models.SubscriptionEnrollment` (`maxio_enrollment_one_claim_per_attempt`, `reference` unique). Insert-or-fail: `writes.Claim.try_claim`, called from `writes.safe_write` via `service.subscribe`. The attempt is picked in `service.subscribe` |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| `customers.create_customer` | lookup `customers.read_customer_by_reference(ref)` (404 → None → still unknown); a 422 on create is confirmed by the same lookup (found → landed); Maxio refuses a second customer with the same reference, so a stale/unknown claim is settled by a same-reference resend (`repeat_is_safe=True`) whose 422 is again confirmed by lookup | `MaxioCustomer.reference` = `{MAXIO_REFERENCE_PREFIX}:user:{pk}:{date_joined epoch}` | `service.ensure_customer` → `find` (`read_customer_by_reference`, 404 → None), and `send` (a 422 is confirmed by `find`). Resend check: `writes.safe_write` with `repeat_is_safe=True` |
| `subscriptions.create_subscription` | lookup `subscriptions.find_subscription(reference=ref)` (404 → None → still unknown); no resend (`repeat_is_safe=False`, subscription-reference uniqueness undocumented) | `SubscriptionEnrollment.reference` = `{customer reference}:sub:{planHandle}:{attempt}` | `service.subscribe` → `find` (`find_subscription(reference=…)`, 404 → None). Check: `writes.safe_write` step 3 with `repeat_is_safe=False` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| `customers.create_customer` | committed `MaxioCustomer(user, reference, outcome="sending", claimed_at)` | `outcome` (done/unknown/failed), `maxio_customer_id`, `provider_time` (= `customer.created_at`) | before: `writes.Claim.try_claim`. After: `writes.Claim.complete` (sets `maxio_customer_id` and `provider_time`), called at the end of `writes.safe_write` |
| `subscriptions.create_subscription` | committed `SubscriptionEnrollment(user, plan_handle, attempt, reference, expected_price_in_cents, outcome="sending", claimed_at)` | `outcome` (done/pending/failed/needs_review/unknown), `maxio_subscription_id`, `state`, `provider_time` (= `subscription.created_at`) | before: `writes.Claim.try_claim` (with `defaults={'expected_price_in_cents': …}` from `service.subscribe`). After: `writes.Claim.complete` with `Answer.fields` from `service._subscription_fields`. Price check: `writes.safe_write` step 4. Later reconciled by `service.my_subscriptions` |

Echoed-amount check: enrollment records the plan's `price_in_cents` from the catalogue before the call;
`subscription.product_price_in_cents` must equal it (integer cents, USD-only site — compared as `int`),
else `needs_review` (it happened, not as asked) → 202 with `outcome: needs_review`.

Rules carried from python-configuration-resilience: a `failed` with no provider id releases the claim
(row deleted) so the next request re-claims under the **same** reference; a claim `sending` younger than
`SEND_WINDOW` (60 s > 10 s timeout, no retries) answers 202 in-progress with no provider call; stale
`sending` / `unknown` → check only (never a fresh create under a new reference). A subscription whose stored
outcome is `failed` **with** a provider id (e.g. later canceled — refreshed by `my-subscriptions`) lets a
new request claim `attempt + 1` (a new reference, because the previous operation is settled).

## Assumptions & Blockers

- Minor: `trialing` counts as done (subscription in effect). Seeded plans have no trial.
- Minor: problem states (`past_due` …) map to pending + raw state is surfaced.
- Minor: `MAXIO_REFERENCE_PREFIX` (default `oscar-sandbox`) + the user's `date_joined` epoch make
  references unique per install/DB; a different install sharing the Maxio site should set its own prefix.
- Minor: `MAXIO_ENVIRONMENT` is also read through settings (not in the mandated list, but supplied).
- Minor (found in live verification): "payment method not required" means signup without a card, but
  the default `automatic` collection still needs one for the first charge. Subscriptions are therefore created
  with invoice-based collection (`remittance` on Relationship Invoicing sites, `invoice` on legacy sites). A plan
  whose product has `require_credit_card` set is refused with 422 before anything is claimed, because card
  capture is out of scope.
- No blockers: every capability needed (plans by family handle, customer create/lookup by reference,
  subscription create/lookup by reference, list per customer) exists in the SDK map.

## REQUIRED READING

| Step | Hazard | Pointer |
| --- | --- | --- |
| client construction, lifetime, transport | keyword-only ctor, close obligation, post-fork construction, custom transport drops ctor timeout | MUST load python-client-initialization (loaded) |
| credentials | omitted `basic_auth` = unauthenticated silently | MUST load python-authentication |
| calls | status decides done, not the id; allow-list success | MUST load python-calling-endpoints (loaded) |
| models | `Optional` ≠ `typing.Optional`; UNSET must not leak to JSON; open enums | MUST load python-models (loaded) |
| errors | one `ApiError`, per-op unions, decode + transport failures not `ApiError` | MUST load python-error-handling (loaded) |
| writes | claim-first safe write, lookup by reference, no retries | MUST load python-configuration-resilience (loaded) |
| tests | stub transport seam, both transport-failure inputs, same-op-twice test | MUST load python-testing (loaded) |
