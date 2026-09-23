from django.contrib import admin

from apps.subscriptions.models import BillingCustomer, SubscriptionEnrollment


@admin.register(BillingCustomer)
class BillingCustomerAdmin(admin.ModelAdmin):
    list_display = ("reference", "user", "maxio_customer_id", "status", "date_updated")
    list_filter = ("status",)
    search_fields = ("reference", "user__email")
    readonly_fields = ("date_created", "date_updated")


@admin.register(SubscriptionEnrollment)
class SubscriptionEnrollmentAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "user",
        "plan_handle",
        "maxio_subscription_id",
        "status",
        "maxio_state",
        "date_updated",
    )
    # 'unknown' and 'needs_review' rows are the ones an operator must resolve.
    list_filter = ("status", "plan_handle")
    search_fields = ("reference", "user__email", "plan_handle")
    readonly_fields = ("date_created", "date_updated")
