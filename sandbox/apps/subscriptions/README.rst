=======================================
Subscriptions (Maxio Advanced Billing)
=======================================

Recurring subscriptions for the sandbox, billed by Maxio Advanced Billing. Maxio is the system of
record for plans and subscriptions; this app adds a JSON API next to Oscar's own basket/checkout.

Install
-------

The Maxio Python SDK is not on PyPI; install it (and its runtime dependencies) into the sandbox venv::

    venv\Scripts\pip install "maxio-advanced-billing @ git+https://github.com/context-plugins/maxio-python-sdk.git@main"
    venv\Scripts\pip install "httpx>=0.28.1,<1.0.0" "pydantic[email]>=2.11.0,<3.0.0" "typing-extensions>=4.13.0,<5.0.0"

Configuration (environment variables, read in ``sandbox/settings.py``)
----------------------------------------------------------------------

``MAXIO_API_KEY``, ``MAXIO_SITE_SUBDOMAIN``, ``MAXIO_DEFAULT_PRODUCT_FAMILY`` (a product family
*handle*), optional ``MAXIO_BASE_URL`` (used verbatim instead of the subdomain), ``MAXIO_ENVIRONMENT``
(``us``/``eu``, default ``us``), ``MAXIO_TIMEOUT`` (seconds, default 15) and ``MAXIO_REFERENCE_PREFIX``
(defaults to a random id stored in the database).

API (session-authenticated; POST/DELETE need the ``X-CSRFToken`` header)
------------------------------------------------------------------------

- ``GET /api/session`` - who is signed in; sets the CSRF cookie and returns ``csrfToken``
- ``POST /api/session`` - ``{"username"|"email", "password"}``: Django session login
- ``DELETE /api/session`` - sign out
- ``GET /api/subscription-plans`` - plans of the configured family, each with ``planHandle``
- ``POST /api/billing-customer`` - ensure a Maxio customer exists for the user (idempotent)
- ``POST /api/subscriptions`` - ``{"planHandle": ...}``: subscribe; returns ``subscriptionId``.
  Optional ``Idempotency-Key`` header.
- ``GET /api/my-subscriptions`` - the user's subscriptions, read back from Maxio

``POST /api/subscriptions`` answers 201 (created, in effect), 200 (repeat of a request already in
effect), 202 (accepted, not in effect yet: ``outcome`` is ``pending``, ``unknown`` or ``sending``),
404 (unknown plan), 422 (rejected, or the subscription is not in effect), 502/503 (provider or
configuration problem) and 504 with ``outcomeUnknown: true`` when Maxio's answer was lost - repeat
the same request to settle it; it is never sent under a new reference.

Without an ``Idempotency-Key`` a user holds at most one live subscription per plan: repeating the
request returns the existing one. Once Maxio reports it ended (canceled/expired), a new request
creates a new subscription.

Tests
-----

::

    cd sandbox
    ..\venv\Scripts\python manage.py test apps.subscriptions
