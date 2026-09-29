=======================
SMS order notifications
=======================

Texts shoppers as their Oscar orders move (placed, dispatched, cancelled, and a
"how did the delivery go?" follow-up queued with Twilio for days later), and
gives operators a view of what reached the customer. JSON API under ``/api/``,
authenticated with the sandbox's own session login.

Configuration (environment → ``sandbox/settings.py``)
=====================================================

``TWILIO_ACCOUNT_SID``, ``TWILIO_AUTH_TOKEN``, ``TWILIO_FROM_NUMBER``,
``TWILIO_MESSAGING_SERVICE_SID`` (required); ``TWILIO_BASE_URL`` (optional,
messaging API base address, used verbatim); ``TWILIO_LOOKUPS_BASE_URL``
(optional, number-lookup base address); ``SMS_FOLLOWUP_DELAY_HOURS`` (default
72); ``SMS_REFERENCE_PREFIX`` (unique per install).

Install the SDK: ``pip install -r sandbox/requirements_sms_notifications.txt``,
then ``sandbox/manage.py migrate``.

Endpoints
=========

Shopper (own data only): ``POST/GET /api/contact-numbers``,
``DELETE /api/contact-numbers/{id}``, ``POST /api/orders``
(``{"lines": [{"productId": 1, "quantity": 1}], "shippingAddress": {...}}``),
``GET /api/my-orders``, ``GET /api/orders/{id}/notifications``.

Operator (``is_staff``): ``POST /api/orders/{id}/dispatch``,
``POST /api/orders/{id}/cancel``, ``POST /api/notifications/{id}/resend``
(``Idempotency-Key`` header required), ``DELETE /api/notifications/{id}/content``,
``GET /api/notifications/reconciliation?from=…&to=…``.

How it behaves
==============

* A number is validated with Twilio Lookup and stored in its canonical E.164
  form; an unusable number is rejected with 422.
* A message that cannot be sent never fails the order operation; its outcome
  (``done``, ``pending``, ``failed``, ``unknown``, ``skipped_no_number``) is on
  the notification. Delivery state is read back from Twilio on each read
  (there is no webhook).
* Every provider write (send, call-off, redaction) is claimed in the database
  before the call, under a deterministic reference, so a repeated request never
  sends twice. A write whose answer was lost is looked up by that reference
  (carried in the message body as ``Ref XXXXXXXX``) rather than sent again.
* Cancelling an order (or removing the number) cancels a follow-up that Twilio
  still holds; disposing of content redacts the body at Twilio, keeping the
  record and its status.
* Reconciliation asks Twilio only for messages sent from ``TWILIO_FROM_NUMBER``
  and reports matched, provider-only, app-only, unsettled and never-sent.

Tests
=====

``PYTHONPATH=sandbox python -m pytest sandbox/apps/sms_notifications/tests --ds=settings -c /dev/null --rootdir=sandbox``
(no network: the SDK client runs over a fake transport).
