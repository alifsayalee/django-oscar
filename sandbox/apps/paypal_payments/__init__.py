"""
PayPal card payments and saved cards for the sandbox site, exposed as a JSON
API under ``/api/``.

Only ``gateway`` talks to PayPal; ``services`` owns the payment state machine
and the duplicate-request claims; ``views`` is the HTTP surface.
"""
