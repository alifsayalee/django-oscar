from django.contrib import admin

from .models import OrderPayment, PaymentRefund, PayPalCustomer, SavedCard


@admin.register(OrderPayment)
class OrderPaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "user", "status", "currency", "amount",
                    "captured_value", "paypal_fee", "net_amount", "created")
    list_filter = ("status", "currency")
    search_fields = ("invoice_id", "paypal_order_id", "authorization_id", "capture_id",
                     "order__number")
    readonly_fields = [f.name for f in OrderPayment._meta.fields]


@admin.register(PaymentRefund)
class PaymentRefundAdmin(admin.ModelAdmin):
    list_display = ("id", "order_payment", "amount", "status", "paypal_refund_id", "created")
    search_fields = ("idempotency_key", "paypal_refund_id")
    readonly_fields = [f.name for f in PaymentRefund._meta.fields]


@admin.register(SavedCard)
class SavedCardAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "brand", "last_digits", "expiry", "created")
    search_fields = ("paypal_token_id", "user__username")
    readonly_fields = [f.name for f in SavedCard._meta.fields]


@admin.register(PayPalCustomer)
class PayPalCustomerAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "customer_id", "created")
    search_fields = ("customer_id", "user__username")
    readonly_fields = [f.name for f in PayPalCustomer._meta.fields]
