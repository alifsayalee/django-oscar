"""URL routes for the PayPal payments API, mounted under ``/api/``."""
from django.urls import path

from . import views

app_name = "payments_api"

urlpatterns = [
    # Orders
    path("orders", views.PlaceOrderView.as_view(), name="place-order"),
    path("my-orders", views.MyOrdersView.as_view(), name="my-orders"),
    path("orders/<str:order_number>/pay", views.PayView.as_view(), name="pay"),
    path(
        "orders/<str:order_number>/fulfil",
        views.FulfilView.as_view(),
        name="fulfil",
    ),
    path(
        "orders/<str:order_number>/cancel",
        views.CancelView.as_view(),
        name="cancel",
    ),
    path(
        "orders/<str:order_number>/refunds",
        views.RefundView.as_view(),
        name="refunds",
    ),
    # Reconciliation (operator)
    path(
        "reconciliation", views.ReconciliationView.as_view(), name="reconciliation"
    ),
    # Saved cards
    path(
        "payment-methods",
        views.PaymentMethodsView.as_view(),
        name="payment-methods",
    ),
    path(
        "payment-methods/<int:pk>",
        views.PaymentMethodDetailView.as_view(),
        name="payment-method-detail",
    ),
]
