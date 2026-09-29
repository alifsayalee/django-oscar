from django.contrib import admin

from .models import OrderPayment, PaymentRefund, PayPalCustomer, SavedCard


class ReadOnlyAdmin(admin.ModelAdmin):
    """PayPal state is written by the payment flows only."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OrderPayment)
class OrderPaymentAdmin(ReadOnlyAdmin):
    list_display = ('reference', 'order', 'state', 'amount', 'currency', 'authorization_id', 'capture_id',
                    'refunded_amount', 'updated')
    list_filter = ('state',)
    search_fields = ('reference', 'order__number', 'authorization_id', 'capture_id', 'paypal_order_id')


@admin.register(PaymentRefund)
class PaymentRefundAdmin(ReadOnlyAdmin):
    list_display = ('payment', 'amount', 'state', 'paypal_refund_id', 'idempotency_key', 'created')
    list_filter = ('state',)


@admin.register(SavedCard)
class SavedCardAdmin(ReadOnlyAdmin):
    list_display = ('user', 'brand', 'last_digits', 'expiry', 'state', 'created')
    list_filter = ('state',)


@admin.register(PayPalCustomer)
class PayPalCustomerAdmin(ReadOnlyAdmin):
    list_display = ('user', 'customer_id', 'created')
