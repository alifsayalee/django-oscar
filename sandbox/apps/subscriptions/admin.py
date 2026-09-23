from typing import TYPE_CHECKING

from django.contrib import admin

from .models import BillingCustomer, SubscriptionRequest

if TYPE_CHECKING:
    BillingCustomerModelAdmin = admin.ModelAdmin[BillingCustomer]
    SubscriptionRequestModelAdmin = admin.ModelAdmin[SubscriptionRequest]
else:
    BillingCustomerModelAdmin = SubscriptionRequestModelAdmin = admin.ModelAdmin


@admin.register(BillingCustomer)
class BillingCustomerAdmin(BillingCustomerModelAdmin):
    list_display = ('user', 'reference', 'maxio_customer_id', 'claimed_at', 'updated_at')
    search_fields = ('reference', 'user__email')


@admin.register(SubscriptionRequest)
class SubscriptionRequestAdmin(SubscriptionRequestModelAdmin):
    # Operators resolve UNKNOWN / NEEDS_REVIEW rows here.
    list_display = ('reference', 'user', 'plan_handle', 'status', 'maxio_subscription_id', 'maxio_state',
                    'updated_at')
    list_filter = ('status', 'plan_handle')
    search_fields = ('reference', 'user__email')
