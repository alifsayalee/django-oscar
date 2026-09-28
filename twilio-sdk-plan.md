# Twilio SDK plan — SMS order notifications for the django-oscar sandbox

Scope: a new Django app `sandbox/apps/sms_notifications/` exposing `/api/...` JSON endpoints
(contact numbers, orders, dispatch/cancel, notifications, resend, content disposal, reconciliation),
sending SMS through the APIMatic `twilio-sdk` (import root `twilio_sdk`, v1.0.0).

## Repo survey (conventions + one exemplar each)

| Concern | Pattern | Exemplar |
| --- | --- | --- |
| Sandbox apps | plain packages under `sandbox/apps/`, imported as `apps.<name>` | `sandbox/apps/sitemaps.py`, `sandbox/urls.py` (`from apps.sitemaps import ...`) |
| Settings | `django-environ` `env = environ.Env()`; `env.str/env.bool(..., default=...)` | `sandbox/settings.py` |
| URL wiring | top-level `urlpatterns` list, then `i18n_patterns(...)` for Oscar | `sandbox/urls.py` |
| Oscar model access | `get_model(app_label, model)` / `get_class(module, name)` | `src/oscar/apps/order/utils.py` |
| Order creation | `OrderCreator().place_order(basket, total, shipping_method, shipping_charge, user=...)` | `src/oscar/apps/order/utils.py` |
| Order status | `order.set_status(...)` over `OSCAR_ORDER_STATUS_PIPELINE` (Pending → Being processed → Complete; → Cancelled) | `sandbox/settings.py` |
| Dispatch | Oscar `ShippingEvent` of `ShippingEventType` code `dispatched` + status `Being processed` | `src/oscar/apps/order/abstract_models.py` |
| Auth | Django session auth (`AuthenticationMiddleware`); staff = `is_staff` | `sandbox/settings.py` |
| DB transactions | `ATOMIC_REQUESTS = True` — API views that write to the provider are `transaction.non_atomic_requests` so a claim commits **before** the provider call | `sandbox/settings.py` |
| Sync vs async | **sync** — Django under WSGI, no `async def` views anywhere | `sandbox/wsgi.py` |

Toolchain: `pip` + `venv` (`venv\Scripts\pip install -e .[test]`), `twilio-sdk` installed from
`git+https://github.com/context-plugins/twilio-python-sdk.git@main` (not previously a dependency).
Tests: `pytest` + `pytest-django`; the app's tests run from `sandbox/` with `--ds=settings`.
Type check: no project type checker configured → `mypy --strict` on the files I add (mypy installed into venv).

## Credentials / environment verification

- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `TWILIO_MESSAGING_SERVICE_SID`
  present; `TWILIO_BASE_URL` unset (→ SDK default `https://api.twilio.com` for server `default`).
- Read-only smoke (scratchpad, 2026-09-28): **every call answers 401 / code 20003
  "authentication failed, account … with status 4 is not active"** — lookups (`default4`),
  messages list (`default`), messaging service (`default1`). Credentials are well-formed (32-char
  alnum token, 34-char SID, no whitespace). → **Blocker B1** below.

## Contract sheet

Client: **`TwilioSdkClient`** (sync). Held as one lazily-built module-level instance
(`apps.sms_notifications.twilio_client.get_client()`), built after fork on first use, closed via
`atexit`. Constructor is keyword-only: `server_config=`, `timeout=` (default 30.0 → we pass 10.0),
`custom_http_client=`, `account_sid_auth_token=BasicAuthCredentials(username=SID, password=TOKEN)`.
**Omitting `account_sid_auth_token` sends unauthenticated requests silently** → `get_client()` raises
`ImproperlyConfigured` when SID/token settings are empty.

Transport: `HttpxClient(timeout=10.0)` wrapped in a logging transport (method, masked URL, status, ms —
no headers, no bodies, phone-like digits masked). Because a custom transport is passed, the client's
`timeout=` does not reach the wire → timeout set on `HttpxClient`.

Servers (`ServerConfig`, frozen, `extra="forbid"`, one environment → `{"<server>": {"base_url": ...}}`):
- Messages ops → server **`default`** (`https://api.twilio.com`). `TWILIO_BASE_URL`, when set, is passed
  verbatim as `server_config={"default": {"base_url": TWILIO_BASE_URL}}`.
- Lookup op → server **`default4`** (`https://lookups.twilio.com`) — **not** governed by `TWILIO_BASE_URL`.

Imports: `from twilio_sdk import TwilioSdkClient`; `from twilio_sdk.core import ApiError, RawError,
BasicAuthCredentials, HttpxClient, HttpRequest, HttpResponse, UNSET, UnsetType`;
`from twilio_sdk.models import ApiV2010AccountMessage, ListMessageResponse, LookupResponse`;
`from twilio_sdk.models.enums import MessageEnumStatus, MessageEnumUpdateStatus, MessageEnumScheduleType`.

Every keyword-only parameter has a real default (`None`) — pass only what is used; no defensive `None`s.
Async twins exist for every op (`AsyncTwilioSdkClient`) — not used. **The SDK performs no retries.**

### Operations in scope

| # | Accessor | Signature (positional \| keyword-only) | Returns | Error union |
| --- | --- | --- | --- | --- |
| 1 | `client.lookups_v2_phone_number.fetch_phone_number3` | `(phone_number: str, *, fields: str\|None, country_code: str\|None, …, request_options)` — path `PhoneNumber`, query `CountryCode` | `LookupResponse` | `RawError` (Case B) |
| 2 | `client.api20100401_message.create_message` | `(account_sid: str, to: str, *, from_: str\|None (wire From), messaging_service_sid: str\|None, body: str\|None, schedule_type: MessageEnumScheduleTypeOrStr\|None, send_at: RFC3339DateTime\|None, …, request_options)` — form fields | `ApiV2010AccountMessage` | `RawError` (Case B) |
| 3 | `client.api20100401_message.fetch_message` | `(account_sid: str, sid: str, *, request_options)` | `ApiV2010AccountMessage` | `RawError` (Case B) |
| 4 | `client.api20100401_message.update_message` | `(account_sid: str, sid: str, *, body: str\|None, status: MessageEnumUpdateStatusOrStr\|None, request_options)` — form `Body`, `Status` | `ApiV2010AccountMessage` | `RawError` (Case B) |
| 5 | `client.api20100401_message.list_message` | `(account_sid: str, *, to: str\|None, from_: str\|None (query From), date_sent, date_sent_query (query DateSent<), date_sent_query_query (query DateSent>), page_size: int\|None, page: int\|None, page_token: str\|None, request_options)` | `ListMessageResponse` | `RawError` (Case B) |

None of these returns `None`; `delete_message` is **not** used (content disposal redacts instead, so
the provider keeps the message record and its outcome). No typed error arms: `ApiError.error` is
always `RawError` (`status_code`, `content`, `text()`, `json()` — `json()` may raise `ValueError`).

Semantics from docstrings (`twilio_sdk/apis/api20100401_message.py`):
- `schedule_type` — "For Messaging Services only: `fixed` in conjunction with send time"; `send_at` —
  "The time that Twilio will send the message. Must be in ISO 8601 format." → scheduled follow-up sends
  `messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID`, `from_=TWILIO_FROM_NUMBER` ("you may also provide
  a `from` … specific Sender from the Sender Pool"), `schedule_type=MessageEnumScheduleType.FIXED`,
  `send_at=<tz-aware datetime>`.
- `update_message` — "used to redact Message `body` text and to cancel not-yet-sent messages"; `body`:
  "To redact the text content of a Message, this parameter's value must be an empty string";
  `status` enum `MessageEnumUpdateStatus` = {`CANCELED`="canceled"}.
- `list_message` `from_` — "Filter by sender"; `date_sent*` — "Accepts GMT dates … `YYYY-MM-DD`" (day
  granularity) → widen to whole days, then narrow locally on `date_sent`.
- `page_size` — default 50, max 1000; `page_token` "provided by the API" → next page parameters parsed
  from `ListMessageResponse.next_page_uri` (`Page`, `PageToken`, `PageSize`).
- Lookup `fields` — optional add-ons; basic lookup (no `fields`) returns `valid`, `phone_number`
  (E.164), `country_code`, `validation_errors`.

RFC3339DateTime (`core/converters/date_time.py`): `Annotated[datetime, …]`; **naive datetimes are
rejected (`ValueError`)**; UTC dumps as `…Z`.

### Models (members used)

`ApiV2010AccountMessage` (all optional; `OptionalNullable[T]` = `T | None | UNSET`):
`sid: OptionalNullable[str]` · `status: Optional[MessageEnumStatusOrStr]` · `body: OptionalNullable[str]` ·
`to`, `from_` (wire `from`) `: OptionalNullable[str]` · `date_sent`, `date_created`, `date_updated:
OptionalNullable[str]` (RFC 2822 strings) · `error_code: OptionalNullable[int]` ·
`error_message: OptionalNullable[str]` · `messaging_service_sid: OptionalNullable[str]`.
**No member is required**, so a truncated 2xx decodes cleanly: after every create/update/fetch, assert
`sid` is a `str` and `status` is not `UNSET`; otherwise treat as outcome-unknown.

`ListMessageResponse`: `messages: Optional[list[ApiV2010AccountMessage]]` · `next_page_uri:
OptionalNullable[str]`.

`LookupResponse`: `valid: Optional[bool]` · `phone_number: OptionalNullable[str]` ·
`country_code: OptionalNullable[str]` · `validation_errors: Optional[list[ValidationErrorOrStr]]`.
Guard: `valid is True` and `phone_number` is a non-empty `str`, else reject.

`MessageEnumStatus` (open enum; unknown values arrive as `str`): QUEUED, SENDING, SENT, FAILED,
DELIVERED, UNDELIVERED, RECEIVING, RECEIVED, ACCEPTED, SCHEDULED, READ, PARTIALLY_DELIVERED, CANCELED.

### Status → outcome mappers (`status_from_provider` family; anything unlisted → `unknown`)

| Mapper | done | pending | failed | unknown |
| --- | --- | --- | --- | --- |
| `delivery_outcome` (immediate message) | DELIVERED, READ | ACCEPTED, QUEUED, SENDING, SENT, SCHEDULED | FAILED, UNDELIVERED, CANCELED, PARTIALLY_DELIVERED | RECEIVING, RECEIVED, unlisted, UNSET |
| `schedule_outcome` (follow-up create: asked = "queued for later") | SCHEDULED | ACCEPTED, QUEUED | FAILED, UNDELIVERED, CANCELED | anything else incl. SENDING/SENT/DELIVERED (went out now, not as asked), UNSET |
| `cancel_outcome` (cancel write; asked = "never reaches them") | CANCELED | SCHEDULED, ACCEPTED, QUEUED (cancel not yet in effect) | SENDING, SENT, DELIVERED, READ, UNDELIVERED, FAILED, PARTIALLY_DELIVERED (too late — it went out) | unlisted, UNSET |
| `redact_outcome` (content disposal) | echoed `body == ""` | — | — | echoed body non-empty / UNSET |

`answer(outcome)` → HTTP: done 200 · pending/sending 202 · failed/needs_review 409 · unknown/other 504.
Order endpoints (place/dispatch/cancel) always answer the **order** result (201/200); the notification
outcomes are reported in the body, never as the HTTP status (a message must not fail the operation).

## OPERATION OUTCOMES

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/orders` → `create_message` (order placed) | `ApiV2010AccountMessage.status` via `delivery_outcome` | DELIVERED/READ → notification `done`; ACCEPTED/QUEUED/SENDING/SENT/SCHEDULED → `pending`; FAILED/UNDELIVERED/CANCELED/PARTIALLY_DELIVERED → `failed`; unlisted/UNSET/no sid → `unknown`. Order response 201 either way, `notification.outcome` in body | `sandbox/apps/sms_notifications/services.py` `place_order` → `notify` → `_send` → `safe_write(..., outcome_of=delivery_outcome)`; later `_fetch_and_apply`/`_refreshed_outcome`; body built by `views.notification_json` |
| `POST /api/orders/{id}/dispatch` → `create_message` (on its way) | same, `delivery_outcome` | as above; order response 200 | `sandbox/apps/sms_notifications/services.py` `dispatch_order` → `notify(order, Notification.DISPATCHED, ...)` → `_send` (`delivery_outcome`) |
| `POST /api/orders/{id}/dispatch` → `create_message` scheduled follow-up | `status` via `schedule_outcome` | SCHEDULED → `done`; ACCEPTED/QUEUED → `pending`; FAILED/UNDELIVERED/CANCELED → `failed`; else → `unknown` | `sandbox/apps/sms_notifications/services.py` `dispatch_order` → `notify(..., send_at=follow_up_at)` → `_send` (`schedule_outcome`) → `provider.send_scheduled` |
| `POST /api/orders/{id}/cancel` → `update_message(status=canceled)` on the follow-up | `status` via `cancel_outcome` | CANCELED → `done` (follow-up called off); SCHEDULED/ACCEPTED/QUEUED → `pending`; went-out statuses → `failed` ("too late"); else → `unknown`. Order response 200, `followUpCancellation.outcome` in body | `sandbox/apps/sms_notifications/services.py` `cancel_order` → `cancel_follow_up` → `safe_write(ActionStore(..., CANCEL), ..., cancel_outcome, repeat_is_safe=True)`; `views.cancel_json` |
| `POST /api/orders/{id}/cancel` → `create_message` (cancelled) | `delivery_outcome` | as order placed | `sandbox/apps/sms_notifications/services.py` `cancel_order` → `notify(order, Notification.CANCELLED, ...)` → `_send` (`delivery_outcome`) |
| `POST /api/notifications/{id}/resend` → `create_message` | `delivery_outcome` → `answer` | done 200 · pending 202 · failed 409 · unknown 504; repeat under same key answers from the stored outcome | `sandbox/apps/sms_notifications/views.py` `resend_notification` → `services.resend` → `_send`; status from `safe_write.answer_status(notification.outcome)` |
| `DELETE /api/notifications/{id}/content` → `update_message(body="")` | echoed `body` via `redact_outcome` | `""` → done 200; non-empty/UNSET → unknown 504; provider 4xx → failed 409 | `sandbox/apps/sms_notifications/views.py` `notification_content` → `services.dispose_content` → `safe_write(..., redact_outcome, status_of=echoed_body)`; `answer_status` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| send (placed / dispatched / follow-up / cancelled) | `Notification` row, `reference` = `{SMS_REFERENCE_PREFIX}:order:{number}:{kind}` | DB `UNIQUE(reference)` constraint | `IntegrityError` inside `transaction.atomic()` in the claim | `sandbox/apps/sms_notifications/services.py` `NotificationSendStore.try_claim` (via `notify`); rejection → `safe_write.safe_write` returns the stored record |
| resend | `Notification` row, `reference` = `{prefix}:resend:{notificationId}:{sha256(idempotencyKey)}` | DB `UNIQUE(reference)` | `IntegrityError` in the claim | `sandbox/apps/sms_notifications/services.py` `NotificationSendStore.try_claim` (via `resend`, `reference("resend", id, sha256(key))`) |
| cancel follow-up | `ProviderAction` row, `reference` = `{prefix}:notification:{id}:cancel` | DB `UNIQUE(reference)` | `IntegrityError` in the claim | `sandbox/apps/sms_notifications/services.py` `ActionStore.try_claim` (via `cancel_follow_up`) |
| redact content | `ProviderAction` row, `reference` = `{prefix}:notification:{id}:redact` | DB `UNIQUE(reference)` | `IntegrityError` in the claim | `sandbox/apps/sms_notifications/services.py` `ActionStore.try_claim` (via `dispose_content`) |

Claims are committed before the provider call (views are `non_atomic_requests`; each claim is its own
`transaction.atomic()` block, autocommit outside it).

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| send / resend (`create_message`) | lookup (kind 3 — no idempotency parameter, no metadata field): the reference's short code `Ref <code>` is appended to the SMS body; `find` lists `list_message(to=…, from_=TWILIO_FROM_NUMBER, page_size=100)` and matches the body code | `Notification.reference` (code = first 10 hex of sha256) | `sandbox/apps/sms_notifications/safe_write.py` `safe_write` step 3 calling `find` = `sandbox/apps/sms_notifications/provider.py` `find_message_by_code` (bound in `services._send` / `services._resolve_send` with `CheckOnlyStore`) |
| cancel (`update_message status=canceled`) | same-reference resend is safe (setting canceled again cannot create a second message); `find` = `fetch_message(sid)` | `ProviderAction.reference` (+ message sid) | `sandbox/apps/sms_notifications/safe_write.py` `safe_write` (`repeat_is_safe=True` → same-reference re-issue) with `find` = `provider.fetch_message`, bound in `services.cancel_follow_up` |
| redact (`update_message body=""`) | same: re-issue is safe; `find` = `fetch_message(sid)` and read `body` | `ProviderAction.reference` (+ message sid) | `sandbox/apps/sms_notifications/safe_write.py` `safe_write` (`repeat_is_safe=True`) with `find` = `provider.fetch_message`, bound in `services.dispose_content` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| send / resend | `Notification` (reference, order, kind, contact number, body, `outcome="sending"`, `claimed_at`) | `provider_sid`, `provider_status`, `outcome`, `provider_date_created/sent`, `error_code` | `sandbox/apps/sms_notifications/services.py` `NotificationSendStore.try_claim` (before) / `NotificationSendStore.complete` + `_apply_message` (after) |
| cancel follow-up | `Notification.cancel_requested_at` set + `ProviderAction(outcome="sending")` | `ProviderAction.outcome`, `provider_status`; `Notification.provider_status` | `sandbox/apps/sms_notifications/services.py` `cancel_order` / `cancel_follow_up` set `cancel_requested_at`, `ActionStore.try_claim` (before) / `ActionStore.complete` (after) |
| redact | local `Notification.body` cleared + `content_disposed_at` + `ProviderAction(outcome="sending")` | `ProviderAction.outcome`; `Notification.provider_body_redacted` | `sandbox/apps/sms_notifications/services.py` `dispose_content` clears `text` + `ActionStore.try_claim` (before) / `ActionStore.complete` (after) |

## Design notes

- Race dispatch ↔ cancel: cancel sets `cancel_requested_at` on the follow-up first; the follow-up send
  path re-reads it after `complete` and, when set, runs the cancel write itself. A follow-up whose
  send is still `sending`/`unknown` at cancel time is resolved through `find` before cancelling.
- Delivery state is provider-owned: `GET /api/my-orders` and `GET /api/orders/{id}/notifications`
  refresh non-terminal notifications through `fetch_message` (throttled to once per 30 s per message).
- Reconciliation filters both sides on the provider's clock (`date_sent`), widening whole days then
  narrowing; locals with no provider `date_sent` are reported as `unsettled`, never dropped.
- Phone numbers are never logged: the logging transport masks digit runs ≥ 6 in URLs; app logs carry
  notification ids only.

## Assumptions & Blockers

- Build note: `oscar_import_catalogue` over the three `sandbox/fixtures/books.*.csv` files WAS needed
  here — `child_products.json` alone gave 11 products and `orders.json` then failed a foreign key;
  after the CSV import there were 209 products and `orders.json` loaded.
- **B1 (blocker for live verification only):** the provided Twilio account is inactive (401, code
  20003, "status 4 is not active") on every host. Not an SDK/plugin gap. I cannot reach a human
  (headless), so implementation proceeds and is verified against a stub transport; live checks are
  retried at the end and the outcome reported.
- A1 (minor): `TWILIO_FROM_NUMBER` is in the messaging service's sender pool (cannot verify — B1).
  If it is not, the scheduled create is refused (recorded `failed`, order still dispatched).
- A2 (minor): `DateSent>`/`DateSent<` are inclusive day bounds; code widens by a day and narrows.
- A3 (minor): basic Lookup `valid` is the provider's "usable destination" test.

## REQUIRED READING

- Client construction, lifetime, custom transport → MUST load `python-client-initialization` (loaded)
- Credentials keyword silently optional → MUST load `python-authentication` (loaded)
- Keyword-only calls, parsed vs raw, status-not-id → MUST load `python-calling-endpoints` (loaded)
- `UNSET` / `OptionalNullable` / open enums → MUST load `python-models` (loaded)
- `ApiError` / `RawError`, transport + decode failures → MUST load `python-error-handling` (loaded)
- Safe write, no retries, base URL, logging transport, reconciliation → MUST load `python-configuration-resilience` (loaded)
- Stub transport tests → MUST load `python-testing` (loaded)
