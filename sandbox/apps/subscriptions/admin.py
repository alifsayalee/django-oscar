from django.contrib import admin

from .models import BillingCustomer, SubscriptionAttempt


@admin.register(BillingCustomer)
class BillingCustomerAdmin(admin.ModelAdmin):
    list_display = ('reference', 'user', 'maxio_customer_id', 'status', 'date_updated')
    list_filter = ('status',)
    search_fields = ('reference', 'user__email')
    readonly_fields = ('user', 'reference', 'maxio_customer_id', 'date_created', 'date_updated')


@admin.register(SubscriptionAttempt)
class SubscriptionAttemptAdmin(admin.ModelAdmin):
    # Status stays editable so an operator can settle an "unknown" attempt after checking Maxio.
    list_display = ('reference', 'user', 'plan_handle', 'status', 'provider_state',
                    'maxio_subscription_id', 'date_created')
    list_filter = ('status', 'plan_handle')
    search_fields = ('reference', 'user__email')
    readonly_fields = ('user', 'plan_handle', 'sequence', 'reference', 'maxio_subscription_id',
                       'provider_state', 'last_error', 'date_created', 'date_updated')
