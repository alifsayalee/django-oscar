# Maxio Advanced Billing — subscription billing for the django-oscar sandbox

## Scope

Additive, parallel capability on the sandbox site (`sandbox/`): a shopper browses Maxio plans,
subscribes to one, and sees their subscriptions. Maxio is the system of record for customers and
subscriptions; locally we record only *that we asked* (the write claims), not a copy of Maxio's data.

Endpoints (new Django app `sandbox/apps/subscriptions/`, routed from `sandbox/urls.py` under `/api/`):

| Route | Auth | What |
| --- | --- | --- |
| `GET /api/session` | none | who am I + sets the CSRF cookie |
| `POST /api/session` | none | Django session login (`django.contrib.auth.authenticate` + `login`) |
| `DELETE /api/session` | session | logout |
| `GET /api/subscription-plans` | none | plans of `MAXIO_DEFAULT_PRODUCT_FAMILY`, each with `planHandle` |
| `POST /api/subscriptions` | session + CSRF | ensure Maxio customer (safe write #1), create subscription (safe write #2); returns top-level `subscriptionId` |
| `GET /api/my-subscriptions` | session | the caller's subscriptions, read live from Maxio, plus unsettled local claims |
| `GET /api/subscriptions/<id>` | session | one of the caller's subscriptions, read live (ownership checked) |

Identity is always `request.user` (Oscar's user model via `get_user_model()`); no user id is ever
taken from the request body.

## Repo survey

- Host is **Django under WSGI, fully sync** → **sync client** (`MaxioAdvancedBillingClient`).
- Sandbox apps live in `sandbox/apps/` and are imported as `apps.<name>` (exemplar: `sandbox/urls.py`
  imports `apps.sitemaps`). Non-Oscar routes sit in the plain `urlpatterns` list of `sandbox/urls.py`
  (exemplar: the `admin/`, `sitemap.xml` entries), outside `i18n_patterns`.
- Settings read env via `django-environ` `env.str/env.bool/...` (exemplar: `sandbox/settings.py`
  `DEBUG`, `SECRET_KEY`).
- `DATABASES['default']['ATOMIC_REQUESTS'] = True` → every view is one transaction. The claim must be
  committed **before** the provider call and survive an exception, so the API views are
  `@transaction.non_atomic_requests` and the claim store uses its own short `transaction.atomic()`
  blocks.
- Store: SQLite by default (PostgreSQL optional via `DATABASE_ENGINE`). A `UNIQUE` column on a table
  is the claim; both engines enforce it across processes. No new store or dependency.
- Toolchain: `venv` (py 3.11) + `pip install -e .[test]`; tests with `pytest` + `pytest-django`;
  no type checker configured → `mypy --strict` on the new files (installed into venv with
  `django-stubs`).
- SDK install: `pip install "maxio-advanced-billing @ git+https://github.com/context-plugins/maxio-python-sdk.git@main"`.
  **Its built metadata declares no `Requires`**, so `httpx`, `pydantic[email]`, `typing-extensions`
  must be installed explicitly → recorded in `sandbox/requirements-maxio.txt`.

## Configuration (all in `sandbox/settings.py`, values only from env)

| Setting | Env var | Default | Use |
| --- | --- | --- | --- |
| `MAXIO_API_KEY` | `MAXIO_API_KEY` | `''` | Basic-auth username |
| `MAXIO_SITE_SUBDOMAIN` | `MAXIO_SITE_SUBDOMAIN` | `''` | `server_config.production.<env>.site` |
| `MAXIO_ENVIRONMENT` | `MAXIO_ENVIRONMENT` | `'us'` | explicit map `{'us','eu'}` (case-insensitive); anything else → `ImproperlyConfigured` |
| `MAXIO_DEFAULT_PRODUCT_FAMILY` | same | `''` | family **handle**; queried as `handle:<value>` |
| `MAXIO_BASE_URL` | same | `None` | when set, used verbatim as `server_config.production.<env>.base_url` |
| `MAXIO_TIMEOUT` | same | `15.0` | seconds, set on our own `HttpxClient` transport |
| `MAXIO_PAYMENT_COLLECTION_METHOD` | same | `'remittance'` | validated against `CollectionMethod`; no card capture in this app |
| `MAXIO_REFERENCE_PREFIX` | same | `'oscar-sandbox'` | install-unique prefix for every reference we send |

Missing key/subdomain/family → the billing endpoints answer `503 billing_not_configured`; the rest of
the site is unaffected.

## Contract sheet (every row from the SDK map / installed source)

### Client

- **Sync** `MaxioAdvancedBillingClient` (import root `maxio_advanced_billing`). Never mixed with async.
- Constructor keyword-only: `environment=` (`Literal["us","eu","maxio_api_gateway"]`, **omitted = "us"
  silently** → always passed explicitly), `timeout=`, `server_config=`, `custom_http_client=`,
  `basic_auth=`, `bearer_auth=`. [sdk-map.md "Getting a client"]
- `server_config` nesting (several servers × several environments): `{"production": {"us": {"site": …,
  "base_url": …}}}`; `ProductionUsConfig` fields `base_url` (default `https://{site}.chargify.com`),
  `site`; `extra="forbid"`. [server/server_config.py]
- Auth: `basic_auth=BasicAuthCredentials(username=MAXIO_API_KEY, password="")` — `username`,
  `password` both required `str`; `:` in username rejected. Omitting it sends unauthenticated requests
  silently → we refuse to build a client without a key. Verified against the real site by the
  read-only smoke (200 with this credential). [core/auth/schemes/basic_auth.py]
- Transport: `custom_http_client=LoggingTransport(HttpxClient(timeout=MAXIO_TIMEOUT))`; with a custom
  transport the client `timeout=` does not reach the wire, so the timeout is set on `HttpxClient`.
  `HttpxClient(*, timeout, proxy_url, verify)`. [core/httpx_transport.py]
- Lifetime: one process-wide client, built lazily on first use (post-fork safe), guarded by a lock for
  construction only; `close()` at `atexit`.
- **No retries in the SDK.** We add bounded retries for idempotent **reads only** (connect-phase
  failures, 429/502/503/504; 3 attempts, short backoff). Writes are never blindly retried — the safe
  write's check path handles them.
- Per-call overrides: `request_options={"timeout": float, "extra_headers": {...}}`; `extra_headers`
  wins over the endpoint's own headers. [core/request_options.py]
- Keyword-only params all have real defaults — no defensive `None`s.

### Operations in scope

| Operation | Signature (positional \| keyword-only) | Returns (parsed) | `ApiError.error` union | Members asserted |
| --- | --- | --- | --- | --- |
| `product_families.list_products_for_product_family` (GET `/product_families/{product_family_id}/products.json`) | `(product_family_id: str, *, page=1, per_page=20, …, include_archived=None, request_options=None)`; id is "the family's id or its handle prefixed with `handle:`"; `per_page` max 200 | `list[ProductResponse]` | `ListProductsForProductFamilyErrorBody` = `str` [404] \| `RawError` | `ProductResponse.product` (required); `Product.handle` (`OptionalNullable[str]`) — a product without a handle is skipped |
| `customers.create_customer` (POST `/customers.json`) | `(*, body: CreateCustomerRequest\|Dict, request_options)` | `CustomerResponse` | `CreateCustomerErrorBody` = `CustomerErrorResponse1` [422] \| `RawError` | `CustomerResponse.customer` (required), `Customer.id` (`Optional[int]`, must not be UNSET) |
| `customers.read_customer_by_reference` (GET `/customers/lookup.json`) | `(reference: str, *, request_options)` | `CustomerResponse` | `RawError` (Case B); a miss is **404 with empty body** (smoke-verified) → `None` | `customer.id` |
| `customers.list_customer_subscriptions` (GET `/customers/{customer_id}/subscriptions.json`) | `(customer_id: int, *, request_options)` | `list[SubscriptionResponse]` | `RawError` (Case B) | `SubscriptionResponse.subscription` is **`Optional[Subscription]`** → skip UNSET |
| `subscriptions.create_subscription` (POST `/subscriptions.json`) | `(*, body: CreateSubscriptionRequest\|Dict, request_options)`; the SDK sets header `Idempotency-Key: uuid4()` per call → we override it via `extra_headers` with `uuid5(ref)` so every attempt carries the same key (enforcement undocumented — not relied on) | `SubscriptionResponse` | `CreateSubscriptionErrorBody` = `ErrorListResponse1` [422] (`errors: list[str]`, required) \| `RawError` | `subscription` (Optional → UNSET = unknown), `id`, `state`, `reference` |
| `subscriptions.find_subscription` (GET `/subscriptions/lookup.json`) | `(*, reference: str \| None = None, request_options)` | `SubscriptionResponse` | `FindSubscriptionErrorBody` = `RawError` [404, anything]; miss = 404 empty body (smoke-verified) → `None` | as above |
| `subscriptions.read_subscription` (GET `/subscriptions/{subscription_id}.json`) | `(subscription_id: int, *, include=None, request_options)` | `SubscriptionResponse` | `RawError` (Case B) | as above; ownership: `subscription.customer.id` must equal the caller's customer id |

None of these returns `None` (none of the 29 `-> None` operations is in scope).

### Request models (members we set)

- `CreateCustomerRequest(customer: CreateCustomer)` (required).
  `CreateCustomer`: `first_name: str`, `last_name: str`, `email: str` **required**; `reference:
  Optional[str] = UNSET` — **always set** to our customer reference. The docstring: "you may only create
  one customer for a given reference value" → the provider rejects a second create for the same
  reference. [models/create_customer.py, apis/customers.py]
- `CreateSubscriptionRequest(subscription: CreateSubscription)` (required).
  `CreateSubscription`: `product_handle: Optional[str]` (set), `customer_id: Optional[int]` (set, from
  write #1), `reference: Optional[str]` — **always set** to our subscription reference ("the reference
  value (provided by your app) for the subscription itself"; uniqueness **not** documented → a
  same-reference resend is **not** safe; the check is a lookup via `find_subscription`).
  `payment_collection_method: Optional[CollectionMethodOrStr]` — set from
  `MAXIO_PAYMENT_COLLECTION_METHOD` (default `remittance`; enum `AUTOMATIC="automatic"`,
  `REMITTANCE="remittance"`, `PREPAID="prepaid"`, `INVOICE="invoice"`). Live-verified: leaving it to the
  default made Maxio answer 422 "No payment method was on file for the $299.00 balance"; with
  `remittance` "the invoice will go into 'open' status and payment won't be attempted" (api-reference),
  and the subscription comes back `active`. No wire aliases on any member we set (none carry `alias=`).
- `Optional[T]` here is `T | UnsetType`, never `None`.

### Response models (members we read)

- `Product`: `id`, `name`, `description` (`OptionalNullable[str]`), `handle` (`OptionalNullable[str]`),
  `price_in_cents: Optional[int]`, `interval: Optional[int]`, `interval_unit: Optional[IntervalUnitOrStr]`
  (`DAY="day"`, `MONTH="month"`), `archived_at: OptionalNullable[datetime]` (archived → not offered),
  `require_credit_card: Optional[bool]`, `initial_charge_in_cents`, `trial_price_in_cents`.
- `Subscription`: `id: Optional[int]`, `state: Optional[SubscriptionStateOrStr]`, `reference:
  OptionalNullable[str]`, `product: Optional[Product]`, `customer: Optional[Customer]`,
  `product_price_in_cents: Optional[int]`, `currency: Optional[str]`, `next_assessment_at:
  OptionalNullable[datetime]` (next billing date), `current_period_ends_at`, `activated_at`,
  `created_at: Optional[datetime]` (provider time), `canceled_at`.
- `Customer`: `id: Optional[int]`, `reference: OptionalNullable[str]`, `email`, `first_name`, `last_name`.
- Every UNSET is resolved to `None`/absent in our own serializer before it leaves the app.

### Status enum — `SubscriptionState` (`models/enums/subscription_state.py`), mapped by `status_from_provider`

| Member (wire) | Outcome | Why (from the enum docstring) |
| --- | --- | --- |
| `ACTIVE` (`active`) | **done** | live, paid, up to date |
| `TRIALING` (`trialing`) | **done** | live, valid trial |
| `PENDING` (`pending`) | pending | transient, in creation |
| `ASSESSING` (`assessing`) | pending | transient, mid-assessment |
| `AWAITING_SIGNUP` (`awaiting_signup`) | pending | not signed up yet |
| `SOFT_FAILURE` (`soft_failure`) | pending | processing failed, retried automatically |
| `PAST_DUE` (`past_due`) | pending | exists, payment overdue — needs attention |
| `UNPAID` (`unpaid`) | pending | exists, unpaid — needs attention |
| `PAUSED` (`paused`) | pending | account in arrears — needs attention |
| `ON_HOLD` (`on_hold`) | pending | billing stopped temporarily, expected to resume |
| `SUSPENDED` (`suspended`) | pending | prepaid balance exhausted, returns when topped up |
| `FAILED_TO_CREATE` (`failed_to_create`) | **failed** | signup failed |
| `CANCELED` (`canceled`) | **failed** | done then undone |
| `EXPIRED` (`expired`) | **failed** | ended |
| `TRIAL_ENDED` (`trial_ended`) | **failed** | ended without card |
| anything else / UNSET | **unknown** | not-yet, never done |

### Error boundary (one ladder, `billing/errors.py` → JSON)

| Failure | Our status | `outcomeUnknown` |
| --- | --- | --- |
| `ApiError` 400/404/409/422 with a typed body on a *write* | same status, provider messages | false |
| `ApiError` 401/403 | 502 (our credentials) | false |
| `ApiError` 429 | 503 | false |
| `ApiError` 5xx / unmapped | 502 | write: resolved by the safe write's lookup |
| `httpx.ConnectError/ConnectTimeout/PoolTimeout/ProxyError` | 502 | false — never sent |
| other `httpx.RequestError` | 504 | true on writes |
| `pydantic.ValidationError`/`ValueError` decoding a 2xx | 502 (read) / unknown (write) | true on writes |
| `OutcomeUnknown` from the safe write | 504 | true |

## OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| `subscriptions.create_subscription` | `SubscriptionResponse.subscription.state` (`SubscriptionStateOrStr`, open enum) | done: `active`, `trialing` → claim `done`, `201`. pending: `pending`, `assessing`, `awaiting_signup`, `soft_failure`, `past_due`, `unpaid`, `paused`, `on_hold`, `suspended` → claim `pending`, `202` with state. failed: `failed_to_create`, `canceled`, `expired`, `trial_ended` → claim `failed` (keeps provider id), `409 subscription_not_active`. `subscription` UNSET / `state` UNSET / unlisted string → `unknown` → claim `unknown`, `202`, never success | `outcomes.status_from_provider` (the mapping), applied in `services._subscription_answer` → `read` and recorded by `safe_write.safe_write` step 5 (`complete`); `subscription` UNSET → `Answer(provider_id=None)` → `safe_write.safe_write` raises `OutcomeUnknown`; HTTP status chosen in `views.subscriptions` via `views.SUBSCRIBE_STATUS`; re-read and kept current by `services.refresh` / `services._sync_outcomes` |
| `customers.create_customer` | none — `CustomerResponse` carries no status; `customer.id` present = the customer exists (a customer has no in-progress state); id UNSET → `unknown` (outcome unreadable) → `504 outcome_unknown` | `services._customer_answer` (id → `Answer`), `safe_write.safe_write` (`provider_id is None` → `complete(UNKNOWN)` + `OutcomeUnknown`), `services.ensure_customer` (anything but `done` with an id → `BillingError customer_not_ready`) |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| `customers.create_customer` (ref `<prefix>-cust-u<user.pk>`) | DB table `subscriptions_maxiowrite`, row keyed by `reference` | `UNIQUE` constraint on `reference` → `IntegrityError` on insert; also Maxio refuses a second customer with the same reference (422) | `try_claim` catches `IntegrityError` → returns False → loser answers from the stored row | `models.MaxioWrite.reference` (`unique=True`), `safe_write.try_claim` (catches `IntegrityError`), `safe_write.safe_write` step 1 (`InProgress` / `load_existing`), reference from `services.customer_reference`, called by `services.ensure_customer` |
| `subscriptions.create_subscription` (ref `<prefix>-sub-u<user.pk>-<planHandle>-g<generation>`) | same table, same `UNIQUE reference` column | `UNIQUE` constraint on `reference` → `IntegrityError` | same `try_claim` | `models.MaxioWrite.reference`, `safe_write.try_claim`, `safe_write.safe_write` step 1; reference from `services.subscription_reference` + `services.next_generation`, called by `services.subscribe` |

Generation: `1 + number of this user's claims for this plan whose outcome is failed *with* a provider id`
(a subscription that ended). Two racing requests compute the same generation → the same reference →
the unique constraint picks one winner.

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| `customers.create_customer` | same-reference resend is safe (provider refuses a duplicate reference) → the check resends; a 422 is then settled by `read_customer_by_reference` (found = landed) | `<prefix>-cust-u<pk>` (same as the claim) | `safe_write.safe_write` (`resending` path, since `services.ensure_customer` passes `repeat_is_safe=True`), duplicate detection in `services.ensure_customer.is_duplicate` → `services._find_customer`, lookup step 3 via `WriteSpec.find` = `services._find_customer` |
| `subscriptions.create_subscription` | lookup `find_subscription(reference=ref)` (404 → not found → stays `unknown`) | `<prefix>-sub-u<pk>-<plan>-g<n>` (same as the claim) | `safe_write.safe_write` step 3 via `WriteSpec.find` = `services._find_subscription` (`repeat_is_safe=False` in `services.subscribe`); later settlement in `services.reconcile_unsettled` (called by `services.my_subscriptions`) and by a repeat POST (stale/unknown claim → check only) |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| `customers.create_customer` | `MaxioWrite(reference=cust ref, kind=customer, user, outcome=sending, claimed_at)` committed | `outcome` (done/unknown/failed), `provider_id` = Maxio customer id, `provider_time` | before: `safe_write.try_claim` (own `transaction.atomic()`, views are `transaction.non_atomic_requests` via `views.api_view`); after: `safe_write.complete` |
| `subscriptions.create_subscription` | `MaxioWrite(reference=sub ref, kind=subscription, user, plan_handle, outcome=sending, claimed_at)` committed | `outcome` from `status_from_provider(state)`, `provider_id` = subscription id, `provider_state`, `provider_time` = `created_at` | before: `safe_write.try_claim`; after: `safe_write.complete` (fed by `services._subscription_answer`) |

Before sending the first attempt of a subscription create, the winner also looks the reference up
(`find_subscription`) — not as the duplicate guard (the claim is), but so a record left by an earlier
database/install with the same prefix is adopted instead of duplicated.

## Assumptions & Blockers

- (minor) Basic-auth credential is `username = MAXIO_API_KEY`, `password = ""`. The plugin documents
  only `BasicAuthCredentials(username, password)`; the read-only smoke against the real site returned
  200 with this credential, so it is verified, not assumed.
- (minor) `Idempotency-Key` enforcement by Maxio is undocumented in the plugin; we send a stable key
  but do not rely on it.
- (minor) `MAXIO_REFERENCE_PREFIX` defaults to `oscar-sandbox`; a production install sets its own.
- (minor) No cancel/upgrade flows are in scope (the task names subscribe + list).
- No blockers.

## REQUIRED READING

- Client construction/lifetime → MUST load `python-client-initialization` (loaded)
- Credentials → MUST load `python-authentication` (loaded)
- Calls, status mapping → MUST load `python-calling-endpoints` (loaded)
- UNSET / open enums / serialization → MUST load `python-models` (loaded)
- Error ladder → MUST load `python-error-handling` (loaded)
- Safe write, timeouts, logging transport → MUST load `python-configuration-resilience` (loaded)
- Tests with a stub transport → MUST load `python-testing` (loaded)
