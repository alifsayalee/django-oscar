# Twilio SDK integration plan — order SMS notifications for the django-oscar sandbox

## Goal & scope

Add an **additive** Django app to the sandbox (`sandbox/apps/sms/`) that keeps shoppers
informed by SMS as their Oscar orders move, using the **twilio-sdk** APIMatic SDK for every
Twilio call. HTTP API under `/api/`. Reuse Oscar's own `Order`/`Line` models. Session auth;
operator actions gated on `is_staff`.

## Host application survey (conventions to imitate)

- Sandbox apps live under `sandbox/apps/<name>/` and are plain Django apps. Exemplar:
  `sandbox/apps/user/` (has `models.py`). URLs wired in `sandbox/urls.py`.
- Settings: `sandbox/settings.py`, uses `django-environ` `env = environ.Env()`. Oscar defaults
  imported. `DEBUG` on. SQLite. `ATOMIC_REQUESTS=True` (every request in a transaction).
- Auth: `django.contrib.auth`, session middleware present. Oscar `EmailBackend` + ModelBackend.
- Order placement uses `oscar.apps.order.utils.OrderCreator.place_order(basket, total,
  shipping_method, shipping_charge, user=, shipping_address=None, order_number=, status=)`.
  Basket needs a strategy (`oscar.apps.partner.strategy.Selector().strategy(user=)`).
  Shipping: `oscar.apps.shipping.methods.Free`; total via
  `oscar.apps.checkout.calculators.OrderTotalCalculator`.
- Order status pipeline (settings): Pending → {Being processed, Cancelled}; Being processed →
  {Complete, Cancelled}. `Order.set_status(new_status)`. We map dispatch→"Being processed",
  cancel→"Cancelled".

## Toolchain

- `py -3.11 -m venv venv`; `venv\Scripts\pip install -e .[test]` (done, Django 5.2.17).
- twilio-sdk installed from git (`twilio-sdk @ git+https://github.com/context-plugins/twilio-python-sdk.git@main`), v1.0.0. Import root `twilio_sdk`.
- Type check: project has no configured type checker; will run `mypy` on the new app files
  (SDK ships `py.typed`, generated under `mypy --strict`).
- Tests: `pytest` (via `.[test]`). New app tests use Django's test client + a faked SDK transport.

## Baseline checks

- Sandbox DB built: 11 products / 5 stockrecords, 2 staff users (superuser, staff), 249 countries.
  pages/ranges/offers.json + orders.json fixtures fail on FK (small catalogue) — not needed;
  our app creates its own orders. No offers loaded → no offer-application side effects.
- Read-only SDK smoke (live creds) PASSED: Lookups v1 canonicalized the CA test number to E.164
  and 404'd an invalid number; `list_message(from_=TWILIO_FROM_NUMBER)` returned messages.

## Sync vs async — SYNC

Django sandbox is a sync WSGI app. Use `twilio_sdk.Client` (sync). One long-lived, module-scoped
client instance (httpx pool), built lazily under a lock, reused across requests; never per-request.
`Client`/`AsyncClient` do not mix. Teardown obligation `close()` — the process-lifetime singleton
is closed at interpreter exit (atexit); acceptable for a dev/sandbox server.

## Configuration & credentials (read via Django settings only)

`sandbox/settings.py` reads, all via `env`, and NEVER hard-codes values:
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `TWILIO_MESSAGING_SERVICE_SID`,
`TWILIO_BASE_URL` (optional, default '').

- Auth: `account_sid_auth_token=BasicAuthCredentials(username=TWILIO_ACCOUNT_SID,
  password=TWILIO_AUTH_TOKEN)`. Auth token is a secret — never logged/returned/written to source.
- Base URL override: `TWILIO_BASE_URL`, when set, overrides ONLY the **messaging** API. Message
  ops resolve against server key `default` (`https://api.twilio.com`); Lookups against `default4`.
  So build the client with `server_config=ServerConfig(default=DefaultConfig(base_url=TWILIO_BASE_URL))`
  when set — this overrides the messaging host verbatim and leaves Lookups' host untouched.
  When unset, omit `server_config` (SDK defaults).

## Server routing (per operation)

| Operation | Server key | Host (default) | Governed by TWILIO_BASE_URL? |
| --- | --- | --- | --- |
| `lookups_v1_phone_number_api.fetch_phone_number2` | `default4` | lookups.twilio.com | No |
| `api20100401_message.create_message` | `default` | api.twilio.com | Yes |
| `api20100401_message.fetch_message` | `default` | api.twilio.com | Yes |
| `api20100401_message.update_message` | `default` | api.twilio.com | Yes |
| `api20100401_message.list_message` | `default` | api.twilio.com | Yes |

## CONTRACT SHEET (facts from SDK map + source; do not re-derive from memory)

Client: keyword-only ctor. `Client(account_sid_auth_token=..., server_config=?, timeout=30.0)`.
Every op ends with keyword-only `request_options`. All ops below are **Case B** → on error the
parsed call raises `ApiError` (`.status_code`, `.error: RawError`, `.response`); raw peer returns
`ApiResult`. **A decode failure raises pydantic `ValidationError`/`ValueError`, NOT `ApiError`,
and bypasses both response modes** — our error boundary must catch it too.

### 1. Validate + canonicalize a number — `lookups_v1_phone_number_api.fetch_phone_number2`
- Signature: `fetch_phone_number2(phone_number: str, *, country_code=None, type_=None, add_ons=None, add_ons_data=None, request_options=None)`. `phone_number` positional required.
- Returns `LookupsV1PhoneNumber`: members `phone_number` (canonical **E.164**), `national_format`,
  `country_code`, `caller_name`, `carrier`, `url` — all `OptionalNullable` (accept null).
- Validation semantics: **404** (ApiError) for a number Twilio can't parse/doesn't consider
  usable → reject the registration. Store `phone_number` (canonical E.164) as the value of record.
- **Why v1, not v2:** v2 (`fetch_phone_number3`/`LookupResponse`) declares its info sub-objects
  (`caller_name`, `sim_swap`, …) as `Optional[<Model>]` (= `<Model> | UnsetType`, no `None`).
  The live API returns them as explicit JSON `null`, so decoding the v2 response raises
  `pydantic.ValidationError` (confirmed against live creds). v1's fields are `OptionalNullable`,
  decode cleanly, and still validate + canonicalize. Both are exposed by the plugin; choosing the
  one that decodes is a design decision, NOT a gap.

### 2. Send an SMS (order placed / dispatched / cancelled / resend) — `api20100401_message.create_message`
- Route `POST /2010-04-01/Accounts/{AccountSid}/Messages.json`. Server `default`.
- Signature (relevant params): `create_message(account_sid: str, to: str, *, from_=None,
  messaging_service_sid=None, body=None, schedule_type=None, send_at=None, status_callback=None,
  ...)`. Required positional: `account_sid`, `to`. `from_` wire alias `From`.
- Immediate send: pass `account_sid`, `to`, `from_=TWILIO_FROM_NUMBER`, `body=...`.
- Returns `ApiV2010AccountMessage`: `sid`, `status` (`MessageEnumStatus` open enum), `body`,
  `from_` (alias `from`), `to`, `date_sent`, `error_code`, `error_message`, `num_segments`,
  `date_created`, `messaging_service_sid`. All `OptionalNullable`.

### 3. Schedule the delivery follow-up (queued with the provider, days later) — `create_message`
- Scheduled send needs a Messaging Service: pass `messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID`,
  `schedule_type='fixed'` (`MessageEnumScheduleType.FIXED`), `send_at=<tz-aware datetime>` (an
  `RFC3339DateTime` = `Annotated[datetime, ...]`; pass a Python tz-aware `datetime`). Do NOT pass
  `from_` with `messaging_service_sid`. Returned message has `status='scheduled'` and a `sid`.
  Provider holds the timer — nothing scheduled in this app.

### 4. Cancel a scheduled follow-up before it sends — `api20100401_message.update_message`
- Signature: `update_message(account_sid: str, sid: str, *, body=None, status=None, request_options=None)`.
- Cancel: `status='canceled'` (`MessageEnumUpdateStatus.CANCELED`). Only works while `scheduled`.

### 5. Redact message content at the provider (content disposal) — `api20100401_message.update_message`
- Redact: `update_message(account_sid, sid, body="")` — empties the body at Twilio while the
  message record (sid + delivery status) survives. Also clear our local body copy + set a flag.

### 6. Poll delivery outcome — `api20100401_message.fetch_message`
- `fetch_message(account_sid: str, sid: str)` → `ApiV2010AccountMessage` (read `status`,
  `error_code`, `error_message`). Used to refresh a notification's cached outcome on read.

### 7. Reconciliation — `api20100401_message.list_message`
- `list_message(account_sid, *, to=None, from_=None, date_sent=None, date_sent_query=None
  [wire `DateSent<`], date_sent_query_query=None [wire `DateSent>`], page_size=None, page=None,
  page_token=None)` → `ListMessageResponse` (`messages: list[ApiV2010AccountMessage]`,
  `next_page_uri`, `page`, `page_size`).
- **Filter by From at the provider**: `from_=TWILIO_FROM_NUMBER` (mandatory — count only this
  app's sending number). Date bounds: `date_sent_query_query`(DateSent>) = from,
  `date_sent_query`(DateSent<) = to; paginate (numeric `page`/`page_size`) until a short page;
  in-app keep only messages whose `date_sent` ∈ [from, to] inclusive (correctness on the bounds).
- MessageEnumStatus values: queued, sending, sent, failed, delivered, undelivered, receiving,
  received, accepted, scheduled, read, partially_delivered, canceled. "Reached" = delivered/read;
  "not reached" (resend-eligible) = failed/undelivered/canceled.

### No retries
The SDK performs no retries. We deliberately omit retry/backoff (sandbox; keep live-SMS volume
minimal). Timeout left at SDK default 30.0s. Delivery-status refresh is on-demand (fetch/list),
not a background poller (task forbids in-app timers/queues).

## Domain model (new app `sms`)

- `ContactNumber(owner=FK(User), e164=CharField, created)` — unique (owner, e164). `e164` = provider
  canonical form. Never logged.
- `OrderNotification(order=FK(order.Order), recipient=FK(ContactNumber,SET_NULL),
  to_number=CharField [E.164 snapshot for history/reconciliation], kind=choices
  {order_placed, order_dispatched, delivery_followup, order_cancelled, resend}, provider_sid,
  status, error_code, error_message, body, content_redacted=bool, is_scheduled=bool,
  idempotency_key=CharField(unique,null), created, updated)`.
  - Follow-up = kind `delivery_followup`, `is_scheduled=True`, status `scheduled`.
  - Cancel order → cancel every still-scheduled follow-up for that order at the provider.
  - Resend → new row; `idempotency_key` unique dedupes repeats (same key returns existing row).
  - Content disposal → redact at provider, clear `body`, set `content_redacted=True`; sid+status stay.

## Endpoints (all under `/api/`, session auth; JSON)

Shopper-scoped (own data only): `POST/GET /api/contact-numbers`,
`DELETE /api/contact-numbers/{id}`, `POST /api/orders`, `GET /api/my-orders`,
`GET /api/orders/{orderId}/notifications` (own order; staff any),
`DELETE /api/notifications/{notificationId}/content` (own notification; a shopper disposing content about them).
Operator-only (`is_staff`): `POST /api/orders/{orderId}/dispatch`,
`POST /api/orders/{orderId}/cancel`, `POST /api/notifications/{notificationId}/resend`,
`GET /api/notifications/reconciliation`.
(Re-examine content-disposal actor: task says "a shopper has asked" → shopper-scoped on own
notification; staff may also act. Not in the operator list.)

Response id fields: `orderId` (POST /orders), `contactNumberId` (POST /contact-numbers),
`notificationId` (POST resend + each entry of order notifications).

A messaging failure must never fail the underlying op: sends are best-effort — the order op and
the HTTP request still succeed; a shopper with no number on file is simply not messaged.

## Layering

- `sms/twilio_gateway.py` — the only module importing `twilio_sdk`. Wraps client construction
  (settings-driven, singleton) + the 7 calls; translates SDK outcomes into plain results/raises a
  narrow `TwilioError`; catches `ApiError`, `RawError`, pydantic `ValidationError`/`ValueError`,
  and httpx transport errors at this boundary. Never logs the destination number or auth token.
- `sms/services.py` — order placement (Oscar OrderCreator), notification orchestration, idempotency,
  best-effort send semantics.
- `sms/views.py` + `sms/urls.py` — thin JSON HTTP handlers, auth/staff gating, ownership checks.
- `sms/models.py`, `sms/apps.py`, migration.

## REQUIRED READING (load before coding the step it governs)
- MUST load `python-error-handling` — the error boundary in `twilio_gateway.py` (ApiError vs
  RawError vs the decode `ValidationError` that bypasses both, vs unwrapped httpx errors). [floor]
- MUST load `python-client-initialization` — before constructing `Client` (keyword-only, pool
  ownership, close(), long-lived singleton). [floor]
- MUST load `python-calling-endpoints` — before the first `client.<op>()` (positional/keyword-only
  split; parsed vs raw response mode; None-returning ops).
- MUST load `python-models` — building `send_at`/`schedule_type`, reading `from_` alias, open enums,
  `Optional`=`| UnsetType`.
- MUST load `python-testing` — before the test file (fake the transport seam, not the client). [floor]

## Assumptions & Blockers
- No blockers. v2 Lookups decode bug handled by using v1 (design decision, documented above).
- Assumption: dispatch/cancel operate on Oscar order status via the sandbox pipeline
  (dispatch→"Being processed", cancel→"Cancelled"). Minor; proceed.
- Assumption: a test shopper (is_staff=False) will be created during verification (both seeded
  users are staff). Minor; proceed.
