# Twilio SDK integration plan — SMS order notifications for the Oscar sandbox

Scope: new Django app `sandbox/apps/sms_notifications/` exposing `/api/...` on the sandbox project,
reusing Oscar's `Order` / `Line` / `Basket` / `Product` models. SDK: `twilio-sdk` 1.0.0 (import
root `twilio_sdk`), installed from `git+https://github.com/context-plugins/twilio-python-sdk.git@main`
into `venv/`.

## Decisions

| Topic | Decision |
| --- | --- |
| Sync vs async | **Sync** `TwilioSdkClient` — the sandbox is Django under WSGI. No `AsyncTwilioSdkClient` anywhere. |
| Client lifetime | One lazily-built, process-wide client (built on first use, i.e. after any fork), closed from `atexit`. Never built per request. |
| Credentials | `account_sid_auth_token=BasicAuthCredentials(username=settings.TWILIO_ACCOUNT_SID, password=settings.TWILIO_AUTH_TOKEN)`. The keyword is optional in the SDK — the factory refuses to build a client when either is empty (`TwilioNotConfigured`), so nothing ever goes out unauthenticated. |
| Servers | Messaging calls resolve against server `default` (`https://api.twilio.com`). When `settings.TWILIO_BASE_URL` is set, `server_config={"default": {"base_url": TWILIO_BASE_URL}}` (verbatim). Lookup resolves against `default4` (`https://lookups.twilio.com`) and is **not** governed by `TWILIO_BASE_URL`. Other servers untouched. |
| Timeout | Client `timeout=10.0`; no per-call overrides. |
| Retries | The SDK does none. **None added** for writes (a blind retry duplicates an SMS); reads are not retried either — a failed refresh just leaves the stored state as it was and is retried on the next GET. |
| Transport | `HttpxClient(timeout=10.0)` wrapped in a logging transport that logs method, host, status, duration only — never the path/query (they carry phone numbers) or headers (credential). |
| Store for claims | The project's own database (SQLite by default, via Django ORM). Unique constraint on `Notification.reference`; the claim row is committed in its own transaction **before** the provider call (views are `non_atomic_requests` because the sandbox sets `ATOMIC_REQUESTS=True`). |
| Canonical number | Lookup **v1** `fetch_phone_number2` (see Assumptions: v2 does not decode). Canonical = response `phone_number`; 404 = not a usable destination → 400. |
| Follow-up | `create_message(..., schedule_type="fixed", send_at=now+3 days, messaging_service_sid=..., from_=TWILIO_FROM_NUMBER)` — queued with the provider. Cancel via `update_message(sid, status="canceled")`. |
| Content disposal | `update_message(sid, body="")` (docstring: "To redact the text content of a Message, this parameter's value must be an empty string"); verified by the echoed `body == ""`. Local copy of the text cleared too; sid/status/error kept. |
| Reconciliation | `list_message(account, from_=TWILIO_FROM_NUMBER, date_sent_query_query=<widened start>, date_sent_query=<widened end>, page_size=1000)`, following `next_page_uri`'s `Page`/`PageToken` to the end; only `direction` outbound-*; narrowed in code to the caller's instants on the provider's clock. |
| Delivery status | No webhooks (no public URL). Status is refreshed by `fetch_message(sid)` for non-final notifications when a caller reads them. |

## Contract sheet (all lookups closed)

Every controller has an identical `Async…` peer; unused here. Every parameter after `*` is keyword-only
with a real default (`None`) — no defensive `None`s are passed. Every call ends with optional
`request_options` (`timeout`, `extra_headers`; `extra_headers` overrides endpoint headers).

| Operation | Server | Signature (positional \| keyword-only used) | Returns | Error |
| --- | --- | --- | --- | --- |
| `client.api20100401_message.create_message` | `default` | `(account_sid, to, *, schedule_type, send_at, from_, messaging_service_sid, body, request_options)` — wire: `To`, `ScheduleType`, `SendAt`, `From`, `MessagingServiceSid`, `Body` (form) | `ApiV2010AccountMessage` | Case B, `RawError` |
| `client.api20100401_message.fetch_message` | `default` | `(account_sid, sid, *, request_options)` | `ApiV2010AccountMessage` | Case B, `RawError` |
| `client.api20100401_message.update_message` | `default` | `(account_sid, sid, *, body, status)` — `status: MessageEnumUpdateStatusOrStr` | `ApiV2010AccountMessage` | Case B, `RawError` |
| `client.api20100401_message.list_message` | `default` | `(account_sid, *, to, from_, date_sent_query (wire DateSent<), date_sent_query_query (wire DateSent>), page_size, page, page_token)` | `ListMessageResponse` | Case B, `RawError` |
| `client.lookups_v1_phone_number_api.fetch_phone_number2` | `default4` | `(phone_number, *)` | `LookupsV1PhoneNumber` | Case B, `RawError` |

Notes per operation:

- `create_message`: the raw peer sends header `Idempotency-Key: uuid4()` per call; we override it via
  `request_options={"extra_headers": {"Idempotency-Key": <reference>}}` so every attempt carries the
  claim reference. Whether Twilio de-duplicates on it is **not** documented in the source → treated as
  not de-duplicating (`repeat_is_safe=False`). `send_at` is typed `RFC3339DateTime` (Annotated
  `datetime`) — pass an aware `datetime`. `schedule_type` is `MessageEnumScheduleType.FIXED` ("fixed");
  docstring: "For Messaging Services only" → `messaging_service_sid` required when scheduling; `from_`
  may be "a specific sender from your Sender Pool" (smoke: the FROM number is in the service's pool).
- `update_message`: `MessageEnumUpdateStatus` has one member, `CANCELED = "canceled"`.
- `list_message`: `page_size` max 1000. `ListMessageResponse` members used: `messages:
  Optional[list[ApiV2010AccountMessage]]`, `next_page_uri: OptionalNullable[str]`. Smoke showed the
  sender-filtered list also contains the **inbound** leg (direction `inbound`, status `received`) when
  the destination is a number on the same account, and scheduled/canceled messages with
  `date_sent=None` → filter `direction` to outbound values and narrow on the provider clock in code.
- `fetch_phone_number2`: `LookupsV1PhoneNumber.phone_number: OptionalNullable[str]`,
  `country_code: OptionalNullable[str]`. Smoke: valid CA/US numbers answer 200 with canonical E.164;
  unparseable input answers `404` (`code 20404`).

`ApiV2010AccountMessage` members read (all optional → UNSET/None must be handled; none required, so a
truncated 2xx decodes cleanly — we assert on `sid` and `status` ourselves):
`sid: OptionalNullable[str]`, `status: Optional[MessageEnumStatusOrStr]`,
`date_sent: OptionalNullable[str]` (RFC 1123, e.g. `Wed, 23 Sep 2026 20:22:47 +0000`),
`date_created: OptionalNullable[str]`, `error_code: OptionalNullable[int]`,
`error_message: OptionalNullable[str]`, `body: OptionalNullable[str]`,
`direction: Optional[MessageEnumDirectionOrStr]` (`inbound`, `outbound-api`, `outbound-call`,
`outbound-reply`), `to`, `from_` (wire `from`).

`MessageEnumStatus` members → outcome (`status_from_provider`), used for **create_message**,
**fetch_message** refreshes and reconciliation display:

| member | outcome |
| --- | --- |
| `DELIVERED` delivered, `READ` read | done |
| `QUEUED` queued, `SENDING` sending, `SENT` sent (carrier accepted, no receipt yet), `ACCEPTED` accepted, `SCHEDULED` scheduled | pending |
| `FAILED` failed, `UNDELIVERED` undelivered, `CANCELED` canceled (undone) | failed |
| `PARTIALLY_DELIVERED`, `RECEIVING`, `RECEIVED`, any unlisted value, UNSET/None | unknown (not-yet) |

Error handling facts: one exception type `ApiError` (`e.status_code`, `e.error` = `RawError` for every
in-scope op: `.text()`, `.json()` may raise `ValueError`). Decode failures raise
`pydantic.ValidationError`/`ValueError` in both modes. Transport errors are raw `httpx`: never-sent =
`ConnectError`, `ConnectTimeout`, `PoolTimeout`, `ProxyError`; may-have-landed = other
`httpx.RequestError`. Auth: basic, so no `OAuthProviderError` path; a 401/403 from Twilio is ours → 502.

## OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| `create_message` (immediate: placed / dispatched / cancelled / resend) | `ApiV2010AccountMessage.status` | done: delivered, read → outcome `done`. pending: queued, sending, sent, accepted, scheduled → `pending`, refreshed by `fetch_message` on later reads. failed: failed, undelivered, canceled → `failed` (error_code kept; resend allowed). unknown: partially_delivered, receiving, received, unlisted, absent → `unknown`, never done. The API call returning never marks anything done. | `sandbox/apps/sms_notifications/outcomes.py::status_from_provider`, applied in `sending.py::_first_send` (last line) and `sending.py::refresh_notification` |
| `create_message` (scheduled follow-up) | `ApiV2010AccountMessage.status` | same mapping; the expected answer is `scheduled` → `pending` (queued with provider, not done). | `outcomes.py::status_from_provider` via `sending.py::_first_send` ← `sending.py::safe_send` ← `services.py::_notify` (from `services.py::dispatch_order`) |
| `update_message(status="canceled")` (call off follow-up) | `ApiV2010AccountMessage.status` | this write asks for the undoing, so `canceled` = done → cancel_state `done`, notification outcome `failed` (never went out). scheduled/queued/accepted → cancel_state `pending` (not yet effective; re-attempted on the next cancel / number delete). sending/sent/delivered/read/undelivered/failed/partially_delivered → cancel_state `failed` (it already went out — reported as `allFollowupsCalledOff: false`). unlisted/absent → `unknown`. | `outcomes.py::cancel_outcome`, applied in `sending.py::cancel_scheduled` to the message returned by `sending.py::_request_cancel` |
| `update_message(body="")` (redact) | not a status — the echoed `body` | `""` → disposal done (local text cleared, `content_disposed_at` set). Anything else (incl. UNSET/None) → not confirmed: 504 with `outcomeUnknown: true`, local text kept so the operator can retry. | `sending.py::redact_content` (the `message.body != ''` check) with `sending.py::_request_redaction` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| order-event SMS (placed / dispatched / cancelled) per contact number | `Notification` row, `reference` = `"{INSTALL_PREFIX}:order:{order_id}:{kind}:{contact_id}"`, committed before the call (views are `non_atomic_requests`) | DB `UNIQUE` on `Notification.reference` → `IntegrityError` | `sending.py::try_claim` catches `IntegrityError` and returns the existing row; `sending.py::safe_send` then answers from it (in flight / settled) or only looks it up (stale / unknown). A `failed` claim with no sid is re-taken by one caller via a conditional `UPDATE` in `try_claim` | `sending.py::try_claim` ← `sending.py::safe_send` ← `services.py::_order_reference` + `services.py::_notify`; constraint `models.py::Notification.reference` (`unique=True`, migration `0001_initial`) |
| delivery follow-up schedule per contact number | same table, `reference` = `"{INSTALL_PREFIX}:order:{order_id}:delivery_followup:{contact_id}"` | same UNIQUE constraint | same | `sending.py::try_claim` ← `sending.py::safe_send` ← `services.py::_notify` ← `services.py::dispatch_order` |
| operator resend | same table, `reference` = `"{INSTALL_PREFIX}:resend:{notification_id}:{Idempotency-Key}"` | same UNIQUE constraint — a repeat under the same key answers from the stored row (same `notificationId`), a fresh key claims a new row | same | `sending.py::try_claim` ← `sending.py::safe_send` ← `services.py::resend_notification` (key read in `views.py::resend_notification`) |
| cancel follow-up (`update_message status=canceled`) | not a create/send — cancelling an already-canceled message is harmless and idempotent; no claim needed | n/a | n/a | `sending.py::cancel_scheduled` |
| redact (`update_message body=""`) | setting a field to a fixed value — harmless to repeat; no claim | n/a | n/a | `sending.py::redact_content` |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| every `create_message` (all kinds) | Lookup (kind 3): the reference token `ref <token>` is embedded in the message body; `list_message(to=dest, from_=FROM)` newest-first, up to 3 pages × 100, match body containing the token; found → settle from its status; not found / lookup fails → stays `unknown` (never `failed`, never re-sent under a new reference). Unknown or stale-`sending` claims are re-checked by the same lookup when the same action is repeated (re-POST of dispatch/cancel/resend with same key) and when a caller reads the notification. | `Notification.reference` → its body token `Notification.ref_token` (`sending.py::ref_token_for`) | `sending.py::find_by_ref` via `sending.py::_settle_by_lookup`, called from `sending.py::_first_send` (5xx / read timeout / unreadable 2xx / missing sid), `sending.py::safe_send` (stale or unknown claim) and `sending.py::refresh_notification` |
| `update_message(status=canceled)` | re-issue is safe (idempotent); on a refusal or lost answer the message is re-read (`fetch_message`) and the cancel state taken from its status; unreadable → `cancel_state=unknown`, never-sent → `pending`, both re-attempted on the next `POST .../cancel` or number delete | provider `sid` (found by the reference lookup first if unknown) | `sending.py::_request_cancel` + `sending.py::_cancel_state_without_sid`, in `sending.py::cancel_scheduled` |
| `update_message(body="")` | re-issue is safe; a 5xx / lost answer is checked by re-reading the message (`fetch_message`) — `body == ""` confirms; otherwise 504 `outcomeUnknown`, operator repeats DELETE | provider `sid` | `sending.py::_request_redaction` → `sending.py::_reread_after_redaction` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| order-event / follow-up / resend `create_message` | committed `Notification` row: reference, ref_token, order, user, contact, kind, body, send_at, outcome=`sending`, claimed_at; the order change itself committed in its own `transaction.atomic()` before any send | provider_sid, provider_status, outcome, error_code/message, provider_time (date_sent or date_created) | `sending.py::try_claim` (before) then `sending.py::_complete` (after); order commit in `services.py::place_order` / `services.py::dispatch_order` / `services.py::cancel_order` |
| cancel follow-up | the `Notification` with its provider sid; order already committed as `Cancelled` (or contact soft-deleted) | cancel_state, provider_status, outcome | `sending.py::cancel_scheduled` (state saved by its inner `record`), after the commit in `services.py::cancel_order` / `services.py::delete_contact`; post-send re-check in `services.py::dispatch_order` |
| redact | the `Notification` with its provider sid | content_disposed_at, body cleared, provider_status/outcome | `sending.py::redact_content` |

## Assumptions & Blockers

- **Minor (resolved): Lookup v2 (`lookups_v2_phone_number.fetch_phone_number3`) cannot be used** — the
  live 200 response carries `null` for `caller_name`, `sim_swap`, … which `LookupResponse` declares
  `Optional[...]` (non-nullable), so decoding raises `ValidationError` on every call. Lookup v1
  (`lookups_v1_phone_number_api.fetch_phone_number2`) decodes and gives the canonical form and a 404 for
  unusable input, so the capability is covered — not a gap.
- Minor: whether Twilio honours the SDK's `Idempotency-Key` header is not stated in the source; the design
  does not rely on it (DB claim + body-token lookup).
- Minor: US destinations are accepted then `undelivered` (error 30034 seen in smoke) — handled as outcome
  `failed`, not a gap.
- No blockers.

## REQUIRED READING

- Client construction and lifetime — MUST load `python-client-initialization` (loaded).
- Every `try/except` around SDK calls — MUST load `python-error-handling` (loaded).
- Safe write, retries, reconciliation, server config, logging transport — MUST load `python-configuration-resilience` (loaded).
- Call shapes, status-to-outcome mapping — MUST load `python-calling-endpoints` (loaded).
- `UNSET`/`OptionalNullable`, open enums — MUST load `python-models` (loaded).
- Tests with a stub transport — MUST load `python-testing` (loaded).
