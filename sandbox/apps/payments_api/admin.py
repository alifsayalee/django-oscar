from django.contrib import admin

from .models import PayPalCustomer, PayPalPayment, PayPalRefund, SavedPaymentMethod


@admin.register(PayPalPayment)
class PayPalPaymentAdmin(admin.ModelAdmin):
    list_display = ("order", "user", "status", "currency", "order_total", "captured_amount", "refunded_amount")
    search_fields = ("order__number", "paypal_order_id", "authorization_id", "capture_id")
    list_filter = ("status", "currency")


@admin.register(PayPalRefund)
class PayPalRefundAdmin(admin.ModelAdmin):
    list_display = ("payment", "refund_id", "amount", "status", "idempotency_key")


@admin.register(SavedPaymentMethod)
class SavedPaymentMethodAdmin(admin.ModelAdmin):
    list_display = ("user", "brand", "last_digits", "expiry", "payment_token_id")


@admin.register(PayPalCustomer)
class PayPalCustomerAdmin(admin.ModelAdmin):
    list_display = ("user", "customer_id")
