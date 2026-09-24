# Twilio SDK integration plan — sandbox SMS order notifications

## Scope

New Django app `sandbox/apps/order_notifications` (label `order_notifications`), wired into
`sandbox/urls.py` under `/api/` (outside `i18n_patterns`), settings in `sandbox/settings.py`.

| Endpoint | Who | What |
| --- | --- | --- |
| `POST /api/contact-numbers` | shopper | Lookup-validate, store canonical E.164 |
| `GET /api/contact-numbers` | shopper | own active numbers |
| `DELETE /api/contact-numbers/{id}` | shopper | soft-delete; cancel scheduled messages to it |
| `POST /api/orders` | shopper | Oscar basket → `OrderCreator.place_order`; "placed" SMS |
| `POST /api/orders/{id}/dispatch` | staff | status `Dispatched`; "on its way" SMS + scheduled follow-up |
| `POST /api/orders/{id}/cancel` | staff | status `Cancelled`; cancel scheduled follow-ups; "cancelled" SMS |
| `GET /api/my-orders` | shopper | own orders + notification states (refreshed from provider) |
| `GET /api/orders/{id}/notifications` | owner or staff | per-message state (refreshed from provider) |
| `POST /api/notifications/{id}/resend` | staff | idempotency key; re-send an undelivered message |
| `DELETE /api/notifications/{id}/content` | staff | redact body at provider + locally |
| `GET /api/notifications/reconciliation` | staff | provider list (From=TWILIO_FROM_NUMBER) vs local |

## Repo survey

- Django 5.2, WSGI, **sync**. Sandbox apps pattern: `sandbox/apps/user/` (plain module app). Settings exemplar: `sandbox/settings.py` (`env = environ.Env()`), URL exemplar: `sandbox/urls.py`.
- `ATOMIC_REQUESTS=True` → API views that call Twilio are `transaction.non_atomic_requests` and manage their own atomic blocks so a "sending" record is committed before the provider call.
- Oscar order pipeline in settings: extend with `Dispatched` (Pending/Being processed → Dispatched → Complete/Cancelled) so a dispatched order can still be cancelled.
- Toolchain: `venv` (py -3.11), `pip install -e .[test]`, SDK from git. Tests: `sandbox/manage.py test apps.order_notifications` (baseline: 0 tests). Type check: `mypy --strict` on the Twilio gateway module (SDK ships `py.typed`).
- Env: all six TWILIO_* vars present; `TWILIO_BASE_URL` unset (→ SDK default `https://api.twilio.com`).

## Smoke results (read-only, real credential)

- `lookups_v2_phone_number.fetch_phone_number3` → **2xx body fails to decode** (`ValidationError`: provider returns `null` for `caller_name`, `sim_swap`, … which `LookupResponse` types non-nullable `Optional[...]`). Not usable → use Lookup v1.
- `lookups_v1_phone_number_api.fetch_phone_number2` → works; test/unreachable/from numbers all resolve (CA / US / US) and canonical `phone_number` returned; malformed input → `ApiError` 404 (`RawError`, body code 20404).
- `api20100401_message.list_message` with `from_` + `date_sent_query`/`date_sent_query_query` → works; `next_page_uri` carries `PageToken` and `Page` query params; `date_sent` is an RFC1123 string; account already has traffic from the from-number incl. `inbound` direction rows.

## Contract sheet

Client: **sync** `TwilioSdkClient` (from `twilio_sdk`), keyword-only construction, one lazily-built module-level instance (post-fork safe), closed at `atexit`. Never mixed with `AsyncTwilioSdkClient`.

```
TwilioSdkClient(
  account_sid_auth_token=BasicAuthCredentials(username=TWILIO_ACCOUNT_SID, password=TWILIO_AUTH_TOKEN),  # MUST be set: omission = silent no-auth
  timeout=10.0,
  server_config={"default": {"base_url": TWILIO_BASE_URL}}   # ONLY when TWILIO_BASE_URL is set; ServerConfig extra="forbid"
)
```

Servers: messaging ops → `default` (`https://api.twilio.com`, overridden by `TWILIO_BASE_URL`); Lookup → `default4` (`https://lookups.twilio.com`, never overridden).

All ops below: **Case B** — `ApiError.error` is always `RawError` (`status_code`, `content`, `text()`, `json()` — `json()` may raise `ValueError`). Every keyword-only param has a real default (`None`); pass only what is used. No retries in SDK. Decode failure = `pydantic.ValidationError`/`ValueError`, not `ApiError`, in both modes. Transport errors = raw `httpx` exceptions.

| Operation | Signature (positional \| keyword-only used) | Returns | Notes |
| --- | --- | --- | --- |
| `client.lookups_v1_phone_number_api.fetch_phone_number2` | `(phone_number: str, *, country_code: str \| None)` | `LookupsV1PhoneNumber` | 404 = not a valid number → reject 422. Assert `phone_number` is `str` (all members `OptionalNullable`, no required members). |
| `client.api20100401_message.create_message` | `(account_sid: str, to: str, *, from_: str, body: str, messaging_service_sid: str, schedule_type: MessageEnumScheduleTypeOrStr, send_at: RFC3339DateTime)` | `ApiV2010AccountMessage` | wire: `To`, `From`, `Body`, `MessagingServiceSid`, `ScheduleType`, `SendAt`. Scheduling = `schedule_type=MessageEnumScheduleType.FIXED` + `send_at` (tz-aware `datetime`) + `messaging_service_sid` ("For Messaging Services only"). No idempotency/reference parameter. Assert `sid` is `str`, read `status`. |
| `client.api20100401_message.fetch_message` | `(account_sid: str, sid: str)` | `ApiV2010AccountMessage` | status refresh (no callbacks — no public URL) |
| `client.api20100401_message.update_message` | `(account_sid: str, sid: str, *, body: str \| None, status: MessageEnumUpdateStatusOrStr \| None)` | `ApiV2010AccountMessage` | `status=MessageEnumUpdateStatus.CANCELED` cancels a not-yet-sent (scheduled) message; `body=""` redacts text (docstring: "must be an empty string"). |
| `client.api20100401_message.list_message` | `(account_sid: str, *, from_: str, date_sent_query: RFC3339DateTime, date_sent_query_query: RFC3339DateTime, page_size: int, page: int, page_token: str)` | `ListMessageResponse` | wire: `From`, `DateSent<` (= `date_sent_query`), `DateSent>` (= `date_sent_query_query`), `PageSize` (≤1000), `Page`, `PageToken`. Paginate by parsing `Page`/`PageToken` from `next_page_uri` until null. |

Models (`twilio_sdk.models`), no required members anywhere → every used member asserted:

- `ApiV2010AccountMessage`: `sid`, `status: Optional[MessageEnumStatusOrStr]`, `from_` (wire `from`), `to`, `body`, `date_sent` (RFC1123 str), `date_created`, `error_code: OptionalNullable[int]`, `error_message`, `direction: Optional[MessageEnumDirectionOrStr]`.
- `ListMessageResponse`: `messages: Optional[list[ApiV2010AccountMessage]]`, `next_page_uri: OptionalNullable[str]`.
- `LookupsV1PhoneNumber`: `phone_number`, `country_code`, `national_format` (all `OptionalNullable[str]`).

Enums (`twilio_sdk.models.enums`, open — unknown strings pass through):

- `MessageEnumStatus` → outcome (the ONE mapping, `status_from_provider`):
  `DELIVERED`, `READ` → `delivered` (done) · `SENT` → `sent` (handed to carrier, not confirmed) ·
  `QUEUED`, `ACCEPTED`, `SENDING` → `pending` · `SCHEDULED` → `scheduled` ·
  `FAILED`, `UNDELIVERED` → `failed` · `CANCELED` → `canceled` (not done) ·
  `PARTIALLY_DELIVERED` → `pending` (not done) · `RECEIVING`, `RECEIVED`, anything else → `unknown`.
- `MessageEnumUpdateStatus`: `CANCELED` only. `MessageEnumScheduleType`: `FIXED` only.
- `MessageEnumDirection`: used only to drop `inbound` rows from reconciliation.

Error boundary (one place, `gateway.py`): `ApiError` 400/404/422 → caller's (lookup: invalid number 422);
401/403 → 502 config; 429 → 503; 5xx/other → 502 with `outcome_unknown` on writes;
`httpx.ConnectError|ConnectTimeout|PoolTimeout|ProxyError` → never sent (known);
other `httpx.RequestError` / `ValidationError` on a write → outcome unknown.

Unknown-outcome writes: `create_message` has no reference/idempotency field and the provider offers no search by
reference, so an unknown send is recorded `unknown` for an operator and **never auto-resent**. For a scheduled
follow-up with unknown outcome, cancellation looks it up by listing `to`+`from_` messages in `scheduled` state
(the only possible lookup). Cancel/redact are naturally idempotent → retried twice on transient failure.

## Assumptions & Blockers

- Minor: Lookup v2 unusable (SDK model drift, see smoke) → Lookup v1 basic (free) used for validation + canonical form. Not a gap: the capability is exposed via v1.
- Minor: resend idempotency is enforced by this app (unique key table), since `create_message` has no idempotency parameter in the SDK.
- Minor: follow-up delay default 72h (`TWILIO_FOLLOWUP_DELAY_MINUTES` setting, default 4320).
- Verified live (2026-09-24): scheduling with `messaging_service_sid` + `from_` = `TWILIO_FROM_NUMBER` returns
  `scheduled`; `update_message(status=CANCELED)` turns it `canceled`; `update_message(body="")` leaves the
  provider body `""` with status/date_sent intact; US destinations end `undelivered` with error 30034 (expected).

## REQUIRED READING

- Client lifetime, sync choice, close → MUST load `python-client-initialization` (loaded)
- Auth keyword must be set → MUST load `python-authentication` (loaded)
- Calls, status-not-id outcome mapping → MUST load `python-calling-endpoints` (loaded)
- UNSET vs None, open enums, isinstance narrowing → MUST load `python-models` (loaded)
- Error ladder, transport split → MUST load `python-error-handling` (loaded)
- No retries, base URL override, may-have-landed writes, logging → MUST load `python-configuration-resilience` (loaded)
- Stub transport tests → MUST load `python-testing` (loaded)
