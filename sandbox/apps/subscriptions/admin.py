from django.contrib import admin

from .models import BillingCustomer, BillingSubscription


@admin.register(BillingCustomer)
class BillingCustomerAdmin(admin.ModelAdmin):
    list_display = ('user', 'provider_id', 'outcome', 'reference', 'claimed_at')
    list_filter = ('outcome',)
    search_fields = ('reference', 'user__email')


@admin.register(BillingSubscription)
class BillingSubscriptionAdmin(admin.ModelAdmin):
    list_display = ('user', 'plan_handle', 'provider_id', 'provider_state', 'outcome', 'claimed_at')
    list_filter = ('outcome', 'plan_handle')
    search_fields = ('reference', 'user__email')
