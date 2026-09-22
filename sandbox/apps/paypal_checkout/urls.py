"""URL routes for the PayPal checkout API, mounted under ``/api/``.

Each capability is a separately-invocable route; there is no do-everything call.
"""
from django.urls import path

from . import views

app_name = "paypal_checkout"

urlpatterns = [
    # Flow 1 -- orders & payments
    path("orders", views.create_order, name="create-order"),
    path("orders/<str:order_number>/pay", views.pay_order, name="pay-order"),
    path("orders/<str:order_number>/fulfil", views.fulfil_order, name="fulfil-order"),
    path("orders/<str:order_number>/cancel", views.cancel_order, name="cancel-order"),
    path("orders/<str:order_number>/refunds", views.create_refund, name="create-refund"),
    path("my-orders", views.my_orders, name="my-orders"),
    path("reconciliation", views.reconciliation, name="reconciliation"),
    # Flow 2 -- saved cards
    path("payment-methods", views.payment_methods, name="payment-methods"),
    path("payment-methods/<int:method_id>", views.delete_payment_method, name="delete-payment-method"),
]
