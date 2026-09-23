from django.urls import path

from . import views

app_name = 'paypal_payments'

urlpatterns = [
    path('auth/csrf', views.csrf, name='csrf'),
    path('auth/login', views.session_login, name='login'),
    path('auth/logout', views.session_logout, name='logout'),

    path('orders', views.orders, name='orders'),
    path('orders/<int:order_id>/pay', views.pay, name='pay'),
    path('orders/<int:order_id>/fulfil', views.fulfil, name='fulfil'),
    path('orders/<int:order_id>/cancel', views.cancel, name='cancel'),
    path('orders/<int:order_id>/refunds', views.refunds, name='refunds'),
    path('my-orders', views.my_orders, name='my-orders'),
    path('reconciliation', views.reconciliation, name='reconciliation'),

    path('payment-methods', views.payment_methods, name='payment-methods'),
    path('payment-methods/<int:payment_method_id>', views.payment_method, name='payment-method'),
]
