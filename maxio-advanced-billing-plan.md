# Maxio Advanced Billing — subscription billing for the django-oscar sandbox

## Scope

Additive, parallel recurring-subscription capability on the sandbox site (`sandbox/`), Maxio as the
system of record. New Django app `sandbox/apps/subscriptions/` wired into `sandbox/urls.py`:

| Endpoint | Action |
| --- | --- |
| `GET /api/subscription-plans` | list the plans in `MAXIO_DEFAULT_PRODUCT_FAMILY` (each with `planHandle`) |
| `GET /api/billing-customer` / `POST /api/billing-customer` | read / ensure (idempotent) the caller's Maxio customer |
| `POST /api/subscriptions` | subscribe the caller to `planHandle` (ensures the customer first); returns top-level `subscriptionId` |
| `GET /api/my-subscriptions` | the caller's subscriptions, read live from Maxio, reconciling any unsettled local claim |

Auth: Django session login (Oscar's existing `/en-gb/accounts/login/`); identity = `request.user`.
CSRF stays enforced on the POSTs (caller sends `X-CSRFToken`). Unauthenticated → `401` JSON.
Oscar models reused: the Oscar/Django user (`AUTH_USER_MODEL`) is the customer identity; name/email
come from it. Local tables hold only *claims* (that we asked Maxio for something) — not a parallel
copy of catalogue/order models.

## Repo survey (read-only)

- Sync Django 5.2 under WSGI/runserver → **sync client** (`MaxioAdvancedBillingClient`).
- Sandbox app convention: `sandbox/apps/<name>` imported as `apps.<name>` (see `sandbox/urls.py`
  import `from apps.sitemaps import base_sitemaps`). Settings exemplar: `sandbox/settings.py` uses
  `env = environ.Env()` with `env.str(..., default=...)`.
- Store: SQLite (default) / any Django DB; unique constraints + `IntegrityError` are available → the
  claim store. `ATOMIC_REQUESTS=True` → claim inserts must use their own `transaction.atomic()`
  savepoint so an `IntegrityError` can be caught without poisoning the request transaction, and the
  claim must be **committed before** the provider call → views that write claims are
  `transaction.non_atomic_requests`.
- Toolchain: `py -3.11 -m venv venv`, `venv\Scripts\pip install -e .[test]`; SDK installed from
  `git+https://github.com/context-plugins/maxio-python-sdk.git@main` (v1.0, identical to the clone the
  map was read from); `mypy` + `django-stubs` installed into the venv for the type gate.
- Tests: Django `TestCase` in the app (`cd sandbox && ..\venv\Scripts\python manage.py test apps.subscriptions`).
- Baseline: `manage.py check` → 1 pre-existing warning (templates.W003 thumbnail). Sandbox DB built
  (209 products, 249 countries, 2 users). NB: the fixture-only sequence failed with FK errors on
  `orders.json`/`offers.json`; running `oscar_import_catalogue` on the three books CSVs first fixed it.
- Credentials: env `MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`, `MAXIO_ENVIRONMENT` (=`US`),
  `MAXIO_DEFAULT_PRODUCT_FAMILY` present; values never written to the repo.
- Read-only smoke (scratch dir, real key): `list_products_for_product_family(f"handle:{MAXIO_DEFAULT_PRODUCT_FAMILY}")`
  → 2 products (`basic-plan` 2900, `eshop-pro` 29900, month); `read_customer_by_reference` /
  `find_subscription` with an unknown reference → `ApiError` 404 `RawError`. No 403s.

## Contract sheet (every row grounded in `sdk-map.md` / `map/operations/*.md` / the named module)

### Client

| Fact | Value | Source |
| --- | --- | --- |
| Class | `MaxioAdvancedBillingClient` (sync). Never the async client. | sdk-map.md *Getting a client* |
| Constructor | keyword-only: `environment`, `timeout`, `server_config`, `custom_http_client`, `basic_auth`, `bearer_auth` | sdk-map.md constructor table |
| Lifetime | one lazily-built module-level client per process (built on first use → after any fork), thread-safe; `close()` at `atexit` | python-client-initialization |
| Auth | `basic_auth=BasicAuthCredentials(username=<MAXIO_API_KEY>, password="x")` — README: `curl -u <api_key>:x`. Missing key → `ImproperlyConfigured` (never an unauthenticated client). | README.md l.32; sdk-map.md *Servers & auth* |
| Environment | explicit always. `MAXIO_ENVIRONMENT` → `{"us": "us", "eu": "eu"}` (case-insensitive); anything else → `ImproperlyConfigured` (omitting would silently be `"us"`; `maxio_api_gateway` needs a bearer connector token — out of scope). | server/environment.py |
| Base URL | `server_config={"production": {<env>: {"site": MAXIO_SITE_SUBDOMAIN}}}`; if `MAXIO_BASE_URL` set → `{"production": {<env>: {"base_url": MAXIO_BASE_URL}}}` verbatim. Nesting server → environment → field (`extra="forbid"`). | server/server_config.py |
| Timeout | `MAXIO_TIMEOUT_SECONDS` (default 15.0) on our own `HttpxClient` transport (client `timeout=` does not reach a custom transport) | python-configuration-resilience |
| Transport | `LoggingTransport(HttpxClient(timeout=…))` logging method/URL/status only | `core` exports `HttpxClient` |
| Retries | **none** in the SDK; we add none (reads fail fast → 502/504; writes go through the safe write) | sdk-map.md; python-configuration-resilience |
| Async peer rule | every operation has an identical `Async…` peer — not used | sdk-map.md |

### Operations in scope

Every keyword-only parameter has a real default — pass only what is set, no defensive `None`s.
All are server `production`, auth `basic_auth OR bearer_auth`. None of them returns `None`.

| Operation | Signature (positional \| keyword-only) | Returns | `ApiError.error` union | Notes |
| --- | --- | --- | --- | --- |
| `client.product_families.list_products_for_product_family` | `(product_family_id: str, *, page=1, per_page=20, …, include_archived=None, …)` | `list[ProductResponse]` | `ListProductsForProductFamilyErrorBody` = `str` [404] \| `RawError` | `product_family_id` = id **or `"handle:<handle>"`** (docstring). We pass `f"handle:{MAXIO_DEFAULT_PRODUCT_FAMILY}"`, `per_page=200`. |
| `client.products.read_product_by_handle` | `(api_handle: str, *)` | `ProductResponse` | Case B `RawError` (404 = unknown handle) | validates `planHandle` on subscribe; must belong to the configured family and not be archived |
| `client.customers.create_customer` | `(*, body: CreateCustomerRequest \| Dict)` | `CustomerResponse` | `CreateCustomerErrorBody` = `CustomerErrorResponse1` [422] \| `RawError` | WRITE (creates). Body is optional in the signature but always passed. |
| `client.customers.read_customer_by_reference` | `(reference: str, *)` | `CustomerResponse` | Case B `RawError` (404 = none) | the may-have-landed lookup for create_customer |
| `client.subscriptions.create_subscription` | `(*, body: CreateSubscriptionRequest \| Dict)` | `SubscriptionResponse` | `CreateSubscriptionErrorBody` = `ErrorListResponse1` [422] \| `RawError` | WRITE (creates). |
| `client.subscriptions.find_subscription` | `(*, reference: str \| None = None)` | `SubscriptionResponse` | `FindSubscriptionErrorBody` = `RawError` [404, anything] | the may-have-landed lookup for create_subscription |
| `client.subscriptions.read_subscription` | `(subscription_id: int, *, include=None)` | `SubscriptionResponse` | Case B `RawError` | refresh a known subscription |
| `client.customers.list_customer_subscriptions` | `(customer_id: int, *)` | `list[SubscriptionResponse]` | Case B `RawError` | live read for my-subscriptions |
| `client.sites.read_site` | `(*)` | `SiteResponse` (`site: Site`, required) | Case B `RawError` | read once per process: `Site.currency: Optional[str]`, `Site.relationship_invoicing_enabled: Optional[bool]` (`models/site.py`) |

### Models (members the task sets or reads; `Optional[T]` = `T | UnsetType`, **never `None`**)

| Model (module) | Member | Req? | Use |
| --- | --- | --- | --- |
| `CreateCustomerRequest` (`models/create_customer_request.py`) | `customer: CreateCustomer` | required | |
| `CreateCustomer` (`models/create_customer.py`) | `first_name: str`, `last_name: str`, `email: str` | required | from Oscar user (fallbacks for blank names) |
| | `reference: Optional[str]` | UNSET-able — **always set** | client reference `"{prefix}-customer-u{user.pk}"`. api-reference.md: "If provided, the `reference` value must be unique" → Maxio refuses a second create with the same reference |
| `CustomerResponse` | `customer: Customer` | required | |
| `Customer` (`models/customer.py`) | `id: Optional[int]`, `reference: OptionalNullable[str]`, `email: Optional[str]`, `created_at: Optional[RFC3339DateTime]` | UNSET-able | **assert `id` is set** (else outcome unknown); `created_at` = provider time |
| `CreateSubscriptionRequest` (`models/create_subscription_request.py`) | `subscription: CreateSubscription` | required | |
| `CreateSubscription` (`models/create_subscription.py`) | `product_handle: Optional[str]`, `customer_id: Optional[int]`, `reference: Optional[str]`, `payment_collection_method: Optional[CollectionMethodOrStr]` | UNSET-able | set all four; `reference` **always set** = `"{prefix}-sub-u{user.pk}-{plan_handle}-{n}"`; `customer_id` must be an `int` (never `None`) |
| `SubscriptionResponse` (`models/subscription_response.py`) | `subscription: Optional[Subscription]` | UNSET-able | **assert set** |
| `Subscription` (`models/subscription.py`) | `id: Optional[int]`, `state: Optional[SubscriptionStateOrStr]`, `reference: OptionalNullable[str]`, `product: Optional[Product]`, `product_price_in_cents: Optional[int]`, `current_billing_amount_in_cents: Optional[int]`, `currency: Optional[str]`, `next_assessment_at: OptionalNullable[RFC3339DateTime]`, `current_period_ends_at: OptionalNullable[RFC3339DateTime]`, `created_at`, `updated_at: Optional[RFC3339DateTime]`, `activated_at: OptionalNullable[RFC3339DateTime]`, `customer: Optional[Customer]` | UNSET-able | **assert `id` and `state`**; next-billing-date = `next_assessment_at` (fallback `current_period_ends_at`) |
| `ProductResponse` / `Product` (`models/product.py`) | `product: Product` (required); `id`, `name`, `price_in_cents`, `interval: Optional[int]`, `interval_unit: Optional[IntervalUnitOrStr]`, `handle`, `description`, `archived_at: OptionalNullable`, `require_credit_card`, `product_family: Optional[ProductFamily]`, `trial_price_in_cents`, `initial_charge_in_cents` | | plan listing |
| `ErrorListResponse1` (`models/error_list_response1.py`) | `errors: list[str]` | required | create_subscription 422 message |
| `CustomerErrorResponse1` (`models/customer_error_response1.py`) | `errors: Optional[Errors1]`, `Errors1 = CustomerError \| list[str]`, `CustomerError.customer: Optional[str]` | | create_customer 422 message |

No member we set is typed `Optional[Any]`. No wire alias on any member we touch (no `Field(alias=…)`
on the fields above). Date-times arrive as `datetime.datetime` (`RFC3339DateTime`).

### Enums

`SubscriptionState` (`models/enums/subscription_state.py`) — open (`SubscriptionStateOrStr`), members
and the outcome each maps to (`status_from_provider`):

| Member | Outcome | Why |
| --- | --- | --- |
| `ACTIVE`, `TRIALING` | **done** | subscription is live and in effect |
| `PENDING`, `ASSESSING`, `AWAITING_SIGNUP` | **pending** | provider not finished (docstring: transient/in creation) |
| `PAST_DUE`, `SOFT_FAILURE`, `UNPAID`, `ON_HOLD`, `PAUSED`, `SUSPENDED` | **pending** | exists but something is wrong — not done; surfaced, never success |
| `FAILED_TO_CREATE` | **failed** | signup failed |
| `CANCELED`, `EXPIRED`, `TRIAL_ENDED` | **failed** | happened and was undone / ended — no longer in effect |
| any other value, or absent | **unknown** | never done |

`IntervalUnit` (`models/enums/interval_unit.py`): `DAY`, `MONTH` — rendered via `str()` (str enum).

`CollectionMethod` (`models/enums/collection_method.py`): `AUTOMATIC`, `REMITTANCE`, `PREPAID`, `INVOICE`.
Docstring: Relationship Invoicing sites take `remittance`/`automatic`/`prepaid`; legacy Statements sites
take `invoice`/`automatic`. Found in the live run: with `automatic` (the site default) Maxio refuses a
signup with no card on file ("No payment method was on file for the $299.00 balance") even though the
plan does not require one at signup. This app captures no card, so subscriptions are **invoiced**:
`MAXIO_PAYMENT_COLLECTION_METHOD` if set, else `remittance` when `relationship_invoicing_enabled` is
not `False`, else `invoice`.

### Error boundary (python-error-handling)

One translation function around every SDK call → `MaxioError(status_code, code, message, outcome_unknown)`:
- `ApiError` 401/403 → 502 `provider_auth`; 429 → 503 `provider_rate_limited`; typed 422 → 422 with the
  provider's messages; other 4xx → same 4xx (`provider_rejected`); 5xx → 502 `provider_error`.
- `pydantic.ValidationError`/`ValueError` on a 2xx → 502 `provider_unreadable` (never "not found").
- `httpx.ConnectError | ConnectTimeout | PoolTimeout | ProxyError` → 502, `outcome_unknown=False`.
- other `httpx.RequestError` → 504, `outcome_unknown=True`.
- `OutcomeUnknown` from the safe write → 504 `outcome_unknown`.
- No `str(e)` to callers.

## OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| `subscriptions.create_subscription` | `SubscriptionResponse.subscription.state` (`SubscriptionStateOrStr`) | done: `active`, `trialing` → claim `done`, `201`. not-yet: `pending`, `assessing`, `awaiting_signup`, `past_due`, `soft_failure`, `unpaid`, `on_hold`, `paused`, `suspended` → claim `pending`, `202`, reconciled on read. failed: `failed_to_create`, `canceled`, `expired`, `trial_ended` → claim `failed` (keeps its provider id; releases the per-plan slot), `409`. Anything else / absent / unreadable → `unknown`, `202`. A plan or reference echoed back that is not what we sent → `needs_review`, `409`. | `billing.status_from_provider`, called from `billing._read_subscription` and recorded by `billing._complete` inside `billing.safe_write`; later reads via `billing.refresh_claim`; HTTP status chosen in `views.subscriptions` (`SUBSCRIBE_STATUS`; 201/200 only for `done`) |
| `customers.create_customer` | none — `Customer` has no status member; the write's completion is the readable `customer.id` echoed with our `reference` | `id` set and `reference` equals ours → `done` (201 new / 200 repeat); `id` UNSET or body unreadable → `unknown`, 504 `outcome_unknown`; `reference` not ours → `needs_review`, 409 | `billing._read_customer` (raises `ValueError` on a missing id → `billing.safe_write` step 4 marks `unknown` and raises `OutcomeUnknown`); `billing._complete` (as_asked=False → `needs_review`); answered by `billing._customer_result` and `views.billing_customer` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| `customers.create_customer` | DB row `BillingCustomer` (one per user), inserted and committed before the call, carrying `reference` and `outcome="sending"` | DB unique constraints: `user` (OneToOne) and `reference` (unique) → `IntegrityError` | `IntegrityError` caught around the insert (own savepoint) → load existing row and answer from it / check it | claim: `models.BillingCustomer` (`user` OneToOne, `reference` unique), inserted by the `insert_claim` lambda in `billing.ensure_customer`; rejection caught in `billing.safe_write` step 1 (`except IntegrityError`); views `views.billing_customer` / `views.subscriptions` are `transaction.non_atomic_requests` so the claim commits before the call |
| `subscriptions.create_subscription` | DB row `SubscriptionClaim`, inserted and committed before the call, carrying `reference` and `outcome="sending"` | DB unique constraint on `reference`, plus partial unique constraint on (`user`, `plan_handle`) where `slot_open=True` → `IntegrityError` | `IntegrityError` caught around the insert (own savepoint) → load the open claim and answer from it / check it | claim: `models.SubscriptionClaim` (`reference` unique + `maxio_one_open_subscription_per_user_plan` partial unique constraint), inserted by `insert_claim` inside `billing.subscribe`; rejection caught in `billing.safe_write` step 1; loser answered from `load_claim` in `billing.subscribe` (fresh `sending` → 202 with no provider call) |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| `customers.create_customer` | lookup `customers.read_customer_by_reference(reference)`; 404 → still `unknown`. A later request finding the claim `unknown` (or stale `sending`) re-runs that lookup; a first-send 422 is followed by the same lookup (the reference is unique at Maxio, so a hit means an earlier attempt landed). Never a create under a new reference. | `BillingCustomer.reference` (`{prefix}-customer-u{pk}`) | `billing.safe_write` step 3 (and the step-2 rejection lookup, `rejection_may_be_duplicate=True`) with `find=client.customers.read_customer_by_reference` passed by `billing.ensure_customer`; unsettled claims re-checked by `billing._settle_customer` from `billing.my_subscriptions` |
| `subscriptions.create_subscription` | lookup `subscriptions.find_subscription(reference=reference)`; 404 → still `unknown`. Later requests (subscribe repeat, my-subscriptions) re-run the lookup — never a resend (Maxio does not document subscription-reference uniqueness, so a resend could duplicate). | `SubscriptionClaim.reference` | `billing.safe_write` step 3 with `find` (→ `client.subscriptions.find_subscription(reference=ref)`) defined in `billing.subscribe`; a repeat that finds the claim `unknown`/stale `sending` runs only that lookup (`checking=True`); unsettled claims re-checked by `billing._settle_subscription` from `billing.my_subscriptions` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| `customers.create_customer` | committed `BillingCustomer(user, reference, outcome="sending", claimed_at)` | `outcome` (done/unknown/needs_review), `maxio_customer_id`, `provider_time` (Maxio `created_at`); a never-sent or refused first send deletes the claim | before: `insert_claim` lambda in `billing.ensure_customer` (run by `billing.safe_write` step 1); after: `billing._complete` with the `Answer` from `billing._read_customer`; release: `billing._release_customer_claim` |
| `subscriptions.create_subscription` | committed `SubscriptionClaim(user, plan_handle, reference, outcome="sending", slot_open=True, claimed_at, plan_name, price_in_cents, currency)` | `outcome` via `status_from_provider`, `maxio_subscription_id`, `state`, price, currency, `next_billing_at`, `provider_time` (Maxio `updated_at`); `slot_open=False` only for `failed` | before: `insert_claim` inside `billing.subscribe` (run by `billing.safe_write` step 1); after: `billing._complete` with the `Answer` from `billing._read_subscription` / `billing._subscription_fields`; release: `billing._release_subscription_claim` |

## Design decisions (YOUR CALL)

- Install-unique reference prefix: `MAXIO_REFERENCE_PREFIX` setting if set; else a per-database random
  token stored in a singleton `BillingInstall` row (created by the app's migration), so two installs
  sharing one Maxio site never collide on `…-u1`.
- Subscription reference `{prefix}-sub-u{pk}-{plan}-{n}`, `n` = number of prior claims for that user+plan
  + 1 (deterministic; two racing requests derive the same `n` → the unique `reference` rejects one).
- One open subscription claim per (user, plan); subscribing to the other plan is a separate subscription.
- Stale `sending` window: 2 × timeout + 30 s; within it a repeat answers `202 in_progress` and makes no
  provider call; past it the repeat runs the lookup only.
- `failed` with no provider id (never sent / refused 4xx) deletes the claim row (releases it) for
  customers; for subscriptions it sets `slot_open=False` (record kept for audit, next attempt uses `n+1`).
- my-subscriptions answers live from `list_customer_subscriptions` and updates matching claims.
- Status codes: subscribe `201` done (new) / `200` done (repeat) / `202` pending|unknown|in-progress /
  `409` failed or needs_review (and `customer_in_progress` while a concurrent request is creating the
  customer) / `404` unknown plan / `400` bad input / `401` anonymous / `403` missing CSRF token /
  `422` provider rejected / `502` provider or config / `504` outcome unknown.
- Retries: none added. Reads fail fast (502/504); writes are never blindly resent — an unknown outcome
  is settled only by the lookup by reference.
- Collection method: invoiced by default (see *Enums*), configurable via `MAXIO_PAYMENT_COLLECTION_METHOD`.
- SDK dependency pinned in `requirements_billing.txt` (commit `3d84ece`, identical to the clone the map
  was read from); its runtime deps are listed there too because the built wheel declares none to pip.

## Assumptions & Blockers

- No blockers. The DB can hold a claim that outlives the process (unique constraints).
- Minor: `MAXIO_ENVIRONMENT` interpreted as the SDK region (`US`→`us`, `EU`→`eu`); the "sandbox" Maxio
  site is selected by the subdomain/key, not by a separate host.
- Minor: Maxio customer `reference` uniqueness is documented (api-reference.md); subscription-reference
  uniqueness is not, so the subscription check is lookup-only.
- Host note correction: the build sequence given for the sandbox fails at `offers.json`/`orders.json`
  (FK errors) unless `oscar_import_catalogue` is run on the three `books.*.csv` fixtures first; with it
  the catalogue has the expected 209 products.

## REQUIRED READING

| Hazard | Pointer |
| --- | --- |
| error ladder, transport failure split, decode failures | MUST load `python-error-handling` (loaded) |
| client lifetime, sync choice, custom transport timeout | MUST load `python-client-initialization` (loaded) |
| safe write, no retries, server_config nesting, logging transport | MUST load `python-configuration-resilience` (loaded) |
| UNSET vs None, open enums, handing values out | MUST load `python-models` (loaded) |
| status → outcome, done allow-list | MUST load `python-calling-endpoints` (loaded) |
| basic auth, missing-credential silence | MUST load `python-authentication` (loaded) |
| stub transport tests, both transport failure kinds | MUST load `python-testing` (loaded) |
