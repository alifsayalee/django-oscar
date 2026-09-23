from django.urls import path

from . import views

app_name = "payments_api"

urlpatterns = [
    path("orders", views.create_order, name="create-order"),
    path("orders/<int:order_id>/pay", views.pay_order, name="pay-order"),
    path("orders/<int:order_id>/fulfil", views.fulfil_order, name="fulfil-order"),
    path("orders/<int:order_id>/cancel", views.cancel_order, name="cancel-order"),
    path("orders/<int:order_id>/refunds", views.refund_order, name="refund-order"),
    path("my-orders", views.my_orders, name="my-orders"),
    path("reconciliation", views.reconciliation, name="reconciliation"),
    path("payment-methods", views.payment_methods, name="payment-methods"),
    path("payment-methods/<int:payment_method_id>", views.payment_method_detail,
         name="payment-method-detail"),
]
