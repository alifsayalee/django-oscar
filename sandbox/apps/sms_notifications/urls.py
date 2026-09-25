from django.urls import path

from . import views

urlpatterns = [
    path('contact-numbers', views.contact_numbers, name='sms-contact-numbers'),
    path('contact-numbers/<int:contact_number_id>', views.contact_number_detail, name='sms-contact-number'),
    path('orders', views.orders, name='sms-orders'),
    path('orders/<int:order_id>/dispatch', views.dispatch, name='sms-order-dispatch'),
    path('orders/<int:order_id>/cancel', views.cancel, name='sms-order-cancel'),
    path('orders/<int:order_id>/notifications', views.order_notification_list, name='sms-order-notifications'),
    path('my-orders', views.my_orders, name='sms-my-orders'),
    path('notifications/reconciliation', views.reconciliation, name='sms-reconciliation'),
    path('notifications/<int:notification_id>/resend', views.resend, name='sms-notification-resend'),
    path('notifications/<int:notification_id>/content', views.notification_content,
         name='sms-notification-content'),
]
