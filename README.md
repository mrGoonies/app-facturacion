# app_facturacion — Irritec KPI tracker

Internal tool to track two KPIs used for the administrative assistant's bonus:

1. **Purchasing** — time from a purchase request to a Purchase Order being issued.
2. **Invoicing** — time from a logistics hand-off (picking lists) to the status
   being marked "in process" and to the invoice being issued, plus billing
   errors and who they're attributable to.

Built with Django + PostgreSQL. The public forms (purchase request, logistics
hand-off, requester status page) need no login; the assistant's workspace
(queue, purchase detail, picking list detail, KPI scorecard) is behind auth.

## Setup

```bash
brew install postgresql@16          # if not already installed
brew services start postgresql@16
createdb app_facturacion

uv sync
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

Visit `http://127.0.0.1:8000/` for the public landing page, or
`http://127.0.0.1:8000/panel/` for the assistant workspace (login required).

Configuration (DB credentials, KPI targets/weights/bonus) is read from
environment variables with sensible local defaults — see
`config/settings.py`'s `KPI_SETTINGS` and `DATABASES` for what's tunable.

Purchase request reference images are uploaded to Cloudinary. Set these env
vars (e.g. in a local `.env`, already gitignored) from your Cloudinary
dashboard before uploads will work — without them the form still accepts
submissions but the upload will fail:

```
CLOUDINARY_CLOUD_NAME=your-cloud-name
CLOUDINARY_API_KEY=your-api-key
CLOUDINARY_API_SECRET=your-api-secret
```

Submitting a purchase request emails the requester (confirmation + status
link) and every active staff user (`is_staff=True`, i.e. anyone who can log
into `/panel/`) with a link to work the request — see `tracker/emails.py`.
By default `DJANGO_EMAIL_BACKEND` is the console backend, so locally these
just print to the terminal running `runserver`.

Real mail goes through **Mailchimp Transactional** (via
[django-anymail](https://anymail.dev/); it's still named "Mandrill" in
Anymail's backend/setting names — Mailchimp Transactional *is* the Mandrill
API, just rebranded, and the same API key works). Grab an API key from
Mailchimp Transactional → Settings → API Keys, then set:

```
DJANGO_EMAIL_BACKEND=anymail.backends.mandrill.EmailBackend
MAILCHIMP_API_KEY=your-api-key
DEFAULT_FROM_EMAIL=Irritec Seguimiento <no-reply@example.com>
SITE_URL=https://your-deployed-domain.example.com   # used to build links in emails
```

The from-address domain needs to be a verified sending domain in your
Mailchimp Transactional account, or sends will be rejected. Staff users need
a real `email` on their account (set in the admin or via `createsuperuser`)
to receive the "new request" notification — accounts with a blank email are
silently skipped.

## Deploying to Render

The repo has a `render.yaml` Blueprint (Postgres + a Python web service).

1. Push this repo to GitHub, then in Render: **New → Blueprint**, point it at
   the repo. Render reads `render.yaml` and provisions the database and web
   service together.
2. It auto-generates `DJANGO_SECRET_KEY` and wires `DATABASE_URL` from the
   new Postgres instance. Everything else with `sync: false` in
   `render.yaml` has no safe default — set these for real in the service's
   **Environment** tab before the first deploy does anything useful:
   - `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`
   - `MAILCHIMP_API_KEY`, `DEFAULT_FROM_EMAIL` (a verified sending domain in
     Mailchimp Transactional)
3. First deploy runs migrations as part of the build (see `buildCommand` in
   `render.yaml`). Once it's live, create the assistant's login from the
   Render shell (**Shell** tab on the service, or `render ssh`):
   ```bash
   uv run python manage.py createsuperuser
   ```
4. `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, and `SITE_URL` (used to build the
   links inside emails) all pick up Render's `*.onrender.com` hostname
   automatically — nothing to configure there unless you attach a custom
   domain, in which case set `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`,
   and `SITE_URL` explicitly to that domain.

No manual setup for uploads or static files: Cloudinary already handles
user-uploaded files (see above), and static assets (`static/tracker/`) are
served straight off the web dyno by WhiteNoise — no separate static host or
CDN needed.

**Before this is used for real data**, two things in `render.yaml` are
placeholders worth revisiting:
- The Postgres plan is `free`, which **expires after 90 days** and gets
  deleted — bump it to a paid plan before that matters.
- `SECURE_HSTS_SECONDS` is intentionally left unset (see the comment in
  `config/settings.py`) — enable it once the domain (custom or
  `*.onrender.com`) is final, since browsers hold onto it for the duration
  you set.

If you'd rather set the service up by hand instead of via Blueprint, the
equivalent manual config is:
- **Build command**: `pip install uv && uv sync --frozen && uv run python manage.py collectstatic --noinput && uv run python manage.py migrate`
- **Start command**: `uv run gunicorn config.wsgi:application`
- A managed Postgres instance, with its connection string set as `DATABASE_URL`
- The same env vars listed above, plus `DJANGO_SECRET_KEY` and `DJANGO_DEBUG=False`

## Notes

- `static/tracker/img/irritec-logo.png` is the real Irritec logo, cropped to
  just the wordmark (the "don't wait for rain" tagline underneath the
  original artwork was dropped — it's illegible at the ~26-30px heights
  every placement uses it at).
- Elapsed-time KPIs are measured in wall-clock time, not working hours
  (no calendar of holidays/weekends is modeled yet).
- `tracker/kpi.py` computes the monthly scorecard live from the data — there's
  no separate "locked" snapshot once a month closes.
