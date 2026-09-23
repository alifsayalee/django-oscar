from django.urls import path

from . import views

app_name = 'paypal_payments'

urlpatterns = [
    path('orders', views.orders, name='orders'),
    path('orders/<int:order_id>/pay', views.pay, name='order-pay'),
    path('orders/<int:order_id>/fulfil', views.fulfil, name='order-fulfil'),
    path('orders/<int:order_id>/cancel', views.cancel, name='order-cancel'),
    path('orders/<int:order_id>/refunds', views.refunds, name='order-refunds'),
    path('my-orders', views.my_orders, name='my-orders'),
    path('payment-methods', views.payment_methods, name='payment-methods'),
    path('payment-methods/<int:payment_method_id>', views.payment_method_detail,
         name='payment-method-detail'),
    path('reconciliation', views.reconciliation, name='reconciliation'),
]
