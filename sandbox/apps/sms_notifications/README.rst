=========================
SMS order notifications
=========================

Texts shoppers as their orders move (placed, dispatched, cancelled, plus a delivery follow-up queued *with Twilio*
for a few days after dispatch), and gives staff operators re-send, content disposal and reconciliation. All of it is
a JSON API under ``/api/``; the design notes and the Twilio contract live in ``twilio-sdk-plan.md`` at the repo root.

Configuration
=============

Read from the environment by ``sandbox/settings.py``; never write the values into a file.

* ``TWILIO_ACCOUNT_SID``, ``TWILIO_AUTH_TOKEN``, ``TWILIO_FROM_NUMBER`` (required to send)
* ``TWILIO_MESSAGING_SERVICE_SID`` (required to queue the scheduled follow-up)
* ``TWILIO_BASE_URL`` (optional: replaces the Messages API base address verbatim)
* ``TWILIO_LOOKUPS_BASE_URL``, ``TWILIO_TIMEOUT``, ``SMS_NOTIFICATIONS_REFERENCE_PREFIX``,
  ``SMS_FOLLOW_UP_DELAY_DAYS`` (optional)

The Twilio SDK is installed from source into the same virtualenv::

    venv\Scripts\pip install "twilio-sdk @ git+https://github.com/context-plugins/twilio-python-sdk.git@main"

Endpoints
=========

==========  ==========================================  =========  ===============================================
Method      Path                                        Who        Notes
==========  ==========================================  =========  ===============================================
GET/POST    ``/api/session``                            anyone     CSRF token / Django session login / (DELETE) out
GET/POST    ``/api/contact-numbers``                    shopper    POST ``{"phoneNumber"}`` -> ``contactNumberId``
DELETE      ``/api/contact-numbers/{id}``               shopper    also calls off follow-ups queued for it
POST        ``/api/orders``                             shopper    ``{"lines": [{"productId", "quantity"}]}``
GET         ``/api/my-orders``                          shopper    each order with its notifications' outcomes
GET         ``/api/orders/{id}/notifications``          owner      every message and what became of it
POST        ``/api/orders/{id}/dispatch``               staff      notice + follow-up scheduled at Twilio
POST        ``/api/orders/{id}/cancel``                 staff      notice + follow-up called off at Twilio
POST        ``/api/notifications/{id}/resend``          staff      ``Idempotency-Key`` header required
DELETE      ``/api/notifications/{id}/content``         staff      Twilio redacts the body; the record survives
GET         ``/api/notifications/reconciliation``       staff      ``?from=…&to=…`` ISO-8601 with offset
==========  ==========================================  =========  ===============================================

Outcomes: ``done`` (delivered / in effect), ``pending`` (accepted, not finished), ``failed``, ``unknown`` (may have
happened; the next read asks Twilio), ``skipped`` (nobody to tell, or no longer wanted). Order operations always
succeed; messaging outcomes are reported in their bodies. Resend and content disposal answer ``200`` done,
``202`` pending, ``409`` failed, ``504`` unknown.

Tests
=====

::

    cd sandbox
    ..\venv\Scripts\python -m pytest apps/sms_notifications/tests --ds=settings
