from django.urls import path

from . import views

app_name = "paypal_api"

urlpatterns = [
    path("orders", views.orders_collection, name="orders"),
    path("orders/<str:number>/pay", views.order_pay, name="order-pay"),
    path("orders/<str:number>/fulfil", views.order_fulfil, name="order-fulfil"),
    path("orders/<str:number>/cancel", views.order_cancel, name="order-cancel"),
    path("orders/<str:number>/refunds", views.order_refunds, name="order-refunds"),
    path("my-orders", views.my_orders, name="my-orders"),
    path("payment-methods", views.payment_methods, name="payment-methods"),
    path("payment-methods/<str:payment_method_id>", views.payment_method_detail, name="payment-method-detail"),
    path("reconciliation", views.reconciliation_report, name="reconciliation"),
]
