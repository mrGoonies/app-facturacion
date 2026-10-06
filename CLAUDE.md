# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Internal Django 6.1 tool for Irritec that tracks the KPIs behind an administrative assistant's monthly bonus. It covers two flows: **purchasing** (request → quotes → requester confirms → PO) and **invoicing** (logistics hand-off of picking lists → in process → invoiced, plus billing errors). It has one app, `tracker`, and `config` is the project package. All user-facing text (labels, choices, messages, emails) is in **Spanish** (`LANGUAGE_CODE='es-cl'`, `TIME_ZONE='America/Santiago'`). Code, comments and docstrings are in English.

## Commands

Python 3.14 is managed with `uv`. Local development uses SQLite (`db.sqlite3` at the repo root, gitignored). Production on Render uses PostgreSQL through `DATABASE_URL`. Keep models and queries database-agnostic: don't use `django.contrib.postgres` or other Postgres-only features.

```bash
uv sync
uv run python manage.py migrate
uv run python manage.py createsuperuser      # staff login for /panel/
uv run python manage.py runserver            # / = public landing, /panel/ = assistant workspace
uv run python manage.py makemigrations tracker
uv run python manage.py test tracker         # all tests (tracker/tests.py)
uv run python manage.py test tracker.tests.OutcomeTests.test_untouched_list_past_target_is_late   # single test
uv run ruff format .                         # formatter (default config, no [tool.ruff] section)
uv run ruff check .
```

Tests that render templates must use the `PLAIN_STATIC` `override_settings` decorator in `tracker/tests.py`. Production's `CompressedManifestStaticFilesStorage` fails without a prior `collectstatic`.

Settings come from env vars through `python-decouple` (a local `.env` is gitignored). Setting `DATABASE_URL` switches the app from SQLite to that database. Deployment is defined in `render.yaml` (Blueprint: Postgres + web service). Its build command runs `collectstatic` and `migrate`. `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` and `SITE_URL` pick up `RENDER_EXTERNAL_HOSTNAME` automatically.

## Architecture

**Two access tiers, one URL namespace (`tracker:`)**
- Public, no login: `solicitudes/nueva/` creates a purchase request. `solicitudes/<uuid:token>/` is the requester's status page, reachable only through the unguessable `PurchaseRequest.token`. The requester acts on their own request there: `confirm_quote` (sets `confirmed_at`), `reject_quotes` (back to `QUOTING`, reason required), `cancel_request` (before the PO only), `confirm_receipt` (`PO_ISSUED` → `CLOSED`) and `post_message`. `solicitudes/<uuid:token>/repetir/` opens the new-request form pre-filled from that request. `logistica/entrega/` is the logistics hand-off form.
- Staff, `@login_required`: everything under `panel/`. That covers the unified queue, purchase detail, picking list detail and KPI scorecard.

**State machines live in views as POST `action=` dispatch.** `request_status`, `purchase_detail` and `picking_list_detail` in `tracker/views.py` each handle one form POST with a hidden `action` field:
- Purchase (staff): `add_quote` / `delete_quote` (while quoting) → `send_quotes_to_requester` (needs `KPI_SETTINGS["MIN_QUOTES"]` quotes, emails the requester) → requester's `confirm_quote` (`remind_requester` re-sends meanwhile) → `issue_po` (needs `ready_to_issue_po`; the assistant types the real ERP PO number, optional PO PDF) → requester's `confirm_receipt`, or staff `close_request` as a fallback. `cancel_request` is allowed only before the PO (`can_cancel`). `post_message` on either side adds a message to the timeline. Sending fewer than `MIN_QUOTES` quotes needs a `single_source_reason`, which the requester sees.
- Scheduled: `manage.py process_stale_purchase_requests` (hourly Render cron job in `render.yaml`) reminds the requester after `PURCHASE_AUTO_REMIND_HOURS` (once per round; a manual reminder counts) and cancels after `PURCHASE_AUTO_CANCEL_DAYS`. `0` disables either.
- Picking list: `mark_in_process`, `issue_invoice`, `report_error`, `correct_error`, `dispute_error`.

Each action guards the current status, sets the status and timestamp fields, and redirects. Every purchase transition also appends a `PurchaseActivity` row (`pr.activities.create(...)`). That row is the timeline shown on both the staff and public pages; rows with `kind` other than `EVENT` are messages between requester and staff (`author` holds who wrote it). Every hand-over between the two sides sends an email (`tracker/emails.py`), so neither has to poll the app. When you add a transition, keep the guards, timestamps and activity log consistent, because the KPIs are computed from those timestamps.

**KPI timing.** Each KPI is measured between two timestamps, in wall-clock time (not business hours):
- PO KPI: `PurchaseRequest.created_at → po_issued_at`, **minus the time the quotes sat with the requester** (`paused_time()`: banked rounds in `requester_wait_time` plus the open one since `quotes_sent_at`; toggle `KPI_SETTINGS["PO_PAUSE_WHILE_AWAITING_REQUESTER"]`). Use `po_elapsed()` / `po_hours_left()` rather than subtracting timestamps. Any transition that ends a wait on the requester (confirm, reject, cancel) must call `end_requester_wait(now)` *before* changing status. The target depends on `urgency` (`po_target_hours`, from `PO_TARGET_HOURS_BY_URGENCY`).
- Invoicing KPIs: `PickingList.handed_off_at → in_process_at` and `→ invoiced_at`.

The models expose two layers. `is_*_on_time` properties only judge finished work. `po_outcome()` / `in_process_outcome()` / `invoice_outcome()` return `True` (on time), `False` (late, including unfinished work past its deadline) or `None` (still pending inside the target). The scorecard uses the outcome methods so that pending items are left out and overdue untouched items count as late. Keep this contract when you add KPIs.

`handled_by` is set to `request.user` on `issue_po`, `mark_in_process` and `issue_invoice`. `compute_scorecard` accepts an optional `user` filter, but the `kpi_scorecard` view calls it without one, so the scorecard counts all of the month's work.

**Where KPI rules live:**
- `config/settings.py` → `KPI_SETTINGS`: per-urgency PO targets, the in-process/invoice hour targets, error-rate target, on-time rate targets, weights, bonus threshold, attainment target and base bonus. All of them can be overridden with `KPI_*` env vars. Don't hardcode these numbers.
- Model properties and outcome methods (above) are the single source of the "on time?" check. `tracker/kpi.py` reuses them instead of redoing the hour math.
- `tracker/kpi.py` → `compute_scorecard(year, month, user=None)` builds the monthly scorecard live. There are no stored snapshots. Cancelled requests are excluded. Only non-disputed `BillingError`s with `attributable_to=ASSISTANT` (`counts_against_bonus`) count against the bonus.
- The `queue` view computes a "time left" for each row against the same targets. Purchase rows show whose turn it is (`PurchaseRequest.staff_status_label`). Sorting: rows the assistant can act on first (not waiting on the requester or on delivery), "Línea detenida" first within those, then overdue / ready-to-issue → due soon → the rest.

**Integrations:**
- Uploads go to Cloudinary through `CloudinaryField`. The default storage is `MediaCloudinaryStorage`, and supplier quote PDFs use `resource_type="raw"`. Credentials come from a single `CLOUDINARY_URL` (`cloudinary://key:secret@cloud`), which `settings.py` parses into `CLOUDINARY_STORAGE`. If it's unset, uploads fail but the rest of the app runs. Static files go through WhiteNoise. In `INSTALLED_APPS`, `django.contrib.staticfiles` must stay *before* `cloudinary_storage` (see the comment in settings).
- Email: `tracker/emails.py` sends plain-text mail with `send_mail`. Locally it uses the console backend. Production uses Mailchimp Transactional through anymail's `mandrill` backend (`MAILCHIMP_API_KEY`). Absolute links are built from `SITE_URL`. "New request" notifications go to every active `is_staff` user who has a non-blank email. `issue_po` also emails `PURCHASE_ACCOUNTING_EMAILS` (comma-separated env var; empty = off) with the PO PDF and the chosen quote PDF downloaded from Cloudinary and attached (`send_po_to_accounting`); if a download fails the email carries the link instead. While that variable is set, `IssuePOForm` requires the PO PDF.

**Templates/UI:** project-level `templates/` (not app dirs). `base.html` → `tracker/_app_base.html` is the staff shell: sidebar nav highlighted through the `active_nav` context var, which each panel view passes. Styling comes from `static/tracker/css/app.css`. Forms use `StyledFormMixin` (`tracker/forms.py`) to add the `.input` class automatically. Duration display filters (`hm`, `hours_only`) are in `tracker/templatetags/tracker_extras.py`. Because of `es-cl` localization, floats render with a comma. Use `|unlocalize` when a value feeds CSS or `<input type="date">`. Model docstrings and comments refer to "design screen 1a–1g". Those are the original mockups the pages were built from.

**Display refs are derived, not stored:** `PR-{2400+pk}` (`display_ref`). PO and invoice numbers are typed in by the assistant (they come from the ERP), never generated.

## In-progress work

`docs/plan-correcciones-ux.md` (Spanish) is the active fix plan from end-user testing, organized as blocks A–H with one PR per block. Its "decisiones de negocio ya tomadas" are settled, so don't relitigate them. Each fix there is expected to ship with at least one test.
