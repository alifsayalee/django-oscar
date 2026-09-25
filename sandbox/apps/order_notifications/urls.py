from django.urls import path

from . import views

app_name = 'order_notifications'

urlpatterns = [
    path('auth/session', views.session, name='session'),
    path('auth/login', views.session_login, name='login'),
    path('auth/logout', views.session_logout, name='logout'),

    path('contact-numbers', views.contact_numbers, name='contact-numbers'),
    path('contact-numbers/<int:contact_number_id>', views.contact_number_detail,
         name='contact-number-detail'),

    path('orders', views.order_create, name='order-create'),
    path('orders/<int:order_id>/dispatch', views.order_dispatch, name='order-dispatch'),
    path('orders/<int:order_id>/cancel', views.order_cancel, name='order-cancel'),
    path('orders/<int:order_id>/notifications', views.order_notifications,
         name='order-notifications'),
    path('my-orders', views.my_orders, name='my-orders'),

    path('notifications/reconciliation', views.notification_reconciliation,
         name='notification-reconciliation'),
    path('notifications/<int:notification_id>/resend', views.notification_resend,
         name='notification-resend'),
    path('notifications/<int:notification_id>/content', views.notification_content,
         name='notification-content'),
]
