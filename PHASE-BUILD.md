> **Approved base task prompt** — approved by the owner (mohammad.ali@apimatic.io)
> on 2026-09-22 for the Python/TypeScript skill-defect before/after experiment
> (codegen-v2 #424–#433). Three bootstrap corrections in the host-notes section were
> established by executing them against the real hosts, not read from documentation.

# Task — Add PayPal payments and saved cards to the django-oscar sandbox site

Make the django-oscar sandbox site actually collect money, with **PayPal** as the payment processor, and
let a shopper **save a card** so a later order can be paid without re-entering it. The app
today ends checkout by writing an order record — no payment is ever taken, and the order
carries no payment or fulfilment state. This adds the money movement and the operator flows
that follow a real payment: hold the money at checkout, take it at fulfilment, give it back on
a return. It is an **additive** capability — it does not replace the existing
catalogue/basket/order flow.

You own the design and every implementation decision — architecture, file layout, build
order, patterns. Just honor the mandates and the details below.

---

## What to build

### Flow 1 — Pay for an order

A logged-in shopper places an order and pays for it by card; an operator then fulfils,
cancels or refunds it.

- `POST /api/orders` — place an order from catalogue items; the request carries catalogue item
  ids and quantities, and reuses the app's existing order/order-line model rather than a
  parallel one (the caller's identity comes from the authenticated session). The order starts
  in a state awaiting payment.
- `POST /api/orders/{orderId}/pay` — **authorize** the order total: put a hold on the money,
  do **not** take it yet. The request either carries card details for a one-off payment,
  **or** names one of the shopper's saved cards (Flow 2) to pay with instead. The amount
  PayPal holds must equal the order total to the cent.
- `POST /api/orders/{orderId}/fulfil` — an operator marks the order fulfilled, and *that* is
  when the money is actually taken. Afterwards the payment must show what PayPal reported:
  the captured amount, PayPal's fee, and the net proceeds to the merchant. An authorization
  that has gone stale before fulfilment has to be renewed rather than failing the fulfilment
  outright — and one that can no longer be renewed must say so in terms an operator can act
  on.
- `POST /api/orders/{orderId}/cancel` — cancel *before* fulfilment: the shopper's held funds
  are released, so no money ever moved.
- `POST /api/orders/{orderId}/refunds` — return *after* fulfilment: refund the captured
  payment, in full or in part. A partly-refunded order must never become refundable beyond
  what was captured.
- `GET /api/my-orders` — the caller's orders with their payment state.
- `GET /api/reconciliation?from={from}&to={to}` — a report listing PayPal's own record of
  transactions for a date range and lining them up against this app's orders, so a payment
  PayPal knows about and the app doesn't — or the reverse — is visible. It covers the whole
  range, not just the first page of it. `from` and `to` are ISO-8601 date-times.

Amounts come from catalogue prices; the currency comes from configuration (below). Payment
operations must be idempotent in effect: a double-click never authorizes or captures the
shopper twice. Refunds carry a caller-supplied idempotency key — repeating a request under the
same key must not refund twice, while two distinct partial refunds of the same capture remain
legitimate.

The payment has to carry enough of the state PayPal owns (ids and current status for the
hold, the capture, the refunds) that a later request can act on it, not only the one that
started it.

### Flow 2 — Saved cards

A logged-in shopper saves a card once and reuses it for later orders.

- `POST /api/payment-methods` — save a card for the signed-in shopper. The response
  identifies the saved card and describes it safely enough for the shopper to recognise which
  card it is — never full card details.
- `GET /api/payment-methods` — the caller's saved cards.
- `DELETE /api/payment-methods/{paymentMethodId}` — remove a saved card. Afterwards it must
  no longer appear among the caller's saved cards, and must no longer be usable to pay.

A saved card belongs to the shopper who saved it: one shopper must never see, use, or delete
another's. The same goes for orders — one shopper must never see or act on another's. Full
card details are never stored in the application's own database and never written to logs.

### Where it goes

Expose all capabilities as HTTP endpoints on the **runnable sandbox project at `sandbox/`** —
the Django site this repository ships as its reference storefront — as a new Django app under
`sandbox/apps/`, wired into `sandbox/urls.py` and routed under `/api/` as named above,
following that project's existing app and URL conventions. Reuse Oscar's own models from
`src/oscar/apps/` rather than building a parallel set. Every flow above has to be drivable
through that API alone, and each action a caller can take stays separately invocable — not one
do-everything call behind a single route. No storefront UI is required.

Authenticate callers the way the sandbox already authenticates them — Django's own session
login — and take the caller's identity from the authenticated request user.

Fulfil, cancel and reconciliation are **operator** actions: restrict them to the staff users the sandbox already treats as privileged
(`is_staff`). Every other endpoint is shopper-scoped and acts only on the caller's own data.

### Response identifiers

So the flows can be driven end to end by a caller, a response that creates something returns
its identifier as a top-level field of the response body: `orderId` from `POST /api/orders`,
`paymentMethodId` from `POST /api/payment-methods`, and `refundId` from
`POST /api/orders/{orderId}/refunds`. Everything else about the response shape is your call.

---

## PayPal tooling — non-negotiable

- Use the **paypal** plugin (from the **context-plugins**
  marketplace) for **every** PayPal interaction. It is your sole reference for how to
  talk to PayPal.
- **Do not** web-search or rely on general/external knowledge for PayPal API details.
- If the plugin does not expose a capability you need, **STOP and report the gap** — do not
  invent or work around it.

---

## Sandbox entities & test fixtures

Nothing is pre-seeded on the PayPal side — orders, payments and saved cards are all created
dynamically. A sandbox **business** account is the merchant, and the app's REST credentials
(client id / secret) belong to it.

That account is enabled for direct card processing and for vaulting cards, so the whole task
is drivable without a browser. Verify the payment, saved-card, fulfilment, cancel and refund
flows with a **direct card payment** using PayPal's sandbox test card: Visa
`4111 1111 1111 1111`, any future expiry date, any CVC, any name and billing address. No card
number is ever kept by this app.

PayPal's transaction reporting lags live activity, so a reconciliation range covering payments
you have just created may legitimately come back empty. That is an expected sandbox result,
not a missing capability — build the report so it is correct over a range that does have data,
and do not report the empty range as a gap.

If PayPal answers a card payment with a challenge that requires a shopper to approve in a
browser, **STOP and report it** — do not build an approval round-trip instead.

---

## Credentials

- Sandbox credentials arrive as env vars: `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`,
  `PAYPAL_ENVIRONMENT`, `PAYPAL_CURRENCY`.
- Target the PayPal **sandbox** for all development and testing.
- **Read every one of them through Django settings in `sandbox/settings.py`**, using exactly
  the setting names below, and hard-code none of their values — the same build has to run
  against a different PayPal account than the one above:
  `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`,
  `PAYPAL_ENVIRONMENT`, `PAYPAL_CURRENCY` and `PAYPAL_BASE_URL`.
- `PAYPAL_BASE_URL` is an optional override: when it is set, use it verbatim as the API base
  address for **every** PayPal call — including the credential/token request — instead of
  deriving one from the environment.

---

## Environment gotchas (this machine)

- **Python 3.11.** Only Python 3.11 and Python 3.14 are installed; several of this project's
  pinned dependencies have no 3.14 wheels, so use `py -3.11`. Create a virtual environment and
  install the project into it editable with its test extras — `py -3.11 -m venv venv` then
  `venv\Scripts\pip install -e .[test]`. The sandbox's own settings import `django-environ`
  and `whitenoise`, both of which come from those extras. Nothing here is installed globally
  for you.
- **SQLite, and it starts empty.** `sandbox/settings.py` defaults `DATABASE_ENGINE` to SQLite
  beside the project, so no database service is needed. A fresh clone has no database and no
  content. `make` is **not installed**, so run the `Makefile`'s `build_sandbox` steps yourself, in
  this order — this exact sequence has been verified to produce a working catalogue:

  ```
  sandbox/manage.py migrate
  sandbox/manage.py loaddata sandbox/fixtures/auth.json
  sandbox/manage.py loaddata sandbox/fixtures/child_products.json
  sandbox/manage.py oscar_populate_countries --initial-only
  sandbox/manage.py loaddata sandbox/fixtures/pages.json sandbox/fixtures/ranges.json sandbox/fixtures/offers.json
  sandbox/manage.py loaddata sandbox/fixtures/orders.json
  ```

  Expect roughly 209 products, 249 countries and 2 users afterwards. Two things worth knowing so
  you do not chase them: `oscar_import_catalogue sandbox/fixtures/*.csv` reports
  `New items: 0, updated items: 0` and skips rows for an invalid field count — that is normal and
  the catalogue comes from `child_products.json`, so the CSV step is optional. And
  `oscar_populate_countries` is **not** optional: a shipping address needs a `Country` row, so
  checkout fails without it. The seeded `superuser` / `staff` accounts come from `auth.json`.
- **The API needs no frontend build.** `npm install` / `npm run build` and the static asset
  pipeline are not needed to exercise the HTTP API. Do not run them. If `collectstatic` or a
  manifest-storage error blocks a page you do not need, skip that page rather than building
  assets.
- **Settings module.** `sandbox/manage.py` defaults `DJANGO_SETTINGS_MODULE` to `settings`
  resolved from the `sandbox/` directory — run management commands from there, or with
  `sandbox/` on `PYTHONPATH`. `DEBUG` is on by default.
- **Ports:** when you run the server, bind only to your assigned block
  (`APP_PORT_BLOCK_BASE` … `+APP_PORT_BLOCK_SIZE-1`). Stop your previous instance before
  starting another — no stray processes on stale builds.

There is otherwise no infra dependency beyond Python — no Docker, no PostgreSQL, no Redis, no
broker, no task queue. Don't introduce any.

---

## Rules of engagement

- We want a **production-grade** integration — you decide what production-grade looks like.
- When done, **self-verify** that the sandbox site runs and the flows actually work — a real authorization
  on the sandbox card, a real capture at fulfilment, a real refund, and a saved card reused to
  pay a second order. No browser step is required. Then give
  me a concise, step-by-step guide to verify the working integration myself.

---

## Constraints

- **Secrets never enter the repository.** Read the API credentials from the environment
  variables above, at run time, and never write their **values** into any file inside this
  repository — not into a settings module, not into `.env`, not into a config file, a script,
  a test fixture, a comment, or a commit message. Referencing the variable **names** is fine,
  the values are not.
- **Report a gap only when it is genuinely a gap.** Stop and report when the source you were
  given does not cover a capability this integration requires. A design decision being hard,
  open-ended, or left to your judgment is **not** a gap — decide it and proceed.
- **You are running headless — there is no one to answer you.** Work until the integration
  is fully complete. Never hand back, never end with a question, and never defer remaining
  work to the user: decide and proceed.

