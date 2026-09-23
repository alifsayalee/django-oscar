from django.urls import path

from . import views

app_name = "paypal_checkout"

urlpatterns = [
    # Flow 1 — pay for an order
    path("orders", views.create_order, name="create-order"),
    path("orders/<str:order_id>/pay", views.pay_order, name="pay-order"),
    path("orders/<str:order_id>/fulfil", views.fulfil_order, name="fulfil-order"),
    path("orders/<str:order_id>/cancel", views.cancel_order, name="cancel-order"),
    path("orders/<str:order_id>/refunds", views.create_refund, name="create-refund"),
    path("my-orders", views.my_orders, name="my-orders"),
    path("reconciliation", views.reconciliation, name="reconciliation"),

    # Flow 2 — saved cards
    path("payment-methods", views.payment_methods, name="payment-methods"),
    path("payment-methods/<int:payment_method_id>", views.delete_payment_method,
         name="delete-payment-method"),
]
