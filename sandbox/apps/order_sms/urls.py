from django.urls import path

from . import views

app_name = "order_sms"

urlpatterns = [
    path("csrf", views.csrf, name="csrf"),
    path("login", views.login_view, name="login"),
    path("logout", views.logout_view, name="logout"),
    path("contact-numbers", views.contact_numbers, name="contact-numbers"),
    path("contact-numbers/<int:contact_number_id>", views.contact_number_detail,
         name="contact-number-detail"),
    path("orders", views.place_order, name="orders"),
    path("my-orders", views.my_orders, name="my-orders"),
    path("orders/<int:order_id>/dispatch", views.dispatch_order, name="order-dispatch"),
    path("orders/<int:order_id>/cancel", views.cancel_order, name="order-cancel"),
    path("orders/<int:order_id>/notifications", views.order_notifications,
         name="order-notifications"),
    path("notifications/reconciliation", views.reconciliation, name="reconciliation"),
    path("notifications/<int:notification_id>/resend", views.resend_notification,
         name="notification-resend"),
    path("notifications/<int:notification_id>/content", views.notification_content,
         name="notification-content"),
]
