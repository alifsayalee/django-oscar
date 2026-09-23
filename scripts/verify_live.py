"""Live end-to-end verification against the real PayPal sandbox.

Drives the actual /api/ HTTP endpoints through Django's test client (real URL
routing, auth, views and a REAL PayPalService that talks to PayPal), using the
sandbox test card. Verifies: authorize -> fulfil(capture) -> refund, and a saved
card reused to pay a second order. Prints each step.

Run from the repo root:
    venv/Scripts/python.exe scripts/verify_live.py
"""

import json
import os
import sys
import uuid
from decimal import Decimal

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'sandbox'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings')

import logging  # noqa: E402

import django  # noqa: E402

django.setup()

# Keep the transport chatter out of the verification output.
for _name in ('httpx', 'httpcore'):
    logging.getLogger(_name).setLevel(logging.WARNING)

from django.contrib.auth import get_user_model  # noqa: E402
from django.test import Client  # noqa: E402
from oscar.core.loading import get_model  # noqa: E402

User = get_user_model()
Product = get_model('catalogue', 'Product')
ProductClass = get_model('catalogue', 'ProductClass')
Partner = get_model('partner', 'Partner')
StockRecord = get_model('partner', 'StockRecord')

CARD = {
    'name': 'Test Buyer',
    'number': '4111111111111111',
    'expiry': '2030-12',
    'securityCode': '123',
    'billingAddress': {
        'addressLine1': '1 Main St',
        'adminArea2': 'San Jose',
        'adminArea1': 'CA',
        'postalCode': '95131',
        'countryCode': 'US',
    },
}


def section(title):
    print('\n' + '=' * 70)
    print(title)
    print('=' * 70)


def show(label, resp):
    try:
        body = resp.json()
    except Exception:
        body = resp.content.decode('utf-8', 'replace')
    print('%s -> HTTP %s' % (label, resp.status_code))
    print(json.dumps(body, indent=2, default=str))
    return body


def seed_products():
    pc, _ = ProductClass.objects.get_or_create(
        name='Verify goods',
        defaults={'track_stock': False, 'requires_shipping': False})
    pc.track_stock = False
    pc.save()
    partner, _ = Partner.objects.get_or_create(name='Verify partner')
    ids = []
    for i, price in enumerate([Decimal('42.00'), Decimal('19.99')]):
        sku = 'VERIFY-%d-%s' % (i, uuid.uuid4().hex[:6])
        product = Product.objects.create(
            title='Verify product %d' % i, product_class=pc, structure='standalone')
        StockRecord.objects.create(
            product=product, partner=partner, partner_sku=sku,
            price_currency='USD', price=price)
        ids.append(product.id)
    return ids


def main():
    shopper, _ = User.objects.get_or_create(
        username='verify_shopper', defaults={'email': 'verify_shopper@example.com'})
    staff, _ = User.objects.get_or_create(
        username='verify_staff',
        defaults={'email': 'verify_staff@example.com', 'is_staff': True})
    staff.is_staff = True
    staff.save()

    product_ids = seed_products()
    print('Seeded products:', product_ids)

    # Use a host that is in ALLOWED_HOSTS (the test client defaults to
    # 'testserver', which the sandbox does not allow outside a TestCase).
    shopper_client = Client(SERVER_NAME='localhost')
    shopper_client.force_login(shopper)
    staff_client = Client(SERVER_NAME='localhost')
    staff_client.force_login(staff)

    def post(client, url, payload=None):
        return client.post(url, data=json.dumps(payload or {}),
                           content_type='application/json')

    # -- Flow 1: one-off card ------------------------------------------------
    section('FLOW 1 — pay with a one-off card, fulfil, refund')
    created = show('POST /api/orders', post(
        shopper_client, '/api/orders',
        {'items': [{'id': product_ids[0], 'quantity': 1}]}))
    order_id = created['orderId']

    pay = show('POST /api/orders/%s/pay (one-off card)' % order_id, post(
        shopper_client, '/api/orders/%s/pay' % order_id, {'card': CARD}))
    assert pay.get('payment', {}).get('state') == 'authorized', 'authorize failed'

    # idempotent double-click
    pay2 = show('POST .../pay again (idempotency)', post(
        shopper_client, '/api/orders/%s/pay' % order_id, {'card': CARD}))
    assert pay2['payment']['authorizationId'] == pay['payment']['authorizationId']

    fulfil = show('POST /api/orders/%s/fulfil (staff, capture)' % order_id, post(
        staff_client, '/api/orders/%s/fulfil' % order_id))
    assert fulfil['payment']['state'] == 'captured', 'capture failed'
    assert fulfil['payment']['captureId'], 'no capture id'
    print('captured gross=%s fee=%s net=%s' % (
        fulfil['payment']['grossAmount'], fulfil['payment']['paypalFee'],
        fulfil['payment']['netAmount']))

    refund = show('POST /api/orders/%s/refunds (partial 10.00)' % order_id, post(
        staff_client, '/api/orders/%s/refunds' % order_id,
        {'amount': '10.00', 'idempotencyKey': uuid.uuid4().hex}))
    assert refund.get('refundId'), 'refund failed'

    # -- Flow 2: saved card reused ------------------------------------------
    section('FLOW 2 — save a card, reuse it to pay a second order')
    saved = show('POST /api/payment-methods (vault card)', post(
        shopper_client, '/api/payment-methods', {'card': CARD, 'label': 'My Visa'}))
    method_id = saved['paymentMethodId']
    assert saved['lastDigits'] == '1111', 'unexpected last digits'

    show('GET /api/payment-methods', shopper_client.get('/api/payment-methods'))

    created2 = show('POST /api/orders (second order)', post(
        shopper_client, '/api/orders',
        {'items': [{'id': product_ids[1], 'quantity': 2}]}))
    order2 = created2['orderId']

    pay_saved = show('POST /api/orders/%s/pay (saved card)' % order2, post(
        shopper_client, '/api/orders/%s/pay' % order2,
        {'paymentMethodId': method_id}))
    assert pay_saved['payment']['state'] == 'authorized', 'saved-card authorize failed'

    fulfil2 = show('POST /api/orders/%s/fulfil (staff)' % order2, post(
        staff_client, '/api/orders/%s/fulfil' % order2))
    assert fulfil2['payment']['state'] == 'captured'

    # cancel path on a fresh third order (authorize then void)
    section('FLOW 1 — cancel (void) before fulfilment')
    created3 = show('POST /api/orders (third order)', post(
        shopper_client, '/api/orders',
        {'items': [{'id': product_ids[0], 'quantity': 1}]}))
    order3 = created3['orderId']
    show('POST .../pay', post(shopper_client, '/api/orders/%s/pay' % order3,
                             {'paymentMethodId': method_id}))
    cancelled = show('POST /api/orders/%s/cancel (staff, void)' % order3, post(
        staff_client, '/api/orders/%s/cancel' % order3))
    assert cancelled['payment']['state'] == 'voided'

    # delete saved card
    section('FLOW 2 — delete saved card')
    show('DELETE /api/payment-methods/%s' % method_id,
         shopper_client.delete('/api/payment-methods/%s' % method_id))
    show('GET /api/payment-methods (after delete)',
         shopper_client.get('/api/payment-methods'))

    # my-orders + reconciliation
    section('Reports')
    show('GET /api/my-orders', shopper_client.get('/api/my-orders'))
    from datetime import datetime, timedelta, timezone
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=3)
    recon = show(
        'GET /api/reconciliation (recent window; may be empty due to reporting lag)',
        staff_client.get('/api/reconciliation',
                         {'from': start.isoformat(), 'to': end.isoformat()}))
    print('reconciliation walked the whole range: %d PayPal transactions'
          % recon.get('paypalTransactionCount', 0))

    print('\nALL LIVE CHECKS PASSED')


if __name__ == '__main__':
    main()
