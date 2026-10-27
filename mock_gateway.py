"""In-process mock of the Flexify H2H/HPP Gate API, enabled with MOCK_GATEWAY=1.

It verifies request signatures, mimics the documented test cards, serves a fake 3DS page and a fake
hosted checkout page, and sends signed callbacks, so the whole flow can be exercised without credentials.
Never enable it in front of a real provider.
"""
import html
import json
import random
import string
import threading
import time
import urllib.request
from urllib.parse import parse_qs, urlparse

from flexify import b64_hmac, b64_sha256, parse_signature_header

# pan -> (needs 3ds, final status, error_code, error_reason); mirrors docs "Test cards"
CARDS = {
    "4242424242424242": (False, "success", 0, ""),
    "5352783000000210": (True, "success", 0, ""),
    "4012000000003010": (False, "success", 0, ""),
    "4012000000003119": (True, "success", 0, ""),
    "4012000000003242": (True, "declined", 1140, "3D secure failed"),
    "4012000000004240": (False, "declined", 1110, "Card lost or stolen"),
    "4012000000006740": (False, "declined", 1120, "Insufficient funds on card"),
}
OTP_CARD = "5060990580000217499"      # mock only: OTP challenge, code 123456
DELAYED_CARD = "4111111111111111"     # mock only: pending without challenge, challenge appears on check
MOCK_OTP = "123456"

API_KEY = API_SECRET = PUBLIC_BASE_URL = ""

lock = threading.Lock()
payments = {}    # reference -> payment dict
intents = {}     # reference -> intention dict
refunds = {}     # identifier -> refund dict
tokens = {}      # token -> card info


def configure(api_key, api_secret, public_base_url):
    global API_KEY, API_SECRET, PUBLIC_BASE_URL
    API_KEY, API_SECRET, PUBLIC_BASE_URL = api_key, api_secret, public_base_url


def _id(prefix):
    return prefix + "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(11))


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _brand(pan):
    return {"4": "visa", "5": "mastercard"}.get(pan[:1], "card")


# ---- signatures --------------------------------------------------------------------------------

def _verify_request(handler, body):
    params = parse_signature_header(handler.headers.get("Authorization"))
    if params.get("keyId") != API_KEY:
        return "Invalid api key"
    headers = {k.lower(): v for k, v in handler.headers.items()}
    if body and headers.get("digest") != "SHA-256=" + b64_sha256(body):
        return "Invalid digest"
    payload = ""
    for name in params.get("headers", "").split(" "):
        if name not in headers:
            return "Missing header " + name
        payload += name + ": " + headers[name]
    if b64_hmac(payload, API_SECRET) != params.get("signature"):
        return "Invalid signature"
    return None


def _send_callback(url, data):

    def run():
        time.sleep(1)
        body = json.dumps(data).encode()
        host = urlparse(url).netloc
        digest = "SHA-256=" + b64_sha256(body)
        payload = "host: " + host + "digest: " + digest + "content-length: " + str(len(body))
        signature = 'keyId="%s", algorithm="HmacSHA256", headers="host digest content-length", signature="%s"' % (
            API_KEY, b64_hmac(payload, API_SECRET))
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Host": host, "Digest": digest, "Signature": signature,
        })
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            print("mock callback to %s failed: %s" % (url, e))

    threading.Thread(target=run, daemon=True).start()


# ---- payment model -----------------------------------------------------------------------------

def _payment_view(p, include_token=True):
    view = {k: p[k] for k in ("error_code", "error_reason", "identifier", "request_id", "reference", "status",
                              "type", "method", "mode", "currency", "amount")}
    view["timestamp"] = _now()
    view["status_date"] = p["status_date"]
    if p.get("card"):
        card = dict(p["card"])
        if not (include_token and p["status"] == "success"):
            card.pop("token", None)
        view["card"] = card
    if p["status"] == "pending" and p.get("challenge") and p.get("challenge_visible", True):
        view["challenge"] = p["challenge"]
    return view


def _intent_view(i):
    view = {k: i[k] for k in ("error_code", "reference", "identifier", "request_id", "status", "amount", "currency")}
    if i.get("error_reason"):
        view["error_reason"] = i["error_reason"]
    if i["status"] == "new":
        view["redirect_url"] = i["redirect_url"]
    p = payments.get(i["reference"])
    if p:
        view["payment"] = dict(_payment_view(p), creation_date=p["creation_date"])
        view["payment"].pop("challenge", None)
    return view


def _create_payment(payload, gate):
    ref = payload["reference"]
    mode = payload.get("mode", "initial")
    card_in = payload.get("card") or {}
    if mode == "token":
        card = tokens.get(card_in.get("token"))
        if not card:
            return {"error_code": 30, "error_reason": "Invalid params", "error_fields": [
                {"field": "card.token", "message": "token not found"}]}
        pan = card["_pan"]
    else:
        pan = card_in.get("pan", "")
        card = {"bin": pan[:6], "last_4": pan[-4:], "exp_month": card_in.get("exp_month"),
                "exp_year": card_in.get("exp_year"), "brand": _brand(pan), "holder_name": card_in.get("holder_name")}
    needs_3ds, final, code, reason = CARDS.get(pan, (False, "declined", 1001, "Transaction is declined"))
    p = {
        "reference": ref, "identifier": _id("PM"), "request_id": _id("rq").lower(), "type": "payment",
        "method": "card", "mode": mode, "currency": payload["currency"], "amount": "%.2f" % float(payload["amount"]),
        "status": "pending", "error_code": 0, "error_reason": "", "status_date": _now(), "creation_date": _now(),
        "card": {k: v for k, v in card.items() if not k.startswith("_")}, "gate": gate,
        "redirect_url": payload.get("redirect_url"), "callback_url": payload.get("callback_url"),
        "customer": payload.get("customer"), "final": (final, code, reason), "_pan": pan,
    }
    if mode == "token" and needs_3ds:
        needs_3ds = False  # token payments in the mock are frictionless
    if pan == OTP_CARD:
        p["final"] = ("success", 0, "")
        p["challenge"] = {"challenge_type": "otp"}
    elif needs_3ds or pan == DELAYED_CARD:
        if pan == DELAYED_CARD:
            p["final"] = ("success", 0, "")
            p["challenge_visible"] = False  # first response has no challenge; shows up on check
        p["challenge"] = {"challenge_type": "redirect",
                          "challenge_redirect": {"url": "%s/mock/3ds/%s" % (PUBLIC_BASE_URL, ref)}}
    with lock:
        payments[ref] = p
    if "challenge" not in p:
        _finalize(ref)
    return _payment_view(payments[ref])


def _finalize(ref, fail=False):
    with lock:
        p = payments[ref]
        if p["status"] != "pending":
            return p
        status, code, reason = ("declined", 1140, "3D secure failed") if fail else p["final"]
        p.update(status=status, error_code=code, error_reason=reason, status_date=_now())
        p.pop("challenge", None)
        if status == "success":
            token = p["card"].get("token") or "tok-" + _id("").lower()
            p["card"]["token"] = token
            tokens[token] = dict(p["card"], _pan=p["_pan"])
    if p["gate"] == "hpp":
        i = intents[ref]
        i.update(status="success" if status == "success" else "failed", error_code=code, error_reason=reason)
        cb = dict(_intent_view(i), billing_amount=i["amount"], billing_currency=i["currency"],
                  fee_amount="0.00", fee_currency=i["currency"])
        _send_callback(i["callback_url"], cb)
    elif p.get("callback_url"):
        cb = dict(_payment_view(p), creation_date=p["creation_date"], customer=p.get("customer"),
                  billing_amount=p["amount"], billing_currency=p["currency"], fee_amount="0.00",
                  fee_currency=p["currency"])
        _send_callback(p["callback_url"], cb)
    return p


# ---- HTTP --------------------------------------------------------------------------------------

def handle(handler, method, path, query, body):
    if path.startswith("/mock/3ds/"):
        return _acs_page(handler, method, path[len("/mock/3ds/"):], body)
    if path.startswith("/mock/checkout/"):
        return _checkout_page(handler, method, path[len("/mock/checkout/"):], body)
    error = _verify_request(handler, body)
    if error:
        return handler.send_json(401, {"error_code": 2, "error_reason": "Unauthorized: " + error})
    payload = json.loads(body.decode() or "{}") if body else {}
    route = path[len("/mock"):]
    if route == "/h2h/v1/payment":
        if payload.get("reference") in payments:
            return handler.send_json(400, {"error_code": 30, "error_reason": "Invalid params", "error_fields": [
                {"field": "reference", "message": "reference already exists"}]})
        return handler.send_json(200, _create_payment(payload, "h2h"))
    if route == "/h2h/v1/payment/check":
        p = payments.get(payload.get("reference"))
        if not p:
            return handler.send_json(200, {"error_code": 101, "error_reason": "Transaction not found",
                                           "request_id": _id("rq").lower(), "timestamp": _now()})
        p["challenge_visible"] = True
        return handler.send_json(200, _payment_view(p))
    if route.startswith("/h2h/v1/payment/challenge/"):
        p = payments.get(payload.get("reference"))
        if not p:
            return handler.send_json(200, {"error_code": 101, "error_reason": "Transaction not found"})
        if route.endswith("/otp") and p["status"] == "pending":
            if payload.get("otp") == MOCK_OTP:
                _finalize(p["reference"])
            else:
                p["otp_attempts"] = p.get("otp_attempts", 0) + 1
                if p["otp_attempts"] >= 3:
                    _finalize(p["reference"], fail=True)
        return handler.send_json(200, _payment_view(p))
    if route == "/h2h/v1/refund":
        p = payments.get(payload.get("parent_reference"))
        if not p or p["status"] != "success":
            return handler.send_json(200, {"error_code": 1001, "error_reason": "Transaction is declined",
                                           "request_id": _id("rq").lower(), "timestamp": _now()})
        r = {"error_code": 0, "error_reason": "", "request_id": _id("rq").lower(), "reference": p["reference"],
             "parent_identifier": p["identifier"], "identifier": _id("RF"), "status": "pending", "type": "refund",
             "method": "card", "currency": p["currency"], "amount": p["amount"], "timestamp": _now()}
        refunds[r["identifier"]] = r

        def settle():
            time.sleep(2)
            r.update(status="success", status_date=_now())
            if payload.get("callback_url"):
                _send_callback(payload["callback_url"], dict(r, creation_date=_now(), customer={"identifier": "mock"}))

        threading.Thread(target=settle, daemon=True).start()
        return handler.send_json(200, r)
    if route == "/h2h/v1/refund/check":
        r = refunds.get(payload.get("identifier"))
        if not r:
            return handler.send_json(200, {"error_code": 101, "error_reason": "Transaction not found"})
        return handler.send_json(200, dict(r, timestamp=_now()))
    if route == "/h2h/v1/balance":
        return handler.send_json(200, {"error_code": 0, "balances": [{"currency": "USD", "amount": "1000.00"}]})
    if route == "/hpp/v1/payment-intention":
        ref = payload.get("reference", "")
        i = {"error_code": 0, "reference": ref, "identifier": _id("HPI"), "request_id": _id("rq").lower(),
             "status": "new", "amount": payload.get("amount"), "currency": payload.get("currency"),
             "redirect_url": "%s/mock/checkout/%s" % (PUBLIC_BASE_URL, ref),
             "merchant_redirect_url": payload.get("redirect_url"), "cancel_url": payload.get("cancel_url"),
             "callback_url": payload.get("callback_url"), "customer": payload.get("customer")}
        intents[ref] = i
        return handler.send_json(200, _intent_view(i))
    if route == "/hpp/v1/payment-intention/check":
        i = intents.get(payload.get("reference"))
        if not i:
            return handler.send_json(200, {"error_code": 101, "error_reason": "Transaction not found"})
        return handler.send_json(200, _intent_view(i))
    handler.send_json(404, {"error_code": 404, "error_reason": "Unknown mock route " + route})


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title><style>body{font:15px system-ui,sans-serif;background:#eef1f5;margin:0;padding:24px 16px;color:#1d2433}
.box{max-width:420px;margin:0 auto;background:#fff;border-radius:12px;padding:24px;box-shadow:0 2px 12px rgba(0,0,0,.08)}
.tag{display:inline-block;background:#fde68a;color:#78350f;font-size:12px;padding:2px 8px;border-radius:99px}
label{display:block;margin:12px 0 4px;font-size:13px;color:#555}input{width:100%%;box-sizing:border-box;padding:10px;border:1px solid #ccd;border-radius:8px;font-size:15px}
.row{display:flex;gap:8px}.row>div{flex:1}button{margin-top:16px;width:100%%;padding:12px;border:0;border-radius:8px;font-size:15px;cursor:pointer;background:#1d4ed8;color:#fff}
button.alt{background:#e5e7eb;color:#111}a{color:#1d4ed8}</style></head><body><div class="box">%s</div></body></html>"""


def _acs_page(handler, method, ref, body):
    p = payments.get(ref)
    if not p:
        return handler.send_html(404, PAGE % ("Not found", "Unknown transaction"))
    if method == "POST":
        action = parse_qs(body.decode()).get("action", ["complete"])[0]
        _finalize(ref, fail=(action == "fail"))
        target = intents[ref]["merchant_redirect_url"] if p["gate"] == "hpp" else p["redirect_url"]
        return handler.redirect(target)
    if p["gate"] == "hpp":
        intents[ref]["status"] = "pending"
    content = """<span class="tag">MOCK 3-D Secure</span><h2>Verify your payment</h2>
<p>Card %s **** %s, amount <b>%s %s</b></p><p>Expected result for this card: <b>%s</b></p>
<form method="post"><button name="action" value="complete">Authenticate</button>
<button class="alt" name="action" value="fail">Fail authentication</button></form>""" % (
        html.escape(p["card"]["bin"]), html.escape(p["card"]["last_4"]), p["amount"], p["currency"], p["final"][0])
    handler.send_html(200, PAGE % ("3-D Secure", content))


def _checkout_page(handler, method, rest, body):
    ref, _, action = rest.partition("/")
    i = intents.get(ref)
    if not i:
        return handler.send_html(404, PAGE % ("Not found", "Unknown payment intention"))
    if action == "cancel":
        return handler.redirect(i["cancel_url"])
    if method == "POST" and i["status"] in ("new", "opened"):
        form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        payload = {"reference": ref, "currency": i["currency"], "amount": i["amount"], "mode": "initial",
                   "card": {"pan": form.get("pan", "").replace(" ", ""), "holder_name": form.get("holder_name"),
                            "exp_month": form.get("exp_month"), "exp_year": form.get("exp_year"), "cvv": form.get("cvv")},
                   "customer": i["customer"]}
        p = _create_payment(payload, "hpp")
        if p.get("status") == "pending" and p.get("challenge", {}).get("challenge_type") == "redirect":
            return handler.redirect(p["challenge"]["challenge_redirect"]["url"])
        if payments[ref]["status"] == "pending" and DELAYED_CARD == payments[ref]["_pan"]:
            return handler.redirect(payments[ref]["challenge"]["challenge_redirect"]["url"])
        return handler.redirect(i["merchant_redirect_url"])
    if i["status"] == "new":
        i["status"] = "opened"
    if i["status"] not in ("new", "opened"):
        return handler.redirect(i["merchant_redirect_url"])
    content = """<span class="tag">MOCK hosted payment page</span><h2>Pay %s %s</h2>
<form method="post"><label>Card number</label><input name="pan" value="4242 4242 4242 4242" autocomplete="off">
<label>Card holder</label><input name="holder_name" value="Test Holder">
<div class="row"><div><label>MM</label><input name="exp_month" value="12"></div><div><label>YYYY</label><input name="exp_year" value="2030"></div>
<div><label>CVV</label><input name="cvv" value="123"></div></div><button>Pay</button></form>
<p><a href="/mock/checkout/%s/cancel">Cancel and return to merchant</a></p>""" % (i["amount"], i["currency"], html.escape(ref))
    handler.send_html(200, PAGE % ("Checkout", content))
