# Order SMS notifications (sandbox app)

JSON API under `/api/` that texts shoppers (via Twilio) as their orders move.
Design notes and the SDK contract sheet: `twilio-sdk-plan.md` at the repo root.

## Setup

```
venv\Scripts\pip install -e .[test]
venv\Scripts\pip install "twilio-sdk @ git+https://github.com/context-plugins/twilio-python-sdk.git@main"
sandbox\manage.py migrate
```

Settings (read from the environment in `sandbox/settings.py`): `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
`TWILIO_FROM_NUMBER`, `TWILIO_MESSAGING_SERVICE_SID`, optional `TWILIO_BASE_URL` (messaging API base address),
`TWILIO_HTTP_TIMEOUT` (default 10 s), `TWILIO_FOLLOWUP_DELAY_SECONDS` (default 3 days).

## Endpoints

Session auth (`GET /api/csrf`, then `POST /api/session` with `{"username", "password"}`; send `X-CSRFToken`).

| Method & path | Who | Notes |
| --- | --- | --- |
| `POST /api/contact-numbers` `{"phoneNumber"}` | shopper | validated + canonicalised by the provider; returns `contactNumberId` |
| `GET /api/contact-numbers` | shopper | own numbers only |
| `DELETE /api/contact-numbers/{id}` | shopper | also calls off queued messages to it |
| `POST /api/orders` `{"items": [{"productId", "quantity"}]}` | shopper | returns `orderId`; "placed" SMS |
| `GET /api/my-orders` | shopper | notification outcomes refreshed from the provider |
| `GET /api/orders/{id}/notifications` | owner / staff | |
| `POST /api/orders/{id}/dispatch` | staff | "on its way" SMS + follow-up scheduled with the provider |
| `POST /api/orders/{id}/cancel` | staff | cancels the queued follow-up, then "cancelled" SMS |
| `POST /api/notifications/{id}/resend` `{"idempotencyKey"}` | staff | returns the new `notificationId`; same key = same result |
| `DELETE /api/notifications/{id}/content` | staff | redacts the text at the provider and locally |
| `GET /api/notifications/reconciliation?from=&to=` | staff | ISO-8601 with offset; provider vs local |

Tests: `cd sandbox && ..\venv\Scripts\python manage.py test apps.order_notifications`
