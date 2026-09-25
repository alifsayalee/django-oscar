===========================
Payments API (PayPal)
===========================

A JSON API on the sandbox site that takes payment for Oscar orders through
PayPal: hold the money when the shopper pays, take it when an operator
fulfils the order, give it back with refunds. Shoppers can keep cards in
PayPal's vault and pay later orders with them. It talks to PayPal only
through the ``paypal`` SDK (``pip install "paypal @
git+https://github.com/context-plugins/paypal-python-sdk.git@main"``).

Configuration
=============

All read in ``sandbox/settings.py`` from the environment:

``PAYPAL_CLIENT_ID``, ``PAYPAL_CLIENT_SECRET``
    REST credentials of the merchant (business) account.
``PAYPAL_ENVIRONMENT``
    ``sandbox`` selects ``https://api-m.sandbox.paypal.com``. Any other value
    needs ``PAYPAL_BASE_URL``.
``PAYPAL_CURRENCY``
    Currency orders are charged in (amounts are the catalogue prices).
``PAYPAL_BASE_URL``
    Optional. Used verbatim for every PayPal call, the OAuth token included.
``PAYPAL_REQUEST_PREFIX``
    Optional. Prefix of every reference sent to PayPal (``PayPal-Request-Id``,
    ``invoice_id``, ``custom_id``). When unset a random one is generated once
    per database, so installs sharing a PayPal account never collide.
``PAYPAL_TIMEOUT``
    Seconds per PayPal request (default 20).

Endpoints
=========

Callers sign in with Django's session login (the site's login page, or
``POST /api/auth/login``) and send ``X-CSRFToken`` on POST/DELETE
(``GET /api/auth/csrf`` returns a token).

==========================================  ===========  =========================================
Route                                       Who          Does
==========================================  ===========  =========================================
``POST /api/orders``                        shopper      ``{"lines": [{"productId", "quantity"}]}``
                                                         → ``orderId``; order awaits payment
``POST /api/orders/{orderId}/pay``          owner        ``{"card": {...}}`` or
                                                         ``{"paymentMethodId": ...}``; authorizes
``POST /api/orders/{orderId}/fulfil``       staff        captures (renews a stale hold first)
``POST /api/orders/{orderId}/cancel``       staff        voids the hold
``POST /api/orders/{orderId}/refunds``      owner        ``{"amount"?}`` + ``Idempotency-Key``
                                                         header → ``refundId``
``GET /api/my-orders``                      shopper      orders with payment state
``GET /api/reconciliation?from=&to=``       staff        PayPal transactions vs. local records
``POST /api/payment-methods``               shopper      vault a card → ``paymentMethodId``
``GET /api/payment-methods``                shopper      the caller's saved cards
``DELETE /api/payment-methods/{id}``        owner        remove a saved card
==========================================  ===========  =========================================

A card is ``{"number", "expiry": "YYYY-MM", "securityCode", "name",
"billingAddress"?}``. Card numbers are never stored or logged.

Repeating a request is safe: each PayPal write is claimed in the database
under a reference derived from the order and the step before PayPal is
called, and a repeat is answered from that record (or, if the first outcome
was unknown, settled by resending under the same reference).

Tests
=====

From ``sandbox/``::

    python manage.py test apps.payments

PayPal is faked at the SDK's transport, so the tests need no credentials.
