"""
Tests for SMS order notifications.

The Twilio SDK client is real; only its transport is faked (``StubTwilio``),
so every request is built by the SDK exactly as in production. No test talks
to the network. Run with: ``sandbox/manage.py test apps.sms_notifications``.
"""
import json
from datetime import timedelta
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from oscar.test.factories import create_product
from twilio_sdk.core import FormBody, HttpRequest, HttpResponse

from . import outcomes, sending, twilio_client
from .models import ContactNumber, Notification

FROM = '+15550001111'
SHOPPER_NUMBER = '+16045550123'
OTHER_NUMBER = '+16045550999'

TWILIO_SETTINGS = dict(
    TWILIO_ACCOUNT_SID='AC_test_account',
    TWILIO_AUTH_TOKEN='test-token',
    TWILIO_FROM_NUMBER=FROM,
    TWILIO_MESSAGING_SERVICE_SID='MG_test_service',
    TWILIO_BASE_URL='',
    SMS_NOTIFICATIONS_INSTALL_PREFIX='test-install',
)


def json_response(status, body):
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


def message_json(sid, status, body='', to=SHOPPER_NUMBER, date_sent=None,
                 date_created='Wed, 23 Sep 2026 10:00:00 +0000', direction='outbound-api'):
    return {'sid': sid, 'status': status, 'body': body, 'to': to, 'from': FROM,
            'direction': direction, 'date_sent': date_sent, 'date_created': date_created,
            'error_code': 30034 if status == 'undelivered' else None, 'error_message': None}


class StubTwilio:
    """
    A fake transport for the SDK: routes each request to the answer queued for
    its method and path, and records every request it was given.
    """

    def __init__(self):
        self.requests: list[HttpRequest] = []
        self.routes: dict[tuple[str, str], list] = {}
        self.created = 0

    def on(self, method, path_suffix, *answers):
        self.routes.setdefault((method, path_suffix), []).extend(answers)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = unquote(urlsplit(request.url).path)
        for (method, suffix), answers in self.routes.items():
            if method == request.method and path.endswith(suffix) and answers:
                answer = answers.pop(0) if len(answers) > 1 else answers[0]
                if isinstance(answer, BaseException):
                    raise answer
                if callable(answer):
                    return answer(request)
                return answer
        raise AssertionError('unexpected request %s %s' % (request.method, path))

    def close(self):
        pass

    # --- helpers for assertions -------------------------------------------
    def calls(self, method, path_suffix):
        return [r for r in self.requests
                if r.method == method and unquote(urlsplit(r.url).path).endswith(path_suffix)]

    def creates(self):
        return self.calls('POST', '/Messages.json')

    @staticmethod
    def form(request):
        assert isinstance(request.body, FormBody)
        return request.body.fields

    @staticmethod
    def query(request):
        return parse_qs(urlsplit(request.url).query)


def created_message(status='queued', sid_prefix='SM'):
    """A create_message answer echoing the request's body, with a fresh sid."""
    counter = {'n': 0}

    def answer(request):
        counter['n'] += 1
        fields = StubTwilio.form(request)
        return json_response(201, message_json(
            '%s%04d' % (sid_prefix, counter['n']), status, body=fields['Body'], to=fields['To']))
    return answer


@override_settings(**TWILIO_SETTINGS)
class ApiTestCase(TestCase):
    def setUp(self):
        self.stub = StubTwilio()
        twilio_client.set_client(twilio_client.build_client(self.stub))
        self.addCleanup(twilio_client.set_client, None)
        User = get_user_model()
        self.shopper = User.objects.create_user('shopper', 'shopper@example.com', 'pw-shopper-1')
        self.other = User.objects.create_user('other', 'other@example.com', 'pw-other-1')
        self.staff = User.objects.create_user('operator', 'op@example.com', 'pw-staff-1', is_staff=True)
        self.product = create_product(price=10, num_in_stock=100)

    def as_user(self, user):
        self.client.force_login(user)

    def add_number(self, user=None, number=SHOPPER_NUMBER):
        return ContactNumber.objects.create(user=user or self.shopper, phone_number=number,
                                            country_code='CA')

    def place_order(self):
        self.as_user(self.shopper)
        response = self.client.post('/api/orders', {'lines': [{'productId': self.product.pk, 'quantity': 2}]},
                                    content_type='application/json')
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()


class StatusMappingTests(TestCase):
    def test_every_listed_status_has_its_outcome(self):
        self.assertEqual(outcomes.status_from_provider('delivered'), 'done')
        self.assertEqual(outcomes.status_from_provider('sent'), 'pending')
        self.assertEqual(outcomes.status_from_provider('scheduled'), 'pending')
        self.assertEqual(outcomes.status_from_provider('undelivered'), 'failed')
        self.assertEqual(outcomes.status_from_provider('canceled'), 'failed')

    def test_unlisted_or_missing_status_is_unknown_not_done(self):
        self.assertEqual(outcomes.status_from_provider('something_new'), 'unknown')
        self.assertEqual(outcomes.status_from_provider(None), 'unknown')
        self.assertEqual(outcomes.status_from_provider('partially_delivered'), 'unknown')

    def test_cancel_is_done_only_when_canceled(self):
        self.assertEqual(outcomes.cancel_outcome('canceled'), 'done')
        self.assertEqual(outcomes.cancel_outcome('scheduled'), 'pending')
        self.assertEqual(outcomes.cancel_outcome('delivered'), 'failed')
        self.assertEqual(outcomes.cancel_outcome('mystery'), 'unknown')


class ContactNumberTests(ApiTestCase):
    def test_register_stores_the_providers_canonical_form(self):
        self.stub.on('GET', '/v1/PhoneNumbers/(604) 555-0123', json_response(
            200, {'phone_number': SHOPPER_NUMBER, 'country_code': 'CA'}))
        self.as_user(self.shopper)
        response = self.client.post('/api/contact-numbers', {'phoneNumber': '(604) 555-0123'},
                                    content_type='application/json')
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['phoneNumber'], SHOPPER_NUMBER)
        self.assertEqual(ContactNumber.objects.get(pk=body['contactNumberId']).phone_number, SHOPPER_NUMBER)
        lookup = self.stub.requests[0]
        self.assertTrue(lookup.url.startswith('https://lookups.twilio.com/v1/PhoneNumbers/'))

    def test_a_number_the_provider_rejects_is_not_stored(self):
        self.stub.on('GET', '/v1/PhoneNumbers/12', json_response(404, {'code': 20404, 'status': 404}))
        self.as_user(self.shopper)
        response = self.client.post('/api/contact-numbers', {'phoneNumber': '12'},
                                    content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['code'], 'invalid_phone_number')
        self.assertFalse(ContactNumber.objects.exists())

    def test_lookup_transport_failures_are_not_the_callers_fault(self):
        self.stub.on('GET', '/v1/PhoneNumbers/+16045550123', httpx.ConnectError('refused'))
        self.as_user(self.shopper)
        response = self.client.post('/api/contact-numbers', {'phoneNumber': SHOPPER_NUMBER},
                                    content_type='application/json')
        self.assertEqual(response.status_code, 502)
        self.assertFalse(ContactNumber.objects.exists())

    def test_numbers_are_private_to_their_owner(self):
        contact = self.add_number()
        self.as_user(self.other)
        self.assertEqual(self.client.get('/api/contact-numbers').json()['contactNumbers'], [])
        self.assertEqual(self.client.delete('/api/contact-numbers/%s' % contact.pk).status_code, 404)
        self.as_user(self.shopper)
        self.assertEqual(len(self.client.get('/api/contact-numbers').json()['contactNumbers']), 1)

    def test_deleted_number_is_not_listed_or_messaged_and_its_followup_is_called_off(self):
        contact = self.add_number()
        self.stub.on('POST', '/Messages.json', created_message('queued'))
        order = self.place_order()
        followup = Notification.objects.create(
            user=self.shopper, order_id=order['orderId'], contact=contact,
            kind=Notification.KIND_DELIVERY_FOLLOWUP, reference='r-f', ref_token='tok-f',
            body='x', outcome=Notification.PENDING, provider_sid='SMF1', provider_status='scheduled',
            claimed_at=timezone.now())
        self.stub.on('POST', '/Messages/SMF1.json', json_response(200, message_json('SMF1', 'canceled')))

        response = self.client.delete('/api/contact-numbers/%s' % contact.pk)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['allFollowupsCalledOff'])
        self.assertEqual(self.stub.form(self.stub.calls('POST', '/Messages/SMF1.json')[0])['Status'], 'canceled')
        followup.refresh_from_db()
        self.assertEqual(followup.cancel_state, 'done')
        self.assertEqual(self.client.get('/api/contact-numbers').json()['contactNumbers'], [])
        # Nothing is sent to a removed number.
        creates_before = len(self.stub.creates())
        self.as_user(self.staff)
        self.client.post('/api/orders/%s/dispatch' % order['orderId'])
        self.assertEqual(len(self.stub.creates()), creates_before)


class OrderFlowTests(ApiTestCase):
    def test_placing_an_order_texts_the_shopper_once_with_its_reference(self):
        self.add_number()
        self.stub.on('POST', '/Messages.json', created_message('queued'))
        order = self.place_order()

        self.assertIn('orderId', order)
        [create] = self.stub.creates()
        fields = self.stub.form(create)
        reference = 'test-install:order:%s:order_placed:%s' % (
            order['orderId'], ContactNumber.objects.get().pk)
        self.assertEqual(fields['To'], SHOPPER_NUMBER)
        self.assertEqual(fields['From'], FROM)
        self.assertIn('(ref %s)' % sending.ref_token_for(reference), fields['Body'])
        self.assertEqual(create.headers['idempotency-key'], reference)
        self.assertTrue(create.url.startswith('https://api.twilio.com/2010-04-01/Accounts/AC_test_account/'))
        [note] = order['notifications']
        self.assertEqual(note['outcome'], 'pending')        # queued is not delivered
        self.assertEqual(note['providerSid'], 'SM0001')

    def test_shopper_without_a_number_is_not_messaged(self):
        self.place_order()
        self.assertEqual(self.stub.creates(), [])

    def test_a_message_that_cannot_be_sent_never_fails_the_order(self):
        self.add_number()
        self.stub.on('POST', '/Messages.json', httpx.ConnectError('refused'))
        order = self.place_order()
        [note] = Notification.objects.all()
        self.assertEqual(note.outcome, 'failed')
        self.assertEqual(note.provider_sid, '')
        self.assertEqual(order['notifications'][0]['outcome'], 'failed')

    def test_dispatch_texts_and_queues_the_followup_with_the_provider(self):
        self.add_number()
        self.stub.on('POST', '/Messages.json', created_message('queued'))
        order = self.place_order()
        self.stub.routes.clear()
        self.stub.on('POST', '/Messages.json', created_message('queued', 'SMD'),
                     created_message('scheduled', 'SMS'))
        self.as_user(self.staff)

        response = self.client.post('/api/orders/%s/dispatch' % order['orderId'])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'Dispatched')
        dispatched, followup = [self.stub.form(r) for r in self.stub.creates()[1:]]
        self.assertNotIn('ScheduleType', dispatched)
        self.assertEqual(followup['ScheduleType'], 'fixed')
        self.assertEqual(followup['MessagingServiceSid'], 'MG_test_service')
        self.assertEqual(followup['From'], FROM)
        self.assertIn('SendAt', followup)
        stored = Notification.objects.get(kind=Notification.KIND_DELIVERY_FOLLOWUP)
        self.assertEqual(stored.outcome, 'pending')
        self.assertGreater(stored.send_at, timezone.now() + timedelta(days=2))

    def test_the_same_dispatch_twice_sends_each_message_once(self):
        self.add_number()
        self.stub.on('POST', '/Messages.json', created_message('queued'))
        order = self.place_order()
        self.as_user(self.staff)
        first = self.client.post('/api/orders/%s/dispatch' % order['orderId']).json()
        second = self.client.post('/api/orders/%s/dispatch' % order['orderId']).json()

        self.assertEqual(len(self.stub.creates()), 3)       # placed + dispatched + follow-up
        self.assertEqual([n['notificationId'] for n in first['notifications']],
                         [n['notificationId'] for n in second['notifications']])

    def test_cancel_calls_off_the_queued_followup_and_tells_the_shopper(self):
        self.add_number()
        self.stub.on('POST', '/Messages.json', created_message('queued'))
        order = self.place_order()
        self.stub.routes.clear()
        self.stub.on('POST', '/Messages.json', created_message('queued', 'SMD'),
                     created_message('scheduled', 'SMF'), created_message('queued', 'SMC'))
        self.as_user(self.staff)
        self.client.post('/api/orders/%s/dispatch' % order['orderId'])
        followup = Notification.objects.get(kind=Notification.KIND_DELIVERY_FOLLOWUP)
        self.stub.on('POST', '/Messages/%s.json' % followup.provider_sid,
                     json_response(200, message_json(followup.provider_sid, 'canceled')))

        response = self.client.post('/api/orders/%s/cancel' % order['orderId'])

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['status'], 'Cancelled')
        self.assertTrue(body['allFollowupsCalledOff'])
        [cancel] = self.stub.calls('POST', '/Messages/%s.json' % followup.provider_sid)
        self.assertEqual(self.stub.form(cancel), {'Status': 'canceled'})
        followup.refresh_from_db()
        self.assertEqual((followup.cancel_state, followup.outcome), ('done', 'failed'))
        self.assertEqual(body['notifications'][0]['kind'], 'order_cancelled')

    def test_a_followup_that_already_went_out_is_reported_not_hidden(self):
        contact = self.add_number()
        self.stub.on('POST', '/Messages.json', created_message('queued'))
        order = self.place_order()
        Notification.objects.create(
            user=self.shopper, order_id=order['orderId'], contact=contact,
            kind=Notification.KIND_DELIVERY_FOLLOWUP, reference='r-late', ref_token='tok-late', body='x',
            outcome=Notification.PENDING, provider_sid='SMLATE', provider_status='scheduled',
            claimed_at=timezone.now())
        self.stub.on('POST', '/Messages/SMLATE.json', json_response(400, {'code': 30409}))
        self.stub.on('GET', '/Messages/SMLATE.json', json_response(200, message_json('SMLATE', 'delivered')))
        self.as_user(self.staff)

        body = self.client.post('/api/orders/%s/cancel' % order['orderId']).json()

        self.assertFalse(body['allFollowupsCalledOff'])
        self.assertEqual(body['followupsCalledOff'][0]['cancelState'], 'failed')

    def test_operator_actions_are_staff_only_and_orders_are_private(self):
        self.stub.on('POST', '/Messages.json', created_message('queued'))
        order = self.place_order()
        self.assertEqual(self.client.post('/api/orders/%s/dispatch' % order['orderId']).status_code, 403)
        self.assertEqual(self.client.post('/api/orders/%s/cancel' % order['orderId']).status_code, 403)
        self.as_user(self.other)
        self.assertEqual(self.client.get('/api/orders/%s/notifications' % order['orderId']).status_code, 404)
        self.assertEqual(self.client.get('/api/my-orders').json()['orders'], [])
        self.client.logout()
        self.assertEqual(self.client.get('/api/my-orders').status_code, 401)


class UnknownOutcomeTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.contact = self.add_number()

    def test_unsent_is_not_the_same_failure_as_unknown(self):
        self.stub.on('POST', '/Messages.json', httpx.ConnectError('refused'))
        self.place_order()
        unsent = Notification.objects.get()
        self.assertEqual(unsent.outcome, 'failed')
        self.assertEqual(self.stub.calls('GET', '/Messages.json'), [])   # nothing to look up

        Notification.objects.all().delete()
        self.stub.routes.clear()
        self.stub.on('POST', '/Messages.json', httpx.ReadTimeout('no reply'))
        self.stub.on('GET', '/Messages.json', json_response(200, {'messages': [], 'next_page_uri': None}))
        self.place_order()
        unknown = Notification.objects.get()
        self.assertEqual(unknown.outcome, 'unknown')                      # never "failed"
        [lookup] = self.stub.calls('GET', '/Messages.json')
        self.assertEqual(self.stub.query(lookup)['To'], [SHOPPER_NUMBER])
        self.assertEqual(self.stub.query(lookup)['From'], [FROM])

    def test_a_lost_answer_is_settled_by_the_reference_in_the_message(self):
        self.stub.on('POST', '/Messages.json', httpx.ReadTimeout('no reply'))

        def listing(request):
            note = Notification.objects.get()
            return json_response(200, {'messages': [
                message_json('SMOTHER', 'delivered', body='unrelated'),
                message_json('SMFOUND', 'sent', body='text (ref %s)' % note.ref_token)],
                'next_page_uri': None})
        self.stub.on('GET', '/Messages.json', listing)
        self.place_order()
        note = Notification.objects.get()
        self.assertEqual((note.outcome, note.provider_sid), ('pending', 'SMFOUND'))

    def test_an_unknown_message_is_never_sent_again_under_a_new_reference(self):
        self.stub.on('POST', '/Messages.json', created_message('queued'))
        order = self.place_order()
        self.stub.routes.clear()
        self.stub.on('POST', '/Messages.json', httpx.ReadTimeout('no reply'))
        self.stub.on('GET', '/Messages.json', json_response(200, {'messages': [], 'next_page_uri': None}))
        self.as_user(self.staff)
        self.client.post('/api/orders/%s/dispatch' % order['orderId'])
        creates = len(self.stub.creates())
        # Repeating the action only looks the unknown messages up again.
        self.client.post('/api/orders/%s/dispatch' % order['orderId'])
        self.assertEqual(len(self.stub.creates()), creates)

    def test_a_refused_send_releases_the_claim_for_the_next_attempt(self):
        self.stub.on('POST', '/Messages.json', created_message('queued'))
        order = self.place_order()
        self.stub.routes.clear()
        self.stub.on('POST', '/Messages.json', httpx.ConnectError('refused'))
        self.as_user(self.staff)
        self.client.post('/api/orders/%s/dispatch' % order['orderId'])
        self.stub.routes.clear()
        self.stub.on('POST', '/Messages.json', created_message('queued', 'SMR'))
        self.client.post('/api/orders/%s/dispatch' % order['orderId'])
        dispatched = Notification.objects.get(kind=Notification.KIND_DISPATCHED)
        self.assertEqual(dispatched.outcome, 'pending')
        self.assertTrue(dispatched.provider_sid.startswith('SMR'))


class OperatorTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.add_number()
        self.stub.on('POST', '/Messages.json', created_message('undelivered', 'SMU'))
        self.order = self.place_order()
        self.note_id = self.order['notifications'][0]['notificationId']
        self.stub.routes.clear()
        self.stub.requests.clear()
        self.as_user(self.staff)

    def resend(self, key):
        return self.client.post('/api/notifications/%s/resend' % self.note_id, HTTP_IDEMPOTENCY_KEY=key)

    def test_resend_is_idempotent_per_key(self):
        self.stub.on('POST', '/Messages.json', created_message('queued', 'SMN'))
        first = self.resend('key-1')
        repeat = self.resend('key-1')
        fresh = self.resend('key-2')

        self.assertEqual((first.status_code, repeat.status_code, fresh.status_code), (201, 200, 201))
        self.assertEqual(first.json()['notificationId'], repeat.json()['notificationId'])
        self.assertNotEqual(first.json()['notificationId'], fresh.json()['notificationId'])
        self.assertTrue(repeat.json()['replayed'])
        self.assertEqual(len(self.stub.creates()), 2)
        self.assertEqual(first.json()['resendOf'], self.note_id)

    def test_resend_requires_a_key_and_a_message_that_did_not_arrive(self):
        self.assertEqual(self.client.post('/api/notifications/%s/resend' % self.note_id).status_code, 400)
        Notification.objects.filter(pk=self.note_id).update(outcome='done', provider_status='delivered')
        self.assertEqual(self.resend('key-x').status_code, 409)
        self.assertEqual(self.stub.creates(), [])

    def test_disposing_of_content_redacts_it_at_the_provider_and_keeps_the_record(self):
        note = Notification.objects.get(pk=self.note_id)
        self.stub.on('POST', '/Messages/%s.json' % note.provider_sid,
                     json_response(200, message_json(note.provider_sid, 'undelivered', body='')))

        response = self.client.delete('/api/notifications/%s/content' % self.note_id)

        self.assertEqual(response.status_code, 200)
        [redact] = self.stub.calls('POST', '/Messages/%s.json' % note.provider_sid)
        self.assertEqual(self.stub.form(redact), {'Body': ''})
        body = response.json()
        self.assertIsNone(body['text'])
        self.assertEqual((body['providerSid'], body['providerStatus']), (note.provider_sid, 'undelivered'))
        note.refresh_from_db()
        self.assertEqual(note.body, '')

    def test_unconfirmed_redaction_is_reported_as_unknown(self):
        note = Notification.objects.get(pk=self.note_id)
        self.stub.on('POST', '/Messages/%s.json' % note.provider_sid, httpx.ReadTimeout('no reply'))
        self.stub.on('GET', '/Messages/%s.json' % note.provider_sid, httpx.ReadTimeout('no reply'))
        response = self.client.delete('/api/notifications/%s/content' % self.note_id)
        self.assertEqual(response.status_code, 504)
        self.assertTrue(response.json()['error']['outcomeUnknown'])

    def test_reconciliation_asks_for_this_senders_messages_and_lines_them_up(self):
        note = Notification.objects.get(pk=self.note_id)
        Notification.objects.filter(pk=note.pk).update(
            provider_time=timezone.now().replace(year=2026, month=9, day=23, hour=10, minute=0))
        page_two = ('/2010-04-01/Accounts/AC_test_account/Messages.json?From=%2B15550001111'
                    '&PageSize=1000&Page=1&PageToken=PA123')

        def listing(request):
            if 'PA123' in request.url:
                return json_response(200, {'messages': [
                    message_json('SMFOREIGN', 'delivered', date_sent='Wed, 23 Sep 2026 11:00:00 +0000'),
                    message_json('SMIN', 'received', date_sent='Wed, 23 Sep 2026 11:00:00 +0000',
                                 direction='inbound'),
                    message_json('SMLATE', 'delivered', date_sent='Thu, 24 Sep 2026 03:00:00 +0000'),
                ], 'next_page_uri': None})
            return json_response(200, {'messages': [
                message_json(note.provider_sid, 'undelivered', date_sent='Wed, 23 Sep 2026 10:00:00 +0000'),
            ], 'next_page_uri': page_two})
        self.stub.on('GET', '/Messages.json', listing)

        response = self.client.get('/api/notifications/reconciliation',
                                   {'from': '2026-09-23T00:00:00Z', 'to': '2026-09-24T00:00:00Z'})

        self.assertEqual(response.status_code, 200)
        report = response.json()
        first, second = self.stub.calls('GET', '/Messages.json')
        self.assertEqual(self.stub.query(first)['From'], [FROM])
        self.assertEqual(self.stub.query(second)['PageToken'], ['PA123'])
        self.assertEqual([m['providerSid'] for m in report['matched']], [note.provider_sid])
        self.assertEqual([m['providerSid'] for m in report['providerOnly']], ['SMFOREIGN'])
        self.assertEqual(report['summary']['providerMessages'], 2)   # inbound and out-of-window dropped

    def test_reconciliation_is_staff_only_and_validates_its_range(self):
        self.assertEqual(self.client.get('/api/notifications/reconciliation', {'from': 'x', 'to': 'y'})
                         .status_code, 400)
        self.as_user(self.shopper)
        self.assertEqual(self.client.get('/api/notifications/reconciliation').status_code, 403)


@override_settings(**dict(TWILIO_SETTINGS, TWILIO_BASE_URL='https://messaging-proxy.example.test'))
class BaseUrlTests(TestCase):
    def test_base_url_override_applies_to_messaging_only(self):
        stub = StubTwilio()
        client = twilio_client.build_client(stub)
        stub.on('GET', '/Messages/SM1.json', json_response(200, message_json('SM1', 'sent')))
        stub.on('GET', '/v1/PhoneNumbers/+16045550123', json_response(200, {'phone_number': SHOPPER_NUMBER}))
        client.api20100401_message.fetch_message('AC_test_account', 'SM1')
        client.lookups_v1_phone_number_api.fetch_phone_number2(SHOPPER_NUMBER)
        self.assertTrue(stub.requests[0].url.startswith('https://messaging-proxy.example.test/2010-04-01/'))
        self.assertTrue(stub.requests[1].url.startswith('https://lookups.twilio.com/'))

    @override_settings(TWILIO_AUTH_TOKEN='')
    def test_missing_credentials_refuse_to_build_a_client(self):
        from .errors import TwilioNotConfigured
        with self.assertRaises(TwilioNotConfigured):
            twilio_client.build_client(StubTwilio())
