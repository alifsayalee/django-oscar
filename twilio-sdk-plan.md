# Twilio SDK integration plan — SMS order notifications for the Oscar sandbox

## Scope

New Django app `sandbox/apps/order_notifications/` (label `order_notifications`) exposing, under `/api/`:
contact numbers (register / list / delete), orders (place / dispatch / cancel / my-orders /
notifications), and operator tools (resend, content disposal, reconciliation). Reuses Oscar's
`order.Order`/`order.Line`, `catalogue.Product`, `basket.Basket` and Oscar's `OrderCreator`.

## Repo survey

| Concern | Finding | Exemplar |
| --- | --- | --- |
| Sync vs async | Django under WSGI, sync views → **sync `TwilioSdkClient`** | `sandbox/wsgi.py` |
| Settings | `django-environ` `env = environ.Env()`; read TWILIO_* there | `sandbox/settings.py` |
| DB | SQLite, `ATOMIC_REQUESTS=True` → provider-calling views must be `non_atomic_requests` so a claim commits before the provider call | `sandbox/settings.py` |
| Unique store | the Django DB (unique constraints) — holds claims across processes | — |
| URL conventions | `path(...)` + `include(...)`, sandbox-level non-i18n routes precede `i18n_patterns` | `sandbox/urls.py` |
| Order placement | `OrderCreator.place_order`, `OrderTotalCalculator`, `OrderNumberGenerator`, shipping `Repository`, `Selector().strategy` via `get_class` | `src/oscar/apps/checkout/mixins.py` |
| Status pipeline | `OSCAR_ORDER_STATUS_PIPELINE` in sandbox settings; add `Dispatched` state (Pending/Being processed → Dispatched → Complete/Cancelled) | `sandbox/settings.py` |
| Toolchain | `py -3.11 -m venv venv`; `venv\Scripts\pip install -e .[test]`; SDK installed from git; tests via `sandbox/manage.py test apps.order_notifications`; `mypy --strict` on the gateway module | — |
| Logging hazard | `httpx` logs full request URLs at INFO (lookup URL contains the phone number) and root is DEBUG → set `httpx`/`httpcore` loggers to WARNING in `LOGGING` | `sandbox/settings.py` |

Baseline: repo tests untouched; sandbox has no tests of its own.

## Credentials / environment verification

* Env vars present: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `TWILIO_MESSAGING_SERVICE_SID`,
  `TWILIO_TEST_TO_NUMBER`, `TWILIO_UNREACHABLE_TO_NUMBER`. `TWILIO_BASE_URL` unset (optional).
* Read-only smoke (scratch dir, outside repo): `api20100401_account.fetch_account`,
  `lookups_v2_phone_number.fetch_phone_number3`, `api20100401_message.list_message` → **every call 401,
  Twilio code 20003 "account … with status 4 is not active"**. See Blockers.

## CONTRACT SHEET

**Client**: `twilio_sdk.TwilioSdkClient` (sync). Keyword-only ctor: `server_config`, `timeout`,
`custom_http_client`, `account_sid_auth_token`. One long-lived, lazily built (post-fork) module client,
closed via `atexit`. Auth: `account_sid_auth_token={"username": TWILIO_ACCOUNT_SID, "password": TWILIO_AUTH_TOKEN}`
— optional at type level; an omitted credential sends unauthenticated → we refuse to build the client when
either setting is empty. Transport: `twilio_sdk.core.HttpxClient(timeout=10.0)` wrapped in a logging
transport that logs method + host + status + ms only (never path/query: they carry phone numbers).

**Servers** (`server_config`, one-environment nesting `{"default": {"base_url": ...}}`):
* messaging (`api20100401_message.*`) → server `default` (`https://api.twilio.com`); when
  `TWILIO_BASE_URL` is set → `server_config={"default": {"base_url": TWILIO_BASE_URL}}` verbatim.
* lookup (`lookups_v2_phone_number.fetch_phone_number3`) → server `default4`
  (`https://lookups.twilio.com`), NOT governed by `TWILIO_BASE_URL`.

**Operations** (all Case B: `ApiError.error` is always `RawError`; no typed arms; every keyword-only param
has a real default — pass only what is used; last param `request_options` keys exactly `timeout`,
`extra_headers`, extra headers win over endpoint headers, names lowercased):

| Operation | Positional | Keyword-only used | Returns |
| --- | --- | --- | --- |
| `client.api20100401_message.create_message` | `account_sid`, `to` | `from_` (wire `From`), `body` (`Body`), `messaging_service_sid` (`MessagingServiceSid`), `schedule_type` (`ScheduleType`, `MessageEnumScheduleType.FIXED` = `"fixed"`, Messaging Services only), `send_at` (`SendAt`, `RFC3339DateTime` = tz-aware `datetime`), `request_options` | `ApiV2010AccountMessage` |
| `client.api20100401_message.fetch_message` | `account_sid`, `sid` | — | `ApiV2010AccountMessage` |
| `client.api20100401_message.update_message` | `account_sid`, `sid` | `body` (`Body`; `""` redacts — only `None` is omitted from the form), `status` (`MessageEnumUpdateStatus.CANCELED` = `"canceled"`; cancels not-yet-sent) | `ApiV2010AccountMessage` |
| `client.api20100401_message.list_message` | `account_sid` | `from_` (`From`), `to` (`To`), `date_sent_query_query` (wire `DateSent>`), `date_sent_query` (wire `DateSent<`), `page_size` (max 1000), `page`, `page_token` (`PageToken`) | `ListMessageResponse` |
| `client.lookups_v2_phone_number.fetch_phone_number3` | `phone_number` | — | `LookupResponse` |

* `create_message` / `update_message` / `delete_message` send an `Idempotency-Key` header = fresh
  `uuid4()` per call. We override it through `request_options={"extra_headers": {"Idempotency-Key": <deterministic ref token>}}`
  (extra headers win). Whether Twilio enforces it is **not documented in the SDK** → treated as unenforced
  (`repeat_is_safe=False` for creates).
* No client-chosen reference field exists on a Message → **kind 3 lookup**: the reference token
  (`ref xxxxxxxxxx`, 10 hex of sha256(reference)) is embedded in `Body`; `find_by_ref` = `list_message(to=…, page_size=50)`
  and match the token in `body`.

**Models** (`Optional[T]` = `T | UnsetType`, `OptionalNullable[T]` = `T | None | UnsetType`; all members UNSET-able; no member required → a truncated 2xx decodes cleanly, so we assert):
* `ApiV2010AccountMessage`: `sid: OptionalNullable[str]`, `status: Optional[MessageEnumStatusOrStr]`,
  `body: OptionalNullable[str]`, `to`, `from_` (alias `from`), `date_sent`, `date_created`, `date_updated`
  (`OptionalNullable[str]`, RFC 2822 strings), `error_code: OptionalNullable[int]`, `error_message`.
  Must assert after every write: `sid` is a non-empty `str`, `status` present.
* `ListMessageResponse`: `messages: Optional[list[ApiV2010AccountMessage]]`, `next_page_uri: OptionalNullable[str]`
  (carries `PageToken`/`Page` query params for the next call).
* `LookupResponse`: `valid: Optional[bool]`, `phone_number: OptionalNullable[str]` (E.164 canonical),
  `country_code`, `validation_errors: Optional[list[ValidationErrorOrStr]]`. Accept only `valid is True`
  and a non-empty `phone_number`.

**`MessageEnumStatus` (open enum) → send outcome (`status_from_provider`)**:
`DELIVERED`, `READ` → done · `ACCEPTED`, `SCHEDULED`, `QUEUED`, `SENDING`, `SENT` → pending ·
`FAILED`, `UNDELIVERED`, `PARTIALLY_DELIVERED`, `CANCELED` (undone) → failed ·
`RECEIVING`, `RECEIVED` (inbound, never ours), any unlisted value, UNSET → unknown.

**Call-off outcome (`cancel_outcome`)**: `CANCELED` → done · `FAILED`, `UNDELIVERED` → done (it will
never reach the shopper) · `SENT`, `DELIVERED`, `READ`, `SENDING`, `QUEUED`, `PARTIALLY_DELIVERED` → failed
(too late) · `SCHEDULED`, `ACCEPTED`, unlisted, UNSET → unknown.

**Redaction outcome**: echoed `body == ""` → done; any non-empty body → needs_review; UNSET/None → unknown.

**Errors**: `ApiError` (`.status_code`, `.error: RawError` → `.text()`); decode → `pydantic.ValidationError`/`ValueError`
in both modes; transport → raw `httpx` exceptions. Never sent: `ConnectError`, `ConnectTimeout`,
`PoolTimeout`, `ProxyError`; maybe sent: other `httpx.RequestError`. SDK performs **no retries** — we add
none for writes (the safe write's lookup handles unknowns); reads are not retried either (a failed refresh
just leaves stored state).

## OPERATION OUTCOMES

(`where in the code` paths are relative to `sandbox/apps/order_notifications/`.)

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/orders` → create_message (placed) | `ApiV2010AccountMessage.status` | delivered/read→done; accepted/scheduled/queued/sending/sent→pending; failed/undelivered/partially_delivered/canceled→failed; receiving/received/unlisted/absent→unknown. Order request always 201; each notification reported with its outcome | `services.py` `place_order` → `notify` → `run_safe_write` + `_send_spec` (`outcome_of` = `twilio_gateway.py` `status_from_provider`); `views.py` `notification_json` |
| `POST /api/orders/{id}/dispatch` → create_message (dispatched) + create_message scheduled (follow_up) | same | same mapping; follow-up `scheduled` = pending | `services.py` `dispatch_order` → `notify` (DISPATCHED; FOLLOW_UP with `send_at`) → `run_safe_write`; `twilio_gateway.py` `TwilioGateway.schedule_message` |
| `POST /api/orders/{id}/cancel` → update_message status=canceled (follow-up call-off) + create_message (cancelled) | `status` of the updated message | canceled/failed/undelivered→done; sent/delivered/read/sending/queued/partially_delivered→failed ("too late"); scheduled/accepted/unlisted/absent→unknown | `services.py` `cancel_order` → `call_off` (`outcome_of` = `twilio_gateway.py` `cancel_outcome`), then `notify` (CANCELLED) |
| `DELETE /api/contact-numbers/{id}` → call-off of scheduled follow-ups to that number | same as cancel | same as cancel | `services.py` `delete_contact_number` → `call_off` |
| `POST /api/notifications/{id}/resend` → create_message | `status` | send mapping; 200 done, 202 pending/sending, 409 failed/needs_review, 504 unknown | `services.py` `resend` → `notify(reference=…)` → `run_safe_write`; caller status from `views.py` `status_for` |
| `DELETE /api/notifications/{id}/content` → update_message body="" | echoed `body` | ""→done (200); non-empty→needs_review (409); absent→unknown (504) | `services.py` `dispose_content` → `run_safe_write` (`outcome_of` = `twilio_gateway.py` `redact_outcome`); `views.py` `notification_content` → `status_for` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| order-event SMS (placed/dispatched/cancelled/follow_up) | `Notification` row, `reference` = `{prefix}:order:{order_id}:{kind}` | DB `UNIQUE(reference)` | `IntegrityError` on insert | `models.py` `Notification.reference` (unique); `services.py` `claim` (insert-or-fail) from `notify`; re-take only via `_take_over` |
| resend SMS | `Notification` row, `reference` = `{prefix}:resend:{notification_id}:{sha256(idempotency key)}` | DB `UNIQUE(reference)` | `IntegrityError` on insert | `services.py` `resend` (derives the reference from the key) → `notify` → `claim` |
| follow-up call-off (cancel) | `ProviderAction` row, `reference` = `{prefix}:notification:{id}:cancel` | DB `UNIQUE(reference)` | `IntegrityError` on insert | `models.py` `ProviderAction.reference` (unique); `services.py` `call_off` → `claim` |
| content redaction | `ProviderAction` row, `reference` = `{prefix}:notification:{id}:redact` | DB `UNIQUE(reference)` | `IntegrityError` on insert | `models.py` `ProviderAction.reference` (unique); `services.py` `dispose_content` → `claim` |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| create_message (all sends) | lookup: `list_message(to=…)`, match ref token in body; then read its status | ref token of the `Notification.reference` | `services.py` `run_safe_write` lookup step → `_send_spec.find` → `twilio_gateway.py` `TwilioGateway.find_by_reference` |
| update_message status=canceled | same-reference resend (cancel by sid is idempotent: re-cancel of a canceled message changes nothing) + `fetch_message(sid)` when no body | `ProviderAction.reference` / message sid | `services.py` `call_off` → `run_safe_write` (`repeat_is_safe=True`, `check_after_refusal`) → `twilio_gateway.py` `TwilioGateway.fetch_message` |
| update_message body="" | same-reference resend (redaction idempotent) / `fetch_message(sid)` | `ProviderAction.reference` / message sid | `services.py` `dispose_content` → `run_safe_write` (`repeat_is_safe=True`, `check_after_refusal`) → `twilio_gateway.py` `TwilioGateway.fetch_message` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| create_message | `Notification` (reference, order, contact, to, body, kind, outcome=sending, claimed_at) committed | provider sid, raw status, outcome, provider_time, error code | `services.py` `claim` (before) → `run_safe_write` → `_store_message` + `_complete` (after) |
| update_message (cancel) | `ProviderAction` (reference, notification, kind=cancel, outcome=sending) committed | status, outcome, provider_time; notification status refreshed | `services.py` `call_off`: `claim` (before) → `run_safe_write` → `_store_action`, `_sync_status_from_action` (after) |
| update_message (redact) | `ProviderAction` (kind=redact, sending) committed | outcome; on done `Notification.body` cleared + `content_disposed_at` | `services.py` `dispose_content`: `claim` (before) → `run_safe_write` → `_sync_status_from_action`, `_clear_content` (after) |

## Assumptions & Blockers

* **BLOCKER (environment, not a plugin gap):** the supplied Twilio account is inactive (401 / 20003 "status 4 is
  not active") on every host tried. Live verification (real delivery, real schedule + cancel, live
  reconciliation) is impossible with these credentials. Headless: proceed; verify every flow end to end
  through the real HTTP API with the SDK's transport seam faked (and with a local fake Twilio for
  `TWILIO_BASE_URL`), and report the blocker.
* Minor: message scheduling needs `TWILIO_MESSAGING_SERVICE_SID`; without it the follow-up is recorded
  `failed` ("scheduling not configured") and the dispatch still succeeds.
* Minor: follow-up delay default 72 h (`ORDER_NOTIFICATIONS_FOLLOW_UP_DELAY`), kept inside Twilio's scheduling window.
* Found while building: the sandbox's `pages/ranges/offers/orders.json` fixtures reference products created by
  `oscar_import_catalogue sandbox/fixtures/*.csv` (the Makefile runs it); without that step they fail with FK
  errors and only 35 products exist. With it: 209 products, 249 countries, 2 users.
* Minor: reconciliation date filters are sent as whole-day-widened datetimes and narrowed locally on the
  provider's `date_sent`.

## REQUIRED READING

* Client construction/lifetime — MUST load `python-client-initialization` (loaded).
* Error boundary — MUST load `python-error-handling` (loaded).
* Safe write, server config, reconciliation — MUST load `python-configuration-resilience` (loaded).
* Status → outcome, `answer` — MUST load `python-calling-endpoints` (loaded).
* UNSET / open enums — MUST load `python-models` (loaded).
* Tests with a stub transport — MUST load `python-testing` (loaded).
* Credentials — MUST load `python-authentication` (loaded).
