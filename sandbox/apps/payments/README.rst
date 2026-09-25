=====================================
PayPal payments API (sandbox site)
=====================================

A JSON API under ``/api/`` that takes real card payments through PayPal: the order total is
**authorized** (held) when the shopper pays, **captured** when an operator fulfils the order, **voided**
if it is cancelled first, and **refunded** (in full or in part) afterwards. Shoppers can **save a card**
(vaulted at PayPal) and pay later orders with it.

Orders, lines, payment sources/transactions and saved cards are Oscar's own models; the app adds only
``PayPalPayment`` (PayPal's ids/statuses/fee/net for an order), ``PayPalCustomer`` and
``PaymentOperation`` (one row per PayPal write: the duplicate-request claim and its outcome).
Design notes and the SDK contract sheet: ``pay-pal-server-sdk-plan.md`` at the repository root.

Setup
=====

::

    py -3.11 -m venv venv
    venv\Scripts\pip install -e .[test]
    venv\Scripts\pip install -r sandbox/apps/payments/requirements.txt
    cd sandbox
    ..\venv\Scripts\python manage.py migrate

Configuration (environment only — never put the values in a file):

=============================  =====================================================================
``PAYPAL_CLIENT_ID``           REST app client id (required)
``PAYPAL_CLIENT_SECRET``       REST app secret (required)
``PAYPAL_ENVIRONMENT``         ``sandbox`` → ``https://api-m.sandbox.paypal.com``; any other value
                               requires ``PAYPAL_BASE_URL``
``PAYPAL_CURRENCY``            ISO currency of every charge, e.g. ``USD`` (required)
``PAYPAL_BASE_URL``            optional; used verbatim for every PayPal call, token request included
``PAYPAL_TIMEOUT_SECONDS``     optional, default 20
``PAYMENTS_REFERENCE_PREFIX``  optional; prefix of invoice ids / idempotency keys sent to PayPal. Must
                               be unique per install sharing a PayPal account (default derived from
                               ``SECRET_KEY`` and the database name)
=============================  =====================================================================

Endpoints
=========

Authentication is Django's session login; send ``X-CSRFToken`` (returned by login) on every
POST/DELETE. ``fulfil``, ``cancel`` and ``reconciliation`` require a staff user; everything else acts only
on the caller's own orders and cards (other shoppers' objects answer 404).

==========================================  ===========================================================
``POST /api/login``                         ``{"username"|"email", "password"}`` → ``csrfToken``
``POST /api/orders``                        ``{"items": [{"itemId", "quantity"}], "shippingAddress"?}``
                                            → 201 ``orderId`` (optional ``Idempotency-Key``)
``POST /api/orders/{orderId}/pay``          ``{"card": {number, expiry "YYYY-MM", securityCode, name?,
                                            billingAddress?}}`` or ``{"paymentMethodId"}`` → authorize
``POST /api/orders/{orderId}/fulfil``       staff: capture (renews a stale authorization first)
``POST /api/orders/{orderId}/cancel``       staff: void the hold (or cancel an unpaid order)
``POST /api/orders/{orderId}/refunds``      ``Idempotency-Key`` header required; ``{"amount"?}``
                                            (omit for "everything left") → 201 ``refundId``
``GET  /api/my-orders``                     the caller's orders with payment state
``GET  /api/reconciliation?from=&to=``      staff: PayPal's transaction records vs this app's
``POST /api/payment-methods``               ``{"card": {...}}`` + ``Idempotency-Key`` → 201 ``paymentMethodId``
``GET  /api/payment-methods``               the caller's saved cards (brand, last 4, expiry)
``DELETE /api/payment-methods/{id}``        remove a saved card (here at once; at PayPal too)
==========================================  ===========================================================

Status codes: 200/201 done; 202 accepted but not finished at PayPal (repeat the same request to
re-check); 402 declined/refused; 409 state conflict (e.g. ``payer_action_required``,
``authorization_not_renewable``); 422 validation / refund ceiling / reused idempotency key;
502 PayPal refused our credentials or answered unreadably; 504 ``outcomeUnknown`` — the write may have
happened; repeating the same request re-checks it, never repeats it.

Operations
==========

* ``manage.py paypal_retry_deletions`` — retries PayPal-side deletion of saved cards already removed here.
* PayPal calls are logged (method, URL, status, latency, PayPal debug id) under ``apps.payments`` —
  never headers or bodies. Card numbers and security codes are never stored or logged; saved cards
  keep only PayPal's token and ``XXXX-XXXX-XXXX-<last4>``.
* Transaction reporting lags live activity by up to three hours; ``localOnly`` rows younger than that are
  flagged ``withinReportingLag``.

Tests (no network; a stub transport stands in for PayPal)::

    cd sandbox
    ..\venv\Scripts\python manage.py test apps.payments
