import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

from .models import InstallIdentity


@dataclass
class ServiceResult:
    status: int
    body: dict[str, Any] = field(default_factory=dict)


class ClientError(Exception):
    """A request this API refuses on its own account (validation, state, ownership)."""

    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


def reference_prefix() -> str:
    """A prefix unique to this install, for every reference sent to PayPal."""
    configured = getattr(settings, "PAYPAL_REFERENCE_PREFIX", "") or ""
    if configured:
        return str(configured)
    identity = InstallIdentity.objects.order_by("pk").first()
    if identity is None:
        identity, _ = InstallIdentity.objects.get_or_create(pk=1, defaults={"value": secrets.token_hex(6)})
    return f"osb-{identity.value}"


def digest(*parts: str, length: int = 24) -> str:
    """A keyed, non-reversible digest (used so no card data or caller key appears in a reference)."""
    message = "|".join(parts).encode()
    return hmac.new(settings.SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()[:length]
