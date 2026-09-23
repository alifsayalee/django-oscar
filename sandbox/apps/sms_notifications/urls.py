from django.urls import path

from . import views

app_name = "sms_notifications"

urlpatterns = [
    path("csrf", views.csrf, name="csrf"),
    path("session", views.session, name="session"),
    path("contact-numbers", views.contact_numbers, name="contact-numbers"),
    path("contact-numbers/<int:contact_number_id>", views.contact_number_detail, name="contact-number"),
    path("orders", views.orders, name="orders"),
    path("orders/<int:order_id>/dispatch", views.order_dispatch, name="order-dispatch"),
    path("orders/<int:order_id>/cancel", views.order_cancel, name="order-cancel"),
    path("orders/<int:order_id>/notifications", views.order_notifications, name="order-notifications"),
    path("my-orders", views.my_orders, name="my-orders"),
    path("notifications/reconciliation", views.reconciliation, name="reconciliation"),
    path("notifications/<int:notification_id>/resend", views.notification_resend, name="notification-resend"),
    path("notifications/<int:notification_id>/content", views.notification_content, name="notification-content"),
]
