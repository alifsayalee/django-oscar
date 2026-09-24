from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from apps.paypal_payments.models import PaymentOperation, PayPalPayment

from . import stubs
from .base import PREFIX, ApiTestCase


class ReconciliationTests(ApiTestCase):
    def query(self, start: str, end: str) -> dict[str, object]:
        self.as_user(self.staff)
        response = self.client.get("/api/reconciliation", {"from": start, "to": end})
        self.assertEqual(response.status_code, 200, response.content)
        body: dict[str, object] = response.json()
        return body

    def test_staff_only_and_parameters_validated(self) -> None:
        self.as_user(self.alice)
        self.assertEqual(self.client.get("/api/reconciliation", {"from": "2026-09-01T00:00:00Z",
                                                                  "to": "2026-09-02T00:00:00Z"}).status_code, 403)
        self.as_user(self.staff)
        self.assertEqual(self.client.get("/api/reconciliation", {"from": "2026-09-01"}).status_code, 400)
        self.assertEqual(self.client.get("/api/reconciliation", {"from": "2026-09-02T00:00:00Z",
                                                                  "to": "2026-09-01T00:00:00Z"}).status_code, 400)
        self.assertEqual(self.client.get("/api/reconciliation", {"from": "2026-09-01T00:00:00",
                                                                  "to": "2026-09-02T00:00:00"}).status_code, 400)

    def test_covers_every_window_and_every_page(self) -> None:
        # 40 days -> two windows (31 + 9 days); the first window has two pages.
        self.transport.queue(
            stubs.json_response(200, stubs.search_page([stubs.transaction_row("A", "2026-08-02T00:00:00Z")], 1, 2)),
            stubs.json_response(200, stubs.search_page([stubs.transaction_row("B", "2026-08-20T00:00:00Z")], 2, 2)),
            stubs.json_response(200, stubs.search_page([stubs.transaction_row("C", "2026-09-05T00:00:00Z")], 1, 1)),
        )
        body = self.query("2026-08-01T00:00:00Z", "2026-09-10T00:00:00Z")
        self.assertEqual(body["summary"]["paypalTransactions"], 3)  # type: ignore[index]
        queries = [parse_qs(urlsplit(r.url).query) for r in self.transport.requests]
        self.assertEqual([(q["start_date"][0], q["end_date"][0], q["page"][0]) for q in queries], [
            ("2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z", "1"),
            ("2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z", "2"),
            ("2026-09-01T00:00:00Z", "2026-09-10T00:00:00Z", "1"),
        ])
        self.assertEqual(queries[0]["balance_affecting_records_only"], ["N"])

    def test_matches_every_record_of_an_order_and_reports_both_sides(self) -> None:
        order_id = self.place_order()
        payment = PayPalPayment.objects.get(order__number=order_id)
        when = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        for kind, provider_id in (("pay", "AUTH1"), ("capture", "CAP1"), ("refund", "RF1"), ("refund", "RF2")):
            PaymentOperation.objects.create(
                reference="%s-%s" % (kind, provider_id), kind=kind, payment=payment, outcome="done",
                provider_id=provider_id, provider_time=when, amount=payment.amount, currency="USD")
        # A write whose outcome is unknown: no provider time yet.
        PaymentOperation.objects.create(reference="pay-x", kind="pay", payment=payment, outcome="unknown",
                                        claimed_at=when)
        self.transport.queue(stubs.json_response(200, stubs.search_page([
            stubs.transaction_row("AUTH1", "2026-09-10T12:00:00Z"),
            stubs.transaction_row("CAP1", "2026-09-10T12:00:01Z"),
            stubs.transaction_row("RF1", "2026-09-10T12:00:02Z"),
            dict(stubs.transaction_row("VOIDEVT", "2026-09-10T12:00:02Z", code="T9900"), paypal_reference_id="AUTH1"),
            stubs.transaction_row("ZZZ", "2026-09-10T12:00:03Z", invoice_id="%s-%s-pay-9" % (PREFIX, order_id)),
            stubs.transaction_row("OTHER", "2026-09-10T12:00:04Z"),
            stubs.transaction_row("LATE", "2026-09-11T00:00:00Z"),  # outside the window: narrowed out
        ])))
        body = self.query("2026-09-10T00:00:00Z", "2026-09-11T00:00:00Z")
        summary = body["summary"]
        self.assertEqual(summary, {  # type: ignore[comparison-overlap]
            "paypalTransactions": 6, "matchedOrders": 1, "paypalOnlyWithOurReference": 1,
            "paypalOnlyOther": 1, "appOnly": 1, "unsettled": 1,
        })
        (matched,) = body["matched"]  # type: ignore[misc]
        self.assertEqual({row["transactionId"] for row in matched["paypal"]}, {"AUTH1", "CAP1", "RF1", "VOIDEVT"})
        self.assertEqual(body["appOnly"][0]["paypalId"], "RF2")  # type: ignore[index]

    def test_window_paypal_has_not_processed_yet_is_reported_not_failed(self) -> None:
        self.transport.queue(stubs.json_response(404, {
            "name": "INVALID_REQUEST", "message": "Data for the given start date is not available.",
            "debug_id": "d", "details": [], "links": []}))
        body = self.query("2026-09-24T13:30:00Z", "2026-09-24T14:30:00Z")
        self.assertEqual(body["summary"]["paypalTransactions"], 0)  # type: ignore[index]
        self.assertEqual(body["paypalDataNotYetAvailable"],
                         [{"from": "2026-09-24T13:30:00Z", "to": "2026-09-24T14:30:00Z"}])

    def test_empty_range_is_a_normal_result(self) -> None:
        self.transport.queue(stubs.json_response(200, {"transaction_details": [], "page": 1, "total_pages": 0}))
        body = self.query("2026-09-24T00:00:00Z", "2026-09-25T00:00:00Z")
        self.assertEqual(body["summary"]["paypalTransactions"], 0)  # type: ignore[index]
