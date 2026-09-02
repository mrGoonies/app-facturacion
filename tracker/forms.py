import re

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.forms import inlineformset_factory

from .models import BillingError, PickingListBatch, PurchaseRequest, PurchaseRequestItem, SupplierQuote

PL_NUMBER_RE = re.compile(r"[A-Za-z]*-?\d+")


class StyledFormMixin:
    """Applies the design system's `.input` class to every text-like widget
    so templates can just `{{ form.field }}` without repeating widget attrs."""

    TEXT_WIDGETS = (
        forms.TextInput, forms.EmailInput, forms.NumberInput,
        forms.DateInput, forms.Textarea, forms.URLInput, forms.PasswordInput,
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
    needed_by = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))

    class Meta:
        model = PurchaseRequest
        fields = ["requester_name", "requester_email", "department", "needed_by", "justification", "urgency"]
        widgets = {
            "justification": forms.Textarea(attrs={"rows": 3}),
            "urgency": forms.RadioSelect,
        }


PurchaseRequestItemFormSet = inlineformset_factory(
    PurchaseRequest,
    PurchaseRequestItem,
    fields=["description", "quantity", "unit"],
    extra=2,
    can_delete=True,
    widgets={
        "description": forms.TextInput(attrs={"class": "input"}),
        "quantity": forms.NumberInput(attrs={"class": "input"}),
        "unit": forms.TextInput(attrs={"class": "input"}),
    },
)


class LogisticsHandoffForm(StyledFormMixin, forms.ModelForm):
    shipped_on = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    list_numbers = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "PL-8836, PL-8837, PL-8838 …"}),
        help_text="Separa los números de lista con comas, espacios o saltos de línea.",
    )

    class Meta:
        model = PickingListBatch
        fields = ["shipped_on", "sent_by", "customer_route"]

    def clean_list_numbers(self):
        raw = self.cleaned_data["list_numbers"]
        numbers = [n.upper() for n in PL_NUMBER_RE.findall(raw)]
        if not numbers:
            raise forms.ValidationError("Ingresa al menos un número de lista de picking.")
        seen, deduped = set(), []
        for n in numbers:
            if n not in seen:
                seen.add(n)
                deduped.append(n)
        return deduped


class SupplierQuoteForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = SupplierQuote
        fields = ["supplier_name", "total_amount", "currency", "lead_time_days", "payment_terms"]


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
        fields = ["invoice_number", "error_type", "attributable_to", "reported_by", "description"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
        }
