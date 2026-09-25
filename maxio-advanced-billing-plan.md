# Maxio Advanced Billing — subscription billing for the django-oscar sandbox

Plan and contract sheet. Every SDK fact below was read from the SDK map (`sdk-map.md`,
`map/operations/*.md` of `maxio-python-sdk@main`) or the installed package
(`venv/Lib/site-packages/maxio_advanced_billing`, distribution `maxio-advanced-billing` 1.0).

## Scope

| Capability | HTTP endpoint (sandbox, outside `i18n_patterns`) | Maxio operations |
| --- | --- | --- |
| Browse plans | `GET /api/subscription-plans` | `product_families.list_products_for_product_family` |
| Ensure billing customer (idempotent, separately invocable) | `POST /api/billing-customer` | `customers.create_customer`, `customers.read_customer_by_reference` |
| Subscribe | `POST /api/subscriptions` `{"planHandle": ...}` | (ensure customer) + `subscriptions.create_subscription`, `subscriptions.find_subscription` |
| Read one subscription (poll a pending one) | `GET /api/subscriptions/<subscriptionId>` | `subscriptions.read_subscription` |
| My subscriptions | `GET /api/my-subscriptions` | `customers.list_customer_subscriptions`, `subscriptions.find_subscription` (reconcile unsettled claims) |

Auth: Django session login (Oscar's `/<lang>/accounts/login/`); identity = `request.user`. Unauthenticated → `401` JSON.
CSRF stays on for the POSTs (`X-CSRFToken` header from the `csrftoken` cookie).

## Repo survey

- Host: Django 5.2 under WSGI → **sync** SDK client. No async views anywhere in the sandbox.
- Sandbox app convention: plain packages under `sandbox/apps/` imported as `apps.<name>` (exemplar: `sandbox/apps/sitemaps.py`, `sandbox/urls.py` imports `apps.sitemaps`). Non-i18n routes sit in the top `urlpatterns` list of `sandbox/urls.py` (exemplar: the `admin/` and `sitemap.xml` entries).
- Settings read through `django-environ` `env` in `sandbox/settings.py` (exemplar: `SECRET_KEY = env.str(...)`).
- `DATABASES.default.ATOMIC_REQUESTS = True` → the API views must be `transaction.non_atomic_requests` so a claim row commits **before** the provider call.
- Oscar models reused: the user model (`AUTH_USER_MODEL`) and `address.UserAddress` (default billing address → Maxio customer address) via `oscar.core.loading.get_model`.
- Toolchain: `py -3.11` venv at `venv/`, `pip install -e .[test]`; SDK installed from git (`maxio-advanced-billing @ git+https://github.com/context-plugins/maxio-python-sdk.git@main`). Its wheel did **not** pull `pydantic[email]`, `httpx` — installed explicitly. Tests of the new app: `cd sandbox && ../venv/Scripts/python manage.py test apps.subscriptions` (sandbox `TEST_RUNNER` = Django `DiscoverRunner`). No type checker configured → install `mypy` into the venv and run `mypy --strict` on the new app's modules.
- Baseline (untouched tree): `pytest tests/unit -n 8` → 16 passed, 79 errors (pre-existing). Sandbox bootstrap: `orders.json` fails with an FK error and only 11 products load — pre-existing fixture drift, irrelevant to billing.
- Smoke (read-only, real credential, scratchpad): `list_products_for_product_family("handle:eshop-subscribe")` → `basic-plan` 2900¢/1 month, `eshop-pro` 29900¢/1 month; `read_customer_by_reference` / `find_subscription` for an unknown reference → `ApiError` 404 `RawError` (empty body); `list_customer_subscriptions(unknown id)` → 404. No 403s — all in-scope operations are entitled.

## Contract sheet

### Client (sync)

- Class: `MaxioAdvancedBillingClient` from `maxio_advanced_billing` (sync; never mixed with `AsyncMaxioAdvancedBillingClient`). Constructor is keyword-only: `environment`, `timeout`, `server_config`, `custom_http_client`, `basic_auth`, `bearer_auth`.
- Held as a lazily-built module-level singleton (built after any fork, on first use), closed via `atexit` (`client.close()` also closes a supplied transport).
- `environment`: from `settings.MAXIO_ENVIRONMENT` lower-cased, explicit map `{"us","eu"}`; unknown → `ImproperlyConfigured` (never silently `"us"`). `maxio_api_gateway` is rejected (it needs a bearer connector token, not the site API key).
- `server_config` (several servers × several environments → nested per environment): `{"production": {env: {"site": MAXIO_SITE_SUBDOMAIN}}}`; when `MAXIO_BASE_URL` is set: `{"production": {env: {"base_url": MAXIO_BASE_URL}}}` verbatim. All in-scope operations are on server `production`. `ServerConfig` is frozen, `extra="forbid"`.
- Auth: `basic_auth=BasicAuthCredentials(username=<MAXIO_API_KEY>, password="x")` — the SDK README's `curl -u <api_key>:x`. Omitting it is silent no-auth → the factory refuses to build without a key.
- Transport: `custom_http_client=LoggingTransport(HttpxClient(timeout=MAXIO_TIMEOUT))` — logs method, URL path, status, ms; never headers/bodies. Logger `apps.subscriptions` is declared in the sandbox `LOGGING` (which sets `disable_existing_loggers: True`). The client's own `timeout=` does not reach the wire once a transport is supplied, so the timeout is set on `HttpxClient`.
- No retries in the SDK. Decision: reads (`list_products_for_product_family`, `read_*`, `find_subscription`, `list_customer_subscriptions`) retried once on never-sent transport errors / 429 / 502 / 503 / 504; **writes are never retried blind** — they go through the safe write.
- Every keyword-only parameter has a real default: no defensive `None`s.

### Operations

| Operation | Signature (positional \| after `*`) | Returns (parsed) | `ApiError.error` union | Members the code asserts on |
| --- | --- | --- | --- | --- |
| `client.product_families.list_products_for_product_family` | `product_family_id: str` \| `page=1, per_page=20, date_field, filter, start_date, end_date, start_datetime, end_datetime, include_archived, include, request_options` — id accepts `"handle:<handle>"` (docstring) | `list[ProductResponse]` (`.product: Product`, required) | `ListProductsForProductFamilyErrorBody` = `str` [404] \| `RawError` | `product.handle` (OptionalNullable[str]; skip entries without one), `product.name`, `product.price_in_cents`, `product.interval`, `product.interval_unit` (`IntervalUnitOrStr`), `product.archived_at`. Pass `per_page=200`, loop pages until a short page. |
| `client.customers.create_customer` | — \| `body: CreateCustomerRequest \| Dict`, `request_options` | `CustomerResponse` (`.customer: Customer`, required) | `CreateCustomerErrorBody` = `CustomerErrorResponse1` [422] \| `RawError` | `customer.id` (Optional[int]) — `UNSET` ⇒ outcome unknown |
| `client.customers.read_customer_by_reference` | `reference: str` \| `request_options` | `CustomerResponse` | `RawError` (Case B); not found = 404 | `customer.id`, `customer.reference` == ref |
| `client.subscriptions.create_subscription` | — \| `body: CreateSubscriptionRequest \| Dict`, `request_options` | `SubscriptionResponse` (`.subscription: Optional[Subscription]` — may be `UNSET`) | `CreateSubscriptionErrorBody` = `ErrorListResponse1` [422] (`errors: list[str]`, required) \| `RawError` | `subscription` set, `subscription.id`, `subscription.state` (status — see OPERATION OUTCOMES), `subscription.product.handle`, `product_price_in_cents`, `next_assessment_at`, `current_period_ends_at`, `created_at` |
| `client.subscriptions.find_subscription` | — \| `reference: str \| None`, `request_options` | `SubscriptionResponse` | `FindSubscriptionErrorBody` = `RawError` [404, anything] | as above; 404 ⇒ `None` (not found) |
| `client.subscriptions.read_subscription` | `subscription_id: int` \| `include`, `request_options` | `SubscriptionResponse` | `RawError` (Case B) | as above + `subscription.customer.id` for the ownership check |
| `client.customers.list_customer_subscriptions` | `customer_id: int` \| `request_options` | `list[SubscriptionResponse]` | `RawError` (Case B) | as above |

None of these return `None`; the parsed mode is used everywhere (no status code needed on success).

### Request models (members the task sets)

- `CreateCustomerRequest(customer: CreateCustomer)` — required `customer`.
  `CreateCustomer`: required `first_name: str`, `last_name: str`, `email: str`; set `reference: Optional[str]` (**always set** — the claim reference; Maxio requires customer references to be unique, `api-reference.md`), optional `address`, `address_2`, `city`, `state`, `zip`, `country`, `phone` (`Optional[str]`, omit — never `None`). No wire aliases on these members.
- `CreateSubscriptionRequest(subscription: CreateSubscription)` — required `subscription`.
  `CreateSubscription` (all `Optional[...] = UNSET`): set `product_handle: str`, `customer_id: int`, `reference: str` (**always set** — the claim reference), `payment_collection_method: CollectionMethodOrStr` = `CollectionMethod.REMITTANCE` by default (setting `MAXIO_PAYMENT_COLLECTION_METHOD`; enum `automatic, remittance, prepaid, invoice` — docstring: `remittance`/`automatic`/`prepaid` for Relationship Invoicing, `invoice`/`automatic` for legacy statements). Found during live verification: with the default (`automatic`) Maxio answers 422 "No payment method was on file for the $299.00 balance" although the plan does not require a card. No aliases. No `payment_profile_*`/`credit_card_attributes`.
- `Optional[T]` is `T | UnsetType` (not `typing.Optional`); read response members with `isinstance(x, UnsetType)` narrowing; `UNSET`/`UnsetType` from `maxio_advanced_billing.core`. `Optional[Any]` trap: none of the members set above are `Any`.
- `SubscriptionState` (`maxio_advanced_billing.models.enums`, open `SubscriptionStateOrStr`): `pending, failed_to_create, trialing, assessing, active, soft_failure, past_due, suspended, canceled, expired, paused, unpaid, trial_ended, on_hold, awaiting_signup`.
- Datetimes: `RFC3339DateTime` → `datetime.datetime`; money: integer cents → `Decimal(cents) / 100` formatted to 2 places for display (Maxio product carries no currency; none is claimed).

### Failures that are not `ApiError`

- Decode failure → `pydantic.ValidationError` / `ValueError` in both modes: on a write ⇒ outcome unknown (lookup); on a read ⇒ 502 "unreadable".
- Transport (`httpx`, unwrapped): `ConnectError, ConnectTimeout, PoolTimeout, ProxyError` ⇒ never sent (502, known); other `httpx.RequestError` ⇒ may have landed (504, unknown).
- 401/403 ⇒ our credentials (502), 429 ⇒ 503, other 4xx ⇒ caller-visible rejection (422/404 passthrough with our message), 5xx ⇒ 502.

## OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| `subscriptions.create_subscription` (and every read that refreshes a stored subscription: `find_subscription`, `read_subscription`, `list_customer_subscriptions`) | `SubscriptionResponse.subscription.state` (`SubscriptionStateOrStr`) | **done** (201, or 200 on replay): `active`, `trialing`. **pending** (202, stored `pending`, shown with the raw state, re-read on every poll): `pending`, `assessing`, `awaiting_signup` (provider not finished); `past_due`, `soft_failure`, `unpaid`, `on_hold`, `paused`, `suspended` (exists but something outstanding — not done, surfaced). **failed** (422 on subscribe; stored `failed` with provider id — claim kept, a new subscribe to the plan gets a new reference): `failed_to_create`; `canceled`, `expired`, `trial_ended` (done then undone). **unknown** (202, neither done nor failed): any value not listed, `UNSET` state, or an `UNSET` subscription/id (the latter also triggers the lookup by reference). | `services.status_from_provider` (the one mapping), applied by `services._subscription_answer` (create/lookup → `safe_write.complete`), `services._refresh_record` (reads), `services.subscription_dict`; HTTP status chosen in `views.subscriptions` |
| `customers.create_customer` | none — `CustomerResponse` carries no status; the outcome is the presence of `customer.id` (`UNSET` ⇒ unknown) | n/a (no status field) | `services._customer_answer` (id present ⇒ done, `UNSET` ⇒ unknown); `safe_write.safe_write` turns a missing id into a lookup / `OutcomeUnknown` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| `customers.create_customer` | `BillingCustomer` row in the Django DB (committed before the call), `reference = "<MAXIO_REFERENCE_PREFIX>:customer:<user.pk>"` | DB `UNIQUE(reference)` + `UNIQUE(user)` → `IntegrityError`; at the provider, Maxio's unique customer reference (422) | `try_claim` catches `IntegrityError` → `load_existing` (in flight → "in progress", no call; stale/unknown → check) | `models.BillingCustomer` (unique `reference`, unique `user`); `safe_write.try_claim` / `safe_write.take_over_check`, called from `services.ensure_customer` |
| `subscriptions.create_subscription` | `BillingSubscription` row in the Django DB (committed before the call), `reference = "<prefix>:subscription:<user.pk>:<planHandle>:<generation>"` (generation = number of this user's ended (`failed` with a provider id) subscriptions to that plan) | DB `UNIQUE(reference)` → `IntegrityError` | `try_claim` catches `IntegrityError` → `load_existing` → answer from the stored outcome through the same done gate | `models.BillingSubscription` (unique `reference`); reference built in `services.subscribe`; `safe_write.try_claim` / `safe_write.take_over_check`; replay answered in `services.subscribe` + `views.subscriptions` |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| `customers.create_customer` | same-reference resend (Maxio refuses a second customer with the same reference); a 422 on the resend or first send → lookup `customers.read_customer_by_reference(ref)` | `<prefix>:customer:<user.pk>` | `safe_write.safe_write` (`repeat_is_safe=True`, `may_be_duplicate` = 422) with `find` = `customers.read_customer_by_reference` via `services._find_or_none`, wired in `services.ensure_customer` |
| `subscriptions.create_subscription` | lookup `subscriptions.find_subscription(reference=ref)` (404 ⇒ not found yet ⇒ stays `unknown`; never a fresh create; subscription-reference uniqueness at Maxio is not documented, so no resend) | `<prefix>:subscription:<user.pk>:<planHandle>:<generation>` | `safe_write.safe_write` (`repeat_is_safe=False`) with `find` = `subscriptions.find_subscription(reference=…)` via `services._find_or_none`, wired in `services.subscribe`; later settled by `services._settle_unresolved` (from `services.my_subscriptions`) |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| `customers.create_customer` | `BillingCustomer(user, reference, outcome="sending", claimed_at)` committed | `outcome` (`done` / `failed` / `unknown`), `maxio_customer_id`, `provider_time` (`customer.created_at`) | before: `safe_write.try_claim` (autocommitted; views are `transaction.non_atomic_requests` via `views.api_view`); after: `safe_write.complete` |
| `subscriptions.create_subscription` | `BillingSubscription(user, plan_handle, reference, outcome="sending", claimed_at)` committed | `outcome` (`done`/`pending`/`failed`/`unknown`), `maxio_subscription_id`, `provider_state`, `provider_time` (`subscription.created_at`) | before: `safe_write.try_claim` (autocommitted; `views.api_view` non-atomic); after: `safe_write.complete`, refreshed by `services._refresh_record` |

## Assumptions & Blockers

- No blockers. The Django DB (SQLite here, any Django backend in production) holds claims across processes via unique constraints.
- `MAXIO_REFERENCE_PREFIX` (optional) namespaces references per install; when empty, a random id is created once per database (`models.BillingInstall`, `maxio.reference_prefix`) — not derived from `SECRET_KEY`, because the sandbox's default key is committed and shared by every clone.
- A subscription create that stays `unknown` after lookups is left for an operator (Django admin shows both claim tables); it is never auto-failed and never re-sent under a new reference.
- Minor: `MAXIO_ENVIRONMENT` is read too (value `US` in this environment); it is not in the mandated list but selects the host region, which must not default silently.

## REQUIRED READING

- MUST load `python-client-initialization` — sync client, lifetime, custom transport. (loaded before implementation)
- MUST load `python-authentication` — basic auth with the site API key; silent no-auth. (loaded before implementation)
- MUST load `python-calling-endpoints` — status → outcome gate, positional/keyword split. (loaded before implementation)
- MUST load `python-models` — `UNSET` narrowing, open enums. (loaded before implementation)
- MUST load `python-error-handling` — the ladder, transport split, decode failures. (loaded before implementation)
- MUST load `python-configuration-resilience` — the safe write, server_config, no retries. (loaded before implementation)
- MUST load `python-testing` — stub transport for the app's tests. (loaded before implementation)

## Verification record (2026-09-25)

- `manage.py test apps.subscriptions` → 27 tests OK (stub transport at the SDK seam).
- `mypy --strict` on `maxio.py, errors.py, safe_write.py, services.py, views.py` → no issues (mypy installed into the venv; Django is untyped, so the test module is not held to `--strict`).
- Live against site `cp-exp-1` via `runserver 127.0.0.1:38500`, shopper logged in through Oscar's login form: two concurrent `POST /api/subscriptions {eshop-pro}` → one 201 (subscription 94519080, `active`, $299.00, next billing 2026-10-25) and one 202 (claim in flight, no provider call); a repeat → 200 with the same id; `POST {basic-plan}` → 201 (94519087, $29.00); `GET /api/my-subscriptions` → exactly those two; one Maxio customer (99126119).
- Retries: reads retried once (never-sent transport error, 429/502/503/504) in `services._read`; writes never resent blind.
