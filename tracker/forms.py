import re

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.contrib.auth.forms import AuthenticationForm
from django.forms import inlineformset_factory

from .models import (
    BillingError,
    PickingListBatch,
    PurchaseRequest,
    PurchaseRequestItem,
    SupplierQuote,
    PickingList,
)

# A letter prefix may be joined to the number by a hyphen or a space
# ("PL-1003", "PL 1003"), but a bare number never absorbs the separator in
# front of it — otherwise "1001, 1002" would yield "-1002". Must match
# TOKEN_RE in logistics_handoff_form.html.
PL_NUMBER_RE = re.compile(r"[A-Za-z]+[ \t-]?\d+|\d+")

UNIT_CHOICES = [
    ("", "Selecciona…"),
    ("pza", "Pieza (pza)"),
    ("caja", "Caja"),
    ("kg", "Kilogramo (kg)"),
    ("litro", "Litro (L)"),
    ("metro", "Metro (m)"),
    ("rollo", "Rollo"),
    ("paquete", "Paquete"),
    ("galon", "Galón"),
    ("ton", "Tonelada"),
    ("other", "Otro"),
]
UNIT_CHOICE_VALUES = {value for value, _ in UNIT_CHOICES if value}


class StyledFormMixin:
    """Applies the design system's `.input` class to every text-like widget
    so templates can just `{{ form.field }}` without repeating widget attrs."""

    TEXT_WIDGETS = (
        forms.TextInput,
        forms.EmailInput,
        forms.NumberInput,
        forms.DateInput,
        forms.Textarea,
        forms.URLInput,
        forms.PasswordInput,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in self.fields.values():
            if isinstance(f.widget, self.TEXT_WIDGETS):
                existing = f.widget.attrs.get("class", "")
                f.widget.attrs["class"] = (existing + " input").strip()


class BrandedAuthenticationForm(StyledFormMixin, AuthenticationForm):
    pass


class PurchaseRequestForm(StyledFormMixin, forms.ModelForm):
    needed_by = forms.DateField(
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["needed_by"].widget.attrs["min"] = timezone.localdate().isoformat()

    def clean_needed_by(self):
        value = self.cleaned_data["needed_by"]
        if value < timezone.localdate():
            raise forms.ValidationError("La fecha debe ser hoy o una fecha futura.")
        return value

    class Meta:
        model = PurchaseRequest
        fields = [
            "requester_name",
            "requester_email",
            "department",
            "needed_by",
            "justification",
            "urgency",
            "reference_image",
        ]
        widgets = {
            "justification": forms.Textarea(attrs={"rows": 3}),
            "urgency": forms.RadioSelect,
        }


class PurchaseRequestItemForm(StyledFormMixin, forms.ModelForm):
    unit = forms.ChoiceField(
        choices=UNIT_CHOICES,
        required=False,
        label="Unidad",
        widget=forms.Select(attrs={"class": "input"}),
    )
    unit_other = forms.CharField(
        required=False,
        label="Especifica la unidad",
        widget=forms.TextInput(
            attrs={"class": "input", "placeholder": "Escribe la unidad"}
        ),
    )

    class Meta:
        model = PurchaseRequestItem
        fields = ["description", "quantity", "unit", "reference_image"]
        widgets = {
            "description": forms.TextInput(attrs={"class": "input"}),
            "quantity": forms.NumberInput(
                attrs={"class": "input", "step": "1", "min": "1"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Pre-existing rows whose stored unit isn't one of the standard
        # choices (data entered before this selector existed, or edited
        # directly in the admin) fall back to "Otro" with the raw text
        # carried over, instead of silently losing it.
        current = self.initial.get("unit")
        if current and current not in UNIT_CHOICE_VALUES:
            self.initial["unit"] = "other"
            self.initial["unit_other"] = current

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("description") and not cleaned.get("unit"):
            self.add_error("unit", "Selecciona una unidad.")
        if cleaned.get("unit") == "other":
            other = (cleaned.get("unit_other") or "").strip()
            if not other:
                self.add_error("unit_other", "Especifica la unidad.")
            else:
                cleaned["unit"] = other
        return cleaned


PurchaseRequestItemFormSet = inlineformset_factory(
    PurchaseRequest,
    PurchaseRequestItem,
    form=PurchaseRequestItemForm,
    extra=2,
    can_delete=True,
    min_num=1,
    validate_min=True,
)


class LogisticsHandoffForm(StyledFormMixin, forms.ModelForm):
    shipped_on = forms.DateField(
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"})
    )
    list_numbers = forms.CharField(
        widget=forms.Textarea(
            attrs={"rows": 2, "placeholder": "PL-8836, PL-8837, PL-8838 …"}
        ),
        help_text="Separa los números de lista con comas, espacios o saltos de línea.",
    )

    class Meta:
        model = PickingListBatch
        fields = ["shipped_on"]

    def clean_list_numbers(self):
        raw = self.cleaned_data["list_numbers"]
        numbers = [re.sub(r"[ \t]+", "-", n.upper()) for n in PL_NUMBER_RE.findall(raw)]
        if not numbers:
            raise forms.ValidationError(
                "Ingresa al menos un número de lista de picking."
            )
        seen, deduped = set(), []
        for n in numbers:
            if n not in seen:
                seen.add(n)
                deduped.append(n)
        existing = set(
            PickingList.objects.filter(number__in=deduped).values_list(
                "number", flat=True
            )
        )
        if existing:
            raise forms.ValidationError(
                f"Ya registradas: {', '.join(sorted(existing))}. Quítalas para continuar."
            )
        return deduped


class SupplierQuoteForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = SupplierQuote
        fields = [
            "supplier_name",
            "quote_pdf",
            "total_amount",
            "currency",
            "lead_time_days",
            "payment_terms",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["currency"].initial = settings.DEFAULT_CURRENCY

    def clean_quote_pdf(self):
        pdf = self.cleaned_data.get("quote_pdf")
        if pdf and not pdf.name.lower().endswith(".pdf"):
            raise forms.ValidationError("El archivo debe ser un PDF.")
        return pdf


class IssuePOForm(StyledFormMixin, forms.ModelForm):
    """The PO number comes from the ERP the order was actually raised in —
    never generated here, or it wouldn't match the real document."""

    class Meta:
        model = PurchaseRequest
        fields = ["po_number", "po_pdf"]
        labels = {"po_number": "Número de OC"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["po_number"].required = True

    def clean_po_pdf(self):
        pdf = self.cleaned_data.get("po_pdf")
        if pdf and hasattr(pdf, "name") and not pdf.name.lower().endswith(".pdf"):
            raise forms.ValidationError("El archivo debe ser un PDF.")
        return pdf


class BillingErrorForm(StyledFormMixin, forms.ModelForm):
    # Redeclared explicitly: ModelForm otherwise inserts a blank "---------"
    # choice for a required CharField with no default, which showed up as a
    # pre-selected, untranslated "- Select an option -" radio.
    attributable_to = forms.ChoiceField(
        choices=BillingError.Attributable.choices,
        widget=forms.RadioSelect,
        initial=BillingError.Attributable.ASSISTANT,
        label="Atribuible a",
    )

    class Meta:
        model = BillingError
        fields = [
            "invoice_number",
            "error_type",
            "attributable_to",
            "reported_by",
            "description",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
        }
