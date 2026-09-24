from django.urls import path

from . import views

urlpatterns = [
    path("contact-numbers", views.contact_numbers, name="api-contact-numbers"),
    path("contact-numbers/<int:contact_number_id>", views.contact_number_detail, name="api-contact-number-detail"),
    path("orders", views.orders, name="api-orders"),
    path("orders/<int:order_id>/dispatch", views.order_dispatch, name="api-order-dispatch"),
    path("orders/<int:order_id>/cancel", views.order_cancel, name="api-order-cancel"),
    path("orders/<int:order_id>/notifications", views.order_notifications, name="api-order-notifications"),
    path("my-orders", views.my_orders, name="api-my-orders"),
    path("notifications/reconciliation", views.notification_reconciliation, name="api-notification-reconciliation"),
    path("notifications/<int:notification_id>/resend", views.notification_resend, name="api-notification-resend"),
    path("notifications/<int:notification_id>/content", views.notification_content, name="api-notification-content"),
]
