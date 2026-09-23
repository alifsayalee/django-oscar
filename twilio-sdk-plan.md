# Twilio SDK plan — SMS order notifications for the django-oscar sandbox

## Scope

New Django app `sandbox/apps/order_notifications/` (label `order_notifications`), wired into
`sandbox/settings.py` (`INSTALLED_APPS`, `TWILIO_*` settings) and `sandbox/urls.py` (`api/`, outside
`i18n_patterns`). Endpoints: see task. Reuses Oscar `Order`/`Line`/`Basket`/`Product`/`OrderCreator`.

## Repo survey (conventions + exemplars)

| Convention | Exemplar |
| --- | --- |
| Sandbox local app lives in `sandbox/apps/<name>`, imported as `apps.<name>` | `sandbox/apps/user/models.py`, `sandbox/urls.py` (`from apps.sitemaps import …`) |
| Order creation: basket + strategy + `OrderCreator().place_order(...)`, `Free()` shipping, `OrderTotalCalculator` | `src/oscar/test/factories/__init__.py::create_order` |
| Status change: `order.set_status()` validates against `OSCAR_ORDER_STATUS_PIPELINE`, raises `InvalidOrderStatus` | `src/oscar/apps/order/abstract_models.py:143` |
| Settings read through `environ.Env` / `os.environ.get` | `sandbox/settings.py` |
| Host is **sync Django under WSGI** → **sync SDK client** | — |
| DB is SQLite with `ATOMIC_REQUESTS=True` → views that call the provider are `@transaction.non_atomic_requests` so the claim row commits before the call | — |

Toolchain: `venv` (py 3.11) with `pip install -e .[test]`; `twilio-sdk` installed from git `@main` into the venv (the
same venv; not added to `setup.py` because the sandbox is not packaged — install line is documented in the app README).
Checks: `sandbox/manage.py check`, `sandbox/manage.py test apps.order_notifications`, `mypy --strict` on the
integration module (`twilio_client.py`, `provider.py`). Baseline: Oscar's own `tests/` require PostgreSQL (connection refused at :5432) —
pre-existing, unrelated.
DB bootstrap note: `oscar_import_catalogue sandbox/fixtures/books.*.csv` DOES import 198 books and must run **before** `orders.json`
(whose line references an imported product) — otherwise `orders.json` fails with a FK error and only 11 products exist.

## Smoke results (read-only, live credential)

- `lookups_v1_phone_number_api.fetch_phone_number2`: 200 for both configured destinations; returns E.164 `phone_number`
  and `country_code`; 404 (`RawError`, code 20404) for `12345` / `abc`. Spaced input canonicalises to E.164.
- `lookups_v2_phone_number.fetch_phone_number3`: **unusable — SDK decode defect.** A 200 response raises
  `pydantic.ValidationError` because the provider returns `null` for un-requested packages (`caller_name`, `sim_swap`, …)
  and `LookupResponse` types them `Optional[Model]` (non-nullable). Not worked around; v1 used instead (same capability for
  this purpose: canonical E.164 + provider rejects unusable numbers).
- `api20100401_message.list_message(from_=…, date_sent_query=…, date_sent_query_query=…, page_size=…)`: 200; `from_` filter
  honoured server-side; results include **inbound** legs (`direction=inbound`) whose From is our number; `date_sent` is an
  RFC 2822 string (`Wed, 23 Sep 2026 08:18:13 +0000`); `next_page_uri` carries `PageToken` and `Page` query params.

## Contract sheet

Client: `twilio_sdk.TwilioSdkClient` (sync). Keyword-only ctor: `account_sid_auth_token=BasicAuthCredentials(username=SID,
password=TOKEN)`, `timeout=`, `server_config=`, `custom_http_client=`. **Omitting credentials = unauthenticated, silently**
→ `build_client()` refuses empty settings. Client is a lazily built, lock-guarded module global (WSGI), closed via `atexit`.
Transport: `LoggingTransport(HttpxClient(timeout=10.0))` passed as `custom_http_client` (so the client-level `timeout=` is
irrelevant; set on `HttpxClient`). Logs method, host, redacted path (`AC…`→`AC***`, `/PhoneNumbers/<n>`→`/PhoneNumbers/***`),
no query string, status, ms. Never headers/bodies.

Servers: messages → `default` (`https://api.twilio.com`); lookups → `default4` (`https://lookups.twilio.com`).
`TWILIO_BASE_URL` (optional) overrides **only** `default`: `server_config={"default": {"base_url": TWILIO_BASE_URL}}`.
Every keyword-only param below has a real default (`None`) — never pass defensive `None`s.
**No SDK retries.** Decision: no automatic retries on writes (create has no idempotency parameter); reads are not
retried either — every GET re-asks the provider on the next request.

| Operation (sync, parsed) | Positional | Keyword-only used | Returns | Error |
| --- | --- | --- | --- | --- |
| `client.lookups_v1_phone_number_api.fetch_phone_number2` | `phone_number: str` | — | `LookupsV1PhoneNumber` | Case B `RawError` (404 = not a usable number) |
| `client.api20100401_message.create_message` | `account_sid: str`, `to: str` | `from_` (wire `From`) = `TWILIO_FROM_NUMBER` always; `body`; for follow-up only: `messaging_service_sid`, `schedule_type=MessageEnumScheduleType.FIXED`, `send_at: datetime` (tz-aware, RFC3339DateTime) | `ApiV2010AccountMessage` | Case B `RawError` |
| `client.api20100401_message.fetch_message` | `account_sid`, `sid` | — | `ApiV2010AccountMessage` | Case B |
| `client.api20100401_message.update_message` | `account_sid`, `sid` | `status=MessageEnumUpdateStatus.CANCELED` (cancel not-yet-sent) **or** `body=""` (redact) | `ApiV2010AccountMessage` | Case B |
| `client.api20100401_message.list_message` | `account_sid` | `from_` (wire `From`), `date_sent_query` (wire `DateSent<`), `date_sent_query_query` (wire `DateSent>`), `page_size`, `page`, `page_token` | `ListMessageResponse` | Case B |

Unused optional fields on `create_message` (`content_retention`, `address_retention`, `validity_period`, `status_callback`, …):
**omit → provider/account default**; not set. `status_callback` omitted: no public URL.

Model members relied on (all `OptionalNullable`/`Optional` = may be `UNSET`; narrow with `isinstance(x, UnsetType)`):
- `LookupsV1PhoneNumber`: `phone_number: OptionalNullable[str]` (E.164), `country_code: OptionalNullable[str]`.
  Assert `phone_number` is a non-empty str, else treat as unreadable (502).
- `ApiV2010AccountMessage`: `sid`, `status: Optional[MessageEnumStatusOrStr]` (open enum), `error_code: OptionalNullable[int]`,
  `error_message`, `date_sent: OptionalNullable[str]` (RFC 2822), `date_created`, `body`, `from_` (alias `from`), `direction`.
  **After create, assert `sid` is a non-empty str** — else outcome unknown.
- `ListMessageResponse`: `messages: Optional[list[ApiV2010AccountMessage]]`, `next_page_uri: OptionalNullable[str]`.

Status map (`MessageEnumStatus`, every member by name; `case _` → `unknown`):
| member | our outcome |
| --- | --- |
| `DELIVERED`, `READ` | `delivered` (done) |
| `SENT` | `sent` (pending — handed to carrier, no receipt yet) |
| `QUEUED`, `SENDING`, `ACCEPTED` | `pending` |
| `SCHEDULED` | `scheduled` |
| `FAILED`, `UNDELIVERED` | `failed` |
| `CANCELED` | `canceled` |
| `PARTIALLY_DELIVERED`, `RECEIVING`, `RECEIVED`, unknown str | `unknown` |
Terminal: delivered, failed, canceled. Non-terminal are re-fetched on read.

Enums: `MessageEnumUpdateStatus` = {`CANCELED`}; `MessageEnumScheduleType` = {`FIXED`}; `MessageEnumDirection` =
{`INBOUND`, `OUTBOUND_API`, `OUTBOUND_CALL`, `OUTBOUND_REPLY`} — reconciliation counts only outbound members.

Error boundary (one place, `provider.py`), order matters:
1. `ApiError` 401/403 → `ProviderConfigError` (502); 429 → `ProviderUnavailable(503, unknown=False)`;
   400/404/409/422 → `ProviderRejected(status, code, message)` from `RawError.json()` guarded (fallback text);
   other → `ProviderUnavailable(502, unknown=<True for 5xx on a write>)`.
2. `pydantic.ValidationError`/`ValueError` on decode → `ProviderUnavailable(502, unknown=True)` (2xx may have landed).
3. `httpx.ConnectError|ConnectTimeout|PoolTimeout|ProxyError` → `ProviderUnavailable(502, unknown=False)`.
4. `httpx.RequestError` → `ProviderUnavailable(504, unknown=True)`.
`str(e)` never surfaced to callers; logs carry status + provider error code only (no numbers, no bodies).

## Design decisions

- **ContactNumber** (user, e164, country_code, created_at). Register = Lookup v1; 404 → 422 to caller; store provider
  `phone_number`. Unique (user, e164). Hard delete; before deleting, cancel every `scheduled` notification addressed to it;
  if a cancel cannot be confirmed the delete answers 502/504 and the number stays (nothing may be sent to it again).
  Messages go to the shopper's most recently registered number; none → not messaged (`skipped` row kept for visibility? no:
  no row — "simply not messaged"; `my-orders` shows zero notifications).
- **Notification** row = the durable claim. Fields: order FK, contact_number FK (SET_NULL), kind (placed / dispatched /
  followup / cancelled), body, resend_of FK, idempotency_key (unique, nullable), status (sending / scheduled / pending /
  sent / delivered / failed / canceled / unknown / not_sent), provider_sid (unique), provider_status, error_code, error_message,
  scheduled_for, provider_date_sent, content_disposed_at, last_checked_at. Partial unique (order, kind) where resend_of is null
  → one original per kind per order. Row committed (`status=sending`) **before** the provider call; settled from the
  provider's returned status. `NEVER_SENT`/4xx → `not_sent` (claim released for resend); unknown → `unknown` then a
  bounded lookup (`list_message(from_, date_sent>=claim-1min)` matched on `to`+`body`+not already recorded, exactly one
  candidate adopted) — else stays `unknown` (reconciliation surfaces it as provider-only).
- Order transitions: sandbox pipeline gains `Dispatched` (`Pending|Being processed → Dispatched → Complete|Cancelled`,
  cascade lines `Shipped`). Claim = `OrderTransition(order, to_status)` unique row inserted in the same atomic block as
  Oscar's `order.set_status()`; IntegrityError → no-op, no messages. `InvalidOrderStatus` → 409.
- Dispatch: send "on its way" now + create the follow-up with `schedule_type=fixed`, `send_at=now+TWILIO_FOLLOWUP_DELAY`
  (setting, default 3 days), `messaging_service_sid` + `from_`. Cancel: cancel every `scheduled` follow-up via
  `update_message(status=canceled)`; verify returned status is `canceled`; if the cancel is refused, fetch to record what
  actually happened (e.g. already sent) → surfaced in the response (`followupCancellation`). Then send "cancelled" message.
  Messaging failures never fail the order operation.
- Resend (staff): body `{"idempotencyKey": "..."}` required. Claim = new Notification with that key (unique). Same key &
  same source → return existing (200, same `notificationId`, no send). Same key & other source → 409. Source must be in
  failed/not_sent/unknown(?) — resend allowed when source outcome ∈ {failed, not_sent}; else 409. Contact number must still
  exist → else 409. Sends to that contact number.
- Content disposal (staff): source must be terminal (delivered/sent/failed/canceled/not_sent); scheduled/pending → 409.
  `update_message(body="")` then assert returned `body == ""`; local body blanked + `content_disposed_at`. Status survives.
  `not_sent` rows (no provider sid) → local blank only.
- Reconciliation (staff): `from`/`to` ISO-8601 (tz required, else 400). Provider side: `list_message(from_=TWILIO_FROM_NUMBER,
  date_sent_query_query=from, date_sent_query=to, page_size=100)`; follow `PageToken`/`Page` from `next_page_uri`, cap 50 pages,
  no-progress guard, `truncated` flag in the response. Narrow in code to `from <= date_sent < to`; keep only outbound directions
  (inbound legs counted separately as `excludedInbound`). Local side on the same clock: notifications whose provider
  `date_sent` is in range, plus unsettled rows (sid, no date_sent / unknown / scheduled / canceled) created in
  `[from - followup_delay, to)` → matched by sid. Output: matched, providerOnly, localOnly, unsettled, and records the
  provider date_sent on matched rows.
- Read refresh: `my-orders` and `orders/{id}/notifications` re-fetch non-terminal notifications (≤ 20 per request); a fetch
  failure keeps the stored state and marks `stale: true`.
- Auth: session login (Django); JSON 401 when anonymous, 403 when non-staff hits an operator endpoint. CSRF: Django's
  normal CSRF protection stays on (clients send `X-CSRFToken` from the `csrftoken` cookie); `GET /api/csrf` hands out the
  cookie; `POST /api/session` logs in (email/username + password) using Django's `authenticate`/`login`.
- Numbers never logged; API responses show numbers only to their owner.

## CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
| --- | --- | --- |
| a sid passed to `update_message`/`fetch_message` must be one `create_message` returned for this app | create → update/fetch | implementation (sids only from our Notification rows) |
| a destination passed to `create_message(to=…)` must be a canonical number `fetch_phone_number2` returned and still registered | lookup → create | implementation (send only via ContactNumber FK, re-checked at send) |
| `update_message(status=canceled)` only valid for a message whose last known status is `scheduled` | create(schedule) → update | implementation + provider refusal handled |
| `list_message(from_=…)` value must equal the `from_` every create sent (`TWILIO_FROM_NUMBER`) | create → list | implementation (single setting used for both) |
| `page_token`/`page` must come from the previous page's `next_page_uri` | list → list | implementation |

## Assumptions & Blockers

- Scheduling with both `messaging_service_sid` and `from_` — **verified live**: provider returned `scheduled`, and
  `update_message(status=canceled)` returned `canceled` (confirmed by an independent `fetch_message`).
- Redaction `update_message(body="")` — **verified live**: `fetch_message` afterwards returns `body == ""` and the status
  (`delivered`) is kept.
- US destination outcome — **verified live**: accepted (`queued`) then `undelivered`, error code 30034 → our `failed`.
- As built: resend allowed only from `failed` / `not_sent`; content disposal allowed from `delivered` / `sent` / `failed` /
  `canceled` / `not_sent` (409 otherwise).
- Minor: Lookup v1 used instead of v2 because of the v2 decode defect (reported, not worked around).
- No blockers.

## REQUIRED READING

- Client construction/lifetime — MUST load `python-client-initialization` (loaded)
- Credentials — MUST load `python-authentication` (loaded)
- Calls, status mapping — MUST load `python-calling-endpoints` (loaded)
- `UNSET`, open enums, aliases — MUST load `python-models` (loaded)
- Error ladder — MUST load `python-error-handling` (loaded)
- Durable claim, reconciliation, pagination, transport logging — MUST load `python-configuration-resilience` (loaded)
- Tests (stub transport) — MUST load `python-testing` (loaded)
