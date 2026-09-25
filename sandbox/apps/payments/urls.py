from django.urls import path

from . import views

app_name = 'payments-api'

urlpatterns = [
    path('auth/csrf', views.csrf, name='csrf'),
    path('auth/login', views.session_login, name='login'),
    path('auth/logout', views.session_logout, name='logout'),
    path('orders', views.orders, name='orders'),
    path('orders/<str:number>/pay', views.pay, name='pay'),
    path('orders/<str:number>/fulfil', views.fulfil, name='fulfil'),
    path('orders/<str:number>/cancel', views.cancel, name='cancel'),
    path('orders/<str:number>/refunds', views.refunds, name='refunds'),
    path('my-orders', views.my_orders, name='my-orders'),
    path('reconciliation', views.reconciliation_report, name='reconciliation'),
    path('payment-methods', views.payment_methods, name='payment-methods'),
    path('payment-methods/<str:public_id>', views.payment_method, name='payment-method'),
]
