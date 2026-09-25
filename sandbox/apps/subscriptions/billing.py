"""
Subscription billing against Maxio Advanced Billing.

Every provider write that creates something (a Maxio customer, a Maxio subscription) goes through
``safe_write``: a claim row is committed *before* the call, keyed by the same reference that is sent
to Maxio, so a double submit reaches Maxio at most once and a write whose answer was lost can be
found again by that reference. Maxio refuses a second customer or subscription carrying a reference
it already holds ("Reference: must be unique"), which makes a resend under the same reference a safe
check as well.
"""
import datetime
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Generic, NoReturn, TypeVar

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.utils import timezone
from maxio_advanced_billing.core import ApiError, UnsetType
from maxio_advanced_billing.models import (
    CreateCustomer, CreateCustomerRequest, CreateSubscription, CreateSubscriptionRequest, CustomerErrorResponse1,
    CustomerResponse, ErrorListResponse1, Product, Subscription, SubscriptionResponse)
from maxio_advanced_billing.models.enums import CollectionMethod, SubscriptionState

from .maxio import get_client
from .models import MaxioClaim, MaxioInstall

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Longer than one attempt can take (client timeout, no retries): a "sending" claim younger than this
# belongs to a request still waiting on Maxio.
SEND_WINDOW = datetime.timedelta(seconds=60)

# Transport failures that happen before the request leaves: nothing can have reached Maxio.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

REFERENCE_TAKEN = re.compile(r"reference.*(must be unique|has been taken|already)", re.IGNORECASE)
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")
PLAN_PAGE_SIZE = 200
MAX_PLAN_PAGES = 10


# ---------------------------------------------------------------------------
# Errors surfaced to the API boundary
# ---------------------------------------------------------------------------

class BillingError(Exception):
    """A failure with the HTTP status and code the API answers with. ``message`` is ours, never str(e)."""

    def __init__(self, status_code: int, code: str, message: str, *, outcome_unknown: bool = False,
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.details = details or {}


class OutcomeUnknown(BillingError):
    """A write may have reached Maxio and neither the resend nor the lookup could say what happened."""

    def __init__(self, reference: str) -> None:
        super().__init__(
            504, "outcome_unknown",
            "Maxio did not confirm the outcome of this request. Repeat the same request to check it again.",
            outcome_unknown=True, details={"reference": reference})


class Unreadable(Exception):
    """A 2xx answer that is missing a member we depend on."""


def _provider_messages(e: ApiError[Any]) -> list[str]:
    """The provider's own validation messages, from the operation's typed 422 bodies only."""
    error = e.error
    if isinstance(error, ErrorListResponse1):
        return list(error.errors)
    if isinstance(error, CustomerErrorResponse1):
        errors = error.errors
        if isinstance(errors, list):
            return [str(m) for m in errors]
        if not isinstance(errors, UnsetType) and isinstance(errors.customer, str):
            return [errors.customer]
    return []


def reference_taken(e: ApiError[Any]) -> bool:
    """Decided from the error BODY: Maxio already holds a record with this reference."""
    return e.status_code == 422 and any(REFERENCE_TAKEN.search(m) for m in _provider_messages(e))


def raise_for_api_error(e: ApiError[Any]) -> NoReturn:
    """One mapping from a Maxio API error to our answer, used at every call site."""
    status = e.status_code
    if status in (401, 403):
        raise BillingError(502, "billing_provider_config", "The billing provider refused our credentials.") from e
    if status == 429:
        raise BillingError(503, "billing_rate_limited", "The billing provider is rate-limiting us.") from e
    if status in (400, 422):
        raise BillingError(422, "billing_rejected", "The billing provider rejected the request.",
                           details={"providerErrors": _provider_messages(e)}) from e
    raise BillingError(502, "billing_provider_error", "The billing provider returned an error.",
                       outcome_unknown=status >= 500) from e


def provider_read(call: Callable[[], T]) -> T:
    """Run a read-only SDK call and translate every failure kind. Reads are not retried."""
    try:
        return call()
    except ImproperlyConfigured as e:
        logger.error("Maxio is not configured: %s", e)
        raise BillingError(503, "billing_not_configured", "Subscription billing is not configured.") from e
    except ApiError as e:
        logger.warning("Maxio read failed: HTTP %s", e.status_code)
        raise_for_api_error(e)
    except NEVER_SENT as e:
        raise BillingError(502, "billing_unavailable", "The billing provider could not be reached.") from e
    except httpx.RequestError as e:
        raise BillingError(504, "billing_timeout", "The billing provider did not answer in time.") from e
    except ValueError as e:  # pydantic ValidationError, or a body that is not JSON
        logger.warning("Unreadable Maxio response: %s", type(e).__name__)
        raise BillingError(502, "billing_provider_unreadable",
                           "The billing provider sent a response we could not read.") from e


def _not_found_is_none(call: Callable[[], T]) -> T | None:
    """A lookup whose miss is a 404. Maxio answers that 404 with an empty body, which is still a 404."""
    try:
        return call()
    except ApiError as e:
        if e.status_code == 404:
            return None
        raise


# ---------------------------------------------------------------------------
# Provider status -> our outcome
# ---------------------------------------------------------------------------

def status_from_provider(state: object) -> str:
    """The ONE place a Maxio subscription state becomes ours: done / pending / failed / unknown."""
    match state:
        case SubscriptionState.ACTIVE | SubscriptionState.TRIALING:
            return MaxioClaim.DONE
        case (SubscriptionState.PENDING | SubscriptionState.ASSESSING | SubscriptionState.AWAITING_SIGNUP
              | SubscriptionState.PAST_DUE | SubscriptionState.SOFT_FAILURE | SubscriptionState.UNPAID
              | SubscriptionState.PAUSED | SubscriptionState.ON_HOLD | SubscriptionState.SUSPENDED):
            return MaxioClaim.PENDING
        case (SubscriptionState.FAILED_TO_CREATE | SubscriptionState.CANCELED | SubscriptionState.EXPIRED
              | SubscriptionState.TRIAL_ENDED):
            return MaxioClaim.FAILED
        case _:
            # An unlisted value (open enum), UNSET or None: never done, never failed.
            return MaxioClaim.UNKNOWN


def _state_text(state: object) -> str:
    if isinstance(state, SubscriptionState):
        return state.value
    if isinstance(state, str):
        return state
    return ""


def _value(v: T | None | UnsetType) -> T | None:
    return None if isinstance(v, UnsetType) else v


# ---------------------------------------------------------------------------
# References and the claim store
# ---------------------------------------------------------------------------

def install_prefix() -> str:
    prefix = str(settings.MAXIO_REFERENCE_PREFIX).strip()
    if prefix:
        return prefix
    install = MaxioInstall.objects.order_by("pk").first()
    if install is None:
        with transaction.atomic():
            MaxioInstall.objects.get_or_create(pk=1)
        install = MaxioInstall.objects.order_by("pk").first()
    assert install is not None
    return f"oscar-{install.install_id}"


def customer_reference(user: Any) -> str:
    return f"{install_prefix()}:u{user.pk}:customer"


def subscription_reference(user: Any, plan_handle: str, idempotency_key: str | None) -> str:
    if idempotency_key:
        return f"{install_prefix()}:u{user.pk}:sub:key:{idempotency_key}"
    # One live subscription per user and plan: once Maxio says the previous one ended, the next
    # request takes the next number. Two concurrent requests compute the same number and the claim's
    # unique reference lets only one of them through.
    ended = MaxioClaim.objects.filter(
        user=user, kind=MaxioClaim.KIND_SUBSCRIPTION, plan_handle=plan_handle,
        outcome=MaxioClaim.FAILED, provider_id__isnull=False, reference__contains=":sub:" + plan_handle + ":",
    ).count()
    return f"{install_prefix()}:u{user.pk}:sub:{plan_handle}:{ended + 1}"


def try_claim(reference: str, kind: str, user: Any, plan_handle: str = "") -> bool:
    """Insert-or-fail. The unique reference makes the database reject every second claim."""
    try:
        with transaction.atomic():
            MaxioClaim.objects.create(reference=reference, kind=kind, user=user, plan_handle=plan_handle,
                                      outcome=MaxioClaim.SENDING, claimed_at=timezone.now())
        return True
    except IntegrityError:
        return False


def load_existing(reference: str) -> MaxioClaim:
    return MaxioClaim.objects.get(reference=reference)


def complete(reference: str, outcome: str, provider_id: int | None = None, provider_state: str = "",
             provider_time: datetime.datetime | None = None) -> MaxioClaim | None:
    """Record what is known. A ``failed`` with no provider id means nothing exists: release the claim."""
    with transaction.atomic():
        if outcome == MaxioClaim.FAILED and provider_id is None:
            MaxioClaim.objects.filter(reference=reference).delete()
            return None
        claim = MaxioClaim.objects.select_for_update().get(reference=reference)
        claim.outcome = outcome
        if provider_id is not None:
            claim.provider_id = provider_id
        if provider_state:
            claim.provider_state = provider_state
        if provider_time is not None:
            claim.provider_time = provider_time
        claim.save()
        return claim


# ---------------------------------------------------------------------------
# The safe write
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Answer:
    """What one write step's response says."""

    provider_id: int
    outcome: str
    provider_state: str
    provider_time: datetime.datetime | None
    # Echoed values checked before the outcome is kept: they must equal what we asked for.
    echoed: dict[str, object]


@dataclass(frozen=True)
class WriteResult(Generic[T]):
    claim: MaxioClaim | None
    payload: T | None   # the provider's record, when this request obtained one
    fresh: bool         # True when this request took the claim (first attempt)


def safe_write(reference: str, *, kind: str, user: Any, plan_handle: str,  # noqa: C901 - one ladder, by design
               send: Callable[[str], T], find: Callable[[str], T | None], read: Callable[[T], Answer],
               expected: dict[str, object]) -> WriteResult[T]:
    """
    The one path for every Maxio write that creates something.

    send(ref)   makes the provider call carrying ``ref`` as the record's reference
    find(ref)   looks the record up by that reference; None when Maxio has none (yet)
    read(r)     reads the response as an Answer; raises Unreadable when a member we need is missing
    expected    the echoed values the provider must return (reference, plan) - else needs_review

    Maxio refuses a second record carrying a reference it already holds, so a resend under the same
    reference is a safe check: it creates what never landed and is refused for what did.
    """
    # 1. CLAIM FIRST - the database decides the winner.
    checking = False
    if not try_claim(reference, kind, user, plan_handle):
        existing = load_existing(reference)
        if existing.outcome == MaxioClaim.SENDING and existing.claimed_at > timezone.now() - SEND_WINDOW:
            return WriteResult(existing, None, False)          # in flight: no provider call
        if existing.outcome not in (MaxioClaim.SENDING, MaxioClaim.UNKNOWN):
            return WriteResult(existing, None, False)          # settled: answer from it
        checking = True                                        # stale or unknown: check, same reference
    resending = checking

    # 2. CALL (a first attempt, or the check by same-reference resend).
    result: T | None = None
    try:
        result = send(reference)
    except ImproperlyConfigured as e:
        complete(reference, MaxioClaim.UNKNOWN if resending else MaxioClaim.FAILED)
        raise BillingError(503, "billing_not_configured", "Subscription billing is not configured.") from e
    except NEVER_SENT as e:
        if resending:
            complete(reference, MaxioClaim.UNKNOWN)
            raise OutcomeUnknown(reference) from e
        complete(reference, MaxioClaim.FAILED)                 # never left: nothing happened
        raise BillingError(502, "billing_unavailable", "The billing provider could not be reached.") from e
    except ApiError as e:
        if reference_taken(e):
            logger.info("Maxio already holds %s; looking it up", reference)   # an earlier attempt landed
        elif e.status_code < 500:
            refused = e.status_code in (400, 422) or not resending
            complete(reference, MaxioClaim.FAILED if refused else MaxioClaim.UNKNOWN)
            if not refused:
                raise OutcomeUnknown(reference) from e
            raise_for_api_error(e)
        else:
            logger.warning("Maxio answered HTTP %s to %s; looking it up", e.status_code, reference)
    except (httpx.RequestError, ValueError) as e:
        logger.warning("No readable answer for %s (%s); looking it up", reference, type(e).__name__)

    answer: Answer | None = None
    if result is not None:
        try:
            answer = read(result)
        except Unreadable:
            logger.warning("Maxio answer for %s is missing members; looking it up", reference)
            result = None

    # 3. CHECK by the reference we sent.
    if result is None:
        try:
            result = find(reference)
            answer = read(result) if result is not None else None
        except (ApiError, httpx.RequestError, ValueError, Unreadable, ImproperlyConfigured) as e:
            complete(reference, MaxioClaim.UNKNOWN)
            raise OutcomeUnknown(reference) from e
        if result is None or answer is None:
            complete(reference, MaxioClaim.UNKNOWN)            # not found YET is not "did not happen"
            raise OutcomeUnknown(reference)

    assert answer is not None
    # 4. VERIFY the echo before keeping it.
    if answer.echoed != expected:
        logger.error("Maxio echoed %s for %s, expected %s", answer.echoed, reference, expected)
        complete(reference, MaxioClaim.NEEDS_REVIEW, answer.provider_id, answer.provider_state, answer.provider_time)
        raise BillingError(502, "needs_review",
                           "The billing provider recorded something other than what was asked; it will be reviewed.",
                           details={"reference": reference})

    # 5. COMPLETE from what the provider said.
    claim = complete(reference, answer.outcome, answer.provider_id, answer.provider_state, answer.provider_time)
    return WriteResult(claim, result, not checking)


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    handle: str
    product_id: int | None
    name: str
    description: str
    price_in_cents: int
    interval: int | None
    interval_unit: str | None
    currency: str | None


def _plan_from_product(product: Product) -> Plan | None:
    handle = _value(product.handle)
    price = _value(product.price_in_cents)
    if not handle or price is None:
        logger.warning("Skipping Maxio product %s: no handle or price", _value(product.id))
        return None
    unit = _value(product.interval_unit)
    return Plan(
        handle=handle,
        product_id=_value(product.id),
        name=_value(product.name) or handle,
        description=_value(product.description) or "",
        price_in_cents=price,
        interval=_value(product.interval),
        interval_unit=str(unit) if unit is not None else None,
        currency=None,  # products carry no currency; it is the site's
    )


def list_plans() -> list[Plan]:
    family = str(settings.MAXIO_DEFAULT_PRODUCT_FAMILY).strip()
    if not family:
        raise BillingError(503, "billing_not_configured", "MAXIO_DEFAULT_PRODUCT_FAMILY is not set.")
    plans: list[Plan] = []
    for page in range(1, MAX_PLAN_PAGES + 1):
        try:
            batch = provider_read(lambda: get_client().product_families.list_products_for_product_family(
                "handle:" + family, page=page, per_page=PLAN_PAGE_SIZE))
        except BillingError as e:
            if e.code == "billing_provider_unreadable" or e.status_code == 422:
                # Maxio answers an unknown family with a 404 and an empty body - a rejection, not an outage.
                raise BillingError(502, "plan_family_unavailable",
                                   "The configured product family could not be read.") from e
            raise
        for item in batch:
            plan = _plan_from_product(item.product)
            if plan is not None:
                plans.append(plan)
        if len(batch) < PLAN_PAGE_SIZE:
            break
    return plans


def get_plan(plan_handle: str) -> Plan:
    for plan in list_plans():
        if plan.handle == plan_handle:
            return plan
    raise BillingError(404, "unknown_plan", "No such subscription plan.", details={"planHandle": plan_handle})


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------

def _read_customer(result: CustomerResponse) -> Answer:
    customer = result.customer
    customer_id = _value(customer.id)
    if not isinstance(customer_id, int):
        raise Unreadable("customer.id")
    # Customers carry no status: an id that Maxio stored under our reference is the whole outcome.
    return Answer(customer_id, MaxioClaim.DONE, "", _value(customer.created_at),
                  {"reference": _value(customer.reference)})


def ensure_customer(user: Any) -> MaxioClaim:
    """Make sure a Maxio customer exists for ``user``; idempotent - at most one per user, ever."""
    email = (user.email or "").strip()
    if not email:
        raise BillingError(422, "email_required", "Your account needs an email address before subscribing.")
    reference = customer_reference(user)
    local_part = email.split("@")[0]
    body = CreateCustomerRequest(customer=CreateCustomer(
        first_name=(user.first_name or "").strip() or local_part,
        last_name=(user.last_name or "").strip() or "Customer",
        email=email,
        reference=reference,
    ))

    def send(ref: str) -> CustomerResponse:
        return get_client().customers.create_customer(body=body)

    def find(ref: str) -> CustomerResponse | None:
        return _not_found_is_none(lambda: get_client().customers.read_customer_by_reference(ref))

    written = safe_write(reference, kind=MaxioClaim.KIND_CUSTOMER, user=user, plan_handle="", send=send, find=find,
                         read=_read_customer, expected={"reference": reference})
    claim = written.claim
    if claim is None:  # released: cannot happen for a write that returned, but never answer success from it
        raise OutcomeUnknown(reference)
    if claim.outcome == MaxioClaim.DONE and claim.provider_id is not None:
        return claim
    if claim.outcome == MaxioClaim.SENDING:
        raise BillingError(409, "in_progress", "Your billing account is being set up; try again shortly.",
                           details={"reference": reference})
    if claim.outcome == MaxioClaim.NEEDS_REVIEW:
        raise BillingError(502, "needs_review", "Your billing account needs review.", details={"reference": reference})
    raise OutcomeUnknown(reference)


def customer_claim(user: Any) -> MaxioClaim | None:
    return MaxioClaim.objects.filter(reference=customer_reference(user), outcome=MaxioClaim.DONE).first()


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

def _read_subscription(result: SubscriptionResponse) -> Answer:
    sub = _value(result.subscription)
    if sub is None:
        raise Unreadable("subscription")
    sub_id = _value(sub.id)
    if not isinstance(sub_id, int):
        raise Unreadable("subscription.id")
    product = _value(sub.product)
    return Answer(
        provider_id=sub_id,
        outcome=status_from_provider(_value(sub.state)),
        provider_state=_state_text(_value(sub.state)),
        provider_time=_value(sub.created_at),
        echoed={"reference": _value(sub.reference), "plan": _value(product.handle) if product else None},
    )


@dataclass(frozen=True)
class SubscribeResult:
    claim: MaxioClaim
    subscription: Subscription | None
    created: bool


def subscribe(user: Any, plan_handle: str, idempotency_key: str | None = None) -> SubscribeResult:  # noqa: C901
    """
    Enroll ``user`` in ``plan_handle``. A repeat of the same request (double click, retry, same
    Idempotency-Key) never creates a second subscription: it is answered from the recorded outcome,
    refreshed from Maxio.
    """
    if idempotency_key is not None and not IDEMPOTENCY_KEY.match(idempotency_key):
        raise BillingError(400, "invalid_idempotency_key", "Idempotency-Key must be 1-64 of [A-Za-z0-9._-].")
    plan = get_plan(plan_handle)
    customer = ensure_customer(user)

    for _attempt in range(3):
        reference = subscription_reference(user, plan.handle, idempotency_key)
        body = CreateSubscriptionRequest(subscription=CreateSubscription(
            product_handle=plan.handle,
            customer_reference=customer.reference,
            reference=reference,
            # Invoice the customer (no card on file): the plans need no payment method.
            payment_collection_method=CollectionMethod.REMITTANCE,
        ))

        def send(ref: str, body: CreateSubscriptionRequest = body) -> SubscriptionResponse:
            return get_client().subscriptions.create_subscription(body=body)

        def find(ref: str) -> SubscriptionResponse | None:
            return _not_found_is_none(lambda: get_client().subscriptions.find_subscription(reference=ref))

        written = safe_write(reference, kind=MaxioClaim.KIND_SUBSCRIPTION, user=user, plan_handle=plan.handle,
                             send=send, find=find, read=_read_subscription,
                             expected={"reference": reference, "plan": plan.handle})
        claim = written.claim
        if claim is None:
            raise OutcomeUnknown(reference)
        if claim.plan_handle != plan.handle:
            raise BillingError(409, "idempotency_key_reused",
                               "This Idempotency-Key was already used for a different plan.")
        if written.payload is not None:
            # This request made the write, or settled an earlier unknown one by its check.
            return SubscribeResult(claim, _value(written.payload.subscription), True)

        # A repeat. Answer from the record, refreshed from Maxio, through the same done check.
        subscription = None
        refreshable = (MaxioClaim.DONE, MaxioClaim.PENDING, MaxioClaim.FAILED)
        if claim.provider_id is not None and claim.outcome in refreshable:
            provider_id = claim.provider_id
            refreshed = provider_read(lambda: get_client().subscriptions.read_subscription(provider_id))
            try:
                answer = _read_subscription(refreshed)
            except Unreadable as e:
                raise BillingError(502, "billing_provider_unreadable",
                                   "The billing provider sent a response we could not read.") from e
            claim = complete(claim.reference, answer.outcome, answer.provider_id, answer.provider_state) or claim
            subscription = _value(refreshed.subscription)
        if claim.outcome == MaxioClaim.FAILED and claim.provider_id is not None and idempotency_key is None:
            # The earlier subscription has ended in Maxio: this is a request for a new one.
            continue
        return SubscribeResult(claim, subscription, False)
    raise BillingError(409, "subscription_conflict", "Could not settle the subscription request; try again.")


def list_my_subscriptions(user: Any) -> list[Subscription]:
    customer = customer_claim(user)
    if customer is None or customer.provider_id is None:
        return []
    customer_id = customer.provider_id
    items = provider_read(lambda: get_client().customers.list_customer_subscriptions(customer_id))
    subscriptions = []
    for item in items:
        sub = _value(item.subscription)
        if sub is not None:
            subscriptions.append(sub)
    return subscriptions


def unsettled_requests(user: Any) -> list[MaxioClaim]:
    return list(MaxioClaim.objects.filter(
        user=user, kind=MaxioClaim.KIND_SUBSCRIPTION,
        outcome__in=(MaxioClaim.SENDING, MaxioClaim.UNKNOWN, MaxioClaim.NEEDS_REVIEW)))


# ---------------------------------------------------------------------------
# Presentation helpers (UNSET never leaves this module)
# ---------------------------------------------------------------------------

def money(cents: int | None) -> str | None:
    if cents is None:
        return None
    return str((Decimal(cents) / 100).quantize(Decimal("0.01")))


def _iso(value: datetime.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    return {
        "planHandle": plan.handle,
        "productId": plan.product_id,
        "name": plan.name,
        "description": plan.description,
        "priceInCents": plan.price_in_cents,
        "price": money(plan.price_in_cents),
        "interval": plan.interval,
        "intervalUnit": plan.interval_unit,
    }


def subscription_to_dict(sub: Subscription) -> dict[str, Any]:
    product = _value(sub.product)
    state = _value(sub.state)
    price = _value(sub.product_price_in_cents)
    unit = _value(product.interval_unit) if product else None
    return {
        "subscriptionId": _value(sub.id),
        "reference": _value(sub.reference),
        "planHandle": _value(product.handle) if product else None,
        "planName": _value(product.name) if product else None,
        "state": _state_text(state) or None,
        "outcome": status_from_provider(state),
        "priceInCents": price,
        "price": money(price),
        "currency": _value(sub.currency),
        "interval": _value(product.interval) if product else None,
        "intervalUnit": str(unit) if unit is not None else None,
        "nextBillingAt": _iso(_value(sub.current_period_ends_at)),
        "currentPeriodEndsAt": _iso(_value(sub.current_period_ends_at)),
        "nextAssessmentAt": _iso(_value(sub.next_assessment_at)),
        "activatedAt": _iso(_value(sub.activated_at)),
        "createdAt": _iso(_value(sub.created_at)),
        "canceledAt": _iso(_value(sub.canceled_at)),
    }


def claim_to_dict(claim: MaxioClaim) -> dict[str, Any]:
    return {
        "reference": claim.reference,
        "planHandle": claim.plan_handle or None,
        "outcome": claim.outcome,
        "subscriptionId": claim.provider_id,
        "state": claim.provider_state or None,
        "requestedAt": _iso(claim.claimed_at),
    }
