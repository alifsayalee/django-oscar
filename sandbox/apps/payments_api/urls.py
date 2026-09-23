from django.urls import path

from . import views

app_name = "payments_api"

urlpatterns = [
    # Orders
    path("orders", views.OrdersView.as_view(), name="orders"),
    path("orders/<str:order_number>/pay", views.OrderPayView.as_view(), name="order-pay"),
    path("orders/<str:order_number>/fulfil", views.OrderFulfilView.as_view(), name="order-fulfil"),
    path("orders/<str:order_number>/cancel", views.OrderCancelView.as_view(), name="order-cancel"),
    path("orders/<str:order_number>/refunds", views.OrderRefundsView.as_view(), name="order-refunds"),
    path("my-orders", views.MyOrdersView.as_view(), name="my-orders"),
    # Saved cards
    path("payment-methods", views.PaymentMethodsView.as_view(), name="payment-methods"),
    path(
        "payment-methods/<str:payment_method_id>",
        views.PaymentMethodDetailView.as_view(),
        name="payment-method-detail",
    ),
    # Reconciliation (operator)
    path("reconciliation", views.ReconciliationView.as_view(), name="reconciliation"),
]
