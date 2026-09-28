import httpx
import pytest

from apps.subscriptions.models import MaxioWrite
from apps.subscriptions.outcomes import answer
from apps.subscriptions.service import subscription_reference

from .conftest import FAMILY_ROUTE, customer, empty_response, json_response, product, subscription

pytestmark = pytest.mark.django_db

CREATE_CUSTOMER = 'POST /customers.json'
FIND_CUSTOMER = 'GET /customers/lookup.json'
CREATE_SUBSCRIPTION = 'POST /subscriptions.json'
FIND_SUBSCRIPTION = 'GET /subscriptions/lookup.json'
LIST_CUSTOMER_SUBSCRIPTIONS = 'GET /customers/{id}/subscriptions.json'


def subscribe(api, plan='pro', **headers):
    return api.post('/api/subscriptions', {'planHandle': plan}, content_type='application/json',
                    headers=headers)


def sub_ref(user, plan='pro', key=None):
    return subscription_reference(user, plan, key)


# --- plans -----------------------------------------------------------------

def test_plans_list_every_unarchived_plan_with_its_handle(client, transport):
    transport.routes[FAMILY_ROUTE] = [json_response(200, [
        product('pro', 29900), product('basic', 2900),
        product('old', 100, archived_at='2026-01-01T00:00:00Z')])]

    response = client.get('/api/subscription-plans')

    assert response.status_code == 200
    plans = response.json()['plans']
    assert [p['planHandle'] for p in plans] == ['pro', 'basic']
    assert plans[0]['price'] == {'amountInCents': 29900, 'amount': '299.00', 'currency': 'USD',
                                 'interval': 1, 'intervalUnit': 'month'}
    assert 'per_page=200' in transport.calls(FAMILY_ROUTE)[0].url


def test_unknown_product_family_is_a_provider_problem_not_the_callers(client, transport):
    # Maxio answers an unknown family with a 404 and an empty body.
    transport.routes[FAMILY_ROUTE] = [empty_response(404)]

    response = client.get('/api/subscription-plans')

    assert response.status_code == 502
    assert response.json()['error']['code'] == 'billing_unreadable'


def test_bad_credentials_are_never_reported_as_the_callers_401(client, transport):
    transport.routes[FAMILY_ROUTE] = [empty_response(401)]
    assert client.get('/api/subscription-plans').status_code == 502


# --- subscribe -------------------------------------------------------------

def test_anonymous_callers_must_sign_in(client, transport):
    response = client.post('/api/subscriptions', {'planHandle': 'pro'},
                           content_type='application/json')
    assert response.status_code == 401
    assert transport.calls(CREATE_SUBSCRIPTION) == []


def test_subscribe_creates_customer_and_subscription(api, user, transport):
    transport.on(CREATE_CUSTOMER, json_response(201, customer(f'test:u:{user.pk}')))
    transport.on(CREATE_SUBSCRIPTION, json_response(201, subscription(reference=sub_ref(user))))

    response = subscribe(api)

    assert response.status_code == 201
    body = response.json()
    assert body['subscriptionId'] == 9001
    assert body['status'] == 'done'
    assert body['state'] == 'active'
    assert body['plan']['planHandle'] == 'pro'
    assert body['price']['amount'] == '299.00'
    assert body['nextBillingAt'].startswith('2026-10-28')

    sent_customer = transport.calls(CREATE_CUSTOMER)[0].body.value['customer']
    assert sent_customer['reference'] == f'test:u:{user.pk}'
    assert sent_customer['email'] == 'sam@example.com'
    assert transport.calls(CREATE_SUBSCRIPTION)[0].timeout == 30.0
    sent_subscription = transport.calls(CREATE_SUBSCRIPTION)[0].body.value['subscription']
    assert sent_subscription == {'product_handle': 'pro', 'customer_id': 501,
                                 'reference': sub_ref(user),
                                 'payment_collection_method': 'remittance'}


def test_the_same_request_twice_makes_one_customer_and_one_subscription(api, user, transport):
    transport.on(CREATE_CUSTOMER, json_response(201, customer(f'test:u:{user.pk}')))
    transport.on(CREATE_SUBSCRIPTION, json_response(201, subscription()))
    transport.on(FIND_SUBSCRIPTION, json_response(200, subscription()))

    first = subscribe(api)
    second = subscribe(api)  # a double click

    assert len(transport.calls(CREATE_CUSTOMER)) == 1
    assert len(transport.calls(CREATE_SUBSCRIPTION)) == 1
    assert second.status_code == first.status_code == 201
    assert second.json()['subscriptionId'] == first.json()['subscriptionId'] == 9001


def test_a_new_idempotency_key_is_a_second_subscription(api, user, transport):
    transport.on(CREATE_CUSTOMER, json_response(201, customer(f'test:u:{user.pk}')))
    transport.on(CREATE_SUBSCRIPTION, json_response(201, subscription(subscription_id=1)),
                 json_response(201, subscription(subscription_id=2)))

    subscribe(api, **{'Idempotency-Key': 'a'})
    subscribe(api, **{'Idempotency-Key': 'b'})

    refs = [r.body.value['subscription']['reference'] for r in transport.calls(CREATE_SUBSCRIPTION)]
    assert refs == [sub_ref(user, key='a'), sub_ref(user, key='b')]
    assert len(transport.calls(CREATE_CUSTOMER)) == 1  # the customer is reused


def test_an_existing_customer_reference_is_a_landing_not_a_failure(api, user, transport):
    ref = f'test:u:{user.pk}'
    transport.on(CREATE_CUSTOMER, json_response(
        422, {'errors': ['Reference: must be unique - that value has been taken.']}))
    transport.on(FIND_CUSTOMER, json_response(200, customer(ref, customer_id=777)))
    transport.on(CREATE_SUBSCRIPTION, json_response(201, subscription(customer_id=777)))

    response = subscribe(api)

    assert response.status_code == 201
    assert transport.calls(CREATE_SUBSCRIPTION)[0].body.value['subscription']['customer_id'] == 777


@pytest.mark.parametrize('state, status, outcome', [
    ('pending', 202, 'pending'),
    ('past_due', 202, 'pending'),
    ('failed_to_create', 409, 'failed'),
    ('canceled', 409, 'failed'),
    ('something_new', 504, 'unknown'),
])
def test_an_id_back_is_not_success_unless_the_state_says_so(api, user, transport, state, status, outcome):
    transport.on(CREATE_CUSTOMER, json_response(201, customer(f'test:u:{user.pk}')))
    transport.on(CREATE_SUBSCRIPTION, json_response(201, subscription(state=state)))

    response = subscribe(api)

    assert response.status_code == status
    assert response.json()['status'] == outcome
    assert response.json()['subscriptionId'] == 9001


def test_a_subscription_not_as_asked_needs_review(api, user, transport):
    transport.on(CREATE_CUSTOMER, json_response(201, customer(f'test:u:{user.pk}')))
    transport.on(CREATE_SUBSCRIPTION, json_response(201, subscription(price=100)))

    response = subscribe(api)

    assert response.status_code == 409
    assert response.json()['status'] == 'needs_review'


def test_a_provider_rejection_is_the_callers_422_and_releases_the_claim(api, user, transport):
    transport.on(CREATE_CUSTOMER, json_response(201, customer(f'test:u:{user.pk}')))
    transport.on(CREATE_SUBSCRIPTION, json_response(422, {'errors': ['Product is archived']}),
                 json_response(201, subscription()))

    rejected = subscribe(api)
    retried = subscribe(api)

    assert rejected.status_code == 422
    assert rejected.json()['error']['details'] == ['Product is archived']
    assert retried.status_code == 201
    refs = {r.body.value['subscription']['reference'] for r in transport.calls(CREATE_SUBSCRIPTION)}
    assert refs == {sub_ref(user)}  # the retry reuses the same reference


def test_a_plan_that_needs_a_card_is_refused_before_any_write(api, transport):
    transport.routes[FAMILY_ROUTE] = [json_response(200, [product('pro', 29900, require_credit_card=True)])]
    response = subscribe(api)
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'payment_method_required'
    assert transport.calls(CREATE_CUSTOMER) == []


def test_unknown_plan_is_rejected_before_any_write(api, transport):
    response = subscribe(api, plan='nope')
    assert response.status_code == 400
    assert transport.calls(CREATE_CUSTOMER) == []


def test_a_refused_connection_is_known_not_to_have_happened(api, user, transport):
    transport.on(CREATE_CUSTOMER, json_response(201, customer(f'test:u:{user.pk}')))
    transport.on(CREATE_SUBSCRIPTION, httpx.ConnectError('refused'))

    response = subscribe(api)

    assert (response.status_code, response.json()['error']['outcomeUnknown']) == (502, False)
    assert transport.calls(FIND_SUBSCRIPTION) == []  # nothing to look up
    assert MaxioWrite.objects.get(reference=sub_ref(user)).outcome == 'failed'


def test_a_read_timeout_is_looked_up_by_reference_and_stays_unknown_until_found(api, user, transport):
    transport.on(CREATE_CUSTOMER, json_response(201, customer(f'test:u:{user.pk}')))
    transport.on(CREATE_SUBSCRIPTION, httpx.ReadTimeout('no reply'))
    transport.on(FIND_SUBSCRIPTION, empty_response(404))

    response = subscribe(api)

    assert (response.status_code, response.json()['error']['outcomeUnknown']) == (504, True)
    lookup = transport.calls(FIND_SUBSCRIPTION)[0]
    assert f'reference={sub_ref(user)}'.replace(':', '%3A') in lookup.url
    assert MaxioWrite.objects.get(reference=sub_ref(user)).outcome == 'unknown'

    # Repeating the request checks again - it never creates under a new reference.
    transport.routes[FIND_SUBSCRIPTION] = [json_response(200, subscription())]
    again = subscribe(api)

    assert again.status_code == 201
    assert again.json()['subscriptionId'] == 9001
    assert len(transport.calls(CREATE_SUBSCRIPTION)) == 1


def test_an_unreadable_success_body_is_unknown_not_failed(api, user, transport):
    transport.on(CREATE_CUSTOMER, json_response(201, customer(f'test:u:{user.pk}')))
    transport.on(CREATE_SUBSCRIPTION, json_response(201, {'subscription': {'id': 'not-a-number'}}))
    transport.on(FIND_SUBSCRIPTION, empty_response(404))

    response = subscribe(api)

    assert response.status_code == 504
    assert response.json()['error']['outcomeUnknown'] is True


def test_a_claim_in_flight_answers_in_progress_without_calling_maxio(api, user, transport):
    from django.utils import timezone
    transport.on(CREATE_CUSTOMER, json_response(201, customer(f'test:u:{user.pk}')))
    MaxioWrite.objects.create(reference=sub_ref(user), kind='subscription', user=user,
                              plan_handle='pro', outcome='sending', claimed_at=timezone.now())

    response = subscribe(api)

    assert response.status_code == 202
    assert response.json()['status'] == 'sending'
    assert transport.calls(CREATE_SUBSCRIPTION) == []


# --- my subscriptions ------------------------------------------------------

def test_my_subscriptions_reads_back_from_maxio_and_settles_unknown_writes(api, user, transport):
    transport.on(CREATE_CUSTOMER, json_response(201, customer(f'test:u:{user.pk}')))
    transport.on(CREATE_SUBSCRIPTION, httpx.ReadTimeout('no reply'))
    transport.on(FIND_SUBSCRIPTION, empty_response(404))
    assert subscribe(api).status_code == 504

    transport.routes[FIND_SUBSCRIPTION] = [json_response(200, subscription())]
    transport.on(LIST_CUSTOMER_SUBSCRIPTIONS, json_response(200, [subscription()]))

    response = api.get('/api/my-subscriptions')

    assert response.status_code == 200
    [entry] = response.json()['subscriptions']
    assert entry['subscriptionId'] == 9001
    assert entry['status'] == 'done'
    assert entry['reference'] == sub_ref(user)
    assert MaxioWrite.objects.get(reference=sub_ref(user)).outcome == 'done'


def test_my_subscriptions_is_empty_before_subscribing(api, transport):
    response = api.get('/api/my-subscriptions')
    assert response.json() == {'subscriptions': []}


def test_no_outcome_but_done_answers_success():
    for outcome in ('pending', 'sending', 'failed', 'needs_review', 'unknown', 'anything'):
        assert answer(outcome, created=True) not in (200, 201, 204)
