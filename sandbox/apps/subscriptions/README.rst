=====================================
Subscription billing (Maxio) JSON API
=====================================

Recurring subscriptions for the sandbox, billed by Maxio Advanced Billing. It sits
beside the basket and checkout flow and does not change it.

Setup
=====

::

    venv\Scripts\pip install -r sandbox/apps/subscriptions/requirements.txt
    cd sandbox && ..\venv\Scripts\python manage.py migrate

``sandbox/settings.py`` reads these environment variables:

==================================  ==================================================
``MAXIO_API_KEY``                   API key (required)
``MAXIO_SITE_SUBDOMAIN``            site subdomain (required unless ``MAXIO_BASE_URL`` is set)
``MAXIO_DEFAULT_PRODUCT_FAMILY``    handle of the product family whose products are the plans
``MAXIO_BASE_URL``                  optional; used as the API base address exactly as given
``MAXIO_ENVIRONMENT``               ``US`` (default) or ``EU``
``MAXIO_REFERENCE_PREFIX``          prefix for references sent to Maxio (default ``oscar-sandbox``).
                                    Give each install that shares a Maxio site its own prefix.
==================================  ==================================================

Endpoints
=========

Callers authenticate with the sandbox's own Django session login. POST requests need
the CSRF token (``X-CSRFToken`` header, plus a ``Referer`` on HTTPS).

=======================================  ===========================================================
``GET/POST/DELETE /api/session``         GET returns the current user and CSRF token; POST
                                         ``{"username", "password"}`` logs in; DELETE logs out
``GET /api/subscription-plans``          the family's active plans, each with ``planHandle``
``POST /api/maxio-customer``             create the caller's Maxio customer if it does not exist
                                         (safe to repeat)
``POST /api/subscriptions``              ``{"planHandle": "..."}``: subscribe. The response has
                                         ``subscriptionId`` at the top level
``GET /api/my-subscriptions``            the caller's subscriptions, read live from Maxio
``GET /api/subscriptions/<id>``          one of the caller's subscriptions
=======================================  ===========================================================

``POST /api/subscriptions`` answers ``201`` only when Maxio reports the subscription
``active`` or ``trialing``. Other answers:

* ``202``: accepted but not in effect yet. The body's ``outcome`` is one of
  ``in_progress``, ``pending``, ``needs_review`` or ``unknown``.
* ``409``: Maxio reports the subscription failed or canceled.
* ``422``: Maxio or this API rejected the request.
* ``504`` with ``outcomeUnknown: true``: Maxio's answer was lost. Send the same request
  again to check. That second request looks the subscription up by its reference; it
  never creates a second one.

How duplicates are prevented
============================

Before calling Maxio, each create commits a claim row (``MaxioCustomer``,
``SubscriptionEnrollment``). The row's unique constraints stop a double-click, or a
second worker, from creating a second customer or subscription. When an outcome is
unknown, it is resolved by looking the record up in Maxio by the reference that was
sent. See ``writes.safe_write``.

Tests
=====

::

    cd sandbox && ..\venv\Scripts\python manage.py test apps.subscriptions
