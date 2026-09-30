import json
from datetime import timedelta

import httpx
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.utils import timezone

from apps.paypal_payments import client as client_module
from apps.paypal_payments.models import OperationClaim, PayPalPayment, PayPalRefund, SavedCard

from .conftest import (
    StubResponse,
    api_client,
    authorization_body,
    authorized_order_body,
    capture_body,
    json_response,
    paypal_error,
    post,
    refund_body,
)

pytestmark = pytest.mark.django_db

CARD = {"number": "4111 1111 1111 1111", "expiry": "2030-12", "securityCode": "123", "name": "Test Shopper"}
ORDERS = r"/v2/checkout/orders"
AUTH = r"/v2/payments/authorizations/[^/]+"


def place(c, product, quantity=2):
    response = post(c, "/api/orders", {"items": [{"productId": product.pk, "quantity": quantity}]})
    assert response.status_code == 201, response.content
    return response.json()


def paid_order(shopper_api, product, paypal, quantity=2):
    order = place(shopper_api, product, quantity)
    paypal.on("POST", ORDERS, json_response(201, authorized_order_body(order["total"])))
    response = post(shopper_api, f"/api/orders/{order['orderId']}/pay", {"card": CARD})
    assert response.status_code == 200, response.content
    return response.json()


def fulfilled_order(shopper_api, operator_api, product, paypal):
    order = paid_order(shopper_api, product, paypal)
    paypal.on("GET", AUTH, json_response(200, authorization_body(created=timezone.now().isoformat())))
    paypal.on("POST", AUTH + "/capture", json_response(201, capture_body(order["total"])))
    response = post(operator_api, f"/api/orders/{order['orderId']}/fulfil")
    assert response.status_code == 200, response.content
    return response.json()


# ---------------------------------------------------------------------------
# Orders and payment
# ---------------------------------------------------------------------------


def test_anonymous_callers_are_rejected(product):
    assert post(api_client(), "/api/orders", {"items": []}).status_code == 401
    assert api_client().get("/api/my-orders").status_code == 401


def test_place_order_uses_catalogue_prices_and_configured_currency(shopper_api, product, paypal):
    order = place(shopper_api, product, 2)
    assert order["orderId"]
    assert order["paymentState"] == "AWAITING_PAYMENT"
    assert order["status"] == "Pending"
    assert order["total"] == "24.68"
    assert order["currency"] == "USD"
    assert order["lines"][0]["quantity"] == 2
    assert paypal.requests == []


def test_place_order_rejects_unknown_products(shopper_api, product):
    response = post(shopper_api, "/api/orders", {"items": [{"productId": 999999, "quantity": 1}]})
    assert response.status_code == 422


def test_place_order_is_idempotent_under_a_key(shopper_api, product):
    body = {"items": [{"productId": product.pk, "quantity": 1}]}
    first = post(shopper_api, "/api/orders", body, **{"Idempotency-Key": "k1"}).json()
    second = post(shopper_api, "/api/orders", body, **{"Idempotency-Key": "k1"}).json()
    assert first["orderId"] == second["orderId"]


def test_pay_authorizes_exact_total_with_card(shopper_api, product, paypal):
    order = paid_order(shopper_api, product, paypal)
    assert order["paymentState"] == "AUTHORIZED"
    assert order["status"] == "Being processed"
    assert order["payment"]["authorization"]["id"] == "AUTH-1"
    assert order["payment"]["card"] == {"brand": "VISA", "lastDigits": "1111"}

    [request] = paypal.calls("POST", ORDERS)
    sent = request.body.value
    assert sent["intent"] == "AUTHORIZE"
    unit = sent["purchase_units"][0]
    assert unit["amount"] == {"currency_code": "USD", "value": "24.68"}
    assert unit["custom_id"] == order["orderId"]
    assert unit["invoice_id"].startswith(f"TEST-{order['orderId']}-")
    assert sent["payment_source"]["card"]["number"] == "4111111111111111"
    assert request.headers["paypal-request-id"]
    assert request.headers["prefer"] == "return=representation"


def test_card_number_is_never_stored(shopper_api, product, paypal):
    paid_order(shopper_api, product, paypal)
    with connection.cursor() as cursor:
        for table in connection.introspection.table_names():
            cursor.execute(f'SELECT * FROM "{table}"')
            for row in cursor.fetchall():
                assert "4111111111111111" not in json.dumps(row, default=str), table


def test_double_pay_authorizes_once(shopper_api, product, paypal):
    order = paid_order(shopper_api, product, paypal)
    again = post(shopper_api, f"/api/orders/{order['orderId']}/pay", {"card": CARD})
    assert again.status_code == 200
    assert len(paypal.calls("POST", ORDERS)) == 1


def test_declined_card_releases_the_claim(shopper_api, product, paypal):
    order = place(shopper_api, product)
    paypal.on("POST", ORDERS, paypal_error(422, "INSTRUMENT_DECLINED"),
              json_response(201, authorized_order_body(order["total"])))
    declined = post(shopper_api, f"/api/orders/{order['orderId']}/pay", {"card": CARD})
    assert declined.status_code == 422
    assert declined.json()["error"]["details"]["issues"][0]["issue"] == "INSTRUMENT_DECLINED"
    retried = post(shopper_api, f"/api/orders/{order['orderId']}/pay", {"card": CARD})
    assert retried.status_code == 200
    assert retried.json()["paymentState"] == "AUTHORIZED"


def test_payer_action_required_is_reported_not_worked_around(shopper_api, product, paypal):
    order = place(shopper_api, product)
    paypal.on("POST", ORDERS, json_response(200, {"id": "PP1", "status": "PAYER_ACTION_REQUIRED"}))
    response = post(shopper_api, f"/api/orders/{order['orderId']}/pay", {"card": CARD})
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "payer_action_required"


def test_card_details_are_validated(shopper_api, product, paypal):
    order = place(shopper_api, product)
    response = post(shopper_api, f"/api/orders/{order['orderId']}/pay", {"card": {**CARD, "expiry": "12/30"}})
    assert response.status_code == 400
    assert paypal.requests == []


def test_other_shoppers_cannot_see_or_pay_an_order(shopper_api, other_shopper, product, paypal):
    order = place(shopper_api, product)
    other = api_client(other_shopper)
    assert other.get(f"/api/orders/{order['orderId']}").status_code == 404
    assert post(other, f"/api/orders/{order['orderId']}/pay", {"card": CARD}).status_code == 404
    assert other.get("/api/my-orders").json()["orders"] == []
    assert len(shopper_api.get("/api/my-orders").json()["orders"]) == 1


# ---------------------------------------------------------------------------
# Transport failures: never sent vs. outcome unknown
# ---------------------------------------------------------------------------


def test_unsent_request_is_a_known_failure_and_frees_the_order(shopper_api, product, paypal):
    order = place(shopper_api, product)
    paypal.on("POST", ORDERS, httpx.ConnectError("refused"))
    response = post(shopper_api, f"/api/orders/{order['orderId']}/pay", {"card": CARD})
    assert response.status_code == 502
    assert "outcomeUnknown" not in response.json()["error"]
    assert not OperationClaim.objects.filter(key__startswith="authorize:").exists()


def test_lost_response_keeps_the_claim_and_resumes_with_the_same_request_id(shopper_api, product, paypal):
    order = place(shopper_api, product)
    paypal.on("POST", ORDERS, httpx.ReadTimeout("no reply"),
              json_response(201, authorized_order_body(order["total"])))
    lost = post(shopper_api, f"/api/orders/{order['orderId']}/pay", {"card": CARD})
    assert lost.status_code == 504
    assert lost.json()["error"]["outcomeUnknown"] is True
    assert OperationClaim.objects.get(key__startswith="authorize:").state == OperationClaim.OUTCOME_UNKNOWN

    resumed = post(shopper_api, f"/api/orders/{order['orderId']}/pay", {"card": CARD})
    assert resumed.status_code == 200
    first, second = paypal.calls("POST", ORDERS)
    assert first.headers["paypal-request-id"] == second.headers["paypal-request-id"]


def test_rejected_credentials_are_a_server_side_failure(shopper_api, product, settings):
    from .conftest import BASE, StubTransport
    from pay_pal_server_sdk import PayPalServerSdkClient

    transport = StubTransport()
    transport.on("POST", r"/v1/oauth2/token", json_response(401, {"error": "invalid_client"}))
    client_module.set_client(PayPalServerSdkClient(
        base_url=BASE, custom_http_client=transport, retry_options=0,
        oauth2={"client_id": "bad", "client_secret": "bad"},
    ))
    try:
        order = place(shopper_api, product)
        response = post(shopper_api, f"/api/orders/{order['orderId']}/pay", {"card": CARD})
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "paypal_auth_failed"
    finally:
        client_module.set_client(None)


# ---------------------------------------------------------------------------
# Fulfil, cancel
# ---------------------------------------------------------------------------


def test_fulfil_is_staff_only(shopper_api, product, paypal):
    order = paid_order(shopper_api, product, paypal)
    assert post(shopper_api, f"/api/orders/{order['orderId']}/fulfil").status_code == 403


def test_fulfil_captures_and_reports_fee_and_net(shopper_api, operator_api, product, paypal):
    order = fulfilled_order(shopper_api, operator_api, product, paypal)
    capture = order["payment"]["capture"]
    assert order["paymentState"] == "CAPTURED"
    assert order["status"] == "Complete"
    assert capture == {**capture, "id": "CAP-1", "amount": "24.68", "paypalFee": "0.81", "netAmount": "23.87"}
    [request] = paypal.calls("POST", AUTH + "/capture")
    assert request.url.endswith("/v2/payments/authorizations/AUTH-1/capture")
    assert request.body.value["amount"] == {"currency_code": "USD", "value": "24.68"}
    # repeat: no second capture
    assert post(operator_api, f"/api/orders/{order['orderId']}/fulfil").status_code == 200
    assert len(paypal.calls("POST", AUTH + "/capture")) == 1


def test_stale_authorization_is_renewed_before_capture(shopper_api, operator_api, product, paypal):
    order = paid_order(shopper_api, product, paypal)
    stale = (timezone.now() - timedelta(days=5)).isoformat()
    paypal.on("GET", AUTH, json_response(200, authorization_body(created=stale)))
    paypal.on("POST", AUTH + "/reauthorize", json_response(201, authorization_body(auth_id="AUTH-2")))
    paypal.on("POST", AUTH + "/capture", json_response(201, capture_body(order["total"])))
    response = post(operator_api, f"/api/orders/{order['orderId']}/fulfil")
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["payment"]["authorization"]["id"] == "AUTH-2"
    assert body["payment"]["authorization"]["reauthorizations"] == 1
    [capture] = paypal.calls("POST", AUTH + "/capture")
    assert "/authorizations/AUTH-2/capture" in capture.url


def test_unrenewable_authorization_explains_what_to_do(shopper_api, operator_api, product, paypal):
    order = paid_order(shopper_api, product, paypal)
    stale = (timezone.now() - timedelta(days=5)).isoformat()
    paypal.on("GET", AUTH, json_response(200, authorization_body(created=stale)))
    paypal.on("POST", AUTH + "/reauthorize", paypal_error(422, "REAUTHORIZATION_NOT_SUPPORTED"))
    response = post(operator_api, f"/api/orders/{order['orderId']}/fulfil")
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "authorization_not_renewable"
    assert f"/api/orders/{order['orderId']}/cancel" in error["message"]
    assert paypal.calls("POST", AUTH + "/capture") == []
    # the claim was released: the operator can still cancel
    paypal.on("POST", AUTH + "/void", json_response(200, authorization_body(status="VOIDED")))
    assert post(operator_api, f"/api/orders/{order['orderId']}/cancel").json()["paymentState"] == "VOIDED"


def test_expired_authorization_cannot_be_captured(shopper_api, operator_api, product, paypal):
    order = paid_order(shopper_api, product, paypal)
    paypal.on("GET", AUTH, json_response(200, authorization_body(
        created="2020-01-01T00:00:00Z", expires="2020-01-30T00:00:00Z")))
    response = post(operator_api, f"/api/orders/{order['orderId']}/fulfil")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "authorization_expired"


def test_cancel_voids_the_hold(shopper_api, operator_api, product, paypal):
    order = paid_order(shopper_api, product, paypal)
    paypal.on("POST", AUTH + "/void", json_response(200, authorization_body(status="VOIDED")))
    response = post(operator_api, f"/api/orders/{order['orderId']}/cancel")
    assert response.status_code == 200
    assert response.json()["paymentState"] == "VOIDED"
    assert response.json()["status"] == "Cancelled"
    assert post(operator_api, f"/api/orders/{order['orderId']}/fulfil").status_code == 409
    assert post(shopper_api, f"/api/orders/{order['orderId']}/refunds", {}, **{"Idempotency-Key": "r"}
                ).status_code == 409


def test_cancel_unpaid_order_blocks_later_payment(shopper_api, operator_api, product, paypal):
    order = place(shopper_api, product)
    assert post(operator_api, f"/api/orders/{order['orderId']}/cancel").json()["paymentState"] == "CANCELLED"
    assert post(shopper_api, f"/api/orders/{order['orderId']}/pay", {"card": CARD}).status_code == 409
    assert paypal.requests == []


def test_cancel_after_fulfilment_is_refused(shopper_api, operator_api, product, paypal):
    order = fulfilled_order(shopper_api, operator_api, product, paypal)
    assert post(operator_api, f"/api/orders/{order['orderId']}/cancel").status_code == 409


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------


def refund(c, order_id, key, amount=None):
    body = {} if amount is None else {"amount": amount}
    return post(c, f"/api/orders/{order_id}/refunds", body, **({"Idempotency-Key": key} if key else {}))


def test_partial_refunds_never_exceed_the_capture(shopper_api, operator_api, product, paypal):
    order = fulfilled_order(shopper_api, operator_api, product, paypal)
    oid = order["orderId"]
    paypal.on("POST", r"/v2/payments/captures/CAP-1/refund",
              json_response(201, refund_body("REF-1", "10.00")), json_response(201, refund_body("REF-2", "14.00")))
    first = refund(shopper_api, oid, "a", "10.00")
    assert first.status_code == 201 and first.json()["refundId"]
    assert first.json()["order"]["paymentState"] == "PARTIALLY_REFUNDED"
    too_much = refund(shopper_api, oid, "b", "15.00")
    assert too_much.status_code == 409
    assert too_much.json()["error"]["details"]["refundableAmount"] == "14.68"
    second = refund(shopper_api, oid, "c", "14.00")
    assert second.status_code == 201
    assert len(paypal.calls("POST", r"/v2/payments/captures/CAP-1/refund")) == 2
    assert str(PayPalPayment.objects.get().refunded_amount) == "24.00"


def test_same_idempotency_key_refunds_once(shopper_api, operator_api, product, paypal):
    order = fulfilled_order(shopper_api, operator_api, product, paypal)
    paypal.on("POST", r"/v2/payments/captures/CAP-1/refund", json_response(201, refund_body("REF-1", "5.00")))
    first = refund(shopper_api, order["orderId"], "same", "5.00")
    again = refund(shopper_api, order["orderId"], "same", "5.00")
    assert first.status_code == 201 and again.status_code == 200
    assert first.json()["refundId"] == again.json()["refundId"]
    [request] = paypal.calls("POST", r"/v2/payments/captures/CAP-1/refund")
    assert request.headers["paypal-request-id"] == str(PayPalRefund.objects.get().request_id)
    assert refund(shopper_api, order["orderId"], "same", "6.00").status_code == 422


def test_full_refund_without_amount(shopper_api, operator_api, product, paypal):
    order = fulfilled_order(shopper_api, operator_api, product, paypal)
    paypal.on("POST", r"/v2/payments/captures/CAP-1/refund", json_response(201, refund_body("REF-1", "24.68")))
    response = refund(shopper_api, order["orderId"], "full")
    assert response.status_code == 201
    assert response.json()["amount"] == "24.68"
    assert response.json()["order"]["paymentState"] == "REFUNDED"
    assert refund(shopper_api, order["orderId"], "more", "0.01").status_code == 409


def test_refund_requires_an_idempotency_key(shopper_api, operator_api, product, paypal):
    order = fulfilled_order(shopper_api, operator_api, product, paypal)
    assert refund(shopper_api, order["orderId"], None, "1.00").status_code == 400


def test_refused_refund_releases_its_reservation(shopper_api, operator_api, product, paypal):
    order = fulfilled_order(shopper_api, operator_api, product, paypal)
    paypal.on("POST", r"/v2/payments/captures/CAP-1/refund", paypal_error(422, "REFUND_NOT_ALLOWED"))
    assert refund(shopper_api, order["orderId"], "x", "24.68").status_code == 422
    assert PayPalPayment.objects.get().refund_reserved_minor == 0
    assert not PayPalRefund.objects.exists()


def test_other_shopper_cannot_refund(shopper_api, operator_api, other_shopper, product, paypal):
    order = fulfilled_order(shopper_api, operator_api, product, paypal)
    assert refund(api_client(other_shopper), order["orderId"], "k", "1.00").status_code == 404


# ---------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------

VAULT = r"/v3/vault/payment-tokens"


def vault_body(token="TOKEN-1"):
    return {"id": token, "customer": {"id": "CUST-1"},
            "payment_source": {"card": {"brand": "VISA", "last_digits": "1111", "expiry": "2030-12",
                                        "name": "Test Shopper"}}}


def save_card(c, paypal, key=None):
    paypal.on("POST", VAULT, json_response(201, vault_body()))
    headers = {"Idempotency-Key": key} if key else {}
    return post(c, "/api/payment-methods", {"card": CARD}, **headers)


def test_save_list_and_pay_with_saved_card(shopper_api, product, paypal):
    saved = save_card(shopper_api, paypal)
    assert saved.status_code == 201
    card = saved.json()
    assert card["paymentMethodId"] and card["lastDigits"] == "1111" and card["brand"] == "VISA"
    assert "4111111111111111" not in saved.content.decode()
    assert "number" not in card
    assert shopper_api.get("/api/payment-methods").json()["paymentMethods"][0]["paymentMethodId"] == \
        card["paymentMethodId"]

    order = place(shopper_api, product)
    paypal.on("POST", ORDERS, json_response(201, authorized_order_body(order["total"])))
    paid = post(shopper_api, f"/api/orders/{order['orderId']}/pay", {"paymentMethodId": card["paymentMethodId"]})
    assert paid.status_code == 200
    assert paid.json()["payment"]["paymentMethodId"] == card["paymentMethodId"]
    sent = paypal.calls("POST", ORDERS)[0].body.value["payment_source"]["card"]
    assert sent == {"vault_id": "TOKEN-1"}


def test_second_card_reuses_the_paypal_customer(shopper_api, paypal):
    save_card(shopper_api, paypal)
    paypal.on("POST", VAULT, json_response(201, vault_body("TOKEN-2")))
    post(shopper_api, "/api/payment-methods", {"card": CARD})
    first, second = paypal.calls("POST", VAULT)
    assert "customer" not in first.body.value
    assert second.body.value["customer"] == {"id": "CUST-1"}


def test_saving_under_the_same_key_vaults_once(shopper_api, paypal):
    first = save_card(shopper_api, paypal, key="once")
    second = post(shopper_api, "/api/payment-methods", {"card": CARD}, **{"Idempotency-Key": "once"})
    assert first.json()["paymentMethodId"] == second.json()["paymentMethodId"]
    assert len(paypal.calls("POST", VAULT)) == 1


def test_saved_cards_are_private(shopper_api, other_shopper, product, paypal):
    card = save_card(shopper_api, paypal).json()
    other = api_client(other_shopper)
    assert other.get("/api/payment-methods").json()["paymentMethods"] == []
    assert other.delete(f"/api/payment-methods/{card['paymentMethodId']}").status_code == 404
    order = place(other, product)
    assert post(other, f"/api/orders/{order['orderId']}/pay",
                {"paymentMethodId": card["paymentMethodId"]}).status_code == 404
    assert paypal.calls("POST", ORDERS) == []


def test_deleted_card_is_gone_and_unusable(shopper_api, product, paypal):
    card = save_card(shopper_api, paypal).json()
    paypal.on("DELETE", VAULT + "/TOKEN-1", StubResponse(204))
    deleted = shopper_api.delete(f"/api/payment-methods/{card['paymentMethodId']}")
    assert deleted.status_code == 200
    assert deleted.json()["removedFromPayPalVault"] is True
    assert shopper_api.get("/api/payment-methods").json()["paymentMethods"] == []
    order = place(shopper_api, product)
    assert post(shopper_api, f"/api/orders/{order['orderId']}/pay",
                {"paymentMethodId": card["paymentMethodId"]}).status_code == 404
    assert shopper_api.delete(f"/api/payment-methods/{card['paymentMethodId']}").status_code == 404


def test_card_deleted_while_paypal_is_down_is_purged_later(shopper_api, paypal):
    from django.core.management import call_command

    card = save_card(shopper_api, paypal).json()
    paypal.on("DELETE", VAULT + "/TOKEN-1", httpx.ConnectError("down"), StubResponse(204))
    deleted = shopper_api.delete(f"/api/payment-methods/{card['paymentMethodId']}")
    assert deleted.json()["removedFromPayPalVault"] is False
    assert shopper_api.get("/api/payment-methods").json()["paymentMethods"] == []
    call_command("paypal_purge_deleted_cards")
    assert SavedCard.objects.get().vault_token_deleted is True


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def search_body(txns, page=1, total_pages=1):
    return {"transaction_details": [{"transaction_info": t} for t in txns], "page": page,
            "total_pages": total_pages, "last_refreshed_datetime": "2099-01-01T00:00:00Z"}


def txn(txn_id, value, invoice=None):
    info = {"transaction_id": txn_id, "transaction_event_code": "T0006", "transaction_status": "S",
            "transaction_initiation_date": "2026-09-30T10:00:00Z",
            "transaction_amount": {"currency_code": "USD", "value": value}}
    if invoice:
        info["invoice_id"] = invoice
    return info


def test_reconciliation_is_staff_only(shopper_api):
    assert shopper_api.get("/api/reconciliation?from=2026-09-01T00:00:00Z&to=2026-09-30T00:00:00Z"
                           ).status_code == 403


def test_reconciliation_walks_every_page_and_window(shopper_api, operator_api, product, paypal):
    order = fulfilled_order(shopper_api, operator_api, product, paypal)
    PayPalPayment.objects.update(captured_at=timezone.now() - timedelta(days=40))
    ghost = PayPalPayment.objects.get()
    paypal.on(
        "GET", r"/v1/reporting/transactions",
        json_response(200, search_body([txn("CAP-1", "24.68")], page=1, total_pages=2)),
        json_response(200, search_body([txn("FOREIGN-1", "5.00", "OTHER-1"),
                                        txn("MISSING-1", "3.00", "TEST-999999-abc")], page=2, total_pages=2)),
        json_response(200, search_body([])),
    )
    start = (timezone.now() - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = timezone.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    response = operator_api.get(f"/api/reconciliation?from={start}&to={end}")
    assert response.status_code == 200, response.content
    report = response.json()
    searches = paypal.calls("GET", r"/v1/reporting/transactions")
    assert len(searches) == 3  # two pages of the first 31-day window, one of the second
    assert "page=2" in searches[1].url
    assert [m["app"]["orderId"] for m in report["matched"]] == [order["orderId"]]
    reasons = {r["transactionId"]: r["reason"] for r in report["paypalOnly"]}
    assert reasons == {"FOREIGN-1": "not_from_this_shop", "MISSING-1": "unknown_order_with_shop_prefix"}
    assert report["appOnly"] == []
    assert ghost.capture_id == "CAP-1"


def test_reconciliation_shows_app_records_paypal_lacks(shopper_api, operator_api, product, paypal):
    fulfilled_order(shopper_api, operator_api, product, paypal)
    paypal.on("GET", r"/v1/reporting/transactions", json_response(200, search_body([])))
    start = (timezone.now() - timedelta(days=1)).isoformat()
    end = (timezone.now() + timedelta(hours=1)).isoformat()
    report = operator_api.get("/api/reconciliation", {"from": start, "to": end}).json()
    assert [r["paypalTransactionId"] for r in report["appOnly"]] == ["CAP-1"]


def test_reconciliation_validates_the_range(operator_api):
    assert operator_api.get("/api/reconciliation?from=nope&to=2026-01-01T00:00:00Z").status_code == 400
    assert operator_api.get("/api/reconciliation?from=2026-02-01T00:00:00Z&to=2026-01-01T00:00:00Z"
                            ).status_code == 400


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_base_url_override_wins(settings):
    settings.PAYPAL_BASE_URL = "https://proxy.example/paypal"
    settings.PAYPAL_ENVIRONMENT = "live"
    assert client_module.resolve_base_url() == "https://proxy.example/paypal"


def test_sandbox_environment_selects_sandbox_host(settings):
    settings.PAYPAL_BASE_URL = ""
    settings.PAYPAL_ENVIRONMENT = "sandbox"
    assert client_module.resolve_base_url() == "https://api-m.sandbox.paypal.com"


def test_unknown_environment_without_base_url_is_refused(settings):
    settings.PAYPAL_BASE_URL = ""
    settings.PAYPAL_ENVIRONMENT = "live"
    with pytest.raises(ImproperlyConfigured):
        client_module.resolve_base_url()


def test_missing_credentials_are_refused(settings):
    settings.PAYPAL_CLIENT_ID = ""
    with pytest.raises(ImproperlyConfigured):
        client_module.build_client()
