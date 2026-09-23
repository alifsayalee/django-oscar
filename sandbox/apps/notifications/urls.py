"""API routes for the SMS notification app, mounted under /api/ by sandbox/urls.py.

Each action a caller can take is a separate route -- no do-everything endpoint.
"""

from django.urls import path

from . import views

app_name = "sms_notifications"

urlpatterns = [
    # Session login, exposed as JSON so the flows are drivable end to end.
    path("auth/login", views.auth_login, name="auth-login"),
    path("auth/logout", views.auth_logout, name="auth-logout"),

    # Flow 1 -- the shopper's contact number.
    path("contact-numbers", views.contact_numbers, name="contact-numbers"),
    path("contact-numbers/<int:pk>", views.contact_number_detail, name="contact-number-detail"),

    # Flow 2 -- orders and the messages as they move.
    path("orders", views.orders, name="orders"),
    path("orders/<str:order_number>/dispatch", views.dispatch_order, name="order-dispatch"),
    path("orders/<str:order_number>/cancel", views.cancel_order, name="order-cancel"),
    path("my-orders", views.my_orders, name="my-orders"),
    path("orders/<str:order_number>/notifications", views.order_notifications, name="order-notifications"),

    # Flow 3 -- operator actions.
    path("notifications/reconciliation", views.reconciliation, name="reconciliation"),
    path("notifications/<int:pk>/resend", views.resend_notification, name="notification-resend"),
    path("notifications/<int:pk>/content", views.notification_content, name="notification-content"),
]
