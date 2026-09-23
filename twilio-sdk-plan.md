# Twilio SDK integration plan — SMS order notifications (sandbox)

## Scope

New Django app `sandbox/apps/order_notifications` (label `order_notifications`), routed under `/api/`
from `sandbox/urls.py` (outside `i18n_patterns`). Reuses Oscar `order.Order`/`order.Line`,
`basket.Basket`, `catalogue.Product`, `OrderCreator`, `OrderTotalCalculator`, shipping `Repository`,
and `Order.set_status`/`EventHandler`. New models: `ContactNumber`, `Notification`,
`ResendRequest`, `OrderTransitionClaim` (all local claim/record rows — the provider stays the
record of what exists).

## Repo survey (read-only)

| Convention | Exemplar |
|---|---|
| Sandbox app layout (`apps.<name>`) | `sandbox/apps/user/` , `sandbox/apps/offers.py` |
| URL wiring | `sandbox/urls.py` (non-i18n `urlpatterns` list) |
| Settings via `django-environ` `env.str/env.bool` | `sandbox/settings.py` |
| Order placement | `src/oscar/apps/order/utils.py` `OrderCreator.place_order` |
| Status change | `src/oscar/apps/order/abstract_models.py` `Order.set_status`, pipeline in settings |
| Test runner | `TEST_RUNNER = DiscoverRunner` in sandbox settings → `manage.py test apps.order_notifications` |

- Host is **sync** Django under WSGI → **sync `TwilioSdkClient`**; never the async client.
- DB: SQLite, `ATOMIC_REQUESTS=True` → views that call the provider are
  `transaction.non_atomic_requests` and commit claim rows *before* the provider call.
- Toolchain: `venv` (py 3.11) + pip, `pip install -e .[test]`; `twilio-sdk` installed from git
  `main` into the venv; `mypy` + `django-stubs` installed for type checking.
- Baseline: fresh DB bootstrapped per Makefile `build_sandbox` steps. **Correction found:** on this
  machine `child_products.json` yields 11 products and `orders.json` fails with a FK error unless the
  three `books.*.csv` files are imported first with `oscar_import_catalogue` (then 209 products, 1 order).

## Credentials / servers

- `settings.TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`,
  `TWILIO_MESSAGING_SERVICE_SID`, `TWILIO_BASE_URL` — `env.str(..., default='')` in
  `sandbox/settings.py`; importing settings never raises. `build_client()` refuses empty SID/token.
- Auth: `account_sid_auth_token=BasicAuthCredentials(username=SID, password=TOKEN)` — omitting it
  silently sends unauthenticated requests, so `build_client()` asserts both are set.
- Servers: messaging operations resolve against `default` (`https://api.twilio.com`); lookup against
  `default4` (`https://lookups.twilio.com`). When `TWILIO_BASE_URL` is non-empty:
  `server_config={"default": {"base_url": TWILIO_BASE_URL}}` (one-environment nesting;
  `ServerConfig` is frozen/`extra="forbid"`). `default4` is never overridden.
- Client: module-level lazy singleton (built on first use, i.e. after any fork), `timeout=10.0`,
  closed via `atexit`. Transport = SDK `HttpxClient(timeout=10.0)` wrapped in a logging transport
  that logs method + path (phone numbers in path/query masked) + status only.

## Smoke results (real credentials, read-only, 2026-09-23)

- `lookups_v2_phone_number.fetch_phone_number3` → HTTP 200 but **SDK decode fails**
  (`ValidationError`: `LookupResponse.caller_name` etc. are `Optional[...]`, not nullable, and the API
  returns `null`). Not usable → use **`lookups_v1_phone_number_api.fetch_phone_number2`** instead (all
  members `OptionalNullable`, decodes cleanly). v1: 200 + E.164 `phone_number` for both configured
  test numbers; **404** for malformed/unassigned input.
- `list_message(from_=FROM, date_sent_query_query=<datetime>)` accepted; `next_page_uri` carries
  `PageToken` + `Page`.
- Messaging service sender pool contains `TWILIO_FROM_NUMBER`.

## Contract sheet

All operations: parsed call raises `ApiError`; **Case B** — `ApiError.error` is always `RawError`
(`status_code`, `text()`, `json()` raises `ValueError` on non-JSON). No typed error arms. Decode
failure → `pydantic.ValidationError`/`ValueError` in both modes. Transport errors → raw `httpx`
exceptions. **The SDK performs no retries.** Every keyword-only param has a real default (`None`) —
omit, never pass defensive `None`s. `request_options` is the trailing keyword-only param.

### 1. `client.lookups_v1_phone_number_api.fetch_phone_number2`
- `GET /v1/PhoneNumbers/{PhoneNumber}` · server `default4`
- `fetch_phone_number2(phone_number: str, *, country_code: str | None = None, type_=…, add_ons=…, add_ons_data=…, request_options=…) -> LookupsV1PhoneNumber`
- Set: `phone_number` (caller input); `country_code` only when caller supplies a national-format
  number (purpose: lets provider parse national format). `type_`/`add_ons` **omitted** (paid
  data; omit → basic validation only).
- Result members used: `phone_number: OptionalNullable[str]` (canonical E.164 — must be a non-empty
  str, else outcome unreadable → 502), `country_code: OptionalNullable[str]`.
- Outcomes: 200 → usable; **404 → provider says not a usable number → our 422**; 400 → 422;
  401/403 → 502; 429 → 503; else 502.

### 2. `client.api20100401_message.create_message`
- `POST /2010-04-01/Accounts/{AccountSid}/Messages.json` · server `default`
- `create_message(account_sid: str, to: str, *, …, schedule_type: MessageEnumScheduleTypeOrStr | None, send_at: RFC3339DateTime | None, from_: str | None, messaging_service_sid: str | None, body: str | None, …) -> ApiV2010AccountMessage`
- positional: `account_sid`, `to`. Wire: `from_`→`From`, `messaging_service_sid`→`MessagingServiceSid`,
  `schedule_type`→`ScheduleType`, `send_at`→`SendAt`, `body`→`Body`.
- Immediate send: `to`, `from_=TWILIO_FROM_NUMBER`, `body`.
- Scheduled follow-up: `messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID` (required for
  scheduling — docstring: "For Messaging Services only"), `schedule_type=MessageEnumScheduleType.FIXED`,
  `send_at=<aware datetime>`, `from_=TWILIO_FROM_NUMBER` (pins sender from the pool so reconciliation
  by `From` sees it), `body`.
- Omitted (omit → provider/account default): `status_callback` (no public URL), `validity_period`,
  `content_retention`, `address_retention`, `smart_encoded`, `shorten_urls`, `risk_check`,
  `attempt` (import/migration-ish counter, not set), `max_price` (obsolete), all content/media/RCS.
- **No idempotency parameter.** Reference = per-notification `ref` token embedded in `body`
  ("Ref XXXXXXXX"); may-have-landed lookup = `list_message(to=…, from_=FROM)` and match `body`
  containing the ref.
- Assert after call: `sid` is a non-empty `str` (else outcome unknown); `status` mapped by name.

### 3. `client.api20100401_message.fetch_message`
- `GET …/Messages/{Sid}.json` · `fetch_message(account_sid: str, sid: str, *, request_options=…) -> ApiV2010AccountMessage`
- 404 → provider has no such message (the only "absence").

### 4. `client.api20100401_message.update_message`
- `POST …/Messages/{Sid}.json` · `update_message(account_sid: str, sid: str, *, body: str | None = None, status: MessageEnumUpdateStatusOrStr | None = None, request_options=…) -> ApiV2010AccountMessage`
- Cancel scheduled follow-up: `status=MessageEnumUpdateStatus.CANCELED` (only member: `canceled`).
  Verify returned `status` maps to `canceled`; otherwise re-fetch and record what the provider says.
- Redact content: `body=""` (docstring: "To redact the text content of a Message, this parameter's
  value must be an empty string"). Verify returned `body` is `""`/`None`. Message record (status,
  error code, dates) survives — **do not use `delete_message`** (it removes the record).

### 5. `client.api20100401_message.list_message`
- `GET …/Messages.json` · `list_message(account_sid: str, *, to: str | None, from_: str | None, date_sent: RFC3339DateTime | None, date_sent_query: RFC3339DateTime | None, date_sent_query_query: RFC3339DateTime | None, page_size: int | None, page: int | None, page_token: str | None, request_options=…) -> ListMessageResponse`
- Wire: `date_sent_query`→`DateSent<`, `date_sent_query_query`→`DateSent>`, `page_token`→`PageToken`,
  `page`→`Page`, `page_size`→`PageSize` (max 1000).
- `ListMessageResponse`: `messages: Optional[list[ApiV2010AccountMessage]]`,
  `next_page_uri: OptionalNullable[str]` (None/UNSET/empty = last page), `page`, `page_size`.
- Pagination: parse `PageToken` and `Page` from `next_page_uri`'s query, re-call with the same
  filters. Bounded: `MAX_PAGES=50`, no-progress guard on token; result carries `truncated`.
- Filter is date-granular in the docs → query widened by one day each side, then narrowed in code
  on the provider's `date_created` (same clock as local `provider_created_at`).

### `ApiV2010AccountMessage` members used (all `Optional`/`OptionalNullable`, none required)
`sid: str|None`, `status: MessageEnumStatusOrStr` (open enum), `to`, `from_` (wire `from`), `body`,
`error_code: int|None`, `error_message: str|None`, `date_created`/`date_sent`/`date_updated`:
RFC 2822 **strings** (parse with `email.utils.parsedate_to_datetime`), `messaging_service_sid`.

### Status mapping — `MessageEnumStatus` (every member, by name; anything else → `unknown`)
| member | outcome |
|---|---|
| `DELIVERED`, `READ` | `delivered` (done) |
| `SENT` | `sent` (done at provider, handset receipt not confirmed — pending delivery) |
| `ACCEPTED`, `QUEUED`, `SENDING` | `pending` |
| `SCHEDULED` | `scheduled` (pending) |
| `CANCELED` | `canceled` |
| `FAILED`, `UNDELIVERED` | `failed` |
| `PARTIALLY_DELIVERED` | `unknown` (MMS/multi-part; needs review) |
| `RECEIVING`, `RECEIVED` | `unknown` (inbound — never expected for our sends) |
| other str | `unknown` |

Terminal (no further polling): `delivered`, `failed`, `canceled`. Resend allowed only from `failed`.

### Error ladder (one place: `gateway._call`)
`ApiError` 400/404/409/422 → `ProviderRejected(status)`; 401/403 → `ProviderConfigError` (502);
429 → `ProviderUnavailable(503, outcome_unknown=False)`; other → `ProviderUnavailable(502,
outcome_unknown=status>=500)` (a 5xx on a write may have landed); `ValidationError`/`ValueError`
→ `ProviderUnreadable` (outcome unknown); `httpx.ConnectError|ConnectTimeout|PoolTimeout|ProxyError`
→ `ProviderUnavailable(502, outcome_unknown=False)`; other `httpx.RequestError` →
`ProviderUnavailable(504, outcome_unknown=True)`. Messaging failures never fail the order operation.

## CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| A number stored/sent-to must be the canonical `phone_number` Lookup returned | `create_message.to` ← `fetch_phone_number2.phone_number` | `register_number`; sends only read `ContactNumber.phone_number` |
| A sid passed to fetch/update must be one `create_message` returned (or found by ref lookup) and belong to the notification's order owner | `fetch/update_message.sid` ← `create_message.sid` | `Notification.provider_sid`, never from request input |
| Cancel is only sent for a follow-up whose sid came from a scheduled create | `update_message(status=canceled)` ← `create_message(schedule_type=fixed)` | `cancel_followups()` |
| Reconciliation `From` filter = the number sends use | `list_message.from_` = `create_message.from_` = `TWILIO_FROM_NUMBER` | gateway |
| Page token passed back must be one the previous page returned, with identical filters | `list_message.page_token` ← `list_message.next_page_uri` | `list_messages()` loop |
| Order/contact ids from requests must belong to the caller (or caller is staff for operator routes) | API ↔ local models | views |

## Durable-row design
- Every send: `Notification` row (status `sending`, `ref`) committed **before** `create_message`.
  NEVER_SENT → `failed`; rejected 4xx → `failed`; unknown → ref lookup → found: settle; else `unknown`.
- Dispatch/cancel: `OrderTransitionClaim(order, to_status)` UNIQUE inserted in the same atomic block
  as Oscar's `set_status`; a request losing the claim returns current state, sends nothing.
- Cancel vs in-flight follow-up: cancel sets `cancel_requested` on follow-ups; the dispatching request
  re-checks it after settling the sid and cancels immediately; follow-ups still `sending`/`unknown`
  are resolved by ref lookup, then cancelled; cancel failures retried (3 attempts, backoff) and left
  `cancel_pending` for the `order_notifications_sweep` management command and every later read.
- Resend: `ResendRequest(idempotency_key UNIQUE)` + new `Notification` inserted in one committed
  transaction before the provider call; a repeat key returns the same notification, sends nothing.

## Assumptions & Blockers
- Minor: "dispatched" is modelled by adding a `Dispatched` status to the sandbox's
  `OSCAR_ORDER_STATUS_PIPELINE` (Pending/Being processed → Dispatched → Complete/Cancelled).
- Minor: a shopper with several numbers is messaged at the most recently registered active one.
- Minor: follow-up delay default 72h (`ORDER_NOTIFICATIONS_FOLLOWUP_DELAY_HOURS`).
- SDK defect (not a capability gap): Lookup v2 response model not decodable → Lookup v1 used.
- No blockers.

## REQUIRED READING
| step | hazard | pointer |
|---|---|---|
| client construction, lifetime, atexit close | pool per request, fork | MUST load python-client-initialization (loaded) |
| credentials | silent no-auth | MUST load python-authentication (loaded) |
| every call site, raw vs parsed, status by name | id ≠ success | MUST load python-calling-endpoints (loaded) |
| UNSET vs None, open enums, RFC3339 send_at | `Optional` ≠ `typing.Optional` | MUST load python-models (loaded) |
| error ladder | Case B `RawError`, decode + httpx not ApiError | MUST load python-error-handling (loaded) |
| writes, claims, reconciliation, pagination, base URL | no retries, may-have-landed | MUST load python-configuration-resilience (loaded) |
| tests with stub transport | fake the transport, not the client | MUST load python-testing (loaded) |
