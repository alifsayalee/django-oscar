# Twilio SDK integration plan — order SMS notifications (sandbox)

## Scope

New Django app `sandbox/apps/order_notifications` (label `order_notifications`), wired into
`sandbox/urls.py` under `/api/`. Reuses Oscar's `order.Order` / `order.Line` / `basket.Basket` /
`catalogue.Product` / `order.ShippingEvent`. Session auth (Django login), CSRF enforced, staff-only
operator endpoints.

## Repo survey (conventions)

| Concern | Pattern | Exemplar |
| --- | --- | --- |
| Sandbox-local app | module under `sandbox/apps/`, imported as `apps.<name>` (sandbox dir is on sys.path) | `sandbox/apps/sitemaps.py` |
| Oscar model access | `get_model('order', 'Order')` / `get_class(...)` | `sandbox/apps/sitemaps.py` |
| Settings from env | `env = environ.Env()`; `env.str(...)` | `sandbox/settings.py` |
| Order placement | `OrderCreator().place_order(basket, total, shipping_method, shipping_charge, user=...)` | `src/oscar/apps/order/utils.py` |
| Status transitions | `order.set_status()` validated by `OSCAR_ORDER_STATUS_PIPELINE` | `src/oscar/apps/order/abstract_models.py` |
| Tests | pytest + pytest-django, plain `assert` | `tests/integration/order/test_models.py` |

- **Sync vs async**: Django under WSGI, sync views → **sync `TwilioSdkClient`** only.
- Toolchain: `py -3.11 -m venv venv`; `venv\Scripts\pip install -e .[test]`; SDK installed (non-editable)
  from `<plugin>/sdk/python/`. Tests: `cd sandbox && ..\venv\Scripts\python manage.py test apps.order_notifications`
  (the repo's pytest config targets `tests/` with its own settings, which do not load sandbox apps).
  Type check: `mypy --strict` on `twilio_gateway.py` (the only module importing the SDK; project has no
  mypy config). Lint: `flake8 --config setup.cfg`.

## Contract sheet

Client: `from twilio_sdk import TwilioSdkClient`; `from twilio_sdk.core import ApiError, RawError,
BasicAuthCredentials, UNSET, UnsetType`. Constructor is keyword-only:
`TwilioSdkClient(server_config=..., timeout=10.0, account_sid_auth_token=BasicAuthCredentials(username=ACCOUNT_SID, password=AUTH_TOKEN))`.
Credentials keyword MUST be set (omission = silent unauthenticated requests) — gateway refuses to
build without SID/token. Held as a lazily-built process-wide singleton (built after fork on first use),
closed via `atexit`.

Server config (`ServerConfig`, frozen, `extra="forbid"`): messaging calls use server `default`
(`https://api.twilio.com`). When `settings.TWILIO_BASE_URL` is set → `server_config={"default": {"base_url": TWILIO_BASE_URL}}`
(verbatim). Lookups uses `default5` (`https://lookups.twilio.com`) and is NOT overridden.

Retries: default policy kept (GET/HEAD/PUT/OPTIONS on 408/429/5xx, max 3). All POSTs (create,
update) are therefore never auto-retried — correct for create_message (non-idempotent). No own retry
layer. Timeout 10 s per wait; sync code has no whole-call bound.

All keyword-only params below have real defaults (`None`); pass only what's used — no defensive `None`s.
Every operation here is **Case B**: `ApiError.error` is always `RawError` (`status_code`, `text()`,
`json()` may raise `ValueError`). Decode failures raise `pydantic.ValidationError`/`ValueError` (not
ApiError); transport errors arrive as raw `httpx` exceptions.

| Operation | Signature (positional \| after `*`) | Returns | Members relied on |
| --- | --- | --- | --- |
| `client.api20100401_message.create_message` | `(account_sid: str, to: str, *, from_: str, messaging_service_sid: str, body: str, schedule_type: MessageEnumScheduleTypeOrStr, send_at: RFC3339DateTime(aware datetime))` — wire `To`,`From`,`MessagingServiceSid`,`Body`,`ScheduleType`,`SendAt` (form) | `ApiV2010AccountMessage` | `sid` (assert not UNSET/None → else outcome unknown), `status`, `from_` (wire `from`), `date_created`, `date_sent`, `error_code`, `error_message` |
| `client.api20100401_message.fetch_message` | `(account_sid, sid)` GET | `ApiV2010AccountMessage` | `status`, `error_code`, `error_message`, `date_sent`, `body` |
| `client.api20100401_message.update_message` | `(account_sid, sid, *, body: str, status: MessageEnumUpdateStatusOrStr)` POST | `ApiV2010AccountMessage` | `status` (== `canceled` after cancel), `body` (== "" after redact) |
| `client.api20100401_message.list_message` | `(account_sid, *, from_: str, date_sent_query: RFC3339DateTime  /*wire DateSent<*/, date_sent_query_query: RFC3339DateTime /*wire DateSent>*/, page_size: int, page: int, page_token: str)` GET | `ListMessageResponse` | `messages: list[ApiV2010AccountMessage]`, `next_page_uri` (OptionalNullable[str]; parse its `Page`/`PageToken` query to fetch the next page) |
| `client.lookups_v2_phone_number.fetch_phone_number2` | `(phone_number: str)` GET (no `fields` → basic validation, free) | `LookupResponse` | `valid: Optional[bool]`, `phone_number: OptionalNullable[str]` (E.164 canonical), `validation_errors` |

Semantics (from docstrings): scheduling is Messaging-Service-only — `schedule_type="fixed"` +
`send_at` + `messaging_service_sid`; `from_` may name a specific sender in the service's pool (we pass
`TWILIO_FROM_NUMBER` so every message is attributable to our number for reconciliation).
`update_message(status="canceled")` cancels a not-yet-sent (scheduled) message;
`update_message(body="")` redacts content. `list_message` filters by sender server-side via `From`.

Enums (`twilio_sdk.models.enums`, open `…OrStr`): `MessageEnumStatus`: queued, sending, sent, failed,
delivered, undelivered, receiving, received, accepted, scheduled, read, partially_delivered, canceled.
`MessageEnumUpdateStatus`: CANCELED="canceled". `MessageEnumScheduleType`: FIXED="fixed". Unknown values
arrive as plain `str` — stored verbatim.

UNSET handling: every response member is `Optional`/`OptionalNullable` → resolve via `isinstance(x, UnsetType)`
to `None` before persisting; never hand raw members to JSON/ORM.

Error boundary (one function, `twilio_gateway._map_*`):
- `ApiError` 401/403 → `ProviderError(502, "provider refused our credentials")`; 429 → 503;
  other 4xx → rejected (outcome known, `rejected=True`, carries Twilio `code` if body is JSON);
  5xx → 502 outcome_unknown.
- `ValidationError`/`ValueError` on decode → 502 outcome_unknown.
- `httpx.ConnectError|ConnectTimeout|PoolTimeout|ProxyError` → 502 never sent (outcome known).
- `httpx.RequestError` (other) → 504 outcome_unknown.
Notification rows record `send_state`: `sent` / `rejected` (known not sent) / `unknown` (may have
landed; never auto-resent) / `not_sent`.

## DUPLICATE CLAIMS

| Write | Where the claim is stored | What rejects the second one | Where that rejection is caught | Where in the code |
| --- | --- | --- | --- | --- |
| Order lifecycle SMS (placed / dispatched / follow-up / cancelled) | `Notification` row (kind, order) inserted in the same DB transaction as the status change | `UniqueConstraint(order, kind) WHERE resend_of IS NULL` (DB unique index `order_notifications_one_lifecycle_message_per_kind`) | `except IntegrityError` in `services.dispatch_order` / `services.cancel_order` → 409 | `services._claim` → `TwilioGateway.send_sms` (called from `services._deliver`) |
| Actually sending a claimed notification (two workers picking the same row) | `Notification.send_state` | conditional `UPDATE … WHERE send_state='pending'` affecting 0 rows | `if not claimed: return` in `services._deliver` | `services._deliver` (claim UPDATE) → `TwilioGateway.send_sms` |
| Operator resend | `Notification` row with `resend_of`, `idempotency_key` | `UniqueConstraint(resend_of, idempotency_key)` (`order_notifications_unique_resend_key`) | `except IntegrityError` in `services.resend_notification` → returns earlier result (claim released only when Twilio definitively refused) | `services.resend_notification` (claim create) → `TwilioGateway.send_sms` (via `services._deliver`) |

## Assumptions & Blockers

- **Environment blocker (not an SDK/plugin gap):** the read-only smoke (lookups `fetch_phone_number2`,
  `list_message`, `fetch_account`) against the supplied credentials answers `401` with Twilio code
  `20003` "account … with status 4 is not active" on every host. The account itself is inactive, so no
  live call can succeed; implementation proceeds, verified against the SDK's transport seam, and the live
  check is re-run at the end. Unverified live facts: `DateSent>`/`DateSent<` accepting a full RFC3339
  datetime (the app also trims to the exact range within the sender-filtered answer), and
  `TWILIO_FROM_NUMBER` being in the Messaging Service's sender pool.
- Minor assumptions: "dispatched" is a new order status added to the sandbox pipeline;
  the shopper's most recently registered number is the notification destination; follow-up is sent
  3 days after dispatch (setting `ORDER_NOTIFICATIONS_FOLLOW_UP_DELAY`).
- US destinations are expected to end `undelivered` for this account — handled as an outcome.

## REQUIRED READING

- Client lifetime / placement → MUST load `python-client-initialization` (loaded)
- Credentials → MUST load `python-authentication` (loaded)
- Calls, raw vs parsed → MUST load `python-calling-endpoints` (loaded)
- UNSET / open enums / aliases → MUST load `python-models` (loaded)
- Error ladder → MUST load `python-error-handling` (loaded)
- Base URL, retries, timeout → MUST load `python-configuration-resilience` (loaded)
- Tests with a fake transport → MUST load `python-testing` (loaded)
