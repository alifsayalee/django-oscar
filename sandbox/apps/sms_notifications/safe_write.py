"""
The single path for every message this app sends: claim, call, check, verify, complete.

The claim is a ``Notification`` row committed with outcome ``sending`` before the
provider is called; the database's unique constraint on ``reference`` rejects a
second claim for the same operation, whichever process or worker it comes from.
Twilio's create-message has no idempotency key, so an unanswered send is checked
by *looking it up* (the body carries a token derived from the reference) and is
never re-sent under a new reference.
"""

import base64
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from twilio_sdk.core import ApiError

from . import gateway
from .models import ContactNumber, Notification

log = logging.getLogger("apps.sms_notifications")

Outcome = Notification.Outcome

TERMINAL = (Outcome.DONE, Outcome.FAILED)


def install_id() -> str:
    configured = settings.SMS_NOTIFICATIONS_INSTALL_ID
    if configured:
        return str(configured)
    seed = f"{settings.SECRET_KEY}:{settings.DATABASES['default']['NAME']}"
    return hashlib.sha256(seed.encode()).hexdigest()[:12]


def ref_token(reference: str) -> str:
    """Short token carried in the message body so the send can be found by its reference."""
    digest = hashlib.sha256(reference.encode()).digest()
    return base64.b32encode(digest).decode()[:10]


def send_window() -> timedelta:
    """How long a claim may sit in ``sending`` before another request may check on it."""
    return timedelta(seconds=float(settings.SMS_NOTIFICATIONS_TIMEOUT_SECONDS) * 2 + 30)


@dataclass(frozen=True)
class SendRequest:
    reference: str
    kind: str
    order_id: int
    contact_number: ContactNumber
    text: str
    scheduled_for: datetime | None = None
    resend_of: Notification | None = None
    idempotency_key_hash: str = ""
    requested_by_id: int | None = None


def _try_claim(req: SendRequest) -> tuple[Notification, bool]:
    token = ref_token(req.reference)
    try:
        with transaction.atomic():
            n = Notification.objects.create(
                reference=req.reference,
                ref_token=token,
                kind=req.kind,
                order_id=req.order_id,
                contact_number=req.contact_number,
                to_number=req.contact_number.phone_number,
                body=f"{req.text} (ref {token})",
                scheduled_for=req.scheduled_for,
                resend_of=req.resend_of,
                idempotency_key_hash=req.idempotency_key_hash,
                requested_by_id=req.requested_by_id,
                outcome=Outcome.SENDING,
                claimed_at=timezone.now(),
            )
        return n, True
    except IntegrityError:
        pass  # someone holds (or held) this reference
    # A send that provably never reached the provider released its claim: re-take it atomically.
    with transaction.atomic():
        retaken = Notification.objects.filter(
            reference=req.reference, outcome=Outcome.FAILED, provider_sid__isnull=True
        ).update(outcome=Outcome.SENDING, outcome_detail="", claimed_at=timezone.now())
    return Notification.objects.get(reference=req.reference), retaken == 1


def _complete(n: Notification, outcome: str, detail: str = "") -> Notification:
    n.outcome = outcome
    n.outcome_detail = detail[:255]
    n.last_checked_at = timezone.now()
    n.save(update_fields=["outcome", "outcome_detail", "last_checked_at", "updated_at"])
    return n


def apply_provider_state(n: Notification, msg: gateway.ProviderMessage, *, outcome: str | None = None) -> Notification:
    """Record the provider's view of the message (ids, status, clock, errors) and the outcome it maps to."""
    n.provider_sid = msg.sid or n.provider_sid
    n.provider_status = msg.status or ""
    n.provider_error_code = msg.error_code
    n.provider_error_message = (msg.error_message or "")[:255]
    n.provider_date_created = msg.date_created or n.provider_date_created
    n.provider_date_sent = msg.date_sent or n.provider_date_sent
    n.outcome = outcome if outcome is not None else gateway.outcome_of_send(msg.status)
    n.outcome_detail = ""
    n.last_checked_at = timezone.now()
    n.save(
        update_fields=[
            "provider_sid", "provider_status", "provider_error_code", "provider_error_message",
            "provider_date_created", "provider_date_sent", "outcome", "outcome_detail",
            "last_checked_at", "updated_at",
        ]
    )
    return n


def verify_and_complete(n: Notification, msg: gateway.ProviderMessage) -> Notification:
    if not msg.sid:
        return _complete(n, Outcome.UNKNOWN, "provider answer carried no message id")
    if msg.to is not None and msg.to != n.to_number:
        apply_provider_state(n, msg, outcome=Outcome.NEEDS_REVIEW)
        return _complete(n, Outcome.NEEDS_REVIEW, "provider reports a different destination")
    return apply_provider_state(n, msg)


def check_by_reference(n: Notification) -> Notification:
    """Settle a send whose outcome is unknown by finding it at the provider -- never by sending again."""
    try:
        found = gateway.find_by_token(n.to_number, n.ref_token, created_since=n.claimed_at)
    except (*gateway.SDK_FAILURES, gateway.ProviderError):
        return _complete(n, Outcome.UNKNOWN, "lookup by reference failed; will retry")
    if found is None:
        # An empty lookup cannot prove the send did not land; it stays unknown under the same reference.
        return _complete(n, Outcome.UNKNOWN, "not found at the provider yet")
    return verify_and_complete(n, found)


def safe_send(req: SendRequest, *, before_send: Callable[[], str | None] | None = None) -> Notification:
    """
    Send ``req`` at most once per reference. Returns the notification record in its
    current state; never raises for a provider failure.

    ``before_send`` runs after the claim is committed and may return a reason to
    abort (e.g. the order was cancelled meanwhile); nothing is sent then.
    """
    n, won = _try_claim(req)
    if not won:
        if n.outcome == Outcome.SENDING and n.claimed_at > timezone.now() - send_window():
            return n  # another request is sending it right now
        if n.outcome not in (Outcome.SENDING, Outcome.UNKNOWN):
            return n  # settled: answer from the stored outcome
        return check_by_reference(n)  # stale or unresolved: look, never create

    if before_send is not None:
        reason = before_send()
        if reason:
            return _complete(n, Outcome.FAILED, reason)  # nothing sent; claim released

    try:
        msg = gateway.send_message(n.to_number, n.body, n.scheduled_for)
    except gateway.NEVER_SENT as e:
        log.warning("SMS %s (notification %s) never left: %s", n.kind, n.pk, type(e).__name__)
        return _complete(n, Outcome.FAILED, "provider unreachable; nothing was sent")
    except ApiError as e:
        if e.status_code < 500:
            code = gateway.twilio_error_code(e.error)
            log.warning("SMS %s (notification %s) refused: HTTP %s code %s", n.kind, n.pk, e.status_code, code)
            return _complete(n, Outcome.FAILED, f"provider refused the message (HTTP {e.status_code}, code {code})")
        log.warning("SMS %s (notification %s): provider HTTP %s; checking by reference", n.kind, n.pk, e.status_code)
        return check_by_reference(n)  # a 5xx may still have landed
    except (httpx.RequestError, ValueError) as e:
        log.warning("SMS %s (notification %s): no readable answer (%s); checking", n.kind, n.pk, type(e).__name__)
        return check_by_reference(n)
    return verify_and_complete(n, msg)
