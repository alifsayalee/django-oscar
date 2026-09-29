# Twilio SDK integration plan — order SMS notifications (django-oscar sandbox)

## Scope

New Django app `sandbox/apps/order_notifications`, routed under `/api/` from `sandbox/urls.py`.
Flows: contact numbers (lookup-validated, canonical E.164), order placed / dispatched (+ provider-scheduled
follow-up) / cancelled (follow-up called off) messages, operator resend (idempotency key), content
disposal (provider-side redaction), reconciliation over a window (provider filtered by `From`).

## Repo survey

| Convention | Pattern | Exemplar |
| --- | --- | --- |
| Sandbox local apps | package under `sandbox/apps/`, imported as `apps.<name>` (sandbox dir is on `sys.path`) | `sandbox/apps/sitemaps.py`, `sandbox/urls.py` |
| Settings | `django-environ` `env(...)` in `sandbox/settings.py` | `sandbox/settings.py` (`DEBUG = env.bool(...)`) |
| Oscar models | `get_model('order', 'Order')` / `get_class(...)` from `oscar.core.loading` | `src/oscar/apps/order/utils.py` |
| Order creation | `OrderCreator().place_order(basket, total, shipping_method, shipping_charge, user=..., shipping_address=...)` | `src/oscar/apps/order/utils.py` |
| Totals / shipping | `Repository().get_default_shipping_method`, `OrderTotalCalculator().calculate` | `src/oscar/apps/checkout/mixins.py` |
| Status pipeline | `OSCAR_ORDER_STATUS_PIPELINE` / `OSCAR_ORDER_STATUS_CASCADE` | `sandbox/settings.py` |
| Transactions | `ATOMIC_REQUESTS = True` — API views that call the provider opt out with `transaction.non_atomic_requests` so the claim commits **before** the provider call | `sandbox/settings.py` |
| Sync vs async | Django under WSGI, sync views → **sync `TwilioSdkClient`** | — |

Toolchain: `py -3.11 -m venv venv`, `venv\Scripts\pip install -e .[test]`; `twilio-sdk` installed from
`git+file:///D:/APIMatic/sdk-regen/twilio-python-sdk.git@main` (v1.0.0, map read from that repo's `main`).
Checks: `python sandbox/manage.py check`, `python sandbox/manage.py test apps.order_notifications`
(sandbox settings, SQLite test DB), `mypy --strict` over the new app with `django-stubs` (config kept outside
the repo). Baseline: `manage.py check` clean apart from the pre-existing `templates.W003` warning.

Bootstrap note: the given fixture sequence yields 11 products; `oscar_import_catalogue` on the three
`books.*.csv` files is **required** to reach 209 products and for `orders.json` to load (FK failure otherwise).

## Credentials / environment

All read in `sandbox/settings.py` through `env(...)`: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
`TWILIO_FROM_NUMBER`, `TWILIO_MESSAGING_SERVICE_SID`, `TWILIO_BASE_URL` (optional). Extra optional settings:
`TWILIO_LOOKUPS_BASE_URL` (lookups host override, for a local fake only), `TWILIO_TIMEOUT_SECONDS` (10),
`ORDER_SMS_REFERENCE_PREFIX` (install prefix for references), `ORDER_SMS_FOLLOWUP_DELAY_HOURS` (72).
`TWILIO_BASE_URL` set → `server_config={"default": {"base_url": TWILIO_BASE_URL}}` (the messaging API's
server); unset → server `default` = `https://api.twilio.com`. Lookups use server `default5`
(`https://lookups.twilio.com`), never governed by `TWILIO_BASE_URL`.

## Smoke results (scratchpad, read-only)

`lookups_v2_phone_number.fetch_phone_number2` and `api20100401_message.list_message` with the supplied
credentials: **HTTP 401, Twilio code 20003, "account … with status 4 is not active"**. The account is not
active, so no live call can succeed. See Blockers.

## CONTRACT SHEET

Client: **sync** `twilio_sdk.TwilioSdkClient` (never mixed with `AsyncTwilioSdkClient`). Keyword-only ctor:
`server_config`, `timeout`, `custom_http_client`, `account_sid_auth_token`. Held as a lazily-built
module-level singleton (built after fork, on first use), closed via `atexit` (`client.close()`).
Auth: `account_sid_auth_token=BasicAuthCredentials(username=ACCOUNT_SID, password=AUTH_TOKEN)` — omitting it
is silent no-auth, so construction refuses empty values. Transport: `HttpxClient(timeout=...)` wrapped in a
logging transport (method + host + path with digits masked; no query, headers or bodies).
Every keyword-only parameter has a real default (`None`); pass only what is used. The SDK performs **no
retries**; this integration adds none for writes (the safe write's lookup replaces them) and none for reads.
Every operation below is **Case B**: `ApiError.error` is always `RawError` (`status_code`, `text()`,
`json()`). Decode failure raises `pydantic.ValidationError`/`ValueError` in both modes. httpx transport
exceptions arrive unwrapped: `ConnectError | ConnectTimeout | PoolTimeout | ProxyError` = never sent;
other `httpx.RequestError` = may have landed.

| Operation (server) | Signature (positional ｜ keyword-only used) | Returns | Members asserted |
| --- | --- | --- | --- |
| `lookups_v2_phone_number.fetch_phone_number2` (`default5`, `GET /v2/PhoneNumbers/{PhoneNumber}`) | `phone_number: str` ｜ `country_code: str \| None` | `LookupResponse` | `valid: Optional[bool]` (UNSET ⇒ unreadable, 502); `phone_number: OptionalNullable[str]` (canonical E.164; stored); `validation_errors: Optional[list[ValidationErrorOrStr]]` (`TOO_SHORT`, `TOO_LONG`, `INVALID_BUT_POSSIBLE`, `INVALID_COUNTRY_CODE`, `INVALID_LENGTH`, `NOT_A_NUMBER`); `country_code`, `national_format` |
| `api20100401_message.create_message` (`default`, `POST /2010-04-01/Accounts/{AccountSid}/Messages.json`) | `account_sid: str, to: str` ｜ `from_: str \| None` (wire `From`), `body: str \| None` (`Body`), `messaging_service_sid` (`MessagingServiceSid`), `schedule_type: MessageEnumScheduleTypeOrStr \| None` (`ScheduleType`, member `FIXED="fixed"`), `send_at: RFC3339DateTime \| None` (`SendAt`, aware datetime) | `ApiV2010AccountMessage` | `sid`, `status`, `date_created`, `date_sent`, `error_code`, `error_message` |
| `api20100401_message.update_message` (`default`, `POST .../Messages/{Sid}.json`) | `account_sid: str, sid: str` ｜ `status: MessageEnumUpdateStatusOrStr \| None` (member `CANCELED="canceled"`), `body: str \| None` (`""` redacts — per docstring) | `ApiV2010AccountMessage` | `status` (cancel), `body` (redact) |
| `api20100401_message.fetch_message` (`default`) | `account_sid: str, sid: str` | `ApiV2010AccountMessage` | as create; `404` ⇒ `GONE` |
| `api20100401_message.list_message` (`default`, `GET .../Messages.json`) | `account_sid: str` ｜ `to` (`To`), `from_` (`From`), `date_sent_query` (wire `DateSent<`), `date_sent_query_query` (wire `DateSent>`), `page_size` (max 1000), `page`, `page_token` | `ListMessageResponse` | `messages: Optional[list[ApiV2010AccountMessage]]`, `next_page_uri: OptionalNullable[str]` (carries `PageToken`/`Page` for the next request) |

`ApiV2010AccountMessage` (all members optional, `UNSET` when absent): `sid`, `status: Optional[MessageEnumStatusOrStr]`,
`body`, `to`, `from_` (wire `from`), `date_created`/`date_sent`/`date_updated` (strings, RFC 2822 dates),
`error_code: OptionalNullable[int]`, `error_message`, `messaging_service_sid`. `Optional[T]` here is
`T | UnsetType` (never pass `None`); narrow with `isinstance(x, UnsetType)`.

`MessageEnumStatus` (open enum; unlisted string ⇒ `unknown`) mapped by `status_from_provider` (a message's delivery):
`delivered`, `read` → **done**; `queued`, `sending`, `sent`, `accepted`, `scheduled` → **pending** (`sent` =
handed to carrier, delivery unconfirmed); `failed`, `undelivered`, `canceled` (sent-then-undone ⇒ not in
effect), `partially_delivered` → **failed**; `receiving`, `received` (inbound, never ours), anything else,
UNSET → **unknown**.

`cancel_outcome` (the follow-up call-off): `canceled`, `GONE` → **done**; `scheduled`, `accepted` → **pending**;
`queued`, `sending`, `sent`, `delivered`, `read`, `partially_delivered`, `failed`, `undelivered` → **failed**
(too late: it left the schedule); else → **unknown**.

`redact_outcome` (content disposal) reads `body`: `""` or `None` → **done**; non-empty string → **failed**;
UNSET → **unknown**.

`answer(outcome)` (the only outcome → HTTP mapping, for endpoints whose primary write is the provider's):
done → 200; pending / sending → 202; failed / needs_review → 409; unknown / other → 504.
Order endpoints' primary write is the order; their message outcomes are reported per notification in the
body and never change the HTTP status.

## OPERATION OUTCOMES

| endpoint → write | the status field | every value it can hold, and what the caller is told for each | where in the code |
| --- | --- | --- | --- |
| `POST /api/orders` → create_message (placed) | `ApiV2010AccountMessage.status` via `status_from_provider` | delivered/read → `done`; queued/sending/sent/accepted/scheduled → `pending`; failed/undelivered/canceled/partially_delivered → `failed`; receiving/received/unlisted/UNSET → `unknown`; transport may-have-landed → `unknown`; never-sent or 4xx → `failed`. Reported as the notification's `outcome` in the 201 body; order always placed | `views.orders` → `services.place_order` → `services.notify_order` → `services.send_notification` (`safe_write(..., outcome_of=outcomes.status_from_provider)`; UNSET status → `unknown` in `services._apply_message`) → `views.notification_json` |
| `POST /api/orders/{id}/dispatch` → create_message (dispatched) | same | same as above, per notification in the 200 body | `views.order_dispatch` → `services.dispatch_order` → `services.notify_order(order, Notification.DISPATCHED)` → `services.send_notification` |
| `POST /api/orders/{id}/dispatch` → create_message scheduled (follow-up) | same | `scheduled` → `pending` (queued with provider, not yet sent); others as above | `services.dispatch_order` → `services.notify_order(order, Notification.FOLLOW_UP, send_at=...)` → `services.send_notification` → `provider.create_message(..., send_at=...)` |
| `POST /api/orders/{id}/cancel` → update_message status=canceled (follow-up call-off) | `status` via `cancel_outcome`; 404 → GONE | canceled/GONE → `done`; scheduled/accepted → `pending`; queued…undelivered → `failed` (too late); else `unknown`. Reported as the follow-up's `callOffOutcome` in the 200 body | `views.order_cancel` → `services.cancel_order` → `services.call_off_follow_up` (`safe_write(..., outcome_of=outcomes.cancel_outcome)`); retried on reads by `services.settle_cancelled_orders_follow_ups` |
| `POST /api/orders/{id}/cancel` → create_message (cancelled) | `status_from_provider` | as placed | `services.cancel_order` → `services.notify_order(order, Notification.CANCELLED)` → `services.send_notification` |
| `DELETE /api/contact-numbers/{id}` → update_message status=canceled for that number's scheduled follow-ups | `cancel_outcome` | as above; nothing left that could still go out (every call-off done, or failed = already left) → number erased, 200; otherwise the number stays disabled (hidden, never messaged) and the caller is told 202 (pending) / 504 (unknown) via `answer_status` | `views.contact_number_detail` → `services.remove_number` → `services.call_off_follow_up`; `outcomes.worst`, `outcomes.answer_status` |
| `POST /api/notifications/{id}/resend` (and its repeat under the same key) → create_message | `status_from_provider` | `answer_status`: done 200, pending/sending 202, failed/needs_review 409, unknown 504; `notificationId` always returned | `views.notification_resend` → `services.resend` → `services.send_notification`; `outcomes.answer_status` |
| `DELETE /api/notifications/{id}/content` → update_message body="" | `body` via `redact_outcome` | `""`/null/GONE → `done` 200; non-empty → `failed` 409; UNSET/unreadable → `unknown` 504; refused → 409 | `views.notification_content` → `services.dispose_content` (`safe_write(..., outcome_of=outcomes.redact_outcome)`); `outcomes.answer_status` |

## DUPLICATE CLAIMS

| write | where the claim is held | what rejects the second one | where that rejection is caught | where in the code |
| --- | --- | --- | --- | --- |
| send notification (placed/dispatched/follow-up/cancelled) | `Notification.trigger_key` (unique: order+kind+contact number) and `ProviderWrite.ref` (unique: `{prefix}:{trigger_key}:send`) in the project's Django DB | DB unique constraints (IntegrityError) | `services._get_or_create_notification` (`except IntegrityError` → existing row) and `safe_write.try_claim` (`except IntegrityError` → `False`) | `services.notify_order` → `services.send_notification` → `safe_write.safe_write` step 1 |
| order status transitions gating those sends | `Order.status` compare-and-set (`UPDATE … WHERE status=old`) | row count 0 | `services._transition` (`if not won`) | `services.dispatch_order`, `services.cancel_order` |
| resend | `Notification.trigger_key = resend:{original_id}:{sha256(idempotency key)}` + `ProviderWrite.ref` | DB unique constraints | `services.resend` (existing trigger → answered from it), `services._get_or_create_notification`, `safe_write.try_claim` | `services.resend` → `services.send_notification` |
| follow-up call-off | `ProviderWrite.ref = {prefix}:{trigger_key}:cancel` | DB unique constraint | `safe_write.try_claim` | `services.call_off_follow_up` → `safe_write.safe_write` |
| content disposal (redact) | `ProviderWrite.ref = {prefix}:{trigger_key}:redact` | DB unique constraint | `safe_write.try_claim` | `services.dispose_content` → `safe_write.safe_write` |

## UNKNOWN OUTCOMES

| write | how you check it (lookup, or a same-reference resend) | the reference you check by | where in the code |
| --- | --- | --- | --- |
| send (create_message, immediate or scheduled) | lookup: `list_message(to=…)` paged, match the reference token embedded in the body (kind 3: Twilio has no idempotency key or client reference field); repeated by later reads | `ProviderWrite.ref` → token `sha256(ref)[:10]` (`Notification.reference_token`) in the body | `find=provider.find_message_by_token` in `services.send_notification` → `safe_write.safe_write` step 3; re-checked by `services.refresh_notification` |
| follow-up call-off (update_message status) | same-reference resend (canceling the same message id again cannot create anything), then lookup `fetch_message(sid)` — the record's own id; a refused first attempt is settled by the lookup too | `Notification.message_sid` | `services.call_off_follow_up` (`send=provider.cancel_message`, `find=provider.fetch_message`, `repeat_is_safe=True`, `check_on_refusal=True`) |
| redact (update_message body) | same-reference resend (setting body to `""` again is idempotent), then `fetch_message(sid)` | `Notification.message_sid` | `services.dispose_content` (`send=provider.redact_message`, `find=provider.fetch_message`, `repeat_is_safe=True`) |

## WRITE ORDER

| write | what exists locally BEFORE the call | what is recorded after it returns | where in the code |
| --- | --- | --- | --- |
| send | committed `Notification` (to, kind, body incl. reference token, trigger key) + committed `ProviderWrite(ref, step=send, outcome=sending, claimed_at)` | `ProviderWrite` outcome/provider_id/provider_time; `Notification.message_sid`, `provider_status`, `outcome`, provider dates, error code | before: `services._get_or_create_notification`, `safe_write.try_claim`; after: `safe_write.complete`, `services._apply_message` in `services.send_notification` |
| call-off | committed `ProviderWrite(ref …:cancel, sending)` | `ProviderWrite` outcome; `Notification.call_off_outcome`, `provider_status`, `outcome` | before: `safe_write.try_claim`; after: `safe_write.complete`, `services.call_off_follow_up` |
| redact | committed `ProviderWrite(ref …:redact, sending)`; local body kept until provider confirms | on done: `Notification.body` cleared, `content_disposed_at`; `ProviderWrite` outcome | before: `safe_write.try_claim`; after: `safe_write.complete`, `services.dispose_content` → `services._clear_content` |

## Assumptions & Blockers

- **Blocker (environment, not a gap): the supplied Twilio account is inactive** (401 / 20003 "status 4 is
  not active" on every call). Live verification (real delivery, real scheduled follow-up + call-off, real
  reconciliation) cannot be performed with these credentials. Running headless, I proceed: full build,
  stub-transport tests, and an end-to-end run against a local fake provider via `TWILIO_BASE_URL` +
  `TWILIO_LOOKUPS_BASE_URL`.
- Minor: scheduled follow-ups are created with `messaging_service_sid` + `schedule_type=fixed` + `send_at`
  (docstring: scheduling is for Messaging Services) **and** `from_=TWILIO_FROM_NUMBER` (docstring: a sender
  from the pool may be given) so reconciliation's `From` filter covers them. Assumes the from number is in
  the service's sender pool — unverifiable while the account is inactive.
- Minor: `list_message` date filters are typed `RFC3339DateTime`; the docstring describes whole-day filtering.
  The window is widened to whole days on the provider side and narrowed locally on `date_sent`.
- Minor: a delivered/sent message's `date_sent` is the provider clock used for reconciliation; a message with
  no `date_sent` (scheduled, or unknown outcome) is reported as **unsettled**.
- Minor: follow-up delay = 72h (setting).
- Minor: redacted body may come back as `""` or null; both mean done.

## REQUIRED READING

- MUST load `python-client-initialization` — singleton placement, close obligation, custom transport keeps `timeout` off the wire. (loaded)
- MUST load `python-authentication` — silent no-auth when the credential keyword is omitted. (loaded)
- MUST load `python-calling-endpoints` — status-not-id outcome, `answer`, keyword-only tails. (loaded)
- MUST load `python-models` — `UNSET` vs `None`, open enums, `OptionalNullable`. (loaded)
- MUST load `python-error-handling` — Case B `RawError`, never-sent vs may-have-landed split, decode failures. (loaded)
- MUST load `python-configuration-resilience` — safe write, no retries, reconciliation on provider clock, logging transport. (loaded)
- MUST load `python-testing` — stub transport seam, both transport-failure inputs, duplicate-operation test. (loaded)
