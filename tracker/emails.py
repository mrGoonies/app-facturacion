"""Notification emails for the purchase-request flow.

EMAIL_BACKEND defaults to the console backend (see config/settings.py), so
locally these just print instead of sending — no mail server required to
develop against this.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.urls import reverse


def _absolute_url(path: str) -> str:
    return f"{settings.SITE_URL.rstrip('/')}{path}"


def _items_summary(pr) -> str:
    return "\n".join(f"- {item.quantity} {item.unit} {item.description}".strip() for item in pr.items.all())


def _quotes_summary(pr) -> str:
    lines = []
    for q in pr.quotes.all():
        total = f"{q.currency} {q.total_amount:,.2f}" if q.total_amount else "monto no capturado"
        lead = f"entrega en {q.lead_time_days} días" if q.lead_time_days else "tiempo de entrega no capturado"
        line = f"- {q.supplier_name} — {total}, {lead}"
        if q.quote_pdf:
            line += f"\n  Ver cotización (PDF): {q.quote_pdf.url}"
        lines.append(line)
    return "\n".join(lines)


def send_purchase_request_created_emails(pr):
    """Notifies the requester their request was received, and every staff
    user who can work it in the panel that a new one is waiting."""
    status_url = _absolute_url(pr.get_status_url())
    send_mail(
        subject=f"Recibimos tu solicitud de compra ({pr.display_ref})",
        message=(
            f"Hola {pr.requester_name},\n\n"
            f"Recibimos tu solicitud de compra {pr.display_ref}:\n\n"
            f"{_items_summary(pr)}\n\n"
            f"Puedes seguir su estado aquí:\n{status_url}\n"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[pr.requester_email],
    )

    staff_emails = list(
        get_user_model().objects
        .filter(is_staff=True, is_active=True)
        .exclude(email="")
        .values_list("email", flat=True)
    )
    if not staff_emails:
        return

    detail_url = _absolute_url(reverse("tracker:purchase_detail", args=[pr.pk]))
    send_mail(
        subject=f"Nueva solicitud de compra: {pr.display_ref} · {pr.department}",
        message=(
            f"{pr.requester_name} ({pr.department}) solicitó lo siguiente — "
            f"urgencia: {pr.get_urgency_display()}\n\n"
            f"{_items_summary(pr)}\n\n"
            f"Se necesita antes del {pr.needed_by:%d/%m/%Y}.\n\n"
            f"Gestionar la solicitud:\n{detail_url}\n"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=staff_emails,
    )


def send_quotes_collected_email(pr):
    """Tells the requester what quotes were collected once the assistant is
    done shopping around, and asks them to confirm which one they want
    before the PO gets issued — the flow's one real approval checkpoint."""
    status_url = _absolute_url(pr.get_status_url())
    send_mail(
        subject=f"Cotizaciones recibidas para tu solicitud ({pr.display_ref})",
        message=(
            f"Hola {pr.requester_name},\n\n"
            f"Reunimos estas cotizaciones para tu solicitud {pr.display_ref}:\n\n"
            f"{_quotes_summary(pr)}\n\n"
            f"Responde a este correo o contáctanos para confirmarnos cuál prefieres — "
            f"en cuanto tengamos tu confirmación emitimos la orden de compra.\n\n"
            f"Puedes ver el detalle completo aquí:\n{status_url}\n"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[pr.requester_email],
    )
