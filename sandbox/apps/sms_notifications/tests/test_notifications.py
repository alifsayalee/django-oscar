import logging
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest
from twilio_sdk.core import UNSET, FormBody
from twilio_sdk.models.enums import MessageEnumStatus

from apps.sms_notifications import outcomes
from apps.sms_notifications.gateway import reference_token
from apps.sms_notifications.models import ContactNumber, SmsNotification
from apps.sms_notifications.services import NotificationService, order_reference

from .conftest import FROM_NUMBER, SERVICE_SID, SHOPPER_NUMBER, json_response

pytestmark = pytest.mark.django_db(transaction=True)


def fields(request):
    assert isinstance(request.body, FormBody)
    return {k: v if isinstance(v, str) else v[0] for k, v in request.body.fields.items()}


def register(api, number='(825) 555-0100'):
    response = api.post('contact-numbers', {'phoneNumber': number})
    assert response.status_code == 201, response.content
    return response.json()


def place(api, product, quantity=1):
    response = api.post('orders', {'items': [{'productId': product.pk, 'quantity': quantity}]})
    assert response.status_code == 201, response.content
    return response.json()


# -- Status mapping -------------------------------------------------------------------------------

@pytest.mark.parametrize('status, expected', [
    (MessageEnumStatus.DELIVERED, 'done'), (MessageEnumStatus.READ, 'done'),
    (MessageEnumStatus.QUEUED, 'pending'), (MessageEnumStatus.SENDING, 'pending'),
    (MessageEnumStatus.SENT, 'pending'), (MessageEnumStatus.ACCEPTED, 'pending'),
    (MessageEnumStatus.SCHEDULED, 'pending'), (MessageEnumStatus.PARTIALLY_DELIVERED, 'pending'),
    (MessageEnumStatus.FAILED, 'failed'), (MessageEnumStatus.UNDELIVERED, 'failed'),
    (MessageEnumStatus.CANCELED, 'failed'),
    (MessageEnumStatus.RECEIVED, 'unknown'), (MessageEnumStatus.RECEIVING, 'unknown'),
    ('something_new', 'unknown'), (UNSET, 'unknown'), (None, 'unknown'),
])
def test_send_status_mapping(status, expected):
    assert outcomes.status_from_provider(status) == expected


def test_call_off_mapping_treats_already_sent_as_failed():
    assert outcomes.cancel_outcome_from_provider('canceled') == 'done'
    assert outcomes.cancel_outcome_from_provider('delivered') == 'failed'
    assert outcomes.cancel_outcome_from_provider('scheduled') == 'pending'
    assert outcomes.cancel_outcome_from_provider('brand_new') == 'unknown'


# -- Contact numbers ---------------------------------------------------------------------------------

def test_registered_number_is_the_providers_canonical_form(fake_twilio, shopper, api_as, caplog):
    caplog.set_level(logging.DEBUG)
    body = register(api_as(shopper))
    assert body['phoneNumber'] == SHOPPER_NUMBER and 'contactNumberId' in body
    assert ContactNumber.objects.get().phone_number == SHOPPER_NUMBER
    assert '555-0100' not in caplog.text and SHOPPER_NUMBER not in caplog.text


def test_unusable_number_is_rejected_at_registration(fake_twilio, shopper, api_as):
    fake_twilio.invalid_numbers.add(SHOPPER_NUMBER)
    response = api_as(shopper).post('contact-numbers', {'phoneNumber': SHOPPER_NUMBER})
    assert response.status_code == 422
    assert not ContactNumber.objects.exists()


def test_numbers_are_private_to_their_owner(fake_twilio, shopper, other_shopper, api_as):
    contact = register(api_as(shopper))
    other = api_as(other_shopper)
    assert other.get('contact-numbers').json() == {'contactNumbers': []}
    assert other.delete('contact-numbers/%d' % contact['contactNumberId']).status_code == 404
    assert ContactNumber.objects.count() == 1


def test_anonymous_callers_are_refused(client):
    assert client.get('/api/contact-numbers').status_code == 401


# -- Order flow --------------------------------------------------------------------------------------

def test_placing_an_order_tells_the_shopper(fake_twilio, shopper, product, api_as):
    api = api_as(shopper)
    register(api)
    order = place(api, product, 2)
    assert 'orderId' in order and order['lines'][0]['quantity'] == 2
    [create] = fake_twilio.creates()
    sent = fields(create)
    assert sent['To'] == SHOPPER_NUMBER and sent['From'] == FROM_NUMBER
    reference = order_reference(type('O', (), {'number': order['number']}), 'order_placed')
    assert sent['Body'].endswith('Ref ' + reference_token(reference))
    assert create.headers['idempotency-key']            # same key on every attempt for this reference
    [note] = order['notifications']
    assert note['kind'] == 'order_placed' and note['outcome'] == 'pending' and note['providerStatus'] == 'queued'


def test_shopper_without_a_number_is_simply_not_messaged(fake_twilio, shopper, product, api_as):
    order = place(api_as(shopper), product)
    assert order['notifications'] == [] and fake_twilio.creates() == []


def test_a_refused_send_never_fails_the_order(fake_twilio, shopper, product, api_as):
    api = api_as(shopper)
    register(api)
    fake_twilio.fail_next('POST', '/Messages.json', json_response(401, {'code': 20003, 'status': 401}))
    order = place(api, product)
    assert order['notifications'][0]['outcome'] == 'failed'


def test_delivery_outcome_is_read_back_from_the_provider(fake_twilio, shopper, product, api_as):
    api = api_as(shopper)
    register(api)
    order = place(api, product)
    fake_twilio.deliver(order['notifications'][0]['providerSid'], 'delivered')
    [listed] = api.get('my-orders').json()['orders']
    assert listed['notifications'][0]['outcome'] == 'done'
    assert listed['notifications'][0]['sentAt'].startswith('2026-09-25T10:00:05')


def test_dispatch_notifies_and_queues_the_followup_with_the_provider(fake_twilio, shopper, operator, product,
                                                                        api_as):
    register(api_as(shopper))
    order = place(api_as(shopper), product)
    response = api_as(operator).post('orders/%d/dispatch' % order['orderId'])
    assert response.status_code == 200, response.content
    kinds = {n['kind']: n for n in response.json()['notifications']}
    assert kinds['order_dispatched']['outcome'] == 'pending'
    followup = kinds['delivery_followup']
    assert followup['providerStatus'] == 'scheduled' and followup['outcome'] == 'pending'
    sent = fields(fake_twilio.creates()[-1])
    assert sent['ScheduleType'] == 'fixed' and sent['MessagingServiceSid'] == SERVICE_SID
    assert sent['From'] == FROM_NUMBER
    send_at = datetime.fromisoformat(sent['SendAt'].replace('Z', '+00:00'))
    assert (send_at - datetime.now(timezone.utc)).total_seconds() > 71 * 3600


def test_dispatch_is_operator_only(fake_twilio, shopper, product, api_as):
    order = place(api_as(shopper), product)
    assert api_as(shopper).post('orders/%d/dispatch' % order['orderId']).status_code == 403


def test_the_same_dispatch_twice_sends_each_message_once(fake_twilio, shopper, operator, product, api_as):
    register(api_as(shopper))
    order = place(api_as(shopper), product)
    op = api_as(operator)
    op.post('orders/%d/dispatch' % order['orderId'])
    op.post('orders/%d/dispatch' % order['orderId'])
    assert len(fake_twilio.creates()) == 3               # placed + dispatched + follow-up
    assert SmsNotification.objects.count() == 3


def test_cancel_calls_off_the_queued_followup_and_tells_the_shopper(fake_twilio, shopper, operator, product,
                                                                     api_as):
    register(api_as(shopper))
    order = place(api_as(shopper), product)
    op = api_as(operator)
    op.post('orders/%d/dispatch' % order['orderId'])
    response = op.post('orders/%d/cancel' % order['orderId'])
    assert response.status_code == 200 and response.json()['status'] == 'Cancelled'
    kinds = {n['kind']: n for n in response.json()['notifications']}
    followup = kinds['delivery_followup']
    assert followup['callOff']['outcome'] == 'done' and followup['providerStatus'] == 'canceled'
    assert fake_twilio.messages[followup['providerSid']]['status'] == 'canceled'
    assert fields(fake_twilio.updates()[0]) == {'Status': 'canceled'}
    assert kinds['order_cancelled']['outcome'] == 'pending'


def test_a_cancel_that_lands_while_the_followup_is_being_queued_still_calls_it_off(
        fake_twilio, shopper, operator, product, api_as):
    """The cancel commits between the follow-up's status check and its create: the dispatch path cancels it."""
    register(api_as(shopper))
    order_id = place(api_as(shopper), product)['orderId']
    from apps.sms_notifications import orders as order_ops

    def cancel_then_create(request):
        order_ops.cancel_order(order_id, operator)       # a concurrent cancel commits first...
        return fake_twilio._route(request)               # ...then the provider queues the follow-up
    order, _ = order_ops.dispatch_order(order_id, operator)
    service = NotificationService()
    fake_twilio.overrides.append(('POST', '/Messages.json', cancel_then_create))
    record = service.schedule_followup(order)
    assert record is not None
    record.refresh_from_db()
    assert record.cancel_outcome == 'done'
    assert fake_twilio.messages[record.provider_sid]['status'] == 'canceled'


def test_deleting_a_number_calls_off_what_is_queued_for_it(fake_twilio, shopper, operator, product, api_as):
    api = api_as(shopper)
    contact = register(api)
    order = place(api, product)
    api_as(operator).post('orders/%d/dispatch' % order['orderId'])
    assert api.delete('contact-numbers/%d' % contact['contactNumberId']).status_code == 204
    assert api.get('contact-numbers').json() == {'contactNumbers': []}
    followup = SmsNotification.objects.get(kind='delivery_followup')
    assert fake_twilio.messages[followup.provider_sid]['status'] == 'canceled'


def test_orders_and_notifications_are_private(fake_twilio, shopper, other_shopper, product, api_as):
    register(api_as(shopper))
    order = place(api_as(shopper), product)
    other = api_as(other_shopper)
    assert other.get('orders/%d/notifications' % order['orderId']).status_code == 404
    assert other.get('my-orders').json() == {'orders': []}


# -- Unknown outcomes ----------------------------------------------------------------------------------

def test_a_timed_out_send_that_landed_is_found_by_reference_not_resent(fake_twilio, shopper, product, api_as):
    api = api_as(shopper)
    register(api)
    fake_twilio.fail_next('POST', '/Messages.json', httpx.ReadTimeout('no reply'), land=True)
    order = place(api, product)
    note = order['notifications'][0]
    assert note['outcome'] == 'pending' and note['providerSid']      # settled from the provider's record
    assert len(fake_twilio.creates()) == 1


def test_never_sent_and_maybe_sent_are_different_outcomes(fake_twilio, shopper, product, api_as):
    api = api_as(shopper)
    register(api)
    fake_twilio.fail_next('POST', '/Messages.json', httpx.ConnectError('refused'))
    unsent = place(api, product)['notifications'][0]
    fake_twilio.fail_next('POST', '/Messages.json', httpx.ReadTimeout('no reply'))   # did NOT land
    maybe = place(api, product)['notifications'][0]
    assert (unsent['outcome'], maybe['outcome']) == ('failed', 'unknown')
    # The unknown one is looked up again later under the same reference - never re-created.
    creates_before = len(fake_twilio.creates())
    api.get('my-orders')
    assert len(fake_twilio.creates()) == creates_before


def test_a_truncated_success_body_is_not_success(fake_twilio, shopper, product, api_as):
    api = api_as(shopper)
    register(api)
    fake_twilio.fail_next('POST', '/Messages.json', json_response(201, {}), land=True)
    note = place(api, product)['notifications'][0]
    assert note['outcome'] == 'pending' and note['providerSid']      # found by reference
    assert len(fake_twilio.creates()) == 1


# -- Operator: resend --------------------------------------------------------------------------------

def failed_notification(fake_twilio, shopper, product, api_as):
    register(api_as(shopper))
    order = place(api_as(shopper), product)
    note = order['notifications'][0]
    fake_twilio.deliver(note['providerSid'], 'undelivered', error_code=30007)
    api_as(shopper).get('orders/%d/notifications' % order['orderId'])
    return note['notificationId']


def test_resend_under_the_same_key_sends_once_and_a_fresh_key_sends_again(fake_twilio, shopper, operator,
                                                                          product, api_as):
    notification_id = failed_notification(fake_twilio, shopper, product, api_as)
    op = api_as(operator)
    creates = len(fake_twilio.creates())
    first = op.post('notifications/%d/resend' % notification_id, HTTP_IDEMPOTENCY_KEY='key-1')
    again = op.post('notifications/%d/resend' % notification_id, HTTP_IDEMPOTENCY_KEY='key-1')
    assert first.status_code == 201 and again.status_code == 200
    assert first.json()['notificationId'] == again.json()['notificationId'] != notification_id
    assert len(fake_twilio.creates()) == creates + 1
    fresh = op.post('notifications/%d/resend' % notification_id, {'idempotencyKey': 'key-2'})
    assert fresh.status_code == 201 and fresh.json()['notificationId'] != first.json()['notificationId']
    assert len(fake_twilio.creates()) == creates + 2
    assert first.json()['resendOf'] == notification_id


def test_resend_is_refused_for_a_message_that_is_not_known_to_have_failed(fake_twilio, shopper, operator,
                                                                          product, api_as):
    register(api_as(shopper))
    note = place(api_as(shopper), product)['notifications'][0]
    response = api_as(operator).post('notifications/%d/resend' % note['notificationId'],
                                     HTTP_IDEMPOTENCY_KEY='k')
    assert response.status_code == 409
    assert api_as(shopper).post('notifications/%d/resend' % note['notificationId'],
                                HTTP_IDEMPOTENCY_KEY='k').status_code == 403


def test_a_refused_resend_still_reports_the_message_it_produced(fake_twilio, shopper, operator, product,
                                                                 api_as):
    notification_id = failed_notification(fake_twilio, shopper, product, api_as)
    fake_twilio.fail_next('POST', '/Messages.json', json_response(401, {'code': 20003, 'status': 401}))
    response = api_as(operator).post('notifications/%d/resend' % notification_id, HTTP_IDEMPOTENCY_KEY='k')
    assert response.status_code == 502
    data = response.json()
    assert data['notificationId'] != notification_id and data['outcome'] == 'failed'


def test_resend_requires_an_idempotency_key(fake_twilio, shopper, operator, product, api_as):
    notification_id = failed_notification(fake_twilio, shopper, product, api_as)
    assert api_as(operator).post('notifications/%d/resend' % notification_id).status_code == 400


# -- Operator: content disposal ------------------------------------------------------------------------

def test_disposing_content_redacts_it_at_the_provider_and_keeps_the_outcome(fake_twilio, shopper, operator,
                                                                            product, api_as):
    register(api_as(shopper))
    note = place(api_as(shopper), product)['notifications'][0]
    fake_twilio.deliver(note['providerSid'])
    response = api_as(operator).delete('notifications/%d/content' % note['notificationId'])
    assert response.status_code == 200, response.content
    data = response.json()
    assert data['contentDisposed'] is True and data['body'] is None and data['providerSid'] == note['providerSid']
    assert fake_twilio.messages[note['providerSid']]['body'] == ''
    assert fields(fake_twilio.updates()[-1]) == {'Body': ''}
    assert SmsNotification.objects.get(pk=note['notificationId']).body == ''


# -- Operator: reconciliation --------------------------------------------------------------------------

def test_reconciliation_lines_up_both_sides_on_the_providers_clock(fake_twilio, shopper, operator, product,
                                                                   api_as):
    register(api_as(shopper))
    ours = place(api_as(shopper), product)['notifications'][0]
    fake_twilio.deliver(ours['providerSid'], date_sent='Thu, 25 Sep 2026 10:00:05 +0000')
    fake_twilio.foreign.append({'sid': 'SMforeign', 'to': '+18255550199', 'from': FROM_NUMBER,
                                'status': 'delivered', 'body': 'x', 'date_sent': 'Thu, 25 Sep 2026 11:00:00 +0000'})
    fake_twilio.foreign.append({'sid': 'SMoutside', 'to': '+18255550199', 'from': FROM_NUMBER,
                                'status': 'delivered', 'body': 'x', 'date_sent': 'Thu, 25 Sep 2026 23:30:00 +0000'})
    response = api_as(operator).get('notifications/reconciliation',
                                    {'from': '2026-09-25T09:00:00Z', 'to': '2026-09-25T12:00:00Z'})
    assert response.status_code == 200, response.content
    report = response.json()
    assert [m['providerSid'] for m in report['matched']] == [ours['providerSid']]
    assert [m['providerSid'] for m in report['providerOnly']] == ['SMforeign']   # 23:30 narrowed out
    query = parse_qs(urlsplit(fake_twilio.requests[-1].url).query)
    assert query['From'] == [FROM_NUMBER]
    assert unquote(query['DateSent>'][0]).startswith('2026-09-25T00:00:00')
    assert unquote(query['DateSent<'][0]).startswith('2026-09-26T00:00:00')


def test_reconciliation_follows_every_page(fake_twilio, operator, api_as, monkeypatch):
    from apps.sms_notifications import gateway
    monkeypatch.setattr(gateway, 'RECONCILE_PAGE_SIZE', 2)
    for i in range(5):
        fake_twilio.foreign.append({'sid': 'SMf%d' % i, 'to': '+18255550199', 'from': FROM_NUMBER,
                                    'status': 'delivered', 'body': 'x',
                                    'date_sent': 'Thu, 25 Sep 2026 10:0%d:00 +0000' % i})
    report = api_as(operator).get('notifications/reconciliation',
                                  {'from': '2026-09-25T00:00:00Z', 'to': '2026-09-26T00:00:00Z'}).json()
    assert report['summary']['providerOnly'] == 5


def test_base_url_override_applies_to_messaging_only(settings):
    from apps.sms_notifications.gateway import build_gateway
    settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN = 'AC1', 'secret'
    settings.TWILIO_FROM_NUMBER, settings.TWILIO_MESSAGING_SERVICE_SID = FROM_NUMBER, SERVICE_SID
    settings.TWILIO_BASE_URL = 'http://127.0.0.1:9'
    gateway = build_gateway()
    try:
        with pytest.raises(httpx.ConnectError):
            gateway.fetch_message('SM1')          # proves the messaging call went to the override host
    finally:
        gateway.close()
