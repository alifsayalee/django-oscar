from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from typing import Any

import httpx
import pytest
from django.utils import timezone

from apps.shop_payments.models import PaymentWrite

from .conftest import CARD, api_client, json_response, paypal_order, place_order, post

pytestmark = pytest.mark.django_db

TOKENS = r"/v3/vault/payment-tokens"


def token(token_id: str = "TOK1") -> dict[str, Any]:
    return {"id": token_id, "customer": {"id": "C1"},
            "payment_source": {"card": {"last_digits": "1111", "brand": "VISA", "expiry": "2030-12"}}}


def save(client: Any, key: str = "save-1") -> Any:
    return post(client, "/api/payment-methods", {"card": CARD}, **{"Idempotency-Key": key})


def test_saving_a_card_stores_no_card_number(stub: Any, shopper: Any) -> None:
    stub.on("POST", TOKENS, json_response(201, token()))
    response = save(api_client(shopper))
    assert response.status_code == 201
    body = response.json()
    assert body["paymentMethodId"] and body["last4"] == "1111" and body["brand"] == "VISA"
    assert CARD["number"] not in response.content.decode()
    from oscar.apps.payment.models import Bankcard

    card = Bankcard.objects.get()
    assert card.number == "XXXX-XXXX-XXXX-1111" and card.partner_reference == "TOK1"
    sent = stub.calls("POST", TOKENS)[0]
    assert sent.body.value["payment_source"]["card"]["number"] == CARD["number"]  # straight to PayPal only


def test_same_save_key_vaults_once(stub: Any, shopper: Any) -> None:
    stub.on("POST", TOKENS, json_response(201, token()))
    client = api_client(shopper)
    first, again = save(client), save(client)
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["paymentMethodId"] == again.json()["paymentMethodId"]
    assert len(stub.calls("POST", TOKENS)) == 1


def test_saved_card_pays_a_later_order(stub: Any, shopper: Any, product: Any) -> None:
    client = api_client(shopper)
    stub.on("POST", TOKENS, json_response(201, token()))
    method_id = save(client).json()["paymentMethodId"]
    order_id = place_order(client, product)
    stub.on("POST", r"/v2/checkout/orders", json_response(201, paypal_order()))

    response = post(client, f"/api/orders/{order_id}/pay", {"paymentMethodId": method_id})

    assert response.status_code == 200
    sent = stub.calls("POST", r"/v2/checkout/orders")[0]
    assert sent.body.value["payment_source"] == {"card": {"vault_id": "TOK1"}}


def test_cards_belong_to_their_owner(stub: Any, shopper: Any, other_shopper: Any, product: Any) -> None:
    stub.on("POST", TOKENS, json_response(201, token()))
    method_id = save(api_client(shopper)).json()["paymentMethodId"]
    intruder = api_client(other_shopper)
    assert intruder.get("/api/payment-methods").json()["paymentMethods"] == []
    assert intruder.delete(f"/api/payment-methods/{method_id}").status_code == 404
    order_id = place_order(intruder, product)
    response = post(intruder, f"/api/orders/{order_id}/pay", {"paymentMethodId": method_id})
    assert response.status_code == 404
    assert stub.calls("POST", r"/v2/checkout/orders") == []


def test_deleted_card_is_gone_and_unusable(stub: Any, shopper: Any, product: Any) -> None:
    client = api_client(shopper)
    stub.on("POST", TOKENS, json_response(201, token()))
    method_id = save(client).json()["paymentMethodId"]
    stub.on("DELETE", TOKENS + "/TOK1", json_response(204, {}))

    assert client.delete(f"/api/payment-methods/{method_id}").status_code == 204
    assert client.get("/api/payment-methods").json()["paymentMethods"] == []
    order_id = place_order(client, product)
    assert post(client, f"/api/orders/{order_id}/pay", {"paymentMethodId": method_id}).status_code == 404


def test_delete_with_lost_answer_hides_card_until_settled(stub: Any, shopper: Any) -> None:
    client = api_client(shopper)
    stub.on("POST", TOKENS, json_response(201, token()))
    method_id = save(client).json()["paymentMethodId"]
    stub.on("DELETE", TOKENS + "/TOK1", httpx.ReadTimeout("no reply"))

    response = client.delete(f"/api/payment-methods/{method_id}")

    assert response.status_code == 504 and response.json()["outcomeUnknown"] is True
    assert client.get("/api/payment-methods").json()["paymentMethods"] == []  # hidden and unusable meanwhile
    stub.routes.clear()
    stub.on("DELETE", TOKENS + "/TOK1", json_response(404, {"name": "RESOURCE_NOT_FOUND", "message": "gone",
                                                            "debug_id": "d"}))
    assert client.delete(f"/api/payment-methods/{method_id}").status_code == 204  # 404: already gone


# --- reconciliation --------------------------------------------------------------------------


def txn(txn_id: str, when: str, value: str, custom: str = "") -> dict[str, Any]:
    return {"transaction_info": {"transaction_id": txn_id, "transaction_event_code": "T0006",
                                 "transaction_initiation_date": when, "transaction_status": "S",
                                 "transaction_amount": {"currency_code": "USD", "value": value},
                                 "fee_amount": {"currency_code": "USD", "value": "-0.50"},
                                 "custom_field": custom}}


def test_reconciliation_walks_every_window_and_page_and_matches(stub: Any, staff: Any) -> None:
    PaymentWrite.objects.create(reference="t:o1:capture:A", kind="capture", outcome="done", provider_id="CAP-MATCH",
                                amount=Decimal("20.00"), currency="USD", claimed_at=timezone.now(),
                                provider_time=datetime(2026, 8, 10, tzinfo=dt_timezone.utc))
    PaymentWrite.objects.create(reference="t:o2:capture:B", kind="capture", outcome="done", provider_id="CAP-LOCAL",
                                amount=Decimal("5.00"), currency="USD", claimed_at=timezone.now(),
                                provider_time=datetime(2026, 9, 20, tzinfo=dt_timezone.utc))

    def search(request: Any) -> Any:
        query = request.url.split("?", 1)[1]
        params = dict(p.split("=", 1) for p in query.split("&"))
        page = int(params["page"])
        if params["start_date"].startswith("2026-08-01"):
            rows = [txn("CAP-MATCH", "2026-08-10T00:00:00+0000", "20.00")] if page == 1 else \
                   [txn("THEIRS", "2026-08-11T00:00:00+0000", "7.00", custom="t:o9")]
            return json_response(200, {"transaction_details": rows, "page": page, "total_pages": 2})
        return json_response(200, {"transaction_details": [], "page": 1, "total_pages": 1})

    stub.on("GET", r"/v1/reporting/transactions", search)
    response = api_client(staff).get("/api/reconciliation?from=2026-08-01T00:00:00Z&to=2026-10-01T00:00:00Z")

    assert response.status_code == 200, response.content
    report = response.json()
    windows = {r.url.split("start_date=")[1][:10] for r in stub.calls("GET", r"/v1/reporting/transactions")}
    assert len(windows) == 2  # 61 days -> two <=31-day windows
    assert len(stub.calls("GET", r"/v1/reporting/transactions")) == 3  # window 1 has two pages
    assert [m["paypalId"] for m in report["matched"]] == ["CAP-MATCH"]
    assert report["matched"][0]["amountMatches"] is True
    assert [r["paypalId"] for r in report["localOnly"]] == ["CAP-LOCAL"]
    assert [r["transactionId"] for r in report["paypalOnly"]] == ["THEIRS"]
    assert report["paypalOnly"][0]["referencesThisShop"] is True


def test_reconciliation_rejects_bad_ranges(stub: Any, staff: Any) -> None:
    client = api_client(staff)
    assert client.get("/api/reconciliation?from=nope&to=2026-01-01T00:00:00Z").status_code == 400
    assert client.get("/api/reconciliation?from=2026-02-01T00:00:00Z&to=2026-01-01T00:00:00Z").status_code == 400
