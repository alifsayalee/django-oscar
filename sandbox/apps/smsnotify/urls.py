from django.urls import path

from . import views

app_name = "smsnotify"

urlpatterns = [
    # Flow 1 -- contact numbers
    path("contact-numbers", views.contact_numbers, name="contact-numbers"),
    path(
        "contact-numbers/<int:contact_number_id>",
        views.contact_number_detail,
        name="contact-number-detail",
    ),
    # Flow 2 -- orders + notifications
    path("orders", views.create_order, name="orders"),
    path("my-orders", views.my_orders, name="my-orders"),
    path(
        "orders/<int:order_id>/notifications",
        views.order_notifications,
        name="order-notifications",
    ),
    path("orders/<int:order_id>/dispatch", views.dispatch_order, name="order-dispatch"),
    path("orders/<int:order_id>/cancel", views.cancel_order, name="order-cancel"),
    # Flow 3 -- operator notification actions + content disposal
    path(
        "notifications/<int:notification_id>/resend",
        views.resend_notification,
        name="notification-resend",
    ),
    path(
        "notifications/<int:notification_id>/content",
        views.notification_content,
        name="notification-content",
    ),
    path("notifications/reconciliation", views.reconciliation, name="reconciliation"),
]
