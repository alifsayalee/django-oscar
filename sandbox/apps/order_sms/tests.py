"""
Tests for the order SMS API. The Twilio client is real; only its transport is a stub, so every
request the SDK builds is exercised and can be asserted on. No network access.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from oscar.core.loading import get_model
from oscar.test.factories import create_product
from twilio_sdk import TwilioSdkClient
from twilio_sdk.core import FormBody, HttpRequest, HttpResponse

from . import gateway, notifications
from .models import ContactNumber, Notification

Country = get_model('address', 'Country')
Order = get_model('order', 'Order')

FROM = '+15550001111'
SHOPPER = '+15550002222'
OTHER = '+15550003333'
TWILIO = dict(TWILIO_ACCOUNT_SID='ACtest', TWILIO_AUTH_TOKEN='secret-token',
              TWILIO_FROM_NUMBER=FROM, TWILIO_MESSAGING_SERVICE_SID='MGtest',
              TWILIO_BASE_URL=None, ORDER_SMS_INSTALL_ID='test-install')

# A reply: a response, an exception to raise, or a callable computing the response at send time.
Reply = HttpResponse | Exception | Callable[[], HttpResponse]


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


def message(sid: str, status: str, *, body: str = 'hello', to: str = SHOPPER,
            direction: str = 'outbound-api', date_sent: str | None = None) -> dict[str, Any]:
    return {'sid': sid, 'status': status, 'body': body, 'to': to, 'from': FROM,
            'direction': direction, 'date_created': 'Wed, 23 Sep 2026 09:04:34 +0000',
            'date_sent': date_sent, 'error_code': None, 'error_message': None}


def page(messages: list[dict[str, Any]], next_uri: str | None = None) -> HttpResponse:
    return json_response(200, {'messages': messages, 'next_page_uri': next_uri, 'page': 0})


class StubTransport:
    """Satisfies the SDK's sync transport protocol. Replies are routed by (method, path suffix)."""

    def __init__(self) -> None:
        self.requests: list[HttpRequest] = []
        self.routes: list[tuple[str, str, list[Reply]]] = []

    def on(self, method: str, suffix: str, *replies: Reply) -> None:
        self.routes.append((method, suffix, list(replies)))

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = unquote(urlsplit(request.url).path)
        for method, suffix, replies in self.routes:
            if method == request.method and path.endswith(suffix) and replies:
                reply = replies.pop(0)
                if isinstance(reply, Exception):
                    raise reply
                return reply if isinstance(reply, HttpResponse) else reply()
        raise AssertionError(f'unexpected request {request.method} {path}')

    def close(self) -> None:
        pass

    def calls(self, method: str, suffix: str) -> list[HttpRequest]:
        return [r for r in self.requests
                if r.method == method and unquote(urlsplit(r.url).path).endswith(suffix)]


def form(request: HttpRequest) -> dict[str, Any]:
    assert isinstance(request.body, FormBody)
    return dict(request.body.fields)


def query(request: HttpRequest) -> dict[str, list[str]]:
    return parse_qs(urlsplit(request.url).query)


@override_settings(**TWILIO)
class OrderSmsTestCase(TestCase):
    def setUp(self) -> None:
        self.transport = StubTransport()
        gateway.set_client(TwilioSdkClient(
            custom_http_client=self.transport,
            account_sid_auth_token={'username': 'ACtest', 'password': 'secret-token'}))
        self.addCleanup(gateway.set_client, None)
        self.shopper = User.objects.create_user('shopper', 'shopper@example.com', 'pw-123456789')
        self.other = User.objects.create_user('other', 'other@example.com', 'pw-123456789')
        self.staff = User.objects.create_user('op', 'op@example.com', 'pw-123456789', is_staff=True)
        Country.objects.create(iso_3166_1_a2='GB', name='United Kingdom', is_shipping_country=True)
        self.product = create_product(price=Decimal('10.00'), num_in_stock=100)

    # helpers ---------------------------------------------------------------------------------

    def as_user(self, user: User) -> None:
        self.client.force_login(user)

    def register(self, user: User, number: str = SHOPPER) -> ContactNumber:
        self.transport.on('GET', f'/PhoneNumbers/{number}',
                          json_response(200, {'phone_number': number, 'country_code': 'US'}))
        self.as_user(user)
        response = self.client.post('/api/contact-numbers', {'phoneNumber': number},
                                    content_type='application/json')
        self.assertEqual(response.status_code, 201, response.content)
        return ContactNumber.objects.get(pk=response.json()['contactNumberId'])

    def place(self, user: User) -> dict[str, Any]:
        self.as_user(user)
        response = self.client.post('/api/orders', {
            'lines': [{'productId': self.product.pk, 'quantity': 2}],
            'shippingAddress': {'firstName': 'Ada', 'lastName': 'Shopper', 'line1': '1 High St',
                                'city': 'London', 'postcode': 'N1 1AA', 'country': 'GB'},
        }, content_type='application/json')
        self.assertEqual(response.status_code, 201, response.content)
        body: dict[str, Any] = response.json()
        return body

    def post_json(self, url: str, data: object = None, **extra: Any) -> Any:
        return self.client.post(url, data or {}, content_type='application/json', **extra)


class ContactNumberTests(OrderSmsTestCase):
    def test_registers_the_providers_canonical_form(self) -> None:
        self.transport.on('GET', '/PhoneNumbers/555 000 2222',
                          json_response(200, {'phone_number': SHOPPER, 'country_code': 'US'}))
        self.as_user(self.shopper)
        response = self.post_json('/api/contact-numbers', {'phoneNumber': '555 000 2222'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['phoneNumber'], SHOPPER)
        self.assertIn('contactNumberId', response.json())
        request = self.transport.requests[-1]
        self.assertEqual(urlsplit(request.url).netloc, 'lookups.twilio.com')
        self.assertTrue(request.headers['authorization'].startswith('Basic '))

    def test_rejects_a_number_the_provider_does_not_recognise(self) -> None:
        self.transport.on('GET', '/PhoneNumbers/12345',
                          json_response(404, {'code': 20404, 'message': 'not found', 'status': 404}))
        self.as_user(self.shopper)
        response = self.post_json('/api/contact-numbers', {'phoneNumber': '12345'})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(ContactNumber.objects.exists())

    def test_provider_credentials_problem_is_not_the_callers_fault(self) -> None:
        self.transport.on('GET', f'/PhoneNumbers/{SHOPPER}', json_response(401, {'code': 20003}))
        self.as_user(self.shopper)
        response = self.post_json('/api/contact-numbers', {'phoneNumber': SHOPPER})
        self.assertEqual(response.status_code, 502)

    def test_unreachable_provider_is_502_not_unknown(self) -> None:
        self.transport.on('GET', f'/PhoneNumbers/{SHOPPER}', httpx.ConnectError('refused'),
                          httpx.ConnectError('refused'))
        self.as_user(self.shopper)
        response = self.post_json('/api/contact-numbers', {'phoneNumber': SHOPPER})
        self.assertEqual((response.status_code, response.json()['outcomeUnknown']), (502, False))

    def test_numbers_are_private_to_their_owner(self) -> None:
        contact = self.register(self.shopper)
        self.as_user(self.other)
        self.assertEqual(self.client.get('/api/contact-numbers').json()['contactNumbers'], [])
        self.assertEqual(self.client.delete(f'/api/contact-numbers/{contact.pk}').status_code, 404)
        self.as_user(self.shopper)
        self.assertEqual(self.client.delete(f'/api/contact-numbers/{contact.pk}').status_code, 204)
        self.assertEqual(self.client.get('/api/contact-numbers').json()['contactNumbers'], [])

    def test_requires_login(self) -> None:
        self.assertEqual(self.client.get('/api/contact-numbers').status_code, 401)


class OrderFlowTests(OrderSmsTestCase):
    def test_placing_an_order_texts_the_shopper(self) -> None:
        self.register(self.shopper)
        self.transport.on('POST', '/Messages.json', json_response(201, message('SM1', 'queued')))
        body = self.place(self.shopper)
        self.assertIn('orderId', body)
        [note] = body['notifications']
        self.assertEqual((note['outcome'], note['messageSid']), ('pending', 'SM1'))
        [request] = self.transport.calls('POST', '/Messages.json')
        fields = form(request)
        self.assertEqual((fields['To'], fields['From']), (SHOPPER, FROM))
        n = Notification.objects.get()
        self.assertIn(f'Ref {notifications.reference_tag(n.reference)}', fields['Body'])
        self.assertEqual(urlsplit(request.url).netloc, 'api.twilio.com')

    def test_shopper_without_a_number_is_simply_not_messaged(self) -> None:
        body = self.place(self.shopper)
        self.assertEqual(body['notifications'], [])
        self.assertEqual(self.transport.requests, [])

    def test_a_refused_message_never_fails_the_order(self) -> None:
        self.register(self.shopper)
        self.transport.on('POST', '/Messages.json',
                          json_response(400, {'code': 21211, 'message': f"Invalid 'To' {SHOPPER}"}))
        body = self.place(self.shopper)
        [note] = body['notifications']
        self.assertEqual(note['outcome'], 'failed')
        self.assertNotIn(SHOPPER, note['errorDetail'])        # scrubbed
        self.assertEqual(Order.objects.get(pk=body['orderId']).status, 'Pending')

    def test_refused_connection_and_lost_reply_are_different_outcomes(self) -> None:
        self.register(self.shopper)
        # Order 1: never sent. Order 2: sent, no reply, and the lookup finds it by its tag.
        self.transport.on('POST', '/Messages.json', httpx.ConnectError('refused'),
                          httpx.ReadTimeout('no reply'))
        first = self.place(self.shopper)['notifications'][0]
        self.assertEqual(first['outcome'], 'failed')
        self.assertEqual(self.transport.calls('GET', '/Messages.json'), [])   # nothing to look up

        def found() -> HttpResponse:
            n = Notification.objects.order_by('-pk').first()
            assert n is not None
            return page([message('SM2', 'sent', body=n.body)])

        self.transport.on('GET', '/Messages.json', found)
        second = self.place(self.shopper)['notifications'][0]
        self.assertEqual((second['outcome'], second['messageSid']), ('pending', 'SM2'))
        [lookup] = self.transport.calls('GET', '/Messages.json')
        self.assertEqual(query(lookup)['To'], [SHOPPER])
        self.assertEqual(query(lookup)['From'], [FROM])

    def test_a_lost_reply_with_nothing_found_stays_unknown(self) -> None:
        self.register(self.shopper)
        self.transport.on('POST', '/Messages.json', json_response(503, {'code': 20500}))
        self.transport.on('GET', '/Messages.json', page([]))
        note = self.place(self.shopper)['notifications'][0]
        self.assertEqual(note['outcome'], 'unknown')

    def test_dispatch_queues_a_follow_up_with_the_provider_once(self) -> None:
        self.register(self.shopper)
        self.transport.on('POST', '/Messages.json',
                          json_response(201, message('SM1', 'queued')),
                          json_response(201, message('SM2', 'queued')),
                          json_response(201, message('SM3', 'scheduled')))
        order_id = self.place(self.shopper)['orderId']
        self.as_user(self.shopper)
        self.assertEqual(self.post_json(f'/api/orders/{order_id}/dispatch').status_code, 403)
        self.as_user(self.staff)
        response = self.post_json(f'/api/orders/{order_id}/dispatch')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['status'], 'Dispatched')
        followup = Notification.objects.get(kind=Notification.KIND_FOLLOWUP)
        self.assertEqual((followup.outcome, followup.message_sid), ('pending', 'SM3'))
        fields = form(self.transport.calls('POST', '/Messages.json')[-1])
        self.assertEqual(fields['ScheduleType'], 'fixed')
        self.assertEqual(fields['MessagingServiceSid'], 'MGtest')
        self.assertIn('SendAt', fields)
        # A repeat (caller retry) sends nothing new: the claims answer it.
        again = self.post_json(f'/api/orders/{order_id}/dispatch')
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(self.transport.calls('POST', '/Messages.json')), 3)

    def test_cancel_calls_off_the_follow_up_before_it_goes_out(self) -> None:
        self.register(self.shopper)
        self.transport.on('POST', '/Messages.json',
                          json_response(201, message('SM1', 'queued')),
                          json_response(201, message('SM2', 'queued')),
                          json_response(201, message('SM3', 'scheduled')),
                          json_response(201, message('SM4', 'queued')))
        self.transport.on('POST', '/Messages/SM3.json', json_response(200, message('SM3', 'canceled')))
        order_id = self.place(self.shopper)['orderId']
        self.as_user(self.staff)
        self.post_json(f'/api/orders/{order_id}/dispatch')
        response = self.post_json(f'/api/orders/{order_id}/cancel')
        self.assertEqual(response.status_code, 200, response.content)
        followup = Notification.objects.get(kind=Notification.KIND_FOLLOWUP)
        self.assertEqual((followup.cancel_outcome, followup.provider_status), ('done', 'canceled'))
        [cancel] = self.transport.calls('POST', '/Messages/SM3.json')
        self.assertEqual(form(cancel), {'Status': 'canceled'})
        self.assertTrue(Notification.objects.filter(kind=Notification.KIND_CANCELLED).exists())

    def test_a_follow_up_that_already_went_out_is_reported_not_hidden(self) -> None:
        self.register(self.shopper)
        self.transport.on('POST', '/Messages.json',
                          json_response(201, message('SM1', 'queued')),
                          json_response(201, message('SM2', 'queued')),
                          json_response(201, message('SM3', 'scheduled')),
                          json_response(201, message('SM4', 'queued')))
        self.transport.on('POST', '/Messages/SM3.json', json_response(400, {'code': 30409}))
        self.transport.on('GET', '/Messages/SM3.json', json_response(200, message('SM3', 'delivered')))
        order_id = self.place(self.shopper)['orderId']
        self.as_user(self.staff)
        self.post_json(f'/api/orders/{order_id}/dispatch')
        self.post_json(f'/api/orders/{order_id}/cancel')
        followup = Notification.objects.get(kind=Notification.KIND_FOLLOWUP)
        self.assertEqual(followup.cancel_outcome, 'failed')

    def test_removing_a_number_calls_off_its_follow_up_and_stops_messages(self) -> None:
        contact = self.register(self.shopper)
        self.transport.on('POST', '/Messages.json',
                          json_response(201, message('SM1', 'queued')),
                          json_response(201, message('SM2', 'queued')),
                          json_response(201, message('SM3', 'scheduled')))
        self.transport.on('POST', '/Messages/SM3.json', json_response(200, message('SM3', 'canceled')))
        order_id = self.place(self.shopper)['orderId']
        self.as_user(self.staff)
        self.post_json(f'/api/orders/{order_id}/dispatch')
        self.as_user(self.shopper)
        self.assertEqual(self.client.delete(f'/api/contact-numbers/{contact.pk}').status_code, 204)
        followup = Notification.objects.get(kind=Notification.KIND_FOLLOWUP)
        self.assertEqual(followup.cancel_outcome, 'done')
        self.assertEqual(self.place(self.shopper)['notifications'], [])
        self.assertEqual(len(self.transport.calls('POST', '/Messages.json')), 3)

    def test_orders_and_their_notifications_are_private(self) -> None:
        body = self.place(self.shopper)
        self.as_user(self.other)
        self.assertEqual(self.client.get('/api/my-orders').json()['orders'], [])
        response = self.client.get(f'/api/orders/{body["orderId"]}/notifications')
        self.assertEqual(response.status_code, 404)


class OperatorTests(OrderSmsTestCase):
    def failed_notification(self) -> Notification:
        self.register(self.shopper)
        self.transport.on('POST', '/Messages.json', json_response(201, message('SM1', 'queued')))
        self.place(self.shopper)
        n = Notification.objects.get()
        # The provider later reports it undelivered.
        self.transport.on('GET', '/Messages/SM1.json', json_response(200, message('SM1', 'undelivered')))
        return n

    def test_resend_is_once_per_key_and_again_under_a_fresh_key(self) -> None:
        n = self.failed_notification()
        self.transport.on('POST', '/Messages.json',
                          json_response(201, message('SM2', 'queued')),
                          json_response(201, message('SM3', 'queued')))
        self.as_user(self.staff)
        url = f'/api/notifications/{n.pk}/resend'
        first = self.post_json(url, {'idempotencyKey': 'key-aaaa-1'})
        self.assertEqual(first.status_code, 201, first.content)
        again = self.post_json(url, {'idempotencyKey': 'key-aaaa-1'})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()['notificationId'], first.json()['notificationId'])
        self.assertTrue(again.json()['replayed'])
        self.assertEqual(len(self.transport.calls('POST', '/Messages.json')), 2)   # order + 1 resend
        fresh = self.post_json(url, HTTP_IDEMPOTENCY_KEY='key-bbbb-2')
        self.assertEqual(fresh.status_code, 201)
        self.assertNotEqual(fresh.json()['notificationId'], first.json()['notificationId'])
        self.assertEqual(len(self.transport.calls('POST', '/Messages.json')), 3)

    def test_resend_requires_staff_a_key_and_a_message_that_did_not_arrive(self) -> None:
        self.register(self.shopper)
        self.transport.on('POST', '/Messages.json', json_response(201, message('SM1', 'delivered')))
        self.place(self.shopper)
        n = Notification.objects.get()
        url = f'/api/notifications/{n.pk}/resend'
        self.as_user(self.shopper)
        self.assertEqual(self.post_json(url, {'idempotencyKey': 'key-aaaa-1'}).status_code, 403)
        self.as_user(self.staff)
        self.assertEqual(self.post_json(url).status_code, 400)
        self.transport.on('GET', '/Messages/SM1.json', json_response(200, message('SM1', 'delivered')))
        self.assertEqual(self.post_json(url, {'idempotencyKey': 'key-aaaa-1'}).status_code, 409)

    def test_content_disposal_erases_the_text_at_the_provider(self) -> None:
        n = self.failed_notification()
        self.transport.on('POST', '/Messages/SM1.json',
                          json_response(200, message('SM1', 'undelivered', body='')))
        self.as_user(self.staff)
        response = self.client.delete(f'/api/notifications/{n.pk}/content')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(form(self.transport.calls('POST', '/Messages/SM1.json')[0]), {'Body': ''})
        n.refresh_from_db()
        self.assertEqual((n.body, n.content_disposal, n.message_sid), ('', 'done', 'SM1'))

    def test_reconciliation_lines_up_both_sides(self) -> None:
        self.register(self.shopper)
        self.transport.on('POST', '/Messages.json', json_response(201, message('SM1', 'queued')))
        self.place(self.shopper)
        now = timezone.now()
        stamp = (now - timedelta(minutes=5)).strftime('%a, %d %b %Y %H:%M:%S +0000')
        old = (now - timedelta(days=3)).strftime('%a, %d %b %Y %H:%M:%S +0000')
        self.transport.on('GET', '/Messages.json',
                          page([message('SM1', 'delivered', date_sent=stamp),
                                message('SMX', 'delivered', date_sent=stamp, to=OTHER),
                                message('SMI', 'received', date_sent=stamp, direction='inbound'),
                                message('SMO', 'delivered', date_sent=old)],
                               next_uri='/2010-04-01/Accounts/ACtest/Messages.json?PageSize=1000'
                                        '&Page=1&PageToken=PT1'),
                          page([]))
        self.as_user(self.staff)
        start = (now - timedelta(hours=1)).isoformat()
        end = (now + timedelta(hours=1)).isoformat()
        response = self.client.get('/api/notifications/reconciliation', {'from': start, 'to': end})
        self.assertEqual(response.status_code, 200, response.content)
        counts = response.json()['counts']
        self.assertEqual((counts['matched'], counts['providerOnly'], counts['excludedInbound']),
                         (1, 1, 1))
        first, second = self.transport.calls('GET', '/Messages.json')
        self.assertEqual(query(first)['From'], [FROM])
        self.assertIn('DateSent>', query(first))
        self.assertEqual(query(second)['PageToken'], ['PT1'])
        self.assertNotIn(OTHER, response.content.decode())      # numbers are masked
        self.assertEqual(Notification.objects.get().outcome, 'done')


class StatusMappingTests(TestCase):
    def test_every_status_has_a_deliberate_outcome(self) -> None:
        expected = {'delivered': 'done', 'read': 'done', 'accepted': 'pending',
                    'scheduled': 'pending', 'queued': 'pending', 'sending': 'pending',
                    'sent': 'pending', 'partially_delivered': 'pending', 'failed': 'failed',
                    'undelivered': 'failed', 'canceled': 'failed', 'received': 'unknown',
                    'receiving': 'unknown', 'something_new': 'unknown', '': 'unknown'}
        for status, outcome in expected.items():
            self.assertEqual(gateway.status_from_provider(status), outcome, status)

    def test_call_off_outcomes(self) -> None:
        self.assertEqual(gateway.call_off_outcome('canceled'), 'done')
        self.assertEqual(gateway.call_off_outcome('scheduled'), 'pending')
        self.assertEqual(gateway.call_off_outcome('sent'), 'failed')
        self.assertEqual(gateway.call_off_outcome('mystery'), 'unknown')

    def test_scrub_removes_phone_numbers(self) -> None:
        self.assertEqual(gateway.scrub("The 'To' number +1 555 000 2222 is invalid"),
                         "The 'To' number *** is invalid")
