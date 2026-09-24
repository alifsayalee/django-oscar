"""
PayPal card payments, saved cards and reconciliation for the sandbox site.

Exposed as a JSON API under ``/api/`` (see ``urls.py``). Orders, lines and
payment sources are Oscar's own models; this app only adds what Oscar has no
home for: PayPal's identifiers and statuses, the provider-write ledger that
makes every payment call idempotent, and handles to vaulted cards.
"""
