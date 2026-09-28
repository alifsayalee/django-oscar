# Twilio SDK plan — SMS order notifications for the Oscar sandbox

Scope: a new Django app `sandbox/apps/sms_notifications/` (label `sms_notifications`), routed under
`/api/` from `sandbox/urls.py`, reusing Oscar's `order.Order` / `order.Line` / `basket.Basket` /
`catalogue.Product`. Provider: the APIMatic-generated `twilio_sdk` 1.0.0 (distribution `twilio-sdk`,
installed from `git+https://github.com/context-plugins/twilio-python-sdk.git@main` into `venv/`).

## Repo survey & toolchain

| Item | Finding |
| --- | --- |
| Host framework | Django (WSGI, sync views) → **sync client** `TwilioSdkClient` |
| App convention | sandbox apps live in `sandbox/apps/<name>` (exemplar: `sandbox/apps/sitemaps.py`, `sandbox/apps/user/`), imported as `apps.<name>` because `sandbox/` is the settings root |
| Oscar class loading | `oscar.core.loading.get_model` / `get_class` (exemplar: `src/oscar/test/factories/__init__.py` `create_order`) |
| Order placement exemplar | `src/oscar/test/factories/__init__.py::create_order` (Basket + `Free()` shipping + `OrderTotalCalculator` + `OrderCreator.place_order`) |
| Order status | `OSCAR_ORDER_STATUS_PIPELINE` in `sandbox/settings.py`; add `Dispatched` |
| Env manager | `py -3.11 -m venv venv`; `venv\Scripts\pip install -e .[test]` + `twilio-sdk` from git + `mypy` |
| Tests | pytest + pytest-django; `setup.cfg` testpaths `tests/`; `--sqlite` flag for SQLite. New app tests run from `sandbox/` with `--ds=settings` |
| Baseline | `pytest --sqlite tests/integration/order tests/functional/dashboard/test_order.py` → 109 passed, **5 failed (pre-existing concurrency tests on SQLite)**, 1 skipped |
| Type checker | none configured → `mypy --strict` on the new provider-facing modules |
| Claim store | the project's own relational DB (SQLite by default) — unique constraints hold claims across processes |

## Credentials / environment verification

- All six `TWILIO_*` env vars present; values have no stray whitespace.
- Read-only smoke (`lookups_v2_phone_number.fetch_phone_number3`, `api20100401_message.list_message`,
  `api20100401_account.fetch_account`) → **every call `401`, Twilio code `20003`, "account … with status 4 is not
  active"**. See Assumptions & Blockers.

## Contract sheet

**Client**: `twilio_sdk.TwilioSdkClient` (sync). Keyword-only ctor: `server_config`, `timeout`, `custom_http_client`,
`account_sid_auth_token`. Held as a lazily-built module singleton (built after fork, on first use), closed at
`atexit`. Never mixed with `AsyncTwilioSdkClient`.
**Auth**: `account_sid_auth_token=BasicAuthCredentials(username=TWILIO_ACCOUNT_SID, password=TWILIO_AUTH_TOKEN)`
(`twilio_sdk.core`). Omitting it silently sends unauthenticated → the factory refuses to build without both.
**Servers** (`server_config`, one environment, flat nesting `{"<server>": {"base_url": …}}`, `extra="forbid"`):
- `default` = `https://api.twilio.com` — every Messages call. Overridden verbatim by `TWILIO_BASE_URL` when set.
- `default4` = `https://lookups.twilio.com` — Lookup v2. Not governed by `TWILIO_BASE_URL`; optional
  `TWILIO_LOOKUPS_BASE_URL` override (used only for offline/fake-provider runs).
Both passed explicitly on every construction.
**Timeout**: `timeout` default 30.0 is too long → the transport is our own `HttpxClient(timeout=TWILIO_TIMEOUT)` (10s)
wrapped in a logging transport; client `timeout=` therefore does not reach the wire.
**Retries**: the SDK performs none. We add none on writes (safe write instead); reads are not retried either
(a failed read is reported, the next request re-reads).
**Keyword-only boundary**: every param after `*` has a real default (`None`); pass only what is used.
**Error model**: every in-scope op is **Case B** → `ApiError.error` is always `RawError`
(`status_code`, `content`, `text()`, `json()`), `ApiError.status_code`. Decode failures raise
`pydantic.ValidationError`/`ValueError` in both modes. Transport failures are raw `httpx` exceptions.
**Async rule**: every op has an identical `Async…` twin — not used.

| Operation | Signature (positional \| keyword-only) | Server | Returns | Error |
| --- | --- | --- | --- | --- |
| `client.api20100401_message.create_message` | `(account_sid: str, to: str, *, … schedule_type: MessageEnumScheduleTypeOrStr \| None, send_at: RFC3339DateTime \| None, from_: str \| None (wire From), messaging_service_sid: str \| None, body: str \| None, …, request_options)` | `default` | `ApiV2010AccountMessage` | `RawError` |
| `client.api20100401_message.fetch_message` | `(account_sid: str, sid: str, *, request_options)` | `default` | `ApiV2010AccountMessage` | `RawError` |
| `client.api20100401_message.update_message` | `(account_sid: str, sid: str, *, body: str \| None, status: MessageEnumUpdateStatusOrStr \| None, request_options)` — docstring: "used to redact Message body text and to cancel not-yet-sent messages"; `body` "must be an empty string" to redact | `default` | `ApiV2010AccountMessage` | `RawError` |
| `client.api20100401_message.list_message` | `(account_sid: str, *, to: str \| None (wire To), from_: str \| None (wire From), date_sent, date_sent_query: RFC3339DateTime \| None (wire DateSent<), date_sent_query_query: RFC3339DateTime \| None (wire DateSent>), page_size: int \| None (max 1000), page: int \| None, page_token: str \| None, request_options)` | `default` | `ListMessageResponse` | `RawError` |
| `client.lookups_v2_phone_number.fetch_phone_number3` | `(phone_number: str, *, fields … country_code …, request_options)` | `default4` | `LookupResponse` | `RawError` |

Not used: `delete_message` (would destroy the provider's record of the send, which must survive).

**Models** (all members `Optional`/`OptionalNullable` → default `UNSET`; none required, so a truncated 2xx
decodes cleanly — our code asserts the members it depends on):
- `ApiV2010AccountMessage` (`twilio_sdk.models`): `sid: OptionalNullable[str]`, `status: Optional[MessageEnumStatusOrStr]`,
  `body: OptionalNullable[str]`, `to`, `from_` (wire `from`), `date_created` / `date_sent` / `date_updated`
  (`OptionalNullable[str]`, RFC 2822 GMT), `error_code: OptionalNullable[int]`, `error_message`, `messaging_service_sid`.
  Asserted after create/fetch/update: `sid` is a non-empty `str`, `status` present. Missing → outcome `unknown`.
- `ListMessageResponse`: `messages: Optional[list[ApiV2010AccountMessage]]`, `next_page_uri: OptionalNullable[str]`
  (carries `Page` and `PageToken` query params for the next call). Absent `messages` on a 2xx → unreadable → 502.
- `LookupResponse`: `valid: Optional[bool]`, `phone_number: OptionalNullable[str]` (canonical E.164),
  `country_code`, `validation_errors: Optional[list[ValidationErrorOrStr]]`
  (`TOO_SHORT`, `TOO_LONG`, `INVALID_BUT_POSSIBLE`, `INVALID_COUNTRY_CODE`, `INVALID_LENGTH`, `NOT_A_NUMBER`).
  `valid is True` and a `str` `phone_number` → accept & store `phone_number`; `valid is False` → 400; anything else → 502.
- Enums (`twilio_sdk.models.enums`, open `…OrStr`): `MessageEnumStatus` = `queued, sending, sent, failed, delivered,
  undelivered, receiving, received, accepted, scheduled, read, partially_delivered, canceled`;
  `MessageEnumUpdateStatus` = `canceled`; `MessageEnumScheduleType` = `fixed`.
- `RFC3339DateTime` requires a **timezone-aware** `datetime` (naive is rejected).

**Status mappers** (`sandbox/apps/sms_notifications/outcomes.py`):
- `status_from_provider` (a send): `delivered`, `read` → done · `accepted`, `scheduled`, `queued`, `sending`, `sent`,
  `partially_delivered` → pending · `failed`, `undelivered` → failed · `canceled` → failed (done then undone / called
  off: it did not reach the shopper) · `receiving`, `received`, unlisted string, `UNSET` → unknown.
- `cancel_outcome` (call-off of a scheduled follow-up): `canceled` or provider `404` (GONE) → done · `scheduled`,
  `accepted`, `queued` → pending (call-off not yet in effect) · `sending`, `sent`, `delivered`, `read`,
  `partially_delivered`, `failed`, `undelivered` → failed (too late: it went out) · anything else → unknown.
- `redaction_outcome` (content disposal): echoed `body == ""` → done · non-empty `str` → failed · `None`/`UNSET` → unknown.
- `answer(outcome, id)`: done → 200 · pending/sending → 202 · failed/needs_review → 409 · anything else → 504.

## OPERATION OUTCOMES

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/orders` → `create_message` (order placed) | `ApiV2010AccountMessage.status` | via `status_from_provider`: delivered/read → `done`; accepted/scheduled/queued/sending/sent/partially_delivered → `pending`; failed/undelivered/canceled → `failed`; other/absent → `unknown`. Order is placed (201) regardless; the notification entry in the body carries that `outcome`. No number on file → `skipped` | `views.orders` → `services.notify_order_placed` → `services.send_notification` → `safe_write.safe_write(outcome_of=outcomes.status_from_provider)`; reported by `views.notification_json` (`sandbox/apps/sms_notifications/views.py`, `sandbox/apps/sms_notifications/services.py`, `sandbox/apps/sms_notifications/outcomes.py`) |
| `POST /api/orders/{id}/dispatch` → `create_message` (dispatched notice) | `ApiV2010AccountMessage.status` | same mapping as above, reported per notification; dispatch answers 200 regardless | `views.order_dispatch` → `services.dispatch_notifications` → `services.send_notification`; wrapped by `views._never_fails` |
| `POST /api/orders/{id}/dispatch` → `create_message` scheduled follow-up (`schedule_type=fixed`, `send_at`) | `ApiV2010AccountMessage.status` | `scheduled` → `pending` (queued with provider, not yet sent); failed/undelivered/canceled → `failed`; delivered/read → `done`; other → `unknown`. Order cancelled before the claim → `skipped` | `services.dispatch_notifications` (`send_at=services._follow_up_time`) → `services.send_notification` → `services._send_guarded` → `provider.send_sms` (scheduled branch: `schedule_type=MessageEnumScheduleType.FIXED`, `messaging_service_sid`, `send_at`) |
| `POST /api/orders/{id}/cancel` → `create_message` (cancellation notice) | `ApiV2010AccountMessage.status` | same mapping as the send rows; cancel answers 200 regardless | `views.order_cancel` → `services.cancel_notifications` → `services.send_notification` |
| `POST /api/orders/{id}/cancel` → `update_message(status=canceled)` on the follow-up | `ApiV2010AccountMessage.status` (and 404) | via `cancel_outcome`: canceled/404 → `done`; scheduled/accepted/queued → `pending`; sending/sent/delivered/read/partially_delivered/failed/undelivered → `failed` (too late); other → `unknown`. Reported as `followUpCallOff` in the body | `services.call_off_follow_up` → `services.call_off` → `safe_write.safe_write(send=provider.cancel_scheduled_sms via services.gone_or, outcome_of=outcomes.cancel_outcome, read=services.read_call_off)`; reported by `views.call_off_json` |
| `DELETE /api/contact-numbers/{id}` → `update_message(status=canceled)` on pending follow-ups to that number | `ApiV2010AccountMessage.status` (and 404) | same `cancel_outcome`; number removed locally first (no new sends); each call-off's outcome listed in the body; the aggregate goes through `answer` | `views.contact_number_detail` → `services.remove_contact_number` → `services.call_off`; aggregate `outcomes.aggregate` → `outcomes.answer_status` |
| `POST /api/notifications/{id}/resend` → `create_message` | `ApiV2010AccountMessage.status` | via `status_from_provider` then `answer`: done → 200, pending/sending → 202, failed/needs_review → 409, unknown → 504; body carries `notificationId` | `views.notification_resend` → `services.resend` → `services.send_notification` (`outcomes.status_from_provider`) → `outcomes.answer_status` |
| `DELETE /api/notifications/{id}/content` → `update_message(body="")` | echoed `ApiV2010AccountMessage.body` | via `redaction_outcome` then `answer`: `""` → done 200; non-empty → failed 409; absent → unknown 504; provider 4xx refusal → failed 409 | `views.notification_content` → `services.dispose_content` → `safe_write.safe_write(send=provider.redact_sms, read=services.read_redaction, outcome_of=outcomes.redaction_outcome)` → `outcomes.answer_status` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| order-placed `create_message` | `Notification` row, `reference = "<prefix>:order:<orderId>:order_placed"` | DB `UNIQUE(reference)` → `IntegrityError` | `try_claim` (insert-or-fail) | `services.send_notification` (ref from `services.reference`) → `safe_write.ClaimStore.try_claim` (insert into `models.Notification`, `reference` `unique=True`) |
| dispatched-notice `create_message` | `Notification.reference = "<prefix>:order:<orderId>:order_dispatched"` | DB `UNIQUE(reference)` | `try_claim` | `services.dispatch_notifications` → `services.send_notification` → `safe_write.ClaimStore.try_claim` |
| follow-up `create_message` (scheduled) | `Notification.reference = "<prefix>:order:<orderId>:delivery_follow_up"`; cancel inserts the same reference as a `skipped` tombstone when none exists, so a racing dispatch loses | DB `UNIQUE(reference)` | `try_claim` | `services.call_off_follow_up` → `services._record_skipped` (tombstone) / `services._tombstone_released_claim`; `services.send_notification` → `safe_write.ClaimStore.try_claim`; post-claim re-check in `services._send_guarded` (raises `safe_write.NotNeeded`) |
| cancelled-notice `create_message` | `Notification.reference = "<prefix>:order:<orderId>:order_cancelled"` | DB `UNIQUE(reference)` | `try_claim` | `services.cancel_notifications` → `services.send_notification` → `safe_write.ClaimStore.try_claim` |
| resend `create_message` | `Notification.reference = "<prefix>:resend:<notificationId>:<sha256(idempotency key)[:32]>"` | DB `UNIQUE(reference)` | `try_claim` | `services.resend` (ref = `services.reference("resend", id, sha256(key)[:32])`) → `services.send_notification` → `safe_write.ClaimStore.try_claim` |
| follow-up call-off `update_message(status=canceled)` | `ProviderAction.reference = "<prefix>:cancel:<notificationId>"` | DB `UNIQUE(reference)` | `try_claim` | `services.call_off` → `safe_write.ClaimStore(ProviderAction).try_claim` (`models.ProviderAction.reference` `unique=True`) |
| content redaction `update_message(body="")` | `ProviderAction.reference = "<prefix>:redact:<notificationId>"` | DB `UNIQUE(reference)` | `try_claim` | `services.dispose_content` → `safe_write.ClaimStore(ProviderAction).try_claim` |

A `failed` claim with no provider id is re-taken by an atomic conditional `UPDATE … WHERE outcome='failed' AND
provider_sid=''` (row count decides the winner) — never by a read-then-write.

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| every `create_message` (placed / dispatched / follow-up / cancelled / resend) | lookup, kind 3: the reference's short token (`ref_token`, 10 hex of sha256(reference)) is written into the message body; `list_message(to=, from_=TWILIO_FROM_NUMBER)` paged, matched on `body` containing the token and `date_created` ≥ claim time − margin | the `Notification.reference` (via its `ref_token`) | `safe_write.safe_write` step 3 calls `find=services._find_sent` → `provider.find_sms_by_token` (token from `services.token_for`, embedded by `services.message_text`); a sid already known → `provider.fetch_sms` |
| follow-up call-off `update_message(status=canceled)` | lookup by the record's own id: `fetch_message(sid)` (404 → GONE → done) | the follow-up's `provider_sid` | `services.call_off`: `find=lambda: services.gone_or(provider.fetch_sms(sid))` inside `safe_write.safe_write` |
| redaction `update_message(body="")` | lookup by the record's own id: `fetch_message(sid)` and read `body` | the notification's `provider_sid` | `services.dispose_content`: `find=lambda: provider.fetch_sms(sid)` inside `safe_write.safe_write` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| every `create_message` | `Notification` row: `reference`, `ref_token`, `order`, `to_number`, `body`, `send_at`, `outcome="sending"`, `claimed_at` | `provider_sid`, `provider_status`, `outcome` (mapped), `provider_created_at` / `provider_sent_at` (provider clock), `error_code` | `services.send_notification` builds the claim defaults → `safe_write.ClaimStore.try_claim`; after the call `safe_write.ClaimStore.complete` with `services.read_send` / `services.message_fields` |
| follow-up call-off | `ProviderAction(action="cancel", reference, outcome="sending", claimed_at)` | `outcome` (via `cancel_outcome`), `provider_status`, `provider_time`; follow-up `Notification` refreshed | `services.call_off` → `safe_write.ClaimStore.try_claim` (ProviderAction) → `safe_write.ClaimStore.complete` with `services.read_call_off`; then `services._refresh_quietly` |
| redaction | `ProviderAction(action="redact", reference, outcome="sending", claimed_at)` | `outcome` (via `redaction_outcome`); on done: local `Notification.body` cleared, `content_disposed_at` set | `services.dispose_content` → `safe_write.ClaimStore.try_claim` → `safe_write.ClaimStore.complete` with `services.read_redaction`; on done `services._clear_local_content`; then `services._refresh_quietly` |

## Reconciliation

`list_message(account_sid, from_=TWILIO_FROM_NUMBER, date_sent_query_query=<from, day-floored, UTC>,
date_sent_query=<to + 1 day, day-floored, UTC>, page_size=1000)`, paged through `next_page_uri`'s `Page`/`PageToken`
until none (hard cap, flagged `truncated` if hit), then narrowed on the provider clock (`date_sent` in
`[from, to]`). Local side on the same clock (`provider_sent_at` in window); local rows without a provider time whose
claim or `send_at` falls in the window are refreshed first, and any still without one are reported as `unsettled`.
Findings: `matched`, `localOnly`, `providerOnly`, `unsettled`, matching by `sid` as a set.

## Assumptions & Blockers

- **Blocker (provider side, not a plugin gap): the live account is inactive.** Every authenticated call returns
  `401`/`20003` "account … with status 4 is not active". Nothing in the integration can fix that; live verification
  (real delivery, real scheduled call-off, real reconciliation data) cannot happen until the account owner
  reactivates it. There is nobody to ask (headless run), so the build proceeds and is verified end to end against a
  local fake Twilio (reached through `TWILIO_BASE_URL` / `TWILIO_LOOKUPS_BASE_URL`), with live runs retried at the end.
- Minor: the shopper is messaged at their most recently registered number.
- Minor: follow-up delay = 3 days (`SMS_FOLLOW_UP_DELAY_DAYS`); scheduled sends use `TWILIO_MESSAGING_SERVICE_SID`
  plus `from_=TWILIO_FROM_NUMBER`, so the reconciliation's `From` filter covers them.
- Minor: status reads are not retried; a later GET re-reads.

## REQUIRED READING

- Error ladder around every call, transport split (never-sent vs may-have-landed) — MUST load `python-error-handling` (loaded)
- Client construction, lifetime, custom transport, close at exit — MUST load `python-client-initialization` (loaded)
- Safe write, claims, unknown outcomes, reconciliation on the provider clock, logging transport — MUST load `python-configuration-resilience` (loaded)
- Status mapping and `answer`, `-> None` ops, `request_options` — MUST load `python-calling-endpoints` (loaded)
- `UNSET` vs `None`, open enums, aliases — MUST load `python-models` (loaded)
- Stub transport tests, both transport-failure inputs, same-operation-twice test — MUST load `python-testing` (loaded)

## Verification record

- `mypy --strict` (+ `warn_unreachable`, django-stubs plugin) on `provider`, `outcomes`, `safe_write`: clean.
  `mypy --check-untyped-defs` on the whole app (services, views, models, tests): clean.
- `flake8 --max-line-length 119` on the app: clean.
- App tests (`cd sandbox && ../venv/Scripts/python -m pytest apps/sms_notifications/tests --ds=settings`): 40 passed —
  real SDK client against a stateful fake at the transport seam (both transport-failure kinds, landed-without-answer,
  5xx-that-landed, truncated 2xx, same-operation-twice, fresh-key resend, call-off, redaction, paged reconciliation,
  ownership/staff rules, no numbers or token in logs).
- Baseline subset re-run after the change: unchanged (109 passed, same 5 pre-existing failures, 1 skipped).
- Offline end-to-end over real HTTP (sandbox `runserver` on the assigned port block, `TWILIO_BASE_URL` /
  `TWILIO_LOOKUPS_BASE_URL` pointed at an HTTP-wrapped fake): every flow driven through `/api/` only; server log
  contained no phone numbers, token or sending number.
- **Live**: not possible — every authenticated call to the real account returns `401`/`20003` ("status 4 is not
  active"), re-checked after the build. Nothing was sent to any number.
