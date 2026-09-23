from django.urls import path

from . import views

app_name = "paypal_payments"

urlpatterns = [
    path("session", views.session, name="session"),
    path("orders", views.orders, name="orders"),
    path("orders/<str:order_id>/pay", views.pay, name="order-pay"),
    path("orders/<str:order_id>/fulfil", views.fulfil, name="order-fulfil"),
    path("orders/<str:order_id>/cancel", views.cancel, name="order-cancel"),
    path("orders/<str:order_id>/refunds", views.refunds, name="order-refunds"),
    path("my-orders", views.my_orders, name="my-orders"),
    path("reconciliation", views.reconciliation, name="reconciliation"),
    path("payment-methods", views.payment_methods, name="payment-methods"),
    path("payment-methods/<str:payment_method_id>", views.payment_method, name="payment-method"),
]
