from django.urls import path

from . import views

app_name = "shop_payments"

urlpatterns = [
    path("csrf", views.csrf, name="csrf"),
    path("login", views.login_view, name="login"),
    path("logout", views.logout_view, name="logout"),
    path("me", views.me, name="me"),
    path("orders", views.create_order, name="orders"),
    path("orders/<str:order_id>/pay", views.pay, name="order-pay"),
    path("orders/<str:order_id>/fulfil", views.fulfil, name="order-fulfil"),
    path("orders/<str:order_id>/cancel", views.cancel, name="order-cancel"),
    path("orders/<str:order_id>/refunds", views.refunds, name="order-refunds"),
    path("my-orders", views.my_orders, name="my-orders"),
    path("payment-methods", views.payment_methods, name="payment-methods"),
    path("payment-methods/<str:payment_method_id>", views.payment_method, name="payment-method"),
    path("reconciliation", views.reconciliation_report, name="reconciliation"),
]
