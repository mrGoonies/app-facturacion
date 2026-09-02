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

## Notes

- `static/tracker/img/irritec-logo.svg` is a placeholder text wordmark in the
  brand green (`#046648`) — swap in the real Irritec logo file when available
  and update the `<img>` references in the templates if the filename changes.
- Elapsed-time KPIs are measured in wall-clock time, not working hours
  (no calendar of holidays/weekends is modeled yet).
- `tracker/kpi.py` computes the monthly scorecard live from the data — there's
  no separate "locked" snapshot once a month closes.
