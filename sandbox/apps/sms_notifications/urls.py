from django.urls import path

from . import views

app_name = 'sms_notifications'

urlpatterns = [
    path('contact-numbers', views.contact_numbers, name='contact-numbers'),
    path('contact-numbers/<int:contact_number_id>', views.contact_number_detail,
         name='contact-number-detail'),
    path('orders', views.orders, name='orders'),
    path('orders/<int:order_id>/dispatch', views.dispatch_order, name='order-dispatch'),
    path('orders/<int:order_id>/cancel', views.cancel_order, name='order-cancel'),
    path('orders/<int:order_id>/notifications', views.order_notifications,
         name='order-notifications'),
    path('my-orders', views.my_orders, name='my-orders'),
    path('notifications/reconciliation', views.reconciliation, name='reconciliation'),
    path('notifications/<int:notification_id>/resend', views.resend_notification,
         name='notification-resend'),
    path('notifications/<int:notification_id>/content', views.notification_content,
         name='notification-content'),
]
