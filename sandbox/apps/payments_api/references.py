"""References sent to PayPal with every write: unique to this install and operation."""
import hashlib

from .models import InstallIdentity


def reference(*parts: object) -> str:
    """``<install prefix>:<parts...>`` -- the same on every attempt and repeat."""
    return ':'.join([InstallIdentity.reference_prefix(), *map(str, parts)])


def key_digest(*parts: object) -> str:
    """A short digest of caller-supplied values (idempotency keys, amounts)."""
    return hashlib.sha256(':'.join(map(str, parts)).encode()).hexdigest()[:32]
