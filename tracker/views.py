import re
from dataclasses import dataclass
from datetime import date

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .emails import (
    send_new_message_email,
    send_picking_lists_handed_off_email,
    send_po_issued_email,
    send_po_to_accounting,
    send_purchase_request_created_emails,
    send_quote_confirmed_email,
    send_quotes_collected_email,
    send_quotes_rejected_email,
    send_request_cancelled_email,
    send_request_closed_email,
)
from .forms import (
    BillingErrorForm,
    BrandedAuthenticationForm,
    IssuePOForm,
    LogisticsHandoffForm,
    PurchaseRequestForm,
    PurchaseRequestItemFormSet,
    SupplierQuoteForm,
    normalize_list_number,
)
from .kpi import compute_scorecard
from .models import (
    BillingError,
    PickingList,
    PickingListBatch,
    PurchaseActivity,
    PurchaseRequest,
    SupplierQuote,
)

# Timeline messages are plain text; cap them so one paste can't flood the
# timeline or the notification email.
MESSAGE_MAX_LENGTH = 2000


class BrandedLoginView(LoginView):
    template_name = "tracker/login.html"
    authentication_form = BrandedAuthenticationForm


# ---------------------------------------------------------------- public ---


def purchase_request_create(request, token=None):
    """New purchase request form. Reached through `token` ("Repetir esta
    solicitud" on the status page), it starts pre-filled with that earlier
    request so recurring purchases don't have to be typed again."""
    source = get_object_or_404(PurchaseRequest, token=token) if token else None
    if request.method == "POST":
        form = PurchaseRequestForm(request.POST, request.FILES)
        formset = PurchaseRequestItemFormSet(request.POST, request.FILES)
        if form.is_valid() and formset.is_valid():
            pr = form.save()
            formset.instance = pr
            formset.save()
            pr.activities.create(message="Solicitud recibida")
            send_purchase_request_created_emails(pr)
            return redirect(pr.get_status_url())
    elif source:
        form = PurchaseRequestForm(
            initial={
                "requester_name": source.requester_name,
                "requester_email": source.requester_email,
                "department": source.department,
                "justification": source.justification,
                "urgency": source.urgency,
            }
        )
        items = [
            {"description": i.description, "quantity": i.quantity, "unit": i.unit}
            for i in source.items.all()
        ]
        formset = PurchaseRequestItemFormSet(initial=items)
        # Enough extra rows for every copied item, plus one blank.
        formset.extra = max(formset.extra, len(items))
    else:
        form = PurchaseRequestForm()
        formset = PurchaseRequestItemFormSet()
    return render(
        request,
        "tracker/purchase_request_form.html",
        {"form": form, "formset": formset, "source": source},
    )


def _message_body(request):
    return request.POST.get("body", "").strip()[:MESSAGE_MAX_LENGTH]


def request_status(request, token):
    """The requester's page. Besides showing progress, it lets them act on
    their own request without going through the assistant: pick a quote or
    ask for others, cancel before the PO, confirm receipt, and write to
    purchasing."""
    pr = get_object_or_404(PurchaseRequest, token=token)
    if request.method != "POST":
        return render(request, "tracker/request_status.html", {"pr": pr})

    action = request.POST.get("action")
    if action == "confirm_quote":
        if not pr.awaiting_requester:
            messages.error(request, "Esta solicitud ya no acepta confirmaciones.")
        else:
            quote = get_object_or_404(
                SupplierQuote, pk=request.POST.get("quote_id"), request=pr
            )
            pr.quotes.update(selected=False)
            quote.selected = True
            quote.save(update_fields=["selected"])
            now = timezone.now()
            pr.end_requester_wait(now)
            pr.confirmed_at = now
            pr.save(update_fields=["confirmed_at", "requester_wait_time"])
            pr.activities.create(
                message=f"{pr.requester_name} eligió la cotización de {quote.supplier_name}"
            )
            send_quote_confirmed_email(pr)
            messages.success(
                request, "Listo. Compras emitirá la orden de compra con esa cotización."
            )
    elif action == "reject_quotes":
        reason = _message_body(request)
        if not pr.awaiting_requester:
            messages.error(
                request, "Esta solicitud ya no acepta cambios de cotización."
            )
        elif not reason:
            messages.error(
                request, "Cuéntanos por qué no te sirven para buscar mejores."
            )
        else:
            pr.end_requester_wait(timezone.now())
            pr.status = PurchaseRequest.Status.QUOTING
            pr.save(update_fields=["status", "requester_wait_time"])
            pr.activities.create(
                message=f"{pr.requester_name} pidió otras cotizaciones"
            )
            pr.activities.create(
                kind=PurchaseActivity.Kind.REQUESTER_MESSAGE,
                author=pr.requester_name,
                message=reason,
            )
            send_quotes_rejected_email(pr, reason)
            messages.success(
                request, "Le avisamos a Compras que busque otras cotizaciones."
            )
    elif action == "cancel_request":
        reason = _message_body(request)
        if not pr.can_cancel:
            messages.error(
                request,
                "Ya se emitió la orden de compra; escríbele a Compras para cancelarla.",
            )
        else:
            pr.closed_at = timezone.now()
            pr.end_requester_wait(pr.closed_at)
            pr.status = PurchaseRequest.Status.CANCELLED
            pr.save(update_fields=["status", "closed_at", "requester_wait_time"])
            pr.activities.create(message=f"Cancelada por {pr.requester_name}")
            if reason:
                pr.activities.create(
                    kind=PurchaseActivity.Kind.REQUESTER_MESSAGE,
                    author=pr.requester_name,
                    message=reason,
                )
            send_request_cancelled_email(pr, by_requester=True, reason=reason)
            messages.success(request, "Cancelaste la solicitud.")
    elif action == "confirm_receipt":
        if pr.status != PurchaseRequest.Status.PO_ISSUED:
            messages.error(request, "Esta solicitud no está esperando recepción.")
        else:
            pr.status = PurchaseRequest.Status.CLOSED
            pr.closed_at = timezone.now()
            pr.save(update_fields=["status", "closed_at"])
            pr.activities.create(
                message=f"{pr.requester_name} confirmó que recibió los artículos"
            )
            messages.success(request, "Gracias. La solicitud quedó cerrada.")
    elif action == "post_message":
        body = _message_body(request)
        if not pr.is_open:
            messages.error(request, "Esta solicitud ya está cerrada.")
        elif not body:
            messages.error(request, "Escribe un mensaje antes de enviarlo.")
        else:
            activity = pr.activities.create(
                kind=PurchaseActivity.Kind.REQUESTER_MESSAGE,
                author=pr.requester_name,
                message=body,
            )
            send_new_message_email(pr, activity)
            messages.success(request, "Mensaje enviado a Compras.")
    return redirect(pr.get_status_url())


def logistics_handoff_create(request):
    if request.method == "POST":
        form = LogisticsHandoffForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                batch = form.save()
                now = timezone.now()
                lists = [
                    PickingList.objects.create(
                        number=number, batch=batch, handed_off_at=now
                    )
                    for number in form.cleaned_data["list_numbers"]
                ]
            send_picking_lists_handed_off_email(lists)
            messages.success(
                request,
                f"Se entregaron {len(lists)} listas. Le avisamos a facturación.",
            )
            if form.skipped_numbers:
                messages.warning(
                    request,
                    f"Omitimos {', '.join(form.skipped_numbers)}: ya estaban registradas.",
                )
            return redirect("tracker:logistics_handoff")
    else:
        form = LogisticsHandoffForm(initial={"shipped_on": timezone.localdate()})

    recent_batches = PickingListBatch.objects.prefetch_related(
        "lists", "lists__errors"
    ).order_by("-created_at")[:7]
    # Untouched lists are the ones logistics can still take back.
    not_started = PickingList.objects.filter(
        status=PickingList.Status.NOT_STARTED
    ).order_by("-handed_off_at", "number")
    return render(
        request,
        "tracker/logistics_handoff_form.html",
        {
            "form": form,
            "recent_batches": recent_batches,
            "not_started": not_started,
            "in_process_target_hours": settings.KPI_SETTINGS["IN_PROCESS_TARGET_HOURS"],
        },
    )


@require_POST
def logistics_list_discard(request, number):
    """Lets logistics take back a mistyped number before invoicing starts
    on it; otherwise it would sit in the queue and count as late."""
    pl = get_object_or_404(PickingList, number=number)
    if pl.discard():
        messages.success(request, f"Quitamos la lista {number}.")
    else:
        messages.error(
            request,
            f"Facturación ya empezó a trabajar la lista {number}; avísales para corregirla.",
        )
    return redirect("tracker:logistics_handoff")


# -------------------------------------------------------------- internal ---


@dataclass
class QueueRow:
    ref: str
    kind: str  # "purchase" | "invoicing"
    summary: str
    origin: str
    received: str
    time_left_label: str
    time_left_class: str
    status_label: str
    status_class: str
    url: str
    # Rows waiting on someone else (the requester, or the supplier's
    # delivery) sink below the ones the assistant can act on right now.
    actionable: bool = True
    line_stopped: bool = False
    ready_to_issue_po: bool = False
    # Picking lists only: the queue's bulk take/invoice controls.
    pk: int | None = None
    can_take: bool = False
    can_invoice: bool = False


def _format_hm(hours):
    total_minutes = round(hours * 60)
    h, m = divmod(total_minutes, 60)
    return f"{h} h {m:02d} m" if h else f"{m} m"


def _time_left(remaining_hours):
    """Maps hours-remaining-to-target to a (label, css class) pair matching
    the design's three status colours: on-time / due-soon / overdue."""
    if remaining_hours is None:
        return "—", "tag-neutral"
    if remaining_hours < 0:
        return f"Vencido hace {_format_hm(abs(remaining_hours))}", "tag-overdue"
    if remaining_hours <= 4:
        return _format_hm(remaining_hours), "tag-due-soon"
    return f"{remaining_hours:.0f} h", "tag-on-time"


def _find_by_ref(query):
    """URL of the picking list or purchase request a search names exactly,
    whatever its status — so "how is PL-8836 going?" is one search away."""
    number = normalize_list_number(query)
    if PickingList.objects.filter(number=number).exists():
        return reverse("tracker:picking_list_detail", args=[number])
    match = re.fullmatch(r"PR-?(\d+)", number)
    if match:
        pr = PurchaseRequest.objects.filter(pk=int(match[1]) - 2400).first()
        if pr:
            return reverse("tracker:purchase_detail", args=[pr.pk])
    return None


@login_required
def queue(request):
    now = timezone.now()
    view_filter = request.GET.get("view", "all")
    query = request.GET.get("q", "").strip()
    if query:
        found = _find_by_ref(query)
        if found:
            return redirect(found)

    # PO-issued requests stay listed until received, so they're followed up
    # instead of vanishing from sight the moment the PO goes out.
    open_requests = PurchaseRequest.objects.filter(
        status__in=[
            PurchaseRequest.Status.REQUESTED,
            PurchaseRequest.Status.QUOTING,
            PurchaseRequest.Status.AWAITING_CONFIRMATION,
            PurchaseRequest.Status.PO_ISSUED,
        ]
    ).prefetch_related("items")
    open_lists = (
        PickingList.objects.exclude(status__in=[PickingList.Status.INVOICED])
        .exclude(status=PickingList.Status.CORRECTED)
        .select_related("batch")
        .prefetch_related("errors")
    )

    rows = []
    for r in open_requests:
        if r.status == PurchaseRequest.Status.PO_ISSUED:
            label, css = "—", "tag-neutral"
        else:
            label, css = _time_left(r.po_hours_left(now))
            if r.is_paused:
                label = f"En pausa · {label}"
        status_label = r.staff_status_label
        if r.awaiting_requester and r.quotes_sent_at:
            waited = (now - r.quotes_sent_at).total_seconds() / 3600
            status_label += f" · hace {_format_hm(waited)}"
        if r.ready_to_issue_po:
            status_class = "tag-outline"
        elif r.awaiting_requester or r.status == PurchaseRequest.Status.PO_ISSUED:
            status_class = "tag-neutral"
        else:
            status_class = "tag-accent"
        rows.append(
            QueueRow(
                ref=r.display_ref,
                kind="purchase",
                summary=", ".join(i.description for i in r.items.all()[:2])
                or "Solicitud de compra",
                origin=f"{r.requester_name} · {r.department}",
                received=r.created_at.strftime("%d %b %H:%M"),
                time_left_label=label,
                time_left_class=css,
                status_label=status_label,
                status_class=status_class,
                url=reverse("tracker:purchase_detail", args=[r.pk]),
                actionable=not (
                    r.awaiting_requester or r.status == PurchaseRequest.Status.PO_ISSUED
                ),
                line_stopped=r.urgency == PurchaseRequest.Urgency.LINE_STOPPED,
                ready_to_issue_po=r.ready_to_issue_po,
            )
        )

    for pl in open_lists:
        if pl.open_error:
            label, css = "Corrección pendiente", "tag-overdue"
            status_label, status_class = "Error", "tag-error"
        elif pl.status == PickingList.Status.NOT_STARTED:
            remaining = (
                pl.in_process_target_hours
                - (now - pl.handed_off_at).total_seconds() / 3600
            )
            label, css = _time_left(remaining)
            status_label, status_class = "Sin iniciar", "tag-neutral"
        else:
            remaining = (
                pl.invoice_target_hours
                - (now - pl.handed_off_at).total_seconds() / 3600
            )
            label, css = _time_left(remaining)
            status_label, status_class = "En proceso", "tag-accent"
        rows.append(
            QueueRow(
                ref=pl.number,
                kind="invoicing",
                summary="Lista de picking",
                origin=f"Lote del {pl.batch.shipped_on:%d %b}",
                received=pl.handed_off_at.strftime("%d %b %H:%M"),
                time_left_label=label,
                time_left_class=css,
                status_label=status_label,
                status_class=status_class,
                url=reverse("tracker:picking_list_detail", args=[pl.number]),
                pk=pl.pk,
                can_take=pl.can_take,
                can_invoice=pl.can_invoice,
            )
        )

    def sort_key(row):
        # Actionable first; within that "Línea detenida" leads, then
        # overdue → due soon → the rest. A confirmed request ready for its
        # PO is one click from done, so it ranks with the overdue ones.
        if row.ready_to_issue_po:
            urgency = 0
        else:
            urgency = {"tag-overdue": 0, "tag-error": 0, "tag-due-soon": 1}.get(
                row.time_left_class, 2
            )
        return (not row.actionable, not row.line_stopped, urgency)

    rows.sort(key=sort_key)

    if view_filter == "purchases":
        rows = [r for r in rows if r.kind == "purchase"]
    elif view_filter == "invoicing":
        rows = [r for r in rows if r.kind == "invoicing"]
    if query:
        needle = query.upper()
        rows = [r for r in rows if needle in f"{r.ref} {r.summary} {r.origin}".upper()]

    stats = {
        "awaiting_quotes": open_requests.filter(
            status=PurchaseRequest.Status.REQUESTED
        ).count(),
        "ready_to_issue": open_requests.filter(
            status=PurchaseRequest.Status.AWAITING_CONFIRMATION,
            confirmed_at__isnull=False,
        ).count(),
        "lists_to_invoice": open_lists.exclude(status=PickingList.Status.ERROR).count(),
        "errors_to_correct": BillingError.objects.filter(
            corrected_at__isnull=True, disputed=False
        ).count(),
    }

    active_nav = {"purchases": "purchasing", "invoicing": "invoicing"}.get(
        view_filter, "queue"
    )
    return render(
        request,
        "tracker/queue.html",
        {
            "rows": rows,
            "stats": stats,
            "view_filter": view_filter,
            "query": query,
            "today": now,
            "active_nav": active_nav,
        },
    )


def _render_purchase_detail(request, pr, quote_form=None, po_form=None):
    return render(
        request,
        "tracker/purchase_detail.html",
        {
            "pr": pr,
            "quote_form": quote_form or SupplierQuoteForm(),
            "po_form": po_form or IssuePOForm(),
            "min_quotes": settings.KPI_SETTINGS["MIN_QUOTES"],
            "active_nav": "purchasing",
        },
    )


@login_required
def purchase_detail(request, pk):
    pr = get_object_or_404(PurchaseRequest, pk=pk)
    if request.method != "POST":
        return _render_purchase_detail(request, pr)

    action = request.POST.get("action")
    if action == "add_quote":
        quote_form = SupplierQuoteForm(request.POST, request.FILES)
        if not quote_form.is_valid():
            # Re-render instead of redirecting so nothing typed is lost and
            # each error shows next to its field.
            messages.error(request, "Revisa la cotización: hay campos con errores.")
            return _render_purchase_detail(request, pr, quote_form=quote_form)
        quote = quote_form.save(commit=False)
        quote.request = pr
        quote.save()
        pr.status = PurchaseRequest.Status.QUOTING
        pr.save()
        pr.activities.create(message=f"Cotización recibida de {quote.supplier_name}")
        messages.success(request, f"Cotización de {quote.supplier_name} agregada.")
    elif action == "delete_quote":
        quote = get_object_or_404(
            SupplierQuote, pk=request.POST.get("quote_id"), request=pr
        )
        if pr.status not in (
            PurchaseRequest.Status.REQUESTED,
            PurchaseRequest.Status.QUOTING,
        ):
            messages.error(
                request, "Solo puedes eliminar cotizaciones antes de enviarlas."
            )
        else:
            quote.delete()
            if not pr.quotes.exists():
                pr.status = PurchaseRequest.Status.REQUESTED
                pr.save()
            pr.activities.create(
                message=f"Cotización de {quote.supplier_name} eliminada"
            )
            messages.success(request, f"Cotización de {quote.supplier_name} eliminada.")
    elif action == "send_quotes_to_requester":
        min_quotes = settings.KPI_SETTINGS["MIN_QUOTES"]
        quote_count = pr.quotes.count()
        # Fewer quotes than the minimum are allowed only with a written
        # reason (a single supplier, say) — it's shown to the requester and
        # kept on the request for review.
        reason = request.POST.get("single_source_reason", "").strip()[
            :MESSAGE_MAX_LENGTH
        ]
        if pr.status != PurchaseRequest.Status.QUOTING:
            messages.error(request, "Esta solicitud no está en etapa de cotización.")
        elif not quote_count:
            messages.error(request, "Agrega al menos una cotización antes de enviarla.")
        elif quote_count < min_quotes and not reason:
            messages.error(
                request,
                f"Agrega al menos {min_quotes} cotizaciones, o explica por qué no hay más proveedores.",
            )
        else:
            pr.quotes.update(selected=False)
            pr.status = PurchaseRequest.Status.AWAITING_CONFIRMATION
            pr.quotes_sent_at = timezone.now()
            pr.single_source_reason = reason if quote_count < min_quotes else ""
            pr.save()
            send_quotes_collected_email(pr)
            pr.activities.create(
                message=f"Cotizaciones enviadas a {pr.requester_name} para confirmación"
            )
            if pr.single_source_reason:
                pr.activities.create(
                    message=f"Enviada con {quote_count} de {min_quotes} cotizaciones mínimas: {pr.single_source_reason}"
                )
            messages.success(request, f"Cotizaciones enviadas a {pr.requester_name}.")
    elif action == "remind_requester":
        if not pr.awaiting_requester:
            messages.error(request, "Esta solicitud no está esperando al solicitante.")
        else:
            send_quotes_collected_email(pr, reminder=True)
            pr.reminded_at = timezone.now()
            pr.save(update_fields=["reminded_at"])
            pr.activities.create(message=f"Recordatorio enviado a {pr.requester_name}")
            messages.success(request, f"Recordatorio enviado a {pr.requester_name}.")
    elif action == "issue_po":
        if not pr.ready_to_issue_po or not pr.selected_quote:
            messages.error(
                request,
                "Envía las cotizaciones al solicitante y espera que elija una antes de emitir la orden de compra.",
            )
        else:
            po_form = IssuePOForm(request.POST, request.FILES, instance=pr)
            if not po_form.is_valid():
                return _render_purchase_detail(request, pr, po_form=po_form)
            pr = po_form.save(commit=False)
            pr.status = PurchaseRequest.Status.PO_ISSUED
            pr.po_issued_at = timezone.now()
            pr.handled_by = request.user
            pr.save()
            pr.activities.create(message=f"Orden de compra {pr.po_number} emitida")
            send_po_issued_email(pr)
            send_po_to_accounting(pr)
            notified = pr.requester_name
            if settings.PURCHASE_ACCOUNTING_EMAILS:
                notified += " y a contabilidad"
            messages.success(
                request, f"OC {pr.po_number} emitida. Le avisamos a {notified}."
            )
    elif action == "close_request":
        if pr.status != PurchaseRequest.Status.PO_ISSUED:
            messages.error(request, "Solo puedes cerrar una solicitud con OC emitida.")
        else:
            pr.status = PurchaseRequest.Status.CLOSED
            pr.closed_at = timezone.now()
            pr.save()
            pr.activities.create(message="Solicitud cerrada")
            send_request_closed_email(pr)
            messages.success(request, "Solicitud cerrada.")
    elif action == "cancel_request":
        if not pr.can_cancel:
            messages.error(
                request, "No se puede cancelar una solicitud con OC emitida."
            )
        else:
            pr.closed_at = timezone.now()
            pr.end_requester_wait(pr.closed_at)
            pr.status = PurchaseRequest.Status.CANCELLED
            pr.save()
            pr.activities.create(message="Solicitud cancelada")
            send_request_cancelled_email(pr)
            messages.success(request, "Solicitud cancelada.")
    elif action == "post_message":
        body = _message_body(request)
        if not pr.is_open:
            messages.error(request, "Esta solicitud ya está cerrada.")
        elif not body:
            messages.error(request, "Escribe un mensaje antes de enviarlo.")
        else:
            activity = pr.activities.create(
                kind=PurchaseActivity.Kind.STAFF_MESSAGE,
                author=request.user.get_full_name() or request.user.get_username(),
                message=body,
            )
            send_new_message_email(pr, activity)
            messages.success(request, f"Mensaje enviado a {pr.requester_name}.")
    return redirect("tracker:purchase_detail", pk=pk)


INVOICE_NUMBER_MAX_LENGTH = PickingList._meta.get_field("invoice_number").max_length


def _invoice_number(request, field="invoice_number"):
    """The typed invoice number, or None (with an error message) when it's
    missing or longer than the field allows."""
    value = request.POST.get(field, "").strip()
    if not value:
        messages.error(request, "Ingresa el número de factura.")
        return None
    if len(value) > INVOICE_NUMBER_MAX_LENGTH:
        messages.error(
            request,
            f"El número de factura admite hasta {INVOICE_NUMBER_MAX_LENGTH} caracteres.",
        )
        return None
    return value


def _render_picking_list_detail(request, pl, error_form=None):
    return render(
        request,
        "tracker/picking_list_detail.html",
        {
            "pl": pl,
            "error_form": error_form
            or BillingErrorForm(initial={"invoice_number": pl.invoice_number}),
            "active_nav": "invoicing",
        },
    )


@login_required
def picking_list_detail(request, number):
    pl = get_object_or_404(PickingList, number=number)
    if request.method != "POST":
        return _render_picking_list_detail(request, pl)

    action = request.POST.get("action")
    if action == "mark_in_process":
        if pl.mark_in_process(request.user):
            messages.success(request, f"{pl.number} en proceso.")
        else:
            messages.error(request, "Esta lista ya estaba en proceso o facturada.")
    elif action == "issue_invoice":
        invoice_number = _invoice_number(request)
        if invoice_number and pl.issue_invoice(invoice_number, request.user):
            messages.success(request, f"{pl.number} facturada con {invoice_number}.")
        elif invoice_number:
            messages.error(request, "Esta lista ya estaba facturada.")
    elif action == "discard_list":
        if pl.discard():
            messages.success(request, f"Quitamos la lista {number}.")
            return redirect(reverse("tracker:queue") + "?view=invoicing")
        messages.error(request, "Solo puedes quitar listas sin iniciar.")
    elif action == "report_error":
        if not pl.invoiced_at or pl.open_error:
            messages.error(
                request,
                "Solo puedes reportar errores en listas facturadas sin otro error abierto.",
            )
        else:
            error_form = BillingErrorForm(request.POST)
            if not error_form.is_valid():
                # Re-render so what was typed isn't lost.
                messages.error(request, "Revisa el reporte: hay campos con errores.")
                return _render_picking_list_detail(request, pl, error_form=error_form)
            err = error_form.save(commit=False)
            err.picking_list = pl
            err.invoice_number = err.invoice_number or pl.invoice_number
            err.save()
            pl.status = PickingList.Status.ERROR
            pl.save(update_fields=["status"])
            messages.success(request, "Error reportado.")
    elif action in ("correct_error", "dispute_error"):
        err = get_object_or_404(
            BillingError, pk=request.POST.get("error_id"), picking_list=pl
        )
        if not err.is_open:
            messages.error(request, "Este error ya estaba cerrado.")
        elif action == "correct_error":
            new_invoice = request.POST.get("corrected_invoice_number", "").strip()
            if len(new_invoice) > INVOICE_NUMBER_MAX_LENGTH:
                messages.error(
                    request,
                    f"El número de factura admite hasta {INVOICE_NUMBER_MAX_LENGTH} caracteres.",
                )
                return redirect("tracker:picking_list_detail", number=number)
            err.corrected_at = timezone.now()
            err.corrected_invoice_number = new_invoice
            err.save(update_fields=["corrected_at", "corrected_invoice_number"])
            pl.status = PickingList.Status.CORRECTED
            pl.invoice_number = new_invoice or pl.invoice_number
            pl.save(update_fields=["status", "invoice_number"])
            messages.success(request, "Error marcado como corregido.")
        else:
            err.disputed = True
            err.save(update_fields=["disputed"])
            pl.status = PickingList.Status.INVOICED
            pl.save(update_fields=["status"])
            messages.success(request, "Error disputado: no cuenta para el bono.")
    return redirect("tracker:picking_list_detail", number=number)


@login_required
@require_POST
def picking_list_bulk(request):
    """The queue's bulk actions on the selected lists: take them (→ in
    process), or invoice them. One shared invoice number covers every
    selected list; a single row's form sends just its own list."""
    action = request.POST.get("action")
    lists = list(
        PickingList.objects.filter(pk__in=request.POST.getlist("lists")).order_by(
            "number"
        )
    )
    back = redirect(reverse("tracker:queue") + "?view=invoicing")
    if not lists:
        messages.error(request, "Selecciona al menos una lista.")
        return back

    now = timezone.now()
    if action == "take":
        done = [pl.number for pl in lists if pl.mark_in_process(request.user, now)]
        verb = "en proceso"
    elif action == "invoice":
        invoice_number = _invoice_number(request)
        if not invoice_number:
            return back
        done = [
            pl.number
            for pl in lists
            if pl.issue_invoice(invoice_number, request.user, now)
        ]
        verb = f"facturada{'s' if len(done) != 1 else ''} con {invoice_number}"
    else:
        return back

    if done:
        messages.success(request, f"{', '.join(done)} {verb}.")
    skipped = [pl.number for pl in lists if pl.number not in done]
    if skipped:
        messages.warning(
            request,
            f"Sin cambios en {', '.join(skipped)}: ya habían avanzado de estado.",
        )
    return back


@login_required
def kpi_scorecard(request):
    from django.utils.dates import MONTHS_3

    local_now = timezone.localtime(timezone.now())
    try:
        year = int(request.GET.get("year", local_now.year))
        month = int(request.GET.get("month", local_now.month))
        if not 1 <= month <= 12:
            raise ValueError
    except TypeError, ValueError:
        year, month = local_now.year, local_now.month
    card = compute_scorecard(year, month)

    months = [(local_now.year, m, MONTHS_3[m]) for m in range(1, local_now.month + 1)]
    return render(
        request,
        "tracker/kpi_scorecard.html",
        {
            "card": card,
            "months": months,
            "year": year,
            "month": month,
            "active_nav": "kpi",
            "po_target_hours": settings.KPI_SETTINGS["PO_TARGET_HOURS"],
            "currency": settings.DEFAULT_CURRENCY,
        },
    )
