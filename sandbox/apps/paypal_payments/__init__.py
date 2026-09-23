"""
PayPal payments and saved cards for the sandbox site.

Exposes a JSON API under ``/api/`` that places Oscar orders, authorizes them
through PayPal (one-off card or a vaulted card), captures at fulfilment,
voids on cancel, refunds after fulfilment and reconciles against PayPal's
transaction reporting.
"""
