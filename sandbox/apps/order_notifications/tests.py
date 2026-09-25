"""
Tests for SMS order notifications.

Twilio is faked at the SDK's transport seam, so every request goes through the
real SDK request pipeline and is captured for assertions. Run with:

    cd sandbox && ../venv/Scripts/python manage.py test apps.order_notifications
"""
import json
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from oscar.core.loading import get_model
from twilio_sdk import TwilioSdkClient
from twilio_sdk.core import BasicAuthCredentials, FormBody, HttpRequest, HttpResponse

from . import provider, services
from .models import ContactNumber, Notification

Product = get_model('catalogue', 'Product')
Partner = get_model('partner', 'Partner')
StockRecord = get_model('partner', 'StockRecord')
ProductClass = get_model('catalogue', 'ProductClass')

FROM = '+15005550006'
SHOPPER_NUMBER = '+16045550100'
TWILIO = dict(
    TWILIO_ACCOUNT_SID='AC00000000000000000000000000000000',
    TWILIO_AUTH_TOKEN='test-token',
    TWILIO_FROM_NUMBER=FROM,
    TWILIO_MESSAGING_SERVICE_SID='MG00000000000000000000000000000000',
    TWILIO_BASE_URL=None,
    SMS_REFERENCE_PREFIX='test',
)


def json_response(status, body):
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


def rfc1123(moment):
    return moment.strftime('%a, %d %b %Y %H:%M:%S +0000')


class FakeTwilio:
    """A transport that answers like Twilio's Messages and Lookup APIs, and records every request."""

    def __init__(self):
        self.requests: list[HttpRequest] = []
        self.messages: dict[str, dict] = {}
        self.create_status = 'queued'
        self.raise_on_create: Exception | None = None
        self.land_before_raising = False
        self.lookup_404 = False
        self.redact_refused = False
        self._n = 0

    # -- helpers for assertions ---------------------------------------
    def calls(self, method, pattern):
        return [r for r in self.requests if r.method == method and re.search(pattern, r.url)]

    def creates(self):
        return self.calls('POST', r'/Messages\.json$')

    def form(self, request):
        assert isinstance(request.body, FormBody)
        return dict(request.body.fields)

    # -- the transport protocol ---------------------------------------
    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        url = urlsplit(request.url)
        if url.netloc == 'lookups.twilio.com':
            if self.lookup_404:
                return json_response(404, {'code': 20404, 'message': 'not found', 'status': 404})
            return json_response(200, {'phone_number': SHOPPER_NUMBER, 'country_code': 'CA',
                                       'national_format': '(604) 555-0100',
                                       'caller_name': None, 'carrier': None, 'add_ons': None})
        if request.method == 'POST' and url.path.endswith('/Messages.json'):
            return self._create(request)
        match = re.search(r'/Messages/(SM\w+)\.json$', url.path)
        if request.method == 'POST' and match:
            return self._update(match.group(1), self.form(request))
        if request.method == 'GET' and match:
            return json_response(200, self.messages[match.group(1)])
        if request.method == 'GET' and url.path.endswith('/Messages.json'):
            return self._list(parse_qs(url.query))
        raise AssertionError(f'unexpected request {request.method} {request.url}')

    def close(self):
        pass

    def _create(self, request):
        fields = self.form(request)
        if self.raise_on_create and not self.land_before_raising:
            raise self.raise_on_create
        self._n += 1
        sid = f'SM{self._n:032d}'
        now = datetime.now(dt_timezone.utc)
        scheduled = fields.get('ScheduleType') == 'fixed'
        self.messages[sid] = {
            'sid': sid, 'to': fields['To'], 'from': fields.get('From'), 'body': fields['Body'],
            'status': 'scheduled' if scheduled else self.create_status,
            'date_created': rfc1123(now), 'date_sent': None if scheduled else rfc1123(now),
            'error_code': None, 'error_message': None,
        }
        if self.raise_on_create:
            raise self.raise_on_create  # landed, then the answer was lost
        return json_response(201, self.messages[sid])

    def _update(self, sid, fields):
        message = self.messages[sid]
        if fields.get('Status') == 'canceled':
            if message['status'] != 'scheduled':
                return json_response(400, {'code': 30409, 'message': 'not scheduled'})
            message['status'] = 'canceled'
        if 'Body' in fields:
            if self.redact_refused:
                return json_response(400, {'code': 20009, 'message': 'cannot redact'})
            message['body'] = fields['Body']
        return json_response(200, message)

    def _list(self, query):
        items = [m for m in self.messages.values()
                 if m['from'] == query.get('From', [None])[0]
                 and ('To' not in query or m['to'] == query['To'][0])]
        return json_response(200, {'messages': items, 'next_page_uri': None,
                                   'page': 0, 'page_size': 50})


@override_settings(**TWILIO)
class NotificationApiTestCase(TestCase):
    def setUp(self):
        self.fake = FakeTwilio()
        provider.reset_client()
        provider._client = TwilioSdkClient(
            server_config={'default': {'base_url': 'https://api.twilio.com'},
                           'default4': {'base_url': 'https://lookups.twilio.com'}},
            custom_http_client=self.fake,
            account_sid_auth_token=BasicAuthCredentials(
                username=str(TWILIO['TWILIO_ACCOUNT_SID']), password='test-token'))
        User = get_user_model()
        self.shopper = User.objects.create_user('shopper', 'shopper@example.com', 'pw-123456789')
        self.other = User.objects.create_user('other', 'other@example.com', 'pw-123456789')
        self.operator = User.objects.create_user('operator', 'op@example.com', 'pw-123456789',
                                                 is_staff=True)
        product_class = ProductClass.objects.create(name='Books')
        self.product = Product.objects.create(title='A book', product_class=product_class)
        StockRecord.objects.create(product=self.product, partner=Partner.objects.create(name='P'),
                                   partner_sku='b1', price=10, price_currency='GBP',
                                   num_in_stock=50)

    def tearDown(self):
        provider._client = None

    # -- helpers --------------------------------------------------------
    def as_user(self, user):
        self.client.force_login(user)

    def post(self, url, data=None, **extra):
        return self.client.post(url, json.dumps(data or {}), content_type='application/json', **extra)

    def register_number(self, user=None, number=SHOPPER_NUMBER):
        self.as_user(user or self.shopper)
        response = self.post('/api/contact-numbers', {'phoneNumber': number})
        self.assertIn(response.status_code, (200, 201), response.content)
        return response.json()['contactNumberId']

    def place_order(self, user=None):
        self.as_user(user or self.shopper)
        response = self.post('/api/orders', {'lines': [{'productId': self.product.pk, 'quantity': 2}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def dispatch(self, order_id):
        self.as_user(self.operator)
        return self.post(f'/api/orders/{order_id}/dispatch')


class ContactNumberTests(NotificationApiTestCase):
    def test_stores_provider_canonical_form(self):
        self.as_user(self.shopper)
        with self.assertLogs('apps.order_notifications', level='INFO') as logs:
            response = self.post('/api/contact-numbers', {'phoneNumber': '(555) 123-4567'})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['phoneNumber'], SHOPPER_NUMBER)
        self.assertIn('contactNumberId', body)
        lookup = self.fake.calls('GET', r'lookups\.twilio\.com/v1/PhoneNumbers/')
        self.assertEqual(len(lookup), 1)
        # The number never reaches the logs, not even in the request line.
        self.assertFalse(any('555' in line or '604' in line for line in logs.output), logs.output)

    def test_rejects_number_provider_does_not_know(self):
        self.fake.lookup_404 = True
        self.as_user(self.shopper)
        response = self.post('/api/contact-numbers', {'phoneNumber': '12345'})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(ContactNumber.objects.exists())

    def test_numbers_are_private_and_deletable(self):
        contact_id = self.register_number()
        self.as_user(self.other)
        self.assertEqual(self.client.get('/api/contact-numbers').json()['contactNumbers'], [])
        self.assertEqual(self.client.delete(f'/api/contact-numbers/{contact_id}').status_code, 404)
        self.as_user(self.shopper)
        self.assertEqual(self.client.delete(f'/api/contact-numbers/{contact_id}').status_code, 204)
        self.assertEqual(self.client.get('/api/contact-numbers').json()['contactNumbers'], [])
        # Nothing is sent to a removed number.
        self.place_order()
        self.assertEqual(self.fake.creates(), [])

    def test_requires_login(self):
        self.assertEqual(self.client.get('/api/contact-numbers').status_code, 401)


class OrderFlowTests(NotificationApiTestCase):
    def test_order_placed_message_goes_out_once_with_its_reference(self):
        self.register_number()
        order = self.place_order()
        self.assertIn('orderId', order)
        creates = self.fake.creates()
        self.assertEqual(len(creates), 1)
        fields = self.fake.form(creates[0])
        reference = services.order_reference(order['orderId'], Notification.KIND_ORDER_PLACED)
        self.assertEqual(fields['To'], SHOPPER_NUMBER)
        self.assertEqual(fields['From'], FROM)
        self.assertTrue(fields['Body'].endswith(f'[ref {services.reference_tag(reference)}]'))
        self.assertEqual(creates[0].headers['idempotency-key'], reference)
        self.assertEqual(order['notification']['outcome'], 'pending')   # queued is not delivered

        # The same event again (double submit / retry) makes no second provider call.
        from oscar.core.loading import get_model as gm
        services.notify(gm('order', 'Order').objects.get(pk=order['orderId']),
                        Notification.KIND_ORDER_PLACED)
        self.assertEqual(len(self.fake.creates()), 1)

    def test_no_number_no_message(self):
        order = self.place_order()
        self.assertIsNone(order['notification'])
        self.assertEqual(self.fake.requests, [])

    def test_refused_connection_is_failed_and_order_still_placed(self):
        self.register_number()
        self.fake.raise_on_create = httpx.ConnectError('refused')
        order = self.place_order()
        self.assertEqual(order['notification']['outcome'], 'failed')
        self.assertEqual(self.fake.calls('GET', r'/Messages\.json'), [])   # nothing to look up

    def test_read_timeout_is_settled_by_lookup_not_by_resending(self):
        self.register_number()
        self.fake.raise_on_create = httpx.ReadTimeout('no reply')
        self.fake.land_before_raising = True       # the message landed; the answer was lost
        order = self.place_order()
        notification = order['notification']
        self.assertEqual(len(self.fake.creates()), 1)
        self.assertEqual(len(self.fake.calls('GET', r'/Messages\.json')), 1)
        self.assertEqual(notification['outcome'], 'pending')
        self.assertIsNotNone(notification['providerSid'])

    def test_read_timeout_not_found_stays_unknown(self):
        self.register_number()
        self.fake.raise_on_create = httpx.ReadTimeout('no reply')   # never landed
        order = self.place_order()
        self.assertEqual(order['notification']['outcome'], 'unknown')
        self.assertEqual(len(self.fake.creates()), 1)

    def test_unlisted_and_failed_statuses_are_not_done(self):
        self.assertEqual(provider.status_from_provider('something_new'), provider.UNKNOWN)
        self.assertEqual(provider.status_from_provider('received'), provider.UNKNOWN)
        self.assertEqual(provider.status_from_provider(None), provider.UNKNOWN)
        from twilio_sdk.models.enums import MessageEnumStatus as S
        self.assertEqual(provider.status_from_provider(S.UNDELIVERED), provider.FAILED)
        self.assertEqual(provider.status_from_provider(S.CANCELED), provider.FAILED)
        self.assertEqual(provider.status_from_provider(S.SENT), provider.PENDING)
        self.assertEqual(provider.status_from_provider(S.DELIVERED), provider.DONE)
        self.assertEqual(provider.cancel_outcome_from_provider(S.CANCELED), provider.DONE)
        self.assertEqual(provider.cancel_outcome_from_provider(S.SENT), provider.FAILED)

    def test_dispatch_schedules_followup_with_provider_and_cancel_calls_it_off(self):
        self.register_number()
        order_id = self.place_order()['orderId']
        response = self.dispatch(order_id)
        self.assertEqual(response.status_code, 200, response.content)
        creates = self.fake.creates()
        self.assertEqual(len(creates), 3)
        followup = self.fake.form(creates[2])
        self.assertEqual(followup['ScheduleType'], 'fixed')
        self.assertEqual(followup['MessagingServiceSid'], TWILIO['TWILIO_MESSAGING_SERVICE_SID'])
        self.assertEqual(followup['From'], FROM)
        self.assertIn('SendAt', followup)
        kinds = {n['kind']: n for n in response.json()['notifications']}
        self.assertEqual(kinds['delivery_followup']['status'], 'scheduled')

        # A second dispatch is refused and sends nothing.
        self.assertEqual(self.dispatch(order_id).status_code, 409)
        self.assertEqual(len(self.fake.creates()), 3)

        response = self.post(f'/api/orders/{order_id}/cancel')
        self.assertEqual(response.status_code, 200, response.content)
        cancels = [r for r in self.fake.calls('POST', r'/Messages/SM\w+\.json$')
                   if self.fake.form(r).get('Status') == 'canceled']
        self.assertEqual(len(cancels), 1)
        followup_row = Notification.objects.get(kind='delivery_followup')
        self.assertEqual(followup_row.cancel_state, Notification.CANCEL_DONE)
        self.assertEqual(followup_row.provider_status, 'canceled')
        self.assertEqual(response.json()['status'], 'Cancelled')
        self.assertEqual(response.json()['notification']['kind'], 'order_cancelled')

    def test_operator_actions_need_staff_and_orders_are_private(self):
        self.register_number()
        order_id = self.place_order()['orderId']
        self.as_user(self.shopper)
        self.assertEqual(self.post(f'/api/orders/{order_id}/dispatch').status_code, 403)
        self.as_user(self.other)
        self.assertEqual(self.client.get(f'/api/orders/{order_id}/notifications').status_code, 404)
        self.assertEqual(self.client.get('/api/my-orders').json()['orders'], [])
        self.as_user(self.shopper)
        mine = self.client.get('/api/my-orders').json()['orders']
        self.assertEqual([o['orderId'] for o in mine], [order_id])
        self.assertEqual(len(mine[0]['notifications']), 1)


class OperatorTests(NotificationApiTestCase):
    def failed_notification(self):
        self.register_number()
        self.fake.create_status = 'undelivered'
        order = self.place_order()
        self.fake.create_status = 'queued'
        return order['notification']['notificationId']

    def test_resend_is_idempotent_per_key(self):
        notification_id = self.failed_notification()
        self.as_user(self.operator)
        url = f'/api/notifications/{notification_id}/resend'
        first = self.post(url, HTTP_IDEMPOTENCY_KEY='k-1')
        self.assertEqual(first.status_code, 201, first.content)
        again = self.post(url, HTTP_IDEMPOTENCY_KEY='k-1')
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()['notificationId'], first.json()['notificationId'])
        self.assertEqual(len(self.fake.creates()), 2)            # original + one resend
        fresh = self.post(url, HTTP_IDEMPOTENCY_KEY='k-2')
        self.assertEqual(fresh.status_code, 201)
        self.assertNotEqual(fresh.json()['notificationId'], first.json()['notificationId'])
        self.assertEqual(len(self.fake.creates()), 3)
        self.assertEqual(self.post(url).status_code, 400)       # key required

    def test_resend_refuses_a_message_that_is_not_failed(self):
        self.register_number()
        notification_id = self.place_order()['notification']['notificationId']
        self.as_user(self.operator)
        response = self.post(f'/api/notifications/{notification_id}/resend', HTTP_IDEMPOTENCY_KEY='k')
        self.assertEqual(response.status_code, 409)

    def test_content_disposal_redacts_at_provider_and_keeps_the_record(self):
        notification_id = self.failed_notification()
        self.as_user(self.operator)
        response = self.client.delete(f'/api/notifications/{notification_id}/content')
        self.assertEqual(response.status_code, 200, response.content)
        redactions = [r for r in self.fake.calls('POST', r'/Messages/SM\w+\.json$')
                      if 'Body' in self.fake.form(r)]
        self.assertEqual([self.fake.form(r)['Body'] for r in redactions], [''])
        row = Notification.objects.get(pk=notification_id)
        self.assertEqual(row.body, '')
        self.assertEqual(row.content_state, Notification.CONTENT_DISPOSED)
        self.assertEqual(row.provider_status, 'undelivered')    # the fact and fate survive
        self.assertIsNone(response.json()['body'])

    def test_content_disposal_refused_by_provider_is_not_claimed(self):
        notification_id = self.failed_notification()
        self.fake.redact_refused = True
        self.as_user(self.operator)
        response = self.client.delete(f'/api/notifications/{notification_id}/content')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Notification.objects.get(pk=notification_id).content_state,
                         Notification.CONTENT_RETAINED)

    def test_reconciliation_lines_up_both_sides(self):
        self.register_number()
        self.place_order()
        # A message the provider knows about and this app does not.
        now = datetime.now(dt_timezone.utc)
        self.fake.messages['SMforeign'] = {
            'sid': 'SMforeign', 'to': '+16045550199', 'from': FROM, 'body': 'x',
            'status': 'delivered', 'date_created': rfc1123(now), 'date_sent': rfc1123(now)}
        # One outside the window, which the widened provider query also returns.
        old = now - timedelta(hours=30)
        self.fake.messages['SMold'] = {
            'sid': 'SMold', 'to': '+16045550199', 'from': FROM, 'body': 'x',
            'status': 'delivered', 'date_created': rfc1123(old), 'date_sent': rfc1123(old)}
        self.as_user(self.operator)
        start = (now - timedelta(hours=1)).isoformat()
        end = (now + timedelta(hours=1)).isoformat()
        response = self.client.get('/api/notifications/reconciliation', {'from': start, 'to': end})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual(report['counts']['matched'], 1)
        self.assertEqual([e['providerSid'] for e in report['providerOnly']], ['SMforeign'])
        self.assertEqual(report['localOnly'], [])
        listing = self.fake.calls('GET', r'/Messages\.json\?')[-1]
        self.assertIn('From=%2B15005550006', listing.url)

    def test_reconciliation_is_for_operators(self):
        self.as_user(self.shopper)
        self.assertEqual(self.client.get('/api/notifications/reconciliation',
                                         {'from': '2026-01-01T00:00:00Z',
                                          'to': '2026-01-02T00:00:00Z'}).status_code, 403)


@override_settings(**{**TWILIO, 'TWILIO_BASE_URL': 'https://twilio-proxy.example.test'})
class BaseUrlOverrideTests(TestCase):
    def test_override_applies_to_messaging_api_only(self):
        seen = []

        class Recorder(FakeTwilio):
            def send(self, request):
                seen.append(urlsplit(request.url).netloc)
                return super().send(request)

        recorder = Recorder()
        provider.reset_client()
        # Build the real client from settings, swapping only the innermost transport.
        try:
            with mock.patch.object(provider, 'HttpxClient', lambda **kwargs: recorder):
                provider.lookup_number('+16045550100')
                provider.send_message(to=SHOPPER_NUMBER, body='hi', reference='test:x')
        finally:
            provider.reset_client()
        self.assertEqual(seen, ['lookups.twilio.com', 'twilio-proxy.example.test'])
