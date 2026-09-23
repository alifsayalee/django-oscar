from django.urls import path

from . import views

app_name = "payments_api"

urlpatterns = [
    path("csrf", views.csrf, name="csrf"),
    path("login", views.login_view, name="login"),
    path("logout", views.logout_view, name="logout"),
    path("products", views.products, name="products"),
    path("orders", views.orders, name="orders"),
    path("orders/<str:order_id>/pay", views.pay, name="order-pay"),
    path("orders/<str:order_id>/fulfil", views.fulfil, name="order-fulfil"),
    path("orders/<str:order_id>/cancel", views.cancel, name="order-cancel"),
    path("orders/<str:order_id>/refunds", views.refunds, name="order-refunds"),
    path("my-orders", views.my_orders, name="my-orders"),
    path("payment-methods", views.payment_methods, name="payment-methods"),
    path("payment-methods/<str:payment_method_id>", views.payment_method_detail, name="payment-method-detail"),
    path("reconciliation", views.reconciliation_report, name="reconciliation"),
]
