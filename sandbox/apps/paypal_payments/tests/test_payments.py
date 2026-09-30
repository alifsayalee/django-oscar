from datetime import timedelta

import httpx
import pytest
from django.utils import timezone
from pay_pal_server_sdk.models import Refund

from apps.paypal_payments.models import CardSaveClaim, PayPalPayment, PayPalRefund, PayPalVaultCard

from .conftest import (
    CARD,
    NOW,
    StubResponse,
    auth,
    capture_body,
    json_response,
    order_body,
    orders_capture,
    pay_and_fulfil,
    pay_with_card,
    payment_auth_body,
    paypal_error,
    post,
    refund_body,
)

pytestmark = pytest.mark.django_db


def payment_of(order_id):
    return PayPalPayment.objects.get(order__number=order_id)


# ---- Flow 1: place, authorize, fulfil ------------------------------------------


def test_order_starts_awaiting_payment_with_catalogue_total(shopper_api, product):
    r = post(shopper_api, "/api/orders", {"items": [{"itemId": product.pk, "quantity": 3}]})
    assert r.status_code == 201
    body = r.json()
    assert body["order"]["status"] == "Awaiting payment"
    assert body["order"]["total"] == "30.00"
    assert body["order"]["currency"] == "USD"
    assert body["order"]["payment"]["status"] == "AWAITING_PAYMENT"


def test_pay_authorizes_exact_order_total(shopper_api, paypal, order_id):
    r = pay_with_card(shopper_api, paypal, order_id)

    req = paypal.requests[-1]
    assert req.method == "POST" and str(req.url).endswith("/v2/checkout/orders")
    body = req.body.value
    assert body["intent"] == "AUTHORIZE"
    assert body["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "20.00"}
    assert body["payment_source"]["card"]["number"] == "4111111111111111"
    assert req.headers["paypal-request-id"].endswith("-create-1")
    assert req.headers["prefer"] == "return=representation"

    payment = r.json()["payment"]
    assert payment["status"] == "AUTHORIZED"
    assert payment["authorization"]["amount"] == "20.00"
    assert payment["card"] == {"brand": "VISA", "lastDigits": "1111"}
    assert r.json()["status"] == "Pending"


def test_approved_order_is_then_authorized(shopper_api, paypal, order_id):
    paypal.add(json_response(201, order_body(status="APPROVED")),
               json_response(201, order_body(auths=[auth()])))
    r = post(shopper_api, f"/api/orders/{order_id}/pay", {"card": CARD})
    assert r.status_code == 200
    assert paypal.paths()[-1] == "POST /v2/checkout/orders/PO-1/authorize"
    assert paypal.requests[-1].headers["paypal-request-id"].endswith("-authorize-1")


def test_double_pay_is_refused_before_paypal(shopper_api, paypal, order_id):
    pay_with_card(shopper_api, paypal, order_id)
    calls = len(paypal.requests)
    r = post(shopper_api, f"/api/orders/{order_id}/pay", {"card": CARD})
    assert r.status_code == 409
    assert len(paypal.requests) == calls


def test_declined_card_can_pay_again(shopper_api, paypal, order_id):
    paypal.add(paypal_error(422))
    r = post(shopper_api, f"/api/orders/{order_id}/pay", {"card": CARD})
    assert r.status_code == 422
    assert r.json()["error"]["paypalError"] == "UNPROCESSABLE_ENTITY/INSTRUMENT_DECLINED"
    assert payment_of(order_id).status == "DECLINED"
    paypal.add(json_response(201, order_body(auths=[auth()])))
    r = post(shopper_api, f"/api/orders/{order_id}/pay", {"card": CARD})
    assert r.status_code == 200
    assert paypal.requests[-1].headers["paypal-request-id"].endswith("-create-2")


def test_denied_authorization_is_a_decline(shopper_api, paypal, order_id):
    paypal.add(json_response(201, order_body(auths=[auth(status="DENIED")])))
    r = post(shopper_api, f"/api/orders/{order_id}/pay", {"card": CARD})
    assert r.status_code == 422
    assert payment_of(order_id).status == "DECLINED"


def test_payer_action_required_is_reported_not_round_tripped(shopper_api, paypal, order_id):
    paypal.add(json_response(201, order_body(status="PAYER_ACTION_REQUIRED")))
    r = post(shopper_api, f"/api/orders/{order_id}/pay", {"card": CARD})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "PAYER_ACTION_REQUIRED"


def test_authorized_amount_mismatch_needs_review(shopper_api, paypal, order_id):
    paypal.add(json_response(201, order_body(auths=[auth(value="19.99")])))
    r = post(shopper_api, f"/api/orders/{order_id}/pay", {"card": CARD})
    assert r.status_code == 200
    assert r.json()["payment"]["status"] == "NEEDS_REVIEW"


def test_fulfil_captures_and_records_fee_and_net(shopper_api, staff_api, paypal, order_id):
    r = pay_and_fulfil(shopper_api, staff_api, paypal, order_id)
    assert paypal.paths()[-1] == "POST /v2/payments/authorizations/AUTH-1/capture"
    assert paypal.requests[-1].body.value["amount"] == {"currency_code": "USD", "value": "20.00"}
    cap = r.json()["payment"]["capture"]
    assert cap == {"id": "CAP-1", "status": "COMPLETED", "amount": "20.00", "paypalFee": "0.88",
                   "netAmount": "19.12", "capturedAt": cap["capturedAt"]}
    assert r.json()["status"] == "Complete"
    assert r.json()["payment"]["status"] == "CAPTURED"


def test_stale_authorization_is_renewed_before_capture(shopper_api, staff_api, paypal, order_id):
    pay_with_card(shopper_api, paypal, order_id, auth_created=NOW - timedelta(days=5))
    paypal.add(json_response(201, payment_auth_body("AUTH-2")), json_response(201, capture_body()))
    r = post(staff_api, f"/api/orders/{order_id}/fulfil")
    assert r.status_code == 200, r.content
    assert paypal.paths()[-2:] == [
        "POST /v2/payments/authorizations/AUTH-1/reauthorize",
        "POST /v2/payments/authorizations/AUTH-2/capture",
    ]
    assert r.json()["payment"]["authorization"]["reauthorizations"] == 1


def test_stale_authorization_that_cannot_be_renewed_is_actionable(shopper_api, staff_api, paypal, order_id):
    pay_with_card(shopper_api, paypal, order_id, auth_created=NOW - timedelta(days=5))
    paypal.add(paypal_error(422, issue="REAUTHORIZATION_NOT_ALLOWED"), paypal_error(422, issue="AUTHORIZATION_EXPIRED"))
    r = post(staff_api, f"/api/orders/{order_id}/fulfil")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "AUTHORIZATION_STALE"
    assert "Cancel this order" in err["message"]
    assert payment_of(order_id).status == "AUTHORIZED"  # claim released: the operator can cancel


def test_expired_authorization_fails_without_calling_paypal(shopper_api, staff_api, paypal, order_id):
    pay_with_card(shopper_api, paypal, order_id)
    PayPalPayment.objects.filter(order__number=order_id).update(
        authorization_expires_at=timezone.now() - timedelta(minutes=1))
    calls = len(paypal.requests)
    r = post(staff_api, f"/api/orders/{order_id}/fulfil")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "AUTHORIZATION_EXPIRED"
    assert len(paypal.requests) == calls
    # ... and the order can still be cancelled (nothing is held any more)
    r = post(staff_api, f"/api/orders/{order_id}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "Cancelled"


def test_double_fulfil_captures_once(shopper_api, staff_api, paypal, order_id):
    pay_and_fulfil(shopper_api, staff_api, paypal, order_id)
    calls = len(paypal.requests)
    assert post(staff_api, f"/api/orders/{order_id}/fulfil").status_code == 409
    assert len(paypal.requests) == calls


# ---- cancel ----------------------------------------------------------------------


def test_cancel_voids_the_authorization(shopper_api, staff_api, paypal, order_id):
    pay_with_card(shopper_api, paypal, order_id)
    paypal.add(json_response(200, payment_auth_body("AUTH-1", status="VOIDED")))
    r = post(staff_api, f"/api/orders/{order_id}/cancel")
    assert r.status_code == 200
    assert paypal.paths()[-1] == "POST /v2/payments/authorizations/AUTH-1/void"
    assert r.json()["payment"]["status"] == "VOIDED"
    assert r.json()["status"] == "Cancelled"


def test_cancel_before_payment_needs_no_paypal_call(staff_api, paypal, order_id):
    r = post(staff_api, f"/api/orders/{order_id}/cancel")
    assert r.status_code == 200 and r.json()["payment"]["status"] == "CANCELLED"
    assert paypal.requests == []


def test_cannot_cancel_after_capture(shopper_api, staff_api, paypal, order_id):
    pay_and_fulfil(shopper_api, staff_api, paypal, order_id)
    assert post(staff_api, f"/api/orders/{order_id}/cancel").status_code == 409


# ---- refunds ---------------------------------------------------------------------


def refund(c, order_id, key, amount=None):
    body = {"amount": amount} if amount is not None else {}
    return post(c, f"/api/orders/{order_id}/refunds", body, HTTP_IDEMPOTENCY_KEY=key)


def test_partial_refunds_and_idempotency(shopper_api, staff_api, paypal, order_id):
    pay_and_fulfil(shopper_api, staff_api, paypal, order_id)

    paypal.add(json_response(201, refund_body("REF-1", value="5.00")))
    r1 = refund(shopper_api, order_id, "key-1", "5.00")
    assert r1.status_code == 201
    assert r1.json()["refundId"]
    req = paypal.requests[-1]
    assert req.body.value["amount"] == {"currency_code": "USD", "value": "5.00"}
    first_request_id = req.headers["paypal-request-id"]

    calls = len(paypal.requests)
    again = refund(shopper_api, order_id, "key-1", "5.00")
    assert again.status_code == 200
    assert again.json()["refundId"] == r1.json()["refundId"]
    assert len(paypal.requests) == calls  # never refunded twice

    assert refund(shopper_api, order_id, "key-1", "6.00").json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    paypal.add(json_response(201, refund_body("REF-2", value="5.00")))
    r2 = refund(shopper_api, order_id, "key-2", "5.00")  # a second, distinct partial refund
    assert r2.status_code == 201
    assert paypal.requests[-1].headers["paypal-request-id"] != first_request_id
    assert r2.json()["payment"]["status"] == "PARTIALLY_REFUNDED"
    assert r2.json()["payment"]["refundableAmount"] == "10.00"


def test_refund_cannot_exceed_capture(shopper_api, staff_api, paypal, order_id):
    pay_and_fulfil(shopper_api, staff_api, paypal, order_id)
    paypal.add(json_response(201, refund_body("REF-1", value="15.00")))
    assert refund(shopper_api, order_id, "k1", "15.00").status_code == 201
    calls = len(paypal.requests)
    r = refund(shopper_api, order_id, "k2", "5.01")
    assert r.status_code == 409 and r.json()["error"]["code"] == "REFUND_EXCEEDS_CAPTURE"
    assert len(paypal.requests) == calls
    paypal.add(json_response(201, refund_body("REF-2", value="5.00")))
    r = refund(shopper_api, order_id, "k3")  # the rest
    assert paypal.requests[-1].body.value["amount"]["value"] == "5.00"
    assert r.json()["payment"]["status"] == "REFUNDED"


def test_refused_refund_releases_its_reservation(shopper_api, staff_api, paypal, order_id):
    pay_and_fulfil(shopper_api, staff_api, paypal, order_id)
    paypal.add(paypal_error(422, issue="REFUND_NOT_ALLOWED"))
    assert refund(shopper_api, order_id, "k1", "20.00").status_code == 422
    assert payment_of(order_id).refund_reserved == 0
    paypal.add(json_response(201, refund_body("REF-1", value="20.00")))
    assert refund(shopper_api, order_id, "k2", "20.00").status_code == 201


def test_refund_requires_idempotency_key(shopper_api, staff_api, paypal, order_id):
    pay_and_fulfil(shopper_api, staff_api, paypal, order_id)
    r = post(shopper_api, f"/api/orders/{order_id}/refunds", {"amount": "1.00"})
    assert r.status_code == 400


# ---- ownership and roles -----------------------------------------------------------


def test_orders_are_private_to_their_shopper(shopper_api, other_api, paypal, order_id):
    assert post(other_api, f"/api/orders/{order_id}/pay", {"card": CARD}).status_code == 404
    assert refund(other_api, order_id, "k").status_code == 404
    assert other_api.get("/api/my-orders").json()["orders"] == []
    mine = shopper_api.get("/api/my-orders").json()["orders"]
    assert [o["orderId"] for o in mine] == [order_id]


def test_operator_actions_need_staff(shopper_api, order_id, client):
    for path in (f"/api/orders/{order_id}/fulfil", f"/api/orders/{order_id}/cancel"):
        assert post(shopper_api, path).status_code == 403
        assert post(client, path).status_code == 401
    assert shopper_api.get("/api/reconciliation?from=2026-01-01T00:00:00Z&to=2026-01-02T00:00:00Z").status_code == 403


# ---- unknown outcomes are settled by re-reading PayPal ---------------------------------


def test_create_order_read_timeout_is_resent_under_the_same_request_id(shopper_api, paypal, order_id):
    paypal.add(httpx.ReadTimeout("no reply"), json_response(201, order_body(auths=[auth()])))
    r = post(shopper_api, f"/api/orders/{order_id}/pay", {"card": CARD})
    assert r.status_code == 200
    ids = [q.headers["paypal-request-id"] for q in paypal.requests]
    assert ids[0] == ids[1]


def test_create_order_unknown_twice_is_left_unknown_then_repaid_with_same_id(shopper_api, paypal, order_id):
    paypal.add(httpx.ReadTimeout("no reply"), httpx.ReadTimeout("no reply"))
    r = post(shopper_api, f"/api/orders/{order_id}/pay", {"card": CARD})
    assert r.status_code == 504 and r.json()["error"]["outcomeUnknown"] is True
    assert payment_of(order_id).status == "UNKNOWN"
    paypal.add(json_response(201, order_body(auths=[auth()])))
    r = post(shopper_api, f"/api/orders/{order_id}/pay", {"card": CARD})
    assert r.status_code == 200
    ids = {q.headers["paypal-request-id"] for q in paypal.requests}
    assert len(ids) == 1  # PayPal returns the original order instead of a second one


def test_authorize_timeout_settled_by_get_order(shopper_api, paypal, order_id):
    paypal.add(json_response(201, order_body(status="APPROVED")), httpx.ReadTimeout("no reply"),
               json_response(200, order_body(auths=[auth()])))
    r = post(shopper_api, f"/api/orders/{order_id}/pay", {"card": CARD})
    assert r.status_code == 200
    assert paypal.paths()[-1] == "GET /v2/checkout/orders/PO-1"
    assert r.json()["payment"]["status"] == "AUTHORIZED"


def test_capture_5xx_is_unknown_then_settled(shopper_api, staff_api, paypal, order_id):
    pay_with_card(shopper_api, paypal, order_id)
    paypal.add(json_response(500, {"oops": True}), json_response(200, order_body(auths=[auth()],
                                                                                   captures=[orders_capture()])))
    r = post(staff_api, f"/api/orders/{order_id}/fulfil")
    assert r.status_code == 200
    assert r.json()["payment"]["status"] == "CAPTURED"


def test_capture_unknown_and_unreadable_stays_unknown(shopper_api, staff_api, paypal, order_id):
    pay_with_card(shopper_api, paypal, order_id)
    paypal.add(httpx.ReadTimeout("no reply"), httpx.ConnectError("refused"))
    r = post(staff_api, f"/api/orders/{order_id}/fulfil")
    assert r.status_code == 504 and r.json()["error"]["outcomeUnknown"] is True
    payment = payment_of(order_id)
    assert (payment.status, payment.pending_operation) == ("UNKNOWN", "capture")
    # The next fulfil settles first: PayPal shows no capture, so the claim is retaken and capture re-sent.
    paypal.add(json_response(200, order_body(auths=[auth()])), json_response(201, capture_body()))
    r = post(staff_api, f"/api/orders/{order_id}/fulfil")
    assert r.status_code == 200 and r.json()["payment"]["status"] == "CAPTURED"


def test_capture_timeout_where_paypal_shows_no_capture_is_a_known_failure(shopper_api, staff_api, paypal, order_id):
    pay_with_card(shopper_api, paypal, order_id)
    paypal.add(httpx.ReadTimeout("no reply"), json_response(200, order_body(auths=[auth()])))
    r = post(staff_api, f"/api/orders/{order_id}/fulfil")
    assert r.status_code == 502 and r.json()["error"]["outcomeUnknown"] is False
    assert payment_of(order_id).status == "AUTHORIZED"


def test_reauthorize_timeout_settled_then_captured(shopper_api, staff_api, paypal, order_id):
    pay_with_card(shopper_api, paypal, order_id, auth_created=NOW - timedelta(days=5))
    paypal.add(
        httpx.ReadTimeout("no reply"),
        json_response(200, order_body(auths=[auth("AUTH-1", created=NOW - timedelta(days=5)), auth("AUTH-2")])),
        json_response(201, capture_body()),
    )
    r = post(staff_api, f"/api/orders/{order_id}/fulfil")
    assert r.status_code == 200, r.content
    assert paypal.paths()[-1] == "POST /v2/payments/authorizations/AUTH-2/capture"
    assert r.json()["payment"]["status"] == "CAPTURED"


def test_void_timeout_settled(shopper_api, staff_api, paypal, order_id):
    pay_with_card(shopper_api, paypal, order_id)
    paypal.add(httpx.ReadTimeout("no reply"), json_response(200, payment_auth_body("AUTH-1", status="VOIDED")))
    r = post(staff_api, f"/api/orders/{order_id}/cancel")
    assert r.status_code == 200 and r.json()["payment"]["status"] == "VOIDED"
    assert paypal.paths()[-1] == "GET /v2/payments/authorizations/AUTH-1"


def test_refund_read_timeout_settled_by_custom_id(shopper_api, staff_api, paypal, order_id):
    pay_and_fulfil(shopper_api, staff_api, paypal, order_id)
    paypal.add(httpx.ReadTimeout("no reply"))
    # PayPal's order lists the refund under the custom_id we sent; we only know it after the call.
    original_send = paypal.send

    def send(request):
        if request.method == "GET":
            ref = PayPalRefund.objects.get().reference
            paypal.queue.insert(0, json_response(200, order_body(
                auths=[auth()], captures=[orders_capture()],
                refunds=[Refund.model_validate(refund_body("REF-9", value="4.00", custom_id=str(ref)))])))
        return original_send(request)

    paypal.send = send
    r = refund(shopper_api, order_id, "k1", "4.00")
    assert r.status_code == 201
    assert r.json()["refund"]["paypalRefundId"] == "REF-9"
    assert r.json()["payment"]["refundedAmount"] == "4.00"


def test_refund_timeout_not_found_at_paypal_is_released(shopper_api, staff_api, paypal, order_id):
    pay_and_fulfil(shopper_api, staff_api, paypal, order_id)
    paypal.add(httpx.ReadTimeout("no reply"), json_response(200, order_body(auths=[auth()],
                                                                          captures=[orders_capture()])))
    r = refund(shopper_api, order_id, "k1", "4.00")
    assert r.status_code == 502 and r.json()["error"]["outcomeUnknown"] is False
    assert PayPalRefund.objects.get().status == "FAILED"
    assert payment_of(order_id).refund_reserved == 0


# ---- the error boundary ---------------------------------------------------------------


def test_unsent_is_not_the_same_failure_as_unknown(shopper_api, staff_api, paypal, order_id):
    pay_with_card(shopper_api, paypal, order_id)
    paypal.add(httpx.ConnectError("refused"))
    unsent = post(staff_api, f"/api/orders/{order_id}/fulfil")
    paypal.add(httpx.ReadTimeout("no reply"), httpx.ConnectError("refused"))
    unknown = post(staff_api, f"/api/orders/{order_id}/fulfil")
    assert (unsent.status_code, unsent.json()["error"]["outcomeUnknown"]) == (502, False)
    assert (unknown.status_code, unknown.json()["error"]["outcomeUnknown"]) == (504, True)
    assert payment_of(order_id).status == "UNKNOWN"  # only the unknown one awaits reconciliation


def test_unreadable_error_body_is_still_a_rejection(shopper_api, staff_api, paypal, order_id):
    # PayPal's real 404 bodies can carry links without "rel", which the SDK's Error model rejects.
    pay_and_fulfil(shopper_api, staff_api, paypal, order_id)
    paypal.add(json_response(404, {"name": "RESOURCE_NOT_FOUND", "message": "m", "debug_id": "d",
                                   "links": [{"href": "https://x", "method": "GET"}]}))
    r = refund(shopper_api, order_id, "k1", "1.00")
    assert r.status_code == 404
    assert r.json()["error"]["outcomeUnknown"] is False
    assert payment_of(order_id).refund_reserved == 0


def test_truncated_success_body_is_an_unknown_outcome(shopper_api, staff_api, paypal, order_id):
    pay_with_card(shopper_api, paypal, order_id)
    paypal.add(json_response(201, {}), httpx.ConnectError("refused"))
    r = post(staff_api, f"/api/orders/{order_id}/fulfil")
    assert r.status_code == 502 and r.json()["error"]["outcomeUnknown"] is True
    assert payment_of(order_id).status == "UNKNOWN"


def test_paypal_credential_rejection_is_not_the_callers_fault(shopper_api, paypal, order_id):
    paypal.add(json_response(401, {"name": "AUTHENTICATION_FAILURE", "message": "m", "debug_id": "d"}))
    r = post(shopper_api, f"/api/orders/{order_id}/pay", {"card": CARD})
    assert r.status_code == 502


def test_bad_credentials_from_the_token_endpoint(settings, shopper_api, order_id):
    from pay_pal_server_sdk import PayPalServerSdkClient

    from apps.paypal_payments import client as client_module
    from apps.paypal_payments.transport import LoggingTransport

    from .conftest import StubTransport

    settings.PAYPAL_CURRENCY = "USD"
    transport = StubTransport()
    transport.add(json_response(401, {"error": "invalid_client", "error_description": "Client Authentication failed"}))
    previous = client_module.set_client(PayPalServerSdkClient(
        base_url="https://paypal.test", custom_http_client=LoggingTransport(transport), retry_options=0,
        oauth2={"client_id": "bad", "client_secret": "bad"}))
    try:
        r = post(shopper_api, f"/api/orders/{order_id}/pay", {"card": CARD})
    finally:
        client_module.set_client(previous)
    assert r.status_code == 502
    assert transport.paths() == ["POST /v1/oauth2/token"]
    assert payment_of(order_id).status == "DECLINED"  # nothing was sent; the shopper may retry


def test_card_details_never_reach_the_database_or_logs(shopper_api, paypal, order_id, caplog):
    caplog.set_level("DEBUG")
    pay_with_card(shopper_api, paypal, order_id)
    assert "4111111111111111" not in caplog.text
    from django.db import connection

    with connection.cursor() as cursor:
        for table in connection.introspection.table_names(cursor):
            if table.startswith(("paypal_payments_", "payment_")):
                cursor.execute(f'SELECT * FROM "{table}"')
                assert "4111111111111111" not in repr(cursor.fetchall())


# ---- Flow 2: saved cards ------------------------------------------------------------------


def vault_body(token="TOKEN-1", customer="CUST-1"):
    return {"id": token, "customer": {"id": customer},
            "payment_source": {"card": {"brand": "VISA", "last_digits": "1111", "expiry": "2030-12"}}}


def save_card(c, paypal, key="save-1", token="TOKEN-1"):
    paypal.add(json_response(201, vault_body(token)))
    r = post(c, "/api/payment-methods", {"card": CARD}, HTTP_IDEMPOTENCY_KEY=key)
    assert r.status_code == 201, r.content
    return r.json()["paymentMethodId"]


def test_save_card_list_and_pay_with_it(shopper_api, paypal, order_id):
    pm = save_card(shopper_api, paypal)
    assert paypal.paths()[-1] == "POST /v3/vault/payment-tokens"
    listed = shopper_api.get("/api/payment-methods").json()["paymentMethods"]
    assert listed == [{"paymentMethodId": pm, "brand": "VISA", "lastDigits": "1111", "expiry": "2030-12",
                       "label": "VISA ending 1111 (expires 12/2030)"}]

    paypal.add(json_response(201, order_body(auths=[auth()])))
    r = post(shopper_api, f"/api/orders/{order_id}/pay", {"paymentMethodId": pm})
    assert r.status_code == 200
    assert paypal.requests[-1].body.value["payment_source"] == {"card": {"vault_id": "TOKEN-1"}}
    assert r.json()["payment"]["paymentMethodId"] == pm


def test_second_card_uses_the_same_paypal_customer(shopper_api, paypal):
    save_card(shopper_api, paypal, "k1", "TOKEN-1")
    save_card(shopper_api, paypal, "k2", "TOKEN-2")
    assert paypal.requests[-1].body.value["customer"] == {"id": "CUST-1"}


def test_save_card_is_idempotent_per_key(shopper_api, paypal):
    pm = save_card(shopper_api, paypal, "same")
    calls = len(paypal.requests)
    r = post(shopper_api, "/api/payment-methods", {"card": CARD}, HTTP_IDEMPOTENCY_KEY="same")
    assert r.status_code == 200 and r.json()["paymentMethodId"] == pm
    assert len(paypal.requests) == calls


def test_saved_cards_are_private(shopper_api, other_api, paypal, product):
    pm = save_card(shopper_api, paypal)
    assert other_api.get("/api/payment-methods").json()["paymentMethods"] == []
    assert other_api.delete(f"/api/payment-methods/{pm}").status_code == 404
    other_order = post(other_api, "/api/orders", {"items": [{"itemId": product.pk, "quantity": 1}]}).json()["orderId"]
    assert post(other_api, f"/api/orders/{other_order}/pay", {"paymentMethodId": pm}).status_code == 404


def test_deleted_card_is_gone_and_unusable(shopper_api, paypal, order_id):
    pm = save_card(shopper_api, paypal)
    paypal.add(StubResponse(status_code=204))
    assert shopper_api.delete(f"/api/payment-methods/{pm}").status_code == 200
    assert paypal.paths()[-1] == "DELETE /v3/vault/payment-tokens/TOKEN-1"
    assert shopper_api.get("/api/payment-methods").json()["paymentMethods"] == []
    assert post(shopper_api, f"/api/orders/{order_id}/pay", {"paymentMethodId": pm}).status_code == 404


def test_delete_card_timeout_keeps_card_hidden(shopper_api, paypal, order_id):
    pm = save_card(shopper_api, paypal)
    paypal.add(httpx.ReadTimeout("no reply"))
    r = shopper_api.delete(f"/api/payment-methods/{pm}")
    assert r.status_code == 504
    assert PayPalVaultCard.objects.get().state == "DELETING"
    assert shopper_api.get("/api/payment-methods").json()["paymentMethods"] == []
    assert post(shopper_api, f"/api/orders/{order_id}/pay", {"paymentMethodId": pm}).status_code == 404
    # Re-driving the delete: PayPal no longer has the token -> deleted here too.
    paypal.add(json_response(404, {"name": "RESOURCE_NOT_FOUND", "message": "m", "debug_id": "d"}))
    assert shopper_api.delete(f"/api/payment-methods/{pm}").status_code == 200
    assert not PayPalVaultCard.objects.exists()


def test_vault_timeout_unknown(shopper_api, paypal):
    paypal.add(httpx.ReadTimeout("no reply"), httpx.ReadTimeout("no reply"))
    r = post(shopper_api, "/api/payment-methods", {"card": CARD}, HTTP_IDEMPOTENCY_KEY="k")
    assert r.status_code == 504 and r.json()["error"]["outcomeUnknown"] is True
    assert CardSaveClaim.objects.get().state == "UNKNOWN"
    assert shopper_api.get("/api/payment-methods").json()["paymentMethods"] == []
    ids = {q.headers["paypal-request-id"] for q in paypal.requests}
    assert len(ids) == 1  # the immediate re-send reused the request id


def test_vault_timeout_settled_by_listing_the_customers_cards(shopper_api, paypal):
    save_card(shopper_api, paypal, "first", "TOKEN-1")  # establishes the PayPal customer
    paypal.add(httpx.ReadTimeout("no reply"), httpx.ReadTimeout("no reply"))
    assert post(shopper_api, "/api/payment-methods", {"card": CARD}, HTTP_IDEMPOTENCY_KEY="k").status_code == 504
    paypal.add(json_response(200, {"payment_tokens": [vault_body("TOKEN-1"), vault_body("TOKEN-2")],
                                   "total_pages": 1}))
    r = post(shopper_api, "/api/payment-methods", {"card": CARD}, HTTP_IDEMPOTENCY_KEY="k")
    assert r.status_code == 200
    assert PayPalVaultCard.objects.filter(token_id="TOKEN-2").exists()
    assert paypal.paths()[-1] == "GET /v3/vault/payment-tokens"


def test_raw_card_number_is_not_stored_for_saved_cards(shopper_api, paypal):
    save_card(shopper_api, paypal)
    from oscar.core.loading import get_model

    card = get_model("payment", "Bankcard").objects.get()
    assert card.number == "XXXX-XXXX-XXXX-1111"
    assert card.name == ""
