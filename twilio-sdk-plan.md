# Twilio SDK plan — SMS order notifications for the Oscar sandbox

Scope: new Django app `sandbox/apps/sms_notifications/`, routed under `/api/` from `sandbox/urls.py`.
SDK: `twilio-sdk` 1.0.0 (import root `twilio_sdk`), installed from
`git+https://github.com/context-plugins/twilio-python-sdk.git@main` into `venv/` (Python 3.11).

## Repo survey

| Convention | Pattern | Exemplar to imitate |
| --- | --- | --- |
| Sandbox-local apps | plain package under `sandbox/apps/`, imported as `apps.<name>` (sandbox dir is on `sys.path`) | `sandbox/apps/sitemaps.py`, `sandbox/urls.py` |
| Settings | `django-environ` `env = environ.Env()` reads, defaults in `sandbox/settings.py` | `sandbox/settings.py` (`SECRET_KEY`, `DEBUG`) |
| Order placement | Basket + `Selector().strategy()` → `Repository().get_default_shipping_method` → `OrderTotalCalculator` → `OrderCreator().place_order` → `basket.submit()` | `src/oscar/apps/checkout/mixins.py` (`handle_order_placement`) |
| Order status changes | `EventHandler.handle_order_status_change` / `order.set_status` against `OSCAR_ORDER_STATUS_PIPELINE` (Pending → Being processed → Complete; Pending/Being processed → Cancelled) | `src/oscar/apps/order/processing.py` |
| Shipping events | `EventHandler.handle_shipping_event(order, ShippingEventType, lines, quantities)` | `src/oscar/apps/order/processing.py` |
| Auth | Django session auth (`AuthenticationMiddleware`, `EmailBackend`), CSRF middleware on | `sandbox/settings.py` |
| Sync vs async | **sync** — Django under WSGI (`sandbox/wsgi.py`), no `async def` views anywhere | — |
| Tests | Django `TestCase` in the sandbox app, run via `sandbox/manage.py test apps.sms_notifications` (the repo's `tests/` tree targets `tests/settings.py`, not the sandbox) | `tests/integration/order/` for style |

Toolchain: `pip` + `venv` (`venv\Scripts\pip install -e .[test]`), no type checker configured in the repo →
`mypy --strict` installed into the venv and run on the files I add. Baseline (`DATABASE_ENGINE=django.db.backends.sqlite3`):
`pytest tests/integration/order tests/functional/checkout` → 218 passed, 2 failed (`TestConcurrentOrderPlacement` — needs PostgreSQL; pre-existing).

Credential / environment verification: all six `TWILIO_*` env vars present; `TWILIO_BASE_URL` unset (default hosts).
Read-only smoke (`scratch/smoke.py`, outside the repo) of `lookups_v2_phone_number.fetch_phone_number3`,
`messaging_v1_phone_number.list_phone_number`, `api20100401_message.list_message` and
`api20100401_account.fetch_account` → **every call answers `401`, Twilio code 20003,
"account … with status 4 is not active"**. See Assumptions & Blockers.

## Contract sheet

### Client
- Class: `TwilioSdkClient` (sync) from `twilio_sdk`. Do not mix with `AsyncTwilioSdkClient`.
- Constructor (keyword-only): `account_sid_auth_token={"username": TWILIO_ACCOUNT_SID, "password": TWILIO_AUTH_TOKEN}`
  (omitting it sends unauthenticated requests — must always be set), `timeout=10.0`,
  `server_config={"default": {"base_url": TWILIO_BASE_URL}}` **only when `TWILIO_BASE_URL` is set**
  (plus `{"default4": {"base_url": TWILIO_LOOKUPS_BASE_URL}}` when that optional app setting is set — used only to point
  lookups at a local mock). `ServerConfig` is frozen, `extra="forbid"`.
- Lifetime: one lazily built module-level client per process (built after fork, on first use), closed via `atexit`.
- Servers: every `api20100401_message.*` op → `default` (`https://api.twilio.com`, governed by `TWILIO_BASE_URL`);
  `lookups_v2_phone_number.fetch_phone_number3` → `default4` (`https://lookups.twilio.com`, NOT governed by `TWILIO_BASE_URL`).
- **No retries** anywhere in the SDK; I add none on writes (safe write settles unknowns by lookup), none on reads.
- Every keyword-only parameter has a real default (`None`) — never pass defensive `None`s.
- `request_options={"extra_headers": {...}}` wins over the endpoint's own headers. Header names lowercased.
- Each write op (`create_message`, `update_message`, `delete_message`) generates a **random** `Idempotency-Key`
  header (`uuid4()`) per call — so it is NOT a stable reference unless overridden via `extra_headers`.
  Whether Twilio de-duplicates on it is not settled by the source → not relied on (kind 2 rejected; kind 3 used).

### Operations in scope (all Case B: `ApiError.error` is always `RawError`; raw peers return `ApiResult[..., RawError]`)

| Op | Signature (positional \| keyword-only) | Returns | Used for |
| --- | --- | --- | --- |
| `client.lookups_v2_phone_number.fetch_phone_number3` | `(phone_number: str, *, fields=…, country_code=…, …, request_options=None)` | `LookupResponse` | validate + canonicalise a contact number |
| `client.api20100401_message.create_message` | `(account_sid: str, to: str, *, from_: str\|None, messaging_service_sid: str\|None, body: str\|None, schedule_type: MessageEnumScheduleTypeOrStr\|None, send_at: RFC3339DateTime\|None, …, request_options=None)` | `ApiV2010AccountMessage` | every send, and the scheduled follow-up |
| `client.api20100401_message.fetch_message` | `(account_sid: str, sid: str, *, request_options=None)` | `ApiV2010AccountMessage` | status refresh; the lookup after a may-have-landed cancel/redact |
| `client.api20100401_message.list_message` | `(account_sid: str, *, to=None, from_=None, date_sent=None, date_sent_query=None, date_sent_query_query=None, page_size=None, page=None, page_token=None, request_options=None)` | `ListMessageResponse` | reconciliation; find-by-reference after a may-have-landed send |
| `client.api20100401_message.update_message` | `(account_sid: str, sid: str, *, body: str\|None=None, status: MessageEnumUpdateStatusOrStr\|None=None, request_options=None)` | `ApiV2010AccountMessage` | cancel scheduled follow-up (`status="canceled"`); redact content (`body=""` — docstring: "To redact the text content of a Message, this parameter's value must be an empty string") |

Wire names: `from_` → form/query `From`; `messaging_service_sid` → `MessagingServiceSid`; `schedule_type` → `ScheduleType`;
`send_at` → `SendAt` (RFC3339, **tz-aware datetime required**, naive raises `ValueError` before sending);
`date_sent_query` → query `DateSent<`; `date_sent_query_query` → query `DateSent>`; `page_token` → `PageToken`;
`phone_number` → path `PhoneNumber`. Docstring: DateSent filters "accept GMT dates … `YYYY-MM-DD`" → whole-day granularity:
widen to whole UTC days, narrow back in code. `schedule_type` docstring: "For Messaging Services only: … value of `fixed` in
conjunction with the send time" → scheduled sends pass `messaging_service_sid` + `schedule_type=MessageEnumScheduleType.FIXED` + `send_at`.
Pagination: `next_page_uri` (nullable str) carries `PageToken`/`Page` query params → parse and pass back as `page_token`/`page`.

### Models (members read; all `UNSET`-defaulted — none required, so a truncated 2xx decodes cleanly and must be guarded)
- `ApiV2010AccountMessage` (`twilio_sdk/models/api_v2010_account_message.py`): `sid: OptionalNullable[str]`,
  `status: Optional[MessageEnumStatusOrStr]`, `body: OptionalNullable[str]`, `from_: OptionalNullable[str]` (wire `from`),
  `to: OptionalNullable[str]`, `date_sent: OptionalNullable[str]`, `date_created: OptionalNullable[str]`,
  `error_code: OptionalNullable[int]`, `error_message: OptionalNullable[str]`, `direction: Optional[MessageEnumDirectionOrStr]`.
  Guard after every write: `sid` must be a `str` (else outcome unknown); `status` must be set (else `unknown`).
  Dates are RFC 2822 strings (`parsedate_to_datetime`), not datetimes.
- `ListMessageResponse`: `messages: Optional[list[ApiV2010AccountMessage]]`, `next_page_uri: OptionalNullable[str]`.
- `LookupResponse`: `valid: Optional[bool]`, `phone_number: OptionalNullable[str]` (E.164 canonical),
  `country_code: OptionalNullable[str]`, `validation_errors: Optional[list[ValidationErrorOrStr]]`.
  Guard: accept only `valid is True` and `phone_number` a non-empty `str`.

### Enums (`twilio_sdk.models.enums`, all open `…OrStr`)
- `MessageEnumStatus` members → **send outcome** (`status_from_provider`):
  - done: `DELIVERED`, `READ`
  - pending (not yet): `QUEUED`, `SENDING`, `SENT`, `ACCEPTED`, `SCHEDULED`, `PARTIALLY_DELIVERED` (meaning not settled by source → not-yet)
  - failed: `FAILED`, `UNDELIVERED`, `CANCELED` (a send that was called off is undone → failed)
  - unknown: `RECEIVING`, `RECEIVED` (inbound states on an outbound message), any unlisted string, `UNSET`
- `MessageEnumStatus` → **cancel outcome** (`cancel_outcome`, the undoing step's own mapper):
  done: `CANCELED`; pending: `SCHEDULED`, `ACCEPTED`, `QUEUED` (still callable-off / not yet reflected);
  failed (too late — it went out or was already terminal): `SENDING`, `SENT`, `DELIVERED`, `READ`, `PARTIALLY_DELIVERED`, `FAILED`, `UNDELIVERED`;
  unknown: `RECEIVING`, `RECEIVED`, unlisted, `UNSET`.
- **Redaction outcome** (`redact_outcome`): read from `body` of the returned message — `""` → done; any other
  string → needs_review (provider answered but content still present); `None`/`UNSET` → unknown.
- `MessageEnumUpdateStatus`: `CANCELED` only. `MessageEnumScheduleType`: `FIXED` only.

### Errors
- All five ops are Case B: `except ApiError as e` → `e.error` is `RawError` (`.status_code`, `.text()`; `.json()` may raise).
- Decode failure → `pydantic.ValidationError`/`ValueError`, raised in both modes; on a write = outcome unknown.
- Transport: `httpx.ConnectError | ConnectTimeout | PoolTimeout | ProxyError` = never sent; other `httpx.RequestError` = may have landed.
- Boundary mapping: 401/403 → 502 (our credentials), 429 → 503, other 4xx on a caller-driven read (lookup) → 422 to the caller,
  5xx → 502; writes go through `safe_write` and answer through `answer()`.

## OPERATION OUTCOMES

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/orders` → `create_message` (placed) | `ApiV2010AccountMessage.status` | order is always `201`; per-notification `outcome`: DELIVERED/READ → `done`; QUEUED/SENDING/SENT/ACCEPTED/SCHEDULED/PARTIALLY_DELIVERED → `pending`; FAILED/UNDELIVERED/CANCELED → `failed`; RECEIVING/RECEIVED/unlisted/absent → `unknown`; no sid readable → `unknown` | `views.orders` → `services.place_order` → `services.notify` → `services._send` → `provider.safe_write(read=provider.send_answer)` → `provider.status_from_provider` |
| `POST /api/orders/{id}/dispatch` → `create_message` (dispatched) | same | order `200`; notification outcome as above | `views.dispatch_order` → `services.dispatch_order` → `services.notify(order, Notification.DISPATCHED)` → `provider.Messaging.send_now` |
| `POST /api/orders/{id}/dispatch` → `create_message` (scheduled follow-up) | same | order `200`; SCHEDULED → `pending` (its done is DELIVERED later); others as above | `services.dispatch_order` → `services.notify(order, Notification.FOLLOW_UP)` → `provider.Messaging.send_later` |
| `POST /api/orders/{id}/cancel` → `create_message` (cancelled) | same | order `200`; outcome as above | `views.cancel_order` → `services.cancel_order` → `services.notify(order, Notification.CANCELLED)` |
| `POST /api/orders/{id}/cancel` → `update_message(status=canceled)` (call off follow-up) | returned `status` | order `200`; `followUpCancellation.outcome`: CANCELED → `done`; SCHEDULED/ACCEPTED/QUEUED → `pending`; SENDING/SENT/DELIVERED/READ/PARTIALLY_DELIVERED/FAILED/UNDELIVERED → `failed` (too late); other/absent → `unknown` | `services.call_off_follow_up` → `provider.safe_write(read=provider.cancel_answer)` → `provider.cancel_outcome` |
| `POST /api/notifications/{id}/resend` → `create_message` | returned `status` | `answer_status()`: done → `200`; pending/sending → `202`; failed/needs_review → `409`; unknown → `504`; always with top-level `notificationId` | `views.resend` → `services.resend` → `services._send`; HTTP status via `provider.answer_status` |
| `DELETE /api/notifications/{id}/content` → `update_message(body="")` | returned `body` | `answer_status()`: `""` → done `200`; other string → needs_review `409`; absent → unknown `504`; refused (4xx) → failed `409`; message not yet final at the provider → `409` before any write | `views.notification_content` → `services.dispose_content` → `provider.safe_write(read=provider.redact_answer)` → `provider.redact_outcome`; HTTP status via `provider.answer_status` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| send "placed" for order N | `Notification` row, `reference = <SMS_INSTALL_ID>:order:<N>:placed` (DB) | `UNIQUE(reference)` (`models.ProviderWrite.reference`) → `IntegrityError` | `store.DjangoClaimStore.try_claim` | `services.order_reference` + `services.notify` → `provider.safe_write` → `store.DjangoClaimStore.try_claim` |
| send "dispatched" for order N | `Notification`, `…:order:<N>:dispatched` | `UNIQUE(reference)` | `store.DjangoClaimStore.try_claim` | `services.dispatch_order` → `services.notify` → `store.DjangoClaimStore.try_claim` |
| schedule follow-up for order N | `Notification`, `…:order:<N>:delivery_follow_up` | `UNIQUE(reference)` | `store.DjangoClaimStore.try_claim` | `services.dispatch_order` → `services.notify` → `store.DjangoClaimStore.try_claim` |
| send "cancelled" for order N | `Notification`, `…:order:<N>:cancelled` | `UNIQUE(reference)` | `store.DjangoClaimStore.try_claim` | `services.cancel_order` → `services.notify` → `store.DjangoClaimStore.try_claim` |
| cancel follow-up message of notification M | `NotificationAction` row, `…:notification:<M>:cancel` | `UNIQUE(reference)` | `store.DjangoClaimStore.try_claim` | `services.action_reference` + `services.call_off_follow_up` → `store.DjangoClaimStore.try_claim` |
| resend of notification M under caller key K | `Notification`, `…:notification:<M>:resend:<sha256(K)[:32]>` | `UNIQUE(reference)` | `store.DjangoClaimStore.try_claim` | `services.resend_reference` + `services.resend` → `services._send` → `store.DjangoClaimStore.try_claim` |
| redact content of notification M | `NotificationAction`, `…:notification:<M>:redact` | `UNIQUE(reference)` | `store.DjangoClaimStore.try_claim` | `services.action_reference` + `services.dispose_content` → `store.DjangoClaimStore.try_claim` |
| re-claim after a first send that never left / was refused | same row, outcome `failed` and no provider sid | conditional `UPDATE … WHERE outcome='failed' AND provider_sid IS NULL` affects exactly one row | `store.DjangoClaimStore.try_claim` | `store.DjangoClaimStore.try_claim` (the `.filter(...).update(...)`, `reclaimed == 1`) |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| every `create_message` (placed/dispatched/follow-up/cancelled/resend) | lookup: `list_message(to=<number>)` newest-first (2 pages × 50), match the body's reference token `Ref <token>` (token = first 10 hex of sha256(reference)); found → settled from its status; not found → stays `unknown`; later GETs repeat the lookup, never a new send | the `Notification.reference` (token in the body) | `provider.Messaging.find_by_reference`, passed as `find` in `services._send` and in `services.refresh` (lookup-only: its `send` is `never_resend`); run by `provider.safe_write` step 3 |
| `update_message(status=canceled)` | lookup: `fetch_message(sid)` and map its status with `cancel_outcome` | `NotificationAction.reference` (target = message sid) | `services.call_off_follow_up` (`find=lambda: messaging.fetch(sid)`) → `provider.safe_write` step 3 |
| `update_message(body="")` | same-reference resend (`repeat_is_safe=True` — blanking a body by id twice has one effect), and `fetch_message(sid)` when that answer is unreadable; mapped with `redact_outcome` | `NotificationAction.reference` (target = message sid) | `services.dispose_content` (`repeat_is_safe=True`, `find=lambda: messaging.fetch(sid)`) → `provider.safe_write` (`resending` branch, then step 3) |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| `create_message` (all kinds) | `Notification` row: reference, order, user, contact number, kind, body (with ref token), scheduled_for, `outcome='sending'`, `claimed_at` | `provider_sid`, `provider_status`, `outcome`, `provider_time`; then `date_sent`, `error_code` | `store.DjangoClaimStore.try_claim` (row built from the defaults `services.notify` / `services.resend` pass) → `store.DjangoClaimStore.complete` → `services._apply_state` |
| `update_message(status=canceled)` | `NotificationAction(kind=cancel)` row: reference, notification, target sid, `outcome='sending'` | returned status, `outcome`, `completed_at`; the follow-up's own status re-read | `services.call_off_follow_up` → `store.DjangoClaimStore.try_claim` / `.complete`, then `services._apply_state` |
| `update_message(body="")` | `NotificationAction(kind=redact)` row (reference, notification, target sid, `outcome='sending'`); local `Notification.body` already cleared | returned body → outcome; `content_disposed_at` set only when done | `services.dispose_content` (`.update(body="")` first) → `store.DjangoClaimStore.try_claim` / `.complete` → `services._mark_disposed` |

## Assumptions & Blockers

- **Blocker (environment, not SDK):** the supplied Twilio account is not active — every authenticated call returns
  `401`/code `20003` "account … with status 4 is not active". Live sends, live scheduling/cancel, and a live
  reconciliation cannot be performed from this machine. The task is headless ("never hand back"), so: build everything,
  verify end-to-end against the SDK's transport seam (unit tests) and against a local HTTP mock of the messaging + lookups
  hosts (via `TWILIO_BASE_URL` and the extra `TWILIO_LOOKUPS_BASE_URL`), and report the blocker plainly.
- Assumption: Twilio does not reliably de-duplicate on `Idempotency-Key` (not settled by the source) → kind-3 lookup via a
  reference token in the message body.
- Assumption: scheduled sends accept `from_` together with `messaging_service_sid`, so all traffic carries
  `From = TWILIO_FROM_NUMBER` (needed for the from-filtered reconciliation). Cannot be verified live (blocker above).
- Assumption: follow-up delay default 72 h (`SMS_FOLLOWUP_DELAY_MINUTES`, configurable).
- Bootstrap correction: the task's sandbox steps skip `oscar_import_catalogue`; without it the catalogue has 11
  products and `loaddata orders.json` fails with a FOREIGN KEY error. Running the Makefile's order (CSV import of the
  three `books.*.csv` files after `child_products.json`) gives 209 products, 249 countries, 1 sample order.
- Retries: none added. Reads fail fast (10 s timeout) and GET endpoints report `statusCheckFailed`; writes are
  never retried blind — an unknown outcome is settled by the lookup on a later request.
- Minor: Oscar has no "dispatched" order status; dispatch = `ShippingEvent("Dispatched")` + status `Being processed`
  (keeps `Cancelled` reachable after dispatch, which the follow-up-cancel flow requires).

## REQUIRED READING

- Client construction and lifetime → MUST load `python-client-initialization` (loaded)
- Credentials / no-auth trap → MUST load `python-authentication`
- Calling, response modes, status → outcome, `answer()` → MUST load `python-calling-endpoints` (loaded)
- `UNSET` vs `None`, open enums, wire aliases → MUST load `python-models` (loaded)
- Error ladder, transport split, decode failures → MUST load `python-error-handling` (loaded)
- Safe write, reconciliation clocks, base URL, no retries → MUST load `python-configuration-resilience` (loaded)
- Stub transport tests → MUST load `python-testing` (loaded)
