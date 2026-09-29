from django.contrib import admin

from .models import BillingCustomer, Subscription


@admin.register(BillingCustomer)
class BillingCustomerAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("user", "reference", "maxio_customer_id", "status", "outcome_unknown")
    search_fields = ("reference", "user__email")
    readonly_fields = ("date_created", "date_updated")


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "user", "plan_handle", "maxio_subscription_id", "state", "claim_state",
        "is_live", "next_billing_at",
    )
    list_filter = ("claim_state", "is_live", "state", "product_family")
    search_fields = ("plan_handle", "user__email")
    readonly_fields = ("date_created", "date_updated", "last_synced_at")
