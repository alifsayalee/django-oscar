# Twilio SDK plan — SMS order notifications for the django-oscar sandbox

Scope: a new Django app `sandbox/apps/sms_notifications` (label `sms_notifications`) exposing the
`/api/...` endpoints from the task, reusing Oscar's `Order`/`Line`/`Basket`/`Product` models.
SDK: `twilio-sdk` 1.0.0 (import root `twilio_sdk`), installed from
`git+file:///D:/APIMatic/sdk-regen/twilio-python-sdk.git@main` into `venv/` (Python 3.11).
Map read from the SDK repo root on branch `main` (same release as installed: 1.0.0).

## Assumptions & Blockers

- **BLOCKER (live account): every call with the supplied credentials answers `401`, code `20003`,
  "account … with status 4 is not active"** (verified with `fetch_account`, `fetch_phone_number2`,
  `list_phone_number`, `list_message` on 2026-09-29). Nobody can answer it (headless run), so:
  the integration is built in full, verified with fake-transport tests and end-to-end over HTTP
  against a local mock Twilio (scratch dir, outside the repo) through the base-URL overrides; the
  live verification steps are reported as not achievable with this account. Not an SDK gap.
- Minor: whether the provider honours an `Idempotency-Key` header is not stated by the source → not
  relied on (lookup kind 3 used instead); the SDK's random per-call key is overridden with our
  deterministic reference via `request_options.extra_headers` (harmless, never trusted).
- Minor: the send-time limits for `schedule_type=fixed` are not stated by the source → the follow-up
  delay is a setting (`SMS_FOLLOWUP_DELAY_HOURS`, default 72 h). Validation errors surface as `failed`.
- Found while bootstrapping: the task's fixture sequence yields 11 products and `orders.json` fails
  (FK to product 12/partner 3). The Makefile's `oscar_import_catalogue` of the three `books.*.csv`
  files is required before `orders.json` (then 209 products, 249 countries, 1 order).
- Minor: `DateSent<` / `DateSent>` are documented as day-granular (`YYYY-MM-DD`) while typed
  `RFC3339DateTime` → the provider query is widened to whole UTC days and narrowed back locally on
  the provider's own `date_sent`.
- Minor: `TWILIO_BASE_URL` governs only the messaging server (`default`). An additional optional
  setting `TWILIO_LOOKUPS_BASE_URL` (server `default5`) exists so the lookup host can be pointed at a
  proxy/mock too; unset = provider default.
- The store: the sandbox DB (SQLite by default, Postgres supported) with unique constraints; claims
  are rows committed **before** the provider call. `ATOMIC_REQUESTS=True` in the sandbox, so the
  API views are `non_atomic_requests` and manage their own transactions — otherwise a claim would
  not be visible to a concurrent request until the whole request committed.

## Decisions

- **Sync client** (`TwilioSdkClient`) — Django under WSGI. One process-wide client, built lazily on
  first use (after any fork), closed via `atexit`. `timeout=10.0`.
- **Auth**: `account_sid_auth_token=BasicAuthCredentials(username=TWILIO_ACCOUNT_SID, password=TWILIO_AUTH_TOKEN)`;
  construction fails fast (ImproperlyConfigured) if either setting is empty — never unauthenticated.
- **Servers**: `server_config={"default": {"base_url": TWILIO_BASE_URL}}` only when set (+ `default5`
  from `TWILIO_LOOKUPS_BASE_URL`); otherwise omitted → `https://api.twilio.com` / `https://lookups.twilio.com`.
- **Retries**: none added. Writes go through the safe write (claim → call → lookup); reads (refresh,
  reconciliation, lookup) are re-run on the caller's next request.
- **Sending**: every message is created with `from_=TWILIO_FROM_NUMBER` **and**
  `messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID` (docstring: a specific sender from the pool may
  be given) so reconciliation by `From` covers scheduled follow-ups too.
- **Reference**: `deterministic_ref` = `f"{SMS_REFERENCE_PREFIX}:{kind}:{ids…}"`; an 8-char base32
  digest of it is appended to the body as `Ref XXXXXXXX` — the only searchable field a Message has
  (lookup kind 3).
- **Which number**: the caller's most recently registered, not-deleted `ContactNumber`. None → the
  notification is recorded `skipped` (no provider call).

## Contract sheet

All operations: sync parsed form raises `ApiError` (from `twilio_sdk.core`); every one here is
**Case B** — `ApiError.error` is always `RawError` (`status_code`, `content`, `text()`, `json()` —
`json()` raises `ValueError` on non-JSON). Every keyword-only parameter has a real default (`None`);
nothing needs an explicit `None`. Trailing `request_options: RequestOptionsOrDict | None` —
keys `timeout`, `extra_headers` (wins over endpoint headers). No retries in the SDK.
Decode failure raises `pydantic.ValidationError`/`ValueError`, not `ApiError`, in both modes;
`httpx` transport exceptions arrive unwrapped.

| operation (accessor) | route / server | positional | keyword-only used (wire) | returns | error |
| --- | --- | --- | --- | --- | --- |
| `client.lookups_v2_phone_number.fetch_phone_number2` | `GET /v2/PhoneNumbers/{PhoneNumber}` / `default5` | `phone_number` (path `PhoneNumber`) | `country_code` (query `CountryCode`, used when the input is national format) | `LookupResponse` | `RawError` |
| `client.api20100401_message.create_message` | `POST /2010-04-01/Accounts/{AccountSid}/Messages.json` / `default` | `account_sid` (path), `to` (form `To`) | `from_` (form `From`), `messaging_service_sid` (form `MessagingServiceSid`), `body` (form `Body`), `schedule_type` (form `ScheduleType`, `MessageEnumScheduleType.FIXED`="fixed"), `send_at` (form `SendAt`, `RFC3339DateTime` = tz-aware `datetime`, dumped `…Z`) | `ApiV2010AccountMessage` | `RawError` |
| `client.api20100401_message.fetch_message` | `GET …/Messages/{Sid}.json` / `default` | `account_sid`, `sid` | — | `ApiV2010AccountMessage` | `RawError` |
| `client.api20100401_message.update_message` | `POST …/Messages/{Sid}.json` / `default` | `account_sid`, `sid` | `status` (form `Status`, `MessageEnumUpdateStatus.CANCELED`="canceled" — cancels a not-yet-sent message), `body` (form `Body`; `""` redacts the text) | `ApiV2010AccountMessage` | `RawError` |
| `client.api20100401_message.list_message` | `GET …/Messages.json` / `default` | `account_sid` | `to` (`To`), `from_` (`From`), `date_sent_query` (`DateSent<`, on-or-before), `date_sent_query_query` (`DateSent>`, on-or-after), `page_size` (`PageSize`, max 1000), `page_token` (`PageToken`, taken from `next_page_uri`'s query) | `ListMessageResponse` | `RawError` |

SDK-set header: `create_message` / `update_message` send `Idempotency-Key: uuid4()` per call; we pass
`request_options={"extra_headers": {"Idempotency-Key": ref}}` so repeats carry the same value.
`delete_message` (returns `None`) is **not used** — deleting would erase the provider's record that a
message was sent; redaction keeps it.

Models (all members optional — `UNSET` default; none required, so a truncated 2xx decodes cleanly and
the code asserts the members it uses):

- `ApiV2010AccountMessage` (`twilio_sdk.models`): `sid: OptionalNullable[str]`,
  `status: Optional[MessageEnumStatusOrStr]`, `body: OptionalNullable[str]`,
  `from_: OptionalNullable[str]` (wire `from`), `to: OptionalNullable[str]`,
  `date_sent`, `date_created`, `date_updated: OptionalNullable[str]` (RFC 2822 strings — parsed with
  `email.utils.parsedate_to_datetime`), `error_code: OptionalNullable[int]`,
  `error_message: OptionalNullable[str]`. **Asserted after create/update/fetch: `sid` and `status`
  set** — otherwise the outcome is `unknown`.
- `ListMessageResponse`: `messages: Optional[list[ApiV2010AccountMessage]]`,
  `next_page_uri: OptionalNullable[str]`.
- `LookupResponse`: `valid: Optional[bool]`, `phone_number: OptionalNullable[str]` (E.164 canonical),
  `country_code`, `validation_errors: Optional[list[ValidationErrorOrStr]]`
  (`TOO_SHORT`, `TOO_LONG`, `INVALID_BUT_POSSIBLE`, `INVALID_COUNTRY_CODE`, `INVALID_LENGTH`,
  `NOT_A_NUMBER`). Accept only `valid is True` **and** a set `phone_number`.
- `UNSET` from `twilio_sdk.core`; test with `isinstance(x, UnsetType)` to narrow.

Status enum `MessageEnumStatus` (`twilio_sdk.models.enums`, open: unknown values arrive as `str`) —
**`status_from_provider`** (send steps):

| member (wire) | outcome |
| --- | --- |
| `DELIVERED` (delivered), `READ` (read) | done |
| `QUEUED`, `SENDING`, `SENT`, `ACCEPTED`, `SCHEDULED` | pending (accepted/handed on, delivery not confirmed) |
| `FAILED`, `UNDELIVERED` | failed |
| `CANCELED` | failed (done-then-undone: the message will never arrive) |
| `PARTIALLY_DELIVERED` | failed (not fully in effect) |
| `RECEIVING`, `RECEIVED` (inbound), any unlisted value, `UNSET` | unknown |

**`cancel_outcome`** (follow-up call-off): `CANCELED` → done; `QUEUED`/`SCHEDULED`/`ACCEPTED` → pending
(not off yet); `SENDING`/`SENT`/`DELIVERED`/`READ`/`PARTIALLY_DELIVERED`/`FAILED`/`UNDELIVERED` → failed
(too late: it went out / was attempted); unlisted/UNSET → unknown; provider `404` (GONE) → done.

**`redact_outcome`** (content disposal): response `body == ""` → done; any non-empty body, UNSET body
→ unknown.

Base URL: `default` ← `TWILIO_BASE_URL` (verbatim) when set; `default5` ← `TWILIO_LOOKUPS_BASE_URL`.

## OPERATION OUTCOMES

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/orders` → `create_message` (placed) | `ApiV2010AccountMessage.status` | `status_from_provider`: delivered/read → done (`"outcome": "done"`); queued/sending/sent/accepted/scheduled → pending (`"pending"`); failed/undelivered/canceled/partially_delivered → failed (`"failed"`); receiving/received/unlisted/absent → unknown (`"unknown"`); no number → skipped (`"skipped_no_number"`). The order itself answers 201 whatever the message did (task mandate); each notification entry carries `answer()`'s label | `services.place_order` → `services.notify_safely` → `services.send_notification` (`safe_write.safe_write`, `outcome_of=gateway.status_from_provider`); label via `views.notification_json` → `views.answer` |
| `POST /api/orders/{id}/dispatch` → `create_message` (dispatched) and `create_message` with `schedule_type=fixed` (follow-up) | `status` of each | as above; a follow-up answering `scheduled` is pending (queued with the provider), never done | `services.dispatch_order` → `services.notify_safely` ×2 → `services.send_notification`; `gateway.create_message(send_at=…)`; label via `views.answer` |
| `POST /api/orders/{id}/cancel` → `create_message` (cancelled) and `update_message(status=canceled)` (follow-up call-off) | `status` of each | send: as above. call-off, `cancel_outcome`: canceled or provider 404 → done (`"callOff": "called_off"`); queued/scheduled/accepted → pending (`"pending"`); sending/sent/delivered/read/partially_delivered/failed/undelivered → failed (`"too_late"`); unlisted/absent → unknown (`"unknown"`); a claim still in flight → `"in_progress"` | `services.cancel_order` → `services.call_off_followup` (`safe_write.safe_write`, `outcome_of=gateway.cancel_outcome`, `on_refusal=LOOKUP_ON_REFUSAL`); label via `views.call_off_json` |
| `POST /api/notifications/{id}/resend` → `create_message` | `status` | `answer()`: done → 200, pending → 202 (`"pending"`), sending → 202 (`"in_progress"`), failed/needs_review → 409, unknown → 504; the body always carries `notificationId` | `services.resend` → `services.send_notification`; HTTP status from `views.answer` in `views.NotificationResendView.post` |
| `DELETE /api/notifications/{id}/content` → `update_message(body="")` | `body` of the answer | `redact_outcome`: `""` → done → 200 (`"contentDisposal": "done"`); any other/absent body → unknown → 504; provider 4xx refusal → 409 (claim released, may be retried); never reached the provider → 502 | `services.dispose_content` (`safe_write.safe_write`, `outcome_of=gateway.redact_outcome`); status via `views.answer` in `views.NotificationContentView.delete` |
| `DELETE /api/contact-numbers/{id}` → `update_message(status=canceled)` for each follow-up still queued to that number | `status` | `cancel_outcome` as in the cancel row, reported per follow-up in `followUpsCalledOff`; the number is removed locally regardless, and the send path never selects a removed number | `services.delete_contact_number` → `services.call_off_followup`; `services.send_notification` (removed-number guard); label via `views.call_off_json` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| send placed/dispatched/cancelled notification | `ProviderWrite` row, `reference` = `{prefix}:order:{order_pk}:{kind}:send` (the `Notification` itself is also unique on `{prefix}:order:{order_pk}:{kind}`) | DB `UNIQUE(reference)` → `IntegrityError` | `safe_write.try_claim` (`except IntegrityError` → claim lost → `safe_write.safe_write` answers from the stored row or looks up); `services._create_notification` for the notification row | `services.notify` → `services.send_notification` → `safe_write.safe_write` |
| schedule follow-up | `ProviderWrite` row `{prefix}:order:{order_pk}:followup:send` | `UNIQUE(reference)` | `safe_write.try_claim` | `services.dispatch_order` → `services.notify` → `safe_write.safe_write` |
| call off follow-up (order cancel, number delete) | `ProviderWrite` row `{followup.reference}:cancel` | `UNIQUE(reference)` | `safe_write.try_claim` | `services.call_off_followup` → `safe_write.safe_write` |
| resend | `Notification` row `UNIQUE(reference)`, reference `{prefix}:resend:{source_pk}:{sha256(Idempotency-Key)[:32]}`, and its `ProviderWrite` `…:send` | `UNIQUE(reference)` (both tables) | `services._create_notification` (`except IntegrityError` → the existing notification); `safe_write.try_claim` | `services.resend` → `services.send_notification` → `safe_write.safe_write` |
| redact content | `ProviderWrite` row `{notification.reference}:redact` | `UNIQUE(reference)` | `safe_write.try_claim` | `services.dispose_content` → `safe_write.safe_write` |

A `failed` claim with no `provider_id` (never sent / refused) is re-taken by an atomic conditional
`UPDATE … WHERE outcome='failed' AND provider_id=''` (row count decides the winner) — never a read
(`safe_write.try_claim`). The API views run outside `ATOMIC_REQUESTS` (`urls.api_view`) so the claim
row is committed before the provider call.

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| every `create_message` (placed, dispatched, cancelled, follow-up, resend) | lookup: by the stored sid when one is known (`fetch_message`), otherwise `list_message(to=…, from_=TWILIO_FROM_NUMBER)` newest-first, up to 3 pages × 100, matching the `Ref XXXXXXXX` token in `body`; never a second create (`repeat_is_safe=False`) | the notification reference (`safe_write.body_token` of it, carried in the body) | `safe_write.safe_write` step 3 calling `services._find_sent` → `gateway.find_message_by_token` / `services._fetch_or_none`; re-checked on later reads by `services.refresh_notification` (`lookup_only=True`) |
| `update_message(status=canceled)` call-off | same-reference resend (`repeat_is_safe=True`: cancelling a cancelled message cannot create anything), then lookup `fetch_message(sid)` (404 → GONE = off) | the record's own sid | `safe_write.safe_write` with `services.call_off_followup`'s `find` (`services._gone_or` + `gateway.fetch_message`) |
| `update_message(body="")` redaction | same-reference resend (idempotent), then `fetch_message(sid)` | the record's own sid | `safe_write.safe_write` with `services.dispose_content`'s `find` (`gateway.fetch_message`) |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| send (any kind, incl. resend) | `Notification` (order, user, contact, kind, body with ref token, reference) + `ProviderWrite(reference…:send, outcome=sending, claimed_at)` | `ProviderWrite.outcome/provider_id/provider_status/provider_time/error_*`; copied onto `Notification.outcome/provider_sid/provider_status/error_*/provider_sent_at` | `services._create_notification`, `safe_write.try_claim` (before); `safe_write.complete`, `services.apply_write` (after) |
| follow-up call-off | the follow-up `Notification` with its `provider_sid` + `ProviderWrite(…:cancel, sending)` | call-off outcome on the `ProviderWrite` and `Notification.call_off_outcome`; provider status onto the notification | `safe_write.try_claim` (before); `safe_write.complete`, `services._set_call_off` (after) — in `services.call_off_followup` |
| redaction | `Notification` + `ProviderWrite(…:redact, sending)` | redaction outcome; local body cleared + `content_redacted_at` only when done | `safe_write.try_claim` (before); `safe_write.complete`, `services._redacted_locally` (after) — in `services.dispose_content` |

## REQUIRED READING

- Client lifetime, sync choice, close obligation — MUST load `python-client-initialization` (loaded).
- Basic credentials, no-auth trap — MUST load `python-authentication` (loaded).
- Positional/keyword split, `request_options`, status-not-id — MUST load `python-calling-endpoints` (loaded).
- `UNSET`, open enums, wire alias `from` — MUST load `python-models` (loaded).
- Error ladder: `ApiError`/`RawError`, never-sent vs unknown transport failures, decode failures — MUST load `python-error-handling` (loaded).
- Safe write, no retries, reconciliation on provider clock, base-URL override — MUST load `python-configuration-resilience` (loaded).
- Stub transport tests, both transport-failure inputs, same-operation-twice — MUST load `python-testing` (loaded).
