"""Demo merchant site for walking through the Flexify card payment flow (H2H and HPP).

Run: python3 server.py   (configuration via environment or .env, see .env.example)
"""
import base64
import json
import mimetypes
import os
import random
import re
import string
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from flexify import FlexifyClient, verify_callback
from store import Store

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")


def load_dotenv(path):
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(os.path.join(ROOT, ".env"))

PORT = int(os.environ.get("PORT", "8080"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:%d" % PORT).rstrip("/")
MOCK = os.environ.get("MOCK_GATEWAY", "0") == "1"
API_KEY = os.environ.get("FLEXIFY_API_KEY", "mock-key" if MOCK else "")
API_SECRET = os.environ.get("FLEXIFY_API_SECRET", "mock-secret" if MOCK else "")
H2H_HOST = os.environ.get("FLEXIFY_H2H_HOST", "h2h.flexify.finance")
HPP_HOST = os.environ.get("FLEXIFY_HPP_HOST", "hpp.flexify.finance")
SITE_USER = os.environ.get("SITE_USER", "")
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "")
# strict: reject callbacks with a bad signature (401); log: record the failure but still apply the callback
CALLBACK_SIGNATURE_MODE = os.environ.get("CALLBACK_SIGNATURE_MODE", "strict")
DB_PATH = os.environ.get("DB_PATH", os.path.join(ROOT, "data.sqlite3"))

store = Store(DB_PATH)
if MOCK:
    import mock_gateway

    mock_gateway.configure(API_KEY, API_SECRET, PUBLIC_BASE_URL)
    local = "http://127.0.0.1:%d" % PORT
    client = FlexifyClient(API_KEY, API_SECRET, H2H_HOST, HPP_HOST, local + "/mock/h2h", local + "/mock/hpp")
else:
    client = FlexifyClient(API_KEY, API_SECRET, H2H_HOST, HPP_HOST)

FINAL_STATUSES = {"success", "declined", "failed", "expired", "error"}
PUBLIC_PATHS = ("/callback", "/health", "/mock/")
CUSTOMER_FIELDS = (
    "identifier", "email", "phone", "first_name", "last_name", "middle_name", "country", "state_code",
    "city", "address", "zip_code", "itn", "birthday", "gender", "identity",
)
BROWSER_FIELDS = (
    "header_accept", "language", "screen_color_depth", "screen_height", "screen_pixel_depth", "screen_width",
    "timezone_offset_minutes", "user_agent", "window_height", "window_width",
)
SECRET_KEYS = {"cvv", "card_pin", "otp"}


class ApiError(Exception):
    def __init__(self, message, status=400):
        Exception.__init__(self, message)
        self.status = status


def new_reference():
    suffix = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(5))
    return "ord-%s-%s" % (time.strftime("%y%m%d%H%M%S"), suffix)


def mask(value):
    """Deep-copy a payload with card data masked, for logs and the UI."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in SECRET_KEYS and v:
                out[k] = "***"
            elif k == "pan" and isinstance(v, str) and len(v) > 10:
                out[k] = v[:6] + "*" * (len(v) - 10) + v[-4:]
            else:
                out[k] = mask(v)
        return out
    if isinstance(value, list):
        return [mask(v) for v in value]
    return value


def parse_amount(raw):
    try:
        amount = round(float(str(raw).replace(",", ".")), 2)
    except (TypeError, ValueError):
        raise ApiError("Invalid amount")
    if amount <= 0:
        raise ApiError("Amount must be greater than 0")
    return amount


def clean_customer(raw, client_ip):
    raw = raw or {}
    customer = {k: str(raw[k]).strip() for k in CUSTOMER_FIELDS if raw.get(k) not in (None, "")}
    customer.setdefault("identifier", customer.get("email") or "demo-customer")
    customer["ip"] = client_ip
    return customer


def call_gateway(reference, title, fn, payload):
    store.add_event(reference, "request", title, mask(payload))
    result = fn(payload)
    store.add_event(reference, "response", title, mask(result))
    return result


def apply_payment(reference, data):
    """Apply an H2H payment response, check response or H2H callback to the order."""
    if not isinstance(data, dict):
        return
    fields = {}
    if data.get("status"):
        fields["status"] = data["status"]
    if "error_code" in data:
        fields["error_code"] = data.get("error_code")
        fields["error_reason"] = data.get("error_reason") or ""
    if data.get("identifier"):
        fields["identifier"] = data["identifier"]
        fields["payment_identifier"] = data["identifier"]
    fields["challenge_json"] = json.dumps(data["challenge"]) if data.get("challenge") else None
    card = data.get("card")
    if card:
        fields["card_json"] = json.dumps(mask({k: v for k, v in card.items() if k != "token"}))
        if card.get("token"):
            fields["card_token"] = card["token"]
    store.update_order(reference, **fields)


def apply_intention(reference, data):
    """Apply an HPP payment-intention check response or HPP callback to the order."""
    if not isinstance(data, dict):
        return
    fields = {}
    if data.get("status"):
        fields["status"] = data["status"]
    if "error_code" in data:
        fields["error_code"] = data.get("error_code")
        fields["error_reason"] = data.get("error_reason") or ""
    if data.get("identifier"):
        fields["identifier"] = data["identifier"]
    if data.get("redirect_url"):
        fields["checkout_url"] = data["redirect_url"]
    payment = data.get("payment") or {}
    if payment.get("identifier"):
        fields["payment_identifier"] = payment["identifier"]
    card = payment.get("card")
    if card:
        fields["card_json"] = json.dumps(mask({k: v for k, v in card.items() if k != "token"}))
        if card.get("token"):
            fields["card_token"] = card["token"]
    store.update_order(reference, **fields)


def apply_refund(order_reference, data):
    if not isinstance(data, dict) or not data.get("identifier"):
        return
    store.upsert_refund(
        data["identifier"],
        order_reference,
        reference=data.get("reference"),
        amount=data.get("amount"),
        currency=data.get("currency"),
        status=data.get("status"),
        error_code=data.get("error_code"),
        error_reason=data.get("error_reason"),
    )


def next_step(reference):
    order = store.get_order(reference)
    challenge = json.loads(order["challenge_json"]) if order and order["challenge_json"] else None
    if order and order["status"] == "pending" and challenge and challenge.get("challenge_type") == "redirect":
        return {"reference": reference, "next": "redirect", "url": challenge["challenge_redirect"]["url"]}
    return {"reference": reference, "next": "order", "url": "/order/" + reference}


def browser_info(raw, client_ip):
    raw = raw or {}
    info = {"ip": client_ip}
    for key in BROWSER_FIELDS:
        if raw.get(key) not in (None, ""):
            info[key] = raw[key]
    return info


# ---- API actions -------------------------------------------------------------------------------

def h2h_pay(body, client_ip, headers):
    amount = parse_amount(body.get("amount"))
    currency = (body.get("currency") or "USD").upper()
    card_in = body.get("card") or {}
    pan = re.sub(r"\D", "", str(card_in.get("pan", "")))
    if not 12 <= len(pan) <= 19:
        raise ApiError("Invalid card number")
    card = {
        "pan": pan,
        "holder_name": str(card_in.get("holder_name", "")).strip(),
        "cvv": str(card_in.get("cvv", "")).strip(),
        "exp_month": str(card_in.get("exp_month", "")).strip().zfill(2),
        "exp_year": str(card_in.get("exp_year", "")).strip(),
    }
    if len(card["exp_year"]) == 2:
        card["exp_year"] = "20" + card["exp_year"]
    reference = new_reference()
    customer = clean_customer(body.get("customer"), client_ip)
    browser = browser_info(body.get("browser"), client_ip)
    browser.setdefault("user_agent", headers.get("user-agent", ""))
    browser.setdefault("header_accept", headers.get("accept", ""))
    payload = {
        "method": "card",
        "mode": "initial",
        "reference": reference,
        "currency": currency,
        "amount": amount,
        "description": body.get("product") or "Demo order",
        "card": card,
        "customer": customer,
        "redirect_url": "%s/order/%s?return=1" % (PUBLIC_BASE_URL, reference),
        "callback_url": PUBLIC_BASE_URL + "/callback",
        "extra": {"browser": browser},
    }
    store.create_order(
        reference, integration="h2h", mode="initial", product=payload["description"], amount="%.2f" % amount,
        currency=currency, customer_json=json.dumps(customer), status="created",
    )
    return run_h2h_payment(reference, payload)


def run_h2h_payment(reference, payload):
    result = call_gateway(reference, "H2H POST /v1/payment", client.h2h_payment, payload)
    data = result.get("data")
    if result.get("http_status") == 200 and isinstance(data, dict):
        apply_payment(reference, data)
    elif result.get("http_status") in (0, 504):
        # timeout: final status will come via callback or check
        store.update_order(reference, status="pending", error_reason="Gateway timeout, waiting for callback/check")
    else:
        data = data if isinstance(data, dict) else {}
        store.update_order(
            reference, status="error", error_code=data.get("error_code"),
            error_reason=data.get("error_reason") or result.get("error") or "HTTP %s" % result.get("http_status"),
        )
    return next_step(reference)


def h2h_repeat(reference, body, client_ip):
    parent = get_order_or_404(reference)
    if not parent["card_token"]:
        raise ApiError("No card token stored for this order")
    amount = parse_amount(body.get("amount") or parent["amount"])
    new_ref = new_reference()
    customer = json.loads(parent["customer_json"] or "{}")
    customer["ip"] = client_ip
    card = {"token": parent["card_token"]}
    if body.get("cvv"):
        card["cvv"] = str(body["cvv"]).strip()
    payload = {
        "method": "card",
        "mode": "token",
        "reference": new_ref,
        "currency": parent["currency"],
        "amount": amount,
        "description": "Repeat of %s" % reference,
        "card": card,
        "customer": customer,
        "redirect_url": "%s/order/%s?return=1" % (PUBLIC_BASE_URL, new_ref),
        "callback_url": PUBLIC_BASE_URL + "/callback",
        "extra": {"browser": browser_info(body.get("browser"), client_ip)},
    }
    store.create_order(
        new_ref, integration="h2h", mode="token", product=payload["description"], amount="%.2f" % amount,
        currency=parent["currency"], customer_json=json.dumps(customer), status="created", parent_reference=reference,
    )
    return run_h2h_payment(new_ref, payload)


def hpp_create(body, client_ip):
    amount = parse_amount(body.get("amount"))
    currency = (body.get("currency") or "USD").upper()
    reference = new_reference()
    customer = clean_customer(body.get("customer"), client_ip)
    widget = {"method": "card"}
    if body.get("locale"):
        widget["locale"] = body["locale"]
    payload = {
        "reference": reference,
        "currency": currency,
        "amount": "%.2f" % amount,
        "description": body.get("product") or "Demo order",
        "customer": customer,
        "widget": widget,
        "redirect_url": "%s/order/%s?return=1" % (PUBLIC_BASE_URL, reference),
        "callback_url": PUBLIC_BASE_URL + "/callback",
        "cancel_url": "%s/order/%s?cancelled=1" % (PUBLIC_BASE_URL, reference),
    }
    store.create_order(
        reference, integration="hpp", product=payload["description"], amount=payload["amount"], currency=currency,
        customer_json=json.dumps(customer), status="created",
    )
    result = call_gateway(reference, "HPP POST /v1/payment-intention", client.hpp_payment_intention, payload)
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if result.get("http_status") == 200 and data.get("error_code") == 0 and data.get("status") == "new":
        apply_intention(reference, data)
        return {"reference": reference, "next": "redirect", "url": data["redirect_url"]}
    store.update_order(
        reference, status="error", error_code=data.get("error_code"),
        error_reason=data.get("error_reason") or result.get("error") or "HTTP %s" % result.get("http_status"),
    )
    return {"reference": reference, "next": "order", "url": "/order/" + reference}


def check_order(reference):
    order = get_order_or_404(reference)
    if order["integration"] == "hpp":
        result = call_gateway(reference, "HPP POST /v1/payment-intention/check", lambda p: client.hpp_check(reference), {"reference": reference})
        if result.get("http_status") == 200:
            apply_intention(reference, result.get("data"))
    else:
        result = call_gateway(reference, "H2H POST /v1/payment/check", lambda p: client.h2h_check(reference), {"reference": reference})
        data = result.get("data")
        # error_code 100 (queued) / 101 (not found) carry no transaction state
        if result.get("http_status") == 200 and isinstance(data, dict) and data.get("status"):
            apply_payment(reference, data)
    for refund in store.list_refunds(reference):
        if refund["status"] not in FINAL_STATUSES:
            refund_check(refund["identifier"])
    return order_view(reference)


def submit_challenge(reference, body):
    get_order_or_404(reference)
    kind = body.get("type")
    fields = {
        "otp": ("otp",),
        "otp_resend": (),
        "card_pin": ("card_pin",),
        "address_verification": ("country", "state_code", "city", "address", "zip_code"),
        "phone_verification": ("phone",),
    }
    if kind not in fields:
        raise ApiError("Unknown challenge type")
    payload = {"reference": reference}
    for f in fields[kind]:
        payload[f] = str(body.get(f, "")).strip()
    result = call_gateway(reference, "H2H challenge " + kind, lambda p: client.h2h_challenge(kind, p), payload)
    if result.get("http_status") == 200 and isinstance(result.get("data"), dict) and result["data"].get("status"):
        apply_payment(reference, result["data"])
    return next_step(reference)


def create_refund(reference, body):
    order = get_order_or_404(reference)
    if order["status"] != "success":
        raise ApiError("Refund is allowed only for successful payments")
    payload = {
        "parent_reference": reference,
        "description": body.get("description") or "Refund of %s" % reference,
        "callback_url": PUBLIC_BASE_URL + "/callback",
    }
    result = call_gateway(reference, "H2H POST /v1/refund", client.refund, payload)
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if data.get("identifier"):
        apply_refund(reference, data)
    elif result.get("http_status") != 200 or data.get("error_code"):
        raise ApiError(data.get("error_reason") or result.get("error") or "Refund failed (HTTP %s)" % result.get("http_status"))
    return order_view(reference)


def refund_check(identifier):
    refund = store.get_refund(identifier)
    if not refund:
        raise ApiError("Refund not found", 404)
    result = call_gateway(
        refund["order_reference"], "H2H POST /v1/refund/check", lambda p: client.refund_check(identifier=identifier),
        {"identifier": identifier},
    )
    if result.get("http_status") == 200:
        apply_refund(refund["order_reference"], result.get("data"))
    return order_view(refund["order_reference"])


def handle_callback(raw, headers):
    """Verify and apply a gateway callback. Returns (http_status, body)."""
    try:
        data = json.loads(raw.decode() or "{}")
    except ValueError:
        data = None
    ok, reason = False, ""
    # The gateway signs the Host it connected to; behind a proxy that may arrive as X-Forwarded-Host.
    hosts = [headers.get("host"), headers.get("x-forwarded-host"), urlparse(PUBLIC_BASE_URL).netloc]
    for host in [h for i, h in enumerate(hosts) if h and h not in hosts[:i]]:
        ok, reason = verify_callback(dict(headers, host=host), raw, API_KEY, API_SECRET)
        if ok:
            break
    reference = data.get("reference") if isinstance(data, dict) else None
    order_ref = None
    if isinstance(data, dict):
        if data.get("type") == "refund":
            parent = store.find_order_by_identifier(data.get("parent_identifier") or "")
            order_ref = parent["reference"] if parent else reference
        elif reference and store.get_order(reference):
            order_ref = reference
    log_headers = {k: v for k, v in headers.items() if k in ("host", "x-forwarded-host", "digest", "signature", "content-length", "content-type")}
    store.add_event(
        order_ref or reference, "callback", "Callback " + ("signature OK" if ok else "signature FAILED: " + reason),
        {"headers": log_headers, "body": mask(data) if data is not None else raw.decode(errors="replace")[:2000]},
    )
    if not ok and CALLBACK_SIGNATURE_MODE == "strict":
        return 401, {"error": reason}
    if not isinstance(data, dict):
        return 400, {"error": "Invalid JSON"}
    if data.get("type") == "refund":
        if order_ref:
            apply_refund(order_ref, data)
    elif order_ref:
        order = store.get_order(order_ref)
        if order["integration"] == "hpp" or "payment" in data:
            apply_intention(order_ref, data)
        else:
            apply_payment(order_ref, data)
    return 200, {"status": "ok"}


def get_order_or_404(reference):
    order = store.get_order(reference)
    if not order:
        raise ApiError("Order not found", 404)
    return order


def order_view(reference, with_events=True):
    order = get_order_or_404(reference)
    view = {k: v for k, v in order.items() if not k.endswith("_json") and k != "card_token"}
    view["has_token"] = bool(order["card_token"])
    view["customer"] = json.loads(order["customer_json"] or "{}")
    view["card"] = json.loads(order["card_json"]) if order["card_json"] else None
    view["challenge"] = json.loads(order["challenge_json"]) if order["challenge_json"] else None
    view["final"] = order["status"] in FINAL_STATUSES
    view["refunds"] = store.list_refunds(reference)
    if with_events:
        view["events"] = [
            dict(e, data=json.loads(e["data_json"]) if e["data_json"] else None, data_json=None)
            for e in store.list_events(reference)
        ]
    return view


def config_view():
    return {
        "mock": MOCK,
        "configured": bool(API_KEY and API_SECRET),
        "public_base_url": PUBLIC_BASE_URL,
        "h2h_host": H2H_HOST,
        "hpp_host": HPP_HOST,
        "callback_url": PUBLIC_BASE_URL + "/callback",
        "callback_signature_mode": CALLBACK_SIGNATURE_MODE,
    }


# ---- HTTP layer --------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "MerchantDemo/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args))

    @property
    def client_ip(self):
        fwd = self.headers.get("X-Forwarded-For")
        return fwd.split(",")[0].strip() if fwd else self.client_address[0]

    def lower_headers(self):
        return {k.lower(): v for k, v in self.headers.items()}

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, name):
        path = os.path.realpath(os.path.join(STATIC, name))
        if not path.startswith(STATIC + os.sep) or not os.path.isfile(path):
            return self.send_json(404, {"error": "Not found"})
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", (mimetypes.guess_type(path)[0] or "application/octet-stream") + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, status, html):
        body = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def authorized(self, path):
        if not SITE_PASSWORD or path.startswith(PUBLIC_PATHS):
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                user, _, pwd = base64.b64decode(auth[6:]).decode().partition(":")
                if user == SITE_USER and pwd == SITE_PASSWORD:
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Merchant demo"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method):
        url = urlparse(self.path)
        path = url.path
        if not self.authorized(path):
            return
        try:
            if path.startswith("/mock/"):
                if not MOCK:
                    return self.send_json(404, {"error": "Mock gateway is disabled"})
                return mock_gateway.handle(self, method, path, parse_qs(url.query), self.read_body() if method == "POST" else b"")
            if method == "GET":
                return self.route_get(path)
            return self.route_post(path)
        except ApiError as e:
            self.send_json(e.status, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self.send_json(500, {"error": "%s: %s" % (type(e).__name__, e)})

    def route_get(self, path):
        if path == "/":
            return self.send_file("index.html")
        if path == "/orders":
            return self.send_file("orders.html")
        if re.fullmatch(r"/order/[a-z0-9_-]+", path):
            return self.send_file("order.html")
        if path.startswith("/static/"):
            return self.send_file(path[len("/static/"):])
        if path == "/health":
            return self.send_json(200, {"status": "ok"})
        if path == "/api/config":
            return self.send_json(200, config_view())
        if path == "/api/orders":
            return self.send_json(200, {"orders": [order_view(o["reference"], False) for o in store.list_orders()]})
        m = re.fullmatch(r"/api/orders/([a-z0-9_-]+)", path)
        if m:
            return self.send_json(200, order_view(m.group(1)))
        self.send_json(404, {"error": "Not found"})

    def route_post(self, path):
        raw = self.read_body()
        if path == "/callback":
            status, payload = handle_callback(raw, self.lower_headers())
            return self.send_json(status, payload)
        try:
            body = json.loads(raw.decode() or "{}")
        except ValueError:
            raise ApiError("Invalid JSON body")
        if not (API_KEY and API_SECRET):
            raise ApiError("FLEXIFY_API_KEY / FLEXIFY_API_SECRET are not configured", 503)
        if path == "/api/h2h/pay":
            return self.send_json(200, h2h_pay(body, self.client_ip, self.lower_headers()))
        if path == "/api/hpp/create":
            return self.send_json(200, hpp_create(body, self.client_ip))
        m = re.fullmatch(r"/api/orders/([a-z0-9_-]+)/(check|challenge|refund|repeat)", path)
        if m:
            ref, action = m.groups()
            if action == "check":
                return self.send_json(200, check_order(ref))
            if action == "challenge":
                return self.send_json(200, submit_challenge(ref, body))
            if action == "refund":
                return self.send_json(200, create_refund(ref, body))
            return self.send_json(200, h2h_repeat(ref, body, self.client_ip))
        m = re.fullmatch(r"/api/refunds/([A-Za-z0-9_-]+)/check", path)
        if m:
            return self.send_json(200, refund_check(m.group(1)))
        self.send_json(404, {"error": "Not found"})


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("Merchant demo on :%d  public=%s  mock=%s  h2h=%s  hpp=%s" % (PORT, PUBLIC_BASE_URL, MOCK, H2H_HOST, HPP_HOST))
    if not (API_KEY and API_SECRET):
        print("WARNING: FLEXIFY_API_KEY / FLEXIFY_API_SECRET not set; payments are disabled")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
