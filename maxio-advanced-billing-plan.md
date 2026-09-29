# Maxio Advanced Billing — subscription billing for the Oscar sandbox

Plan and contract sheet for adding recurring-subscription billing, with Maxio Advanced
Billing as the system of record, to the runnable sandbox site at `sandbox/`.

## Scope

| Endpoint | What it does |
| --- | --- |
| `GET /api/subscription-plans` | Lists the non-archived products in the product family `MAXIO_DEFAULT_PRODUCT_FAMILY`; each entry carries `planHandle`. |
| `POST /api/subscriptions` | `{"planHandle": "..."}` (optional `Idempotency-Key` header). Makes sure a Maxio customer exists for `request.user` (step 1), then subscribes that customer to the plan (step 2). Returns `subscriptionId` at the top level. |
| `GET /api/my-subscriptions` | Reads the caller's subscriptions back from Maxio (customer found by reference → subscriptions for that customer), plus any local requests Maxio has not settled yet. |

Auth: Django session login (the sandbox's `/<lang>/accounts/login/`); the caller is `request.user`.
Unauthenticated → `401` JSON. CSRF is enforced on the POST (Django default).

## Repo survey

| Convention | Pattern | Exemplar |
| --- | --- | --- |
| Sandbox apps | plain packages under `sandbox/apps/`, imported as `apps.<name>` (the `sandbox/` dir is on `sys.path`) | `sandbox/apps/sitemaps.py`, `sandbox/urls.py` (`from apps.sitemaps import ...`) |
| URL wiring | `sandbox/urls.py`: non-i18n paths in `urlpatterns`, Oscar under `i18n_patterns` | `sandbox/urls.py` |
| Settings | `django-environ` `env = environ.Env()`; `env('X', default=...)` | `sandbox/settings.py` |
| Models / identity | Oscar's `AUTH_USER_MODEL` via `oscar.core.compat.AUTH_USER_MODEL` | `sandbox/apps/user/models.py` |
| DB | SQLite, **`ATOMIC_REQUESTS = True`** → the write views must be `transaction.non_atomic_requests` so each claim step commits on its own (otherwise a rollback erases the claim / the `unknown` record) | `sandbox/settings.py` |
| Sync vs async | Django under WSGI, sync views everywhere → **sync** `MaxioAdvancedBillingClient` | `sandbox/wsgi.py` |
| Tests | repo suite is pytest + PostgreSQL (baseline: DB tests fail with "connection refused" — no Postgres here). App tests use Django `TestCase`, run with `sandbox/manage.py test apps.subscriptions` on SQLite | `tests/integration/...` |
| Lint / type check | flake8 (`setup.cfg`, max 119). No mypy configured → `mypy --strict` + `django-stubs` from a scratch config on the files I add | `setup.cfg` |

Toolchain: `py -3.11 -m venv venv`, `venv\Scripts\pip install -e .[test]`; SDK installed from source
`pip install "git+file:///D:/APIMatic/sdk-regen/maxio-python-sdk@main"` (distribution
`maxio-advanced-billing` 1.0, import `maxio_advanced_billing`). It is a sandbox-only dependency (the
`django-oscar` package itself does not need it), recorded in `sandbox/requirements_maxio.txt`.

Credentials: `MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`, `MAXIO_ENVIRONMENT` (`US`), `MAXIO_DEFAULT_PRODUCT_FAMILY`
are set in the environment. Read-only smoke done from a scratch script (list products for
`handle:eshop-subscribe` → `basic-plan` 2900, `eshop-pro` 29900, both `1 month`; customer and
subscription lookups by an unknown reference → `404`). No endpoint in scope is gated.

## Contract sheet

Client (sync; the two clients do not mix):

| Fact | Value | Source |
| --- | --- | --- |
| Class | `MaxioAdvancedBillingClient` from `maxio_advanced_billing` | sdk-map.md "Getting a client" |
| Constructor | keyword-only: `environment`, `timeout`, `server_config`, `basic_auth`, (`bearer_auth`, `custom_http_client`) | sdk-map.md constructor table |
| Environment | `Literal["us","eu","maxio_api_gateway"]`, default `"us"` **silently** → always passed explicitly from `MAXIO_ENVIRONMENT` via an explicit map (`us`/`eu`, case-insensitive), unknown value → `ImproperlyConfigured` | `server/environment.py` |
| Base URL | `production` server: us `https://{site}.chargify.com`, eu `https://{site}.ebilling.maxio.com`; `{site}` defaults to `"subdomain"`. Set `server_config={"production": {<env>: {"site": MAXIO_SITE_SUBDOMAIN}}}`; when `MAXIO_BASE_URL` is set: `{"production": {<env>: {"base_url": MAXIO_BASE_URL}}}` (verbatim) | sdk-map.md Servers & auth; `server/server_config.py` |
| Auth | Basic: `basic_auth=BasicAuthCredentials(username=MAXIO_API_KEY, password="x")` (API key as user, `x` as password — README `curl -u <api_key>:x`). Omitting it sends unauthenticated requests silently → missing key is `ImproperlyConfigured` before construction | README.md; sdk-map.md |
| Timeout | default `30.0`; set `15.0` (single float, connect/read/write/pool) | sdk-map.md |
| Lifetime | one lazily built module-level client per process (built after fork, on first use), `close()` via `atexit` | python-client-initialization |
| Retries | the SDK does none. Writes: never retried blindly — the safe write below. Reads: deliberately not retried (a failed read answers 502/504 and the caller may repeat) | python-configuration-resilience |
| Keyword-only | every parameter after `*` has a real default — no defensive `None`s | map pages |
| Async twin | exists for every op, not used | sdk-map.md |

Operations (all server `production`, auth `basic_auth` OR `bearer_auth`):

| Operation | Route | Signature (positional before `*`) | Returns | `ApiError.error` union |
| --- | --- | --- | --- | --- |
| `client.product_families.list_products_for_product_family` | `GET /product_families/{product_family_id}/products.json` | `(product_family_id: str, *, page=1, per_page=20, ..., include_archived=None, ...)`; id or `handle:<handle>` (docstring) | `list[ProductResponse]` | `ListProductsForProductFamilyErrorBody` = `str` [404] \| `RawError` |
| `client.customers.create_customer` | `POST /customers.json` | `(*, body: CreateCustomerRequest \| Dict \| None = None)` | `CustomerResponse` | `CreateCustomerErrorBody` = `CustomerErrorResponse1` [422] \| `RawError` |
| `client.customers.read_customer_by_reference` | `GET /customers/lookup.json` | `(reference: str, *)` | `CustomerResponse` | `RawError` (Case B); 404 when no match (smoke) |
| `client.customers.list_customer_subscriptions` | `GET /customers/{customer_id}/subscriptions.json` | `(customer_id: int, *)` | `list[SubscriptionResponse]` | `RawError` (Case B) |
| `client.subscriptions.create_subscription` | `POST /subscriptions.json` | `(*, body: CreateSubscriptionRequest \| Dict \| None = None)` | `SubscriptionResponse` | `CreateSubscriptionErrorBody` = `ErrorListResponse1` [422] \| `RawError` |
| `client.sites.read_site` | `GET /site.json` | `(*)` | `SiteResponse` | `RawError` (Case B) |
| `client.subscriptions.find_subscription` | `GET /subscriptions/lookup.json` | `(*, reference: str \| None = None)` | `SubscriptionResponse` | `FindSubscriptionErrorBody` = `RawError` [404, anything] |

No operation in scope returns `None`. No operation in scope has an idempotency-key parameter.

Models (members the code sets or reads; `Optional[T]` = `T | UnsetType`, never `None`):

| Model | Members | Source |
| --- | --- | --- |
| `ProductResponse` | `product: Product` (required) | `models/product_response.py` |
| `Product` | all `Optional`/`OptionalNullable` = `UNSET`: `id: int`, `name: str`, `handle: str\|None`, `description: str\|None`, `price_in_cents: int`, `interval: int`, `interval_unit: IntervalUnitOrStr`, `archived_at: datetime\|None`, `require_credit_card: bool`, `product_family: ProductFamily` | `models/product.py` |
| `CreateCustomerRequest` | `customer: CreateCustomer` (required) | `models/create_customer_request.py` |
| `CreateCustomer` | **required** `first_name: str`, `last_name: str`, `email: str`; `reference: Optional[str]` — **always set** (the claim's reference); "you may only create one customer for a given reference value" (docstring) | `models/create_customer.py`, `apis/customers.py` |
| `CustomerResponse` | `customer: Customer` (required) | `models/customer_response.py` |
| `Customer` | `id: Optional[int]`, `reference: OptionalNullable[str]`, `email`, `created_at: Optional[datetime]` — no status member | `models/customer.py` |
| `CreateSubscriptionRequest` | `subscription: CreateSubscription` (required) | `models/create_subscription_request.py` |
| `CreateSubscription` | `product_handle: Optional[str]` (required unless `product_id`), `customer_id: Optional[int]` (required unless `customer_reference`/`customer_attributes`), `reference: Optional[str]` — **always set** (the claim's reference) | `models/create_subscription.py` |
| `SubscriptionResponse` | `subscription: Optional[Subscription]` — **may decode as `UNSET`**: assert it | `models/subscription_response.py` |
| `Subscription` | all `UNSET`-able: `id: int`, `state: SubscriptionStateOrStr`, `product_price_in_cents: int`, `current_period_ends_at: datetime\|None`, `next_assessment_at: datetime\|None`, `created_at: datetime`, `activated_at: datetime\|None`, `reference: str\|None`, `product: Product`, `customer: Customer`, `currency: str` | `models/subscription.py` |
| `CreateSubscription.payment_collection_method` | `Optional[CollectionMethodOrStr]`; docstring: Relationship Invoicing sites take `remittance`/`automatic`/`prepaid`, legacy Statements sites `invoice`/`automatic`. `CollectionMethod` members: `AUTOMATIC`, `REMITTANCE`, `PREPAID`, `INVOICE` | `models/create_subscription.py`, `models/enums/collection_method.py` |
| `SiteResponse` / `Site` | `site: Site` (required); `relationship_invoicing_enabled: Optional[bool]`, `default_payment_collection_method: Optional[str]` | `models/site_response.py`, `models/site.py` |
| `ErrorListResponse1` | `errors: list[str]` (required) | `models/error_list_response1.py` |
| `CustomerErrorResponse1` | `errors: Optional[Errors1]` | `models/customer_error_response1.py` |

Members asserted after each call whose result is used: customer create/lookup → `customer.id` set and
`customer.reference == <sent reference>`; subscription create/lookup → `subscription` set,
`subscription.id` set, `subscription.state` set (else outcome `unknown`), `subscription.reference ==
<sent reference>`, `product_price_in_cents` equals the plan price (else `needs_review`).

`SubscriptionState` (`models/enums/subscription_state.py`, open: `SubscriptionStateOrStr`) → outcome
(`status_from_provider` for a subscribe):

| Member(s) | Outcome | Why |
| --- | --- | --- |
| `ACTIVE`, `TRIALING` | done | subscription in effect |
| `PENDING`, `ASSESSING`, `AWAITING_SIGNUP` | pending | provider not finished |
| `PAST_DUE`, `SOFT_FAILURE`, `UNPAID`, `PAUSED`, `ON_HOLD`, `SUSPENDED` | pending | exists but awaiting action / on hold — not "in effect with nothing outstanding" |
| `FAILED_TO_CREATE`, `CANCELED`, `EXPIRED`, `TRIAL_ENDED` | failed | never made, or made and undone |
| anything else (a string newer than the SDK), or `UNSET` | unknown | never done |

Error boundary (python-error-handling): one `ProviderError(status_code, message, outcome_unknown)`;
`ApiError` → 401/403 → 502, 429 → 503, typed 4xx → same 4xx with the provider's messages, other 4xx → same
4xx, 5xx → 502 (`outcome_unknown` on writes); `ValidationError`/`ValueError` on a 2xx → 502 unknown;
`ConnectError/ConnectTimeout/PoolTimeout/ProxyError` → 502 never sent; other `httpx.RequestError` → 504 unknown.
`OutcomeUnknown` → 504 `outcomeUnknown: true`; `AmountMismatch` → 409 `needs_review`.

## Design

- New Django app `sandbox/apps/subscriptions/` (label `subscriptions`): `models.py` (`BillingClaim`),
  `maxio.py` (settings → client), `safe_write.py` (claim store + the safe write), `services.py`
  (plans, ensure customer, subscribe, my-subscriptions), `views.py`, `urls.py`, `tests.py`, migrations.
- Oscar has no subscription/billing-account model; plans are **not** mirrored locally (Maxio is the
  system of record). The one local table, `BillingClaim`, is the write-claim ledger the safe write
  needs (the provider records *what exists*; this records *that we asked*). Identity reuses Oscar's
  `AUTH_USER_MODEL`.
- **References.** Claim key (local, unique, deterministic): `customer:<user.pk>` and
  `subscription:<user.pk>:<planHandle>:<sha256(Idempotency-Key)[:16] or "default">`. The reference sent to
  Maxio is generated **once, when the claim row is inserted** (`<MAXIO_REFERENCE_PREFIX>-<kind>-<16 hex>`),
  persisted before the call and reused on every attempt and every repeat — never regenerated. A
  per-claim random suffix (rather than the user pk) keeps two installs, or a re-created sandbox DB whose
  user pks restart at 1, from colliding on one Maxio site.
- Views are `non_atomic_requests`; each store step commits on its own.

## OPERATION OUTCOMES

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/subscriptions` → step 1 `customers.create_customer` (and its repeat) | none — `Customer` has no status member; outcome is read from the echoed `customer.reference` | reference echoed == sent and `id` set → done (step 2 runs); response absent/`id` UNSET/reference differs → unknown → `504 outcomeUnknown`; 4xx refusal with no customer under the reference → failed → the provider's 4xx (422 → 422 with its messages); claim held by an in-flight sender → sending → `202` | `services.read_customer` reads the echo, `services.customer_outcome` maps it inside `safe_write.safe_write` (called by `services.ensure_customer`); `views.answer` turns the claim into the response (`step: "customer"`), `views.provider_boundary` answers `OutcomeUnknown` / refusals |
| `POST /api/subscriptions` → step 2 `subscriptions.create_subscription` (and its repeat) | `subscription.state` (`SubscriptionState`) | per the state table above: done → `200`; pending (incl. `sending`) → `202`; failed → `409`; needs_review (price differs) → `409`; unknown / UNSET → `504 outcomeUnknown` | `services.subscription_outcome` (applied by `safe_write.safe_write` to `services.read_subscription`'s status; `services.subscribe`); `safe_write.safe_write` raises `AmountMismatch` for needs_review; `views.answer` + `views.OUTCOME_STATUS` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| step 1 create customer | `BillingClaim` row, key `customer:<user.pk>` (SQLite/any Django DB, outlives the process) | DB `UNIQUE(key)` on insert; re-claim of a released row is a single conditional `UPDATE … WHERE outcome='failed' AND provider_id=''`; second guard: Maxio refuses a second customer with the same `reference` | `IntegrityError` → `None` | `safe_write.try_claim` (insert under `models.BillingClaim.key` unique, conditional re-claim `UPDATE`), losers handled by `safe_write._take_claim` + `safe_write._answered_from_record`; key from `services.customer_key` |
| step 2 create subscription | `BillingClaim` row, key `subscription:<user.pk>:<plan>:<idem>` | DB `UNIQUE(key)`; same conditional re-claim | `IntegrityError` → `None` | `safe_write.try_claim`, `safe_write._take_claim`, `safe_write._answered_from_record`; key from `services.subscription_key` |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| step 1 create customer | same-reference resend (Maxio allows one customer per reference, so a resend cannot make a second one) and then lookup `customers.read_customer_by_reference` (404 → not found) | the claim's stored `reference` | `safe_write._send` with `resending=True` (`repeat_is_safe=True` in `services.ensure_customer`), then `safe_write._find` → `services.ensure_customer.find` (`read_customer_by_reference`, 404 → None via `services.none_if_not_found`) |
| step 2 create subscription | lookup `subscriptions.find_subscription(reference=…)` (404 → not found); never a resend (subscription reference uniqueness is not documented) | the claim's stored `reference` | `safe_write._find` → `services.subscribe.find` (`find_subscription`, 404 → None via `services.none_if_not_found`); `repeat_is_safe=False` in `services.subscribe` so `safe_write.safe_write` never resends |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| step 1 create customer | committed `BillingClaim` (`outcome=sending`, `reference`, `claimed_at`, user) | `outcome` (done/unknown/failed), `provider_id` = customer id, `provider_time` = `created_at`, snapshot | before: `safe_write.try_claim` (committed; views are `transaction.non_atomic_requests`, `views.create_subscription`); after: `safe_write.complete` with `services.read_customer`'s `Answer` |
| step 2 create subscription | committed `BillingClaim` (`outcome=sending`, `reference`, plan handle, user) | `outcome` from state map, `provider_id` = subscription id, `provider_time` = `activated_at`/`created_at`, snapshot (plan, price, state, next billing) | before: `safe_write.try_claim`; after: `safe_write.complete` with `services.read_subscription`'s `Answer` (snapshot from `services.subscription_json`) |

## Assumptions & Blockers

- No blockers. The claim store is the project's own Django DB (SQLite here), which enforces `UNIQUE`
  across processes.
- Minor: the Maxio `site` template variable is set from `MAXIO_SITE_SUBDOMAIN`; `MAXIO_BASE_URL`, when
  set, replaces the production base URL verbatim for the selected environment.
- Minor: price verification compares `product_price_in_cents` with the plan's `price_in_cents` (the
  product carries no currency member; the site currency is Maxio's).
- Found during live verification: with the default `automatic` collection method Maxio refuses the
  signup (`422 "No payment method was on file for the $299.00 balance"`) even though the plans do not
  require a card. This API captures no payment method, so subscriptions are created with
  `payment_collection_method` = `remittance` on a Relationship Invoicing site and `invoice` on a legacy
  one, read once per process from `sites.read_site().site.relationship_invoicing_enabled`
  (`services.collection_method`). The shopper is invoiced by Maxio.
- Minor: no cancel endpoint (not requested). A second subscription to the same plan requires a new
  `Idempotency-Key`.
- Environment note: this machine needs the Makefile's `oscar_import_catalogue` CSV step to get 209
  products (without it: 11 products and FK failures loading pages/orders fixtures).

## REQUIRED READING

- Client construction & lifetime — MUST load `python-client-initialization` (loaded)
- Credentials — MUST load `python-authentication` (loaded)
- First call, response modes, status → outcome — MUST load `python-calling-endpoints` (loaded)
- `UNSET`, open enums, dict companions — MUST load `python-models` (loaded)
- Error boundary — MUST load `python-error-handling` (loaded)
- Safe write, timeouts, no retries — MUST load `python-configuration-resilience` (loaded)
- Tests with a fake transport — MUST load `python-testing` (loaded)
