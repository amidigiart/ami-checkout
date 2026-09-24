"""
ami* Checkout API — Railway Backend

Endpoints:
  POST /api/checkout  — creates a Stripe Checkout Session (Supabase login required for app=amistampai)
  POST /api/webhook   — Stripe webhook; keeps public.subscriptions in Supabase in sync (signature required)
  POST /api/portal    — Stripe Customer Portal for the signed-in user
  GET  /api/status    — subscription status of the signed-in user
  GET  /health

Environment (Railway):
  STRIPE_SECRET_KEY            sk_test_… / sk_live_…
  STRIPE_WEBHOOK_SECRET        whsec_… (required: unsigned webhooks are rejected)
  SUPABASE_URL                 https://<project>.supabase.co
  SUPABASE_SERVICE_ROLE_KEY    service role key (server only, never in the browser)
  ALLOWED_ORIGINS              comma-separated site origins
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse

try:
    import stripe
except ImportError:
    raise SystemExit("pip install stripe")

StripeError = getattr(stripe, "StripeError", None) or getattr(stripe.error, "StripeError")
SignatureVerificationError = getattr(stripe, "SignatureVerificationError", None) or getattr(stripe.error, "SignatureVerificationError")

VERSION = "2026-09-24.2"
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "ALLOWED_ORIGINS",
    "https://amiluxai.com,https://amiqiai.com,https://amipecetai.com,"
    "https://amibellai.com,https://amiwealthai.com,https://amitherai.com,"
    "https://amirentai.com,https://amiapiai.com,https://amiecosystems.com,"
    "https://amistampai.com"
).split(",") if o.strip()]
PORT = int(os.environ.get("PORT", "8080"))
ACCOUNT_APPS = {"amistampai"}          # apps whose plans are stored in Supabase
ACTIVE_STATUSES = {"active", "trialing"}


# ── helpers ───────────────────────────────────

def supabase(method: str, path: str, body=None, token: str | None = None, prefer: str | None = None):
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Supabase is not configured")
    req = urllib.request.Request(SUPABASE_URL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("apikey", SUPABASE_SERVICE_ROLE_KEY)
    req.add_header("Authorization", "Bearer " + (token or SUPABASE_SERVICE_ROLE_KEY))
    req.add_header("Content-Type", "application/json")
    if prefer:
        req.add_header("Prefer", prefer)
    with urllib.request.urlopen(req, timeout=15) as r:
        data = r.read()
        return json.loads(data) if data else None


def same_origin_allowed(url: str | None) -> bool:
    if not url:
        return False
    u = urlparse(url)
    return u.scheme == "https" and f"{u.scheme}://{u.netloc}" in ALLOWED_ORIGINS


def iso(ts) -> str | None:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat() if ts else None


def period_end(sub: dict):
    # Newer Stripe API versions moved current_period_end from the subscription to its items.
    if sub.get("current_period_end"):
        return sub["current_period_end"]
    items = (sub.get("items") or {}).get("data") or []
    ends = [i.get("current_period_end") for i in items if i.get("current_period_end")]
    return max(ends) if ends else None


def known_customer(customer_id: str | None) -> str | None:
    # A customer saved in test mode does not exist in live mode (and vice versa); treat it as absent.
    if not customer_id:
        return None
    try:
        c = stripe.Customer.retrieve(customer_id)
        return None if getattr(c, "deleted", False) else customer_id
    except StripeError:
        return None


def upsert_subscription(user_id: str, sub: dict, deleted: bool = False) -> None:
    status = "canceled" if deleted else (sub.get("status") or "inactive")
    row = {
        "user_id": user_id,
        "stripe_customer_id": sub.get("customer"),
        "stripe_subscription_id": sub.get("id"),
        "plan": "pro" if status in ACTIVE_STATUSES else "free",
        "status": status,
        "current_period_end": iso(period_end(sub)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    supabase("POST", "/rest/v1/subscriptions?on_conflict=user_id", [row],
             prefer="resolution=merge-duplicates,return=minimal")
    print(f"SUBSCRIPTION {row['status']}: user={user_id} sub={row['stripe_subscription_id']} plan={row['plan']}")


# ── HTTP handler ──────────────────────────────

class CheckoutHandler(BaseHTTPRequestHandler):

    def _cors(self):
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _user(self):
        """Supabase user from 'Authorization: Bearer <access_token>', validated by Supabase itself."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not SUPABASE_URL:
            return None
        try:
            user = supabase("GET", "/auth/v1/user", token=auth[7:].strip())
            return user if user and user.get("id") else None
        except Exception:
            return None

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        if path == "/api/checkout":
            self._handle_checkout(raw)
        elif path == "/api/webhook":
            self._handle_webhook(raw)
        elif path == "/api/portal":
            self._handle_portal(raw)
        else:
            self._json(404, {"error": "not found"})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._json(200, {"status": "ok", "service": "ami-checkout", "version": VERSION,
                             "webhook_signing": bool(WEBHOOK_SECRET), "accounts": bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)})
        elif path == "/api/status":
            self._handle_status()
        else:
            self._json(404, {"error": "not found"})

    # ── Checkout Session ──────────────────────

    def _handle_checkout(self, raw):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return self._json(400, {"error": "invalid json"})

        lookup_key = data.get("lookup_key")
        success_url = data.get("success_url")
        cancel_url = data.get("cancel_url") or success_url
        app = data.get("app") or ""
        mode = data.get("mode", "subscription")
        if mode not in ("subscription", "payment"):
            return self._json(400, {"error": "invalid mode"})
        if not lookup_key or not success_url:
            return self._json(400, {"error": "missing lookup_key or success_url"})
        if not same_origin_allowed(success_url) or not same_origin_allowed(cancel_url):
            return self._json(400, {"error": "redirect url not allowed"})
        try:
            quantity = max(1, min(int(data.get("quantity", 1)), 100))
        except (TypeError, ValueError):
            return self._json(400, {"error": "invalid quantity"})

        user = self._user()
        if app in ACCOUNT_APPS and not user:
            return self._json(401, {"error": "sign in required"})

        try:
            prices = stripe.Price.list(lookup_keys=[lookup_key], limit=1)
            if not prices.data:
                return self._json(404, {"error": f"price not found: {lookup_key}"})

            metadata = {"lookup_key": lookup_key, "app": app}
            params = dict(
                mode=mode,
                line_items=[{"price": prices.data[0].id, "quantity": quantity}],
                success_url=success_url,
                cancel_url=cancel_url,
                allow_promotion_codes=True,
                billing_address_collection="auto",
                tax_id_collection={"enabled": True},
                metadata=metadata,
            )
            if user:
                metadata["user_id"] = user["id"]
                params["client_reference_id"] = user["id"]
                existing = None
                try:
                    rows = supabase("GET", f"/rest/v1/subscriptions?user_id=eq.{quote(user['id'])}&select=stripe_customer_id")
                    existing = rows[0]["stripe_customer_id"] if rows and rows[0].get("stripe_customer_id") else None
                except Exception:
                    pass
                existing = known_customer(existing)
                if existing:
                    params["customer"] = existing
                elif user.get("email"):
                    params["customer_email"] = user["email"]
                if mode == "subscription":
                    params["subscription_data"] = {"metadata": {"user_id": user["id"], "app": app}}

            session = stripe.checkout.Session.create(**params)
            self._json(200, {"url": session.url, "sessionId": session.id})
        except StripeError as e:
            self._json(500, {"error": str(e)})

    # ── Webhook ───────────────────────────────

    def _handle_webhook(self, raw):
        if not WEBHOOK_SECRET:
            return self._json(503, {"error": "webhook signing secret not configured"})
        try:
            stripe.Webhook.construct_event(raw, self.headers.get("Stripe-Signature", ""), WEBHOOK_SECRET)
        except (SignatureVerificationError, ValueError):
            return self._json(400, {"error": "invalid signature"})

        event = json.loads(raw)            # signature verified above; work with plain dicts
        event_type = event.get("type", "")
        obj = (event.get("data") or {}).get("object") or {}

        try:
            if event_type in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
                md = obj.get("metadata") or {}
                if md.get("app") in ACCOUNT_APPS and md.get("user_id"):
                    upsert_subscription(md["user_id"], obj, deleted=event_type.endswith(".deleted"))
            elif event_type == "checkout.session.completed":
                md = obj.get("metadata") or {}
                print(f"CHECKOUT COMPLETED: app={md.get('app')} user={obj.get('client_reference_id')} mode={obj.get('mode')}")
            elif event_type == "invoice.payment_failed":
                print(f"PAYMENT FAILED: customer={obj.get('customer')}")
        except Exception as e:           # let Stripe retry on storage errors
            print(f"WEBHOOK ERROR {event_type}: {e}")
            return self._json(500, {"error": "processing failed"})

        self._json(200, {"received": True})

    # ── Customer Portal (own account only) ────

    def _handle_portal(self, raw):
        user = self._user()
        if not user:
            return self._json(401, {"error": "sign in required"})
        try:
            data = json.loads(raw or b"{}")
        except (json.JSONDecodeError, ValueError):
            return self._json(400, {"error": "invalid json"})
        return_url = data.get("return_url")
        if not same_origin_allowed(return_url):
            return self._json(400, {"error": "return url not allowed"})
        try:
            rows = supabase("GET", f"/rest/v1/subscriptions?user_id=eq.{quote(user['id'])}&select=stripe_customer_id")
            customer = known_customer(rows[0]["stripe_customer_id"] if rows else None)
            if not customer:
                return self._json(404, {"error": "no subscription"})
            session = stripe.billing_portal.Session.create(customer=customer, return_url=return_url)
            self._json(200, {"url": session.url})
        except StripeError as e:
            self._json(500, {"error": str(e)})
        except Exception:
            self._json(500, {"error": "lookup failed"})

    # ── Subscription status (own account only) ─

    def _handle_status(self):
        user = self._user()
        if not user:
            return self._json(401, {"error": "sign in required"})
        try:
            rows = supabase("GET", f"/rest/v1/subscriptions?user_id=eq.{quote(user['id'])}&select=plan,status,current_period_end")
            self._json(200, rows[0] if rows else {"plan": "free", "status": "inactive", "current_period_end": None})
        except Exception:
            self._json(500, {"error": "lookup failed"})


if __name__ == "__main__":
    if not stripe.api_key:
        print("WARNING: STRIPE_SECRET_KEY not set")
    if not WEBHOOK_SECRET:
        print("WARNING: STRIPE_WEBHOOK_SECRET not set — webhooks will be rejected")
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        print("WARNING: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set — plans will not be stored")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), CheckoutHandler)
    print(f"ami* Checkout API {VERSION} running on :{PORT}")
    server.serve_forever()
