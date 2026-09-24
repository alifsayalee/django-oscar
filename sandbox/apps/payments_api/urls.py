from django.urls import path

from . import views

app_name = 'payments_api'

urlpatterns = [
    path('auth/csrf', views.csrf, name='csrf'),
    path('auth/login', views.session_login, name='login'),
    path('auth/logout', views.session_logout, name='logout'),

    path('orders', views.orders, name='orders'),
    path('orders/<str:order_number>', views.order_detail, name='order-detail'),
    path('orders/<str:order_number>/pay', views.pay, name='order-pay'),
    path('orders/<str:order_number>/fulfil', views.fulfil, name='order-fulfil'),
    path('orders/<str:order_number>/cancel', views.cancel, name='order-cancel'),
    path('orders/<str:order_number>/refunds', views.refunds, name='order-refunds'),
    path('my-orders', views.my_orders, name='my-orders'),
    path('reconciliation', views.reconciliation, name='reconciliation'),

    path('payment-methods', views.payment_methods, name='payment-methods'),
    path('payment-methods/<str:payment_method_id>', views.payment_method_detail,
         name='payment-method-detail'),
]
