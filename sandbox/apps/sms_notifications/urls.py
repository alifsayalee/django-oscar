from django.urls import path

from . import views

app_name = "sms_notifications"

urlpatterns = [
    path("contact-numbers", views.contact_numbers_view, name="contact-numbers"),
    path("contact-numbers/<int:contact_number_id>", views.contact_number_view, name="contact-number"),
    path("orders", views.orders_view, name="orders"),
    path("my-orders", views.my_orders_view, name="my-orders"),
    path("orders/<int:order_id>/dispatch", views.dispatch_view, name="order-dispatch"),
    path("orders/<int:order_id>/cancel", views.cancel_view, name="order-cancel"),
    path("orders/<int:order_id>/notifications", views.order_notifications_view, name="order-notifications"),
    path("notifications/reconciliation", views.reconciliation_view, name="reconciliation"),
    path("notifications/<int:notification_id>/resend", views.resend_view, name="notification-resend"),
    path("notifications/<int:notification_id>/content", views.content_view, name="notification-content"),
]
