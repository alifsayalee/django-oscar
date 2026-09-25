# Twilio SDK plan — SMS order notifications for the django-oscar sandbox

## Scope

New Django app `sandbox/apps/order_notifications/`, wired into `sandbox/urls.py` under `/api/`
(outside `i18n_patterns`). Reuses Oscar's `order.Order` / `order.Line` (via Oscar's own
`OrderCreator` from a programmatically built `basket.Basket`), `catalogue.Product`, the partner
strategy for pricing, and `order.OrderStatusChange` for status history. Session auth
(`django.contrib.auth`), operators = `is_staff`.

## Repo survey (conventions → exemplar)

| Convention | Exemplar |
| --- | --- |
| Sandbox app layout (`apps.<name>`) | `sandbox/apps/user/models.py`, `sandbox/apps/sitemaps.py` |
| URL wiring | `sandbox/urls.py` (plain `path()` list + `i18n_patterns`) |
| Settings via `django-environ` `env.*` | `sandbox/settings.py` (`env.bool`, `env.str`) |
| Loading Oscar models/classes | `get_model` / `get_class` from `oscar.core.loading` (e.g. `src/oscar/apps/checkout/mixins.py`) |
| Order status pipeline | `OSCAR_ORDER_STATUS_PIPELINE` in `sandbox/settings.py` |

- Host is **sync** (Django under WSGI, `ATOMIC_REQUESTS=True`). → **sync `TwilioSdkClient`**.
- Toolchain: `pip` + `venv` (`py -3.11 -m venv venv`, `venv\Scripts\pip install -e .[test]`),
  `twilio-sdk` 1.0.0 installed from git into the same venv. Tests: `pytest` (repo suite needs
  PostgreSQL → baseline cannot run here: `OperationalError: connection to server at "localhost"`).
  New app tests run under the sandbox settings (SQLite) with `manage.py test apps.order_notifications`.
  Type check: no mypy config in repo → `mypy --strict` on the new app (django-stubs installed).
- Sandbox bootstrap: migrate + fixtures ran; `orders.json` fails with a FK error (sample orders only;
  not needed). 11 products; products 9 and 10 are purchasable (GBP 15.00).

## Credentials / environment

- `sandbox/settings.py` reads `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`,
  `TWILIO_MESSAGING_SERVICE_SID`, `TWILIO_BASE_URL` (optional) from env. No values in files.
- Auth: `account_sid_auth_token=BasicAuthCredentials(username=ACCOUNT_SID, password=AUTH_TOKEN)`.
  Missing SID/token → integration reports "not configured" (never constructs a no-auth client).
- Servers: `server_config={"default": {"base_url": TWILIO_BASE_URL or "https://api.twilio.com"},
  "default4": {"base_url": "https://lookups.twilio.com"}}` — `TWILIO_BASE_URL` governs only the
  messaging API (`default`); Lookups (`default4`) is a different host and is not governed by it.

## Read-only smoke results (live credential, from scratch dir)

- `lookups_v2_phone_number.fetch_phone_number3` → **200 but SDK decode fails**
  (`ValidationError`: live body carries `null` for `caller_name`, `sim_swap`, … which the model types
  as `Optional[...]` without a `None` arm). Not usable → use Lookup **v1**.
- `lookups_v1_phone_number_api.fetch_phone_number2` → 200, decodes; returns `phone_number` (E.164) and
  `country_code` (CA for test number, US for unreachable). Garbage input (`12345`, `not-a-number`,
  `+999123`) → `ApiError` 404 (`code 20404`).
- `api20100401_message.list_message(from_=…, date_sent_query_query=…, date_sent_query=…)` → 200; the
  SDK serialises datetimes as `DateSent>=2026-…Z`, and the filter works at instant precision; canceled
  (never-sent) messages carry `date_sent=None`. `date_sent`/`date_created` are RFC1123 strings.
  Pagination via `next_page_uri` (`PageToken`, `Page` query params).
- `messaging_v1_phone_number.list_phone_number(MESSAGING_SERVICE_SID)` → `TWILIO_FROM_NUMBER` is in the
  service's sender pool (so scheduled messages can pin `from_=TWILIO_FROM_NUMBER`).
- Account holds other traffic from the same number (earlier runs) → shows as provider-only in reports.

## Contract sheet (all lookups closed)

Rules for every call below: sync client; the async twin is identical but awaited (unused). Everything
after `*` is keyword-only and has a real default — pass only what is used, never defensive `None`s.
Every op is **Case B**: `ApiError.error` is always `RawError` (`status_code`, `content`, `text()`,
`json()`); raw peers return `ApiResult[T, RawError]`. **No retries in the SDK.** Decode failure raises
`pydantic.ValidationError`/`ValueError` in both modes; httpx transport errors arrive unwrapped.
`request_options={"timeout": float, "extra_headers": {...}}` — `extra_headers` overrides the SDK's own
`Idempotency-Key` header (the SDK sends a random `uuid4()` per call on create/update/delete).

| Op (accessor) | Server | Signature (positional · keyword-only used) | Returns | Members asserted |
| --- | --- | --- | --- | --- |
| `api20100401_message.create_message` | `default` | `(account_sid, to, *, from_, body, messaging_service_sid, schedule_type, send_at: datetime, request_options)` — wire `From`, `Body`, `MessagingServiceSid`, `ScheduleType`, `SendAt` | `ApiV2010AccountMessage` | `sid` (str, else unreadable→unknown), `status` (`MessageEnumStatus`), `date_sent`/`date_created`, `error_code`, `error_message` |
| `api20100401_message.fetch_message` | `default` | `(account_sid, sid, *, request_options)` | `ApiV2010AccountMessage` | `sid`, `status`, `body`, `error_code`, `date_sent`, `date_created` |
| `api20100401_message.update_message` | `default` | `(account_sid, sid, *, body: str, status: MessageEnumUpdateStatus, request_options)` — `body=""` redacts; `status="canceled"` cancels a not-yet-sent message | `ApiV2010AccountMessage` | `status`, `body` |
| `api20100401_message.list_message` | `default` | `(account_sid, *, to, from_, date_sent_query (DateSent<), date_sent_query_query (DateSent>), page_size (max 1000), page, page_token, request_options)` | `ListMessageResponse` | `messages: list[ApiV2010AccountMessage]`, `next_page_uri` |
| `lookups_v1_phone_number_api.fetch_phone_number2` | `default4` | `(phone_number, *, request_options)` | `LookupsV1PhoneNumber` | `phone_number` (E.164), `country_code`; 404 ⇒ not a usable number |

Model/enum facts:
- `ApiV2010AccountMessage` (`models/api_v2010_account_message.py`): all members `OptionalNullable`/
  `Optional` (UNSET default); `from_` wire alias `from`; `status: Optional[MessageEnumStatusOrStr]`
  (open enum → may be a plain `str`); `error_code: OptionalNullable[int]`.
- `MessageEnumStatus` (`models/enums/message_enum_status.py`): `QUEUED, SENDING, SENT, FAILED,
  DELIVERED, UNDELIVERED, RECEIVING, RECEIVED, ACCEPTED, SCHEDULED, READ, PARTIALLY_DELIVERED, CANCELED`.
- `MessageEnumUpdateStatus`: `CANCELED`. `MessageEnumScheduleType`: `FIXED`.
- `ListMessageResponse`: `messages: Optional[list[...]]`, `next_page_uri: OptionalNullable[str]`.
- `LookupsV1PhoneNumber` (`models/lookups_v1_phone_number.py`): `phone_number`, `country_code`,
  `national_format` — `OptionalNullable[str]`; `caller_name`/`carrier`/`add_ons` are
  `OptionalNullable[Any]` (never serialised by us).
- Imports: `TwilioSdkClient` from `twilio_sdk`; `ApiError, RawError, BasicAuthCredentials, HttpxClient,
  HttpRequest, HttpResponse, UNSET, UnsetType` from `twilio_sdk.core`; `ApiV2010AccountMessage` from
  `twilio_sdk.models`; `MessageEnumStatus, MessageEnumUpdateStatus, MessageEnumScheduleType` from
  `twilio_sdk.models.enums`.
- Client: one module-level lazily built instance per process (built after fork, on first use), closed at
  `atexit`; wrapped transport `HttpxClient(timeout=…)` for method/host/status/latency logging (path
  digits redacted — lookup paths and `To=` queries carry phone numbers). `timeout` goes on the
  transport (client `timeout=` does not reach a custom transport).

## OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| `create_message` (placed / dispatched / cancelled / resend; immediate) | `ApiV2010AccountMessage.status` | done: `delivered`, `read`. pending (not-yet): `queued`, `sending`, `sent` (no delivery receipt yet), `accepted`, `scheduled`, `partially_delivered`. failed: `failed`, `undelivered`, `canceled` (undone). unknown (not-yet): `receiving`, `received` (inbound values on an outbound send), any value the enum lacks, UNSET/None. Stored as `outcome` + raw `provider_status`; pending/unknown are re-read by `fetch_message` on later reads | `provider.status_from_provider` (applied in `provider.read_message`), stored by `services._record` from `services._send_claimed`; re-read by `services.refresh` (via `services.sweep` from `views.my_orders` / `views.order_notifications` and `manage.py sms_sweep`) |
| `create_message` (delivery follow-up, `schedule_type=fixed`) | same field | same mapping; the expected answer is `scheduled` ⇒ pending (queued with provider, not sent). `canceled` ⇒ failed (for the send); | `provider.send_message` (scheduled branch: `schedule_type=MessageEnumScheduleType.FIXED`, `send_at`, `messaging_service_sid`) called from `services.notify(…, send_at=…)` in `views.order_dispatch`; mapped by `provider.status_from_provider`, stored by `services._record` |
| `update_message(status=canceled)` (call off follow-up) | `ApiV2010AccountMessage.status` | done: `canceled`. pending: `scheduled` (cancel not yet in effect → retried by sweep). failed (too late, message is out): `queued`, `sending`, `sent`, `accepted`, `delivered`, `read`, `partially_delivered`, `failed`, `undelivered`. unknown: `receiving`, `received`, unlisted, UNSET | `provider.cancel_outcome_from_provider` → `services._cancel_state` (done→`done`, pending→`pending`, failed→`too_late`, else `unknown`), applied in `services.call_off` and, for unsettled call-offs, in `services._record` |
| `update_message(body="")` (content disposal) | `ApiV2010AccountMessage.body` (no status for this write) | done: `body == ""` (verified again by `fetch_message`). unknown: body non-empty, UNSET or None — not claimed disposed | `services.dispose_content` (`provider.redact_message`, then `provider.fetch_message` and `state.body != ''` → `content_state=unknown` + 502; `''` → `disposed`) |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| `create_message` for an order event (placed / dispatched / follow-up / cancelled) | `Notification` row, `reference = "{SMS_REFERENCE_PREFIX}:order:{order_id}:{kind}"`, inserted with `outcome="sending"` before the call | DB `UNIQUE(reference)` → `IntegrityError` | `IntegrityError` around the insert → load existing, never re-send (a `failed` row with no `provider_sid` is re-taken by a conditional UPDATE and sent under the same reference) | `services.safe_send` (insert in `transaction.atomic`, `except IntegrityError`), reference from `services.order_reference`, called by `services.notify`; constraint `Notification.reference` (`unique=True`, `models.py`) |
| `create_message` for operator resend | `Notification` row, `reference = "{prefix}:resend:{original_id}:{sha256(idempotency key)}"` | DB `UNIQUE(reference)` | same | `services.resend` → `services.resend_reference` → `services.safe_send`; `views.notification_resend` answers 201 (new) / 200 `replayed: true` |
| `update_message(status=canceled)` | `Notification.cancel_state` compare-and-set `UPDATE … WHERE cancel_state IN (none, pending, unknown) OR (requested AND older than SEND_WINDOW) → requested` | conditional UPDATE matching 0 rows | rows==0 → skip (another request owns it) | `services.call_off` (`Notification.objects.filter(…).update(cancel_state=CANCEL_REQUESTED…)`, `if not claimed: return`) |
| `update_message(body="")` | `Notification.content_state` compare-and-set `retained/unknown (or disposing older than SEND_WINDOW) → disposing` | conditional UPDATE matching 0 rows | rows==0 → already `disposed`: answer from it; else 409 "in progress" | `services.dispose_content` (conditional `update(content_state=CONTENT_DISPOSING)`, `if not claimed`) |
| Order status transitions (dispatch/cancel) — local only, gates the sends | `Order.status` compare-and-set `UPDATE … WHERE status IN allowed` | 0 rows updated | → 409 | `orders.transition` (`Order.objects.filter(pk, status=old_status).update(…)`, `TransitionRefused`) caught in `views._operator_transition` |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| `create_message` (all kinds) | lookup: `list_message(to=destination, from_=TWILIO_FROM_NUMBER)` (≤3 pages) matching the body tag `[ref <token>]`, token = first 10 hex of sha256(reference); found → settle from its status; not found → stays `unknown` (never failed by time, never resent under a new reference) | `Notification.reference` (token in body; same value sent as `Idempotency-Key` header) | `services._send_claimed` (`e.outcome_unknown` → `services._settle_by_lookup` → `services.find_by_reference` → `provider.list_messages(to=…, max_pages=LOOKUP_PAGES)`); also `services.safe_send` (unknown / stale `sending` claim → lookup, never create) and `services.refresh` |
| `update_message(status=canceled)` | lookup: `fetch_message(sid)` → map status with the cancel mapping; still `scheduled` → cancel again (harmless) | `Notification.provider_sid` | `services.call_off` (`except provider.ProviderError` → `provider.fetch_message(sid)`; unreadable → `cancel_state=unknown`/`pending`, retried by `services.sweep`) |
| `update_message(body="")` | lookup: `fetch_message(sid)` → `body == ""` | `Notification.provider_sid` | `services.dispose_content` (an `outcome_unknown` error from `provider.redact_message` falls through to the verification `provider.fetch_message(sid)`) |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| `create_message` | committed `Notification` (reference, order, user, contact number, kind, body incl. ref tag, `outcome=sending`, `claimed_at`, `scheduled_for`) | `provider_sid`, `provider_status`, `outcome`, `error_code/message`, `provider_time` (date_sent or date_created) | `services.safe_send` (`Notification.objects.create(… outcome=OUTCOME_SENDING …)` committed, views are `non_atomic_requests` via `views.api`) → `services._send_claimed` → `services._record` |
| `update_message(status=canceled)` | committed `cancel_state=requested`, `cancel_requested_at` | `cancel_state` (done/pending/failed/unknown), `provider_status`, `outcome` | `services.call_off` (claim `update(cancel_state=CANCEL_REQUESTED, cancel_requested_at=now)` before `provider.cancel_message`; then `services._record` + `services._set(cancel_state=…)`) |
| `update_message(body="")` | committed `content_state=disposing` | `content_state=disposed` + local `body` wiped, or `unknown` | `services.dispose_content` (claim `update(content_state=CONTENT_DISPOSING)` before `provider.redact_message`; then `services._set(body='', content_state=CONTENT_DISPOSED, …)`) |

## Other decisions

- Destination: the shopper's most recently registered active number; none → no message (no row).
- Removing a number soft-deletes it (`removed_at`), and calls off any still-scheduled message to it.
- Follow-up delay: `SMS_FOLLOWUP_DELAY_HOURS` (default 72) — queued at the provider via
  `schedule_type=fixed` + `send_at` + `messaging_service_sid` + `from_=TWILIO_FROM_NUMBER`.
- Cancel-after-dispatch is allowed: pipeline gains `Dispatched`. Dispatch re-checks the order after
  scheduling the follow-up and calls it off if the order was cancelled meanwhile; a sweep (on every
  read of notifications + `manage.py sms_sweep`) re-cancels any follow-up of a cancelled order still
  `scheduled`, and resolves `unknown`/stale `sending` notifications by lookup.
- Delivery status: no callbacks possible → polled with `fetch_message` on reads and by `sms_sweep`.
- Resend: only when outcome is `failed`, not for a follow-up of a cancelled order, content not
  disposed; `Idempotency-Key` header (or `idempotencyKey` body field) required.
- Reconciliation: provider side `list_message(from_=TWILIO_FROM_NUMBER, DateSent> from-1d,
  DateSent< to+1d, page_size=1000)` through every page, narrowed in code to `[from, to)` on the
  provider time (`date_sent` else `date_created`); local side filtered on the stored provider time;
  buckets matched / provider_only / local_only / unsettled (no provider sid yet).
- Retries: none added. Every write goes out once; unknowns are settled by lookup only.
- `GET /api/orders/{id}/notifications` is owner-only for shoppers (others get 404); staff may
  inspect any order, since that is where operators find the `notificationId`s they act on.
- A message failure never fails the order operation: all SMS work is after the order commit and
  every exception from it is contained (logged without numbers/bodies).

## Assumptions & Blockers

- Minor: Lookup v2 unusable due to SDK model/live-body mismatch — using Lookup v1 (same SDK) instead.
- Minor: Twilio's enforcement of the `Idempotency-Key` header is not documented in the SDK → treated
  as not enforced (`repeat_is_safe=False`); dedupe relies on the DB claim + body-tag lookup.
- Minor: `sent` treated as pending (not delivered) — the carrier has not confirmed.
- No blockers.

## REQUIRED READING

- Client construction/lifetime, transport ownership → MUST load `python-client-initialization` (loaded)
- Error ladder, transport split, decode failures → MUST load `python-error-handling` (loaded)
- Safe write, reconciliation, timeouts, logging transport → MUST load `python-configuration-resilience` (loaded)
- Calling conventions, status mapping → MUST load `python-calling-endpoints` (loaded)
- UNSET / open enums / aliases → MUST load `python-models` (loaded)
- Stub transport tests → MUST load `python-testing` (loaded)
- Basic auth wiring → MUST load `python-authentication` (loaded)
