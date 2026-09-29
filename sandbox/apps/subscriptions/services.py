"""
Subscription billing against Maxio Advanced Billing: plans, the shopper's
Maxio customer, subscribing, and reading subscriptions back.
"""
import functools
import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, TypeVar

from maxio_advanced_billing import MaxioAdvancedBillingClient
from maxio_advanced_billing.core import ApiError, UnsetType
from maxio_advanced_billing.models import (
    CreateCustomer, CreateCustomerRequest, CreateSubscription, CreateSubscriptionRequest, CustomerResponse,
    Subscription, SubscriptionResponse)
from maxio_advanced_billing.models.enums import CollectionMethod, SubscriptionState

from . import maxio
from .models import BillingClaim
from .errors import ProviderError
from .safe_write import Answer, safe_write

T = TypeVar('T')

# Products per page when listing a product family (the provider's maximum).
PAGE_SIZE = 200

# The status a customer write is read as when Maxio echoes the reference sent.
CUSTOMER_ON_RECORD = 'on_record'


def present(value: T | None | UnsetType) -> T | None:
    """An SDK member, with UNSET resolved to None before it leaves this module."""
    return None if isinstance(value, UnsetType) else value


def isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def format_cents(cents: int | None) -> str | None:
    return None if cents is None else '%.2f' % (Decimal(cents) / 100)


def none_if_not_found(call: Callable[[], T]) -> T | None:
    try:
        return call()
    except ApiError as e:
        if e.status_code == 404:
            return None
        raise


# Plans

@dataclass(frozen=True)
class Plan:
    handle: str
    name: str
    description: str
    price_in_cents: int
    interval: int | None
    interval_unit: str | None
    product_id: int | None

    def as_json(self) -> dict[str, Any]:
        return {
            'planHandle': self.handle,
            'name': self.name,
            'description': self.description,
            'priceInCents': self.price_in_cents,
            'price': format_cents(self.price_in_cents),
            'interval': self.interval,
            'intervalUnit': self.interval_unit,
            'productId': self.product_id,
        }


def list_plans(client: MaxioAdvancedBillingClient) -> list[Plan]:
    """The live (non-archived) products of the configured product family."""
    family = 'handle:%s' % maxio.product_family()
    plans = []
    page = 1
    while True:
        responses = client.product_families.list_products_for_product_family(
            family, page=page, per_page=PAGE_SIZE)
        for response in responses:
            product = response.product
            handle = present(product.handle)
            price = present(product.price_in_cents)
            if not handle or price is None or present(product.archived_at) is not None:
                continue
            unit = present(product.interval_unit)
            plans.append(Plan(
                handle=handle,
                name=present(product.name) or handle,
                description=present(product.description) or '',
                price_in_cents=price,
                interval=present(product.interval),
                interval_unit=str(unit) if unit is not None else None,
                product_id=present(product.id),
            ))
        if len(responses) < PAGE_SIZE:
            return plans
        page += 1


def find_plan(client: MaxioAdvancedBillingClient, handle: str) -> Plan | None:
    return next((plan for plan in list_plans(client) if plan.handle == handle), None)


# Step 1: the shopper's Maxio customer

def customer_key(user: Any) -> str:
    return 'customer:%s' % user.pk


def read_customer(response: CustomerResponse, reference: str) -> Answer:
    customer = response.customer
    customer_id = present(customer.id)
    echoed = present(customer.reference)
    on_record = customer_id is not None and echoed == reference
    return Answer(
        provider_id=str(customer_id) if customer_id is not None else '',
        status=CUSTOMER_ON_RECORD if on_record else None,
        provider_time=present(customer.created_at),
        snapshot={'customerId': customer_id, 'reference': echoed},
    )


def customer_outcome(status: object) -> str:
    # A customer has no status of its own: it is in effect once Maxio holds a
    # customer under the reference this site sent. Anything else is unknown.
    return BillingClaim.DONE if status == CUSTOMER_ON_RECORD else BillingClaim.UNKNOWN


def ensure_customer(client: MaxioAdvancedBillingClient, user: Any) -> BillingClaim:
    """The Maxio customer for ``user``, created once however often it is asked for."""
    def send(reference: str) -> CustomerResponse:
        return client.customers.create_customer(body=CreateCustomerRequest(customer=CreateCustomer(
            first_name=user.first_name or user.get_username(),
            last_name=user.last_name or 'Customer',
            email=user.email,
            reference=reference,
        )))

    def find(reference: str) -> CustomerResponse | None:
        return none_if_not_found(lambda: client.customers.read_customer_by_reference(reference))

    return safe_write(
        customer_key(user),
        kind=BillingClaim.CUSTOMER,
        user=user,
        send=send,
        find=find,
        read=read_customer,
        outcome_of=customer_outcome,
        # Maxio allows one customer per reference, so a resend under the same
        # reference cannot make a second one.
        repeat_is_safe=True,
    )


# Step 2: the subscription

@functools.lru_cache(maxsize=4)
def collection_method(client: MaxioAdvancedBillingClient) -> CollectionMethod:
    """
    How subscriptions are paid. This API captures no payment method, so the
    subscriber is invoiced: "remittance" on a Relationship Invoicing site,
    "invoice" on a legacy (Statements) site. The default, "automatic", would
    charge the signup balance immediately and fail with no card on file.
    """
    enabled = present(client.sites.read_site().site.relationship_invoicing_enabled)
    if enabled is None:
        raise ProviderError(502, 'The billing provider did not say how this site invoices.')
    return CollectionMethod.REMITTANCE if enabled else CollectionMethod.INVOICE


def subscription_outcome(state: object) -> str:
    """What a subscription's state means for the shopper who asked for it."""
    match state:
        case SubscriptionState.ACTIVE | SubscriptionState.TRIALING:
            return BillingClaim.DONE
        case SubscriptionState.PENDING | SubscriptionState.ASSESSING | SubscriptionState.AWAITING_SIGNUP:
            return BillingClaim.PENDING  # the provider has not finished
        case (SubscriptionState.PAST_DUE | SubscriptionState.SOFT_FAILURE | SubscriptionState.UNPAID
              | SubscriptionState.PAUSED | SubscriptionState.ON_HOLD | SubscriptionState.SUSPENDED):
            return BillingClaim.PENDING  # exists, but something is outstanding
        case (SubscriptionState.FAILED_TO_CREATE | SubscriptionState.CANCELED | SubscriptionState.EXPIRED
              | SubscriptionState.TRIAL_ENDED):
            return BillingClaim.FAILED  # never made, or made and undone
        case _:
            return BillingClaim.UNKNOWN  # absent, or a state newer than this SDK


def subscription_json(subscription: Subscription) -> dict[str, Any]:
    product = present(subscription.product)
    customer = present(subscription.customer)
    state = present(subscription.state)
    price = present(subscription.product_price_in_cents)
    return {
        'subscriptionId': present(subscription.id),
        'reference': present(subscription.reference),
        'planHandle': present(product.handle) if product is not None else None,
        'planName': present(product.name) if product is not None else None,
        'priceInCents': price,
        'price': format_cents(price),
        'currency': present(subscription.currency),
        'state': str(state) if state is not None else None,
        'status': subscription_outcome(state),
        'nextBillingAt': isoformat(present(subscription.current_period_ends_at)),
        'nextAssessmentAt': isoformat(present(subscription.next_assessment_at)),
        'createdAt': isoformat(present(subscription.created_at)),
        'activatedAt': isoformat(present(subscription.activated_at)),
        'customerId': present(customer.id) if customer is not None else None,
    }


def read_subscription(response: SubscriptionResponse, reference: str) -> Answer:
    subscription = present(response.subscription)
    if subscription is None:
        return Answer(provider_id='', status=None, provider_time=None)
    subscription_id = present(subscription.id)
    echoed = present(subscription.reference)
    return Answer(
        provider_id=str(subscription_id) if subscription_id is not None else '',
        # Not ours, or no id: the state says nothing about this write.
        status=present(subscription.state) if subscription_id is not None and echoed == reference else None,
        provider_time=present(subscription.created_at),
        amount=present(subscription.product_price_in_cents),
        snapshot=subscription_json(subscription),
    )


def subscription_key(user: Any, plan: Plan, idempotency_key: str) -> str:
    # The same shopper, plan and Idempotency-Key is the same request; a new
    # key is a new subscription the caller meant.
    request_id = hashlib.sha256(idempotency_key.encode()).hexdigest()[:16] if idempotency_key else 'default'
    return 'subscription:%s:%s:%s' % (user.pk, plan.handle, request_id)


def subscribe(client: MaxioAdvancedBillingClient, user: Any, plan: Plan,
              idempotency_key: str = '') -> BillingClaim:
    """
    Subscribe ``user`` to ``plan``. Returns the claim of the step the request
    stopped at: the customer's when step 1 is not done, else the subscription's.
    """
    customer = ensure_customer(client, user)
    if customer.outcome != BillingClaim.DONE:
        return customer
    customer_id = int(customer.provider_id)
    payment_collection_method = collection_method(client)

    def send(reference: str) -> SubscriptionResponse:
        return client.subscriptions.create_subscription(body=CreateSubscriptionRequest(
            subscription=CreateSubscription(
                product_handle=plan.handle,
                customer_id=customer_id,
                payment_collection_method=payment_collection_method,
                reference=reference,
            )))

    def find(reference: str) -> SubscriptionResponse | None:
        return none_if_not_found(lambda: client.subscriptions.find_subscription(reference=reference))

    return safe_write(
        subscription_key(user, plan, idempotency_key),
        kind=BillingClaim.SUBSCRIPTION,
        user=user,
        plan_handle=plan.handle,
        send=send,
        find=find,
        read=read_subscription,
        outcome_of=subscription_outcome,
        # Maxio does not document subscription references as unique: a
        # resend could make a second subscription, so only a lookup checks.
        repeat_is_safe=False,
        sent_amount=plan.price_in_cents,
    )


# Reading back

def my_subscriptions(client: MaxioAdvancedBillingClient, user: Any) -> dict[str, Any]:
    """
    The shopper's subscriptions as Maxio has them, plus the requests this site
    made that Maxio has not settled yet.
    """
    subscriptions = []
    customer = BillingClaim.objects.filter(key=customer_key(user), outcome=BillingClaim.DONE).first()
    if customer is not None:
        for response in client.customers.list_customer_subscriptions(int(customer.provider_id)):
            subscription = present(response.subscription)
            if subscription is not None:
                subscriptions.append(subscription_json(subscription))

    listed = {str(s['subscriptionId']) for s in subscriptions}
    unsettled = [
        {
            'kind': claim.kind,
            'reference': claim.reference,
            'planHandle': claim.plan_handle or None,
            'status': claim.outcome,
            'requestedAt': isoformat(claim.claimed_at),
        }
        for claim in BillingClaim.objects.filter(user=user).exclude(
            outcome__in=(BillingClaim.DONE, BillingClaim.FAILED))
        if not claim.provider_id or claim.provider_id not in listed
    ]
    return {
        'customerId': int(customer.provider_id) if customer is not None else None,
        'subscriptions': subscriptions,
        'unsettled': unsettled,
    }
