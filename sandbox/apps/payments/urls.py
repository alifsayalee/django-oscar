from django.urls import path

from . import views

app_name = "payments_api"

urlpatterns = [
    path("auth/csrf", views.csrf, name="csrf"),
    path("auth/login", views.login_view, name="login"),
    path("auth/logout", views.logout_view, name="logout"),
    path("orders", views.orders, name="orders"),
    path("orders/<str:order_id>", views.order_detail, name="order-detail"),
    path("orders/<str:order_id>/pay", views.pay, name="order-pay"),
    path("orders/<str:order_id>/fulfil", views.fulfil, name="order-fulfil"),
    path("orders/<str:order_id>/cancel", views.cancel, name="order-cancel"),
    path("orders/<str:order_id>/refunds", views.refunds, name="order-refunds"),
    path("my-orders", views.my_orders, name="my-orders"),
    path("payment-methods", views.payment_methods, name="payment-methods"),
    path("payment-methods/<str:payment_method_id>", views.payment_method_detail, name="payment-method-detail"),
    path("reconciliation", views.reconciliation_report, name="reconciliation"),
]
