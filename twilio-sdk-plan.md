# Twilio SDK plan — order SMS notifications for the django-oscar sandbox

Scope: a new Django app `sandbox/apps/sms_notifications/` exposing `/api/...` JSON endpoints
(contact numbers, orders + transitions, notifications, resend, content disposal, reconciliation),
sending through the APIMatic Twilio Python SDK (`twilio-sdk` 1.0.0, import root `twilio_sdk`).

## Toolchain (established)

- venv: `py -3.11 -m venv venv`, `venv\Scripts\pip install -e .[test]`; SDK installed (non-editable)
  from a clone of `context-plugins/twilio-python-sdk@main` (the direct git install hit a transient
  connection reset). `mypy` + `django-stubs` installed into the venv for type checking.
- Sandbox tests: `cd sandbox && ..\venv\Scripts\python manage.py test apps.sms_notifications`.
- Project baseline: `pytest tests/integration/order tests/functional/checkout` on the untouched tree.
- DB bootstrap: the host notes' sequence **is missing** `oscar_import_catalogue fixtures/*.csv`; without
  it there are 11 products / 5 stock records and `orders.json` fails with a FK error. With it:
  209 products, 203 stock records, `orders.json` loads.

## Host decisions

- **Sync client** (`TwilioSdkClient`): Django under WSGI, sync views. Held as a lazily-built,
  lock-guarded module singleton (built on first use, never at import; never per request);
  `close()` registered with `atexit`. Tests inject a client built over a stub transport.
- Credentials: `account_sid_auth_token=BasicAuthCredentials(username=TWILIO_ACCOUNT_SID,
  password=TWILIO_AUTH_TOKEN)`. Settings default to `""` (import never raises); `build_client()`
  refuses empty SID/token/from-number with a configuration error that names the missing settings.
- Servers: messaging operations are all on server `default` (api.twilio.com). When
  `settings.TWILIO_BASE_URL` is non-empty → `server_config={"default": {"base_url": TWILIO_BASE_URL}}`
  verbatim. Lookups (`default4`, lookups.twilio.com) is **not** governed by it. Omitted keys fall
  through to the config's own defaults (single environment — nothing else to select).
- `timeout=settings.TWILIO_TIMEOUT_SECONDS` (default 10.0) on our own `HttpxClient`, wrapped by a
  logging transport (method, host, **masked** path, status, ms; never query string, headers or
  bodies — phone numbers appear in the Lookups path and `To` query).
- **No SDK retries exist.** Ours: reads (fetch/list/lookup) get 1 retry on 429/5xx/never-sent
  transport errors; `create_message` is **never retried** (no idempotency parameter on it) — an
  uncertain outcome is reconciled by lookup; cancel is retried on 404 (observed propagation delay
  right after scheduling) with 1s/2s/4s backoff, then left `requested` for the sweep.

## Contract sheet (every row from the SDK map / source; smoke-verified where noted)

Common: every op below is **Case B** — `ApiError.error` is always `RawError` (`status_code`,
`text()`, `json()`); import `ApiError, RawError, BasicAuthCredentials, HttpxClient, HttpRequest,
HttpResponse, UNSET, UnsetType` from `twilio_sdk.core`, enums from `twilio_sdk.models.enums`. Every
keyword-only parameter has a real default (`None`) — never pass defensive `None`s. Decode failures
raise `pydantic.ValidationError`/`ValueError` in both modes; httpx exceptions arrive unwrapped.

| # | Call | Server | Positional | Keywords we set | Returns | Notes |
|---|---|---|---|---|---|---|
| 1 | `client.lookups_v1_phone_number_api.fetch_phone_number2` | default4 | `phone_number` (path `PhoneNumber`) | `country_code` (query `CountryCode`) only when the caller gave one | `LookupsV1PhoneNumber` | members used: `phone_number: OptionalNullable[str]` (canonical E.164 — **assert not UNSET/None**), `country_code: OptionalNullable[str]`. Smoke: valid → 200; unusable input → **404, JSON code 20404**. |
| 2 | `client.api20100401_message.create_message` | default | `account_sid`, `to` (form `To`) | `from_` (form `From`) = `TWILIO_FROM_NUMBER` always; `body` (form `Body`); for the follow-up only: `messaging_service_sid` (form `MessagingServiceSid`) = `TWILIO_MESSAGING_SERVICE_SID`, `schedule_type=MessageEnumScheduleType.FIXED` (form `ScheduleType`), `send_at: RFC3339DateTime` (datetime, tz-aware UTC; form `SendAt`) | `ApiV2010AccountMessage` | Scheduling is "for Messaging Services only" (docstring). Smoke: `from_` + MSS + fixed schedule → `status=scheduled`, `from == TWILIO_FROM_NUMBER`, `date_sent None`. **No idempotency/reference parameter.** Other optionals (status_callback, validity_period, content_retention, …) omitted → provider/account defaults; no public URL so no `status_callback`. |
| 3 | `client.api20100401_message.fetch_message` | default | `account_sid`, `sid` | — | `ApiV2010AccountMessage` | missing sid → 404 code 20404 |
| 4 | `client.api20100401_message.update_message` (cancel) | default | `account_sid`, `sid` | `status=MessageEnumUpdateStatus.CANCELED` (only member) | `ApiV2010AccountMessage` | Smoke: immediately after scheduling → **404** (transient); seconds later → 200 `canceled`; again → **409 code 30409** "not in a cancelable state" → fetch to learn the real state. |
| 5 | `client.api20100401_message.update_message` (redact) | default | `account_sid`, `sid` | `body=""` ("To redact … must be an empty string") | `ApiV2010AccountMessage` | Smoke: returns `body == ""`; fetch afterwards `body == ""`. **Verify echoed body is `""`** before recording disposal. |
| 6 | `client.api20100401_message.list_message` | default | `account_sid` | `from_` (query `From`) = `TWILIO_FROM_NUMBER`; `date_sent_query_query` (query **`DateSent>`**, on/after) = window start; `date_sent_query` (query **`DateSent<`**, on/before) = window end; `page_size` (query `PageSize`, max 1000); `page` / `page_token` (query `Page`/`PageToken`) taken from `next_page_uri` | `ListMessageResponse` | Smoke: datetime values accepted (200). Filters documented as GMT **dates** → treat as whole-day granularity: widen to dates, **narrow back in code** on parsed `date_sent`. Paging: `next_page_uri: OptionalNullable[str]` carries `Page`, `PageToken` (smoke-verified); bounded loop (MAX_PAGES), no-progress guard, `truncated` flag in the report. Also used by find-by-ref with `to` (query `To`) + `from_`, first page only. |

`ApiV2010AccountMessage` members we read (all `Optional…`, check with `isinstance(x, UnsetType)`/None):
`sid`, `status: Optional[MessageEnumStatusOrStr]`, `body`, `to`, `from_` (wire `from`),
`date_created`, `date_sent` (RFC 2822 strings, e.g. `Wed, 23 Sep 2026 12:22:59 +0000` → parse with
`email.utils.parsedate_to_datetime`), `error_code: OptionalNullable[int]`, `direction`
(`MessageEnumDirection`: inbound, outbound-api, outbound-call, outbound-reply). **Assert `sid` is a
non-empty str after every create/update; missing → outcome unknown.**

### Status mapping — `MessageEnumStatus` (every member, by name; anything else → `unknown`)

| provider | ours | meaning |
|---|---|---|
| DELIVERED, READ | `delivered` | done |
| QUEUED, ACCEPTED, SENDING, SENT, SCHEDULED | `pending` | accepted, not confirmed (SENT = handed to carrier, no receipt yet) |
| FAILED, UNDELIVERED, PARTIALLY_DELIVERED | `failed` | did not (fully) reach the shopper — resend-eligible |
| CANCELED | `canceled` | will never be sent |
| RECEIVING, RECEIVED, any unlisted/new string | `unknown` | neither done nor failed |

Local-only states: `sending` (claimed, provider call in flight/unsettled), `skipped` never used —
shoppers without numbers simply get no rows.

### Error ladder (one place: `gateway.translate`)

`ApiError`: 401/403 → `ProviderConfigError` (502); 429 → `ProviderUnavailable(503, outcome_unknown=False)`;
400/404/409/422 → `ProviderRejected(status, twilio_code)` (caller-fault **only where the call site
says so**); 5xx and unmapped → `ProviderFailure(502, outcome_unknown=status>=500)`.
`ValidationError`/`ValueError` from decoding → `ProviderUnreadable` (outcome unknown).
`httpx.ConnectError|ConnectTimeout|PoolTimeout|ProxyError` → `ProviderUnavailable(502, outcome_unknown=False)`;
other `httpx.RequestError` → `ProviderUnavailable(504, outcome_unknown=True)`.
Logged: status + Twilio numeric `code` only (error text embeds the account SID / numbers).

## Design

Models (`sms_notifications`): `ContactNumber` (user FK, canonical `phone_number`, `country_code`,
soft-delete `deleted_at`; unique active (user, phone_number)); `Notification` (order FK → Oscar
`order.Order`, contact_number FK, `kind` placed/dispatched/followup/cancelled, `reference` UUID
(claim key, embedded in body as `Ref XXXXXXXX`), `resend_of`, `idempotency_key`, `status`,
`provider_status`, `provider_sid` unique, `error_code`, `scheduled_for`, `provider_date_created`,
`provider_date_sent`, `cancel_state` ""/requested/canceled/too_late, `content_disposed_at`,
timestamps; UNIQUE (order, kind, contact_number) WHERE resend_of IS NULL; UNIQUE (resend_of,
idempotency_key) WHERE resend_of IS NOT NULL); `OrderTransitionClaim` UNIQUE (order, to_status).

Send path (claim → call → reconcile → settle): insert the Notification row (`sending`) first; unique
violation → someone else owns it, answer from their row. Call create; never-sent/4xx → `failed`;
may-have-landed (5xx, read timeout, unreadable) → `find_by_ref` (list To+From, match `Ref`);
found → settle from it, else `unknown`. Settle from provider `status` via the table above. Never raises
into the order flow.

Flows: POST /api/orders builds an owned Oscar `Basket` via the partner strategy, shipping
`Repository`, `OrderTotalCalculator`, `OrderNumberGenerator`, `OrderCreator.place_order` (host models,
no parallel ones); dispatch/cancel = `OrderTransitionClaim` + host `Order.set_status` in one
transaction (sandbox pipeline gains `Dispatched`); side effects only for the claim winner.
Cancel: mark all live follow-ups `cancel_state=requested` (committed) → cancel each at provider →
then send the cancellation notice. Dispatch re-reads the follow-up after settling and cancels it if a
cancel raced it. Sweep (on reads + `manage.py sms_sync`): retry `requested` cancels, refresh non-final
rows (bounded), resolve stale `sending`/`unknown` by ref. Resend: operator-only; target must be
`failed`, number active, content not disposed, order not cancelled (unless it is the cancellation
notice); key claim = UNIQUE (resend_of, key) — the same key always returns the same row, never a second
send. Dispose: cancel first if still scheduled, redact, verify `body == ""`, record
`content_disposed_at`; row/status survive. Reconciliation: provider side = list From=our number over
the widened day window, narrowed to [from, to) on `date_sent`, inbound excluded; local side on the
**same clock** (`provider_date_sent`), refreshed first; report matched (with status agreement),
provider_only, local_only, unsettled (no provider send time yet), `truncated`.

HTTP: session auth (Django `login()`; `POST /api/session` convenience login + `GET /api/csrf`), CSRF
enforced (`X-CSRFToken`), 401 unauthenticated, 403 non-staff on operator routes, 404 for other
users' objects. Routes under `/api/` outside `i18n_patterns`.

## CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| `to` sent to create_message is a number the Lookup returned (canonical), still active, owned by the order's user | create_message ← fetch_phone_number2 | ContactNumber row; send path filters `deleted_at IS NULL` |
| sid given to fetch/update is one create_message returned for this app | fetch/update ← create_message | `Notification.provider_sid` |
| `from_` filter on list equals the `from_` every send used | list_message ← create_message | both read `settings.TWILIO_FROM_NUMBER` |
| follow-up scheduling needs the Messaging Service | create_message(schedule) ← settings | `TWILIO_MESSAGING_SERVICE_SID` required for dispatch follow-up |
| notificationId accepted by resend/dispose is one the notifications list returned | API ← API | 404 otherwise |
| orderId / contactNumberId accepted belong to the caller (or caller is staff for operator routes) | API ← API | queryset scoping |
| productId in POST /api/orders is a purchasable catalogue product | orders ← catalogue | partner strategy availability check |

## Assumptions & Blockers

- **SDK/spec defect (not a gap):** `lookups_v2_phone_number.fetch_phone_number3` cannot decode the live
  response — the API returns `null` for `caller_name`, `sim_swap`, … which `LookupResponse` types as
  non-nullable `Optional[...]` → `ValidationError` on every call (smoke-verified). The plugin also
  exposes Lookups v1 (`fetch_phone_number2`, all-nullable model) which returns the canonical number
  and 404 for unusable input — used instead. Minor.
- The US "unreachable" number validates as a real US number (v1 lookup 200), so it can be registered;
  its messages are accepted then refused by the carrier (`undelivered`) — an outcome, not a gap.
- No public URL → no status callbacks; state is pulled (fetch/list) on read. Minor.
- Messages go to every active number of the shopper. Minor.

## REQUIRED READING (all loaded before implementation)

- MUST load `python-error-handling` — the ladder above, never-sent vs unknown split.
- MUST load `python-client-initialization` — lazy singleton, close obligation, custom transport timeout.
- MUST load `python-configuration-resilience` — claim/settle rows, no retries of create, pagination
  bound + truncated flag, reconciliation clock/window narrowing, no-op transitions fire nothing.
- MUST load `python-calling-endpoints` — status-from-provider allow-list; id ≠ success.
- MUST load `python-models` — `UNSET` vs `None`, open enums (`str` arm → unknown), `from_` alias.
- MUST load `python-authentication` — settings default `""`, check in `build_client()`.
- MUST load `python-testing` — stub transport seam; never-sent vs unknown tests; unknown status test.

## Implementation notes (as built)

- Messages for order events are driven by Oscar's own `order_placed` / `order_status_changed`
  signals (run on commit), so storefront and dashboard orders are covered too; the API's
  `OrderTransitionClaim` makes a repeated dispatch/cancel a no-op that sends nothing.
- The sandbox runs with `ATOMIC_REQUESTS=True`; the API views opt out (`non_atomic_requests`) so
  claim rows commit before provider calls and on-commit messages are sent before the response.
- `httpx`/`httpcore` loggers pinned to WARNING in the sandbox (the root logger is DEBUG and those
  libraries log full URLs); the gateway's transport logs a masked line instead.
- Verified live (2026-09-23): delivered SMS to the CA test number; US number `undelivered`
  (30034) → resend under a key, replay returns the same notification with no second send;
  follow-up scheduled 72h out and cancelled (provider `canceled`, `date_sent` null); redaction
  confirmed by an independent fetch (`body` empty, status kept); reconciliation matched 5/5 in a
  1-hour window and surfaced 39 provider-only messages from the same number over 24 hours.
