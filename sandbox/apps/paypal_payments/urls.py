from django.urls import path

from . import views

app_name = 'paypal_payments'

urlpatterns = [
    path('auth/csrf', views.csrf, name='csrf'),
    path('auth/login', views.login_view, name='login'),
    path('auth/register', views.register_view, name='register'),
    path('auth/logout', views.logout_view, name='logout'),

    path('orders', views.orders, name='orders'),
    path('orders/<str:order_id>/pay', views.pay, name='pay'),
    path('orders/<str:order_id>/fulfil', views.fulfil, name='fulfil'),
    path('orders/<str:order_id>/cancel', views.cancel, name='cancel'),
    path('orders/<str:order_id>/refunds', views.refunds, name='refunds'),
    path('my-orders', views.my_orders, name='my-orders'),
    path('reconciliation', views.reconciliation, name='reconciliation'),

    path('payment-methods', views.payment_methods, name='payment-methods'),
    path('payment-methods/<str:payment_method_id>', views.payment_method, name='payment-method'),
]
