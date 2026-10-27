"""Flexify Gate API client: request signing, H2H/HPP calls, callback verification.

Signature scheme (docs.flexifai.io/docs/getting-started/auth):
  POST: payload = "host: <host>digest: <digest>content-length: <len>date: <date>"
  GET:  payload = "host: <host>date: <date>"
  signature = base64(HMAC-SHA256(payload, api_secret))
  Authorization: Signature keyId="<key>", algorithm="HmacSHA256", headers="...", signature="..."
"""
import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request


def b64_sha256(data):
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def b64_hmac(payload, secret):
    return base64.b64encode(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()).decode()


def utc_now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sign_headers(api_key, api_secret, host, body=None, date=None):
    """Build the auth headers for a request. body is bytes for POST, None for GET."""
    date = date or utc_now_iso()
    headers = {"Content-Type": "application/json", "Host": host, "Date": date}
    if body is not None:
        digest = "SHA-256=" + b64_sha256(body)
        payload = "host: " + host + "digest: " + digest + "content-length: " + str(len(body)) + "date: " + date
        signed = "host digest content-length date"
        headers["Digest"] = digest
    else:
        payload = "host: " + host + "date: " + date
        signed = "host date"
    signature = b64_hmac(payload, api_secret)
    headers["Authorization"] = (
        'Signature keyId="%s", algorithm="HmacSHA256", headers="%s", signature="%s"' % (api_key, signed, signature)
    )
    return headers


def parse_signature_header(value):
    """Parse 'keyId="..", algorithm="..", headers="..", signature=".."' into a dict.

    An optional leading 'Signature ' scheme token is tolerated.
    """
    value = (value or "").strip()
    if value.lower().startswith("signature "):
        value = value[len("signature "):]
    params = {}
    for item in value.split(", "):
        n = item.find("=")
        if n <= 0:
            continue
        params[item[:n].strip()] = item[n + 1:].strip().strip('"')
    return params


def verify_callback(headers, body, api_key, api_secret):
    """Verify an incoming callback. headers: dict with lower-cased names. Returns (ok, reason)."""
    expected_digest = "SHA-256=" + b64_sha256(body)
    if headers.get("digest") != expected_digest:
        return False, "Invalid digest. Expected %s got %s" % (expected_digest, headers.get("digest"))
    params = parse_signature_header(headers.get("signature") or headers.get("authorization"))
    if not params:
        return False, "Missing Signature header"
    if params.get("keyId") != api_key:
        return False, "Invalid api key"
    signed = params.get("headers")
    if not signed:
        return False, "Undefined signature param `headers`"
    payload = ""
    for name in signed.split(" "):
        value = headers.get(name.lower())
        if value is None:
            return False, "Undefined header " + name
        payload += name + ": " + value
    if not hmac.compare_digest(b64_hmac(payload, api_secret), params.get("signature", "")):
        return False, "Invalid signature"
    return True, "Signature verified"


class FlexifyClient:
    def __init__(self, api_key, api_secret, h2h_host, hpp_host, h2h_base=None, hpp_base=None, timeout=40):
        self.api_key = api_key
        self.api_secret = api_secret
        self.h2h_host = h2h_host
        self.hpp_host = hpp_host
        # base URLs can differ from the signed Host (used by the local mock gateway)
        self.h2h_base = h2h_base or "https://" + h2h_host
        self.hpp_base = hpp_base or "https://" + hpp_host
        self.timeout = timeout

    def _request(self, gate, method, path, payload=None):
        host = self.h2h_host if gate == "h2h" else self.hpp_host
        base = self.h2h_base if gate == "h2h" else self.hpp_base
        body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None
        headers = sign_headers(self.api_key, self.api_secret, host, body)
        req = urllib.request.Request(base + path, data=body, method=method, headers=headers)
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except Exception as e:  # network error / timeout
            return {
                "http_status": 0,
                "error": "%s: %s" % (type(e).__name__, e),
                "url": base + path,
                "duration_ms": int((time.time() - started) * 1000),
            }
        try:
            data = json.loads(raw.decode() or "null")
        except ValueError:
            data = {"raw": raw.decode(errors="replace")[:2000]}
        return {
            "http_status": status,
            "data": data,
            "url": base + path,
            "duration_ms": int((time.time() - started) * 1000),
        }

    # H2H Gate API
    def h2h_payment(self, payload):
        return self._request("h2h", "POST", "/v1/payment", payload)

    def h2h_check(self, reference):
        return self._request("h2h", "POST", "/v1/payment/check", {"reference": reference})

    def h2h_challenge(self, kind, payload):
        path = {
            "otp": "/v1/payment/challenge/otp",
            "otp_resend": "/v1/payment/challenge/otp/resend",
            "card_pin": "/v1/payment/challenge/card-pin",
            "address_verification": "/v1/payment/challenge/address-verification",
            "phone_verification": "/v1/payment/challenge/phone-verification",
        }[kind]
        return self._request("h2h", "POST", path, payload)

    def refund(self, payload):
        return self._request("h2h", "POST", "/v1/refund", payload)

    def refund_check(self, identifier=None, reference=None):
        body = {"identifier": identifier} if identifier else {"reference": reference}
        return self._request("h2h", "POST", "/v1/refund/check", body)

    def balance(self):
        return self._request("h2h", "GET", "/v1/balance")

    # HPP Gate API
    def hpp_payment_intention(self, payload):
        return self._request("hpp", "POST", "/v1/payment-intention", payload)

    def hpp_check(self, reference):
        return self._request("hpp", "POST", "/v1/payment-intention/check", {"reference": reference})
