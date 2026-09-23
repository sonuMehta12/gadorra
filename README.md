# Gradorra GPET API

Student pre-registration, payment and WhatsApp acknowledgement API.
FastAPI + PostgreSQL. Built to run fully on a laptop; AWS deploy is the last step, not a prerequisite.

## Run it

```bash
docker compose up -d                      # Postgres on :5435 (other containers untouched)
python3.12 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env                      # then fill in the credentials
./.venv/bin/python scripts/init_db.py     # tables + 75 UP districts
./.venv/bin/uvicorn app.main:app --reload
```

- Test UI: http://localhost:8000/
- API docs: http://localhost:8000/docs
- Health: http://localhost:8000/health

## The flow

```
POST /otp/send            code to WhatsApp (or the server log in dev)
POST /otp/verify          returns a 15-minute form token
POST /registrations       needs X-Form-Token; saves the student, fee decided server-side
POST /payments/order      creates a Razorpay order at the server's price
     -> Razorpay checkout in the browser
POST /payments/verify     signature check, then status fetched from Razorpay,
                          registration marked PAID, acknowledgement number issued,
                          WhatsApp message sent
POST /webhooks/razorpay   the same thing, for when the browser never comes back
POST /acknowledgements/lookup   by number or by verified mobile -> prefill + discount
```

## Things that are deliberate

- **The fee never comes from the browser.** It is read from config on the server every time.
- **Razorpay's own API is the source of truth**, not the checkout callback. The signature is
  checked first, then the payment is fetched server to server.
- **Everything is idempotent on the Razorpay payment id.** Verify and webhook can both fire;
  the acknowledgement number is generated once and the WhatsApp message is sent once.
- **The acknowledgement number is generated inside the transaction that marks the payment PAID**,
  so one payment can never produce two numbers.
- **Lookup needs an OTP-verified token.** The `GPET26/UP61/69684` format is short enough to
  guess, so a bare number never reveals anyone's details.

## Configuration worth knowing

| Variable | What it does |
| --- | --- |
| `OTP_CHANNEL` | `console` (prints to the log), `whatsapp_text`, `whatsapp_template` |
| `WHATSAPP_PROVIDER` | `console` or `meta` |
| `WHATSAPP_ACK_CHANNEL` | `template` or `text` (free-form, 24h window only) |
| `WHATSAPP_AUTH_TEMPLATE_BUTTON` | must be `true` for copy-code OTP templates |
| `WHATSAPP_MIN_SECONDS_BETWEEN_MESSAGES` | 6 -- Meta's per-user throughput rule |
| `WHATSAPP_DAILY_UNIQUE_RECIPIENTS` | 250 -- raise as the WABA tier grows |
| `SYNC_INTERVAL_MINUTES` | 10 -- background settler for unreported payments |
| `CURRENT_PHASE` | `PRE_LAUNCH` or `POST_LAUNCH` -- decides which fee applies |
| `ACK_EXAM_CODE`, `ACK_STATE_PREFIX`, `ACK_DIGITS` | acknowledgement format, no code change needed |
| `FEE_*_PAISE` | Rs 99 pre, Rs 499 post, Rs 299 for a pre-registered student |

Every provider sits behind an interface picked by an env variable, so going live is a
credentials change rather than a code change.

## Tests

```bash
./.venv/bin/python -m pytest tests/ -q      # 55 tests, ~1 second
./.venv/bin/python scripts/e2e_check.py     # end-to-end against a running server
```

Tests run against a separate `gradorra_test` database, created automatically, so
they can never touch development data. Each test runs inside a transaction that
is rolled back. WhatsApp and Razorpay are stubbed -- no network call leaves the
machine. `e2e_check.py` is the opposite: it drives a live server, creates a real
Razorpay test order, and cleans up after itself. It needs `OTP_CHANNEL=console`.

## Abuse control

| Where | Limit |
| --- | --- |
| `POST /registrations` | 5 per IP per 10 minutes |
| `POST /otp/send` | 10 per IP per 10 minutes, and 3 per mobile per hour |
| Everything under `/api/v1` | 100 per IP per minute |

Limits are applied in middleware, before request validation, so a flood of
malformed requests still counts. The counters live in process -- behind several
API tasks the limit becomes per-task, so move `app/rate_limit.py` to Redis
before scaling out. Nothing else changes.

`BOT_CHECK_PROVIDER` accepts `none` (default), `turnstile` or `recaptcha`. The
front end sends the widget token as `bot_check_token` and the server verifies it.
If the provider is unreachable the request is allowed through and logged -- a
captcha outage must not stop registrations.

## Dev helpers

```bash
# run the whole payment path without touching Razorpay
./.venv/bin/python scripts/simulate_payment.py <registration_id>

# expose the webhook to Razorpay while developing
ngrok http 8000     # then set the webhook URL to https://<id>.ngrok.app/api/v1/webhooks/razorpay
```

## WhatsApp limits the code enforces

Meta rejects sends that break these, and rejections hurt the number's quality
rating, so they are checked before a request is made, never after:

- **Consent** -- `consent_whatsapp` false means the student is never messaged.
- **Throughput** -- one message per 6 seconds to the same user.
- **Messaging limit** -- 250 unique recipients per rolling 24 hours on a fresh
  number, 2,000 after business verification, then 10K and 100K. A recipient
  already messaged inside the window costs nothing more.
- **Free-form** -- a non-template message only lands inside a 24-hour customer
  service window, which opens when the *student* messages the business first.

A blocked send is written to `notifications` with the reason and can be resent;
it is never silently dropped.

### Authentication templates

A copy-code OTP template needs the code **twice** -- in the body parameters and
in a button component. Meta's own page says the button is fixed at creation
time; sending body-only returns `(#131008) Button at index 0 of type Url
requires a parameter`. `WHATSAPP_AUTH_TEMPLATE_BUTTON=true` handles it.

## Deploying it free, so the front end can start

Everything on Render: push the repo, then **New → Blueprint** and pick it.
`render.yaml` creates the API and a Postgres database and wires `DATABASE_URL`
between them. Five values are marked `sync: false`, so Render asks for them:

| Variable | Value |
| --- | --- |
| `WHATSAPP_TOKEN` | the permanent system-user token |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | the **test** pair while the front end is being built |
| `RAZORPAY_WEBHOOK_SECRET` | from the Razorpay dashboard, in Test Mode |
| `CORS_ORIGINS` | the front end's origins, comma separated |

`AUTO_INIT_DB=true` is set, so the first boot creates the tables and loads the
75 districts. There are no migrations yet, so leave it on until Alembic arrives.

Then hand the front-end team three links:

- `https://<service>.onrender.com/docs` — the API reference
- `https://<service>.onrender.com/api/v1` — the base URL
- `https://<service>.onrender.com/` — the test UI, to see the flow working

**What the free plan costs you.** The service sleeps after 15 minutes idle and
the next request takes about 50 seconds to wake it — tell the front-end team, or
they will report the API as down. The database expires 30 days after creation,
with a 14-day grace period; upgrading to a paid plan before then keeps the data.
The sync job does not run while the service is asleep, so a payment whose browser
never came back settles on the next wake instead of within ten minutes.

None of this is meant for launch. That goes to the AWS setup in the scope
document.

## Still open

- `gpet_acknowledgement` is still **in review**. Until Meta approves it the
  acknowledgement falls back to a free-form message, which only reaches students
  whose 24-hour window is open -- in practice, almost none. The number is always
  on the success screen and the receipt, so nothing is lost, but WhatsApp
  delivery of it is not dependable yet.
- `RAZORPAY_WEBHOOK_SECRET` is empty; the webhook rejects everything until it is
  set. The 10-minute sync job covers the same gap meanwhile.
- The Meta app is **unpublished**. Unpublished apps only receive test webhooks
  from the dashboard, not production events. Publish before go-live.
- The WhatsApp webhook callback URL is not configured -- it needs a public HTTPS
  endpoint, so it waits on the first deploy.
- `gpet.org.in` is not live yet, and the acknowledgement template links to it.
- District codes are sequential over the alphabetical list (`UP01`..`UP75`). If the client
  uses another scheme, only `app/seed_data.py` changes.
