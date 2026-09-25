from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from apps.payments import money
from apps.payments import paypal_gateway as gw
from paypal.models.enums import AuthorizationStatus, CaptureStatus, OrderStatus, RefundStatus

from .support import PaymentsTestCase, capture_body, json_response


def txn(txn_id, when, invoice=None, ref=None, amount="20.00"):
    info = {
        "transaction_id": txn_id,
        "paypal_reference_id": ref,
        "invoice_id": invoice,
        "transaction_initiation_date": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "transaction_amount": {"currency_code": "USD", "value": amount},
        "fee_amount": {"currency_code": "USD", "value": "-0.88"},
        "transaction_status": "S",
        "transaction_event_code": "T0006",
    }
    return {"transaction_info": {k: v for k, v in info.items() if v is not None}}  # PayPal omits, never nulls


class ReconciliationTests(PaymentsTestCase):
    def test_reads_every_page_and_lines_records_up(self):
        order_id = self.fulfilled_order()  # AUTH1 then CAP1, both on PayPal's clock
        now = datetime.now(timezone.utc)
        start, end = now - timedelta(days=2), now + timedelta(days=2)
        from apps.payments.models import PaymentOperation

        PaymentOperation.objects.filter(provider_id__in=["AUTH1", "CAP1"]).update(provider_time=now)
        self.transport.queue(
            json_response(
                200,
                {"transaction_details": [txn("CAP1", now, invoice=f"tst-{order_id}-1")], "total_pages": 2, "page": 1},
            ),
            json_response(
                200,
                {
                    "transaction_details": [
                        txn("FOREIGN1", now, invoice="other-1"),
                        txn("LOST1", now, invoice="tst-P9999999-1"),
                    ],
                    "total_pages": 2,
                    "page": 2,
                },
            ),
        )
        self.as_user(self.operator)
        response = self.client.get("/api/reconciliation", {"from": start.isoformat(), "to": end.isoformat()})
        self.assertEqual(response.status_code, 200, response.content)
        report = response.json()
        self.assertEqual(report["summary"]["providerRecords"], 3)
        self.assertEqual([m["transactionId"] for m in report["matched"]], ["CAP1"])
        self.assertEqual(
            {r["transactionId"]: r["thisInstall"] for r in report["providerOnly"]}, {"FOREIGN1": False, "LOST1": True}
        )
        self.assertEqual([r["paypalId"] for r in report["localOnly"]], ["AUTH1"])
        pages = [r.url for r in self.transport.operation_requests() if "/v1/reporting/" in r.url]
        self.assertEqual(len(pages), 2)
        self.assertIn("balance_affecting_records_only=N", pages[0])

    def test_long_ranges_are_split_into_windows_paypal_accepts(self):
        end = datetime(2026, 9, 1, tzinfo=timezone.utc)
        start = end - timedelta(days=65)
        self.transport.queue(*[json_response(200, {"transaction_details": [], "total_pages": 1})] * 3)
        self.as_user(self.operator)
        response = self.client.get("/api/reconciliation", {"from": start.isoformat(), "to": end.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.transport.calls()), 3)

    def test_records_outside_the_window_are_dropped(self):
        now = datetime.now(timezone.utc)
        self.transport.queue(
            json_response(200, {"transaction_details": [txn("EARLY", now - timedelta(hours=5))], "total_pages": 1})
        )
        self.as_user(self.operator)
        response = self.client.get(
            "/api/reconciliation", {"from": (now - timedelta(hours=1)).isoformat(), "to": now.isoformat()}
        )
        self.assertEqual(response.json()["summary"]["providerRecords"], 0)

    def test_an_unreadable_report_is_a_classified_provider_error(self):
        self.transport.queue(json_response(200, {"transaction_details": "not-a-list"}))
        self.as_user(self.operator)
        response = self.client.get(
            "/api/reconciliation", {"from": "2026-09-01T00:00:00Z", "to": "2026-09-02T00:00:00Z"}
        )
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (502, "provider_unreadable"))

    def test_bad_ranges_are_rejected(self):
        self.as_user(self.operator)
        self.assertEqual(
            self.client.get("/api/reconciliation", {"from": "nope", "to": "2026-01-01T00:00:00Z"}).status_code, 400
        )
        self.assertEqual(
            self.client.get(
                "/api/reconciliation", {"from": "2026-02-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"}
            ).status_code,
            400,
        )


class StatusMapTests(SimpleTestCase):
    def test_every_member_has_an_outcome_and_unknown_values_are_not_done(self):
        self.assertEqual(gw.authorization_outcome(AuthorizationStatus.CREATED), "done")
        self.assertEqual(gw.authorization_outcome(AuthorizationStatus.PENDING), "pending")
        self.assertEqual(gw.authorization_outcome(AuthorizationStatus.DENIED), "failed")
        self.assertEqual(gw.authorization_outcome(AuthorizationStatus.VOIDED), "failed")
        self.assertEqual(gw.void_outcome(AuthorizationStatus.VOIDED), "done")
        self.assertEqual(gw.void_outcome(AuthorizationStatus.CAPTURED), "failed")
        self.assertEqual(gw.capture_outcome(CaptureStatus.COMPLETED), "done")
        self.assertEqual(gw.capture_outcome(CaptureStatus.PENDING), "pending")
        self.assertEqual(gw.capture_outcome(CaptureStatus.REFUNDED), "failed")
        self.assertEqual(gw.capture_outcome(CaptureStatus.DECLINED), "failed")
        self.assertEqual(gw.refund_outcome(RefundStatus.COMPLETED), "done")
        self.assertEqual(gw.refund_outcome(RefundStatus.CANCELLED), "failed")
        self.assertEqual(gw.order_outcome(OrderStatus.PAYER_ACTION_REQUIRED), "pending")
        for mapper in (
            gw.authorization_outcome,
            gw.void_outcome,
            gw.capture_outcome,
            gw.refund_outcome,
            gw.order_outcome,
        ):
            self.assertEqual(mapper("SOMETHING_NEW"), "unknown")
            self.assertEqual(mapper(None), "unknown")

    def test_capture_without_a_breakdown_still_reads(self):
        from paypal.models import CapturedPayment

        body = capture_body()
        del body["seller_receivable_breakdown"]
        answer = gw.read_capture(CapturedPayment.model_validate(body))
        self.assertEqual((answer.outcome, answer.detail), ("done", {}))


class ConfigTests(SimpleTestCase):
    def config(self, **overrides):
        values = dict(
            client_id="id", client_secret="secret", environment="sandbox", currency="usd", base_url=None, timeout=5.0
        )
        values.update(overrides)
        return gw.PayPalConfig.from_values(**values)

    def test_sandbox_uses_the_sdk_host_and_base_url_overrides_it(self):
        self.assertEqual(self.config().base_url, "https://api-m.sandbox.paypal.com")
        self.assertEqual(self.config(base_url="http://mock.local:9000").base_url, "http://mock.local:9000")
        self.assertEqual(self.config().currency, "USD")

    def test_unknown_environment_without_base_url_fails_loudly(self):
        with self.assertRaises(gw.ConfigurationError):
            self.config(environment="live")
        self.assertEqual(self.config(environment="live", base_url="https://h.example").base_url, "https://h.example")

    def test_missing_credentials_fail_loudly(self):
        with self.assertRaises(gw.ConfigurationError):
            self.config(client_secret=None)

    def test_secret_is_not_in_the_repr(self):
        self.assertNotIn("secret", repr(self.config(client_secret="secret")).replace("client_secret", ""))

    def test_base_url_moves_the_token_request_too(self):
        from .support import StubTransport, token_response

        transport = StubTransport(token_response(), json_response(200, {"transaction_details": []}))
        client = gw.build_client(self.config(base_url="http://mock.local:9000"), transport)
        client.transaction_search.search_transactions("2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")
        self.assertTrue(all(r.url.startswith("http://mock.local:9000/") for r in transport.requests))
        self.assertTrue(transport.requests[0].url.endswith("/v1/oauth2/token"))


class MoneyTests(SimpleTestCase):
    def test_amounts_are_exact_in_the_currency(self):
        from decimal import Decimal

        self.assertEqual(money.to_wire(Decimal("20.00"), "USD"), "20.00")
        self.assertEqual(money.to_wire(Decimal("1000"), "JPY"), "1000")
        with self.assertRaises(money.AmountError):
            money.to_wire(Decimal("9.99"), "JPY")  # never rounded: the hold must equal the total
        with self.assertRaises(money.AmountError):
            money.parse("5.001", "USD")
        with self.assertRaises(money.AmountError):
            money.parse("-1", "USD")


class ReportingHorizonTests(PaymentsTestCase):
    def test_a_range_past_paypals_reporting_horizon_is_empty_not_an_error(self):
        from .support import json_response as jr

        self.transport.queue(
            jr(
                404,
                {
                    "name": "INVALID_REQUEST",
                    "message": "Data for the given start date is not available.",
                    "debug_id": "x",
                    "details": [],
                },
            )
        )
        self.as_user(self.operator)
        now = datetime.now(timezone.utc)
        response = self.client.get(
            "/api/reconciliation", {"from": (now - timedelta(minutes=30)).isoformat(), "to": now.isoformat()}
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["summary"]["providerRecords"], 0)
        self.assertIsNotNone(response.json()["providerDataUnavailableFrom"])

    def test_other_404s_are_still_errors(self):
        from .support import json_response as jr

        self.transport.queue(jr(404, {"name": "RESOURCE_NOT_FOUND", "message": "nope", "debug_id": "x"}))
        self.as_user(self.operator)
        response = self.client.get(
            "/api/reconciliation", {"from": "2026-09-01T00:00:00Z", "to": "2026-09-02T00:00:00Z"}
        )
        self.assertEqual(response.status_code, 404)
