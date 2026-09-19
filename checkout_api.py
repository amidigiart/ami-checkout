"""
ami* Checkout API — Railway Backend

Endpoints:
  POST /api/checkout     — creates Stripe Checkout Session
  POST /api/webhook      — handles Stripe webhook events
  POST /api/portal        — creates Customer Portal session
  GET  /api/status/:uid  — subscription status check

Deploy on Railway with:
  STRIPE_SECRET_KEY=sk_live_xxx
  STRIPE_WEBHOOK_SECRET=whsec_xxx
  ALLOWED_ORIGINS=https://amiluxai.com,https://amiqiai.com,...
"""

from __future__ import annotations

import os
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

try:
    import stripe
except ImportError:
    raise SystemExit("pip install stripe")

StripeError = getattr(stripe, "StripeError", None) or getattr(stripe.error, "StripeError")
SignatureVerificationError = getattr(stripe, "SignatureVerificationError", None) or getattr(stripe.error, "SignatureVerificationError")

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
stripe.api_version = "2025-06-30.basil"
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://amiluxai.com,https://amiqiai.com,https://amipecetai.com,"
    "https://amibellai.com,https://amiwealthai.com,https://amitherai.com,"
    "https://amirentai.com,https://amiapiai.com,https://amiecosystems.com,"
    "https://amistampai.com"
).split(",")
PORT = int(os.environ.get("PORT", "8080"))


class CheckoutHandler(BaseHTTPRequestHandler):

    def _cors(self):
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

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
            self._json(200, {"status": "ok", "service": "ami-checkout"})
        elif path.startswith("/api/status/"):
            uid = path.split("/")[-1]
            self._handle_status(uid)
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
        cancel_url = data.get("cancel_url")

        if not lookup_key or not success_url:
            return self._json(400, {"error": "missing lookup_key or success_url"})

        mode = data.get("mode", "subscription")
        quantity = data.get("quantity", 1)

        try:
            prices = stripe.Price.list(lookup_keys=[lookup_key], limit=1)
            if not prices.data:
                return self._json(404, {"error": f"price not found: {lookup_key}"})

            price = prices.data[0]

            session_params = dict(
                mode=mode,
                line_items=[{"price": price.id, "quantity": quantity}],
                success_url=success_url,
                cancel_url=cancel_url or success_url,
                allow_promotion_codes=True,
                billing_address_collection="auto",
                tax_id_collection={"enabled": True},
                metadata={"lookup_key": lookup_key},
            )

            session = stripe.checkout.Session.create(**session_params)

            self._json(200, {"url": session.url, "sessionId": session.id})

        except StripeError as e:
            self._json(500, {"error": str(e)})

    # ── Webhook ───────────────────────────────

    def _handle_webhook(self, raw):
        sig = self.headers.get("Stripe-Signature", "")

        if WEBHOOK_SECRET:
            try:
                event = stripe.Webhook.construct_event(raw, sig, WEBHOOK_SECRET)
            except (SignatureVerificationError, ValueError):
                return self._json(400, {"error": "invalid signature"})
        else:
            try:
                event = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return self._json(400, {"error": "invalid json"})

        event_type = event.get("type", "")

        if event_type == "checkout.session.completed":
            session = event["data"]["object"]
            customer_email = session.get("customer_details", {}).get("email", "unknown")
            lookup_key = session.get("metadata", {}).get("lookup_key", "unknown")
            print(f"NEW SUBSCRIBER: {customer_email} — {lookup_key}")

        elif event_type == "customer.subscription.deleted":
            sub = event["data"]["object"]
            print(f"CANCELLED: {sub.get('id')}")

        elif event_type == "invoice.payment_failed":
            invoice = event["data"]["object"]
            print(f"PAYMENT FAILED: {invoice.get('customer_email', 'unknown')}")

        self._json(200, {"received": True})

    # ── Customer Portal ───────────────────────

    def _handle_portal(self, raw):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return self._json(400, {"error": "invalid json"})

        customer_id = data.get("customer_id")
        return_url = data.get("return_url")

        if not customer_id or not return_url:
            return self._json(400, {"error": "missing customer_id or return_url"})

        try:
            session = stripe.billing_portal.Session.create(
                customer=customer_id,
                return_url=return_url,
            )
            self._json(200, {"url": session.url})
        except StripeError as e:
            self._json(500, {"error": str(e)})

    # ── Subscription Status ───────────────────

    def _handle_status(self, uid):
        try:
            subs = stripe.Subscription.list(
                customer=uid, status="active", limit=10
            )
            active = []
            for sub in subs.data:
                for item in sub["items"]["data"]:
                    active.append({
                        "product": item["price"]["product"],
                        "lookup_key": item["price"].get("lookup_key"),
                        "status": sub["status"],
                        "current_period_end": sub["current_period_end"],
                    })
            self._json(200, {"subscriptions": active})
        except StripeError as e:
            self._json(500, {"error": str(e)})


if __name__ == "__main__":
    if not stripe.api_key:
        print("WARNING: STRIPE_SECRET_KEY not set")
    server = HTTPServer(("0.0.0.0", PORT), CheckoutHandler)
    print(f"ami* Checkout API running on :{PORT}")
    server.serve_forever()
