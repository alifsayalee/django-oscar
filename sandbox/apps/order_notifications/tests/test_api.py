import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from oscar.core.loading import get_model
from oscar.test.factories import create_product

from apps.order_notifications import services
from apps.order_notifications import twilio_gateway as tg
from apps.order_notifications.models import ContactNumber, Notification, ProviderAction

from .fake_twilio import ACCOUNT_SID, FakeTwilio

Order = get_model('order', 'Order')

FROM = '+15550001111'
GOOD_TYPED = '(416) 555-0100'
GOOD = '+14165550100'
BAD = '+12025550199'
OTHER_SENDER = '+15559998888'


@override_settings(
    TWILIO_ACCOUNT_SID=ACCOUNT_SID, TWILIO_AUTH_TOKEN='test-token', TWILIO_FROM_NUMBER=FROM,
    TWILIO_MESSAGING_SERVICE_SID='MG' + '1' * 32, TWILIO_BASE_URL='',
    ORDER_NOTIFICATIONS_REFERENCE_PREFIX='test', ORDER_NOTIFICATIONS_REFRESH_INTERVAL_SECONDS=0,
)
class ApiTestCase(TestCase):
    def setUp(self):
        self.fake = FakeTwilio(deliverable=[GOOD], undeliverable=[BAD])
        self.fake.add_number(GOOD_TYPED.replace(' ', '').replace('(', '').replace(')', '').replace('-', ''), GOOD)
        self.fake.add_number('4165550100', GOOD)
        self.fake.add_number(GOOD, GOOD)
        self.fake.add_number(BAD, BAD, country='US')
        client = tg.build_client(account_sid=ACCOUNT_SID, auth_token='test-token', base_url=None,
                                 timeout=5.0, transport=self.fake)
        services.set_gateway(tg.TwilioGateway(client, account_sid=ACCOUNT_SID, from_number=FROM,
                                              messaging_service_sid='MG' + '1' * 32))
        self.addCleanup(services.set_gateway, None)

        self.shopper = User.objects.create_user('shopper', 'shopper@example.com', 'pw')
        self.other = User.objects.create_user('other', 'other@example.com', 'pw')
        self.operator = User.objects.create_user('operator', 'op@example.com', 'pw', is_staff=True)
        self.product = create_product(price=10, num_in_stock=50)

    # -- helpers ---------------------------------------------------------------------------------

    def as_user(self, user):
        self.client.force_login(user)

    def post(self, url, data=None, **extra):
        return self.client.post(url, json.dumps(data or {}), content_type='application/json', **extra)

    def register(self, number=GOOD, user=None):
        self.as_user(user or self.shopper)
        return self.post('/api/contact-numbers', {'phoneNumber': number})

    def place(self, user=None, quantity=1):
        self.as_user(user or self.shopper)
        return self.post('/api/orders', {'items': [{'productId': self.product.pk, 'quantity': quantity}]})

    def dispatch(self, order_id):
        self.as_user(self.operator)
        return self.post('/api/orders/%s/dispatch' % order_id)

    def cancel(self, order_id):
        self.as_user(self.operator)
        return self.post('/api/orders/%s/cancel' % order_id)


class ContactNumberTests(ApiTestCase):
    def test_registers_the_providers_canonical_form(self):
        response = self.register('416 555 0100')
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIn('contactNumberId', body)
        self.assertEqual(body['phoneNumber'], GOOD)
        self.assertEqual(ContactNumber.objects.get().phone_number, GOOD)

    def test_registering_twice_returns_the_same_number(self):
        first = self.register().json()['contactNumberId']
        second = self.register()
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['contactNumberId'], first)

    def test_rejects_a_number_the_provider_does_not_consider_usable(self):
        response = self.register('+15551234')
        self.assertEqual(response.status_code, 422)
        self.assertFalse(ContactNumber.objects.exists())

    def test_rejects_garbage_without_asking_the_provider(self):
        self.assertEqual(self.register('call me maybe').status_code, 422)
        self.assertEqual(self.fake.requests, [])

    def test_our_credentials_refused_is_not_the_callers_fault(self):
        self.fake.fail_next.append(('status', 401))
        self.assertEqual(self.register().status_code, 502)

    def test_provider_unreachable_is_reported(self):
        self.fake.fail_next.append(('before', httpx.ConnectError('refused')))
        self.assertEqual(self.register().status_code, 502)

    def test_lists_only_the_callers_numbers(self):
        self.register(user=self.shopper)
        self.register(BAD, user=self.other)
        self.as_user(self.shopper)
        numbers = self.client.get('/api/contact-numbers').json()['contactNumbers']
        self.assertEqual([n['phoneNumber'] for n in numbers], [GOOD])

    def test_cannot_delete_someone_elses_number(self):
        contact_id = self.register(user=self.other).json()['contactNumberId']
        self.as_user(self.shopper)
        self.assertEqual(self.client.delete('/api/contact-numbers/%s' % contact_id).status_code, 404)
        self.assertTrue(ContactNumber.objects.filter(pk=contact_id).exists())

    def test_delete_removes_it_and_calls_off_waiting_messages(self):
        contact_id = self.register().json()['contactNumberId']
        order_id = self.place().json()['orderId']
        self.dispatch(order_id)
        follow_up = Notification.objects.get(kind=Notification.FOLLOW_UP)
        self.assertEqual(self.fake.messages[follow_up.provider_sid]['status'], 'scheduled')

        self.as_user(self.shopper)
        response = self.client.delete('/api/contact-numbers/%s' % contact_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['scheduledMessagesCalledOff'][0]['outcome'], 'done')
        self.assertEqual(self.fake.messages[follow_up.provider_sid]['status'], 'canceled')
        self.assertEqual(self.client.get('/api/contact-numbers').json()['contactNumbers'], [])

        # And nothing is sent to it again: the next order event messages nobody.
        before = len(self.fake.creates())
        self.cancel(order_id)
        self.assertEqual(len(self.fake.creates()), before)

    def test_requires_login(self):
        self.assertEqual(self.client.get('/api/contact-numbers').status_code, 401)

    def test_number_never_reaches_the_logs(self):
        with self.assertLogs('apps.order_notifications', level='DEBUG') as logs:
            self.register('416 555 0100')
            order_id = self.place().json()['orderId']
            self.dispatch(order_id)
        joined = '\n'.join(logs.output)
        for fragment in ('5550100', GOOD):
            self.assertNotIn(fragment, joined)


class OrderFlowTests(ApiTestCase):
    def test_place_order_uses_oscars_order_model_and_texts_the_shopper(self):
        self.register()
        response = self.place(quantity=2)
        self.assertEqual(response.status_code, 201)
        body = response.json()
        order = Order.objects.get(pk=body['orderId'])
        self.assertEqual(order.user, self.shopper)
        self.assertEqual(order.lines.get().quantity, 2)
        self.assertEqual(body['notification']['kind'], 'placed')
        self.assertEqual(body['notification']['outcome'], 'pending')     # queued: accepted, not delivered
        create = self.fake.creates()[0]
        self.assertEqual(create.body.fields['To'], GOOD)
        self.assertEqual(create.body.fields['From'], FROM)
        self.assertIn(order.number, create.body.fields['Body'])
        # The key is derived from the reference, not random.
        notification = Notification.objects.get()
        self.assertEqual(create.headers['idempotency-key'], services._idempotency_key(notification.reference))

    def test_no_number_on_file_means_no_message(self):
        response = self.place()
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.json()['notification'])
        self.assertEqual(self.fake.requests, [])

    def test_a_refused_message_never_fails_the_order(self):
        self.register()
        self.fake.fail_next.append(('status', 400))
        response = self.place()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['notification']['outcome'], 'failed')

    def test_never_sent_is_failed_but_a_lost_answer_is_looked_up(self):
        self.register()
        self.fake.fail_next.append(('before', httpx.ConnectError('refused')))
        unsent = self.place().json()['notification']
        self.assertEqual(unsent['outcome'], 'failed')

        self.fake.fail_next.append(('after', httpx.ReadTimeout('no reply')))
        landed = self.place().json()['notification']
        # It landed; the lookup by the reference in its body found it, with no second create.
        self.assertEqual(landed['outcome'], 'pending')
        self.assertIsNotNone(landed['providerSid'])
        self.assertEqual(len(self.fake.creates()), 2)

    def test_unreadable_answer_with_nothing_found_is_unknown_not_failed(self):
        self.register()
        self.fake.fail_next.append(('status', 503))      # 5xx on a write: may have landed
        body = self.place().json()['notification']
        self.assertEqual(body['outcome'], 'unknown')

    def test_dispatch_texts_and_queues_the_follow_up_with_the_provider(self):
        self.register()
        order_id = self.place().json()['orderId']
        response = self.dispatch(order_id)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['status'], 'Dispatched')
        self.assertEqual(body['dispatchNotification']['kind'], 'dispatched')
        follow_up = body['followUpNotification']
        self.assertEqual(follow_up['providerStatus'], 'scheduled')
        self.assertEqual(follow_up['outcome'], 'pending')
        fields = self.fake.creates()[-1].body.fields
        self.assertEqual(fields['ScheduleType'], 'fixed')
        self.assertTrue(fields['MessagingServiceSid'].startswith('MG'))
        send_at = datetime.fromisoformat(fields['SendAt'].replace('Z', '+00:00'))
        self.assertAlmostEqual((send_at - datetime.now(timezone.utc)).total_seconds(), 72 * 3600, delta=120)

    def test_dispatch_twice_sends_nothing_new(self):
        self.register()
        order_id = self.place().json()['orderId']
        self.dispatch(order_id)
        before = len(self.fake.creates())
        self.assertEqual(self.dispatch(order_id).status_code, 200)
        self.assertEqual(len(self.fake.creates()), before)

    def test_dispatch_and_cancel_are_operator_only(self):
        self.register()
        order_id = self.place().json()['orderId']
        self.as_user(self.shopper)
        self.assertEqual(self.post('/api/orders/%s/dispatch' % order_id).status_code, 403)
        self.assertEqual(self.post('/api/orders/%s/cancel' % order_id).status_code, 403)

    def test_cancel_calls_the_follow_up_off_before_it_goes_out(self):
        self.register()
        order_id = self.place().json()['orderId']
        self.dispatch(order_id)
        follow_up = Notification.objects.get(kind=Notification.FOLLOW_UP)

        response = self.cancel(order_id)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['status'], 'Cancelled')
        self.assertEqual(body['followUpCallOffs'][0]['outcome'], 'done')
        self.assertEqual(self.fake.messages[follow_up.provider_sid]['status'], 'canceled')
        self.assertEqual(body['cancellationNotification']['kind'], 'cancelled')
        cancel_request = [r for r in self.fake.writes() if follow_up.provider_sid in r.url][0]
        self.assertEqual(cancel_request.body.fields, {'Status': 'canceled'})

        # Repeating the cancel makes no further provider writes.
        before = len(self.fake.writes())
        self.assertEqual(self.cancel(order_id).status_code, 200)
        self.assertEqual(len(self.fake.writes()), before)

    def test_cancel_too_late_is_reported_not_hidden(self):
        self.register()
        order_id = self.place().json()['orderId']
        self.dispatch(order_id)
        follow_up = Notification.objects.get(kind=Notification.FOLLOW_UP)
        self.fake.messages[follow_up.provider_sid]['status'] = 'delivered'    # it already went out
        body = self.cancel(order_id).json()
        self.assertEqual(body['followUpCallOffs'][0]['outcome'], 'failed')
        self.assertEqual(body['status'], 'Cancelled')

    def test_cancelled_order_cannot_be_dispatched(self):
        self.register()
        order_id = self.place().json()['orderId']
        self.cancel(order_id)
        self.assertEqual(self.dispatch(order_id).status_code, 409)

    def test_my_orders_is_shopper_scoped_and_reports_delivery(self):
        self.register()
        self.place()
        self.place(user=self.other)
        self.fake.settle()
        self.as_user(self.shopper)
        orders = self.client.get('/api/my-orders').json()['orders']
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]['notifications'][0]['outcome'], 'done')
        self.assertEqual(orders[0]['notifications'][0]['providerStatus'], 'delivered')

    def test_order_notifications_are_hidden_from_other_shoppers(self):
        self.register()
        order_id = self.place().json()['orderId']
        self.as_user(self.other)
        self.assertEqual(self.client.get('/api/orders/%s/notifications' % order_id).status_code, 404)
        self.as_user(self.shopper)
        entries = self.client.get('/api/orders/%s/notifications' % order_id).json()['notifications']
        self.assertEqual(len(entries), 1)
        self.assertIn('notificationId', entries[0])

    def test_unknown_outcome_is_settled_by_a_later_request(self):
        self.register()
        self.fake.fail_next.append(('status', 503))
        order_id = self.place().json()['orderId']
        notification = Notification.objects.get()
        self.assertEqual(notification.outcome, 'unknown')
        # The provider had in fact taken it: simulate that record existing now.
        self.fake.add_message(GOOD, FROM, notification.body, status='delivered')
        Notification.objects.filter(pk=notification.pk).update(
            claimed_at=notification.claimed_at - timedelta(minutes=5))
        self.as_user(self.shopper)
        entries = self.client.get('/api/orders/%s/notifications' % order_id).json()['notifications']
        self.assertEqual(entries[0]['outcome'], 'done')
        self.assertEqual(len(self.fake.creates()), 1)      # looked up, never re-sent


class OperatorToolTests(ApiTestCase):
    def _undelivered_notification(self):
        self.register(BAD)
        order_id = self.place().json()['orderId']
        self.fake.settle()
        return order_id, Notification.objects.get()

    def test_resend_is_idempotent_per_key(self):
        order_id, original = self._undelivered_notification()
        # The shopper fixes their number; the operator re-sends.
        self.register(GOOD)
        self.as_user(self.operator)
        url = '/api/notifications/%s/resend' % original.pk
        first = self.post(url, HTTP_IDEMPOTENCY_KEY='key-1')
        self.assertIn(first.status_code, (200, 202))
        new_id = first.json()['notificationId']
        self.assertNotEqual(new_id, original.pk)
        creates = len(self.fake.creates())

        repeat = self.post(url, HTTP_IDEMPOTENCY_KEY='key-1')
        self.assertEqual(repeat.json()['notificationId'], new_id)
        self.assertEqual(len(self.fake.creates()), creates)

        fresh = self.post(url, HTTP_IDEMPOTENCY_KEY='key-2')
        self.assertNotEqual(fresh.json()['notificationId'], new_id)
        self.assertEqual(len(self.fake.creates()), creates + 1)

    def test_resend_goes_to_the_original_number_while_it_is_registered(self):
        _, original = self._undelivered_notification()
        self.as_user(self.operator)
        self.post('/api/notifications/%s/resend' % original.pk, HTTP_IDEMPOTENCY_KEY='k')
        self.assertEqual(self.fake.creates()[-1].body.fields['To'], BAD)

    def test_resend_requires_a_key_and_a_message_that_did_not_arrive(self):
        self.register()
        self.place()
        self.fake.settle()
        delivered = Notification.objects.get()
        self.as_user(self.operator)
        url = '/api/notifications/%s/resend' % delivered.pk
        self.assertEqual(self.post(url).status_code, 400)
        self.assertEqual(self.post(url, HTTP_IDEMPOTENCY_KEY='k').status_code, 409)

    def test_resend_is_operator_only(self):
        _, original = self._undelivered_notification()
        self.as_user(self.shopper)
        response = self.post('/api/notifications/%s/resend' % original.pk, HTTP_IDEMPOTENCY_KEY='k')
        self.assertEqual(response.status_code, 403)

    def test_never_resends_a_follow_up_for_a_cancelled_order(self):
        self.register()
        order_id = self.place().json()['orderId']
        self.dispatch(order_id)
        self.cancel(order_id)
        follow_up = Notification.objects.get(kind=Notification.FOLLOW_UP)
        self.as_user(self.operator)
        response = self.post('/api/notifications/%s/resend' % follow_up.pk, HTTP_IDEMPOTENCY_KEY='k')
        self.assertEqual(response.status_code, 409)

    def test_content_disposal_redacts_at_the_provider_and_keeps_the_record(self):
        self.register()
        self.place()
        self.fake.settle()
        notification = Notification.objects.get()
        self.as_user(self.operator)
        response = self.client.delete('/api/notifications/%s/content' % notification.pk)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['contentDisposal'], 'done')
        self.assertIsNone(body['body'])
        self.assertEqual(body['providerStatus'], 'delivered')
        self.assertEqual(self.fake.messages[notification.provider_sid]['body'], '')
        redact = [r for r in self.fake.writes() if notification.provider_sid in r.url][0]
        self.assertEqual(redact.body.fields, {'Body': ''})
        self.assertEqual(ProviderAction.objects.get().kind, ProviderAction.REDACT)
        # Repeating it is harmless and makes no new provider write.
        before = len(self.fake.writes())
        self.assertEqual(self.client.delete('/api/notifications/%s/content' % notification.pk).status_code, 200)
        self.assertEqual(len(self.fake.writes()), before)

    def test_reconciliation_counts_only_our_number_and_shows_both_directions(self):
        self.fake.page_size = 2         # force several pages
        self.register()
        for _ in range(3):
            self.place()
        self.fake.settle()
        foreign = self.fake.add_message(GOOD, FROM, 'sent by someone else with our number')
        self.fake.add_message(GOOD, OTHER_SENDER, 'another app on the same account')
        lost = Notification.objects.order_by('pk')[0]
        self.fake.messages.pop(lost.provider_sid)          # the provider has no record of this one

        start = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        end = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.as_user(self.operator)
        response = self.client.get('/api/notifications/reconciliation', {'from': start, 'to': end})
        self.assertEqual(response.status_code, 200)
        report = response.json()
        self.assertEqual(report['counts']['provider'], 3)            # 2 of ours + 1 foreign, same sender
        self.assertEqual(report['counts']['matched'], 2)
        self.assertEqual([r['providerSid'] for r in report['providerOnly']], [foreign['sid']])
        self.assertEqual([n['notificationId'] for n in report['appOnly']], [lost.pk])
        # The provider was asked for our number's messages; nothing was filtered after the fact.
        lists = [r for r in self.fake.requests if r.method == 'GET' and urlsplit(r.url).path.endswith('Messages.json')]
        for request in lists:
            self.assertEqual(parse_qs(urlsplit(request.url).query)['From'], [FROM])
        self.assertGreater(len(lists), 1)

    def test_reconciliation_validates_the_range_and_is_operator_only(self):
        self.as_user(self.operator)
        self.assertEqual(self.client.get('/api/notifications/reconciliation', {'from': 'x', 'to': 'y'}).status_code, 400)
        self.as_user(self.shopper)
        response = self.client.get('/api/notifications/reconciliation',
                                   {'from': '2026-01-01T00:00:00Z', 'to': '2026-01-02T00:00:00Z'})
        self.assertEqual(response.status_code, 403)


class GatewayTests(TestCase):
    def test_base_url_overrides_the_messaging_host_only(self):
        fake = FakeTwilio()
        fake.add_number(GOOD, GOOD)
        client = tg.build_client(account_sid=ACCOUNT_SID, auth_token='t', base_url='http://127.0.0.1:9',
                                 timeout=5.0, transport=fake)
        gateway = tg.TwilioGateway(client, account_sid=ACCOUNT_SID, from_number=FROM, messaging_service_sid='')
        gateway.lookup_number(GOOD)
        try:
            gateway.fetch_message('SM1')
        except tg.ProviderError:
            pass
        self.assertTrue(fake.requests[0].url.startswith('https://lookups.twilio.com/'))
        self.assertTrue(fake.requests[1].url.startswith('http://127.0.0.1:9/2010-04-01/'))

    def test_missing_credentials_refuse_to_build_a_client(self):
        with self.assertRaises(ValueError):
            tg.build_client(account_sid='', auth_token='', base_url=None, timeout=5.0)

    def test_status_mapping(self):
        self.assertEqual(tg.status_from_provider('delivered'), 'done')
        self.assertEqual(tg.status_from_provider('sent'), 'pending')
        self.assertEqual(tg.status_from_provider('scheduled'), 'pending')
        self.assertEqual(tg.status_from_provider('undelivered'), 'failed')
        self.assertEqual(tg.status_from_provider('canceled'), 'failed')
        self.assertEqual(tg.status_from_provider('something_new'), 'unknown')
        self.assertEqual(tg.status_from_provider(None), 'unknown')
        self.assertEqual(tg.cancel_outcome('canceled'), 'done')
        self.assertEqual(tg.cancel_outcome('delivered'), 'failed')
        self.assertEqual(tg.cancel_outcome('scheduled'), 'unknown')
        self.assertEqual(tg.redact_outcome(''), 'done')
        self.assertEqual(tg.redact_outcome('still here'), 'needs_review')

    def test_a_write_answer_without_a_sid_is_an_unknown_outcome(self):
        class Empty(FakeTwilio):
            def _create(self, request):
                from .fake_twilio import _json
                return _json(201, {})
        fake = Empty()
        client = tg.build_client(account_sid=ACCOUNT_SID, auth_token='t', base_url=None, timeout=5.0, transport=fake)
        gateway = tg.TwilioGateway(client, account_sid=ACCOUNT_SID, from_number=FROM, messaging_service_sid='')
        with self.assertRaises(tg.WriteOutcomeUnknown):
            gateway.send_message(GOOD, 'hi', idempotency_key='k')

    def test_unsent_and_unknown_transport_failures_differ(self):
        fake = FakeTwilio()
        client = tg.build_client(account_sid=ACCOUNT_SID, auth_token='t', base_url=None, timeout=5.0, transport=fake)
        gateway = tg.TwilioGateway(client, account_sid=ACCOUNT_SID, from_number=FROM, messaging_service_sid='')
        fake.fail_next.append(('before', httpx.ConnectError('refused')))
        with self.assertRaises(tg.WriteNotSent) as unsent:
            gateway.send_message(GOOD, 'hi', idempotency_key='k')
        fake.fail_next.append(('before', httpx.ReadTimeout('no reply')))
        with self.assertRaises(tg.WriteOutcomeUnknown) as unknown:
            gateway.send_message(GOOD, 'hi', idempotency_key='k')
        self.assertEqual((unsent.exception.status_code, unsent.exception.outcome_unknown), (502, False))
        self.assertEqual((unknown.exception.status_code, unknown.exception.outcome_unknown), (504, True))
