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

Subscriptions (Maxio Advanced Billing)
--------------------------------------

``apps/subscriptions`` adds recurring subscriptions billed through Maxio
Advanced Billing, as a JSON API next to the normal basket/checkout flow.

Setup:

* Install the Maxio SDK into the same environment (it is not on PyPI)::

    pip install "maxio-advanced-billing @ git+https://github.com/context-plugins/maxio-python-sdk.git@main"

* Export ``MAXIO_API_KEY``, ``MAXIO_SITE_SUBDOMAIN``, ``MAXIO_ENVIRONMENT``
  (``us``/``eu``) and ``MAXIO_DEFAULT_PRODUCT_FAMILY``. Optional:
  ``MAXIO_BASE_URL`` (used verbatim as the API base address),
  ``MAXIO_PAYMENT_COLLECTION_METHOD`` (default: bill by invoice, since no card
  is captured), ``MAXIO_TIMEOUT``, ``MAXIO_REFERENCE_PREFIX``.
* ``sandbox/manage.py migrate``

Endpoints (Django session auth; send ``X-CSRFToken`` on POST/DELETE):

* ``GET|POST|DELETE /api/session`` - CSRF token / log in / log out
* ``GET /api/subscription-plans`` - plans, each with ``planHandle``
* ``GET|POST /api/billing-customer`` - read / ensure the Maxio customer
* ``POST /api/subscriptions`` ``{"planHandle": "..."}`` - subscribe; returns
  ``subscriptionId`` (optional ``Idempotency-Key`` header)
* ``GET /api/subscriptions/<id>`` and ``GET /api/my-subscriptions``

Tests: ``cd sandbox && PYTHONPATH=. pytest apps/subscriptions --ds=settings``
