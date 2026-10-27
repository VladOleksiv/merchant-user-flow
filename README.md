# Merchant demo: Flexify card flow (H2H + HPP)

A test merchant site that lets a provider walk through the whole card payment flow against the
Flexify Gate API (https://docs.flexifai.io/). It is pure Python 3.9+ with only the standard library, and stores data in SQLite.

## What the site does

- **Shop / checkout** (`/`): pick a product, edit the amount and currency, fill in customer details, then choose a flow:
  - **H2H** (card on site): the card form posts to our backend, which sends a signed `POST https://h2h.../v1/payment`.
    - `challenge.redirect`: the customer is sent to 3DS and comes back to `redirect_url`.
    - `pending` with no challenge: the backend calls `/v1/payment/check` until the challenge appears.
    - `otp`, `card_pin`, `address_verification` and `phone_verification` challenges each get their own form on the order page.
  - **HPP** (hosted payment page): `POST https://hpp.../v1/payment-intention` with `widget.method=card`, then a redirect to the returned `redirect_url`. `cancel_url` brings the customer back to the order page.
- **Order page** (`/order/<reference>`):
  - Shows the status, polling `/v1/payment/check` or `/v1/payment-intention/check` every 4 seconds while the payment is pending.
  - Offers a refund (`/v1/refund` and `/v1/refund/check`).
  - Offers a repeat payment with the saved card token (`mode: token`).
  - Shows the full log of requests, responses and callbacks, with PAN and CVV masked.
- **Callback** (`POST /callback`): checks the `Digest` and the HMAC-SHA256 `Signature` as described in the docs, then updates the order or refund.
- **Orders** (`/orders`): lists every test payment.

Every request is signed as described in docs/getting-started/auth: `Authorization: Signature keyId=..., algorithm="HmacSHA256", headers="host digest content-length date", signature=...`.

## Configuration (environment variables or a `.env` file next to `server.py`)

| Variable | Default | Purpose |
|---|---|---|
| `FLEXIFY_API_KEY`, `FLEXIFY_API_SECRET` | none | API keys from Merchant Portal: Projects, then Project, then API Keys |
| `FLEXIFY_H2H_HOST` | `h2h.flexify.finance` | H2H Gate API host |
| `FLEXIFY_HPP_HOST` | `hpp.flexify.finance` | HPP Gate API host |
| `PUBLIC_BASE_URL` | `http://localhost:PORT` | Public HTTPS URL of the site. Used to build `callback_url`, `redirect_url` and `cancel_url` |
| `PORT` | `8080` | Listen port |
| `SITE_USER`, `SITE_PASSWORD` | empty | Optional basic auth for the site. `/callback` stays open |
| `CALLBACK_SIGNATURE_MODE` | `strict` | `strict` returns 401 when the signature is wrong. `log` records the failure and still applies the callback |
| `MOCK_GATEWAY` | `0` | `1` switches to the built-in simulator, so no keys are needed |
| `DB_PATH` | `./data.sqlite3` | SQLite file |

## Run

```bash
# local, with the simulator (no keys needed)
MOCK_GATEWAY=1 python3 server.py        # http://localhost:8080

# real gateway
FLEXIFY_API_KEY=... FLEXIFY_API_SECRET=... PUBLIC_BASE_URL=https://demo.example.com python3 server.py

# docker
docker build -t merchant-demo .
docker run -d -p 8080:8080 -v merchant-data:/data \
  -e FLEXIFY_API_KEY=... -e FLEXIFY_API_SECRET=... -e PUBLIC_BASE_URL=https://demo.example.com merchant-demo
```

## Before going live with the provider

1. **IP whitelist**: the H2H and HPP Gate APIs only accept requests from whitelisted IPs. Deploy on a server with a static IP and ask Flexify to add it.
2. **HTTPS and a public domain**: the 3DS return, the HPP return and callbacks all go to `PUBLIC_BASE_URL`. Put the app behind nginx or caddy with TLS. The proxy has to forward `Host` (or `X-Forwarded-Host`), because the callback signature covers the `host` header.
3. **PCI DSS**: in the H2H flow, the PAN and CVV pass through this server. That is allowed only with real cards in a PCI-compliant setup. For test cards it is fine. Card data is never written to the database or logs; it is masked.

## Test cards (from the docs)

Any CVV and expiry date work.

| PAN | 3DS | Result |
|---|---|---|
| 4242424242424242 | none | success |
| 5352783000000210 | V1 | success |
| 4012000000003010 | V2 frictionless | success |
| 4012000000003119 | V2 challenge | success |
| 4012000000003242 | V2 challenge | declined 1140 |
| 4012000000004240 | none | declined 1110 |
| 4012000000006740 | none | declined 1120 |

These cards exist only in the simulator: `5060990580000217499` triggers an OTP challenge (code `123456`), and `4111111111111111` returns pending with no challenge, so the challenge only shows up on check.

## Files

- `server.py`: HTTP server, routes, payment logic
- `flexify.py`: request signing, Gate API client, callback verification
- `store.py`: SQLite storage (orders, refunds, events)
- `mock_gateway.py`: gateway simulator (`MOCK_GATEWAY=1` only)
- `static/`: frontend (shop, order page, orders list)
