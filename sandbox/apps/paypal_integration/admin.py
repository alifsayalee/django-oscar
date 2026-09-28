from django.contrib import admin

from .models import PayPalCustomer, PayPalPayment, ProviderWrite


@admin.register(PayPalPayment)
class PayPalPaymentAdmin(admin.ModelAdmin):
    list_display = ('source', 'state', 'authorization_id', 'capture_id', 'date_updated')
    list_filter = ('state',)
    search_fields = ('paypal_order_id', 'authorization_id', 'capture_id', 'source__order__number')


@admin.register(ProviderWrite)
class ProviderWriteAdmin(admin.ModelAdmin):
    list_display = ('ref', 'kind', 'outcome', 'provider_id', 'provider_status', 'amount', 'claimed_at')
    list_filter = ('kind', 'outcome')
    search_fields = ('ref', 'provider_id', 'order__number')
    readonly_fields = [f.name for f in ProviderWrite._meta.fields]


admin.site.register(PayPalCustomer)
