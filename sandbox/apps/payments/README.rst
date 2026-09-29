PayPal payments API (sandbox)
=============================

JSON endpoints under ``/api/`` that take real card payments through PayPal
(PayPal Server SDK, sync client). Authentication is Django's session login.

=========================================  =======  ==============================================
Endpoint                                   Who      What
=========================================  =======  ==============================================
``GET  /api/csrf``                         anyone   CSRF token (cookie + ``csrfToken``)
``POST /api/login`` / ``/api/logout``      anyone   session login ``{"username","password"}``
``POST /api/orders``                       shopper  ``{"items":[{"productId":9,"quantity":2}]}``
``POST /api/orders/{orderId}/pay``         owner    ``{"card":{...}}`` or ``{"paymentMethodId":..}``
``POST /api/orders/{orderId}/fulfil``      staff    capture (renews a stale hold first)
``POST /api/orders/{orderId}/cancel``      staff    void the hold
``POST /api/orders/{orderId}/refunds``     owner    ``Idempotency-Key`` header, ``{"amount":"5.00"}``
``GET  /api/my-orders``                    shopper  own orders with payment state
``GET/POST /api/payment-methods``          shopper  list / save a card
``DELETE /api/payment-methods/{id}``       owner    remove a saved card
``GET  /api/reconciliation?from=&to=``     staff    PayPal transactions vs. local captures/refunds
=========================================  =======  ==============================================

Card body: ``{"number","expiry":"YYYY-MM","securityCode","name","billingAddress":{"addressLine1",
"city","state","postalCode","countryCode"}}``. Card data is never stored or logged.

Status codes for payment actions: ``200`` done, ``202`` accepted but not finished (repeat the
request), ``409`` refused / needs review, ``504`` outcome unknown (repeat the same request; it is
settled by re-sending under the same ``PayPal-Request-Id``, never by a second charge).

Settings (read from the environment in ``sandbox/settings.py``): ``PAYPAL_CLIENT_ID``,
``PAYPAL_CLIENT_SECRET``, ``PAYPAL_ENVIRONMENT`` (``sandbox``), ``PAYPAL_CURRENCY``, optional
``PAYPAL_BASE_URL`` (used verbatim for every call, token included) and ``PAYPAL_REFERENCE_PREFIX``.

Tests: ``cd sandbox && python manage.py test apps.payments`` (PayPal faked at the SDK transport).
