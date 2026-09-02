from dataclasses import dataclass
from datetime import date

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .emails import send_purchase_request_created_emails, send_quotes_collected_email
from .forms import (
    BillingErrorForm, BrandedAuthenticationForm, LogisticsHandoffForm,
    PurchaseRequestForm, PurchaseRequestItemFormSet, SupplierQuoteForm,
)
from .kpi import compute_scorecard
from .models import BillingError, PickingList, PickingListBatch, PurchaseRequest, SupplierQuote


class BrandedLoginView(LoginView):
    template_name = "tracker/login.html"
    authentication_form = BrandedAuthenticationForm


# ---------------------------------------------------------------- public ---

def purchase_request_create(request):
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
    else:
        form = PurchaseRequestForm()
        formset = PurchaseRequestItemFormSet()
    return render(request, "tracker/purchase_request_form.html", {"form": form, "formset": formset})


def request_status(request, token):
    pr = get_object_or_404(PurchaseRequest, token=token)
    return render(request, "tracker/request_status.html", {"pr": pr})


def logistics_handoff_create(request):
    if request.method == "POST":
        form = LogisticsHandoffForm(request.POST)
        if form.is_valid():
            batch = form.save()
            now = timezone.now()
            for number in form.cleaned_data["list_numbers"]:
                PickingList.objects.update_or_create(
                    number=number,
                    defaults={
                        "batch": batch,
                        "handed_off_at": now,
                    },
                )
            messages.success(request, f"Se entregaron {len(form.cleaned_data['list_numbers'])} listas.")
            return redirect("tracker:logistics_handoff")
    else:
        form = LogisticsHandoffForm(initial={"shipped_on": date.today()})

    recent_batches = (
        PickingListBatch.objects.prefetch_related("lists", "lists__errors").order_by("-created_at")[:7]
    )
    return render(
        request, "tracker/logistics_handoff_form.html",
        {"form": form, "recent_batches": recent_batches},
    )


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


@login_required
def queue(request):
    now = timezone.now()
    view_filter = request.GET.get("view", "all")

    open_requests = PurchaseRequest.objects.filter(
        status__in=[
            PurchaseRequest.Status.REQUESTED, PurchaseRequest.Status.QUOTING,
            PurchaseRequest.Status.AWAITING_CONFIRMATION,
        ]
    ).prefetch_related("items")
    open_lists = PickingList.objects.exclude(
        status__in=[PickingList.Status.INVOICED]
    ).exclude(status=PickingList.Status.CORRECTED).select_related("batch").prefetch_related("errors")

    rows = []
    for r in open_requests:
        remaining = r.po_target_hours - (now - r.created_at).total_seconds() / 3600
        label, css = _time_left(remaining)
        rows.append(QueueRow(
            ref=r.display_ref, kind="purchase",
            summary=", ".join(i.description for i in r.items.all()[:2]) or "Solicitud de compra",
            origin=f"{r.requester_name} · {r.department}",
            received=r.created_at.strftime("%d %b %H:%M"),
            time_left_label=label, time_left_class=css,
            status_label=r.get_status_display(), status_class="tag-accent",
            url=reverse("tracker:purchase_detail", args=[r.pk]),
        ))

    for pl in open_lists:
        if pl.open_error:
            label, css = "Corrección pendiente", "tag-overdue"
            status_label, status_class = "Error", "tag-error"
        elif pl.status == PickingList.Status.NOT_STARTED:
            remaining = pl.in_process_target_hours - (now - pl.handed_off_at).total_seconds() / 3600
            label, css = _time_left(remaining)
            status_label, status_class = "Sin iniciar", "tag-neutral"
        else:
            remaining = pl.invoice_target_hours - (now - pl.handed_off_at).total_seconds() / 3600
            label, css = _time_left(remaining)
            status_label, status_class = "En proceso", "tag-accent"
        rows.append(QueueRow(
            ref=pl.number, kind="invoicing",
            summary="Lista de picking",
            origin=f"Lote del {pl.batch.shipped_on:%d %b}",
            received=pl.handed_off_at.strftime("%d %b %H:%M"),
            time_left_label=label, time_left_class=css,
            status_label=status_label, status_class=status_class,
            url=reverse("tracker:picking_list_detail", args=[pl.number]),
        ))

    def sort_key(row):
        return {"tag-overdue": 0, "tag-error": 0, "tag-due-soon": 1}.get(row.time_left_class, 2)
    rows.sort(key=sort_key)

    if view_filter == "purchases":
        rows = [r for r in rows if r.kind == "purchase"]
    elif view_filter == "invoicing":
        rows = [r for r in rows if r.kind == "invoicing"]

    stats = {
        "awaiting_quotes": open_requests.filter(status=PurchaseRequest.Status.REQUESTED).count(),
        "ready_to_issue": SupplierQuote.objects.filter(selected=True, request__status=PurchaseRequest.Status.AWAITING_CONFIRMATION).count(),
        "lists_to_invoice": open_lists.exclude(status=PickingList.Status.ERROR).count(),
        "errors_to_correct": BillingError.objects.filter(corrected_at__isnull=True, disputed=False).count(),
    }

    active_nav = {"purchases": "purchasing", "invoicing": "invoicing"}.get(view_filter, "queue")
    return render(request, "tracker/queue.html", {
        "rows": rows, "stats": stats, "view_filter": view_filter,
        "today": now, "active_nav": active_nav,
    })


@login_required
def purchase_detail(request, pk):
    pr = get_object_or_404(PurchaseRequest, pk=pk)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add_quote":
            quote_form = SupplierQuoteForm(request.POST, request.FILES)
            if quote_form.is_valid():
                quote = quote_form.save(commit=False)
                quote.request = pr
                quote.save()
                pr.status = PurchaseRequest.Status.QUOTING
                pr.save()
                pr.activities.create(message=f"Cotización recibida de {quote.supplier_name}")
            else:
                messages.error(request, "Revisa la cotización — falta adjuntar el PDF o algún dato no es válido.")
        elif action == "select_quote":
            quote_id = request.POST.get("quote_id")
            pr.quotes.update(selected=False)
            quote = get_object_or_404(SupplierQuote, pk=quote_id, request=pr)
            quote.selected = True
            quote.save()
            pr.activities.create(message=f"Cotización seleccionada de {quote.supplier_name}")
        elif action == "send_quotes_to_requester":
            if not pr.quotes.exists():
                messages.error(request, "Agrega al menos una cotización antes de enviarla al solicitante.")
            else:
                send_quotes_collected_email(pr)
                pr.status = PurchaseRequest.Status.AWAITING_CONFIRMATION
                pr.quotes_sent_at = timezone.now()
                pr.save()
                pr.activities.create(message=f"Cotizaciones enviadas a {pr.requester_name} para confirmación")
        elif action == "issue_po":
            selected = pr.quotes.filter(selected=True).first()
            if pr.status != PurchaseRequest.Status.AWAITING_CONFIRMATION:
                messages.error(request, "Envía las cotizaciones al solicitante y espera su confirmación antes de emitir la orden de compra.")
            elif not selected:
                messages.error(request, "Selecciona una cotización antes de emitir la orden de compra.")
            else:
                pr.status = PurchaseRequest.Status.PO_ISSUED
                pr.po_issued_at = timezone.now()
                pr.po_number = f"PO-{2000 + pr.pk}"
                pr.handled_by = request.user
                pr.save()
                pr.activities.create(message=f"Orden de compra {pr.po_number} emitida")
        elif action == "close_request":
            pr.status = PurchaseRequest.Status.CLOSED
            pr.closed_at = timezone.now()
            pr.save()
            pr.activities.create(message="Solicitud cerrada")
        elif action == "cancel_request":
            pr.status = PurchaseRequest.Status.CANCELLED
            pr.closed_at = timezone.now()
            pr.save()
            pr.activities.create(message="Solicitud cancelada")
        return redirect("tracker:purchase_detail", pk=pk)

    quote_form = SupplierQuoteForm()
    return render(request, "tracker/purchase_detail.html", {"pr": pr, "quote_form": quote_form, "active_nav": "purchasing"})


@login_required
def picking_list_detail(request, number):
    pl = get_object_or_404(PickingList, number=number)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "mark_in_process":
            pl.status = PickingList.Status.IN_PROCESS
            pl.in_process_at = timezone.now()
            pl.handled_by = request.user
            pl.save()
        elif action == "issue_invoice":
            invoice_number = request.POST.get("invoice_number") or f"F-{20000 + pl.pk}"
            pl.invoice_number = invoice_number
            pl.invoiced_at = timezone.now()
            pl.status = PickingList.Status.INVOICED
            pl.handled_by = request.user
            pl.save()
        elif action == "report_error":
            error_form = BillingErrorForm(request.POST)
            if error_form.is_valid():
                err = error_form.save(commit=False)
                err.picking_list = pl
                err.invoice_number = err.invoice_number or pl.invoice_number
                err.save()
                pl.status = PickingList.Status.ERROR
                pl.save()
        elif action == "correct_error":
            error_id = request.POST.get("error_id")
            err = get_object_or_404(BillingError, pk=error_id, picking_list=pl)
            err.corrected_at = timezone.now()
            err.save()
            pl.status = PickingList.Status.CORRECTED
            pl.save()
        elif action == "dispute_error":
            error_id = request.POST.get("error_id")
            err = get_object_or_404(BillingError, pk=error_id, picking_list=pl)
            err.disputed = True
            err.save()
            pl.status = PickingList.Status.INVOICED
            pl.save()
        return redirect("tracker:picking_list_detail", number=number)

    error_form = BillingErrorForm(initial={"invoice_number": pl.invoice_number})
    return render(request, "tracker/picking_list_detail.html", {"pl": pl, "error_form": error_form, "active_nav": "invoicing"})


@login_required
def kpi_scorecard(request):
    from django.utils.dates import MONTHS_3

    local_now = timezone.localtime(timezone.now())
    year = int(request.GET.get("year", local_now.year))
    month = int(request.GET.get("month", local_now.month))
    card = compute_scorecard(year, month, user=request.user)

    months = [(local_now.year, m, MONTHS_3[m]) for m in range(1, local_now.month + 1)]
    return render(request, "tracker/kpi_scorecard.html", {
        "card": card, "months": months, "year": year, "month": month, "active_nav": "kpi",
        "po_target_hours": settings.KPI_SETTINGS["PO_TARGET_HOURS"],
    })
