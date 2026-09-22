> **Approved base task prompt** — approved by the owner (mohammad.ali@apimatic.io)
> on 2026-09-22 for the Python/TypeScript skill-defect before/after experiment
> (codegen-v2 #424–#433). Three bootstrap corrections in the host-notes section were
> established by executing them against the real hosts, not read from documentation.

# Task — Add Maxio subscription billing to the django-oscar sandbox site

Add recurring-subscription billing to the django-oscar sandbox site, with **Maxio Advanced Billing** as the
billing system of record. The app today is one-time commerce (catalogue → basket → order);
this is an **additive, parallel** capability — it does not replace the existing cart/checkout
flow.

You own the design and every implementation decision — architecture, file layout, build
order, patterns. Just honor the mandates and the details below.

---

## What to build

**The hero flow: Subscribe.**
A logged-in shopper browses available plans, subscribes to one, and sees it reflected in
their account. Ensure a Maxio customer exists for the app's user (idempotent, so a
double-click never creates two customers/subscriptions), enroll them, and confirm
plan/price/state/next-billing-date back to the user.

Route the endpoints under `/api/` named for the capability — `GET /api/subscription-plans`,
`POST /api/subscriptions`, `GET /api/my-subscriptions`.

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

### Response identifiers

So the flow can be driven end to end by a caller, `POST /api/subscriptions` returns
`subscriptionId` as a top-level field of the response body, and each entry
`GET /api/subscription-plans` returns carries its own `planHandle`. Everything else about the
response shape is your call.

---

## Maxio tooling — non-negotiable

- Use the **maxio** plugin (from the **context-plugins**
  marketplace) for **every** Maxio interaction. It is your sole reference for how to
  talk to Maxio.
- **Do not** web-search or rely on general/external knowledge for Maxio API details.
- If the plugin does not expose a capability you need, **STOP and report the gap** — do not
  invent or work around it.

---

## Sandbox entities (already seeded on site `cp-exp-1`)

The demo catalog already exists — no need to create it. **Handles are stable; numeric IDs
are not** — Maxio reassigns them on re-seed, so the IDs below may already be stale.

| Entity | Handle | ID (current) | Notes |
|--------|--------|--------------|-------|
| Product Family | `eshop-subscribe` | 3023074 | Container for the plans + component |
| Pro Plan | `eshop-pro` | 7126957 | $299.00/mo — default subscribe target |
| Basic Plan | `basic-plan` | 7126958 | $29.00/mo — alternate plan (also seeded) |
| Metered component | `api-call` | 3057195 | Metered, $0.01/unit — also seeded on the family |

Both plans: no trial, no setup fee, expires never, taxable no, **payment method not
required** (so subscribe works without card capture / 3-DS).

---

## Credentials

- Sandbox credentials arrive as env vars: `MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`,
  `MAXIO_ENVIRONMENT`, `MAXIO_DEFAULT_PRODUCT_FAMILY`.
- Target the Maxio **sandbox** for all development and testing.
- **Read every one of them through Django settings in `sandbox/settings.py`**, using exactly
  the setting names below, and hard-code none of their values — the same build has to run
  against a different Maxio account than the one above:
  `MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`,
  `MAXIO_DEFAULT_PRODUCT_FAMILY` and `MAXIO_BASE_URL`.
- `MAXIO_BASE_URL` is an optional override: when it is set, use it verbatim as the API base
  address instead of deriving one from the subdomain.

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
- When done, **self-verify** that the sandbox site runs and the flows actually work — a real subscription
  created on the sandbox site against a real seeded plan, and read back through your own
  endpoints. Then give
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

