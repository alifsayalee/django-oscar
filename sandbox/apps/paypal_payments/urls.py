from django.urls import path

from . import views

app_name = "paypal_payments"

urlpatterns = [
    path("csrf", views.csrf, name="csrf"),
    path("login", views.session_login, name="login"),
    path("logout", views.session_logout, name="logout"),
    path("orders", views.create_order, name="orders"),
    path("orders/<str:order_number>/pay", views.pay_order, name="order-pay"),
    path("orders/<str:order_number>/fulfil", views.fulfil_order, name="order-fulfil"),
    path("orders/<str:order_number>/cancel", views.cancel_order, name="order-cancel"),
    path("orders/<str:order_number>/refunds", views.refund_order, name="order-refunds"),
    path("my-orders", views.my_orders, name="my-orders"),
    path("payment-methods", views.payment_methods, name="payment-methods"),
    path("payment-methods/<str:payment_method_id>", views.payment_method, name="payment-method"),
    path("reconciliation", views.reconciliation, name="reconciliation"),
]
