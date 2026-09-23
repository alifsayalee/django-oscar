# smsnotify — SMS order notifications (Twilio)

An additive sandbox app that keeps shoppers informed by text message as their orders
progress. It reuses Oscar's own `Order`/`Line`/`Product`/basket machinery and adds only
the shopper's contact numbers and a notification log. Every Twilio interaction goes
through the `twilio-sdk` APIMatic SDK, wrapped in `gateway.py`.

## Configuration (all from the environment; no values in the repo)

Read through Django settings in `sandbox/settings.py`:

- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` — HTTP Basic credentials.
- `TWILIO_FROM_NUMBER` — the sending number for immediate messages and the only number
  reconciliation counts.
- `TWILIO_MESSAGING_SERVICE_SID` — used to schedule the delivery follow-up.
- `TWILIO_BASE_URL` — optional override for the **messaging** API base URL only
  (`api.twilio.com`). Lookup and other hosts are unaffected.

The auth token is never logged, never returned by an endpoint, and never written to a file.
Shopper phone numbers are never written to logs.

## Endpoints (session auth; all under `/api/`)

Shopper-scoped (act only on the caller's own data):

| Method + path | Purpose |
|---|---|
| `POST /api/contact-numbers` | Register a mobile number (validated + canonicalised via Twilio Lookup). → `contactNumberId` |
| `GET /api/contact-numbers` | The caller's registered numbers. |
| `DELETE /api/contact-numbers/{id}` | Remove a number (and call off any not-yet-sent follow-up to it). |
| `POST /api/orders` | Place an order from `{items:[{productId,quantity}]}`. → `orderId` |
| `GET /api/my-orders` | The caller's orders, each with its notifications' current status. |
| `GET /api/orders/{orderId}/notifications` | What was sent for an order; each entry carries `notificationId`. |
| `DELETE /api/notifications/{id}/content` | Dispose of a message's content (redacted at Twilio too). |

Operator-only (`is_staff`):

| Method + path | Purpose |
|---|---|
| `POST /api/orders/{orderId}/dispatch` | Mark dispatched; tell the shopper; queue a delivery follow-up a few days out. |
| `POST /api/orders/{orderId}/cancel` | Cancel; tell the shopper; call off the pending follow-up. |
| `POST /api/notifications/{id}/resend` | Re-send under a caller-supplied `{idempotencyKey}`. → `notificationId` |
| `GET /api/notifications/reconciliation?from=…&to=…` | Provider vs app message reconciliation over an ISO-8601 range. |

A message that cannot be sent never fails the order operation — the failure is recorded on
the `Notification` and the caller's request still succeeds. A shopper with no number on
file is simply not messaged.

## Layout

- `gateway.py` — the only place that talks to Twilio (SDK client + error boundary + resilience).
- `services.py` — order placement and notification orchestration; status transitions.
- `views.py` / `urls.py` — the HTTP API.
- `models.py` — `ContactNumber`, `Notification`.
- `status.py` — Twilio status → our `NotificationStatus`.
- `tests.py` — unit tests that fake the SDK transport (the documented test seam).

See `../../twilio-sdk-plan.md` for the design/contract sheet and the verified runtime notes.
