"""
Subscription use cases: plans, the user's Maxio customer, subscribing, and
reading subscriptions back. Views call these; these call ``gateway``.
"""
import datetime
import logging

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import gateway
from .exceptions import (
    BillingError, BillingNotConfigured, ProviderRejected, SubscriptionInProgress, SubscriptionNotFound, UnknownPlan)
from .models import BillingAccount, SubscriptionEnrollment

logger = logging.getLogger(__name__)

PLANS_CACHE_SECONDS = 300

# Maxio states after which a subscription no longer counts as the user's
# current subscription to that plan, so they may subscribe again.
ENDED_STATES = frozenset({'canceled', 'expired', 'failed_to_create'})

# How long an attempt whose outcome is unknown blocks a new attempt. Far
# longer than any request can stay in flight (the client timeout is seconds),
# so a write that has not surfaced by then did not land.
RECONCILE_AFTER = datetime.timedelta(minutes=10)


# --- plans -------------------------------------------------------------------------------------------------


def _product_family():
    family = settings.MAXIO_DEFAULT_PRODUCT_FAMILY.strip()
    if not family:
        raise BillingNotConfigured("Subscription billing is not configured.")
    return family


def list_plans():
    family = _product_family()
    key = 'subscriptions:plans:%s' % family
    plans = cache.get(key)
    if plans is None:
        plans = gateway.list_plans(family)
        cache.set(key, plans, PLANS_CACHE_SECONDS)
    return plans


def get_plan(plan_handle):
    for plan in list_plans():
        if plan.handle == plan_handle:
            return plan
    raise UnknownPlan("There is no plan with handle %r." % plan_handle)


# --- customer ----------------------------------------------------------------------------------------------


def billing_account(user, create=True):
    """The user's billing account (their Maxio customer reference); None if absent and not created."""
    account = BillingAccount.objects.filter(user=user).first()
    if account is not None or not create:
        return account
    try:
        with transaction.atomic():
            return BillingAccount.objects.create(user=user)
    except IntegrityError:
        # A concurrent request created it first.
        return BillingAccount.objects.get(user=user)


def _customer_names(user):
    local_part = user.email.split('@', 1)[0]
    first_name = (user.first_name or '').strip() or local_part
    last_name = (user.last_name or '').strip() or '-'
    return first_name, last_name


def ensure_customer(user):
    """
    Return ``(customer, created)`` for the user's Maxio customer, creating it
    when it does not exist. Maxio allows one customer per reference, so repeat
    or concurrent calls converge on one customer.
    """
    account = billing_account(user)
    reference = account.reference
    customer = gateway.find_customer(reference)
    if customer is not None:
        _remember(account, customer)
        return customer, False
    if not user.email:
        raise ProviderRejected("An email address is required to set up billing.", status_code=422,
                               code='email_required')
    first_name, last_name = _customer_names(user)
    try:
        customer = gateway.create_customer(reference=reference, first_name=first_name, last_name=last_name,
                                           email=user.email)
    except BillingError as exc:
        # A concurrent request may have created it (Maxio rejects the duplicate
        # reference), or our create may have landed without us hearing back.
        if not (exc.outcome_unknown or exc.status_code == 422):
            raise
        existing = gateway.find_customer(reference)
        if existing is None:
            raise
        _remember(account, existing)
        return existing, False
    logger.info("Created Maxio customer %s for user %s", customer.id, user.pk)
    _remember(account, customer)
    return customer, True


def _remember(account, customer):
    if account.maxio_customer_id != customer.id:
        account.maxio_customer_id = customer.id
        account.save(update_fields=['maxio_customer_id'])


# --- subscribing -------------------------------------------------------------------------------------------


def _reconcile(enrollment):
    """
    Settle an open enrollment. Returns its subscription when it stands, or
    None once the enrollment is closed and a new attempt may start.
    """
    if enrollment.status == SubscriptionEnrollment.ACTIVE:
        subscription = gateway.read_subscription(enrollment.maxio_subscription_id)
        if subscription is None or subscription.state in ENDED_STATES:
            enrollment.mark(SubscriptionEnrollment.ENDED)
            return None
        return subscription

    subscription = gateway.find_subscription(enrollment.reference)
    if subscription is not None:
        enrollment.mark(SubscriptionEnrollment.ACTIVE, maxio_subscription_id=subscription.id)
        if subscription.state in ENDED_STATES:
            enrollment.mark(SubscriptionEnrollment.ENDED)
            return None
        return subscription

    if timezone.now() - enrollment.date_updated > RECONCILE_AFTER:
        logger.warning("Enrollment %s (%s) never surfaced at Maxio; closing it", enrollment.pk,
                       enrollment.reference)
        enrollment.mark(SubscriptionEnrollment.FAILED)
        return None

    raise SubscriptionInProgress(
        "A subscription request for this plan is already being processed; retry shortly.",
        outcome_unknown=enrollment.status == SubscriptionEnrollment.UNKNOWN)


def _open_enrollment(user, plan_handle):
    return (SubscriptionEnrollment.objects
            .filter(user=user, plan_handle=plan_handle, status__in=SubscriptionEnrollment.OPEN_STATUSES)
            .first())


def subscribe(user, plan_handle):
    """
    Subscribe the user to a plan. Returns ``(subscription, created)``; a repeat
    of a request that already succeeded returns the same subscription with
    ``created`` False. Must run outside a transaction, so the enrollment row is
    committed (and visible to a concurrent request) before Maxio is called.
    """
    plan = get_plan(plan_handle)

    enrollment = _open_enrollment(user, plan.handle)
    if enrollment is not None:
        subscription = _reconcile(enrollment)
        if subscription is not None:
            return subscription, False

    try:
        with transaction.atomic():
            enrollment = SubscriptionEnrollment.objects.create(user=user, plan_handle=plan.handle)
    except IntegrityError:
        raise SubscriptionInProgress(
            "A subscription request for this plan is already being processed; retry shortly.") from None

    try:
        customer, _ = ensure_customer(user)
    except BillingError:
        # No subscription request was sent.
        enrollment.mark(SubscriptionEnrollment.FAILED)
        raise
    enrollment.mark(SubscriptionEnrollment.PENDING, maxio_customer_id=customer.id)

    subscription = _create_subscription(enrollment, billing_account(user).reference)
    enrollment.mark(SubscriptionEnrollment.ACTIVE, maxio_subscription_id=subscription.id)
    logger.info("User %s subscribed to %s: Maxio subscription %s", user.pk, plan.handle, subscription.id)
    return subscription, True


def _create_subscription(enrollment, customer_reference):
    """Create the enrollment's subscription at Maxio; an unanswered create is looked up by its reference."""
    try:
        return gateway.create_subscription(
            product_handle=enrollment.plan_handle, customer_reference=customer_reference,
            reference=enrollment.reference,
            payment_collection_method=settings.MAXIO_PAYMENT_COLLECTION_METHOD.strip().lower())
    except BillingError as exc:
        if not exc.outcome_unknown:
            enrollment.mark(SubscriptionEnrollment.FAILED)
            raise
        # It may have landed: ask Maxio by the reference we sent. Not finding
        # it (yet) proves nothing, so the outcome stays unknown.
        try:
            found = gateway.find_subscription(enrollment.reference)
        except BillingError:
            found = None
        if found is None:
            enrollment.mark(SubscriptionEnrollment.UNKNOWN)
            raise
        return found


# --- reading back ------------------------------------------------------------------------------------------


def my_subscriptions(user):
    account = billing_account(user, create=False)
    if account is None:
        return []
    customer_id = account.maxio_customer_id
    if customer_id is None:
        customer = gateway.find_customer(account.reference)
        if customer is None:
            return []
        _remember(account, customer)
        customer_id = customer.id
    subscriptions = gateway.list_customer_subscriptions(customer_id)
    return sorted(subscriptions, key=lambda s: s.created_at or timezone.now(), reverse=True)


def my_subscription(user, subscription_id):
    account = billing_account(user, create=False)
    if account is None:
        raise SubscriptionNotFound("Subscription not found.")
    subscription = gateway.read_subscription(subscription_id)
    if subscription is None or subscription.customer_reference != account.reference:
        # Never reveal whether someone else's subscription exists.
        raise SubscriptionNotFound("Subscription not found.")
    return subscription
