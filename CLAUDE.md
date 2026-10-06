# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Internal Django 6.1 tool for Irritec that tracks the KPIs behind an administrative assistant's monthly bonus. It covers two flows: **purchasing** (request → quotes → PO) and **invoicing** (logistics hand-off of picking lists → in process → invoiced, plus billing errors). It has one app, `tracker`, and `config` is the project package. All user-facing text (labels, choices, messages, emails) is in **Spanish** (`LANGUAGE_CODE='es-cl'`, `TIME_ZONE='America/Santiago'`). Code, comments and docstrings are in English.

## Commands

Python 3.14 is managed with `uv`. Local development uses SQLite (`db.sqlite3` at the repo root, gitignored). Production on Render uses PostgreSQL through `DATABASE_URL`. Keep models and queries database-agnostic: don't use `django.contrib.postgres` or other Postgres-only features.

```bash
uv sync
uv run python manage.py migrate
uv run python manage.py runserver            # / = public landing, /panel/ = assistant workspace
uv run python manage.py makemigrations tracker
uv run python manage.py test                 # all tests (tracker/tests.py is currently empty)
uv run python manage.py test tracker.tests.SomeTestCase.test_method   # single test
```

No linter or formatter is configured.

Settings come from env vars through `python-decouple` (a local `.env` is gitignored). Setting `DATABASE_URL` switches the app from SQLite to that database. Deployment is defined in `render.yaml`. Its build command runs `collectstatic` and `migrate`.

## Architecture

**Two access tiers, one URL namespace (`tracker:`)**
- Public, no login: `solicitudes/nueva/` creates a purchase request. `solicitudes/<uuid:token>/` is the requester's status page, reachable only through the unguessable `PurchaseRequest.token`. `logistica/entrega/` is the logistics hand-off form.
- Staff, `@login_required`: everything under `panel/`. That covers the unified queue, purchase detail, picking list detail and KPI scorecard.

**State machines live in views as POST `action=` dispatch.** `purchase_detail` and `picking_list_detail` in `tracker/views.py` each handle one form POST with a hidden `action` field. The actions are `add_quote`, `select_quote`, `send_quotes_to_requester`, `issue_po`, `close_request`, `cancel_request`, and `mark_in_process`, `issue_invoice`, `report_error`, `correct_error`, `dispute_error`. Each action sets the status and timestamp fields and then redirects. Status changes on a purchase request also append a `PurchaseActivity` row, which is the timeline shown on both the staff and public pages. When you add a transition, keep the timestamps and the activity log consistent. The KPIs are computed from those timestamps.

**KPI timing fields matter.** Each KPI is measured between two timestamps:
- PO KPI: `PurchaseRequest.created_at → po_issued_at`
- Invoicing KPIs: `PickingList.handed_off_at → in_process_at` and `→ invoiced_at`

Attribution is through `handled_by`. It is set to `request.user` on `issue_po`, `mark_in_process` and `issue_invoice`, and the scorecard filters by it. Elapsed time is wall-clock time, not business hours.

**Where KPI rules live:**
- `config/settings.py` → `KPI_SETTINGS`: the targets (48h/2h/8h/2%), weights, bonus threshold and base bonus, all overridable with `KPI_*` env vars. Don't hardcode these numbers.
- Model properties (`is_po_on_time`, `is_in_process_on_time`, `is_invoice_on_time`) are the single source of the "on time?" check.
- `tracker/kpi.py` → `compute_scorecard(year, month, user)` builds the monthly scorecard live from those properties. There are no stored snapshots. Only non-disputed `BillingError`s with `attributable_to=ASSISTANT` count against the bonus.
- The queue view (`queue`) computes a "time left" for each row against the same targets and sorts rows overdue first.

**Integrations:**
- Uploaded files go to Cloudinary through `CloudinaryField`. The default storage is `MediaCloudinaryStorage`, and supplier quote PDFs use `resource_type="raw"`. Static files go through WhiteNoise. In `INSTALLED_APPS`, `django.contrib.staticfiles` must stay *before* `cloudinary_storage` (see the comment in settings).
- Email: `tracker/emails.py` uses plain-text `send_mail`. It uses the console backend locally and Mailchimp Transactional in production, through anymail's `mandrill` backend. Absolute links are built from `SITE_URL`. "New request" notifications go to every active `is_staff` user who has a non-blank email.

**Templates/UI:** project-level `templates/` (not app dirs). `base.html` → `tracker/_app_base.html` is the staff shell: sidebar nav highlighted through the `active_nav` context var, which each panel view passes. Styling comes from `static/tracker/css/app.css`. Forms use `StyledFormMixin` (`tracker/forms.py`) to add the `.input` class automatically. Duration display filters (`hm`, `hours_only`) are in `tracker/templatetags/tracker_extras.py`. Model docstrings and comments refer to "design screen 1a–1g". Those are the original mockups the pages were built from.

**Display refs are derived, not stored:** `PR-{2400+pk}` (`display_ref`). PO and invoice numbers are generated in the views (`PO-{2000+pk}`, `F-{20000+pk}` as the invoice fallback).
