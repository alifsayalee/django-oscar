# Maxio subscriptions app

Recurring-subscription billing for the sandbox, backed by **Maxio Advanced Billing** as the system
of record. Additive and parallel to Oscar's one-time cart/checkout flow — it does not replace it.

## Endpoints (session-authenticated, under `/api/`)

| Method & path | Purpose |
| --- | --- |
| `GET /api/subscription-plans` | List available plans; each entry carries a `planHandle`. |
| `POST /api/subscriptions` | Ensure a Maxio customer exists (idempotent) and subscribe. Returns `subscriptionId` as a top-level field. Body: `{"planHandle": "eshop-pro"}` (optional; defaults to `eshop-pro`). |
| `GET /api/my-subscriptions` | The caller's subscriptions. |

Callers are authenticated with Django's own session login, and identity is taken from
`request.user`. The state-changing `POST` is CSRF-protected (as befits a session-authenticated
endpoint); `GET /api/subscription-plans` sets the CSRF cookie so a caller can obtain the token.

## Design

- **`client.py`** — builds and holds one long-lived `MaxioAdvancedBillingClient` (sync; Django/WSGI),
  configured from settings. Closed at interpreter shutdown.
- **`services.py`** — the only module that talks to the SDK. Idempotency lives here: a stable
  per-user customer reference (`oscar-user-<pk>`) makes customer creation idempotent, and existing
  live subscriptions are reused before creating a new one. Every SDK failure is translated into a
  `BillingError` here.
- **`exceptions.py`** — `BillingError` hierarchy, each carrying the HTTP status the view returns.
- **`views.py` / `urls.py`** — thin JSON views; each capability has its own route.

No Django models: Maxio is the system of record, and Oscar's user model supplies caller identity.

## Configuration (settings, read from the environment at runtime)

`MAXIO_API_KEY`, `MAXIO_SITE_SUBDOMAIN`, `MAXIO_DEFAULT_PRODUCT_FAMILY`, and the optional
`MAXIO_BASE_URL` (used verbatim as the API base address when set; otherwise the base URL is derived
from the site subdomain). No credential value is stored in the repository.

## Tests

`python manage.py test apps.subscriptions` — unit tests that fake the SDK's transport seam and
assert our boundary behaviour (response shapes, idempotency, and the mapping of each provider
failure kind onto an HTTP status).
