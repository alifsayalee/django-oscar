# Twilio SMS order-notifications — plan & contract sheet

Additive Django app in the oscar **sandbox** (`sandbox/apps/notifications/`) that texts shoppers as
their orders move, using the APIMatic-generated **`twilio-sdk`** Python SDK as the sole Twilio surface.

## Sync vs async — SYNC

Host is Django under WSGI (sandbox `wsgi.py`, no ASGI). Use `TwilioSdkClient` (sync). Teardown:
`client.close()` at process exit via `atexit`. The two clients do not mix. Client is a **lazily
initialised module global** (`twilio_gateway._client`), built on first use so importing the module
(tests, migrations) needs no credentials.

## Server / base URL

Several servers, one environment → `server_config: ServerConfigOrDict | None`. Each server field
(`default`..`default14`) is a nested config with `base_url`; every field has a default_factory so I
set only the one I override. `ServerConfig` is frozen + `extra="forbid"`.
- **Messaging API** (send/read/reconcile: `api20100401_message.*`) resolves against server **`default`**
  = `https://api.twilio.com`. `TWILIO_BASE_URL`, when set, overrides **only** this: build
  `ServerConfig(default={"base_url": TWILIO_BASE_URL})`. When unset, pass no server_config (defaults).
- **Lookup** (`lookups_v1_phone_number_api.fetch_phone_number2`) resolves against **`default4`**
  = `https://lookups.twilio.com` — NOT governed by `TWILIO_BASE_URL` (task: override is messaging-only).
  Left at its default even when `TWILIO_BASE_URL` is set.

## Auth — HTTP Basic

`account_sid_auth_token=BasicAuthCredentials(username=TWILIO_ACCOUNT_SID, password=TWILIO_AUTH_TOKEN)`.
Read from Django settings (which read env). Never logged, never returned, never written to a file.
No OAuth → no token-fetch failure mode.

## Credentials (read via Django settings in sandbox/settings.py; values never hard-coded)

`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `TWILIO_MESSAGING_SERVICE_SID`,
`TWILIO_BASE_URL` (optional). Added as `env.str(..., default='')` before the `settings_local` import.
`TWILIO_TEST_TO_NUMBER` / `TWILIO_UNREACHABLE_TO_NUMBER` are used only by the verification script (env),
not by app code.

---

## In-scope operations — CONTRACT SHEET

Every operation below is **Case B** (error union = `RawError` only; nothing to narrow). Every
controller has an `Async` peer — not used (sync). All calls resolve auth via `account_sid_auth_token`.
Keyword-only boundary is after `*`; every keyword-only param has a real default, so **never pass
defensive `None`s**.

### 1. Validate + canonicalize a destination — `lookups_v1_phone_number_api.fetch_phone_number2`
- Route `GET /v1/PhoneNumbers/{PhoneNumber}` · Server `default4` · Returns `LookupsV1PhoneNumber`.
- Signature: `fetch_phone_number2(phone_number: str, *, country_code=None, type_=None, add_ons=None, add_ons_data=None, request_options=None)`. `phone_number` positional (path).
- Response members used: `phone_number` (`OptionalNullable[str]` — canonical **E.164**), `country_code`.
  Model is all `OptionalNullable` → decodes cleanly (verified against live API).
- **Semantics (verified live):** 200 ⇒ usable destination; store `phone_number` (canonical E.164).
  404 (code 20404) ⇒ not a usable destination ⇒ reject registration. The reserved *unreachable* US
  number returns **200** (registerable; its undeliverability is a send-time outcome, not a lookup
  failure) — correct per task.
- **Why v1 not v2:** `lookups_v2_phone_number.fetch_phone_number3` returns `line_type_intelligence`,
  `caller_name`, etc. as JSON `null`; its model types them `Optional[X]` = `X | UnsetType` (no `None`
  arm) → every real 200 response raises `pydantic.ValidationError`, in BOTH response modes (verified
  live). v1's model is all `OptionalNullable` and decodes. v1 gives validity (200 vs 404) + canonical
  form, which is exactly what the task needs. This is a **design decision, not a gap** — the plugin
  exposes the capability and v1 works.
- Assert after call: `phone_number is not UNSET`/not None; else treat as unusable.

### 2. Send a message (immediate) — `api20100401_message.create_message`
- Route `POST /2010-04-01/Accounts/{AccountSid}/Messages.json` · Server `default` · Returns `ApiV2010AccountMessage`.
- Signature positional: `account_sid, to`. Keyword-only used: `from_` (wire `From`), `body` (wire `Body`).
- Immediate send: `create_message(ACCOUNT_SID, to, from_=TWILIO_FROM_NUMBER, body=text)`.
- Response members used: `sid` (provider id), `status` (`MessageEnumStatusOrStr`), `error_code`
  (`OptionalNullable[int]`), `date_sent` (`OptionalNullable[str]`, RFC-2822).
- Assert `sid is not UNSET`/not None after call → else outcome unknown.

### 3. Schedule the delivery-feedback follow-up — `api20100401_message.create_message`
- Same op. Scheduling (docstring + enum): requires `schedule_type='fixed'`, `send_at` (RFC3339
  datetime), and `messaging_service_sid` (Messaging Services only — **not** `from_`).
- `create_message(ACCOUNT_SID, to, messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID,
  schedule_type=MessageEnumScheduleType.FIXED, send_at=<now+3 days>, body=text)`.
- `MessageEnumScheduleType.FIXED = "fixed"` (open enum). `send_at` is `RFC3339DateTime` (Annotated over
  `datetime.datetime`) — assign an aware `datetime`, no hand-formatting. Twilio window: 15 min–7 days;
  3 days is valid. Returned `status` = `scheduled`.

### 4. Cancel a not-yet-sent (scheduled) message — `api20100401_message.update_message`
- Route `POST /.../Messages/{Sid}.json` · Server `default` · Returns `ApiV2010AccountMessage`.
- Signature positional: `account_sid, sid`. Keyword-only: `body` (wire `Body`), `status` (wire `Status`).
- Cancel: `update_message(ACCOUNT_SID, sid, status=MessageEnumUpdateStatus.CANCELED)`.
  `MessageEnumUpdateStatus.CANCELED = "canceled"` (only member). Docstring: "cancel not-yet-sent messages".

### 5. Redact message content at the provider — `api20100401_message.update_message`
- Same op. Docstring: "To redact the text content of a Message, this parameter's value must be an empty
  string." → `update_message(ACCOUNT_SID, sid, body="")`. Record/status survive; body gone at provider.

### 6. Read delivery outcome — `api20100401_message.fetch_message`
- Route `GET /.../Messages/{Sid}.json` · Server `default` · Returns `ApiV2010AccountMessage`.
- `fetch_message(ACCOUNT_SID, sid)`. Members used: `status`, `error_code`, `date_sent`.

### 7. Reconciliation list — `api20100401_message.list_message`
- Route `GET /.../Messages.json` · Server `default` · Returns `ListMessageResponse`.
- Signature: `list_message(account_sid, *, to=None, from_=None, date_sent=None, date_sent_query=None,
  date_sent_query_query=None, page_size=None, page=None, page_token=None, request_options=None)`.
- Wire: `from_`→`From`, `date_sent_query`→`DateSent<` (upper bound), `date_sent_query_query`→`DateSent>`
  (lower bound), `page`→`Page`, `page_size`→`PageSize`.
- **Ask the provider for OUR number's traffic:** always pass `from_=TWILIO_FROM_NUMBER` (never filter a
  wider answer). Whole-day granularity → widen: `date_sent_query_query=from.date()`,
  `date_sent_query=to.date()`; then **narrow in code** to `[from, to)` on each message's parsed
  `date_sent`. `ListMessageResponse.messages: list[ApiV2010AccountMessage]`; `next_page_uri`
  (`OptionalNullable[str]`) drives paging. Bound the page loop (MAX_PAGES) + report truncation.

### Status → outcome mapping (the ONE place a provider status becomes ours)
`MessageEnumStatus` members: queued, sending, sent, failed, delivered, undelivered, receiving,
received, accepted, scheduled, read, partially_delivered, canceled. Map (enumerate by name; default =
`unknown`, never `failed`):
- **done**: `delivered`, `sent`, `received`, `read`
- **pending**: `queued`, `sending`, `accepted`, `scheduled`, `receiving`
- **failed**: `failed`, `undelivered`, `canceled`
- **partial**: `partially_delivered` (report as-is; not folded into done/failed)
- **unknown**: anything else / a value newer than the SDK.
Store the raw provider status string on the row too; never fold pending/unknown into done/failed.

### Error boundary (one place; `twilio_gateway`), per python-error-handling
`except ApiError`: 404 on lookup ⇒ `NotAUsableDestination` (caller 400/422). `401/403` ⇒ config error
(our creds) → 502. `429` ⇒ 503. other 4xx ⇒ provider-rejected. `5xx`/unmapped ⇒ 502. `RawError.text()`
never surfaced raw to caller (leak/secret) — logged server-side only, and the shopper's number is
never logged. `except ValidationError` ⇒ outcome-unknown (do NOT assume failure). Transport:
`(ConnectError, ConnectTimeout, PoolTimeout, ProxyError)` ⇒ never-sent (known); other `httpx.RequestError`
⇒ outcome-unknown. `raise ... from e` always. Timeout set explicitly (e.g. 15s), not the 30s default.
No retries in SDK — send failures are recorded on the row, not auto-retried.

### CROSS-OPERATION INVARIANTS
| invariant | operations | enforced where |
|---|---|---|
| `to` of a send/schedule must be a canonical number produced by a prior successful lookup+register | `create_message.to` ← `fetch_phone_number2` (registration) | implementation: only send to a `ContactNumber.canonical_number` the caller owns |
| `sid` passed to cancel/redact/fetch must be a `sid` a prior `create_message` returned and recorded | `update_message.sid` / `fetch_message.sid` ← `create_message.sid` | implementation: operator endpoints act on an `OrderNotification` row, using its stored `provider_sid` |
| reconciliation local side keyed on provider `date_sent`, not our `created_at` (different clocks) | `list_message` ↔ local rows | implementation: store `provider_time` from `date_sent`; filter local on it |

---

## Application design

### App: `sandbox/apps/notifications/` (label `sms_notifications`)
Files: `__init__.py`, `apps.py` (`NotificationsConfig`, `name='apps.notifications'`,
`label='sms_notifications'`), `models.py`, `views.py`, `urls.py`, `twilio_gateway.py`,
`services.py`, `serializers.py` (plain dict builders), `migrations/`. Plain Django views + `JsonResponse`
(no DRF in sandbox). Register in `INSTALLED_APPS`; route `path('api/', include('apps.notifications.urls'))`
in `sandbox/urls.py` **outside** `i18n_patterns` (stable `/api/` prefix).

### Models (my durable rows — the provider stays system-of-record for messages; these record *that we asked*)
- **ContactNumber**: `user` (FK AUTH_USER_MODEL), `canonical_number` (E.164), `created_at`.
  `unique(user, canonical_number)`. Hard-delete on DELETE; scoped to `user`.
- **OrderNotification**: `order` (FK `order.Order`), `user` (FK), `kind`
  (placed/dispatched/dispatched_followup/cancelled/resend), `to_number`, `body` (nullable; cleared on
  redact), `provider_sid` (nullable), `provider_status` (raw string), `outcome`
  (sending/done/pending/failed/partial/unknown), `error_code` (nullable int), `is_followup` (bool),
  `canceled` (bool), `content_redacted` (bool), `scheduled_send_at` (nullable), `provider_time`
  (nullable datetime, from `date_sent`), `idempotency_key` (nullable; **`unique`**), `created_at`,
  `updated_at`. Scoped to `user`/order-owner.
- **OrderTransition** (dispatch/cancel claim): `order` (FK), `to_status`, `created_at`,
  `unique(order, to_status)`. The insert is the claim that gates side effects (no double send).

### Endpoints (all session-auth; operator = `is_staff`; CSRF-exempt JSON API + auth decorator)
- `POST /api/auth/login`, `POST /api/auth/logout` — convenience wrappers over
  `django.contrib.auth.authenticate/login/logout` (this **is** Django session login), so the flow is
  curl-drivable. Separate, small.
- `POST /api/contact-numbers` → lookup+validate; store canonical; return `contactNumberId`. reject
  unusable (400).
- `GET /api/contact-numbers` — caller's numbers.
- `DELETE /api/contact-numbers/{id}` — caller's only; hard-delete; also cancel any still-`scheduled`
  follow-ups addressed to that number (so nothing is sent to it again).
- `POST /api/orders` — body: `{items:[{productId,quantity}...]}`; build basket from caller's user,
  Free shipping, OrderCreator.place_order; message "order placed"; return `orderId` (order.number).
- `POST /api/orders/{orderId}/dispatch` — staff. Claim `OrderTransition(order,'dispatched')`;
  set order status 'Being processed'; message "on its way"; **schedule** follow-up (+3d) via messaging
  service; record both rows. Idempotent (claim gates).
- `POST /api/orders/{orderId}/cancel` — staff. Claim `OrderTransition(order,'cancelled')`; set status
  'Cancelled'; **cancel** any pending scheduled follow-up for the order at the provider
  (`update_message status=canceled`), mark row canceled; message "cancelled". Idempotent.
- `GET /api/my-orders` — caller's orders + each order's notifications' outcome summary.
- `GET /api/orders/{orderId}/notifications` — caller's order; list rows, each with `notificationId`,
  provider_sid, status, outcome (refreshing live status via `fetch_message` for non-terminal rows).
- `POST /api/notifications/{notificationId}/resend` — staff. Body carries caller idempotency key.
  Claim-first: insert new `OrderNotification(kind=resend, idempotency_key=key, outcome=sending)`;
  `unique(idempotency_key)` violation ⇒ return the existing row's `notificationId` (no 2nd send).
  Else re-send the original message's text to its number; record sid/status; return new `notificationId`.
- `DELETE /api/notifications/{notificationId}/content` — shopper owns it. `update_message(sid, body="")`
  (redact at provider); mark `content_redacted`, clear local `body`. Fact of sending + outcome survive.
- `GET /api/notifications/reconciliation?from=&to=` — staff. Provider list (from=FROM, widened days,
  narrowed in code) vs local rows (on `provider_time`); report matched / provider_only / local_only /
  unsettled; bounded pagination + truncation flag.

### "Send must never fail the operation"
Order placed/dispatched/cancelled: the DB state change commits first; messaging is attempted after and
wrapped so any Twilio failure is recorded on the `OrderNotification` row (outcome failed/unknown) but
the HTTP response still succeeds. A shopper with no `ContactNumber` is simply not messaged (no row or a
row with outcome `skipped`). Number never logged.

### Idempotency / races (python-configuration-resilience)
- dispatch/cancel: `OrderTransition` unique insert = the claim; side effects (status change, send,
  schedule/cancel-followup) gated on winning the insert. No-op transition fires nothing again.
- resend: `OrderNotification.idempotency_key` unique = claim; duplicate key ⇒ return existing, no 2nd send.
- reconciliation: provider event-clock (`date_sent`) on both sides; widen-then-narrow day window;
  match by `provider_sid` set.

## Assumptions & blockers
- **Blocker? No.** The only non-obvious call is Lookup: v2 is unusable (decode bug on live data); v1
  covers the requirement. Decided v1 — not a gap.
- CSRF-exempt on the JSON API (session auth still required) so the flow is curl-drivable; documented.
- orderId = Oscar `order.number` (unique, human id). Route `{orderId}` matches `number`.
- Follow-up delay = 3 days (within Twilio's 15min–7day scheduling window).
- Products passed as catalogue **product ids** that have a purchasable stockrecord; validated per item.
- `orders.json` seed fixture is skipped (references a missing stockrecord); app creates orders
  dynamically, so it is not needed.

## REQUIRED READING (loaded before implementing)
- MUST load `python-error-handling` — error boundary (loaded).
- MUST load `python-client-initialization` — client construction/lifetime (loaded).
- MUST load `python-configuration-resilience` — base-URL override, idempotency/claims, reconciliation,
  no-retries, pagination bounds (loaded).
- MUST load `python-calling-endpoints` — call shape, status-not-id (loaded).
- MUST load `python-models` — UNSET, open enums, OptionalNullable, RFC3339 datetime (loaded).
- MUST load `python-testing` — before the verification script/tests (load at that step).
