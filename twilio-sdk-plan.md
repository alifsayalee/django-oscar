# Twilio SMS order-notifications — plan & contract sheet

Add SMS order notifications to the django-oscar sandbox, provider = Twilio, via the
APIMatic-generated **`twilio-sdk`** Python SDK (import root `twilio_sdk`). Additive Django app
under `sandbox/apps/`, routed under `/api/`.

## Toolchain / environment (verified)
- Python 3.11 venv at `repo/venv` (`py -3.11`). Project installed editable with `.[test]`
  (django-environ + whitenoise present; Django 5.2.17).
- `twilio-sdk` installed **non-editable** from the local clone at
  `../twilio-sdk-map` (pip's own `git+https` clone failed on this box; a non-editable build from
  the full clone is a proper wheel install, not a sys.path/editable hack). Import verified.
- SDK map clone: `../twilio-sdk-map` (branch `main`, v1.0.0).
- Ports: bind only `36900`..`36919` (APP_PORT_BLOCK_BASE=36900, SIZE=20).
- Credentials present in env: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER,
  TWILIO_MESSAGING_SERVICE_SID, TWILIO_TEST_TO_NUMBER, TWILIO_UNREACHABLE_TO_NUMBER.
  TWILIO_BASE_URL unset (optional override).

## Sync vs async
**Sync** (`TwilioSdkClient` / alias `Client`). Django under WSGI. One lazily-initialised
module-global client, `close()` at process exit via `atexit`. The two client classes do not mix.

## Auth & client construction
- HTTP Basic: `account_sid_auth_token=BasicAuthCredentials(username=ACCOUNT_SID, password=AUTH_TOKEN)`.
  Keyword-only constructor. Omitting creds ⇒ unauthenticated (no error) — always set it.
- Several servers, **one** environment ⇒ `server_config: ServerConfigOrDict`. Each server field
  (`default`, `default4`, …) holds `base_url` **directly** (one-environment nesting). `ServerConfig`
  is frozen, `extra="forbid"`.
- **TWILIO_BASE_URL governs the messaging API only.** Messaging ops (create/fetch/list/update/delete
  message) resolve against server **`default`** (`https://api.twilio.com`). When TWILIO_BASE_URL is
  set, pass `server_config={"default": {"base_url": TWILIO_BASE_URL}}` — overriding only `default`.
  **Lookup uses `default4`** (`https://lookups.twilio.com`) and is NOT overridden by TWILIO_BASE_URL.
- `timeout`: set explicitly (default 30.0 too long); use 20.0. Validated `>0`.
- SDK does **no retries** — none added (send failures must not fail the underlying op anyway;
  a single attempt keeps the no-duplicate-write property).

## Operations in scope (contract facts — from SDK map + source; do not re-derive from memory)

Keyword-only boundary: everything after `*` is keyword-only with a real default (no defensive
`None`s needed). Async peers identical + awaited (unused). All in-scope ops are **Case B**:
`ApiError.error` is always `RawError` (no typed arm to narrow). Every op's `Server` noted.

### 1. `client.lookups_v1_phone_number_api.fetch_phone_number2` — number validation (server `default4`)
- Sig: `fetch_phone_number2(phone_number: str, *, country_code=None, type_=None, add_ons=None, add_ons_data=None, request_options=None)`. Positional: `phone_number` (path).
- Returns `LookupsV1PhoneNumber`; fields all `OptionalNullable` (decode `null` cleanly).
  Members used: `phone_number` (E.164 canonical), `country_code`.
- **Behaviour verified live**: usable number ⇒ 200 with canonical `phone_number`
  (e.g. `+18254751588`, and national input canonicalised to `+16475551234`); malformed/impossible
  ⇒ **404** (`ApiError` status 404). This is the registration-time reject + canonical-store path.
- **Why v1, not v2**: `lookups_v2_phone_number.fetch_phone_number3` returns `LookupResponse` whose
  sub-objects (`caller_name`, `identity_match`, `reassigned_number`, `sms_pumping_risk`, …) are
  typed `Optional[X]` (= `X | UnsetType`, **no `None` arm**), but Twilio returns them as JSON
  `null` ⇒ `pydantic.ValidationError` (16 errors) on every live response, in BOTH response modes.
  v1's model is all `OptionalNullable`, so it decodes. **Not a gap** — a working operation exists.
- `LookupsV1PhoneNumber.caller_name/carrier/add_ons` are `OptionalNullable[Any]`: never call
  `to_dict()` on the response (Any+UNSET is unserialisable) — read `.phone_number` and map to our
  own type instead.

### 2. `client.api20100401_message.create_message` — send / schedule (server `default`)
- Sig (relevant): `create_message(account_sid: str, to: str, *, status_callback=None, ...,
  schedule_type: MessageEnumScheduleTypeOrStr|None=None, send_at: RFC3339DateTime|None=None,
  from_: str|None=None, messaging_service_sid: str|None=None, body: str|None=None, media_url=None,
  content_sid=None, request_options=None)`. Positional: `account_sid`, `to`.
- Returns `ApiV2010AccountMessage`. Members used: `sid`, `status` (`MessageEnumStatusOrStr`),
  `error_code` (int|null), `error_message`, `from_` (alias `from`), `to`, `date_sent`,
  `date_created` (RFC-2822 strings), `body`.
- **Immediate send** (order placed/dispatched/cancelled, resend): `from_=TWILIO_FROM_NUMBER`,
  `body=...`, `to=<E164>`. Using From (not the service) so the message carries From=FROM_NUMBER and
  is reconcilable by `list_message(from_=FROM_NUMBER)`.
- **Scheduled follow-up**: `messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID`,
  `schedule_type="fixed"` (enum `MessageEnumScheduleType.FIXED` — sole member), `send_at=now+3 days`
  (`RFC3339DateTime`; window 15 min–7 days ⇒ 3 days OK). Scheduling requires a Messaging Service;
  From cannot be combined. Its wire `from` is chosen from the service pool ⇒ may not equal
  FROM_NUMBER, so a follow-up may not appear in FROM-filtered reconciliation (documented, expected).
- **Purpose notes**: `schedule_type`+`send_at` set only for the follow-up (omit ⇒ send now).
  `from_` vs `messaging_service_sid`: exactly one, per above. All other optional fields left UNSET
  (⇒ provider/account defaults; not our concern).

### 3. `client.api20100401_message.update_message` — cancel schedule / redact content (server `default`)
- Sig: `update_message(account_sid: str, sid: str, *, body: str|None=None,
  status: MessageEnumUpdateStatusOrStr|None=None, request_options=None)`. Positional: `account_sid`, `sid`.
- **Cancel scheduled follow-up**: `status="canceled"` (`MessageEnumUpdateStatus.CANCELED` — sole
  member). Only affects a message still in `scheduled`.
- **Content disposal (redaction)**: `body=""` (empty string, a real `str`, distinct from UNSET) —
  empties the body at Twilio while the record/status survive. Returns `ApiV2010AccountMessage`.

### 4. `client.api20100401_message.fetch_message` — refresh delivery outcome (server `default`)
- Sig: `fetch_message(account_sid: str, sid: str, *, request_options=None)`. Returns
  `ApiV2010AccountMessage`. Used to refresh `status`/`error_*`/`date_sent` for GET endpoints.

### 5. `client.api20100401_message.list_message` — reconciliation (server `default`)
- Sig: `list_message(account_sid: str, *, to=None, from_=None, date_sent=None,
  date_sent_query=None, date_sent_query_query=None, page_size=None, page=None, page_token=None,
  request_options=None)`. Positional: `account_sid`.
- Wire: `from_`→`From`, `date_sent_query`→`DateSent<`, `date_sent_query_query`→`DateSent>`,
  `page_size`→`PageSize`, `page`→`Page`, `page_token`→`PageToken`.
- Returns `ListMessageResponse`: `.messages` (`list[ApiV2010AccountMessage]`), `.next_page_uri`
  (`OptionalNullable[str]`), `.page`.
- **Reconciliation**: `from_=TWILIO_FROM_NUMBER` (ask the provider for THIS number's traffic — the
  account carries other traffic), `date_sent_query_query`=window start, `date_sent_query`=window end.
  DateSent filters are day-granular ⇒ widen to day boundaries then narrow to [from,to) instants in
  code. **Page the whole range**: follow `next_page_uri` (extract `Page`+`PageToken`) with a page
  cap (MAX_PAGES) + `truncated` flag; never an unbounded loop.

### `delete_message` — NOT USED.
Content disposal must keep the record (fact + outcome survive), so we **redact** (update body="")
rather than delete. `delete_message` returns `None` (would need raw peer for status) — not needed.

## Status mapping (the one place a provider status becomes ours)
`MessageEnumStatus` members → our outcome. Enumerate by name; default arm = `unknown`.
- `delivered`, `received`, `read` → **delivered**
- `sent` → **sent**
- `queued`, `sending`, `accepted` → **pending**
- `scheduled` → **scheduled**
- `failed`, `undelivered` → **failed**
- `canceled` → **canceled**
- `partially_delivered` → **partial**
- anything else / newer than SDK (`str`) → **unknown**

## Error boundary (send path) — never fails the underlying op
Ladder (most specific first), converting to a structured `SendResult(sid, outcome, status,
error_code, error_message, outcome_unknown)` and swallowing for order flows:
- `ApiError`: read `e.status_code`, `RawError` body via `e.error.text()` (never `.json()`). Outcome
  `failed` (4xx reject / 5xx) — record status.
- `pydantic.ValidationError` (decode failure, bypasses both modes): outcome **unknown** (may have
  landed) — do not report as definite failure.
- `httpx.(ConnectError|ConnectTimeout|PoolTimeout|ProxyError)`: **never sent** ⇒ `failed`
  (definitive, nothing happened).
- `httpx.RequestError` (base, after the narrow tuple): **may have landed** ⇒ outcome `unknown`.
- Never log the destination number or the auth token. `RawError` bodies do not contain the To.

## Idempotency & races (python-configuration-resilience)
- **Resend idempotency**: the resend Notification row IS the claim. Unique (source_notification,
  idempotency_key). Create row (status=pending) BEFORE the send; on `IntegrityError` return the
  existing row's result (no second send). Then send, settle the row from what Twilio said. A fresh
  key ⇒ new row ⇒ legitimate second send.
- **Order transitions (dispatch/cancel) are no-op-guarded**: one conditional write decides the
  winner (`Order.objects.filter(pk=…).exclude(status=target)...` via the host `set_status` inside a
  claim), so the notification / schedule / cancel side-effects fire exactly once. Cancel of an
  already-cancelled order does nothing and re-sends nothing.
- **Cancel calls off the follow-up**: on the winning cancel, cancel every not-yet-sent
  delivery-followup for the order (provider `update_message status=canceled`). Best-effort; records
  outcome; does not fail the cancel. The incident to prevent: a "how did delivery go?" for a
  cancelled order.

## Reconciliation clock
Store Twilio's `date_sent`/`date_created` (parsed RFC-2822 → aware datetime) on the Notification.
Filter local rows on the provider clock (not created_at). Match provider msgs to local by `sid`.
Report: matched, provider_only (sid at provider not local), local_only (local sid, from our path,
not at provider), unsettled (local rows in window with no provider sid). Whole-day widen → narrow
to instants in code; match against the set; `truncated` flag surfaced.

## Django app design
- New app `apps.sms` (label `sms`), plain `AppConfig`, added to `INSTALLED_APPS`. Registered in
  `sandbox/settings.py`; Twilio settings read there via `env.str(..., default='')` — **names only,
  never values in the repo**.
- Models: `ContactNumber(owner, e164, created_at)`; `Notification(order, owner, kind, provider_sid,
  provider_status, outcome, to_number, body, content_disposed, error_code, error_message,
  provider_date_sent, provider_date_created, idempotency_key, source_notification, created_at,
  updated_at)` with `unique_together(source_notification, idempotency_key)`.
- `provider.py`: lazy module-global client + gateway fns (lookup_number, send_sms,
  schedule_followup, cancel_scheduled, redact_message, fetch_status, list_messages_from).
- `services.py`: register/list/delete contact number, place_order (reuse Oscar OrderCreator),
  dispatch_order, cancel_order, resend, dispose_content, reconcile.
- `views.py` + `urls.py`: plain Django JSON views, session auth (`request.user`), `is_staff` gate on
  operator actions (dispatch/cancel/resend/content/reconciliation); shopper endpoints owner-scoped.
  `login_required`; `csrf_exempt` on the JSON API (session-auth, non-browser clients) — documented.
- Order placement reuses Oscar: Basket + `Selector().strategy(user)`, `NoShippingRequired`,
  `OrderTotalCalculator`, `OrderCreator().place_order(...)`. No shipping address needed
  (NoShippingRequired; Order.shipping_address nullable). Products: only purchasable children/books
  (strategy `is_available_to_buy`).
- Response ids: `orderId`, `contactNumberId`, `notificationId` (resend result), and each
  notifications-list entry carries `notificationId`.

## Ports / self-verify
Run `runserver 127.0.0.1:36900`. Send one real SMS to TWILIO_TEST_TO_NUMBER (Canadian, reachable),
one to TWILIO_UNREACHABLE_TO_NUMBER (accepted then carrier-refused = expected outcome, not a gap).
Schedule a follow-up then cancel it before it sends. Operator resend. Reconciliation over a window
with data. Register/message ONLY those two numbers.

## Assumptions (minor — proceed)
- Dispatch maps to Oscar status `Being processed` (only forward step from `Pending` besides
  `Cancelled`); cancel → `Cancelled`. Pipeline allows `Being processed → Cancelled`, so a dispatched
  order can still be cancelled (and its follow-up called off).
- Message a shopper's most-recently-registered contact number (single), to bound live SMS cost.
- Follow-up delay = 3 days (a few days), constant.

## CROSS-OPERATION INVARIANTS
| invariant | operations | enforced where |
|---|---|---|
| a `to` passed to `create_message` must be a canonical E.164 that `fetch_phone_number2` accepted (200) and returned | `create_message.to` ← `fetch_phone_number2.phone_number` | implementation (register stores canonical; sends read stored `e164`) |
| `sid` passed to `update_message`/`fetch_message` must be one a prior `create_message` returned | `update_message.sid`,`fetch_message.sid` ← `create_message.sid` | implementation (Notification.provider_sid) |
| a message counted in reconciliation is one whose `from` = TWILIO_FROM_NUMBER | `list_message.from_` ← `create_message.from_` | implementation (immediate sends set From=FROM_NUMBER) |
| resend re-sends to the same `to` as its source notification | `create_message.to` (resend) ← source `Notification.to_number` | implementation |

## REQUIRED READING (loaded)
- python-getting-started — MUST load (lookup layer) — loaded.
- python-client-initialization — MUST load (client construct/lifetime) — loaded.
- python-authentication — Basic auth keyword — (covered by getting-started; scheme trivial).
- python-calling-endpoints — MUST load — loaded.
- python-models — MUST load (UNSET, open enums, Any+UNSET trap, RFC3339) — loaded.
- python-error-handling — MUST load (single ApiError, decode/transport failures) — loaded.
- python-configuration-resilience — MUST load (no retries, durable-row idempotency, no-op guard,
  reconciliation clock, pagination bound) — loaded.
- python-testing — MUST load (transport-seam stub; Basic auth ⇒ no token request) — loaded.
