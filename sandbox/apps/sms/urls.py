"""API routes for SMS order notifications, mounted under /api/ in sandbox/urls.py.

Each action is separately invocable — no do-everything route. Paths carry no trailing slash to
match the task's contract exactly (POST is not redirected by APPEND_SLASH).
"""
from django.urls import path

from . import views

app_name = "sms"

urlpatterns = [
    path("contact-numbers", views.contact_numbers, name="contact-numbers"),
    path(
        "contact-numbers/<int:contact_id>",
        views.contact_number_detail,
        name="contact-number-detail",
    ),
    path("orders", views.create_order, name="create-order"),
    path("orders/<int:order_id>/dispatch", views.dispatch_order, name="dispatch-order"),
    path("orders/<int:order_id>/cancel", views.cancel_order, name="cancel-order"),
    path("orders/<int:order_id>/notifications", views.order_notifications, name="order-notifications"),
    path("my-orders", views.my_orders, name="my-orders"),
    path(
        "notifications/<int:notification_id>/resend",
        views.resend_notification,
        name="notification-resend",
    ),
    path(
        "notifications/<int:notification_id>/content",
        views.dispose_notification_content,
        name="notification-content",
    ),
    path("notifications/reconciliation", views.reconciliation, name="notification-reconciliation"),
]
