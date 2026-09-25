from django.urls import URLPattern, path
from django.utils.translation import gettext_lazy as _

from oscar.core.application import OscarConfig


class SubscriptionsConfig(OscarConfig):  # type: ignore[misc]  # Oscar ships no type hints
    """
    Recurring subscriptions billed by Maxio Advanced Billing, exposed as a JSON API.

    Additive to Oscar's one-time basket/checkout flow: Maxio is the system of record for plans and
    subscriptions, and the only local state is the ledger of writes this site asked Maxio to make.
    """

    label = "subscriptions"
    name = "apps.subscriptions"
    verbose_name = _("Subscriptions")
    default_auto_field = "django.db.models.BigAutoField"

    namespace = "subscriptions-api"

    def get_urls(self) -> list[URLPattern]:
        from apps.subscriptions import views

        urls = [
            path("session", views.SessionView.as_view(), name="session"),
            path("subscription-plans", views.SubscriptionPlansView.as_view(), name="plans"),
            path("billing-customer", views.BillingCustomerView.as_view(), name="billing-customer"),
            path("subscriptions", views.SubscriptionsView.as_view(), name="subscriptions"),
            path("my-subscriptions", views.MySubscriptionsView.as_view(), name="my-subscriptions"),
        ]
        processed: list[URLPattern] = self.post_process_urls(urls)
        return processed
