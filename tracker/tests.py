from datetime import timedelta
from io import StringIO

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import LogisticsHandoffForm
from .kpi import compute_scorecard
from .models import (
    BillingError,
    PickingList,
    PickingListBatch,
    PurchaseActivity,
    PurchaseRequest,
)


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

    def test_skips_numbers_already_handed_off(self):
        _make_list("PL-1001", hours_ago=1)
        form = LogisticsHandoffForm(
            data={
                "shipped_on": timezone.localdate().isoformat(),
                "list_numbers": "PL-1001, PL-1002",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["list_numbers"], ["PL-1002"])
        self.assertEqual(form.skipped_numbers, ["PL-1001"])

    def test_rejects_when_every_number_was_handed_off(self):
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
        self.assertContains(resp, "En pausa")
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


class PoClockPauseTests(TestCase):
    """The request → PO clock stops while the quotes sit with the requester,
    so their think-time doesn't count against the assistant."""

    def _waiting(self, created_hours_ago, sent_hours_ago, **fields):
        now = timezone.now()
        pr = _make_request(
            urgency=PurchaseRequest.Urgency.LINE_STOPPED,  # 8 h target
            status=PurchaseRequest.Status.AWAITING_CONFIRMATION,
            quotes_sent_at=now - timedelta(hours=sent_hours_ago),
            **fields,
        )
        PurchaseRequest.objects.filter(pk=pr.pk).update(
            created_at=now - timedelta(hours=created_hours_ago)
        )
        pr.refresh_from_db()
        return pr

    def test_waiting_on_requester_does_not_count(self):
        pr = self._waiting(created_hours_ago=10, sent_hours_ago=6)
        self.assertIsNone(pr.po_outcome())
        self.assertAlmostEqual(pr.po_hours_left(), 4, places=1)
        self.assertTrue(pr.is_paused)

    @override_settings(
        KPI_SETTINGS={
            **settings.KPI_SETTINGS,
            "PO_PAUSE_WHILE_AWAITING_REQUESTER": False,
        }
    )
    def test_pause_can_be_turned_off(self):
        pr = self._waiting(created_hours_ago=10, sent_hours_ago=6)
        self.assertIs(pr.po_outcome(), False)

    def test_confirmation_banks_the_wait_and_po_is_on_time(self):
        pr = self._waiting(created_hours_ago=10, sent_hours_ago=5)
        quote = pr.quotes.create(supplier_name="Barato")
        self.client.post(
            pr.get_status_url(), {"action": "confirm_quote", "quote_id": quote.pk}
        )
        pr.refresh_from_db()
        self.assertAlmostEqual(
            pr.requester_wait_time.total_seconds() / 3600, 5, places=1
        )
        PurchaseRequest.objects.filter(pk=pr.pk).update(
            status=PurchaseRequest.Status.PO_ISSUED, po_issued_at=timezone.now()
        )
        pr.refresh_from_db()
        self.assertIs(pr.is_po_on_time, True)

    def test_each_round_with_the_requester_adds_up(self):
        pr = self._waiting(created_hours_ago=10, sent_hours_ago=3)
        self.client.post(
            pr.get_status_url(), {"action": "reject_quotes", "body": "Muy caras"}
        )
        PurchaseRequest.objects.filter(pk=pr.pk).update(
            status=PurchaseRequest.Status.AWAITING_CONFIRMATION,
            quotes_sent_at=timezone.now() - timedelta(hours=2),
        )
        pr.refresh_from_db()
        self.assertAlmostEqual(pr.paused_time().total_seconds() / 3600, 5, places=1)


@PLAIN_STATIC
class SingleSourceQuoteTests(TestCase):
    def setUp(self):
        self.client.force_login(
            get_user_model().objects.create_user(
                "asistente", password="x", is_staff=True
            )
        )
        self.pr = _make_request(status=PurchaseRequest.Status.QUOTING)
        self.pr.quotes.create(supplier_name="Único")

    def _send(self, **data):
        return self.client.post(
            reverse("tracker:purchase_detail", args=[self.pr.pk]),
            {"action": "send_quotes_to_requester", **data},
        )

    def test_below_minimum_needs_a_reason(self):
        self._send()
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.status, PurchaseRequest.Status.QUOTING)

    def test_reason_lets_it_through_and_reaches_requester(self):
        self._send(single_source_reason="Distribuidor exclusivo de la marca")
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.status, PurchaseRequest.Status.AWAITING_CONFIRMATION)
        self.assertEqual(
            self.pr.single_source_reason, "Distribuidor exclusivo de la marca"
        )
        self.assertIn("Distribuidor exclusivo de la marca", mail.outbox[-1].body)
        resp = self.client.get(self.pr.get_status_url())
        self.assertContains(resp, "Distribuidor exclusivo de la marca")

    def test_reason_is_dropped_when_minimum_is_met(self):
        self.pr.quotes.create(supplier_name="Otro")
        self._send(single_source_reason="No aplica")
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.single_source_reason, "")


class StaleRequestCommandTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_user(
            "asistente", email="compras@example.com", password="x", is_staff=True
        )

    def _waiting(self, hours, **fields):
        return _make_request(
            status=PurchaseRequest.Status.AWAITING_CONFIRMATION,
            quotes_sent_at=timezone.now() - timedelta(hours=hours),
            **fields,
        )

    def _run(self):
        from django.core.management import call_command

        call_command("process_stale_purchase_requests", stdout=StringIO())

    def test_reminds_once_per_round(self):
        pr = self._waiting(hours=49)
        self._run()
        self._run()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["pedro@example.com"])
        self.assertIn("se cancelará automáticamente", mail.outbox[0].body)
        pr.refresh_from_db()
        self.assertIsNotNone(pr.reminded_at)

    def test_manual_reminder_counts(self):
        self._waiting(hours=49, reminded_at=timezone.now() - timedelta(hours=1))
        self._run()
        self.assertEqual(len(mail.outbox), 0)

    def test_leaves_recent_and_confirmed_requests_alone(self):
        self._waiting(hours=10)
        self._waiting(hours=24 * 8, confirmed_at=timezone.now())
        self._run()
        self.assertEqual(len(mail.outbox), 0)

    def test_cancels_after_the_limit_and_tells_both_sides(self):
        pr = self._waiting(hours=24 * 7 + 1)
        self._run()
        pr.refresh_from_db()
        self.assertEqual(pr.status, PurchaseRequest.Status.CANCELLED)
        self.assertEqual(
            {tuple(m.to) for m in mail.outbox},
            {("pedro@example.com",), ("compras@example.com",)},
        )

    @override_settings(PURCHASE_AUTO_CANCEL_DAYS=0, PURCHASE_AUTO_REMIND_HOURS=0)
    def test_zero_turns_both_steps_off(self):
        pr = self._waiting(hours=24 * 30)
        self._run()
        pr.refresh_from_db()
        self.assertEqual(pr.status, PurchaseRequest.Status.AWAITING_CONFIRMATION)
        self.assertEqual(len(mail.outbox), 0)


ACCOUNTING = override_settings(
    PURCHASE_ACCOUNTING_EMAILS=[
        "contador@example.com",
        "asistente.contable@example.com",
    ]
)


@PLAIN_STATIC
@ACCOUNTING
class PoToAccountingTests(TestCase):
    """Accounting receives every issued PO with the PO PDF and the chosen
    quote attached."""

    def setUp(self):
        self.client.force_login(
            get_user_model().objects.create_user(
                "asistente", password="x", is_staff=True
            )
        )
        self.pr = _make_request(
            status=PurchaseRequest.Status.AWAITING_CONFIRMATION,
            confirmed_at=timezone.now(),
        )
        self.quote = self.pr.quotes.create(
            supplier_name="Ferretería Sur", total_amount=125000, selected=True
        )
        self.pr.items.create(description="Guantes", quantity=10, unit="caja")

    def _with_uploads(self):
        # Stored public ids, as left behind by a real Cloudinary upload.
        PurchaseRequest.objects.filter(pk=self.pr.pk).update(
            status=PurchaseRequest.Status.PO_ISSUED,
            po_number="OC-4510",
            po_issued_at=timezone.now(),
            po_pdf="purchase_orders/oc-4510",
        )
        self.quote.__class__.objects.filter(pk=self.quote.pk).update(
            quote_pdf="supplier_quotes/sur"
        )
        self.pr.refresh_from_db()

    def _accounting_mail(self):
        return next(m for m in mail.outbox if "contador@example.com" in m.to)

    def test_po_pdf_is_required_while_accounting_is_set(self):
        resp = self.client.post(
            reverse("tracker:purchase_detail", args=[self.pr.pk]),
            {"action": "issue_po", "po_number": "OC-4510"},
        )
        self.assertEqual(resp.status_code, 200)
        self.pr.refresh_from_db()
        self.assertIsNone(self.pr.po_issued_at)
        self.assertContains(resp, "se envía a contabilidad")

    def test_sends_po_and_quote_as_attachments(self):
        from unittest import mock

        from .emails import send_po_to_accounting

        self._with_uploads()
        with mock.patch(
            "tracker.emails._fetch_file",
            return_value=("https://res.example/x.pdf", b"%PDF-1.4"),
        ):
            send_po_to_accounting(self.pr)
        msg = self._accounting_mail()
        self.assertEqual(
            msg.to, ["contador@example.com", "asistente.contable@example.com"]
        )
        self.assertIn("OC-4510", msg.subject)
        self.assertEqual(
            [name for name, _, _ in msg.attachments],
            ["OC-4510.pdf", "Cotizacion_Ferretería_Sur.pdf"],
        )
        self.assertIn("Ferretería Sur", msg.body)
        self.assertIn("Guantes", msg.body)

    def test_falls_back_to_link_when_download_fails(self):
        from unittest import mock

        from .emails import send_po_to_accounting

        self._with_uploads()
        with mock.patch(
            "tracker.emails._fetch_file",
            return_value=("https://res.example/x.pdf", None),
        ):
            send_po_to_accounting(self.pr)
        msg = self._accounting_mail()
        self.assertEqual(msg.attachments, [])
        self.assertIn("https://res.example/x.pdf", msg.body)

    @override_settings(PURCHASE_ACCOUNTING_EMAILS=[])
    def test_off_when_not_configured(self):
        from .emails import send_po_to_accounting

        self._with_uploads()
        send_po_to_accounting(self.pr)
        self.assertEqual(mail.outbox, [])


@PLAIN_STATIC
class PickingFlowTests(TestCase):
    """Logistics hands lists off and can take back a mistyped one; the
    assistant takes and invoices them in bulk from the queue, and no
    transition can rewind the timestamps the invoicing KPIs read."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "asistente", email="facturacion@example.com", password="x", is_staff=True
        )
        self.client.force_login(self.user)

    def _bulk(self, action, lists, follow=False, **data):
        return self.client.post(
            reverse("tracker:picking_list_bulk"),
            {"action": action, "lists": [pl.pk for pl in lists], **data},
            follow=follow,
        )

    def _detail(self, pl, **data):
        return self.client.post(
            reverse("tracker:picking_list_detail", args=[pl.number]), data
        )

    def test_handoff_emails_staff_and_reports_skipped_numbers(self):
        _make_list("PL-1", hours_ago=1)
        self.client.logout()
        resp = self.client.post(
            reverse("tracker:logistics_handoff"),
            {
                "shipped_on": timezone.localdate().isoformat(),
                "list_numbers": "PL-1, PL-2, PL-3",
            },
            follow=True,
        )
        self.assertEqual(
            set(PickingList.objects.values_list("number", flat=True)),
            {"PL-1", "PL-2", "PL-3"},
        )
        self.assertContains(resp, "Omitimos PL-1")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["facturacion@example.com"])
        self.assertIn("PL-2", mail.outbox[0].body)
        self.assertNotIn("PL-1\n", mail.outbox[0].body)

    def test_logistics_can_discard_only_untouched_lists(self):
        mistyped = _make_list("PL-9", hours_ago=1)
        taken = _make_list("PL-8", hours_ago=1)
        taken.mark_in_process(self.user)
        self.client.logout()
        for pl in (mistyped, taken):
            self.client.post(
                reverse("tracker:logistics_list_discard", args=[pl.number])
            )
        self.assertFalse(PickingList.objects.filter(number="PL-9").exists())
        self.assertFalse(PickingListBatch.objects.filter(pk=mistyped.batch_id).exists())
        self.assertTrue(PickingList.objects.filter(number="PL-8").exists())

    def test_bulk_take_marks_only_untouched_lists(self):
        fresh = _make_list("PL-1", hours_ago=1)
        started = _make_list("PL-2", hours_ago=1)
        started.mark_in_process(self.user, now=timezone.now() - timedelta(minutes=30))
        first_take = PickingList.objects.get(pk=started.pk).in_process_at
        self._bulk("take", [fresh, started])
        fresh.refresh_from_db()
        started.refresh_from_db()
        self.assertEqual(fresh.status, PickingList.Status.IN_PROCESS)
        self.assertEqual(fresh.handled_by, self.user)
        self.assertEqual(started.in_process_at, first_take)

    def test_bulk_invoice_shares_one_invoice_number(self):
        lists = [_make_list(f"PL-{n}", hours_ago=1) for n in (1, 2)]
        lists[0].mark_in_process(self.user)
        self._bulk("invoice", lists, invoice_number="F-100")
        for pl in lists:
            pl.refresh_from_db()
            self.assertEqual(pl.status, PickingList.Status.INVOICED)
            self.assertEqual(pl.invoice_number, "F-100")
        # Invoiced straight from "not started": both legs stop at once.
        self.assertEqual(lists[1].in_process_at, lists[1].invoiced_at)

    def test_bulk_invoice_requires_a_number(self):
        pl = _make_list("PL-1", hours_ago=1)
        resp = self._bulk("invoice", [pl], invoice_number=" ", follow=True)
        pl.refresh_from_db()
        self.assertEqual(pl.status, PickingList.Status.NOT_STARTED)
        self.assertContains(resp, "Ingresa el número de factura.")

    def test_repeated_posts_cannot_rewind_an_invoiced_list(self):
        pl = _make_list("PL-1", hours_ago=3)
        self._detail(pl, action="issue_invoice", invoice_number="F-1")
        pl.refresh_from_db()
        invoiced_at = pl.invoiced_at
        self._detail(pl, action="mark_in_process")
        self._detail(pl, action="issue_invoice", invoice_number="F-2")
        pl.refresh_from_db()
        self.assertEqual(pl.status, PickingList.Status.INVOICED)
        self.assertEqual(pl.invoice_number, "F-1")
        self.assertEqual(pl.invoiced_at, invoiced_at)

    def test_invalid_error_report_rerenders_with_errors(self):
        pl = _make_list("PL-1", hours_ago=3)
        pl.issue_invoice("F-1", self.user)
        resp = self._detail(pl, action="report_error", error_type="")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Este campo es obligatorio.")
        self.assertFalse(pl.errors.exists())

    def test_correction_records_new_invoice_once(self):
        pl = _make_list("PL-1", hours_ago=3)
        pl.issue_invoice("F-1", self.user)
        err = pl.errors.create(
            error_type="Precio", attributable_to=BillingError.Attributable.ASSISTANT
        )
        self._detail(
            pl,
            action="correct_error",
            error_id=err.pk,
            corrected_invoice_number="F-1b",
        )
        err.refresh_from_db()
        pl.refresh_from_db()
        self.assertEqual(err.corrected_invoice_number, "F-1b")
        self.assertEqual(pl.invoice_number, "F-1b")
        corrected_at = err.corrected_at
        self._detail(pl, action="dispute_error", error_id=err.pk)
        err.refresh_from_db()
        self.assertFalse(err.disputed)
        self.assertEqual(err.corrected_at, corrected_at)

    def test_search_jumps_to_an_exact_list_number(self):
        pl = _make_list("PL-8836", hours_ago=1)
        pl.issue_invoice("F-1", self.user)
        resp = self.client.get(reverse("tracker:queue") + "?q=pl 8836")
        self.assertRedirects(
            resp, reverse("tracker:picking_list_detail", args=["PL-8836"])
        )

    def test_invoicing_queue_offers_bulk_controls(self):
        _make_list("PL-1", hours_ago=1)
        resp = self.client.get(reverse("tracker:queue") + "?view=invoicing")
        self.assertContains(resp, "Tomar seleccionadas")
        self.assertContains(resp, 'name="lists"', count=2)
