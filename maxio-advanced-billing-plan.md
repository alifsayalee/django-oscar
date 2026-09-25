# Maxio Advanced Billing — subscription billing for the Oscar sandbox

Plan + contract sheet for adding recurring subscriptions (Maxio as the billing system of record) to
the runnable sandbox at `sandbox/`, as a new Django app `sandbox/apps/subscriptions`.

## Scope

| Endpoint | Purpose |
| --- | --- |
| `GET /api/subscription-plans` | list the plans (products) of `MAXIO_DEFAULT_PRODUCT_FAMILY`; each entry carries `planHandle` |
| `POST /api/subscriptions` | subscribe the session user to `planHandle` (ensures the Maxio customer first); returns top-level `subscriptionId` |
| `GET /api/my-subscriptions` | the session user's subscriptions, read back from Maxio, plus any locally unsettled requests |
| `GET/POST/DELETE /api/session` | Django session login/logout (the sandbox's own auth backends), so the flow is drivable through the API alone; `GET` also sets the CSRF cookie |

Each is separately invocable. Customer creation is an internal step of subscribe (idempotent per user);
it is also exposed as `POST /api/billing-customer` so it is independently invocable.

## Repo survey (read-only)

| Convention | Pattern | Exemplar |
| --- | --- | --- |
| Sandbox apps | plain packages under `sandbox/apps/`, imported as `apps.<name>` (sandbox dir is on `sys.path`) | `sandbox/apps/sitemaps.py`, `sandbox/urls.py` (`from apps.sitemaps import ...`) |
| URL wiring | `sandbox/urls.py` — non-i18n routes in `urlpatterns`, Oscar under `i18n_patterns` | `sandbox/urls.py` |
| Settings | `django-environ` `env = environ.Env()`; `env.bool/str/list` with defaults | `sandbox/settings.py` |
| App config | Oscar apps use `AppConfig` subclasses | `src/oscar/apps/wishlists/apps.py` |
| Models / user | `AUTH_USER_MODEL` via `oscar.core.compat.AUTH_USER_MODEL` | `sandbox/apps/user/models.py` |
| Auth | Django session login, backends `oscar.apps.customer.auth_backends.EmailBackend` + `ModelBackend` | `sandbox/settings.py` `AUTHENTICATION_BACKENDS` |
| Transactions | `ATOMIC_REQUESTS=True` — every view is one DB transaction unless opted out | `sandbox/settings.py` |
| Sync vs async | **sync** — Django under WSGI (`sandbox/wsgi.py`), no async views anywhere | `sandbox/wsgi.py` |
| Tests | repo suite (`tests/`, pytest) needs PostgreSQL → not runnable here (baseline: 16 passed / 79 errors, all DB-connection refused). Sandbox tests run with Django's runner: `cd sandbox && ../venv/Scripts/python manage.py test apps.subscriptions` (`TEST_RUNNER = DiscoverRunner`) on SQLite | `sandbox/settings.py` |
| Lint / types | flake8 max-line 119 (`setup.cfg`); no type checker configured → `mypy --strict` on the files touched (installed into venv) | `setup.cfg` |

Toolchain: `pip` + venv at `venv/` (gitignored). `maxio-advanced-billing` 1.0 installed from
`git+https://github.com/context-plugins/maxio-python-sdk.git@main`; its declared deps (`httpx`,
`pydantic[email]`, `typing-extensions`) had to be installed explicitly (the wheel's metadata came out
with no `Requires`). It is not added to the root `pyproject.toml` — that is Oscar's library package and
the sandbox is not a distribution; the install command is in `sandbox/apps/subscriptions/README.rst`.

## Configuration (settings.py — names fixed by the task)

| Setting | Source | Use |
| --- | --- | --- |
| `MAXIO_API_KEY` | env `MAXIO_API_KEY` | Basic auth username; password is `"x"` (SDK `pyproject.toml` description: `curl -u <api_key>:x`) |
| `MAXIO_SITE_SUBDOMAIN` | env | `server_config={"production": {<env>: {"site": ...}}}` |
| `MAXIO_DEFAULT_PRODUCT_FAMILY` | env | family handle → `"handle:" + value` |
| `MAXIO_BASE_URL` | env, optional | when set: `server_config={"production": {<env>: {"base_url": value}}}` verbatim (template has no `{site}` then) |
| `MAXIO_ENVIRONMENT` | env, default `us` | explicit map `{"us": "us", "eu": "eu"}` (case-insensitive); unknown → `ImproperlyConfigured`. Passed explicitly — omission is silently `"us"` |
| `MAXIO_TIMEOUT` | env, default 15.0 | client timeout seconds |
| `MAXIO_REFERENCE_PREFIX` | env, optional | install prefix for references; when unset, a random install id persisted in the DB (`MaxioInstall` singleton) |

Missing API key / subdomain (with no base URL) → `ImproperlyConfigured` at first client use → API answers 503 `billing_not_configured`.

## Contract sheet

**Client**: sync `MaxioAdvancedBillingClient` (from `maxio_advanced_billing`), keyword-only constructor:
`environment=<"us"|"eu">`, `timeout=float`, `server_config=ServerConfigOrDict`,
`basic_auth=BasicAuthCredentials(username=api_key, password="x")` (from `maxio_advanced_billing.core`).
One process-wide client, built lazily on first use (post-fork safe), closed via `atexit` → `client.close()`.
No async client anywhere (WSGI). Every keyword-only operation parameter has a real default — no defensive `None`s.
**The SDK performs no retries** — none added for writes (the safe write resends only under the same
reference as its check); reads are not retried either (a failed read answers 502/504 and the caller may retry).
Auth is `basic_auth` OR `bearer_auth` on every op used; only `basic_auth` is set.
Server for every op below: `production`. Async twins exist for all; not used.

Every signature ends with keyword-only `request_options: RequestOptionsOrDict | None = None`.
`request_options={"extra_headers": {...}}` overrides SDK headers (SDK sends a random `Idempotency-Key: uuid4()` on
`create_customer`/`create_subscription`; **smoke-verified the site does NOT de-duplicate on it** — two creates
with the same key made two customers — so it is not relied on).

| Operation | Signature (positional \| after `*`) | Returns (parsed) | Error union (`ApiError.error`) | Assert after call |
| --- | --- | --- | --- | --- |
| `client.product_families.list_products_for_product_family` — `GET /product_families/{product_family_id}/products.json` | `product_family_id: str` \| `page=1, per_page=20, date_field, filter, start_date, end_date, start_datetime, end_datetime, include_archived, include` | `list[ProductResponse]` | `ListProductsForProductFamilyErrorBody = str [404] \| RawError`. **Smoke: 404 has an empty body → `ValueError("Response body is not valid JSON")` raised from the error mapper, not `ApiError`** | each `ProductResponse.product` (required) — `Product.handle`, `.id`, `.name`, `.price_in_cents`, `.interval`, `.interval_unit` are `Optional`/`OptionalNullable`; entries lacking handle or price are skipped + logged |
| `client.customers.create_customer` — `POST /customers.json` | — \| `body: CreateCustomerRequest \| CreateCustomerRequestDict \| None` | `CustomerResponse` | `CreateCustomerErrorBody = CustomerErrorResponse1 [422] \| RawError`. `CustomerErrorResponse1.errors: Optional[Errors1]`, `Errors1 = CustomerError \| list[str]`; `CustomerError.customer: Optional[str]`. **Smoke: duplicate reference → 422 `{"errors":["Reference: must be unique - that value has been taken."]}`** | `CustomerResponse.customer: Customer` (required); `Customer.id: Optional[int]` must be int; `Customer.reference: OptionalNullable[str]` must equal the sent ref |
| `client.customers.read_customer_by_reference` — `GET /customers/lookup.json` | `reference: str` \| — | `CustomerResponse` | `RawError` (Case B). Smoke: miss → 404, empty body | as above |
| `client.subscriptions.create_subscription` — `POST /subscriptions.json` | — \| `body: CreateSubscriptionRequest \| CreateSubscriptionRequestDict \| None` | `SubscriptionResponse` | `CreateSubscriptionErrorBody = ErrorListResponse1 [422] \| RawError`; `ErrorListResponse1.errors: list[str]` (required). **Smoke: duplicate reference → 422 `["Reference: must be unique - that value has been taken."]`; no card + `automatic` collection → 422 `["No payment method was on file for the $29.00 balance"]`** | `SubscriptionResponse.subscription: Optional[Subscription]` must be set; `Subscription.id: Optional[int]`, `.state: Optional[SubscriptionStateOrStr]`, `.product: Optional[Product]` (`.handle` must equal the requested plan — echoed-value check), `.reference: OptionalNullable[str]` must equal sent ref |
| `client.subscriptions.find_subscription` — `GET /subscriptions/lookup.json` | — \| `reference: str \| None = None` | `SubscriptionResponse` | `FindSubscriptionErrorBody = RawError [404, unmapped]`. Smoke: miss → 404 | as create |
| `client.subscriptions.read_subscription` — `GET /subscriptions/{subscription_id}.json` | `subscription_id: int` \| `include` | `SubscriptionResponse` | `RawError` (Case B) | as create |
| `client.customers.list_customer_subscriptions` — `GET /customers/{customer_id}/subscriptions.json` | `customer_id: int` \| — | `list[SubscriptionResponse]` | `RawError` (Case B) | each `.subscription` set |

None of these returns `None`, so the parsed mode is used throughout (no `with_raw_response` needed).

### Request models (members set)

- `CreateCustomerRequest(customer: CreateCustomer)` — `CreateCustomer`: **required** `first_name: str`,
  `last_name: str`, `email: str`; set `reference: Optional[str]` (always set — the claim reference). No wire aliases.
- `CreateSubscriptionRequest(subscription: CreateSubscription)` — all members `Optional[...] = UNSET`; set
  `product_handle: str`, `customer_reference: str` (the customer's claim reference),
  `reference: str` (always set — the claim reference), `payment_collection_method: CollectionMethodOrStr`
  = `CollectionMethod.REMITTANCE` (enum `maxio_advanced_billing.models.enums.CollectionMethod`: `AUTOMATIC`,
  `REMITTANCE`, `PREPAID`, `INVOICE`; docstring: Relationship Invoicing valid = remittance/automatic/prepaid).
  Remittance = invoice the customer, no card on file — needed because the plans take a charge at signup
  (smoke: `automatic` → 422 "No payment method"). No wire aliases. No `Optional[Any]` members set.

### Response members read

- `Product` (`models/product.py`): `id`, `name`, `handle`, `description`, `price_in_cents`, `interval`,
  `interval_unit: Optional[IntervalUnitOrStr]` (`IntervalUnit.DAY="day"`, `MONTH="month"`), `archived_at`,
  `require_credit_card`. All `Optional`/`OptionalNullable` → `UNSET` resolved to `None` before JSON.
- `Subscription` (`models/subscription.py`): `id`, `state`, `reference`, `product`, `product_price_in_cents`,
  `currency`, `current_period_ends_at: OptionalNullable[RFC3339DateTime]` (docstring: "when the next regularly
  scheduled attempted charge will occur" → **next billing date**), `next_assessment_at`, `created_at`,
  `activated_at`, `canceled_at`, `customer`.
- Dates are `datetime.datetime` (RFC3339 converter) → `isoformat()`.

### `SubscriptionState` (`models/enums/subscription_state.py`) → `status_from_provider`

| Group | Members |
| --- | --- |
| **done** (enrollment in effect, nothing outstanding) | `ACTIVE`, `TRIALING` |
| **pending** (not done yet / exists but something outstanding) | `PENDING`, `ASSESSING`, `AWAITING_SIGNUP`, `PAST_DUE`, `SOFT_FAILURE`, `UNPAID`, `PAUSED`, `ON_HOLD`, `SUSPENDED` |
| **failed** (never created, or done then undone/ended) | `FAILED_TO_CREATE`, `CANCELED`, `EXPIRED`, `TRIAL_ENDED` |
| **unknown** (not-yet; never done) | any unlisted string (open enum), `UNSET`, `None` |

### Error mapping at the API boundary

| Failure | HTTP answer |
| --- | --- |
| `ApiError` 401/403 (our credentials), 429 (our quota) | 502 `billing_provider_config` / 503 `billing_rate_limited` |
| `ApiError` 422 on create (not "reference taken") | 422 `billing_rejected` with provider messages (caller-actionable) |
| other `ApiError` 4xx | 502 `billing_provider_error` |
| `ApiError` 5xx on a read | 502 |
| `httpx.ConnectError/ConnectTimeout/PoolTimeout/ProxyError` | 502, `outcomeUnknown: false` (never sent) |
| other `httpx.RequestError` on a read | 504, `outcomeUnknown: false` (read — nothing to land) |
| `ValueError`/`ValidationError` on a read | 502 `billing_provider_unreadable` |
| write whose outcome cannot be settled (`OutcomeUnknown`) | 504 `outcome_unknown`, `outcomeUnknown: true`, with the reference |
| echoed plan/reference mismatch (`needs_review`) | 502 `needs_review` |

`str(e)` is never returned; provider messages come only from the typed 422 bodies.

## OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| `subscriptions.create_subscription` (and its lookup `find_subscription`) | `SubscriptionResponse.subscription.state` (`SubscriptionStateOrStr`) | done: `active`, `trialing` → claim `done`, 201 with plan/price/state/next billing. pending: `pending`, `assessing`, `awaiting_signup`, `past_due`, `soft_failure`, `unpaid`, `paused`, `on_hold`, `suspended` → claim `pending`, 202 `outcome: pending` (not reported as subscribed). failed: `failed_to_create`, `canceled`, `expired`, `trial_ended` → claim `failed` (provider id kept), 422 `subscription_failed`. unknown: unlisted string / `UNSET` / `None` → claim `unknown`, 202 `outcome: unknown` | `sandbox/apps/subscriptions/billing.py` `status_from_provider` (the mapping) ← `_read_subscription` (reads `.state` into `Answer.outcome`) ← `safe_write` step 5 `complete(...)`; repeat path refreshes via `subscribe` → `read_subscription` → `status_from_provider`; HTTP answer chosen in `sandbox/apps/subscriptions/views.py` `SubscriptionsView.post` (201/200 only for `done`, 202 pending/unknown/sending, 422 failed) |
| `customers.create_customer` | none — `Customer` has no status member | `Customer.id` set (int) + echoed `Customer.reference == ref` → `done`; missing/non-int id → `Unreadable` → lookup by reference → still missing → `unknown` (504); echoed reference differs → `needs_review` (502) | `sandbox/apps/subscriptions/billing.py` `_read_customer` (raises `Unreadable`, builds `Answer`), `safe_write` steps 3–5, `ensure_customer` (only `done` with a provider id is returned; `sending` → 409 `in_progress`, `needs_review` → 502, else `OutcomeUnknown`) |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| `customers.create_customer` | `MaxioClaim` row (sandbox DB, committed in its own short transaction before the call), `reference = "{prefix}:u{user_pk}:customer"` | DB `UNIQUE` constraint on `MaxioClaim.reference` (`IntegrityError`); at the provider, Maxio's unique customer `reference` (422 "must be unique") | `sandbox/apps/subscriptions/billing.py` `try_claim` catches `IntegrityError` → `False`; `safe_write` step 1 then answers from `load_existing` (in-flight → no provider call; settled → stored outcome) or checks; provider refusal caught in `safe_write` `except ApiError` via `reference_taken` (decided from the typed 422 body `CustomerErrorResponse1`/`ErrorListResponse1`) | `sandbox/apps/subscriptions/billing.py` `ensure_customer` → `customer_reference` + `safe_write` → `try_claim`; model `sandbox/apps/subscriptions/models.py` `MaxioClaim.reference` (`unique=True`) |
| `subscriptions.create_subscription` | `MaxioClaim` row, `reference = "{prefix}:u{user_pk}:sub:{planHandle}:{n}"` (or `…:sub:key:{Idempotency-Key}` when the caller sends one); `n` = 1 + ended (failed-with-provider-id) claims for that user+plan | DB `UNIQUE` on `MaxioClaim.reference`; at the provider, Maxio's unique subscription `reference` (422 "must be unique") | `sandbox/apps/subscriptions/billing.py` `try_claim` (`IntegrityError` → `False`), `safe_write` step 1 (`load_existing`; `sending` < `SEND_WINDOW` → returned without a provider call, answered 202 by `sandbox/apps/subscriptions/views.py` `SubscriptionsView.post`); provider refusal: `safe_write` `except ApiError` → `reference_taken` | `sandbox/apps/subscriptions/billing.py` `subscribe` → `subscription_reference` + `safe_write` → `try_claim`; `MaxioClaim.reference` `unique=True` |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| `customers.create_customer` | same-reference resend (Maxio refuses a second customer with the reference → 422 "reference taken" = landed) then lookup `customers.read_customer_by_reference(ref)`; 404 → still `unknown` | `{prefix}:u{user_pk}:customer` | `sandbox/apps/subscriptions/billing.py` `safe_write` step 2 (`resending` = the check is a same-reference `send`) and step 3 (`find` → `ensure_customer.find` → `customers.read_customer_by_reference` via `_not_found_is_none`); not found / check failed → `complete(UNKNOWN)` + `OutcomeUnknown` (504) |
| `subscriptions.create_subscription` | same-reference resend (Maxio refuses a duplicate reference) then lookup `subscriptions.find_subscription(reference=ref)`; 404 → still `unknown` | the subscription claim reference | `sandbox/apps/subscriptions/billing.py` `safe_write` steps 2–3 (`send` under the same reference, then `find` → `subscribe.find` → `subscriptions.find_subscription(reference=ref)` via `_not_found_is_none`); not found → `complete(UNKNOWN)` + `OutcomeUnknown` (504, `outcomeUnknown: true`) |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| `customers.create_customer` | committed `MaxioClaim(reference, kind=customer, user, outcome=sending, claimed_at)` | same row: `outcome`, `provider_id` (Maxio customer id), `provider_time` (customer `created_at`), `provider_state` | `sandbox/apps/subscriptions/billing.py` `try_claim` (inserts + commits `outcome=sending` in its own `transaction.atomic()` before `send`) and `complete` (records the outcome; `failed` without provider id deletes = releases); views run outside `ATOMIC_REQUESTS` via `transaction.non_atomic_requests` on `sandbox/apps/subscriptions/views.py` `ApiView` |
| `subscriptions.create_subscription` | committed `MaxioClaim(reference, kind=subscription, user, plan_handle, outcome=sending, claimed_at)` | same row: `outcome`, `provider_id` (subscription id), `provider_state`, `provider_time` (subscription `created_at`) | `sandbox/apps/subscriptions/billing.py` `try_claim` → `send` → `complete` inside `safe_write`, called from `subscribe`; `sandbox/apps/subscriptions/views.py` `ApiView` is `non_atomic_requests` so the claim commits before the Maxio call |

## Design notes

- `ATOMIC_REQUESTS=True` would hold the claim in an uncommitted transaction across the provider call (and roll it
  back on an exception, losing the record of a write that may have landed). Views are `non_atomic_requests`;
  claim insert and completion each run in their own `transaction.atomic()`.
- Loser rule: claim in `sending` younger than `SEND_WINDOW` (60 s > 15 s timeout, no retries) → 202 `in_progress`,
  no provider call. Stale `sending` / `unknown` → the check (same-reference resend, then lookup). `done`/`pending`/
  `failed`/`needs_review` → answer from the stored outcome (subscription: refreshed via `read_subscription` so a
  subscription canceled in Maxio since is seen as ended, after which a new plan request takes claim `n+1`).
- `failed` with no provider id (never sent / refused 400/422) deletes the claim → the next request may claim it again.
- Reference prefix: `MAXIO_REFERENCE_PREFIX` or a random per-database install id, so another install on the same
  Maxio site never collides with this one's references.
- Reuses Oscar/Django models: `AUTH_USER_MODEL` for identity (Maxio customer name/email from the Oscar user).
  The only new models are the claim ledger and the install id — Oscar has no subscription/claim model.

## Assumptions & Blockers

- Minor: `trialing` counted as done (a live enrollment); seeded plans have no trial anyway.
- Minor: `payment_collection_method=remittance` (invoice, no card) — the brief says the plans require no payment
  method; smoke showed `automatic` needs one for the signup charge.
- Minor: one live subscription per user per plan unless the caller sends an `Idempotency-Key` header (then each
  new key is a deliberate new subscription).
- No blockers. Every capability needed is in the SDK.

## REQUIRED READING

- MUST load `python-error-handling` — error ladder, transport split, decode failures. (loaded)
- MUST load `python-client-initialization` — sync client, lifetime, close. (loaded)
- MUST load `python-configuration-resilience` — safe write, no retries, server_config. (loaded)
- MUST load `python-testing` — stub transport for tests. (loaded)
- MUST load `python-calling-endpoints` — status mapping, parsed mode. (loaded)
- MUST load `python-models` — UNSET handling, open enums. (loaded)
- MUST load `python-authentication` — basic auth. (loaded)
