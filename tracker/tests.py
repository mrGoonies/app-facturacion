from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import LogisticsHandoffForm
from .kpi import compute_scorecard
from .models import PickingList, PickingListBatch, PurchaseActivity, PurchaseRequest


def _make_list(number, hours_ago, **fields):
    batch = PickingListBatch.objects.create(shipped_on=timezone.localdate())
    return PickingList.objects.create(
        number=number,
        batch=batch,
        handed_off_at=timezone.now() - timedelta(hours=hours_ago),
        **fields,
    )


class OutcomeTests(TestCase):
    """Pending work (still inside its target) must not be scored as on time."""

    def test_untouched_list_within_target_is_pending(self):
        pl = _make_list("PL-1", hours_ago=0.5)
        self.assertIsNone(pl.in_process_outcome())
        self.assertIsNone(pl.invoice_outcome())

    def test_untouched_list_past_target_is_late(self):
        pl = _make_list("PL-1", hours_ago=3)
        self.assertIs(pl.in_process_outcome(), False)

    def test_list_marked_in_process_on_time(self):
        pl = _make_list("PL-1", hours_ago=1)
        pl.in_process_at = pl.handed_off_at + timedelta(minutes=30)
        self.assertIs(pl.in_process_outcome(), True)

    def test_request_without_po_within_target_is_pending(self):
        pr = PurchaseRequest(
            urgency=PurchaseRequest.Urgency.PRIORITY,
            created_at=timezone.now() - timedelta(hours=1),
        )
        self.assertIsNone(pr.po_outcome())

    def test_request_without_po_past_target_is_late(self):
        pr = PurchaseRequest(
            urgency=PurchaseRequest.Urgency.LINE_STOPPED,
            created_at=timezone.now() - timedelta(hours=9),
        )
        self.assertIs(pr.po_outcome(), False)


class ScorecardPendingTests(TestCase):
    @staticmethod
    def _scorecard_for(dt):
        local = timezone.localtime(dt)
        return compute_scorecard(local.year, local.month)

    def test_pending_lists_do_not_inflate_in_process_rate(self):
        late = _make_list("PL-LATE", hours_ago=2.5)  # past the 2 h target
        _make_list("PL-NEW", hours_ago=0.1)  # still inside the target
        card = self._scorecard_for(late.handed_off_at)
        self.assertEqual(card.indicators[1].actual_label, "0%")

    def test_pending_requests_are_not_counted(self):
        pr = PurchaseRequest.objects.create(
            requester_name="Pedro",
            requester_email="pedro@example.com",
            department="Mantención",
            needed_by=timezone.localdate(),
            urgency=PurchaseRequest.Urgency.PRIORITY,
        )
        card = self._scorecard_for(pr.created_at)
        self.assertEqual((card.po_on_time_count, card.po_total_count), (0, 0))

        overdue = timezone.now() - timedelta(hours=49)
        PurchaseRequest.objects.filter(pk=pr.pk).update(created_at=overdue)
        card = self._scorecard_for(overdue)
        self.assertEqual((card.po_on_time_count, card.po_total_count), (0, 1))


class PickingListNumberTests(TestCase):
    def _clean(self, raw):
        form = LogisticsHandoffForm(
            data={"shipped_on": timezone.localdate().isoformat(), "list_numbers": raw}
        )
        self.assertTrue(form.is_valid(), form.errors)
        return form.cleaned_data["list_numbers"]

    def test_bare_numbers_keep_no_leading_separator(self):
        self.assertEqual(
            self._clean("1001, 1002\n1003 1004"), ["1001", "1002", "1003", "1004"]
        )

    def test_prefix_joined_by_space_or_hyphen(self):
        self.assertEqual(
            self._clean("PL-1001, PL 1002, pl1003"), ["PL-1001", "PL-1002", "PL1003"]
        )

    def test_rejects_numbers_already_handed_off(self):
        _make_list("PL-1001", hours_ago=1)
        form = LogisticsHandoffForm(
            data={
                "shipped_on": timezone.localdate().isoformat(),
                "list_numbers": "PL-1001",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("PL-1001", str(form.errors["list_numbers"]))


# The manifest storage used in production needs `collectstatic` to have run;
# tests that render templates use the plain storage instead.
PLAIN_STATIC = override_settings(
    STORAGES={
        **settings.STORAGES,
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }
)


@PLAIN_STATIC
class PurchaseRequestFormErrorsTests(TestCase):
    """Server-side validation errors must be visible on the re-rendered form
    (the form is `novalidate`, so the browser never catches them first)."""

    def _post(self, needed_by=None, **item):
        data = {
            "requester_name": "Pedro",
            "requester_email": "pedro@example.com",
            "department": "Mantención",
            "needed_by": (needed_by or timezone.localdate()).isoformat(),
            "urgency": "standard",
            "items-TOTAL_FORMS": "3",
            "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "1",
            "items-MAX_NUM_FORMS": "1000",
        }
        data.update({f"items-0-{k}": v for k, v in item.items()})
        return self.client.post(reverse("tracker:purchase_request_create"), data)

    def test_missing_unit_error_is_shown(self):
        resp = self._post(description="Guantes", quantity="10", unit="")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Selecciona una unidad.")

    def test_missing_quantity_error_is_shown(self):
        resp = self._post(description="Guantes", quantity="", unit="caja")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Este campo es obligatorio.")

    def test_past_needed_by_error_is_shown(self):
        resp = self._post(
            needed_by=timezone.localdate() - timedelta(days=1),
            description="Guantes",
            quantity="10",
            unit="caja",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "La fecha debe ser hoy o una fecha futura.")

    def test_valid_request_redirects_to_status(self):
        resp = self._post(description="Guantes", quantity="10", unit="caja")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(PurchaseRequest.objects.get().items.count(), 1)


@PLAIN_STATIC
class QuoteConfirmationTests(TestCase):
    """The requester picks the quote on their public status page; the
    assistant can only issue the PO after that confirmation."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(
            "asistente", password="x", is_staff=True
        )
        self.client.force_login(self.user)
        self.pr = PurchaseRequest.objects.create(
            requester_name="Pedro",
            requester_email="pedro@example.com",
            department="Mantención",
            needed_by=timezone.localdate(),
            status=PurchaseRequest.Status.AWAITING_CONFIRMATION,
            quotes_sent_at=timezone.now(),
        )
        self.cheap = self.pr.quotes.create(supplier_name="Barato", total_amount=100)
        self.pricey = self.pr.quotes.create(supplier_name="Caro", total_amount=200)

    def _issue_po(self):
        return self.client.post(
            reverse("tracker:purchase_detail", args=[self.pr.pk]),
            {"action": "issue_po", "po_number": "OC-4510"},
        )

    def _confirm(self, quote):
        return self.client.post(
            self.pr.get_status_url(),
            {"action": "confirm_quote", "quote_id": quote.pk},
        )

    def test_panel_has_no_select_button(self):
        resp = self.client.get(reverse("tracker:purchase_detail", args=[self.pr.pk]))
        self.assertNotContains(resp, "select_quote")
        self.assertNotContains(resp, "Emitir orden de compra")

    def test_issue_po_requires_requester_confirmation(self):
        self._issue_po()
        self.pr.refresh_from_db()
        self.assertIsNone(self.pr.po_issued_at)

    def test_requester_can_confirm_quote_via_token(self):
        self._confirm(self.cheap)
        self.pr.refresh_from_db()
        self.assertIsNotNone(self.pr.confirmed_at)
        self.assertEqual(self.pr.selected_quote, self.cheap)

        resp = self.client.get(reverse("tracker:purchase_detail", args=[self.pr.pk]))
        self.assertContains(resp, "Emitir orden de compra")
        self._issue_po()
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.status, PurchaseRequest.Status.PO_ISSUED)
        self.assertEqual(self.pr.handled_by, self.user)

    def test_confirm_rejected_when_not_awaiting(self):
        PurchaseRequest.objects.filter(pk=self.pr.pk).update(
            status=PurchaseRequest.Status.QUOTING
        )
        self._confirm(self.cheap)
        self.pr.refresh_from_db()
        self.assertIsNone(self.pr.confirmed_at)
        self.assertFalse(self.pr.quotes.filter(selected=True).exists())


def _make_request(**fields):
    defaults = {
        "requester_name": "Pedro",
        "requester_email": "pedro@example.com",
        "department": "Mantención",
        "needed_by": timezone.localdate(),
    }
    return PurchaseRequest.objects.create(**{**defaults, **fields})


@PLAIN_STATIC
class PurchaseFlowStaffTests(TestCase):
    """The assistant's side: the queue says whose turn it is, and every
    hand-over to the requester notifies them."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "asistente", email="compras@example.com", password="x", is_staff=True
        )
        self.client.force_login(self.user)

    def _post(self, pr, **data):
        return self.client.post(reverse("tracker:purchase_detail", args=[pr.pk]), data)

    def test_queue_lists_ready_to_issue_first_and_waiting_last(self):
        waiting = _make_request(
            status=PurchaseRequest.Status.AWAITING_CONFIRMATION,
            quotes_sent_at=timezone.now(),
        )
        _make_request()
        ready = _make_request(
            status=PurchaseRequest.Status.AWAITING_CONFIRMATION,
            confirmed_at=timezone.now(),
        )
        issued = _make_request(status=PurchaseRequest.Status.PO_ISSUED)
        resp = self.client.get(reverse("tracker:queue") + "?view=purchases")
        refs = [row.ref for row in resp.context["rows"]]
        self.assertEqual(refs[0], ready.display_ref)
        self.assertEqual(set(refs[-2:]), {waiting.display_ref, issued.display_ref})
        self.assertContains(resp, "Lista para emitir OC")
        self.assertContains(resp, "Esperando al solicitante")
        self.assertContains(resp, "Esperando recepción")
        self.assertEqual(resp.context["stats"]["ready_to_issue"], 1)

    def test_line_stopped_leads_actionable_rows(self):
        _make_request()
        stopped = _make_request(urgency=PurchaseRequest.Urgency.LINE_STOPPED)
        resp = self.client.get(reverse("tracker:queue"))
        self.assertEqual(resp.context["rows"][0].ref, stopped.display_ref)

    def test_invalid_quote_rerenders_with_errors(self):
        pr = _make_request()
        resp = self._post(pr, action="add_quote", supplier_name="", currency="CLP")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Este campo es obligatorio.")
        self.assertFalse(pr.quotes.exists())

    def test_delete_last_quote_returns_to_requested(self):
        pr = _make_request(status=PurchaseRequest.Status.QUOTING)
        quote = pr.quotes.create(supplier_name="Errado")
        self._post(pr, action="delete_quote", quote_id=quote.pk)
        pr.refresh_from_db()
        self.assertFalse(pr.quotes.exists())
        self.assertEqual(pr.status, PurchaseRequest.Status.REQUESTED)

    def test_cannot_delete_quote_once_sent(self):
        pr = _make_request(status=PurchaseRequest.Status.AWAITING_CONFIRMATION)
        quote = pr.quotes.create(supplier_name="Enviada")
        self._post(pr, action="delete_quote", quote_id=quote.pk)
        self.assertTrue(pr.quotes.filter(pk=quote.pk).exists())

    def test_remind_requester_resends_quotes(self):
        pr = _make_request(status=PurchaseRequest.Status.AWAITING_CONFIRMATION)
        self._post(pr, action="remind_requester")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["pedro@example.com"])
        self.assertIn("Recordatorio", mail.outbox[0].subject)

    def test_issue_po_requires_real_number_and_notifies_requester(self):
        pr = _make_request(
            status=PurchaseRequest.Status.AWAITING_CONFIRMATION,
            confirmed_at=timezone.now(),
        )
        pr.quotes.create(supplier_name="Barato", selected=True)

        resp = self._post(pr, action="issue_po", po_number="")
        self.assertEqual(resp.status_code, 200)
        pr.refresh_from_db()
        self.assertIsNone(pr.po_issued_at)

        self._post(pr, action="issue_po", po_number="OC-4510")
        pr.refresh_from_db()
        self.assertEqual(pr.status, PurchaseRequest.Status.PO_ISSUED)
        self.assertEqual(pr.po_number, "OC-4510")
        self.assertEqual(mail.outbox[-1].to, ["pedro@example.com"])
        self.assertIn("OC-4510", mail.outbox[-1].body)

    def test_close_only_after_po_and_notifies_requester(self):
        pr = _make_request(status=PurchaseRequest.Status.QUOTING)
        self._post(pr, action="close_request")
        pr.refresh_from_db()
        self.assertEqual(pr.status, PurchaseRequest.Status.QUOTING)

        PurchaseRequest.objects.filter(pk=pr.pk).update(
            status=PurchaseRequest.Status.PO_ISSUED
        )
        self._post(pr, action="close_request")
        pr.refresh_from_db()
        self.assertEqual(pr.status, PurchaseRequest.Status.CLOSED)
        self.assertEqual(mail.outbox[-1].to, ["pedro@example.com"])

    def test_staff_cancel_notifies_requester(self):
        pr = _make_request()
        self._post(pr, action="cancel_request")
        pr.refresh_from_db()
        self.assertEqual(pr.status, PurchaseRequest.Status.CANCELLED)
        self.assertEqual(mail.outbox[-1].to, ["pedro@example.com"])

    def test_staff_message_reaches_requester(self):
        pr = _make_request()
        self._post(pr, action="post_message", body="¿Qué talla de guantes?")
        activity = pr.activities.get()
        self.assertEqual(activity.kind, PurchaseActivity.Kind.STAFF_MESSAGE)
        self.assertEqual(mail.outbox[-1].to, ["pedro@example.com"])
        self.assertIn("¿Qué talla de guantes?", mail.outbox[-1].body)


@PLAIN_STATIC
class PurchaseFlowRequesterTests(TestCase):
    """The requester's side: they act on their own request from the status
    page, and each action lands with the assistant by email."""

    def setUp(self):
        get_user_model().objects.create_user(
            "asistente", email="compras@example.com", password="x", is_staff=True
        )

    def _post(self, pr, **data):
        return self.client.post(pr.get_status_url(), data)

    def test_confirming_quote_notifies_staff(self):
        pr = _make_request(status=PurchaseRequest.Status.AWAITING_CONFIRMATION)
        quote = pr.quotes.create(supplier_name="Barato")
        self._post(pr, action="confirm_quote", quote_id=quote.pk)
        self.assertEqual(mail.outbox[-1].to, ["compras@example.com"])
        self.assertIn("lista para emitir OC", mail.outbox[-1].subject)

    def test_reject_quotes_needs_reason_and_reopens_quoting(self):
        pr = _make_request(status=PurchaseRequest.Status.AWAITING_CONFIRMATION)
        pr.quotes.create(supplier_name="Caro")

        self._post(pr, action="reject_quotes", body="")
        pr.refresh_from_db()
        self.assertEqual(pr.status, PurchaseRequest.Status.AWAITING_CONFIRMATION)

        self._post(pr, action="reject_quotes", body="Muy caras")
        pr.refresh_from_db()
        self.assertEqual(pr.status, PurchaseRequest.Status.QUOTING)
        self.assertTrue(
            pr.activities.filter(
                kind=PurchaseActivity.Kind.REQUESTER_MESSAGE, message="Muy caras"
            ).exists()
        )
        self.assertEqual(mail.outbox[-1].to, ["compras@example.com"])

    def test_requester_can_cancel_before_po_only(self):
        pr = _make_request(status=PurchaseRequest.Status.QUOTING)
        self._post(pr, action="cancel_request", body="Ya no se necesita")
        pr.refresh_from_db()
        self.assertEqual(pr.status, PurchaseRequest.Status.CANCELLED)
        self.assertEqual(mail.outbox[-1].to, ["compras@example.com"])

        issued = _make_request(status=PurchaseRequest.Status.PO_ISSUED)
        self._post(issued, action="cancel_request")
        issued.refresh_from_db()
        self.assertEqual(issued.status, PurchaseRequest.Status.PO_ISSUED)

    def test_confirm_receipt_closes_request(self):
        pr = _make_request(status=PurchaseRequest.Status.PO_ISSUED)
        self._post(pr, action="confirm_receipt")
        pr.refresh_from_db()
        self.assertEqual(pr.status, PurchaseRequest.Status.CLOSED)
        self.assertIsNotNone(pr.closed_at)

    def test_confirm_receipt_rejected_before_po(self):
        pr = _make_request(status=PurchaseRequest.Status.QUOTING)
        self._post(pr, action="confirm_receipt")
        pr.refresh_from_db()
        self.assertEqual(pr.status, PurchaseRequest.Status.QUOTING)

    def test_requester_message_reaches_staff(self):
        pr = _make_request()
        self._post(pr, action="post_message", body="Sirve cualquier marca")
        self.assertEqual(
            pr.activities.get().kind, PurchaseActivity.Kind.REQUESTER_MESSAGE
        )
        self.assertEqual(mail.outbox[-1].to, ["compras@example.com"])

    def test_status_page_shows_message_thread(self):
        pr = _make_request()
        pr.activities.create(
            kind=PurchaseActivity.Kind.STAFF_MESSAGE,
            author="Ana",
            message="¿Qué talla?",
        )
        resp = self.client.get(pr.get_status_url())
        self.assertContains(resp, "Ana · Compras")
        self.assertContains(resp, "¿Qué talla?")
        self.assertContains(resp, "Repetir esta solicitud")

    def test_repeat_prefills_items_and_requester(self):
        pr = _make_request(justification="Reposición mensual")
        pr.items.create(description="Guantes de nitrilo", quantity=10, unit="caja")
        pr.items.create(description="Lentes", quantity=5, unit="Par")
        resp = self.client.get(
            reverse("tracker:purchase_request_repeat", args=[pr.token])
        )
        self.assertContains(resp, 'value="pedro@example.com"')
        self.assertContains(resp, 'value="Guantes de nitrilo"')
        self.assertContains(resp, 'value="Lentes"')
        # A non-standard unit comes back through "Otro" instead of being lost.
        self.assertContains(resp, 'value="Par"')
