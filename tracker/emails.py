"""Notification emails for the purchase-request flow.

EMAIL_BACKEND defaults to the console backend (see config/settings.py), so
locally these just print instead of sending — no mail server required to
develop against this.

Every step that hands the request over to the other side sends an email, so
neither the requester nor the assistant has to keep checking the app to find
out it's their turn.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.urls import reverse


def _absolute_url(path: str) -> str:
    return f"{settings.SITE_URL.rstrip('/')}{path}"


def _detail_url(pr) -> str:
    return _absolute_url(reverse("tracker:purchase_detail", args=[pr.pk]))


def _staff_emails() -> list[str]:
    return list(
        get_user_model()
        .objects.filter(is_staff=True, is_active=True)
        .exclude(email="")
        .values_list("email", flat=True)
    )


def _send_to_requester(pr, subject, message):
    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[pr.requester_email],
    )


def _send_to_staff(subject, message):
    staff_emails = _staff_emails()
    if not staff_emails:
        return
    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=staff_emails,
    )


def _items_summary(pr) -> str:
    return "\n".join(
        f"- {item.quantity} {item.unit} {item.description}".strip()
        for item in pr.items.all()
    )


def _quotes_summary(pr) -> str:
    lines = []
    for q in pr.quotes.all():
        total = (
            # CLP has no minor unit, so no decimals for it.
            f"{q.currency} {q.total_amount:,.{0 if q.currency == 'CLP' else 2}f}"
            if q.total_amount
            else "monto no capturado"
        )
        lead = (
            f"entrega en {q.lead_time_days} días"
            if q.lead_time_days
            else "tiempo de entrega no capturado"
        )
        line = f"- {q.supplier_name} — {total}, {lead}"
        if q.quote_pdf:
            line += f"\n  Ver cotización (PDF): {q.quote_pdf.url}"
        lines.append(line)
    return "\n".join(lines)


def send_purchase_request_created_emails(pr):
    """Notifies the requester their request was received, and every staff
    user who can work it in the panel that a new one is waiting."""
    status_url = _absolute_url(pr.get_status_url())
    _send_to_requester(
        pr,
        f"Recibimos tu solicitud de compra ({pr.display_ref})",
        f"Hola {pr.requester_name},\n\n"
        f"Recibimos tu solicitud de compra {pr.display_ref}:\n\n"
        f"{_items_summary(pr)}\n\n"
        f"Puedes seguir su estado aquí:\n{status_url}\n",
    )
    _send_to_staff(
        f"Nueva solicitud de compra: {pr.display_ref} · {pr.department}",
        f"{pr.requester_name} ({pr.department}) solicitó lo siguiente — "
        f"urgencia: {pr.get_urgency_display()}\n\n"
        f"{_items_summary(pr)}\n\n"
        f"Se necesita antes del {pr.needed_by:%d/%m/%Y}.\n\n"
        f"Gestionar la solicitud:\n{_detail_url(pr)}\n",
    )


def send_quotes_collected_email(pr, reminder=False):
    """Tells the requester what quotes were collected once the assistant is
    done shopping around, and asks them to confirm which one they want
    before the PO gets issued — the flow's one real approval checkpoint.
    `reminder` re-sends it when the requester hasn't answered yet."""
    status_url = _absolute_url(pr.get_status_url())
    subject = (
        f"Recordatorio: elige tu cotización ({pr.display_ref})"
        if reminder
        else f"Cotizaciones recibidas para tu solicitud ({pr.display_ref})"
    )
    intro = (
        f"Tu solicitud {pr.display_ref} sigue esperando que elijas una cotización:"
        if reminder
        else f"Reunimos estas cotizaciones para tu solicitud {pr.display_ref}:"
    )
    _send_to_requester(
        pr,
        subject,
        f"Hola {pr.requester_name},\n\n"
        f"{intro}\n\n"
        f"{_quotes_summary(pr)}\n\n"
        f"Elige tu cotización aquí — en cuanto la elijas emitimos la orden de compra:\n"
        f"{status_url}\n\n"
        f"Si ninguna te sirve, desde el mismo link puedes pedir otras.\n",
    )


def send_quote_confirmed_email(pr):
    """The requester picked a quote — the PO is now the assistant's move."""
    quote = pr.selected_quote
    _send_to_staff(
        f"{pr.display_ref}: lista para emitir OC",
        f"{pr.requester_name} eligió la cotización de {quote.supplier_name} "
        f"para {pr.display_ref}.\n\n"
        f"Emitir la orden de compra:\n{_detail_url(pr)}\n",
    )


def send_quotes_rejected_email(pr, reason):
    _send_to_staff(
        f"{pr.display_ref}: el solicitante pidió otras cotizaciones",
        f"{pr.requester_name} revisó las cotizaciones de {pr.display_ref} "
        f"y ninguna le sirve. Su comentario:\n\n{reason}\n\n"
        f"Ver la solicitud:\n{_detail_url(pr)}\n",
    )


def send_po_issued_email(pr):
    quote = pr.selected_quote
    pdf_line = f"\nVer la orden de compra (PDF): {pr.po_pdf.url}\n" if pr.po_pdf else ""
    _send_to_requester(
        pr,
        f"Orden de compra emitida ({pr.display_ref})",
        f"Hola {pr.requester_name},\n\n"
        f"Emitimos la orden de compra {pr.po_number} con "
        f"{quote.supplier_name if quote else 'el proveedor elegido'} "
        f"para tu solicitud {pr.display_ref}.\n{pdf_line}\n"
        f"Cuando recibas los artículos, confírmalo aquí para cerrar la solicitud:\n"
        f"{_absolute_url(pr.get_status_url())}\n",
    )


def send_request_closed_email(pr):
    _send_to_requester(
        pr,
        f"Solicitud cerrada ({pr.display_ref})",
        f"Hola {pr.requester_name},\n\n"
        f"Cerramos tu solicitud de compra {pr.display_ref}.\n\n"
        f"Detalle:\n{_absolute_url(pr.get_status_url())}\n",
    )


def send_request_cancelled_email(pr, by_requester=False, reason=""):
    """Tells the other side: staff when the requester cancelled, the
    requester when the assistant did."""
    reason_text = f"\nMotivo: {reason}\n" if reason else ""
    if by_requester:
        _send_to_staff(
            f"{pr.display_ref}: cancelada por el solicitante",
            f"{pr.requester_name} canceló su solicitud {pr.display_ref}.\n"
            f"{reason_text}\nVer la solicitud:\n{_detail_url(pr)}\n",
        )
    else:
        _send_to_requester(
            pr,
            f"Solicitud cancelada ({pr.display_ref})",
            f"Hola {pr.requester_name},\n\n"
            f"Tu solicitud de compra {pr.display_ref} fue cancelada.\n"
            f"{reason_text}\nDetalle:\n{_absolute_url(pr.get_status_url())}\n",
        )


def send_new_message_email(pr, activity):
    """Relays a timeline message to the other side of the conversation."""
    if activity.kind == activity.Kind.REQUESTER_MESSAGE:
        _send_to_staff(
            f"{pr.display_ref}: nuevo mensaje de {activity.author}",
            f"{activity.author} escribió sobre {pr.display_ref}:\n\n"
            f"{activity.message}\n\n"
            f"Responder desde el panel:\n{_detail_url(pr)}\n",
        )
    else:
        _send_to_requester(
            pr,
            f"Mensaje de Compras sobre tu solicitud ({pr.display_ref})",
            f"Hola {pr.requester_name},\n\n"
            f"{activity.author} (Compras) te escribió:\n\n"
            f"{activity.message}\n\n"
            f"Responder:\n{_absolute_url(pr.get_status_url())}\n",
        )
