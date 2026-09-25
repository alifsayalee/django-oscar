# Twilio SDK plan — SMS order notifications for the django-oscar sandbox

Scope: `PHASE-BUILD.md` flows 1–3, delivered as a new Django app `sandbox/apps/order_sms/`, routed
under `/api/` from `sandbox/urls.py`. SDK: `twilio-sdk` 1.0.0 (import root `twilio_sdk`), installed
into `venv/` from `git+https://github.com/context-plugins/twilio-python-sdk.git@main`. Map read from a
`--branch main` clone outside the repo.

## Host decisions

| decision | value |
| --- | --- |
| sync vs async | **sync** — the sandbox is Django under WSGI (sync views). `TwilioSdkClient`; never `AsyncTwilioSdkClient`. |
| client lifetime | one module-level client, built lazily on first use (so after any worker fork), closed by `atexit`; never per request. |
| transport | `HttpxClient(timeout=10.0)` wrapped by a redacting logging transport, passed as `custom_http_client` (so the timeout is set on the transport). |
| auth | `account_sid_auth_token=BasicAuthCredentials(username=TWILIO_ACCOUNT_SID, password=TWILIO_AUTH_TOKEN)` — always set explicitly; client refuses to build when either is empty (the SDK would otherwise send unauthenticated). |
| servers | messaging calls use server `default` (`https://api.twilio.com`); `TWILIO_BASE_URL`, when set, is passed verbatim as `server_config={"default": {"base_url": TWILIO_BASE_URL}}`. Lookup uses `default4` (`https://lookups.twilio.com`) and is never overridden by `TWILIO_BASE_URL`. |
| retries | SDK performs none. Reads (`fetch_message`, `list_message`, lookup) get up to 3 attempts on transient failures. `create_message` is **never** retried blind — unknown outcomes go through the safe write's lookup. The two idempotent updates (cancel, redact) get up to 3 attempts, because repeating them cannot create anything. |
| storage for claims | the project's Django database (SQLite by default), via UNIQUE constraints on the notification table. |
| webhooks | none possible (no public URL): delivery outcome is always obtained with `fetch_message` / `list_message`. |

## Contract sheet (no open lookups)

All operations: sync parsed call raises `ApiError`; `.with_raw_response` returns `ApiResult`. Every
keyword-only parameter has a real default (`None`), so none is passed defensively. All in-scope
operations are **Case B**: `ApiError.error` is always `RawError` (`status_code`, `content`, `text()`,
`json()`). A decode failure raises `pydantic.ValidationError`/`ValueError` in both modes. httpx
transport exceptions arrive unwrapped.

| operation | server | signature (positional \| keyword-only) | returns | notes |
| --- | --- | --- | --- | --- |
| `client.lookups_v1_phone_number_api.fetch_phone_number2` | `default4` | `(phone_number: str, *, country_code: str \| None, type_: list[str] \| None, add_ons: list[str] \| None, add_ons_data: Any \| None, request_options)` | `LookupsV1PhoneNumber` | Route `GET /v1/PhoneNumbers/{PhoneNumber}`. Not-a-number answers **404** (`RawError`, code 20404) — verified live. Members used: `phone_number: OptionalNullable[str]` (canonical E.164), `country_code: OptionalNullable[str]`. Both must be checked `isinstance(str)` — a missing one is "unreadable", not "valid". |
| ~~`client.lookups_v2_phone_number.fetch_phone_number3`~~ | `default4` | — | `LookupResponse` | **Not usable**: live 200 bodies carry `null` for `caller_name`, `sim_swap`, … which the model types `Optional[...]` (non-nullable) ⇒ `ValidationError` on every call (verified live). v1 is used instead. |
| `client.api20100401_message.create_message` | `default` | `(account_sid: str, to: str, *, from_: str \| None (wire From), messaging_service_sid: str \| None, body: str \| None, schedule_type: MessageEnumScheduleTypeOrStr \| None, send_at: RFC3339DateTime \| None, …)` | `ApiV2010AccountMessage` | Route `POST /2010-04-01/Accounts/{AccountSid}/Messages.json`, form body. **No idempotency-key parameter.** Scheduling: `schedule_type=MessageEnumScheduleType.FIXED` + `send_at=<aware datetime>` + `messaging_service_sid` (docstring: "For Messaging Services only"); `from_` still passed so the message is sent from `TWILIO_FROM_NUMBER`. |
| `client.api20100401_message.fetch_message` | `default` | `(account_sid: str, sid: str, *, request_options)` | `ApiV2010AccountMessage` | 404 = provider has no such message. |
| `client.api20100401_message.list_message` | `default` | `(account_sid: str, *, to: str \| None (To), from_: str \| None (From), date_sent (DateSent), date_sent_query (wire DateSent<), date_sent_query_query (wire DateSent>), page_size: int \| None, page: int \| None, page_token: str \| None, request_options)` | `ListMessageResponse` | `messages: Optional[list[ApiV2010AccountMessage]]`, `next_page_uri: OptionalNullable[str]` (carries `Page` and `PageToken` query params for the next call). Date filters are `RFC3339DateTime` and are sent as a full timestamp; the docstring describes day granularity ⇒ widen to whole UTC days, narrow back in code. |
| `client.api20100401_message.update_message` | `default` | `(account_sid: str, sid: str, *, body: str \| None, status: MessageEnumUpdateStatusOrStr \| None, request_options)` | `ApiV2010AccountMessage` | Docstring: "used to redact Message body text and to cancel not-yet-sent messages"; `body=""` redacts; `status=MessageEnumUpdateStatus.CANCELED` cancels a scheduled message. |
| `client.api20100401_message.delete_message` | — | — | `None` | **Not used**: deleting would destroy the provider's record that the message was sent; disposal must keep it. |

`ApiV2010AccountMessage` members used (all optional ⇒ `UNSET` when absent): `sid: OptionalNullable[str]`,
`status: Optional[MessageEnumStatusOrStr]`, `body: OptionalNullable[str]`, `to: OptionalNullable[str]`,
`from_: OptionalNullable[str]` (wire `from`), `direction: Optional[MessageEnumDirectionOrStr]`,
`error_code: OptionalNullable[int]`, `error_message: OptionalNullable[str]`,
`date_sent` / `date_created: OptionalNullable[str]` (RFC 2822 strings, parsed with
`email.utils.parsedate_to_datetime`). **Required assertion after every create/update/fetch: `sid` is a
`str`** — a 2xx without it is an unknown outcome, never a success.

Enums (`twilio_sdk.models.enums`, open `…OrStr` — a value newer than the SDK arrives as `str`):

- `MessageEnumStatus`: `QUEUED`, `SENDING`, `SENT`, `FAILED`, `DELIVERED`, `UNDELIVERED`, `RECEIVING`,
  `RECEIVED`, `ACCEPTED`, `SCHEDULED`, `READ`, `PARTIALLY_DELIVERED`, `CANCELED`.
- `MessageEnumUpdateStatus`: `CANCELED`. `MessageEnumScheduleType`: `FIXED`.
- `MessageEnumDirection`: `INBOUND`, `OUTBOUND_API`, `OUTBOUND_CALL`, `OUTBOUND_REPLY`.

Core imports (`twilio_sdk.core`): `ApiError`, `RawError`, `BasicAuthCredentials`, `HttpxClient`,
`HttpRequest`, `HttpResponse`, `FormBody`, `UNSET`, `UnsetType`. Client: `twilio_sdk.TwilioSdkClient`.
Server config: `server_config={"default": {"base_url": ...}}` (frozen, `extra="forbid"`).

## OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| `create_message` (immediate: order placed / dispatched / cancelled / resend) | `ApiV2010AccountMessage.status` | **done**: `delivered`, `read`. **pending** (accepted, not finished — shown as in progress, refreshed by `fetch_message` on later reads): `accepted`, `queued`, `sending`, `sent`, `scheduled`, `partially_delivered`. **failed**: `failed`, `undelivered`, `canceled` (undone). **unknown** (neither; kept for refresh/operator): `receiving`, `received` (inbound-only states), any value the enum does not list, and an absent status. The order operation succeeds regardless; the notification record carries the outcome. | `provider.status_from_provider` (mapping); applied in `services._record_state` via `MessageState.outcome`; send in `provider.create_message` ← `services._send_claimed` |
| `create_message` (scheduled follow-up) | `ApiV2010AccountMessage.status` | Same mapping. `scheduled` is **pending** (queued with the provider, not delivered); `canceled` is **failed** for the delivery, and the record is additionally marked cancelled-by-us when our cancel did it. | `provider.create_message(send_at=...)` ← `services.dispatch_order` → `services.notify_order_event(..., send_at=...)`; outcome via `provider.status_from_provider` in `services._record_state` |
| `update_message(status=canceled)` (call off follow-up) | `ApiV2010AccountMessage.status` | This write asks for the undoing, so **done**: `canceled`. **pending**: `scheduled`, `accepted` (cancel not yet in effect ⇒ `cancel_state=pending`; the next `POST …/cancel` attempts it again). **failed** (too late — it went out or is going out): `queued`, `sending`, `sent`, `delivered`, `read`, `partially_delivered`, `failed`, `undelivered`. **unknown**: anything else / absent ⇒ `cancel_state=pending`, attempted again on the next cancel call. | `provider.cancel_outcome` (mapping) + `provider.cancel_message`, branched in `services.call_off` (sets `cancel_state`) |
| `update_message(body="")` (content disposal) | no status for this write — the echoed `body` | **done**: returned `body == ""`. **failed**: returned body is a non-empty string (not redacted) ⇒ 502 with `outcomeUnknown=false`, local text kept. **unknown**: `body` absent/`None`/unreadable ⇒ 502 with `outcomeUnknown=true`, local text kept until the provider confirms. A 4xx (message not yet final, e.g. still scheduled) ⇒ 409. | `services.dispose_content` (branches on the echoed `state.body`) → `provider.redact_message` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| order event SMS (`placed`, `dispatched`, `followup`, `cancelled`) — `create_message` | `Notification` row inserted **before** the provider call, `reference = "<SMS_REFERENCE_PREFIX>:order:<order.number>:<kind>"`, `outcome="sending"` | DB `UNIQUE(reference)` (Django `unique=True`) — an `IntegrityError` for any second insert, across processes | `IntegrityError` caught around the insert in the claim function; the second request answers from the stored record (in-flight ⇒ no provider call; stale `sending`/`unknown` ⇒ lookup only) | `services._claim` (insert, `IntegrityError` → existing) + `services._answer_repeat`; key from `services._event_reference`; constraint `Notification.reference unique=True` |
| operator resend — `create_message` | `Notification` row with `resend_of=<original>`, `idempotency_key=<caller key>`, `reference="<prefix>:resend:<original id>:<sha256(key)[:16]>"` | DB `UNIQUE(reference)` plus `UNIQUE(resend_of, idempotency_key)` | same claim function; the repeat returns the first resend's `notificationId` and outcome, with no second send | `services.resend` → `services._safe_send` → `services._claim`; key from `services._resend_reference`; constraint `order_sms_one_resend_per_key` |
| follow-up cancel — `update_message(status=canceled)` | not a create: repeating it cannot make a second message (cancelling a cancelled message is harmless) | none needed | — | none (idempotent by nature) |
| content disposal — `update_message(body="")` | not a create: repeating it is harmless | none needed | — | none (idempotent by nature) |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| `create_message` (all kinds) | **lookup** (kind 3 — Twilio offers no idempotency key and no reference field): every body ends with `Ref <token>`, `token = sha256(reference)[:10]`; the check is `list_message(to=<destination>, from_=TWILIO_FROM_NUMBER)` paged newest-first back to 1 day before the claim, matching the token in `body`. Found ⇒ settle from the provider's status; not found / check failed ⇒ stays `unknown` under the same reference (never re-sent under a new one). | the notification's `reference` (its `Ref` token in the body) — the same one the claim is keyed by | `services._settle_by_lookup` → `provider.find_by_reference`; token embedded by `services._message_body`; entered from `services._send_claimed` and `services._answer_repeat` |
| `update_message(status=canceled)` | re-read with `fetch_message(sid)` and map its status (the cancel row above) | provider `sid` | `services.call_off` (falls back to `provider.fetch_message` when the cancel's answer is missing) |
| `update_message(body="")` | re-read with `fetch_message(sid)`; `body == ""` ⇒ done | provider `sid` | `services.dispose_content` (falls back to `provider.fetch_message` when the redaction's answer is missing) |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| `create_message` (event + resend) | `Notification` claim: order, user, kind, destination (E.164), body text incl. `Ref` token, `reference`, `outcome="sending"`, `claimed_at` | provider `sid`, `provider_status`, `outcome` (mapped), `error_code`, `provider_time` (provider `date_sent` or else `date_created`), `scheduled_for` | `services._claim` (row before the call) → `services._send_claimed` → `services._record_state` (after) |
| `update_message(status=canceled)` | `Notification` with provider `sid` and `cancel_requested_at` set before the call | `provider_status`, `outcome`, `cancelled_by_us`, `cancel_state` (`done`/`pending`/`failed`) | `services.cancel_order` / `services.call_off` set `cancel_requested_at` before `provider.cancel_message`; `services._record_state(..., cancel_state=...)` after |
| `update_message(body="")` | `Notification` with provider `sid`; `content_disposal_requested_at` set before the call | local `body` cleared and `content_disposed_at` set only once the provider echoes `body == ""` | `services.dispose_content` sets `content_disposal_requested_at` before `provider.redact_message`; `services._clear_local_content` after |

## Reconciliation design

`GET /api/notifications/reconciliation?from&to` (staff): provider side = `list_message(from_=TWILIO_FROM_NUMBER, date_sent_query_query=<from, floored to UTC day>, date_sent_query=<to, next UTC day>)`, following `next_page_uri` (`Page`/`PageToken`) to the end, then narrowed in code to `from <= provider_time < to` using the provider's `date_sent` (else `date_created`). Local side = notifications whose stored `provider_time` is in the window (same clock), plus notifications with no provider time whose `claimed_at` is in the window (reported as **unsettled**, never dropped). Matching by provider `sid`. Local records not in the listing are re-checked with `fetch_message` (a scheduled message cancelled before sending has no `date_sent` and never appears in a DateSent listing). Report sections: `matched` (with status agreement), `appOnly`, `providerOnly`, `unsettled`, `inboundIgnored` (inbound copies with `From` = our number).

## Assumptions & Blockers

- **Minor** — Lookup v2 decoding is broken against live responses (see sheet); Lookup v1 gives the same "usable destination + canonical E.164" answer and decodes. No blocker.
- **Minor** — The follow-up delay defaults to 3 days (`ORDER_SMS_FOLLOWUP_DELAY_HOURS=72`), inside Twilio's scheduling window.
- **Minor** — A shopper may have several numbers; the most recently registered active one receives messages.
- **Minor** — Order dispatch uses a new `Dispatched` status added to the sandbox's `OSCAR_ORDER_STATUS_PIPELINE`, applied through Oscar's own `Order.set_status`.
- **Minor** — Sandbox fixtures on this commit load 11 products (5 stock records); `ranges.json` and `orders.json` fail on FK constraints on the untouched tree. Pre-existing; not touched.
- No blockers: the Django DB can hold a claim that outlives a process (UNIQUE constraints).

## REQUIRED READING

- Client construction/lifetime, sync client, transport ownership — MUST load `python-client-initialization` (loaded).
- Basic auth keyword, never-unauthenticated guard — MUST load `python-authentication`.
- Operation calls, status → outcome mapping, raw vs parsed — MUST load `python-calling-endpoints` (loaded).
- `UNSET` vs `None`, open enums, `OptionalNullable` reads — MUST load `python-models` (loaded).
- Error ladder, `RawError`, never-sent vs may-have-landed httpx split — MUST load `python-error-handling` (loaded).
- Safe write (claim/call/check/complete), no retries, reconciliation clocks, logging transport — MUST load `python-configuration-resilience` (loaded).
- Stub transport tests, both transport-failure inputs, same-operation-twice test — MUST load `python-testing` (loaded).

## Verification record (2026-09-25, live account)

- Unit: `python manage.py test apps.order_sms` — 27 tests, OK (stub transport; covers never-sent vs may-have-landed, same-operation-twice, repeat key vs fresh key, unreadable 2xx, base-URL override, ownership, staff gates).
- Types: `mypy --strict` clean on `provider.py` (the SDK boundary); remaining modules only report untyped-def notes (sandbox style).
- Live over HTTP: invalid number → 422; Canadian test number stored in canonical E.164; order placed → SMS `delivered`; dispatch → SMS `delivered` + follow-up `scheduled` at Twilio for +72h; cancel → follow-up `canceled` at Twilio (`cancelState=done`) and cancellation SMS delivered; US number → `undelivered` (30034) → resend under a key gave a new `notificationId`, the repeat of that key returned the same one with no second send; content disposal → Twilio `fetch_message` returns `body ''` with status still `delivered`; reconciliation over the day → 6 matched, 0 app-only, 0 provider-only, 3 inbound copies ignored. Server log contains neither phone number nor auth token.
