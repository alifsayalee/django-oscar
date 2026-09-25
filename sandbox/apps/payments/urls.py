from django.urls import path

from . import views

app_name = "payments_api"

urlpatterns = [
    path("login", views.login_view, name="login"),
    path("logout", views.logout_view, name="logout"),
    path("orders", views.orders_view, name="orders"),
    path("orders/<str:order_id>/pay", views.pay_view, name="order-pay"),
    path("orders/<str:order_id>/fulfil", views.fulfil_view, name="order-fulfil"),
    path("orders/<str:order_id>/cancel", views.cancel_view, name="order-cancel"),
    path("orders/<str:order_id>/refunds", views.refunds_view, name="order-refunds"),
    path("my-orders", views.my_orders_view, name="my-orders"),
    path("reconciliation", views.reconciliation_view, name="reconciliation"),
    path("payment-methods", views.payment_methods_view, name="payment-methods"),
    path("payment-methods/<int:payment_method_id>", views.payment_method_view, name="payment-method"),
]
