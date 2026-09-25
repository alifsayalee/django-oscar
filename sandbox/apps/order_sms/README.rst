=========================
Order SMS notifications
=========================

A sandbox app that texts shoppers as their orders move, through Twilio, and
gives operators a view of what reached them. JSON API under ``/api/``.

Configuration (environment, read in ``sandbox/settings.py``)
------------------------------------------------------------

``TWILIO_ACCOUNT_SID``, ``TWILIO_AUTH_TOKEN``, ``TWILIO_FROM_NUMBER``,
``TWILIO_MESSAGING_SERVICE_SID`` (needed to schedule the follow-up) and,
optionally, ``TWILIO_BASE_URL`` (overrides the messaging API host only).
Optional: ``ORDER_SMS_FOLLOWUP_DELAY_HOURS`` (default 72),
``ORDER_SMS_REFERENCE_PREFIX`` (unique per install sharing an account),
``ORDER_SMS_TIMEOUT_SECONDS`` (default 10).

Endpoints
---------

Session (Django session login; CSRF stays on — send ``X-CSRFToken``):

- ``GET /api/csrf`` · ``POST /api/login`` ``{"username", "password"}`` · ``POST /api/logout``

Shopper (acts on the caller's own data only):

- ``POST /api/contact-numbers`` ``{"number"}`` → ``contactNumberId``; 422 if the provider rejects the number
- ``GET /api/contact-numbers`` · ``DELETE /api/contact-numbers/{id}``
- ``POST /api/orders`` ``{"lines": [{"productId", "quantity"}]}`` → ``orderId``
- ``GET /api/my-orders`` · ``GET /api/orders/{orderId}/notifications``

Operator (``is_staff``):

- ``POST /api/orders/{orderId}/dispatch`` · ``POST /api/orders/{orderId}/cancel``
- ``POST /api/notifications/{id}/resend`` with an ``Idempotency-Key`` header → ``notificationId``
- ``DELETE /api/notifications/{id}/content``
- ``GET /api/notifications/reconciliation?from=<ISO-8601>&to=<ISO-8601>``

Behaviour worth knowing
-----------------------

- A message that cannot be sent never fails the order operation; its
  notification records the outcome (``pending``, ``done``, ``failed``,
  ``unknown``). There are no webhooks: status is re-read from Twilio when
  notifications are listed.
- Each message is sent at most once: a claim row with a unique reference is
  written before Twilio is called, and a send whose outcome is unknown is
  found again by the ``Ref`` token in its text, never re-sent blind.
- The delivery follow-up is scheduled with Twilio at dispatch time and
  cancelled at Twilio when the order is cancelled (``cancelState``).
  Repeating ``cancel`` re-attempts a call-off that was not confirmed.
- Content disposal redacts the text at Twilio (the message record stays) and
  locally. A message still scheduled or sending cannot be redacted yet (409).
- Phone numbers are never logged; responses mask them except in the owner's
  own contact-number list.

Tests: ``cd sandbox && python manage.py test apps.order_sms`` (stub transport,
no network).
