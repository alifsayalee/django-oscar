from django.urls import path

from . import views

app_name = "paypal_api"

urlpatterns = [
    path("orders", views.OrdersView.as_view(), name="orders"),
    path("orders/<int:order_id>/pay", views.PayView.as_view(), name="order-pay"),
    path("orders/<int:order_id>/fulfil", views.FulfilView.as_view(), name="order-fulfil"),
    path("orders/<int:order_id>/cancel", views.CancelView.as_view(), name="order-cancel"),
    path("orders/<int:order_id>/refunds", views.RefundsView.as_view(), name="order-refunds"),
    path("my-orders", views.MyOrdersView.as_view(), name="my-orders"),
    path("reconciliation", views.ReconciliationView.as_view(), name="reconciliation"),
    path("payment-methods", views.PaymentMethodsView.as_view(), name="payment-methods"),
    path(
        "payment-methods/<int:method_id>",
        views.PaymentMethodDetailView.as_view(),
        name="payment-method-detail",
    ),
]
