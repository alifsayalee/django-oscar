from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.exceptions import ImproperlyConfigured

from apps.paypal_payments import client as client_module
from apps.paypal_payments.models import PayPalPayment

from .conftest import StubTransport, json_response, pay_and_fulfil

pytestmark = pytest.mark.django_db


def search_page(rows, page, total_pages):
    return json_response(200, {
        "transaction_details": [{"transaction_info": r} for r in rows],
        "page": page, "total_pages": total_pages, "total_items": len(rows),
    })


def row(txn_id, amount, when="2026-09-10T10:00:00+0000", **extra):
    return {"transaction_id": txn_id, "transaction_event_code": "T0006", "transaction_status": "S",
            "transaction_initiation_date": when,
            "transaction_amount": {"currency_code": "USD", "value": amount}, **extra}


def test_reconciliation_covers_every_window_and_page(shopper_api, staff_api, paypal, order_id):
    pay_and_fulfil(shopper_api, staff_api, paypal, order_id)
    payment = PayPalPayment.objects.get()
    # Pretend PayPal stamped these inside the report range.
    PayPalPayment.objects.filter(pk=payment.pk).update(
        authorized_at="2026-09-10T09:59:00Z", captured_at="2026-09-10T10:00:00Z")

    paypal.add(
        # window 1 (31 days), two pages
        search_page([row("CAP-1", "20.00")], 1, 2),
        search_page([row("STRANGER-1", "7.00")], 2, 2),
        # window 2, one page: a transaction tied to our payment by custom_id under an id we do not hold
        search_page([row("OLD-AUTH", "20.00", when="2026-10-05T10:00:00+0000",
                          custom_field=str(payment.request_ns))], 1, 1),
    )
    r = staff_api.get("/api/reconciliation", {"from": "2026-09-01T00:00:00Z", "to": "2026-10-10T00:00:00Z"})
    assert r.status_code == 200, r.content

    searches = [q for q in paypal.requests if "/v1/reporting/transactions" in str(q.url)]
    params = [parse_qs(urlsplit(str(q.url)).query) for q in searches]
    assert [(p["start_date"][0], p["end_date"][0], p["page"][0]) for p in params] == [
        ("2026-09-01T00:00:00Z", "2026-10-02T00:00:00Z", "1"),
        ("2026-09-01T00:00:00Z", "2026-10-02T00:00:00Z", "2"),
        ("2026-10-02T00:00:00Z", "2026-10-10T00:00:00Z", "1"),
    ]
    assert all(p["balance_affecting_records_only"] == ["N"] for p in params)

    report = r.json()
    assert report["summary"] == {"matched": 2, "amountMismatches": 0, "paypalOnly": 1, "appOnly": 1, "unsettled": 0}
    assert {m["transactionId"] for m in report["matched"]} == {"CAP-1", "OLD-AUTH"}
    assert report["paypalOnly"][0]["transactionId"] == "STRANGER-1"
    assert report["appOnly"][0] == {"transactionId": "AUTH-1", "orderId": order_id, "kind": "authorization",
                                    "amount": "20.00", "at": "2026-09-10T09:59:00+00:00"}


def test_reconciliation_flags_amount_mismatch(shopper_api, staff_api, paypal, order_id):
    pay_and_fulfil(shopper_api, staff_api, paypal, order_id)
    PayPalPayment.objects.update(captured_at="2026-09-10T10:00:00Z")
    paypal.add(search_page([row("CAP-1", "19.00")], 1, 1))
    r = staff_api.get("/api/reconciliation", {"from": "2026-09-01T00:00:00Z", "to": "2026-09-20T00:00:00Z"})
    assert r.json()["amountMismatches"][0]["localAmount"] == "20.00"


def test_reconciliation_validates_range(staff_api):
    assert staff_api.get("/api/reconciliation", {"from": "nope", "to": "2026-09-20T00:00:00Z"}).status_code == 400
    assert staff_api.get("/api/reconciliation", {"from": "2026-09-20T00:00:00Z",
                                                 "to": "2026-09-01T00:00:00Z"}).status_code == 400


def test_base_url_override_moves_every_call_including_the_token(settings, monkeypatch):
    settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET = "id", "secret"
    settings.PAYPAL_BASE_URL = "https://paypal-proxy.internal"
    transport = StubTransport()
    transport.add(json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600}),
                  json_response(200, {"id": "PO-1", "status": "CREATED"}))
    monkeypatch.setattr(client_module, "HttpxClient", lambda timeout: transport)
    sdk = client_module.build_client()
    try:
        sdk.orders.get_order("PO-1")
    finally:
        sdk.close()
    assert [str(q.url).split("?")[0] for q in transport.requests] == [
        "https://paypal-proxy.internal/v1/oauth2/token",
        "https://paypal-proxy.internal/v2/checkout/orders/PO-1",
    ]


def test_environment_selects_host_and_unknown_environment_fails_loudly(settings):
    settings.PAYPAL_BASE_URL = None
    settings.PAYPAL_ENVIRONMENT = "sandbox"
    assert client_module.resolve_base_url() == "https://api-m.sandbox.paypal.com"
    settings.PAYPAL_ENVIRONMENT = "live"
    with pytest.raises(ImproperlyConfigured):
        client_module.resolve_base_url()


def test_missing_credentials_fail_at_startup_not_as_anonymous_calls(settings):
    settings.PAYPAL_CLIENT_ID = None
    with pytest.raises(ImproperlyConfigured):
        client_module.build_client()
