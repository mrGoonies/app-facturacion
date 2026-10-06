"""Follows up on requests whose quotes are waiting on the requester, so the
assistant doesn't have to chase them by hand. Meant to run hourly (Render
cron job, see render.yaml):

- after PURCHASE_AUTO_REMIND_HOURS without an answer, re-send the quotes once
  per round (a manual "Recordar" from the panel counts as that reminder);
- after PURCHASE_AUTO_CANCEL_DAYS, cancel the request and tell both sides.

Rounds are measured from `quotes_sent_at`, so re-sending quotes after the
requester asks for others starts the count again.
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from tracker.emails import (
    send_quotes_collected_email,
    send_request_auto_cancelled_email,
)
from tracker.models import PurchaseRequest


class Command(BaseCommand):
    help = "Recuerda o cancela solicitudes cuyas cotizaciones esperan al solicitante."

    def handle(self, *args, **options):
        now = timezone.now()
        remind_hours = settings.PURCHASE_AUTO_REMIND_HOURS
        cancel_days = settings.PURCHASE_AUTO_CANCEL_DAYS
        reminded = cancelled = 0

        waiting = PurchaseRequest.objects.filter(
            status=PurchaseRequest.Status.AWAITING_CONFIRMATION,
            confirmed_at__isnull=True,
            quotes_sent_at__isnull=False,
        )
        for pr in waiting:
            waited = now - pr.quotes_sent_at
            if cancel_days and waited >= timedelta(days=cancel_days):
                pr.end_requester_wait(now)
                pr.status = PurchaseRequest.Status.CANCELLED
                pr.closed_at = now
                pr.save()
                pr.activities.create(
                    message=f"Cancelada automáticamente: sin respuesta en {cancel_days} días"
                )
                send_request_auto_cancelled_email(pr, cancel_days)
                cancelled += 1
            elif (
                remind_hours
                and waited >= timedelta(hours=remind_hours)
                and (pr.reminded_at is None or pr.reminded_at < pr.quotes_sent_at)
            ):
                send_quotes_collected_email(pr, reminder=True)
                pr.reminded_at = now
                pr.save(update_fields=["reminded_at"])
                pr.activities.create(
                    message=f"Recordatorio automático enviado a {pr.requester_name}"
                )
                reminded += 1

        self.stdout.write(f"Recordatorios: {reminded} · Canceladas: {cancelled}")
