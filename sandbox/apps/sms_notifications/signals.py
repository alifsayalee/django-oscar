"""
Hook the notifications onto Oscar's own order signals, so an order placed or
moved through the storefront or the dashboard is covered exactly like one
driven through the API. Work runs after the surrounding transaction commits,
and never raises into the order flow.
"""
import logging

from django.db import transaction
from django.dispatch import receiver
from oscar.apps.order.signals import order_placed, order_status_changed

from . import services
from .models import Notification

logger = logging.getLogger(__name__)


@receiver(order_placed, dispatch_uid="sms_notifications.order_placed")
def on_order_placed(sender, order, **kwargs):
    def send():
        try:
            services.notify(order, Notification.KIND_PLACED)
        except Exception:
            logger.exception("order %s: failed to send the order-placed message", order.number)

    transaction.on_commit(send)


@receiver(order_status_changed, dispatch_uid="sms_notifications.order_status_changed")
def on_order_status_changed(sender, order, old_status, new_status, **kwargs):
    if new_status not in (services.STATUS_DISPATCHED, services.STATUS_CANCELLED):
        return
    transaction.on_commit(lambda: services.handle_status_change(order, new_status))
