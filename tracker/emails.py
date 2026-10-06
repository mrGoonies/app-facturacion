"""Notification emails for the purchase-request flow.

EMAIL_BACKEND defaults to the console backend (see config/settings.py), so
locally these just print instead of sending — no mail server required to
develop against this.

Every step that hands the request over to the other side sends an email, so
neither the requester nor the assistant has to keep checking the app to find
out it's their turn.
"""

import logging
from urllib.request import urlopen

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMessage, send_mail
from django.urls import reverse
from django.utils import timezone
from django.utils.text import get_valid_filename

logger = logging.getLogger(__name__)

# Per file — issuing a PO waits on these downloads, so keep it bounded.
ATTACHMENT_TIMEOUT_SECONDS = 15


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
    single_source = (
        f"Solo conseguimos {pr.quotes.count()} cotización(es): {pr.single_source_reason}\n\n"
        if pr.single_source_reason
        else ""
    )
    deadline = ""
    if pr.auto_cancel_at:
        cancel_at = timezone.localtime(pr.auto_cancel_at)
        deadline = (
            f"\nSi no eliges antes del {cancel_at:%d/%m a las %H:%M}, "
            f"la solicitud se cancelará automáticamente.\n"
        )
    _send_to_requester(
        pr,
        subject,
        f"Hola {pr.requester_name},\n\n"
        f"{intro}\n\n"
        f"{_quotes_summary(pr)}\n\n"
        f"{single_source}"
        f"Elige tu cotización aquí — en cuanto la elijas emitimos la orden de compra:\n"
        f"{status_url}\n\n"
        f"Si ninguna te sirve, desde el mismo link puedes pedir otras.\n"
        f"{deadline}",
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


def _fetch_file(resource):
    """Downloads an uploaded Cloudinary file so it can be attached.
    Returns (url, content); either is None when it can't be had — the
    email then carries the link, or a note, instead of failing the PO."""
    try:
        url = resource.url
    except ValueError:  # Cloudinary not configured
        logger.exception("No se pudo obtener la URL de %s", resource)
        return None, None
    try:
        with urlopen(url, timeout=ATTACHMENT_TIMEOUT_SECONDS) as response:
            return url, response.read()
    except OSError:
        logger.exception("No se pudo descargar %s para adjuntarlo", url)
        return url, None


def send_po_to_accounting(pr):
    """Sends accounting (PURCHASE_ACCOUNTING_EMAILS) the issued PO with the
    PO PDF and the quote the requester chose attached."""
    recipients = settings.PURCHASE_ACCOUNTING_EMAILS
    if not recipients:
        return
    quote = pr.selected_quote
    issued_at = timezone.localtime(pr.po_issued_at)
    total = (
        f"{quote.currency} {quote.total_amount:,.{0 if quote.currency == 'CLP' else 2}f}"
        if quote and quote.total_amount
        else "no capturado"
    )
    message = EmailMessage(
        subject=f"OC {pr.po_number} emitida · {quote.supplier_name if quote else pr.display_ref}",
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
    )

    documents = [
        ("Orden de compra", f"{pr.po_number}.pdf", pr.po_pdf),
        (
            "Cotización elegida",
            f"Cotizacion {quote.supplier_name}.pdf" if quote else "",
            quote.quote_pdf if quote else None,
        ),
    ]
    document_lines = []
    for label, filename, resource in documents:
        if not resource:
            document_lines.append(f"- {label}: no se subió a la app")
            continue
        url, content = _fetch_file(resource)
        if content is not None:
            message.attach(get_valid_filename(filename), content, "application/pdf")
            document_lines.append(f"- {label}: adjunta")
        elif url:
            document_lines.append(
                f"- {label}: no se pudo adjuntar, descárgala aquí: {url}"
            )
        else:
            document_lines.append(f"- {label}: no disponible")

    message.body = (
        f"Se emitió la orden de compra {pr.po_number} el {issued_at:%d/%m/%Y %H:%M}.\n\n"
        f"Proveedor: {quote.supplier_name if quote else '—'}\n"
        f"Total cotizado: {total}\n"
        f"Condiciones de pago: {(quote.payment_terms if quote else '') or '—'}\n"
        f"Solicitud: {pr.display_ref} · {pr.requester_name} ({pr.department})\n\n"
        f"Artículos:\n{_items_summary(pr)}\n\n"
        f"Documentos:\n" + "\n".join(document_lines) + "\n"
    )
    message.send()


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


def send_request_auto_cancelled_email(pr, days):
    """Both sides hear about it: nobody chose to cancel this one."""
    _send_to_requester(
        pr,
        f"Solicitud cancelada por falta de respuesta ({pr.display_ref})",
        f"Hola {pr.requester_name},\n\n"
        f"Cancelamos tu solicitud {pr.display_ref} porque pasaron {days} días "
        f"sin que eligieras una cotización.\n\n"
        f"Si todavía lo necesitas, puedes repetirla desde aquí:\n"
        f"{_absolute_url(reverse('tracker:purchase_request_repeat', args=[pr.token]))}\n",
    )
    _send_to_staff(
        f"{pr.display_ref}: cancelada automáticamente",
        f"{pr.display_ref} ({pr.requester_name}) se canceló porque pasaron "
        f"{days} días sin que el solicitante eligiera cotización.\n\n"
        f"Ver la solicitud:\n{_detail_url(pr)}\n",
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
