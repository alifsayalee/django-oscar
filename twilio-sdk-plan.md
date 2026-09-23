# Twilio SDK plan — order SMS notifications for the django-oscar sandbox

## Scope

New Django app `sandbox/apps/order_sms/` (label `order_sms`), wired into `sandbox/urls.py` under `/api/`
(outside `i18n_patterns`). All Twilio traffic goes through `twilio_sdk` (distribution `twilio-sdk` 1.0.0,
installed into `venv/` from `git+https://github.com/context-plugins/twilio-python-sdk.git@main`).

## Repo survey

| Convention | Exemplar |
| --- | --- |
| Sandbox-local apps live in `sandbox/apps/<name>`, imported as `apps.<name>` | `sandbox/apps/sitemaps.py`, `sandbox/urls.py` |
| Settings read env via `django-environ` (`env = environ.Env()`) / `os.environ.get` | `sandbox/settings.py` |
| Oscar classes are loaded dynamically with `get_class` / `get_model` | `src/oscar/apps/basket/middleware.py` |
| Order placement = basket → `OrderTotalCalculator` → `OrderCreator.place_order` → `basket.submit()` | `src/oscar/apps/checkout/mixins.py` |
| Status transitions use the host's `Order.set_status` (validates pipeline, records `OrderStatusChange`) | `src/oscar/apps/order/abstract_models.py` |
| `ATOMIC_REQUESTS=True` — API views must be `transaction.non_atomic_requests` so claim rows commit **before** provider calls | `sandbox/settings.py` |
| Auth = Django session (`django.contrib.auth.login`); CSRF middleware on | `sandbox/settings.py` |
| Host is **sync** (Django under WSGI) → **sync** `TwilioSdkClient` | — |

Toolchain: `py -3.11` venv at `venv/`, `pip install -e .[test]`; tests for the new app run with
`venv\Scripts\python sandbox/manage.py test apps.order_sms` (sandbox settings, sqlite test DB). Type check:
`mypy --strict` on the Twilio gateway module (`sandbox/apps/order_sms/twilio_gateway.py`), non-strict + django-stubs
elsewhere. Baseline: new app, nothing to regress; project `tests/` suite untouched.

## Smoke results (read-only, from scratch dir, real credentials)

* `lookups_v2_phone_number.fetch_phone_number3` → **decode failure** (`ValidationError`): the API returns `null` for
  `caller_name`, `sim_swap`, … which `LookupResponse` types as `Optional[...]` (non-nullable). Not usable in this SDK
  version. **Decision: use Lookup v1 (`lookups_v1_phone_number_api.fetch_phone_number2`)**, which decodes cleanly:
  canonical E.164 `phone_number` + `country_code`; unusable input → `404` (Twilio code 20404). Not a gap.
* `api20100401_message.list_message(from_=…, date_sent_query_query=dt, date_sent_query=dt)` → OK, datetimes accepted
  (sent as `DateSent>` / `DateSent<`). Returned rows include **inbound** legs (`direction=inbound`) whose From is our
  number (destination is on the same account) → reconciliation keeps `outbound-*` only and reports inbound legs as
  excluded. Timestamps arrive as RFC 1123 strings (`Wed, 23 Sep 2026 09:15:57 +0000`) — parse with
  `email.utils.parsedate_to_datetime`. A canceled scheduled message has `date_sent = None`.

## Client

* `TwilioSdkClient(account_sid_auth_token=BasicAuthCredentials(username=SID, password=TOKEN), timeout=10.0,
  server_config=…, custom_http_client=LoggingTransport(HttpxClient(timeout=10.0)))` — keyword-only.
* Built lazily on first use (module singleton + lock), **never at import**; `build_client()` raises
  `TwilioNotConfigured` if `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`/`TWILIO_FROM_NUMBER` settings are empty. Closed via
  `atexit`. Tests inject a stub transport through `set_client_for_tests()`.
* `server_config`: when `settings.TWILIO_BASE_URL` is non-empty → `{"default": {"base_url": TWILIO_BASE_URL}}`
  (messaging API server `default` = `https://api.twilio.com`). Lookups use server `default4`
  (`https://lookups.twilio.com`) and are **not** governed by `TWILIO_BASE_URL`.
* Omitting `account_sid_auth_token` = unauthenticated requests → always set; checked in `build_client()`.
* Logging transport logs method + host + path with digit runs ≥6 masked, **no query string, no headers, no body**
  (Lookup path and `To` query carry the shopper's number).
* **No retries in the SDK.** Decision: no automatic retries on writes (duplicate SMS risk); reads are not retried
  either (user-facing path, 10 s timeout); stale reads are retried on the next GET.

## Contract sheet (sync parsed spelling; the raw peer via `.with_raw_response` takes the same params)

All five message ops: server `default`; auth `account_sid_auth_token`; **Case B — `ApiError.error` is always
`RawError`** (`status_code`, `content`, `text()`, `json()` — `json()` raises `ValueError` on non-JSON).
Every keyword-only param has a real default (`None`); never pass defensive `None`s.
Every message op header carries a generated `Idempotency-Key: uuid4()`; overridable per call through
`request_options={"extra_headers": {"idempotency-key": …}}` (extra_headers win; names lowercased).
Provider enforcement of that header: **not documented in the SDK** → treated as best-effort only; our dedupe is the
durable DB claim.

| Operation | Positional | Keyword-only used | Returns |
| --- | --- | --- | --- |
| `client.api20100401_message.create_message` | `account_sid: str`, `to: str` | `from_: str` (wire `From`), `body: str` (`Body`), `messaging_service_sid: str` (`MessagingServiceSid`, only for scheduled), `schedule_type: MessageEnumScheduleTypeOrStr` (`ScheduleType`, `MessageEnumScheduleType.FIXED` = `"fixed"`, "Messaging Services only", with `send_at`), `send_at: RFC3339DateTime` (`SendAt`, tz-aware `datetime` required — naive raises), `request_options` | `ApiV2010AccountMessage` |
| `client.api20100401_message.fetch_message` | `account_sid`, `sid` | `request_options` | `ApiV2010AccountMessage` |
| `client.api20100401_message.update_message` | `account_sid`, `sid` | `status: MessageEnumUpdateStatusOrStr` (`Status`; only member `CANCELED="canceled"` — cancels not-yet-sent), `body: str` (`Body`; `""` redacts the text — `None` is skipped on the wire, `""` is sent) | `ApiV2010AccountMessage` |
| `client.api20100401_message.list_message` | `account_sid` | `from_` (`From`), `to` (`To`), `date_sent_query_query: RFC3339DateTime` (wire **`DateSent>`** = on/after), `date_sent_query: RFC3339DateTime` (wire **`DateSent<`** = on/before), `page_size: int` (≤1000), `page: int`, `page_token: str` (`PageToken`) | `ListMessageResponse` |
| `client.lookups_v1_phone_number_api.fetch_phone_number2` | `phone_number: str` (path) | `country_code: str` (`CountryCode`, for national-format input) | `LookupsV1PhoneNumber` (server `default4`) |

Omitted optional create fields (provider defaults stand): `status_callback` (no public URL — must stay unset),
`validity_period`, `content_retention`, `address_retention`, `smart_encoded`, `shorten_urls`, `risk_check`,
`attempt` (import/migration-style counter — unset), `send_as_mms`, `media_url`, `content_sid`.

### Response members asserted on

* `ApiV2010AccountMessage` (all `Optional`/`OptionalNullable`, nothing required → a truncated 2xx decodes clean):
  `sid` (must be a `str`, else outcome **unknown**), `status: MessageEnumStatusOrStr` (open enum), `error_code:
  int|None`, `error_message: str|None`, `date_created: str|None`, `date_sent: str|None` (RFC 1123 strings),
  `from_` (wire `from`), `to`, `direction: MessageEnumDirectionOrStr`, `body: str|None`.
* `ListMessageResponse`: `messages: list[ApiV2010AccountMessage]`, `next_page_uri: str|None` (carries `Page` and
  `PageToken` query params for the next call).
* `LookupsV1PhoneNumber`: `phone_number: str|None` (canonical E.164 — stored), `country_code: str|None`.
  Missing `phone_number` on a 2xx → treat as provider unreadable (502), never store caller input.

### `MessageEnumStatus` → local outcome (the ONE mapping function; default arm = `unknown`)

| member | wire | outcome |
| --- | --- | --- |
| `DELIVERED`, `READ` | delivered, read | `delivered` |
| `QUEUED`, `SENDING`, `SENT`, `ACCEPTED` | … | `pending` (accepted, not confirmed) |
| `SCHEDULED` | scheduled | `scheduled` |
| `FAILED`, `UNDELIVERED` | failed, undelivered | `failed` |
| `CANCELED` | canceled | `canceled` |
| `PARTIALLY_DELIVERED`, `RECEIVING`, `RECEIVED`, any unlisted string | … | `unknown` |

Local-only states: `sending` (claimed, no answer yet → `pending`), `rejected` (API refused / never sent →
`failed`), `unknown` (may have landed, could not confirm → `unknown`).

### Error boundary (one ladder, `twilio_gateway.py`)

1. `ApiError`: 401/403 → `ProviderConfigError` (502); 429 → `ProviderUnavailable(503, outcome_unknown=False)`;
   404 → `ProviderRejected(404)`; 400/409/422 → `ProviderRejected(status, code)`; 5xx & every other status →
   `ProviderFailure(502, outcome_unknown=True on writes)`. Provider `code` read from `RawError.json()` guarded
   by `ValueError`. Error text is masked (digit runs) before logging, never returned verbatim.
2. `pydantic.ValidationError` / `ValueError` from decoding → `ProviderUnreadable` (outcome unknown on writes).
3. `httpx.ConnectError | ConnectTimeout | PoolTimeout | ProxyError` → `ProviderUnavailable(502, outcome_unknown=False)`.
4. `httpx.RequestError` → `ProviderUnavailable(504, outcome_unknown=True)`.
   `TwilioNotConfigured` → never sent.

## Design

Models (app `order_sms`):
* `ContactNumber(user, phone_number [canonical E.164], country_code, created_at)`, unique `(user, phone_number)`.
  Messages go to the shopper's most recently registered number. Hard delete; before delete, every scheduled
  follow-up aimed at it gets `cancel_requested_at` and is canceled at the provider.
* `Notification(order FK oscar Order, user, contact FK SET_NULL, kind, ref uuid unique, body, status, provider_sid
  unique null, error_code, error_message, send_at, provider_date_created, provider_date_sent, cancel_requested_at,
  content_redacted_at, resend_of FK self null, idempotency_key, created_at, updated_at, last_synced_at)`.
  Constraints: unique `(order, kind)` where `resend_of IS NULL` (one automatic message per order event — the
  durable claim); unique `(resend_of, idempotency_key)` (resend claim).
* `OrderTransitionClaim(order, to_status)` unique — inserted in the same transaction as `order.set_status(...)`;
  only the inserting request sends messages.
* Sandbox pipeline gains `Dispatched` (`Pending|Being processed → Dispatched → Complete|Cancelled`), cascade
  `Dispatched → Shipped` (additive).

Send (claim → call → reconcile → settle): row inserted `status=sending` and committed; body carries `[ref xxxxxxxx]`;
provider call with `idempotency-key` = row ref. `outcome_unknown` failures → `find_by_ref` = `list_message(to, from_,
DateSent>=claim-2min)` matching the ref tag in `body`; found → settle, not found → `unknown` (never `failed`).
Never-sent / rejected → `rejected` with code. Settling a follow-up re-reads `cancel_requested_at` and cancels if set.

Follow-up: `create_message(..., messaging_service_sid=MS, from_=FROM, schedule_type=FIXED, send_at=now+
ORDER_SMS_FOLLOWUP_DELAY (default 3 days))` — queued at the provider, nothing timed locally.

Cancel order: claim+`set_status("Cancelled")` → mark all follow-ups `cancel_requested_at` (one UPDATE) → cancel each
with a sid via `update_message(status=CANCELED)`; on a 4xx, `fetch_message` to learn the truth; on transport failure
leave it flagged — every later sync of that row retries the cancel. Then send the cancellation SMS.

Refresh: GET endpoints `fetch_message` rows whose outcome is not terminal (bounded per request, min 15 s between
syncs of one row); failures leave the stored state and flag `stale`.

Content disposal: `update_message(sid, body="")`; verify echoed `body == ""` (else `needs_review`, local text kept);
then clear local body and set `content_redacted_at`. Non-final messages (scheduled/pending) → 409. Idempotent.

Resend: header `Idempotency-Key` or JSON `idempotencyKey`; existing `(source, key)` → same result, no send.
Eligible only when the (refreshed) source outcome is `failed`, contact still exists, content not redacted, and not a
placed/dispatched/follow-up message of a cancelled order.

Reconciliation (`from`, `to` ISO-8601, tz required or UTC assumed): provider query `from_=TWILIO_FROM_NUMBER`,
`DateSent>` = from's UTC day start, `DateSent<` = to's UTC day +1 (widened), bounded paging (1000/page, 50 pages →
`truncated` flag); narrowed back in code on the provider clock `date_sent or date_created` ∈ [from, to); outbound
only. Local side on the same clock (stored provider timestamps). Match by SID. `providerOnly` = SID unknown anywhere
locally; `appOnly` candidates are confirmed with `fetch_message` (≤100) — found elsewhere in time → `outOfWindow`;
`unsettled` = local rows with no SID (sending/unknown/rejected) created in window. Status drift reported and synced.

## CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
| --- | --- | --- |
| a number messaged must be one Lookup returned in canonical form and still registered to that shopper | `create_message.to` ← `fetch_phone_number2.phone_number` | `ContactNumber` only stores lookup output; sends read `contact.phone_number`; delete cancels scheduled |
| a SID cancelled/redacted/fetched must be one `create_message` returned for this app | `update_message.sid`, `fetch_message.sid` ← `create_message.sid` | only `Notification.provider_sid` is ever passed |
| `messaging_service_sid` + `from_` must be a sender in that service's pool | `create_message` (scheduled) | settings; failure recorded as `rejected`, never fails the order op |
| paging token passed must come from the previous page's `next_page_uri` | `list_message.page_token` ← `ListMessageResponse.next_page_uri` | reconciliation loop, no-progress guard |
| reconciliation `From` must equal the sending `from_` | `list_message.from_` ↔ `create_message.from_` | both read `settings.TWILIO_FROM_NUMBER` |

## Assumptions & Blockers

* Minor: Lookup v2 unusable (decode failure) → Lookup v1 used; v1 has no `valid` flag, 404 = unusable.
* Minor: `ORDER_SMS_FOLLOWUP_DELAY_SECONDS` default 259200 (3 days); the provider's scheduling window is not
  documented in the SDK — a rejection is recorded as a `rejected` follow-up, not a failed dispatch.
* Minor: Idempotency-Key header enforcement undocumented → best-effort only.
* No blockers.

## REQUIRED READING (all loaded before implementation)

* Client construction / lifetime / transport seam → MUST load `python-client-initialization` ✔
* Credentials, never at import, empty-default + check in `build_client` → MUST load `python-authentication` ✔
* Call shapes, status→outcome mapping, raw vs parsed → MUST load `python-calling-endpoints` ✔
* `UNSET` vs `None`, open enums, `isinstance(…, UnsetType)` narrowing → MUST load `python-models` ✔
* Error ladder, never-sent vs unknown split → MUST load `python-error-handling` ✔
* Durable claim, no-op transitions, reconciliation clocks, bounded paging, logging transport → MUST load
  `python-configuration-resilience` ✔
* Stub transport tests, both transport failure kinds, truncated 2xx → MUST load `python-testing` ✔
