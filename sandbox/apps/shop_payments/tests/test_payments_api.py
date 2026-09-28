from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from apps.shop_payments import paypal_client
from apps.shop_payments.models import PaymentWrite, PayPalPayment

from .conftest import (
    CARD,
    api_client,
    authorization,
    capture,
    json_response,
    paypal_error,
    paypal_order,
    place_order,
    post,
    refund,
)

pytestmark = pytest.mark.django_db

ORDERS = r"/v2/checkout/orders"
AUTH = r"/v2/payments/authorizations/{}"


def pay(client: Any, order_id: str, body: dict[str, Any] | None = None) -> Any:
    return post(client, f"/api/orders/{order_id}/pay", body or {"card": CARD})


def authorized_order(stub: Any, shopper: Any, product: Any) -> tuple[Any, str]:
    client = api_client(shopper)
    order_id = place_order(client, product)
    stub.on("POST", ORDERS, json_response(201, paypal_order()))
    assert pay(client, order_id).status_code == 200
    return client, order_id


# --- order placement and authorization ---------------------------------------------------


def test_order_starts_awaiting_payment_with_catalogue_total(stub: Any, shopper: Any, product: Any) -> None:
    client = api_client(shopper)
    order_id = place_order(client, product, quantity=2)
    body = client.get("/api/my-orders").json()["orders"][0]
    assert body["orderId"] == order_id
    assert body["status"] == "Pending" and body["awaitingPayment"] is True
    assert body["total"] == "20.00" and body["currency"] == "USD"
    assert stub.requests == []  # placing an order moves no money


def test_pay_holds_exactly_the_order_total(stub: Any, shopper: Any, product: Any) -> None:
    client = api_client(shopper)
    order_id = place_order(client, product)
    stub.on("POST", ORDERS, json_response(201, paypal_order()))

    response = pay(client, order_id)

    assert response.status_code == 200
    payment = response.json()["order"]["payment"]
    assert payment["state"] == "authorized" and payment["authorization"]["id"] == "AUTH1"
    sent = stub.calls("POST", ORDERS)[0]
    unit = sent.body.value["purchase_units"][0]
    assert sent.body.value["intent"] == "AUTHORIZE"
    assert unit["amount"] == {"currency_code": "USD", "value": "20.00"}
    assert sent.headers["paypal-request-id"] == f"t:o{order_id}:auth:1"
    assert response.json()["order"]["status"] == "Being processed"


def test_the_same_pay_twice_sends_one_reference_and_one_call(stub: Any, shopper: Any, product: Any) -> None:
    client = api_client(shopper)
    order_id = place_order(client, product)
    stub.on("POST", ORDERS, json_response(201, paypal_order()))

    first = pay(client, order_id)
    second = pay(client, order_id)

    assert len(stub.calls("POST", ORDERS)) == 1
    assert first.status_code == second.status_code == 200
    assert second.json()["order"]["payment"]["authorization"]["id"] == "AUTH1"


def test_an_id_with_a_denied_status_is_not_authorized(stub: Any, shopper: Any, product: Any) -> None:
    client = api_client(shopper)
    order_id = place_order(client, product)
    stub.on("POST", ORDERS, json_response(201, paypal_order(auth_status="DENIED")))

    response = pay(client, order_id)

    assert response.status_code == 402
    assert response.json()["error"]["code"] == "card_declined"
    payment = PayPalPayment.objects.get(order__number=order_id)
    assert payment.state == PayPalPayment.AWAITING_PAYMENT and payment.authorize_attempt == 2
    assert payment.source is None  # nothing recorded as held


def test_an_unlisted_status_is_unknown_not_done(stub: Any, shopper: Any, product: Any) -> None:
    client = api_client(shopper)
    order_id = place_order(client, product)
    stub.on("POST", ORDERS, json_response(201, paypal_order(auth_status="SOMETHING_NEW")))

    response = pay(client, order_id)

    assert response.status_code == 504 and response.json()["outcomeUnknown"] is True
    assert PaymentWrite.objects.get(kind="authorize").outcome == "unknown"
    assert PayPalPayment.objects.get(order__number=order_id).state == PayPalPayment.AUTHORIZING


def test_declined_card_422_is_a_known_refusal(stub: Any, shopper: Any, product: Any) -> None:
    client = api_client(shopper)
    order_id = place_order(client, product)
    stub.on("POST", ORDERS, json_response(422, paypal_error("UNPROCESSABLE_ENTITY", "CARD_DECLINED",
                                                              "The card was declined.")))
    response = pay(client, order_id)
    assert response.status_code == 402
    assert "declined" in response.json()["error"]["message"]
    assert response.json()["outcomeUnknown"] is False


def test_payer_action_required_is_reported_not_built(stub: Any, shopper: Any, product: Any) -> None:
    client = api_client(shopper)
    order_id = place_order(client, product)
    body = paypal_order()
    body["status"] = "PAYER_ACTION_REQUIRED"
    del body["purchase_units"][0]["payments"]
    stub.on("POST", ORDERS, json_response(201, body))
    response = pay(client, order_id)
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "payer_action_required"


def test_unsent_is_not_the_same_failure_as_unknown(stub: Any, shopper: Any, product: Any) -> None:
    client = api_client(shopper)
    unsent_order = place_order(client, product)
    unknown_order = place_order(client, product)
    stub.on("POST", ORDERS, httpx.ConnectError("refused"))
    unsent = pay(client, unsent_order)
    stub.routes.clear()
    stub.on("POST", ORDERS, httpx.ReadTimeout("no reply"))
    unknown = pay(client, unknown_order)

    assert (unsent.status_code, unsent.json()["outcomeUnknown"]) == (502, False)
    assert (unknown.status_code, unknown.json()["outcomeUnknown"]) == (504, True)
    # The timed-out write was checked by re-sending under the SAME reference, never a new one.
    references = {r.headers["paypal-request-id"] for r in stub.calls("POST", ORDERS)
                  if f"o{unknown_order}" in r.headers["paypal-request-id"]}
    assert references == {f"t:o{unknown_order}:auth:1"}


def test_an_unknown_authorization_is_settled_by_the_same_reference(stub: Any, shopper: Any, product: Any) -> None:
    client = api_client(shopper)
    order_id = place_order(client, product)
    stub.on("POST", ORDERS, httpx.ReadTimeout("no reply"))
    assert pay(client, order_id).status_code == 504

    stub.routes.clear()
    stub.on("POST", ORDERS, json_response(201, paypal_order()))
    response = pay(client, order_id)  # the shopper repeats the request

    assert response.status_code == 200
    assert response.json()["order"]["payment"]["state"] == "authorized"
    assert {r.headers["paypal-request-id"] for r in stub.calls("POST", ORDERS)} == {f"t:o{order_id}:auth:1"}


def test_amount_echo_mismatch_needs_review(stub: Any, shopper: Any, product: Any) -> None:
    client = api_client(shopper)
    order_id = place_order(client, product)
    stub.on("POST", ORDERS, json_response(201, paypal_order(value="19.99")))
    response = pay(client, order_id)
    assert response.status_code == 409 and response.json()["error"]["code"] == "amount_mismatch"
    assert PayPalPayment.objects.get(order__number=order_id).state == PayPalPayment.NEEDS_REVIEW


def test_bad_credentials_is_a_config_error(paypal_settings: Any, shopper: Any, product: Any) -> None:
    class Refusing:
        def send(self, request: Any) -> Any:
            return json_response(401, {"error": "invalid_client", "error_description": "Client Authentication failed"})

        def close(self) -> None:
            pass

    from paypal import PaypalClient
    from paypal.core import ClientCredentials

    paypal_client.set_client(PaypalClient(custom_http_client=Refusing(),
                                          oauth2=ClientCredentials(client_id="x", client_secret="y")))
    try:
        client = api_client(shopper)
        response = pay(client, place_order(client, product))
    finally:
        paypal_client.set_client(None)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "paypal_auth_failed"


# --- fulfil ------------------------------------------------------------------------------------


def test_fulfil_captures_and_records_fee_and_net(stub: Any, shopper: Any, staff: Any, product: Any) -> None:
    _, order_id = authorized_order(stub, shopper, product)
    stub.on("GET", AUTH.format("AUTH1"), json_response(200, authorization()))
    stub.on("POST", AUTH.format("AUTH1") + "/capture", json_response(201, capture()))

    response = post(api_client(staff), f"/api/orders/{order_id}/fulfil")

    assert response.status_code == 200
    body = response.json()["order"]
    assert body["status"] == "Complete"
    assert body["payment"]["capture"] == {
        "id": "CAP1", "status": "COMPLETED", "amount": "20.00", "paypalFee": "0.88", "netAmount": "19.12",
        "capturedAt": "2026-09-28T02:00:00+00:00",
    }
    sent = stub.calls("POST", AUTH.format("AUTH1") + "/capture")[0]
    assert sent.body.value["amount"] == {"currency_code": "USD", "value": "20.00"}
    assert sent.headers["paypal-request-id"] == f"t:o{order_id}:capture:AUTH1"


def test_fulfil_twice_captures_once(stub: Any, shopper: Any, staff: Any, product: Any) -> None:
    _, order_id = authorized_order(stub, shopper, product)
    stub.on("GET", AUTH.format("AUTH1"), json_response(200, authorization()))
    stub.on("POST", AUTH.format("AUTH1") + "/capture", json_response(201, capture()))
    operator = api_client(staff)
    assert post(operator, f"/api/orders/{order_id}/fulfil").status_code == 200
    assert post(operator, f"/api/orders/{order_id}/fulfil").status_code == 200
    assert len(stub.calls("POST", AUTH.format("AUTH1") + "/capture")) == 1


def test_a_pending_capture_is_not_fulfilled(stub: Any, shopper: Any, staff: Any, product: Any) -> None:
    _, order_id = authorized_order(stub, shopper, product)
    stub.on("GET", AUTH.format("AUTH1"), json_response(200, authorization()))
    stub.on("POST", AUTH.format("AUTH1") + "/capture", json_response(201, capture(status="PENDING")))
    response = post(api_client(staff), f"/api/orders/{order_id}/fulfil")
    assert response.status_code == 202
    assert response.json()["order"]["payment"]["state"] == "capture_pending"
    assert response.json()["order"]["status"] != "Complete"


def _age_authorization(order_id: str, days: int) -> None:
    PayPalPayment.objects.filter(order__number=order_id).update(
        authorization_created_at=timezone.now() - timedelta(days=days),
        authorization_expires_at=timezone.now() + timedelta(days=29 - days),
    )


def test_stale_authorization_is_renewed_before_capture(stub: Any, shopper: Any, staff: Any, product: Any) -> None:
    _, order_id = authorized_order(stub, shopper, product)
    _age_authorization(order_id, days=5)
    future = (timezone.now() + timedelta(days=29)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stub.on("GET", AUTH.format("AUTH1"), json_response(200, authorization(expires=future)))
    stub.on("POST", AUTH.format("AUTH1") + "/reauthorize",
            json_response(201, authorization(auth_id="AUTH2", expires=future)))
    stub.on("POST", AUTH.format("AUTH2") + "/capture", json_response(201, capture()))

    response = post(api_client(staff), f"/api/orders/{order_id}/fulfil")

    assert response.status_code == 200, response.content
    payment = response.json()["order"]["payment"]
    assert payment["authorization"]["id"] == "AUTH2" and payment["authorization"]["reauthorizedAt"]
    assert payment["capture"]["id"] == "CAP1"
    assert stub.calls("POST", AUTH.format("AUTH1") + "/capture") == []


def test_unrenewable_authorization_says_what_to_do(stub: Any, shopper: Any, staff: Any, product: Any) -> None:
    _, order_id = authorized_order(stub, shopper, product)
    _age_authorization(order_id, days=5)
    future = (timezone.now() + timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stub.on("GET", AUTH.format("AUTH1"), json_response(200, authorization(expires=future)))
    stub.on("POST", AUTH.format("AUTH1") + "/reauthorize", json_response(
        422, paypal_error("UNPROCESSABLE_ENTITY", "REAUTHORIZATION_NOT_ALLOWED", "Reauthorization is not allowed.")))

    response = post(api_client(staff), f"/api/orders/{order_id}/fulfil")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "authorization_not_renewable"
    assert "Cancel this order" in error["message"]
    assert response.json()["order"]["payment"]["state"] == "authorized"


def test_expired_authorization_is_not_captured(stub: Any, shopper: Any, staff: Any, product: Any) -> None:
    _, order_id = authorized_order(stub, shopper, product)
    past = (timezone.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stub.on("GET", AUTH.format("AUTH1"), json_response(200, authorization(expires=past)))
    response = post(api_client(staff), f"/api/orders/{order_id}/fulfil")
    assert response.status_code == 409 and response.json()["error"]["code"] == "authorization_expired"
    assert stub.calls("POST", AUTH.format("AUTH1") + "/capture") == []


# --- cancel ------------------------------------------------------------------------------------


def test_cancel_voids_the_hold(stub: Any, shopper: Any, staff: Any, product: Any) -> None:
    _, order_id = authorized_order(stub, shopper, product)
    stub.on("POST", AUTH.format("AUTH1") + "/void", json_response(200, authorization(status="VOIDED")))
    response = post(api_client(staff), f"/api/orders/{order_id}/cancel")
    assert response.status_code == 200
    assert response.json()["order"]["status"] == "Cancelled"
    assert response.json()["order"]["payment"]["state"] == "voided"


def test_cancel_after_capture_is_refused_by_paypal_state(stub: Any, shopper: Any, staff: Any, product: Any) -> None:
    _, order_id = authorized_order(stub, shopper, product)
    stub.on("POST", AUTH.format("AUTH1") + "/void", json_response(200, authorization(status="CAPTURED")))
    response = post(api_client(staff), f"/api/orders/{order_id}/cancel")
    assert response.status_code == 409 and response.json()["error"]["code"] == "already_captured"


def test_cancel_unpaid_order_moves_no_money(stub: Any, shopper: Any, staff: Any, product: Any) -> None:
    order_id = place_order(api_client(shopper), product)
    response = post(api_client(staff), f"/api/orders/{order_id}/cancel")
    assert response.status_code == 200 and response.json()["order"]["status"] == "Cancelled"
    assert stub.requests == []


# --- refunds -----------------------------------------------------------------------------------


def captured_order(stub: Any, shopper: Any, staff: Any, product: Any) -> tuple[Any, str]:
    client, order_id = authorized_order(stub, shopper, product)
    stub.on("GET", AUTH.format("AUTH1"), json_response(200, authorization()))
    stub.on("POST", AUTH.format("AUTH1") + "/capture", json_response(201, capture()))
    assert post(api_client(staff), f"/api/orders/{order_id}/fulfil").status_code == 200
    return client, order_id


REFUND = r"/v2/payments/captures/CAP1/refund"


def test_same_refund_key_refunds_once(stub: Any, shopper: Any, staff: Any, product: Any) -> None:
    client, order_id = captured_order(stub, shopper, staff, product)
    stub.on("POST", REFUND, json_response(201, refund("5.00")))
    first = post(client, f"/api/orders/{order_id}/refunds", {"amount": "5.00"}, **{"Idempotency-Key": "k1"})
    again = post(client, f"/api/orders/{order_id}/refunds", {"amount": "5.00"}, **{"Idempotency-Key": "k1"})
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["refundId"] == again.json()["refundId"]
    assert len(stub.calls("POST", REFUND)) == 1


def test_distinct_partial_refunds_never_exceed_capture(stub: Any, shopper: Any, staff: Any, product: Any) -> None:
    client, order_id = captured_order(stub, shopper, staff, product)
    stub.on("POST", REFUND, json_response(201, refund("8.00", "R1")), json_response(201, refund("8.00", "R2")))
    url = f"/api/orders/{order_id}/refunds"
    assert post(client, url, {"amount": "8.00"}, **{"Idempotency-Key": "a"}).status_code == 201
    assert post(client, url, {"amount": "8.00"}, **{"Idempotency-Key": "b"}).status_code == 201
    too_much = post(client, url, {"amount": "8.00"}, **{"Idempotency-Key": "c"})
    assert too_much.status_code == 409 and too_much.json()["error"]["code"] == "exceeds_refundable"
    assert too_much.json()["error"]["refundableAmount"] == "4.00"
    assert len(stub.calls("POST", REFUND)) == 2
    payment = PayPalPayment.objects.get(order__number=order_id)
    assert payment.refunded_amount == payment.refund_reserved == 16


def test_refused_refund_releases_the_reservation(stub: Any, shopper: Any, staff: Any, product: Any) -> None:
    client, order_id = captured_order(stub, shopper, staff, product)
    stub.on("POST", REFUND, json_response(422, paypal_error("UNPROCESSABLE_ENTITY", "REFUND_NOT_ALLOWED")))
    response = post(client, f"/api/orders/{order_id}/refunds", {"amount": "5.00"}, **{"Idempotency-Key": "k"})
    assert response.status_code == 402 and response.json()["error"]["code"] == "refund_rejected"
    assert PayPalPayment.objects.get(order__number=order_id).refund_reserved == 0


def test_same_key_different_amount_is_rejected(stub: Any, shopper: Any, staff: Any, product: Any) -> None:
    client, order_id = captured_order(stub, shopper, staff, product)
    stub.on("POST", REFUND, json_response(201, refund("5.00")))
    url = f"/api/orders/{order_id}/refunds"
    assert post(client, url, {"amount": "5.00"}, **{"Idempotency-Key": "k"}).status_code == 201
    response = post(client, url, {"amount": "6.00"}, **{"Idempotency-Key": "k"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "idempotency_key_reused"


def test_refund_requires_an_idempotency_key(stub: Any, shopper: Any, staff: Any, product: Any) -> None:
    client, order_id = captured_order(stub, shopper, staff, product)
    response = post(client, f"/api/orders/{order_id}/refunds", {"amount": "1.00"})
    assert response.status_code == 400


# --- error bodies the SDK cannot decode -----------------------------------------------------


def test_undecodable_rejection_is_a_refusal_not_unknown(stub: Any, shopper: Any, product: Any) -> None:
    client = api_client(shopper)
    order_id = place_order(client, product)
    body = paypal_error("RESOURCE_NOT_FOUND", "TOKEN_NOT_FOUND")
    body["links"] = [{"href": "https://example.invalid", "method": "GET"}]  # no "rel": fails the SDK model
    stub.on("POST", ORDERS, json_response(404, body))
    response = pay(client, order_id)
    assert response.json()["outcomeUnknown"] is False
    assert response.status_code == 402
    assert PayPalPayment.objects.get(order__number=order_id).authorize_attempt == 2


# --- access control ----------------------------------------------------------------------------


def test_callers_see_and_act_only_on_their_own_orders(
    stub: Any, shopper: Any, other_shopper: Any, staff: Any, product: Any
) -> None:
    order_id = place_order(api_client(shopper), product)
    intruder = api_client(other_shopper)
    assert pay(intruder, order_id).status_code == 404
    assert post(intruder, f"/api/orders/{order_id}/refunds", {}, **{"Idempotency-Key": "x"}).status_code == 404
    assert intruder.get("/api/my-orders").json()["orders"] == []
    assert post(intruder, f"/api/orders/{order_id}/fulfil").status_code == 403
    assert post(intruder, f"/api/orders/{order_id}/cancel").status_code == 403
    assert intruder.get("/api/reconciliation?from=2026-01-01T00:00:00Z&to=2026-01-02T00:00:00Z").status_code == 403
    from django.test import Client

    assert Client().get("/api/my-orders").status_code == 401


# --- configuration -----------------------------------------------------------------------------


def test_base_url_override_is_used_verbatim() -> None:
    assert paypal_client.resolve_base_url("sandbox", "https://mock.local:9000") == "https://mock.local:9000"
    assert paypal_client.resolve_base_url("sandbox", None) == paypal_client.SANDBOX_BASE_URL
    with pytest.raises(ImproperlyConfigured):
        paypal_client.resolve_base_url("live", None)


def test_base_url_override_moves_the_token_request_too(settings: Any) -> None:
    settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET = "id", "secret"
    settings.PAYPAL_ENVIRONMENT, settings.PAYPAL_CURRENCY = "sandbox", "USD"
    settings.PAYPAL_BASE_URL = "https://mock.local:9000"
    seen: list[str] = []

    class Recorder:
        def send(self, request: Any) -> Any:
            seen.append(request.url)
            if request.url.endswith("/v1/oauth2/token"):
                return json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 60})
            return json_response(200, authorization())

        def close(self) -> None:
            pass

    from paypal import PaypalClient
    from paypal.core import ClientCredentials

    config = paypal_client.get_config()
    client = PaypalClient(base_url=config.base_url, custom_http_client=Recorder(),
                          oauth2=ClientCredentials(client_id=config.client_id, client_secret=config.client_secret))
    client.payments.get_authorized_payment("AUTH1")
    assert seen == ["https://mock.local:9000/v1/oauth2/token", "https://mock.local:9000/v2/payments/authorizations/AUTH1"]
