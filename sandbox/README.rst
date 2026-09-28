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

PayPal payments API (``apps.payments_api``)
-------------------------------------------

A JSON API under ``/api/`` that takes card payments through PayPal. The money is held when the order
is paid, taken at fulfilment, and given back by a refund. Shoppers can also save a card for later
orders. Callers sign in with Django's session login (``POST /api/session``). Fulfil, cancel and
reconciliation are staff-only.

Setup::

    pip install -r sandbox/requirements.txt   # the PayPal Server SDK (import root ``paypal``)
    export PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... PAYPAL_ENVIRONMENT=sandbox PAYPAL_CURRENCY=USD
    # optional: PAYPAL_BASE_URL (used verbatim for every PayPal call), PAYPAL_TIMEOUT, PAYPAL_REFERENCE_PREFIX

Endpoints:

* ``POST /api/orders`` ``{"items": [{"productId": 12, "quantity": 1}], "shippingAddress": {...}}``
  returns ``orderId``
* ``POST /api/orders/{orderId}/pay`` ``{"card": {...}}`` or ``{"paymentMethodId": "..."}``: puts a
  hold on the money
* ``POST /api/orders/{orderId}/fulfil`` (staff): takes the money and reports PayPal's fee and the net
* ``POST /api/orders/{orderId}/cancel`` (staff): releases the hold
* ``POST /api/orders/{orderId}/refunds`` ``{"amount": "5.00", "idempotencyKey": "..."}`` returns
  ``refundId``
* ``GET /api/my-orders``
* ``POST|GET /api/payment-methods``, ``DELETE /api/payment-methods/{paymentMethodId}``
* ``GET /api/reconciliation?from=2026-09-01T00:00:00Z&to=2026-09-30T00:00:00Z`` (staff)

Tests: ``cd sandbox && python manage.py test apps.payments_api``.
