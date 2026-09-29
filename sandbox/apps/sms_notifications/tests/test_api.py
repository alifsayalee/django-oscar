import json
from datetime import timedelta
from decimal import Decimal as D

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from oscar.test.factories import create_product, create_stockrecord

from apps.sms_notifications.models import ContactNumber, Notification, Outcome, ProviderWrite
from apps.sms_notifications.safe_write import body_token

from .helpers import (
    SHOPPER_NUMBER, ProviderTestMixin, error_response, form_fields, json_response, lookup_response,
    message, message_response, query)

User = get_user_model()


class ApiTestCase(ProviderTestMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.shopper = User.objects.create_user('shopper', 'shopper@example.com', 'secret-password-1')
        self.other = User.objects.create_user('other', 'other@example.com', 'secret-password-1')
        self.staff = User.objects.create_user('operator', 'op@example.com', 'secret-password-1', is_staff=True)
        self.product = create_product(title='A book')
        create_stockrecord(self.product, num_in_stock=50, price=D('12.00'))
        self.client.force_login(self.shopper)

    def as_user(self, user):
        self.client.force_login(user)

    def post_json(self, url, data=None, **headers):
        return self.client.post(url, data=json.dumps(data or {}), content_type='application/json', **headers)

    def register(self, number=SHOPPER_NUMBER, user=None):
        contact = ContactNumber.objects.create(user=user or self.shopper, phone_number=number)
        return contact

    def place_order(self, provider_answer=None):
        if provider_answer is not None:
            self.transport.queue(provider_answer)
        response = self.post_json('/api/orders', {'lines': [{'productId': self.product.pk, 'quantity': 2}]})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()


class AuthTests(ApiTestCase):

    def test_anonymous_callers_are_refused(self):
        self.client.logout()
        self.assertEqual(self.client.get('/api/contact-numbers').status_code, 401)
        self.assertEqual(self.client.get('/api/my-orders').status_code, 401)

    def test_operator_actions_need_staff(self):
        order = self.place_order()
        for url in ('/api/orders/%s/dispatch' % order['orderId'], '/api/orders/%s/cancel' % order['orderId']):
            self.assertEqual(self.post_json(url).status_code, 403)
        self.assertEqual(self.client.get('/api/notifications/reconciliation?from=2026-01-01T00:00:00Z'
                                         '&to=2026-01-02T00:00:00Z').status_code, 403)
        self.assertEqual(self.client.delete('/api/notifications/1/content').status_code, 403)
        self.assertEqual(self.post_json('/api/notifications/1/resend').status_code, 403)


class ContactNumberTests(ApiTestCase):

    def test_registers_the_providers_canonical_form(self):
        self.transport.queue(lookup_response(phone_number=SHOPPER_NUMBER))
        response = self.post_json('/api/contact-numbers', {'phoneNumber': '(555) 555-0142', 'countryCode': 'US'})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIn('contactNumberId', body)
        self.assertEqual(body['phoneNumber'], SHOPPER_NUMBER)
        request = self.transport.requests[0]
        self.assertEqual(request.method, 'GET')
        self.assertTrue(request.url.startswith('https://lookups.twilio.com/v2/PhoneNumbers/'))
        self.assertEqual(query(request)['CountryCode'], 'US')

    def test_an_unusable_number_is_rejected_and_not_stored(self):
        self.transport.queue(lookup_response(phone_number='+1555', valid=False, errors=['TOO_SHORT']))
        response = self.post_json('/api/contact-numbers', {'phoneNumber': '+1555'})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['validationErrors'], ['TOO_SHORT'])
        self.assertFalse(ContactNumber.objects.exists())

    def test_lookup_unreachable_is_not_reported_as_an_invalid_number(self):
        self.transport.queue(httpx.ConnectError('refused'))
        response = self.post_json('/api/contact-numbers', {'phoneNumber': SHOPPER_NUMBER})
        self.assertEqual(response.status_code, 502)
        self.assertFalse(ContactNumber.objects.exists())

    def test_our_credentials_refused_is_not_the_callers_fault(self):
        self.transport.queue(error_response(401, 20003, 'Authenticate'))
        response = self.post_json('/api/contact-numbers', {'phoneNumber': SHOPPER_NUMBER})
        self.assertEqual(response.status_code, 502)

    def test_listing_and_deleting_are_scoped_to_the_owner(self):
        mine = self.register()
        theirs = self.register(user=self.other)
        listed = self.client.get('/api/contact-numbers').json()['contactNumbers']
        self.assertEqual([c['contactNumberId'] for c in listed], [mine.pk])
        self.assertEqual(self.client.delete('/api/contact-numbers/%s' % theirs.pk).status_code, 404)
        self.assertEqual(self.client.delete('/api/contact-numbers/%s' % mine.pk).status_code, 200)
        self.assertEqual(self.client.get('/api/contact-numbers').json()['contactNumbers'], [])

    def test_a_deleted_number_is_never_messaged(self):
        contact = self.register()
        self.client.delete('/api/contact-numbers/%s' % contact.pk)
        order = self.place_order()
        self.assertEqual(order['notifications'][0]['outcome'], 'skipped_no_number')
        self.assertEqual(self.transport.requests, [])


class OrderFlowTests(ApiTestCase):

    def test_placing_an_order_tells_the_shopper(self):
        self.register()
        order = self.place_order(message_response(status='queued'))
        self.assertIn('orderId', order)
        self.assertEqual(order['lines'][0]['quantity'], 2)
        [note] = order['notifications']
        self.assertEqual(note['outcome'], 'pending')      # queued is not delivered
        self.assertEqual(note['kind'], 'placed')
        [request] = self.transport.requests
        self.assertTrue(request.url.endswith('/2010-04-01/Accounts/AC00000000000000000000000000000000/Messages.json'))
        fields = form_fields(request)
        self.assertEqual(fields['To'], SHOPPER_NUMBER)
        self.assertEqual(fields['From'], '+15555550100')
        self.assertEqual(fields['MessagingServiceSid'], 'MG00000000000000000000000000000000')
        notification = Notification.objects.get()
        self.assertIn('Ref %s' % body_token(notification.reference), fields['Body'])
        self.assertEqual(request.headers['idempotency-key'], notification.reference + ':send')

    def test_a_shopper_without_a_number_is_not_messaged(self):
        order = self.place_order()
        self.assertEqual(order['notifications'][0]['outcome'], 'skipped_no_number')
        self.assertEqual(self.transport.requests, [])

    def test_a_refused_message_never_fails_the_order(self):
        self.register()
        order = self.place_order(error_response(400, 21211, "The 'To' number is not a valid phone number."))
        self.assertEqual(order['notifications'][0]['outcome'], 'failed')
        self.assertEqual(order['notifications'][0]['errorMessage'], "The 'To' number is not a valid phone number.")

    def test_an_order_for_an_unknown_product_is_rejected(self):
        response = self.post_json('/api/orders', {'lines': [{'productId': 999999, 'quantity': 1}]})
        self.assertEqual(response.status_code, 422)

    def test_dispatch_tells_the_shopper_and_queues_the_follow_up_with_the_provider(self):
        self.register()
        order = self.place_order(message_response(sid='SM1', status='queued'))
        self.as_user(self.staff)
        self.transport.queue(message_response(sid='SM2', status='queued'),
                             message_response(sid='SM3', status='scheduled'))
        response = self.post_json('/api/orders/%s/dispatch' % order['orderId'])
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['status'], 'Dispatched')
        kinds = {n['kind']: n for n in body['notifications']}
        self.assertEqual(kinds['followup']['outcome'], 'pending')
        self.assertEqual(kinds['followup']['deliveryStatus'], 'scheduled')
        followup_fields = form_fields(self.transport.requests[-1])
        self.assertEqual(followup_fields['ScheduleType'], 'fixed')
        self.assertIn('SendAt', followup_fields)
        self.assertEqual(followup_fields['MessagingServiceSid'], 'MG00000000000000000000000000000000')

    def test_the_same_dispatch_twice_sends_nothing_new(self):
        self.register()
        order = self.place_order(message_response(sid='SM1'))
        self.as_user(self.staff)
        self.transport.queue(message_response(sid='SM2'), message_response(sid='SM3', status='scheduled'))
        first = self.post_json('/api/orders/%s/dispatch' % order['orderId']).json()
        creates = len(self.provider_calls('POST'))
        # The repeat only re-reads the two pending messages; it creates nothing.
        self.transport.queue(message_response(200, sid='SM2'), message_response(200, sid='SM3', status='scheduled'))
        second = self.post_json('/api/orders/%s/dispatch' % order['orderId']).json()
        self.assertEqual(len(self.provider_calls('POST')), creates)
        self.assertEqual(self.transport.pending, 0)
        self.assertEqual([n['notificationId'] for n in first['notifications']],
                         [n['notificationId'] for n in second['notifications']])

    def test_cancel_calls_off_the_queued_follow_up(self):
        self.register()
        order = self.place_order(message_response(sid='SM1'))
        self.as_user(self.staff)
        self.transport.queue(message_response(sid='SM2'), message_response(sid='SM3', status='scheduled'))
        self.post_json('/api/orders/%s/dispatch' % order['orderId'])
        self.transport.queue(message_response(sid='SM4'), message_response(200, sid='SM3', status='canceled'))
        response = self.post_json('/api/orders/%s/cancel' % order['orderId'])
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['status'], 'Cancelled')
        followup = [n for n in body['notifications'] if n['kind'] == 'followup'][0]
        self.assertEqual(followup['callOff'], 'called_off')
        self.assertEqual(followup['deliveryStatus'], 'canceled')
        call_off = self.transport.requests[-1]
        self.assertEqual(call_off.method, 'POST')
        self.assertTrue(call_off.url.endswith('/Messages/SM3.json'))
        self.assertEqual(form_fields(call_off), {'Status': 'canceled'})

    def test_a_follow_up_that_already_went_out_is_reported_too_late(self):
        self.register()
        order = self.place_order(message_response(sid='SM1'))
        self.as_user(self.staff)
        self.transport.queue(message_response(sid='SM2'), message_response(sid='SM3', status='scheduled'))
        self.post_json('/api/orders/%s/dispatch' % order['orderId'])
        # The provider refuses the call-off; reading the message shows it was already sent.
        self.transport.queue(message_response(sid='SM4'), error_response(400, 30409, 'Cannot cancel'),
                             message_response(200, sid='SM3', status='delivered'))
        body = self.post_json('/api/orders/%s/cancel' % order['orderId']).json()
        followup = [n for n in body['notifications'] if n['kind'] == 'followup'][0]
        self.assertEqual(followup['callOff'], 'too_late')

    def test_deleting_the_number_calls_off_its_queued_follow_up(self):
        contact = self.register()
        order = self.place_order(message_response(sid='SM1'))
        self.as_user(self.staff)
        self.transport.queue(message_response(sid='SM2'), message_response(sid='SM3', status='scheduled'))
        self.post_json('/api/orders/%s/dispatch' % order['orderId'])
        self.as_user(self.shopper)
        self.transport.queue(message_response(200, sid='SM3', status='canceled'))
        response = self.client.delete('/api/contact-numbers/%s' % contact.pk)
        self.assertEqual(response.json()['followUpsCalledOff'], ['called_off'])

    def test_orders_are_scoped_to_their_shopper(self):
        order = self.place_order()
        self.as_user(self.other)
        self.assertEqual(self.client.get('/api/orders/%s/notifications' % order['orderId']).status_code, 404)
        self.assertEqual(self.client.get('/api/my-orders').json()['orders'], [])
        self.as_user(self.staff)
        self.assertEqual(self.client.get('/api/orders/%s/notifications' % order['orderId']).status_code, 200)

    def test_reading_notifications_asks_the_provider_where_they_got_to(self):
        self.register()
        order = self.place_order(message_response(sid='SM1', status='queued'))
        self.transport.queue(message_response(200, sid='SM1', status='delivered',
                                              date_sent='Tue, 29 Sep 2026 10:00:05 +0000'))
        body = self.client.get('/api/orders/%s/notifications' % order['orderId']).json()
        [note] = body['notifications']
        self.assertEqual(note['outcome'], 'done')
        self.assertEqual(note['deliveryStatus'], 'delivered')
        self.assertEqual(self.transport.requests[-1].method, 'GET')
        # Settled: a second read makes no provider call.
        calls = len(self.transport.requests)
        self.client.get('/api/my-orders')
        self.assertEqual(len(self.transport.requests), calls)

    def test_an_undelivered_message_is_failed_not_done(self):
        self.register()
        order = self.place_order(message_response(sid='SM1', status='queued'))
        self.transport.queue(message_response(200, sid='SM1', status='undelivered', error_code=30034))
        [note] = self.client.get('/api/my-orders').json()['orders'][0]['notifications']
        self.assertEqual(note['outcome'], 'failed')
        self.assertEqual(note['errorCode'], 30034)


class UnknownOutcomeTests(ApiTestCase):

    def test_a_timed_out_send_is_found_by_its_reference_not_sent_again(self):
        self.register()
        self.transport.queue(httpx.ReadTimeout('no reply'))
        # The lookup: the provider has it (its body carries our reference token).
        self.transport.queue(json_response(200, {'messages': [], 'next_page_uri': None}))
        order = self.place_order()
        note = order['notifications'][0]
        self.assertEqual(note['outcome'], 'unknown')
        lookup = self.transport.requests[-1]
        self.assertEqual(lookup.method, 'GET')
        self.assertEqual(query(lookup)['To'], SHOPPER_NUMBER)
        self.assertEqual(query(lookup)['From'], '+15555550100')

        # Later: the lookup finds it. No second create is ever made.
        notification = Notification.objects.get()
        found = message(sid='SM9', status='sent', body='... Ref %s' % body_token(notification.reference))
        self.transport.queue(json_response(200, {'messages': [found], 'next_page_uri': None}))
        body = self.client.get('/api/orders/%s/notifications' % order['orderId']).json()
        self.assertEqual(body['notifications'][0]['providerSid'], 'SM9')
        self.assertEqual(body['notifications'][0]['outcome'], 'pending')
        creates = [r for r in self.transport.requests if r.method == 'POST']
        self.assertEqual(len(creates), 1)

    def test_a_send_that_never_left_is_failed_and_may_be_sent_again_under_the_same_reference(self):
        self.register()
        order = self.place_order(httpx.ConnectError('refused'))
        self.assertEqual(order['notifications'][0]['outcome'], 'failed')
        write = ProviderWrite.objects.get()
        self.assertEqual((write.outcome, write.provider_id), (Outcome.FAILED, ''))

    def test_never_sent_and_unknown_are_different_outcomes(self):
        self.register()
        unsent = self.place_order(httpx.ConnectError('refused'))['notifications'][0]
        self.transport.queue(httpx.ReadTimeout('no reply'), json_response(200, {'messages': []}))
        unknown = self.place_order()['notifications'][0]
        self.assertEqual((unsent['outcome'], unknown['outcome']), ('failed', 'unknown'))
        lookups = [r for r in self.transport.requests if r.method == 'GET']
        self.assertEqual(len(lookups), 1)      # only the unknown one is looked up


class ResendTests(ApiTestCase):

    def failed_notification(self):
        self.register()
        order = self.place_order(message_response(sid='SM1', status='queued'))
        self.transport.queue(message_response(200, sid='SM1', status='undelivered', error_code=30006))
        self.client.get('/api/my-orders')
        self.as_user(self.staff)
        return Notification.objects.get(order_id=order['orderId'])

    def test_the_same_key_twice_sends_one_message(self):
        source = self.failed_notification()
        self.transport.queue(message_response(sid='SM2', status='queued'))
        url = '/api/notifications/%s/resend' % source.pk
        first = self.post_json(url, HTTP_IDEMPOTENCY_KEY='key-1')
        # The repeat re-reads the pending message by its sid; it sends nothing.
        self.transport.queue(message_response(200, sid='SM2', status='queued'))
        second = self.post_json(url, HTTP_IDEMPOTENCY_KEY='key-1')
        self.assertTrue(self.transport.requests[-1].url.endswith('/Messages/SM2.json'))
        self.assertEqual(self.transport.requests[-1].method, 'GET')
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json()['notificationId'], second.json()['notificationId'])
        self.assertNotEqual(first.json()['notificationId'], source.pk)
        creates = [r for r in self.transport.requests if r.method == 'POST']
        self.assertEqual(len(creates), 2)       # the original send + ONE resend

    def test_a_fresh_key_is_a_second_legitimate_attempt(self):
        source = self.failed_notification()
        self.transport.queue(message_response(sid='SM2'), message_response(sid='SM3'))
        url = '/api/notifications/%s/resend' % source.pk
        a = self.post_json(url, HTTP_IDEMPOTENCY_KEY='key-1').json()['notificationId']
        b = self.post_json(url, HTTP_IDEMPOTENCY_KEY='key-2').json()['notificationId']
        self.assertNotEqual(a, b)

    def test_a_key_is_required(self):
        source = self.failed_notification()
        self.assertEqual(self.post_json('/api/notifications/%s/resend' % source.pk).status_code, 400)

    def test_a_delivered_message_is_not_resent(self):
        self.register()
        order = self.place_order(message_response(sid='SM1', status='delivered'))
        self.as_user(self.staff)
        source = Notification.objects.get(order_id=order['orderId'])
        response = self.post_json('/api/notifications/%s/resend' % source.pk, HTTP_IDEMPOTENCY_KEY='k')
        self.assertEqual(response.status_code, 409)


class ContentDisposalTests(ApiTestCase):

    def test_the_text_is_redacted_at_the_provider_and_the_record_survives(self):
        self.register()
        order = self.place_order(message_response(sid='SM1', status='delivered'))
        self.as_user(self.staff)
        notification = Notification.objects.get(order_id=order['orderId'])
        self.transport.queue(message_response(200, sid='SM1', status='delivered', body=''))
        response = self.client.delete('/api/notifications/%s/content' % notification.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['contentDisposal'], 'done')
        request = self.transport.requests[-1]
        self.assertTrue(request.url.endswith('/Messages/SM1.json'))
        self.assertEqual(form_fields(request), {'Body': ''})
        notification.refresh_from_db()
        self.assertEqual(notification.body, '')
        self.assertEqual((notification.provider_sid, notification.provider_status), ('SM1', 'delivered'))
        self.assertIsNone(response.json()['notification']['body'])

    def test_a_refused_redaction_is_a_conflict_and_keeps_the_text(self):
        self.register()
        order = self.place_order(message_response(sid='SM1', status='queued'))
        self.as_user(self.staff)
        notification = Notification.objects.get(order_id=order['orderId'])
        self.transport.queue(error_response(400, 20009, 'Cannot redact a message that is not complete'))
        response = self.client.delete('/api/notifications/%s/content' % notification.pk)
        self.assertEqual(response.status_code, 409)
        notification.refresh_from_db()
        self.assertNotEqual(notification.body, '')


class ReconciliationTests(ApiTestCase):

    def test_lines_up_the_providers_messages_from_our_number_with_ours(self):
        self.register()
        order = self.place_order(message_response(sid='SM1', status='delivered',
                                                  date_sent='Tue, 29 Sep 2026 10:00:05 +0000'))
        self.as_user(self.staff)
        page1 = {'messages': [message(sid='SM1', status='delivered', date_sent='Tue, 29 Sep 2026 10:00:05 +0000')],
                 'next_page_uri': '/2010-04-01/Accounts/AC0/Messages.json?From=%2B15555550100&PageSize=1000&Page=1&PageToken=PASM1'}
        page2 = {'messages': [message(sid='SMX', status='delivered', date_sent='Tue, 29 Sep 2026 11:00:00 +0000'),
                              message(sid='SMOUT', status='delivered', date_sent='Wed, 30 Sep 2026 11:00:00 +0000')],
                 'next_page_uri': None}
        self.transport.queue(json_response(200, page1), json_response(200, page2))
        response = self.client.get('/api/notifications/reconciliation',
                                   {'from': '2026-09-29T00:00:00Z', 'to': '2026-09-30T00:00:00Z'})
        self.assertEqual(response.status_code, 200)
        report = response.json()
        self.assertEqual(report['summary']['matched'], 1)
        self.assertEqual(report['matched'][0]['notificationId'], Notification.objects.get(order_id=order['orderId']).pk)
        self.assertEqual([p['providerSid'] for p in report['providerOnly']], ['SMX'])   # SMOUT is outside the range
        first, second = [r for r in self.transport.requests if r.method == 'GET']
        self.assertEqual(query(first)['From'], '+15555550100')
        self.assertIn('DateSent>', query(first))
        self.assertIn('DateSent<', query(first))
        self.assertEqual(query(second)['PageToken'], 'PASM1')

    def test_a_message_we_sent_that_the_provider_does_not_list_is_app_only(self):
        self.register()
        self.place_order(message_response(sid='SM1', status='delivered',
                                          date_sent='Tue, 29 Sep 2026 10:00:05 +0000'))
        self.as_user(self.staff)
        self.transport.queue(json_response(200, {'messages': [], 'next_page_uri': None}))
        report = self.client.get('/api/notifications/reconciliation',
                                 {'from': '2026-09-29T00:00:00Z', 'to': '2026-09-30T00:00:00Z'}).json()
        self.assertEqual([a['providerSid'] for a in report['appOnly']], ['SM1'])

    def test_bad_dates_are_rejected(self):
        self.as_user(self.staff)
        self.assertEqual(self.client.get('/api/notifications/reconciliation', {'from': 'x', 'to': 'y'}).status_code, 400)


class BaseUrlTests(ApiTestCase):

    def test_the_messaging_base_url_override_is_used_verbatim_and_only_for_messaging(self):
        from apps.sms_notifications import gateway
        with self.settings(TWILIO_BASE_URL='http://127.0.0.1:9/mock'):
            gateway.set_client(gateway.build_client(transport=self.transport))
            self.register()
            self.transport.queue(lookup_response())
            self.post_json('/api/contact-numbers', {'phoneNumber': SHOPPER_NUMBER})
            self.place_order(message_response(sid='SM1'))
        lookup, create = self.transport.requests
        self.assertTrue(lookup.url.startswith('https://lookups.twilio.com/'))
        self.assertTrue(create.url.startswith('http://127.0.0.1:9/mock/2010-04-01/Accounts/'))


class FollowUpScheduleTests(ApiTestCase):

    def test_the_follow_up_is_scheduled_days_later(self):
        self.register()
        order = self.place_order(message_response(sid='SM1'))
        self.as_user(self.staff)
        self.transport.queue(message_response(sid='SM2'), message_response(sid='SM3', status='scheduled'))
        before = timezone.now()
        self.post_json('/api/orders/%s/dispatch' % order['orderId'])
        followup = Notification.objects.get(kind=Notification.FOLLOWUP)
        self.assertGreaterEqual(followup.send_at, before + timedelta(hours=71))


class ClaimTests(ApiTestCase):

    def test_a_send_in_flight_elsewhere_is_answered_without_a_provider_call(self):
        from apps.sms_notifications import services
        contact = self.register()
        order = self.place_order(message_response(sid="SM1"))
        notification = Notification.objects.create(
            reference='test-install:order:%s:dispatched' % order['orderId'], order_id=order['orderId'],
            user=self.shopper, contact_number=contact, kind=Notification.DISPATCHED, body='x')
        # Another worker holds the claim and has not had an answer yet.
        ProviderWrite.objects.create(reference=notification.reference + ':send', notification=notification,
                                     step=ProviderWrite.SEND, outcome=Outcome.SENDING)
        calls = len(self.transport.requests)
        result = services.send_notification(notification)
        self.assertEqual(len(self.transport.requests), calls)
        self.assertEqual(result.outcome, Outcome.SENDING)

    def test_a_called_off_follow_up_is_reported_as_never_sent(self):
        self.register()
        order = self.place_order(message_response(sid='SM1', status='delivered',
                                                  date_sent='Tue, 29 Sep 2026 10:00:05 +0000'))
        self.as_user(self.staff)
        self.transport.queue(message_response(sid='SM2', status='delivered', date_sent='Tue, 29 Sep 2026 10:00:06 +0000'),
                             message_response(sid='SM3', status='scheduled'))
        self.post_json('/api/orders/%s/dispatch' % order['orderId'])
        self.transport.queue(message_response(sid='SM4', status='delivered', date_sent='Tue, 29 Sep 2026 10:00:07 +0000'),
                             message_response(200, sid='SM3', status='canceled'))
        self.post_json('/api/orders/%s/cancel' % order['orderId'])
        self.transport.queue(json_response(200, {'messages': [], 'next_page_uri': None}))
        start = (timezone.now() - timedelta(minutes=5)).isoformat()
        end = (timezone.now() + timedelta(minutes=5)).isoformat()
        report = self.client.get('/api/notifications/reconciliation', {'from': start, 'to': end}).json()
        self.assertEqual([n['kind'] for n in report['neverSent']], ['followup'])
        self.assertEqual(report['unsettled'], [])
