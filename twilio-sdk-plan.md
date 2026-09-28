# Twilio SDK integration plan — order SMS notifications (django-oscar sandbox)

## Scope

New Django app `sandbox/apps/sms_notifications/` (label `sms_notifications`), routed under `/api/`
from `sandbox/urls.py`. Reuses Oscar's `order.Order` / `order.Line` / `basket.Basket` /
`order.ShippingEvent` models and `OrderCreator`. Twilio SDK: distribution `twilio-sdk` 1.0.0
(installed from `git+https://github.com/context-plugins/twilio-python-sdk.git@main`), import root
`twilio_sdk`.

## Repo survey

| Convention | Exemplar |
| --- | --- |
| Sandbox-local apps live in `sandbox/apps/<name>` and are imported as `apps.<name>` | `sandbox/apps/sitemaps.py`, `sandbox/urls.py` (`from apps.sitemaps import …`) |
| Settings read from env via `os.environ.get` / `environ.Env` | `sandbox/settings.py` (`DATABASES`, `THUMBNAIL_KVSTORE`) |
| Order status machine is `OSCAR_ORDER_STATUS_PIPELINE` + `order.set_status()` | `sandbox/settings.py`, `src/oscar/apps/order/abstract_models.py` |
| Order placement goes through `OrderCreator().place_order(...)` | `src/oscar/test/factories/__init__.py::create_order` |
| App models use `get_model` for Oscar models | `src/oscar/apps/order/utils.py` |

- Host is **Django under WSGI, sync views** → **sync client** (`TwilioSdkClient`).
- Toolchain: `pip` + `venv` at `repo/venv` (Python 3.11). Tests: `venv\Scripts\python sandbox\manage.py test apps.sms_notifications`
  (Django test runner, sandbox settings). Type check: `mypy --strict` over the provider layer
  (`twilio_gateway.py`, `safe_write.py`) with `django-stubs` installed.
- Baseline: no existing tests for sandbox apps; Oscar's own suite (`tests/`) is not touched by this change.

## Credentials / environment verification

- Settings (all read from env in `sandbox/settings.py`, no values in repo): `TWILIO_ACCOUNT_SID`,
  `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `TWILIO_MESSAGING_SERVICE_SID`, `TWILIO_BASE_URL` (optional).
- Server selection: messaging ops (`api20100401_message.*`) resolve against server `default`
  (`https://api.twilio.com`); when `TWILIO_BASE_URL` is set it is passed verbatim as
  `server_config={"default": {"base_url": TWILIO_BASE_URL}}`. Lookups (`lookups_v2_phone_number`)
  resolve against `default4` (`https://lookups.twilio.com`) and are NOT governed by `TWILIO_BASE_URL`.
- Read-only smoke (2026-09-28, scratchpad): `fetch_phone_number3`, `list_message`, `fetch_account`
  all answered **401 code 20003 "account … with status 4 is not active"**. The credentials reach
  Twilio; the account itself is inactive. See Blockers.

## Contract sheet

Sync vs async: **sync** `TwilioSdkClient` (alias `Client`), one module-level lazily-built instance per
process (built on first use, i.e. after any fork), closed via `atexit` → `client.close()`. Never
mixed with `AsyncTwilioSdkClient`. Async twins exist for every operation with identical parameters —
not used.

Construction (keyword-only, all optional): `TwilioSdkClient(server_config=…, timeout=10.0,
custom_http_client=LoggingTransport(HttpxClient(timeout=10.0)), account_sid_auth_token=BasicAuthCredentials(username=ACCOUNT_SID, password=AUTH_TOKEN))`.
`account_sid_auth_token` omitted ⇒ unauthenticated requests, no error — we fail fast at startup of
the client if either setting is empty. `timeout` must be > 0 (`ValueError`). Because we pass our own
transport, the timeout is set on `HttpxClient(timeout=…)` (client `timeout=` does not reach the wire).

Keyword-only boundary: everything after `*` has a real default (`None`); no defensive `None`s are
written. All ops below are **Case B** — `ApiError.error` is always `RawError` (`status_code`,
`content`, `text()`, `json()` — `json()` raises `ValueError` on non-JSON). No typed error arms.

No retries in the SDK. We add none on writes; reads are not retried either (a failed read is reported).
A decode failure raises `pydantic.ValidationError`/`ValueError` in both response modes; transport
errors arrive as raw `httpx` exceptions.

The SDK injects a random `Idempotency-Key: uuid4()` header on `create_message` / `update_message` /
`delete_message` — per call, therefore **no dedupe across attempts**; we do not rely on it.

| Operation | Server | Signature (positional ⏐ keyword-only used) | Returns | Members asserted |
| --- | --- | --- | --- | --- |
| `client.lookups_v2_phone_number.fetch_phone_number3` | `default4` | `(phone_number: str, *, …)` — no keywords used (basic validation is returned without `fields`) | `LookupResponse` | `valid: Optional[bool]` (must be `True`), `phone_number: OptionalNullable[str]` (E.164 canonical form — stored), `country_code: OptionalNullable[str]`, `validation_errors: Optional[list[ValidationErrorOrStr]]` (TOO_SHORT, TOO_LONG, INVALID_BUT_POSSIBLE, INVALID_COUNTRY_CODE, INVALID_LENGTH, NOT_A_NUMBER) |
| `client.api20100401_message.create_message` | `default` | `(account_sid: str, to: str, *, from_=…, body=…, messaging_service_sid=…, schedule_type=…, send_at=…)` wire: `To`, `From`, `Body`, `MessagingServiceSid`, `ScheduleType`, `SendAt` (form fields) | `ApiV2010AccountMessage` | `sid: OptionalNullable[str]` (absent ⇒ outcome unknown), `status: Optional[MessageEnumStatusOrStr]` (absent ⇒ unknown), `date_sent`/`date_created: OptionalNullable[str]` RFC 2822, `error_code: OptionalNullable[int]`, `body` |
| `client.api20100401_message.fetch_message` | `default` | `(account_sid: str, sid: str, *)` | `ApiV2010AccountMessage` | same as above; 404 ⇒ gone |
| `client.api20100401_message.update_message` | `default` | `(account_sid: str, sid: str, *, body: str \| None, status: MessageEnumUpdateStatusOrStr \| None)` — `status=MessageEnumUpdateStatus.CANCELED` cancels a not-yet-sent (scheduled) message; `body=""` redacts the stored text (docstring: "To redact the text content of a Message, this parameter's value must be an empty string") | `ApiV2010AccountMessage` | `status`, `body` |
| `client.api20100401_message.list_message` | `default` | `(account_sid: str, *, to=…, from_=…, date_sent_query=… (wire `DateSent<`), date_sent_query_query=… (wire `DateSent>`), page_size=…, page=…, page_token=…)`; dates typed `RFC3339DateTime` (aware `datetime`), docstring: GMT-date granularity | `ListMessageResponse` | `messages: Optional[list[ApiV2010AccountMessage]]`, `next_page_uri: OptionalNullable[str]` (carries `PageToken`/`Page` for the next page) |

Scheduling: `schedule_type=MessageEnumScheduleType.FIXED` ("For Messaging Services only … in
conjunction with the send time") + `send_at` (aware datetime, ISO 8601) + `messaging_service_sid`.
`from_` is sent too (docstring: "you can provide a specific sender from your Sender Pool") so every
message this app sends carries `From = TWILIO_FROM_NUMBER`, which is what reconciliation filters on.

Enums (`twilio_sdk.models.enums`):
- `MessageEnumStatus`: QUEUED, SENDING, SENT, FAILED, DELIVERED, UNDELIVERED, RECEIVING, RECEIVED,
  ACCEPTED, SCHEDULED, READ, PARTIALLY_DELIVERED, CANCELED (open alias `MessageEnumStatusOrStr` ⇒ an
  unknown wire value arrives as plain `str`).
- `MessageEnumUpdateStatus`: CANCELED. `MessageEnumScheduleType`: FIXED.

Model notes: `ApiV2010AccountMessage.from_` wire alias `from`. All response members are
`Optional`/`OptionalNullable` (UNSET when absent) — narrowed with `isinstance(x, UnsetType)`.
Date strings are RFC 2822 → parsed with `email.utils.parsedate_to_datetime`.

## OPERATION OUTCOMES

`status_from_provider` (send): DELIVERED, READ → done · ACCEPTED, SCHEDULED, QUEUED, SENDING, SENT,
PARTIALLY_DELIVERED → pending · FAILED, UNDELIVERED → failed · CANCELED → failed (undone) ·
RECEIVING, RECEIVED, unlisted str, absent → unknown.
`cancel_outcome` (call-off): CANCELED or record gone (404) → done · SENDING, SENT, DELIVERED, READ,
PARTIALLY_DELIVERED, FAILED, UNDELIVERED, QUEUED → failed (too late — it left the schedule) ·
anything else (SCHEDULED, ACCEPTED, unlisted, absent) → unknown.
`redact_outcome` (content disposal): `body` read back as `""` / null → done · record gone (404) → done ·
body still non-empty → failed · body absent (UNSET) → unknown.
`answer`: done → 200 · pending/sending → 202 · failed/needs_review → 409 · unknown → 504.
For order endpoints (place/dispatch/cancel) the HTTP status belongs to the order operation, which
must succeed regardless of messaging; each notification's outcome is reported in the body through
the same `answer`-mapping (`notification_view`) — never folded into success.

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/orders` → create_message ("placed") | `ApiV2010AccountMessage.status` | per `status_from_provider` above; body `notifications[].outcome` done/pending/failed/unknown, order still 201 | `services.place_order` → `services.notify` → `services.send_notification` → `safe_write.safe_write(outcome_of=services._send_outcome)` → `twilio_gateway.status_from_provider`; shown by `views.notification_view` |
| `POST /api/orders/{id}/dispatch` → create_message ("dispatched") | `.status` | as above | `services.dispatch_order` → `services.notify(KIND_DISPATCHED)` → `services._send_outcome` → `twilio_gateway.status_from_provider` |
| `POST /api/orders/{id}/dispatch` → create_message scheduled ("follow-up") | `.status` (SCHEDULED expected ⇒ pending) | as above; SCHEDULED is pending, never done | `services.dispatch_order` → `services.notify(KIND_FOLLOW_UP, send_at=…)` → `twilio_gateway.create_message(send_at=…)`; mapped by `twilio_gateway.status_from_provider` |
| `POST /api/orders/{id}/cancel` → create_message ("cancelled") | `.status` | as above | `services.cancel_order` → `services.notify(KIND_CANCELLED)` → `services._send_outcome` |
| `POST /api/orders/{id}/cancel` → update_message(status=canceled) on each scheduled follow-up | `.status` of the updated record | per `cancel_outcome`; body `followUpCallOffs[].outcome` | `services.cancel_order` → `services.call_off` (inner `cancel_outcome` → `twilio_gateway.cancel_outcome`); shown by `views.call_off_view` |
| `DELETE /api/contact-numbers/{id}` → update_message(status=canceled) on follow-ups scheduled to that number | `.status` | per `cancel_outcome`; HTTP via `answer` of the worst call-off (no follow-ups ⇒ 200) | `services.remove_contact_number` → `services.call_off`; HTTP from `views.contact_number` via `safe_write.worst_outcome` + `safe_write.answer_status` |
| `POST /api/notifications/{id}/resend` → create_message | `.status` | per `status_from_provider`; HTTP via `answer` (202 pending normally) | `services.resend` → `safe_write.safe_write(outcome_of=services._send_outcome)`; HTTP from `views.resend_notification` via `safe_write.answer_status` |
| `POST /api/notifications/{id}/resend` repeated with same key → no write, stored outcome | stored `ProviderWrite.outcome` | same mapping, answered from the record | `safe_write.try_claim` (IntegrityError → existing record) → early return in `safe_write.safe_write`; `views.resend_notification` → `safe_write.answer_status` |
| `DELETE /api/notifications/{id}/content` → update_message(body="") | `.body` of the updated record | per `redact_outcome`; HTTP via `answer` | `services.dispose_content` → `twilio_gateway.redact_message` / `twilio_gateway.redact_outcome`; HTTP from `views.notification_content` via `safe_write.answer_status` |

## DUPLICATE CLAIMS

Claim store: the sandbox's own database (SQLite by default, any Django backend) — table
`sms_notifications_providerwrite`, column `reference` **UNIQUE**. `try_claim` = `INSERT`; the DB rejects
the second with `IntegrityError`. A released claim (failed, no provider sid) is re-taken by a
conditional `UPDATE … WHERE outcome='failed' AND provider_sid=''` (row count decides the winner).

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| send "placed" (ref `<prefix>:order:<id>:placed`) | `ProviderWrite.reference` unique row | DB UNIQUE constraint | `IntegrityError` in `try_claim` | `safe_write.try_claim` (INSERT on `ProviderWrite.reference`; `except IntegrityError`), called from `safe_write.safe_write`; ref from `services.reference` in `services.notify` |
| send "dispatched" (ref `…:order:<id>:dispatched`) | same | same | same | `safe_write.try_claim` (INSERT on `ProviderWrite.reference`; `except IntegrityError`), called from `safe_write.safe_write`; ref from `services.notify` via `services.dispatch_order` |
| send "follow-up" scheduled (ref `…:order:<id>:follow_up`) | same | same | same | `safe_write.try_claim` (INSERT on `ProviderWrite.reference`; `except IntegrityError`), called from `safe_write.safe_write`; ref from `services.notify` via `services.dispatch_order` |
| send "cancelled" (ref `…:order:<id>:cancelled`) | same | same | same | `safe_write.try_claim` (INSERT on `ProviderWrite.reference`; `except IntegrityError`), called from `safe_write.safe_write`; ref from `services.notify` via `services.cancel_order` |
| resend (ref `…:notification:<id>:resend:<sha256(idempotency key)[:32]>`) | same | same | same | `safe_write.try_claim` (INSERT on `ProviderWrite.reference`; `except IntegrityError`), called from `safe_write.safe_write`; ref from `services.resend` (`services.hash_key`) |
| call-off of a follow-up (ref `…:notification:<id>:cancel`) — shared by order-cancel and contact-delete | same | same | same | `safe_write.try_claim` (INSERT on `ProviderWrite.reference`; `except IntegrityError`), called from `safe_write.safe_write`; ref from `services.call_off` |
| redact (ref `…:notification:<id>:redact`) | same | same | same | `safe_write.try_claim` (INSERT on `ProviderWrite.reference`; `except IntegrityError`), called from `safe_write.safe_write`; ref from `services.dispose_content` |

## UNKNOWN OUTCOMES

Twilio `create_message` offers no lookup by a client reference and no dedupe (kind 3): the reference
travels **in the message body** as a short tag `Ref <sha256(ref)[:10]>`; `find` = `list_message(to=…, from_=TWILIO_FROM_NUMBER)`
(recent pages) filtered on the tag, or `fetch_message(sid)` when the sid is already known.
Call-offs/redactions look up by the record's own sid (`fetch_message`).

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| every send (placed / dispatched / follow-up / cancelled / resend) | lookup: `list_message(to, from_)` filtered on body tag; `fetch_message(sid)` once known | the write's `reference` (tag = sha256(reference)[:10]) | step 3 of `safe_write.safe_write` (and `safe_write.settle`) calling `services._find_send` → `twilio_gateway.find_message_by_tag` / `twilio_gateway.fetch_message`; tag from `services.reference_tag`, embedded by `services.message_text` |
| call-off of a follow-up | lookup: `fetch_message(sid)` (404 ⇒ gone ⇒ done) | the notification's provider sid | step 3 of `safe_write.safe_write` with `find=lambda w: gw.fetch_message(sid)` in `services.call_off`; `twilio_gateway.fetch_message` returns `GONE` on 404 |
| redact | lookup: `fetch_message(sid)` | the notification's provider sid | step 3 of `safe_write.safe_write` with `find=lambda w: gw.fetch_message(sid)` in `services.dispose_content` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| every send | `Notification` row (order, user, contact, kind, body incl. tag) + `ProviderWrite` claim (`reference`, outcome `sending`, `claimed_at`) in one transaction | `ProviderWrite` outcome/sid/status/provider_time; `Notification` sid/status/error/date_sent | before: `safe_write.try_claim` (runs the factory from `services.notify` / `services.resend` inside `transaction.atomic`); after: `safe_write.complete` → `safe_write.apply_record` |
| call-off | `ProviderWrite` claim for `…:cancel` linked to the notification | outcome + status; notification status refreshed | before: `safe_write.try_claim` from `services.call_off`; after: `safe_write.complete` → `safe_write.apply_record` |
| redact | `ProviderWrite` claim for `…:redact` | outcome; local `Notification.body` wiped + `content_redacted_at` only when done | before: `safe_write.try_claim` from `services.dispose_content`; after: `safe_write.complete`, then `services._wipe_local_content` only on done |

## Implementation notes (decided during the build)

- `safe_write.safe_write(lookup_on_refusal=True)` for the call-off and the redaction: those writes act
  on a record already named by its sid, so a 4xx (e.g. "cannot cancel: not scheduled") is settled by
  reading that same record back (`fetch_message(sid)`) and mapping it with the step's own mapper —
  canceled ⇒ done, delivered ⇒ failed (too late). A lookup by the record's own id cannot lag the write.
- No write uses a same-reference resend as its check: the provider offers no de-duplication for
  `create_message` (the SDK's own `Idempotency-Key` header is a fresh uuid4 per call), so every check
  is a lookup.
- No retries anywhere (the SDK has none, none were added): a send that fails is recorded with its
  outcome, and a later request (a repeated dispatch/cancel, or any read of the notifications) settles it.
- Sandbox bootstrap correction: `oscar_import_catalogue` of the three `books.*.csv` fixtures is
  required — without it only 11 products exist and `loaddata orders.json` fails on foreign keys.
- Type check: `mypy --strict` (django-stubs plugin) is clean on `twilio_gateway.py`, `safe_write.py`
  and `models.py`; `services.py`/`views.py` follow the sandbox's unannotated Django style.
- Logging: provider calls are logged only by `twilio_gateway.LoggingTransport` (query dropped, digit
  runs masked); `httpx`/`httpcore` loggers are raised to WARNING in `sandbox/settings.py`.

## Reconciliation

`GET /api/notifications/reconciliation?from&to`: `list_message(from_=TWILIO_FROM_NUMBER,
date_sent_query_query=<from day 00:00Z> (DateSent>), date_sent_query=<to day +1 00:00Z> (DateSent<),
page_size=1000)`, paginated via `next_page_uri` (`PageToken`, `Page`), then narrowed to
`from <= date_sent < to` on the provider's clock. Local side = notifications whose stored provider
`date_sent` is in window (refreshed from matching provider records first). Findings: matched,
provider-only (sid unknown to the app), local-only, unsettled (no provider time yet, created in window).

## Assumptions & Blockers

- **Blocker (environment, not an SDK gap):** the live account answers 401 / 20003 "status 4 is not
  active" to every call. The integration is built and tested against a stub transport; live
  verification is retried at the end and reported honestly.
- Assumption: `TWILIO_FROM_NUMBER` is a sender in `TWILIO_MESSAGING_SERVICE_SID`'s pool (needed for
  scheduled follow-ups sent with both). Could not be verified (account inactive).
- Assumption: `DateSent>`/`DateSent<` accept the RFC 3339 datetime the SDK serialises (docstring
  describes day granularity) — the report widens to whole days and narrows locally, so either reading is safe.
- Follow-up delay: `SMS_FOLLOWUP_DELAY` setting, default 3 days.
- Dispatch is a new `Dispatched` status added to the sandbox `OSCAR_ORDER_STATUS_PIPELINE`
  (cancellable), plus an Oscar `ShippingEvent` of type "Dispatched".

## REQUIRED READING

- Client lifetime, sync choice, transport ownership — MUST load `python-client-initialization` (loaded)
- Basic auth keyword / silent no-auth — MUST load `python-authentication` (loaded)
- Keyword-only calls, parsed vs raw mode, status → outcome, `answer` — MUST load `python-calling-endpoints` (loaded)
- UNSET vs None, open enums, wire alias `from` — MUST load `python-models` (loaded)
- ApiError/RawError, decode + transport failures, known vs unknown — MUST load `python-error-handling` (loaded)
- Safe write, reconciliation, timeouts, logging transport — MUST load `python-configuration-resilience` (loaded)
- Stub transport tests — MUST load `python-testing` (loaded)
