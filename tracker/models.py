import uuid

from cloudinary.models import CloudinaryField
from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone


class PurchaseRequest(models.Model):
    """A public "what do you need us to buy?" request (design screen 1a).

    No login is required to create one — the requester is identified by the
    name/email they type in, and can later be re-identified via `token`.
    """

    class Urgency(models.TextChoices):
        STANDARD = "standard", "Estándar — 5 días hábiles"
        PRIORITY = "priority", "Prioritaria — 48 horas"
        LINE_STOPPED = "line_stopped", "Línea detenida"

    class Status(models.TextChoices):
        REQUESTED = "requested", "Solicitada"
        QUOTING = "quoting", "Cotizando"
        AWAITING_CONFIRMATION = "awaiting_confirmation", "Esperando confirmación"
        PO_ISSUED = "po_issued", "Orden de compra emitida"
        CLOSED = "closed", "Cerrada"
        CANCELLED = "cancelled", "Cancelada"

    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    requester_name = models.CharField(max_length=120)
    requester_email = models.EmailField()
    department = models.CharField(max_length=120)
    needed_by = models.DateField()
    justification = models.TextField(blank=True, verbose_name="¿Para qué se necesita?")
    urgency = models.CharField(max_length=20, choices=Urgency.choices, default=Urgency.STANDARD)
    reference_image = CloudinaryField(
        "imagen", folder="purchase_requests", blank=True, null=True,
        help_text="Foto de referencia del artículo o la necesidad (opcional).",
    )

    status = models.CharField(max_length=25, choices=Status.choices, default=Status.REQUESTED)
    po_number = models.CharField(max_length=40, blank=True)

    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="purchase_requests_handled",
        help_text="La asistente que emitió la orden de compra — cuenta para su KPI.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    quotes_sent_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Cuándo se le enviaron al solicitante las cotizaciones recopiladas para su confirmación.",
    )
    po_issued_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"PR-{self.pk} · {self.requester_name}"

    @property
    def display_ref(self):
        return f"PR-{2400 + self.pk}" if self.pk else "PR-new"

    def get_status_url(self):
        return reverse("tracker:request_status", args=[self.token])

    @property
    def selected_quote(self):
        return self.quotes.filter(selected=True).first()

    @property
    def time_to_po(self):
        end = self.po_issued_at or timezone.now()
        return end - self.created_at

    @property
    def po_target_hours(self):
        return settings.KPI_SETTINGS["PO_TARGET_HOURS"]

    @property
    def is_po_on_time(self):
        if not self.po_issued_at:
            return None
        hours = (self.po_issued_at - self.created_at).total_seconds() / 3600
        return hours <= self.po_target_hours

    @property
    def is_open(self):
        return self.status not in (self.Status.CLOSED, self.Status.CANCELLED)

    # No "Solicitada" step here on purpose: the assistant's stepper starts
    # at "Cotizando" straight away, since a freshly-requested purchase is
    # already hers to start quoting — there's nothing distinct to show for
    # the moment before the first quote lands. The `requested` status itself
    # still exists on the model (see Status above) — it's what the public
    # request_status page and the queue's "awaiting_quotes" stat key off of.
    STATUS_STEPS = [
        (Status.QUOTING, "Cotizando"),
        (Status.AWAITING_CONFIRMATION, "Esperando confirmación"),
        (Status.PO_ISSUED, "OC emitida"),
        (Status.CLOSED, "Cerrada"),
    ]

    @property
    def status_steps(self):
        """Ordered steps with a done/current/upcoming state each, so the
        stepper on the purchase detail page can distinguish "already passed"
        from "not reached yet" instead of only highlighting the active step."""
        order = [key for key, _ in self.STATUS_STEPS]
        # `requested` isn't one of the visible steps — treat it as the start
        # of "Cotizando" rather than leaving the whole stepper unhighlighted.
        effective_status = self.Status.QUOTING if self.status == self.Status.REQUESTED else self.status
        try:
            current_index = order.index(effective_status)
        except ValueError:
            current_index = -1
        steps = []
        for i, (key, label) in enumerate(self.STATUS_STEPS):
            if i < current_index:
                state = "done"
            elif i == current_index:
                state = "current"
            else:
                state = "upcoming"
            steps.append({"key": key, "label": label, "state": state})
        return steps


class PurchaseRequestItem(models.Model):
    request = models.ForeignKey(PurchaseRequest, related_name="items", on_delete=models.CASCADE)
    description = models.CharField(max_length=255)
    quantity = models.PositiveIntegerField()
    unit = models.CharField(max_length=40, blank=True)
    reference_image = CloudinaryField(
        "imagen", folder="purchase_request_items", blank=True, null=True,
        help_text="Foto de referencia de este artículo (opcional).",
    )

    def __str__(self):
        return f"{self.quantity} {self.unit} — {self.description}"


class SupplierQuote(models.Model):
    request = models.ForeignKey(PurchaseRequest, related_name="quotes", on_delete=models.CASCADE)
    supplier_name = models.CharField(max_length=150)
    quote_pdf = CloudinaryField(
        "documento", resource_type="raw", folder="supplier_quotes", null=True,
        help_text="PDF de la cotización del proveedor.",
    )
    # Optional now that the PDF is the source of truth — kept for the
    # side-by-side comparison table, filled in only if the assistant wants
    # a quick read of the totals without opening every PDF. Nullable at the
    # DB level for quotes logged before quote_pdf existed.
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    currency = models.CharField(max_length=8, default="MXN", blank=True)
    lead_time_days = models.PositiveIntegerField(blank=True, null=True)
    payment_terms = models.CharField(max_length=80, blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    selected = models.BooleanField(default=False)

    class Meta:
        ordering = ["received_at"]

    def __str__(self):
        return f"{self.supplier_name} — {self.currency} {self.total_amount}"


class PurchaseActivity(models.Model):
    """Timeline entries shown on the purchase detail and requester status
    pages (design screens 1d/1f)."""

    request = models.ForeignKey(PurchaseRequest, related_name="activities", on_delete=models.CASCADE)
    message = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name_plural = "actividades de compra"

    def __str__(self):
        return self.message


class PickingListBatch(models.Model):
    """One logistics hand-off submission (design screen 1b) — a set of
    picking list numbers logged in a single form post."""

    shipped_on = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Lote {self.shipped_on}"

    @property
    def invoiced_count(self):
        return sum(1 for l in self.lists.all() if l.invoiced_at)

    @property
    def error_count(self):
        return sum(1 for l in self.lists.all() if l.errors.exists())

    @property
    def avg_time_to_invoice(self):
        durations = [l.hand_off_to_invoice for l in self.lists.all() if l.hand_off_to_invoice]
        if not durations:
            return None
        return sum(durations, timezone.timedelta()) / len(durations)


class PickingList(models.Model):
    class Status(models.TextChoices):
        NOT_STARTED = "not_started", "Sin iniciar"
        IN_PROCESS = "in_process", "En proceso"
        INVOICED = "invoiced", "Facturada"
        ERROR = "error", "Error"
        CORRECTED = "corrected", "Corregida"

    number = models.CharField(max_length=20, unique=True)
    batch = models.ForeignKey(PickingListBatch, related_name="lists", on_delete=models.CASCADE)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NOT_STARTED)
    handed_off_at = models.DateTimeField()
    in_process_at = models.DateTimeField(null=True, blank=True)
    invoice_number = models.CharField(max_length=40, blank=True)
    invoiced_at = models.DateTimeField(null=True, blank=True)

    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="picking_lists_handled",
        help_text="La asistente que procesó esta lista — cuenta para su KPI.",
    )

    class Meta:
        ordering = ["-handed_off_at"]

    def __str__(self):
        return self.number

    @property
    def hand_off_to_process(self):
        if not self.in_process_at:
            return None
        return self.in_process_at - self.handed_off_at

    @property
    def hand_off_to_invoice(self):
        """Total elapsed time from hand-off to invoice — this is the leg the
        KPI scorecard scores against its 8h target (see tracker/kpi.py)."""
        if not self.invoiced_at:
            return None
        return self.invoiced_at - self.handed_off_at

    @property
    def in_process_target_hours(self):
        return settings.KPI_SETTINGS["IN_PROCESS_TARGET_HOURS"]

    @property
    def invoice_target_hours(self):
        return settings.KPI_SETTINGS["INVOICE_TARGET_HOURS"]

    @property
    def is_in_process_on_time(self):
        d = self.hand_off_to_process
        if d is None:
            return None
        return d.total_seconds() / 3600 <= self.in_process_target_hours

    @property
    def is_invoice_on_time(self):
        d = self.hand_off_to_invoice
        if d is None:
            return None
        return d.total_seconds() / 3600 <= self.invoice_target_hours

    @property
    def open_error(self):
        return self.errors.filter(corrected_at__isnull=True, disputed=False).first()


class BillingError(models.Model):
    class Attributable(models.TextChoices):
        ASSISTANT = "assistant", "Asistente"
        LOGISTICS = "logistics", "Datos de logística"
        CUSTOMER = "customer", "Datos maestros del cliente"

    picking_list = models.ForeignKey(PickingList, related_name="errors", on_delete=models.CASCADE)
    invoice_number = models.CharField(max_length=40, blank=True)
    error_type = models.CharField(max_length=150)
    attributable_to = models.CharField(max_length=20, choices=Attributable.choices)
    description = models.TextField(blank=True)
    reported_by = models.CharField(max_length=120, blank=True)
    reported_at = models.DateTimeField(auto_now_add=True)
    corrected_at = models.DateTimeField(null=True, blank=True)
    disputed = models.BooleanField(default=False)

    class Meta:
        ordering = ["-reported_at"]

    def __str__(self):
        return f"{self.picking_list.number} — {self.error_type}"

    @property
    def counts_against_bonus(self):
        return self.attributable_to == self.Attributable.ASSISTANT
