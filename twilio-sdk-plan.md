# Twilio SDK integration plan — order SMS notifications (django-oscar sandbox)

Scope: a new Django app `sandbox/apps/sms_notifications` exposing `/api/...` endpoints for contact
numbers, order placement/dispatch/cancel with SMS notifications, operator resend, content disposal and
reconciliation. SDK: `twilio-sdk` 1.0.0 (import root `twilio_sdk`), installed non-editable into `venv/`
from the `main` branch source (fetched as a codeload tarball because `github.com` resets connections on
this host; same repository, same branch). Map read from that same source tree.

## Decisions

- **Sync client** (`TwilioSdkClient`). Host is Django under WSGI (`sandbox/wsgi.py`, `uwsgi.ini`), views
  are sync. One long-lived client per process, built lazily on first use (post-fork safe), closed via
  `atexit`. Never built per request.
- **Auth**: `account_sid_auth_token=BasicAuthCredentials(username=TWILIO_ACCOUNT_SID, password=TWILIO_AUTH_TOKEN)`.
  Both read from Django settings (env). Missing either → gateway reports "not configured"; no client is
  built unauthenticated.
- **Servers**: messaging calls (`api20100401_message.*`) resolve against server `default`
  (`https://api.twilio.com`). When `settings.TWILIO_BASE_URL` is set it is passed verbatim as
  `server_config={"default": {"base_url": TWILIO_BASE_URL}}`. Lookup (`lookups_v2_phone_number`) is on
  server `default4` and is **not** governed by `TWILIO_BASE_URL` (per task).
- **Timeout**: `timeout=10.0` on the client. No retries (SDK performs none; we add none for writes; reads
  are not retried either — a failed refresh just leaves stored state).
- **Logging**: `httpx`/`httpcore` loggers raised to WARNING in `sandbox/settings.py` (their INFO lines
  carry URLs containing phone numbers). App logs never include phone numbers, bodies or the auth token.
- **Transactions**: sandbox has `ATOMIC_REQUESTS=True`; all API views are `non_atomic_requests` and manage
  `transaction.atomic()` explicitly so the claim row is committed before any provider call.
- **Idempotency header**: create_message's raw layer sends `Idempotency-Key: uuid4()` per call. We override it
  via `request_options={"extra_headers": {"Idempotency-Key": str(uuid5(NS, reference))}}` so every attempt for
  one reference carries the same value. Whether Twilio de-duplicates on it is **not documented** in the SDK →
  `repeat_is_safe=False`; the claim + lookup is what we rely on.

## Contract sheet

All operations: `client.<controller>.<op>(positional..., *, keyword-only...)`; every keyword-only param has a
real default (`None`) — pass only what is used, never defensive `None`s. Async twins exist (`AsyncTwilioSdkClient`),
not used. Every op ends with keyword-only `request_options: RequestOptionsOrDict | None` (keys exactly `timeout`,
`extra_headers`; `extra_headers` wins over the endpoint's own headers; header names lowercased).

### 1. `client.api20100401_message.create_message` — server `default`
- `POST /2010-04-01/Accounts/{AccountSid}/Messages.json`, form body.
- `def create_message(account_sid: str, to: str, *, ..., schedule_type: MessageEnumScheduleTypeOrStr | None = None, send_at: RFC3339DateTime | None = None, from_: str | None = None, messaging_service_sid: str | None = None, body: str | None = None, request_options: RequestOptionsOrDict | None = None, ...)`
  - positional: `account_sid`, `to`. Used keywords: `from_` (wire `From`), `body` (`Body`), and for the
    follow-up `messaging_service_sid` (`MessagingServiceSid`), `schedule_type` (`ScheduleType`), `send_at` (`SendAt`).
  - Docstring: `schedule_type` — "For Messaging Services only: value `fixed` in conjunction with the send time";
    `from_` may be given together with `messaging_service_sid` to pick a specific pool sender.
- Returns parsed `ApiV2010AccountMessage`; raw `ApiResult[ApiV2010AccountMessage, RawError]`.
- Error: **Case B** — `ApiError.error` is always `RawError`.
- Enum `MessageEnumScheduleType` (`twilio_sdk.models.enums`): `FIXED = "fixed"` (only member).
- `send_at: RFC3339DateTime` = `Annotated[datetime]`; aware datetimes serialize as `...Z` UTC.

### 2. `client.api20100401_message.fetch_message` — server `default`
- `GET /2010-04-01/Accounts/{AccountSid}/Messages/{Sid}.json`
- `def fetch_message(account_sid: str, sid: str, *, request_options=None) -> ApiV2010AccountMessage`; Case B.

### 3. `client.api20100401_message.update_message` — server `default`
- `POST /2010-04-01/Accounts/{AccountSid}/Messages/{Sid}.json`, form body.
- `def update_message(account_sid: str, sid: str, *, body: str | None = None, status: MessageEnumUpdateStatusOrStr | None = None, request_options=None) -> ApiV2010AccountMessage`; Case B.
- Docstring: "used to redact Message body text and to cancel not-yet-sent messages"; "To redact the text
  content of a Message, this parameter's value must be an empty string".
- Enum `MessageEnumUpdateStatus`: `CANCELED = "canceled"` (only member).

### 4. `client.api20100401_message.list_message` — server `default`
- `GET /2010-04-01/Accounts/{AccountSid}/Messages.json`
- `def list_message(account_sid: str, *, to: str | None = None, from_: str | None = None, date_sent: RFC3339DateTime | None = None, date_sent_query: RFC3339DateTime | None = None, date_sent_query_query: RFC3339DateTime | None = None, page_size: int | None = None, page: int | None = None, page_token: str | None = None, request_options=None) -> ListMessageResponse`; Case B.
- Wire names: `to`→`To`, `from_`→`From`, `date_sent_query`→**`DateSent<`**, `date_sent_query_query`→**`DateSent>`**,
  `page_size`→`PageSize` (max 1000), `page`→`Page`, `page_token`→`PageToken`.
- Docstring: date filters are GMT, whole-day granularity (`YYYY-MM-DD`, on-and-before / on-and-after).
- `ListMessageResponse` members (all optional, `UNSET` default): `messages: Optional[list[ApiV2010AccountMessage]]`,
  `next_page_uri: OptionalNullable[str]`, `page: Optional[int]`, `page_size`, `first_page_uri`, `previous_page_uri`,
  `start`, `end`, `uri`. Paging: follow `next_page_uri` → parse its `Page`/`PageToken` query params into the next call.

### 5. `client.lookups_v2_phone_number.fetch_phone_number3` — server `default4` (not governed by TWILIO_BASE_URL)
- `GET /v2/PhoneNumbers/{PhoneNumber}`
- `def fetch_phone_number3(phone_number: str, *, fields: str | None = None, country_code: str | None = None, ..., request_options=None) -> LookupResponse`; Case B.
- Used without `fields` (basic validation only; no paid packages).
- `LookupResponse` members used: `valid: Optional[bool]`, `phone_number: OptionalNullable[str]` (E.164 canonical),
  `validation_errors: Optional[list[ValidationErrorOrStr]]`, `country_code: OptionalNullable[str]`.
  Trap: `phone_number_quality_score`, `pre_fill` are `Optional[Any]` — never `to_dict()` this model.

### `ApiV2010AccountMessage` (twilio_sdk/models/api_v2010_account_message.py) — members read
- `sid: OptionalNullable[str]` · `status: Optional[MessageEnumStatusOrStr]` · `body: OptionalNullable[str]` ·
  `to: OptionalNullable[str]` · `from_: OptionalNullable[str]` (wire `from`) · `date_sent: OptionalNullable[str]` ·
  `date_created: OptionalNullable[str]` · `error_code: OptionalNullable[int]` · `messaging_service_sid: OptionalNullable[str]`.
  Dates are plain `str` (not converted) — parsed by us (RFC 2822 via `email.utils.parsedate_to_datetime`, ISO fallback);
  unparseable → no provider time.
- **No member is required** → a truncated 2xx decodes cleanly. After every write we assert `sid` and `status` are set
  (not `UNSET`/`None`); absent → treated as unreadable → outcome unknown → lookup by reference.
- Trap: `subresource_uris: OptionalNullable[Any]` → never `to_dict()` a message; map members explicitly.
- `MessageEnumStatus` members (open enum; unknown string passes through as `str`):
  `QUEUED, SENDING, SENT, FAILED, DELIVERED, UNDELIVERED, RECEIVING, RECEIVED, ACCEPTED, SCHEDULED, READ, PARTIALLY_DELIVERED, CANCELED`.

### Errors, response modes, failure kinds
- All five ops are Case B: `ApiError.error` is `RawError` (`status_code`, `content`, `text()`, `json()` — json may raise ValueError).
- Parsed mode everywhere (we need the payload; no op here returns `None`).
- Not `ApiError`: `pydantic.ValidationError`/`ValueError` on decode (both modes) → unreadable; `httpx` exceptions unwrapped:
  never-sent = `ConnectError, ConnectTimeout, PoolTimeout, ProxyError`; may-have-landed = other `httpx.RequestError`.
- 401/403 from Twilio = our credentials/account → our 502 (config); 429 → our 503; other 4xx → rejection; 5xx → may have landed on a write.

## OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| create_message (immediate: placed / dispatched / cancelled / resend) | `ApiV2010AccountMessage.status` (`MessageEnumStatus`) | **done**: `delivered`, `read`. **pending** (not finished): `accepted`, `queued`, `sending`, `sent` (handed to carrier, no delivery receipt yet), `scheduled`, `partially_delivered` (not settled by source → not-yet). **failed**: `failed`, `undelivered`, `canceled` (undone). **unknown** (not-yet, never done): `receiving`, `received` (inbound values, meaningless for an outbound send), any unlisted string, `UNSET`/`None`. Stored as `outcome` + raw `provider_status`; API answers "done" only from the done set. | `outcomes.status_from_provider`, applied by `safe_write.safe_write` → `ClaimStore.complete` (`claims.py`) at send time and by `NotificationService.refresh` / `_resolve_unknown` / `reconcile` (`services.py`) on read-back; API `delivered` = `outcome == done` in `services.notification_json` |
| create_message (scheduled delivery follow-up) | same | same mapping: `scheduled` = pending (queued with provider, not sent); `delivered`/`read` = done; `canceled` = failed for the send (undone) — the cancel row below records the cancel itself. | `outcomes.status_from_provider` via `safe_write.safe_write`, from `NotificationService.schedule_followup` → `notify` → `_send` (`services.py`) |
| update_message status=canceled (call off follow-up) | `ApiV2010AccountMessage.status` | **done** (the undoing is what we asked for): `canceled`. **failed** (it already went out — cancel impossible): `sending`, `sent`, `delivered`, `read`, `undelivered`, `failed`, `partially_delivered` → cancel_outcome `failed`, flagged. **pending**: `scheduled`, `accepted`, `queued` (cancel not yet reflected). **unknown**: anything else/`UNSET`. | `outcomes.cancel_outcome_from_provider`, applied in `NotificationService._call_off` → `_set_cancel` (`services.py`) |
| update_message body="" (content disposal) | `ApiV2010AccountMessage.body` (no status for this write) | **done**: `body == ""`. **unknown**: `UNSET`/`None`/non-empty → re-check via fetch_message; still non-empty → `unknown`, reported 502/504, local text kept flagged. | `outcomes.redaction_outcome`, applied in `NotificationService._redact_at_provider`, answered by `NotificationService.dispose_content` (`services.py`) |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| create_message — order placed | `SmsNotification` row, `reference = "{prefix}:order:{order.number}:placed"` in SQLite (Django DB) | `UNIQUE(reference)` constraint → `IntegrityError` | `ClaimStore.try_claim` (`claims.py`) catches IntegrityError → returns loser path | `ClaimStore.try_claim` (`claims.py`), first step of `safe_write.safe_write`; entry `NotificationService.notify` → `_send` (`services.py`) from `views.orders` |
| create_message — dispatched | same table, `...:{order.number}:dispatched` | same UNIQUE constraint | same | `NotificationService.notify` → `_send` → `safe_write.safe_write` → `ClaimStore.try_claim`, from `views.order_dispatch` |
| create_message — scheduled follow-up | same table, `...:{order.number}:delivery_followup` | same UNIQUE constraint | same | `NotificationService.schedule_followup` → `notify` → `_send` → `safe_write.safe_write` → `ClaimStore.try_claim` |
| create_message — cancelled | same table, `...:{order.number}:cancelled` | same UNIQUE constraint | same | `NotificationService.notify` → `_send` → `safe_write.safe_write` → `ClaimStore.try_claim`, from `views.order_cancel` |
| create_message — operator resend | same table, `"{prefix}:resend:{notification.id}:{sha256(idempotency_key)[:32]}"` — same key = repeat, fresh key = new message | same UNIQUE constraint | same | `services.resend_reference` + `NotificationService.resend` → `_send` → `safe_write.safe_write` → `ClaimStore.try_claim`; repeat answered 200 from the stored record, surfaced by `views.notification_resend` |
| update_message status=canceled | none needed: cancelling an already-cancelled message is a fixed-value update, harmless to repeat; outcome stored on the follow-up row (`cancel_outcome`) | n/a (not a create/charge/send) | n/a | `NotificationService._call_off` (returns early when `cancel_outcome == done`) |
| update_message body="" | none needed: setting body to a fixed value `""` is harmless to repeat | n/a | n/a | `NotificationService.dispose_content` (returns early when `content_redacted_at` is set) |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| create_message (all five kinds above) | lookup (kind 3 — Twilio offers no client-reference field): `list_message(account_sid, to=<destination>)` newest pages (≤3 pages × 50), match a message whose `body` contains the reference token `Ref <token>` we embed in every body; found → settle from its status; not found / check fails → stays `unknown` under the same reference (never resent under a new one). Re-checked on later GETs (`refresh`). | `reference` (token = first 10 base32 chars of sha256(reference)), the same value the claim row holds | `gateway.TwilioGateway.find_message_by_token` (token: `gateway.reference_token`, embedded by `gateway.with_reference`) passed as `find` by `NotificationService._send` into `safe_write.safe_write`; later re-checks in `NotificationService._resolve_unknown` (from `refresh`, `_call_off`, `dispose_content`) |
| update_message status=canceled | lookup: `fetch_message(sid)`; `canceled` → done | the follow-up's `provider_sid` (claim row) | `NotificationService._call_off` (fetch_message after a refused/unreadable cancel) |
| update_message body="" | lookup: `fetch_message(sid)`; `body == ""` → done | the notification's `provider_sid` | `NotificationService._redact_at_provider` (fetch_message after a refused-by-5xx/unreadable redaction) |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| create_message (placed/dispatched/cancelled/follow-up/resend) | committed `SmsNotification` row: `reference`, `kind`, `order`, `to_number`, `body`, `outcome="sending"`, `claimed_at` | `provider_sid`, `provider_status`, `outcome` (done/pending/failed/unknown), `error_code`, `provider_date_sent`/`provider_date_created`, `scheduled_for` | `ClaimStore.try_claim` / `ClaimStore.complete` + `claims.apply_snapshot` (`claims.py`), sequenced by `safe_write.safe_write`; views wrapped in `transaction.non_atomic_requests` by `views.api` so the claim commits first |
| update_message status=canceled | follow-up row with `provider_sid`; `cancel_requested_at` set and committed | `cancel_outcome`, `provider_status`, `outcome` (canceled → failed for the send) | `NotificationService._call_off` (saves `cancel_requested_at` before the call) → `_set_cancel` |
| update_message body="" | notification row with `provider_sid`; `redaction_requested_at` committed | `content_redacted_at`, local `body` blanked, `redaction_outcome` | `NotificationService.dispose_content` (saves `redaction_requested_at` before `_redact_at_provider`) |

## Reconciliation design
- Provider side: `list_message(account_sid, from_=TWILIO_FROM_NUMBER, date_sent_query_query=<from day 00:00Z>, date_sent_query=<(to + 1 day) day 00:00Z>, page_size=1000)`, following `next_page_uri` to exhaustion (whole range). Widened to whole days, narrowed back in code to `from <= date_sent < to` on the provider's `date_sent`.
- Local side on the same clock: our rows whose stored `provider_date_sent` is in the window; rows with no provider time yet but created in the window reported separately as `unsettled`.
- Matched by provider SID (set-based). Report: `matched`, `providerOnly`, `localOnly`, `unsettled`, plus per-item status agreement.

## Implementation notes (post-build)
- Deleting a contact number calls off follow-ups queued for it (`NotificationService.cancel_followups_to_contact`, from `views.contact_number_detail`); a record whose number was deleted is never re-sent (`notify`, `resend`).
- Cancel-vs-dispatch race: `schedule_followup` re-reads the order after its create and calls off if cancelled; `views.order_cancel` calls off before notifying. Test: `test_a_cancel_that_lands_while_the_followup_is_being_queued_still_calls_it_off`.
- Retries: none added (deliberate). Unsettled records are re-checked on later GETs.
- Type check: `mypy --strict` (django-stubs plugin) clean over `apps.sms_notifications`; 43 pytest tests at the SDK transport seam.

## Assumptions & Blockers
- **BLOCKER (environment, not SDK)**: the supplied Twilio account answers every call with HTTP 401 code 20003
  "account … with status 4 is not active" (verified via the SDK and plain curl on 2026-09-25). No live send, schedule,
  cancel, resend or reconciliation can succeed against it. Headless → proceed: build fully, verify with a stub transport
  at the SDK seam + the running sandbox (where every provider call is refused and the app must still succeed), and report.
- UNVERIFIED (would be settled by a live call): whether `DateSent>`/`DateSent<` accept the full RFC3339 datetime the SDK
  serializes (docstring shows `YYYY-MM-DD`); we send midnight-UTC datetimes.
- UNVERIFIED: whether Twilio honours `Idempotency-Key` on create — not relied upon.
- Minor: follow-up delay configurable (`SMS_FOLLOWUP_DELAY_HOURS`, default 72).

## REQUIRED READING
- MUST load `python-client-initialization` — lazy per-process sync client, close obligation. (loaded)
- MUST load `python-authentication` — Basic credentials keyword optional/silent. (loaded)
- MUST load `python-calling-endpoints` — keyword-only boundary, status-not-id outcomes. (loaded)
- MUST load `python-models` — `Optional` ≠ `typing.Optional`, `UNSET`, open enums, `Optional[Any]` to_dict trap. (loaded)
- MUST load `python-error-handling` — Case B RawError, decode failures, httpx never-sent vs unknown split. (loaded)
- MUST load `python-configuration-resilience` — safe write, server_config override, reconciliation clocks. (loaded)
- MUST load `python-testing` — stub transport seam, lowercase headers, FormBody fields. (loaded)
