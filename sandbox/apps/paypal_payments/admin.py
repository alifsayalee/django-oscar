from django.contrib import admin

from .models import PayPalCustomer, PayPalPayment, PayPalRefund


class PayPalRefundInline(admin.TabularInline):
    model = PayPalRefund
    extra = 0
    readonly_fields = (
        "idempotency_key",
        "paypal_refund_id",
        "paypal_status",
        "amount",
        "status",
        "last_error",
        "date_created",
    )
    exclude = ("paypal_request_id",)


@admin.register(PayPalPayment)
class PayPalPaymentAdmin(admin.ModelAdmin):
    list_display = (
        "order",
        "status",
        "amount",
        "currency",
        "authorization_id",
        "capture_id",
        "date_updated",
    )
    list_filter = ("status",)
    search_fields = (
        "order__number",
        "paypal_order_id",
        "authorization_id",
        "capture_id",
    )
    inlines = [PayPalRefundInline]


@admin.register(PayPalCustomer)
class PayPalCustomerAdmin(admin.ModelAdmin):
    list_display = ("user", "paypal_customer_id", "date_created")
