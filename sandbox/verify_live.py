"""Live end-to-end verification against the real Twilio account.

Drives the HTTP API (view -> service -> gateway) in-process via Django's test
Client against the REAL dev database, so real SMS are sent. Kept to the minimum
needed to verify every required behaviour. Not a unit test; run explicitly.
"""
import os
import json
import time
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
os.environ.setdefault("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")
django.setup()

from django.test import Client
from django.contrib.auth import get_user_model
from oscar.core.loading import get_model

User = get_user_model()
Product = get_model("catalogue", "Product")

TEST_TO = os.environ["TWILIO_TEST_TO_NUMBER"]
UNREACHABLE = os.environ["TWILIO_UNREACHABLE_TO_NUMBER"]

# Pick two purchasable products.
from oscar.core.loading import get_class
strat = get_class("partner.strategy", "Selector")().strategy()
purchasable = [p.id for p in Product.objects.all() if strat.fetch_for_product(p).availability.is_available_to_buy]
assert purchasable, "no purchasable products loaded"
PID = purchasable[0]
print("using product id", PID)


def jprint(label, resp):
    try:
        body = resp.json()
    except Exception:
        body = resp.content[:300]
    print(f"\n### {label}  [HTTP {resp.status_code}]")
    print(json.dumps(body, indent=2, default=str)[:1600])
    return body if isinstance(body, dict) else {}


def post(c, url, payload=None):
    return c.post(url, data=json.dumps(payload or {}), content_type="application/json")


# Fresh users for a clean run.
shopper = User.objects.filter(username="verify_shopper").first()
if shopper:
    shopper.orders.all().delete()
    shopper.sms_contact_numbers.all().delete()
else:
    shopper = User.objects.create_user("verify_shopper", "verify_shopper@example.com", "password123")
staff = User.objects.filter(username="verify_staff").first() or User.objects.create_user(
    "verify_staff", "verify_staff@example.com", "password123", is_staff=True
)

shopper_c = Client()
shopper_c.force_login(shopper)
staff_c = Client()
staff_c.force_login(staff)

# 1. Register the reachable Canadian number (validated + canonicalized by Twilio).
r = jprint("POST /api/contact-numbers (reachable)", post(shopper_c, "/api/contact-numbers", {"number": TEST_TO}))
contact_id = r.get("contactNumberId")

# 2. List my numbers.
jprint("GET /api/contact-numbers", shopper_c.get("/api/contact-numbers"))

# 3. Place an order -> a real "order placed" SMS is sent to the reachable number.
r = jprint("POST /api/orders", post(shopper_c, "/api/orders", {"items": [{"product_id": PID, "quantity": 1}]}))
order_id = r.get("orderId")

# 4. Operator dispatches -> "on its way" SMS + follow-up survey queued a few days out.
r = jprint("POST /api/orders/{id}/dispatch (staff)", post(staff_c, f"/api/orders/{order_id}/dispatch"))

# Show notifications (with live provider status).
r = jprint("GET /api/orders/{id}/notifications", shopper_c.get(f"/api/orders/{order_id}/notifications"))
followup = next((n for n in r.get("notifications", []) if n["isFollowup"]), None)
print("\nfollow-up scheduled:", followup and followup["providerStatus"], "sid:", followup and followup["providerSid"])

# 5. Operator cancels -> the pending follow-up is called off BEFORE it can send.
jprint("POST /api/orders/{id}/cancel (staff)", post(staff_c, f"/api/orders/{order_id}/cancel"))
r = jprint("GET /api/orders/{id}/notifications (after cancel)", shopper_c.get(f"/api/orders/{order_id}/notifications"))
followup = next((n for n in r.get("notifications", []) if n["isFollowup"]), None)
print("\nfollow-up after cancel:", followup and followup["providerStatus"], "cancelled:", followup and followup["followupCancelled"])

# 6. Operator re-sends the dispatched notification (idempotency key).
dispatched = next((n for n in r.get("notifications", []) if n["kind"] == "dispatched"), None)
if dispatched:
    key = "verify-resend-%d" % int(time.time())
    r1 = jprint("POST /api/notifications/{id}/resend (key A)", post(staff_c, f"/api/notifications/{dispatched['notificationId']}/resend", {"idempotencyKey": key}))
    r2 = jprint("POST /api/notifications/{id}/resend (key A again -> no new send)", post(staff_c, f"/api/notifications/{dispatched['notificationId']}/resend", {"idempotencyKey": key}))
    print("\nresend idempotent:", r1.get("notificationId") == r2.get("notificationId"), "resent flag:", r2.get("resent"))

# 7. Content disposal of the placed message.
placed = next((n for n in r.get("notifications", []) if n["kind"] == "placed"), None)
if placed:
    jprint("DELETE /api/notifications/{id}/content (staff)", staff_c.delete(f"/api/notifications/{placed['notificationId']}/content"))

# 8. Message the UNREACHABLE US number to demonstrate the undeliverable outcome.
jprint("POST /api/contact-numbers (unreachable)", post(shopper_c, "/api/contact-numbers", {"number": UNREACHABLE}))
# (Most recent number is now the unreachable one -> next order messages it.)
r = jprint("POST /api/orders (to unreachable)", post(shopper_c, "/api/orders", {"items": [{"product_id": PID, "quantity": 1}]}))
unreach_order = r.get("orderId")
time.sleep(6)  # let Twilio attempt delivery
jprint("GET /api/orders/{id}/notifications (unreachable, after delay)", shopper_c.get(f"/api/orders/{unreach_order}/notifications"))

# 9. Reconciliation over today's range (has data from the sends above).
from datetime import datetime, timedelta, timezone as tz
now = datetime.now(tz.utc)
frm = (now - timedelta(days=1)).isoformat()
to = (now + timedelta(days=1)).isoformat()
jprint("GET /api/notifications/reconciliation", staff_c.get(f"/api/notifications/reconciliation?from={frm}&to={to}"))

print("\nDONE")
