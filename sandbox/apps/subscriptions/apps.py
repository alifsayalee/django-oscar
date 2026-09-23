from django.urls import path
from django.utils.translation import gettext_lazy as _

from oscar.core.application import OscarConfig


class SubscriptionsConfig(OscarConfig):
    """
    Recurring-subscription billing, with Maxio Advanced Billing as the billing
    system of record. Runs alongside (not instead of) the basket/checkout flow.
    """

    label = "subscriptions"
    name = "apps.subscriptions"
    verbose_name = _("Subscriptions")
    default_auto_field = "django.db.models.BigAutoField"

    namespace = "subscriptions"

    def get_urls(self):
        from apps.subscriptions import views

        urls = [
            path("subscription-plans", views.subscription_plans, name="plans"),
            path("billing-customer", views.billing_customer, name="billing-customer"),
            path("subscriptions", views.subscriptions, name="subscriptions"),
            path(
                "subscriptions/<int:subscription_id>",
                views.subscription_detail,
                name="subscription-detail",
            ),
            path("my-subscriptions", views.my_subscriptions, name="my-subscriptions"),
        ]
        return self.post_process_urls(urls)
