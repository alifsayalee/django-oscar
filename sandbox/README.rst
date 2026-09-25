============
Sandbox site
============

This site is deployed there:

https://latest.oscarcommerce.com
-------------------------------

This is a vanilla install of Oscar with as little customisation as possible to
get a basic site working.  It's really intended for local development and QA.

It does have a few customisations:

* A profile model with a few fields, designed to test Oscar's account section
  which should automatically allow the profile fields to be edited.

It is deployed automatically to: https://latest.oscarcommerce.com

Subscription billing (Maxio Advanced Billing)
---------------------------------------------

``apps/subscriptions`` adds recurring subscriptions, billed through Maxio
Advanced Billing, alongside the normal basket/checkout. Install its extra
dependencies with ``pip install -r requirements_billing.txt`` and set these
environment variables (read through ``settings.py``; never commit values):

* ``MAXIO_API_KEY``, ``MAXIO_SITE_SUBDOMAIN``, ``MAXIO_DEFAULT_PRODUCT_FAMILY``
* ``MAXIO_ENVIRONMENT`` (``US`` or ``EU``, default ``US``)
* optional: ``MAXIO_BASE_URL`` (used verbatim as the API base address),
  ``MAXIO_TIMEOUT_SECONDS``, ``MAXIO_PAYMENT_COLLECTION_METHOD``,
  ``MAXIO_REFERENCE_PREFIX``

Endpoints (session login; POSTs need the ``X-CSRFToken`` header):

* ``GET /api/subscription-plans``
* ``GET|POST /api/billing-customer``
* ``POST /api/subscriptions`` with ``{"planHandle": "..."}``
* ``GET /api/my-subscriptions``

Run its tests with ``python manage.py test apps.subscriptions`` from this directory.
