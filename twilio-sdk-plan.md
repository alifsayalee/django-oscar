# Twilio SDK plan — order SMS notifications for the django-oscar sandbox

Scope: a new Django app `sandbox/apps/order_sms/` (label `order_sms`) exposing `/api/...` endpoints
for contact numbers, order placement/dispatch/cancel with SMS notifications, operator resend,
content disposal, and a reconciliation report. SDK: `twilio-sdk` 1.0.0 (import root `twilio_sdk`),
installed from `git+https://github.com/context-plugins/twilio-python-sdk.git@main` into `venv/`.
The map was read from a clone of the same branch outside the repo.

## Host decisions

| Decision | Value |
| --- | --- |
| Sync or async | **Sync** — Django under WSGI (`sandbox/wsgi.py`), sync views. `TwilioSdkClient` only; never `AsyncTwilioSdkClient`. Teardown `close()` via `atexit`. |
| Where the client lives | One lazily built, module-level client per process in `twilio_gateway.py` (built on first use → after any fork). Tests inject a client built on a stub transport. |
| Transport | `custom_http_client=RedactingLogTransport(HttpxClient(timeout=10.0))` — logs method, host, status, latency; never query strings, bodies, headers, or phone digits (paths are digit-masked because Lookups carries the number in the path). Since a transport is supplied, the timeout lives on `HttpxClient`. |
| Servers | Messages (`api20100401_message.*`) → server `default` (`https://api.twilio.com`); overridden verbatim by `settings.TWILIO_BASE_URL` when set, via `server_config={"default": {"base_url": ...}}`. Lookups (`lookups_v1_phone_number_api.*`) → `default4` (`https://lookups.twilio.com`), never overridden. `server_config` is always passed explicitly. |
| Auth | `account_sid_auth_token=BasicAuthCredentials(username=TWILIO_ACCOUNT_SID, password=TWILIO_AUTH_TOKEN)`. The keyword is optional at type level (no-auth if omitted) — the gateway refuses to build a client when either value is empty (`TwilioNotConfigured`). |
| Retries | The SDK does none. Reads (`fetch_message`, `list_message`, `fetch_phone_number2`) get one retry on `httpx.ConnectError/ConnectTimeout/PoolTimeout/ReadTimeout` and `ApiError` 429/5xx. Writes are never blindly retried — they go through the safe write. |
| Keyword-only boundary | Every keyword-only parameter has a real default (`None`); pass only what is used, never defensive `None`s. |
| Sending | Immediate sends: `from_=TWILIO_FROM_NUMBER`. Scheduled follow-up: `from_=TWILIO_FROM_NUMBER` **and** `messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID`, `schedule_type=MessageEnumScheduleType.FIXED`, `send_at=<aware datetime>` (docstring: scheduling is "For Messaging Services only"). Smoke confirmed the from-number is in that service's sender pool, so every message (scheduled or not) is `From` the configured number, which is what reconciliation filters on. |
| Idempotency reference | `ref = f"{ORDER_SMS_INSTALL_ID}:{kind}:{subject}"`; a 12-hex `ref_token = sha256(ref)[:12]` is appended to the body as `Ref <token>` (lookup kind 3: searchable field). The same token is also sent as `Idempotency-Key` via `request_options.extra_headers` (the SDK otherwise sends a random uuid4); **not relied upon** — nothing in the SDK docs says the provider de-duplicates on it, so `repeat_is_safe=False`. |

## Contract sheet (all from the SDK map + source modules; no open lookups)

Common: every op below is **Case B** — `ApiError.error` is always `RawError` (`status_code`, `content`, `text()`, `json()`); no typed arm to narrow. Import `ApiError`, `RawError`, `BasicAuthCredentials`, `HttpxClient`, `HttpRequest`, `HttpResponse`, `UNSET`, `UnsetType` from `twilio_sdk.core`; enums from `twilio_sdk.models.enums`; models from `twilio_sdk.models`. Decode failures raise `pydantic.ValidationError`/`ValueError` in both modes. Transport errors are raw `httpx` exceptions.

| Operation | Signature (positional \| keyword-only) | Server | Returns | Members the code must assert on |
| --- | --- | --- | --- | --- |
| `client.api20100401_message.create_message` | `(account_sid, to, *, from_, messaging_service_sid, body, schedule_type, send_at, request_options, …)`; wire `To`, `From`, `MessagingServiceSid`, `Body`, `ScheduleType`, `SendAt` (form fields); `send_at: RFC3339DateTime` (aware `datetime`) | `default` | `ApiV2010AccountMessage` | `sid` (`OptionalNullable[str]` — UNSET/None ⇒ unreadable ⇒ outcome unknown), `status` (`Optional[MessageEnumStatusOrStr]`), `date_created`/`date_sent` (`OptionalNullable[str]`, RFC 1123 text, e.g. `Thu, 24 Sep 2026 04:06:44 +0000`), `error_code` (`OptionalNullable[int]`), `error_message`, `body` |
| `client.api20100401_message.fetch_message` | `(account_sid, sid, *, request_options)` | `default` | `ApiV2010AccountMessage` | same as above |
| `client.api20100401_message.update_message` | `(account_sid, sid, *, body, status, request_options)`; wire `Body`, `Status`; docstring: `body=""` redacts the text; `status` is `MessageEnumUpdateStatusOrStr` (only member `CANCELED="canceled"`) cancels a not-yet-sent message | `default` | `ApiV2010AccountMessage` | `status`, `body` (must read back `""` after redaction) |
| `client.api20100401_message.list_message` | `(account_sid, *, to, from_, date_sent, date_sent_query, date_sent_query_query, page_size, page, page_token, request_options)`; wire: `date_sent_query`→`DateSent<`, `date_sent_query_query`→`DateSent>` (both `RFC3339DateTime`; smoke: sub-day precision honoured); `page_size` max 1000 | `default` | `ListMessageResponse` | `messages: Optional[list[ApiV2010AccountMessage]]`, `next_page_uri: OptionalNullable[str]` — the next page is requested by parsing `PageToken` and `Page` out of it |
| `client.lookups_v1_phone_number_api.fetch_phone_number2` | `(phone_number, *, country_code, type_, add_ons, add_ons_data, request_options)`; wire path `PhoneNumber`, query `CountryCode` | `default4` | `LookupsV1PhoneNumber` | `phone_number` (canonical E.164, `OptionalNullable[str]`), `country_code`. **404 ⇒ not a usable number** (smoke: `12345`, `+1555` → 404 code 20404). |
| ~~`lookups_v2_phone_number.fetch_phone_number3`~~ | not used | — | — | Smoke: a real 200 fails to decode (`LookupResponse.caller_name` etc. are `Optional[...]`, provider sends `null`) → `ValidationError` in both modes. Not a gap: v1 covers "is this a usable destination + canonical form". |

`MessageEnumStatus` members (`twilio_sdk/models/enums/message_enum_status.py`), as mapped by `status_from_provider` for **a send**:

| member | wire | outcome |
| --- | --- | --- |
| `DELIVERED`, `READ` | `delivered`, `read` | done |
| `QUEUED`, `SENDING`, `SENT`, `ACCEPTED`, `SCHEDULED`, `PARTIALLY_DELIVERED` | … | pending (sent = handed to carrier, not confirmed; partially = not fully done) |
| `FAILED`, `UNDELIVERED` | … | failed |
| `CANCELED` | `canceled` | failed (a send that was called off is not in effect) |
| `RECEIVING`, `RECEIVED` | inbound values | unknown (never expected on an outbound send) |
| anything else / UNSET | — | unknown |

For **a cancel** (`update_message(status=CANCELED)`): `canceled` → done; `scheduled`/`accepted`/`queued`/UNSET/other → per re-read (a message that already went out → failed, "too late"). For **a redaction** (`update_message(body="")`): done iff the echoed `body == ""`; otherwise unknown.

## OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| `create_message` (order placed / dispatched / cancelled / resend) | `ApiV2010AccountMessage.status` | done: `delivered`, `read` → outcome `done`. not-yet: `queued`, `sending`, `sent`, `accepted`, `scheduled`, `partially_delivered` → `pending` (refreshed later by `fetch_message`). failed: `failed`, `undelivered`, `canceled` → `failed` (error_code/error_message stored). `receiving`, `received`, unlisted, UNSET → `unknown`. No `sid` → unreadable → `unknown`. | `twilio_gateway.status_from_provider` (via `twilio_gateway.to_view`), stored by `services._apply_view` inside `services.deliver`; refreshed by `services.refresh` |
| `create_message` (scheduled delivery follow-up) | `ApiV2010AccountMessage.status` | same mapping; the expected first answer is `scheduled` → `pending`. It becomes done only when a later fetch reads `delivered`/`read`. | `services.dispatch_order` → `services.notify(..., send_at=...)` → `services.deliver` → `twilio_gateway.Gateway.create_message(send_at=...)`; mapped by `twilio_gateway.status_from_provider` |
| `update_message(status=canceled)` (call off follow-up) | `ApiV2010AccountMessage.status` | done: `canceled` → cancel `done`, follow-up recorded `failed/canceled`. `scheduled`/`accepted` → cancel `pending` (re-checked by fetch). Any sent-side value (`queued`,`sending`,`sent`,`delivered`,`read`,`failed`,`undelivered`) → cancel `failed` ("too late", flagged). UNSET/unlisted → `unknown`. A 4xx is followed by `fetch_message` and mapped from the fetched status. | `services.call_off` → `twilio_gateway.Gateway.cancel_message`, mapped by `twilio_gateway.cancel_outcome_from_provider`; on 4xx/transport/unreadable re-read with `Gateway.fetch_message` |
| `update_message(body="")` (content disposal) | `ApiV2010AccountMessage.body` (no status of its own for this write) | `""` → done (local copy cleared, `content_disposed_at` set). Anything else / UNSET → unknown → 502, local body kept so the operator can retry. 4xx → 409 with provider refusal (e.g. message still in flight). | `services.dispose_content` → `twilio_gateway.Gateway.redact_message`; checks `view.body != ""`; re-read via `services._reread` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| order-placed SMS | `order_sms_notification` row, `reference` = `<install>:placed:<order_id>` | DB `UNIQUE(reference)` → `IntegrityError` | `try_claim` returns False → `load_existing` | `services._try_claim` (IntegrityError on `Notification.reference`) called by `services.deliver`; reference from `services.notify` → `services.reference_for` |
| dispatched SMS | same table, `<install>:dispatched:<order_id>` | `UNIQUE(reference)` | `try_claim` | `services._try_claim` ← `services.deliver` ← `services.notify` ← `services.dispatch_order` |
| delivery follow-up (scheduled) | same table, `<install>:followup:<order_id>` | `UNIQUE(reference)` | `try_claim` | `services._try_claim` ← `services.deliver` ← `services.notify(send_at=…)` ← `services.dispatch_order` |
| cancelled SMS | same table, `<install>:cancelled:<order_id>` | `UNIQUE(reference)` | `try_claim` | `services._try_claim` ← `services.deliver` ← `services.notify` ← `services.cancel_order` |
| operator resend | same table, `<install>:resend:<notification_id>:<sha256(Idempotency-Key)[:24]>` | `UNIQUE(reference)` — same key ⇒ same row, answered from stored outcome; fresh key ⇒ new row | `try_claim` | `services.resend` (reference `resend:<id>:<sha256(key)[:24]>`) → `services.deliver` → `services._try_claim` |
| cancel follow-up / redact body | none — both set a field to a fixed value and are harmless to repeat | n/a | n/a | n/a (no claim by design) |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| every `create_message` above | lookup: `list_message(to=<number>, from_=TWILIO_FROM_NUMBER, page_size=50)` newest-first, up to 4 pages or until `date_created` is older than claim − 1 h, match `Ref <ref_token>` in `body`; found → settle from its status; empty → stays `unknown` (never `failed`); lookup error → stays `unknown`. A stale `sending` claim (older than `SEND_WINDOW` 2 min) or an `unknown` row is only ever *checked* by this lookup, never re-sent. Operators also re-trigger the check via the notifications GET (`refresh`). | `ref_token` (sha256 of the claim `reference`) | `services._check` → `twilio_gateway.Gateway.find_by_token` (outbound records only); called from `services.deliver` (5xx / `httpx.RequestError` / `ValueError` / stale or unknown claim) and from `services.refresh` |
| `update_message(status=canceled)` | re-read with `fetch_message(sid)`: `canceled` → done; still `scheduled` → pending; sent-side → too late | follow-up `provider_sid` | `services.call_off` (fallback `twilio_gateway.Gateway.fetch_message`) |
| `update_message(body="")` | re-read with `fetch_message(sid)`: body `""` → done | `provider_sid` | `services.dispose_content` → `services._reread` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| every `create_message` | committed `Notification` row: `reference` (unique), `outcome="sending"`, `claimed_at`, `kind`, `order`, `contact_number`, `body` (containing the ref token), `scheduled_for` | `provider_sid`, `provider_status`, `outcome` (from `status_from_provider`), `error_code`, `error_message`, `provider_date_created`, `provider_date_sent`, `last_checked_at` — or `failed` (never sent / 400-422 refused, claim released) / `unknown` | `services._try_claim` commits the row before `twilio_gateway.Gateway.create_message`; `services._complete` / `services._apply_view` record the answer |
| cancel follow-up | follow-up row with `provider_sid`; order already committed as `Cancelled` | `cancel_outcome` (`done/pending/failed/unknown`), refreshed `provider_status`/`outcome` | `services.call_off` saves `cancel_requested_at` before `Gateway.cancel_message`; `services._set_cancel` records the outcome |
| redact body | notification row with `provider_sid`; `content_disposal_requested_at` committed first | on done: `body=""`, `content_disposed_at`; otherwise request stays open, body kept for retry | `services.dispose_content` saves `content_disposal_requested_at` before `Gateway.redact_message`; sets `text=""`/`content_disposed_at` only on confirmed `body == ""` |

## Findings from the live run (settled by the provider's answers, reflected in the code)

- **Inbound twins.** The Canadian test destination routes messages back into this same account, so every delivered SMS has an *inbound* record with the same To/From/body (status `received`). The lookup by reference therefore only accepts outbound records (`twilio_gateway.is_outbound` on `ApiV2010AccountMessage.direction`, `MessageEnumDirection.OUTBOUND_API|OUTBOUND_CALL|OUTBOUND_REPLY`), and reconciliation excludes inbound records from the send comparison and reports their count as `inboundRecordsIgnored`.
- **Stale pooled connection.** One create hit `httpx.RemoteProtocolError` (request sent, reply lost) and the immediate lookup hit `ConnectError`: recorded `unknown`, then settled to `delivered` by the reference lookup on the next read, with no second send.

## Reconciliation

`GET /api/notifications/reconciliation?from=&to=`: provider side = `list_message(from_=TWILIO_FROM_NUMBER, date_sent_query_query=from−1d, date_sent_query=to+1d, page_size=1000)` followed through every `next_page_uri` (cap 100 pages → 422 "range too large", never a silently partial report), then narrowed in code to `from <= provider_time < to`, where `provider_time = date_sent or date_created` (canceled scheduled messages have `date_sent=null`). Inbound records are excluded (see findings). Local side = notifications with a `provider_sid` whose stored `provider_time` (same function) is in the widened window; matched by SID (one local row ↔ one provider message). Findings: `matched` (with `statusAgrees`), `providerOnly`, `localOnly`, `unsettled` (local rows claimed in the window with no provider SID — sending/unknown/failed-before-send). Local rows re-dated by the provider answer are narrowed with the provider's time.

## Assumptions & Blockers

- No blockers. SQLite (the sandbox default) holds the claim via a unique index; views run with `transaction.non_atomic_requests` so the claim commits before the provider call (the sandbox sets `ATOMIC_REQUESTS=True`).
- Minor: a shopper with several numbers is messaged at their most recently registered active number.
- Minor: the order pipeline in `sandbox/settings.py` gains a `Dispatched` status (Pending/Being processed → Dispatched → Complete/Cancelled) so an order can be cancelled after dispatch, which is the path that has to call off the follow-up.
- Minor: follow-up delay `ORDER_SMS_FOLLOWUP_DELAY` defaults to 3 days (configurable).
- Removing a contact number also cancels any scheduled follow-up still queued for it ("nothing may be sent to it again").
- Repo test suite needs PostgreSQL (baseline: `tests/integration/order` errors with connection refused on :5432, pre-existing). The new app's tests run on the sandbox's SQLite settings: `cd sandbox && ../venv/Scripts/python manage.py test apps.order_sms`.

## REQUIRED READING

- Client construction/lifetime, transport ownership → MUST load `python-client-initialization` (loaded)
- Auth keyword optional ⇒ silent no-auth → MUST load `python-authentication` (loaded)
- Keyword-only split, parsed vs raw, status-not-id → MUST load `python-calling-endpoints` (loaded)
- `Optional` ≠ `typing.Optional`, open enums, `UNSET` at the boundary → MUST load `python-models` (loaded)
- Error ladder, never-sent vs may-have-landed → MUST load `python-error-handling` (loaded)
- Safe write, no retries, reconciliation clocks, logging transport → MUST load `python-configuration-resilience` (loaded)
- Stub transport tests → MUST load `python-testing` (loaded)
