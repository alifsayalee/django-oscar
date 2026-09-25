# Maxio Advanced Billing — subscription billing for the django-oscar sandbox

## Scope

Additive, parallel capability on the sandbox site (`sandbox/`): a logged-in shopper lists plans,
ensures a Maxio customer exists for them, subscribes, and reads their subscriptions back. Maxio is the
system of record for plans and subscriptions; the app stores only the Maxio customer link and the
enrollment claims (what it *asked* for), keyed to Oscar's own user model (`AUTH_USER_MODEL`).

Endpoints (new Django app `sandbox/apps/subscriptions`, mounted at `/api/` in `sandbox/urls.py`, JSON):

| Route | Action |
| --- | --- |
| `GET /api/session` | CSRF token + current user (sets csrftoken cookie) |
| `POST /api/session` | Django session login (`username`/`email` + `password`) |
| `DELETE /api/session` | logout |
| `GET /api/subscription-plans` | plans of `MAXIO_DEFAULT_PRODUCT_FAMILY`, each with `planHandle` |
| `GET /api/billing-customer` | the caller's Maxio customer link |
| `POST /api/billing-customer` | idempotently ensure the Maxio customer exists |
| `POST /api/subscriptions` | subscribe to `planHandle` (ensures customer first); returns top-level `subscriptionId` |
| `GET /api/subscriptions/<id>` | one of the caller's subscriptions, read live from Maxio |
| `GET /api/my-subscriptions` | the caller's subscriptions, read live from Maxio + reconciled local claims |

All but `/api/session` require an authenticated Django session (401 JSON otherwise); unsafe methods
require the CSRF token (`X-CSRFToken`), exactly as the rest of the site.

## Repo survey

- Host: Django 5.2 under WSGI → **sync**. Exemplar settings: `sandbox/settings.py` (reads env through
  `django-environ` `env.*`). Exemplar URL wiring: `sandbox/urls.py` (plain `path()` list + `include`).
  Sandbox apps live in `sandbox/apps/` imported as `apps.<name>` (e.g. `apps.sitemaps`).
- `ATOMIC_REQUESTS = True` → the claim must be committed before the provider call:
  write views are `transaction.non_atomic_requests` and take the claim in its own short transaction.
- Store: the site's database (SQLite by default, `DATABASE_ENGINE` overridable) — survives processes
  and is shared by every worker; unique constraints are the claim.
- Toolchain: `py -3.11` venv at `venv/`, `pip install -e .[test]`; tests `pytest` (+pytest-django);
  no type checker configured → install `mypy` (+`django-stubs` for the Django layer) into the venv and
  run `mypy --strict` on the new files.
- SDK: `maxio-advanced-billing` 1.0 installed into `venv` from its source repo (pip's
  `git+https` blob-filter clone failed on this host, so it was installed from a depth-1 clone of
  `main`; the built wheel dropped its runtime deps, installed explicitly at the SDK's own ranges:
  `httpx>=0.28.1,<1`, `pydantic[email]>=2.11,<3`, `typing-extensions>=4.13,<5`). Declared in
  `pyproject.toml` test extras? **No** — the sandbox is not packaged; documented in the sandbox README
  section instead (see Assumptions).
- Baseline: `pytest -n 8` on the untouched tree (see Verification log).
- Credentials present: `MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN=cp-exp-1`, `MAXIO_ENVIRONMENT=US`,
  `MAXIO_DEFAULT_PRODUCT_FAMILY=eshop-subscribe`. `MAXIO_ENVIRONMENT` is mapped explicitly
  (`us`/`eu`, case-insensitive); anything else → `ImproperlyConfigured` (never silently `"us"`).
- Read-only smoke (scratch, outside repo): `list_products_for_product_family("handle:eshop-subscribe")`
  → `basic-plan` (2900¢, 1 month, id 7130994) and `eshop-pro` (29900¢, 1 month, id 7130993) — IDs
  already differ from the task table, handles stable. `read_customer_by_reference` miss → 404 RawError;
  `find_subscription` miss → 404 RawError; `list_customers` OK (no 403 gating). Unknown family handle →
  404 with a non-JSON body → the typed `str` arm cannot decode → **`ValueError`**, not `ApiError`.

## Contract sheet

Client: **sync** `MaxioAdvancedBillingClient` (import root `maxio_advanced_billing`). Keyword-only
constructor: `environment` (`Literal["us","eu","maxio_api_gateway"]`, **default `"us"` silently** — we
always pass it), `timeout` (float, default 30.0 → we pass `MAXIO_TIMEOUT`, default 10), `server_config`
(`{"production": {<env>: {"site": <subdomain>}}}`, or `{"production": {<env>: {"base_url": MAXIO_BASE_URL}}}`
when the override is set — `extra="forbid"` nesting per sdk-map *Servers & auth*), `custom_http_client`
(our logging wrapper around `HttpxClient(timeout=...)` — so `timeout` is set on the transport),
`basic_auth=BasicAuthCredentials(username=<MAXIO_API_KEY>, password="x")` (the API key as Basic user,
`x` as password — per the SDK's own API description `curl -u <api_key>:x`). Omitting `basic_auth` is
silent no-auth → we refuse to construct without a key. One long-lived client per process, built lazily
on first use (post-fork), closed at `atexit` (`close()`).

Every operation below also has the `with_raw_response` peer and an `Async…` twin — not used (sync host).
All keyword-only params have real defaults; pass none defensively. None of the in-scope ops returns
`None`. **The SDK performs no retries** — we add bounded retries for **reads only**; writes are never
resent blind (safe write below). Decode failures raise `ValidationError`/`ValueError` (not `ApiError`)
in both modes; `httpx` transport errors arrive unwrapped.

| Operation | Signature (positional \| after `*`) | Returns | `ApiError.error` union | Members we assert on | Map |
| --- | --- | --- | --- | --- | --- |
| `client.product_families.list_products_for_product_family` | `product_family_id: str` \| `page=1, per_page=20, include_archived=None, …` — id **or** `"handle:<handle>"` (docstring) | `list[ProductResponse]` (`.product: Product`, required) | `ListProductsForProductFamilyErrorBody = str [404] \| RawError`; a non-JSON 404 body raises `ValueError` | `product.handle` (OptionalNullable), `name`, `price_in_cents`, `interval`, `interval_unit` (`IntervalUnitOrStr`), `archived_at`; skip entries without a handle | map/operations/product_families.md |
| `client.sites.read_site` | — \| `request_options` | `SiteResponse` (`.site: Site` required) | `RawError` (Case B) | `site.currency` (Optional), `site.relationship_invoicing_enabled: Optional[bool]` | map/operations/sites.md |
| `client.customers.read_customer_by_reference` | `reference: str` | `CustomerResponse` (`.customer: Customer` required) | `RawError` (Case B); 404 = not found | `customer.id` (Optional → UNSET ⇒ unreadable), `customer.reference` | map/operations/customers.md |
| `client.customers.create_customer` | — \| `body: CreateCustomerRequest \| Dict` | `CustomerResponse` | `CreateCustomerErrorBody = CustomerErrorResponse1 [422] \| RawError`; `CustomerErrorResponse1.errors: Optional[CustomerError \| list[str]]`, `CustomerError.customer: Optional[str]` | `customer.id` | map/operations/customers.md |
| `client.subscriptions.create_subscription` | — \| `body: CreateSubscriptionRequest \| Dict` | `SubscriptionResponse` (`.subscription: Optional[Subscription]`!) | `CreateSubscriptionErrorBody = ErrorListResponse1 [422] \| RawError`; `ErrorListResponse1.errors: list[str]` | `subscription` set, `.id`, `.state` (status below), `.product.handle`, `.reference` | map/operations/subscriptions.md |
| `client.subscriptions.find_subscription` | — \| `reference: str \| None = None` | `SubscriptionResponse` | `FindSubscriptionErrorBody = RawError [404, other]` | as create | map/operations/subscriptions.md |
| `client.subscriptions.read_subscription` | `subscription_id: int` \| `include=None` | `SubscriptionResponse` | `RawError` (Case B) | as create + `customer.id` (ownership) | map/operations/subscriptions.md |
| `client.customers.list_customer_subscriptions` | `customer_id: int` | `list[SubscriptionResponse]` | `RawError` (Case B) | as create | map/operations/customers.md |

Request models (`maxio_advanced_billing.models`; `Optional[T]` = `T | UNSET`, **not** `typing.Optional` —
never pass `None`):

- `CreateCustomerRequest(customer: CreateCustomer)` — `CreateCustomer` **required** `first_name: str`,
  `last_name: str`, `email: str`; we also set `reference: Optional[str]` (**always set** — our claim
  reference; docstring: "you may only create one customer for a given reference value", i.e. the
  provider rejects a second create with the same reference). No wire aliases on these members.
- `CreateSubscriptionRequest(subscription: CreateSubscription)` — all members optional; we set
  `product_handle: Optional[str]`, `customer_id: Optional[int]`,
  `payment_collection_method: Optional[CollectionMethodOrStr]` (enum `CollectionMethod`: `AUTOMATIC`
  `automatic`, `REMITTANCE` `remittance`, `PREPAID` `prepaid`, `INVOICE` `invoice`; docstring: legacy
  Statements sites take `invoice`/`automatic`, Relationship Invoicing sites `remittance`/`automatic`/`prepaid`
  — added after the live run showed `automatic` refusing signup with "No payment method was on file";
  default derived from `site.relationship_invoicing_enabled`, overridable by `MAXIO_PAYMENT_COLLECTION_METHOD`),
  `reference: Optional[str]` (**always
  set** — "The reference value (provided by your app) for the subscription itself"; the SDK does not
  document that Maxio rejects a duplicate subscription reference → treat a resend as **unsafe**,
  lookup only). No wire aliases on these members.

Response models: `Subscription` members used — `id: Optional[int]`, `state: Optional[SubscriptionStateOrStr]`,
`product_price_in_cents`, `current_billing_amount_in_cents`, `currency: Optional[str]`,
`next_assessment_at`, `current_period_ends_at`, `created_at`, `updated_at` (`RFC3339DateTime` →
`datetime`; OptionalNullable ones may be `None`), `reference: OptionalNullable[str]`,
`product: Optional[Product]`, `customer: Optional[Customer]`. `Customer.id: Optional[int]`,
`Customer.reference: OptionalNullable[str]`. Every value is mapped to our own JSON with `UNSET → null`
before it crosses the boundary.

Enum `SubscriptionState` (`maxio_advanced_billing.models.enums`, open `SubscriptionStateOrStr`) — full
member list and our outcome (`status_from_provider`):

| member (wire) | outcome | why |
| --- | --- | --- |
| `ACTIVE` (`active`) | done | in effect |
| `TRIALING` (`trialing`) | done | valid trial subscription is in effect |
| `PENDING` (`pending`) | pending | creation in progress (transient) |
| `ASSESSING` (`assessing`) | pending | transient periodic assessment |
| `AWAITING_SIGNUP` (`awaiting_signup`) | pending | not yet signed up; meaning not settled by source → not-yet |
| `PAUSED` (`paused`) | pending | exists, something wrong (site in arrears) |
| `SOFT_FAILURE` (`soft_failure`) | pending | exists, will be retried automatically |
| `PAST_DUE` (`past_due`) | pending | exists, payment overdue — not done, surfaced |
| `UNPAID` (`unpaid`) | pending | exists, unpaid — not done, surfaced |
| `ON_HOLD` (`on_hold`) | pending | exists, billing stopped temporarily |
| `SUSPENDED` (`suspended`) | pending | exists, prepayment used up |
| `FAILED_TO_CREATE` (`failed_to_create`) | failed | signup failed |
| `CANCELED` (`canceled`) | failed | done then undone |
| `EXPIRED` (`expired`) | failed | no longer in effect |
| `TRIAL_ENDED` (`trial_ended`) | failed | trial over with no card; no longer in effect |
| anything else / `UNSET` | unknown | not-yet, never done |

Environment: `MAXIO_ENVIRONMENT` → `us` / `eu` only (gateway needs a bearer connector token, not in
scope → ImproperlyConfigured). Retries: reads only (`list_products_for_product_family`, `read_site`,
`read_customer_by_reference`, `find_subscription`, `read_subscription`, `list_customer_subscriptions`),
3 attempts, backoff 0.5s/1s (honouring `retry-after` up to 5s), on `httpx.TransportError` and
`ApiError` 429/5xx; never on 4xx or decode errors.

## OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| `create_subscription` (and its lookup `find_subscription`) | `SubscriptionResponse.subscription.state` (`SubscriptionStateOrStr`) | done: `active`, `trialing` → enrollment `done`, 201 (200 on replay). pending: `pending`, `assessing`, `awaiting_signup`, `paused`, `soft_failure`, `past_due`, `unpaid`, `on_hold`, `suspended` → `pending`, 202 with `subscriptionId`, re-read on `GET /api/my-subscriptions`. failed: `failed_to_create`, `canceled`, `expired`, `trial_ended` → `failed` (with provider id: row kept, no longer blocks a new subscribe), 422. `UNSET`/unlisted value/no `subscription`/no `id` → `unknown`, 504 `outcomeUnknown: true`, never done | `services.status_from_provider` (the mapping), applied in `services._complete_enrollment` (no id → `unknown`) and `services._refresh_row_from`; answered per outcome in `views.subscriptions` (201/200/202/409/422/504) |
| `create_customer` | none — `Customer` carries no status | an id back (`customer.id` set) → `done`; missing id / unreadable body → `unknown` (504); nothing else to map | `services._complete_customer` (id missing → `unknown` + `OutcomeUnknown`); unreadable body → lookup path in `services.ensure_customer` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| `create_customer` | `BillingCustomer` row (site DB), inserted `sending` before the call; `user` OneToOne unique, `reference` unique (`<prefix>-cust-u<user pk>`) | DB unique constraint on `user` / `reference` (`IntegrityError`); additionally Maxio refuses a second customer with the same reference (422) | `IntegrityError` caught in `services._claim_customer` → `services._settle_loser`; the 422 is caught in `services.ensure_customer` and settled by `services._find_customer` | `models.BillingCustomer` (`user` OneToOne, `reference` unique), inserted by `services._claim_customer` |
| `create_subscription` | `SubscriptionEnrollment` row (site DB), inserted `sending` before the call; `reference` unique; partial unique constraint `(user, plan_handle)` while outcome ∈ {sending, pending, done, unknown, needs_review} | DB unique constraints (`IntegrityError`) — the second request never reaches Maxio; it answers from the stored row (in progress → 202, done → 200 with the same `subscriptionId`) | `IntegrityError` caught in `services._claim_enrollment` → `services._settle_loser`; `InProgress` answered by `views.api_view` | `models.SubscriptionEnrollment` (`reference` unique + `subscriptions_one_live_enrollment_per_plan`), inserted by `services._claim_enrollment` |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| `create_customer` | lookup `read_customer_by_reference`; if 404, resend `create_customer` under the **same** reference (safe: Maxio allows one customer per reference; a 422 then triggers the lookup again) | `BillingCustomer.reference` (`<prefix>-cust-u<user pk>`) | `services.ensure_customer` (CHECK branch: `services._find_customer`, then same-reference resend; after a 5xx/422/timeout/unreadable answer: `services._find_customer`) |
| `create_subscription` | lookup `find_subscription(reference=…)` only (no documented duplicate-reference rejection → resend unsafe); 404 or failed lookup → stays `unknown`, re-checked on the next POST for that plan and on `GET /api/my-subscriptions`, never turned into failed by time | `SubscriptionEnrollment.reference` (`<prefix>-sub-u<pk>-<plan>-<n>` or `<prefix>-sub-u<pk>-k<sha(idempotency key)>`, from `services.subscription_reference`) | `services._check_enrollment` → `services._find_subscription`, called from `services.subscribe` (after 5xx/timeout/unreadable answer, and for a stale/unknown claim) and `services.reconcile` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| `create_customer` | committed `BillingCustomer(user, reference, outcome=sending, claimed_at)` | `maxio_customer_id`, `outcome` (done/failed/unknown), `last_error` | before: `services._claim_customer` (own `transaction.atomic`, views are `non_atomic_requests` via `views.api_view`); after: `services._complete_customer` / `services._complete` |
| `create_subscription` | committed `SubscriptionEnrollment(user, customer, plan_handle, reference, idempotency_key, outcome=sending, claimed_at)` | `maxio_subscription_id`, `provider_state`, `outcome`, price/currency/next billing, `provider_updated_at` (provider clock), `last_error` | before: `services._claim_enrollment` (own `transaction.atomic`); after: `services._complete_enrollment` + `services._subscription_fields` |

Loser rules (both writes): row `sending` and younger than the send window (`max(60s, 3×timeout)`) →
"in progress", no provider call. `sending` older, or `unknown` → one caller wins a compare-and-set
`UPDATE … WHERE outcome=<seen> AND claimed_at=<seen>` and runs the **check** (never a fresh create
under a new reference). `failed` with no provider id → CAS back to `sending`, resend under the same
reference. Verification before complete: the echoed `subscription.product.handle` must equal the plan
asked for, else `needs_review` (409). No money is sent in either request (price comes from the plan),
so there is no amount echo to compare.

## Error boundary (one ladder, `translate_provider_error`)

- `ApiError` 401/403 → 502 `billing_provider_auth` (our credentials); 429 → 503; typed 422 → 422 with
  provider messages; other 4xx → same 4xx (404 on a read → 404); 5xx → 502 (`outcomeUnknown` true on a write).
- `ValidationError`/`ValueError` → 502 `billing_provider_unreadable` (on a write: handled inside the
  safe write as may-have-landed).
- `httpx.ConnectError | ConnectTimeout | PoolTimeout | ProxyError` → 502, `outcomeUnknown: false`.
- other `httpx.RequestError` → 504, `outcomeUnknown: true`.
- `OutcomeUnknown` → 504 `outcomeUnknown: true`. Never `str(e)` to the caller.

## Assumptions & Blockers

- None blocking. Minor: reference prefix `MAXIO_REFERENCE_PREFIX` (default: `oscar-` + 8 hex of
  sha256(`SECRET_KEY`)) keeps references install-unique on a shared Maxio site.
- If a Maxio customer with our reference already exists (e.g. DB rebuilt, same user pk), it is adopted.
- The SDK is not published to an index; the sandbox README documents installing it from the source repo
  (the sandbox is not a package, so there is no dependency file to pin it in beyond that note).
- Plans are not mirrored into Oscar's catalogue: Maxio is the system of record; the plan list is read
  live (cached 60 s in Django's configured cache).

## REQUIRED READING

- Client construction and lifetime → MUST load `python-client-initialization` (loaded)
- Basic-auth wiring, silent no-auth → MUST load `python-authentication` (loaded)
- Calls, status-from-provider gate → MUST load `python-calling-endpoints` (loaded)
- `UNSET` vs `None`, open enums, mapping values out → MUST load `python-models` (loaded)
- Error ladder, transport split, decode failures → MUST load `python-error-handling` (loaded)
- Safe write, retries, timeout, logging transport → MUST load `python-configuration-resilience` (loaded)
- Stub transport tests → MUST load `python-testing` (loaded)

## Verification log

- Baseline: the repo's own suite (`tests/`, `tests/settings.py`) needs PostgreSQL on localhost, which
  this machine does not have (8 errors "connection to server at localhost" on the untouched tree) — not
  attributable to this change; not run further.
- `cd sandbox && PYTHONPATH=. pytest apps/subscriptions --ds=settings` → 33 passed (stub transport at
  the SDK seam: same-request-twice sends one create, in-flight loser makes no call, unsent (502,
  outcomeUnknown=false) vs read-timeout (lookup; 504, outcomeUnknown=true), unknown never resent,
  refused state / unlisted state not done, needs_review, every `SubscriptionState` member mapped, CSRF +
  session auth, base-URL override, read retries).
- `mypy --strict` (+ django-stubs) on `sandbox/apps/subscriptions/` → no issues.
- Sandbox DB: the task's six-step sequence left 35 catalogue objects and `pages/ranges/offers` failed on
  an FK here; adding the Makefile's `oscar_import_catalogue sandbox/fixtures/books.*.csv` step after
  `child_products.json` gives 209 products / 249 countries / 2 users and every fixture loads.
- Live (Maxio site `cp-exp-1`, server on 127.0.0.1:38660): plans listed (`basic-plan`, `eshop-pro`);
  `superuser` → customer 99128406; first subscribe with `automatic` collection refused by Maxio (422
  "No payment method was on file") → claim released, retried under the same reference after the
  collection-method fix → subscription 94521899 `eshop-pro` active $299.00, next billing 2026-10-25;
  repeat → 200 same id; two concurrent `basic-plan` POSTs → one 201 (94521904) + one 202 in_progress,
  one Maxio create; `staff` fresh user with `Idempotency-Key` → customer 99128436, subscription 94521917,
  replay → same id; `GET /api/my-subscriptions` and `/api/subscriptions/<id>` read back from Maxio.
- Retries: bounded retries on reads only (`maxio.read`); writes are never resent blind.
