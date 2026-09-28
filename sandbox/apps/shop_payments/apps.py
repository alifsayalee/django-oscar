from django.apps import AppConfig


class ShopPaymentsConfig(AppConfig):
    name = "apps.shop_payments"
    label = "shop_payments"
    verbose_name = "Shop payments (PayPal)"
    default_auto_field = "django.db.models.AutoField"
