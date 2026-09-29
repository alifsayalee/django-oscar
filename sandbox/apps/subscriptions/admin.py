from django.contrib import admin

from .models import BillingClaim


@admin.register(BillingClaim)
class BillingClaimAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ('key', 'kind', 'user', 'plan_handle', 'outcome', 'provider_id', 'claimed_at')
    list_filter = ('kind', 'outcome')
    search_fields = ('key', 'reference', 'provider_id', 'user__email')
    readonly_fields = ('key', 'reference', 'kind', 'user', 'plan_handle', 'provider_id',
                       'provider_time', 'snapshot', 'claimed_at', 'updated_at')
