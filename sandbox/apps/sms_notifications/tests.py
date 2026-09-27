"""
API tests for SMS order notifications.

The real Twilio SDK client runs against a stub transport (the SDK's own test
seam), so every request is built by the SDK exactly as it would go on the wire.
"""
import json
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal as D
from urllib.parse import parse_qs, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from oscar.test.factories import create_product
from twilio_sdk.core import FormBody, HttpRequest, HttpResponse

from . import twilio_client
from .models import ContactNumber, Notification, ProviderWrite
from .safe_write import cancel_outcome, ref_token, status_from_provider
from .views import answer_status

ACCOUNT = 'AC' + '0' * 32
FROM = '+15550000001'
SHOPPER_NUMBER = '+14165550123'
OTHER_NUMBER = '+14165550999'

TWILIO_SETTINGS = dict(
    TWILIO_ACCOUNT_SID=ACCOUNT,
    TWILIO_AUTH_TOKEN='test-token',
    TWILIO_FROM_NUMBER=FROM,
    TWILIO_MESSAGING_SERVICE_SID='MG' + '0' * 32,
    TWILIO_BASE_URL=None,
    SMS_REFERENCE_PREFIX='test-install',
    SMS_FOLLOW_UP_DELAY_DAYS=3,
)


def json_response(status, body):
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


def rfc2822(dt):
    return dt.strftime('%a, %d %b %Y %H:%M:%S +0000')


def message(sid, status, *, body='x', to=SHOPPER_NUMBER, date_sent=None, error_code=None):
    now = datetime.now(dt_timezone.utc)
    return {
        'sid': sid, 'status': status, 'body': body, 'to': to, 'from': FROM,
        'date_created': rfc2822(now), 'date_updated': rfc2822(now),
        'date_sent': rfc2822(date_sent) if date_sent else None,
        'error_code': error_code, 'account_sid': ACCOUNT,
    }


def lookup(number, valid=True, errors=None):
    return {'phone_number': number, 'valid': valid, 'country_code': 'CA',
            'validation_errors': errors or [], 'calling_country_code': '1'}


class StubTransport:
    """
    The SDK's sync transport protocol: answers queued responses, raises queued
    errors, or calls a queued function with the request.
    """

    def __init__(self):
        self.queue = []
        self.requests = []

    def add(self, *items):
        self.queue.extend(items)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self.queue:
            raise AssertionError('unexpected provider call: %s %s' % (request.method, request.url))
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(request)
        return item

    def close(self):
        pass


def form(request):
    assert isinstance(request.body, FormBody)
    return request.body.fields


def query(request):
    return {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}


@override_settings(**TWILIO_SETTINGS)
class ApiTestCase(TestCase):

    def setUp(self):
        self.transport = StubTransport()
        twilio_client.set_client(twilio_client.build_client(custom_http_client=self.transport))
        User = get_user_model()
        self.shopper = User.objects.create_user('shopper', 'shopper@example.com', 'pw-shopper-1')
        self.other = User.objects.create_user('other', 'other@example.com', 'pw-other-1')
        self.staff = User.objects.create_user('op', 'op@example.com', 'pw-staff-1', is_staff=True)
        self.product = create_product(price=D('12.00'), num_in_stock=50)
        self.shopper_client = Client()
        self.shopper_client.force_login(self.shopper)
        self.other_client = Client()
        self.other_client.force_login(self.other)
        self.staff_client = Client()
        self.staff_client.force_login(self.staff)

    def tearDown(self):
        twilio_client.set_client(None)

    def post(self, client, url, data=None, **extra):
        return client.post(url, json.dumps(data or {}), content_type='application/json', **extra)

    def register(self, client=None, number=SHOPPER_NUMBER):
        self.transport.add(json_response(200, lookup(number)))
        response = self.post(client or self.shopper_client, '/api/contact-numbers',
                             {'phoneNumber': number})
        self.assertIn(response.status_code, (200, 201), response.content)
        return response.json()['contactNumberId']

    def place_order(self, client=None, send=None):
        if send is not None:
            self.transport.add(send)
        response = self.post(client or self.shopper_client, '/api/orders',
                             {'items': [{'productId': self.product.pk, 'quantity': 2}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()


class ContactNumberTests(ApiTestCase):

    def test_register_stores_the_providers_canonical_form(self):
        self.transport.add(json_response(200, lookup(SHOPPER_NUMBER)))
        response = self.post(self.shopper_client, '/api/contact-numbers',
                             {'phoneNumber': '(416) 555-0123', 'countryCode': 'CA'})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIn('contactNumberId', body)
        self.assertEqual(body['phoneNumber'], SHOPPER_NUMBER)
        request = self.transport.requests[0]
        self.assertEqual(request.method, 'GET')
        self.assertTrue(request.url.startswith('https://lookups.twilio.com/v2/PhoneNumbers/'))
        self.assertEqual(query(request)['CountryCode'], 'CA')
        self.assertTrue(request.headers['authorization'].startswith('Basic '))

    def test_a_number_the_provider_rejects_is_not_stored(self):
        self.transport.add(json_response(200, lookup(None, valid=False, errors=['TOO_SHORT'])))
        response = self.post(self.shopper_client, '/api/contact-numbers', {'phoneNumber': '12'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['validationErrors'], ['TOO_SHORT'])
        self.assertFalse(ContactNumber.objects.exists())

    def test_lookup_unavailable_is_not_a_rejection(self):
        self.transport.add(httpx.ConnectError('refused'))
        response = self.post(self.shopper_client, '/api/contact-numbers',
                             {'phoneNumber': SHOPPER_NUMBER})
        self.assertEqual(response.status_code, 502)
        self.assertFalse(ContactNumber.objects.exists())

    def test_numbers_are_private_to_their_owner(self):
        contact_id = self.register()
        self.assertEqual(self.other_client.get('/api/contact-numbers').json()['contactNumbers'], [])
        self.assertEqual(self.other_client.delete('/api/contact-numbers/%s' % contact_id).status_code, 404)
        self.assertEqual(len(self.shopper_client.get('/api/contact-numbers').json()['contactNumbers']), 1)

    def test_deleted_number_is_not_listed_and_not_messaged(self):
        contact_id = self.register()
        self.assertEqual(self.shopper_client.delete('/api/contact-numbers/%s' % contact_id).status_code, 204)
        self.assertEqual(self.shopper_client.get('/api/contact-numbers').json()['contactNumbers'], [])
        calls = len(self.transport.requests)
        order = self.place_order()
        self.assertIsNone(order['notification'])
        self.assertEqual(len(self.transport.requests), calls)

    def test_anonymous_callers_are_refused(self):
        self.assertEqual(Client().get('/api/contact-numbers').status_code, 401)

    def test_session_posts_need_the_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.shopper)
        response = self.post(client, '/api/contact-numbers', {'phoneNumber': SHOPPER_NUMBER})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.transport.requests, [])


class OrderPlacedTests(ApiTestCase):

    def test_placing_an_order_reuses_oscar_orders_and_texts_the_shopper(self):
        self.register()
        order = self.place_order(send=json_response(201, message('SM1', 'queued')))
        self.assertIn('orderId', order)
        from oscar.apps.order.models import Order
        placed = Order.objects.get(pk=order['orderId'])
        self.assertEqual(placed.user, self.shopper)
        self.assertEqual(placed.lines.get().quantity, 2)

        request = self.transport.requests[-1]
        self.assertEqual(request.method, 'POST')
        self.assertEqual(request.url,
                         'https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json' % ACCOUNT)
        fields = form(request)
        self.assertEqual((fields['To'], fields['From']), (SHOPPER_NUMBER, FROM))
        reference = 'test-install:order:%s:order_placed' % placed.pk
        self.assertIn('Ref %s' % ref_token(reference), fields['Body'])
        self.assertNotIn('ScheduleType', fields)

        notification = order['notification']
        self.assertEqual((notification['status'], notification['messageSid']), ('pending', 'SM1'))
        self.assertEqual(ProviderWrite.objects.get(reference=reference).outcome, 'pending')

    def test_no_number_on_file_means_no_message(self):
        order = self.place_order()
        self.assertIsNone(order['notification'])
        self.assertEqual(self.transport.requests, [])

    def test_a_refused_connection_still_places_the_order(self):
        self.register()
        order = self.place_order(send=httpx.ConnectError('refused'))
        self.assertEqual(order['notification']['status'], 'failed')
        self.assertIsNone(order['notification']['messageSid'])

    def test_a_timeout_is_looked_up_by_reference_not_resent(self):
        self.register()

        def found(request):
            # Built when the lookup arrives: the token is only known once the claim exists.
            token = ProviderWrite.objects.get(operation='send').notification.ref_token
            return json_response(200, {'messages': [message('SM9', 'sent', body='hi Ref %s' % token)],
                                       'next_page_uri': None})
        self.transport.add(httpx.ReadTimeout('no reply'), found)
        order = self.place_order()
        creates = [r for r in self.transport.requests if r.method == 'POST']
        self.assertEqual(len(creates), 1)
        self.assertEqual(order['notification']['status'], 'pending')
        self.assertEqual(order['notification']['messageSid'], 'SM9')
        self.assertEqual(query(self.transport.requests[-1])['To'], SHOPPER_NUMBER)

    def test_a_timeout_that_cannot_be_found_is_unknown_not_failed(self):
        self.register()
        self.transport.add(httpx.ReadTimeout('no reply'),
                           json_response(200, {'messages': [], 'next_page_uri': None}))
        order = self.place_order()
        self.assertEqual(order['notification']['status'], 'unknown')

    def test_a_2xx_without_a_sid_is_unknown(self):
        self.register()
        self.transport.add(json_response(201, {'status': 'queued'}),
                           json_response(200, {'messages': [], 'next_page_uri': None}))
        order = self.place_order()
        self.assertEqual(order['notification']['status'], 'unknown')

    def test_orders_are_private_to_their_owner(self):
        order = self.place_order()
        url = '/api/orders/%s/notifications' % order['orderId']
        self.assertEqual(self.other_client.get(url).status_code, 404)
        self.assertEqual(self.shopper_client.get(url).status_code, 200)
        self.assertEqual(self.other_client.get('/api/my-orders').json()['orders'], [])

    def test_my_orders_reads_delivery_state_back_from_the_provider(self):
        self.register()
        order = self.place_order(send=json_response(201, message('SM1', 'queued')))
        self.transport.add(json_response(200, message('SM1', 'delivered')))
        orders = self.shopper_client.get('/api/my-orders').json()['orders']
        self.assertEqual(orders[0]['orderId'], order['orderId'])
        self.assertEqual(orders[0]['notifications'][0]['status'], 'done')
        self.assertEqual(orders[0]['notifications'][0]['providerStatus'], 'delivered')
        self.assertTrue(self.transport.requests[-1].url.endswith('/Messages/SM1.json'))


class DispatchAndCancelTests(ApiTestCase):

    def dispatch(self, order_id):
        self.transport.add(json_response(201, message('SM2', 'queued')),
                           json_response(201, message('SM3', 'scheduled')))
        response = self.post(self.staff_client, '/api/orders/%s/dispatch' % order_id)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_only_staff_dispatch_or_cancel(self):
        order = self.place_order()
        for action in ('dispatch', 'cancel'):
            response = self.post(self.shopper_client, '/api/orders/%s/%s' % (order['orderId'], action))
            self.assertEqual(response.status_code, 403)

    def test_dispatch_texts_now_and_queues_the_follow_up_with_the_provider(self):
        self.register()
        order = self.place_order(send=json_response(201, message('SM1', 'queued')))
        before = datetime.now(dt_timezone.utc)
        body = self.dispatch(order['orderId'])
        self.assertEqual(body['status'], 'Dispatched')
        kinds = [(n['kind'], n['status'], n['providerStatus']) for n in body['notifications']]
        self.assertEqual(kinds, [('order_dispatched', 'pending', 'queued'),
                                 ('delivery_follow_up', 'pending', 'scheduled')])
        follow_up = form(self.transport.requests[-1])
        self.assertEqual(follow_up['ScheduleType'], 'fixed')
        self.assertEqual(follow_up['MessagingServiceSid'], TWILIO_SETTINGS['TWILIO_MESSAGING_SERVICE_SID'])
        self.assertEqual(follow_up['From'], FROM)
        send_at = datetime.fromisoformat(follow_up['SendAt'].replace('Z', '+00:00'))
        self.assertGreaterEqual(send_at, before + timedelta(days=3))

    def test_cancel_calls_off_the_scheduled_follow_up_before_telling_the_shopper(self):
        self.register()
        order = self.place_order(send=json_response(201, message('SM1', 'queued')))
        self.dispatch(order['orderId'])
        self.transport.add(json_response(200, message('SM3', 'canceled')),
                           json_response(201, message('SM4', 'queued')))
        response = self.post(self.staff_client, '/api/orders/%s/cancel' % order['orderId'])
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body['status'], 'Cancelled')
        self.assertEqual(body['followUpCancellation'][0]['outcome'], 'done')
        cancel_request = self.transport.requests[-2]
        self.assertTrue(cancel_request.url.endswith('/Messages/SM3.json'))
        self.assertEqual(form(cancel_request), {'Status': 'canceled'})
        self.assertEqual(body['notification']['kind'], 'order_cancelled')
        follow_up = Notification.objects.get(kind=Notification.FOLLOW_UP)
        self.assertEqual(follow_up.send_write.provider_status, 'canceled')
        assert follow_up.cancel_write is not None
        self.assertEqual(follow_up.cancel_write.outcome, 'done')

    def test_a_refused_cancel_does_not_fail_the_order_cancel(self):
        self.register()
        order = self.place_order(send=json_response(201, message('SM1', 'queued')))
        self.dispatch(order['orderId'])
        self.transport.add(json_response(400, {'code': 21000, 'message': 'no'}),
                           json_response(200, message('SM3', 'delivered')),
                           json_response(201, message('SM4', 'queued')))
        body = self.post(self.staff_client, '/api/orders/%s/cancel' % order['orderId']).json()
        self.assertEqual(body['status'], 'Cancelled')
        self.assertEqual(body['followUpCancellation'][0]['outcome'], 'failed')

    def test_a_cancelled_order_cannot_be_dispatched(self):
        order = self.place_order()
        self.post(self.staff_client, '/api/orders/%s/cancel' % order['orderId'])
        response = self.post(self.staff_client, '/api/orders/%s/dispatch' % order['orderId'])
        self.assertEqual(response.status_code, 409)

    def test_removing_the_number_calls_off_its_scheduled_follow_up(self):
        contact_id = self.register()
        order = self.place_order(send=json_response(201, message('SM1', 'queued')))
        self.dispatch(order['orderId'])
        self.transport.add(json_response(200, message('SM3', 'canceled')))
        self.assertEqual(self.shopper_client.delete('/api/contact-numbers/%s' % contact_id).status_code, 204)
        self.assertEqual(form(self.transport.requests[-1]), {'Status': 'canceled'})


class OperatorTests(ApiTestCase):

    def failed_notification(self):
        self.register()
        order = self.place_order(send=json_response(201, message('SM1', 'undelivered', error_code=30007)))
        self.assertEqual(order['notification']['status'], 'failed')
        return order['notification']['notificationId']

    def resend(self, notification_id, key):
        return self.post(self.staff_client, '/api/notifications/%s/resend' % notification_id,
                         {'idempotencyKey': key})

    def test_resend_is_staff_only(self):
        notification_id = self.failed_notification()
        response = self.post(self.shopper_client, '/api/notifications/%s/resend' % notification_id,
                             {'idempotencyKey': 'key-00000001'})
        self.assertEqual(response.status_code, 403)

    def test_the_same_idempotency_key_sends_once(self):
        notification_id = self.failed_notification()
        self.transport.add(json_response(201, message('SM5', 'queued')))
        first = self.resend(notification_id, 'key-00000001')
        self.assertEqual(first.status_code, 202)
        second = self.resend(notification_id, 'key-00000001')
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json()['notificationId'], second.json()['notificationId'])
        creates = [r for r in self.transport.requests if r.method == 'POST']
        self.assertEqual(len(creates), 2)        # the original send and ONE resend
        self.assertNotEqual(first.json()['notificationId'], notification_id)

    def test_a_fresh_key_is_a_second_legitimate_attempt(self):
        notification_id = self.failed_notification()
        self.transport.add(json_response(201, message('SM5', 'undelivered')),
                           json_response(201, message('SM6', 'queued')))
        first = self.resend(notification_id, 'key-00000001').json()
        second = self.resend(notification_id, 'key-00000002').json()
        self.assertNotEqual(first['notificationId'], second['notificationId'])
        self.assertEqual(first['status'], 'failed')
        self.assertEqual(second['status'], 'pending')

    def test_a_message_that_arrived_is_not_resent(self):
        self.register()
        order = self.place_order(send=json_response(201, message('SM1', 'queued')))
        self.transport.add(json_response(200, message('SM1', 'delivered')))
        response = self.resend(order['notification']['notificationId'], 'key-00000001')
        self.assertEqual(response.status_code, 409)

    def test_a_resend_needs_a_key(self):
        notification_id = self.failed_notification()
        self.assertEqual(self.resend(notification_id, '').status_code, 400)

    def test_content_disposal_redacts_at_the_provider_and_keeps_the_outcome(self):
        notification_id = self.failed_notification()
        self.transport.add(json_response(200, message('SM1', 'undelivered', body='')))
        response = self.staff_client.delete('/api/notifications/%s/content' % notification_id)
        self.assertEqual(response.status_code, 200, response.content)
        request = self.transport.requests[-1]
        self.assertTrue(request.url.endswith('/Messages/SM1.json'))
        self.assertEqual(form(request), {'Body': ''})
        body = response.json()['notification']
        self.assertIsNone(body['body'])
        self.assertTrue(body['contentRedacted'])
        self.assertEqual((body['status'], body['messageSid']), ('failed', 'SM1'))
        self.assertEqual(Notification.objects.get(pk=notification_id).body, '')

    def test_a_redaction_the_provider_did_not_apply_is_not_done(self):
        notification_id = self.failed_notification()
        self.transport.add(json_response(200, message('SM1', 'undelivered', body='still here')))
        response = self.staff_client.delete('/api/notifications/%s/content' % notification_id)
        self.assertEqual(response.status_code, 409)
        self.assertNotEqual(Notification.objects.get(pk=notification_id).body, '')
        # Redaction is idempotent, so the operator can try again.
        self.transport.add(json_response(200, message('SM1', 'undelivered', body='')))
        retry = self.staff_client.delete('/api/notifications/%s/content' % notification_id)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(Notification.objects.get(pk=notification_id).body, '')

    def test_reconciliation_asks_for_our_numbers_messages_and_lines_them_up(self):
        self.register()
        now = datetime.now(dt_timezone.utc).replace(microsecond=0)
        self.place_order(send=json_response(201, message('SM1', 'sent', date_sent=now)))
        self.place_order(send=json_response(201, message('SM2', 'sent', date_sent=now)))
        start, end = now - timedelta(hours=1), now + timedelta(hours=1)
        self.transport.add(json_response(200, {
            'messages': [message('SM1', 'delivered', date_sent=now),
                         message('SMX', 'delivered', date_sent=now),
                         message('SMOLD', 'delivered', date_sent=now - timedelta(days=1))],
            'next_page_uri': None}))
        response = self.staff_client.get('/api/notifications/reconciliation', {
            'from': start.isoformat(), 'to': end.isoformat()})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        params = query(self.transport.requests[-1])
        self.assertEqual(params['From'], FROM)
        self.assertIn('DateSent>', params)
        self.assertIn('DateSent<', params)
        self.assertEqual([m['messageSid'] for m in report['matched']], ['SM1'])
        self.assertTrue(report['matched'][0]['statusDiffers'])
        self.assertEqual([m['messageSid'] for m in report['providerOnly']], ['SMX'])
        self.assertEqual([m['messageSid'] for m in report['localOnly']], ['SM2'])

    def test_reconciliation_follows_every_page(self):
        now = datetime.now(dt_timezone.utc)
        self.transport.add(
            json_response(200, {'messages': [message('SMA', 'sent', date_sent=now)],
                                'next_page_uri': '/2010-04-01/Accounts/%s/Messages.json?PageSize=1000'
                                                 '&Page=1&PageToken=PAabc' % ACCOUNT}),
            json_response(200, {'messages': [message('SMB', 'sent', date_sent=now)],
                                'next_page_uri': None}))
        report = self.staff_client.get('/api/notifications/reconciliation', {
            'from': (now - timedelta(hours=1)).isoformat(),
            'to': (now + timedelta(hours=1)).isoformat()}).json()
        self.assertEqual(report['providerCount'], 2)
        self.assertEqual(query(self.transport.requests[-1])['PageToken'], 'PAabc')

    def test_reconciliation_is_staff_only_and_validates_dates(self):
        self.assertEqual(self.shopper_client.get('/api/notifications/reconciliation').status_code, 403)
        self.assertEqual(self.staff_client.get('/api/notifications/reconciliation',
                                               {'from': 'x', 'to': 'y'}).status_code, 400)


class OutcomeMappingTests(TestCase):

    def test_statuses_map_to_done_pending_failed_unknown(self):
        self.assertEqual(status_from_provider('delivered'), 'done')
        for status in ('accepted', 'queued', 'sending', 'sent', 'scheduled'):
            self.assertEqual(status_from_provider(status), 'pending')
        for status in ('failed', 'undelivered', 'canceled'):
            self.assertEqual(status_from_provider(status), 'failed')
        self.assertEqual(status_from_provider('something_new'), 'unknown')
        self.assertEqual(status_from_provider(None), 'unknown')

    def test_cancel_has_its_own_mapping(self):
        self.assertEqual(cancel_outcome('canceled'), 'done')
        self.assertEqual(cancel_outcome('delivered'), 'failed')
        self.assertEqual(cancel_outcome('scheduled'), 'pending')

    def test_nothing_but_done_answers_success(self):
        for outcome in ('pending', 'sending', 'failed', 'needs_review', 'unknown', 'whatever'):
            self.assertNotEqual(answer_status(outcome), 200)
        self.assertEqual(answer_status('done'), 200)


@override_settings(**dict(TWILIO_SETTINGS, TWILIO_BASE_URL='http://messaging.example.test'))
class BaseUrlTests(ApiTestCase):

    def test_base_url_governs_messaging_calls_but_not_lookup(self):
        twilio_client.set_client(twilio_client.build_client(custom_http_client=self.transport))
        self.register()
        self.place_order(send=json_response(201, message('SM1', 'queued')))
        lookup_request, create_request = self.transport.requests
        self.assertTrue(lookup_request.url.startswith('https://lookups.twilio.com/'))
        self.assertTrue(create_request.url.startswith(
            'http://messaging.example.test/2010-04-01/Accounts/'))
