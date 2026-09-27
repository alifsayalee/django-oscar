# Twilio SDK plan — SMS order notifications for the Oscar sandbox

Scope: new Django app `sandbox/apps/sms_notifications/`, routed under `/api/` from `sandbox/urls.py`.
SDK: `twilio-sdk` 1.0.0 (import root `twilio_sdk`), installed into `venv/` from
`git+https://github.com/context-plugins/twilio-python-sdk.git@main`. Map read from a clone of the same
branch (outside the repo).

## Host decisions

| Decision | Value |
| --- | --- |
| Sync / async | **Sync** `TwilioSdkClient` — the sandbox is Django under WSGI, with sync views. Never mix in `AsyncTwilioSdkClient`. |
| Client lifetime | One lazily built, module-level client per process (built after any fork, on first use); closed from an `atexit` hook. Never one per request. |
| Auth | `account_sid_auth_token=BasicAuthCredentials(username=TWILIO_ACCOUNT_SID, password=TWILIO_AUTH_TOKEN)`. If either setting is empty, raise `ImproperlyConfigured` before building the client, so it never goes out unauthenticated. |
| Servers | `default` (`https://api.twilio.com`) carries every messaging call; `TWILIO_BASE_URL`, when set, is passed verbatim as `server_config={"default": {"base_url": TWILIO_BASE_URL}}`. `default4` (`https://lookups.twilio.com`) carries the Lookup call, and `TWILIO_BASE_URL` does not override it. |
| Timeout | `timeout=10.0` on the client; the SDK performs **no retries** and none are added (writes go through the safe write below; reads are simply re-done by the next request). |
| Logging | No SDK hook. Nothing logs a phone number, a message body or the auth token. The app logs notification ids, message SIDs and outcomes only. |

## Contract sheet (every in-scope operation)

Keyword-only boundary: everything after `*` has a real default (`None`), so no defensive `None`s are passed.
`request_options` is the trailing keyword-only slot on each (not used).
Every in-scope operation is **Case B**: `ApiError.error` is always `RawError` (`status_code`, `content`, `text()`, `json()`).
Error handling in both modes: a decode failure raises `pydantic.ValidationError`/`ValueError`, **not** `ApiError`. `httpx` transport errors arrive unwrapped.

| Operation | Server | Positional | Keyword-only used | Returns (parsed) |
| --- | --- | --- | --- | --- |
| `client.api20100401_message.create_message` (`POST /2010-04-01/Accounts/{AccountSid}/Messages.json`) | `default` | `account_sid`, `to` (form `To`) | `from_` (form `From`), `body` (form `Body`), `messaging_service_sid` (form `MessagingServiceSid`), `schedule_type: MessageEnumScheduleTypeOrStr` (form `ScheduleType`, only member `FIXED="fixed"`), `send_at: RFC3339DateTime` (form `SendAt`, tz-aware datetime → RFC3339 `Z`) | `ApiV2010AccountMessage` |
| `client.api20100401_message.fetch_message` (`GET …/Messages/{Sid}.json`) | `default` | `account_sid`, `sid` | — | `ApiV2010AccountMessage` |
| `client.api20100401_message.update_message` (`POST …/Messages/{Sid}.json`) | `default` | `account_sid`, `sid` | `status: MessageEnumUpdateStatusOrStr` (form `Status`, only member `CANCELED="canceled"`) · `body: str` (form `Body`; docstring: **empty string redacts the text**) | `ApiV2010AccountMessage` |
| `client.api20100401_message.list_message` (`GET …/Messages.json`) | `default` | `account_sid` | `to` (query `To`), `from_` (query `From`), `date_sent_query` (query `DateSent<`), `date_sent_query_query` (query `DateSent>`), `page_size` (query `PageSize`, max 1000), `page` (query `Page`), `page_token` (query `PageToken`) — all `RFC3339DateTime` for dates | `ListMessageResponse` |
| `client.lookups_v2_phone_number.fetch_phone_number3` (`GET /v2/PhoneNumbers/{PhoneNumber}`) | `default4` | `phone_number` (path) | none (basic validation only) | `LookupResponse` |

`delete_message` (returns `None`) is **not used**: content disposal must keep the fact of the message and its outcome, and delete would remove both.

Model members read (all optional → `UNSET` when absent; none are required, so a truncated 2xx decodes cleanly and **the code asserts on each**):

- `ApiV2010AccountMessage` (`twilio_sdk/models/api_v2010_account_message.py`): `sid: OptionalNullable[str]` · `status: Optional[MessageEnumStatusOrStr]` · `body: OptionalNullable[str]` · `to: OptionalNullable[str]` · `from_` (wire `from`) · `date_sent`, `date_created`: `OptionalNullable[str]` (**RFC 2822** strings, GMT) · `error_code: OptionalNullable[int]` · `messaging_service_sid`. Asserted after a create: `sid` is a non-empty str, `status` is set; otherwise → outcome `unknown` (resolved by the lookup).
- `ListMessageResponse` (`twilio_sdk/models/list_message_response.py`): `messages: Optional[list[ApiV2010AccountMessage]]` · `next_page_uri: OptionalNullable[str]` (paging: carry its `Page`/`PageToken` query values into the next call).
- `LookupResponse` (`twilio_sdk/models/lookup_response.py`): `valid: Optional[bool]` · `phone_number: OptionalNullable[str]` (canonical E.164) · `country_code: OptionalNullable[str]` · `validation_errors: Optional[list[ValidationErrorOrStr]]` (`TOO_SHORT`, `TOO_LONG`, `INVALID_BUT_POSSIBLE`, `INVALID_COUNTRY_CODE`, `INVALID_LENGTH`, `NOT_A_NUMBER`).

`MessageEnumStatus` (`twilio_sdk/models/enums/message_enum_status.py`) — open enum, an unknown value arrives as a plain `str`:

| member | send step (`status_from_provider`) | cancel step (`cancel_outcome`) |
| --- | --- | --- |
| `DELIVERED`, `READ` | done | failed (went through) |
| `ACCEPTED`, `QUEUED`, `SENDING`, `SENT`, `SCHEDULED` | pending | `SCHEDULED`/`ACCEPTED`/`QUEUED` → pending (cancel not yet applied); `SENDING`/`SENT` → failed (went out) |
| `PARTIALLY_DELIVERED` | pending (meaning not settled by the source) | failed (went out) |
| `FAILED`, `UNDELIVERED` | failed | done — it will never reach the shopper (the cancel's aim) |
| `CANCELED` | failed (undone) | done |
| `RECEIVING`, `RECEIVED` (inbound), any unlisted value, `UNSET` | unknown | unknown |

Redaction step (`redact_outcome`): done when the echoed `body` is `""`; failed (needs review) when a body is still echoed; unknown when `body` is `UNSET`.

## OPERATION OUTCOMES

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/orders` → `create_message` (order placed) | `ApiV2010AccountMessage.status` | done=`delivered`,`read`; pending=`accepted`,`queued`,`sending`,`sent`,`scheduled`,`partially_delivered`; failed=`failed`,`undelivered`,`canceled`; unknown=`receiving`,`received`, unlisted, UNSET. The order is placed (201 with `orderId`) whatever the outcome; the notification outcome is reported beside it and never fails the order. | `services.notify` → `services._send` → `safe_write.safe_write(outcome_of=safe_write.status_from_provider)`; order answered by `views.orders` (201), notification outcome via `services.serialize_notification` |
| `POST /api/orders/{id}/dispatch` → `create_message` (dispatched) and `create_message` scheduled (follow-up) | same field | Same mapping for both. For the follow-up, `scheduled` is pending (queued with the provider, not delivered). The dispatch succeeds (200) whatever the outcome. | `services.dispatch_order` → `services.notify` ×2 (follow-up with `send_at`) → `safe_write.safe_write` + `safe_write.status_from_provider`; answered by `views.dispatch_order` |
| `POST /api/orders/{id}/cancel` → `update_message(status=canceled)` on each follow-up not yet gone out | same field, read by `cancel_outcome` | done=`canceled`,`failed`,`undelivered`; pending=`scheduled`,`accepted`,`queued`; failed=`sending`,`sent`,`delivered`,`read`,`partially_delivered`; unknown=rest. Reported per follow-up (`followUpCancellation`); the order cancel still succeeds. | `services.cancel_order` → `services.cancel_follow_up` → `safe_write.safe_write(outcome_of=safe_write.cancel_outcome)`; per-follow-up outcome in `views.cancel_order` (`followUpCancellation`) |
| `POST /api/orders/{id}/cancel` → `create_message` (cancelled notice) | `status` | as for the order-placed message | `services.cancel_order` → `services.notify` → `safe_write.safe_write` + `safe_write.status_from_provider`; answered by `views.cancel_order` |
| `POST /api/notifications/{id}/resend` → `create_message` | `status` | done→200, pending/sending→202, failed/needs_review→409, unknown→504, all with `notificationId` | `services.resend` → `services._send` → `safe_write.safe_write`; HTTP status from `views.answer_status` in `views.resend_notification` |
| `DELETE /api/notifications/{id}/content` → `update_message(body="")` | echoed `body` | done→200, pending→202, failed/needs_review→409, unknown→504 | `services.redact` → `safe_write.safe_write(read=safe_write.read_redaction, outcome_of=safe_write.redact_outcome)`; HTTP status from `views.answer_status` in `views.notification_content` |

## DUPLICATE CLAIMS

Claim store: the sandbox's own database (SQLite by default, PostgreSQL via `DATABASE_ENGINE`). The claim is the `ProviderWrite` table's **unique `reference` column**, which already holds across processes. It is inserted in its own committed transaction before the provider call, and the API views opt out of `ATOMIC_REQUESTS`, so the claim is not rolled back with the request.

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| order-placed SMS (`create_message`) | `ProviderWrite.reference = "{prefix}:order:{orderId}:placed"` | DB unique constraint on `reference` | `IntegrityError` in `try_claim` | `safe_write.try_claim` (IntegrityError on `ProviderWrite.reference`), called from `safe_write.safe_write` via `services.notify`/`services._send` |
| dispatched SMS | `"{prefix}:order:{orderId}:dispatched"` | same | same | `safe_write.try_claim`, via `services.dispatch_order` → `services.notify` |
| follow-up scheduled SMS | `"{prefix}:order:{orderId}:followup"` | same | same | `safe_write.try_claim`, via `services.dispatch_order` → `services.notify(send_at=…)` |
| cancelled SMS | `"{prefix}:order:{orderId}:cancelled"` | same | same | `safe_write.try_claim`, via `services.cancel_order` → `services.notify` |
| follow-up cancel (`update_message status=canceled`) | `"{prefix}:notification:{id}:cancel"` | same | same | `safe_write.try_claim`, via `services.cancel_follow_up` (reference `…:notification:{id}:cancel`) |
| resend (`create_message`) | `"{prefix}:notification:{id}:resend:{sha256(idempotencyKey)}"` — same key → same claim; new key → new claim | same | same | `safe_write.try_claim`, via `services.resend` (reference `…:resend:{sha256(key)}`); a held claim is answered from the record by `safe_write._loser_must_check` |
| content redaction (`update_message body=""`) | `"{prefix}:notification:{id}:redact"` | same | same | `safe_write.try_claim` via `services.redact`; `retry_outcomes=(needs_review, failed)` lets the idempotent redaction be re-taken by the same atomic conditional update |

## UNKNOWN OUTCOMES

`create_message` has no idempotency key, and the Message resource has no metadata field. The reference is therefore carried in the message text as a short token (`Ref XXXXXXXX`, derived from the claim reference), and the lookup lists by `To` and matches that token (kind 3).

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| every `create_message` (placed / dispatched / follow-up / cancelled / resend) | lookup: `list_message(to=…, from_=TWILIO_FROM_NUMBER)` over recent pages, matching the ref token in `body`; not found → stays `unknown` | the claim reference (token = hash of it) | `safe_write._look_up` calling `provider.find_sms_by_token` (wired as `find` in `services._send`); also `services.refresh` for later requests |
| follow-up cancel `update_message(status=canceled)` | same-reference resend (setting `canceled` again is idempotent), then `fetch_message(sid)` | claim reference + message SID | `safe_write._call` with `resending=True` (`repeat_is_safe=True` in `services.cancel_follow_up`), then `safe_write._look_up` → `provider.fetch_sms` |
| redaction `update_message(body="")` | same-reference resend (idempotent), then `fetch_message(sid)` | claim reference + message SID | `safe_write._call` with `resending=True` (`repeat_is_safe=True` in `services.redact`), then `safe_write._look_up` → `provider.fetch_sms` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| every `create_message` | committed `ProviderWrite(reference, outcome="sending", claimed_at)` and the `Notification` row pointing at it (same transaction) | `provider_id`=`sid`, `provider_status`, `outcome`, `provider_time` (`date_sent`, else `date_created`), `error_code` | `safe_write.try_claim` + the `on_claim` in `services._send` (creates `Notification` in the claim's transaction); `safe_write.complete` records the answer |
| follow-up cancel | committed `ProviderWrite(ref …:cancel, sending)` linked from the notification | outcome from `cancel_outcome`, the message's status copied onto the follow-up's send record | `safe_write.try_claim` + `on_claim` in `services.cancel_follow_up` (links `cancel_write`); `safe_write.complete`, then the send record updated in `services.cancel_follow_up` |
| redaction | committed `ProviderWrite(ref …:redact, sending)`; the local body copy is blanked only after `done` | outcome from `redact_outcome`, `content_redacted_at` | `safe_write.try_claim` + `on_claim` in `services.redact` (links `redact_write`); local body blanked in `services.redact` only when the outcome is done |

## Assumptions & Blockers

- **BLOCKER (environment, not SDK) — still open at hand-off:** on 2026-09-27 every call with the supplied credentials returned `401` code `20003` ("account … with status 4 is not active"). That covered `fetch_account`, Lookup and `list_message`. The credentials are well-formed, so the account itself is inactive at Twilio. No live smoke, send, schedule, cancel or reconciliation is possible. The integration was verified two ways: through the real SDK against a stub transport (35 tests), and over HTTP on the running sandbox, with `TWILIO_BASE_URL` pointed at a local mock of the Messages API. Live verification must be re-run once the account is reactivated. On the running site, `POST /api/contact-numbers` with the live credentials answers `502` ("refused our credentials"), which is the account's state surfacing correctly.
- UNVERIFIED (would have been settled by the live smoke): (1) Twilio accepts an RFC 3339 datetime for `DateSent<`/`DateSent>`, although the docstring describes `YYYY-MM-DD`. The code sends whole-day values, midnight UTC, widens the window by a day on each side, and narrows the result back in code. (2) `schedule_type=fixed` requires `messaging_service_sid`, per the docstring. `from_` is sent as well, so the reconciliation's `From` filter covers scheduled messages. (3) How far ahead `send_at` must be. The follow-up is set 3 days out.
- Minor: a shopper's newest active number is the destination.
- Minor: an order "dispatched" is a new `Dispatched` status added to the sandbox's pipeline.
- Minor: a message that cannot be sent never fails the order operation (`services._guarded_send` records the outcome and swallows the error). Retries are not added: the SDK performs none, and an unknown outcome is settled only by a lookup under its original reference (`services.refresh` on later reads), never by a resend under a new one.
- Setup note: the brief's DB sequence leaves `orders.json` failing on a missing product 12. Running `oscar_import_catalogue sandbox/fixtures/*.csv` before it adds the books (209 products total), even though its final summary line reads 0.

## REQUIRED READING

- MUST load `python-client-initialization` — client lifetime under WSGI, close obligation. (loaded)
- MUST load `python-authentication` — Basic credentials, no-auth silent default. (loaded)
- MUST load `python-calling-endpoints` — status-driven outcomes, `answer`. (loaded)
- MUST load `python-models` — `UNSET` vs `None`, open enums, RFC3339 converter. (loaded)
- MUST load `python-error-handling` — Case B `RawError`, transport split, decode failures. (loaded)
- MUST load `python-configuration-resilience` — safe write, reconciliation clocks. (loaded)
- MUST load `python-testing` — stub transport seam for the test suite. (loaded)
