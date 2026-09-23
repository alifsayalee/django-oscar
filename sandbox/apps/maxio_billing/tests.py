"""
Tests for the Maxio billing API. The SDK's transport is replaced with a stub, so
the real request-building and decoding pipeline runs without any network.

Run with: sandbox/manage.py test apps.maxio_billing
"""
import json
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from maxio_advanced_billing.core import HttpRequest, HttpResponse
from maxio_advanced_billing.models import (
    Customer, CustomerResponse, Product, ProductResponse, Subscription, SubscriptionResponse)
from maxio_advanced_billing.models.enums import SubscriptionState

from . import client as client_module
from .models import BillingCustomer, SubscriptionEnrollment

NEXT_BILLING = datetime(2026, 10, 24, 12, 0, tzinfo=dt_timezone.utc)
MAXIO_CUSTOMER_ID = 77


def json_response(status, body):
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


def product(handle='basic-plan', price=2900, name='Basic Plan'):
    return Product(id=10, handle=handle, name=name, price_in_cents=price, interval=1, interval_unit='month')


def subscription_body(reference, *, state=SubscriptionState.ACTIVE, sub_id=555, handle='basic-plan',
                      price=2900, customer_id=MAXIO_CUSTOMER_ID):
    return SubscriptionResponse(subscription=Subscription(
        id=sub_id, state=state, reference=reference, product_price_in_cents=price,
        next_assessment_at=NEXT_BILLING, current_period_ends_at=NEXT_BILLING,
        customer=Customer(id=customer_id, reference='cust-ref'),
        product=product(handle=handle, price=price),
    )).to_dict()


class StubMaxio:
    """Satisfies the SDK's sync transport protocol; answers by (method, path)."""

    def __init__(self):
        self.routes = {}
        self.requests = []

    def on(self, method, path, *answers):
        self.routes.setdefault((method, path), []).extend(answers)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        key = (request.method, urlsplit(request.url).path)
        queue = self.routes.get(key)
        if not queue:
            raise AssertionError(f'unexpected Maxio call {key}')
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        response: HttpResponse = answer(request) if callable(answer) else answer
        return response

    def close(self):
        pass

    def calls(self, method, path):
        return [r for r in self.requests if r.method == method and urlsplit(r.url).path == path]


PLANS = ('GET', '/product_families/handle%3Atest-family/products.json')
CUSTOMERS = ('POST', '/customers.json')
CUSTOMER_LOOKUP = ('GET', '/customers/lookup.json')
SUBSCRIPTIONS = ('POST', '/subscriptions.json')
SUBSCRIPTION_LOOKUP = ('GET', '/subscriptions/lookup.json')


def echo_subscription(**kwargs):
    def answer(request):
        return json_response(201, subscription_body(request.body.value['subscription']['reference'], **kwargs))
    return answer


def echo_customer(request):
    sent = request.body.value['customer']
    return json_response(201, CustomerResponse(customer=Customer(
        id=MAXIO_CUSTOMER_ID, reference=sent['reference'], email=sent['email'])).to_dict())


@override_settings(
    MAXIO_API_KEY='test-key', MAXIO_SITE_SUBDOMAIN='test-site', MAXIO_BASE_URL='',
    MAXIO_DEFAULT_PRODUCT_FAMILY='test-family', MAXIO_ENVIRONMENT='US',
    MAXIO_TIMEOUT_SECONDS=5.0, MAXIO_PAYMENT_COLLECTION_METHOD='remittance')
class BillingApiTestCase(TestCase):

    def setUp(self):
        cache.clear()
        self.stub = StubMaxio()
        self.stub.on(*PLANS, json_response(200, [
            ProductResponse(product=product()).to_dict(),
            ProductResponse(product=product('eshop-pro', 29900, 'Pro Plan')).to_dict(),
        ]))
        self.previous = client_module.use_client(client_module.build_client(transport=self.stub))
        self.user = get_user_model().objects.create_user(
            username='shopper', email='shopper@example.com', password='a-long-password-1')
        self.client.force_login(self.user)

    def tearDown(self):
        client_module.use_client(self.previous)

    def subscribe(self, plan='basic-plan'):
        return self.client.post('/api/subscriptions', data=json.dumps({'planHandle': plan}),
                                content_type='application/json')

    def link_customer(self):
        return BillingCustomer.objects.create(
            user=self.user, status=BillingCustomer.LINKED, maxio_customer_id=MAXIO_CUSTOMER_ID)


class PlanTests(BillingApiTestCase):

    def test_lists_plans_with_handles(self):
        response = self.client.get('/api/subscription-plans')
        self.assertEqual(response.status_code, 200)
        plans = response.json()['plans']
        self.assertEqual([p['planHandle'] for p in plans], ['basic-plan', 'eshop-pro'])
        self.assertEqual(plans[1]['price'], '299.00')
        request = self.stub.requests[0]
        self.assertIn('https://test-site.chargify.com/product_families/handle%3Atest-family/products.json',
                      request.url)
        self.assertTrue(request.headers['authorization'].startswith('Basic '))

    def test_plans_are_cached(self):
        self.client.get('/api/subscription-plans')
        self.client.get('/api/subscription-plans')
        self.assertEqual(len(self.stub.calls(*PLANS)), 1)

    @override_settings(MAXIO_BASE_URL='https://billing.example.test/v1')
    def test_base_url_override_is_used_verbatim(self):
        client_module.use_client(client_module.build_client(transport=self.stub))
        self.stub.on('GET', '/v1' + PLANS[1], self.stub.routes[PLANS][0])
        self.client.get('/api/subscription-plans')
        self.assertTrue(self.stub.requests[0].url.startswith(
            'https://billing.example.test/v1/product_families/'))


class SubscribeTests(BillingApiTestCase):

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.subscribe().status_code, 401)
        self.assertEqual(self.client.get('/api/my-subscriptions').status_code, 401)

    def test_subscribe_creates_customer_then_subscription(self):
        self.stub.on(*CUSTOMERS, echo_customer)
        self.stub.on(*SUBSCRIPTIONS, echo_subscription())

        response = self.subscribe()

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['subscriptionId'], 555)
        self.assertEqual(body['status'], 'active')
        self.assertEqual(body['planHandle'], 'basic-plan')
        self.assertEqual(body['price'], '29.00')
        self.assertEqual(body['nextBillingAt'], NEXT_BILLING.isoformat())

        customer = BillingCustomer.objects.get(user=self.user)
        sent_customer = self.stub.calls(*CUSTOMERS)[0].body.value['customer']
        self.assertEqual(sent_customer['reference'], customer.reference)
        self.assertEqual(sent_customer['email'], 'shopper@example.com')

        row = SubscriptionEnrollment.objects.get(user=self.user)
        sent = self.stub.calls(*SUBSCRIPTIONS)[0].body.value['subscription']
        self.assertEqual(sent, {
            'product_handle': 'basic-plan', 'customer_id': MAXIO_CUSTOMER_ID,
            'reference': row.reference, 'payment_collection_method': 'remittance'})
        self.assertEqual((row.status, row.maxio_subscription_id), ('active', 555))

    def test_repeat_submit_never_subscribes_twice(self):
        self.stub.on(*CUSTOMERS, echo_customer)
        self.stub.on(*SUBSCRIPTIONS, echo_subscription())

        first = self.subscribe()
        reference = SubscriptionEnrollment.objects.get().reference
        self.stub.on('GET', '/subscriptions/555.json', json_response(200, subscription_body(reference)))
        second = self.subscribe()

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['subscriptionId'], 555)
        self.assertEqual(second.json()['nextBillingAt'], NEXT_BILLING.isoformat())
        self.assertTrue(second.json()['existing'])
        self.assertEqual(len(self.stub.calls(*SUBSCRIPTIONS)), 1)
        self.assertEqual(len(self.stub.calls(*CUSTOMERS)), 1)

    def test_resubscribe_after_the_held_subscription_ended(self):
        customer = self.link_customer()
        old = SubscriptionEnrollment.objects.create(
            user=self.user, customer=customer, plan_handle='basic-plan', status='active',
            maxio_subscription_id=444, state='active')
        self.stub.on('GET', '/subscriptions/444.json', json_response(
            200, subscription_body(old.reference, sub_id=444, state=SubscriptionState.CANCELED)))
        self.stub.on(*SUBSCRIPTIONS, echo_subscription())

        response = self.subscribe()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['subscriptionId'], 555)
        old.refresh_from_db()
        self.assertEqual(old.status, 'ended')

    def test_request_in_flight_is_not_sent_again(self):
        customer = self.link_customer()
        SubscriptionEnrollment.objects.create(user=self.user, customer=customer, plan_handle='basic-plan')

        response = self.subscribe()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'in_progress')
        self.assertEqual(self.stub.calls(*SUBSCRIPTIONS), [])

    def test_unknown_plan_is_rejected_before_any_write(self):
        response = self.subscribe('no-such-plan')
        self.assertEqual(response.status_code, 422)
        self.assertFalse(BillingCustomer.objects.exists())
        self.assertEqual(self.stub.calls(*CUSTOMERS), [])

    def test_refused_connection_is_known_and_releases_the_claim(self):
        self.link_customer()
        self.stub.on(*SUBSCRIPTIONS, httpx.ConnectError('refused'))

        response = self.subscribe()

        self.assertEqual(response.status_code, 502)
        self.assertEqual(SubscriptionEnrollment.objects.get().status, 'failed')
        self.assertEqual(self.stub.calls(*SUBSCRIPTION_LOOKUP), [])   # nothing to reconcile

    def test_read_timeout_is_unknown_and_reconciled_by_reference(self):
        self.link_customer()
        self.stub.on(*SUBSCRIPTIONS, httpx.ReadTimeout('no reply'))
        self.stub.on(*SUBSCRIPTION_LOOKUP, json_response(404, {}))

        response = self.subscribe()

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()['error']['code'], 'outcome_unknown')
        row = SubscriptionEnrollment.objects.get()
        self.assertEqual(row.status, 'unknown')
        self.assertIn(f'reference={row.reference}', self.stub.calls(*SUBSCRIPTION_LOOKUP)[0].url)

        # It did land after all: the repeat finds it by reference and never creates again.
        self.stub.routes[SUBSCRIPTION_LOOKUP] = [json_response(200, subscription_body(row.reference))]
        again = self.subscribe()
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()['subscriptionId'], 555)
        self.assertEqual(len(self.stub.calls(*SUBSCRIPTIONS)), 1)
        self.assertEqual(SubscriptionEnrollment.objects.get().status, 'active')

    def test_unknown_outcome_not_found_is_resent_under_the_same_reference(self):
        customer = self.link_customer()
        row = SubscriptionEnrollment.objects.create(
            user=self.user, customer=customer, plan_handle='basic-plan', status='unknown')
        self.stub.on(*SUBSCRIPTION_LOOKUP, json_response(404, {}))
        self.stub.on(*SUBSCRIPTIONS, echo_subscription())

        response = self.subscribe()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.stub.calls(*SUBSCRIPTIONS)[0].body.value['subscription']['reference'],
                         row.reference)

    def test_validation_rejection_is_the_callers_and_releases_the_claim(self):
        self.link_customer()
        self.stub.on(*SUBSCRIPTIONS, json_response(422, {'errors': ['No payment method was on file']}))
        self.stub.on(*SUBSCRIPTION_LOOKUP, json_response(404, {}))

        response = self.subscribe()

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['details'], ['No payment method was on file'])
        self.assertEqual(SubscriptionEnrollment.objects.get().status, 'failed')

    def test_reference_taken_means_an_earlier_attempt_landed(self):
        self.link_customer()
        self.stub.on(*SUBSCRIPTIONS, json_response(
            422, {'errors': ['Reference: must be unique - that value has been taken.']}))
        self.stub.on(*SUBSCRIPTION_LOOKUP, lambda request: json_response(
            200, subscription_body(SubscriptionEnrollment.objects.get().reference)))

        response = self.subscribe()

        self.assertEqual(response.json()['subscriptionId'], 555)
        self.assertEqual(SubscriptionEnrollment.objects.get().status, 'active')

    def test_provider_auth_failure_is_ours_not_the_callers(self):
        self.link_customer()
        self.stub.on(*SUBSCRIPTIONS, json_response(401, {}))
        response = self.subscribe()
        self.assertEqual(response.status_code, 502)

    def test_an_id_with_a_failed_state_is_not_success(self):
        self.link_customer()
        self.stub.on(*SUBSCRIPTIONS, echo_subscription(state=SubscriptionState.FAILED_TO_CREATE))
        response = self.subscribe()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['status'], 'failed')

    def test_an_unlisted_state_is_unknown_not_active(self):
        self.link_customer()
        self.stub.on(*SUBSCRIPTIONS, echo_subscription(state='something_new'))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['status'], 'unknown')

    def test_past_due_is_accepted_not_active(self):
        self.link_customer()
        self.stub.on(*SUBSCRIPTIONS, echo_subscription(state=SubscriptionState.PAST_DUE))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['status'], 'attention')

    def test_price_mismatch_is_kept_for_review(self):
        self.link_customer()
        self.stub.on(*SUBSCRIPTIONS, echo_subscription(price=100))
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(SubscriptionEnrollment.objects.get().status, 'needs_review')

    def test_unreadable_success_body_is_reconciled_not_failed(self):
        self.link_customer()
        self.stub.on(*SUBSCRIPTIONS, json_response(201, {'subscription': {'id': 'not-a-number'}}))
        self.stub.on(*SUBSCRIPTION_LOOKUP, lambda request: json_response(
            200, subscription_body(SubscriptionEnrollment.objects.get().reference)))
        response = self.subscribe()
        self.assertEqual(response.json()['subscriptionId'], 555)


class CustomerTests(BillingApiTestCase):

    def test_ensure_customer_is_idempotent(self):
        self.stub.on(*CUSTOMERS, echo_customer)
        first = self.client.post('/api/billing-customer', data='{}', content_type='application/json')
        second = self.client.post('/api/billing-customer', data='{}', content_type='application/json')
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(second.json()['customer']['maxioCustomerId'], MAXIO_CUSTOMER_ID)
        self.assertEqual(len(self.stub.calls(*CUSTOMERS)), 1)

    def test_stale_customer_claim_is_looked_up_before_resending(self):
        row = BillingCustomer.objects.create(user=self.user, status=BillingCustomer.SENDING)
        BillingCustomer.objects.filter(pk=row.pk).update(updated=timezone.now() - timedelta(hours=1))
        self.stub.on(*CUSTOMER_LOOKUP, json_response(200, CustomerResponse(
            customer=Customer(id=MAXIO_CUSTOMER_ID, reference=row.reference)).to_dict()))

        response = self.client.post('/api/billing-customer', data='{}', content_type='application/json')

        self.assertEqual(response.json()['customer']['maxioCustomerId'], MAXIO_CUSTOMER_ID)
        self.assertEqual(self.stub.calls(*CUSTOMERS), [])


class ReadBackTests(BillingApiTestCase):

    def test_my_subscriptions_reads_maxio_and_refreshes_state(self):
        customer = self.link_customer()
        row = SubscriptionEnrollment.objects.create(
            user=self.user, customer=customer, plan_handle='basic-plan', status='active',
            maxio_subscription_id=555, state='active')
        self.stub.on('GET', f'/customers/{MAXIO_CUSTOMER_ID}/subscriptions.json', json_response(
            200, [subscription_body(row.reference, state=SubscriptionState.CANCELED)]))

        response = self.client.get('/api/my-subscriptions')

        self.assertEqual(response.status_code, 200)
        [sub] = response.json()['subscriptions']
        self.assertEqual((sub['subscriptionId'], sub['state'], sub['status']), (555, 'canceled', 'ended'))
        self.assertEqual(SubscriptionEnrollment.objects.get().status, 'ended')

    def test_no_customer_means_no_subscriptions_and_no_provider_call(self):
        response = self.client.get('/api/my-subscriptions')
        self.assertEqual(response.json()['subscriptions'], [])
        self.assertEqual(self.stub.requests, [])

    def test_someone_elses_subscription_is_not_found(self):
        self.link_customer()
        self.stub.on('GET', '/subscriptions/999.json', json_response(
            200, subscription_body('other', sub_id=999, customer_id=12345)))
        self.assertEqual(self.client.get('/api/subscriptions/999').status_code, 404)

    def test_own_subscription_detail(self):
        self.link_customer()
        self.stub.on('GET', '/subscriptions/555.json', json_response(200, subscription_body('mine')))
        response = self.client.get('/api/subscriptions/555')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['nextBillingAt'], NEXT_BILLING.isoformat())
