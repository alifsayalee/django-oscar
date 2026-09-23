PayPal payments API (sandbox)
=============================

JSON API under ``/api/`` that places Oscar orders, holds money with PayPal at
checkout, captures it at fulfilment, releases or refunds it afterwards, and
lets shoppers save cards in PayPal's vault. All PayPal calls go through
``gateway.py`` (PayPal Server SDK, package ``paypal``).

Configuration (environment, read in ``sandbox/settings.py``):
``PAYPAL_CLIENT_ID``, ``PAYPAL_CLIENT_SECRET``, ``PAYPAL_ENVIRONMENT``
(``sandbox``; any other value needs ``PAYPAL_BASE_URL``), ``PAYPAL_CURRENCY``,
optional ``PAYPAL_BASE_URL`` (used verbatim for every call, token included) and
``PAYPAL_TIMEOUT_SECONDS``.

Authentication is the sandbox's session login (``/en-gb/accounts/login/``);
send the ``csrftoken`` cookie value as ``X-CSRFToken`` on POST/DELETE.

==========================================  ======  ==========================================
Endpoint                                    Who     Notes
==========================================  ======  ==========================================
``POST /api/orders``                        owner   ``{"items": [{"productId", "quantity"}]}``
``POST /api/orders/{id}/pay``               owner   ``{"card": {...}}`` or ``{"paymentMethodId"}``
``POST /api/orders/{id}/fulfil``            staff   captures; renews a stale hold first
``POST /api/orders/{id}/cancel``            staff   voids the hold
``POST /api/orders/{id}/refunds``           owner   ``Idempotency-Key`` header, optional
                                            /staff  ``{"amount": "5.00"}`` (default: remainder)
``GET  /api/my-orders``                     owner
``GET/POST /api/payment-methods``           owner   card: ``number``, ``expiry`` (YYYY-MM),
                                                    ``securityCode``, ``name``, ``billingAddress``
``DELETE /api/payment-methods/{id}``        owner
``GET  /api/reconciliation?from=&to=``      staff   ISO-8601 with offset; any range ≤ 3 years
==========================================  ======  ==========================================

Tests: ``cd sandbox && python manage.py test apps.paypal_payments`` (PayPal is
stubbed at the SDK transport seam; no network).
