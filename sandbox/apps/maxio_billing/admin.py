from django.contrib import admin

from .models import BillingCustomer, SubscriptionEnrollment


@admin.register(BillingCustomer)
class BillingCustomerAdmin(admin.ModelAdmin):
    list_display = ('user', 'status', 'maxio_customer_id', 'reference', 'updated')
    list_filter = ('status',)
    search_fields = ('reference', 'user__email')
    readonly_fields = ('reference', 'created', 'updated')


@admin.register(SubscriptionEnrollment)
class SubscriptionEnrollmentAdmin(admin.ModelAdmin):
    list_display = ('user', 'plan_handle', 'status', 'state', 'maxio_subscription_id', 'next_billing_at', 'created')
    list_filter = ('status', 'plan_handle')
    search_fields = ('reference', 'user__email')
    readonly_fields = ('reference', 'created', 'updated')
