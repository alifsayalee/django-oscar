from django.urls import path

from . import views

app_name = "payments-api"

urlpatterns = [
    path("csrf", views.csrf, name="csrf"),
    path("session", views.session, name="session"),
    path("orders", views.orders, name="orders"),
    path("orders/<str:number>/pay", views.pay, name="pay"),
    path("orders/<str:number>/fulfil", views.fulfil, name="fulfil"),
    path("orders/<str:number>/cancel", views.cancel, name="cancel"),
    path("orders/<str:number>/refunds", views.refunds, name="refunds"),
    path("my-orders", views.my_orders, name="my-orders"),
    path("reconciliation", views.reconciliation, name="reconciliation"),
    path("payment-methods", views.payment_methods, name="payment-methods"),
    path("payment-methods/<str:payment_method_id>", views.payment_method, name="payment-method"),
]
