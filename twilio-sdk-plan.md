# Twilio SDK plan — SMS order notifications for the django-oscar sandbox

## Scope

New Django app `sandbox/apps/sms_notifications` (label `sms_notifications`), routed under `/api/` from
`sandbox/urls.py` (outside `i18n_patterns`). Reuses Oscar `order.Order` / `order.Line` / `OrderCreator`,
`catalogue.Product`, `basket.Basket`, Django `auth.User`. Adds a `Dispatched` status to
`OSCAR_ORDER_STATUS_PIPELINE`.

## Repo survey (conventions + exemplar)

| Convention | Exemplar |
| --- | --- |
| sandbox-local app under `sandbox/apps/`, imported as `apps.<name>` | `sandbox/apps/user/models.py` |
| settings read via `django-environ` `env(...)` | `sandbox/settings.py` (`env.bool('DEBUG', …)`) |
| order creation = basket + strategy + `OrderCreator().place_order` + `OrderTotalCalculator` + `Free` shipping | `src/oscar/test/factories/__init__.py::create_order` |
| status transitions = `order.set_status()` validated against the pipeline | `src/oscar/apps/order/abstract_models.py::set_status` |
| Host is **sync** (Django/WSGI) → sync `TwilioSdkClient` | — |

Toolchain: `py -3.11 -m venv venv`, `venv\Scripts\pip install -e .[test]`; `twilio-sdk` installed (non-editable)
from `../marketplace/plugins/twilio/sdk/python/`. Tests: `cd sandbox && ..\venv\Scripts\python manage.py test apps.sms_notifications`.
Type check: `mypy --strict` on the app's SDK-facing module (`gateway.py`) + `mypy` on the rest.

## Credentials / environment

Settings (all via `env`, none hard-coded): `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`,
`TWILIO_MESSAGING_SERVICE_SID`, `TWILIO_BASE_URL` (optional). Plus non-secret tunables
`SMS_FOLLOWUP_DELAY_HOURS` (default 72), `SMS_HTTP_TIMEOUT` (default 10.0).

## Contract sheet

**Client**: `twilio_sdk.TwilioSdkClient` (sync). Keyword-only ctor. Lazily built module singleton in
`gateway.py` (after fork), closed via `atexit`. `account_sid_auth_token=BasicAuthCredentials(username=SID,
password=TOKEN)` — both required at startup of the client; missing → `ImproperlyConfigured` (never unauthenticated).
`timeout=SMS_HTTP_TIMEOUT` (each wait; **sync code has no whole-call limit**).
**Retries**: default policy kept (GET/HEAD/PUT/OPTIONS on 408/429/5xx/transport). Every write here is a
`POST` and therefore never re-sent by the SDK; no retry layer of our own.
**Servers**: `server_config={"default": {"base_url": TWILIO_BASE_URL}}` only when that setting is set
(messaging API = server `default`, `https://api.twilio.com`). Lookups use server `default5`
(`https://lookups.twilio.com`) and are never overridden by `TWILIO_BASE_URL`.

Every keyword-only parameter has a real default — pass only what is used, never defensive `None`s.
Error case for **all five** operations below: **Case B** → `ApiError.error` is always `RawError`
(`status_code`, `content`, `text()`, `json()` — `json()` raises `ValueError` on non-JSON).

| Operation | Positional | Keyword-only used (wire) | Returns | Server |
| --- | --- | --- | --- | --- |
| `client.lookups_v2_phone_number.fetch_phone_number2` | `phone_number` (path `PhoneNumber`) | `country_code` (`CountryCode`) | `LookupResponse` | `default5` |
| `client.api20100401_message.create_message` | `account_sid` (path), `to` (form `To`) | `from_` (`From`), `body` (`Body`), `messaging_service_sid` (`MessagingServiceSid`), `schedule_type` (`ScheduleType`, `MessageEnumScheduleType.FIXED`="fixed"), `send_at` (`SendAt`, `RFC3339DateTime`=tz-aware `datetime`) | `ApiV2010AccountMessage` | `default` |
| `client.api20100401_message.fetch_message` | `account_sid`, `sid` | — | `ApiV2010AccountMessage` | `default` |
| `client.api20100401_message.update_message` | `account_sid`, `sid` | `body` (`Body`; `""` redacts), `status` (`Status`; `MessageEnumUpdateStatus.CANCELED`="canceled") | `ApiV2010AccountMessage` | `default` |
| `client.api20100401_message.list_message` | `account_sid` | `to` (`To`), `from_` (`From`), `date_sent_query` (`DateSent<`), `date_sent_query_query` (`DateSent>`), `page_size` (`PageSize`, max 1000), `page` (`Page`), `page_token` (`PageToken`) — dates are `RFC3339DateTime` | `ListMessageResponse` | `default` |

None of these return `None`; none needs `with_raw_response` (parsed mode everywhere; status code is on `ApiError`).

**Models (all members optional → `UNSET` when absent; `OptionalNullable` may also be `None`)**
- `LookupResponse`: `valid: Optional[bool]`, `phone_number: OptionalNullable[str]` (E.164 canonical),
  `country_code`, `national_format: OptionalNullable[str]`, `validation_errors: Optional[list[ValidationErrorOrStr]]`.
  Guard: `valid is True` and `phone_number` a non-empty `str`; else reject (valid False) / unknown (UNSET).
- `ApiV2010AccountMessage`: `sid`, `body`, `to`, `from_` (wire `from`), `date_sent`, `date_created`,
  `date_updated`, `error_message`, `messaging_service_sid: OptionalNullable[str]`; `status: Optional[MessageEnumStatusOrStr]`;
  `error_code: OptionalNullable[int]`. Dates are RFC-2822 **strings** — parse with `email.utils.parsedate_to_datetime`
  (fallback ISO). Guard after create: `sid` must be a non-empty `str`, else outcome unknown.
- `ListMessageResponse`: `messages: Optional[list[ApiV2010AccountMessage]]`, `next_page_uri: OptionalNullable[str]`
  (next page = parse `Page` + `PageToken` query params out of it; stop when `None`/UNSET/empty), `page_size`.
- Enums are open (`…OrStr`): compare against `.value` strings / members, with an explicit unknown arm.

**Status meaning (`MessageEnumStatus`, wire values)**
| Value | Meaning here |
| --- | --- |
| `delivered`, `read` | done (reached the shopper) |
| `accepted`, `scheduled`, `queued`, `sending`, `sent` | not done yet |
| `failed`, `undelivered` | failed |
| `canceled` | final, never sent (desired outcome for a cancelled order's follow-up) |
| `partially_delivered`, `receiving`, `received`, any unknown value, UNSET | not done (unexpected; reported as-is) |

A returned `sid` means *accepted*, not delivered; delivery is learned only by re-reading (`fetch_message`) —
no public URL, so no status callbacks.

**Error boundary** (one function, `gateway._call`) → `ProviderError(status_code, message, outcome_unknown)`:
`ApiError` 401/403 → 502 (our credentials); 429 → 503; other 4xx → 4xx-ish domain rejection (`rejected=True`);
5xx → 502 `outcome_unknown=True`; `pydantic.ValidationError`/`ValueError` on decode → 502 unknown;
`httpx.ConnectError|ConnectTimeout|PoolTimeout|ProxyError` → 502 known-not-sent; other `httpx.RequestError` → 504 unknown.
Never log `str(e)` bodies or phone numbers; log Twilio `code` + HTTP status only.

**Reconciliation window**: provider filter `DateSent>`/`DateSent<` may match whole days → query from
(from − 1 day) to (to + 1 day), `From=TWILIO_FROM_NUMBER`, page through all pages, trim by parsed `date_sent`
to [from, to]. App side filtered by the stored provider `date_sent` (re-read from provider first for any record
with a sid and no `date_sent`). Records with no provider time and non-final status → reported as `unsettled`.
Scheduled/canceled messages have no `date_sent` → reported separately, not dropped.

## DUPLICATE CLAIMS

| Write | Where the claim is stored | What rejects the second one | Where that rejection is caught | Where in the code |
| --- | --- | --- | --- | --- |
| Order-placed SMS | `Notification` row (order, kind=`order_placed`, `resend_of IS NULL`) in SQLite/DB | partial `UniqueConstraint(order, kind) WHERE resend_of IS NULL` | `except IntegrityError` in `services._claim_notification` | `services._claim_notification` → `services._send_now` |
| Dispatched SMS + follow-up schedule | `Notification` rows kind=`dispatched` / `delivery_followup`, inserted in the same transaction as `order.set_status("Dispatched")` | same partial unique constraint | `except IntegrityError` in `services.dispatch_order` (→ 409) | `services.dispatch_order` (claim via `_claim_notification`) → `services._send_now` / `services._schedule_followup` |
| Cancelled SMS | `Notification` kind=`cancelled`, same transaction as `set_status("Cancelled")` | same partial unique constraint | `except IntegrityError` in `services.cancel_order` (→ 409) | `services.cancel_order` (claim via `_claim_notification`) → `services._send_now` |
| Follow-up cancellation | `Notification.cancel_requested_at` set by conditional `UPDATE … WHERE cancel_requested_at IS NULL` | conditional update matches 0 rows for a second caller | rowcount check in `services._cancel_scheduled` (second caller skips the provider call) | `services._cancel_scheduled` (claim: conditional update) → `gateway.cancel_message` |
| Resend | `Notification` row with `idempotency_key` (unique) inserted before the send | `UniqueConstraint(idempotency_key)` | `except IntegrityError` in `services.resend_notification` (→ return the existing row) | `services.resend_notification` (claim) → `services._send_now` |
| Content disposal | `Notification.content_disposal_requested_at` conditional `UPDATE … IS NULL` | 0 rows updated for a second caller | rowcount check in `services.dispose_content` | `services.dispose_content` (claim) → `gateway.redact_message` |
| Contact-number registration | `ContactNumber` row | `UniqueConstraint(user, phone_number)` | `except IntegrityError` in `services.register_contact_number` (→ 409) | `services.register_contact_number` (lookup is a read) → insert |

## UNKNOWN OUTCOMES

| Write | Re-read with | Reference searched by | Where in the code | Test |
| --- | --- | --- | --- | --- |
| `create_message` (immediate + scheduled + resend) | `list_message(to=, from_=)` newest page(s) | to + from + exact body + `date_created ≥ attempt_started_at − 60s`, sid not already known | `services._submit` (used by `_send_now`/`_schedule_followup`) `except ProviderError` → `services._settle_unknown` (immediately), state `unknown` otherwise; `services.settle_order` re-runs it on every GET of notifications/orders, before resend, cancel and reconciliation | `tests.SendOutcomeTests.test_read_timeout_on_create_is_settled_by_rereading` / `test_read_timeout_unmatched_stays_unknown` |
| `update_message(status=canceled)` | `fetch_message(sid)` | the message sid | `services._cancel_scheduled` `except ProviderError` → fetch; not canceled & still `scheduled` → `cancel_state=unknown`, retried by `services.settle_order` and by a repeat `POST …/cancel` | `tests.DispatchAndCancelTests.test_cancel_timeout_rechecked_and_retried` |
| `update_message(body="")` | `fetch_message(sid)` | sid; settled when `body == ""` | `services.dispose_content` `except ProviderError` → fetch; else `disposal_state=unknown`, retried on repeat DELETE | `tests.DisposalTests.test_redact_timeout_rechecked` |

## Assumptions & Blockers

- **BLOCKER (environment, not a plugin gap):** the supplied Twilio account answers every call (Lookup,
  Messages list, Account fetch) with `401`, Twilio code `20003`, "account … with status 4 is not active".
  Live verification (real delivery, real scheduled-then-cancelled follow-up, live reconciliation) is
  impossible until the account is re-activated. Running headless: proceed, verify every flow through the
  real SDK with a stub transport, and exercise the live API to show the integration degrades correctly
  (orders still succeed, 502 on number registration).
- UNVERIFIED against live: whether `DateSent>`/`DateSent<` accept a full RFC3339 datetime (SDK types them
  `RFC3339DateTime`); mitigated by ±1 day padding + trim. Whether `From` + `MessagingServiceSid` together is
  accepted for scheduling (docstring: "you may also provide a from parameter … from your Sender Pool").
- Minor: one number is messaged per shopper — the most recently registered active one.
- Bootstrap note: on this machine `oscar_import_catalogue sandbox/fixtures/*.csv` imports 198 books and is
  required for 209 products; `orders.json` fails with an FK error without it.

## REQUIRED READING (loaded before implementation)

- Client lifetime / sync choice / close — MUST load `python-client-initialization` ✅
- Credentials, never unauthenticated — MUST load `python-authentication` ✅
- Signatures, keyword-only tail, parsed vs raw — MUST load `python-calling-endpoints` ✅
- `UNSET` vs `None`, open enums, RFC3339 `send_at` — MUST load `python-models` ✅
- Error ladder, decode + transport failures — MUST load `python-error-handling` ✅
- Base URL override, retries, unknown outcomes, duplicate claims, reconciliation window — MUST load `python-configuration-resilience` ✅
- Stub transport tests — MUST load `python-testing` ✅

## Implementation status

- Retries: **kept the SDK default policy** (no `retry_options`); reads (GET) retry on 408/429/5xx/transport,
  every write (POST) is never re-sent — lost write outcomes are settled by re-reading, per the tables above.
- Unit tests: 41 passing (`manage.py test apps.sms_notifications`), real `TwilioSdkClient` over a stub transport.
- Type check: `mypy --strict` clean on `gateway.py`; `mypy` + django-stubs plugin clean on the whole app.
- Live run against the supplied account: every provider call answered 401/20003 (account inactive); orders,
  dispatch, cancel, resend idempotency and disposal all behaved as designed around that failure.
