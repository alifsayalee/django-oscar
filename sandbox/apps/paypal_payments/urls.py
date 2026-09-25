from django.urls import path

from . import views

app_name = 'paypal_payments'

urlpatterns = [
    path('csrf', views.csrf, name='csrf'),
    path('session', views.session, name='session'),
    path('orders', views.order_create, name='orders'),
    path('orders/<str:order_id>/pay', views.order_pay, name='order-pay'),
    path('orders/<str:order_id>/fulfil', views.order_fulfil, name='order-fulfil'),
    path('orders/<str:order_id>/cancel', views.order_cancel, name='order-cancel'),
    path('orders/<str:order_id>/refunds', views.order_refunds, name='order-refunds'),
    path('my-orders', views.my_orders, name='my-orders'),
    path('reconciliation', views.reconciliation_report, name='reconciliation'),
    path('payment-methods', views.payment_methods, name='payment-methods'),
    path('payment-methods/<int:payment_method_id>', views.payment_method_detail, name='payment-method'),
]
