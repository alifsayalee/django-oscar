from django.urls import path

from . import views

app_name = "ordersms"

urlpatterns = [
    # Flow 1 — contact numbers
    path("contact-numbers", views.contact_numbers, name="contact-numbers"),
    path("contact-numbers/<int:pk>", views.contact_number_detail, name="contact-number-detail"),
    # Flow 2 — orders
    path("orders", views.orders, name="orders"),
    path("orders/<int:pk>/dispatch", views.order_dispatch, name="order-dispatch"),
    path("orders/<int:pk>/cancel", views.order_cancel, name="order-cancel"),
    path("my-orders", views.my_orders, name="my-orders"),
    path("orders/<int:pk>/notifications", views.order_notifications, name="order-notifications"),
    # Flow 3 — operator actions
    path("notifications/<int:pk>/resend", views.notification_resend, name="notification-resend"),
    path("notifications/<int:pk>/content", views.notification_content, name="notification-content"),
    path("notifications/reconciliation", views.notification_reconciliation, name="notification-reconciliation"),
]
