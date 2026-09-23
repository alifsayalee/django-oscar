# PayPal checkout API (`apps.paypal_checkout`)

An additive Django app for the Oscar sandbox that collects money with **PayPal** as
the processor and lets a shopper **save a card** for reuse. It does not replace the
existing catalogue/basket/order flow — it adds the money movement and the operator
flows that follow a real payment.

## What it reuses vs. adds

* **Reuses Oscar's own models**: orders/lines come from `oscar.apps.order` (built via
  `OrderCreator` from a real Oscar basket); the money ledger is `oscar.apps.payment`
  `Source`/`Transaction`; saved cards are `oscar.apps.payment` `Bankcard`
  (`partner_reference` holds the PayPal vault token; only a masked number + brand +
  expiry are stored — never the PAN or CVV).
* **Adds** only the PayPal-specific state Oscar has no field for: `PayPalPayment`
  (the hold/capture ids and statuses, and the fee/net PayPal reports) and
  `PayPalRefund` (per-refund idempotency + amount).

All PayPal traffic goes through the **paypal** SDK (`paypal.PaypalClient`), wrapped in
`services/gateway.py`. No PayPal detail is hard-coded from memory.

## Configuration (env → Django settings, read in `sandbox/settings.py`)

`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENVIRONMENT`, `PAYPAL_CURRENCY`,
and optional `PAYPAL_BASE_URL` (used verbatim as the API base — including the OAuth
token request — when set; otherwise the SDK targets the PayPal sandbox). Secret
*values* are never written into the repository.

## Endpoints (all under `/api/`, session-authenticated)

| Method & path | Who | Purpose |
| --- | --- | --- |
| `POST /api/orders` | shopper | Place an order from catalogue items → `orderId`. Starts `AWAITING_PAYMENT`. |
| `POST /api/orders/{id}/pay` | shopper | **Authorize** (hold) the total. Body: `{"card": {...}}` or `{"paymentMethodId": N}`. |
| `POST /api/orders/{id}/fulfil` | operator | **Capture** (take the money). Renews a stale hold automatically. Reports fee/net. |
| `POST /api/orders/{id}/cancel` | operator | **Void** the hold before fulfilment (no money moved). |
| `POST /api/orders/{id}/refunds` | shopper | Refund a captured payment (full/partial) → `refundId`. Body: `{"amount"?, "idempotencyKey"}`. |
| `GET /api/my-orders` | shopper | The caller's orders with payment state. |
| `GET /api/reconciliation?from&to` | operator | PayPal's transactions for a range, lined up against app orders. Chunks the range (PayPal's 31-day limit) and covers every page. |
| `POST /api/payment-methods` | shopper | Save a card → `paymentMethodId`. Body: `{"card": {...}}`. |
| `GET /api/payment-methods` | shopper | The caller's saved cards (safe display only). |
| `DELETE /api/payment-methods/{id}` | shopper | Remove a saved card (also deletes the PayPal vault token). |

Payment operations are idempotent: a double-click never authorizes or captures twice
(PayPal-Request-Id derived from the stable payment id); refunds are keyed by a
caller-supplied `idempotencyKey`. A partly-refunded order can never be refunded beyond
what was captured.

## Verify it works

With the sandbox built and the PayPal env vars set:

```
sandbox/manage.py paypal_selfcheck
```

drives the whole thing against the **real PayPal sandbox** through the HTTP endpoints
(place → authorize with the test card → fulfil/capture with fee/net → partial refund
→ save a card → reuse it → cancel a third order → reconciliation → delete card) and
prints PASS/FAIL per step.

Unit tests (offline; the SDK transport is faked):

```
sandbox/manage.py test apps.paypal_checkout
```
