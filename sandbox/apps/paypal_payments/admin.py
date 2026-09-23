from django.contrib import admin

from .models import PayPalCustomer, PayPalPayment, PayPalRefund


class PayPalRefundInline(admin.TabularInline):
    model = PayPalRefund
    extra = 0
    can_delete = False
    readonly_fields = ('idempotency_key', 'amount', 'currency', 'status', 'paypal_refund_id',
                       'error_code', 'error_message', 'requested_by', 'date_created')


@admin.register(PayPalPayment)
class PayPalPaymentAdmin(admin.ModelAdmin):
    list_display = ('order', 'state', 'amount', 'currency', 'authorization_id', 'capture_id',
                    'date_updated')
    list_filter = ('state',)
    search_fields = ('order__number', 'paypal_order_id', 'authorization_id', 'capture_id')
    readonly_fields = [f.name for f in PayPalPayment._meta.fields]
    inlines = [PayPalRefundInline]


@admin.register(PayPalCustomer)
class PayPalCustomerAdmin(admin.ModelAdmin):
    list_display = ('user', 'paypal_customer_id', 'date_created')
    readonly_fields = ('user', 'paypal_customer_id', 'date_created')
