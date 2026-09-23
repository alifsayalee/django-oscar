# Order SMS notifications (sandbox app)

Texts shoppers as their orders move, through Twilio. JSON API under `/api/`.

Setup (in the sandbox's virtualenv):

    pip install "twilio-sdk @ git+https://github.com/context-plugins/twilio-python-sdk.git@main"
    sandbox/manage.py migrate order_notifications

Configuration comes from the environment through `sandbox/settings.py`:
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`,
`TWILIO_MESSAGING_SERVICE_SID` (needed to schedule the delivery follow-up; its
sender pool must contain `TWILIO_FROM_NUMBER`), optional `TWILIO_BASE_URL`
(messaging API base address) and `ORDER_NOTIFICATIONS_FOLLOWUP_DELAY_HOURS`
(default 72).

Endpoints (session login; operator = `is_staff`):

| Method | Path | Who |
|---|---|---|
| GET/POST | `/api/contact-numbers` | shopper |
| DELETE | `/api/contact-numbers/{id}` | shopper |
| POST | `/api/orders` `{"lines": [{"productId", "quantity"}]}` | shopper |
| GET | `/api/my-orders` | shopper |
| GET | `/api/orders/{id}/notifications` | owner or operator |
| POST | `/api/orders/{id}/dispatch`, `/api/orders/{id}/cancel` | operator |
| POST | `/api/notifications/{id}/resend` `{"idempotencyKey"}` | operator |
| DELETE | `/api/notifications/{id}/content` | operator |
| GET | `/api/notifications/reconciliation?from=…&to=…` | operator |

The provider cannot call back into this app, so delivery outcomes are read
from Twilio whenever notifications are listed. `manage.py
order_notifications_sweep` does the same in bulk and retries any follow-up
cancellation that could not be completed at the time; run it periodically
(e.g. from cron) in a real deployment.

Tests: `cd sandbox && python manage.py test apps.order_notifications`.
