=====================
PayPal payments (API)
=====================

JSON API under ``/api/`` that takes real money for sandbox orders through
PayPal: hold at checkout, capture at fulfilment, void on cancel, refund on a
return, plus saved (vaulted) cards. Oscar's own ``Order``/``Line`` and payment
``Source``/``Transaction`` models are reused; this app only stores PayPal's ids
and statuses and the durable claims that keep every payment action idempotent.
Card numbers and security codes are never stored or logged.

Configuration (environment variables, read in ``sandbox/settings.py``):

``PAYPAL_CLIENT_ID``, ``PAYPAL_CLIENT_SECRET``, ``PAYPAL_ENVIRONMENT``
(``sandbox``), ``PAYPAL_CURRENCY`` (e.g. ``USD``), optional
``PAYPAL_BASE_URL`` (used verbatim for every PayPal call, token request
included; required for any environment other than ``sandbox``) and
``PAYPAL_TIMEOUT`` (seconds, default 20).

Authentication is Django's session login. ``GET /api/session`` returns a CSRF
token; ``POST /api/session`` with ``{"username"|"email", "password"}`` signs in.
Send the token as ``X-CSRFToken`` on every POST/DELETE.

=========================================  =========  ==============================================
Endpoint                                   Who        Notes
=========================================  =========  ==============================================
``POST /api/orders``                       shopper    ``{"items": [{"productId", "quantity"}]}`` →
                                                      ``orderId``
``POST /api/orders/{orderId}/pay``         owner      ``{"card": {number, expiry YYYY-MM,
                                                      securityCode, name?, billingAddress?}}`` or
                                                      ``{"paymentMethodId"}``; authorizes only
``POST /api/orders/{orderId}/fulfil``      staff      captures; renews a stale authorization first
``POST /api/orders/{orderId}/cancel``      staff      voids the hold (before fulfilment only)
``POST /api/orders/{orderId}/refunds``     owner,     ``Idempotency-Key`` header required;
                                           staff      ``{"amount"?}`` (omit = remaining) → ``refundId``
``GET /api/my-orders``                     shopper    orders with payment state
``GET /api/reconciliation?from=&to=``      staff      PayPal transactions vs. local captures/refunds
``POST /api/payment-methods``              shopper    ``{"card": {...}}``, optional ``Idempotency-Key``
                                                      → ``paymentMethodId``
``GET /api/payment-methods``               shopper    brand, last digits, expiry only
``DELETE /api/payment-methods/{id}``       owner      204
=========================================  =========  ==============================================

Tests (no network; PayPal is replaced at the SDK's transport seam)::

    cd sandbox
    ../venv/Scripts/python manage.py test apps.paypal_payments
