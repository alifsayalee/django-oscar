# Twilio SDK plan — order SMS notifications for the django-oscar sandbox

## Scope

New Django app `sandbox/apps/order_sms/` exposing JSON endpoints under `/api/`:
contact numbers (register / list / delete), orders (place / dispatch / cancel / my-orders /
per-order notifications), operator actions (resend with idempotency key, content disposal,
reconciliation report). Session auth (`django.contrib.auth.login`), `is_staff` for operator actions.
Oscar models reused: `order.Order`, `order.Line` via `OrderCreator` + `basket.Basket`,
`order.ShippingAddress`, `address.Country`, `catalogue.Product`.

## Toolchain (established)

- `py -3.11 -m venv venv`, `venv\Scripts\pip install -e .[test]`, SDK installed from
  `git+https://github.com/context-plugins/twilio-python-sdk.git@main` (twilio-sdk 1.0.0).
- Map/source clone read at `../scratch/twilio-python-sdk` (outside the repo).
- Tests: Django `DiscoverRunner` from `sandbox/` → `python manage.py test apps.order_sms`.
- Type check: `mypy --strict` (+ django-stubs plugin) over `sandbox/apps/order_sms`.
- DB bootstrap: the approved sequence yields only 11 products and `orders.json` fails a FK;
  running `oscar_import_catalogue` on the three `books.*.csv` first gives 209 products and
  `orders.json` loads. (Recorded for the final report.)

## Decisions

- **Sync.** Django under WSGI/runserver → `TwilioSdkClient` (sync). Lazily-built module-level
  singleton (built after any fork, on first use), closed via `atexit`. Never per request.
- **Server config.** `server_config={"default": {"base_url": settings.TWILIO_BASE_URL}}` only when
  `TWILIO_BASE_URL` is set; messaging operations are all on server `default`
  (`https://api.twilio.com`). Lookups run on `default4` (`https://lookups.twilio.com`) and are
  deliberately NOT redirected by `TWILIO_BASE_URL` (task: it governs the messaging API only).
- **Auth.** `account_sid_auth_token=BasicAuthCredentials(username=TWILIO_ACCOUNT_SID,
  password=TWILIO_AUTH_TOKEN)`, both from Django settings; missing either → `ProviderConfigError`
  raised before building a client (never an unauthenticated client).
- **Timeout.** `timeout=10.0` on the client; no SDK retries exist. Reads (fetch/list/lookup) get one
  retry on never-sent transport errors / 429 / 5xx. Writes are never blindly retried — they go through
  the safe write.
- **Logging.** Transport wrapper logs method, host, path with phone-like digit runs masked, no query
  string, status and latency. No headers, no bodies. Provider error text is scrubbed of phone-like
  sequences before being stored or logged.
- **Response modes.** Parsed calls everywhere (all in-scope ops return bodies); `ApiError` ladder at
  the one gateway boundary.

## Contract sheet

Every operation: async twin identical (not used). Everything after `*` is keyword-only with a real
default — pass only what is needed, never defensive `None`s. All in-scope ops are **Case B**:
`ApiError.error` is always `RawError` (`status_code`, `content`, `text()`, `json()`). Decode failure →
`pydantic.ValidationError`/`ValueError` in both modes. httpx exceptions arrive unwrapped.

| op | server | signature (positional \| keyword-only used) | returns | members asserted |
| --- | --- | --- | --- | --- |
| `client.api20100401_message.create_message` | `default` | `(account_sid, to, *, from_, body, messaging_service_sid, schedule_type, send_at)` — wire: `To`, `From`, `Body`, `MessagingServiceSid`, `ScheduleType`, `SendAt` (form fields) | `ApiV2010AccountMessage` | `sid` (OptionalNullable[str]) must be a str else outcome unknown; `status` (Optional[MessageEnumStatusOrStr]); `date_sent`/`date_created` (OptionalNullable[str], RFC 2822 on the wire e.g. `Wed, 23 Sep 2026 09:04:34 +0000`); `error_code` (OptionalNullable[int]); `error_message` |
| `client.api20100401_message.fetch_message` | `default` | `(account_sid, sid)` | `ApiV2010AccountMessage` | as above + `body` |
| `client.api20100401_message.update_message` | `default` | `(account_sid, sid, *, body=None, status=None)`; `status: MessageEnumUpdateStatusOrStr` only member `CANCELED="canceled"`; docstring: "used to redact Message body text and to cancel not-yet-sent messages"; "To redact the text content of a Message, this parameter's value must be an empty string" | `ApiV2010AccountMessage` | `status`, `body` (`""` ⇒ redacted) |
| `client.api20100401_message.list_message` | `default` | `(account_sid, *, to, from_, date_sent_query (wire DateSent<), date_sent_query_query (wire DateSent>), page_size, page, page_token)`; date params `RFC3339DateTime` (tz-aware datetime; serialised RFC3339; smoke-verified accepted). Pagination: `next_page_uri` carries `PageToken` and `Page` query params (smoke-verified) → pass them back as `page_token`/`page` with the same filters | `ListMessageResponse` | `messages: Optional[list[ApiV2010AccountMessage]]`, `next_page_uri: OptionalNullable[str]` |
| `client.lookups_v1_phone_number_api.fetch_phone_number2` | `default4` | `(phone_number)` | `LookupsV1PhoneNumber` | `phone_number` (OptionalNullable[str]; canonical E.164), `country_code` |

- `lookups_v2_phone_number.fetch_phone_number3` was smoke-tested and **cannot be used**: the SDK's
  `LookupResponse` rejects the live response (`null` for `caller_name`, `sim_swap`, … which the model
  types `Optional[...]`, non-nullable) → `ValidationError` on every call. Lookup v1 (model fully
  `OptionalNullable`) decodes; an unusable number answers `404` (smoke: code 20404). That is the SDK
  surface used for validation/canonicalisation. Recorded as an SDK defect in the final report.
- `MessageEnumStatus` members: `QUEUED queued`, `SENDING sending`, `SENT sent`, `FAILED failed`,
  `DELIVERED delivered`, `UNDELIVERED undelivered`, `RECEIVING receiving`, `RECEIVED received`,
  `ACCEPTED accepted`, `SCHEDULED scheduled`, `READ read`, `PARTIALLY_DELIVERED partially_delivered`,
  `CANCELED canceled`. Open enum: an unknown value arrives as plain `str`.
- `MessageEnumScheduleType`: `FIXED fixed`. `MessageEnumDirection`: `INBOUND inbound`,
  `OUTBOUND_API outbound-api`, `OUTBOUND_CALL outbound-call`, `OUTBOUND_REPLY outbound-reply`.
- `create_message` has **no idempotency parameter**. De-dup is ours (claim), and the may-have-landed
  lookup is kind 3: a short reference tag derived from the claim reference is appended to the body
  (`Ref: XXXXXXXX`) and `find` lists `list_message(to=…, from_=TWILIO_FROM_NUMBER)` and matches it.
- `list_message(from_=…)` also returns the **inbound** copy when the destination is itself a number
  on this account (smoke) → reconciliation counts only non-`inbound` directions and reports the rest
  as excluded.
- Credentials: `BasicAuthCredentials` from `twilio_sdk.core`; `ApiError`, `RawError`, `UNSET`,
  `UnsetType`, `HttpClient`, `HttpxClient`, `HttpRequest`, `HttpResponse` from `twilio_sdk.core`;
  enums from `twilio_sdk.models.enums`.
- Scheduling: `schedule_type=MessageEnumScheduleType.FIXED`, `send_at=<aware datetime>`,
  `messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID` and `from_=TWILIO_FROM_NUMBER` (smoke: the
  from number is in that service's sender pool, so the scheduled message carries our `From`).

### `status_from_provider` (one place)

| provider value | outcome |
| --- | --- |
| `delivered`, `read` | done |
| `accepted`, `scheduled`, `queued`, `sending`, `sent`, `partially_delivered` | pending |
| `failed`, `undelivered`, `canceled` | failed |
| `receiving`, `received`, any unlisted str, UNSET/None | unknown |

## OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| `create_message` (placed / dispatched / cancelled / resend SMS) | `ApiV2010AccountMessage.status` | done: `delivered`,`read` → outcome done. pending: `accepted`,`scheduled`,`queued`,`sending`,`sent`,`partially_delivered` → outcome pending, re-read via `fetch_message` on later reads. failed: `failed`,`undelivered`,`canceled` → outcome failed (resend allowed). unknown: `receiving`,`received`, unlisted, absent → outcome unknown, re-read later. Missing `sid` → unknown and `find` by reference. | `sandbox/apps/order_sms/notifications.py` `_call_provider` → `_complete` (calls `sandbox/apps/order_sms/gateway.py` `status_from_provider`); re-read in `refresh` |
| `create_message` (scheduled delivery follow-up) | `ApiV2010AccountMessage.status` | same mapping; a successfully queued follow-up comes back `scheduled` → pending (not done: it has not reached anyone). | `sandbox/apps/order_sms/notifications.py` `notify(..., send_at=)` from `orders.dispatch` → `_call_provider` → `_complete`/`status_from_provider` |
| `update_message(status=canceled)` (call off follow-up) | `ApiV2010AccountMessage.status` | this write asks for the undoing, so `canceled` is its done → cancel_outcome done. `failed`,`undelivered` → cancel_outcome done (terminal, never reached the shopper; noted). `scheduled`,`accepted` → cancel_outcome pending (retried on next read / sync). `queued`,`sending`,`sent`,`delivered`,`read`,`partially_delivered` → cancel_outcome failed (it already went out; surfaced). unlisted/absent → cancel_outcome unknown (re-fetched later). A 4xx refusal → fetch_message and map its status the same way. | `sandbox/apps/order_sms/notifications.py` `call_off` (maps with `sandbox/apps/order_sms/gateway.py` `call_off_outcome`; 4xx/5xx/transport → `fetch_message` then same mapping) |
| `update_message(body="")` (content disposal) | `ApiV2010AccountMessage.body` (no status on this write's meaning) | `""` → disposal done. any non-empty/absent body → disposal failed/unknown respectively; 4xx → failed (provider refused, e.g. message still in flight), 5xx/transport/unreadable → unknown then `fetch_message` checks `body`. | `sandbox/apps/order_sms/notifications.py` `dispose_content` (checks `state.body == ''`; 4xx → failed; 5xx/transport → `fetch_message`) |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| order-placed SMS | `Notification` row, `reference` = `<install>:order:<pk>:placed` | DB `UNIQUE(reference)` | `IntegrityError` in `try_claim` | `sandbox/apps/order_sms/notifications.py` `_try_claim` (via `_safe_send`, reference from `order_reference`) |
| order-dispatched SMS | `Notification`, `<install>:order:<pk>:dispatched` | DB `UNIQUE(reference)` | `IntegrityError` in `try_claim` | `sandbox/apps/order_sms/notifications.py` `_try_claim` (via `_safe_send`) |
| delivery follow-up (scheduled create) | `Notification`, `<install>:order:<pk>:followup` | DB `UNIQUE(reference)` | `IntegrityError` in `try_claim` | `sandbox/apps/order_sms/notifications.py` `_try_claim` (via `_safe_send`) |
| order-cancelled SMS | `Notification`, `<install>:order:<pk>:cancelled` | DB `UNIQUE(reference)` | `IntegrityError` in `try_claim` | `sandbox/apps/order_sms/notifications.py` `_try_claim` (via `_safe_send`) |
| operator resend | `Notification`, `<install>:resend:<source pk>:<sha256(idempotency key)>` | DB `UNIQUE(reference)` | `IntegrityError` in `try_claim` | `sandbox/apps/order_sms/notifications.py` `resend` → `_safe_send`/`_try_claim` (reference from `resend_reference`) |
| cancel follow-up / redact body | none — setting a field to a fixed value, harmless to repeat | n/a | n/a | `sandbox/apps/order_sms/notifications.py` `call_off`, `dispose_content` (no claim by design) |

Reclaim of a `failed` claim with no provider id is a conditional `UPDATE … WHERE outcome='failed'
AND message_sid IS NULL` (atomic; row count decides the winner).

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| every `create_message` (placed/dispatched/followup/cancelled/resend) | lookup: `list_message(to=<contact E.164>, from_=TWILIO_FROM_NUMBER)` pages, match body containing the tag; found → settle from its status; not found / lookup fails → stays `unknown`, re-checked on later reads and by `order_sms_sync` | tag derived from the claim `reference` (same reference as DUPLICATE CLAIMS) | `sandbox/apps/order_sms/notifications.py` `_settle_by_lookup` → `_find` → `sandbox/apps/order_sms/gateway.py` `find_message`; loser path `_answer_existing` |
| `update_message(status=canceled)` | `fetch_message(sid)` and map its status | message sid | `sandbox/apps/order_sms/notifications.py` `call_off` (the `except` arm calls `sandbox/apps/order_sms/gateway.py` `fetch_message`) |
| `update_message(body="")` | `fetch_message(sid)` and check `body == ""` | message sid | `sandbox/apps/order_sms/notifications.py` `dispose_content` (`state is None` branch calls `sandbox/apps/order_sms/gateway.py` `fetch_message`) |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| every `create_message` | committed `Notification` (outcome `sending`, reference, contact, body with tag, kind, order, claimed_at) | sid, provider status, outcome, provider dates, error code/scrubbed message | `sandbox/apps/order_sms/notifications.py` `_try_claim` (committed row) before `_call_provider`; `_complete` after |
| follow-up cancel | committed `Notification.cancel_outcome='requested'` + order already `Cancelled` | cancel_outcome, provider status, outcome | `sandbox/apps/order_sms/orders.py` `cancel` (status committed) then `sandbox/apps/order_sms/notifications.py` `call_off` (saves `cancel_outcome='requested'` before `sandbox/apps/order_sms/gateway.py` `cancel_message`) |
| content disposal | committed local body cleared + `content_disposal='requested'` | content_disposal outcome, disposed_at | `sandbox/apps/order_sms/notifications.py` `dispose_content` (saves cleared body + `requested` before `sandbox/apps/order_sms/gateway.py` `redact_message`) |

## Assumptions & Blockers

- No blockers. Minor assumptions:
  - A shopper with several numbers is messaged on the most recently registered active one.
  - Dispatch adds a `Dispatched` status to the sandbox's `OSCAR_ORDER_STATUS_PIPELINE`
    (Pending/Being processed → Dispatched → Complete/Cancelled) so a dispatched order can still be
    cancelled; this is additive to the sandbox settings.
  - Deleting a contact number also calls off any scheduled follow-up still addressed to it.
  - Staff may read any order's notifications (they need the ids to act); shoppers only their own.
  - Idempotency keys are scoped per source notification.
- SDK defect (not a gap): Lookup v2 model rejects live responses; Lookup v1 used instead.

## REQUIRED READING

- Error ladder at the gateway boundary — MUST load `python-error-handling` (loaded).
- Client construction/lifetime — MUST load `python-client-initialization` (loaded).
- Safe write, reconciliation, logging transport, timeouts — MUST load
  `python-configuration-resilience` (loaded).
- Calls, status mapping, raw vs parsed — MUST load `python-calling-endpoints` (loaded).
- UNSET / OptionalNullable / open enums — MUST load `python-models` (loaded).
- Credentials — MUST load `python-authentication` (loaded).
- Tests with a stub transport — MUST load `python-testing` (loaded).
