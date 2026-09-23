"""URL routes for the checkout API, mounted under /api/ by the sandbox urlconf."""

from django.urls import path

from . import views

app_name = "api"

urlpatterns = [
    # Session login/logout (Django session auth, JSON-driven)
    path("session", views.session, name="session"),
    # Flow 1 -- orders and payments
    path("orders", views.create_order, name="create-order"),
    path("orders/<str:order_number>/pay", views.pay_order, name="pay-order"),
    path("orders/<str:order_number>/fulfil", views.fulfil_order, name="fulfil-order"),
    path("orders/<str:order_number>/cancel", views.cancel_order, name="cancel-order"),
    path("orders/<str:order_number>/refunds", views.refund_order, name="refund-order"),
    path("my-orders", views.my_orders, name="my-orders"),
    path("reconciliation", views.reconciliation, name="reconciliation"),
    # Flow 2 -- saved cards
    path("payment-methods", views.payment_methods, name="payment-methods"),
    path(
        "payment-methods/<int:payment_method_id>",
        views.payment_method_detail,
        name="payment-method-detail",
    ),
]
