# Maxio Advanced Billing — subscription billing for the django-oscar sandbox

Scope: the **Subscribe** hero flow as three separately invocable HTTP endpoints on the sandbox site
(`sandbox/`), with Maxio Advanced Billing as the billing system of record.

| Endpoint | Auth | Does |
| --- | --- | --- |
| `GET /api/subscription-plans` | none (public catalogue) | lists the non-archived products of the configured product family; each entry carries `planHandle` |
| `POST /api/subscriptions` `{"planHandle": "..."}` | Django session + CSRF | ensures the user's Maxio customer (idempotent), subscribes it to the plan (idempotent); top-level `subscriptionId` |
| `GET /api/my-subscriptions` | Django session | the user's subscriptions read back from Maxio, plus any locally unsettled attempts |

## Repo survey (read-only)

| Convention | Pattern | Exemplar to imitate |
| --- | --- | --- |
| Sandbox apps | plain Django packages under `sandbox/apps/`, imported as `apps.<name>` (sandbox dir is on `sys.path`) | `sandbox/apps/sitemaps.py`, `sandbox/apps/user/models.py` |
| URL wiring | `sandbox/urls.py`; non-i18n routes (admin, sitemap) sit in the plain `urlpatterns` list, Oscar under `i18n_patterns` | `sandbox/urls.py` |
| Settings | `django-environ` `env.str/env.bool/...` in `sandbox/settings.py` | `sandbox/settings.py` (`SECRET_KEY`, `DEBUG`) |
| Oscar models | `oscar.core.loading.get_model`, users via `oscar.core.compat.AUTH_USER_MODEL` | `sandbox/apps/user/models.py`, `sandbox/apps/sitemaps.py` |
| Auth | Django session login (`oscar.apps.customer.auth_backends.EmailBackend`), `/en-gb/accounts/login/` | `sandbox/settings.py` `AUTHENTICATION_BACKENDS` |
| Transactions | `ATOMIC_REQUESTS = True` — **a claim written inside the request transaction is invisible to a racing request until commit**; the API views must opt out (`transaction.non_atomic_requests`) and commit each claim on its own | `sandbox/settings.py` `DATABASES` |
| Sync vs async | **WSGI, sync** (`sandbox/wsgi.py`, plain Django views) → the **sync** client | `sandbox/wsgi.py` |
| Tests | Django `DiscoverRunner` (`TEST_RUNNER` in sandbox settings); Oscar's own suite is pytest under `tests/` with its own settings | `sandbox/settings.py` |
| Lint | flake8 max-line-length 119, isort | `setup.cfg` |

Toolchain: `pip` + venv (`venv/`, Python 3.11). `maxio-advanced-billing` was not a dependency; installed from
`git+https://github.com/context-plugins/maxio-python-sdk.git@main` (commit `3d84ecef`). **Its wheel metadata drops the
three dependencies its `pyproject.toml` declares** (`httpx>=0.28.1,<1`, `pydantic[email]>=2.11,<3`,
`typing-extensions>=4.13,<5`) — installed explicitly and recorded in `sandbox/requirements-maxio.txt`.
Checks: `sandbox/manage.py check` (baseline: 1 pre-existing warning `templates.W003`),
`sandbox/manage.py test apps.subscriptions`, `mypy --strict` on the new app (config kept outside the repo).

Credentials: `MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`, `MAXIO_ENVIRONMENT` (`US`), `MAXIO_DEFAULT_PRODUCT_FAMILY` present
in the environment. Read-only smoke (scratchpad, real key): `list_products_for_product_family("handle:eshop-subscribe")`
→ `basic-plan` 2900/month, `eshop-pro` 29900/month (IDs already differ from the task table — handles only);
`read_customer_by_reference` and `find_subscription` for a missing reference → `ApiError` 404 `RawError`;
`list_subscriptions` OK. The site is shared with other installs (foreign references present) → references carry an
install-unique prefix.

## Contract sheet

**Client** — sync `MaxioAdvancedBillingClient` (root `maxio_advanced_billing`); never the async client (WSGI app).
Held as a lazily-built per-process singleton (built after fork, on first use), closed via `atexit`; never per request.
Constructor is keyword-only: `environment=`, `timeout=`, `server_config=`, `custom_http_client=`, `basic_auth=`.
- `environment`: omitted = `"us"` silently → always passed, from `MAXIO_ENVIRONMENT` through an explicit map
  `{"us": "us", "eu": "eu"}`; any other value → `ImproperlyConfigured` (gateway env needs bearer auth; not in scope).
- `server_config`: `{"production": {<env>: {"site": MAXIO_SITE_SUBDOMAIN}}}`; when `MAXIO_BASE_URL` is set,
  `{"production": {<env>: {"base_url": MAXIO_BASE_URL, "site": ...}}}` — used verbatim (the `{site}` template only
  substitutes if the override contains it). `ServerConfig` is `extra="forbid"` — wrong nesting raises.
- Auth: `basic_auth=BasicAuthCredentials(username=MAXIO_API_KEY, password="x")` (SDK README: `curl -u <api_key>:x`).
  Omitting it = unauthenticated silently → a blank key raises `ImproperlyConfigured` before construction.
- `timeout`: 10.0 s (default 30 is too long on a request path), passed to our own `HttpxClient(timeout=...)` wrapped in a
  logging transport (`custom_http_client=`) — the client's `timeout=` does not reach a supplied transport.
- **No retries in the SDK**; we add none for writes (deliberate: the safe write's lookup replaces them) and none for
  reads (a failed read is reported, caller may retry).
- Every keyword-only parameter has a real default — no defensive `None`s.

| Operation | Signature (positional \| keyword-only) | Returns (parsed) | `ApiError.error` union | Members asserted on |
| --- | --- | --- | --- | --- |
| `product_families.list_products_for_product_family` | `(product_family_id: str, *, page=1, per_page=20, …, include_archived=None, request_options=None)`; id = `"handle:" + family` | `list[ProductResponse]` (`.product: Product`) | `ListProductsForProductFamilyErrorBody` = `str` [404] \| `RawError` | `product.handle` (skip entries without), `name`, `price_in_cents`, `interval`, `interval_unit`, `description`, `archived_at` |
| `customers.create_customer` | `(*, body: CreateCustomerRequest \| Dict = None, request_options=None)` | `CustomerResponse` (`.customer: Customer`) | `CreateCustomerErrorBody` = `CustomerErrorResponse1` [422] \| `RawError` | `customer.id` (UNSET → unreadable → unknown), `customer.reference` echo, `customer.created_at` |
| `customers.read_customer_by_reference` | `(reference: str, *, request_options=None)` | `CustomerResponse` | Case B `RawError` (404 = not found → `None`) | as above |
| `subscriptions.create_subscription` | `(*, body: CreateSubscriptionRequest \| Dict = None, request_options=None)` | `SubscriptionResponse` (`.subscription: Subscription`) | `CreateSubscriptionErrorBody` = `ErrorListResponse1` [422] \| `RawError` | `subscription.id`, `.state`, `.product.handle` echo, `.created_at`, `.next_assessment_at`, `.current_period_ends_at`, `.product_price_in_cents`, `.currency`, `.reference` |
| `subscriptions.find_subscription` | `(*, reference: str \| None = None, request_options=None)` | `SubscriptionResponse` | `FindSubscriptionErrorBody` = `RawError` [404, other] (404 → `None`) | as above |
| `sites.read_site` *(added after the live run)* | `(*, request_options=None)`; read once per client | `SiteResponse` (`.site: Site`, required) | Case B `RawError` | `site.relationship_invoicing_enabled` (UNSET → 502 unreadable), `site.currency`, `site.test` (live site → warning) |
| `customers.list_customer_subscriptions` | `(customer_id: int, *, request_options=None)` | `list[SubscriptionResponse]` | Case B `RawError` | as above |

Models (source `maxio_advanced_billing/models/…`), members the task sets — no wire aliases on any of them:
- `CreateCustomerRequest(customer: CreateCustomer)` — required. `CreateCustomer`: **required** `first_name: str`,
  `last_name: str`, `email: str`; `reference: Optional[str] = UNSET` — **always set** (client-chosen reference).
- `CreateSubscriptionRequest(subscription: CreateSubscription)` — required. `CreateSubscription`: all `Optional[...] = UNSET`;
  we set `product_handle: str`, `customer_id: int`, `reference: str` (**always set** — "reference value (provided by your
  app) for the subscription itself"), and `payment_collection_method: CollectionMethodOrStr` — `REMITTANCE` when
  `sites.read_site().site.relationship_invoicing_enabled` is true, else `INVOICE` (no card is captured; the default
  `automatic` was refused live: "No payment method was on file for the $299.00 balance"). *(Added after the live run.)*
- `Optional[T]` here is `T | UnsetType`, **not** `typing.Optional` — never pass `None`; read with `isinstance(x, UnsetType)`.
  Response members are mostly `OptionalNullable[...]` (`None` possible) — map UNSET/None to JSON `null` ourselves; never
  hand a raw SDK member to `JsonResponse`.
- `Subscription.state: Optional[SubscriptionStateOrStr]` — open enum `maxio_advanced_billing.models.enums.SubscriptionState`:
  `PENDING, FAILED_TO_CREATE, TRIALING, ASSESSING, ACTIVE, SOFT_FAILURE, PAST_DUE, SUSPENDED, CANCELED, EXPIRED, PAUSED,
  UNPAID, TRIAL_ENDED, ON_HOLD, AWAITING_SIGNUP`; an unknown wire value arrives as plain `str`.
- `ErrorListResponse1.errors: list[str]` (required). `CustomerErrorResponse1.errors: Optional[Errors1]`,
  `Errors1 = CustomerError | list[str]` (from `models/unions/errors1.py`).
- Envelopes: `CustomerResponse.customer: Customer` and `ProductResponse.product: Product` are **required**, but
  `SubscriptionResponse.subscription: Optional[Subscription] = UNSET` — a missing envelope member is an unreadable answer
  (write → lookup; read-back → skipped with a warning). *(Revised after mypy flagged it.)*
- Dates: `RFC3339DateTime` → `datetime.datetime`.
- Decode failure → `pydantic.ValidationError`/`ValueError`, not `ApiError`, in both response modes; transport failures are
  unwrapped `httpx` exceptions. The provider enforces **one customer per reference** (create_customer docstring); no such
  guarantee is documented for subscription `reference`.

Status mapping (`status_from_provider`, the one place a `SubscriptionState` becomes ours):

| outcome | members |
| --- | --- |
| `done` | `ACTIVE`, `TRIALING` |
| `pending` (not-yet / not in good standing, still exists) | `PENDING`, `ASSESSING`, `AWAITING_SIGNUP`, `SOFT_FAILURE`, `PAST_DUE`, `UNPAID`, `PAUSED`, `ON_HOLD`, `SUSPENDED` |
| `failed` (never created, or undone) | `FAILED_TO_CREATE`, `CANCELED`, `EXPIRED`, `TRIAL_ENDED` |
| `unknown` (treated not-yet) | any unlisted value, a plain `str`, `UNSET`, `None` |

### OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| `subscriptions.create_subscription` | `SubscriptionResponse.subscription.state` (`SubscriptionState`) | done (`active`,`trialing`) → record `done`, answer 201 with plan/price/state/next billing; pending (`pending`,`assessing`,`awaiting_signup`,`soft_failure`,`past_due`,`unpaid`,`paused`,`on_hold`,`suspended`) → record `pending`, answer 202 not-done; failed (`failed_to_create`,`canceled`,`expired`,`trial_ended`) → record `failed` with the provider id (claim kept), answer 409 not-done; unknown (unlisted / str / UNSET / None) → record `unknown`, answer 202 not-done. `GET /api/my-subscriptions` re-reads the provider and re-maps pending/unknown records through the same function. | `billing.status_from_provider` (sandbox/apps/subscriptions/billing.py:37) via `billing.subscription_answer` (:281); recorded by `safe_write.safe_write` step 6 → `safe_write.complete`; answered by `views.create_subscription` (`_NOT_DONE_STATUS`, 201/200 only for DONE); re-mapped in `billing.my_subscriptions` → `safe_write.settle` |
| `customers.create_customer` | none — `CustomerResponse.customer` carries no status member; a customer exists or it does not. Outcome is `done` only when `customer.id` is readable and `customer.reference` echoes ours; an unreadable id → `unknown` | `billing._customer_answer.read` (billing.py:219) — `Unreadable` on a missing id sends `safe_write.safe_write` to the lookup; reference mismatch → `needs_review` |

### DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| `customers.create_customer` (one per user) | `MaxioWriteClaim` row (Django DB — SQLite here, Postgres in `settings_postgres.py`), committed in its own transaction **before** the provider call; reference `"{prefix}:u{user_pk}-{date_joined_ts}:customer"` | the DB `UNIQUE` constraint on `MaxioWriteClaim.reference` (`IntegrityError`) | `try_claim` catches `IntegrityError` and returns False; the loser answers from `load_existing` | `models.MaxioWriteClaim.reference` (`unique=True`); `safe_write.try_claim` (safe_write.py:83); loser path in `safe_write.safe_write` step 1; claim committed outside the request transaction by `views.billing_endpoint` (`transaction.non_atomic_requests`); reference from `billing.customer_reference`; called from `billing.ensure_customer` |
| `subscriptions.create_subscription` (one per user per plan) | same table; reference `"{prefix}:u{user_pk}-{date_joined_ts}:sub:{plan_handle}"` | same `UNIQUE` constraint | same | same: `safe_write.try_claim` / `safe_write.safe_write` step 1; reference from `billing.subscription_reference`; called from `billing.subscribe` |

`{prefix}` = `MAXIO_REFERENCE_PREFIX` setting if set, else a random token generated once per database
(`MaxioInstall` singleton row) — unique per install even on a shared Maxio site with identical fixtures.

### UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| `customers.create_customer` | same-reference resend (provider allows one customer per reference); a 422 on the resend or first send → lookup `customers.read_customer_by_reference(ref)`: found → landed, adopt; 404 → refused, recorded `failed` and the claim released (a 422 is a verdict on the request itself, on a first send or a check); a check that fails any other way (never sent, 401/403/404/409/429, lookup error) → still `unknown` | the customer reference from DUPLICATE CLAIMS | `billing.ensure_customer` (`repeat_is_safe=True`) → `safe_write.safe_write` (`resending` path, 422 branch → `safe_write._find_or_unknown` → `billing._find_customer`) |
| `subscriptions.create_subscription` | lookup only (no documented reference uniqueness): `subscriptions.find_subscription(reference=ref)`; 404 → still `unknown` (504, `outcome_unknown`); also on 422 before recording `failed`; `GET /api/my-subscriptions` re-checks unknown/stale records against `list_customer_subscriptions` by `reference` | the subscription reference from DUPLICATE CLAIMS | `billing.subscribe` (`repeat_is_safe=False`) → `safe_write.safe_write` step 4 / 422 branch → `safe_write._find_or_unknown` → `billing._find_subscription`; later reconciliation in `billing.my_subscriptions` → `safe_write.settle` |

### WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| `customers.create_customer` | committed `MaxioWriteClaim(kind=customer, reference, user, outcome="sending", claimed_at)` | `outcome` (`done`/`failed`/`unknown`/`needs_review`), `provider_id` = customer id, `provider_time` = `customer.created_at` | `safe_write.try_claim` before `send` in `safe_write.safe_write`; `safe_write.complete` after |
| `subscriptions.create_subscription` | committed `MaxioWriteClaim(kind=subscription, reference, user, plan_handle, outcome="sending", claimed_at)` | `outcome`, `provider_id` = subscription id, `provider_state`, `provider_time` = `subscription.created_at`, snapshot (`next_billing_at`, `price_in_cents`, `currency`, `plan_name`) | `safe_write.try_claim` before `send` in `safe_write.safe_write`; `safe_write.complete` with `billing._subscription_snapshot` after |

### Error boundary (every call site, reads included)

| failure | caller sees |
| --- | --- |
| provider 400/404/409/422 (caller's input) | same status, our message (+ provider `errors` list for 422) |
| provider 401/403 | 502 (our credentials) |
| provider 429 | 503 |
| provider 5xx / unmapped 4xx | 502; on a write → lookup first, still unknown → 504 `outcomeUnknown: true` |
| `httpx.ConnectError/ConnectTimeout/PoolTimeout/ProxyError` | 502, `outcomeUnknown: false`; a first write send releases its claim |
| other `httpx.RequestError` | 504, `outcomeUnknown: true` on a write (after lookup) |
| `ValidationError`/`ValueError` on a 2xx | write → lookup; read → 502 unreadable |
| missing configuration | 503 `billing_not_configured` |
| anonymous caller on a session endpoint | 401 JSON |

## Assumptions & Blockers

- Minor: one subscription per user **per plan** (the reference is derived from user + plan). A second POST for the same
  plan returns the stored outcome instead of creating another — this is the idempotency the task asks for; re-subscribing
  after a cancellation is out of scope.
- Minor: Maxio customer name fields are required; users without first/last names get the email local-part / `"Customer"`.
  A user with no email gets 400.
- Minor: the plans endpoint is public (catalogue data); subscribe/my-subscriptions require a session.
- Minor: POST keeps Django CSRF protection — API callers send the `csrftoken` cookie value as `X-CSRFToken`.
- No blockers: every capability (list plans by family handle, create/lookup customer by reference, create/lookup
  subscription by reference, list a customer's subscriptions) is in the SDK map.

## REQUIRED READING

| Hazard | Skill |
| --- | --- |
| error ladder, `ApiError` union narrowing, never-sent vs may-have-landed transport split, decode failures | MUST load `python-error-handling` (loaded) |
| client lifetime, sync choice, transport ownership/timeout | MUST load `python-client-initialization` (loaded) |
| safe write (claim/call/check/verify/complete), no retries, logging transport, server config | MUST load `python-configuration-resilience` (loaded) |
| status gate, keyword-only calls, parsed vs raw mode | MUST load `python-calling-endpoints` (loaded) |
| `UNSET` vs `None`, open enums, not leaking SDK values into JSON | MUST load `python-models` (loaded) |
| basic auth credentials, silent no-auth | MUST load `python-authentication` (loaded) |
| stub transport tests, both transport-failure inputs, same-operation-twice test | MUST load `python-testing` (loaded) |
