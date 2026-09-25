from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from .models import BillingCustomer, SubscriptionEnrollment


class ReadOnlyAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """These rows mirror requests made to Maxio; operators inspect them, they do not edit them."""

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(BillingCustomer)
class BillingCustomerAdmin(ReadOnlyAdmin):
    list_display = ('user', 'reference', 'maxio_customer_id', 'outcome', 'updated_at')
    list_filter = ('outcome',)
    search_fields = ('reference', 'user__email')


@admin.register(SubscriptionEnrollment)
class SubscriptionEnrollmentAdmin(ReadOnlyAdmin):
    list_display = ('user', 'plan_handle', 'reference', 'maxio_subscription_id', 'provider_state', 'outcome',
                    'updated_at')
    list_filter = ('outcome', 'plan_handle')
    search_fields = ('reference', 'user__email')
