"""
Tests for order SMS notifications.

Twilio is replaced by an in-memory fake plugged into the SDK's transport seam
(``custom_http_client``), so the real request-building and decoding pipeline of
the SDK runs on every call and no message is ever sent.

Run with:  cd sandbox && python manage.py test apps.order_notifications
"""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from email.utils import format_datetime
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from oscar.test.factories import create_product
from twilio_sdk.core import FormBody

from . import twilio_gateway
from .models import ContactNumber, Notification
from .twilio_gateway import ProviderError, TwilioGateway

ACCOUNT_SID = 'ACtest0000000000000000000000000000'
FROM_NUMBER = '+18005550199'
SERVICE_SID = 'MGtest000000000000000000000000000'
CA_NUMBER = '+14165550100'       # fake numbers: this fake never reaches a network
US_NUMBER = '+12025550123'
OTHER_SENDER = '+18005550100'


@dataclass
class StubResponse:
    status_code: int = 200
    headers: dict = field(default_factory=dict)
    chunks: list = field(default_factory=list)
    url: str = 'https://api.twilio.com/'
    closed: bool = False

    def iter_bytes(self, chunk_size):
        yield from self.chunks
        self.close()

    def read(self):
        self.close()
        return b''.join(self.chunks)

    def close(self):
        self.closed = True


def json_response(status, body):
    return StubResponse(status_code=status, headers={'content-type': 'application/json'},
                        chunks=[json.dumps(body).encode()])


class FakeTwilio:
    """Just enough of Twilio's Lookups and Messages APIs to drive the app."""

    def __init__(self):
        self.messages = {}
        self.requests = []
        self.reject_to = set()
        self.raise_on_send = None
        self.page_size_override = None
        self._seq = 0

    # transport protocol
    def send(self, request):
        self.requests.append(request)
        parts = urlsplit(request.url)
        path, query = parts.path, parse_qs(parts.query)
        if parts.netloc == 'lookups.twilio.com':
            return self._lookup(unquote(path.rsplit('/', 1)[-1]), query)
        base = '/2010-04-01/Accounts/%s/Messages' % ACCOUNT_SID
        if path == base + '.json' and request.method == 'POST':
            if self.raise_on_send is not None:
                error, self.raise_on_send = self.raise_on_send, None
                raise error
            return self._create(self._form(request))
        if path == base + '.json' and request.method == 'GET':
            return self._list(query)
        match = re.fullmatch(re.escape(base) + r'/(\w+)\.json', path)
        if match:
            message = self.messages.get(match.group(1))
            if message is None:
                return json_response(404, {'code': 20404, 'message': 'Not found', 'status': 404})
            if request.method == 'GET':
                return json_response(200, message)
            return self._update(message, self._form(request))
        return json_response(404, {'code': 20404, 'message': 'Unknown route'})

    def close(self):
        pass

    @staticmethod
    def _form(request):
        assert isinstance(request.body, FormBody)
        return dict(request.body.fields)

    def _lookup(self, number, query):
        digits = re.sub(r'\D', '', number)
        country = (query.get('CountryCode') or [None])[0]
        if number.startswith('+') and len(digits) == 11 and digits.startswith('1'):
            e164 = '+' + digits
        elif country in ('CA', 'US') and len(digits) == 10:
            e164 = '+1' + digits
        else:
            return json_response(200, {'phone_number': number, 'valid': False, 'validation_errors': ['TOO_SHORT']})
        return json_response(200, {'phone_number': e164, 'valid': True, 'validation_errors': [],
                                   'country_code': 'CA', 'calling_country_code': '1'})

    def _create(self, form):
        to = form['To']
        if to in self.reject_to:
            return json_response(400, {'code': 21211, 'message': "The 'To' number %s is not valid." % to,
                                       'status': 400})
        self._seq += 1
        sid = 'SM%032d' % self._seq
        now = datetime.now(timezone.utc)
        scheduled = form.get('ScheduleType') == 'fixed'
        self.messages[sid] = {
            'sid': sid, 'account_sid': ACCOUNT_SID, 'to': to, 'from': form.get('From'),
            'body': form.get('Body'), 'status': 'scheduled' if scheduled else 'queued',
            'messaging_service_sid': form.get('MessagingServiceSid'),
            'date_created': format_datetime(now, usegmt=True),
            'date_sent': None if scheduled else format_datetime(now, usegmt=True),
            'error_code': None, 'error_message': None, 'direction': 'outbound-api',
            '_send_at': form.get('SendAt'),
        }
        return json_response(201, self.messages[sid])

    def _update(self, message, form):
        if form.get('Status') == 'canceled':
            if message['status'] != 'scheduled':
                return json_response(400, {'code': 30409, 'message': 'Message is not scheduled.'})
            message['status'] = 'canceled'
        if 'Body' in form:
            if message['status'] in ('queued', 'sending', 'scheduled', 'accepted'):
                return json_response(400, {'code': 20009, 'message': 'Cannot redact an in-flight message.'})
            message['body'] = form['Body']
        return json_response(200, message)

    def _list(self, query):
        sender = query.get('From', [None])[0]
        items = [m for m in self.messages.values() if m['date_sent'] and (sender is None or m['from'] == sender)]
        items.sort(key=lambda m: m['sid'])
        size = self.page_size_override or int(query.get('PageSize', ['50'])[0])
        page = int(query.get('Page', ['0'])[0])
        chunk = items[page * size:(page + 1) * size]
        more = (page + 1) * size < len(items)
        next_uri = ('/2010-04-01/Accounts/%s/Messages.json?PageSize=%d&Page=%d&PageToken=PA%d'
                    % (ACCOUNT_SID, size, page + 1, page + 1)) if more else None
        return json_response(200, {'messages': chunk, 'next_page_uri': next_uri, 'page': page, 'page_size': size})

    # helpers for tests
    def created(self):
        return [r for r in self.requests if r.method == 'POST' and r.url.endswith('/Messages.json')]

    def set_status(self, sid, status, error_code=None):
        self.messages[sid]['status'] = status
        self.messages[sid]['error_code'] = error_code

    def fire_scheduled(self, sid):
        """What Twilio would do when the scheduled time arrives."""
        message = self.messages[sid]
        if message['status'] == 'scheduled':
            message['status'] = 'delivered'
            message['date_sent'] = format_datetime(datetime.now(timezone.utc), usegmt=True)


@override_settings(ORDER_NOTIFICATIONS_FOLLOW_UP_DELAY=3 * 24 * 3600)
class NotificationApiTestCase(TestCase):

    def setUp(self):
        self.twilio = FakeTwilio()
        self.gateway = TwilioGateway(
            account_sid=ACCOUNT_SID, auth_token='test-token', from_number=FROM_NUMBER,
            messaging_service_sid=SERVICE_SID, http_client=self.twilio)
        twilio_gateway.set_gateway(self.gateway)
        User = get_user_model()
        self.shopper = User.objects.create_user('shopper', 'shopper@example.com', 'x-password-1')
        self.other = User.objects.create_user('other', 'other@example.com', 'x-password-2')
        self.operator = User.objects.create_user('operator', 'operator@example.com', 'x-password-3', is_staff=True)
        self.product = create_product(price=D('12.50'), num_in_stock=100)
        self.shop = Client()
        self.shop.force_login(self.shopper)
        self.ops = Client()
        self.ops.force_login(self.operator)

    def tearDown(self):
        twilio_gateway.set_gateway(None)

    # helpers
    def post(self, client, url, data=None, **extra):
        return client.post(url, json.dumps(data or {}), content_type='application/json', **extra)

    def register(self, client=None, number=CA_NUMBER, **extra):
        return self.post(client or self.shop, '/api/contact-numbers', {'phoneNumber': number, **extra})

    def place(self, client=None):
        response = self.post(client or self.shop, '/api/orders',
                             {'lines': [{'productId': self.product.pk, 'quantity': 2}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def notifications(self, order_id, client=None):
        response = (client or self.shop).get('/api/orders/%s/notifications' % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        return {n['kind']: n for n in response.json()['notifications']}


class ContactNumberTests(NotificationApiTestCase):

    def test_stores_providers_canonical_form(self):
        response = self.register(number='(416) 555-0100', countryCode='CA')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['phoneNumber'], CA_NUMBER)
        self.assertIn('contactNumberId', response.json())
        lookup = self.twilio.requests[-1]
        self.assertTrue(lookup.url.startswith('https://lookups.twilio.com/v2/PhoneNumbers/'))
        self.assertTrue(lookup.headers['authorization'].startswith('Basic '))

    def test_rejects_number_provider_considers_unusable(self):
        response = self.register(number='+1416555')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ContactNumber.objects.exists())

    def test_list_and_delete_are_scoped_to_owner(self):
        contact_id = self.register().json()['contactNumberId']
        other = Client()
        other.force_login(self.other)
        self.assertEqual(other.get('/api/contact-numbers').json()['contactNumbers'], [])
        self.assertEqual(other.delete('/api/contact-numbers/%s' % contact_id).status_code, 404)
        self.assertEqual(self.shop.delete('/api/contact-numbers/%s' % contact_id).status_code, 204)
        self.assertEqual(self.shop.get('/api/contact-numbers').json()['contactNumbers'], [])

    def test_requires_login(self):
        self.assertEqual(Client().get('/api/contact-numbers').status_code, 401)

    def test_deleting_number_calls_off_scheduled_follow_up(self):
        contact_id = self.register().json()['contactNumberId']
        order = self.place()
        self.post(self.ops, '/api/orders/%s/dispatch' % order['orderId'])
        follow_up = Notification.objects.get(kind=Notification.KIND_FOLLOW_UP)
        self.assertEqual(self.shop.delete('/api/contact-numbers/%s' % contact_id).status_code, 204)
        self.assertEqual(self.twilio.messages[follow_up.provider_sid]['status'], 'canceled')
        before = len(self.twilio.created())
        self.post(self.ops, '/api/orders/%s/cancel' % order['orderId'])
        self.assertEqual(len(self.twilio.created()), before)  # nothing is sent to a removed number


class OrderLifecycleTests(NotificationApiTestCase):

    def test_placing_order_texts_shopper(self):
        self.register()
        order = self.place()
        self.assertIn('orderId', order)
        placed = self.notifications(order['orderId'])[Notification.KIND_PLACED]
        self.assertEqual(placed['sendState'], 'sent')
        self.assertEqual(placed['providerStatus'], 'queued')
        request = self.twilio.created()[-1]
        self.assertEqual(request.body.fields['To'], CA_NUMBER)
        self.assertEqual(request.body.fields['From'], FROM_NUMBER)
        self.assertIn(order['number'], request.body.fields['Body'])

    def test_order_lines_reuse_oscar_models(self):
        order = self.place()
        self.assertEqual(order['lines'][0]['productId'], self.product.pk)
        self.assertEqual(order['lines'][0]['quantity'], 2)
        self.assertEqual(order['status'], 'Pending')

    def test_shopper_without_number_is_not_messaged(self):
        order = self.place()
        self.assertEqual(order['notifications'], [])
        self.assertEqual(self.twilio.created(), [])

    def test_dispatch_schedules_follow_up_with_provider(self):
        self.register()
        order = self.place()
        response = self.post(self.ops, '/api/orders/%s/dispatch' % order['orderId'])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['status'], 'Dispatched')
        by_kind = self.notifications(order['orderId'])
        self.assertEqual(by_kind[Notification.KIND_DISPATCHED]['sendState'], 'sent')
        follow_up = by_kind[Notification.KIND_FOLLOW_UP]
        self.assertEqual(follow_up['providerStatus'], 'scheduled')
        fields = self.twilio.created()[-1].body.fields
        self.assertEqual(fields['ScheduleType'], 'fixed')
        self.assertEqual(fields['MessagingServiceSid'], SERVICE_SID)
        send_at = datetime.fromisoformat(fields['SendAt'].replace('Z', '+00:00'))
        self.assertGreater(send_at - datetime.now(timezone.utc), timedelta(days=2, hours=23))

    def test_dispatch_twice_does_not_text_twice(self):
        self.register()
        order = self.place()
        self.post(self.ops, '/api/orders/%s/dispatch' % order['orderId'])
        sent = len(self.twilio.created())
        response = self.post(self.ops, '/api/orders/%s/dispatch' % order['orderId'])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.twilio.created()), sent)

    def test_cancel_calls_off_follow_up_so_it_never_arrives(self):
        self.register()
        order = self.place()
        self.post(self.ops, '/api/orders/%s/dispatch' % order['orderId'])
        response = self.post(self.ops, '/api/orders/%s/cancel' % order['orderId'])
        self.assertEqual(response.status_code, 200)
        by_kind = self.notifications(order['orderId'])
        follow_up = by_kind[Notification.KIND_FOLLOW_UP]
        self.assertEqual(follow_up['providerStatus'], 'canceled')
        self.assertFalse(follow_up['cancelNotConfirmed'])
        self.assertEqual(by_kind[Notification.KIND_CANCELLED]['sendState'], 'sent')
        self.twilio.fire_scheduled(follow_up['providerSid'])  # the scheduled time passes
        self.assertEqual(self.twilio.messages[follow_up['providerSid']]['status'], 'canceled')

    def test_operator_actions_require_staff(self):
        order = self.place()
        self.assertEqual(self.post(self.shop, '/api/orders/%s/dispatch' % order['orderId']).status_code, 403)
        self.assertEqual(self.post(self.shop, '/api/orders/%s/cancel' % order['orderId']).status_code, 403)
        self.assertEqual(self.shop.get('/api/notifications/reconciliation?from=2020-01-01T00:00:00Z'
                                       '&to=2020-01-02T00:00:00Z').status_code, 403)

    def test_orders_are_scoped_to_owner(self):
        order = self.place()
        other = Client()
        other.force_login(self.other)
        self.assertEqual(other.get('/api/orders/%s/notifications' % order['orderId']).status_code, 404)
        self.assertEqual(other.get('/api/my-orders').json()['orders'], [])
        self.assertEqual(len(self.shop.get('/api/my-orders').json()['orders']), 1)

    def test_provider_rejection_does_not_fail_the_order(self):
        self.register()
        self.twilio.reject_to.add(CA_NUMBER)
        order = self.place()
        placed = self.notifications(order['orderId'])[Notification.KIND_PLACED]
        self.assertEqual(placed['sendState'], 'rejected')
        self.assertEqual(placed['errorCode'], 21211)
        self.assertNotIn(CA_NUMBER, placed['errorMessage'])  # provider echoes the number; we scrub it

    def test_refused_connection_is_known_not_sent(self):
        self.register()
        self.twilio.raise_on_send = httpx.ConnectError('refused')
        order = self.place()
        self.assertEqual(self.notifications(order['orderId'])[Notification.KIND_PLACED]['sendState'], 'rejected')

    def test_read_timeout_is_unknown_outcome(self):
        self.register()
        self.twilio.raise_on_send = httpx.ReadTimeout('no reply')
        order = self.place()
        self.assertEqual(self.notifications(order['orderId'])[Notification.KIND_PLACED]['sendState'], 'unknown')

    def test_status_refreshed_from_provider(self):
        self.register(number=US_NUMBER)
        order = self.place()
        sid = Notification.objects.get().provider_sid
        self.twilio.set_status(sid, 'undelivered', 30034)
        orders = self.shop.get('/api/my-orders').json()['orders']
        notification = orders[0]['notifications'][0]
        self.assertEqual(notification['providerStatus'], 'undelivered')
        self.assertEqual(notification['errorCode'], 30034)
        self.assertEqual(orders[0]['orderId'], order['orderId'])


class OperatorTests(NotificationApiTestCase):

    def undelivered_notification(self):
        self.register(number=US_NUMBER)
        order = self.place()
        notification = Notification.objects.get(order_id=order['orderId'])
        self.twilio.set_status(notification.provider_sid, 'undelivered', 30034)
        return notification

    def test_resend_is_idempotent_per_key(self):
        original = self.undelivered_notification()
        url = '/api/notifications/%s/resend' % original.pk
        first = self.post(self.ops, url, HTTP_IDEMPOTENCY_KEY='key-1')
        self.assertEqual(first.status_code, 201, first.content)
        self.assertNotEqual(first.json()['notificationId'], original.pk)
        sent = len(self.twilio.created())
        again = self.post(self.ops, url, HTTP_IDEMPOTENCY_KEY='key-1')
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()['notificationId'], first.json()['notificationId'])
        self.assertEqual(len(self.twilio.created()), sent)
        fresh = self.post(self.ops, url, {'idempotencyKey': 'key-2'})
        self.assertEqual(fresh.status_code, 201)
        self.assertEqual(len(self.twilio.created()), sent + 1)

    def test_resend_refused_for_delivered_message(self):
        self.register()
        order = self.place()
        notification = Notification.objects.get(order_id=order['orderId'])
        self.twilio.set_status(notification.provider_sid, 'delivered')
        response = self.post(self.ops, '/api/notifications/%s/resend' % notification.pk, HTTP_IDEMPOTENCY_KEY='k')
        self.assertEqual(response.status_code, 409)

    def test_resend_requires_key(self):
        original = self.undelivered_notification()
        response = self.post(self.ops, '/api/notifications/%s/resend' % original.pk)
        self.assertEqual(response.status_code, 400)

    def test_failed_resend_releases_the_key(self):
        original = self.undelivered_notification()
        self.twilio.reject_to.add(US_NUMBER)
        url = '/api/notifications/%s/resend' % original.pk
        self.assertEqual(self.post(self.ops, url, HTTP_IDEMPOTENCY_KEY='k').status_code, 502)
        self.twilio.reject_to.clear()
        self.assertEqual(self.post(self.ops, url, HTTP_IDEMPOTENCY_KEY='k').status_code, 201)

    def test_dispose_content_erases_it_at_provider(self):
        self.register()
        order = self.place()
        notification = Notification.objects.get(order_id=order['orderId'])
        self.twilio.set_status(notification.provider_sid, 'delivered')
        response = self.ops.delete('/api/notifications/%s/content' % notification.pk)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.json()['content'])
        self.assertEqual(response.json()['providerStatus'], 'delivered')  # what became of it survives
        self.assertEqual(self.twilio.messages[notification.provider_sid]['body'], '')
        self.assertEqual(self.notifications(order['orderId'])[Notification.KIND_PLACED]['content'], None)

    def test_dispose_refused_while_in_flight(self):
        self.register()
        order = self.place()
        notification = Notification.objects.get(order_id=order['orderId'])
        response = self.ops.delete('/api/notifications/%s/content' % notification.pk)
        self.assertEqual(response.status_code, 409)
        self.assertNotEqual(self.twilio.messages[notification.provider_sid]['body'], '')

    def test_reconciliation_asks_for_our_number_and_lines_up_both_sides(self):
        self.register()
        self.twilio.page_size_override = 1  # force pagination
        order = self.place()
        ours = Notification.objects.get(order_id=order['orderId'])
        # A message the provider knows about and the app does not (another sender is not ours).
        now = format_datetime(datetime.now(timezone.utc), usegmt=True)
        self.twilio.messages['SMforeign'] = dict(self.twilio.messages[ours.provider_sid], sid='SMforeign',
                                                 **{'from': OTHER_SENDER, 'date_sent': now})
        self.twilio.messages['SMunknown'] = dict(self.twilio.messages[ours.provider_sid], sid='SMunknown',
                                                 date_sent=now)
        start = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        end = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        response = self.ops.get('/api/notifications/reconciliation', {'from': start, 'to': end})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual([m['providerSid'] for m in report['matched']], [ours.provider_sid])
        self.assertEqual([m['providerSid'] for m in report['providerOnly']], ['SMunknown'])
        self.assertEqual(report['appOnly'], [])
        lists = [r for r in self.twilio.requests if r.method == 'GET' and r.url.split('?')[0].endswith(
            '/Messages.json')]
        self.assertEqual(len(lists), 2)  # both pages were read
        query = parse_qs(urlsplit(lists[0].url).query)
        self.assertEqual(query['From'], [FROM_NUMBER])  # the provider filters by our number
        self.assertIn('DateSent>', query)
        self.assertIn('DateSent<', query)

    def test_reconciliation_reports_app_only_message(self):
        self.register()
        order = self.place()
        ours = Notification.objects.get(order_id=order['orderId'])
        del self.twilio.messages[ours.provider_sid]
        start = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        end = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        report = self.ops.get('/api/notifications/reconciliation', {'from': start, 'to': end}).json()
        self.assertEqual([m['notificationId'] for m in report['appOnly']], [ours.pk])

    def test_reconciliation_validates_range(self):
        self.assertEqual(self.ops.get('/api/notifications/reconciliation?from=nope&to=nope').status_code, 400)


class GatewayTests(TestCase):

    def test_base_url_override_applies_to_messaging_only(self):
        fake = FakeTwilio()
        gateway = TwilioGateway(account_sid=ACCOUNT_SID, auth_token='t', from_number=FROM_NUMBER,
                                messaging_service_sid=SERVICE_SID, base_url='http://127.0.0.1:9/mock',
                                http_client=fake)
        with self.assertRaises(ProviderError):
            gateway.fetch('SM1')  # the fake does not serve this host's path prefix
        self.assertTrue(fake.requests[-1].url.startswith('http://127.0.0.1:9/mock/2010-04-01/'))
        gateway.lookup_number(CA_NUMBER)
        self.assertTrue(fake.requests[-1].url.startswith('https://lookups.twilio.com/'))

    def test_credentials_rejected_maps_to_502(self):
        class Unauthorized(FakeTwilio):
            def send(self, request):
                return json_response(401, {'code': 20003, 'message': 'Authenticate'})
        gateway = TwilioGateway(account_sid=ACCOUNT_SID, auth_token='t', from_number=FROM_NUMBER,
                                messaging_service_sid=SERVICE_SID, http_client=Unauthorized())
        with self.assertRaises(ProviderError) as ctx:
            gateway.fetch('SM1')
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(ctx.exception.provider_code, 20003)

    def test_missing_settings_refused(self):
        with self.assertRaises(twilio_gateway.GatewayNotConfigured):
            TwilioGateway(account_sid='', auth_token='', from_number='', messaging_service_sid='')


class SessionTests(TestCase):

    def test_session_login_with_csrf(self):
        get_user_model().objects.create_user('shopper', 'shopper@example.com', 'x-password-1')
        client = Client(enforce_csrf_checks=True)
        token = client.get('/api/session/csrf').json()['csrfToken']
        refused = client.post('/api/session/login', json.dumps({'username': 'shopper', 'password': 'x-password-1'}),
                              content_type='application/json')
        self.assertEqual(refused.status_code, 403)
        response = client.post('/api/session/login', json.dumps({'username': 'shopper', 'password': 'x-password-1'}),
                               content_type='application/json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get('/api/contact-numbers').status_code, 200)
