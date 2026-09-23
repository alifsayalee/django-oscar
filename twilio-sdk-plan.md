# Twilio SMS order-notifications — plan & contract sheet

Add SMS order-notifications to the django-oscar **sandbox** site (`sandbox/`), as a new Django
app `sandbox/apps/smsnotify/`, wired into `sandbox/urls.py` under `/api/`. Twilio is reached
**only** through the `twilio-sdk` APIMatic SDK (import root `twilio_sdk`), per the mandate.

## Host decision — SYNC

The sandbox is a classic WSGI Django site; all views are sync `def`. → Use the **sync** client
`twilio_sdk.Client` (alias of `TwilioSdkClient`). The two client classes do not mix. Teardown
obligation: `client.close()`. The client is **long-lived, module-scoped** (built lazily once in
`gateway.py`, reused across requests) — never rebuilt per request. A Django management process is
long-running; we do not attempt a shutdown hook (acceptable — the pool is closed by process exit).

## Settings / credentials (read via Django settings only; values never in repo)

Add to `sandbox/settings.py`, each read from the environment through the existing `env`/`os.environ`
machinery, no literal values:
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `TWILIO_MESSAGING_SERVICE_SID`,
`TWILIO_BASE_URL` (optional; default None).

- Auth = HTTP Basic → `account_sid_auth_token=BasicAuthCredentials(username=ACCOUNT_SID, password=AUTH_TOKEN)`.
- `TWILIO_BASE_URL` overrides the **messaging** API only. Messaging ops (create/fetch/list/update
  message) resolve against server key **`default`** (`https://api.twilio.com`). So when set, build
  `server_config = {"default": {"base_url": TWILIO_BASE_URL}}` — leaving every other server (esp.
  `default4` = lookups.twilio.com) at its built-in default. When unset, pass `server_config=None`.
- Auth token is a secret: never logged, never returned by an endpoint, never written to a file.
  Shopper phone numbers are never logged either.

## SDK identity (verified)

- Distribution `twilio-sdk` 1.0.0 installed from source into `venv`; import root `twilio_sdk`.
- Client is keyword-only: `Client(server_config=..., timeout=30.0 default, account_sid_auth_token=...)`.
- Error model: parsed call raises `ApiError` (`.error`, `.status_code`, `.response`); raw call via
  `.with_raw_response` returns `ApiResult` (`Success.payload` / `Failure.error`). **All 5 in-scope
  ops are Case B → `.error` is always `RawError`** (`.status_code`, `.text()`, `.json()`).
- **The SDK performs NO retries.** We add a small bounded retry (see Resilience) around idempotent
  reads and deliberately DO NOT retry sends (a retry could double-send; sends are one-shot).
- Decode failures raise `pydantic.ValidationError`/`ValueError`, NOT `ApiError`, and bypass both
  response modes. **This bit us in the smoke** — see Lookup decision.

## Lookup decision — use v1, not v2 (verified against the live account)

`lookups_v2_phone_number.fetch_phone_number3` returns `LookupResponse`, whose data-package fields
(`caller_name`, `sim_swap`, …) are typed `Optional[CallerNameInfo] = UNSET` (i.e. `T | UnsetType`,
where `None` is NOT legal). A basic v2 lookup returns those fields as JSON `null`, so the parsed
**and** raw call both raise `ValidationError` at decode — an unusable operation for plain validation.
This is a decode limitation of one operation, not a missing capability: the plugin still exposes
phone validation via **Lookup v1** `lookups_v1_phone_number_api.fetch_phone_number2`, whose model
`LookupsV1PhoneNumber` types the same fields `OptionalNullable[Any]` (accepts `null`) and decodes
cleanly. Smoke confirmed: valid CA number → 200 with canonical `phone_number` `+1825…`; bogus
`+15550001111` and `12345` → `ApiError` 404. → **Not a gap.** Validation = call v1; 200 ⇒ usable,
store returned `phone_number` (provider canonical E.164); `ApiError` 404 ⇒ reject the registration.

## Domain model (reuse Oscar; new models only for what Oscar lacks)

Reuse `oscar.apps.order` (Order/OrderLine), `oscar.apps.catalogue` (Product), `oscar.apps.basket`,
`OrderCreator`, `OrderTotalCalculator`, `shipping.methods.Free`. Order status uses the sandbox's
configured pipeline (`OSCAR_ORDER_STATUS_PIPELINE`): initial `Pending`; dispatch ⇒
`set_status('Being processed')` (legal from Pending); cancel ⇒ `set_status('Cancelled')` (legal from
Pending and Being processed). `InvalidOrderStatus` ⇒ HTTP 409.

New app models (`smsnotify`):

- **ContactNumber**: `owner` FK→AUTH_USER_MODEL, `phone_number` (E.164 canonical from Lookup),
  `created`. Hard-deleted on DELETE; before delete, cancel any of the owner's not-yet-sent scheduled
  follow-up Notifications addressed to that number (so "nothing may be sent to it again").
- **Notification**: `order` FK→Order, `recipient` (E.164 snapshot str), `contact_number`
  FK→ContactNumber (SET_NULL), `category` (placed/dispatched/cancelled/delivery_followup/resend),
  `body` (text sent; nulled on content disposal), `provider_sid` (Twilio Message SID; null until
  accepted), `twilio_status` (raw), `status` (our mapped enum), `error_code`/`error_message`
  (nullable), `is_scheduled` (bool), `send_at` (nullable), `content_redacted` (bool),
  `idempotency_key` (unique, null — set only for resend-produced rows), `created`, `updated`.
  Scoping: a caller sees only Notifications whose `order.user == request.user`; operators (is_staff)
  act on any.

Send is best-effort: any Twilio failure when sending a notification is caught, recorded on the
Notification (status=failed / provider_sid null), and NEVER propagates to fail the order op. A
shopper with no ContactNumber is simply not messaged (no Notification rows created).

Status mapping (open enum — compare on `str(value)`), MessageEnumStatus members verified:
`delivered|received|read` → **delivered**; `sent` → **sent**; `queued|sending|accepted|scheduled`
→ **pending**; `failed|undelivered` → **failed**; `canceled` → **canceled**;
`partially_delivered` → **partial**; anything else → **unknown**.

## HTTP endpoints (all under /api/, session auth, JSON)

Shopper-scoped: `POST/GET /api/contact-numbers`, `DELETE /api/contact-numbers/{id}`,
`POST /api/orders`, `GET /api/my-orders`, `GET /api/orders/{id}/notifications`,
`DELETE /api/notifications/{id}/content` (shopper's own).
Operator-only (`is_staff`): `POST /api/orders/{id}/dispatch`, `POST /api/orders/{id}/cancel`,
`POST /api/notifications/{id}/resend`, `DELETE /api/notifications/{id}/content` is shopper-scoped per
spec ("a shopper has asked") — allow the owning shopper; operators too. `GET /api/notifications/reconciliation`.
Response id fields: `orderId`, `contactNumberId`, `notificationId` (resend → the new message's id;
each notifications-list entry carries `notificationId`).

CSRF: these are JSON API endpoints driven by tools; use session auth but exempt CSRF on the API
views (documented, sandbox-appropriate) and return 401/403 for unauthenticated / non-staff.

---

## CONTRACT SHEET (per-operation — authoritative; all facts from map + source + live smoke)

All ops: keyword-only boundary after `*`; every kw has a real default (no defensive `None` needed);
trailing `request_options` kw; sync client, each op also on `.with_raw_response`. **We use the
parsed call** (raises `ApiError`) for all, except we wrap sends so failures never bubble to the order op.

### 1. Validate/canonicalize — `client.lookups_v1_phone_number_api.fetch_phone_number2`
- Sig: `fetch_phone_number2(phone_number: str, *, country_code: str|None=None, type_=None, add_ons=None, add_ons_data=None, request_options=None)` — positional: `phone_number`.
- Server: **`default4`** (lookups.twilio.com) — NOT affected by TWILIO_BASE_URL.
- Returns `LookupsV1PhoneNumber`: `phone_number: OptionalNullable[str]` (canonical E.164 — the value we store), `country_code`, `national_format`, `carrier/caller_name/add_ons: OptionalNullable[Any]`.
- Error: Case B `RawError`. **404 ⇒ number not a usable destination → reject.** Other statuses ⇒ surface as a 502-style error to the caller (Twilio unavailable), do not store a number.
- Purpose of `country_code`: default region for national-format inputs; we pass it only if the caller supplies one, else omit (E.164 input needs none).

### 2. Send / schedule — `client.api20100401_message.create_message`
- Sig positional: `account_sid: str, to: str`. kw used: `from_`, `body`, `messaging_service_sid`, `schedule_type`, `send_at`, `status_callback`(NOT set — no public URL).
- Wire: `to`→`To`, `from_`→`From`, `body`→`Body`, `messaging_service_sid`→`MessagingServiceSid`, `schedule_type`→`ScheduleType`, `send_at`→`SendAt`.
- **Immediate send**: `create_message(ACCOUNT_SID, to, from_=FROM_NUMBER, body=text)`.
- **Scheduled follow-up**: `create_message(ACCOUNT_SID, to, messaging_service_sid=MSG_SVC_SID, schedule_type="fixed", send_at=<aware datetime, now+3 days>, body=text)`. Twilio rule: send_at 15 min–7 days out, requires messaging service + schedule_type=fixed; `from_` must NOT be combined with messaging_service_sid for scheduling. `send_at` is `RFC3339DateTime` = Annotated over datetime; **must be tz-aware** (`_require_tzaware`) → pass `timezone.now()+timedelta(days=3)`.
- `schedule_type` enum `MessageEnumScheduleType.FIXED = "fixed"` (open enum; pass the member or "fixed").
- Server: **`default`** (overridable by TWILIO_BASE_URL).
- Returns `ApiV2010AccountMessage` (all members Optional/UNSET → no decode-truncation risk). Read: `sid` (store as provider_sid), `status` (map), `error_code`, `error_message`. **A returned sid means accepted, not delivered** — state derives from `status` per mapping, default arm `unknown`.
- Error: Case B `RawError`. On any ApiError/transport/decode error: record notification as failed, swallow (never fail the order op).

### 3. Poll delivery — `client.api20100401_message.fetch_message`
- Sig positional: `account_sid, sid`. Server `default`. Returns `ApiV2010AccountMessage`.
- Used by GET my-orders / notifications / before resend, to refresh non-terminal statuses.
- Error Case B `RawError`; on error keep last-known status (best-effort refresh).

### 4. Cancel scheduled / redact content — `client.api20100401_message.update_message`
- Sig positional: `account_sid, sid`. kw: `body`, `status` (`MessageEnumUpdateStatus.CANCELED="canceled"`, only member).
- **Cancel follow-up**: `update_message(ACCOUNT_SID, sid, status="canceled")` — only valid while scheduled/not yet sent.
- **Redact content**: `update_message(ACCOUNT_SID, sid, body="")` — empties the provider-side body, record survives. Then null local `body`, set `content_redacted=True`.
- Server `default`. Returns `ApiV2010AccountMessage`. Error Case B `RawError`.

### 5. Reconciliation — `client.api20100401_message.list_message`
- Sig positional: `account_sid`. kw: `from_`, `date_sent_query`(wire `DateSent<`), `date_sent_query_query`(wire `DateSent>`), `page_size`, `page`, `page_token`.
- Range [from,to]: `date_sent_query_query = from` (DateSent> lower), `date_sent_query = to` (DateSent< upper). Both `RFC3339DateTime` (tz-aware). **Filter `from_ = FROM_NUMBER`** so only THIS app's own sending-number traffic is counted (ask provider for that number, per spec).
- Pagination: loop pages via `next_page_uri` → parse its `PageToken` query param → pass as `page_token`; stop when `next_page_uri` is UNSET/None. Covers the whole range.
- Server `default`. Returns `ListMessageResponse` (`messages: list[ApiV2010AccountMessage]`, `next_page_uri: OptionalNullable[str]`). Error Case B `RawError`.

## CROSS-OPERATION INVARIANTS

| invariant | operations | enforced where |
|---|---|---|
| a `sid` passed to fetch/update(cancel)/update(redact) must be a Message sid returned by a prior `create_message` | `fetch_message`/`update_message` ← `create_message` | implementation (store `provider_sid` on Notification) |
| the scheduled follow-up cancelled on order-cancel is the message `create_message(schedule_type=fixed)` produced | `update_message(status=canceled)` ← `create_message` | implementation (follow-up Notification row holds its `provider_sid`; cancel looks it up) |
| reconciliation lines up the provider's messages against app records by `provider_sid`, scoped to `from_ = FROM_NUMBER` | `list_message` ↔ Notification rows | implementation |
| resend idempotency key ⇒ at most one message per key | app-level (no Twilio field) | implementation (unique `idempotency_key`; repeat returns existing) |
| a number stored by contact-number register must be one Lookup v1 returned (canonical) | `create_message.to` ← `fetch_phone_number2.phone_number` | implementation |

## REQUIRED READING (load every one BEFORE implementing the step it governs)

- `python-client-initialization` — MUST load before building `Client(...)` (keyword-only, long-lived, close()).
- `python-authentication` — MUST load before setting `account_sid_auth_token` (omission ⇒ unauth, no failure at construction).
- `python-calling-endpoints` — MUST load before the first `client.<ctrl>.<op>(...)` (kw-only tail; parsed vs raw; sid≠delivered).
- `python-models` — MUST load before touching `ApiV2010AccountMessage`/`LookupsV1PhoneNumber`/enums (Optional=T|Unset, open enums, wire aliases, send_at datetime).
- `python-error-handling` — MUST load before any try/except (single ApiError; Case B RawError; decode ⇒ ValidationError bypasses both modes; httpx errors unwrapped).
- `python-configuration-resilience` — MUST load before the first write call (no retries; server/base-url selection; timeout semantics; may-have-landed lookup; reconciliation).
- `python-testing` — MUST load before any test/verification script that fakes the transport.

## Runtime discoveries (verified against the live account; folded into the code)

- **Lookup v2 decodes to a `ValidationError`** on a basic lookup (null data-package
  fields vs `Optional[T]=UNSET`), bypassing both response modes → use Lookup **v1**
  (`fetch_phone_number2`): 200 ⇒ canonical `phone_number`, 404 ⇒ reject. Not a gap.
- **A just-created/just-scheduled message is briefly not updatable** (Twilio eventual
  consistency): the update endpoint (cancel *and* redact) answers `404` for a few
  seconds even though GET already returns the message. Both operations are idempotent,
  so `gateway._update_message_with_404_retry` retries a transient 404 for ~9s. Verified:
  cancel at t≈0 → 404; at t≈5s → OK. In real operator use cancel/dispose happen long
  after sending, so the first attempt succeeds with no delay.
- **Redaction (`update_message(body="")`) genuinely empties the provider body** — a fresh
  fetch returns `body=""` while `status` (e.g. delivered) survives. Verified.
- A deleted number is never messaged again, resends included (`resend_notification`
  refuses when the recipient is no longer a registered `ContactNumber`).

## Assumptions & Blockers

- No blockers. Lookup v2 decode issue resolved by using v1 (a plugin-exposed capability) — not a gap.
- Follow-up delay: 3 days (within Twilio's 15min–7day window; "a few days later").
- Orders placed without a ShippingAddress (`OrderCreator.place_order` allows it); a Country exists in
  fixtures if one is ever needed. Free shipping method; NoTax strategy (sandbox default).
- CSRF-exempt JSON API is acceptable for the sandbox reference storefront.
