from django.contrib import admin

from .models import BillingCustomer, SubscriptionEnrollment


@admin.register(BillingCustomer)
class BillingCustomerAdmin(admin.ModelAdmin):
    list_display = ("user", "maxio_customer_id", "reference", "status", "date_updated")
    list_filter = ("status",)
    search_fields = ("reference", "user__email")
    readonly_fields = ("date_created", "date_updated")


@admin.register(SubscriptionEnrollment)
class SubscriptionEnrollmentAdmin(admin.ModelAdmin):
    list_display = ("user", "plan_handle", "maxio_subscription_id", "maxio_state", "status", "date_created")
    list_filter = ("status", "plan_handle")
    search_fields = ("reference", "user__email")
    readonly_fields = ("date_created", "date_updated")
