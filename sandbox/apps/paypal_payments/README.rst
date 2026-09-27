PayPal payments for the sandbox
===============================

A JSON API under ``/api/`` that takes card payments through PayPal: authorize
at checkout, capture at fulfilment, void on cancel, refund after fulfilment,
plus saved cards (PayPal vault) and a reconciliation report.

It reuses Oscar's models: orders are Oscar ``Order``/``Line`` rows placed
through Oscar's basket and ``OrderCreator``; money movements are recorded on
Oscar's ``payment.Source``/``payment.Transaction``; saved cards are Oscar
``payment.Bankcard`` rows holding only a masked number and the PayPal vault
token. Full card numbers are sent to PayPal and never stored or logged.

Setup
-----

::

    py -3.11 -m venv venv
    venv\Scripts\pip install -e .[test]
    venv\Scripts\pip install -r sandbox\requirements-paypal.txt

Configuration comes from the environment, read in ``sandbox/settings.py``:

``PAYPAL_CLIENT_ID``, ``PAYPAL_CLIENT_SECRET``
    REST credentials of the merchant account.
``PAYPAL_ENVIRONMENT``
    ``sandbox`` selects ``https://api-m.sandbox.paypal.com``. Any other value
    requires ``PAYPAL_BASE_URL``.
``PAYPAL_CURRENCY``
    Currency every order is charged in (catalogue prices are charged in it).
``PAYPAL_BASE_URL`` (optional)
    Used verbatim for every PayPal call, including the OAuth token request.
``PAYPAL_REFERENCE_PREFIX`` (optional, default ``oscar-sandbox``)
    Prefix of every reference sent to PayPal; make it unique per install when
    several installs share one PayPal account.

Build the sandbox database (run from ``sandbox/``)::

    python manage.py migrate
    python manage.py loaddata fixtures/auth.json
    python manage.py loaddata fixtures/child_products.json
    python manage.py oscar_import_catalogue fixtures/books.computers-in-fiction.csv fixtures/books.essential.csv fixtures/books.hacking.csv
    python manage.py oscar_populate_countries --initial-only
    python manage.py loaddata fixtures/pages.json fixtures/ranges.json fixtures/offers.json
    python manage.py loaddata fixtures/orders.json

Endpoints
---------

Callers log in with Django's session (``POST /api/auth/login``) and send the
CSRF token they get back in an ``X-CSRFToken`` header on every POST/DELETE.

=========================================  ========  ==============================================
Endpoint                                   Who       What
=========================================  ========  ==============================================
``GET  /api/auth/csrf``                    anyone    CSRF cookie + ``csrfToken``
``POST /api/auth/login``                   anyone    ``{"username"|"email", "password"}``
``POST /api/auth/register``                anyone    ``{"email", "password"}`` (creates a shopper)
``POST /api/orders``                       shopper   ``{"lines": [{"productId", "quantity"}], "shippingAddress"?}``; optional ``Idempotency-Key``; returns ``orderId``
``POST /api/orders/{orderId}/pay``         shopper   ``{"card": {...}}`` or ``{"paymentMethodId"}`` -- authorizes (holds) the total
``POST /api/orders/{orderId}/fulfil``      staff     captures; renews a stale authorization first
``POST /api/orders/{orderId}/cancel``      staff     voids the hold (or cancels an unpaid order)
``POST /api/orders/{orderId}/refunds``     shopper   ``{"amount"?}`` + ``Idempotency-Key`` header; returns ``refundId``
``GET  /api/my-orders``                    shopper   the caller's orders with payment state
``GET  /api/reconciliation?from=&to=``     staff     PayPal transactions vs. this site's captures/refunds
``POST /api/payment-methods``              shopper   ``{"card": {...}}``; returns ``paymentMethodId``
``GET  /api/payment-methods``              shopper   the caller's saved cards
``DELETE /api/payment-methods/{id}``       shopper   deletes the vault token and the saved card
=========================================  ========  ==============================================

A card is ``{"number", "expiry": "YYYY-MM", "securityCode", "name",
"billingAddress": {"line1", "city", "state", "postalCode", "countryCode"}}``.

Status codes: ``200``/``201`` done, ``202`` accepted but PayPal has not
finished (repeat the request to check), ``402`` declined, ``409`` not allowed
in the current state or needs review, ``504`` with ``outcomeUnknown`` when
PayPal did not confirm a write -- repeat the same request (same body and
idempotency key) and it is settled without being applied twice.

Design notes
------------

* Every PayPal write goes through ``gateway.safe_write``: a unique
  ``ProviderWrite.ref`` row is inserted (the claim) before the call and is
  sent as ``PayPal-Request-Id``; an unknown outcome is settled by resending
  under the same id inside PayPal's retention window, never under a new one.
* Refund amounts are reserved with an atomic conditional update (backed by a
  check constraint) so concurrent partial refunds cannot exceed the capture.
* Reconciliation filters both sides on PayPal's clock, pages through every
  result page and splits ranges longer than PayPal's 31-day limit. PayPal's
  reporting lags by up to three hours, so fresh captures show up as
  ``localOnly`` with ``withinReportingLag: true`` until reported.

Tests::

    cd sandbox
    python manage.py test apps.paypal_payments
