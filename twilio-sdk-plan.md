# Twilio SMS order-notifications — plan & contract sheet

Add SMS order notifications to the django-oscar **sandbox** site via the APIMatic-generated
`twilio-sdk` (import root `twilio_sdk`). Additive Django app under `sandbox/apps/`, routed at `/api/`.

## Repo survey (conventions to imitate)

- Sandbox apps live in `sandbox/apps/<name>/` (exemplar: `sandbox/apps/user/`, `sandbox/apps/sitemaps.py`).
  URLs wired in `sandbox/urls.py` (exemplar: the `path('', include(...))` block).
- Oscar models loaded via `oscar.core.loading.get_model` / `get_class` (exemplar: `sandbox/apps/sitemaps.py`).
- Order placement uses `OrderCreator.place_order` (`src/oscar/apps/order/utils.py:44`); basket built via
  `partner.strategy.Selector().strategy(user=...)`, shipping via `oscar.apps.shipping.methods.Free`,
  totals via `oscar.apps.checkout.calculators.OrderTotalCalculator`, shipping address needs a `Country`
  row (exemplar: `src/oscar/test/factories/order.py`).
- Order status pipeline (settings.py): `Pending -> {Being processed, Cancelled}`,
  `Being processed -> {Complete, Cancelled}`. **Dispatch** = set `Being processed`; **Cancel** = `Cancelled`.
- Sync stack: Django under WSGI → **sync** SDK client (`Client` / `TwilioSdkClient`).
- No test suite is configured for the sandbox; project uses stdlib/pytest elsewhere. We add unit tests
  under the app using Django's `unittest`-style `TestCase` + the SDK transport-seam stub.

## Toolchain

- `py -3.11` venv at `repo/venv`. Project installed editable with `[test]`; `twilio-sdk` installed from
  git into the same venv (import verified at `venv/Lib/site-packages/twilio_sdk`).
- Run management/tests from `sandbox/` (DJANGO_SETTINGS_MODULE defaults to `settings`).
- Type check: `venv/Scripts/python -m mypy` (mypy not preconfigured → install into venv, run on our files).

## Settings (secrets — names only, never values; read at runtime via django-environ)

In `sandbox/settings.py`, add (all default to '' so import never raises — see python-authentication):
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `TWILIO_MESSAGING_SERVICE_SID`,
`TWILIO_BASE_URL` (optional messaging-API override). The missing-credential check lives in the gateway
builder, not in settings. Auth token never logged / returned / written to a file.

## Server / base URL

`ServerConfig` has one field per server, each `{base_url: str}` with a real default (one-environment arm).
`TWILIO_BASE_URL` overrides ONLY the **messaging** API = server `default` (`https://api.twilio.com`,
used by every `api20100401_message.*` op). Lookups use server `default4` (`https://lookups.twilio.com`)
and are NOT governed by `TWILIO_BASE_URL`. Build:
`ServerConfig(default=DefaultConfig(base_url=TWILIO_BASE_URL))` when set (dict form
`{"default": {"base_url": ...}}` also valid); omit `server_config` entirely when unset. Frozen,
`extra="forbid"` → misspelled key raises ValidationError.

## Auth / client lifetime

HTTP Basic: `account_sid_auth_token=BasicAuthCredentials(username=ACCOUNT_SID, password=AUTH_TOKEN)`.
No OAuth → **no token pre-fetch** before calls (simplifies error boundary & tests). Client is
long-lived: lazily-initialised module global in `apps/ordersms/twilio_gateway.py`, `timeout=15.0`.
Close via `atexit`.

## In-scope operations (contract sheet — all sync; async peer not used)

All ops: keyword-only boundary after positional path params; every keyword has a real default (no
defensive `None`s). All in-scope ops are **Case B**: `.error` is always `RawError` (no typed arms),
so the error boundary narrows on `e.status_code` only. **No in-scope op returns `None`.**

1. `lookups_v1_phone_number_api.fetch_phone_number2(phone_number: str)` → `LookupsV1PhoneNumber`.
   Server `default4`. Purpose: validate + canonicalize a contact number.
   - Smoke result: valid → 200, `phone_number` = canonical E.164 (`+18254751588`); malformed → **404**
     (`ApiError`, `RawError`). Well-formed-but-undeliverable US number → **200** (registerable; the
     carrier refuses at *send* time, surfacing as message status, not here).
   - Rule: 404 → reject registration; 200 → store `.phone_number`. Assert `.phone_number is not UNSET`
     (truncated body ⇒ outcome unknown).
   - v2 (`fetch_phone_number3`) is NOT usable: its response nests `caller_name`/`sim_swap`/… typed
     `Optional[X]` (no `None` arm) and Twilio returns them as `null`, so every response fails to decode
     (`ValidationError`). v1 types those `OptionalNullable[Any]` and decodes cleanly. This is a design
     choice between two working ops, not a gap.

2. `api20100401_message.create_message(account_sid: str, to: str, *, from_: str|None=None, body: str|None=None, messaging_service_sid: str|None=None, schedule_type: MessageEnumScheduleTypeOrStr|None=None, send_at: RFC3339DateTime|None=None, ...)` → `ApiV2010AccountMessage`. Server `default` (overridable).
   - **Immediate** notification (placed/dispatched/cancelled/resend): pass `from_=TWILIO_FROM_NUMBER`,
     `body=...`. Wire fields: `To`, `From`, `Body`.
   - **Scheduled follow-up** (delivery survey): pass `messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID`,
     `schedule_type=MessageEnumScheduleType.FIXED` (wire `fixed`), `send_at=<aware datetime>`, `body=...`.
     Twilio rule (docstring/domain): scheduling requires a Messaging Service and `send_at` 15 min–7 days
     out; `from_` must NOT be combined with it. Use now+3 days.
   - Read back: `.sid` (assert `is not UNSET` — provider identifier), `.status`
     (`MessageEnumStatus` open enum, wire values), `.error_code`, `.error_message`.
     `RFC3339DateTime` = `datetime.datetime` alias — pass aware datetime.

3. `api20100401_message.update_message(account_sid: str, sid: str, *, body: str|None=None, status: MessageEnumUpdateStatusOrStr|None=None)` → `ApiV2010AccountMessage`. Server `default`.
   - **Cancel** scheduled follow-up: `status=MessageEnumUpdateStatus.CANCELED` (wire `canceled`). Only
     works while status is `scheduled`.
   - **Content disposal / redaction**: `body=""` → Twilio redacts the body at the provider while keeping
     the record and its status (the "fact survives, text gone" requirement). We do NOT use
     `delete_message` (that removes the record ⇒ fact would not survive).

4. `api20100401_message.fetch_message(account_sid: str, sid: str)` → `ApiV2010AccountMessage`. Server
   `default`. Purpose: refresh the current delivery outcome on read endpoints.

5. `api20100401_message.list_message(account_sid: str, *, from_: str|None=None, date_sent_query: RFC3339DateTime|None=None, date_sent_query_query: RFC3339DateTime|None=None, page: int|None=None, page_size: int|None=None, page_token: str|None=None)` → `ListMessageResponse` (`.messages: list[ApiV2010AccountMessage]`, `.next_page_uri`). Server `default`.
   - Reconciliation: `from_=TWILIO_FROM_NUMBER` (ask provider for that number's traffic — do NOT filter
     after), `date_sent_query`=`DateSent<`=`to`, `date_sent_query_query`=`DateSent>`=`from`. Twilio
     date filters are whole-day granular → widen the query by ±1 day then **narrow in code** to the
     exact `[from,to)` window (python-configuration-resilience). Paginate with a `MAX_PAGES` bound;
     stop when `next_page_uri` is UNSET/None; report `truncated` if the cap stops us.

## Error boundary (python-error-handling; all Case B)

One translation layer in the gateway. `ApiError`: `404` on lookup → `InvalidPhoneNumber` (caller fault);
`401/403/429` → provider-config/quota (ours); other → provider failure. `ValidationError` → unreadable
(outcome unknown). httpx `ConnectError/ConnectTimeout/PoolTimeout/ProxyError` → never-sent (known);
other `httpx.RequestError` → outcome-unknown. **Messaging failures must never fail the order op**: the
notification service catches gateway errors, records the notification row as failed/unknown, and the
order op still succeeds. A shopper with no number → not messaged.

## Data model (app `ordersms`; reuse Oscar Order/Line)

- `ContactNumber(user FK, e164 str, created)` — unique(user, e164). Number never logged.
- `OrderNotification(order FK→order.Order, kind, to_number, provider_sid, status, error_code,
  error_message, is_followup bool, scheduled_send_at, content_disposed bool, resend_of self-FK,
  idempotency_key unique-null, provider_date_sent, created, updated)`. `kind ∈ {placed, dispatched,
  cancelled, delivery_survey, resend}`. Stores provider identifier + last known outcome so later
  requests can act/report. Phone numbers stored in DB (not logs).

## Idempotency & concurrency

- `create_message` has **no** idempotency parameter → app-level. Resend: unique `idempotency_key`;
  claim-first (insert row, catch IntegrityError → return existing) then send (python-configuration-resilience).
- Cancel/dispatch are no-op-safe: gate side effects on a conditional status write
  (`Order.objects.filter(pk=...).exclude(status=target).update(...)` returns rowcount) so a repeat
  doesn't re-message. Cancelling calls off the pending follow-up before it sends.

## Authorization

Session login (Django). Shopper endpoints act only on `request.user`'s own data (contact numbers,
orders, notifications scoped by ownership). Operator endpoints (dispatch, cancel, resend, content
disposal, reconciliation) require `is_staff`.

## Endpoints (all under /api/, each separately invocable)

POST/GET `/api/contact-numbers`, DELETE `/api/contact-numbers/{id}`; POST `/api/orders`,
POST `/api/orders/{id}/dispatch`, POST `/api/orders/{id}/cancel`, GET `/api/my-orders`,
GET `/api/orders/{id}/notifications`; POST `/api/notifications/{id}/resend`,
DELETE `/api/notifications/{id}/content`, GET `/api/notifications/reconciliation?from&to`.
Response id fields: `orderId`, `contactNumberId`, `notificationId`.

## CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| a `to` accepted for send must be a canonical number the lookup returned | `create_message` ← `fetch_phone_number2` | implementation (register validates+canonicalizes; only stored numbers are messaged) |
| the `sid` fetch/update/cancel/redact act on must be one `create_message` returned | `fetch_message`/`update_message` ← `create_message` | implementation (provider_sid stored on OrderNotification) |
| reconciliation `from_` filter value = `TWILIO_FROM_NUMBER`, and only immediate notifications carry that From | `list_message` ← `create_message` | implementation (immediate sends set from_=FROM_NUMBER; scheduled follow-ups go via messaging service and are out of the From-scoped report by design) |
| `orderId` used by dispatch/cancel/notifications is an Order the caller placed | order ops ← `POST /orders` | implementation (Order.user == request.user; staff for operator ops) |
| `notificationId` resend/dispose act on is an app OrderNotification | notification ops ← notifications list | implementation (DB scoping) |

## REQUIRED READING (loaded)

- MUST load `python-error-handling` — error boundary (loaded).
- MUST load `python-client-initialization` — client lifetime/global (loaded).
- MUST load `python-authentication` — Basic auth, secret loading (loaded).
- MUST load `python-calling-endpoints` — call shapes, status-not-success (loaded).
- MUST load `python-models` — UNSET/OptionalNullable, open enums, RFC3339 datetime (loaded).
- MUST load `python-configuration-resilience` — server override, no retries, pagination, reconciliation clocks, idempotency claim-first (loaded).
- MUST load `python-testing` — transport-seam stub (loaded before tests).

## Assumptions & blockers

- No blockers. v2-lookup decode failure resolved by using v1 (design choice). Follow-ups via messaging
  service is required by Twilio's scheduling rule; consequence for the From-scoped reconciliation is
  documented above. Headless — proceeding.
