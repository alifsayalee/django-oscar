# Maxio Advanced Billing: subscription billing for the django-oscar sandbox

## Scope

This adds a recurring-subscription capability next to Oscar's one-time commerce. It lives in a new
Django app, `sandbox/apps/subscriptions/`, and exposes JSON endpoints under `/api/`:

| Endpoint | What it does |
| --- | --- |
| `GET /api/session` | Returns the CSRF token and the current user. Sets the `csrftoken` cookie. |
| `POST /api/session` | Logs in with Django session login (email + password, through Oscar's `EmailBackend`). |
| `GET /api/subscription-plans` | Lists the plans in `MAXIO_DEFAULT_PRODUCT_FAMILY` that are not archived. Each entry carries `planHandle`. |
| `POST /api/subscriptions` | Makes sure a Maxio customer exists for the user, then creates the subscription. Returns `subscriptionId` at the top level. |
| `GET /api/my-subscriptions` | Lists the user's subscriptions as Maxio reports them, merged with local claims that have not settled yet. |

Maxio is the system of record for what exists. The local table (`ProviderWrite`) records that *we asked*.
It is the durable claim behind idempotency and unknown-outcome recovery. The caller's identity is
`request.user`, which is Oscar's `AUTH_USER_MODEL`. The Maxio customer is built from that user's
first name, last name and email.

## Repo survey (conventions)

| Convention | Exemplar |
| --- | --- |
| Sandbox apps live under `sandbox/apps/`, imported as `apps.<name>` (sandbox dir on sys.path) | `sandbox/apps/sitemaps.py`, `sandbox/urls.py` (`from apps.sitemaps import ...`) |
| URL wiring: `urlpatterns` list plus `i18n_patterns` for Oscar. API routes go in the plain (non-i18n) list, like `admin/` | `sandbox/urls.py` |
| Settings are read with `django-environ` (`env.str/.bool/.list`) | `sandbox/settings.py` |
| `ATOMIC_REQUESTS = True`. API views that write claims must be `transaction.non_atomic_requests` so a claim commits before the provider call and survives an exception | `sandbox/settings.py` |
| Test runner: sandbox uses `DiscoverRunner`. The app's tests run with `sandbox/manage.py test apps.subscriptions` | `sandbox/settings.py` |
| Style: flake8, max line 119, isort | `setup.cfg` |
| **Sync vs async: sync.** Django under WSGI, sync views, no ASGI anywhere, so the sync `MaxioAdvancedBillingClient` is used | `sandbox/wsgi.py` |

Toolchain: `py -3.11 -m venv venv`, `venv\Scripts\pip install -e .[test]`. The SDK is installed from git
(`maxio-advanced-billing @ git+https://github.com/context-plugins/maxio-python-sdk.git@main`); its runtime deps
(httpx, pydantic[email], typing-extensions) have to be installed explicitly. `pip` did not pull them in here.
The type checker is `mypy --strict` with `django-stubs` on the new app. There is no existing mypy config.

## Contract sheet

Every fact below comes from `sdk-map.md`, `map/operations/{customers,subscriptions,product_families}.md`
and the modules named there, in the SDK cloned from `main`, version `1.0`.

### Client

| Fact | Value | Source |
| --- | --- | --- |
| Class | `MaxioAdvancedBillingClient` (sync). Never mix it with `AsyncMaxioAdvancedBillingClient` | sdk-map.md "Getting a client" |
| Constructor | keyword-only: `environment`, `timeout` (default 30.0), `server_config`, `custom_http_client`, `basic_auth`, `bearer_auth` | sdk-map.md constructor table |
| Auth | `basic_auth=BasicAuthCredentials(username=<MAXIO_API_KEY>, password="x")`. The API key goes in as the username and the password is `x` | SDK README (`curl -u <api_key>:x`) |
| Missing credential | builds `no_auth` silently, so we fail fast with `ImproperlyConfigured` when `MAXIO_API_KEY` is empty | sdk-map.md Servers & auth |
| Environment | `environment=` is `"us"` / `"eu"` / `"maxio_api_gateway"`. **Omitting it is silently `"us"`.** We map `MAXIO_ENVIRONMENT` (`US`/`EU`, case-insensitive) explicitly and raise on any other value | sdk-map.md Environments |
| Base URL | `server_config={"production": {"<env>": {"site": MAXIO_SITE_SUBDOMAIN}}}`. When `MAXIO_BASE_URL` is set: `{"production": {"<env>": {"base_url": MAXIO_BASE_URL}}}`, used verbatim. The config is `extra="forbid"` | sdk-map.md Servers table; `server/server_config.py` `ProductionUsConfig(base_url, site)` |
| Lifetime | one long-lived client, built lazily on first use in each worker process (post-fork), closed `atexit`. `close()` also closes the supplied transport | python-client-initialization |
| Transport | `custom_http_client=LoggingTransport(HttpxClient(timeout=...))`. The client's `timeout=` does not reach a supplied transport, so the timeout is set on `HttpxClient`. The transport logs method, URL and status only | `core.HttpxClient(*, timeout, proxy_url, verify)` |
| Retries | **The SDK performs none.** We add none on writes. Reads are not retried either: a failed read answers 502/504 and the caller retries | python-configuration-resilience |
| Keyword-only boundary | Every keyword-only parameter has a real default, so we never pass defensive `None`s | sdk-map.md |

### Operations in scope

| Operation | Signature (sync, parsed) | Returns | `ApiError.error` union | Members asserted after the call |
| --- | --- | --- | --- | --- |
| `client.product_families.list_products_for_product_family` | `(product_family_id: str, *, page=1, per_page=20, …, include_archived: bool \| None = None, …)`. `product_family_id` is "id or handle prefixed with `handle:`" | `list[ProductResponse]` (`ProductResponse.product: Product`, required) | `ListProductsForProductFamilyErrorBody = str \| RawError` (`str` on 404) | `product.handle` (str, not None), `product.name`, `product.price_in_cents`, `product.interval`, `product.interval_unit` (`IntervalUnitOrStr`), `product.archived_at` (skip if not None/UNSET), `product.description` |
| `client.customers.create_customer` | `(*, body: CreateCustomerRequest \| CreateCustomerRequestDict \| None = None)`. `CreateCustomer` required: `first_name`, `last_name`, `email` (all `str`). Optional `reference: Optional[str]`, **always set** | `CustomerResponse` (`customer: Customer`, required) | `CreateCustomerErrorBody = CustomerErrorResponse1 \| RawError` (422). `CustomerErrorResponse1.errors: Optional[Errors1]` where `Errors1 = CustomerError \| list[str]`, `CustomerError.customer: Optional[str]` | `customer.id` (`Optional[int]`; UNSET means outcome unknown), `customer.reference`, `customer.created_at` |
| `client.customers.read_customer_by_reference` | `(reference: str, *)` | `CustomerResponse` | `RawError` only (Case B); 404 when absent (smoke-verified) | `customer.id` |
| `client.subscriptions.create_subscription` | `(*, body: CreateSubscriptionRequest \| CreateSubscriptionRequestDict \| None = None)`. `CreateSubscription` members set: `product_handle: Optional[str]`, `customer_id: Optional[int]`, `reference: Optional[str]` (**always set**). No idempotency-key parameter exists | `SubscriptionResponse` (`subscription: Optional[Subscription]`, UNSET means outcome unknown) | `CreateSubscriptionErrorBody = ErrorListResponse1 \| RawError` (422). `ErrorListResponse1.errors: list[str]` (required) | `subscription.id`, `subscription.state` (`SubscriptionStateOrStr`, all members mapped below), `subscription.product.handle` (echo check), `subscription.product_price_in_cents`, `subscription.next_assessment_at`, `subscription.current_period_ends_at`, `subscription.created_at`, `subscription.reference` |
| `client.subscriptions.find_subscription` | `(*, reference: str \| None = None)` | `SubscriptionResponse` | `FindSubscriptionErrorBody = RawError` (404 when absent, smoke-verified) | as above |
| `client.customers.list_customer_subscriptions` | `(customer_id: int, *)` | `list[SubscriptionResponse]` | `RawError` only (Case B) | as above |

Async: every operation has an identically named awaited peer on `AsyncMaxioAdvancedBillingClient`. It is not used here.
None of the in-scope operations returns `None`.

Model rules: `Optional[T]` is `T | UnsetType`, **not** `typing.Optional`, so never pass `None` to it.
`OptionalNullable[T]` (for example `next_assessment_at`, `archived_at`, `handle`) may be `None` or `UNSET`.
Narrow with `isinstance(x, UnsetType)` before use. `UNSET` never crosses the JSON boundary, so values are
mapped into plain dicts. Enums are open, so an unknown `state` arrives as a plain `str`. Dates are
`datetime`. No `Optional[Any]` fields are set by us.

`SubscriptionState` members (`models/enums/subscription_state.py`) are `PENDING`, `FAILED_TO_CREATE`, `TRIALING`,
`ASSESSING`, `ACTIVE`, `SOFT_FAILURE`, `PAST_DUE`, `SUSPENDED`, `CANCELED`, `EXPIRED`, `PAUSED`, `UNPAID`,
`TRIAL_ENDED`, `ON_HOLD`, `AWAITING_SIGNUP`.

Error facts:
- A decode failure raises `ValidationError`/`ValueError`, not `ApiError`, in both response modes.
- `httpx` transport exceptions arrive unwrapped.
- Never sent: `ConnectError`, `ConnectTimeout`, `PoolTimeout`, `ProxyError`.
- May have landed: any other `httpx.RequestError`.
- There is no OAuth scheme, so `OAuthProviderError` cannot occur.

Provider duplicate semantics:
- **Customer `reference` is unique at Maxio.** This is the documented rule in the `create_customer` docstring, and it was verified live: a second create answers
  `422 {"errors":["Reference: must be unique - that value has been taken."]}` (`Errors1` `list[str]` arm).
  That answer means an earlier attempt landed.
- **Subscription `reference` uniqueness is not documented.** So a check is a lookup
  (`find_subscription(reference=…)`), never a resend.

## OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| `subscriptions.create_subscription` (and the same mapping for subscriptions read back by `find_subscription` / `list_customer_subscriptions`) | `SubscriptionResponse.subscription.state` (`SubscriptionStateOrStr`) | **done**: `ACTIVE`, `TRIALING` (in effect now, answer 201). **pending**: `PENDING`, `ASSESSING`, `AWAITING_SIGNUP` (not finished yet), plus `PAST_DUE`, `SOFT_FAILURE`, `UNPAID`, `PAUSED`, `ON_HOLD`, `SUSPENDED` (exists, but something is outstanding). All of these answer 202 with the state shown. **failed**: `FAILED_TO_CREATE`, `CANCELED`, `EXPIRED`, `TRIAL_ENDED` (never in effect, or done and later undone). Answer 409 `subscription_not_in_effect`. **unknown**: a value not in the enum, an absent `state`, or an absent `subscription`. Answer 202 with `outcome: unknown`, never success | `maxio.subscription_outcome` (the only state→outcome map), applied in `services.subscription_detail` / `services._answer_for_subscription`; stored by `services.complete`; answered by `views.subscriptions_view` through `views.OUTCOME_STATUS` (only `done` → 201) |
| `customers.create_customer` | none: `Customer` carries no status | **done** when `customer.id` is present. When `customer.id` is UNSET the outcome is **unknown** and nothing downstream acts on it | `services._read_customer` (UNSET id → `unknown`); `services.ensure_customer` raises `OutcomeUnknown` unless the record is `done` with an id |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| `customers.create_customer` | `ProviderWrite` row in the Django DB (SQLite/any backend), `reference = <MAXIO_REFERENCE_PREFIX>-u<user pk>-customer`, committed before the call (view is `non_atomic_requests`) | the DB `UNIQUE` constraint on `ProviderWrite.reference` (and, second line, Maxio's unique customer reference → 422 "taken") | `IntegrityError` caught in the claim function; Maxio's 422 "taken" is recognised as a landing in the customer write | claim: `services.try_claim` on `models.ProviderWrite.reference` (unique), called from `services.safe_write` step 1 via `services.ensure_customer`; provider-side: `services.says_reference_taken` in `services.safe_write` step 2 |
| `subscriptions.create_subscription` | `ProviderWrite` row, `reference = <prefix>-u<pk>-sub-<plan_handle>-<sha256(Idempotency-Key or "default")[:16]>`, committed before the call | the DB `UNIQUE` constraint on `ProviderWrite.reference` | `IntegrityError` caught in the claim function; the loser answers from the stored record (in flight → 202 `in_progress`, no provider call) | claim: `services.try_claim` from `services.safe_write` step 1 via `services.subscribe`; loser answered from `services.load_existing` → `services.record_to_dict` (`in_progress`) in `views.subscriptions_view` |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| `customers.create_customer` | same-reference resend (Maxio refuses a second customer with that reference; the 422 "taken" is followed by `read_customer_by_reference`) | `<prefix>-u<pk>-customer` | `services.safe_write` (`repeat_is_safe=True`: resend under the same reference, then step 3 lookup `services._find_customer` → `customers.read_customer_by_reference`) |
| `subscriptions.create_subscription` | lookup: `subscriptions.find_subscription(reference=ref)`, 404 → not found yet (stays `unknown`) | `<prefix>-u<pk>-sub-<plan>-<key hash>` | `services.safe_write` step 3 → `services._find_subscription` (`subscriptions.find_subscription`); later re-checks in `services._settle`, called from `services.my_subscriptions` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| `customers.create_customer` | `ProviderWrite(kind=customer, reference, user, outcome=sending, claimed_at)` | `outcome` (done/unknown/failed), `provider_id` = Maxio customer id, `provider_time` = `customer.created_at` | before: `services.try_claim`; after: `services.complete(reference, outcome, services._read_customer(...))` inside `services.safe_write` |
| `subscriptions.create_subscription` | `ProviderWrite(kind=subscription, reference, user, plan_handle, outcome=sending, claimed_at)` | `outcome` from state map, `provider_id` = subscription id, `provider_state`, `provider_time` = `subscription.created_at`, `detail` snapshot (plan name, price, next billing date) | before: `services.try_claim`; after: `services.complete(reference, outcome, services._answer_for_subscription(plan)(...))` inside `services.safe_write`; echo check → `needs_review` in `services.safe_write` step 4 |

Echo check (the analogue of the amount check): the write sends no amount, only a plan handle. The check is that
`subscription.product.handle` equals the requested plan handle. A mismatch is recorded as `needs_review` and answered 502.

## Assumptions & Blockers

- Found during live verification: even though the product does not require a card, the default (automatic) collection
  method makes Maxio reject signup with `422 "No payment method was on file for the $299.00 balance"`. So subscriptions
  are created with `payment_collection_method=remittance`, the documented Relationship Invoicing value
  (`models/enums/collection_method.py`). The value is configurable through `MAXIO_PAYMENT_COLLECTION_METHOD` and
  validated against the enum.

- No blockers. The store (the Django DB) holds claims across processes, and the plugin covers every operation needed.
- Minor: plans are read live from Maxio. There is no local plan catalogue, because Maxio is the system of record and
  Oscar has no subscription/plan model. Oscar's `User` is reused for identity.
- Minor: the default idempotency key is `"default"`, so without an `Idempotency-Key` header a user gets one subscription
  per plan. A deliberate second subscription to the same plan requires a new `Idempotency-Key`.
- Minor: `POST /api/session` / `GET /api/session` are added so the flow can be driven through the API alone
  (session login + CSRF). The authentication is still Django's own session login.

## REQUIRED READING

- Client construction and lifetime: MUST load `python-client-initialization` (loaded)
- Credentials and the silent no-auth trap: MUST load `python-authentication` (loaded)
- First call, parsed vs raw, the status-over-id rule: MUST load `python-calling-endpoints` (loaded)
- `UNSET`/`Optional`, open enums, handing values out: MUST load `python-models` (loaded)
- Error ladder and the never-sent vs may-have-landed split: MUST load `python-error-handling` (loaded)
- Safe write, no retries, timeouts, logging transport: MUST load `python-configuration-resilience` (loaded)
- Stub transport tests: MUST load `python-testing` (loaded)
