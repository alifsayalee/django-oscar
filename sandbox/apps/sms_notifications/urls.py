from collections.abc import Callable

from django.db import transaction
from django.http.response import HttpResponseBase
from django.urls import path
from django.views import View

from . import views


def api_view(view_class: type[View]) -> Callable[..., HttpResponseBase]:
    # Outside ATOMIC_REQUESTS: a provider-write claim must be committed before the provider is called.
    return transaction.non_atomic_requests(view_class.as_view())


app_name = 'sms_notifications'

urlpatterns = [
    path('contact-numbers', api_view(views.ContactNumberListView), name='contact-numbers'),
    path('contact-numbers/<int:contact_number_id>', api_view(views.ContactNumberDetailView),
         name='contact-number'),
    path('orders', api_view(views.OrderCreateView), name='orders'),
    path('orders/<int:order_id>/dispatch', api_view(views.OrderDispatchView), name='order-dispatch'),
    path('orders/<int:order_id>/cancel', api_view(views.OrderCancelView), name='order-cancel'),
    path('orders/<int:order_id>/notifications', api_view(views.OrderNotificationsView),
         name='order-notifications'),
    path('my-orders', api_view(views.MyOrdersView), name='my-orders'),
    path('notifications/reconciliation', api_view(views.ReconciliationView), name='reconciliation'),
    path('notifications/<int:notification_id>/resend', api_view(views.NotificationResendView),
         name='notification-resend'),
    path('notifications/<int:notification_id>/content', api_view(views.NotificationContentView),
         name='notification-content'),
]
