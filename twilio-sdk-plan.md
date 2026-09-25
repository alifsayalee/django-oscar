# Twilio SDK plan — SMS order notifications for the django-oscar sandbox

Scope: a new Django app `sandbox/apps/sms_notifications/` exposing `/api/...` (wired in
`sandbox/urls.py`, outside `i18n_patterns`), reusing Oscar's `order.Order` / `order.Line` /
`basket.Basket` / `catalogue.Product`. Every Twilio interaction goes through the APIMatic
`twilio-sdk` Python SDK (import root `twilio_sdk`, version 1.0.0, installed from
`git+https://github.com/context-plugins/twilio-python-sdk.git@main` into `venv/`).

## Repo survey (conventions to imitate)

| Convention | Exemplar |
| --- | --- |
| Sandbox-local app lives under `sandbox/apps/`, imported as `apps.<name>` | `sandbox/apps/sitemaps.py`, `sandbox/urls.py` (`from apps.sitemaps import ...`) |
| Oscar models loaded via `get_model` / classes via `get_class` | `src/oscar/apps/order/utils.py` |
| Order placement = `OrderCreator().place_order(basket=..., total=..., shipping_method=..., shipping_charge=..., user=..., order_number=...)` | `src/oscar/apps/checkout/mixins.py:125` |
| Status transitions via `order.set_status()` against `OSCAR_ORDER_STATUS_PIPELINE` in settings | `src/oscar/apps/order/abstract_models.py:143`, `sandbox/settings.py` |
| Settings read with `django-environ` (`env.str/int/bool`) | `sandbox/settings.py` |
| `ATOMIC_REQUESTS=True` on the default DB | `sandbox/settings.py` — the API views must opt out (`transaction.non_atomic_requests`) so a claim row commits **before** the provider call |
| Session auth (Oscar login at `/<lang>/accounts/login/`, form prefix `login`, submit key `login_submit`); CSRF middleware on | `src/oscar/apps/customer/views.py:104` |
| No DRF in the project — plain Django `JsonResponse` views | n/a |

Sync vs async: **sync**. Django under WSGI (`sandbox/wsgi.py`, `runserver`), all views sync →
`TwilioSdkClient` (sync), transport keyword `custom_http_client`, teardown `close()`.

Toolchain: `py -3.11` venv at `venv/` (`pip install -e .[test]` + `twilio-sdk` + `mypy` +
`django-stubs`). Tests: Django test runner from `sandbox/` (`python manage.py test apps.sms_notifications`).
Type check: `mypy --strict` over the new app with a config file kept outside the repo
(django-stubs plugin). Baseline: `pytest tests/integration/order tests/functional/checkout` on the
untouched tree.

## Credentials / configuration

All read in `sandbox/settings.py` via `env`: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
`TWILIO_FROM_NUMBER`, `TWILIO_MESSAGING_SERVICE_SID`, `TWILIO_BASE_URL` (optional). No values in
any file. Plus app tunables: `SMS_NOTIFICATIONS_TIMEOUT_SECONDS` (10), `SMS_NOTIFICATIONS_FOLLOWUP_DELAY_HOURS` (72),
`SMS_NOTIFICATIONS_INSTALL_ID` (default derived from a hash of SECRET_KEY + DB name).

Server selection (every call states it):
- Messaging API = server **`default`** (`https://api.twilio.com`). When `TWILIO_BASE_URL` is set →
  `server_config={"default": {"base_url": TWILIO_BASE_URL}}` verbatim; otherwise `server_config={"default": {}}`
  is not needed — omit override and the config default is `https://api.twilio.com` (verified in
  `server/server_config.py`, `DefaultConfig.base_url`).
- Lookups = server **`default4`** (`https://lookups.twilio.com`), never governed by `TWILIO_BASE_URL`.
- Auth: `account_sid_auth_token=BasicAuthCredentials(username=ACCOUNT_SID, password=AUTH_TOKEN)`. The
  keyword is optional at the type level — the factory refuses to build a client when either value is
  empty (otherwise requests go out unauthenticated).

## Contract sheet (all facts from `sdk-map.md`, `map/operations/*.md`, and the installed modules)

General:
- Client: `from twilio_sdk import TwilioSdkClient`; keyword-only ctor `server_config`, `timeout` (float, >0,
  default 30.0), `custom_http_client`, `account_sid_auth_token`. Long-lived, lazily built per process
  (post-fork safe), closed via `atexit`.
- Core imports from `twilio_sdk.core`: `ApiError`, `RawError`, `BasicAuthCredentials`, `HttpxClient`,
  `HttpRequest`, `HttpResponse`, `UNSET`, `UnsetType`. `HttpxClient(*, timeout, proxy_url, verify)`.
- Enums from `twilio_sdk.models.enums`: `MessageEnumStatus`, `MessageEnumUpdateStatus`, `MessageEnumScheduleType`.
- Every keyword-only param has a real default (`None`); never pass defensive `None`s.
- Every op here is **Case B**: `ApiError.error` is always `RawError` (`status_code`, `content`, `text()`, `json()`).
- No retries in the SDK. Decode failure = `pydantic.ValidationError`/`ValueError` (not `ApiError`), in both modes.
  httpx transport exceptions arrive unwrapped.
- Transport protocol: `send(request: HttpRequest) -> HttpResponse`, `close()`. `HttpRequest(method, url, headers, body, timeout)`;
  `HttpResponse(status_code, headers (lowercase), content, request)`.
- Date-time params typed `RFC3339DateTime` (pass aware `datetime`; dumped as `...Z`). Response date fields on
  messages are plain `str` in RFC 1123 form (`'Wed, 23 Sep 2026 11:31:24 +0000'`) → parse with
  `email.utils.parsedate_to_datetime`.

| Operation | Server | Signature (positional \| keyword-only) | Returns | Members the code asserts on |
| --- | --- | --- | --- | --- |
| `client.api20100401_message.create_message` | `default` | `(account_sid, to, *, from_, messaging_service_sid, body, schedule_type, send_at, …)`; wire `To`,`From`,`MessagingServiceSid`,`Body`,`ScheduleType`,`SendAt` (form fields) | `ApiV2010AccountMessage` | `sid` (OptionalNullable[str]) must be a str else outcome unknown; `status` (Optional[MessageEnumStatusOrStr]); `to` echo; `date_sent`, `date_created` |
| `client.api20100401_message.fetch_message` | `default` | `(account_sid, sid, *)` | `ApiV2010AccountMessage` | `sid`, `status`, `body`, `date_sent`, `error_code`, `error_message` |
| `client.api20100401_message.update_message` | `default` | `(account_sid, sid, *, body: str\|None, status: MessageEnumUpdateStatusOrStr\|None)`; docstring: `body=""` redacts text; used to cancel not-yet-sent messages | `ApiV2010AccountMessage` | cancel: `status`; redact: `body == ""` |
| `client.api20100401_message.list_message` | `default` | `(account_sid, *, to, from_, date_sent, date_sent_query (wire "DateSent<"), date_sent_query_query (wire "DateSent>"), page_size (max 1000), page, page_token)` | `ListMessageResponse` (`messages: Optional[list[ApiV2010AccountMessage]]`, `next_page_uri: OptionalNullable[str]`) | pagination: parse `Page` and `PageToken` from `next_page_uri` query (smoke-verified) |
| `client.lookups_v1_phone_number_api.fetch_phone_number2` | `default4` | `(phone_number, *, country_code, type_, add_ons, add_ons_data)` | `LookupsV1PhoneNumber` (`phone_number`, `country_code`, `national_format`: OptionalNullable[str]) | `phone_number` must be a str (canonical E.164); 404 = not a usable destination |

Scheduling (create_message docstring): `schedule_type` "For Messaging Services only … value `fixed` in
conjunction with the send time"; `messaging_service_sid` + a specific `from_` from the pool is allowed. The
follow-up is sent with `messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID`, `from_=TWILIO_FROM_NUMBER`,
`schedule_type=MessageEnumScheduleType.FIXED`, `send_at=<aware datetime>`.

`MessageEnumStatus` members (models/enums/message_enum_status.py): QUEUED, SENDING, SENT, FAILED, DELIVERED,
UNDELIVERED, RECEIVING, RECEIVED, ACCEPTED, SCHEDULED, READ, PARTIALLY_DELIVERED, CANCELED.
`MessageEnumUpdateStatus`: CANCELED. `MessageEnumScheduleType`: FIXED.

Smoke results (read-only, real credential, from scratchpad):
- `lookups_v2_phone_number.fetch_phone_number3` **cannot be used**: every 200 fails to decode
  (`ValidationError`: `caller_name`, `sim_swap`, … arrive `null` but are typed non-nullable `Optional[...]`).
  Using Lookups **v1** instead — it decodes (all fields `OptionalNullable`) and returns the canonical E.164;
  a junk number answers 404 (`code 20404`). Both test numbers resolve (CA and US); canonical == configured.
- `list_message(from_=…, date_sent_query/_query=aware datetimes)` → 200, filter honoured; a future lower
  bound returns 0 rows. The listing includes `direction=inbound` copies (another account number receiving from
  ours) → reconciliation counts only `outbound-*` directions.

## OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| `create_message` (order-placed / dispatched / cancelled / resend — immediate send) | `ApiV2010AccountMessage.status` (`MessageEnumStatusOrStr`) | **done**: `delivered`, `read`. **pending** (accepted, not finished): `accepted`, `queued`, `sending`, `sent` (handed to carrier, no delivery receipt yet), `scheduled`, `partially_delivered` (meaning not settled by the source → not-yet). **failed**: `failed`, `undelivered`, `canceled` (undone). **unknown**: `receiving`, `received` (inbound values on an outbound send), any unlisted string, absent (`UNSET`/None) status. Stored as `outcome` + raw `provider_status`; later refreshed by `fetch_message`. | `sandbox/apps/sms_notifications/gateway.py` `outcome_of_send` (the only mapping), applied by `sandbox/apps/sms_notifications/safe_write.py` `verify_and_complete` → `apply_provider_state`; later refreshes by `sandbox/apps/sms_notifications/services.py` `refresh` |
| `create_message` (delivery follow-up, scheduled) | same | same mapping; `scheduled` is **pending** (queued with the provider, not sent). After an operator cancel the provider reports `canceled` → **failed** from the send's perspective (never delivered), and the cancel itself is recorded separately (next row). | `sandbox/apps/sms_notifications/services.py` `notify` (kind `followup`, `scheduled_for`) → `sandbox/apps/sms_notifications/gateway.py` `send_message` (scheduled branch) → `outcome_of_send` via `sandbox/apps/sms_notifications/safe_write.py` `apply_provider_state` |
| `update_message(status=canceled)` (cancel a queued follow-up) | `ApiV2010AccountMessage.status` | **done**: `canceled` (this write asks for the undoing, so canceled is its done). **failed** (too late — the message went out): `sending`, `sent`, `delivered`, `read`, `undelivered`, `failed`, `partially_delivered`. **pending**: `scheduled`, `accepted`, `queued` (cancel not yet effective) → re-attempted on next refresh while `cancel_requested_at` is set. **unknown**: anything else / absent → re-fetched on refresh. A 4xx refusal is followed by a `fetch_message` and the fetched status mapped through this same table. | `sandbox/apps/sms_notifications/gateway.py` `outcome_of_cancel`, used by `sandbox/apps/sms_notifications/services.py` `honour_cancel_request` (4xx/unreadable → `gateway.fetch_message` and the same mapping), recorded by `_set_cancel`; retried from `services.refresh` |
| `update_message(body="")` (content disposal) | `ApiV2010AccountMessage.body` (no status for this write; the echoed body is the outcome) | **done**: a follow-up `fetch_message` after the redaction returns an empty body (the echo alone is not trusted). **failed**: provider 4xx refusal (400/404/409/422) → 409 to caller; 401/403/429 → 502/503; local body kept. **unknown**: transport failure after sending / 5xx / unreadable answer → the confirming fetch decides; if that fetch fails → 504 `outcomeUnknown`; if the provider still returns content → 502. Local body kept until confirmed; the operator may repeat (setting a fixed value is harmless to repeat). Messages not yet in a final state are refused up front with 409. | `sandbox/apps/sms_notifications/services.py` `dispose_content` (→ `gateway.redact_message`, `gateway.fetch_message`, `_mark_disposed`) |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| order-placed SMS (`create_message`) | `sms_notifications_notification` row, `reference = <install>:order:<orderId>:placed`, inserted with `outcome="sending"` in its own committed transaction before the call | DB `UNIQUE` constraint on `Notification.reference` (SQLite unique index) | `IntegrityError` in the claim function → `False`, then answer from the stored row | `sandbox/apps/sms_notifications/safe_write.py` `_try_claim` (called first thing in `safe_send`); reference built in `sandbox/apps/sms_notifications/services.py` `_order_reference` |
| dispatched SMS | same table, `…:order:<orderId>:dispatched` | same unique constraint | same | `sandbox/apps/sms_notifications/safe_write.py` `_try_claim` (called first thing in `safe_send`); reference built in `sandbox/apps/sms_notifications/services.py` `_order_reference` |
| delivery follow-up (scheduled `create_message`) | same table, `…:order:<orderId>:followup` | same unique constraint | same | `sandbox/apps/sms_notifications/safe_write.py` `_try_claim` (called first thing in `safe_send`); reference built in `sandbox/apps/sms_notifications/services.py` `_order_reference` |
| cancelled SMS | same table, `…:order:<orderId>:cancelled` | same unique constraint | same | `sandbox/apps/sms_notifications/safe_write.py` `_try_claim` (called first thing in `safe_send`); reference built in `sandbox/apps/sms_notifications/services.py` `_order_reference` |
| operator resend | same table, `…:resend:<notificationId>:<sha256(Idempotency-Key)[:24]>` (same key = repeat; new key = new legitimate send) | same unique constraint | same | `sandbox/apps/sms_notifications/services.py` `resend` (reference from the key hash) → `sandbox/apps/sms_notifications/safe_write.py` `safe_send` / `_try_claim` |
| re-claim after a never-sent failure (`failed` with no provider sid) | same row | conditional `UPDATE … SET outcome='sending' WHERE reference=? AND outcome='failed' AND provider_sid IS NULL` — the DB lets exactly one updater see rowcount 1 | rowcount 0 → treated as losing the claim | `sandbox/apps/sms_notifications/safe_write.py` `_try_claim` (the conditional `update(...)` after `except IntegrityError`) |
| contact-number registration (no provider write; Lookups is a read) | `ContactNumber` row | partial `UniqueConstraint(user, phone_number) WHERE deleted_at IS NULL` | `IntegrityError` → return the existing active row | `sandbox/apps/sms_notifications/services.py` `register_contact_number` (`except IntegrityError` → existing row); constraint in `sandbox/apps/sms_notifications/models.py` `ContactNumber.Meta` |

`update_message` (cancel / redact) sets a field to a fixed value — harmless to repeat, so no claim; see
UNKNOWN OUTCOMES for how an unanswered one is checked.

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| every `create_message` (all kinds above) | **lookup** (kind 3 — Twilio has no idempotency key and no metadata on messages): the body carries a short token derived from the claim reference (`(ref XXXXXXXXXX)`); `list_message(to=<destination>, page_size=100)` newest-first, scanning pages back to the claim time, matching the token in `body`. Found → settle from that record's status. Not found / check fails → stays `unknown` under the **same** reference; never re-sent under a new one; retried by the refresh path (`GET` endpoints and the `sms_refresh_statuses` command). `repeat_is_safe=False` → the check is always a lookup, never a resend. | the claim `reference` (token = base32(sha256(reference))[:10]) | `sandbox/apps/sms_notifications/safe_write.py` `check_by_reference` → `sandbox/apps/sms_notifications/gateway.py` `find_by_token`; reached from `safe_send` (5xx / `httpx.RequestError` / `ValueError`, and for a stale or unknown claim), from `sandbox/apps/sms_notifications/services.py` `refresh` and `honour_cancel_request`, and from `manage.py sms_refresh_statuses`; `sandbox/apps/sms_notifications/reconciliation.py` `reconcile` also recovers unknown sends by token |
| `update_message(status=canceled)` | lookup: `fetch_message(sid)` and map its status through the cancel row above; if still `scheduled` the cancel is repeated (fixed value — safe) | message `sid` | `sandbox/apps/sms_notifications/services.py` `honour_cancel_request` (the `except gateway.SDK_FAILURES` branch → `gateway.fetch_message`) |
| `update_message(body="")` | lookup: `fetch_message(sid)` and check `body == ""` | message `sid` | `sandbox/apps/sms_notifications/services.py` `dispose_content` (the confirming `gateway.fetch_message`) |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| every `create_message` | committed `Notification` row: `reference`, `ref_token`, `kind`, `order`, `contact_number`, `to_number`, `body`, `scheduled_for`, `outcome="sending"`, `claimed_at` | `provider_sid`, `provider_status`, `outcome` (mapped), `provider_date_created`, `provider_date_sent`, `error_code`/`error_message`, `last_checked_at` — or `outcome="unknown"`/`"failed"` with the reason | `sandbox/apps/sms_notifications/safe_write.py` `_try_claim` (before) → `gateway.send_message` → `verify_and_complete` / `apply_provider_state` / `_complete` (after), all inside `safe_send` |
| `update_message(status=canceled)` | `Notification.cancel_requested_at` committed (so a still-`sending` follow-up is cancelled by whoever completes it, and refresh keeps retrying) | `provider_status`, `outcome`, `cancel_outcome`, `canceled_at` | `sandbox/apps/sms_notifications/services.py` `request_cancellation` (commits the flag) → `honour_cancel_request` → `apply_provider_state` + `_set_cancel`; the sender re-checks the flag in `notify` after `safe_send` |
| `update_message(body="")` | `Notification.content_disposal_requested_at` committed | `content_disposed_at`, local `body` cleared (only after the provider confirmed an empty body) | `sandbox/apps/sms_notifications/services.py` `dispose_content` (commits the request flag before `gateway.redact_message`) → `_mark_disposed` |

## Design summary

- Models: `ContactNumber` (user, E.164 `phone_number`, `country_code`, `national_format`, `created_at`, `deleted_at`),
  `Notification` (see WRITE ORDER; `kind` ∈ placed/dispatched/followup/cancelled/resend, `resend_of`,
  `idempotency_key_hash`, `outcome` ∈ sending/pending/done/failed/needs_review/unknown).
- Safe write (`claim → call → check → verify → complete`) in one helper used by every send.
- Order endpoints never fail because of messaging: the order change commits first, messaging runs after in
  its own guarded block, the response reports each notification's outcome.
- No webhooks (no public URL): status is pulled with `fetch_message` on read endpoints (bounded per request)
  and by `manage.py sms_refresh_statuses`.
- Cancel: marks `cancel_requested_at` on every not-yet-sent follow-up of the order, then cancels each at the
  provider; a follow-up still `sending`/`unknown` is cancelled as soon as its sid is known (the completing
  send checks the flag; refresh retries). Deleting a contact number does the same for its queued messages.
- Reconciliation: provider side = `list_message(from_=TWILIO_FROM_NUMBER, DateSent> , DateSent<)` over whole
  days covering the window, fully paginated, `outbound-*` only, then narrowed back to `[from, to)` on the
  provider's `date_sent`. Local side = notifications with stored provider `date_sent` in the window; matched by
  sid against the set; provider-only records are also matched by ref token to unsettled local rows. Four
  buckets: matched, provider_only, local_only, unsettled (no provider send time — scheduled/canceled/unknown).
- Logging: a transport wrapper logs method, host, path with phone numbers redacted, status and latency — never
  query strings, headers or bodies. Views never log numbers.

## Assumptions & Blockers

- Minor: Lookups v2 is unusable through this SDK version (decode defect above); v1 gives exactly what is needed
  (canonical form, 404 for unusable) — proceeding with v1. Not a gap.
- Minor: "sent" (carrier accepted, no receipt) is reported as pending, not delivered.
- Minor: a shopper with several numbers is messaged on their most recently registered active number.
- Minor: new order status `Dispatched` added to the sandbox's `OSCAR_ORDER_STATUS_PIPELINE`.
- No blockers: the claim store is the project's own database (unique constraints), no new infrastructure.

## REQUIRED READING

- Client construction/lifetime — MUST load `python-client-initialization` (loaded).
- Every try/except around SDK calls — MUST load `python-error-handling` (loaded).
- Every create/send/cancel/redact + reconciliation — MUST load `python-configuration-resilience` (loaded).
- First call per operation, status → outcome mapping — MUST load `python-calling-endpoints` (loaded).
- `UNSET` vs `None`, open enums, `OptionalNullable` members — MUST load `python-models` (loaded).
- Tests with a stub transport — MUST load `python-testing` (loaded).
- Credentials keyword semantics — `python-authentication` (Basic auth only; covered by the sheet above).
