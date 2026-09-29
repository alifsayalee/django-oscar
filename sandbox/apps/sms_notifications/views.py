"""
JSON API for SMS order notifications. Callers authenticate with the
sandbox's own session login; the caller is always ``request.user``.
"""
import json
import logging
from datetime import datetime, timezone as dt_timezone
from typing import Any

from django.http import Http404, HttpRequest, JsonResponse
from django.http.response import HttpResponseBase
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from django.views import View
from oscar.core.loading import get_model

from . import services
from .gateway import ProviderError
from .models import ContactNumber, Notification, Outcome

logger = logging.getLogger(__name__)

Order = get_model('order', 'Order')


def answer(outcome: str) -> tuple[int, str]:
    """
    The one place a write's outcome becomes the caller's status. Success
    comes from ``done`` alone.
    """
    match outcome:
        case Outcome.DONE:
            return 200, 'done'
        case Outcome.PENDING | Outcome.SENDING:
            return 202, 'pending' if outcome == Outcome.PENDING else 'in_progress'
        case Outcome.FAILED | Outcome.NEEDS_REVIEW:
            return 409, outcome
        case Outcome.SKIPPED:
            return 409, 'skipped_no_number'
        case _:
            return 504, 'unknown'


def error(status: int, message: str, **extra: Any) -> JsonResponse:
    return JsonResponse({'error': message, **extra}, status=status)


class ApiView(View):
    """Session-authenticated JSON view; ``staff_only`` views are operator actions."""

    staff_only = False

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if not request.user.is_authenticated:
            return error(401, 'Authentication required.')
        if self.staff_only and not request.user.is_staff:
            return error(403, 'Only staff may do this.')
        try:
            return super().dispatch(request, *args, **kwargs)
        except Http404:
            return error(404, 'Not found.')
        except services.ServiceError as e:
            return error(e.status_code, e.message, **e.extra)
        except ProviderError as e:
            return error(e.status_code, e.message, outcomeUnknown=e.outcome_unknown)

    def http_method_not_allowed(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        response = super().http_method_not_allowed(request, *args, **kwargs)
        return error(405, 'Method not allowed.', allowed=response['Allow'])

    def json_body(self) -> dict[str, Any]:
        if not self.request.body:
            return {}
        try:
            data = json.loads(self.request.body)
        except (ValueError, UnicodeDecodeError):
            raise services.ServiceError(400, 'The request body must be JSON.')
        if not isinstance(data, dict):
            raise services.ServiceError(400, 'The request body must be a JSON object.')
        return data


# Serialisation
# =============

def contact_number_json(contact: ContactNumber) -> dict[str, Any]:
    return {
        'contactNumberId': contact.pk,
        'phoneNumber': contact.phone_number,
        'countryCode': contact.country_code or None,
        'created': contact.created.isoformat(),
    }


def notification_json(notification: Notification) -> dict[str, Any]:
    status_code, label = answer(notification.outcome)
    contact = notification.contact_number
    return {
        'notificationId': notification.pk,
        'orderId': notification.order_id,
        'kind': notification.kind,
        'outcome': label,
        'deliveryStatus': notification.provider_status or None,
        'providerSid': notification.provider_sid or None,
        'errorCode': notification.error_code,
        'errorMessage': notification.error_message or None,
        'to': contact.masked if contact else None,
        'scheduledFor': notification.send_at.isoformat() if notification.send_at else None,
        'sentAt': notification.provider_sent_at.isoformat() if notification.provider_sent_at else None,
        'callOff': call_off_json(notification.call_off_outcome) if notification.kind == Notification.FOLLOWUP else None,
        'resendOf': notification.resend_of_id,
        'body': notification.body if notification.content_redacted_at is None else None,
        'contentDisposedAt': (notification.content_redacted_at.isoformat()
                              if notification.content_redacted_at else None),
        'lastCheckedAt': notification.last_checked_at.isoformat() if notification.last_checked_at else None,
        'created': notification.created.isoformat(),
    }


def call_off_json(outcome: str) -> str | None:
    if not outcome:
        return None
    if outcome == Outcome.DONE:
        return 'called_off'
    if outcome == Outcome.FAILED:
        return 'too_late'
    return answer(outcome)[1]


def order_json(order: Any, notifications: list[Notification] | None = None) -> dict[str, Any]:
    if notifications is None:
        notifications = list(order.sms_notifications.select_related('contact_number'))
    return {
        'orderId': order.pk,
        'number': order.number,
        'status': order.status,
        'total': str(order.total_incl_tax),
        'currency': order.currency,
        'datePlaced': order.date_placed.isoformat(),
        'lines': [{
            'productId': line.product_id,
            'title': line.title,
            'quantity': line.quantity,
        } for line in order.lines.all()],
        'notifications': [notification_json(n) for n in notifications],
    }


# Flow 1 - contact numbers
# ========================

class ContactNumberListView(ApiView):

    def get(self, request: HttpRequest) -> JsonResponse:
        contacts = ContactNumber.objects.active().filter(user_id=int(request.user.pk or 0))
        return JsonResponse({'contactNumbers': [contact_number_json(c) for c in contacts]})

    def post(self, request: HttpRequest) -> JsonResponse:
        data = self.json_body()
        contact, created = services.register_contact_number(
            request.user, str(data.get('phoneNumber') or ''), str(data.get('countryCode') or ''))
        return JsonResponse(contact_number_json(contact), status=201 if created else 200)


class ContactNumberDetailView(ApiView):

    def delete(self, request: HttpRequest, contact_number_id: int) -> JsonResponse:
        contact = get_contact_or_404(request.user, contact_number_id)
        call_offs = services.delete_contact_number(contact)
        return JsonResponse({
            'contactNumberId': contact.pk,
            'deleted': True,
            'followUpsCalledOff': [call_off_json(outcome) for outcome in call_offs],
        })


def get_contact_or_404(user: Any, contact_number_id: int) -> ContactNumber:
    return get_object_or_404(ContactNumber.objects.active(), pk=contact_number_id, user=user)


# Flow 2 - orders
# ===============

class OrderCreateView(ApiView):

    def post(self, request: HttpRequest) -> JsonResponse:
        data = self.json_body()
        raw_lines = data.get('lines')
        if not isinstance(raw_lines, list) or not raw_lines:
            raise services.ServiceError(400, '`lines` must be a non-empty list of {productId, quantity}.')
        lines = []
        for raw in raw_lines:
            try:
                product_id, quantity = int(raw['productId']), int(raw.get('quantity', 1))
            except (TypeError, KeyError, ValueError):
                raise services.ServiceError(400, 'Each line needs an integer productId and quantity.')
            if quantity < 1:
                raise services.ServiceError(400, 'quantity must be at least 1.')
            lines.append((product_id, quantity))
        address = data.get('shippingAddress')
        if address is not None and not isinstance(address, dict):
            raise services.ServiceError(400, '`shippingAddress` must be an object.')
        order, notifications = services.place_order(request.user, lines, address)
        return JsonResponse(order_json(order, notifications), status=201)


class OrderDispatchView(ApiView):
    staff_only = True

    def post(self, request: HttpRequest, order_id: int) -> JsonResponse:
        order = get_object_or_404(Order, pk=order_id)
        order, notifications = services.dispatch_order(order)
        return JsonResponse(order_json(order, notifications))


class OrderCancelView(ApiView):
    staff_only = True

    def post(self, request: HttpRequest, order_id: int) -> JsonResponse:
        order = get_object_or_404(Order, pk=order_id)
        order, notifications = services.cancel_order(order)
        return JsonResponse(order_json(order, notifications))


class MyOrdersView(ApiView):

    def get(self, request: HttpRequest) -> JsonResponse:
        orders = list(Order.objects.filter(user=request.user).order_by('-date_placed')
                      .prefetch_related('lines'))
        notifications = list(Notification.objects.filter(order__in=orders).select_related('contact_number'))
        services.refresh_many(notifications)
        by_order: dict[int, list[Notification]] = {}
        for notification in notifications:
            by_order.setdefault(notification.order_id, []).append(notification)
        return JsonResponse({'orders': [order_json(o, by_order.get(o.pk, [])) for o in orders]})


class OrderNotificationsView(ApiView):

    def get(self, request: HttpRequest, order_id: int) -> JsonResponse:
        order = get_object_or_404(Order, pk=order_id)
        # A shopper sees only their own orders; operators may look at any.
        if order.user_id != request.user.pk and not request.user.is_staff:
            return error(404, 'Not found.')
        notifications = list(order.sms_notifications.select_related('contact_number'))
        services.refresh_many(notifications)
        return JsonResponse({
            'orderId': order.pk,
            'number': order.number,
            'status': order.status,
            'notifications': [notification_json(n) for n in notifications],
        })


# Flow 3 - operator actions
# =========================

class NotificationResendView(ApiView):
    staff_only = True

    def post(self, request: HttpRequest, notification_id: int) -> JsonResponse:
        source = get_object_or_404(Notification.objects.select_related('order'), pk=notification_id)
        notification = services.resend(source, request.headers.get('Idempotency-Key', ''))
        status, label = answer(notification.outcome)
        return JsonResponse({
            **notification_json(notification),
            'notificationId': notification.pk,
            'outcome': label,
        }, status=status)


class NotificationContentView(ApiView):
    staff_only = True

    def delete(self, request: HttpRequest, notification_id: int) -> JsonResponse:
        notification = get_object_or_404(Notification, pk=notification_id)
        outcome = services.dispose_content(notification)
        notification.refresh_from_db()
        status, label = answer(outcome)
        return JsonResponse({
            'notificationId': notification.pk,
            'contentDisposal': label,
            'notification': notification_json(notification),
        }, status=status)


class ReconciliationView(ApiView):
    staff_only = True

    def get(self, request: HttpRequest) -> JsonResponse:
        date_from = parse_iso(request.GET.get('from'), 'from')
        date_to = parse_iso(request.GET.get('to'), 'to')
        return JsonResponse(services.reconcile(date_from, date_to))


def parse_iso(value: str | None, name: str) -> datetime:
    parsed = parse_datetime(value or '')
    if parsed is None:
        raise services.ServiceError(400, '`%s` must be an ISO-8601 date-time.' % name)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed

