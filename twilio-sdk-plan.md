# Twilio SDK plan — SMS order notifications for the django-oscar sandbox

## Scope

New Django app `sandbox/apps/sms_notifications/`, routed under `/api/` from `sandbox/urls.py`:

| Route | Who | Provider calls |
| --- | --- | --- |
| `POST /api/contact-numbers` | shopper | Lookup v1 fetch (validate + canonicalise) |
| `GET /api/contact-numbers` | shopper | none |
| `DELETE /api/contact-numbers/{id}` | shopper (own) | cancel of any still-scheduled follow-up to that number |
| `POST /api/orders` | shopper | send "order placed" |
| `POST /api/orders/{id}/dispatch` | staff | send "dispatched" + schedule follow-up (`schedule_type=fixed`, `send_at`) |
| `POST /api/orders/{id}/cancel` | staff | send "cancelled" + cancel scheduled follow-up (`update_message status=canceled`) |
| `GET /api/my-orders` | shopper | status refresh (`fetch_message`) of unsettled notifications |
| `GET /api/orders/{id}/notifications` | shopper (own order) | status refresh |
| `POST /api/notifications/{id}/resend` | staff | send (new message, claim keyed by caller idempotency key) |
| `DELETE /api/notifications/{id}/content` | staff | redact (`update_message body=""`) |
| `GET /api/notifications/reconciliation?from=&to=` | staff | `list_message(from_=TWILIO_FROM_NUMBER, DateSent>/<)`, all pages |

Plus `manage.py refresh_sms_notifications` (settles pending/unknown notifications by asking the provider — there is no callback URL).

## Repo survey

- Host: Django (WSGI, sync) sandbox, settings `sandbox/settings.py` (django-environ `env`), `ATOMIC_REQUESTS=True`, SQLite by default. Existing sandbox app exemplar: `sandbox/apps/user/models.py` (`apps.<name>` package); URL exemplar `sandbox/urls.py` (`path(..., include(...))`).
- Oscar models reused: `order.Order`/`order.Line` (created via `order.utils.OrderCreator`), `basket.Basket`, `catalogue.Product`, `partner` strategy `Selector`, `shipping.repository.Repository`, `checkout.calculators.OrderTotalCalculator`, `order.ShippingEvent`/`ShippingEventType` (dispatch record). Order statuses from `OSCAR_ORDER_STATUS_PIPELINE`; a `Dispatched` status is added to the sandbox pipeline.
- **Sync vs async: sync.** Django under WSGI → `TwilioSdkClient`, `custom_http_client=`, `close()`.
- Toolchain: `py -3.11` venv at `venv/` (gitignored), `venv\Scripts\pip install -e .[test]`; SDK installed with `pip install "twilio-sdk @ git+https://github.com/context-plugins/twilio-python-sdk.git@main"` (1.0.0, commit cb311ab). No type checker configured in the repo → `mypy --strict` installed into the venv only, run on the app's SDK-facing modules. Tests: Django test runner from `sandbox/` (`manage.py test apps.sms_notifications`).
- Baseline: `manage.py check` clean apart from the pre-existing `templates.W003` thumbnail warning. Bootstrap differs from the brief: `child_products.json` yields 11 products (2 purchasable: ids 9, 10), and `loaddata orders.json` fails with `FOREIGN KEY constraint failed` on the untouched tree — pre-existing, not caused by this work, not needed by it.

## Credentials / environment

- Settings (`sandbox/settings.py`, read from env at run time, no values in the repo): `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `TWILIO_MESSAGING_SERVICE_SID`, `TWILIO_BASE_URL` (optional). Plus non-secret tuning: `SMS_NOTIFICATIONS_INSTALL_ID`, `SMS_FOLLOWUP_DELAY_HOURS` (72), `TWILIO_TIMEOUT_SECONDS` (10).
- Auth: `account_sid_auth_token=BasicAuthCredentials(username=ACCOUNT_SID, password=AUTH_TOKEN)`; client construction refuses (ImproperlyConfigured) when either is empty — the SDK would otherwise send unauthenticated.
- Servers: every Messages call is on server `default` (`https://api.twilio.com`); `server_config={"default": {"base_url": TWILIO_BASE_URL or "https://api.twilio.com"}}`. Lookup is on `default4` (`https://lookups.twilio.com`) and is NOT governed by `TWILIO_BASE_URL` (left at SDK default).

## Read-only smoke (scratchpad `tmp/smoke.py`, real credentials, 2026-09-25)

- `lookups_v2_phone_number.fetch_phone_number3` → **decode failure** (`ValidationError`, 16 errors): the provider returns `null` for `caller_name`, `sim_swap`, `line_type_intelligence`, … which the SDK types `Optional[X]` (no `None` arm). A decode failure bypasses both response modes, so v2 is unusable through this SDK. **Lookup v1 (`lookups_v1_phone_number_api.fetch_phone_number2`) decodes cleanly** and provides the capability: canonical E.164 `phone_number` + `country_code`; an unusable number answers `404` (code 20404). Carrier type (`type_=["carrier"]`) returned no data for this account, so line-type is not used.
- `list_message(from_=…, date_sent_query_query=<aware datetime>)` → 200; the SDK serialises `DateSent>` as RFC3339; `next_page_uri` carries `PageToken` + `Page`. `date_sent` is RFC 2822 (`Fri, 25 Sep 2026 08:57:51 +0000`); scheduled/canceled messages have `date_sent = None`. The account already holds other traffic from the same number (earlier runs).
- `messaging_v1_phone_number.list_phone_number(TWILIO_MESSAGING_SERVICE_SID)` → the configured from-number IS in the service's sender pool, so scheduled messages can carry `from_=TWILIO_FROM_NUMBER` + `messaging_service_sid`.

## Contract sheet

All operations: sync parsed form on `TwilioSdkClient`; the async twin is identical and unused. Every keyword-only parameter has a real default — pass only what is needed, never defensive `None`s. Every call ends with optional `request_options`. **Case B everywhere: `ApiError.error` is always `RawError`** (`status_code`, `content`, `text()`, `json()` raises ValueError on non-JSON). No retries in the SDK; this app adds none for writes (a write is settled by lookup, never by blind resend) and none for reads (a failed read is reported/left for the next refresh).

| Operation | Signature (positional \| keyword-only) | Server | Returns | Members asserted after the call |
| --- | --- | --- | --- | --- |
| `client.api20100401_message.create_message` | `(account_sid, to, *, from_, messaging_service_sid, body, schedule_type, send_at, validity_period, …)` — `from_` wire `From`, `send_at: RFC3339DateTime` (aware datetime), `schedule_type: MessageEnumScheduleTypeOrStr` (`FIXED="fixed"`, needs `messaging_service_sid`) | `default` | `ApiV2010AccountMessage` | `sid` (str, else outcome unknown), `status`, `date_sent`, `date_created`, `error_code` |
| `client.api20100401_message.fetch_message` | `(account_sid, sid, *)` | `default` | `ApiV2010AccountMessage` | `sid`, `status` |
| `client.api20100401_message.list_message` | `(account_sid, *, to, from_, date_sent, date_sent_query (wire DateSent<), date_sent_query_query (wire DateSent>), page_size (≤1000), page, page_token)` | `default` | `ListMessageResponse` (`messages: Optional[list[ApiV2010AccountMessage]]`, `next_page_uri: OptionalNullable[str]`) | `messages` present; `next_page_uri` → `PageToken`/`Page` for the next call |
| `client.api20100401_message.update_message` | `(account_sid, sid, *, body, status: MessageEnumUpdateStatusOrStr)` — `body=""` redacts; `status="canceled"` (`MessageEnumUpdateStatus.CANCELED`) cancels a not-yet-sent message | `default` | `ApiV2010AccountMessage` | redact: echoed `body == ""`; cancel: echoed `status` |
| `client.lookups_v1_phone_number_api.fetch_phone_number2` | `(phone_number, *)` | `default4` | `LookupsV1PhoneNumber` (`phone_number: OptionalNullable[str]`, `country_code: OptionalNullable[str]`) | `phone_number` is a str (else unreadable → 502) |

`ApiV2010AccountMessage` members used (all `OptionalNullable[str]` unless noted; read with `isinstance` narrowing, never passed out raw): `sid`, `status: Optional[MessageEnumStatusOrStr]` (open enum), `body`, `to`, `from_` (wire `from`), `date_sent` (RFC 2822 str), `date_created` (RFC 2822 str), `error_code: OptionalNullable[int]`, `error_message`, `messaging_service_sid`.

`MessageEnumStatus` (`twilio_sdk.models.enums`): `QUEUED, SENDING, SENT, FAILED, DELIVERED, UNDELIVERED, RECEIVING, RECEIVED, ACCEPTED, SCHEDULED, READ, PARTIALLY_DELIVERED, CANCELED`; unknown wire values arrive as plain `str`.

Transport failures arrive as unwrapped `httpx` exceptions: never-sent = `ConnectError, ConnectTimeout, PoolTimeout, ProxyError`; may-have-landed = other `httpx.RequestError`. A decode failure is `ValidationError`/`ValueError` in both modes. `str(ApiError)` omits the body — log `status_code` + provider `code` only (bodies may echo the phone number).

## OPERATION OUTCOMES

| write | the status field | every value it can hold, and what the app does with each | where in the code |
| --- | --- | --- | --- |
| `create_message` (placed / dispatched / cancelled / resend — immediate send) | `ApiV2010AccountMessage.status` | **done**: `delivered`, `read` → outcome `done`. **pending** (not yet): `queued`, `accepted`, `sending`, `sent`, `scheduled` → outcome `pending`, refreshed later by `fetch_message`. **failed**: `failed`, `undelivered`, `canceled` (sent-then-undone / never delivered), `partially_delivered` (not fully in effect) → outcome `failed` (resend allowed). **unknown**: `receiving`, `received` (inbound values, not ours), any unlisted str, `UNSET`/absent → outcome `unknown`, never done. An `sid` alone is never success. | `sandbox/apps/sms_notifications/gateway.py` `send_outcome`, applied in `sandbox/apps/sms_notifications/safe_write.py` `safe_send` (last line) / `settle_by_lookup`; an absent `sid` goes to `settle_by_lookup` |
| `create_message` (follow-up, `schedule_type=fixed`) | `ApiV2010AccountMessage.status` | same mapping; `scheduled` is the expected answer → `pending`. `canceled` → `failed` (for the follow-up that is the intended end state after an order cancel; recorded as the provider said). | `sandbox/apps/sms_notifications/gateway.py` `send_outcome` via `sandbox/apps/sms_notifications/safe_write.py` `safe_send`, called from `sandbox/apps/sms_notifications/services.py` `dispatch_order` → `notify(..., send_at=...)` |
| `update_message(status=canceled)` (call off follow-up) | `ApiV2010AccountMessage.status` | **done**: `canceled` → cancel confirmed. **pending**: `scheduled` (cancel not yet applied) → cancel `pending`, re-checked on refresh. **failed**: `queued`, `accepted`, `sending`, `sent`, `delivered`, `read`, `undelivered`, `failed`, `partially_delivered` → the message already left the schedule; cancel `failed` and surfaced. **unknown**: other/absent → `unknown`. | `sandbox/apps/sms_notifications/gateway.py` `cancel_outcome`, applied in `sandbox/apps/sms_notifications/services.py` `call_off` (and re-applied by `refresh` while `cancel_state` is set) |
| `update_message(body="")` (content disposal) | no status — the echoed `body` | **done**: echoed `body == ""` (redaction in effect). **failed**: echoed body non-empty → not redacted, 502 and local text kept. Absent/unreadable body → unknown → 502, local text kept, operator may repeat (harmless). | `sandbox/apps/sms_notifications/services.py` `dispose_content` (`answer.body != ""` → `ProviderUnavailable(502)`, local text kept) |
| `fetch_message` (refresh — a read, not a write) | `status` | same mapping as the first row, applied to the stored notification | `sandbox/apps/sms_notifications/services.py` `refresh` → `sandbox/apps/sms_notifications/gateway.py` `fetch_message` + `send_outcome` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| send "order placed" | `SmsNotification` row, unique `reference` = `<install>:order:<order_id>:placed`, inserted in autocommit before the provider call | DB unique constraint on `reference` (IntegrityError) | `try_claim` catches IntegrityError → answers from the stored row | `sandbox/apps/sms_notifications/services.py` `place_order` → `notify` → `sandbox/apps/sms_notifications/safe_write.py` `safe_send` → `try_claim` (`SmsNotification.reference` `unique=True` in `models.py`) |
| send "dispatched" | same table, `<install>:order:<id>:dispatched` | unique `reference` | `try_claim` | `sandbox/apps/sms_notifications/services.py` `dispatch_order` → `notify` → `sandbox/apps/sms_notifications/safe_write.py` `safe_send`/`try_claim` |
| schedule follow-up | same table, `<install>:order:<id>:followup` | unique `reference` | `try_claim` | `sandbox/apps/sms_notifications/services.py` `dispatch_order` → `notify(KIND_FOLLOWUP, send_at)` → `sandbox/apps/sms_notifications/safe_write.py` `safe_send`/`try_claim` |
| send "cancelled" | same table, `<install>:order:<id>:cancelled` | unique `reference` | `try_claim` | `sandbox/apps/sms_notifications/services.py` `cancel_order` → `notify` → `sandbox/apps/sms_notifications/safe_write.py` `safe_send`/`try_claim` |
| resend | same table, `<install>:resend:<notification_id>:<sha256(idempotency key)>` — same key = repeat, new key = new message | unique `reference` | `try_claim` (a failed send that never reached the provider is re-taken by a compare-and-set `UPDATE … WHERE outcome=failed AND provider_sid IS NULL`) | `sandbox/apps/sms_notifications/services.py` `resend` (reference from `sha256(key)`) → `sandbox/apps/sms_notifications/safe_write.py` `safe_send`/`try_claim` |
| cancel follow-up (`update_message status=canceled`) | none — setting a fixed status on a known sid is harmless to repeat | n/a | n/a | `sandbox/apps/sms_notifications/services.py` `call_off` → `sandbox/apps/sms_notifications/gateway.py` `cancel_message` |
| redact content (`update_message body=""`) | none — setting body to a fixed value is harmless to repeat | n/a | n/a | `sandbox/apps/sms_notifications/services.py` `dispose_content` → `sandbox/apps/sms_notifications/gateway.py` `redact_message` |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| every `create_message` above | lookup (kind 3 — Messages has no idempotency key and no reference field): the short ref token is written into the message body (`Ref <token>`); `find_by_ref` lists `list_message(to=<dest>, from_=TWILIO_FROM_NUMBER)` (first pages) and matches the token in `body`. Found → settle from its status; not found / lookup failed → stays `unknown` under the same reference (no timer, no new-reference retry). | `ref_token` = base32(sha256(`reference`))[:10] — derived from the same `reference` the claim holds | `sandbox/apps/sms_notifications/safe_write.py` `safe_send` (5xx / `httpx.RequestError` / `ValueError` / absent sid → `settle_by_lookup`) → `sandbox/apps/sms_notifications/gateway.py` `find_by_reference`; unsettled rows re-checked by `sandbox/apps/sms_notifications/services.py` `refresh` and `manage.py refresh_sms_notifications` |
| `update_message status=canceled` | re-read via `fetch_message(sid)` on the next cancel/refresh; the write is harmless to repeat | provider `sid` | `sandbox/apps/sms_notifications/services.py` `call_off` (`ProviderRejected` → `gateway.fetch_message`; other failure → `cancel_state=unknown`, re-run by repeating `POST /cancel`) and `refresh` |
| `update_message body=""` | operator repeats the DELETE (harmless); the local text is kept until the provider echoes `""` | provider `sid` | `sandbox/apps/sms_notifications/services.py` `dispose_content` |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| every `create_message` | committed `SmsNotification` row: `reference`, `ref_token`, order, contact number, `to_number`, `body`, `kind`, `outcome="sending"`, `claimed_at` | `provider_sid`, `provider_status`, `outcome` (mapped), `error_code`, `provider_sent_at` / `provider_created_at` (provider clock), `last_checked_at` | `sandbox/apps/sms_notifications/safe_write.py` `try_claim` (before) → `gateway.create_message` → `complete` (after); provider-calling views are `transaction.non_atomic_requests` (`views.py` `api_view`) |
| cancel follow-up | the follow-up row with its `provider_sid` (or a settled lookup of it) and the committed order status `Cancelled` | `cancel_state`, `provider_status`, `outcome` | `sandbox/apps/sms_notifications/services.py` `cancel_order` (status committed in its own `transaction.atomic()` block first) → `call_off` → `sandbox/apps/sms_notifications/safe_write.py` `complete` |
| redact content | the row with `provider_sid` | `content_disposed_at`, local `body=""` — only after the provider echoed `""` | `sandbox/apps/sms_notifications/services.py` `dispose_content` |

## Design decisions

- Views that talk to the provider are `transaction.non_atomic_requests`: the claim must commit before the provider call (ATOMIC_REQUESTS would roll it back / hide it from a second worker).
- Follow-up vs cancel race: dispatch re-reads the order status (committed) before scheduling and again after recording the follow-up sid, calling it off itself if the order was cancelled meanwhile; cancel commits `Cancelled` first, then calls off every follow-up whose sid is known and settles an `unknown` one by lookup first.
- Deleting a contact number soft-deletes it (history keeps the fact of a message) and calls off any scheduled follow-up to it; no send path ever uses a deleted number.
- Numbers never logged: SDK logging transport masks digit runs in URLs; app logs carry notification ids only.
- Reconciliation uses the provider clock on both sides (`date_sent`), widens the provider query by a day for day-granular filtering, narrows back in code to `[from, to)`, matches by `sid` against the set, and reports matched / provider-only / local-only / not-sent (scheduled or canceled, no `date_sent`) / unsettled (no sid).

## Assumptions & Blockers

- Minor: Lookup v2 is unusable through this SDK version (decode failure above); Lookup v1 provides validation + canonical form. Not a gap.
- Minor: no line-type data for this account, so a valid landline is accepted; the carrier then refuses it and it surfaces as `failed`.
- Minor: US destinations are accepted by the API and refused by the carrier (`undelivered`) — an outcome, not a defect.
- No blockers. The claim store is the existing Django database (unique constraint) — it outlives the process and is shared by all workers.

## REQUIRED READING

- Client lifetime, sync client, close at exit — MUST load `python-client-initialization` (loaded).
- Basic auth keyword is optional at the type level — MUST load `python-authentication` (loaded).
- Positional/keyword split, status-not-id outcome gate — MUST load `python-calling-endpoints` (loaded).
- `Optional` ≠ `typing.Optional`, open enums, UNSET narrowing — MUST load `python-models` (loaded).
- Error ladder, never-sent vs may-have-landed, decode failures — MUST load `python-error-handling` (loaded).
- Safe write, claim-before-call, reconciliation clock, logging transport — MUST load `python-configuration-resilience` (loaded).
- Stub transport seam for the app's tests — MUST load `python-testing` (loaded).
