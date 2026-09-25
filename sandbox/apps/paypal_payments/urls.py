from django.urls import path

from . import views

app_name = "paypal_payments"

urlpatterns = [
    path("csrf", views.csrf, name="csrf"),
    path("login", views.login_view, name="login"),
    path("logout", views.logout_view, name="logout"),
    path("orders", views.orders, name="orders"),
    path("orders/<str:number>/pay", views.pay, name="pay"),
    path("orders/<str:number>/fulfil", views.fulfil, name="fulfil"),
    path("orders/<str:number>/cancel", views.cancel, name="cancel"),
    path("orders/<str:number>/refunds", views.refunds, name="refunds"),
    path("my-orders", views.my_orders, name="my-orders"),
    path("reconciliation", views.reconciliation_view, name="reconciliation"),
    path("payment-methods", views.payment_methods, name="payment-methods"),
    path("payment-methods/<str:method_id>", views.payment_method_detail, name="payment-method-detail"),
]
