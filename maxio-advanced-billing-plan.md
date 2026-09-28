# Maxio Advanced Billing — subscription billing for the django-oscar sandbox

Plan + contract sheet for the `sandbox/apps/subscriptions` Django app. All SDK facts below come
from the SDK map (`sdk-map.md`, `map/operations/*.md`) and the source modules of
`maxio-advanced-billing` 1.0 (`main` branch), installed into `venv/`, plus a read-only smoke run
against site `cp-exp-1` from a scratch directory outside the repo.

## Scope

| Capability | Route | Maxio operations |
| --- | --- | --- |
| Browse plans | `GET /api/subscription-plans` | `product_families.list_products_for_product_family`, `sites.read_site` |
| Subscribe | `POST /api/subscriptions` | `customers.create_customer` (+ `read_customer_by_reference`), `subscriptions.create_subscription` (+ `find_subscription`) |
| My subscriptions | `GET /api/my-subscriptions` | `customers.list_customer_subscriptions`, `subscriptions.find_subscription` (to settle unknowns) |

No storefront UI. Callers authenticate using Oscar's existing session login (`/en-gb/accounts/login/`).
Their identity comes from `request.user`. POSTs carry Django's CSRF token (`X-CSRFToken`), as every
other session-authenticated POST on the site does.

## Architecture decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Sync vs async | **Sync** `MaxioAdvancedBillingClient` | The sandbox is Django under WSGI with sync views, so there is no event loop. |
| Client lifetime | One per process, built lazily on first use (so it is built post-fork), closed via `atexit` | Pool reuse, and fork safety under uWSGI (`sandbox/uwsgi.ini`). |
| Environment | `environment=` mapped explicitly from `MAXIO_ENVIRONMENT` (`US`→`"us"`, `EU`→`"eu"`), with an unknown value → `ImproperlyConfigured` | Omitting it is silently `"us"`. |
| Base URL | `server_config={"production": {env: {"site": MAXIO_SITE_SUBDOMAIN}}}`; if `MAXIO_BASE_URL` is set → `{"production": {env: {"base_url": MAXIO_BASE_URL}}}` verbatim | The map's *Servers & auth* nesting. |
| Auth | `basic_auth=BasicAuthCredentials(username=MAXIO_API_KEY, password="x")` | The SDK README/api-reference curl example `-u <api_key>:x`, confirmed by the smoke run (200). |
| Timeout | 10 s for reads (`MAXIO_TIMEOUT`); **30 s per call for the two creates** (`request_options={'timeout': WRITE_TIMEOUT}`), which the SDK's `HttpxClient` honours through `request.timeout` | The 30 s default is too long for a read. The live run saw a 8.7 s `create_subscription`, and a write timeout costs an unsettled outcome. |
| Transactions | The write endpoints are `@transaction.non_atomic_requests`. The sandbox sets `ATOMIC_REQUESTS: True`, which would otherwise hold the claim uncommitted across the Maxio call and roll it back on a crash | A claim must be committed before the call. |
| SQLite | The sandbox SQLite connection uses `transaction_mode: IMMEDIATE` with a `timeout` of 20 | The live double-click test hit "database is locked" on a read→write lock upgrade. |
| Collection method | `payment_collection_method` = `remittance` on Relationship-Invoicing sites (`Site.relationship_invoicing_enabled`), else `invoice`. A plan with `require_credit_card` → 422 `payment_method_required` from us | The live run: the default (automatic) → 422 "No payment method was on file for the $299.00 balance". The docstring for `CreateSubscription.payment_collection_method` lists the valid values per architecture. |
| Retries | Reads only: up to 3 attempts on never-sent transport errors and 429/502/503/504, with short backoff (Retry-After honoured, capped). **Writes are never auto-retried.** Unknown writes are settled by the safe write's check. | The SDK performs none. |
| Logging | `LoggingTransport` wrapping `HttpxClient`: logs method, path and status, never headers or bodies | The SDK has no hook. |
| Claim store | Django ORM on the sandbox DB: a `MaxioWrite` table with a **unique** `reference` column. The claim is INSERT-or-`IntegrityError`, and re-claims are conditional UPDATEs (compare-and-set) | The codebase already enforces uniqueness through DB constraints. |
| Install prefix | `MAXIO_REFERENCE_PREFIX` setting if set, else `oscar-<12 hex>` from a one-row `MaxioInstall` table (uuid created once per DB) | Two installs sharing one Maxio site never collide on `user.pk`. |
| Oscar models reused | `AUTH_USER_MODEL` (Oscar's customer/user) is the identity; no parallel user/customer/product tables. Maxio is the system of record for plans and subscriptions. Locally we store only the write claims. | Task mandate. |
| Plans | Every non-archived product in family `handle:<MAXIO_DEFAULT_PRODUCT_FAMILY>`, paged with `per_page=200` | Handles are stable; IDs are not. The smoke run showed IDs already differ from the brief (7130993/7130994). |
| Subscribe reference | `<prefix>:u:<user.pk>:s:<sha256(key)[:24]>`, where key = the caller's `Idempotency-Key` header if sent, else `plan:<planHandle>` | A double-click with no key collapses to one write. A deliberate second subscription sends a new key. |
| Customer reference | `<prefix>:u:<user.pk>` | Maxio enforces uniqueness on the customer reference (see the contract sheet). |
| Dependency | `sandbox/requirements-maxio.txt` pins the git source and the SDK's runtime deps | The SDK is not on PyPI. A direct-URL dependency in Oscar's `pyproject.toml` would block its PyPI release. |

## Contract sheet

All operations are on the **sync** client. Every `Async…` peer has the identical name and params,
but it is not used here. Every keyword-only parameter has a real default: **never pass defensive `None`s.**
Every call also takes `request_options=` (keys: `timeout`, `extra_headers`). None of the in-scope
operations returns `None`.

| # | Operation | Signature (positional \| keyword-only) | Returns (parsed) | `ApiError.error` union | Source |
| --- | --- | --- | --- | --- | --- |
| 1 | `client.product_families.list_products_for_product_family` | `(product_family_id: str, *, page=1, per_page=20, …, include_archived: bool\|None=None, …)`. `product_family_id` is "either the product family's id or its handle prefixed with `handle:`" (docstring) | `list[ProductResponse]` | `ListProductsForProductFamilyErrorBody` = `str` [404] \| `RawError`. **Smoke: a 404 for an unknown family has an empty body → `ValueError("Response body is not valid JSON")`, not `ApiError`** | map/operations/product_families.md |
| 2 | `client.sites.read_site` | `(*, request_options=None)` | `SiteResponse` (`.site: Site`, `.site.currency: Optional[str]`, `.site.relationship_invoicing_enabled: Optional[bool]`) | Case B: `RawError` | map/operations/sites.md |
| 3 | `client.customers.create_customer` | `(*, body: CreateCustomerRequest\|Dict\|None=None)`. Body required in practice | `CustomerResponse` (`.customer: Customer`, required) | `CreateCustomerErrorBody` = `CustomerErrorResponse1` [422] \| `RawError`. `CustomerErrorResponse1.errors: Optional[Errors1]`, where `Errors1 = CustomerError \| list[str]`. **Smoke: a duplicate reference → 422 `{"errors":["Reference: must be unique - that value has been taken."]}`** | map/operations/customers.md |
| 4 | `client.customers.read_customer_by_reference` | `(reference: str, *)` | `CustomerResponse` | Case B: `RawError`. **Smoke: a miss → `ApiError` 404 with empty `RawError`** | customers.md |
| 5 | `client.customers.list_customer_subscriptions` | `(customer_id: int, *)` | `list[SubscriptionResponse]` | Case B: `RawError` | customers.md |
| 6 | `client.subscriptions.create_subscription` | `(*, body: CreateSubscriptionRequest\|Dict\|None=None)` | `SubscriptionResponse` (`.subscription: Optional[Subscription]`, so it **can be UNSET**) | `CreateSubscriptionErrorBody` = `ErrorListResponse1` [422] (`errors: list[str]`, required) \| `RawError` | subscriptions.md |
| 7 | `client.subscriptions.find_subscription` | `(*, reference: str\|None=None)` | `SubscriptionResponse` | `FindSubscriptionErrorBody` = `RawError` [404, anything]. **Smoke: a miss → 404, empty body** | subscriptions.md |

Imports: `MaxioAdvancedBillingClient` from the root; `ApiError`, `RawError`, `BasicAuthCredentials`,
`HttpxClient`, `HttpRequest`, `HttpResponse`, `UNSET`, `UnsetType` from `maxio_advanced_billing.core`;
models from `maxio_advanced_billing.models`; `SubscriptionState` from `maxio_advanced_billing.models.enums`.

### Request models (members this task sets)

| Model | Member | Required? | Value |
| --- | --- | --- | --- |
| `CreateCustomerRequest` | `customer: CreateCustomer` | required | — |
| `CreateCustomer` | `first_name: str`, `last_name: str`, `email: str` | **required** (bare annotations) | from `request.user`: names fall back to the email local part / `"Customer"`. An empty email → 400 from us |
| `CreateCustomer` | `reference: Optional[str]` | UNSET-able, but **always set** | the customer reference |
| `CreateSubscriptionRequest` | `subscription: CreateSubscription` | required | — |
| `CreateSubscription` | `product_handle: Optional[str]` | always set | `planHandle` |
| `CreateSubscription` | `customer_id: Optional[int]` | always set | the Maxio customer id from step 1 |
| `CreateSubscription` | `reference: Optional[str]` | UNSET-able, but **always set** | the subscription reference ("The reference value (provided by your app) for the subscription itself") |
| `CreateSubscription` | `payment_collection_method: Optional[CollectionMethodOrStr]` | always set | `CollectionMethod.REMITTANCE` or `CollectionMethod.INVOICE` (enum `models/enums/collection_method.py`: `automatic`, `remittance`, `prepaid`, `invoice`) |

`Optional[T]` here is `T | UnsetType`, **not** `typing.Optional`. Never pass `None` to it. No member
set by this task is typed `Any`. No wire aliases differ for these members (none carry `Field(alias=…)`).

### Response members asserted on

| Model | Member | Type | Use |
| --- | --- | --- | --- |
| `ProductResponse.product` | `handle` (`OptionalNullable[str]`), `name`, `description`, `price_in_cents`, `interval`, `interval_unit` (`IntervalUnitOrStr`: `day`, `month`), `archived_at`, `require_credit_card`, `trial_price_in_cents`, `trial_interval`, `initial_charge_in_cents` | — | plan listing. A product with no handle is skipped (it has no `planHandle`) |
| `Customer` | `id: Optional[int]`, `reference: OptionalNullable[str]` | — | **Assert `id` is set and `reference == sent`** |
| `Subscription` | `id`, `state: Optional[SubscriptionStateOrStr]`, `product.handle`, `customer.id`, `product_price_in_cents`, `currency`, `next_assessment_at`, `current_period_ends_at`, `created_at`/`updated_at` (provider time), `reference` | — | **Assert `id` and `state` are set, `product.handle == planHandle`, `customer.id == customer_id` and `product_price_in_cents == the plan's price_in_cents`** |

### Status enum → outcome (`SubscriptionState`, `models/enums/subscription_state.py`, 15 members)

| Member (wire) | Outcome | Reason |
| --- | --- | --- |
| `active`, `trialing` | **done** | a live subscription, with the plan in effect |
| `pending`, `assessing`, `awaiting_signup` | pending | still being created or assessed |
| `past_due`, `soft_failure`, `unpaid`, `paused`, `on_hold`, `suspended` | pending | exists, but blocked on payment/action. Not "in effect with nothing outstanding" |
| `failed_to_create`, `canceled`, `expired`, `trial_ended` | failed | never came into effect, or was undone |
| anything else (open-enum `str`), or `state` UNSET | unknown | not done |

Customer (`models/customer.py`) has **no status member**. The create-customer outcome is read from
the echoed `reference`: equal to ours with `id` set → done; a different reference → needs_review;
`id` UNSET → unknown.

### Other sheet rows

- **No retries in the SDK.** Our read retry is described above. Writes go through the safe write only.
- **Decode failures** (`pydantic.ValidationError` / `ValueError`) propagate in both modes. On a write's
  2xx → unknown outcome (check by reference). On a read → 502. On a family 404 with an empty body → 502
  "plan catalogue unavailable".
- **Transport errors are raw httpx.** Never-sent = `ConnectError`, `ConnectTimeout`, `PoolTimeout`, `ProxyError`.
  Everything else under `httpx.RequestError` may have landed.
- `401/403` from Maxio → our 502 (our credentials). `429` → 503.
- `environment` is always passed explicitly.

## OPERATION OUTCOMES

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/subscriptions` → `create_customer` | none on `Customer`. The echoed `customer.reference` (plus `id`) is read instead | reference == ours and id set → done (proceed to the subscription). Reference differs → needs_review → 409. id UNSET → unknown → 504. Claim in flight → sending → 202 | `outcomes.customer_outcome` (echoed reference → outcome), `service._read_customer`, applied in `safe_write.safe_write` step 5. `service.subscribe` stops at `stage: customer` unless done. `outcomes.answer` maps it to the HTTP status in `views.subscriptions` |
| `POST /api/subscriptions` → `create_subscription` | `subscription.state` (`SubscriptionState`) | `active`/`trialing` → done → **201**. `pending`/`assessing`/`awaiting_signup`/`past_due`/`soft_failure`/`unpaid`/`paused`/`on_hold`/`suspended` → pending → 202. `failed_to_create`/`canceled`/`expired`/`trial_ended` → failed → 409. Unlisted value or UNSET → unknown → 504. Echo mismatch (plan/customer/price) → needs_review → 409. Stored `sending` → 202 | `outcomes.status_from_provider` (every `SubscriptionState` member by name, `case _` → unknown). `service._subscription_reader` (echo check → `as_asked`), with needs_review in `safe_write.safe_write` step 4. `outcomes.answer` (only done → 201) in `views.subscriptions` |
| `POST /api/subscriptions` (repeat of the same reference) | the stored outcome from the claim record | answered from the record through the same `answer` (done → 201, pending/sending → 202, failed/needs_review → 409, unknown → a lookup is re-run, then 504 if still unsettled) | `safe_write.safe_write` step 1 (`load_existing`: settled or in-flight records are returned without a provider call; unknown/stale → `take_over` then check). `service.subscribe` reads back via `_find_subscription`. `outcomes.answer` |
| `GET /api/my-subscriptions` (finds unsettled writes) | `subscription.state` of the found record | the same mapping as the create. Each listed entry carries its `status`. Unresolved claims are listed with `status: unknown`/`sending` | `service.my_subscriptions` → `safe_write.settle` (lookup only) with `outcomes.status_from_provider`. Listed entries get `status_from_provider(state)` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| `create_customer` for a user | `MaxioWrite` row, `reference = <prefix>:u:<pk>` (sandbox DB) | the DB UNIQUE constraint on `MaxioWrite.reference` (INSERT → `IntegrityError`). Re-claims use a compare-and-set UPDATE. The provider also rejects a duplicate customer reference (422) | `safe_write.try_claim` (catches `IntegrityError` → conditional re-claim of a released `failed` row). The provider 422 duplicate is caught in `safe_write.safe_write` via `service._reference_taken` | `service.ensure_customer` → `safe_write.safe_write` with `service.customer_reference`. Unique constraint: `models.MaxioWrite.reference` |
| `create_subscription` for (user, key) | `MaxioWrite` row, `reference = <prefix>:u:<pk>:s:<digest>` | the same DB UNIQUE constraint and compare-and-set UPDATE | `safe_write.try_claim` (catches `IntegrityError`). A concurrent loser gets the `sending` record from `safe_write.safe_write` step 1 → 202 | `service.subscribe` → `safe_write.safe_write` with `service.subscription_reference` (from `Idempotency-Key` or the plan). Unique constraint: `models.MaxioWrite.reference` |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| `create_customer` | a same-reference resend (Maxio refuses a second customer with the same reference, and that 422 is read as a landing), then a lookup via `read_customer_by_reference` | `<prefix>:u:<pk>` | `safe_write.safe_write` with `repeat_is_safe=True`: the check is `send(ref)` (a 422 duplicate → `service._reference_taken` → lookup), then `find` = `service._not_found_is_none(client.customers.read_customer_by_reference)` |
| `create_subscription` | a lookup via `find_subscription(reference=…)` (404 → not found yet → stays unknown). **Never** a resend: subscription-reference uniqueness is not documented | `<prefix>:u:<pk>:s:<digest>` | `safe_write.safe_write` step 3 (`find` = `service._find_subscription`, 404 → None → `unknown` + `OutcomeUnknown`). Later settled by `safe_write.settle` from `service.my_subscriptions`, or by a repeat POST |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| `create_customer` | a `MaxioWrite(kind=customer, reference, user, outcome=sending, claimed_at)` row | outcome, `provider_id` (Maxio customer id), `provider_time` (`customer.created_at`), `completed_at` | `safe_write.try_claim` inserts the row before `send`. `safe_write.complete` records the outcome, `provider_id`, `provider_time`. Committed immediately because `views.subscriptions` is `transaction.non_atomic_requests` |
| `create_subscription` | a `MaxioWrite(kind=subscription, reference, user, plan_handle, outcome=sending, claimed_at)` row | outcome, `provider_id` (subscription id), `provider_time` (`subscription.created_at`), `provider_state`, `completed_at` | `safe_write.try_claim` (row with `plan_handle`) before `send`. `safe_write.complete` records outcome/`provider_id`/`provider_time`/`provider_state`. `views.subscriptions` is non-atomic |

## Assumptions & Blockers

- No blockers. The sandbox DB (SQLite by default, Postgres optional) holds unique-constraint claims
  across processes.
- Minor: whether Maxio enforces uniqueness of the *subscription* `reference` is UNVERIFIED (the
  docstring doesn't say). The design does not rely on it: it uses lookup-only checks.
- Resolved during the live run: the site default (automatic) collection refuses a signup with no card on file, so subscriptions are created with invoice-based collection (see *Collection method*).
- Minor: the host notes say the CSV import is optional. In this run, `orders.json` failed with an FK
  error until `oscar_import_catalogue sandbox/fixtures/*.csv` ran (11 → 209 products). The CSV step is
  needed.
- Minor: Oscar's own `tests/` need PostgreSQL (baseline: 79 errors, all connection-refused). Our app
  tests run on the sandbox settings (SQLite).

## REQUIRED READING

- Client construction/lifetime → MUST load `python-client-initialization` (loaded)
- Credentials → MUST load `python-authentication` (loaded)
- Calls, response modes, status→outcome, `answer` → MUST load `python-calling-endpoints` (loaded)
- UNSET / open enums / request models → MUST load `python-models` (loaded)
- Error ladder, decode/transport failures → MUST load `python-error-handling` (loaded)
- Safe write, timeouts, logging transport, read retries → MUST load `python-configuration-resilience` (loaded)
- Tests with a stub transport → MUST load `python-testing` (loaded)
