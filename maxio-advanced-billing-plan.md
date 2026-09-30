# Maxio Advanced Billing — subscription billing for the django-oscar sandbox

## Scope

Additive, parallel capability on the sandbox site (`sandbox/`): a logged-in shopper lists plans,
subscribes to one, and reads their subscriptions back. Maxio is the system of record.

| Route | View | Maxio operations |
| --- | --- | --- |
| `GET /api/subscription-plans` | list plans in `MAXIO_DEFAULT_PRODUCT_FAMILY` | `product_families.list_products_for_product_family` |
| `POST /api/subscriptions` | ensure customer, then enroll in a plan | `products.read_product_by_handle`, `customers.read_customer_by_reference`, `customers.create_customer`, `subscriptions.create_subscription`, `subscriptions.find_subscription` (settling) |
| `GET /api/my-subscriptions` | the caller's subscriptions, read from Maxio | `customers.list_customer_subscriptions`, `subscriptions.find_subscription` (settling) |

## Repo survey (read-only)

- Host: Django 5.2 under WSGI (`sandbox/wsgi.py`, `runserver`) — **sync**. No ASGI anywhere.
- Settings: `sandbox/settings.py`, `django-environ` `env = environ.Env()`; `DATABASES['default']['ATOMIC_REQUESTS'] = True` (SQLite) — the API views must opt out (`transaction.non_atomic_requests`) so a claim commits before the SDK call.
- Sandbox apps live in `sandbox/apps/` (exemplar: `sandbox/apps/user/models.py` for a plain model module; `sandbox/apps/sitemaps.py`). URL wiring: `sandbox/urls.py` (non-i18n `urlpatterns` list — the `/api/` routes go there, not under `i18n_patterns`, so they have no language prefix).
- Auth: Django session login (`oscar.apps.customer.auth_backends.EmailBackend`, login page `/en-gb/accounts/login/`). CSRF middleware is on; POST requires the `csrftoken` cookie + `X-CSRFToken` header.
- Oscar models reused: `AUTH_USER_MODEL` (Oscar's `auth.User` in the sandbox) via `oscar.core.compat.AUTH_USER_MODEL` / `get_user_model()`.
- No DRF: plain Django `JsonResponse` views.
- Toolchain: `pip` + `venv` (`venv\Scripts\python`), `pip install -e .[test]`, SDK installed from `marketplace/plugins/maxio/sdk/python/` (non-editable). `venv` is git-ignored.
- Tests: `cd sandbox && ..\venv\Scripts\python manage.py test apps.subscriptions` (Django DiscoverRunner, SQLite test DB). The repo's own `tests/` suite targets Postgres and is not touched.
- Type check: no project config. Gate = `mypy --strict` over the new app with `django-stubs` (installed into the venv).
- Baseline: `manage.py check` clean except pre-existing `templates.W003` (thumbnail tag clash) — not ours.

## Credentials / environment

- Env vars (values never written to the repo): `MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`, `MAXIO_ENVIRONMENT`, `MAXIO_DEFAULT_PRODUCT_FAMILY`, optional `MAXIO_BASE_URL`.
- Read in `sandbox/settings.py` as settings `MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`, `MAXIO_DEFAULT_PRODUCT_FAMILY`, `MAXIO_BASE_URL` (+ `MAXIO_ENVIRONMENT`).
- Auth: HTTP Basic, API key as username, password `x` (SDK `README.md`: `curl -u <api_key>:x`).
- Environment: `MAXIO_ENVIRONMENT` mapped explicitly `US→"us"`, `EU→"eu"`; unknown value → `ImproperlyConfigured` (never silently default). Current env = `US` → `"us"` → `https://{site}.chargify.com`.
- `MAXIO_BASE_URL` set → `server_config={"production": {<env>: {"base_url": MAXIO_BASE_URL}}}` verbatim (no `{site}` substitution needed); else `{"production": {<env>: {"site": MAXIO_SITE_SUBDOMAIN}}}`.
- Read-only smoke (2026-09-30, real key): `list_products_for_product_family("handle:eshop-subscribe")` → 2 products (`basic-plan` 2900¢/1 month, `eshop-pro` 29900¢/1 month; IDs already differ from the task table — key on handles). `read_customer_by_reference("<missing>")` → `ApiError` 404 `RawError` (empty body). `find_subscription(reference="<missing>")` → 404 `RawError`. `list_products_for_product_family("handle:<missing>")` → 404 with non-JSON body → **`ValueError` (not `ApiError`)** because the typed `str` arm cannot decode. No 403s.

## Contract sheet

Rule for every row: sync client `MaxioAdvancedBillingClient` only (the async peer mirrors names but is not used). Everything after `*` is keyword-only with a real default — no defensive `None`s. Parsed mode (raises `ApiError`) for every call; none of the in-scope operations returns `None`.

### Client (`maxio_advanced_billing/client.py`; map `sdk-map.md` › Getting a client)

- `MaxioAdvancedBillingClient(*, environment: Environment = "us", timeout: float = 30.0, server_config: ServerConfigOrDict | None, retry_options: int | RetryOptionsOrDict | None, custom_http_client: HttpClient | None, basic_auth: BasicAuthCredentialsOrDict | None, bearer_auth: str | None)`.
- `basic_auth={"username": <key>, "password": "x"}` — omitting it is silent no-auth; the factory refuses to build without a key.
- Held: one lazily built module-level client per process (WSGI; built after fork on first use), guarded by a `threading.Lock`; closed by `atexit`. Tests inject a stub transport via `custom_http_client`.
- `timeout=15.0` (each wait). Sync code: **no whole-call limit exists** through this SDK.
- Retries: **keep the default policy** (GET/HEAD/PUT/OPTIONS on 408/429/5xx/no response, 3 retries). POSTs (`create_customer`, `create_subscription`) are never retried by the SDK; unknown outcomes are settled by reference lookup instead. No retry layer of our own.

### Operations

| Operation (map page) | Signature | Returns (parsed) | `ApiError.error` union |
| --- | --- | --- | --- |
| `product_families.list_products_for_product_family` (`map/operations/product_families.md`) | `(product_family_id: str, *, page=1, per_page=20, ..., include_archived: bool \| None = None, ...)` — `product_family_id` accepts `"handle:<handle>"` (docstring) | `list[ProductResponse]` | `ListProductsForProductFamilyErrorBody` = `str` [404] \| `RawError`; a non-JSON 404 body raises `ValueError` |
| `products.read_product_by_handle` (`map/operations/products.md`) | `(api_handle: str, *, request_options=None)` | `ProductResponse` | Case B `RawError` |
| `customers.read_customer_by_reference` (`map/operations/customers.md`) | `(reference: str, *, request_options=None)` | `CustomerResponse` | Case B `RawError` (404 = no such customer; smoke-confirmed) |
| `customers.create_customer` | `(*, body: CreateCustomerRequest \| CreateCustomerRequestDict \| None = None, request_options=None)` | `CustomerResponse` | `CreateCustomerErrorBody` = `CustomerErrorResponse1` [422] \| `RawError` |
| `customers.list_customer_subscriptions` | `(customer_id: int, *, request_options=None)` | `list[SubscriptionResponse]` | Case B `RawError` |
| `subscriptions.create_subscription` (`map/operations/subscriptions.md`) | `(*, body: CreateSubscriptionRequest \| CreateSubscriptionRequestDict \| None = None, request_options=None)` | `SubscriptionResponse` | `CreateSubscriptionErrorBody` = `ErrorListResponse1` [422] \| `RawError` |
| `subscriptions.find_subscription` | `(*, reference: str \| None = None, request_options=None)` | `SubscriptionResponse` | `FindSubscriptionErrorBody` = `RawError` [404, anything] |

### Models (members the task sets or reads; none carries a wire alias — all Python names = wire names)

- `CreateCustomerRequest(customer: CreateCustomer)` — required.
- `CreateCustomer`: **required** `first_name: str`, `last_name: str`, `email: str`; `reference: Optional[str] = UNSET` (must be unique per site — docstring of `create_customer`: "you may only create one customer for a given reference value").
- `CustomerResponse(customer: Customer)` — required. `Customer.id: Optional[int]`, `reference: OptionalNullable[str]`, `email: Optional[str]` — all `UNSET`-able → assert `id` is `int` after every read/create.
- `CreateSubscriptionRequest(subscription: CreateSubscription)` — required.
- `CreateSubscription` (all optional `UNSET`): `product_handle: Optional[str]`, `customer_id: Optional[int]` ("Required, unless a `customer_reference` or `customer_attributes` is given"), `reference: Optional[str]` ("The reference value (provided by your app) for the subscription itself"). `payment_collection_method: Optional[CollectionMethodOrStr]` — `CollectionMethod`: `automatic`, `remittance`, `prepaid`, `invoice` (docstring: Relationship Invoicing → `remittance|automatic|prepaid`; legacy Statements → `invoice|automatic`). Sent as `remittance` (setting `MAXIO_PAYMENT_COLLECTION_METHOD`): live smoke showed `automatic` (the default) answers 422 "No payment method was on file for the $299.00 balance" because this site captures no card. `defer_signup: bool = False`, `dunning_communication_delay_enabled: bool = False` are real defaults (sent).
- `SubscriptionResponse.subscription: Optional[Subscription] = UNSET` — **not required**: a truncated 2xx decodes cleanly → assert `subscription` and `subscription.id` present.
- `Subscription` members read: `id: Optional[int]`, `state: Optional[SubscriptionStateOrStr]`, `reference: OptionalNullable[str]`, `product: Optional[Product]`, `product_price_in_cents: Optional[int]`, `current_billing_amount_in_cents: Optional[int]`, `next_assessment_at: OptionalNullable[RFC3339DateTime]`, `current_period_ends_at: OptionalNullable[RFC3339DateTime]`, `activated_at`, `created_at`, `canceled_at` (datetimes), `currency: Optional[str]`, `customer: Optional[Customer]`.
- `ProductResponse(product: Product)` — required. `Product` members read (all `UNSET`-able): `id`, `name`, `handle: OptionalNullable[str]`, `description: OptionalNullable[str]`, `price_in_cents: Optional[int]`, `interval: Optional[int]`, `interval_unit: Optional[IntervalUnitOrStr]`, `trial_price_in_cents`, `trial_interval`, `trial_interval_unit`, `initial_charge_in_cents`, `require_credit_card: Optional[bool]`, `archived_at: OptionalNullable[RFC3339DateTime]`, `product_family: Optional[ProductFamily]` (`.handle`).
- `ErrorListResponse1.errors: list[str]` (required). `CustomerErrorResponse1.errors: Optional[ErrorsModel]` (ErrorsModel members: `per_page`, `price_point` — `Optional[list[str]]`; other keys preserved as extras → read via `to_dict()`).
- `Optional[T]` here is `T | UnsetType` — never pass `None`. Narrow with `isinstance(x, UnsetType)` before use. Map every SDK value into our own dicts before `JsonResponse` (UNSET is not JSON-serialisable).
- No member in scope is typed bare `Optional[Any]`.

### Enums (`maxio_advanced_billing/models/enums/`)

- `SubscriptionState` (open, `SubscriptionStateOrStr`): `pending`, `failed_to_create`, `trialing`, `assessing`, `active`, `soft_failure`, `past_due`, `suspended`, `canceled`, `expired`, `paused`, `unpaid`, `trial_ended`, `on_hold`, `awaiting_signup`.
- `IntervalUnit`: `day`, `month`.

### What each status means (the outcome of `create_subscription`, read from `subscription.state`)

| State | Enrolment outcome |
| --- | --- |
| `active`, `trialing` | **done** |
| `pending`, `assessing`, `awaiting_signup` | **not done yet** |
| `failed_to_create` | **failed** |
| `soft_failure`, `past_due`, `unpaid`, `on_hold`, `paused`, `suspended`, `canceled`, `expired`, `trial_ended` | **failed** (a final/lapsed state the call did not ask for) |
| anything else / missing | **not done** (recorded, reported as-is, not claimed active) |

The subscription id alone is not the outcome. Local record status follows this table: `active` / `provisioning` / `failed`.

### Error boundary (one map, `apps/subscriptions/maxio.py`)

`MaxioError(status_code, message, outcome_unknown)`:
- `ApiError` 401/403 → 502 (our credentials); 429 → 503; typed 4xx (`ErrorListResponse1`, `CustomerErrorResponse1`) → same 4xx with provider messages; other 4xx → same 4xx generic; 5xx → 502, `outcome_unknown=True`.
- `pydantic.ValidationError`/`ValueError` from a decode → 502 `outcome_unknown=True` (the create path only; for reads it is just "unreadable").
- `httpx.ConnectError|ConnectTimeout|PoolTimeout|ProxyError` → 502, `outcome_unknown=False` (never sent).
- other `httpx.RequestError` → 504, `outcome_unknown=True`.
- `str(e)` never surfaced; log status + our own summary.

## DUPLICATE CLAIMS

| Write | Where the claim is stored | What rejects the second one | Where that rejection is caught | Where in the code |
| --- | --- | --- | --- | --- |
| `customers.create_customer` (one Maxio customer per app user) | `MaxioCustomer` row (SQLite, app DB) — `user` OneToOne + unique `reference`, inserted with `status=pending` in its own committed transaction before the SDK call | DB `UNIQUE` on `MaxioCustomer.user_id` → `IntegrityError`; second barrier: Maxio's unique customer `reference` (422) | `except IntegrityError` in the claim; 422 → settle via `read_customer_by_reference` | claim: `services.ensure_customer` (`MaxioCustomer.objects.create` in `transaction.atomic`; stale takeover `services._take_lease`) → SDK call: `services.ensure_customer` (`client.customers.create_customer`) |
| `subscriptions.create_subscription` (one open subscription per user+plan) | `MaxioSubscription` row — conditional `UniqueConstraint(user, plan_handle, condition=status in open statuses)`, inserted with `status=pending` + fresh `reference` before the SDK call | DB partial unique index → `IntegrityError` | `except IntegrityError` in the claim → return the existing record (200 if settled, 409 if still in flight) | claim: `services._claim_subscription` → SDK call: `services.subscribe` (`client.subscriptions.create_subscription`) |

## UNKNOWN OUTCOMES

| Write | The operation you re-read with | The reference you search by | Where in the code | The test that leaves the outcome unknown |
| --- | --- | --- | --- | --- |
| `customers.create_customer` | `customers.read_customer_by_reference` | `MaxioCustomer.reference` (`<prefix><user.pk>`) | `except MaxioError` in `services.ensure_customer` → `services._find_customer`; unsettled claim stays `pending` and the next call (after `STALE_CLAIM_AFTER`) settles it via `_take_lease` + `_find_customer` | `SubscribeTests.test_customer_create_timeout_is_settled_by_reference`, `SubscribeTests.test_stale_customer_claim_is_settled_by_reference` |
| `subscriptions.create_subscription` | `subscriptions.find_subscription` | `MaxioSubscription.reference` (UUID minted at claim time, sent as `CreateSubscription.reference`) | `except MaxioError` in `services.subscribe` → `services.settle_subscription`; still-unknown records (`status=unknown`) settled by `services.my_subscriptions` and by `services.subscribe` on the next POST (`_needs_settling`) | `SubscribeTests.test_refused_connection_is_known_but_read_timeout_is_unknown`, `test_outcome_stays_unknown_until_a_later_read_settles_it`, `test_unknown_outcome_not_found_releases_the_claim`, `test_truncated_success_is_not_reported_as_success`, `test_unreadable_success_is_an_unknown_outcome` |

Unsettled records stay `status=unknown`; `GET /api/my-subscriptions` and the next `POST` for the same plan re-run the settle step.

## Assumptions & Blockers

- No blockers. Every capability is in the plugin's SDK.
- Minor: customer reference = `MAXIO_CUSTOMER_REFERENCE_PREFIX` (default `oscar-sandbox-user-`) + user pk; overridable so two sandboxes sharing one Maxio site don't collide.
- Minor: "one open subscription per user per plan" is the double-click rule; subscribing to a different plan is allowed.
- Minor: plans = non-archived products in the default family; POST rejects a handle outside it (404).

## Build order

1. settings (`MAXIO_*`), app `sandbox/apps/subscriptions/` (apps.py, models.py + migration, maxio.py client/boundary, services.py, views.py, urls.py), wire in `INSTALLED_APPS` + `sandbox/urls.py`.
2. tests (stub transport) → `manage.py test apps.subscriptions` + `mypy --strict`.
3. Live verify on `runserver` bound to port 39680.

## REQUIRED READING

- MUST load `python-error-handling` — boundary map, decode vs transport split (loaded).
- MUST load `python-client-initialization` — module-level client, close obligation (loaded).
- MUST load `python-calling-endpoints` — keyword-only tail, state vs id (loaded).
- MUST load `python-models` — UNSET narrowing, mapping out (loaded).
- MUST load `python-configuration-resilience` — server_config nesting, retries, duplicate claims, unknown outcomes (loaded).
- MUST load `python-authentication` — basic auth, missing-credential silence (loaded).
- MUST load `python-testing` — stub transport seam, both transport failure kinds (loaded).
