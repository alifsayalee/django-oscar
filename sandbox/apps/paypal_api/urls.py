"""URL routing for the PayPal API, mounted under /api/ (see sandbox/urls.py)."""
from django.urls import path

from . import views

app_name = "paypal_api"

urlpatterns = [
    # Session bootstrap (Django session login)
    path("session", views.session_view, name="session"),
    path("session/login", views.login_view, name="login"),
    path("session/logout", views.logout_view, name="logout"),

    # Flow 1 — orders & payments
    path("orders", views.orders_view, name="orders"),
    path("orders/<int:order_id>/pay", views.order_pay_view, name="order-pay"),
    path("orders/<int:order_id>/fulfil", views.order_fulfil_view, name="order-fulfil"),
    path("orders/<int:order_id>/cancel", views.order_cancel_view, name="order-cancel"),
    path("orders/<int:order_id>/refunds", views.order_refunds_view, name="order-refunds"),
    path("my-orders", views.my_orders_view, name="my-orders"),
    path("reconciliation", views.reconciliation_view, name="reconciliation"),

    # Flow 2 — saved cards
    path("payment-methods", views.payment_methods_view, name="payment-methods"),
    path(
        "payment-methods/<int:payment_method_id>",
        views.payment_method_detail_view,
        name="payment-method-detail",
    ),
]
